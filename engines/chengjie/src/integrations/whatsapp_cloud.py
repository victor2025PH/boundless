"""WhatsApp Cloud API（官方）适配器 —— Phase G1。

官方 Business Cloud API：**合规、高送达、不封号**，根治 RPA/Baileys 的封号风险，
与既有 WhatsApp RPA（mode=device）/ Baileys（mode=protocol）按账号并存（mode=official）。

文档：
- Cloud API: https://developers.facebook.com/docs/whatsapp/cloud-api
- 发消息:    https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
- Webhook:   https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks

★ 设计原则（与 line_webhook.py / facebook_webhook.py 同构）★
- GET 验签（hub.mode/verify_token/challenge）/ POST 强制 X-Hub-Signature-256 校验。
- 入站文字 → SkillManager 路由 → 回发（仅被用户激活后回复，不主动外发）。
- echo/status（delivered/read）事件直接 ack，不喂 SkillManager（防自答）。
- 发送前查 Kill-Switch（global/platform:whatsapp/account:whatsapp:<phone_id>）→ 冻结即跳过。
- **24h 客服窗口**：窗口内可发自由文本；窗口外需用模板消息（template）——``wa_send_template``
  发模板；自由文本命中 ``window_expired`` 且配置了回退模板 → 自动改发模板。
  会话语言 zh/en/tl（Taglish 归 tl）从 ``by_language`` 里挑；只配顶层 ``name`` 时不分流。
  没有定时「沉默满 24h」外发，只在这次发送被拒之后回退。
- **STOP 硬闸**（2026-10-08）：入站命中 STOP → 冻结（需人工 + 名单），本条不自答；之后
  ``wa_send_text`` / ``wa_send_media`` / ``wa_send_template`` 对该客户一律不真发
  （``shared/official_stop_gate``）。
- **进待人工**：自答路径 AI 异常 / 空回复 / 发送失败 / 客户点名要真人 / 入站媒体 / 投递失败
  → 打「需人工」（``shared/official_handoff``）。
- **健康 + Meta 按条费用**：``wa_cloud_health()`` 汇总凭证/回调可达/发送成败/投递失败/
  STOP/转人工/费用（``wa_cloud_billing`` 吃 status webhook 的 ``pricing``）；
  ``wa_cloud_probe()`` 只读探 ``GET /{phone_number_id}``（不发消息）。

config.yaml 示例：
  whatsapp_cloud:
    enabled: true
    phone_number_id: "1234567890"
    access_token: "EAAxxxx..."
    app_secret: "abcdef..."            # X-Hub-Signature-256 校验
    verify_token: "your-verify-token"  # GET 校验口令
    webhook_path: "/wa/webhook"
    unsupported_type_reply: "目前仅支持文字消息。"
    window_fallback_template:                  # 可选；name 空且 by_language 无名 = 不回退
      name: ""                                 # 旧形状单模板（不按语言分流）
      language: "zh_CN"
      text_param: true
      default_language: zh
      by_language: {}                          # zh/en/tl → {name, language, text_param}
    stop_gate: {allow_farewell: false, extra_keywords: []}
    handoff: {on_human_request: true, on_media: true, on_empty_reply: true}
    pricing: {currency: USD, rates: {default: {marketing: 0.0, utility: 0.0}}}
    graph_base: ""   # 仅测试/回环：https://… 或 http://127.0.0.1:<port>；留空 = Meta 官方
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from fastapi import FastAPI, Query, Request, Response

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
WA_TEXT_MAX = 4000  # 官方上限 4096，留余量给 emoji 编码膨胀


def _truncate(text: str) -> str:
    s = (text or "").strip()
    if len(s) <= WA_TEXT_MAX:
        return s
    return s[: WA_TEXT_MAX - 1] + "…"


# ── 运行期配置（register / worker 注入；测试与回环 e2e 可改 Graph 基址）────────────
_runtime_lock = threading.Lock()
_runtime: Dict[str, Any] = {}
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _safe_graph_base(raw: Any) -> str:
    """Graph 基址覆盖只接受 https://… 或回环 http（防 access_token 被配置引到明文外网）。"""
    s = str(raw or "").strip().rstrip("/")
    if not s:
        return ""
    try:
        u = urlparse(s)
    except Exception:
        return ""
    if u.scheme == "https" and u.hostname:
        return s
    if u.scheme == "http" and (u.hostname or "") in _LOOPBACK_HOSTS:
        return s
    logger.warning("[wa_cloud] graph_base 覆盖被拒（只允许 https 或回环 http）")
    return ""


def configure_runtime(cfg: Optional[Dict[str, Any]]) -> None:
    """注入 ``whatsapp_cloud`` 配置块（窗口回退模板 / STOP 闸 / 转人工 / 计价 / Graph 基址）。"""
    c = dict(cfg or {})
    with _runtime_lock:
        _runtime.clear()
        _runtime.update(c)


def ensure_runtime(cfg: Optional[Dict[str, Any]]) -> None:
    """运行期配置为空时才注入（worker 出站用；webhook 注册已注入则不覆盖）。"""
    with _runtime_lock:
        empty = not _runtime
    if empty and cfg:
        configure_runtime(cfg)


def _rt(key: str, default: Any = None) -> Any:
    with _runtime_lock:
        v = _runtime.get(key, default)
    return default if v is None else v


def graph_base() -> str:
    """当前 Graph 基址：env ``WA_CLOUD_GRAPH_BASE`` > 配置 ``graph_base`` > 官方。"""
    return (_safe_graph_base(os.environ.get("WA_CLOUD_GRAPH_BASE"))
            or _safe_graph_base(_rt("graph_base", ""))
            or GRAPH_BASE)


def send_url(phone_number_id: str) -> str:
    return f"{graph_base()}/{phone_number_id}/messages"


# ── 发送可观测（进程内，健康面板用；无收件人/正文）──────────────────────────────
_stats_lock = threading.Lock()
_send_stats: Dict[str, Any] = {}
_status_stats: Dict[str, Any] = {}


def _empty_send_stats() -> Dict[str, Any]:
    return {"ok": 0, "fail": 0, "blocked_stop": 0, "blocked_kill_switch": 0,
            "template_ok": 0, "template_fail": 0, "window_fallback": 0,
            "by_error_kind": {}, "last_ok_ts": 0.0, "last_fail_ts": 0.0,
            "last_error_kind": "", "consecutive_fail": 0}


def _record_send(kind: str, out: Dict[str, Any]) -> None:
    try:
        with _stats_lock:
            st = _send_stats or _empty_send_stats()
            _send_stats.update(st)
            now = time.time()
            err = str(out.get("error") or "")
            if out.get("ok"):
                key = "template_ok" if kind == "template" else "ok"
                _send_stats[key] += 1
                _send_stats["last_ok_ts"] = now
                _send_stats["consecutive_fail"] = 0
                return
            if err.startswith("stop_gate:"):
                _send_stats["blocked_stop"] += 1
                return
            if err.startswith("kill_switch:"):
                _send_stats["blocked_kill_switch"] += 1
                return
            key = "template_fail" if kind == "template" else "fail"
            _send_stats[key] += 1
            ek = str(out.get("error_kind") or "unknown")
            _send_stats["by_error_kind"][ek] = int(_send_stats["by_error_kind"].get(ek) or 0) + 1
            _send_stats["last_fail_ts"] = now
            _send_stats["last_error_kind"] = ek
            _send_stats["consecutive_fail"] += 1
    except Exception:
        logger.debug("[wa_cloud] 发送计数失败（忽略）", exc_info=True)


def _record_status_event(status: Dict[str, Any]) -> None:
    try:
        state = str((status or {}).get("status") or "unknown").lower()
        with _stats_lock:
            if not _status_stats:
                _status_stats.update({"by_status": {}, "failed_codes": {}, "last_failed_ts": 0.0})
            _status_stats["by_status"][state] = int(_status_stats["by_status"].get(state) or 0) + 1
            if state == "failed":
                _status_stats["last_failed_ts"] = time.time()
                for e in (status.get("errors") or [])[:3]:
                    code = str((e or {}).get("code") or "?")
                    _status_stats["failed_codes"][code] = int(
                        _status_stats["failed_codes"].get(code) or 0) + 1
    except Exception:
        logger.debug("[wa_cloud] status 计数失败（忽略）", exc_info=True)


def send_stats_snapshot() -> Dict[str, Any]:
    with _stats_lock:
        st = dict(_send_stats or _empty_send_stats())
        st["by_error_kind"] = dict(st.get("by_error_kind") or {})
        ss = {"by_status": dict((_status_stats or {}).get("by_status") or {}),
              "failed_codes": dict((_status_stats or {}).get("failed_codes") or {}),
              "last_failed_ts": float((_status_stats or {}).get("last_failed_ts") or 0.0)}
    return {"send": st, "delivery": ss}


def reset_stats_for_tests() -> None:
    with _stats_lock:
        _send_stats.clear()
        _status_stats.clear()
    with _probe_lock:
        _last_probe.clear()


def _stop_gate_blocked(to: str, phone_number_id: str, text: str = "") -> str:
    """出站 STOP 闸：冻结 → 返回原因（调用方不发）；否则空串。"""
    try:
        from src.integrations.shared.official_stop_gate import outbound_gate
        sg = _rt("stop_gate", {}) or {}
        blocked, reason = outbound_gate(
            "whatsapp", phone_number_id, f"wa:user:{to}", text=text,
            allow_farewell=bool(sg.get("allow_farewell", False)))
        return reason if blocked else ""
    except Exception:
        logger.debug("[wa_cloud] stop gate 异常（放行）", exc_info=True)
        return ""


def _fail(platform: str, status: int, raw_body: str) -> Dict[str, Any]:
    """构造统一失败结果：分类 error_kind（窗口/token/限速…），不再只丢不透明 HTTP 串。"""
    parsed: Any = None
    try:
        parsed = json.loads(raw_body)
    except Exception:
        parsed = None
    out: Dict[str, Any] = {"ok": False, "error": f"HTTP {status}: {raw_body[:200]}"}
    try:
        from src.integrations.shared.official_send_error import (
            classify_official_send_error,
        )
        info = classify_official_send_error(
            platform, status=status, body=parsed, error_text=raw_body)
        out["error_kind"] = info["kind"]
        out["retriable"] = info["retriable"]
    except Exception:
        out["error_kind"] = "unknown"
    return out


def verify_wa_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """校验 X-Hub-Signature-256（'sha256=<hex>'）。空 secret 硬拒。"""
    if not app_secret or not signature_header:
        return False
    sig = signature_header.strip()
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig[len("sha256="):])


def _lookup_wa_conv_lang(to: str, phone_number_id: str) -> str:
    """收件箱里该客户的会话语言（``conversations.language``）。没有就空串。"""
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        store = get_inbox_store()
        if store is None or not hasattr(store, "get_conversation"):
            return ""
        cid = f"whatsapp:{phone_number_id}:wa:user:{to}"
        row = store.get_conversation(cid) or {}
        return str(row.get("language") or "")
    except Exception:
        logger.debug("[wa_cloud] 会话语言读取失败", exc_info=True)
        return ""


async def wa_send_text(
    to: str,
    text: str,
    phone_number_id: str,
    access_token: str,
    *,
    check_kill_switch: bool = True,
    window_fallback: bool = True,
    lang: str = "",
) -> Dict[str, Any]:
    """通过 Cloud API 发文字消息。返回 {ok, data} 或 {ok:False, error}；永不抛。

    ``check_kill_switch``：发送前查全局/平台/账号冻结（account_id=phone_number_id）。
    STOP 硬闸恒查（客户已停联 → ``{ok:False, error:"stop_gate:<reason>", blocked:"stop_contact"}``）。
    模板回退同样先过 STOP 闸（``wa_send_template``），停联客户不会收到窗口外模板。
    ``window_fallback``：命中 24h 窗口过期且配置了回退模板 → 改发模板。
    ``lang``：会话语言（zh/en/tl，Taglish 归 tl）。空则读收件箱 ``conversations.language``。
    只有旧的单模板配置时忽略语言。
    """
    text = _truncate(text)
    if not text:
        return {"ok": True, "data": {"skipped": "empty"}}
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("whatsapp", phone_number_id)
            if blocked:
                logger.warning("[wa_cloud][kill-switch] 冻结发送，跳过（scope=%s）", scope)
                out = {"ok": False, "error": f"kill_switch:{scope}"}
                _record_send("text", out)
                return out
        except Exception:
            logger.debug("[wa_cloud] kill-switch 查询异常（放行）", exc_info=True)
    _sg = _stop_gate_blocked(to, phone_number_id, text)
    if _sg:
        out = {"ok": False, "error": f"stop_gate:{_sg}", "blocked": "stop_contact"}
        _record_send("text", out)
        return out
    out = await _wa_post_text(to, text, phone_number_id, access_token)
    _record_send("text", out)
    if (not out.get("ok")) and window_fallback and out.get("error_kind") == "window_expired":
        from src.integrations.wa_fallback_templates import (
            prepare_text_param, runtime_edition, select_window_fallback,
        )
        block = _rt("window_fallback_template", {}) or {}
        conv_lang = str(lang or "").strip() or _lookup_wa_conv_lang(to, phone_number_id)
        chosen = select_window_fallback(block, conv_lang, edition=runtime_edition())
        if chosen.get("name"):
            params: List[str] = []
            if chosen.get("text_param", True):
                params = [prepare_text_param(text, max_chars=chosen.get("param_max_chars"))]
            fb = await wa_send_template(
                to, str(chosen["name"]), str(chosen.get("language") or "en_US"),
                phone_number_id, access_token, body_params=params,
                check_kill_switch=False)
            if fb.get("ok"):
                with _stats_lock:
                    _send_stats["window_fallback"] = int(_send_stats.get("window_fallback") or 0) + 1
                fb["window_fallback"] = True
                fb["fallback_lang"] = chosen.get("resolved_lang") or ""
                fb["fallback_source"] = chosen.get("source") or ""
                return fb
            out["window_fallback_error"] = str(fb.get("error") or "")[:200]
            out["window_fallback_template_name"] = str(chosen.get("name") or "")
    return out


async def _wa_post_text(to: str, text: str, phone_number_id: str,
                        access_token: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": str(to),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                send_url(phone_number_id), headers=headers, json=payload
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning("WA send HTTP %s: %s", resp.status, body[:500])
                    return _fail("whatsapp", resp.status, body)
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body[:500]}
                return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001
        logger.warning("WA send failed: %s", e)
        return {"ok": False, "error": str(e)}


# ── 媒体出站（Phase B：语音/图片/视频/文件，与 Telegram 出站对齐）──────────────
# WhatsApp Cloud 媒体须先 upload→media_id 再 send（不接受外链本地文件），故无需公网 URL。
# 语音条（PTT 麦克风样式）要求 audio/ogg + opus 编码；上游 send-voice 路由已转 OGG/Opus。

_WA_MIME_BY_TYPE: Dict[str, str] = {
    "voice": "audio/ogg",
    "audio": "audio/ogg",
    "image": "image/jpeg",
    "video": "video/mp4",
}
# 扩展名 → mime（上传需精确 mime，否则 Cloud API 拒收）
_WA_MIME_BY_EXT: Dict[str, str] = {
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".amr": "audio/amr",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".mp4": "video/mp4", ".pdf": "application/pdf",
}


def _wa_guess_mime(media_path: str, media_type: str) -> str:
    """优先按扩展名（上传 mime 必须精确），回退按 media_type 粗分类。"""
    import os
    ext = os.path.splitext(str(media_path or ""))[1].lower()
    if ext in _WA_MIME_BY_EXT:
        return _WA_MIME_BY_EXT[ext]
    return _WA_MIME_BY_TYPE.get(str(media_type or "").lower(), "application/octet-stream")


def _wa_send_kind(media_type: str, mime: str) -> str:
    """映射到 Cloud API message type：audio/image/video/document。"""
    mt = str(media_type or "").lower()
    if mt in ("voice", "audio") or mime.startswith("audio/"):
        return "audio"
    if mt == "image" or mime.startswith("image/"):
        return "image"
    if mt == "video" or mime.startswith("video/"):
        return "video"
    return "document"


def extract_wa_media_id(msg: Dict[str, Any]) -> Tuple[str, str]:
    """从入站非文字消息抽 (media_type, media_id)。无附件 → ("", "")。"""
    mt = str((msg or {}).get("type") or "").strip().lower()
    if mt not in ("image", "audio", "video", "sticker", "document", "voice"):
        return "", ""
    # voice 在 Cloud API 里常归 audio；保留原始 type 给占位文案
    blob = (msg or {}).get(mt) if mt != "voice" else ((msg or {}).get("audio") or (msg or {}).get("voice"))
    if not isinstance(blob, dict):
        blob = (msg or {}).get("audio") if mt == "voice" else {}
    mid = str((blob or {}).get("id") or "").strip()
    return (mt or "file"), mid


async def wa_download_media_file(
    media_id: str, access_token: str, *, media_type: str = "image",
) -> str:
    """把 Cloud API 媒体拉到本地临时文件（需 Bearer；enrich/识图用）。

    Graph 两步：``GET /{media-id}`` 取临时 url → 再带 Authorization 下载二进制。
    失败返回空串（调用方仍可镜像占位）。临时文件由消费方 / OS 清理。
    """
    import os
    import tempfile
    mid = str(media_id or "").strip()
    token = str(access_token or "").strip()
    if not (mid and token):
        return ""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            meta_url = f"{graph_base()}/{mid}"
            async with session.get(meta_url, headers=headers) as resp:
                if resp.status != 200:
                    return ""
                meta = await resp.json(content_type=None)
            dl = str((meta or {}).get("url") or "").strip()
            mime = str((meta or {}).get("mime_type") or "").strip()
            if not dl:
                return ""
            async with session.get(dl, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.read()
            if not data:
                return ""
            ext = ".bin"
            if "jpeg" in mime or "jpg" in mime:
                ext = ".jpg"
            elif "png" in mime:
                ext = ".png"
            elif "webp" in mime:
                ext = ".webp"
            elif "ogg" in mime or "opus" in mime:
                ext = ".ogg"
            elif "mp4" in mime:
                ext = ".mp4"
            elif "mpeg" in mime or "mp3" in mime:
                ext = ".mp3"
            elif media_type in ("image", "sticker"):
                ext = ".jpg"
            elif media_type in ("audio", "voice"):
                ext = ".ogg"
            fd, path = tempfile.mkstemp(prefix="wa_in_", suffix=ext)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return path
    except Exception:
        logger.debug("[wa_cloud] 入站媒体下载失败 media_id=%s", mid, exc_info=True)
        return ""


async def wa_upload_media(
    media_path: str,
    mime_type: str,
    phone_number_id: str,
    access_token: str,
) -> Dict[str, Any]:
    """上传媒体到 Cloud API，返回 {ok, media_id} 或 {ok:False, error[, error_kind]}；永不抛。"""
    import os
    if not media_path or not os.path.isfile(media_path):
        return {"ok": False, "error": f"file not found: {media_path}",
                "error_kind": "bad_request"}
    url = f"{graph_base()}/{phone_number_id}/media"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        with open(media_path, "rb") as fh:
            form = aiohttp.FormData()
            form.add_field("messaging_product", "whatsapp")
            form.add_field("type", mime_type)
            form.add_field(
                "file", fh, filename=os.path.basename(media_path),
                content_type=mime_type)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, data=form) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        logger.warning("WA media upload HTTP %s: %s", resp.status, body[:500])
                        return _fail("whatsapp", resp.status, body)
                    try:
                        data = json.loads(body)
                    except Exception:
                        data = {}
                    mid = str((data or {}).get("id") or "")
                    if not mid:
                        return {"ok": False, "error": "upload returned no media id",
                                "error_kind": "unknown"}
                    return {"ok": True, "media_id": mid}
    except Exception as e:  # noqa: BLE001
        logger.warning("WA media upload failed: %s", e)
        return {"ok": False, "error": str(e), "error_kind": "network"}


async def wa_send_media(
    to: str,
    media_path: str,
    phone_number_id: str,
    access_token: str,
    *,
    media_type: str = "",
    caption: str = "",
    check_kill_switch: bool = True,
) -> Dict[str, Any]:
    """上传并发送媒体（语音/图片/视频/文件）。返回 {ok, data} 或 {ok:False, error}；永不抛。

    语音（media_type=voice/audio，且为 ogg/opus）→ WhatsApp 呈现为语音条（PTT）。
    ``caption`` 仅 image/video/document 生效（audio 无 caption，Cloud API 不支持）。
    """
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("whatsapp", phone_number_id)
            if blocked:
                logger.warning("[wa_cloud][kill-switch] 冻结媒体发送，跳过（scope=%s）", scope)
                out = {"ok": False, "error": f"kill_switch:{scope}"}
                _record_send("media", out)
                return out
        except Exception:
            logger.debug("[wa_cloud] kill-switch 查询异常（放行）", exc_info=True)
    _sg = _stop_gate_blocked(to, phone_number_id)
    if _sg:
        out = {"ok": False, "error": f"stop_gate:{_sg}", "blocked": "stop_contact"}
        _record_send("media", out)
        return out
    out = await _wa_send_media_inner(to, media_path, phone_number_id, access_token,
                                     media_type=media_type, caption=caption)
    _record_send("media", out)
    return out


async def _wa_send_media_inner(
    to: str, media_path: str, phone_number_id: str, access_token: str, *,
    media_type: str = "", caption: str = "",
) -> Dict[str, Any]:
    mime = _wa_guess_mime(media_path, media_type)
    up = await wa_upload_media(media_path, mime, phone_number_id, access_token)
    if not up.get("ok"):
        return up
    kind = _wa_send_kind(media_type, mime)
    obj: Dict[str, Any] = {"id": up["media_id"]}
    if kind in ("image", "video", "document") and caption:
        obj["caption"] = _truncate(caption)
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": str(to),
        "type": kind,
        kind: obj,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                send_url(phone_number_id), headers=headers, json=payload
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning("WA send media HTTP %s: %s", resp.status, body[:500])
                    return _fail("whatsapp", resp.status, body)
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body[:500]}
                return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001
        logger.warning("WA send media failed: %s", e)
        return {"ok": False, "error": str(e), "error_kind": "network"}


# ── 模板消息（24h 窗口外唯一合规出站方式）──────────────────────────────────────

async def wa_send_template(
    to: str,
    template_name: str,
    language_code: str,
    phone_number_id: str,
    access_token: str,
    *,
    body_params: Optional[List[str]] = None,
    components: Optional[List[Dict[str, Any]]] = None,
    check_kill_switch: bool = True,
) -> Dict[str, Any]:
    """发已审核模板。``body_params`` = 正文 {{1}}..{{n}} 文本参数（便捷）；``components`` 原样透传
    （按钮/头部等高级用法，二者都给时以 components 为准）。返回 {ok, data} / {ok:False, error[, error_kind]}。

    STOP 硬闸恒查——营销/通知模板对已停联客户同样不发（Meta 政策 + 红线）。
    """
    name = str(template_name or "").strip()
    if not name:
        out = {"ok": False, "error": "template name required", "error_kind": "bad_request"}
        _record_send("template", out)
        return out
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked("whatsapp", phone_number_id)
            if blocked:
                out = {"ok": False, "error": f"kill_switch:{scope}"}
                _record_send("template", out)
                return out
        except Exception:
            logger.debug("[wa_cloud] kill-switch 查询异常（放行）", exc_info=True)
    _sg = _stop_gate_blocked(to, phone_number_id)
    if _sg:
        out = {"ok": False, "error": f"stop_gate:{_sg}", "blocked": "stop_contact"}
        _record_send("template", out)
        return out
    tpl: Dict[str, Any] = {"name": name, "language": {"code": str(language_code or "en_US")}}
    if components:
        tpl["components"] = list(components)
    elif body_params:
        tpl["components"] = [{"type": "body", "parameters": [
            {"type": "text", "text": _truncate(str(p))[:1024]} for p in body_params]}]
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
               "to": str(to), "type": "template", "template": tpl}
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(send_url(phone_number_id), headers=headers,
                                    json=payload) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning("WA template HTTP %s: %s", resp.status, body[:500])
                    out = _fail("whatsapp", resp.status, body)
                    _record_send("template", out)
                    return out
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"raw": body[:500]}
                out = {"ok": True, "data": data, "template": name}
                _record_send("template", out)
                return out
    except Exception as e:  # noqa: BLE001
        logger.warning("WA template send failed: %s", e)
        out = {"ok": False, "error": str(e), "error_kind": "network"}
        _record_send("template", out)
        return out


def extract_statuses(body: Dict[str, Any]) -> list:
    """webhook 顶层 → ``statuses[]``（sent/delivered/read/failed，带 pricing/conversation/errors）。"""
    if str((body or {}).get("object") or "") != "whatsapp_business_account":
        return []
    out = []
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            phone_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for st in value.get("statuses") or []:
                if isinstance(st, dict):
                    st["_phone_number_id"] = phone_id
                    out.append(st)
    return out


def inbound_text_of(msg: Dict[str, Any]) -> str:
    """入站「可读文本」：text.body；模板快捷按钮（type=button → button.text/payload）；
    交互回复（type=interactive → button_reply/list_reply 的 title）。营销退订按钮
    「Stop promotions」就是 button 类型——不归一的话 STOP 闸看不见。"""
    m = msg or {}
    mt = str(m.get("type") or "")
    if mt == "text":
        return str((m.get("text") or {}).get("body") or "").strip()
    if mt == "button":
        b = m.get("button") or {}
        return str(b.get("text") or b.get("payload") or "").strip()
    if mt == "interactive":
        it = m.get("interactive") or {}
        for k in ("button_reply", "list_reply"):
            r = it.get(k)
            if isinstance(r, dict) and (r.get("title") or r.get("id")):
                return str(r.get("title") or r.get("id") or "").strip()
    return ""


def extract_inbound_messages(body: Dict[str, Any]) -> list:
    """从 Cloud API webhook 顶层结构提取入站文字消息事件。

    结构：{object:'whatsapp_business_account', entry:[{changes:[{value:{
      metadata:{phone_number_id}, messages:[{from,id,timestamp,type,text:{body}}]}}]}]}。
    statuses（delivered/read）与非文字类型不在此返回（由调用方 ack）。
    """
    if str(body.get("object") or "") != "whatsapp_business_account":
        return []
    out = []
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            phone_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for msg in value.get("messages") or []:
                msg["_phone_number_id"] = phone_id
                out.append(msg)
    return out


def register_whatsapp_cloud_routes(
    app: FastAPI, config_manager: Any, telegram_client: Any,
) -> None:
    """挂载 GET/POST webhook（路径可配）。缺凭证 → 不注册（与 fb/line 同策略）。"""
    cfg = (getattr(config_manager, "config", None) or {}).get("whatsapp_cloud") or {}
    if not cfg.get("enabled"):
        return

    sm = getattr(telegram_client, "skill_manager", None)
    if sm is None:
        logger.warning("WhatsApp Cloud 已启用但 SkillManager 不可用，跳过 Webhook")
        return

    phone_number_id = str(cfg.get("phone_number_id") or "").strip()
    access_token = str(cfg.get("access_token") or "").strip()
    app_secret = str(cfg.get("app_secret") or "").strip()
    verify_token = str(cfg.get("verify_token") or "").strip()
    if not (phone_number_id and access_token and app_secret and verify_token):
        logger.error(
            "WhatsApp Cloud 缺 phone_number_id/access_token/app_secret/verify_token，Webhook 未注册")
        return

    unsupported = (cfg.get("unsupported_type_reply") or "").strip() or "目前仅支持文字消息。"
    configure_runtime(cfg)
    try:
        from src.integrations.official_api_worker import official_pipeline_enabled
        wa_use_pipeline = official_pipeline_enabled(
            getattr(config_manager, "config", None) or {})
    except Exception:
        wa_use_pipeline = False
    path = cfg.get("webhook_path") or "/wa/webhook"
    if isinstance(path, str) and not path.startswith("/"):
        path = "/" + path
    app.state.wa_cloud_webhook_path = path

    async def wa_webhook_verify(
        request: Request,
        hub_mode: Optional[str] = Query(None, alias="hub.mode"),
        hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
        hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    ) -> Response:
        from src.integrations.official_webhook_stats import record_verify
        if hub_mode != "subscribe":
            # 裸 GET / 扫描噪声不算握手尝试，不记账
            return Response(status_code=400, content=b"bad mode")
        if (hub_verify_token or "") != verify_token:
            logger.warning("WA Webhook verify_token 不匹配")
            record_verify("whatsapp", ok=False)
            return Response(status_code=403, content=b"forbidden")
        record_verify("whatsapp", ok=True)
        return Response(status_code=200, content=(hub_challenge or "").encode("utf-8"))

    async def wa_webhook_event(request: Request) -> Response:
        from src.integrations.official_webhook_stats import record_error, record_event
        raw = await request.body()
        sig = (request.headers.get("X-Hub-Signature-256")
               or request.headers.get("x-hub-signature-256") or "")
        if not verify_wa_signature(raw, sig, app_secret):
            logger.warning("WA Webhook 签名校验失败")
            record_error("whatsapp", "bad_signature")
            return Response(status_code=403, content=b"invalid signature")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            record_error("whatsapp", "bad_json")
            return Response(status_code=400, content=b"invalid json")
        # 到达即记（验签已过）：单条处理失败不影响「回调可达」事实
        record_event("whatsapp")
        for st in extract_statuses(data):
            try:
                _handle_status(st, phone_number_id=phone_number_id)
            except Exception as e:  # noqa: BLE001
                logger.exception("WA status 处理异常: %s", e)
        for msg in extract_inbound_messages(data):
            try:
                await _handle_one_message(
                    msg=msg, sm=sm, phone_number_id=phone_number_id,
                    access_token=access_token, unsupported=unsupported,
                    use_pipeline=wa_use_pipeline,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("WA 事件处理异常: %s", e)
        return Response(status_code=200, content=b"OK")

    app.add_api_route(path, wa_webhook_verify, methods=["GET"],
                      name="whatsapp_cloud_webhook_verify")
    app.add_api_route(path, wa_webhook_event, methods=["POST"],
                      name="whatsapp_cloud_webhook_event")
    logger.info("WhatsApp Cloud Webhook 已注册: GET/POST %s (phone_id=%s)",
                path, phone_number_id)


def _handle_status(st: Dict[str, Any], *, phone_number_id: str) -> None:
    """status 事件：投递计数 + Meta 按条计费落账 + 投递失败转人工（不喂 SkillManager）。"""
    _record_status_event(st)
    try:
        from src.integrations.wa_cloud_billing import record_status
        record_status(st, phone_number_id=str(st.get("_phone_number_id") or phone_number_id),
                      pricing_cfg=_rt("pricing", {}) or {})
    except Exception:
        logger.debug("[wa_cloud] 计费落账失败", exc_info=True)
    if str(st.get("status") or "").lower() == "failed" and \
            bool((_rt("handoff", {}) or {}).get("on_delivery_failed", True)):
        rid = str(st.get("recipient_id") or "")
        if rid:
            codes = [str((e or {}).get("code") or "") for e in (st.get("errors") or [])][:3]
            from src.integrations.shared.official_handoff import tag_official_handoff
            reason = "window_expired" if "131047" in codes else "delivery_failed"
            tag_official_handoff("whatsapp", phone_number_id, f"wa:user:{rid}", reason,
                                 hits=[c for c in codes if c])


def _handoff_cfg(key: str, default: bool = True) -> bool:
    return bool((_rt("handoff", {}) or {}).get(key, default))


async def _handle_one_message(
    *, msg: Dict[str, Any], sm: Any, phone_number_id: str,
    access_token: str, unsupported: str, use_pipeline: bool = False,
) -> None:
    """单条入站消息：STOP 闸 → 镜像 → 转人工判定 → 管道 / SkillManager 自答 → 回发。"""
    from src.integrations.shared.official_handoff import (
        human_request_hits, tag_official_handoff,
    )
    sender = str(msg.get("from") or "")
    if not sender:
        return
    chat_key = f"wa:user:{sender}"
    mtype = str(msg.get("type") or "")
    text = inbound_text_of(msg)
    if mtype not in ("text", "button", "interactive") or not text:
        if mtype in ("text", "button", "interactive"):
            return
        # Phase I1：入站媒体可见化——镜像占位（坐席台可见/可接管），再回不支持。
        # P2：尽力把 Cloud 媒体拉到本地路径写入 media_ref，供识图/转写链消费
        # （Cloud CDN 必须带 Bearer，不能只塞远程 URL 给 remote_fetch）。
        try:
            from src.integrations.shared.official_inbound import mirror_inbound_media
            _mt, _mid = extract_wa_media_id(msg)
            _ref = ""
            if _mid:
                _ref = await wa_download_media_file(
                    _mid, access_token, media_type=_mt or "image")
            mirror_inbound_media(
                platform="whatsapp", account_id=phone_number_id,
                chat_key=chat_key,
                media_type=_mt or str(msg.get("type") or "file"),
                name=sender, msg_id=str(msg.get("id") or ""),
                media_ref=_ref)
        except Exception:
            logger.debug("[wa_cloud] 入站媒体镜像失败", exc_info=True)
        if mtype in ("reaction", "sticker", "unsupported", "system"):
            return
        if _handoff_cfg("on_media"):
            tag_official_handoff("whatsapp", phone_number_id, chat_key, "media_inbound",
                                 hits=[mtype])
        # 停联客户发来媒体：只镜像 + 转人工，不回「仅支持文字」
        from src.integrations.shared.official_stop_gate import is_stopped
        if is_stopped("whatsapp", phone_number_id, chat_key):
            return
        await wa_send_text(sender, unsupported, phone_number_id, access_token)
        return

    user_key = f"wa:{sender}"
    req_id = f"r-{uuid.uuid4().hex[:12]}"

    # Phase G4：入站镜像进统一收件箱（旁路，坐席台可见/可接管）
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mirror_to_inbox("whatsapp", phone_number_id, chat_key, text,
                        direction="in", name=sender, msg_id=str(msg.get("id") or ""))
    except Exception:
        logger.debug("[wa_cloud] 入站镜像失败", exc_info=True)

    # STOP 硬闸（镜像之后：坐席要看得到客户说了 STOP）——命中/已冻结 → 不自答、不进管道。
    from src.integrations.shared.official_stop_gate import inbound_gate
    _sg_cfg = _rt("stop_gate", {}) or {}
    _kw = None
    if _sg_cfg.get("extra_keywords"):
        from src.integrations.shared.official_stop_gate import DEFAULT_STOP_KEYWORDS
        _kw = list(DEFAULT_STOP_KEYWORDS) + [str(k) for k in _sg_cfg.get("extra_keywords") or []]
    gate = inbound_gate("whatsapp", phone_number_id, chat_key, text, name=sender, keywords=_kw,
                        allow_resubscribe=bool(_sg_cfg.get("allow_resubscribe", True)))
    if gate.get("action") != "pass":
        logger.info("[wa_cloud] STOP 闸 action=%s，本条不自答", gate.get("action"))
        return

    # 客户点名要真人 → 进待人工，AI 不抢答
    if _handoff_cfg("on_human_request"):
        _hr = human_request_hits(text)
        if _hr:
            tag_official_handoff("whatsapp", phone_number_id, chat_key, "human_request", hits=_hr)
            return

    # Phase A：auto_ai 让位——该会话由统一收件箱 autosend(System Z) 全自动接管
    # （人设+语言+风控+拟人延迟，与 Telegram 同一条），跳过自答/管道避免双发。
    try:
        from src.integrations.shared.official_inbound import inbox_will_autosend
        if inbox_will_autosend("whatsapp", phone_number_id, chat_key):
            return
    except Exception:
        logging.getLogger(__name__).debug("swallowed in _handle_one_message", exc_info=True)

    # Phase G4c：走主管道 → maybe_auto_reply（护栏/canary/记忆），回复经 orch.send→官方 worker；不在此自答。
    if use_pipeline:
        try:
            from src.integrations.protocol_bridge import make_message, maybe_auto_reply
            await maybe_auto_reply(make_message(
                platform="whatsapp", account_id=phone_number_id, chat_key=chat_key,
                text=text, direction="in", name=sender, msg_id=str(msg.get("id") or "")))
        except Exception:
            logger.debug("WA 主管道回复失败", exc_info=True)
        return

    async def _send_followup(_chat_id: Any, t: str) -> bool:
        out = await wa_send_text(sender, t, phone_number_id, access_token)
        return bool(out.get("ok"))

    context: Dict[str, Any] = {
        "chat_id": chat_key,
        "chat_title": "",
        "request_id": req_id,
        "channel": "whatsapp_cloud",
        "wa_phone_number_id": phone_number_id,
        "wa_from": sender,
        "wa_message_id": str(msg.get("id") or ""),
        "wa_received_at": float(msg.get("timestamp") or time.time()),
        "_send_to_chat": _send_followup,
    }
    try:
        reply_text = await sm.process_message(text=text, user_id=user_key, context=context)
    except Exception as e:  # noqa: BLE001
        logger.exception("WA process_message 异常: %s", e)
        tag_official_handoff("whatsapp", phone_number_id, chat_key, "generate_error")
        return
    if not reply_text:
        if _handoff_cfg("on_empty_reply"):
            tag_official_handoff("whatsapp", phone_number_id, chat_key, "empty_reply")
        return
    sent = await wa_send_text(sender, str(reply_text), phone_number_id, access_token)
    if not sent.get("ok"):
        if not sent.get("blocked") and not str(sent.get("error") or "").startswith("kill_switch:"):
            reason = "window_expired" if sent.get("error_kind") == "window_expired" else "send_error"
            tag_official_handoff("whatsapp", phone_number_id, chat_key, reason)
        return
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mirror_to_inbox("whatsapp", phone_number_id, chat_key, str(reply_text),
                        direction="out")
    except Exception:
        logger.debug("[wa_cloud] 出站镜像失败", exc_info=True)


# ── 健康状态（凭证 / 回调可达 / 发送 / 投递 / STOP / 转人工 / Meta 费用）─────────────
_probe_lock = threading.Lock()
_last_probe: Dict[str, Any] = {}


def _mask_number(s: Any) -> str:
    d = str(s or "")
    return (d[:3] + "****" + d[-2:]) if len(d) > 6 else ("****" if d else "")


async def wa_cloud_probe(config: Dict[str, Any], *, timeout: float = 8.0) -> Dict[str, Any]:
    """只读探活：``GET /{phone_number_id}?fields=…``（**不发消息**、不计费）。

    返回 ``{ok, status, error_kind, quality_rating, messaging_limit_tier, verified_name,
    display_phone_number(打码), probed_at}``；结果缓存进健康面板。绝不抛。
    """
    cfg = dict((config or {}).get("whatsapp_cloud") or {})
    pnid = str(cfg.get("phone_number_id") or "").strip()
    token = str(cfg.get("access_token") or "").strip()
    out: Dict[str, Any] = {"ok": False, "status": 0, "error_kind": "", "probed_at": time.time()}
    if not (pnid and token):
        out["error_kind"] = "missing_credentials"
    else:
        if cfg.get("graph_base") and not _rt("graph_base"):
            configure_runtime(cfg)
        url = (f"{graph_base()}/{pnid}?fields=display_phone_number,verified_name,"
               f"quality_rating,messaging_limit_tier,name_status")
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
                    body = await resp.text()
                    out["status"] = resp.status
                    if resp.status == 200:
                        data = json.loads(body or "{}")
                        out.update(
                            ok=True,
                            quality_rating=str(data.get("quality_rating") or ""),
                            messaging_limit_tier=str(data.get("messaging_limit_tier") or ""),
                            verified_name=str(data.get("verified_name") or ""),
                            name_status=str(data.get("name_status") or ""),
                            display_phone_number=_mask_number(data.get("display_phone_number")))
                    else:
                        out["error_kind"] = str(_fail("whatsapp", resp.status, body)
                                                .get("error_kind") or "unknown")
        except Exception as e:  # noqa: BLE001
            out["error_kind"] = "network"
            logger.warning("[wa_cloud] 探活失败: %s", type(e).__name__)
    with _probe_lock:
        _last_probe.clear()
        _last_probe.update(out)
    return out


def wa_cloud_health(config: Dict[str, Any], *, app_state: Any = None) -> Dict[str, Any]:
    """WhatsApp 官方通道健康汇总（只读、零密钥字段）。

    ``verdict``：``disabled`` / ``misconfigured``（缺凭证或路由没挂）/ ``down``（探活鉴权失败、
    或连续 ≥5 次发送失败）/ ``degraded``（回调从未到达 / 验签失败 / 近 1h 有发送或投递失败 /
    质量评级 RED）/ ``ok``。``alerts`` 列人话原因。
    """
    cfg = dict((config or {}).get("whatsapp_cloud") or {})
    enabled = bool(cfg.get("enabled"))
    creds = {k: bool(str(cfg.get(k) or "").strip())
             for k in ("phone_number_id", "access_token", "app_secret", "verify_token")}
    out: Dict[str, Any] = {"platform": "whatsapp", "track": "official_cloud_api",
                           "enabled": enabled, "creds": creds, "alerts": []}
    webhook: Dict[str, Any] = {}
    try:
        from src.integrations.official_webhook_stats import collect_status
        for row in collect_status(config or {}, app_state=app_state).get("platforms") or []:
            if row.get("platform") == "whatsapp":
                webhook = row
                break
    except Exception:
        logger.debug("[wa_cloud] webhook 台账读取失败", exc_info=True)
    out["webhook"] = webhook
    out.update(send_stats_snapshot())
    try:
        from src.integrations.shared.official_stop_gate import stats_snapshot
        out["stop_gate"] = stats_snapshot("whatsapp")
    except Exception:
        out["stop_gate"] = {}
    try:
        from src.integrations.shared.official_handoff import handoff_snapshot
        out["handoff"] = handoff_snapshot("whatsapp")
    except Exception:
        out["handoff"] = {}
    try:
        from src.integrations.wa_cloud_billing import summary
        out["meta_billing"] = summary(days=30, phone_number_id=str(cfg.get("phone_number_id") or ""))
    except Exception:
        out["meta_billing"] = {}
    with _probe_lock:
        out["probe"] = dict(_last_probe)
    tpl = cfg.get("window_fallback_template") or {}
    try:
        from src.integrations.wa_fallback_templates import (
            configured_languages, fallback_configured, runtime_edition,
        )
        edition = runtime_edition()
        out["window_fallback_template"] = fallback_configured(tpl, edition=edition)
        out["window_fallback_languages"] = configured_languages(tpl, edition=edition)
    except Exception:
        logger.debug("[wa_cloud] 回退模板配置读取失败", exc_info=True)
        out["window_fallback_template"] = bool(
            isinstance(tpl, dict) and str(tpl.get("name") or "").strip())
        out["window_fallback_languages"] = []

    alerts: List[str] = out["alerts"]
    now = time.time()
    send = out["send"]
    probe = out["probe"]
    if not enabled:
        out["verdict"] = "disabled"
        return out
    verdict = "ok"
    if not all(creds.values()):
        alerts.append("缺凭证：" + ",".join(k for k, v in creds.items() if not v))
        verdict = "misconfigured"
    elif webhook.get("verdict") == "not_mounted":
        alerts.append("Webhook 路由未挂载（凭证启动后才填？需重启）")
        verdict = "misconfigured"
    if verdict == "ok":
        if probe and not probe.get("ok") and probe.get("error_kind") == "invalid_token":
            alerts.append("探活鉴权失败（invalid_token）：access_token 失效或无权限")
            verdict = "down"
        elif int(send.get("consecutive_fail") or 0) >= 5 or \
                send.get("last_error_kind") == "invalid_token" and \
                float(send.get("last_fail_ts") or 0) > float(send.get("last_ok_ts") or 0):
            alerts.append(f"连续 {send.get('consecutive_fail')} 次发送失败（最近：{send.get('last_error_kind')}）")
            verdict = "down"
    if verdict == "ok":
        wv = str(webhook.get("verdict") or "")
        if wv in ("never_reached", "auth_failing"):
            alerts.append(f"回调可达性={wv}（公网 URL / verify_token / app_secret）")
            verdict = "degraded"
        if float(send.get("last_fail_ts") or 0) > now - 3600:
            alerts.append(f"近 1 小时有发送失败（{send.get('last_error_kind')}）")
            verdict = "degraded"
        if float(out["delivery"].get("last_failed_ts") or 0) > now - 3600:
            alerts.append("近 1 小时有投递失败（status=failed）")
            verdict = "degraded"
        if str(probe.get("quality_rating") or "").upper() == "RED":
            alerts.append("号码质量评级 RED（Meta 可能限流/降级）")
            verdict = "degraded"
    if int((out.get("meta_billing") or {}).get("unpriced_billable") or 0) > 0:
        alerts.append("有计费消息未配置单价（whatsapp_cloud.pricing.rates），费用金额偏低")
    out["verdict"] = verdict
    return out
