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
import re
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
EVENT_MARK_READ = "im_mark_read_msg"
EVENT_HIGH_INTENT_COMMENT = "im_receive_high_intent_comment"

# 授权码模式（Business Account 持有人授权；business_id ＝ /tt_user/oauth2/token/ 返回的 open_id）
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = f"{API_BASE}/tt_user/oauth2/token/"
REFRESH_URL = f"{API_BASE}/tt_user/oauth2/refresh_token/"
REVOKE_URL = f"{API_BASE}/tt_user/oauth2/revoke/"
BUSINESS_GET_URL = f"{API_BASE}/business/get/"
CAPABILITIES_URL = f"{API_BASE}/business/message/capabilities/get/"
WEBHOOK_UPDATE_URL = f"{API_BASE}/business/webhook/update/"
WEBHOOK_LIST_URL = f"{API_BASE}/business/webhook/list/"
MEDIA_DOWNLOAD_URL = f"{API_BASE}/business/message/media/download/"
WEBHOOK_EVENT_TYPE = "DIRECT_MESSAGE"
#: 私信收发所需 scope（用户可部分授权 → 回调后按 data.scope 校验齐全）
OAUTH_SCOPES = ("user.info.basic", "user.info.username", "user.info.profile",
                "message.list.read", "message.list.send", "message.list.manage")
MESSAGING_SCOPES = ("message.list.read", "message.list.send", "message.list.manage")
DEFAULT_OAUTH_CALLBACK_PATH = "/webhook/tiktok/oauth/callback"
OAUTH_STATE_TTL_SEC = 600.0

DEFAULT_WEBHOOK_PATH = "/webhook/tiktok"
#: 官方 webhook 验签头：``Tiktok-Signature: t=<unix>,s=<hex>``，``s = HMAC-SHA256(client_secret, f"{t}.{raw_body}")``
DEFAULT_SIGNATURE_HEADER = "Tiktok-Signature"
DEFAULT_SIGNATURE_TOLERANCE_SEC = 300.0
TEXT_MAX = 6000
IMAGE_MAX_BYTES = 3 * 1024 * 1024
SEEN_TTL_SEC = 3 * 24 * 3600.0
CONVERSATION_TTL_SEC = 48 * 3600.0   # 与 channel_policy tiktok 回复窗一致（用户先发后 48h）
REFRESH_AHEAD_SEC = 30 * 60.0        # access 到期前 30 分钟刷新（短期令牌，全自动值守必须自刷）

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
    try:
        tol = float(blk.get("signature_tolerance_sec") or DEFAULT_SIGNATURE_TOLERANCE_SEC)
    except (TypeError, ValueError):
        tol = DEFAULT_SIGNATURE_TOLERANCE_SEC
    return {
        "enabled": enabled,
        "app_id": str(blk.get("app_id") or ""),
        "secret": str(blk.get("secret") or ""),
        "webhook_secret": str(blk.get("webhook_secret") or blk.get("secret") or ""),
        "webhook_path": str(blk.get("webhook_path") or DEFAULT_WEBHOOK_PATH),
        "webhook_signature_header": str(blk.get("webhook_signature_header") or DEFAULT_SIGNATURE_HEADER),
        "signature_tolerance_sec": tol,
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


# ── 官方验签：Tiktok-Signature: t=<unix>,s=<hex>；s = HMAC-SHA256(client_secret, f"{t}.{raw_body}") ──────

def sign_body(secret: str, body: bytes, ts: Optional[int] = None) -> str:
    """返回可直接放进 ``Tiktok-Signature`` 头的 ``t=…,s=…``（测试与自检用；ts 缺省取当前时间）。"""
    t = int(ts if ts is not None else time.time())
    mac = hmac.new(str(secret or "").encode("utf-8"), f"{t}.".encode("utf-8") + bytes(body or b""),
                   hashlib.sha256).hexdigest()
    return f"t={t},s={mac}"


def parse_signature_header(header_value: Any) -> Tuple[Optional[int], str]:
    parts: Dict[str, str] = {}
    for seg in str(header_value or "").split(","):
        k, _, v = seg.strip().partition("=")
        if k and v:
            parts[k.strip().lower()] = v.strip()
    ts = parts.get("t", "")
    return (int(ts) if ts.isdigit() else None), parts.get("s", "").lower()


def verify_signature(secret: str, body: bytes, header_value: Any, *, now: Optional[float] = None,
                     tolerance: float = DEFAULT_SIGNATURE_TOLERANCE_SEC) -> bool:
    if not secret:
        return False
    ts, sig = parse_signature_header(header_value)
    if ts is None or not sig:
        return False
    want = sign_body(secret, body, ts).split("s=", 1)[1]
    if not hmac.compare_digest(want, sig):
        return False
    t_now = float(now if now is not None else time.time())
    return abs(t_now - ts) <= float(tolerance)


# ── 状态库 ───────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS peer_ctx (
    business_id     TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    last_msg_id     TEXT NOT NULL DEFAULT '',
    last_msg_ts     REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0,
    ref             TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (business_id, user_id)
);
CREATE TABLE IF NOT EXISTS seen_events (key TEXT PRIMARY KEY, ts REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS biz_stats (
    business_id     TEXT PRIMARY KEY,
    first_event_ts  REAL NOT NULL DEFAULT 0,
    last_event_ts   REAL NOT NULL DEFAULT 0,
    events_total    INTEGER NOT NULL DEFAULT 0
);
"""

# ── 入口经营：TikTok.me 引流链接（业务方不能先开口，链接/二维码是唯一可控的进线口）──────────
TIKTOK_ME_BASE = "https://tiktok.me/"
REF_MAX_LEN = 60
_REF_ALLOWED = re.compile(r"[^A-Za-z0-9_=\-]")


def sanitize_ref(ref: Any) -> str:
    """ref 只允许字母数字与 ``- _ =``（官方规则），截到 60 字符。"""
    return _REF_ALLOWED.sub("", str(ref or ""))[:REF_MAX_LEN]


def tiktok_me_link(username: Any, ref: Any = "") -> str:
    """``https://tiktok.me/<username>?ref=<ref>``——点开直接进入与该 Business Account 的私信会话。"""
    u = str(username or "").strip().lstrip("@")
    if not u:
        return ""
    r = sanitize_ref(ref)
    return f"{TIKTOK_ME_BASE}{u}" + (f"?ref={r}" if r else "")


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
        # 老库补列（骨架期建的 peer_ctx 没有 ref）
        try:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(peer_ctx)").fetchall()}
            if "ref" not in cols:
                self._conn.execute("ALTER TABLE peer_ctx ADD COLUMN ref TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
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

    def record_inbound(self, business_id: str, user_id: str, *, conversation_id: str, msg_id: str, ts: float,
                       ref: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO peer_ctx(business_id, user_id, conversation_id, last_msg_id, last_msg_ts, updated_at, ref) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(business_id, user_id) DO UPDATE SET "
                "conversation_id=CASE WHEN excluded.conversation_id<>'' THEN excluded.conversation_id ELSE peer_ctx.conversation_id END, "
                "last_msg_id=CASE WHEN excluded.last_msg_ts>=peer_ctx.last_msg_ts THEN excluded.last_msg_id ELSE peer_ctx.last_msg_id END, "
                "last_msg_ts=MAX(excluded.last_msg_ts, peer_ctx.last_msg_ts), updated_at=excluded.updated_at, "
                "ref=CASE WHEN peer_ctx.ref='' THEN excluded.ref ELSE peer_ctx.ref END",
                (str(business_id), str(user_id), str(conversation_id or ""), str(msg_id or ""), float(ts or 0), time.time(),
                 sanitize_ref(ref)))
            self._conn.commit()

    def record_event(self, business_id: str, ts: Optional[float] = None) -> None:
        """任何 webhook 事件到达都记一笔——给「webhook 静默」检测用。"""
        t = float(ts if ts is not None else time.time())
        if not business_id:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO biz_stats(business_id, first_event_ts, last_event_ts, events_total) VALUES(?,?,?,1) "
                "ON CONFLICT(business_id) DO UPDATE SET last_event_ts=MAX(excluded.last_event_ts, biz_stats.last_event_ts), "
                "events_total=biz_stats.events_total+1", (str(business_id), t, t))
            self._conn.commit()

    def stats(self, business_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM biz_stats WHERE business_id=?", (str(business_id),)).fetchone()
        return dict(row) if row else {"business_id": str(business_id), "first_event_ts": 0.0, "last_event_ts": 0.0,
                                      "events_total": 0}

    def ref_counts(self, business_id: str, *, since_ts: float = 0.0, limit: int = 10) -> Dict[str, int]:
        """按引流 ref 统计进线用户数（首条进线的 ref 固定不变）——面板「入口来源」用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ref, COUNT(*) AS n FROM peer_ctx WHERE business_id=? AND ref<>'' AND last_msg_ts>=? "
                "GROUP BY ref ORDER BY n DESC LIMIT ?", (str(business_id), float(since_ts or 0), int(limit))).fetchall()
        return {str(r["ref"]): int(r["n"]) for r in rows}

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

    # ── OAuth（Business Account 持有人授权）──
    async def exchange_code(self, *, client_id: str, client_secret: str, code: str, redirect_uri: str) -> Dict[str, Any]:
        status, data = await self._t("POST", TOKEN_URL, headers={"Content-Type": "application/json"},
                                     json_body={"client_id": client_id, "client_secret": client_secret,
                                                "grant_type": "authorization_code", "auth_code": code,
                                                "redirect_uri": redirect_uri})
        return _grant_result(status, data)

    async def refresh_token(self, *, client_id: str, client_secret: str, refresh_token: str) -> Dict[str, Any]:
        status, data = await self._t("POST", REFRESH_URL, headers={"Content-Type": "application/json"},
                                     json_body={"client_id": client_id, "client_secret": client_secret,
                                                "grant_type": "refresh_token", "refresh_token": refresh_token})
        return _grant_result(status, data)

    async def business_profile(self, *, access_token: str, business_id: str) -> Dict[str, Any]:
        status, data = await self._t("GET", BUSINESS_GET_URL, headers={"Access-Token": access_token},
                                     params={"business_id": business_id,
                                             "fields": json.dumps(["username", "display_name", "profile_image"])})
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "message": msg}
        d = data.get("data") or {}
        return {"ok": True, "username": str(d.get("username") or ""), "display_name": str(d.get("display_name") or ""),
                "profile_image": str(d.get("profile_image") or "")}

    async def capabilities(self, *, access_token: str, business_id: str, capability_types: Any,
                           conversation_id: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {"business_id": business_id, "capability_types": json.dumps(list(capability_types))}
        if conversation_id:
            params["conversation_id"] = conversation_id
        status, data = await self._t("GET", CAPABILITIES_URL, headers={"Access-Token": access_token}, params=params)
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "message": msg}
        infos = list((data.get("data") or {}).get("capability_infos") or [])
        return {"ok": True, "capabilities": {str(i.get("capability_type") or ""): bool(i.get("capability_result"))
                                             for i in infos if isinstance(i, dict)}}

    # ── Webhook 配置（应用级，用 app_id/secret，不需要用户令牌）──
    async def webhook_update(self, *, app_id: str, secret: str, callback_url: str,
                             event_type: str = WEBHOOK_EVENT_TYPE) -> Dict[str, Any]:
        status, data = await self._t("POST", WEBHOOK_UPDATE_URL, headers={"Content-Type": "application/json"},
                                     json_body={"app_id": app_id, "secret": secret, "event_type": event_type,
                                                "callback_url": callback_url})
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "message": msg}
        return {"ok": True, "callback_url": str((data.get("data") or {}).get("callback_url") or callback_url)}

    async def webhook_list(self, *, app_id: str, secret: str, event_type: str = WEBHOOK_EVENT_TYPE) -> Dict[str, Any]:
        status, data = await self._t("GET", WEBHOOK_LIST_URL, params={"app_id": app_id, "secret": secret,
                                                                       "event_type": event_type})
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "message": msg}
        d = data.get("data") or {}
        return {"ok": True, "callback_url": str(d.get("callback_url") or ""), "raw": d}


def _grant_result(status: int, data: Dict[str, Any]) -> Dict[str, Any]:
    code, msg = _code(data)
    d = data.get("data") or {}
    if status != 200 or code or not d.get("access_token"):
        return {"ok": False, "code": code, "message": msg or str(data.get("message") or "")}
    return {"ok": True, "access_token": str(d.get("access_token")), "open_id": str(d.get("open_id") or ""),
            "expires_in": float(d.get("expires_in") or 0), "refresh_token": str(d.get("refresh_token") or ""),
            "refresh_expires_in": float(d.get("refresh_token_expires_in") or d.get("refresh_expires_in") or 0),
            "scope": str(d.get("scope") or "")}


# ── 令牌生命周期 / 授权码流程 ────────────────────────────────────────────────────

def token_state(meta: Dict[str, Any], now: float) -> str:
    """``ok`` / ``refresh_due``（access 快到期或已到期，refresh 仍活）/ ``needs_reauth``（无令牌，或 refresh 已死且 access 也死）。"""
    at = str(meta.get("access_token") or "")
    rt = str(meta.get("refresh_token") or "")
    aexp = float(meta.get("access_expires_at") or 0)
    rexp = float(meta.get("refresh_expires_at") or 0)
    refresh_alive = bool(rt) and (rexp <= 0 or now < rexp)
    access_alive = bool(at) and (aexp <= 0 or now < aexp - REFRESH_AHEAD_SEC)
    if access_alive:
        return "ok"
    if refresh_alive:
        return "refresh_due"
    return "ok" if (at and aexp > now) else "needs_reauth"


REAUTH_WARN_DAYS = 7
WEBHOOK_SILENCE_SEC = 24 * 3600.0


def reauth_days_left(meta: Dict[str, Any], now: float) -> Optional[int]:
    """距「必须人工重新授权」还有几天：有活的 refresh → 按 refresh 到期日；只有 access（手填令牌）→ 按 access 到期日；
    没有到期信息 → None（未知，不预警）。"""
    rt = str(meta.get("refresh_token") or "")
    rexp = float(meta.get("refresh_expires_at") or 0)
    aexp = float(meta.get("access_expires_at") or 0)
    end = rexp if (rt and rexp > 0) else aexp
    if end <= 0:
        return None
    return max(0, int((end - now) // 86400))


def webhook_silence(stats: Dict[str, Any], now: float, *, threshold_sec: float = WEBHOOK_SILENCE_SEC) -> Dict[str, Any]:
    """``{"seen_any": bool, "last_event_ts": float, "silent_sec": float, "silent": bool}``——曾收到过事件且超过阈值无事件才算静默；
    从未收到过事件不是故障（可能还没人来），只提示「尚未收到任何事件」。"""
    last = float((stats or {}).get("last_event_ts") or 0)
    if last <= 0:
        return {"seen_any": False, "last_event_ts": 0.0, "silent_sec": 0.0, "silent": False}
    gap = max(0.0, now - last)
    return {"seen_any": True, "last_event_ts": last, "silent_sec": gap, "silent": gap > threshold_sec}


def token_meta_from_grant(grant: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    t = float(now if now is not None else time.time())
    out: Dict[str, Any] = {"access_token": str(grant.get("access_token") or ""),
                           "access_expires_at": (t + float(grant["expires_in"])) if grant.get("expires_in") else 0.0,
                           "scope": str(grant.get("scope") or ""), "authorized_at": t}
    if grant.get("refresh_token"):
        out["refresh_token"] = str(grant["refresh_token"])
        out["refresh_expires_at"] = (t + float(grant["refresh_expires_in"])) if grant.get("refresh_expires_in") else 0.0
    return out


def missing_scopes(granted: Any) -> Tuple[str, ...]:
    have = {s.strip() for s in str(granted or "").replace(" ", "").split(",") if s.strip()}
    return tuple(s for s in MESSAGING_SCOPES if s not in have)


def oauth_state(secret: str, region: str = "", now: Optional[float] = None) -> str:
    """防篡改 state：``<ts>.<region>.<hmac[:32]>``——把面板选好的注册地一并带回回调。"""
    ts = str(int(now if now is not None else time.time()))
    reg = str(region or "").upper()
    mac = hmac.new(str(secret or "").encode("utf-8"), f"{ts}.{reg}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{reg}.{mac}"


def verify_oauth_state(secret: str, state: Any, now: Optional[float] = None,
                       ttl: float = OAUTH_STATE_TTL_SEC) -> Optional[str]:
    """合法 → 返回 region（可能为空串）；非法/过期 → None。"""
    parts = str(state or "").split(".")
    if len(parts) != 3 or not secret or not parts[0].isdigit():
        return None
    ts, reg, mac = parts
    t_now = float(now if now is not None else time.time())
    if not (0 <= t_now - float(ts) <= ttl):
        return None
    want = hmac.new(str(secret).encode("utf-8"), f"{ts}.{reg}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return reg if hmac.compare_digest(want, mac) else None


def authorize_url(client_key: str, redirect_uri: str, state: str, scopes: Any = OAUTH_SCOPES) -> str:
    from urllib.parse import urlencode
    return AUTHORIZE_URL + "?" + urlencode({"client_key": client_key, "response_type": "code",
                                            "scope": ",".join(scopes), "redirect_uri": redirect_uri, "state": state})


async def complete_oauth(code: str, *, config: Optional[Dict[str, Any]], redirect_uri: str, region: str = "",
                         registry: Any = None, api: Optional[TikTokApi] = None,
                         now: Optional[float] = None) -> Dict[str, Any]:
    """code 换令牌 → 校验私信 scope 齐全 → 拉账号资料 → ``tiktok/<open_id>`` mode=official 落注册表（meta 原子合并）。"""
    cfg = tiktok_cfg(config)
    if not cfg["app_id"] or not cfg["secret"]:
        return {"ok": False, "error": "missing_credentials"}
    api = api or TikTokApi()
    grant = await api.exchange_code(client_id=cfg["app_id"], client_secret=cfg["secret"], code=str(code or ""),
                                    redirect_uri=redirect_uri)
    if not grant.get("ok"):
        return {"ok": False, "error": "exchange_failed", "error_code": grant.get("code"), "description": grant.get("message")}
    if not grant.get("open_id"):
        return {"ok": False, "error": "exchange_failed", "error_code": 0, "description": "no open_id"}
    lack = missing_scopes(grant.get("scope"))
    if lack:
        return {"ok": False, "error": "ungranted_scopes", "missing": list(lack), "open_id": grant["open_id"]}
    open_id = grant["open_id"]
    meta = token_meta_from_grant(grant, now)
    meta["client_id"] = cfg["app_id"]
    if region:
        meta["region"] = str(region).upper()
    prof = await api.business_profile(access_token=meta["access_token"], business_id=open_id)
    if prof.get("ok"):
        meta["username"] = prof["username"]
        meta["display_name"] = prof["display_name"]
        if prof.get("profile_image"):
            meta["avatar_url"] = prof["profile_image"]
    reg = registry
    if reg is None:
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
    existing = reg.get(PLATFORM, open_id) or {}
    label = (existing.get("label") or meta.get("display_name") or meta.get("username") or f"TikTok {open_id[:8]}")
    reg.upsert(PLATFORM, open_id, mode=MODE, status="active", label=label, meta=meta, merge_meta=True)
    try:
        from src.integrations.platform_session_health import get_platform_session_health
        get_platform_session_health().record(PLATFORM, open_id, "authorized")
    except Exception:
        pass
    return {"ok": True, "open_id": open_id, "scope": meta["scope"], "username": meta.get("username", ""),
            "display_name": meta.get("display_name", ""), "region": meta.get("region", ""),
            "access_expires_at": meta["access_expires_at"], "refresh_expires_at": meta.get("refresh_expires_at", 0.0)}


async def ensure_webhook(config: Optional[Dict[str, Any]], callback_url: str, *, api: Optional[TikTokApi] = None) -> Dict[str, Any]:
    """用应用凭证把私信 webhook 回调地址注册到 TikTok（幂等：已一致则不重复写）。"""
    cfg = tiktok_cfg(config)
    if not cfg["app_id"] or not cfg["secret"]:
        return {"ok": False, "error": "missing_credentials"}
    api = api or TikTokApi()
    cur = await api.webhook_list(app_id=cfg["app_id"], secret=cfg["secret"])
    if cur.get("ok") and cur.get("callback_url") == callback_url:
        return {"ok": True, "callback_url": callback_url, "changed": False}
    res = await api.webhook_update(app_id=cfg["app_id"], secret=cfg["secret"], callback_url=callback_url)
    if not res.get("ok"):
        return {"ok": False, "error": "webhook_update_failed", "error_code": res.get("code"), "description": res.get("message")}
    return {"ok": True, "callback_url": res["callback_url"], "changed": True}


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
        self.token_state = token_state(self.meta, self._now())
        self.last_error = ""
        self._reported = None

    @property
    def state(self) -> TikTokStateStore:
        if self._state is None:
            self._state = get_state_store(self.cfg["state_db_path"] or None)
        return self._state

    async def start(self) -> None:
        self.running = True
        await self.maybe_refresh_tokens()
        self._report_session_health()
        logger.info("[tiktok-official] worker 启动 business=%s region=%s dm_api=%s token=%s", self.account_id,
                    self.region or "?", self.dm_ok, self.token_state)

    async def maybe_refresh_tokens(self, *, force: bool = False) -> str:
        """access 到期前 30 分钟（或 force）用 refresh 换新；refresh 也死 → needs_reauth。返回刷新后的 token_state。"""
        now = self._now()
        self.token_state = token_state(self.meta, now)
        if self.token_state != "refresh_due" and not (force and self.meta.get("refresh_token")):
            return self.token_state
        r = await self.api.refresh_token(client_id=self.cfg["app_id"], client_secret=self.cfg["secret"],
                                         refresh_token=str(self.meta.get("refresh_token") or ""))
        if r.get("ok"):
            patch = token_meta_from_grant(r, now)
            patch.pop("authorized_at", None)
            if not patch.get("refresh_token"):
                patch.pop("refresh_token", None)
                patch.pop("refresh_expires_at", None)
            self._persist_meta(patch)
            self.token_state = token_state(self.meta, now)
            self.last_error = ""
        else:
            self.last_error = f"refresh:{r.get('code')}:{r.get('message') or ''}"
            if int(r.get("code") or 0) in TOKEN_CODES:
                # refresh 本身被拒 → 两把令牌都作废，避免每次发送都再撞一次刷新接口
                self._persist_meta({"access_token": "", "refresh_token": ""})
                self.token_state = "needs_reauth"
            elif not self.meta.get("access_token"):
                self.token_state = "needs_reauth"
        return self.token_state

    async def stop(self) -> None:
        self.running = False

    async def healthy(self) -> bool:
        self._report_session_health()
        return self.running and self.token_state != "needs_reauth" and self.dm_ok is not False

    def status(self) -> Dict[str, Any]:
        now = self._now()
        stats = self.state.stats(self.account_id) if self._state is not None else {}
        return {"type": "tiktok_official", "running": self.running, "token_state": self.token_state,
                "region": self.region, "dm_api": self.dm_ok, "media_send": self.media_ok,
                "reauth_days_left": reauth_days_left(self.meta, now), "auto_refresh": bool(self.meta.get("refresh_token")),
                "webhook": webhook_silence(stats, now), "last_error": self.last_error}

    def _report_session_health(self) -> None:
        now = self._now()
        days = reauth_days_left(self.meta, now)
        warn_days = days if (days is not None and days <= REAUTH_WARN_DAYS) else None
        st = ("region_unsupported" if self.dm_ok is False else self.token_state)
        key = (st, warn_days)
        if key == self._reported:
            return
        self._reported = key
        try:
            from src.integrations.platform_session_health import get_platform_session_health
            h = get_platform_session_health()
            if st == "needs_reauth":
                h.record(PLATFORM, self.account_id, "expired", detail="[rc:other] TikTok 授权失效，请重新授权 Business Account")
            elif st == "region_unsupported":
                h.record(PLATFORM, self.account_id, "blocked",
                         detail=f"[rc:forbidden] 注册地 {self.region} 不支持 Business Messaging API（EEA/瑞士/英国/美国）")
            elif warn_days is not None:
                # 提前 7 天预警：仍健康不禁发，detail 带倒计时进账号卡 / 面板
                how = "自动续期令牌" if self.meta.get("refresh_token") else "手填的 access_token"
                h.record(PLATFORM, self.account_id, "authorized",
                         detail=f"[rc:other] TikTok {how} {warn_days} 天后到期，请提前重新授权")
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
        if await self.maybe_refresh_tokens() == "needs_reauth" or not self.meta.get("access_token"):
            self.token_state = "needs_reauth"
            self._report_session_health()
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
        if not res.get("ok") and int(res.get("code") or 0) in TOKEN_CODES and self.meta.get("refresh_token"):
            # 鉴权失败先强制刷一次再重试一次；仍失败才判失效
            if await self.maybe_refresh_tokens(force=True) != "needs_reauth":
                res = await self.api.send_message(access_token=str(self.meta.get("access_token")),
                                                  business_id=self.account_id, conversation_id=conv,
                                                  message_type=message_type, payload=payload, referenced_message_id=ref)
        if res.get("ok"):
            self.last_error = ""
            mid = str(res.get("message_id") or "")
            if mid:
                # 我方发送成功的消息 id 入幂等表：随后到达的 im_send_msg 回显不再二次落库
                self.state.seen(f"{EVENT_SEND}:{mid}", now=self._now())
            return {"delivered": True, "message_id": mid, "kind": kind, "quote_applied": bool(ref)}
        code = int(res.get("code") or 0)
        self.last_error = f"{code}:{res.get('message') or ''}"
        out: Dict[str, Any] = {"delivered": False, "error": str(res.get("message") or f"tiktok_error_{code}"),
                               "error_kind": f"tiktok_error_{code}", "error_code": code}
        if code in TOKEN_CODES:
            self.token_state = "needs_reauth"
            self._persist_meta({"access_token": ""})
            self._report_session_health()
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

def _unwrap_content(payload: Dict[str, Any]) -> Dict[str, Any]:
    """官方外壳 ``{event, user_openid, content:"<JSON 字符串>"}``：``content`` 需二次解析；兼容 dict / ``message`` 壳 / 扁平。"""
    c = payload.get("content")
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except Exception:
            c = None
    if isinstance(c, dict):
        return c
    if isinstance(payload.get("message"), dict):
        return payload["message"]
    return payload


def _extract_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    """字段名对齐官方 webhook 载荷 / SDK MessageItem：conversation_id、message_id、timestamp(ms)、message_type、
    text.body、image.media_id、share_post.embed_url、from_user.id、to_user.id、referenced_message_info。"""
    m = _unwrap_content(payload)
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
    ref = m.get("referenced_message_info") if isinstance(m.get("referenced_message_info"), dict) else {}
    sp = m.get("share_post") if isinstance(m.get("share_post"), dict) else {}
    img = m.get("image") if isinstance(m.get("image"), dict) else {}
    referral = m.get("referral") if isinstance(m.get("referral"), dict) else {}
    return {
        "sender": str(m.get("sender") or fu.get("id") or ""), "recipient": str(m.get("recipient") or tu.get("id") or ""),
        "sender_name": str(m.get("from") or fu.get("display_name") or ""),
        "sender_role": str(fu.get("role") or "").upper(), "conversation_id": str(m.get("conversation_id") or ""),
        "message_id": str(m.get("message_id") or ""), "ts": ts, "text": text, "media_type": media, "message_type": mtype,
        "media_id": str(img.get("media_id") or ""), "embed_url": str(sp.get("embed_url") or ""),
        "referenced_message_id": str(ref.get("referenced_message_id") or ""), "referral": referral,
        "business_id": str(payload.get("user_openid") or payload.get("business_id") or m.get("business_id") or ""),
    }


async def handle_webhook(body: bytes, signature: Any, *, config: Optional[Dict[str, Any]],
                         state: Optional[TikTokStateStore] = None, now: Optional[float] = None,
                         emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                         auto_reply: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None
                         ) -> Tuple[int, Dict[str, Any]]:
    cfg = tiktok_cfg(config)
    t_now = float(now if now is not None else time.time())
    if cfg["verify_signature"] and not verify_signature(cfg["webhook_secret"], body, signature, now=t_now,
                                                        tolerance=cfg["signature_tolerance_sec"]):
        return 401, {"error": "bad_signature"}
    try:
        payload = json.loads(bytes(body or b"").decode("utf-8") or "{}")
    except Exception:
        return 400, {"error": "bad_json"}
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    # 注册回调时的探活：无 event 的空壳 / challenge 原样回显
    if payload.get("challenge") is not None and not payload.get("event"):
        return 200, {"challenge": payload.get("challenge")}
    event = str(payload.get("event") or payload.get("event_type") or "")
    st = state or get_state_store(cfg["state_db_path"] or None)
    m = _extract_message(payload)
    if not m["message_id"] and event in (EVENT_RECEIVE, EVENT_SEND):
        return 400, {"error": "missing_message_id"}
    if m["message_id"] and event in (EVENT_RECEIVE, EVENT_SEND) and st.seen(f"{event}:{m['message_id']}", now=t_now):
        return 200, {"ok": True, "dup": True}
    if emit is None or auto_reply is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit, maybe_auto_reply as _ar
        emit = emit or _emit
        auto_reply = auto_reply or _ar
    from src.integrations.protocol_bridge import make_message

    business_id = m["business_id"]
    if business_id:
        st.record_event(business_id, t_now)
    # 方向：官方载荷两个事件都可能出现「商家从 TikTok App 手发」的消息——以 to_user.id == business_id 判进线
    incoming = (event == EVENT_RECEIVE) if not business_id else (m["recipient"] == business_id)
    if event in (EVENT_RECEIVE, EVENT_SEND):
        if not business_id:
            business_id = m["recipient"] if event == EVENT_RECEIVE else m["sender"]
            st.record_event(business_id, t_now)
        user_id = m["sender"] if incoming else m["recipient"]
        source: Dict[str, Any] = {"conversation_id": m["conversation_id"], "message_type": m["message_type"],
                                  "server_message_id": m["message_id"]}
        if m["referenced_message_id"]:
            source["reply_to_msg_id"] = m["referenced_message_id"]
        if m["media_id"]:
            source["media_id"] = m["media_id"]
        if m["embed_url"]:
            source["embed_url"] = m["embed_url"]
        if m["referral"]:
            source["referral"] = m["referral"]
            sl = m["referral"].get("short_link") if isinstance(m["referral"], dict) else None
            if isinstance(sl, list) and sl and isinstance(sl[0], dict) and sl[0].get("ref"):
                source["ref"] = str(sl[0]["ref"])
        if incoming:
            st.record_inbound(business_id, user_id, conversation_id=m["conversation_id"], msg_id=m["message_id"],
                              ts=m["ts"] or t_now, ref=str(source.get("ref") or ""))
            msg = make_message(platform=PLATFORM, account_id=business_id, chat_key=chat_key_for(user_id), text=m["text"],
                               name=m["sender_name"], ts=m["ts"] or t_now, msg_id=m["message_id"], direction="in",
                               media_type=m["media_type"], source=source)
            emit(msg)
            try:
                await auto_reply(msg)
            except Exception:
                logger.debug("[tiktok-official] maybe_auto_reply 异常", exc_info=True)
            return 200, {"ok": True}
        source["echo"] = True
        emit(make_message(platform=PLATFORM, account_id=business_id, chat_key=chat_key_for(user_id), text=m["text"],
                          ts=m["ts"] or t_now, msg_id=m["message_id"], direction="out", media_type=m["media_type"],
                          source=source))
        return 200, {"ok": True, "echo": True}
    if event == EVENT_MARK_READ:
        logger.debug("[tiktok-official] 对方已读 business=%s conversation=%s", business_id, m["conversation_id"])
        return 200, {"ok": True, "read": True}
    if event == EVENT_HIGH_INTENT_COMMENT:
        # 高意向评论：只记录（Comment-to-Message 灰度，不在本骨架自动私信）
        logger.info("[tiktok-official] 高意向评论事件 business=%s（记录，不自动私信）", m["business_id"])
        return 200, {"ok": True, "noted": True}
    return 200, {"ok": True, "ignored": event}


def register_tiktok_routes(app: Any, config_manager: Any, telegram_client: Any = None) -> None:
    """TikTok 平台路由的唯一挂载口（admin.py / 接入面板热挂载都只调这里）：私信 webhook（``tiktok.enabled``）、
    店铺客服 webhook + 订单卡只读接口（``tiktok.shop.enabled``）、huoke 桥（``tiktok.huoke_bridge.enabled``）——各自独立门控。"""
    for _mount in ("src.integrations.tiktok_shop_cs:register_tiktok_shop_routes",
                   "src.integrations.tiktok_huoke_bridge:register_tiktok_huoke_routes"):
        try:
            import importlib
            _modname, _fn = _mount.split(":")
            getattr(importlib.import_module(_modname), _fn)(app, config_manager)
        except Exception:
            logger.debug("[tiktok-official] 子模块路由挂载跳过 %s", _mount, exc_info=True)
    cfg0 = tiktok_cfg(getattr(config_manager, "config", None) or {})
    if not cfg0["enabled"] or JSONResponse is None:
        return
    path = cfg0["webhook_path"]
    if any(getattr(r, "path", "") == path for r in getattr(app, "routes", [])):
        return

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


__all__ = ["PLATFORM", "MODE", "SEND_URL", "MEDIA_UPLOAD_URL", "EVENT_RECEIVE", "EVENT_SEND", "EVENT_MARK_READ",
           "EVENT_HIGH_INTENT_COMMENT", "AUTHORIZE_URL", "TOKEN_URL", "REFRESH_URL", "WEBHOOK_UPDATE_URL",
           "OAUTH_SCOPES", "MESSAGING_SCOPES", "DEFAULT_OAUTH_CALLBACK_PATH", "DEFAULT_SIGNATURE_HEADER",
           "tiktok_cfg", "official_enabled", "chat_key_for", "user_id_from_chat_key",
           "sign_body", "parse_signature_header", "verify_signature", "TikTokStateStore", "get_state_store",
           "TikTokApi", "token_state", "token_meta_from_grant", "missing_scopes", "oauth_state", "verify_oauth_state",
           "authorize_url", "complete_oauth", "ensure_webhook", "TikTokOfficialWorker", "handle_webhook",
           "register_tiktok_routes", "register_tiktok_official_worker", "sanitize_ref", "tiktok_me_link",
           "reauth_days_left", "webhook_silence", "REAUTH_WARN_DAYS", "WEBHOOK_SILENCE_SEC"]
