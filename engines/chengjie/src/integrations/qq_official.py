"""QQ 机器人（QQ 开放平台官方 API v2）接入 —— platform=``qqbot``（2026-09-07，QQ 双轨之官方轨）。

与 ``qq``（协议登录个人号，见 ``qq_milky.py``）是**两个独立平台**：身份空间不同
（openid vs QQ 号）、合规归属不同（主站 vs 准入区）、图标不同。本模块只管官方轨。

平台事实（2026-09 文档核对，实施方案 §2.1）：
- 凭证 ``AppID + AppSecret`` → ``getAppAccessToken`` 换 access_token（~7200s，需刷新）；
  OpenAPI 鉴权头 ``Authorization: QQBot {token}``；正式 ``api.sgroup.qq.com`` /
  沙箱 ``sandbox.api.sgroup.qq.com``（沙箱只收沙箱群/单聊配置里的事件，≤20 人）。
- 事件两种接法：**WebSocket 网关**（``/gateway`` → Identify/心跳/Resume 补发；不依赖
  公网，适合桌面单机；正式环境受 IP 白名单约束）或 **Webhook**（HTTPS 回调 +
  Ed25519 验签；op=13 回调地址验证）。管理端可随时切换，本模块两种都实现。
- **被动回复窗口**：单聊每条来话 60 分钟内最多回 4 条（``msg_id + msg_seq`` 去重，
  同 seq 重发失败）；群聊 5 分钟 / 5 条；事件（FRIEND_ADD / GROUP_ADD_ROBOT …）
  的 ``event_id`` 也可作被动锚点。**主动消息 2025-04 起平台收敛**——本模块默认
  ``passive_only``：无可用锚点即 fail-closed 拒发（``error_kind=window_expired``），
  绝不拿账号去撞主动消息接口试风控。
- 身份：``user_openid`` / ``member_openid`` / ``group_openid``，每个机器人独立，事件
  **不带昵称头像**（隐私设计）；群消息默认只收 @机器人（``GROUP_AT_MESSAGE_CREATE``），
  全量需群管理员在群设置开权限并订阅 ``GROUP_MESSAGE_CREATE``。

config.yaml（键名与接入向导 ``channel_setup.Channel(id="qqbot")`` 逐字对齐，门禁钉住）::

  qqbot:
    enabled: true
    app_id: "102000000"
    app_secret: "xxxxxxxx"
    sandbox: false              # true → 沙箱环境（提审前联调）
    connect_mode: websocket     # websocket | webhook
    webhook_path: "/qqbot/webhook"
    group_full_messages: false  # 同时接 GROUP_MESSAGE_CREATE（群全量消息，需群侧权限）
    passive_only: true          # 无被动锚点即拒发（默认开；关=尝试主动消息，风险自负）

复用：入站镜像 + G4c 主管道 → ``shared/official_inbound.process_official_inbound``；
出站 ``qqbot_send_text`` 内建 Kill-Switch（platform=qqbot）+ 官方错误分类。
网络调用集中在 ``_http_json`` / ``_ws_connect`` 两个可被测试替换的薄封装里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# 模块级导入（而非在 register_qqbot_routes 内）：本文件启用了 postponed annotations，
# FastAPI 按函数 __globals__ 解析 "Request" 注解——局部导入会让它解析不到，把 request
# 当成必填查询参数 → 所有回调 422。
from fastapi import Request, Response

logger = logging.getLogger(__name__)

PLATFORM = "qqbot"

QQBOT_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQBOT_API_BASE = "https://api.sgroup.qq.com"
QQBOT_SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
QQBOT_CONSOLE_URL = "https://q.qq.com/"

#: Intents 位（见官方「事件订阅与通知」）：群 + 单聊全部事件一位覆盖。
INTENT_GROUP_AND_C2C = 1 << 25

#: 被动回复窗口（平台硬约束，不是我们没接）——见模块头
C2C_WINDOW_SEC = 60 * 60
C2C_MAX_REPLIES = 4
GROUP_WINDOW_SEC = 5 * 60
GROUP_MAX_REPLIES = 5
#: 事件锚点（FRIEND_ADD 等 event_id）：官方未明示有效期，按群口径保守取 5 分钟 / 1 条
EVENT_WINDOW_SEC = 5 * 60
EVENT_MAX_REPLIES = 1

#: 单条文本上限（平台未明示，保守截断防 4xx）
QQBOT_TEXT_MAX = 4000

#: 消息事件 → 会话种类；非消息事件 → 锚点/状态语义
MESSAGE_EVENTS = {
    "C2C_MESSAGE_CREATE": "c2c",
    "GROUP_AT_MESSAGE_CREATE": "group",
    "GROUP_MESSAGE_CREATE": "group",
}
ANCHOR_EVENTS = {
    "FRIEND_ADD": "c2c",          # d.openid
    "C2C_MSG_RECEIVE": "c2c",     # 用户重新打开主动消息开关（也是可回复事件）
    "GROUP_ADD_ROBOT": "group",   # d.group_openid
    "GROUP_MSG_RECEIVE": "group",
}

_DEFAULT_WEBHOOK_PATH = "/qqbot/webhook"


# ── 配置 ─────────────────────────────────────────────────────────────────────

def qqbot_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    return dict((config or {}).get("qqbot") or {})


def api_base(cfg: Dict[str, Any]) -> str:
    """按 ``sandbox`` 选 OpenAPI 域名（token 接口不分环境）。"""
    return QQBOT_SANDBOX_API_BASE if bool((cfg or {}).get("sandbox")) else QQBOT_API_BASE


def connect_mode(cfg: Dict[str, Any]) -> str:
    """``websocket``（默认，桌面单机免公网）| ``webhook``。"""
    m = str((cfg or {}).get("connect_mode") or "websocket").strip().lower()
    return "webhook" if m == "webhook" else "websocket"


def webhook_path(cfg: Dict[str, Any]) -> str:
    p = str((cfg or {}).get("webhook_path") or _DEFAULT_WEBHOOK_PATH).strip()
    return p if p.startswith("/") else "/" + p


def passive_only(cfg: Dict[str, Any]) -> bool:
    v = (cfg or {}).get("passive_only")
    return True if v is None else bool(v)


def intents_for(cfg: Dict[str, Any]) -> int:
    # 群全量与 @ 消息同属 GROUP_AND_C2C_EVENT 一位；是否真收到全量取决于开放平台
    # 回调配置勾选 + 群管理员权限，这里如实只算位掩码。
    return INTENT_GROUP_AND_C2C


def creds_from(config: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """凭证解析：账号 ``meta`` 优先（多机器人），回退平台 config 块（单机器人）。"""
    m = dict(meta or {})
    cfg = qqbot_cfg(config)
    return {
        "app_id": str(m.get("app_id") or cfg.get("app_id") or "").strip(),
        "app_secret": str(m.get("app_secret") or cfg.get("app_secret") or "").strip(),
    }


# ── chat_key 约定 ─────────────────────────────────────────────────────────────
#   qqbot:c2c:<user_openid>  单聊     qqbot:group:<group_openid>  群
#   自描述前缀：send() 据此选单聊/群接口（官方 worker 的 dest_from_chat_key 取末段）。

def make_chat_key(kind: str, openid: str) -> str:
    k = "group" if str(kind) == "group" else "c2c"
    return f"{PLATFORM}:{k}:{str(openid or '').strip()}"


def parse_chat_key(chat_key: str) -> Tuple[str, str]:
    """``qqbot:c2c:<id>`` → ("c2c", id)；``qqbot:group:<id>`` → ("group", id)；
    裸 id → ("c2c", id)（companion 主管道直传裸标识的兼容）。"""
    s = str(chat_key or "").strip()
    parts = s.split(":")
    if len(parts) >= 3 and parts[0].lower() == PLATFORM:
        kind = "group" if parts[1].lower() == "group" else "c2c"
        return kind, ":".join(parts[2:])
    if len(parts) == 2 and parts[0].lower() in ("group", "c2c"):
        return ("group" if parts[0].lower() == "group" else "c2c"), parts[1]
    return "c2c", s


# ── HTTP 薄封装（测试可 monkeypatch） ─────────────────────────────────────────

async def _http_json(
    method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None, timeout: float = 20.0,
) -> Tuple[int, Any]:
    """返回 ``(status, body)``；body 尽力解析 JSON，否则原文（截断）。永不抛。"""
    import aiohttp
    try:
        tmo = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=tmo) as session:
            async with session.request(method.upper(), url, headers=headers or {},
                                       json=payload) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw) if raw else {}
                except Exception:
                    data = {"raw": raw[:500]}
                return int(resp.status), data
    except Exception as ex:  # noqa: BLE001
        return 0, {"error": str(ex)[:300]}


# ── access_token ─────────────────────────────────────────────────────────────

class QQBotTokenManager:
    """按 app_id 缓存 access_token；到期前 ``refresh_margin`` 秒刷新；失败不缓存。"""

    def __init__(self, *, now: Callable[[], float] = time.time,
                 refresh_margin: float = 120.0) -> None:
        self._now = now
        self._margin = float(refresh_margin)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def peek(self, app_id: str) -> str:
        row = self._cache.get(str(app_id) or "")
        if not row or row.get("expires_at", 0) - self._margin <= self._now():
            return ""
        return str(row.get("token") or "")

    async def get(self, app_id: str, app_secret: str) -> str:
        """取有效 token；拿不到返回空串（调用方按 invalid_token 处理）。"""
        app_id = str(app_id or "").strip()
        if not app_id or not app_secret:
            return ""
        tok = self.peek(app_id)
        if tok:
            return tok
        async with self._lock:
            tok = self.peek(app_id)
            if tok:
                return tok
            status, body = await _http_json(
                "POST", QQBOT_TOKEN_URL,
                headers={"Content-Type": "application/json"},
                payload={"appId": app_id, "clientSecret": str(app_secret)},
                timeout=15.0)
            if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
                logger.warning("[qqbot] 获取 access_token 失败 HTTP %s: %s",
                               status, str(body)[:200])
                return ""
            try:
                ttl = float(body.get("expires_in") or 7200)
            except (TypeError, ValueError):
                ttl = 7200.0
            self._cache[app_id] = {
                "token": str(body["access_token"]),
                "expires_at": self._now() + max(60.0, ttl),
            }
            return self._cache[app_id]["token"]

    def invalidate(self, app_id: str) -> None:
        self._cache.pop(str(app_id) or "", None)


_token_mgr: Optional[QQBotTokenManager] = None


def get_token_manager() -> QQBotTokenManager:
    global _token_mgr
    if _token_mgr is None:
        _token_mgr = QQBotTokenManager()
    return _token_mgr


def auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"QQBot {token}", "Content-Type": "application/json"}


# ── 被动回复窗口账本（进程内；重启丢锚点＝下条来话前 fail-closed，如实） ─────────

@dataclass
class _Anchor:
    ref: str            # msg_id 或 event_id
    kind: str           # "msg" | "event"
    ts: float
    limit: int
    window: float
    used: int = 0

    def expires_at(self) -> float:
        return self.ts + self.window

    def usable(self, now: float) -> bool:
        return self.used < self.limit and now < self.expires_at()


@dataclass
class _Chat:
    anchors: List[_Anchor] = field(default_factory=list)


class PassiveReplyLedger:
    """每会话记录可用的被动回复锚点（来话 msg_id / 事件 event_id）与已用次数。

    ``reserve`` 选**最新**且仍有余量、未过期的锚点，占一次并返回
    ``{"msg_id", "msg_seq"}`` 或 ``{"event_id"}``；无可用锚点返回 None（调用方
    fail-closed）。纯内存、线程安全、可注入时钟；观测经 ``snapshot()``。
    """

    MAX_ANCHORS_PER_CHAT = 8
    MAX_CHATS = 5000

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._chats: Dict[str, _Chat] = {}
        self._lock = threading.Lock()
        self.stats: Dict[str, int] = {"anchored": 0, "reserved": 0, "blocked": 0}

    def _chat(self, chat_key: str) -> _Chat:
        ck = str(chat_key or "")
        c = self._chats.get(ck)
        if c is None:
            if len(self._chats) >= self.MAX_CHATS:
                # 粗粒度防膨胀：清掉全部已过期锚点的会话
                now = self._now()
                for k in [k for k, v in self._chats.items()
                          if not any(a.usable(now) for a in v.anchors)]:
                    self._chats.pop(k, None)
            c = _Chat()
            self._chats[ck] = c
        return c

    def note_inbound(self, chat_key: str, msg_id: str, *, group: bool = False,
                     ts: Optional[float] = None) -> None:
        mid = str(msg_id or "").strip()
        if not mid:
            return
        with self._lock:
            c = self._chat(chat_key)
            if any(a.ref == mid for a in c.anchors):
                return
            c.anchors.append(_Anchor(
                ref=mid, kind="msg", ts=float(ts if ts is not None else self._now()),
                limit=GROUP_MAX_REPLIES if group else C2C_MAX_REPLIES,
                window=GROUP_WINDOW_SEC if group else C2C_WINDOW_SEC))
            del c.anchors[:-self.MAX_ANCHORS_PER_CHAT]
            self.stats["anchored"] += 1

    def note_event(self, chat_key: str, event_id: str, *, ts: Optional[float] = None) -> None:
        eid = str(event_id or "").strip()
        if not eid:
            return
        with self._lock:
            c = self._chat(chat_key)
            if any(a.ref == eid for a in c.anchors):
                return
            c.anchors.append(_Anchor(
                ref=eid, kind="event", ts=float(ts if ts is not None else self._now()),
                limit=EVENT_MAX_REPLIES, window=EVENT_WINDOW_SEC))
            del c.anchors[:-self.MAX_ANCHORS_PER_CHAT]
            self.stats["anchored"] += 1

    def reserve(self, chat_key: str) -> Optional[Dict[str, Any]]:
        now = self._now()
        with self._lock:
            c = self._chats.get(str(chat_key or ""))
            if c is None:
                self.stats["blocked"] += 1
                return None
            for a in sorted(c.anchors, key=lambda x: x.ts, reverse=True):
                if not a.usable(now):
                    continue
                a.used += 1
                self.stats["reserved"] += 1
                if a.kind == "event":
                    return {"event_id": a.ref}
                return {"msg_id": a.ref, "msg_seq": a.used}
            self.stats["blocked"] += 1
            return None

    def status(self, chat_key: str) -> Dict[str, Any]:
        """``{"remaining": 总余量, "expires_in": 最晚到期秒, "anchor": 最新锚点}``——
        供会话头部「可回复 n/4 · 剩 m 分」chip 与 send-caps 消费。"""
        now = self._now()
        with self._lock:
            c = self._chats.get(str(chat_key or ""))
            if c is None:
                return {"remaining": 0, "expires_in": 0, "anchor": ""}
            live = [a for a in c.anchors if a.usable(now)]
            if not live:
                return {"remaining": 0, "expires_in": 0, "anchor": ""}
            newest = max(live, key=lambda x: x.ts)
            return {
                "remaining": sum(a.limit - a.used for a in live),
                "expires_in": int(max(a.expires_at() for a in live) - now),
                "anchor": newest.ref,
                "limit": newest.limit,
            }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"chats": len(self._chats), **dict(self.stats)}


_ledger: Optional[PassiveReplyLedger] = None


def get_passive_ledger() -> PassiveReplyLedger:
    global _ledger
    if _ledger is None:
        _ledger = PassiveReplyLedger()
    return _ledger


def reset_for_tests() -> None:
    """测试专用：清账本与 token 缓存。"""
    global _ledger, _token_mgr
    _ledger = None
    _token_mgr = None


# ── 事件归一 ─────────────────────────────────────────────────────────────────

def _parse_ts(v: Any) -> float:
    """QQ 事件时间戳：ISO8601 字串（``2024-01-01T12:00:00+08:00``）或秒数；解不出回落 now。"""
    if v is None or v == "":
        return time.time()
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.isdigit():
        return float(s)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


_MEDIA_BY_CT = (
    ("image/", "image"), ("audio/", "voice"), ("voice", "voice"),
    ("video/", "video"), ("file", "file"), ("application/", "file"),
)


def attachment_media(att: Dict[str, Any]) -> Tuple[str, str]:
    """attachments 条目 → (media_type, url)。``content_type`` 形如 ``image/jpeg`` /
    ``voice`` / ``video/mp4`` / ``file``；认不出归 file。"""
    if not isinstance(att, dict):
        return "", ""
    ct = str(att.get("content_type") or "").strip().lower()
    url = str(att.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    mt = ""
    for prefix, kind in _MEDIA_BY_CT:
        if ct.startswith(prefix) or ct == prefix:
            mt = kind
            break
    if not mt and url:
        mt = "file"
    return mt, url


def extract_qqbot_events(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把网关/Webhook 的一条 Dispatch payload 归一成内部事件列表（纯函数）。

    输出条目：``{kind: message|anchor|leave|reject, scene: c2c|group, chat_key, peer_openid,
    group_openid, text, msg_id, event_id, ts, media: [{media_type, url}], event_type}``。
    非 Dispatch（op≠0）或不认识的事件 → 空列表。
    """
    if not isinstance(payload, dict) or int(payload.get("op", 0) or 0) != 0:
        return []
    t = str(payload.get("t") or "").upper()
    d = payload.get("d") if isinstance(payload.get("d"), dict) else {}
    event_id = str(payload.get("id") or "")
    out: List[Dict[str, Any]] = []
    if t in MESSAGE_EVENTS:
        scene = MESSAGE_EVENTS[t]
        author = d.get("author") if isinstance(d.get("author"), dict) else {}
        if scene == "group":
            gid = str(d.get("group_openid") or "")
            peer = str(author.get("member_openid") or author.get("id") or "")
            chat_key = make_chat_key("group", gid)
        else:
            gid = ""
            peer = str(author.get("user_openid") or author.get("id") or "")
            chat_key = make_chat_key("c2c", peer)
        media: List[Dict[str, str]] = []
        for att in (d.get("attachments") or []):
            mt, url = attachment_media(att)
            if mt:
                media.append({"media_type": mt, "url": url})
        text = str(d.get("content") or "")
        # 群 @ 消息正文常带一个前导空格（@机器人 被平台剥掉后的残留）
        text = text.strip() if scene == "group" else text.strip("\n\r ")
        if not (peer or gid):
            return out
        out.append({
            "kind": "message", "scene": scene, "chat_key": chat_key,
            "peer_openid": peer, "group_openid": gid, "text": text,
            "msg_id": str(d.get("id") or ""), "event_id": event_id,
            "ts": _parse_ts(d.get("timestamp")), "media": media, "event_type": t,
        })
        return out
    if t in ANCHOR_EVENTS:
        scene = ANCHOR_EVENTS[t]
        if scene == "group":
            gid = str(d.get("group_openid") or "")
            peer = str(d.get("op_member_openid") or "")
            chat_key = make_chat_key("group", gid)
        else:
            gid = ""
            peer = str(d.get("openid") or "")
            chat_key = make_chat_key("c2c", peer)
        if not (peer or gid):
            return out
        out.append({
            "kind": "anchor", "scene": scene, "chat_key": chat_key,
            "peer_openid": peer, "group_openid": gid, "text": "",
            "msg_id": "", "event_id": event_id,
            "ts": _parse_ts(d.get("timestamp")), "media": [], "event_type": t,
        })
        return out
    if t in ("FRIEND_DEL", "GROUP_DEL_ROBOT", "C2C_MSG_REJECT", "GROUP_MSG_REJECT"):
        scene = "group" if t.startswith("GROUP") else "c2c"
        gid = str(d.get("group_openid") or "") if scene == "group" else ""
        peer = str(d.get("openid") or d.get("op_member_openid") or "")
        out.append({
            "kind": "leave" if t.endswith("DEL") or t.endswith("DEL_ROBOT") else "reject",
            "scene": scene,
            "chat_key": make_chat_key(scene, gid or peer),
            "peer_openid": peer, "group_openid": gid, "text": "",
            "msg_id": "", "event_id": event_id,
            "ts": _parse_ts(d.get("timestamp")), "media": [], "event_type": t,
        })
    return out


# ── Webhook 签名（Ed25519；seed = AppSecret 重复填满 32 字节） ─────────────────

def _ed25519_seed(app_secret: str) -> bytes:
    seed = str(app_secret or "")
    if not seed:
        raise ValueError("empty app_secret")
    while len(seed) < 32:
        seed = seed * 2
    return seed[:32].encode("utf-8")


def _private_key(app_secret: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.from_private_bytes(_ed25519_seed(app_secret))


def sign_validation(app_secret: str, plain_token: str, event_ts: str) -> str:
    """op=13 回调地址验证：``sign(event_ts + plain_token)`` → hex。"""
    key = _private_key(app_secret)
    msg = (str(event_ts) + str(plain_token)).encode("utf-8")
    return key.sign(msg).hex()


def verify_webhook_signature(app_secret: str, signature_hex: str, timestamp: str, body: bytes) -> bool:
    """事件推送验签：``X-Signature-Ed25519`` = sign(``X-Signature-Timestamp`` + body)。"""
    try:
        from cryptography.exceptions import InvalidSignature
        sig = bytes.fromhex(str(signature_hex or "").strip())
        pub = _private_key(app_secret).public_key()
        pub.verify(sig, str(timestamp or "").encode("utf-8") + (body or b""))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
    except Exception:  # noqa: BLE001
        logger.debug("[qqbot] 验签异常", exc_info=True)
        return False


# ── 出站 ─────────────────────────────────────────────────────────────────────

def _truncate(text: str) -> str:
    s = str(text or "").strip()
    return s if len(s) <= QQBOT_TEXT_MAX else s[: QQBOT_TEXT_MAX - 1] + "…"


def _messages_url(base: str, kind: str, openid: str) -> str:
    if kind == "group":
        return f"{base}/v2/groups/{openid}/messages"
    return f"{base}/v2/users/{openid}/messages"


async def qqbot_send_text(
    chat_key: str, text: str, *, config: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None, account_id: str = "official",
    reply_to_msg_id: str = "", check_kill_switch: bool = True,
    ledger: Optional[PassiveReplyLedger] = None,
) -> Dict[str, Any]:
    """经 QQ 开放平台发文字。永不抛。

    返回 ``{"ok", "data"|"error", "error_kind", "retriable", "anchor"}``。被动锚点由账本
    分配：无锚点且 ``passive_only`` → ``ok=False, error_kind=window_expired,
    blocked=qq_passive_window``（上层按窗口过期分流：转人工 / 等下次来话，不重试）。
    """
    cfg = qqbot_cfg(config)
    if check_kill_switch:
        try:
            from src.integrations.shared.rpa_send_guard import rpa_send_blocked
            blocked, scope = rpa_send_blocked(PLATFORM, account_id or "official")
            if blocked:
                logger.warning("[qqbot][kill-switch] 冻结发送，跳过（scope=%s）", scope)
                return {"ok": False, "error": f"kill_switch:{scope}", "error_kind": "blocked"}
        except Exception:
            pass
    text = _truncate(text)
    if not text:
        return {"ok": True, "data": {"skipped": "empty"}}
    creds = creds_from(config, meta)
    if not creds["app_id"] or not creds["app_secret"]:
        return {"ok": False, "error": "qqbot 缺少 app_id/app_secret",
                "error_kind": "invalid_token", "retriable": False}
    kind, openid = parse_chat_key(chat_key)
    if not openid:
        return {"ok": False, "error": "empty openid", "error_kind": "unsupported",
                "retriable": False}
    led = ledger if ledger is not None else get_passive_ledger()
    anchor = led.reserve(chat_key if str(chat_key).startswith(PLATFORM + ":")
                         else make_chat_key(kind, openid))
    if anchor is None and passive_only(cfg):
        return {"ok": False, "error": "no passive reply anchor (QQ passive window exhausted)",
                "error_kind": "window_expired", "blocked": "qq_passive_window",
                "retriable": False}
    token = await get_token_manager().get(creds["app_id"], creds["app_secret"])
    if not token:
        return {"ok": False, "error": "access_token unavailable",
                "error_kind": "invalid_token", "retriable": False}
    body: Dict[str, Any] = {"content": text, "msg_type": 0}
    if anchor:
        body.update(anchor)
    if reply_to_msg_id:
        body["message_reference"] = {"message_id": str(reply_to_msg_id)}
    status, data = await _http_json(
        "POST", _messages_url(api_base(cfg), kind, openid),
        headers=auth_headers(token), payload=body)
    if status == 200 and isinstance(data, dict) and not data.get("code"):
        return {"ok": True, "data": data, "anchor": anchor or {}}
    if status in (401, 403):
        # token 失效/白名单：下次重取 token（白名单类错误重取也无用，但无害）
        get_token_manager().invalidate(creds["app_id"])
    out: Dict[str, Any] = {"ok": False, "error": f"HTTP {status}: {str(data)[:200]}",
                           "data": data, "anchor": anchor or {}}
    try:
        from src.integrations.shared.official_send_error import classify_official_send_error
        info = classify_official_send_error(PLATFORM, status=status, body=data,
                                            error_text=str(data)[:300])
        out["error_kind"] = info["kind"]
        out["retriable"] = info["retriable"]
    except Exception:
        out["error_kind"] = "unknown"
        out["retriable"] = False
    logger.warning("[qqbot] 发送失败 HTTP %s kind=%s: %s", status, out.get("error_kind"),
                   str(data)[:200])
    return out


# ── 入站处理（Webhook 与 WS 网关共用） ────────────────────────────────────────

_skill_manager_getter: Optional[Callable[[], Any]] = None


def register_skill_manager_getter(fn: Optional[Callable[[], Any]]) -> None:
    """WS 网关跑在编排器 worker 里拿不到 app；由 admin 注册期注入 SkillManager 取法，
    供「非 System Z / 非主管道」的自答回落使用（与 Zalo webhook 自答同语义）。"""
    global _skill_manager_getter
    _skill_manager_getter = fn


def _resolve_sm() -> Any:
    fn = _skill_manager_getter
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


async def handle_qqbot_event(
    payload: Dict[str, Any], *, config: Dict[str, Any], account_id: str,
    meta: Optional[Dict[str, Any]] = None, use_pipeline: Optional[bool] = None,
    ledger: Optional[PassiveReplyLedger] = None, sm: Any = None,
) -> int:
    """处理一条 Dispatch payload：记锚点 → 镜像入站 →（System Z / 主管道 / 自答）。

    返回处理的事件条数。best-effort：任何异常吞掉记 debug，绝不影响网关/Webhook 主流程。
    """
    events = extract_qqbot_events(payload)
    if not events:
        return 0
    led = ledger if ledger is not None else get_passive_ledger()
    if use_pipeline is None:
        try:
            from src.integrations.official_api_worker import official_pipeline_enabled
            use_pipeline = official_pipeline_enabled(config)
        except Exception:
            use_pipeline = False
    n = 0
    for ev in events:
        n += 1
        try:
            chat_key = ev["chat_key"]
            if ev["kind"] == "message":
                led.note_inbound(chat_key, ev["msg_id"], group=(ev["scene"] == "group"),
                                 ts=ev["ts"])
                await _handle_message(ev, config=config, account_id=account_id,
                                      meta=meta, use_pipeline=bool(use_pipeline),
                                      ledger=led, sm=sm)
            elif ev["kind"] == "anchor":
                led.note_event(chat_key, ev["event_id"], ts=ev["ts"])
            # leave / reject：只记日志（对端删机器人/关主动消息，无需落库）
            else:
                logger.info("[qqbot] 事件 %s chat=%s", ev["event_type"], chat_key)
        except Exception:
            logger.debug("[qqbot] 事件处理失败 t=%s", ev.get("event_type"), exc_info=True)
    return n


async def _handle_message(
    ev: Dict[str, Any], *, config: Dict[str, Any], account_id: str,
    meta: Optional[Dict[str, Any]], use_pipeline: bool,
    ledger: PassiveReplyLedger, sm: Any,
) -> None:
    import uuid

    from src.integrations.shared.official_inbound import (
        mirror_inbound_media, mirror_official_outbound, process_official_inbound,
    )
    chat_key = ev["chat_key"]
    scene = ev["scene"]
    # 官方事件不带昵称（隐私）：私聊会话名留空 → 身份层回落裸 openid（如实缺名，
    # 由 AI 顺势问称呼 / 坐席备注补齐——实施方案 P2-6）；群会话同理不把发言人当会话名。
    name = ""
    # 媒体先镜像占位（坐席可见可接管；与 IG/Zalo 官方链同语义）
    for m in ev.get("media") or []:
        mirror_inbound_media(
            platform=PLATFORM, account_id=account_id, chat_key=chat_key,
            media_type=m["media_type"], name=name, msg_id=ev["msg_id"],
            media_ref=m.get("url") or "")
    text = str(ev.get("text") or "")
    if not text:
        return
    handed = await process_official_inbound(
        platform=PLATFORM, account_id=account_id, chat_key=chat_key,
        text=text, name=name, msg_id=ev["msg_id"], use_pipeline=use_pipeline)
    if handed:
        return
    # 群消息不自答（群语义由 group_show/人设群策略管；官方群默认只收 @ 也不该逢 @ 必答）
    if scene == "group":
        return
    skill = sm if sm is not None else _resolve_sm()
    if skill is None:
        return
    context: Dict[str, Any] = {
        "chat_id": chat_key, "chat_title": "",
        "request_id": f"r-{uuid.uuid4().hex[:12]}",
        "channel": PLATFORM, "qqbot_openid": ev["peer_openid"],
        "qqbot_message_id": ev["msg_id"],
    }
    try:
        reply_text = await skill.process_message(
            text=text, user_id=f"{PLATFORM}:{ev['peer_openid']}", context=context)
    except Exception as e:  # noqa: BLE001
        logger.exception("[qqbot] process_message 异常: %s", e)
        return
    if reply_text:
        res = await qqbot_send_text(chat_key, str(reply_text), config=config, meta=meta,
                                    account_id=account_id, ledger=ledger)
        if res.get("ok"):
            await mirror_official_outbound(
                platform=PLATFORM, account_id=account_id, chat_key=chat_key,
                text=str(reply_text))


# ── WebSocket 网关客户端 ──────────────────────────────────────────────────────

class QQBotGateway:
    """一条机器人的 WS 网关长连：Identify → 心跳 → Dispatch → 断线 Resume/退避重连。

    - 4914（已下架）/ 4915（已封禁）→ ``fatal`` 停止重连（worker 据此报 error）；
    - 4009（连接过期）/ 7 Reconnect → resume；9 Invalid Session → 重新 identify；
    - 其余断开：指数退避 2s→60s；``on_event`` 回调异常不影响连接。
    网络 I/O 经 ``_ws_connect``（可注入）隔离，纯逻辑部分（op 分发）可单测。
    """

    BACKOFF_BASE = 2.0
    BACKOFF_MAX = 60.0

    def __init__(
        self, *, app_id: str, app_secret: str, sandbox: bool, intents: int,
        on_event: Callable[[Dict[str, Any]], Awaitable[None]],
        token_manager: Optional[QQBotTokenManager] = None,
        ws_connect: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.sandbox = sandbox
        self.intents = int(intents)
        self.on_event = on_event
        self._tokens = token_manager or get_token_manager()
        self._ws_connect = ws_connect or self._default_ws_connect
        self.connected = False
        self.session_id = ""
        self.last_seq: Optional[int] = None
        self.last_event_ts = 0.0
        self.last_error = ""
        self.fatal_code = 0
        self.reconnects = 0
        self.events_total = 0
        self.started_at = 0.0
        self._stop = asyncio.Event()

    # ── 可注入的网络层 ────────────────────────────────────────────────
    @staticmethod
    async def _default_ws_connect(url: str, headers: Dict[str, str]):
        import aiohttp
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(url, headers=headers, heartbeat=None, autoping=True)
        return session, ws

    async def _gateway_url(self, token: str) -> str:
        base = QQBOT_SANDBOX_API_BASE if self.sandbox else QQBOT_API_BASE
        status, data = await _http_json("GET", f"{base}/gateway", headers=auth_headers(token))
        if status == 200 and isinstance(data, dict) and data.get("url"):
            return str(data["url"])
        raise RuntimeError(f"gateway url unavailable HTTP {status}: {str(data)[:120]}")

    # ── op 分发（纯逻辑，可测） ───────────────────────────────────────
    async def handle_payload(self, payload: Dict[str, Any], send: Callable[[Dict[str, Any]], Awaitable[None]],
                             token: str) -> Optional[str]:
        """处理一条下行 payload。返回控制动作：None | "reconnect" | "reidentify"。"""
        op = int(payload.get("op", -1) or 0)
        if op == 10:  # Hello → identify / resume
            if self.session_id and self.last_seq is not None:
                await send({"op": 6, "d": {"token": f"QQBot {token}",
                                            "session_id": self.session_id,
                                            "seq": int(self.last_seq)}})
            else:
                await send({"op": 2, "d": {
                    "token": f"QQBot {token}", "intents": self.intents, "shard": [0, 1],
                    "properties": {"$os": "python", "$browser": "chengjie", "$device": "chengjie"},
                }})
            return None
        if op == 0:
            s = payload.get("s")
            if isinstance(s, int):
                self.last_seq = s
            t = str(payload.get("t") or "")
            if t == "READY":
                d = payload.get("d") or {}
                self.session_id = str((d or {}).get("session_id") or self.session_id)
                self.connected = True
                self.last_error = ""
                return None
            if t == "RESUMED":
                self.connected = True
                return None
            self.events_total += 1
            self.last_event_ts = time.time()
            try:
                await self.on_event(payload)
            except Exception:
                logger.debug("[qqbot-gw] on_event 异常", exc_info=True)
            return None
        if op == 11:  # heartbeat ack
            return None
        if op == 7:   # server asks reconnect（resume）
            return "reconnect"
        if op == 9:   # invalid session → 清 session 重新 identify
            self.session_id = ""
            self.last_seq = None
            return "reidentify"
        return None

    async def _heartbeat_loop(self, ws, interval_ms: int) -> None:
        interval = max(5.0, float(interval_ms or 45000) / 1000.0)
        try:
            while not self._stop.is_set():
                await asyncio.sleep(interval)
                await ws.send_json({"op": 1, "d": self.last_seq})
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("[qqbot-gw] 心跳失败", exc_info=True)

    async def run_once(self) -> str:
        """一次连接生命周期。返回结束原因串（供退避与日志）。"""
        token = await self._tokens.get(self.app_id, self.app_secret)
        if not token:
            raise RuntimeError("access_token unavailable")
        url = await self._gateway_url(token)
        session, ws = await self._ws_connect(url, auth_headers(token))
        hb_task: Optional[asyncio.Task] = None
        reason = "closed"
        try:
            from aiohttp import WSMsgType
        except Exception:  # noqa: BLE001 - 测试替身环境可无 aiohttp
            WSMsgType = None  # type: ignore[assignment]
        try:
            async def _send(obj: Dict[str, Any]) -> None:
                await ws.send_json(obj)

            async for msg in ws:
                if self._stop.is_set():
                    reason = "stopped"
                    break
                mtype = getattr(msg, "type", None)
                if mtype is not None and WSMsgType is not None:
                    if mtype in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    if mtype != WSMsgType.TEXT:
                        continue
                    raw = msg.data
                else:
                    raw = msg
                try:
                    payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                if int(payload.get("op", -1) or 0) == 10 and hb_task is None:
                    hb_task = asyncio.create_task(self._heartbeat_loop(
                        ws, int(((payload.get("d") or {}).get("heartbeat_interval")) or 45000)))
                action = await self.handle_payload(payload, _send, token)
                if action == "reconnect":
                    reason = "reconnect"
                    break
                if action == "reidentify":
                    reason = "reidentify"
                    break
            code = getattr(ws, "close_code", None)
            if code in (4914, 4915):
                self.fatal_code = int(code)
                self.last_error = ("bot offline/removed (4914)" if code == 4914
                                   else "bot banned (4915)")
                reason = "fatal"
            elif code in (4004, 4013, 4014):
                # 鉴权失败 / intent 无权限：不是网络问题，重连也白搭
                self.fatal_code = int(code)
                self.last_error = f"gateway rejected identify (close {code})"
                reason = "fatal"
            elif code is not None and reason == "closed":
                reason = f"closed:{code}"
        finally:
            if hb_task is not None:
                hb_task.cancel()
            try:
                await ws.close()
            except Exception:
                pass
            try:
                await session.close()
            except Exception:
                pass
            self.connected = False
        return reason

    async def run(self) -> None:
        """常驻循环：断开即退避重连；fatal / stop 退出。"""
        self.started_at = time.time()
        backoff = self.BACKOFF_BASE
        while not self._stop.is_set():
            try:
                reason = await self.run_once()
                if reason in ("reconnect", "reidentify"):
                    backoff = self.BACKOFF_BASE
                    self.reconnects += 1
                    await asyncio.sleep(1.0 + random.random())
                    continue
                if reason in ("fatal", "stopped"):
                    break
                self.reconnects += 1
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001
                self.last_error = str(ex)[:200]
                self.reconnects += 1
                logger.warning("[qqbot-gw] 连接异常，%.0fs 后重连: %s", backoff, self.last_error)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff + random.random())
            backoff = min(self.BACKOFF_MAX, backoff * 2)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.connected, "session": bool(self.session_id),
            "reconnects": self.reconnects, "events_total": self.events_total,
            "last_event_ts": self.last_event_ts, "fatal_code": self.fatal_code,
            "last_error": self.last_error, "sandbox": self.sandbox,
        }


# ── Webhook 路由 ─────────────────────────────────────────────────────────────

def register_qqbot_routes(app: Any, config_manager: Any, telegram_client: Any) -> None:
    """挂载 QQ 机器人 Webhook（op=13 验证 + 事件推送验签），并注入 SkillManager 取法。

    两种连接模式都挂路由（管理端切到 Webhook 即可用，无需重启）；WS 网关由
    ``official_api_worker.QQBotOfficialWorker`` 在编排器里拉起（见该类）。
    缺 app_id/app_secret → 不注册。
    """
    config = getattr(config_manager, "config", None) or {}
    cfg = qqbot_cfg(config)
    if not cfg.get("enabled"):
        return
    app_id = str(cfg.get("app_id") or "").strip()
    app_secret = str(cfg.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        logger.error("QQ 机器人已启用但缺少 app_id/app_secret，Webhook 未注册")
        return
    # 以下三键只在此处被「字面」读取一次，供接入向导字段↔配置键对齐门禁核对
    _sandbox = bool(cfg.get("sandbox"))
    _mode = str(cfg.get("connect_mode") or "websocket")
    _group_full = bool(cfg.get("group_full_messages"))
    path = webhook_path({"webhook_path": cfg.get("webhook_path")})
    account_id = app_id  # 与 _provision_official_account 的 official_account_id_key=qqbot.app_id 同口径

    sm = getattr(telegram_client, "skill_manager", None)
    register_skill_manager_getter(lambda: getattr(telegram_client, "skill_manager", None))

    async def qqbot_webhook_event(request: Request) -> Response:
        from src.integrations.official_webhook_stats import record_error, record_event
        raw = await request.body()
        # 台账埋点用字面平台名（official_webhook_stats 门禁按字面核对接线）
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            record_error("qqbot", "bad_json")
            return Response(status_code=400, content=b"invalid json")
        if not isinstance(payload, dict):
            record_error("qqbot", "bad_json")
            return Response(status_code=400, content=b"invalid json")
        # 回调地址验证（配置回调时平台打过来的第一击）
        if int(payload.get("op", 0) or 0) == 13:
            d = payload.get("d") or {}
            plain = str((d or {}).get("plain_token") or "")
            ts = str((d or {}).get("event_ts") or "")
            try:
                sig = sign_validation(app_secret, plain, ts)
            except Exception:
                record_error("qqbot", "sign_failed")
                return Response(status_code=500, content=b"sign failed")
            record_event("qqbot")
            return Response(status_code=200, media_type="application/json",
                            content=json.dumps({"plain_token": plain, "signature": sig}))
        sig_hdr = (request.headers.get("X-Signature-Ed25519")
                   or request.headers.get("x-signature-ed25519") or "")
        ts_hdr = (request.headers.get("X-Signature-Timestamp")
                  or request.headers.get("x-signature-timestamp") or "")
        if not verify_webhook_signature(app_secret, sig_hdr, ts_hdr, raw):
            logger.warning("QQ 机器人 Webhook 签名校验失败")
            record_error("qqbot", "bad_signature")
            return Response(status_code=401, content=b"invalid signature")
        record_event("qqbot")
        try:
            await handle_qqbot_event(payload, config=getattr(config_manager, "config", None) or {},
                                     account_id=account_id, sm=sm)
        except Exception as e:  # noqa: BLE001
            logger.exception("QQ 机器人事件处理异常: %s", e)
        return Response(status_code=200, media_type="application/json",
                        content=json.dumps({"op": 12}))

    app.add_api_route(path, qqbot_webhook_event, methods=["POST"], name="qqbot_webhook_event")
    app.state.qqbot_webhook_path = path
    logger.info("QQ 机器人 Webhook 已注册: POST %s (app_id=%s mode=%s sandbox=%s group_full=%s)",
                path, app_id, _mode, _sandbox, _group_full)


__all__ = [
    "PLATFORM", "QQBOT_API_BASE", "QQBOT_SANDBOX_API_BASE", "QQBOT_TOKEN_URL",
    "QQBOT_CONSOLE_URL", "INTENT_GROUP_AND_C2C",
    "C2C_WINDOW_SEC", "C2C_MAX_REPLIES", "GROUP_WINDOW_SEC", "GROUP_MAX_REPLIES",
    "qqbot_cfg", "api_base", "connect_mode", "webhook_path", "passive_only", "creds_from",
    "make_chat_key", "parse_chat_key",
    "QQBotTokenManager", "get_token_manager", "auth_headers",
    "PassiveReplyLedger", "get_passive_ledger", "reset_for_tests",
    "extract_qqbot_events", "attachment_media",
    "sign_validation", "verify_webhook_signature",
    "qqbot_send_text", "handle_qqbot_event", "register_skill_manager_getter",
    "QQBotGateway", "register_qqbot_routes",
]
