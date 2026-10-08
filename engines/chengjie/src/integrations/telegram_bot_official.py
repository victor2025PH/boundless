# -*- coding: utf-8 -*-
"""Telegram Bot API 官方通道（与 Telegram 个人号协议轨并列的双轨，2026-10-08 智聊 DM 接入）。

与 ``whatsapp_cloud``（Meta 官方轨）同构，补齐「官方 bot」这条合规轨：

- **Webhook 验签**：Telegram ``setWebhook(secret_token=…)`` 后每个回调带
  ``X-Telegram-Bot-Api-Secret-Token``；本模块常量时间比对，**空 secret 硬拒**（不允许裸 webhook）。
- **入箱**：私聊 ``message`` / ``callback_query``（按钮）→ 统一收件箱（platform=telegram，
  account_id=bot id，chat_key=chat id）；群/频道消息不接（bot 私信承接场景）。
  ``update_id`` 去重（Telegram 非 200 会重投）。
- **发送**：``tg_bot_send_text`` → ``POST /bot<token>/sendMessage``；kill-switch（platform=telegram）+
  **STOP 硬闸**（``shared/official_stop_gate``）恒查。错误归类：401 invalid_token / 403 被拉黑·踢出 /
  429 rate_limited（带 retry_after）/ 400 chat not found。
- **STOP**：``/stop``、``STOP``、``退订``… 或主线停联词表 → 冻结；用户**拉黑 bot**
  （``my_chat_member`` → ``kicked``）同样视为停联。之后一律不真发。
- **进待人工**：点名真人 / AI 异常 / 空回复 / 发送失败 / 入站媒体（``shared/official_handoff``）。
- **发媒体**：``tg_bot_send_media`` → multipart 直传（``sendPhoto`` / ``sendVideo`` / ``sendVoice``
  （ogg/opus 呈现为语音条）/ ``sendAudio`` / ``sendDocument``），无需公网媒体 URL；同样过 kill-switch
  与 STOP 硬闸。worker ``send_media`` 与 ``orch.send_media`` 契约一致。
- **setWebhook 只手动触发**：``tg_bot_set_webhook(config, public_base_url)``（向导按钮
  ``POST /api/admin/telegram-bot/set-webhook`` 或 ``tools/tg_bot_set_webhook.py``）；启动、保存凭证
  都**不会**自动调用（改回调地址是对外可见动作，必须人点）。
- **健康**：``tg_bot_health()`` + 只读探活 ``tg_bot_probe()``（``getMe`` + ``getWebhookInfo``，
  不发消息）；路由 ``GET /api/admin/telegram-bot/health``。
- **编排器**：注册 ``(telegram, official)`` worker（坐席接管从收件箱回复走 ``orch.send``），
  ``auto_account: true`` 时启动即登记账号行（account_id=bot id，mode=official）。

config.yaml：
  telegram_bot:
    enabled: false
    bot_token: ""                 # @BotFather 给的 token
    webhook_secret: ""            # setWebhook 的 secret_token（1-256 字符 A-Za-z0-9_-）
    webhook_path: "/tg/bot/webhook"
    unsupported_type_reply: "目前仅支持文字消息。"
    start_reply: ""               # /start 首次进入的欢迎语（空 = 交给 AI）
    auto_account: true
    handoff: {on_human_request: true, on_media: true, on_empty_reply: true}
    stop_gate: {allow_farewell: false, extra_keywords: []}
    api_base: ""                  # 仅测试/回环：https://… 或 http://127.0.0.1:<port>
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from fastapi import FastAPI, Request, Response

logger = logging.getLogger(__name__)

PLATFORM = "telegram"
TG_API_BASE = "https://api.telegram.org"
TG_TEXT_MAX = 4000   # 官方 4096
TG_CAPTION_MAX = 1000   # 官方 1024
TG_UPLOAD_MAX = 50 * 1024 * 1024   # Bot API multipart 上传上限 50MB
TG_PHOTO_MAX = 10 * 1024 * 1024    # sendPhoto 上限 10MB（超了改走 sendDocument）
#: setWebhook 只订阅本模块真正处理的更新类型（私聊消息 / 按钮 / 拉黑·解除）。
TG_ALLOWED_UPDATES = ("message", "edited_message", "callback_query", "my_chat_member")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_rt_lock = threading.Lock()
_runtime: Dict[str, Any] = {}
_stats_lock = threading.Lock()
_stats: Dict[str, Any] = {}
_seen_updates: "OrderedDict[str, float]" = OrderedDict()
_SEEN_MAX = 2048
_probe_lock = threading.Lock()
_last_probe: Dict[str, Any] = {}


# ── 运行期配置 ───────────────────────────────────────────────────────────────

def _safe_api_base(raw: Any) -> str:
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
    logger.warning("[tg_bot] api_base 覆盖被拒（只允许 https 或回环 http）")
    return ""


def configure_runtime(cfg: Optional[Dict[str, Any]]) -> None:
    with _rt_lock:
        _runtime.clear()
        _runtime.update(dict(cfg or {}))


def ensure_runtime(cfg: Optional[Dict[str, Any]]) -> None:
    with _rt_lock:
        empty = not _runtime
    if empty and cfg:
        configure_runtime(cfg)


def _rt(key: str, default: Any = None) -> Any:
    with _rt_lock:
        v = _runtime.get(key, default)
    return default if v is None else v


def api_base() -> str:
    return (_safe_api_base(os.environ.get("TG_BOT_API_BASE"))
            or _safe_api_base(_rt("api_base", "")) or TG_API_BASE)


def method_url(token: str, method: str) -> str:
    return f"{api_base()}/bot{token}/{method}"


def bot_id_from_token(token: Any) -> str:
    """``123456789:AA…`` → ``123456789``（bot 的数字 id，作 account_id；不含密钥部分）。"""
    head = str(token or "").split(":", 1)[0].strip()
    return head if head.isdigit() else ""


def verify_tg_secret(header_value: Any, secret: Any) -> bool:
    """``X-Telegram-Bot-Api-Secret-Token`` 常量时间比对；空 secret 硬拒。"""
    s = str(secret or "")
    h = str(header_value or "")
    if not s or not h:
        return False
    return hmac.compare_digest(h.encode("utf-8"), s.encode("utf-8"))


# ── 可观测 ───────────────────────────────────────────────────────────────────

def _empty_stats() -> Dict[str, Any]:
    return {"events": 0, "bad_secret": 0, "bad_json": 0, "duplicates": 0,
            "last_event_ts": 0.0, "first_event_ts": 0.0,
            "send_ok": 0, "send_fail": 0, "blocked_stop": 0, "blocked_kill_switch": 0,
            "by_error_kind": {}, "last_ok_ts": 0.0, "last_fail_ts": 0.0,
            "last_error_kind": "", "consecutive_fail": 0}


def _bump(**kw: Any) -> None:
    try:
        with _stats_lock:
            if not _stats:
                _stats.update(_empty_stats())
            for k, v in kw.items():
                if isinstance(v, (int, float)) and k in ("events", "bad_secret", "bad_json",
                                                         "duplicates"):
                    _stats[k] = int(_stats.get(k) or 0) + int(v)
                else:
                    _stats[k] = v
    except Exception:
        logger.debug("[tg_bot] 计数失败（忽略）", exc_info=True)


def _record_send(out: Dict[str, Any]) -> None:
    try:
        with _stats_lock:
            if not _stats:
                _stats.update(_empty_stats())
            now = time.time()
            err = str(out.get("error") or "")
            if out.get("ok"):
                _stats["send_ok"] += 1
                _stats["last_ok_ts"] = now
                _stats["consecutive_fail"] = 0
            elif err.startswith("stop_gate:"):
                _stats["blocked_stop"] += 1
            elif err.startswith("kill_switch:"):
                _stats["blocked_kill_switch"] += 1
            else:
                _stats["send_fail"] += 1
                ek = str(out.get("error_kind") or "unknown")
                _stats["by_error_kind"][ek] = int(_stats["by_error_kind"].get(ek) or 0) + 1
                _stats["last_fail_ts"] = now
                _stats["last_error_kind"] = ek
                _stats["consecutive_fail"] += 1
    except Exception:
        logger.debug("[tg_bot] 发送计数失败（忽略）", exc_info=True)


def stats_snapshot() -> Dict[str, Any]:
    with _stats_lock:
        st = dict(_stats or _empty_stats())
        st["by_error_kind"] = dict(st.get("by_error_kind") or {})
    return st


def reset_for_tests() -> None:
    with _stats_lock:
        _stats.clear()
        _seen_updates.clear()
    with _probe_lock:
        _last_probe.clear()
    configure_runtime({})


def _seen(update_id: Any) -> bool:
    key = str(update_id or "")
    if not key:
        return False
    with _stats_lock:
        if key in _seen_updates:
            return True
        _seen_updates[key] = time.time()
        while len(_seen_updates) > _SEEN_MAX:
            _seen_updates.popitem(last=False)
    return False


# ── 发送 ─────────────────────────────────────────────────────────────────────

def classify_tg_error(status: int, body: Any) -> Dict[str, Any]:
    """Bot API 错误 → {kind, retriable, retry_after}。"""
    desc = ""
    retry_after = 0
    code = status
    if isinstance(body, dict):
        desc = str(body.get("description") or "")
        code = int(body.get("error_code") or status or 0)
        retry_after = int(((body.get("parameters") or {}).get("retry_after")) or 0)
    d = desc.lower()
    if code == 401 or "unauthorized" in d:
        kind = "invalid_token"
    elif code == 403 or "blocked by the user" in d or "kicked" in d or "deactivated" in d:
        kind = "recipient_unavailable"
    elif code == 429 or "too many requests" in d:
        kind = "rate_limited"
    elif "chat not found" in d or "user not found" in d:
        kind = "recipient_unavailable"
    elif code >= 500:
        kind = "transient"
    elif code == 400:
        kind = "bad_request"
    else:
        kind = "unknown"
    return {"kind": kind, "retriable": kind in ("rate_limited", "transient"),
            "retry_after": retry_after}


def _stop_gate_blocked(account_id: str, chat_id: str, text: str = "") -> str:
    try:
        from src.integrations.shared.official_stop_gate import outbound_gate
        sg = _rt("stop_gate", {}) or {}
        blocked, reason = outbound_gate(PLATFORM, account_id, str(chat_id), text=text,
                                        allow_farewell=bool(sg.get("allow_farewell", False)))
        return reason if blocked else ""
    except Exception:
        logger.debug("[tg_bot] stop gate 异常（放行）", exc_info=True)
        return ""


async def tg_bot_send_text(
    chat_id: Any, text: str, bot_token: str, *, account_id: str = "",
    check_kill_switch: bool = True, reply_markup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """``sendMessage``。返回 {ok, data} / {ok:False, error, error_kind[, blocked]}；永不抛。"""
    acct = str(account_id or bot_id_from_token(bot_token) or "default")
    body_text = str(text or "").strip()
    if len(body_text) > TG_TEXT_MAX:
        body_text = body_text[: TG_TEXT_MAX - 1] + "…"
    if not body_text:
        return {"ok": True, "data": {"skipped": "empty"}}
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked(PLATFORM, acct)
            if blocked:
                out = {"ok": False, "error": f"kill_switch:{scope}"}
                _record_send(out)
                return out
        except Exception:
            logger.debug("[tg_bot] kill-switch 查询异常（放行）", exc_info=True)
    sg = _stop_gate_blocked(acct, str(chat_id), body_text)
    if sg:
        out = {"ok": False, "error": f"stop_gate:{sg}", "blocked": "stop_contact"}
        _record_send(out)
        return out
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": body_text,
                               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(method_url(bot_token, "sendMessage"), json=payload) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw or "{}")
                except Exception:
                    data = {"raw": raw[:300]}
                if resp.status == 200 and isinstance(data, dict) and data.get("ok"):
                    out = {"ok": True, "data": data.get("result") or {}}
                else:
                    info = classify_tg_error(resp.status, data)
                    desc = str((data or {}).get("description") or "")[:200] if isinstance(data, dict) else ""
                    out = {"ok": False, "error": f"HTTP {resp.status}: {desc}",
                           "error_kind": info["kind"], "retriable": info["retriable"]}
                    if info["retry_after"]:
                        out["retry_after"] = info["retry_after"]
                    logger.warning("[tg_bot] sendMessage 失败 HTTP %s kind=%s", resp.status, info["kind"])
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": type(e).__name__, "error_kind": "network"}
        logger.warning("[tg_bot] sendMessage 异常: %s", type(e).__name__)
    _record_send(out)
    return out


_VOICE_EXT = (".ogg", ".oga", ".opus")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm")
_AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".flac")


def tg_media_method(media_path: str, media_type: str = "", size: int = 0) -> tuple:
    """按媒体类型 / 扩展名选 Bot API 方法 → ``(method, field)``。纯函数。

    - voice/audio + ogg/opus → ``sendVoice``（语音条）；其他音频 → ``sendAudio``；
    - image（≤10MB）→ ``sendPhoto``；超限或 gif 以外的动图走 ``sendDocument``；
    - video → ``sendVideo``；其余 → ``sendDocument``。
    """
    mt = str(media_type or "").lower()
    ext = os.path.splitext(str(media_path or ""))[1].lower()
    if mt in ("voice", "audio", "ptt") or (not mt and ext in _VOICE_EXT + _AUDIO_EXT):
        if ext in _VOICE_EXT:
            return "sendVoice", "voice"
        return "sendAudio", "audio"
    if mt in ("image", "photo", "selfie", "sticker_image") or (not mt and ext in _IMAGE_EXT):
        if size and size > TG_PHOTO_MAX:
            return "sendDocument", "document"
        return "sendPhoto", "photo"
    if mt == "video" or (not mt and ext in _VIDEO_EXT):
        return "sendVideo", "video"
    return "sendDocument", "document"


async def tg_bot_send_media(
    chat_id: Any, media_path: str, bot_token: str, *, media_type: str = "", caption: str = "",
    account_id: str = "", check_kill_switch: bool = True,
) -> Dict[str, Any]:
    """multipart 直传媒体。返回 {ok, data, method} / {ok:False, error, error_kind[, blocked]}；永不抛。"""
    acct = str(account_id or bot_id_from_token(bot_token) or "default")
    path = str(media_path or "")
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked(PLATFORM, acct)
            if blocked:
                out = {"ok": False, "error": f"kill_switch:{scope}"}
                _record_send(out)
                return out
        except Exception:
            logger.debug("[tg_bot] kill-switch 查询异常（放行）", exc_info=True)
    sg = _stop_gate_blocked(acct, str(chat_id), caption)
    if sg:
        out = {"ok": False, "error": f"stop_gate:{sg}", "blocked": "stop_contact"}
        _record_send(out)
        return out
    try:
        size = os.path.getsize(path) if path else -1
    except OSError:
        size = -1
    if size <= 0:
        out = {"ok": False, "error": "media file missing", "error_kind": "bad_media"}
        _record_send(out)
        return out
    if size > TG_UPLOAD_MAX:
        out = {"ok": False, "error": f"media too large ({size} bytes)", "error_kind": "too_large"}
        _record_send(out)
        return out
    method, field = tg_media_method(path, media_type, size)
    cap = str(caption or "").strip()
    if len(cap) > TG_CAPTION_MAX:
        cap = cap[: TG_CAPTION_MAX - 1] + "…"
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if cap:
            form.add_field("caption", cap)
        form.add_field(field, blob, filename=os.path.basename(path) or field)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.post(method_url(bot_token, method), data=form) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw or "{}")
                except Exception:
                    data = {"raw": raw[:300]}
                if resp.status == 200 and isinstance(data, dict) and data.get("ok"):
                    out = {"ok": True, "data": data.get("result") or {}, "method": method}
                else:
                    info = classify_tg_error(resp.status, data)
                    desc = str((data or {}).get("description") or "")[:200] if isinstance(data, dict) else ""
                    out = {"ok": False, "error": f"HTTP {resp.status}: {desc}", "method": method,
                           "error_kind": info["kind"], "retriable": info["retriable"]}
                    if info["retry_after"]:
                        out["retry_after"] = info["retry_after"]
                    logger.warning("[tg_bot] %s 失败 HTTP %s kind=%s", method, resp.status, info["kind"])
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": type(e).__name__, "error_kind": "network", "method": method}
        logger.warning("[tg_bot] %s 异常: %s", method, type(e).__name__)
    _record_send(out)
    return out


# ── setWebhook（只手动触发）────────────────────────────────────────────────────

def webhook_url_for(cfg: Dict[str, Any], public_base_url: Any) -> str:
    """``https://<公网>`` + ``webhook_path`` → 完整回调地址；非 https / 带查询串 → ""。纯函数。"""
    base = str(public_base_url or "").strip().rstrip("/")
    pu = urlparse(base)
    if pu.scheme != "https" or not pu.hostname or pu.query or pu.fragment:
        return ""
    path = str((cfg or {}).get("webhook_path") or "/tg/bot/webhook")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


_SECRET_OK = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


async def tg_bot_set_webhook(config: Dict[str, Any], public_base_url: Any, *,
                             drop_pending_updates: bool = False,
                             timeout: float = 10.0) -> Dict[str, Any]:
    """调 Telegram ``setWebhook``（url + secret_token + allowed_updates）。**只由人手动触发**。

    返回 ``{ok, error_kind, host, path}``（只回主机名与路径，不回 token / secret）。
    ``error_kind``：``missing_credentials`` / ``bad_secret`` / ``bad_url`` / Telegram 错误归类 / ``network``。
    """
    cfg = dict((config or {}).get("telegram_bot") or {})
    token = str(cfg.get("bot_token") or "").strip()
    secret = str(cfg.get("webhook_secret") or "").strip()
    out: Dict[str, Any] = {"ok": False, "error_kind": "", "host": "", "path": ""}
    if not token or not bot_id_from_token(token):
        out["error_kind"] = "missing_credentials"
        return out
    if not _SECRET_OK.match(secret):
        out["error_kind"] = "bad_secret"
        return out
    url = webhook_url_for(cfg, public_base_url)
    if not url:
        out["error_kind"] = "bad_url"
        return out
    pu = urlparse(url)
    out["host"], out["path"] = pu.hostname or "", pu.path
    if cfg.get("api_base") and not _rt("api_base"):
        configure_runtime(cfg)
    payload = {"url": url, "secret_token": secret, "allowed_updates": list(TG_ALLOWED_UPDATES),
               "drop_pending_updates": bool(drop_pending_updates)}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(method_url(token, "setWebhook"), json=payload) as r:
                data = json.loads((await r.text()) or "{}")
                if r.status == 200 and isinstance(data, dict) and data.get("ok"):
                    out["ok"] = True
                else:
                    out["error_kind"] = classify_tg_error(r.status, data)["kind"]
                    out["description"] = str((data or {}).get("description") or "")[:160] \
                        if isinstance(data, dict) else ""
    except Exception as e:  # noqa: BLE001
        out["error_kind"] = "network"
        logger.warning("[tg_bot] setWebhook 异常: %s", type(e).__name__)
    logger.warning("[tg_bot] setWebhook（手动）host=%s path=%s ok=%s kind=%s",
                   out["host"], out["path"], out["ok"], out["error_kind"] or "-")
    return out


# ── 入站解析 ─────────────────────────────────────────────────────────────────

def parse_update(update: Dict[str, Any]) -> Dict[str, Any]:
    """Update → {kind: text|media|callback|blocked|unblocked|ignore, chat_id, user_id, name, text, msg_id, media_type}。"""
    u = update or {}
    if isinstance(u.get("message"), dict):
        m = u["message"]
        chat = m.get("chat") or {}
        if str(chat.get("type") or "") != "private":
            return {"kind": "ignore", "why": "non_private"}
        frm = m.get("from") or {}
        if frm.get("is_bot"):
            return {"kind": "ignore", "why": "from_bot"}
        base = {"chat_id": str(chat.get("id") or ""), "user_id": str(frm.get("id") or ""),
                "name": " ".join(x for x in (str(frm.get("first_name") or ""),
                                             str(frm.get("last_name") or "")) if x).strip()
                or str(frm.get("username") or ""),
                "msg_id": str(m.get("message_id") or ""), "lang": str(frm.get("language_code") or "")}
        txt = str(m.get("text") or "").strip()
        if txt:
            return dict(base, kind="text", text=txt)
        for mt in ("photo", "voice", "audio", "video", "video_note", "document", "sticker",
                   "animation", "location", "contact"):
            if m.get(mt):
                return dict(base, kind="media", media_type=mt, text=str(m.get("caption") or ""))
        return dict(base, kind="ignore", why="empty")
    if isinstance(u.get("callback_query"), dict):
        cq = u["callback_query"]
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        if chat and str(chat.get("type") or "") != "private":
            return {"kind": "ignore", "why": "non_private"}
        frm = cq.get("from") or {}
        return {"kind": "callback", "chat_id": str(chat.get("id") or frm.get("id") or ""),
                "user_id": str(frm.get("id") or ""), "name": str(frm.get("first_name") or ""),
                "msg_id": str(cq.get("id") or ""), "text": str(cq.get("data") or "").strip(),
                "callback_id": str(cq.get("id") or "")}
    if isinstance(u.get("my_chat_member"), dict):
        mcm = u["my_chat_member"]
        chat = mcm.get("chat") or {}
        if str(chat.get("type") or "") != "private":
            return {"kind": "ignore", "why": "non_private"}
        status = str((mcm.get("new_chat_member") or {}).get("status") or "")
        kind = "blocked" if status == "kicked" else ("unblocked" if status == "member" else "ignore")
        return {"kind": kind, "chat_id": str(chat.get("id") or ""),
                "user_id": str((mcm.get("from") or {}).get("id") or "")}
    return {"kind": "ignore", "why": "unsupported_update"}


# ── 路由注册 ─────────────────────────────────────────────────────────────────

def _cfg_of(config_manager: Any) -> Dict[str, Any]:
    return dict(((getattr(config_manager, "config", None) or {}).get("telegram_bot")) or {})


def _ensure_account(bot_id: str) -> bool:
    try:
        from src.integrations.account_registry import get_account_registry
        get_account_registry().upsert(PLATFORM, bot_id, mode="official", status="online",
                                      label="Telegram Bot", merge_meta=True)
        return True
    except Exception:
        logger.debug("[tg_bot] 账号行登记失败（忽略）", exc_info=True)
        return False


def register_telegram_bot_routes(app: FastAPI, config_manager: Any, telegram_client: Any) -> None:
    """挂载 POST webhook（缺 token/secret → 不注册，与 WA Cloud 同策略）+ 注册 (telegram, official) worker。"""
    cfg = _cfg_of(config_manager)
    if not cfg.get("enabled"):
        return
    token = str(cfg.get("bot_token") or "").strip()
    secret = str(cfg.get("webhook_secret") or "").strip()
    bot_id = bot_id_from_token(token)
    if not (token and secret and bot_id):
        logger.error("Telegram Bot 缺 bot_token/webhook_secret（或 token 格式不对），Webhook 未注册")
        return
    sm = getattr(telegram_client, "skill_manager", None)
    configure_runtime(cfg)
    try:
        from src.integrations.official_api_worker import official_pipeline_enabled
        use_pipeline = official_pipeline_enabled(getattr(config_manager, "config", None) or {})
    except Exception:
        use_pipeline = False
    unsupported = (cfg.get("unsupported_type_reply") or "").strip() or "目前仅支持文字消息。"
    path = str(cfg.get("webhook_path") or "/tg/bot/webhook")
    if not path.startswith("/"):
        path = "/" + path
    app.state.tg_bot_webhook_path = path

    async def tg_bot_webhook(request: Request) -> Response:
        if not verify_tg_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), secret):
            _bump(bad_secret=1)
            logger.warning("[tg_bot] webhook secret 校验失败")
            return Response(status_code=403, content=b"forbidden")
        try:
            update = json.loads((await request.body()).decode("utf-8"))
        except Exception:
            _bump(bad_json=1)
            return Response(status_code=400, content=b"invalid json")
        now = time.time()
        with _stats_lock:
            first = float((_stats or {}).get("first_event_ts") or 0.0)
        _bump(events=1, last_event_ts=now, first_event_ts=first or now)
        if _seen(update.get("update_id")):
            _bump(duplicates=1)
            return Response(status_code=200, content=b"OK")
        try:
            await _handle_update(update, sm=sm, token=token, bot_id=bot_id,
                                 unsupported=unsupported, use_pipeline=use_pipeline)
        except Exception as e:  # noqa: BLE001
            logger.exception("[tg_bot] update 处理异常: %s", e)
        return Response(status_code=200, content=b"OK")

    app.add_api_route(path, tg_bot_webhook, methods=["POST"], name="telegram_bot_webhook")
    try:
        from src.integrations.account_orchestrator import get_worker_factory, register_worker
        if get_worker_factory(PLATFORM, "official") is None:
            register_worker(PLATFORM, "official", lambda acc, c: TelegramBotWorker(acc, c))
    except Exception:
        logger.debug("[tg_bot] worker 注册失败", exc_info=True)
    if cfg.get("auto_account", True):
        _ensure_account(bot_id)
    logger.info("Telegram Bot Webhook 已注册: POST %s (bot_id=%s)", path, bot_id)


def _hcfg(key: str, default: bool = True) -> bool:
    return bool((_rt("handoff", {}) or {}).get(key, default))


async def _handle_update(update: Dict[str, Any], *, sm: Any, token: str, bot_id: str,
                         unsupported: str, use_pipeline: bool = False) -> None:
    from src.integrations.shared.official_handoff import human_request_hits, tag_official_handoff
    from src.integrations.shared.official_stop_gate import apply_stop, inbound_gate, is_stopped
    ev = parse_update(update)
    kind = ev.get("kind")
    chat_id = str(ev.get("chat_id") or "")
    if kind == "ignore" or not chat_id:
        return
    chat_key = chat_id
    if kind == "blocked":
        # 用户拉黑 bot：等同「别再联系我」——冻结，之后一律不发
        apply_stop(PLATFORM, bot_id, chat_key, hits=["blocked_bot"])
        return
    if kind == "unblocked":
        return   # 解除拉黑≠重新同意：客户点「开始」发 /start 才解冻（inbound_gate → resubscribed）
    name = str(ev.get("name") or "")
    msg_id = str(ev.get("msg_id") or "")
    if kind == "media":
        try:
            from src.integrations.shared.official_inbound import mirror_inbound_media
            mirror_inbound_media(platform=PLATFORM, account_id=bot_id, chat_key=chat_key,
                                 media_type=str(ev.get("media_type") or "file"),
                                 name=name, msg_id=msg_id)
        except Exception:
            logger.debug("[tg_bot] 入站媒体镜像失败", exc_info=True)
        if ev.get("media_type") in ("sticker", "animation"):
            return
        if _hcfg("on_media"):
            tag_official_handoff(PLATFORM, bot_id, chat_key, "media_inbound",
                                 hits=[str(ev.get("media_type") or "")])
        if not is_stopped(PLATFORM, bot_id, chat_key):
            await tg_bot_send_text(chat_id, unsupported, token, account_id=bot_id)
        return

    text = str(ev.get("text") or "").strip()
    if not text:
        return
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mirror_to_inbox(PLATFORM, bot_id, chat_key, text, direction="in", name=name,
                        msg_id=msg_id, chat_type="user")
    except Exception:
        logger.debug("[tg_bot] 入站镜像失败", exc_info=True)

    sg_cfg = _rt("stop_gate", {}) or {}
    kws = None
    if sg_cfg.get("extra_keywords"):
        from src.integrations.shared.official_stop_gate import DEFAULT_STOP_KEYWORDS
        kws = list(DEFAULT_STOP_KEYWORDS) + [str(k) for k in sg_cfg.get("extra_keywords") or []]
    gate = inbound_gate(PLATFORM, bot_id, chat_key, text, name=name, keywords=kws,
                        allow_resubscribe=bool(sg_cfg.get("allow_resubscribe", True)))
    if gate.get("action") != "pass":
        logger.info("[tg_bot] STOP 闸 action=%s，本条不自答", gate.get("action"))
        return

    low = text.lower()
    if low == "/start" or low.startswith("/start ") or low.startswith("/start@"):
        start_reply = str(_rt("start_reply", "") or "").strip()
        if start_reply:
            await tg_bot_send_text(chat_id, start_reply, token, account_id=bot_id)
            return
        text = "你好"   # 交给 AI 打招呼（不把命令原文喂模型）

    if _hcfg("on_human_request"):
        hr = human_request_hits(text)
        if hr:
            tag_official_handoff(PLATFORM, bot_id, chat_key, "human_request", hits=hr)
            return

    try:
        from src.integrations.shared.official_inbound import inbox_will_autosend
        if inbox_will_autosend(PLATFORM, bot_id, chat_key):
            return
    except Exception:
        logger.debug("[tg_bot] autosend 让位判定异常", exc_info=True)

    if use_pipeline:
        try:
            from src.integrations.protocol_bridge import make_message, maybe_auto_reply
            await maybe_auto_reply(make_message(
                platform=PLATFORM, account_id=bot_id, chat_key=chat_key, text=text,
                direction="in", name=name, msg_id=msg_id))
        except Exception:
            logger.debug("[tg_bot] 主管道回复失败", exc_info=True)
        return

    if sm is None:
        tag_official_handoff(PLATFORM, bot_id, chat_key, "generate_error", hits=["no_skill_manager"])
        return

    async def _send_followup(_chat: Any, t: str) -> bool:
        return bool((await tg_bot_send_text(chat_id, t, token, account_id=bot_id)).get("ok"))

    context: Dict[str, Any] = {
        "chat_id": f"tgbot:{bot_id}:{chat_key}", "chat_title": "",
        "request_id": f"r-{uuid.uuid4().hex[:12]}", "channel": "telegram_bot",
        "tg_bot_id": bot_id, "tg_chat_id": chat_id, "tg_message_id": msg_id,
        "user_language": str(ev.get("lang") or ""), "_send_to_chat": _send_followup,
    }
    try:
        reply = await sm.process_message(text=text, user_id=f"tgbot:{chat_key}", context=context)
    except Exception as e:  # noqa: BLE001
        logger.exception("[tg_bot] process_message 异常: %s", e)
        tag_official_handoff(PLATFORM, bot_id, chat_key, "generate_error")
        return
    if not reply:
        if _hcfg("on_empty_reply"):
            tag_official_handoff(PLATFORM, bot_id, chat_key, "empty_reply")
        return
    sent = await tg_bot_send_text(chat_id, str(reply), token, account_id=bot_id)
    if not sent.get("ok"):
        if not sent.get("blocked") and not str(sent.get("error") or "").startswith("kill_switch:"):
            tag_official_handoff(PLATFORM, bot_id, chat_key, "send_error",
                                 hits=[str(sent.get("error_kind") or "")])
        return
    try:
        from src.integrations.shared.inbox_mirror import mirror_to_inbox
        mirror_to_inbox(PLATFORM, bot_id, chat_key, str(reply), direction="out")
    except Exception:
        logger.debug("[tg_bot] 出站镜像失败", exc_info=True)


# ── 编排器 worker（坐席接管回复走 orch.send）────────────────────────────────

class TelegramBotWorker:
    """(telegram, official) 无状态出站 worker。"""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account or {}
        self.config = config or {}
        self.account_id = str(self.account.get("account_id") or "").strip()
        self.state = "stopped"
        self.detail = ""

    def _token(self) -> str:
        meta = dict(self.account.get("meta") or {})
        tok = str(meta.get("bot_token") or (self.config.get("telegram_bot") or {}).get("bot_token") or "")
        # 账号行与配置 token 必须是同一个 bot（防把 A bot 的回复用 B bot 发出去）
        if self.account_id and bot_id_from_token(tok) != self.account_id:
            return ""
        return tok.strip()

    async def start(self) -> None:
        if not self._token():
            raise RuntimeError("Telegram Bot 缺少 bot_token（或与账号 id 不匹配）")
        ensure_runtime(dict(self.config.get("telegram_bot") or {}))
        self.state, self.detail = "running", ""

    async def stop(self) -> None:
        self.state = "stopped"

    async def healthy(self) -> bool:
        return self.state == "running" and bool(self._token())

    def status(self) -> Dict[str, Any]:
        return {"type": "telegram_official", "account_id": self.account_id,
                "state": self.state, "detail": self.detail}

    async def send(self, chat_key: str, text: str, *, reply_to: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
        dest = str(chat_key or "").rsplit(":", 1)[-1]
        out = await tg_bot_send_text(dest, text, self._token(), account_id=self.account_id)
        res: Dict[str, Any] = {"delivered": bool(out.get("ok")),
                               "message_id": str((out.get("data") or {}).get("message_id") or "")}
        if not out.get("ok"):
            res["error_kind"] = str(out.get("error_kind") or "unknown")
            res["error"] = str(out.get("error") or "")
        if out.get("blocked"):
            res["blocked"] = str(out["blocked"])
        return res

    async def send_media(self, chat_key: str, *, media_path: str, media_type: str,
                         caption: str = "", media_url: str = "") -> Dict[str, Any]:
        """媒体出站（multipart 直传，不需要公网 URL；``media_url`` 收下即忽略）。"""
        dest = str(chat_key or "").rsplit(":", 1)[-1]
        out = await tg_bot_send_media(dest, media_path, self._token(), media_type=media_type,
                                      caption=caption, account_id=self.account_id)
        res: Dict[str, Any] = {"delivered": bool(out.get("ok")),
                               "message_id": str((out.get("data") or {}).get("message_id") or "")}
        if not out.get("ok"):
            res["error_kind"] = str(out.get("error_kind") or "unknown")
            res["error"] = str(out.get("error") or "")
        if out.get("blocked"):
            res["blocked"] = str(out["blocked"])
        return res


# ── 健康 ─────────────────────────────────────────────────────────────────────

async def tg_bot_probe(config: Dict[str, Any], *, timeout: float = 8.0) -> Dict[str, Any]:
    """只读探活：``getMe`` + ``getWebhookInfo``（不发消息）。URL 只回主机名、错误只回摘要。"""
    cfg = dict((config or {}).get("telegram_bot") or {})
    token = str(cfg.get("bot_token") or "").strip()
    out: Dict[str, Any] = {"ok": False, "error_kind": "", "probed_at": time.time()}
    if not token:
        out["error_kind"] = "missing_credentials"
    else:
        if cfg.get("api_base") and not _rt("api_base"):
            configure_runtime(cfg)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.get(method_url(token, "getMe")) as r:
                    me = json.loads((await r.text()) or "{}")
                    if r.status != 200 or not me.get("ok"):
                        out["error_kind"] = classify_tg_error(r.status, me)["kind"]
                    else:
                        res = me.get("result") or {}
                        out.update(ok=True, username=str(res.get("username") or ""),
                                   bot_id=str(res.get("id") or ""))
                if out["ok"]:
                    async with s.get(method_url(token, "getWebhookInfo")) as r:
                        wi = (json.loads((await r.text()) or "{}") or {}).get("result") or {}
                        url = str(wi.get("url") or "")
                        out["webhook"] = {
                            "set": bool(url),
                            "host": (urlparse(url).hostname or "") if url else "",
                            "pending_update_count": int(wi.get("pending_update_count") or 0),
                            "last_error_age_sec": (int(time.time() - float(wi["last_error_date"]))
                                                   if wi.get("last_error_date") else -1),
                            "last_error_message": str(wi.get("last_error_message") or "")[:120],
                        }
        except Exception as e:  # noqa: BLE001
            out["ok"] = False
            out["error_kind"] = out.get("error_kind") or "network"
            logger.warning("[tg_bot] 探活失败: %s", type(e).__name__)
    with _probe_lock:
        _last_probe.clear()
        _last_probe.update(out)
    return out


def tg_bot_health(config: Dict[str, Any], *, app_state: Any = None) -> Dict[str, Any]:
    """Telegram Bot 官方通道健康（只读、零密钥）。判词同 WA Cloud：disabled / misconfigured / down / degraded / ok。"""
    cfg = dict((config or {}).get("telegram_bot") or {})
    enabled = bool(cfg.get("enabled"))
    token = str(cfg.get("bot_token") or "").strip()
    creds = {"bot_token": bool(token), "webhook_secret": bool(str(cfg.get("webhook_secret") or "").strip()),
             "token_format": bool(bot_id_from_token(token))}
    mounted = bool(str(getattr(app_state, "tg_bot_webhook_path", "") or "").strip()) \
        if app_state is not None else (enabled and all(creds.values()))
    st = stats_snapshot()
    out: Dict[str, Any] = {"platform": PLATFORM, "track": "official_bot_api", "enabled": enabled,
                           "bot_id": bot_id_from_token(token), "creds": creds, "mounted": mounted,
                           "stats": st, "alerts": []}
    try:
        from src.integrations.shared.official_stop_gate import stats_snapshot as sg
        out["stop_gate"] = sg(PLATFORM)
    except Exception:
        out["stop_gate"] = {}
    try:
        from src.integrations.shared.official_handoff import handoff_snapshot
        out["handoff"] = handoff_snapshot(PLATFORM)
    except Exception:
        out["handoff"] = {}
    with _probe_lock:
        out["probe"] = dict(_last_probe)
    if not enabled:
        out["verdict"] = "disabled"
        return out
    alerts: List[str] = out["alerts"]
    probe = out["probe"]
    now = time.time()
    verdict = "ok"
    if not all(creds.values()):
        alerts.append("缺凭证：" + ",".join(k for k, v in creds.items() if not v))
        verdict = "misconfigured"
    elif not mounted:
        alerts.append("Webhook 路由未挂载（凭证启动后才填？需重启）")
        verdict = "misconfigured"
    if verdict == "ok":
        if probe and not probe.get("ok") and probe.get("error_kind") == "invalid_token":
            alerts.append("探活鉴权失败（invalid_token）：bot_token 失效")
            verdict = "down"
        elif int(st.get("consecutive_fail") or 0) >= 5:
            alerts.append(f"连续 {st.get('consecutive_fail')} 次发送失败（最近：{st.get('last_error_kind')}）")
            verdict = "down"
    if verdict == "ok":
        wh = probe.get("webhook") or {}
        if probe.get("ok") and not wh.get("set"):
            alerts.append("Telegram 侧未 setWebhook（回调不会到达）")
            verdict = "degraded"
        elif wh.get("last_error_age_sec", -1) != -1 and wh.get("last_error_age_sec", 1e9) < 3600:
            alerts.append("Telegram 报告近 1 小时回调投递错误：" + str(wh.get("last_error_message") or ""))
            verdict = "degraded"
        if int(st.get("bad_secret") or 0) > 0 and int(st.get("events") or 0) == 0:
            alerts.append("有回调到达但 secret 从未通过（webhook_secret 配错？）")
            verdict = "degraded"
        if float(st.get("last_fail_ts") or 0) > now - 3600:
            alerts.append(f"近 1 小时有发送失败（{st.get('last_error_kind')}）")
            verdict = "degraded"
    out["verdict"] = verdict
    return out


__all__ = [
    "register_telegram_bot_routes", "tg_bot_send_text", "tg_bot_send_media", "tg_media_method",
    "tg_bot_set_webhook", "webhook_url_for", "TG_ALLOWED_UPDATES", "parse_update", "verify_tg_secret",
    "bot_id_from_token", "classify_tg_error", "tg_bot_probe", "tg_bot_health",
    "TelegramBotWorker", "configure_runtime", "ensure_runtime", "stats_snapshot",
    "reset_for_tests",
]
