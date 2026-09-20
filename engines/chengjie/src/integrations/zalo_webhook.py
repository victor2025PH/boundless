"""Zalo Official Account OA OpenAPI v3.0 Webhook（Phase H：东南亚出海重点区）。

文档：
- Send：POST https://openapi.zalo.me/v3.0/oa/message/{type}（type=cs|transaction|promotion）
  header `access_token: <token>`，body {"recipient":{"user_id":...},"message":{"text":...}}。
  ⚠️ cs（客服消息）仅能发给 7 天内与 OA 互动过的用户。
- Webhook：POST，body 含 app_id / sender.id / recipient.id(OA) / event_name / timestamp /
  message.text / mac；签名头 `X-ZEvent-Signature`。事件 `user_send_text` = 用户发文字。

复用：入站镜像 + G4c 主管道开关 → `shared/official_inbound.process_official_inbound`；
出站 `zalo_send_text` 内建 G2 Kill-Switch（platform=zalo）。

config.yaml：
  zalo:
    enabled: true
    oa_id: "..."
    access_token: "..."
    oa_secret: "..."            # webhook MAC 验签；留空→跳过验签（仅开发期）
    message_type: "cs"
    webhook_path: "/zalo/webhook"
    unsupported_type_reply: "目前仅支持文字消息。"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, List

import aiohttp
from fastapi import FastAPI, Request, Response

logger = logging.getLogger(__name__)

ZALO_SEND_BASE = "https://openapi.zalo.me/v3.0/oa/message"
ZALO_TEXT_MAX = 2000


def _truncate(text: str) -> str:
    s = (text or "").strip()
    return s if len(s) <= ZALO_TEXT_MAX else s[: ZALO_TEXT_MAX - 1] + "…"


def verify_zalo_signature(body: bytes, signature_header: str, oa_secret: str) -> bool:
    """校验 Zalo webhook 签名（best-effort）。

    Zalo 的 MAC 方案随版本而异（常见 `X-ZEvent-Signature` 为 HMAC-SHA256(rawBody, secret) 的 hex，
    可能带 `mac=` 前缀）。此处实现该常见方案；**确切公式须以 Zalo 控制台为准**。
    ``oa_secret`` 留空 → 返回 False（由调用方决定是否在未配置 secret 时放行）。
    """
    if not oa_secret or not signature_header:
        return False
    provided = signature_header.strip()
    if provided.lower().startswith("mac="):
        provided = provided[4:]
    expected = hmac.new(oa_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


def extract_zalo_messages(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解析 Zalo webhook → [{sender, text, msg_id}]（仅 user_send_text）。"""
    if str(body.get("event_name") or "") != "user_send_text":
        return []
    sender = str((body.get("sender") or {}).get("id") or "")
    msg = body.get("message") or {}
    text = (msg.get("text") or "").strip()
    if not (sender and text):
        return []
    return [{"sender": sender, "text": text, "msg_id": str(msg.get("msg_id") or "")}]


#: Zalo 媒体事件名 → 收件箱媒体类型（与 official_inbound.media_placeholder 词表对齐）
_ZALO_MEDIA_EVENTS = {
    "user_send_image": "image", "user_send_sticker": "sticker",
    "user_send_gif": "gif", "user_send_audio": "audio",
    "user_send_video": "video", "user_send_file": "file",
    "user_send_location": "location",
}


def extract_zalo_media(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解析 Zalo 媒体事件 → ``[{sender, msg_id, media_type, url}]``。

    与 :func:`extract_zalo_messages` 刻意分离（那边有门禁钉死「仅 user_send_text」）：
    文字走回复产线，媒体只做收件箱可见化。此前这些事件被整体忽略——客户发图，
    坐席端连占位都没有。
    """
    media_type = _ZALO_MEDIA_EVENTS.get(str(body.get("event_name") or ""))
    if not media_type:
        return []
    sender = str((body.get("sender") or {}).get("id") or "")
    if not sender:
        return []
    msg = body.get("message") or {}
    url = ""
    atts = msg.get("attachments")
    if isinstance(atts, list) and atts:
        payload = (atts[0] or {}).get("payload") or {}
        url = str(payload.get("url") or payload.get("thumbnail") or "")
    return [{"sender": sender, "msg_id": str(msg.get("msg_id") or ""),
             "media_type": media_type, "url": url}]


async def zalo_send_text(
    user_id: str,
    text: str,
    access_token: str,
    *,
    message_type: str = "cs",
    account_id: str = "default",
    check_kill_switch: bool = True,
) -> Dict[str, Any]:
    """通过 Zalo OA OpenAPI 发文字。永不抛异常。G2：发送前查 Kill-Switch（platform=zalo）。"""
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("zalo", account_id or "default")
            if blocked:
                logger.warning("[zalo][kill-switch] 冻结发送，跳过（scope=%s）", scope)
                return {"ok": False, "error": f"kill_switch:{scope}"}
        except Exception:
            pass
    text = _truncate(text)
    if not text:
        return {"ok": True, "data": {"skipped": "empty"}}
    mtype = str(message_type or "cs").strip().lower() or "cs"
    url = f"{ZALO_SEND_BASE}/{mtype}"
    headers = {"access_token": access_token, "Content-Type": "application/json"}
    payload = {"recipient": {"user_id": user_id}, "message": {"text": text}}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {"raw": raw[:500]}
                # Zalo 200 也可能 body.error!=0 表示失败
                if resp.status != 200 or int((data or {}).get("error", 0) or 0) != 0:
                    logger.warning("Zalo send 失败 HTTP %s: %s", resp.status, raw[:500])
                    out: Dict[str, Any] = {
                        "ok": False, "error": f"HTTP {resp.status}: {raw[:200]}",
                        "data": data}
                    try:
                        from src.integrations.shared.official_send_error import (
                            classify_official_send_error,
                        )
                        info = classify_official_send_error(
                            "zalo", status=resp.status, body=data, error_text=raw)
                        out["error_kind"] = info["kind"]
                        out["retriable"] = info["retriable"]
                    except Exception:
                        out["error_kind"] = "unknown"
                    return out
                return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001
        logger.warning("Zalo send failed: %s", e)
        return {"ok": False, "error": str(e)}


def register_zalo_routes(
    app: FastAPI, config_manager: Any, telegram_client: Any,
) -> None:
    """挂载 Zalo POST webhook。缺 access_token → 不注册。"""
    cfg = (getattr(config_manager, "config", None) or {}).get("zalo") or {}
    if not cfg.get("enabled"):
        return
    sm = getattr(telegram_client, "skill_manager", None)
    if sm is None:
        logger.warning("Zalo 已启用但 SkillManager 不可用，跳过 Webhook")
        return

    access_token = (cfg.get("access_token") or "").strip()
    if not access_token:
        logger.error("Zalo 缺少 access_token，Webhook 未注册")
        return
    oa_secret = (cfg.get("oa_secret") or "").strip()
    oa_account_id = str(cfg.get("oa_id") or "").strip() or "official"
    message_type = str(cfg.get("message_type") or "cs").strip().lower() or "cs"
    # 显式空串 = 运营选择对媒体消息保持沉默；键缺席才落默认话术
    _raw_unsup = cfg.get("unsupported_type_reply")
    unsupported = ("目前仅支持文字消息。" if _raw_unsup is None
                   else str(_raw_unsup).strip())
    try:
        from src.integrations.official_api_worker import official_pipeline_enabled
        use_pipeline = official_pipeline_enabled(getattr(config_manager, "config", None) or {})
    except Exception:
        use_pipeline = False

    path = cfg.get("webhook_path") or "/zalo/webhook"
    if isinstance(path, str) and not path.startswith("/"):
        path = "/" + path
    app.state.zalo_webhook_path = path

    async def zalo_webhook_event(request: Request) -> Response:
        from src.integrations.official_webhook_stats import record_error, record_event
        raw = await request.body()
        # 配了 oa_secret 才验签（留空跳过——仅开发期；生产强烈建议配）
        if oa_secret:
            sig = (request.headers.get("X-ZEvent-Signature")
                   or request.headers.get("x-zevent-signature") or "")
            if not verify_zalo_signature(raw, sig, oa_secret):
                logger.warning("Zalo Webhook 签名校验失败")
                record_error("zalo", "bad_signature")
                return Response(status_code=403, content=b"invalid signature")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            record_error("zalo", "bad_json")
            return Response(status_code=400, content=b"invalid json")
        # 到达即记（Zalo 无 GET 握手，事件是唯一的可达性证据）
        record_event("zalo")

        for m in extract_zalo_messages(data):
            try:
                await _handle_zalo_message(
                    sender=m["sender"], text=m["text"], msg_id=m["msg_id"], sm=sm,
                    access_token=access_token, oa_account_id=oa_account_id,
                    message_type=message_type, use_pipeline=use_pipeline)
            except Exception as e:  # noqa: BLE001
                logger.exception("Zalo 事件处理异常: %s", e)
        # 媒体事件：镜像占位进收件箱（坐席可见可接管）+ 可选「暂不支持」回复
        for mm in extract_zalo_media(data):
            try:
                await _handle_zalo_media(
                    item=mm, access_token=access_token,
                    oa_account_id=oa_account_id, message_type=message_type,
                    unsupported=unsupported)
            except Exception as e:  # noqa: BLE001
                logger.exception("Zalo 媒体事件处理异常: %s", e)
        return Response(status_code=200, content=b"OK")

    app.add_api_route(path, zalo_webhook_event, methods=["POST"], name="zalo_webhook_event")
    logger.info("Zalo Webhook 已注册: POST %s (oa_id=%s)", path, oa_account_id)


async def _handle_zalo_media(
    *, item: Dict[str, Any], access_token: str, oa_account_id: str,
    message_type: str = "cs", unsupported: str = "",
) -> None:
    """单条 Zalo 媒体入站 → 收件箱占位镜像 + 可选「暂不支持」回复。

    与 IG/Messenger 官方链同款语义；绝不喂 SkillManager（媒体无可靠文字语义）。
    """
    sender = str(item.get("sender") or "")
    if not sender:
        return
    chat_key = f"zalo:user:{sender}"
    try:
        from src.integrations.shared.official_inbound import mirror_inbound_media
        mirror_inbound_media(
            platform="zalo", account_id=oa_account_id, chat_key=chat_key,
            media_type=str(item.get("media_type") or "file"),
            name=sender, msg_id=str(item.get("msg_id") or ""),
            media_ref=str(item.get("url") or ""))
    except Exception:  # noqa: BLE001
        logger.debug("Zalo 媒体镜像失败（已忽略）", exc_info=True)
    if unsupported:
        await zalo_send_text(sender, unsupported, access_token,
                             message_type=message_type, account_id=oa_account_id)


async def _handle_zalo_message(
    *, sender: str, text: str, msg_id: str, sm: Any, access_token: str,
    oa_account_id: str, message_type: str = "cs", use_pipeline: bool = False,
) -> None:
    """单条 Zalo 入站 → 镜像 +（管道 or 自答）。"""
    import uuid

    chat_key = f"zalo:user:{sender}"
    user_key = f"zalo:{sender}"

    from src.integrations.shared.official_inbound import (
        mirror_official_outbound, process_official_inbound,
    )
    handed = await process_official_inbound(
        platform="zalo", account_id=oa_account_id, chat_key=chat_key,
        text=text, name=sender, msg_id=msg_id, use_pipeline=use_pipeline)
    if handed:
        return

    context: Dict[str, Any] = {
        "chat_id": chat_key, "chat_title": "",
        "request_id": f"r-{uuid.uuid4().hex[:12]}",
        "channel": "zalo", "zalo_sender": sender, "zalo_message_id": msg_id,
    }
    try:
        reply_text = await sm.process_message(text=text, user_id=user_key, context=context)
    except Exception as e:  # noqa: BLE001
        logger.exception("Zalo process_message 异常: %s", e)
        return
    if reply_text:
        await zalo_send_text(sender, str(reply_text), access_token,
                             message_type=message_type, account_id=oa_account_id)
        await mirror_official_outbound(
            platform="zalo", account_id=oa_account_id, chat_key=chat_key,
            text=str(reply_text))


__all__ = [
    "zalo_send_text", "verify_zalo_signature", "extract_zalo_messages",
    "extract_zalo_media", "register_zalo_routes",
]
