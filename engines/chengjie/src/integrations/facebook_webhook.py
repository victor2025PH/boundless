"""Facebook Page Messenger Webhook（Graph API v25 兼容）。

文档：
- Webhooks: https://developers.facebook.com/docs/messenger-platform/webhooks/
- messages event: https://developers.facebook.com/docs/messenger-platform/reference/webhook-events/messages/
- Send API: https://developers.facebook.com/docs/messenger-platform/reference/send-api/

★ 设计原则 ★
- 与 line_webhook.py 同构（GET 验签 / POST 处理 / SkillManager 路由 / 24h push）
- 强制 X-Hub-Signature-256 校验，避免被恶意 POST 伪造消息
- messaging_type=RESPONSE 在 24h window 内回，过期自动降级为 MESSAGE_TAG
- 不主动外发，所有出站消息都是被用户激活后的 reply/push
- echo / delivery / read 事件直接 ack，不喂给 SkillManager
- 路由**常驻挂载、按实时配置热门控**（2026-08-10）：旧行为是启动时 enabled+三凭证
  齐全才注册路由——接入向导保存凭证后出站 worker 已热注册、入站 webhook 却要等
  下次重启才存在（「出站热、入站冷」半截生效态，Meta 侧 Callback 校验 404）。
  现在路由始终在，未启用时 GET/POST 一律 403；签名硬拒不变（空 app_secret 拒收）

config.yaml 示例：
  facebook_messenger:
    enabled: true
    page_id: "1234567890"
    page_access_token: "EAAxxxxxxxx..."
    app_secret: "abcdef0123456789"           # 用于 X-Hub-Signature-256 校验
    verify_token: "your-verify-token-1234"   # GET 校验自定义口令
    webhook_path: "/fb/webhook"
    fallback_message_tag: "ACCOUNT_UPDATE"   # 24h 外回退 tag
    unsupported_type_reply: "目前仅支持文字消息。"
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

import aiohttp
from fastapi import FastAPI, Query, Request, Response

logger = logging.getLogger(__name__)

# Graph API 默认版本（v25 是 2026-Q1 的稳定版）
GRAPH_API_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SEND_API_URL = f"{GRAPH_BASE}/me/messages"
FB_TEXT_MAX = 1900  # 实际限 2000 字符，留余量给 emoji 编码膨胀


def _truncate(text: str) -> str:
    s = (text or "").strip()
    if len(s) <= FB_TEXT_MAX:
        return s
    return s[: FB_TEXT_MAX - 1] + "…"


def verify_fb_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """校验 X-Hub-Signature-256（FB 推送时签的 HMAC-SHA256）。

    格式：'sha256=<hex>'。空 secret 时硬拒（绝不允许放行未签名请求）。
    """
    if not app_secret or not signature_header:
        return False
    sig = signature_header.strip()
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    provided = sig[len("sha256="):]
    return hmac.compare_digest(expected, provided)


# Page Token 失效特征（Graph 多以 HTTP 400 + OAuthException/code 190 报鉴权错，
# 不能只看 401）：命中即视为「凭证坏了」而非普通发送失败。
_TOKEN_FAIL_MARKERS = (
    "oauthexception",
    "error validating access token",
    "invalid oauth access token",
    "session has expired",
    "has not authorized application",
)


def _looks_like_token_failure(status: int, body: str) -> bool:
    """纯函数：Graph 错误响应是否指向 Page Access Token 失效/吊销。"""
    if int(status or 0) == 401:
        return True
    low = (body or "").lower()
    if '"code":190' in low.replace(" ", ""):
        return True
    return any(m in low for m in _TOKEN_FAIL_MARKERS)


def _maybe_alert_page_token(status: int, body: str) -> None:
    """Page Token 失效 → host_alert（日志 + EventBus 镜像 + 算力机弹窗，6h 去抖）。

    这类凭证坏掉的默认形态是**静默**：token 被吊销/过期后，所有官方通道出站只在
    WARNING 日志里积灰，坐席与机主零感知，客户消息有来无回。复用 host_alert 出口
    使其与「云端 Key 失效」同一告警面；恢复无需动作（换 token 后自然不再触发）。
    """
    try:
        if not _looks_like_token_failure(status, body):
            return
        from src.utils.host_alert import notify_host
        notify_host(
            "Messenger Page Token 异常",
            "Facebook Page Access Token 疑似失效或被吊销（HTTP "
            f"{status}）。官方通道出站已受影响，请到「接入向导」更新凭证。\n"
            f"详情: {(body or '')[:200]}",
            key="fb_page_token", cooldown_sec=6 * 3600.0,
        )
    except Exception:
        pass


def parse_page_probe(status: int, body: str) -> Dict[str, Any]:
    """Graph ``/me`` 探针响应 → 结构化结论（纯函数，供向导保存探针/凭证体检复用）。

    成功时带回主页身份（page_id/name/picture）——page_id 从「要用户去 Meta 后台
    抄」变成「token 自己说」，向导可自动回填；失败区分 auth（token 坏）与 http。
    """
    try:
        data = json.loads(body or "")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if int(status or 0) == 200 and data.get("id"):
        try:
            pic = str(((data.get("picture") or {}).get("data") or {}).get("url") or "")
        except Exception:
            pic = ""
        return {"ok": True, "page_id": str(data.get("id") or ""),
                "name": str(data.get("name") or ""), "picture": pic}
    err = str(((data.get("error") or {}).get("message")) or "") or f"HTTP {status}"
    etype = str(((data.get("error") or {}).get("type")) or "").lower()
    kind = "auth" if (int(status or 0) in (401, 403) or "oauth" in etype
                      or _looks_like_token_failure(status, body)) else "http"
    return {"ok": False, "error": err, "error_kind": kind}


async def fb_probe_page(
    page_access_token: str, *, timeout_sec: float = 8.0,
) -> Dict[str, Any]:
    """用 Page Token 打 Graph ``/me`` 验证凭证并带回主页身份。

    供「接入向导保存前探针」使用：token 抄错一个字符，旧链路要等第一次真实出站
    失败才暴露；这里 8 秒内给确定性结论。纯出站 egress（LAN 部署无公网入口也能
    跑）；永不抛异常。
    """
    tok = (page_access_token or "").strip()
    if not tok:
        return {"ok": False, "error": "empty token", "error_kind": "bad_request"}
    params = {"access_token": tok, "fields": "id,name,picture{url}"}
    try:
        timeout = aiohttp.ClientTimeout(total=max(2.0, float(timeout_sec)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{GRAPH_BASE}/me", params=params) as resp:
                body = await resp.text()
                return parse_page_probe(resp.status, body)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "error_kind": "network"}


async def fb_send_message(
    psid: str,
    text: str,
    page_access_token: str,
    *,
    messaging_type: str = "RESPONSE",
    message_tag: Optional[str] = None,
    account_id: str = "default",
    check_kill_switch: bool = True,
) -> Dict[str, Any]:
    """通过 Send API 发文字消息。

    返回 {"ok": True, "data": ...} 或 {"ok": False, "error": "..."}。
    永不抛异常。

    G2：发送前查 Kill-Switch（platform=messenger；global/platform:messenger/account:messenger:<page>）。

    messaging_type:
      - RESPONSE: 24h 内回复（**默认**，要求用户最近 24h 主动给 Page 发过消息）
      - UPDATE: 不要求用户互动，但内容受限
      - MESSAGE_TAG: 24h 外发，必须带合法 tag（CONFIRMED_EVENT_UPDATE/POST_PURCHASE_UPDATE/ACCOUNT_UPDATE/HUMAN_AGENT）
    """
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("messenger", account_id or "default")
            if blocked:
                logger.warning("[fb][kill-switch] 冻结发送，跳过（scope=%s）", scope)
                return {"ok": False, "error": f"kill_switch:{scope}"}
        except Exception:
            pass
    text = _truncate(text)
    if not text:
        return {"ok": True, "data": {"skipped": "empty"}}
    payload: Dict[str, Any] = {
        "recipient": {"id": psid},
        "message": {"text": text},
        "messaging_type": messaging_type,
    }
    if message_tag and messaging_type == "MESSAGE_TAG":
        payload["tag"] = message_tag
    params = {"access_token": page_access_token}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                SEND_API_URL, params=params, json=payload
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning(
                        "FB send_message HTTP %s: %s", resp.status, body[:500]
                    )
                    _maybe_alert_page_token(resp.status, body)
                    return {
                        "ok": False,
                        "error": f"HTTP {resp.status}: {body[:200]}",
                    }
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body[:500]}
                return {"ok": True, "data": data}
    except Exception as e:
        logger.warning("FB send_message failed: %s", e)
        return {"ok": False, "error": str(e)}


async def fb_send_with_window_fallback(
    psid: str,
    text: str,
    page_access_token: str,
    *,
    fallback_tag: str = "ACCOUNT_UPDATE",
    account_id: str = "default",
) -> Dict[str, Any]:
    """优先用 RESPONSE 发；若返回 24h window 错误（10:2534022），
    自动降级用 MESSAGE_TAG=fallback_tag 重发。``account_id`` 透传给 Kill-Switch 账号级作用域。"""
    out = await fb_send_message(
        psid, text, page_access_token, messaging_type="RESPONSE",
        account_id=account_id,
    )
    if out.get("ok"):
        return out
    err = str(out.get("error") or "")
    if "2534022" in err or "outside of allowed window" in err.lower():
        logger.info("FB 24h 窗口已关闭，降级 tag=%s 重发", fallback_tag)
        return await fb_send_message(
            psid,
            text,
            page_access_token,
            messaging_type="MESSAGE_TAG",
            message_tag=fallback_tag,
            account_id=account_id,
        )
    return out


async def fb_send_attachment(
    psid: str,
    media_url: str,
    page_access_token: str,
    *,
    media_type: str = "audio",
    messaging_type: str = "RESPONSE",
    account_id: str = "default",
    check_kill_switch: bool = True,
) -> Dict[str, Any]:
    """通过 Send API 发媒体附件（audio/image/video/file，按 URL）。

    Messenger 接受**公网可达 URL**附件（FB 自取），故 ``media_url`` 须为 https 公网链接。
    返回 {"ok": True, "data": ...} 或 {"ok": False, "error"[, "error_kind"]}；永不抛。
    """
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("messenger", account_id or "default")
            if blocked:
                logger.warning("[fb][kill-switch] 冻结媒体发送，跳过（scope=%s）", scope)
                return {"ok": False, "error": f"kill_switch:{scope}"}
        except Exception:
            pass
    if not media_url or not str(media_url).lower().startswith("https://"):
        return {"ok": False, "error": f"attachment needs https url: {media_url}",
                "error_kind": "no_public_url"}
    mt = str(media_type or "").lower()
    att_type = {"voice": "audio", "audio": "audio", "image": "image",
                "video": "video"}.get(mt, "file")
    payload: Dict[str, Any] = {
        "recipient": {"id": psid},
        "message": {
            "attachment": {
                "type": att_type,
                "payload": {"url": media_url, "is_reusable": False},
            }
        },
        "messaging_type": messaging_type,
    }
    params = {"access_token": page_access_token}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                SEND_API_URL, params=params, json=payload
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning("FB send_attachment HTTP %s: %s", resp.status, body[:500])
                    _maybe_alert_page_token(resp.status, body)
                    return {"ok": False, "error": f"HTTP {resp.status}: {body[:200]}"}
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body[:500]}
                return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001
        logger.warning("FB send_attachment failed: %s", e)
        return {"ok": False, "error": str(e), "error_kind": "network"}


# 出站媒体 ext → mime（multipart 上传需精确 mime）。
_FB_MIME_BY_EXT: Dict[str, str] = {
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".mp4": "video/mp4",
}


def _fb_attachment_type(media_type: str) -> str:
    return {"voice": "audio", "audio": "audio", "image": "image",
            "video": "video"}.get(str(media_type or "").lower(), "file")


async def fb_send_attachment_upload(
    psid: str,
    media_path: str,
    page_access_token: str,
    *,
    media_type: str = "audio",
    messaging_type: str = "RESPONSE",
    account_id: str = "default",
    check_kill_switch: bool = True,
) -> Dict[str, Any]:
    """通过 Send API 以 multipart ``filedata`` **上传本地字节**发媒体附件（**免公网 URL**）。

    优于 URL 式（``fb_send_attachment``）：无需把音频托管到公网即可发——与 WhatsApp Cloud
    上传式对齐。返回 {"ok": True, "data": ...} 或 {"ok": False, "error"[, "error_kind"]}；永不抛。
    """
    import os
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("messenger", account_id or "default")
            if blocked:
                logger.warning("[fb][kill-switch] 冻结媒体上传，跳过（scope=%s）", scope)
                return {"ok": False, "error": f"kill_switch:{scope}"}
        except Exception:
            pass
    if not media_path or not os.path.isfile(media_path):
        return {"ok": False, "error": f"file not found: {media_path}",
                "error_kind": "bad_request"}
    att_type = _fb_attachment_type(media_type)
    ext = os.path.splitext(media_path)[1].lower()
    mime = _FB_MIME_BY_EXT.get(ext, "application/octet-stream")
    recipient = json.dumps({"id": psid})
    message = json.dumps({"attachment": {"type": att_type,
                                         "payload": {"is_reusable": False}}})
    params = {"access_token": page_access_token}
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        with open(media_path, "rb") as fh:
            form = aiohttp.FormData()
            form.add_field("recipient", recipient)
            form.add_field("message", message)
            form.add_field("messaging_type", messaging_type)
            form.add_field("filedata", fh, filename=os.path.basename(media_path),
                           content_type=mime)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(SEND_API_URL, params=params, data=form) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.warning("FB attachment upload HTTP %s: %s",
                                       resp.status, body[:500])
                        _maybe_alert_page_token(resp.status, body)
                        return {"ok": False, "error": f"HTTP {resp.status}: {body[:200]}"}
                    try:
                        data = json.loads(body)
                    except Exception:
                        data = {"raw": body[:500]}
                    return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001
        logger.warning("FB attachment upload failed: %s", e)
        return {"ok": False, "error": str(e), "error_kind": "network"}


def _extract_messaging_events(body: Dict[str, Any]) -> list:
    """FB Webhook 顶层结构：{object, entry:[{id,time,messaging:[...]}]}。"""
    if str(body.get("object") or "") != "page":
        return []
    out = []
    for entry in body.get("entry") or []:
        page_id = str(entry.get("id") or "")
        for ev in entry.get("messaging") or []:
            ev["_page_id"] = page_id
            out.append(ev)
    return out


def register_fb_messenger_routes(
    app: FastAPI,
    config_manager: Any,
    telegram_client: Any,
) -> None:
    """挂载 GET/POST /fb/webhook（路径取注册时配置，默认 /fb/webhook）。

    **常驻挂载 + 实时门控**：开关/凭证每个请求从 ``config_manager.config`` 现读，
    接入向导保存凭证（写 overlay 后深合并进同一 config 对象）即全链热生效——与
    出站 worker 的 ``ensure_builtin_workers`` 热注册同一节奏，消灭「出站热、入站
    冷」的半截生效态。仅 ``webhook_path`` 钉在注册时刻（改路径
    意味着 Meta 后台也要同步改，属重启级运维动作）。``official_pipeline_enabled``
    （G4c 主管道开关）同样逐请求现读，随 overlay 热切。

    SkillManager 于**请求期**经 ``resolve_skill_manager`` 解析（telegram_client →
    app.state 双兜底）：注册发生在 create_app 期间，``app.state.skill_manager``
    彼时尚未挂载；协议号未配置的部署 ``telegram_client`` 本身就是 None（2026-08-10
    搭车验证实锤：注册期取 SkillManager 拿不到 → 路由整个没挂 → /fb/webhook 404）。
    请求期两条通路必有一条就绪；极端仍取不到 → 503 让 Meta 稍后重投。
    """
    def _live_cfg() -> Dict[str, Any]:
        return (
            getattr(config_manager, "config", None) or {}
        ).get("facebook_messenger") or {}

    path = _live_cfg().get("webhook_path") or "/fb/webhook"
    if isinstance(path, str) and not path.startswith("/"):
        path = "/" + path
    app.state.fb_webhook_path = path

    # ── GET：FB 平台校验回调（hub.mode=subscribe + hub.verify_token + hub.challenge）
    async def fb_webhook_verify(
        request: Request,
        hub_mode: Optional[str] = Query(None, alias="hub.mode"),
        hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
        hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    ) -> Response:
        from src.integrations.official_webhook_stats import record_verify
        cfg = _live_cfg()
        if not cfg.get("enabled"):
            # 渠道未启用：只回拒绝，不记账（公网扫描噪声不该污染握手统计）
            return Response(status_code=403, content=b"channel disabled")
        if hub_mode != "subscribe":
            # 裸 GET / 扫描噪声不算握手尝试，不记账
            return Response(status_code=400, content=b"bad mode")
        verify_token = (cfg.get("verify_token") or "").strip()
        if not verify_token or (hub_verify_token or "") != verify_token:
            logger.warning("FB Webhook verify_token 不匹配")
            record_verify("messenger", ok=False)
            return Response(status_code=403, content=b"forbidden")
        record_verify("messenger", ok=True)
        # 必须原样返回 challenge
        return Response(
            status_code=200, content=(hub_challenge or "").encode("utf-8")
        )

    # ── POST：真实事件
    async def fb_webhook_event(request: Request) -> Response:
        from src.integrations.official_webhook_stats import record_error, record_event
        cfg = _live_cfg()
        if not cfg.get("enabled"):
            return Response(status_code=403, content=b"channel disabled")
        raw = await request.body()
        sig = (
            request.headers.get("X-Hub-Signature-256")
            or request.headers.get("x-hub-signature-256")
            or ""
        )
        app_secret = (cfg.get("app_secret") or "").strip()
        if not app_secret:
            # 启用但缺 app_secret＝配置残缺而非攻击：单独记一类账（与 bad_signature
            # 区分开），运维在 webhook 状态里能看出「该去补 App Secret」而不是疑心被打
            logger.warning("FB Webhook 已启用但未配置 app_secret，入站事件拒收")
            record_error("messenger", "no_app_secret")
            return Response(status_code=403, content=b"app_secret not configured")
        if not verify_fb_signature(raw, sig, app_secret):
            logger.warning("FB Webhook 签名校验失败")
            record_error("messenger", "bad_signature")
            return Response(status_code=403, content=b"invalid signature")

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            record_error("messenger", "bad_json")
            return Response(status_code=400, content=b"invalid json")
        # SkillManager 请求期解析（telegram_client → app.state 双兜底，见函数
        # docstring）。刻意放在 record_event **之前**：未就绪回 503，Meta 会重投
        # 同一批事件——先记账再 503 会让重投把到达数翻倍。
        try:
            from src.web.web_context import resolve_skill_manager
            sm = resolve_skill_manager(telegram_client, request.app)
        except Exception:
            sm = getattr(telegram_client, "skill_manager", None)
        if sm is None:
            logger.warning("FB Webhook 事件到达但 SkillManager 未就绪，回 503 待重投")
            record_error("messenger", "sm_unready")
            return Response(status_code=503, content=b"skill manager not ready")
        # 到达即记（验签已过）：单事件处理失败不影响「回调可达」事实
        record_event("messenger")

        page_token = (cfg.get("page_access_token") or "").strip()
        page_id = str(cfg.get("page_id") or "").strip()
        fallback_tag = str(cfg.get("fallback_message_tag") or "ACCOUNT_UPDATE")
        unsupported = (cfg.get("unsupported_type_reply") or "").strip() or (
            "目前仅支持文字消息。"
        )
        try:
            from src.integrations.official_api_worker import official_pipeline_enabled
            use_pipeline = official_pipeline_enabled(
                getattr(config_manager, "config", None) or {})
        except Exception:
            use_pipeline = False

        events = _extract_messaging_events(data)
        for ev in events:
            try:
                await _handle_one_event(
                    ev=ev,
                    sm=sm,
                    page_token=page_token,
                    fallback_tag=fallback_tag,
                    unsupported=unsupported,
                    page_id_filter=page_id,
                    use_pipeline=use_pipeline,
                )
            except Exception as e:
                logger.exception("FB 事件处理异常: %s", e)
                # 单事件失败不影响 200 ack（FB 会重发整个 batch）

        return Response(status_code=200, content=b"OK")

    app.add_api_route(
        path,
        fb_webhook_verify,
        methods=["GET"],
        name="fb_messenger_webhook_verify",
    )
    app.add_api_route(
        path,
        fb_webhook_event,
        methods=["POST"],
        name="fb_messenger_webhook_event",
    )
    logger.info(
        "FB Messenger Webhook 已挂载（热门控）: GET %s + POST %s", path, path,
    )


async def _handle_one_event(
    *,
    ev: Dict[str, Any],
    sm: Any,
    page_token: str,
    fallback_tag: str,
    unsupported: str,
    page_id_filter: str,
    use_pipeline: bool = False,
) -> None:
    """单条 messaging 事件路由。"""
    page_id = str(ev.get("_page_id") or "")
    if page_id_filter and page_id and page_id != page_id_filter:
        # 同一 App 监听了多个 Page 时可以过滤
        return

    # echo 是 Page 自己发出的回声，绝对不能再喂 SkillManager（会无限自答）
    msg = ev.get("message") or {}
    if msg.get("is_echo"):
        return
    if "delivery" in ev or "read" in ev or "reaction" in ev:
        return

    sender_id = str((ev.get("sender") or {}).get("id") or "")
    if not sender_id:
        return

    # 如果 sender 就是本 Page（少见，但出现过），跳过
    if sender_id == page_id:
        return

    # 文字消息
    text = (msg.get("text") or "").strip()
    if not text:
        # 附件 / sticker 等
        atts = msg.get("attachments")
        if atts:
            # Phase I1：入站媒体可见化——先镜像占位（坐席台看到「[图片]」等并可接管），再回不支持
            try:
                from src.integrations.shared.official_inbound import (
                    meta_attachment_url, mirror_inbound_media,
                )
                _first = (atts[0] or {}) if isinstance(atts, list) and atts else {}
                _atype = str(_first.get("type") or "file")
                mirror_inbound_media(
                    platform="messenger", account_id=(page_id or "official"),
                    chat_key=f"fb:user:{sender_id}", media_type=_atype,
                    name=sender_id, msg_id=str(msg.get("mid") or ""),
                    media_ref=meta_attachment_url(atts))
            except Exception:
                pass
            await fb_send_with_window_fallback(
                sender_id,
                unsupported,
                page_token,
                fallback_tag=fallback_tag,
                account_id=(page_id or "official"),
            )
        return

    chat_key = f"fb:user:{sender_id}"
    user_key = f"fb:{sender_id}"
    req_id = f"r-{uuid.uuid4().hex[:12]}"
    _mirror_acct = page_id or "official"

    # Phase G4：入站镜像进统一收件箱（旁路，坐席台可见/可接管）
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mirror_to_inbox("messenger", _mirror_acct, chat_key, text,
                        direction="in", name=sender_id, msg_id=str(msg.get("mid") or ""))
    except Exception:
        pass

    # Phase A：auto_ai 让位——交统一收件箱 autosend(System Z) 全自动接管
    # （人设+语言+风控+拟人延迟，与 Telegram 同一条），跳过自答/管道避免双发。
    try:
        from src.integrations.shared.official_inbound import inbox_will_autosend
        if inbox_will_autosend("messenger", _mirror_acct, chat_key):
            return
    except Exception:
        pass

    # Phase G4c：走主管道 → maybe_auto_reply（护栏/canary/记忆），回复经 orch.send→官方 worker；不在此自答。
    if use_pipeline:
        try:
            from src.integrations.protocol_bridge import make_message, maybe_auto_reply
            await maybe_auto_reply(make_message(
                platform="messenger", account_id=_mirror_acct, chat_key=chat_key,
                text=text, direction="in", name=sender_id, msg_id=str(msg.get("mid") or "")))
        except Exception:
            logger.debug("Messenger 主管道回复失败", exc_info=True)
        return

    async def _send_followup(_chat_id: Any, t: str) -> bool:
        out = await fb_send_with_window_fallback(
            sender_id, t, page_token, fallback_tag=fallback_tag
        )
        return bool(out.get("ok"))

    context: Dict[str, Any] = {
        "chat_id": chat_key,
        "chat_title": "",
        "request_id": req_id,
        "channel": "facebook_messenger",
        "fb_page_id": page_id,
        "fb_psid": sender_id,
        "fb_message_id": str(msg.get("mid") or ""),
        "fb_received_at": float(ev.get("timestamp") or time.time() * 1000) / 1000,
        "_send_to_chat": _send_followup,
    }

    try:
        reply_text = await sm.process_message(
            text=text,
            user_id=user_key,
            context=context,
        )
    except Exception as e:
        logger.exception("FB process_message 异常: %s", e)
        await fb_send_with_window_fallback(
            sender_id,
            "处理消息时出现错误，请稍后再试。",
            page_token,
            fallback_tag=fallback_tag,
        )
        return

    if reply_text:
        await fb_send_with_window_fallback(
            sender_id,
            str(reply_text),
            page_token,
            fallback_tag=fallback_tag,
            account_id=_mirror_acct,
        )
        try:
            from src.integrations.shared.inbox_mirror import mirror_to_inbox
            mirror_to_inbox("messenger", _mirror_acct, chat_key, str(reply_text),
                            direction="out")
        except Exception:
            pass
