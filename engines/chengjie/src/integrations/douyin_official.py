# -*- coding: utf-8 -*-
"""抖音官方通道（小程序 IM）——API 客户端 + Webhook 处理 + 编排器 worker（实施96 P1-1 骨架，2026-09-08）。

**为什么现在就写**：企业主体小程序上线 + 能力实验室审核是 4–8 周的长尾，接口契约却已由文档钉死
（``/im/send/msg/`` 四种 scene、24h/6 条、进私事件 30 秒内 ≤3 条、``X-Douyin-Signature = sha1(secret+body)``、
``verify_webhook`` challenge 回显、access 15 天 / refresh 30 天 / 最多续 5 次）。全部 IO 经可注入的
``transport``，资质到手前用假 transport 把每条分支钉在测试里，到手当天换凭证即联调。

**形状照微信客服（实施97 ``wechat_kf_worker``）**：独立模块，一个 ``register_douyin_official_worker(config)``
注册 ``(douyin, official)`` 工厂；不改 ``official_api_worker.py``（他线在途）。收件箱 / 拟稿 / 三条回复引擎 /
出站镜像 / 策略层 / 回复窗执行器全部复用——入站按 ``make_message`` 形状 ``emit_incoming``，出站由编排器
调本 worker 的 ``send`` / ``send_media``。

**抖音特有的三件事本模块自己扛**：
1. **场景选择**：回复必须携带 24h 内有效的 ``server_message_id`` 与长期有效的 ``conversation_short_id``
   —— webhook 收到即落 ``peer_ctx``；进私事件 30 秒内用 ``im_enter_direct_msg`` 场景；都没有 → 不发
   （与 ``window_guard`` 的「无入站不许发」同口径）。
2. **进私快路径**：``im_enter_direct_msg`` 到达 30 秒内必须回话，等不了拟稿→人审；配置 ``douyin.enter_greeting``
   即刻发预置问候（过策略层），随后 AI 接管。
3. **令牌生命周期**：``healthy()`` 里顺手续期（access 到期前 1 天刷、refresh 到期前 3 天续、续满 5 次后
   ``needs_reauth`` 提醒重新扫码授权）。

**刻意不做**：主动私信（scene ``im_authorize_message``，需用户在小程序内授权）与 B2B 触达——等场景明确再加；
视频消息只能分享账号自己发布的视频（按 item_id），上传的视频文件**发不了**，故媒体白名单只有图片。
"""
from __future__ import annotations

import asyncio
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

try:  # 路由处理函数的注解必须在模块作用域可解析（from __future__ annotations 下局部导入会被当查询参数）
    from fastapi import Request
    from fastapi.responses import JSONResponse
except Exception:  # pragma: no cover - 非 web 进程（CLI/worker）无 fastapi 也能 import 本模块
    Request = Any  # type: ignore[misc,assignment]
    JSONResponse = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLATFORM = "douyin"
MODE = "official"

API_BASE = "https://open.douyin.com"
SEND_MSG_URL = f"{API_BASE}/im/send/msg/"
IMAGE_UPLOAD_URL = f"{API_BASE}/tool/imagex/client_upload/"
CLIENT_TOKEN_URL = f"{API_BASE}/oauth/client_token/"
REFRESH_TOKEN_URL = f"{API_BASE}/oauth/refresh_token/"
RENEW_REFRESH_URL = f"{API_BASE}/oauth/renew_refresh_token/"

SCENE_REPLY = "im_reply_msg"
SCENE_ENTER = "im_enter_direct_msg"
SCENE_B2B = "im_b2b_direct_message"
SCENE_AUTHORIZE = "im_authorize_message"

EVENT_VERIFY = "verify_webhook"
EVENT_RECEIVE = "im_receive_msg"
EVENT_SEND = "im_send_msg"
EVENT_ENTER = "im_enter_direct_msg"
EVENT_GROUP_RECEIVE = "im_group_receive_msg"

SIGNATURE_HEADER = "X-Douyin-Signature"
DEFAULT_WEBHOOK_PATH = "/webhook/douyin"

ENTER_FAST_PATH_SEC = 30.0          # 进私事件后 30 秒内可用 im_enter_direct_msg 场景
MSG_ID_TTL_SEC = 24 * 3600.0        # server_message_id 24 小时有效
ACCESS_TTL_SEC = 15 * 24 * 3600.0
REFRESH_TTL_SEC = 30 * 24 * 3600.0
RENEW_MAX = 5
REFRESH_AHEAD_SEC = 24 * 3600.0     # access 到期前 1 天刷
RENEW_AHEAD_SEC = 3 * 24 * 3600.0   # refresh 到期前 3 天续
CLIENT_TOKEN_TTL_SEC = 2 * 3600.0
SEEN_TTL_SEC = 3 * 24 * 3600.0      # 幂等表保留 3 天
TEXT_MAX = 1000

#: 平台错误码 → 本地原因码（``policy_*`` 进 send_gate_status 的 policy 族；token 类触发重新授权提醒）
ERROR_MAP: Dict[int, str] = {
    2190001: "douyin_quota_exhausted",
    2190002: "douyin_token_invalid",
    2190008: "douyin_token_expired",
    2190015: "douyin_token_openid_mismatch",
    28001038: "douyin_content_invalid",
    28003018: "douyin_rate_limited",
    28003070: "policy_window_exhausted",
    28003080: "policy_window_expired",
    28003081: "policy_window_expired",
    28003082: "douyin_peer_mismatch",
    28003095: "douyin_workbench_policy_active",
    28029004: "douyin_send_banned",
    28003101: "douyin_user_banned",
}
TOKEN_ERRORS = {"douyin_token_invalid", "douyin_token_expired", "douyin_token_openid_mismatch"}

#: 入站消息类型 → 收件箱占位（文本以外抖音不下发内容本体，只能占位）
_TYPE_PLACEHOLDER = {"image": "[图片]", "video": "[视频]", "emoji": "[表情]",
                     "retain_consult_card": "[留资卡回填]", "other": "[消息]"}


# ── 配置 / 键 ─────────────────────────────────────────────────────────────────

def douyin_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    blk: Dict[str, Any] = {}
    try:
        blk = dict((config or {}).get("douyin") or {})
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
        "client_key": str(blk.get("client_key") or ""),
        "client_secret": str(blk.get("client_secret") or ""),
        "webhook_path": str(blk.get("webhook_path") or DEFAULT_WEBHOOK_PATH),
        "enter_greeting": str(blk.get("enter_greeting") or ""),
        "enter_greeting_enabled": bool(blk.get("enter_greeting_enabled", True)),
        "state_db_path": str(blk.get("state_db_path") or ""),
    }


def official_enabled(config: Optional[Dict[str, Any]]) -> bool:
    return douyin_cfg(config)["enabled"]


def chat_key_for(open_id: Any) -> str:
    return f"douyin:user:{str(open_id or '').strip()}"


def open_id_from_chat_key(chat_key: Any) -> str:
    ck = str(chat_key or "").strip()
    return ck.rsplit(":", 1)[-1] if ":" in ck else ck


# ── 签名 ─────────────────────────────────────────────────────────────────────

def sign_body(client_secret: str, body: bytes) -> str:
    """``X-Douyin-Signature`` = sha1(client_secret + 原始 body) 的十六进制。**按字节**算，不解码。"""
    h = hashlib.sha1()
    h.update(str(client_secret or "").encode("utf-8"))
    h.update(bytes(body or b""))
    return h.hexdigest()


def verify_signature(client_secret: str, body: bytes, header_value: Any) -> bool:
    if not client_secret:
        return False
    want = sign_body(client_secret, body)
    got = str(header_value or "").strip().lower()
    return bool(got) and hmac.compare_digest(want, got)


# ── 状态库：peer 上下文（场景选择用）+ 幂等 ───────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS peer_ctx (
    account_id       TEXT NOT NULL,
    open_id          TEXT NOT NULL,
    conversation_id  TEXT NOT NULL DEFAULT '',
    last_msg_id      TEXT NOT NULL DEFAULT '',
    last_msg_ts      REAL NOT NULL DEFAULT 0,
    enter_msg_id     TEXT NOT NULL DEFAULT '',
    enter_ts         REAL NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, open_id)
);
CREATE TABLE IF NOT EXISTS seen_events (
    key        TEXT PRIMARY KEY,
    ts         REAL NOT NULL DEFAULT 0
);
"""


class DouyinStateStore:
    """SQLite（WAL）；``path=":memory:"`` 供单测。全部方法绝不抛（读挂＝无上下文）。"""

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
            return str(config_dir() / "douyin_official_state.db")
        except Exception:
            return os.path.join("config", "douyin_official_state.db")

    def get_ctx(self, account_id: str, open_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM peer_ctx WHERE account_id=? AND open_id=?",
                (str(account_id), str(open_id))).fetchone()
        return dict(row) if row else {}

    def record_inbound(self, account_id: str, open_id: str, *, conversation_id: str,
                       msg_id: str, ts: float) -> None:
        """客户消息到达：刷新 24h 回复凭据（旧于已记录的消息不回拨）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO peer_ctx(account_id, open_id, conversation_id, last_msg_id, last_msg_ts, "
                "updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(account_id, open_id) DO UPDATE SET "
                "conversation_id=CASE WHEN excluded.conversation_id<>'' THEN excluded.conversation_id "
                "ELSE peer_ctx.conversation_id END, "
                "last_msg_id=CASE WHEN excluded.last_msg_ts>=peer_ctx.last_msg_ts THEN excluded.last_msg_id "
                "ELSE peer_ctx.last_msg_id END, "
                "last_msg_ts=MAX(excluded.last_msg_ts, peer_ctx.last_msg_ts), updated_at=excluded.updated_at",
                (str(account_id), str(open_id), str(conversation_id or ""), str(msg_id or ""),
                 float(ts or 0), time.time()))
            self._conn.commit()

    def record_enter(self, account_id: str, open_id: str, *, conversation_id: str,
                     msg_id: str, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO peer_ctx(account_id, open_id, conversation_id, enter_msg_id, enter_ts, "
                "updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(account_id, open_id) DO UPDATE SET "
                "conversation_id=CASE WHEN excluded.conversation_id<>'' THEN excluded.conversation_id "
                "ELSE peer_ctx.conversation_id END, "
                "enter_msg_id=excluded.enter_msg_id, enter_ts=excluded.enter_ts, updated_at=excluded.updated_at",
                (str(account_id), str(open_id), str(conversation_id or ""), str(msg_id or ""),
                 float(ts or 0), time.time()))
            self._conn.commit()

    def seen(self, key: str, *, now: Optional[float] = None) -> bool:
        """幂等：首次见 → 记下并返回 False；重放 → True。顺手清 3 天前的记录。"""
        t = float(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM seen_events WHERE key=?", (str(key),)).fetchone()
            if row:
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


_STORE: Optional[DouyinStateStore] = None
_STORE_LOCK = threading.Lock()


def get_state_store(path: Optional[str] = None) -> DouyinStateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = DouyinStateStore(path)
        return _STORE


def _reset_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = None


# ── 场景选择（纯函数）──────────────────────────────────────────────────────────

def choose_scene(ctx: Dict[str, Any], now: float) -> Tuple[str, str, str, str]:
    """→ ``(scene, msg_id, conversation_id, block_reason)``；无可用场景时 scene 为空、block_reason 非空。

    优先进私快路径（30 秒内），其次 24h 内回复；都不行按窗口语义给原因（与 window_guard 同族）。
    """
    if not ctx:
        return "", "", "", "policy_window_no_inbound"
    enter_ts = float(ctx.get("enter_ts") or 0)
    enter_id = str(ctx.get("enter_msg_id") or "")
    conv = str(ctx.get("conversation_id") or "")
    if enter_id and enter_ts > 0 and 0 <= now - enter_ts <= ENTER_FAST_PATH_SEC:
        return SCENE_ENTER, enter_id, conv, ""
    last_ts = float(ctx.get("last_msg_ts") or 0)
    last_id = str(ctx.get("last_msg_id") or "")
    if last_id and last_ts > 0:
        if now - last_ts <= MSG_ID_TTL_SEC:
            return SCENE_REPLY, last_id, conv, ""
        return "", "", conv, "policy_window_expired"
    return "", "", conv, "policy_window_no_inbound"


# ── API 客户端 ─────────────────────────────────────────────────────────────────

Transport = Callable[..., Awaitable[Tuple[int, Dict[str, Any]]]]


async def _aiohttp_transport(method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                             params: Optional[Dict[str, Any]] = None,
                             json_body: Optional[Dict[str, Any]] = None,
                             form: Optional[Dict[str, Any]] = None,
                             file_field: Optional[Tuple[str, str]] = None,
                             timeout: float = 20.0) -> Tuple[int, Dict[str, Any]]:
    """默认传输：aiohttp。``file_field=(字段名, 本地路径)`` 走 multipart。返回 (HTTP 状态, 解析后的 JSON)。"""
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


def _err(body: Dict[str, Any]) -> Tuple[int, str]:
    """抖音两种错误壳：``extra.error_code`` / ``data.error_code``（0＝成功）。"""
    for holder in (body.get("extra") or {}, body.get("data") or {}):
        try:
            code = int((holder or {}).get("error_code", 0) or 0)
        except (TypeError, ValueError):
            code = 0
        if code:
            return code, str((holder or {}).get("description") or (holder or {}).get("sub_description") or "")
    return 0, ""


class DouyinApi:
    def __init__(self, transport: Optional[Transport] = None) -> None:
        self._t: Transport = transport or _aiohttp_transport

    async def send_msg(self, *, access_token: str, open_id: str, to_user_id: str, scene: str,
                       content: Dict[str, Any], msg_id: str = "", conversation_id: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"scene": scene, "to_user_id": to_user_id, "content": content}
        if msg_id:
            body["msg_id"] = msg_id
        if conversation_id:
            body["conversation_id"] = conversation_id
        status, data = await self._t("POST", SEND_MSG_URL, headers={
            "access-token": access_token, "Content-Type": "application/json"},
            params={"open_id": open_id}, json_body=body)
        code, desc = _err(data)
        if status != 200 or code:
            kind = ERROR_MAP.get(code, f"douyin_error_{code or status}")
            return {"ok": False, "error_code": code, "http": status, "description": desc, "kind": kind}
        return {"ok": True, "msg_id": str(data.get("msg_id") or ""), "http": status}

    async def client_token(self, client_key: str, client_secret: str) -> Dict[str, Any]:
        status, data = await self._t("POST", CLIENT_TOKEN_URL, headers={"Content-Type": "application/json"},
                                     json_body={"client_key": client_key, "client_secret": client_secret,
                                                "grant_type": "client_credential"})
        d = data.get("data") or {}
        code, desc = _err(data)
        if status != 200 or code or not d.get("access_token"):
            return {"ok": False, "error_code": code, "description": desc}
        return {"ok": True, "access_token": str(d.get("access_token")),
                "expires_in": float(d.get("expires_in") or CLIENT_TOKEN_TTL_SEC)}

    async def upload_image(self, client_token: str, path: str) -> Dict[str, Any]:
        status, data = await self._t("POST", IMAGE_UPLOAD_URL, headers={"access-token": client_token},
                                     file_field=("image", path))
        d = data.get("data") or {}
        code, desc = _err(data)
        if status != 200 or code or not d.get("image_id"):
            return {"ok": False, "error_code": code, "description": desc}
        return {"ok": True, "image_id": str(d.get("image_id"))}

    async def refresh_access_token(self, client_key: str, refresh_token: str) -> Dict[str, Any]:
        status, data = await self._t("POST", REFRESH_TOKEN_URL, form={
            "client_key": client_key, "grant_type": "refresh_token", "refresh_token": refresh_token})
        d = data.get("data") or {}
        code, desc = _err(data)
        if status != 200 or code or not d.get("access_token"):
            return {"ok": False, "error_code": code, "description": desc}
        return {"ok": True, "access_token": str(d.get("access_token")),
                "expires_in": float(d.get("expires_in") or ACCESS_TTL_SEC),
                "refresh_token": str(d.get("refresh_token") or refresh_token),
                "refresh_expires_in": float(d.get("refresh_expires_in") or 0) or None}

    async def renew_refresh_token(self, client_key: str, refresh_token: str) -> Dict[str, Any]:
        status, data = await self._t("POST", RENEW_REFRESH_URL, form={
            "client_key": client_key, "refresh_token": refresh_token})
        d = data.get("data") or {}
        code, desc = _err(data)
        if status != 200 or code or not d.get("refresh_token"):
            return {"ok": False, "error_code": code, "description": desc}
        return {"ok": True, "refresh_token": str(d.get("refresh_token")),
                "expires_in": float(d.get("expires_in") or REFRESH_TTL_SEC)}


# ── 内容构造 ─────────────────────────────────────────────────────────────────

def text_content(text: str) -> Dict[str, Any]:
    return {"msg_type": 1, "text": {"text": str(text or "")[:TEXT_MAX]}}


def image_content(media_id: str) -> Dict[str, Any]:
    return {"msg_type": 2, "image": {"media_id": str(media_id)}}


def retain_card_content(card_id: str) -> Dict[str, Any]:
    return {"msg_type": 8, "retain_consult_card": {"card_id": str(card_id)}}


def question_guide_content(title: str, questions: Any) -> Dict[str, Any]:
    qs = [str(q)[:14] for q in list(questions or [])[:6]]
    return {"msg_type": 204, "question_guide_msg_card": {"title": str(title or "")[:30],
                                                          "question_list": [{"text": q} for q in qs]}}


# ── 令牌生命周期（纯函数判定 + 异步执行）─────────────────────────────────────────

def token_state(meta: Dict[str, Any], now: float) -> str:
    """``ok`` / ``refresh_due``（access 快到期，refresh 可用）/ ``renew_due``（refresh 快到期且还能续）
    / ``needs_reauth``（无 token，或 refresh 已过期，或续满 5 次且 access 已过期）。"""
    at = str(meta.get("access_token") or "")
    rt = str(meta.get("refresh_token") or "")
    aexp = float(meta.get("access_expires_at") or 0)
    rexp = float(meta.get("refresh_expires_at") or 0)
    renew_count = int(meta.get("renew_count") or 0)
    if not at and not rt:
        return "needs_reauth"
    refresh_alive = bool(rt) and (rexp <= 0 or now < rexp)
    if not refresh_alive:
        return "ok" if (at and aexp > now) else "needs_reauth"
    if aexp and now >= aexp - REFRESH_AHEAD_SEC:
        return "refresh_due"
    if rexp and now >= rexp - RENEW_AHEAD_SEC:
        return "renew_due" if renew_count < RENEW_MAX else ("ok" if aexp > now else "needs_reauth")
    return "ok"


# ── Worker ──────────────────────────────────────────────────────────────────

class DouyinOfficialWorker:
    """编排器契约：``start/stop/healthy/status/send/send_media``（+ ``send_card`` 抖音专属）。"""

    def __init__(self, account: Dict[str, Any], config: Optional[Dict[str, Any]] = None, *,
                 api: Optional[DouyinApi] = None, state: Optional[DouyinStateStore] = None,
                 registry: Any = None, now: Optional[Callable[[], float]] = None) -> None:
        self.account_id = str((account or {}).get("account_id") or "")   # ＝经营者 open_id
        self.meta: Dict[str, Any] = dict((account or {}).get("meta") or {})
        self.config = config or {}
        self.cfg = douyin_cfg(self.config)
        self.api = api or DouyinApi()
        self._state = state
        self._registry = registry
        self._now = now or time.time
        self.running = False
        self.token_state = token_state(self.meta, self._now())
        self.last_error = ""
        self._client_token = ""
        self._client_token_exp = 0.0

    # ── 生命周期 ──
    @property
    def state(self) -> DouyinStateStore:
        if self._state is None:
            self._state = get_state_store(self.cfg["state_db_path"] or None)
        return self._state

    async def start(self) -> None:
        self.running = True
        await self.maybe_refresh_tokens()
        logger.info("[douyin-official] worker 启动 account=%s token=%s", self.account_id, self.token_state)

    async def stop(self) -> None:
        self.running = False

    async def healthy(self) -> bool:
        await self.maybe_refresh_tokens()
        return self.running and self.token_state != "needs_reauth"

    def status(self) -> Dict[str, Any]:
        return {"type": "douyin_official", "running": self.running, "token_state": self.token_state,
                "access_expires_at": float(self.meta.get("access_expires_at") or 0),
                "refresh_expires_at": float(self.meta.get("refresh_expires_at") or 0),
                "renew_count": int(self.meta.get("renew_count") or 0), "last_error": self.last_error}

    # ── 令牌 ──
    def _persist_meta(self, patch: Dict[str, Any]) -> None:
        self.meta.update(patch)
        try:
            reg = self._registry
            if reg is None:
                from src.integrations.account_registry import get_account_registry
                reg = get_account_registry()
            reg.upsert(PLATFORM, self.account_id, meta=dict(patch), merge_meta=True)
        except Exception:
            logger.debug("[douyin-official] 令牌落注册表失败", exc_info=True)

    async def maybe_refresh_tokens(self) -> str:
        """一次巡检最多做两步（先刷 access 再续 refresh），失败不抛只记 last_error。"""
        for _ in range(2):
            now = self._now()
            st = token_state(self.meta, now)
            if st not in ("refresh_due", "renew_due"):
                break
            await self._token_step(st, now)
        self.token_state = token_state(self.meta, self._now())
        if self.token_state == "needs_reauth":
            logger.warning("[douyin-official] account=%s 需要重新扫码授权（refresh 过期或续期已满 %d 次）",
                           self.account_id, RENEW_MAX)
        return self.token_state

    async def _token_step(self, st: str, now: float) -> None:
        try:
            if st == "refresh_due":
                r = await self.api.refresh_access_token(self.cfg["client_key"], str(self.meta.get("refresh_token")))
                if r.get("ok"):
                    patch = {"access_token": r["access_token"],
                             "access_expires_at": now + float(r.get("expires_in") or ACCESS_TTL_SEC)}
                    if r.get("refresh_token"):
                        patch["refresh_token"] = r["refresh_token"]
                    if r.get("refresh_expires_in"):
                        patch["refresh_expires_at"] = now + float(r["refresh_expires_in"])
                    self._persist_meta(patch)
                else:
                    self.last_error = f"refresh_failed:{r.get('error_code')}"
            elif st == "renew_due":
                r = await self.api.renew_refresh_token(self.cfg["client_key"], str(self.meta.get("refresh_token")))
                if r.get("ok"):
                    self._persist_meta({"refresh_token": r["refresh_token"],
                                        "refresh_expires_at": now + float(r.get("expires_in") or REFRESH_TTL_SEC),
                                        "renew_count": int(self.meta.get("renew_count") or 0) + 1})
                else:
                    self.last_error = f"renew_failed:{r.get('error_code')}"
        except Exception:
            logger.debug("[douyin-official] 令牌续期异常", exc_info=True)

    async def _client_token_value(self) -> str:
        now = self._now()
        if self._client_token and now < self._client_token_exp - 60:
            return self._client_token
        r = await self.api.client_token(self.cfg["client_key"], self.cfg["client_secret"])
        if r.get("ok"):
            self._client_token = r["access_token"]
            self._client_token_exp = now + float(r.get("expires_in") or CLIENT_TOKEN_TTL_SEC)
        return self._client_token

    # ── 出站 ──
    async def _deliver(self, chat_key: str, content: Dict[str, Any], *, kind: str) -> Dict[str, Any]:
        open_id = open_id_from_chat_key(chat_key)
        if not open_id:
            return {"delivered": False, "blocked": "douyin_bad_chat_key"}
        if self.token_state == "needs_reauth" or not self.meta.get("access_token"):
            return {"delivered": False, "blocked": "douyin_needs_reauth",
                    "error": "抖音授权已失效，请重新扫码授权"}
        now = self._now()
        scene, msg_id, conv, why = choose_scene(self.state.get_ctx(self.account_id, open_id), now)
        if not scene:
            return {"delivered": False, "blocked": why}
        res = await self.api.send_msg(access_token=str(self.meta.get("access_token")),
                                      open_id=self.account_id, to_user_id=open_id, scene=scene,
                                      content=content, msg_id=msg_id, conversation_id=conv)
        if res.get("ok"):
            self.last_error = ""
            return {"delivered": True, "message_id": str(res.get("msg_id") or ""), "scene": scene, "kind": kind}
        k = str(res.get("kind") or "douyin_error")
        self.last_error = f"{k}:{res.get('description') or ''}"
        if k in TOKEN_ERRORS:
            self._persist_meta({"access_token": ""})
            self.token_state = token_state(self.meta, self._now())
        out: Dict[str, Any] = {"delivered": False, "error": str(res.get("description") or k),
                               "error_kind": k, "error_code": res.get("error_code")}
        if k.startswith("policy_") or k in TOKEN_ERRORS or k in (
                "douyin_workbench_policy_active", "douyin_send_banned", "douyin_user_banned"):
            out["blocked"] = k
        return out

    async def send(self, chat_key: str, text: str, *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        t = str(text or "")
        if len(t) > TEXT_MAX:
            return {"delivered": False, "blocked": f"policy_text_too_long:{len(t)}/{TEXT_MAX}"}
        return await self._deliver(chat_key, text_content(t), kind="text")

    async def send_media(self, chat_key: str, *, media_path: str, media_type: str,
                         caption: str = "") -> Dict[str, Any]:
        mt = str(media_type or "").lower().split("/", 1)[0]
        if mt not in ("image", "photo"):
            # 视频只能按 item_id 分享账号自己发布的作品，上传文件发不了；语音/贴纸/文件平台无此类型
            return {"delivered": False, "blocked": f"policy_media_type_denied:{mt or 'unknown'}"}
        ct = await self._client_token_value()
        if not ct:
            return {"delivered": False, "error": "获取 client_token 失败", "error_kind": "douyin_client_token"}
        up = await self.api.upload_image(ct, media_path)
        if not up.get("ok"):
            return {"delivered": False, "error": f"图片上传失败 {up.get('error_code')}",
                    "error_kind": "douyin_image_upload"}
        res = await self._deliver(chat_key, image_content(up["image_id"]), kind="image")
        if res.get("delivered") and str(caption or "").strip():
            # 配文另发一条文本（抖音图片消息无 caption 字段）；失败不影响图片已送达的事实
            cap = await self._deliver(chat_key, text_content(caption), kind="text")
            res["caption_delivered"] = bool(cap.get("delivered"))
        return res

    async def send_card(self, chat_key: str, card: Dict[str, Any]) -> Dict[str, Any]:
        """留资卡 ``{"type":"retain","card_id":…}`` / 问题引导卡 ``{"type":"question","title":…,"questions":[…]}``。"""
        kind = str((card or {}).get("type") or "")
        if kind == "retain":
            content = retain_card_content(str(card.get("card_id") or ""))
        elif kind == "question":
            content = question_guide_content(str(card.get("title") or ""), card.get("questions") or [])
        else:
            return {"delivered": False, "blocked": "douyin_unknown_card"}
        return await self._deliver(chat_key, content, kind=f"card:{kind}")

    async def send_enter_greeting(self, open_id: str) -> Dict[str, Any]:
        """进私快路径：事件到达 30 秒内用 ``im_enter_direct_msg`` 场景发预置问候（过策略层）。"""
        text = self.cfg["enter_greeting"].strip()
        if not text or not self.cfg["enter_greeting_enabled"]:
            return {"delivered": False, "blocked": "douyin_enter_greeting_disabled"}
        try:
            from src.inbox.channel_policy import text_block_reason
            why = text_block_reason(PLATFORM, text, config=self.config)
            if why:
                return {"delivered": False, "blocked": why}
        except Exception:
            pass
        return await self._deliver(chat_key_for(open_id), text_content(text), kind="enter_greeting")


# ── Webhook 处理 ───────────────────────────────────────────────────────────────

def _content_text(content: Dict[str, Any]) -> Tuple[str, str]:
    """(收件箱文本, media_type)。文本消息取 ``text``；其它类型只下发类型 → 占位。"""
    mtype = str(content.get("message_type") or "text").lower()
    if mtype == "text":
        txt = content.get("text")
        if isinstance(txt, dict):
            txt = txt.get("text")
        return str(txt or ""), ""
    if mtype == "emoji":
        txt = content.get("text")
        return (str(txt) if isinstance(txt, str) and txt else "[表情]"), ""
    media = {"image": "image", "video": "video"}.get(mtype, "")
    return _TYPE_PLACEHOLDER.get(mtype, "[消息]"), media


async def handle_webhook(body: bytes, signature: Any, *, config: Optional[Dict[str, Any]],
                         registry: Any = None, state: Optional[DouyinStateStore] = None,
                         api: Optional[DouyinApi] = None, now: Optional[float] = None,
                         emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                         auto_reply: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None
                         ) -> Tuple[int, Dict[str, Any]]:
    """抖音开放平台 webhook → ``(HTTP 状态, 响应体)``。验签 → challenge → 幂等 → 分事件处理。

    入站消息按 ``make_message`` 形状 ``emit_incoming``（与 Node 边车 ingest 同路），再
    ``maybe_auto_reply``——收件箱 / SSE / System Z / B 线 / 策略层全部复用，不在 webhook 里自答。
    """
    cfg = douyin_cfg(config)
    if not verify_signature(cfg["client_secret"], body, signature):
        return 401, {"error": "bad_signature"}
    try:
        payload = json.loads(bytes(body or b"").decode("utf-8") or "{}")
    except Exception:
        return 400, {"error": "bad_json"}
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    event = str(payload.get("event") or "")
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    if event == EVENT_VERIFY:
        return 200, {"challenge": content.get("challenge")}
    t_now = float(now if now is not None else time.time())
    st = state or get_state_store(cfg["state_db_path"] or None)
    from_user = str(payload.get("from_user_id") or "")
    to_user = str(payload.get("to_user_id") or "")
    conv = str(content.get("conversation_short_id") or "")
    smid = str(content.get("server_message_id") or "")
    try:
        ts = float(content.get("create_time") or 0) / 1000.0 or t_now
    except (TypeError, ValueError):
        ts = t_now
    key = f"{event}:{smid or payload.get('log_id') or ''}:{from_user}:{to_user}"
    if smid and st.seen(key, now=t_now):
        return 200, {"ok": True, "dup": True}

    if emit is None or auto_reply is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit, maybe_auto_reply as _ar
        emit = emit or _emit
        auto_reply = auto_reply or _ar
    from src.integrations.protocol_bridge import make_message

    if event in (EVENT_RECEIVE, EVENT_GROUP_RECEIVE):
        account_id, peer = to_user, from_user          # 经营者收到客户消息
        text, media_type = _content_text(content)
        is_group = event == EVENT_GROUP_RECEIVE
        # 群消息：会话键用群会话 id（分流到「群组动态」，不进 SLA/自动回复），发言人进 sender_id
        ck = f"douyin:group:{conv or peer}" if is_group else chat_key_for(peer)
        if not is_group:
            st.record_inbound(account_id, peer, conversation_id=conv, msg_id=smid, ts=ts)
        msg = make_message(platform=PLATFORM, account_id=account_id, chat_key=ck,
                           text=text, ts=ts, msg_id=smid, direction="in", media_type=media_type,
                           source={"conversation_id": conv, "server_message_id": smid,
                                   "message_type": str(content.get("message_type") or "text"),
                                   "chat_type": "group" if is_group else "",
                                   "sender_id": peer if is_group else ""})
        emit(msg)
        try:
            await auto_reply(msg)
        except Exception:
            logger.debug("[douyin-official] maybe_auto_reply 异常", exc_info=True)
        return 200, {"ok": True}

    if event == EVENT_SEND:
        # 经营者侧（含 App 内人工 / 本系统）发出的私信回显：镜像为出站；本系统自己发的同 msg_id 落库幂等
        account_id, peer = from_user, to_user
        text, media_type = _content_text(content)
        emit(make_message(platform=PLATFORM, account_id=account_id, chat_key=chat_key_for(peer),
                          text=text, ts=ts, msg_id=smid, direction="out", media_type=media_type,
                          source={"conversation_id": conv, "server_message_id": smid,
                                  "echo": True, "app_source": str(content.get("source") or "")}))
        return 200, {"ok": True}

    if event == EVENT_ENTER:
        account_id, peer = to_user, from_user
        st.record_enter(account_id, peer, conversation_id=conv, msg_id=smid, ts=t_now)
        greeted: Dict[str, Any] = {}
        try:
            reg = registry
            if reg is None:
                from src.integrations.account_registry import get_account_registry
                reg = get_account_registry()
            row = reg.get(PLATFORM, account_id) or {}
            if row:
                w = DouyinOfficialWorker(row, config, api=api, state=st, registry=reg, now=lambda: t_now)
                greeted = await w.send_enter_greeting(peer)
                if greeted.get("delivered"):
                    emit(make_message(platform=PLATFORM, account_id=account_id, chat_key=chat_key_for(peer),
                                      text=cfg["enter_greeting"].strip(), ts=t_now,
                                      msg_id=str(greeted.get("message_id") or ""), direction="out",
                                      source={"conversation_id": conv, "scene": SCENE_ENTER}))
        except Exception:
            logger.debug("[douyin-official] 进私问候失败", exc_info=True)
        return 200, {"ok": True, "greeted": bool(greeted.get("delivered"))}

    return 200, {"ok": True, "ignored": event}


def register_douyin_routes(app: Any, config_manager: Any, telegram_client: Any = None) -> None:
    """挂 ``POST {douyin.webhook_path}``（默认 ``/webhook/douyin``）。未启用则不挂（零痕迹）。"""
    cfg0 = douyin_cfg(getattr(config_manager, "config", None) or {})
    if not cfg0["enabled"] or JSONResponse is None:
        return

    path = cfg0["webhook_path"]

    @app.post(path)
    async def douyin_webhook(request: Request):  # noqa: D401
        body = await request.body()
        cfg = getattr(config_manager, "config", None) or {}
        status, resp = await handle_webhook(body, request.headers.get(SIGNATURE_HEADER), config=cfg)
        return JSONResponse(resp, status_code=status)

    logger.info("[douyin-official] webhook 已挂载 %s", path)


def register_douyin_official_worker(config: Optional[Dict[str, Any]], *, registry: Any = None) -> bool:
    """``douyin.enabled``（或 ``platform_login.official.douyin.enabled``）为真时注册 ``(douyin, official)`` 工厂。"""
    if not official_enabled(config):
        return False
    from src.integrations.account_orchestrator import get_worker_factory, register_worker
    if get_worker_factory(PLATFORM, MODE) is None:
        register_worker(PLATFORM, MODE, lambda acc, cfg: DouyinOfficialWorker(acc, cfg, registry=registry))
        logger.info("[douyin-official] 已注册 (douyin, official) worker 工厂")
    return True


__all__ = [
    "PLATFORM", "MODE", "SEND_MSG_URL", "IMAGE_UPLOAD_URL", "SCENE_REPLY", "SCENE_ENTER",
    "EVENT_VERIFY", "EVENT_RECEIVE", "EVENT_SEND", "EVENT_ENTER", "SIGNATURE_HEADER", "ERROR_MAP",
    "douyin_cfg", "official_enabled", "chat_key_for", "open_id_from_chat_key", "sign_body",
    "verify_signature", "DouyinStateStore", "get_state_store", "choose_scene", "DouyinApi",
    "text_content", "image_content", "retain_card_content", "question_guide_content", "token_state",
    "DouyinOfficialWorker", "handle_webhook", "register_douyin_routes", "register_douyin_official_worker",
]
