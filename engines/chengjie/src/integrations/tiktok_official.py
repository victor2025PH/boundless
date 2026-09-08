# -*- coding: utf-8 -*-
"""TikTok 官方通道（Business Messaging API，Open Beta）——API 客户端 + Webhook + 编排器 worker（指令 TK-1 B 骨架，2026-09-08）。

老板 2026-09-08 拍板 TikTok 与抖音并行。形状照 ``douyin_official.py``：独立模块、可注入 transport、
``tiktok.enabled`` 才注册与挂载；收件箱 / 拟稿 / 策略层 / 回复窗执行器（48h·10 条，``channel_policy``）全部复用。

**接口形状**（2026-09-08 按 business-api.tiktok.com v1.3 与官方 SDK 类型定义核实）：
- Base ``https://business-api.tiktok.com/open_api/v1.3``，请求头 ``Access-Token``，响应 ``{code, message, request_id, data}``（code 0 成功）。
- 发送 ``POST /business/message/send/``：``business_id`` + ``recipient_type=CONVERSATION`` + ``recipient=<conversation_id>`` +
  ``message_type`` ∈ TEXT / IMAGE / SHARE_POST / TEMPLATE，正文 ``text: {body}`` / ``image: {media_id}`` / ``share_post: {item_id}``；
  可带 ``referenced_message_info.referenced_message_id`` 做引用回复。
- 图片 ``POST /business/message/media/upload/`` multipart（``business_id`` / ``file`` / ``media_type=IMAGE``）→ ``data.media_id``（30 天有效，JPG/PNG ≤ 3MB）。
- 会话内容 ``GET /business/message/content/list/``（补拉对账）。
- Webhook 事件 ``im_receive_msg`` / ``im_send_msg`` / ``im_receive_high_intent_comment``；消息条目含
  ``sender`` / ``recipient`` / ``conversation_id`` / ``message_id`` / ``timestamp`` / ``message_type`` / ``text.body`` / ``from_user.role``。

**TikTok 特有的三件事**：① **只能回复先发消息的用户**——发送需要 webhook 落下的 ``conversation_id``（``peer_ctx``），
没有就按 ``policy_window_no_inbound`` 拒发；② **按注册地门控**（``tiktok_regions``）：账号 meta.region 在不可用区 →
worker 不健康 + 发送拒绝 ``tiktok_region_unsupported``，图片按地区表；③ 无进私事件、无主动私信、无按钮（问题引导用
纯文本）——欢迎语/推荐问题走平台自带的 auto_message，不在本骨架。

**刻意不做**：Comment-to-Message（仅 VN/TH/ID 灰度、无公开 webhook 契约）、店铺客服 API（TK-1 C 另模块）、令牌刷新
（Business API 令牌生命周期以控制台为准；本骨架在 4xx 鉴权码时标 ``needs_reauth`` 提醒重新授权）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

try:
    from fastapi import Request
    from fastapi.responses import JSONResponse
except Exception:  # pragma: no cover
    Request = Any  # type: ignore[misc,assignment]
    JSONResponse = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLATFORM = "tiktok"
MODE = "official"

API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
SEND_URL = f"{API_BASE}/business/message/send/"
MEDIA_UPLOAD_URL = f"{API_BASE}/business/message/media/upload/"
CONTENT_LIST_URL = f"{API_BASE}/business/message/content/list/"

EVENT_RECEIVE = "im_receive_msg"
EVENT_SEND = "im_send_msg"
EVENT_HIGH_INTENT_COMMENT = "im_receive_high_intent_comment"

DEFAULT_WEBHOOK_PATH = "/webhook/tiktok"
DEFAULT_SIGNATURE_HEADER = "X-TT-Signature"   # 以控制台 Webhook 配置页为准，可经 tiktok.webhook_signature_header 覆写
TEXT_MAX = 6000
IMAGE_MAX_BYTES = 3 * 1024 * 1024
SEEN_TTL_SEC = 3 * 24 * 3600.0
CONVERSATION_TTL_SEC = 48 * 3600.0   # 与 channel_policy tiktok 回复窗一致（用户先发后 48h）

#: 鉴权类返回码（Business API 通用）：40001 参数/鉴权、40100 权限、40102/40105 token 无效或过期
TOKEN_CODES = {40100, 40101, 40102, 40104, 40105, 40001}

_TYPE_PLACEHOLDER = {"IMAGE": "[图片]", "VIDEO": "[视频]", "STICKER": "[贴纸]", "EMOJI": "[表情]",
                     "SHARE_POST": "[分享的视频]", "TEMPLATE": "[卡片]", "REACTION": "[表情回应]"}


# ── 配置 ─────────────────────────────────────────────────────────────────────

def tiktok_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    blk: Dict[str, Any] = {}
    try:
        blk = dict((config or {}).get("tiktok") or {})
    except Exception:
        blk = {}
    enabled = bool(blk.get("enabled", False))
    try:
        enabled = enabled or bool((((config or {}).get("platform_login") or {}).get("official")
                                   or {}).get(PLATFORM, {}).get("enabled", False))
    except Exception:
        pass
    return {
        "enabled": enabled,
        "app_id": str(blk.get("app_id") or ""),
        "secret": str(blk.get("secret") or ""),
        "webhook_secret": str(blk.get("webhook_secret") or blk.get("secret") or ""),
        "webhook_path": str(blk.get("webhook_path") or DEFAULT_WEBHOOK_PATH),
        "webhook_signature_header": str(blk.get("webhook_signature_header") or DEFAULT_SIGNATURE_HEADER),
        "state_db_path": str(blk.get("state_db_path") or ""),
        "verify_signature": bool(blk.get("verify_signature", True)),
    }


def official_enabled(config: Optional[Dict[str, Any]]) -> bool:
    return tiktok_cfg(config)["enabled"]


def chat_key_for(user_id: Any) -> str:
    return f"tiktok:user:{str(user_id or '').strip()}"


def user_id_from_chat_key(chat_key: Any) -> str:
    ck = str(chat_key or "").strip()
    return ck.rsplit(":", 1)[-1] if ":" in ck else ck


# ── 签名（HMAC-SHA256，密钥为 webhook secret；控制台若给出不同算法按文档改此处一点）────────

def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(str(secret or "").encode("utf-8"), bytes(body or b""), hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header_value: Any) -> bool:
    if not secret:
        return False
    got = str(header_value or "").strip().lower()
    if got.startswith("sha256="):
        got = got[7:]
    return bool(got) and hmac.compare_digest(sign_body(secret, body), got)


# ── 状态库 ───────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS peer_ctx (
    business_id     TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    last_msg_id     TEXT NOT NULL DEFAULT '',
    last_msg_ts     REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (business_id, user_id)
);
CREATE TABLE IF NOT EXISTS seen_events (key TEXT PRIMARY KEY, ts REAL NOT NULL DEFAULT 0);
"""


class TikTokStateStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or self.default_path()
        if self.path != ":memory:":
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            except Exception:
                pass
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self._conn.executescript(_DDL)
        self._conn.commit()

    @staticmethod
    def default_path() -> str:
        try:
            from src.licensing.data_paths import config_dir
            return str(config_dir() / "tiktok_official_state.db")
        except Exception:
            return os.path.join("config", "tiktok_official_state.db")

    def get_ctx(self, business_id: str, user_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM peer_ctx WHERE business_id=? AND user_id=?",
                                     (str(business_id), str(user_id))).fetchone()
        return dict(row) if row else {}

    def record_inbound(self, business_id: str, user_id: str, *, conversation_id: str, msg_id: str, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO peer_ctx(business_id, user_id, conversation_id, last_msg_id, last_msg_ts, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(business_id, user_id) DO UPDATE SET "
                "conversation_id=CASE WHEN excluded.conversation_id<>'' THEN excluded.conversation_id ELSE peer_ctx.conversation_id END, "
                "last_msg_id=CASE WHEN excluded.last_msg_ts>=peer_ctx.last_msg_ts THEN excluded.last_msg_id ELSE peer_ctx.last_msg_id END, "
                "last_msg_ts=MAX(excluded.last_msg_ts, peer_ctx.last_msg_ts), updated_at=excluded.updated_at",
                (str(business_id), str(user_id), str(conversation_id or ""), str(msg_id or ""), float(ts or 0), time.time()))
            self._conn.commit()

    def seen(self, key: str, *, now: Optional[float] = None) -> bool:
        t = float(now if now is not None else time.time())
        with self._lock:
            if self._conn.execute("SELECT 1 FROM seen_events WHERE key=?", (str(key),)).fetchone():
                return True
            self._conn.execute("INSERT OR IGNORE INTO seen_events(key, ts) VALUES(?,?)", (str(key), t))
            self._conn.execute("DELETE FROM seen_events WHERE ts < ?", (t - SEEN_TTL_SEC,))
            self._conn.commit()
        return False

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_STORE: Optional[TikTokStateStore] = None
_STORE_LOCK = threading.Lock()


def get_state_store(path: Optional[str] = None) -> TikTokStateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = TikTokStateStore(path)
        return _STORE


def _reset_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = None


# ── API 客户端 ─────────────────────────────────────────────────────────────────

Transport = Callable[..., Awaitable[Tuple[int, Dict[str, Any]]]]


async def _aiohttp_transport(method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                             params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None,
                             form: Optional[Dict[str, Any]] = None, file_field: Optional[Tuple[str, str]] = None,
                             timeout: float = 20.0) -> Tuple[int, Dict[str, Any]]:
    import aiohttp
    tmo = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=tmo) as session:
        kwargs: Dict[str, Any] = {"headers": headers or {}, "params": params or None}
        if json_body is not None:
            kwargs["json"] = json_body
        elif file_field is not None or form is not None:
            data = aiohttp.FormData()
            for k, v in (form or {}).items():
                data.add_field(k, str(v))
            if file_field is not None:
                name, path = file_field
                data.add_field(name, open(path, "rb"), filename=os.path.basename(path))
            kwargs["data"] = data
        async with session.request(method, url, **kwargs) as resp:
            raw = await resp.text()
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {"raw": raw[:500]}
            return resp.status, (body if isinstance(body, dict) else {"raw": body})


def _code(body: Dict[str, Any]) -> Tuple[int, str]:
    try:
        code = int(body.get("code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    return code, str(body.get("message") or "")


class TikTokApi:
    def __init__(self, transport: Optional[Transport] = None) -> None:
        self._t: Transport = transport or _aiohttp_transport

    async def send_message(self, *, access_token: str, business_id: str, conversation_id: str,
                           message_type: str, payload: Dict[str, Any],
                           referenced_message_id: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"business_id": business_id, "message_type": message_type,
                                "recipient_type": "CONVERSATION", "recipient": conversation_id}
        body.update(payload)
        if referenced_message_id:
            body["referenced_message_info"] = {"referenced_message_id": referenced_message_id}
        status, data = await self._t("POST", SEND_URL, headers={"Access-Token": access_token,
                                                                "Content-Type": "application/json"},
                                     json_body=body)
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "http": status, "message": msg,
                    "request_id": str(data.get("request_id") or "")}
        mid = str(((data.get("data") or {}).get("message") or {}).get("message_id") or "")
        return {"ok": True, "message_id": mid, "request_id": str(data.get("request_id") or "")}

    async def upload_image(self, *, access_token: str, business_id: str, path: str) -> Dict[str, Any]:
        status, data = await self._t("POST", MEDIA_UPLOAD_URL, headers={"Access-Token": access_token},
                                     form={"business_id": business_id, "media_type": "IMAGE"},
                                     file_field=("file", path))
        code, msg = _code(data)
        mid = str((data.get("data") or {}).get("media_id") or "")
        if status != 200 or code or not mid:
            return {"ok": False, "code": code, "message": msg}
        return {"ok": True, "media_id": mid}

    async def list_content(self, *, access_token: str, business_id: str, conversation_id: str) -> Dict[str, Any]:
        status, data = await self._t("GET", CONTENT_LIST_URL, headers={"Access-Token": access_token},
                                     params={"business_id": business_id, "conversation_id": conversation_id})
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "message": msg}
        return {"ok": True, "messages": list((data.get("data") or {}).get("messages") or [])}


# ── Worker ──────────────────────────────────────────────────────────────────

class TikTokOfficialWorker:
    """编排器契约：``start/stop/healthy/status/send/send_media``。account_id ＝ business_id。"""

    def __init__(self, account: Dict[str, Any], config: Optional[Dict[str, Any]] = None, *,
                 api: Optional[TikTokApi] = None, state: Optional[TikTokStateStore] = None,
                 registry: Any = None, now: Optional[Callable[[], float]] = None) -> None:
        from src.integrations.tiktok_regions import dm_api_available, media_send_allowed, normalize_region
        self.account_id = str((account or {}).get("account_id") or "")
        self.meta: Dict[str, Any] = dict((account or {}).get("meta") or {})
        self.config = config or {}
        self.cfg = tiktok_cfg(self.config)
        self.api = api or TikTokApi()
        self._state = state
        self._registry = registry
        self._now = now or time.time
        self.region = normalize_region(self.meta.get("region"))
        self.dm_ok = dm_api_available(self.region)
        self.media_ok = media_send_allowed(self.region)
        self.running = False
        self.token_state = "ok" if self.meta.get("access_token") else "needs_reauth"
        self.last_error = ""
        self._reported = None

    @property
    def state(self) -> TikTokStateStore:
        if self._state is None:
            self._state = get_state_store(self.cfg["state_db_path"] or None)
        return self._state

    async def start(self) -> None:
        self.running = True
        self._report_session_health()
        logger.info("[tiktok-official] worker 启动 business=%s region=%s dm_api=%s", self.account_id,
                    self.region or "?", self.dm_ok)

    async def stop(self) -> None:
        self.running = False

    async def healthy(self) -> bool:
        self._report_session_health()
        return self.running and self.token_state != "needs_reauth" and self.dm_ok is not False

    def status(self) -> Dict[str, Any]:
        return {"type": "tiktok_official", "running": self.running, "token_state": self.token_state,
                "region": self.region, "dm_api": self.dm_ok, "media_send": self.media_ok,
                "last_error": self.last_error}

    def _report_session_health(self) -> None:
        st = ("region_unsupported" if self.dm_ok is False else self.token_state)
        if st == self._reported:
            return
        self._reported = st
        try:
            from src.integrations.platform_session_health import get_platform_session_health
            h = get_platform_session_health()
            if st == "needs_reauth":
                h.record(PLATFORM, self.account_id, "expired", detail="[rc:other] TikTok 授权失效，请重新授权 Business Account")
            elif st == "region_unsupported":
                h.record(PLATFORM, self.account_id, "blocked",
                         detail=f"[rc:forbidden] 注册地 {self.region} 不支持 Business Messaging API（EEA/瑞士/英国/美国）")
            else:
                h.record(PLATFORM, self.account_id, "authorized")
        except Exception:
            logger.debug("[tiktok-official] 会话健康上报失败", exc_info=True)

    def _persist_meta(self, patch: Dict[str, Any]) -> None:
        self.meta.update(patch)
        try:
            reg = self._registry
            if reg is None:
                from src.integrations.account_registry import get_account_registry
                reg = get_account_registry()
            reg.upsert(PLATFORM, self.account_id, meta=dict(patch), merge_meta=True)
        except Exception:
            logger.debug("[tiktok-official] meta 落注册表失败", exc_info=True)

    async def _deliver(self, chat_key: str, message_type: str, payload: Dict[str, Any], *, kind: str,
                       reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.dm_ok is False:
            return {"delivered": False, "blocked": "tiktok_region_unsupported",
                    "error": f"注册地 {self.region} 不支持 Business Messaging API"}
        if self.token_state == "needs_reauth" or not self.meta.get("access_token"):
            return {"delivered": False, "blocked": "tiktok_needs_reauth", "error": "TikTok 授权失效，请重新授权"}
        user_id = user_id_from_chat_key(chat_key)
        ctx = self.state.get_ctx(self.account_id, user_id)
        conv = str(ctx.get("conversation_id") or "")
        if not conv:
            return {"delivered": False, "blocked": "policy_window_no_inbound"}
        if float(ctx.get("last_msg_ts") or 0) > 0 and self._now() - float(ctx["last_msg_ts"]) > CONVERSATION_TTL_SEC:
            return {"delivered": False, "blocked": "policy_window_expired"}
        ref = str((reply_to or {}).get("id") or "") if isinstance(reply_to, dict) else ""
        res = await self.api.send_message(access_token=str(self.meta.get("access_token")), business_id=self.account_id,
                                          conversation_id=conv, message_type=message_type, payload=payload,
                                          referenced_message_id=ref)
        if res.get("ok"):
            self.last_error = ""
            return {"delivered": True, "message_id": str(res.get("message_id") or ""), "kind": kind,
                    "quote_applied": bool(ref)}
        code = int(res.get("code") or 0)
        self.last_error = f"{code}:{res.get('message') or ''}"
        out: Dict[str, Any] = {"delivered": False, "error": str(res.get("message") or f"tiktok_error_{code}"),
                               "error_kind": f"tiktok_error_{code}", "error_code": code}
        if code in TOKEN_CODES:
            self.token_state = "needs_reauth"
            self._persist_meta({"access_token": ""})
            out["blocked"] = "tiktok_needs_reauth"
        return out

    async def send(self, chat_key: str, text: str, *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        t = str(text or "")
        if len(t) > TEXT_MAX:
            return {"delivered": False, "blocked": f"policy_text_too_long:{len(t)}/{TEXT_MAX}"}
        return await self._deliver(chat_key, "TEXT", {"text": {"body": t}}, kind="text", reply_to=reply_to)

    async def send_media(self, chat_key: str, *, media_path: str, media_type: str, caption: str = "") -> Dict[str, Any]:
        mt = str(media_type or "").lower().split("/", 1)[0]
        if mt not in ("image", "photo"):
            return {"delivered": False, "blocked": f"policy_media_type_denied:{mt or 'unknown'}"}
        if self.media_ok is False:
            return {"delivered": False, "blocked": "tiktok_media_region_unsupported",
                    "error": f"注册地 {self.region} 不支持发送图片"}
        try:
            if os.path.getsize(media_path) > IMAGE_MAX_BYTES:
                return {"delivered": False, "blocked": "policy_media_too_large:3MB"}
        except OSError:
            pass
        up = await self.api.upload_image(access_token=str(self.meta.get("access_token")), business_id=self.account_id,
                                         path=media_path)
        if not up.get("ok"):
            return {"delivered": False, "error": f"图片上传失败 {up.get('code')}", "error_kind": "tiktok_image_upload"}
        res = await self._deliver(chat_key, "IMAGE", {"image": {"media_id": up["media_id"]}}, kind="image")
        if res.get("delivered") and str(caption or "").strip():
            cap = await self._deliver(chat_key, "TEXT", {"text": {"body": str(caption)}}, kind="text")
            res["caption_delivered"] = bool(cap.get("delivered"))
        return res

    async def send_share_post(self, chat_key: str, item_id: str) -> Dict[str, Any]:
        return await self._deliver(chat_key, "SHARE_POST", {"share_post": {"item_id": str(item_id)}}, kind="share_post")


# ── Webhook ─────────────────────────────────────────────────────────────────

def _extract_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    """兼容两种壳：``{"event":…, "message": {...}}`` 与顶层扁平；字段名对齐 SDK MessageItem。"""
    m = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    text = ""
    t = m.get("text")
    if isinstance(t, dict):
        text = str(t.get("body") or t.get("text") or "")
    elif isinstance(t, str):
        text = t
    mtype = str(m.get("message_type") or ("TEXT" if text else "")).upper()
    media = {"IMAGE": "image", "VIDEO": "video", "STICKER": "sticker"}.get(mtype, "")
    if not text and mtype and mtype != "TEXT":
        text = _TYPE_PLACEHOLDER.get(mtype, "[消息]")
    ts = m.get("timestamp") or payload.get("timestamp") or 0
    try:
        ts = float(ts)
        if ts > 1e12:
            ts /= 1000.0
    except (TypeError, ValueError):
        ts = 0.0
    fu = m.get("from_user") if isinstance(m.get("from_user"), dict) else {}
    tu = m.get("to_user") if isinstance(m.get("to_user"), dict) else {}
    return {
        "sender": str(m.get("sender") or fu.get("id") or ""), "recipient": str(m.get("recipient") or tu.get("id") or ""),
        "sender_role": str(fu.get("role") or "").upper(), "conversation_id": str(m.get("conversation_id") or ""),
        "message_id": str(m.get("message_id") or ""), "ts": ts, "text": text, "media_type": media, "message_type": mtype,
        "business_id": str(payload.get("business_id") or m.get("business_id") or ""),
    }


async def handle_webhook(body: bytes, signature: Any, *, config: Optional[Dict[str, Any]],
                         state: Optional[TikTokStateStore] = None, now: Optional[float] = None,
                         emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                         auto_reply: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None
                         ) -> Tuple[int, Dict[str, Any]]:
    cfg = tiktok_cfg(config)
    if cfg["verify_signature"] and not verify_signature(cfg["webhook_secret"], body, signature):
        return 401, {"error": "bad_signature"}
    try:
        payload = json.loads(bytes(body or b"").decode("utf-8") or "{}")
    except Exception:
        return 400, {"error": "bad_json"}
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    # 控制台保存 webhook 时的探活：无 event 的空壳 / challenge 原样回显
    if payload.get("challenge") is not None and not payload.get("event"):
        return 200, {"challenge": payload.get("challenge")}
    event = str(payload.get("event") or payload.get("event_type") or "")
    t_now = float(now if now is not None else time.time())
    st = state or get_state_store(cfg["state_db_path"] or None)
    m = _extract_message(payload)
    if not m["message_id"] and event in (EVENT_RECEIVE, EVENT_SEND):
        return 400, {"error": "missing_message_id"}
    if m["message_id"] and st.seen(f"{event}:{m['message_id']}", now=t_now):
        return 200, {"ok": True, "dup": True}
    if emit is None or auto_reply is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit, maybe_auto_reply as _ar
        emit = emit or _emit
        auto_reply = auto_reply or _ar
    from src.integrations.protocol_bridge import make_message

    if event == EVENT_RECEIVE:
        business_id = m["business_id"] or m["recipient"]
        user_id = m["sender"]
        st.record_inbound(business_id, user_id, conversation_id=m["conversation_id"], msg_id=m["message_id"],
                          ts=m["ts"] or t_now)
        msg = make_message(platform=PLATFORM, account_id=business_id, chat_key=chat_key_for(user_id), text=m["text"],
                           ts=m["ts"] or t_now, msg_id=m["message_id"], direction="in", media_type=m["media_type"],
                           source={"conversation_id": m["conversation_id"], "message_type": m["message_type"],
                                   "server_message_id": m["message_id"]})
        emit(msg)
        try:
            await auto_reply(msg)
        except Exception:
            logger.debug("[tiktok-official] maybe_auto_reply 异常", exc_info=True)
        return 200, {"ok": True}
    if event == EVENT_SEND:
        business_id = m["business_id"] or m["sender"]
        user_id = m["recipient"]
        emit(make_message(platform=PLATFORM, account_id=business_id, chat_key=chat_key_for(user_id), text=m["text"],
                          ts=m["ts"] or t_now, msg_id=m["message_id"], direction="out", media_type=m["media_type"],
                          source={"conversation_id": m["conversation_id"], "echo": True}))
        return 200, {"ok": True}
    if event == EVENT_HIGH_INTENT_COMMENT:
        # 高意向评论：只记录（Comment-to-Message 灰度，不在本骨架自动私信）
        logger.info("[tiktok-official] 高意向评论事件 business=%s（记录，不自动私信）", m["business_id"])
        return 200, {"ok": True, "noted": True}
    return 200, {"ok": True, "ignored": event}


def register_tiktok_routes(app: Any, config_manager: Any, telegram_client: Any = None) -> None:
    cfg0 = tiktok_cfg(getattr(config_manager, "config", None) or {})
    if not cfg0["enabled"] or JSONResponse is None:
        return
    path = cfg0["webhook_path"]

    @app.post(path)
    async def tiktok_webhook(request: Request):  # noqa: D401
        body = await request.body()
        cfg = getattr(config_manager, "config", None) or {}
        header = tiktok_cfg(cfg)["webhook_signature_header"]
        status, resp = await handle_webhook(body, request.headers.get(header), config=cfg)
        return JSONResponse(resp, status_code=status)

    logger.info("[tiktok-official] webhook 已挂载 %s", path)


def register_tiktok_official_worker(config: Optional[Dict[str, Any]], *, registry: Any = None) -> bool:
    if not official_enabled(config):
        return False
    from src.integrations.account_orchestrator import get_worker_factory, register_worker
    if get_worker_factory(PLATFORM, MODE) is None:
        register_worker(PLATFORM, MODE, lambda acc, cfg: TikTokOfficialWorker(acc, cfg, registry=registry))
        logger.info("[tiktok-official] 已注册 (tiktok, official) worker 工厂")
    return True


__all__ = ["PLATFORM", "MODE", "SEND_URL", "MEDIA_UPLOAD_URL", "EVENT_RECEIVE", "EVENT_SEND",
           "EVENT_HIGH_INTENT_COMMENT", "tiktok_cfg", "official_enabled", "chat_key_for", "user_id_from_chat_key",
           "sign_body", "verify_signature", "TikTokStateStore", "get_state_store", "TikTokApi",
           "TikTokOfficialWorker", "handle_webhook", "register_tiktok_routes", "register_tiktok_official_worker"]
