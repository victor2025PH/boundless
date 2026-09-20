# -*- coding: utf-8 -*-
"""TikTok Shop 店铺客服（Customer Service API，Partner Center「Customer Service」类目）——API 客户端 + Webhook +
编排器 worker（指令 TikTok 线续做 C 段，2026-09-10）。

与 ``tiktok_official.py``（Business Messaging：企业号私信）**同一个 ``tiktok`` 平台、同一个 ``official`` mode**，
按账号 ``meta.source == "shop"`` 分流：账号 ``account_id = shop_id``，会话键 ``tiktok:shop:<conversation_id>``
（文档写法 ``tiktok/shop/<id>``；代码里与 ``tiktok:user:<id>`` 同用冒号分层），消息 ``source.source = "shop"``。
收件箱 / 拟稿 / ``channel_policy`` / ``window_guard`` 全部复用——发送经 ``AccountOrchestrator.send`` 与私信同一道
策略层（文本 ≤6000、首条禁链、仅图片）。Shop 会话按事实表**无 48h 窗**，``window_guard`` 按平台声明仍以 48h/10
保守判定（买家先发，无入站不许发本就成立）；按 source 放宽归 TK-2。

**接口形状**（2026-09-10 按 partner.tiktokshop.com docv2 202309 版核实；以当日文档为准，差异写落点表）：
- Base ``https://open-api.tiktokglobalshop.com``；每个请求 query 带 ``app_key`` / ``timestamp`` / ``sign`` / ``shop_cipher``，
  header ``x-tts-access-token``；``sign = HMAC-SHA256(app_secret, app_secret + path + Σ(sorted k+v，不含 sign/access_token) + body + app_secret)``。
- 发送 ``POST /customer_service/202309/conversations/{conversation_id}/messages``：``{"type": "TEXT"|"IMAGE", "content": "<JSON 字符串>"}``
  → ``data.message_id``；文本 ``{"content": "..."}``、图片 ``{"url","width","height"}``（先 ``POST /customer_service/202309/images/upload``）。
- 已读 ``POST .../conversations/{conversation_id}/messages/read``。
- 补拉 ``GET .../conversations/{conversation_id}/messages``（``page_size`` ≤ 10、``page_token`` / ``next_page_token``，拉取不置已读）。
- 订单卡只读：``GET /order/202309/orders?ids=<order_id>`` → 摘要进线程侧栏（不做任何订单操作）。
- Webhook：``NEW_MESSAGE``（event type 14）；签名在 ``Authorization`` 头（无 Bearer 前缀），
  ``HMAC-SHA256(app_secret, app_key + raw_body)`` 小写 hex；**3 秒内回 200**（本模块同步落库即回，重活留补拉）；
  幂等键 ``tts_notification_id`` + ``message_id``。载荷 ``{type, tts_notification_id, shop_id, timestamp, data:{conversation_id,
  message_id, index, create_time, type, visibility, content, sender:{role, im_user_id, nickname, avatar}}}``。
- 令牌：``GET https://auth.tiktok-shops.com/api/v2/token/refresh?app_key&app_secret&refresh_token&grant_type=refresh_token``
  → ``data.access_token / access_token_expire_in（epoch 秒）/ refresh_token / refresh_token_expire_in``。

**刻意不做**：订单 / 物流 / 售后操作（``ecommerce_tools`` 的 TikTok 连接器归 TK-2）、坐席设置（agent settings）、
New Conversation 事件的转人工 / 排队映射（只记日志）。开关 ``tiktok.shop.enabled``（默认关）：不开则不注册工厂、不挂路由、零痕迹。
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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

try:
    from fastapi import Depends, Request
    from fastapi.responses import JSONResponse
except Exception:  # pragma: no cover
    Request = Any  # type: ignore[misc,assignment]
    Depends = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLATFORM = "tiktok"
MODE = "official"
SOURCE = "shop"
CHAT_PREFIX = f"{PLATFORM}:{SOURCE}:"

API_BASE = "https://open-api.tiktokglobalshop.com"
AUTH_BASE = "https://auth.tiktok-shops.com"
CS_VERSION = "202309"
CONVERSATIONS_PATH = f"/customer_service/{CS_VERSION}/conversations"
IMAGE_UPLOAD_PATH = f"/customer_service/{CS_VERSION}/images/upload"
ORDERS_PATH = "/order/202309/orders"
TOKEN_REFRESH_PATH = "/api/v2/token/refresh"

EVENT_NEW_MESSAGE = 14
EVENT_NEW_CONVERSATION = 15

DEFAULT_WEBHOOK_PATH = "/webhook/tiktok/shop"
DEFAULT_SIGNATURE_HEADER = "Authorization"
ORDER_CARD_ROUTE = "/api/tiktok/shop/order-card"
TEXT_MAX = 6000
IMAGE_MAX_BYTES = 3 * 1024 * 1024
PAGE_SIZE_MAX = 10
SEEN_TTL_SEC = 3 * 24 * 3600.0
REFRESH_AHEAD_SEC = 30 * 60.0
ORDER_CACHE_TTL_SEC = 10 * 60.0

#: Shop 开放平台鉴权类错误码（token 失效 / 未授权 / 权限不足）
TOKEN_CODES = {105001, 105002, 105003, 105004, 36004001, 36004002}

_TYPE_PLACEHOLDER = {"IMAGE": "[图片]", "VIDEO": "[视频]", "ORDER_CARD": "[订单卡片]", "PRODUCT_CARD": "[商品卡片]",
                     "STICKER": "[贴纸]", "COUPON_CARD": "[优惠券]", "SYSTEM": "[系统消息]"}


# ── 配置 ─────────────────────────────────────────────────────────────────────

def shop_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``tiktok.shop.*``：Partner Center 应用凭证（app_key / app_secret）与 webhook 路径。默认关。"""
    tk: Dict[str, Any] = {}
    try:
        tk = dict((config or {}).get("tiktok") or {})
    except Exception:
        tk = {}
    blk = tk.get("shop") if isinstance(tk.get("shop"), dict) else {}
    return {
        "enabled": bool(blk.get("enabled", False)),
        "app_key": str(blk.get("app_key") or ""),
        "app_secret": str(blk.get("app_secret") or ""),
        "webhook_path": str(blk.get("webhook_path") or DEFAULT_WEBHOOK_PATH),
        "webhook_signature_header": str(blk.get("webhook_signature_header") or DEFAULT_SIGNATURE_HEADER),
        "verify_signature": bool(blk.get("verify_signature", True)),
        "state_db_path": str(blk.get("state_db_path") or ""),
    }


def shop_enabled(config: Optional[Dict[str, Any]]) -> bool:
    return shop_cfg(config)["enabled"]


def is_shop_account(account: Optional[Dict[str, Any]]) -> bool:
    """同一 ``tiktok/official`` 工厂下按账号 meta 分流：``meta.source == "shop"``（兼容 ``meta.kind``）。"""
    meta = (account or {}).get("meta") if isinstance(account, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    return str(meta.get("source") or meta.get("kind") or "").lower() == SOURCE


def chat_key_for(conversation_id: Any) -> str:
    return f"{CHAT_PREFIX}{str(conversation_id or '').strip()}"


def conversation_id_from_chat_key(chat_key: Any) -> str:
    ck = str(chat_key or "").strip()
    return ck[len(CHAT_PREFIX):] if ck.startswith(CHAT_PREFIX) else ck.rsplit(":", 1)[-1]


# ── 签名 ─────────────────────────────────────────────────────────────────────

def sign_webhook(app_secret: str, app_key: str, body: bytes) -> str:
    """``Authorization`` 头的值：``HMAC-SHA256(app_secret, app_key + raw_body)`` 小写 hex。"""
    return hmac.new(str(app_secret or "").encode("utf-8"), str(app_key or "").encode("utf-8") + bytes(body or b""),
                    hashlib.sha256).hexdigest()


def verify_webhook(app_secret: str, app_key: str, body: bytes, header_value: Any) -> bool:
    if not app_secret or not app_key:
        return False
    got = str(header_value or "").strip()
    if got.lower().startswith("bearer "):
        got = got[7:].strip()
    if not got:
        return False
    return hmac.compare_digest(sign_webhook(app_secret, app_key, body), got.lower())


def sign_request(app_secret: str, path: str, params: Dict[str, Any], body: bytes = b"") -> str:
    """开放平台请求签名：``HMAC-SHA256(app_secret, app_secret + path + Σ(k+v 按 k 排序，不含 sign/access_token) + body + app_secret)``。"""
    keys = sorted(k for k in params if k not in ("sign", "access_token"))
    base = str(path) + "".join(f"{k}{params[k]}" for k in keys)
    if body:
        base += bytes(body).decode("utf-8", "replace")
    base = str(app_secret) + base + str(app_secret)
    return hmac.new(str(app_secret).encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


# ── 状态库 ───────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS seen_events (key TEXT PRIMARY KEY, ts REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS conv_ctx (
    shop_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    buyer_id        TEXT NOT NULL DEFAULT '',
    buyer_name      TEXT NOT NULL DEFAULT '',
    last_index      INTEGER NOT NULL DEFAULT 0,
    last_msg_ts     REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (shop_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS order_cards (
    shop_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '{}',
    fetched_at      REAL NOT NULL DEFAULT 0,
    seen_at         REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (shop_id, conversation_id, order_id)
);
CREATE TABLE IF NOT EXISTS shop_stats (
    shop_id         TEXT PRIMARY KEY,
    first_event_ts  REAL NOT NULL DEFAULT 0,
    last_event_ts   REAL NOT NULL DEFAULT 0,
    events_total    INTEGER NOT NULL DEFAULT 0
);
"""


class TikTokShopStateStore:
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
            return str(config_dir() / "tiktok_shop_cs_state.db")
        except Exception:
            return os.path.join("config", "tiktok_shop_cs_state.db")

    def seen(self, key: str, *, now: Optional[float] = None) -> bool:
        t = float(now if now is not None else time.time())
        with self._lock:
            if self._conn.execute("SELECT 1 FROM seen_events WHERE key=?", (str(key),)).fetchone():
                return True
            self._conn.execute("INSERT OR IGNORE INTO seen_events(key, ts) VALUES(?,?)", (str(key), t))
            self._conn.execute("DELETE FROM seen_events WHERE ts < ?", (t - SEEN_TTL_SEC,))
            self._conn.commit()
        return False

    def record_inbound(self, shop_id: str, conversation_id: str, *, buyer_id: str = "", buyer_name: str = "",
                       index: int = 0, ts: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO conv_ctx(shop_id, conversation_id, buyer_id, buyer_name, last_index, last_msg_ts, updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(shop_id, conversation_id) DO UPDATE SET "
                "buyer_id=CASE WHEN excluded.buyer_id<>'' THEN excluded.buyer_id ELSE conv_ctx.buyer_id END, "
                "buyer_name=CASE WHEN excluded.buyer_name<>'' THEN excluded.buyer_name ELSE conv_ctx.buyer_name END, "
                "last_index=MAX(excluded.last_index, conv_ctx.last_index), "
                "last_msg_ts=MAX(excluded.last_msg_ts, conv_ctx.last_msg_ts), updated_at=excluded.updated_at",
                (str(shop_id), str(conversation_id), str(buyer_id or ""), str(buyer_name or ""), int(index or 0),
                 float(ts or 0), time.time()))
            self._conn.commit()

    def get_ctx(self, shop_id: str, conversation_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM conv_ctx WHERE shop_id=? AND conversation_id=?",
                                     (str(shop_id), str(conversation_id))).fetchone()
        return dict(row) if row else {}

    def record_event(self, shop_id: str, ts: Optional[float] = None) -> None:
        if not shop_id:
            return
        t = float(ts if ts is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO shop_stats(shop_id, first_event_ts, last_event_ts, events_total) VALUES(?,?,?,1) "
                "ON CONFLICT(shop_id) DO UPDATE SET last_event_ts=MAX(excluded.last_event_ts, shop_stats.last_event_ts), "
                "events_total=shop_stats.events_total+1", (str(shop_id), t, t))
            self._conn.commit()

    def stats(self, shop_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM shop_stats WHERE shop_id=?", (str(shop_id),)).fetchone()
        return dict(row) if row else {"shop_id": str(shop_id), "first_event_ts": 0.0, "last_event_ts": 0.0, "events_total": 0}

    # ── 订单卡（只读侧栏）──
    def note_order_card(self, shop_id: str, conversation_id: str, order_id: str, *, now: Optional[float] = None) -> None:
        t = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO order_cards(shop_id, conversation_id, order_id, summary, fetched_at, seen_at) VALUES(?,?,?,'{}',0,?) "
                "ON CONFLICT(shop_id, conversation_id, order_id) DO UPDATE SET seen_at=excluded.seen_at",
                (str(shop_id), str(conversation_id), str(order_id), t))
            self._conn.commit()

    def put_order_summary(self, shop_id: str, conversation_id: str, order_id: str, summary: Dict[str, Any], *,
                          now: Optional[float] = None) -> None:
        t = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO order_cards(shop_id, conversation_id, order_id, summary, fetched_at, seen_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(shop_id, conversation_id, order_id) DO UPDATE SET summary=excluded.summary, fetched_at=excluded.fetched_at",
                (str(shop_id), str(conversation_id), str(order_id), json.dumps(summary or {}, ensure_ascii=False), t, t))
            self._conn.commit()

    def order_cards(self, shop_id: str, conversation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT order_id, summary, fetched_at, seen_at FROM order_cards WHERE shop_id=? AND conversation_id=? "
                "ORDER BY seen_at DESC LIMIT 20", (str(shop_id), str(conversation_id))).fetchall()
        out = []
        for r in rows:
            try:
                summ = json.loads(r["summary"] or "{}")
            except Exception:
                summ = {}
            out.append({"order_id": str(r["order_id"]), "summary": summ if isinstance(summ, dict) else {},
                        "fetched_at": float(r["fetched_at"] or 0), "seen_at": float(r["seen_at"] or 0)})
        return out

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_STORE: Optional[TikTokShopStateStore] = None
_STORE_LOCK = threading.Lock()


def get_state_store(path: Optional[str] = None) -> TikTokShopStateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = TikTokShopStateStore(path)
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


def text_content(text: str) -> str:
    return json.dumps({"content": str(text or "")}, ensure_ascii=False)


def image_content(url: str, width: int = 0, height: int = 0) -> str:
    return json.dumps({"url": str(url or ""), "width": int(width or 0), "height": int(height or 0)}, ensure_ascii=False)


def parse_content(raw: Any) -> Dict[str, Any]:
    """``content`` 是二次序列化的 JSON 字符串；坏值 → ``{}``；已是 dict 原样。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {"content": str(d)}
        except Exception:
            return {"content": raw}
    return {}


class TikTokShopApi:
    """签名与 shop_cipher / access_token 注入集中在 ``_call``；业务方法只关心路径与正文。"""

    def __init__(self, transport: Optional[Transport] = None, *, app_key: str = "", app_secret: str = "",
                 now: Optional[Callable[[], float]] = None) -> None:
        self._t: Transport = transport or _aiohttp_transport
        self.app_key = str(app_key or "")
        self.app_secret = str(app_secret or "")
        self._now = now or time.time

    async def _call(self, method: str, path: str, *, access_token: str, shop_cipher: str = "",
                    query: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None,
                    form: Optional[Dict[str, Any]] = None, file_field: Optional[Tuple[str, str]] = None
                    ) -> Tuple[int, Dict[str, Any]]:
        params: Dict[str, Any] = {"app_key": self.app_key, "timestamp": int(self._now())}
        if shop_cipher:
            params["shop_cipher"] = shop_cipher
        for k, v in (query or {}).items():
            if v is not None and v != "":
                params[k] = v
        body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if json_body is not None else b""
        params["sign"] = sign_request(self.app_secret, path, params, body)
        headers = {"x-tts-access-token": str(access_token or "")}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        return await self._t(method, API_BASE + path, headers=headers, params=params, json_body=json_body,
                             form=form, file_field=file_field)

    async def send_message(self, *, access_token: str, shop_cipher: str, conversation_id: str, message_type: str,
                           content: str) -> Dict[str, Any]:
        status, data = await self._call("POST", f"{CONVERSATIONS_PATH}/{conversation_id}/messages", access_token=access_token,
                                        shop_cipher=shop_cipher, json_body={"type": str(message_type).upper(), "content": content})
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "http": status, "message": msg, "request_id": str(data.get("request_id") or "")}
        return {"ok": True, "message_id": str((data.get("data") or {}).get("message_id") or ""),
                "request_id": str(data.get("request_id") or "")}

    async def mark_read(self, *, access_token: str, shop_cipher: str, conversation_id: str) -> Dict[str, Any]:
        status, data = await self._call("POST", f"{CONVERSATIONS_PATH}/{conversation_id}/messages/read",
                                        access_token=access_token, shop_cipher=shop_cipher, json_body={})
        code, msg = _code(data)
        return {"ok": status == 200 and not code, "code": code, "message": msg}

    async def list_messages(self, *, access_token: str, shop_cipher: str, conversation_id: str,
                            page_token: str = "", page_size: int = PAGE_SIZE_MAX, locale: str = "") -> Dict[str, Any]:
        q: Dict[str, Any] = {"page_size": max(1, min(int(page_size or PAGE_SIZE_MAX), PAGE_SIZE_MAX))}
        if page_token:
            q["page_token"] = page_token
        if locale:
            q["locale"] = locale
        status, data = await self._call("GET", f"{CONVERSATIONS_PATH}/{conversation_id}/messages", access_token=access_token,
                                        shop_cipher=shop_cipher, query=q)
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "message": msg}
        d = data.get("data") or {}
        return {"ok": True, "messages": list(d.get("messages") or []), "next_page_token": str(d.get("next_page_token") or "")}

    async def upload_image(self, *, access_token: str, path: str) -> Dict[str, Any]:
        status, data = await self._call("POST", IMAGE_UPLOAD_PATH, access_token=access_token, form={}, file_field=("data", path))
        code, msg = _code(data)
        d = data.get("data") or {}
        if status != 200 or code or not d.get("url"):
            return {"ok": False, "code": code, "message": msg}
        return {"ok": True, "url": str(d.get("url")), "width": int(d.get("width") or 0), "height": int(d.get("height") or 0)}

    async def get_order(self, *, access_token: str, shop_cipher: str, order_id: str) -> Dict[str, Any]:
        status, data = await self._call("GET", ORDERS_PATH, access_token=access_token, shop_cipher=shop_cipher,
                                        query={"ids": str(order_id)})
        code, msg = _code(data)
        if status != 200 or code:
            return {"ok": False, "code": code, "message": msg}
        orders = list((data.get("data") or {}).get("orders") or [])
        if not orders:
            return {"ok": False, "code": 0, "message": "order_not_found"}
        return {"ok": True, "order": orders[0]}

    async def refresh_token(self, *, refresh_token: str) -> Dict[str, Any]:
        status, data = await self._t("GET", AUTH_BASE + TOKEN_REFRESH_PATH,
                                     params={"app_key": self.app_key, "app_secret": self.app_secret,
                                             "refresh_token": refresh_token, "grant_type": "refresh_token"})
        code, msg = _code(data)
        d = data.get("data") or {}
        if status != 200 or code or not d.get("access_token"):
            return {"ok": False, "code": code, "message": msg}
        return {"ok": True, "access_token": str(d.get("access_token")),
                "access_expires_at": float(d.get("access_token_expire_in") or 0),
                "refresh_token": str(d.get("refresh_token") or ""),
                "refresh_expires_at": float(d.get("refresh_token_expire_in") or 0),
                "seller_name": str(d.get("seller_name") or ""), "seller_base_region": str(d.get("seller_base_region") or "")}


def order_summary(order: Dict[str, Any]) -> Dict[str, Any]:
    """订单接口原始对象 → 侧栏摘要（只读、字段不齐则留空；绝不抛）。"""
    o = order if isinstance(order, dict) else {}
    pay = o.get("payment") if isinstance(o.get("payment"), dict) else {}
    items = []
    for li in (o.get("line_items") or [])[:10]:
        if not isinstance(li, dict):
            continue
        items.append({"name": str(li.get("product_name") or ""), "sku": str(li.get("sku_name") or ""),
                      "qty": int(li.get("quantity") or 1), "price": str(li.get("sale_price") or li.get("original_price") or "")})
    return {"order_id": str(o.get("id") or ""), "status": str(o.get("status") or ""),
            "total": str(pay.get("total_amount") or ""), "currency": str(pay.get("currency") or ""),
            "create_time": float(o.get("create_time") or 0), "items": items,
            "tracking_number": str(o.get("tracking_number") or ""), "shipping_provider": str(o.get("shipping_provider") or "")}


# ── 令牌 ─────────────────────────────────────────────────────────────────────

def token_state(meta: Dict[str, Any], now: float) -> str:
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


# ── Worker ──────────────────────────────────────────────────────────────────

class TikTokShopCSWorker:
    """编排器契约：``start/stop/healthy/status/send/send_media/mark_read``。account_id ＝ shop_id；
    meta：``access_token / refresh_token / access_expires_at / refresh_expires_at / shop_cipher / shop_region / source=shop``。"""

    def __init__(self, account: Dict[str, Any], config: Optional[Dict[str, Any]] = None, *,
                 api: Optional[TikTokShopApi] = None, state: Optional[TikTokShopStateStore] = None,
                 registry: Any = None, now: Optional[Callable[[], float]] = None) -> None:
        self.account_id = str((account or {}).get("account_id") or "")
        self.meta: Dict[str, Any] = dict((account or {}).get("meta") or {})
        self.config = config or {}
        self.cfg = shop_cfg(self.config)
        self._now = now or time.time
        self.api = api or TikTokShopApi(app_key=self.cfg["app_key"], app_secret=self.cfg["app_secret"], now=self._now)
        self._state = state
        self._registry = registry
        self.shop_cipher = str(self.meta.get("shop_cipher") or "")
        try:
            from src.integrations.tiktok_regions import shop_site
            self.shop_site = shop_site(self.meta.get("shop_region")) or str(self.meta.get("shop_region") or "").upper()
        except Exception:
            self.shop_site = str(self.meta.get("shop_region") or "").upper()
        self.running = False
        self.token_state = token_state(self.meta, self._now())
        self.last_error = ""
        self._reported = None

    @property
    def state(self) -> TikTokShopStateStore:
        if self._state is None:
            self._state = get_state_store(self.cfg["state_db_path"] or None)
        return self._state

    async def start(self) -> None:
        self.running = True
        await self.maybe_refresh_tokens()
        self._report_session_health()
        logger.info("[tiktok-shop] worker 启动 shop=%s site=%s token=%s", self.account_id, self.shop_site or "?", self.token_state)

    async def stop(self) -> None:
        self.running = False

    async def healthy(self) -> bool:
        self._report_session_health()
        return self.running and self.token_state != "needs_reauth"

    def status(self) -> Dict[str, Any]:
        stats = self.state.stats(self.account_id) if self._state is not None else {}
        return {"type": "tiktok_shop_cs", "source": SOURCE, "running": self.running, "token_state": self.token_state,
                "shop_site": self.shop_site, "shop_cipher_set": bool(self.shop_cipher),
                "events_total": int(stats.get("events_total") or 0), "last_event_ts": float(stats.get("last_event_ts") or 0),
                "last_error": self.last_error}

    async def maybe_refresh_tokens(self, *, force: bool = False) -> str:
        now = self._now()
        self.token_state = token_state(self.meta, now)
        if self.token_state != "refresh_due" and not (force and self.meta.get("refresh_token")):
            return self.token_state
        r = await self.api.refresh_token(refresh_token=str(self.meta.get("refresh_token") or ""))
        if r.get("ok"):
            patch = {"access_token": r["access_token"], "access_expires_at": r["access_expires_at"]}
            if r.get("refresh_token"):
                patch["refresh_token"] = r["refresh_token"]
                patch["refresh_expires_at"] = r.get("refresh_expires_at") or 0.0
            self._persist_meta(patch)
            self.token_state = token_state(self.meta, now)
            self.last_error = ""
        else:
            self.last_error = f"refresh:{r.get('code')}:{r.get('message') or ''}"
            if int(r.get("code") or 0) in TOKEN_CODES or not self.meta.get("access_token"):
                self._persist_meta({"access_token": "", "refresh_token": ""})
                self.token_state = "needs_reauth"
        return self.token_state

    def _persist_meta(self, patch: Dict[str, Any]) -> None:
        self.meta.update(patch)
        try:
            reg = self._registry
            if reg is None:
                from src.integrations.account_registry import get_account_registry
                reg = get_account_registry()
            reg.upsert(PLATFORM, self.account_id, meta=dict(patch), merge_meta=True)
        except Exception:
            logger.debug("[tiktok-shop] meta 落注册表失败", exc_info=True)

    def _report_session_health(self) -> None:
        key = self.token_state
        if key == self._reported:
            return
        self._reported = key
        try:
            from src.integrations.platform_session_health import get_platform_session_health
            h = get_platform_session_health()
            if key == "needs_reauth":
                h.record(PLATFORM, self.account_id, "expired", detail="[rc:other] TikTok Shop 店铺授权失效，请重新授权店铺")
            else:
                h.record(PLATFORM, self.account_id, "authorized")
        except Exception:
            logger.debug("[tiktok-shop] 会话健康上报失败", exc_info=True)

    async def _deliver(self, chat_key: str, message_type: str, content: str, *, kind: str) -> Dict[str, Any]:
        if await self.maybe_refresh_tokens() == "needs_reauth" or not self.meta.get("access_token"):
            self.token_state = "needs_reauth"
            self._report_session_health()
            return {"delivered": False, "blocked": "tiktok_needs_reauth", "error": "TikTok Shop 授权失效，请重新授权店铺"}
        conv = conversation_id_from_chat_key(chat_key)
        if not conv:
            return {"delivered": False, "blocked": "policy_window_no_inbound"}
        res = await self.api.send_message(access_token=str(self.meta.get("access_token")), shop_cipher=self.shop_cipher,
                                          conversation_id=conv, message_type=message_type, content=content)
        if not res.get("ok") and int(res.get("code") or 0) in TOKEN_CODES and self.meta.get("refresh_token"):
            if await self.maybe_refresh_tokens(force=True) != "needs_reauth":
                res = await self.api.send_message(access_token=str(self.meta.get("access_token")), shop_cipher=self.shop_cipher,
                                                  conversation_id=conv, message_type=message_type, content=content)
        if res.get("ok"):
            self.last_error = ""
            mid = str(res.get("message_id") or "")
            if mid:
                self.state.seen(f"msg:{mid}", now=self._now())
            return {"delivered": True, "message_id": mid, "kind": kind}
        code = int(res.get("code") or 0)
        self.last_error = f"{code}:{res.get('message') or ''}"
        out: Dict[str, Any] = {"delivered": False, "error": str(res.get("message") or f"tiktok_shop_error_{code}"),
                               "error_kind": f"tiktok_shop_error_{code}", "error_code": code}
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
        return await self._deliver(chat_key, "TEXT", text_content(t), kind="text")

    async def send_media(self, chat_key: str, *, media_path: str, media_type: str, caption: str = "") -> Dict[str, Any]:
        mt = str(media_type or "").lower().split("/", 1)[0]
        if mt not in ("image", "photo"):
            return {"delivered": False, "blocked": f"policy_media_type_denied:{mt or 'unknown'}"}
        try:
            if os.path.getsize(media_path) > IMAGE_MAX_BYTES:
                return {"delivered": False, "blocked": "policy_media_too_large:3MB"}
        except OSError:
            pass
        up = await self.api.upload_image(access_token=str(self.meta.get("access_token")), path=media_path)
        if not up.get("ok"):
            return {"delivered": False, "error": f"图片上传失败 {up.get('code')}", "error_kind": "tiktok_shop_image_upload"}
        res = await self._deliver(chat_key, "IMAGE", image_content(up["url"], up.get("width", 0), up.get("height", 0)), kind="image")
        if res.get("delivered") and str(caption or "").strip():
            cap = await self._deliver(chat_key, "TEXT", text_content(str(caption)), kind="text")
            res["caption_delivered"] = bool(cap.get("delivered"))
        return res

    async def mark_read(self, chat_key: str, *_a: Any, **_kw: Any) -> bool:
        conv = conversation_id_from_chat_key(chat_key)
        if not conv or not self.meta.get("access_token"):
            return False
        r = await self.api.mark_read(access_token=str(self.meta.get("access_token")), shop_cipher=self.shop_cipher,
                                     conversation_id=conv)
        return bool(r.get("ok"))

    async def order_card(self, chat_key: str, order_id: str, *, max_age_sec: float = ORDER_CACHE_TTL_SEC) -> Dict[str, Any]:
        """只读订单摘要（侧栏用）：状态库有新鲜缓存直接给；否则拉一次 ``GET /order/202309/orders``。"""
        conv = conversation_id_from_chat_key(chat_key)
        now = self._now()
        for c in self.state.order_cards(self.account_id, conv):
            if c["order_id"] == str(order_id) and c["summary"] and now - c["fetched_at"] <= max_age_sec:
                return {"ok": True, "cached": True, **c["summary"]}
        if not self.meta.get("access_token"):
            return {"ok": False, "error": "tiktok_needs_reauth"}
        r = await self.api.get_order(access_token=str(self.meta.get("access_token")), shop_cipher=self.shop_cipher,
                                     order_id=str(order_id))
        if not r.get("ok"):
            return {"ok": False, "error": str(r.get("message") or f"order_error_{r.get('code')}")}
        summ = order_summary(r["order"])
        self.state.put_order_summary(self.account_id, conv, str(order_id), summ, now=now)
        return {"ok": True, "cached": False, **summ}

    async def backfill(self, chat_key: str, *, max_pages: int = 5, emit: Optional[Callable[[Dict[str, Any]], Any]] = None
                       ) -> Dict[str, Any]:
        """webhook 漏投时补拉对账：分页 ≤10 条、按 message_id 幂等落库、**不置已读**。返回 ``{pulled, new}``。"""
        conv = conversation_id_from_chat_key(chat_key)
        if not conv or not self.meta.get("access_token"):
            return {"ok": False, "pulled": 0, "new": 0}
        token, pulled, new = "", 0, 0
        for _ in range(max(1, int(max_pages))):
            r = await self.api.list_messages(access_token=str(self.meta.get("access_token")), shop_cipher=self.shop_cipher,
                                             conversation_id=conv, page_token=token)
            if not r.get("ok"):
                return {"ok": False, "pulled": pulled, "new": new, "error": r.get("message")}
            for raw in r["messages"]:
                pulled += 1
                if _ingest_message(raw, shop_id=self.account_id, conversation_id=conv, state=self.state, now=self._now(),
                                   emit=emit, backfill=True):
                    new += 1
            token = r.get("next_page_token") or ""
            if not token:
                break
        return {"ok": True, "pulled": pulled, "new": new}


# ── Webhook ─────────────────────────────────────────────────────────────────

def _extract(data: Dict[str, Any]) -> Dict[str, Any]:
    """载荷 ``data`` / 补拉 ``messages[]`` 同形：conversation_id、message_id、index、create_time、type、content、sender。"""
    m = data if isinstance(data, dict) else {}
    sender = m.get("sender") if isinstance(m.get("sender"), dict) else {}
    mtype = str(m.get("type") or "TEXT").upper()
    content = parse_content(m.get("content"))
    text = ""
    if mtype == "TEXT":
        text = str(content.get("content") or content.get("text") or "")
    if not text and mtype != "TEXT":
        text = _TYPE_PLACEHOLDER.get(mtype, "[消息]")
    ts = m.get("create_time") or 0
    try:
        ts = float(ts)
        if ts > 1e12:
            ts /= 1000.0
    except (TypeError, ValueError):
        ts = 0.0
    media = {"IMAGE": "image", "VIDEO": "video", "STICKER": "sticker"}.get(mtype, "")
    return {"conversation_id": str(m.get("conversation_id") or ""), "message_id": str(m.get("message_id") or m.get("id") or ""),
            "index": int(m.get("index") or 0), "ts": ts, "message_type": mtype, "text": text, "media_type": media,
            "content": content, "sender_role": str(sender.get("role") or "").upper(),
            "sender_id": str(sender.get("im_user_id") or sender.get("id") or ""),
            "sender_name": str(sender.get("nickname") or sender.get("name") or ""),
            "avatar": str(sender.get("avatar") or ""), "order_id": str(content.get("order_id") or ""),
            "product_id": str(content.get("product_id") or ""), "image_url": str(content.get("url") or "")}


def _ingest_message(raw: Dict[str, Any], *, shop_id: str, conversation_id: str, state: TikTokShopStateStore, now: float,
                    emit: Optional[Callable[[Dict[str, Any]], Any]], backfill: bool = False) -> bool:
    """一条消息 → 收件箱（幂等）。返回是否新落库。买家 → in；卖家/坐席/系统 → out 回显。"""
    m = _extract(raw)
    conv = m["conversation_id"] or conversation_id
    if not m["message_id"] or not conv:
        return False
    if state.seen(f"msg:{m['message_id']}", now=now):
        return False
    if emit is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit
        emit = _emit
    from src.integrations.protocol_bridge import make_message
    incoming = m["sender_role"] in ("BUYER", "USER", "CUSTOMER", "")
    source: Dict[str, Any] = {"source": SOURCE, "conversation_id": conv, "server_message_id": m["message_id"],
                              "message_type": m["message_type"], "index": m["index"], "shop_id": shop_id}
    if m["order_id"]:
        source["order_card"] = {"order_id": m["order_id"]}
        state.note_order_card(shop_id, conv, m["order_id"], now=now)
    if m["product_id"]:
        source["product_card"] = {"product_id": m["product_id"]}
    if m["image_url"]:
        source["image_url"] = m["image_url"]
    if backfill:
        source["backfill"] = 1
        source["backfill_source"] = "tiktok_shop_cs"
    if not incoming:
        source["echo"] = True
    if incoming:
        state.record_inbound(shop_id, conv, buyer_id=m["sender_id"], buyer_name=m["sender_name"], index=m["index"], ts=m["ts"] or now)
    emit(make_message(platform=PLATFORM, account_id=shop_id, chat_key=chat_key_for(conv), text=m["text"],
                      name=(m["sender_name"] if incoming else ""), ts=m["ts"] or now, msg_id=m["message_id"],
                      direction=("in" if incoming else "out"), media_type=m["media_type"],
                      avatar_url=(m["avatar"] if incoming else ""), source=source))
    return True


async def handle_shop_webhook(body: bytes, signature: Any, *, config: Optional[Dict[str, Any]],
                              state: Optional[TikTokShopStateStore] = None, now: Optional[float] = None,
                              emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
                              auto_reply: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None
                              ) -> Tuple[int, Dict[str, Any]]:
    """``NEW_MESSAGE``（14）→ 幂等（tts_notification_id + message_id）→ 买家消息进收件箱 + ``maybe_auto_reply``；
    卖家侧消息 out 回显；``NEW_CONVERSATION``（15）只记日志。验签失败 401、坏 JSON 400。同步完成即回 200（3 秒契约）。"""
    cfg = shop_cfg(config)
    t_now = float(now if now is not None else time.time())
    if cfg["verify_signature"] and not verify_webhook(cfg["app_secret"], cfg["app_key"], body, signature):
        return 401, {"error": "bad_signature"}
    try:
        payload = json.loads(bytes(body or b"").decode("utf-8") or "{}")
    except Exception:
        return 400, {"error": "bad_json"}
    if not isinstance(payload, dict):
        return 400, {"error": "bad_json"}
    st = state or get_state_store(cfg["state_db_path"] or None)
    shop_id = str(payload.get("shop_id") or "")
    try:
        etype = int(payload.get("type") or 0)
    except (TypeError, ValueError):
        etype = 0
    nid = str(payload.get("tts_notification_id") or "")
    if nid and st.seen(f"notif:{nid}", now=t_now):
        return 200, {"ok": True, "dup": True}
    if shop_id:
        st.record_event(shop_id, t_now)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if etype == EVENT_NEW_CONVERSATION:
        logger.info("[tiktok-shop] 新会话 shop=%s conversation=%s（只记录）", shop_id, data.get("conversation_id"))
        return 200, {"ok": True, "noted": True}
    if etype != EVENT_NEW_MESSAGE:
        return 200, {"ok": True, "ignored": etype}
    m = _extract(data)
    if not m["message_id"] or not m["conversation_id"] or not shop_id:
        return 400, {"error": "missing_message_id"}
    if emit is None or auto_reply is None:
        from src.integrations.protocol_bridge import emit_incoming as _emit, maybe_auto_reply as _ar
        emit = emit or _emit
        auto_reply = auto_reply or _ar
    captured: List[Dict[str, Any]] = []

    def _emit_capture(msg: Dict[str, Any]) -> None:
        captured.append(msg)
        emit(msg)  # type: ignore[misc]

    if not _ingest_message(data, shop_id=shop_id, conversation_id=m["conversation_id"], state=st, now=t_now, emit=_emit_capture):
        return 200, {"ok": True, "dup": True}
    msg = captured[0] if captured else None
    if msg is not None and msg.get("direction") == "in":
        try:
            await auto_reply(msg)
        except Exception:
            logger.debug("[tiktok-shop] maybe_auto_reply 异常", exc_info=True)
        return 200, {"ok": True}
    return 200, {"ok": True, "echo": True}


def register_tiktok_shop_routes(app: Any, config_manager: Any) -> bool:
    """``tiktok.shop.enabled`` 才挂：``POST {webhook_path}``（无鉴权，验签）+ ``GET /api/tiktok/shop/order-card``（api_auth，只读侧栏）。"""
    cfg0 = shop_cfg(getattr(config_manager, "config", None) or {})
    if not cfg0["enabled"] or JSONResponse is None:
        return False
    path = cfg0["webhook_path"]
    if any(getattr(r, "path", "") == path for r in getattr(app, "routes", [])):
        return True

    @app.post(path)
    async def tiktok_shop_webhook(request: Request):  # noqa: D401
        body = await request.body()
        cfg = getattr(config_manager, "config", None) or {}
        header = shop_cfg(cfg)["webhook_signature_header"]
        status, resp = await handle_shop_webhook(body, request.headers.get(header), config=cfg)
        # 官方要求空 200；失败码照实回（401 表示拒签）
        return JSONResponse(resp if status != 200 else {}, status_code=status)

    api_auth = getattr(getattr(app, "state", None), "api_auth", None)
    deps = [Depends(api_auth)] if (api_auth is not None and Depends is not None) else []

    @app.get(ORDER_CARD_ROUTE, dependencies=deps)
    async def tiktok_shop_order_card(request: Request, account_id: str = "", chat_key: str = "", order_id: str = ""):
        """线程侧栏只读订单卡：无 order_id 时列出该会话出现过的订单卡（含已缓存摘要）。"""
        from src.integrations.account_orchestrator import account_key, get_orchestrator
        cfg = getattr(config_manager, "config", None) or {}
        st = get_state_store(shop_cfg(cfg)["state_db_path"] or None)
        conv = conversation_id_from_chat_key(chat_key)
        if not order_id:
            return {"ok": True, "cards": st.order_cards(account_id, conv)}
        m = get_orchestrator()._managed.get(account_key(PLATFORM, account_id))
        w = m.worker if (m is not None and m.state == "running") else None
        if w is None or not hasattr(w, "order_card"):
            for c in st.order_cards(account_id, conv):
                if c["order_id"] == order_id and c["summary"]:
                    return {"ok": True, "cached": True, **c["summary"]}
            return JSONResponse({"ok": False, "error": "no_worker"}, status_code=503)
        return await w.order_card(chat_key, order_id)

    logger.info("[tiktok-shop] webhook 已挂载 %s；订单卡只读接口 %s", path, ORDER_CARD_ROUTE)
    return True


def register_tiktok_shop_cs_worker(config: Optional[Dict[str, Any]], *, registry: Any = None) -> bool:
    """``tiktok.shop.enabled`` 为真时把 ``(tiktok, official)`` 工厂包成分流器：``meta.source == "shop"`` → 本 worker，
    其余 → 既有私信 worker（``tiktok_official``）。幂等：已是分流器不再包。"""
    if not shop_enabled(config):
        return False
    from src.integrations.account_orchestrator import get_worker_factory, register_worker
    prev = get_worker_factory(PLATFORM, MODE)
    if prev is not None and getattr(prev, "_tiktok_shop_dispatch", False):
        return True

    def _factory(acc: Dict[str, Any], cfg: Dict[str, Any]) -> Any:
        if is_shop_account(acc):
            return TikTokShopCSWorker(acc, cfg, registry=registry)
        if prev is not None:
            return prev(acc, cfg)
        from src.integrations.tiktok_official import TikTokOfficialWorker
        return TikTokOfficialWorker(acc, cfg, registry=registry)

    _factory._tiktok_shop_dispatch = True  # type: ignore[attr-defined]
    _factory._tiktok_shop_prev = prev  # type: ignore[attr-defined]
    register_worker(PLATFORM, MODE, _factory)
    logger.info("[tiktok-shop] 已注册 (tiktok, official) 分流工厂（meta.source=shop → Shop CS worker）")
    return True


__all__ = ["PLATFORM", "MODE", "SOURCE", "CHAT_PREFIX", "API_BASE", "AUTH_BASE", "CONVERSATIONS_PATH", "IMAGE_UPLOAD_PATH",
           "ORDERS_PATH", "TOKEN_REFRESH_PATH", "EVENT_NEW_MESSAGE", "EVENT_NEW_CONVERSATION", "DEFAULT_WEBHOOK_PATH",
           "DEFAULT_SIGNATURE_HEADER", "ORDER_CARD_ROUTE", "PAGE_SIZE_MAX", "shop_cfg", "shop_enabled", "is_shop_account",
           "chat_key_for", "conversation_id_from_chat_key", "sign_webhook", "verify_webhook", "sign_request",
           "TikTokShopStateStore", "get_state_store", "TikTokShopApi", "text_content", "image_content", "parse_content",
           "order_summary", "token_state", "TikTokShopCSWorker", "handle_shop_webhook", "register_tiktok_shop_routes",
           "register_tiktok_shop_cs_worker"]
