"""QQ 协议登录（个人号）—— platform=``qq``，mode=``protocol``（2026-09-07，QQ 双轨之准入区轨）。

与 ``qqbot``（QQ 开放平台官方机器人，见 ``qq_official.py``）是**两个独立平台**：这里登录的是
用户**自己的 QQ 号**，身份是 QQ 号 + 昵称 + 头像，能力与 Telegram 协议号对齐（文字/图/语音/视频、
已读、群全量消息、群管理、好友/群成员列表）；但属非官方接入（腾讯 ToS 之外，有风控风险）→
默认关、准入区、``notice_unofficial`` 正向建议（小号 + 固定 IP）。

**协议端由用户自装、本模块只做 Milky 应用端**（`milky.ntqqrev.org`，MIT，v1.3）：NapCat /
LLOneBot / Lagrange.Milky 三家协议端都实现了 Milky——我们刻意不打包任何协议端（NapCat 许可证
禁商用、Lagrange GPL-3.0）、不绑死其中一家。Milky 通信：``POST {base}/api/{api}``（JSON，
``Authorization: Bearer {token}``，响应 ``{status, retcode, data|message}``；``-403``＝协议端未登录）
+ ``GET {base}/event`` WebSocket 事件流（``{time, self_id, event_type, data}``）。

config.yaml（键名与接入向导 ``channel_setup.Channel(id="qq")`` 逐字对齐）::

  platform_login:
    qq:
      protocol_enabled: false        # 默认关（非官方接入，运营显式 opt-in；不进桌面默认开表）
      milky_url: "http://127.0.0.1:3000"   # 协议端 Milky 服务地址（NapCat/LLOneBot/Lagrange）
      milky_token: ""                # 协议端 access_token（强烈建议设置）
      auto_accept_friend: false      # 收到好友申请是否自动同意

账号 ``meta``：``milky_url`` / ``milky_token``（登录时从平台配置快照进账号，之后每号各用各的
端点——一号一协议端）、``uin`` / ``nickname``。

chat_key 约定（自描述，落库层 ``normalizer.infer_chat_type`` 认 ``:group:``）：
``qq:friend:<QQ号>`` 私聊 / ``qq:group:<群号>`` 群 / ``qq:temp:<QQ号>`` 临时会话（只收不发）。

网络调用集中在 ``MilkyClient``（``http`` / ``ws_connect`` 可注入），纯逻辑（消息段渲染、
chat_key、事件归一）可离线单测。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from src.integrations.platform_login import resolve_login_switch

logger = logging.getLogger(__name__)

PLATFORM = "qq"
DEFAULT_MILKY_URL = "http://127.0.0.1:3000"
#: 出站媒体内联 base64 的体积上限（协议端可能在另一台机器，file:// 路径不可达；小文件走 base64
#: 更稳；超限回落 file://（要求协议端与本进程同机））
INLINE_MEDIA_MAX_BYTES = 15 * 1024 * 1024
#: 入站媒体落盘上限 / 拉取超时：Milky 给的是**临时 URL**（QQ CDN，会过期），与 LINE /
#: 微信客服同口径先落 ``protocol_media`` 根再入库，坐席回看、AI 识图/转写才不会踩到失效链接。
INBOUND_MEDIA_MAX_BYTES = 50 * 1024 * 1024
INBOUND_MEDIA_TIMEOUT_SEC = 20.0
#: Milky ``retcode``：协议端未登录
RETCODE_NOT_LOGGED_IN = -403
#: QQ 头像 CDN（公开规则，按 QQ 号取）
AVATAR_URL_FMT = "https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"


# ── 配置 ─────────────────────────────────────────────────────────────────────

def _qq_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    pl = (config or {}).get("platform_login", {}) or {}
    return dict(pl.get("qq", {}) or {})


def protocol_enabled(config: Dict[str, Any]) -> bool:
    """显式配置优先（含 false）；未写过 → False（非官方接入不随桌面默认开）。单一事实源。"""
    return resolve_login_switch(config, "platform_login.qq.protocol_enabled")


def service_base_url(config: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> str:
    m = dict(meta or {})
    return str(m.get("milky_url") or _qq_cfg(config).get("milky_url") or DEFAULT_MILKY_URL).rstrip("/")


def service_token(config: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> str:
    m = dict(meta or {})
    return str(m.get("milky_token") or _qq_cfg(config).get("milky_token") or "").strip()


def auto_accept_friend(config: Dict[str, Any]) -> bool:
    return bool(_qq_cfg(config).get("auto_accept_friend"))


def avatar_url_for(uin: Any) -> str:
    s = str(uin or "").strip()
    return AVATAR_URL_FMT.format(uin=s) if s.isdigit() else ""


# ── chat_key ─────────────────────────────────────────────────────────────────

_SCENES = ("friend", "group", "temp")


def make_chat_key(scene: str, peer_id: Any) -> str:
    sc = str(scene or "friend").lower()
    if sc not in _SCENES:
        sc = "friend"
    return f"{PLATFORM}:{sc}:{str(peer_id or '').strip()}"


def parse_chat_key(chat_key: str) -> Tuple[str, str]:
    """``qq:group:123`` → ("group", "123")；裸数字 → ("friend", 数字)。"""
    s = str(chat_key or "").strip()
    parts = s.split(":")
    if len(parts) >= 3 and parts[0].lower() == PLATFORM and parts[1].lower() in _SCENES:
        return parts[1].lower(), ":".join(parts[2:])
    if len(parts) == 2 and parts[0].lower() in _SCENES:
        return parts[0].lower(), parts[1]
    return "friend", s


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ── Milky 客户端 ─────────────────────────────────────────────────────────────

class MilkyError(Exception):
    def __init__(self, retcode: int, message: str, api: str = "") -> None:
        super().__init__(f"milky {api} retcode={retcode}: {message}")
        self.retcode = int(retcode)
        self.message = str(message or "")
        self.api = api

    @property
    def not_logged_in(self) -> bool:
        return self.retcode == RETCODE_NOT_LOGGED_IN


async def _default_http(method: str, url: str, *, headers: Dict[str, str],
                        payload: Optional[Dict[str, Any]], timeout: float) -> Tuple[int, Any]:
    import aiohttp
    tmo = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=tmo) as session:
        async with session.request(method, url, headers=headers, json=payload) as resp:
            raw = await resp.text()
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {"raw": raw[:500]}
            return int(resp.status), data


class MilkyClient:
    """一个协议端端点的薄客户端：``call(api, params)`` + ``events()``。永不吞 MilkyError。"""

    def __init__(self, base_url: str, token: str = "", *, timeout: float = 20.0,
                 http: Optional[Callable[..., Awaitable[Tuple[int, Any]]]] = None,
                 ws_connect: Optional[Callable[..., Any]] = None) -> None:
        self.base_url = str(base_url or DEFAULT_MILKY_URL).rstrip("/")
        self.token = str(token or "")
        self.timeout = float(timeout)
        self._http = http or _default_http
        self._ws_connect = ws_connect or self._default_ws_connect

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def call(self, api: str, params: Optional[Dict[str, Any]] = None,
                   *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """成功返回 ``data``（dict）；``status=failed`` → MilkyError；HTTP/网络错误 → RuntimeError。"""
        status, body = await self._http(
            "POST", f"{self.base_url}/api/{api}", headers=self._headers(),
            payload=dict(params or {}), timeout=float(timeout or self.timeout))
        if status == 401:
            raise MilkyError(-401, "access_token 不匹配或未提供", api)
        if status == 404:
            raise MilkyError(-404, f"协议端不支持 API {api}", api)
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"milky {api} HTTP {status}: {str(body)[:200]}")
        if str(body.get("status") or "") != "ok" or int(body.get("retcode", 0) or 0) != 0:
            raise MilkyError(int(body.get("retcode", -1) or -1), str(body.get("message") or ""), api)
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    async def _default_ws_connect(url: str, headers: Dict[str, str]):
        import aiohttp
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(url, headers=headers, heartbeat=30.0)
        return session, ws

    def ws_url(self) -> str:
        base = self.base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}/event"

    async def events(self, stop: asyncio.Event) -> AsyncIterator[Dict[str, Any]]:
        """一条 WS 连接生命周期内的事件流（断开即结束；重连由调用方决定）。"""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        session, ws = await self._ws_connect(self.ws_url(), headers)
        try:
            try:
                from aiohttp import WSMsgType
            except Exception:  # noqa: BLE001
                WSMsgType = None  # type: ignore[assignment]
            async for msg in ws:
                if stop.is_set():
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
                    ev = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                except Exception:
                    continue
                if isinstance(ev, dict) and ev.get("event_type"):
                    yield ev
        finally:
            try:
                await ws.close()
            except Exception:
                pass
            try:
                await session.close()
            except Exception:
                pass


# ── 消息段 ↔ 文本 / 媒体 ────────────────────────────────────────────────────

_FACE_PLACEHOLDER = "[表情]"


def _first_media_seg(segments: Any) -> Optional[Dict[str, Any]]:
    """首个媒体段（图/语音/视频/文件）——决定入站是否要走「先落盘再入库」路径。"""
    for seg in (segments or []):
        if isinstance(seg, dict) and str(seg.get("type") or "") in ("image", "record", "video", "file"):
            return seg
    return None


_CTYPE_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/bmp": ".bmp", "audio/silk": ".silk", "audio/amr": ".amr", "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mp4": ".m4a",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
}
_KIND_DEFAULT_EXT = {"image": ".jpg", "sticker": ".gif", "voice": ".amr", "video": ".mp4"}


def _media_ext(media_type: str, content_type: str, url: str) -> str:
    """落盘扩展名：Content-Type 优先 → URL 后缀 → 按媒体大类缺省。"""
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    if ct in _CTYPE_EXT:
        return _CTYPE_EXT[ct]
    path = str(url or "").split("?", 1)[0]
    ext = os.path.splitext(path)[1].lower()
    if 1 < len(ext) <= 5 and ext[1:].isalnum():
        return ext
    return _KIND_DEFAULT_EXT.get(str(media_type or ""), ".bin")


def render_segments(segments: Any, self_id: Optional[int] = None) -> Dict[str, Any]:
    """Milky ``IncomingSegment[]`` → 收件箱可用形态（纯函数）。

    返回 ``{"text", "mentioned", "media": [{media_type, url, resource_id}], "reply_seq"}``。
    首个图/语音/视频/文件进 media（收件箱单媒体字段），其余媒体以占位并入 text 保住语义。
    """
    text_parts: List[str] = []
    media: List[Dict[str, Any]] = []
    mentioned = False
    reply_seq: Optional[int] = None
    for seg in (segments or []):
        if not isinstance(seg, dict):
            continue
        t = str(seg.get("type") or "")
        d = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if t == "text":
            text_parts.append(str(d.get("text") or ""))
        elif t == "mention":
            uid = _int_or_none(d.get("user_id"))
            if self_id is not None and uid == int(self_id):
                mentioned = True
            name = str(d.get("name") or "")
            text_parts.append(f"@{name or uid or ''} ")
        elif t == "mention_all":
            mentioned = True
            text_parts.append("@全体成员 ")
        elif t == "face":
            text_parts.append(_FACE_PLACEHOLDER)
        elif t == "reply":
            reply_seq = _int_or_none(d.get("message_seq"))
        elif t in ("image", "record", "video"):
            kind = {"image": "image", "record": "voice", "video": "video"}[t]
            if t == "image" and str(d.get("sub_type") or "") == "sticker":
                kind = "sticker"
            item = {"media_type": kind, "url": str(d.get("temp_url") or ""),
                    "resource_id": str(d.get("resource_id") or "")}
            if not media:
                media.append(item)
            else:
                text_parts.append({"image": "[图片]", "sticker": "[贴纸]", "voice": "[语音]",
                                   "video": "[视频]"}[kind])
        elif t == "file":
            item = {"media_type": "file", "url": "", "resource_id": str(d.get("file_id") or ""),
                    "file_name": str(d.get("file_name") or ""),
                    "file_hash": str(d.get("file_hash") or "")}
            if not media:
                media.append(item)
            text_parts.append(f"[文件] {item['file_name']}".rstrip())
        elif t == "forward":
            text_parts.append(f"[合并转发] {str(d.get('summary') or d.get('title') or '')}".rstrip())
        elif t == "market_face":
            text_parts.append(f"[表情] {str(d.get('summary') or '')}".rstrip())
        elif t == "light_app":
            text_parts.append(f"[小程序] {str(d.get('app_name') or '')}".rstrip())
        elif t == "xml":
            text_parts.append("[卡片]")
        elif t == "markdown":
            text_parts.append(str(d.get("content") or ""))
    text = "".join(text_parts).strip()
    return {"text": text, "mentioned": mentioned, "media": media, "reply_seq": reply_seq}


def build_text_segments(text: str, *, reply_seq: Optional[int] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if reply_seq is not None:
        out.append({"type": "reply", "data": {"message_seq": int(reply_seq)}})
    out.append({"type": "text", "data": {"text": str(text or "")}})
    return out


def media_uri(path: str, *, inline_limit: int = INLINE_MEDIA_MAX_BYTES) -> str:
    """本地文件 → Milky 文件 URI：小文件 ``base64://``（协议端异机也可达），超限 ``file://``。"""
    p = os.path.abspath(str(path or ""))
    try:
        size = os.path.getsize(p)
    except OSError:
        size = -1
    if 0 <= size <= inline_limit:
        with open(p, "rb") as fh:
            return "base64://" + base64.b64encode(fh.read()).decode("ascii")
    return "file:///" + p.replace("\\", "/").lstrip("/")


_MEDIA_SEG_TYPE = {
    "image": "image", "photo": "image", "sticker": "image", "gif": "image",
    "voice": "record", "audio": "record", "record": "record",
    "video": "video",
}


def build_media_segments(media_type: str, path: str, *, caption: str = "") -> List[Dict[str, Any]]:
    """出站媒体段（图/语音/视频；文件类 Milky 无消息段——调用方回 not_supported）。"""
    seg_type = _MEDIA_SEG_TYPE.get(str(media_type or "").lower())
    if not seg_type:
        raise ValueError(f"unsupported media_type: {media_type}")
    data: Dict[str, Any] = {"uri": media_uri(path)}
    if seg_type == "image" and str(media_type).lower() == "sticker":
        data["sub_type"] = "sticker"
    segs: List[Dict[str, Any]] = []
    if caption and seg_type != "record":
        segs.append({"type": "text", "data": {"text": str(caption)}})
    segs.append({"type": seg_type, "data": data})
    return segs


# ── 事件归一（纯函数） ────────────────────────────────────────────────────────

def normalize_message_event(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``message_receive`` 事件 → ``{scene, peer_id, sender_id, seq, ts, segments, name,
    sender_name, avatar_url, group_name}``；非消息事件 → None。"""
    if not isinstance(ev, dict) or str(ev.get("event_type") or "") != "message_receive":
        return None
    d = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    scene = str(d.get("message_scene") or "friend")
    peer = str(d.get("peer_id") or "")
    sender = str(d.get("sender_id") or "")
    if not peer:
        return None
    friend = d.get("friend") if isinstance(d.get("friend"), dict) else {}
    group = d.get("group") if isinstance(d.get("group"), dict) else {}
    member = d.get("group_member") if isinstance(d.get("group_member"), dict) else {}
    if scene == "group":
        name = str(group.get("group_name") or "")
        sender_name = str(member.get("card") or member.get("nickname") or "")
    else:
        name = str(friend.get("remark") or friend.get("nickname") or "")
        sender_name = name
    return {
        "scene": scene if scene in _SCENES else "friend",
        "peer_id": peer, "sender_id": sender,
        "seq": _int_or_none(d.get("message_seq")),
        "ts": float(d.get("time") or ev.get("time") or time.time()),
        "segments": list(d.get("segments") or []),
        "name": name, "sender_name": sender_name,
        "avatar_url": avatar_url_for(sender if scene == "group" else peer),
        "group_name": str(group.get("group_name") or ""),
        "self_id": _int_or_none(ev.get("self_id")),
    }


# ── worker ───────────────────────────────────────────────────────────────────

class QQPersonalWorker:
    """保活一个 QQ 个人号（用户自装协议端，经 Milky）：事件流 → 收件箱；出站/已读/群管理 → 协议端。

    与 LINE protocol worker 同族（Python 侧直接持有长连）；协议端进程本身由用户/其守护进程保活，
    本 worker 只在 WS 断开时按退避重连，``healthy()`` 以 ``get_login_info`` 为准
    （``-403`` = 协议端在但 QQ 未登录 → 报 needs_login 让账号栏出「重新登录」）。
    """

    BACKOFF_BASE = 2.0
    BACKOFF_MAX = 60.0
    HEALTH_TTL_SEC = 15.0

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.account = account
        self.config = config
        self.account_id = str(account.get("account_id") or "")
        meta = dict(account.get("meta") or {})
        self.client = MilkyClient(service_base_url(config, meta), service_token(config, meta))
        self.state = "stopped"
        self.detail = ""
        self.uin: Optional[int] = _int_or_none(meta.get("uin") or self.account_id)
        self.nickname = str(meta.get("nickname") or "")
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_in_seq: Dict[str, int] = {}
        self._health_ts = 0.0
        self._health_ok = False
        self.events_total = 0
        self.last_event_ts = 0.0
        self.reconnects = 0
        self.impl: Dict[str, Any] = {}

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        info = await self.client.call("get_login_info")   # 未登录/不可达 → 抛给编排器退避
        self.uin = _int_or_none(info.get("uin")) or self.uin
        self.nickname = str(info.get("nickname") or self.nickname)
        try:
            self.impl = await self.client.call("get_impl_info")
        except Exception:
            self.impl = {}
        self._health_ok, self._health_ts = True, time.time()
        self._report("authorized", detail=self.nickname)
        self._task = asyncio.create_task(self._run_events())
        self.state = "running"
        self.detail = f"{self.impl.get('impl_name', 'milky')} {self.impl.get('impl_version', '')}".strip()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.state = "stopped"

    async def healthy(self) -> bool:
        now = time.time()
        if now - self._health_ts < self.HEALTH_TTL_SEC:
            return self._health_ok
        try:
            info = await self.client.call("get_login_info", timeout=8.0)
            self.uin = _int_or_none(info.get("uin")) or self.uin
            self._health_ok = True
            self.detail = self.detail or "milky"
        except MilkyError as ex:
            self._health_ok = False
            self.detail = "协议端已连上但 QQ 未登录（去协议端 WebUI 扫码）" if ex.not_logged_in else str(ex)[:160]
            self._report("needs_login" if ex.not_logged_in else "failed", detail=self.detail)
        except Exception as ex:  # noqa: BLE001
            self._health_ok = False
            self.detail = f"协议端不可达: {str(ex)[:120]}"
        self._health_ts = now
        if self._health_ok and self._task is not None and self._task.done():
            self.detail = "event stream exited"
            return False
        return self._health_ok

    def status(self) -> Dict[str, Any]:
        return {
            "type": "qq_milky", "account_id": self.account_id, "state": self.state,
            "detail": self.detail, "uin": self.uin, "nickname": self.nickname,
            "impl": self.impl, "events_total": self.events_total,
            "last_event_ts": self.last_event_ts, "reconnects": self.reconnects,
            "milky_url": self.client.base_url,
        }

    def _report(self, status: str, *, detail: str = "") -> None:
        try:
            from src.integrations.platform_session_health import report_session_transition
            report_session_transition(PLATFORM, self.account_id, status, detail=detail)
        except Exception:
            pass

    # ── 事件流 ──────────────────────────────────────────────────────────
    async def _run_events(self) -> None:
        backoff = self.BACKOFF_BASE
        while not self._stop.is_set():
            try:
                async for ev in self.client.events(self._stop):
                    backoff = self.BACKOFF_BASE
                    self.events_total += 1
                    self.last_event_ts = time.time()
                    self._handle_event(ev)
                if self._stop.is_set():
                    break
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001
                logger.warning("[qq-milky] 事件流断开，%.0fs 后重连: %s", backoff, str(ex)[:160])
            self.reconnects += 1
            await asyncio.sleep(backoff * (1.0 + random.random()))
            backoff = min(self.BACKOFF_MAX, backoff * 2)

    def _handle_event(self, ev: Dict[str, Any]) -> None:
        et = str(ev.get("event_type") or "")
        if et == "message_receive":
            msg = normalize_message_event(ev)
            if msg is not None and str(msg.get("sender_id") or "") != str(self.uin or ""):
                if self._loop is not None and _first_media_seg(msg.get("segments")) is not None:
                    # 带媒体：先落盘再入库（与 LINE 同序——AI 识图/转写与坐席回看都拿稳定链接）
                    self._schedule(self._ingest_with_media(msg))
                else:
                    self._ingest_inbound(msg)
            return
        if et == "bot_offline":
            reason = str(((ev.get("data") or {}).get("reason")) or "offline")
            self._health_ok, self._health_ts = False, time.time()
            self.detail = f"QQ 已离线: {reason}"
            self._report("logged_out", detail=reason)
            return
        if et == "friend_request":
            d = ev.get("data") or {}
            logger.info("[qq-milky] 好友申请 from=%s via=%s comment=%r", d.get("initiator_id"),
                        d.get("via"), str(d.get("comment") or "")[:60])
            if auto_accept_friend(self.config) and self._loop is not None and d.get("initiator_uid"):
                self._schedule(self._safe_call(
                    "accept_friend_request",
                    {"initiator_uid": str(d["initiator_uid"]), "is_filtered": False}))
            return
        logger.debug("[qq-milky] 忽略事件 %s", et)

    async def _safe_call(self, api: str, params: Dict[str, Any]) -> None:
        try:
            await self.client.call(api, params)
        except Exception:
            logger.debug("[qq-milky] %s 失败", api, exc_info=True)

    async def _ingest_with_media(self, msg: Dict[str, Any]) -> None:
        """带媒体的入站：把临时 URL 落到 ``protocol_media`` 根后再入库；拉不到就用临时 URL 兜底。
        文件段没有临时 URL，先向协议端换下载链接（``get_*_file_download_url``）。"""
        rendered = render_segments(msg.get("segments"), self_id=self.uin)
        media = (rendered.get("media") or [{}])[0]
        media_type = str(media.get("media_type") or "")
        url = str(media.get("url") or "")
        if media_type == "file" and not url:
            url = await self._file_download_url(msg, media)
        local = await self._persist_media(media_type, url, msg.get("seq"),
                                          file_name=str(media.get("file_name") or ""))
        self._ingest_inbound(msg, media_ref_override=local or url)

    async def _file_download_url(self, msg: Dict[str, Any], media: Dict[str, Any]) -> str:
        file_id = str(media.get("resource_id") or "")
        pid = _int_or_none(msg.get("peer_id"))
        if not file_id or pid is None:
            return ""
        scene = str(msg.get("scene") or "friend")
        try:
            if scene == "group":
                data = await self.client.call("get_group_file_download_url",
                                              {"group_id": pid, "file_id": file_id})
            else:
                params: Dict[str, Any] = {"user_id": pid, "file_id": file_id}
                if media.get("file_hash"):
                    params["file_hash"] = str(media["file_hash"])
                data = await self.client.call("get_private_file_download_url", params)
            return str(data.get("download_url") or "")
        except Exception:
            logger.debug("[qq-milky] 取文件下载链接失败 file_id=%s", file_id, exc_info=True)
            return ""

    async def _persist_media(self, media_type: str, url: str, seq: Any, *,
                             file_name: str = "") -> str:
        """下载入站媒体 → ``/static/protocol_media/qq/<账号>_<seq>.<ext>``；失败返回空串（不抛）。"""
        if not url.startswith(("http://", "https://")):
            return ""
        try:
            import aiohttp
            from src.integrations.protocol_bridge import media_paths
            buf = bytearray()
            tmo = aiohttp.ClientTimeout(total=INBOUND_MEDIA_TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=tmo) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return ""
                    ctype = str(resp.headers.get("Content-Type") or "")
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        buf += chunk
                        if len(buf) > INBOUND_MEDIA_MAX_BYTES:
                            logger.info("[qq-milky] 入站媒体超限跳过落盘 %s bytes>%d",
                                        media_type, INBOUND_MEDIA_MAX_BYTES)
                            return ""
            if not buf:
                return ""
            dest, static_url = media_paths(
                PLATFORM, f"{self.account_id}_{seq if seq is not None else int(time.time())}",
                _media_ext(media_type, ctype, file_name or url))
            dest.write_bytes(bytes(buf))
            return static_url
        except Exception:
            logger.debug("[qq-milky] 入站媒体落盘失败 type=%s", media_type, exc_info=True)
            return ""

    def _ingest_inbound(self, msg: Dict[str, Any], *, media_ref_override: str = "") -> None:
        """入站消息统一投递口（best-effort 绝不抛）：渲染消息段 → 落库 → 自动回复。"""
        try:
            from src.integrations.protocol_bridge import (
                emit_incoming, make_message, maybe_auto_reply,
            )
            scene = str(msg.get("scene") or "friend")
            is_group = scene == "group"
            chat_key = make_chat_key(scene, msg["peer_id"])
            seq = msg.get("seq")
            if seq is not None:
                if len(self._last_in_seq) > 2000:
                    self._last_in_seq.clear()
                self._last_in_seq[chat_key] = int(seq)
            rendered = render_segments(msg.get("segments"), self_id=self.uin)
            media = (rendered.get("media") or [{}])[0] if rendered.get("media") else {}
            media_type = str(media.get("media_type") or "")
            media_ref = media_ref_override or str(media.get("url") or "")
            text = str(rendered.get("text") or "")
            if not text and media_type:
                text = {"image": "[图片]", "sticker": "[贴纸]", "voice": "[语音]",
                        "video": "[视频]", "file": "[文件]"}.get(media_type, "[媒体]")
            payload = make_message(
                platform=PLATFORM, account_id=self.account_id, chat_key=chat_key,
                name=str(msg.get("name") or ""), avatar_url=str(msg.get("avatar_url") or ""),
                text=text, ts=float(msg.get("ts") or 0), msg_id=str(seq or ""),
                media_type=media_type, media_ref=media_ref, direction="in")
            if is_group:
                payload["chat_type"] = "group"
                payload["sender_id"] = str(msg.get("sender_id") or "")
                payload["sender_name"] = str(msg.get("sender_name") or "")
            if rendered.get("mentioned"):
                payload["mentioned"] = True
            if rendered.get("reply_seq") is not None:
                payload["reply_to"] = {"id": str(rendered["reply_seq"])}
            emit_incoming(payload)
            # 临时会话只收不发（Milky 无 temp 发送接口），不触发自动回复；群走群策略
            if scene == "friend" and self._loop is not None:
                self._schedule(maybe_auto_reply(payload))
        except Exception:
            logger.debug("[qq-milky] inbound 推送失败", exc_info=True)

    def _schedule(self, coro: Awaitable[Any]) -> None:
        """把协程排进 worker 的事件循环：同线程直接建 task（事件流回调就跑在这个循环上），
        跨线程走 run_coroutine_threadsafe（拉取兜底等外部调用者）。"""
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(coro)  # type: ignore[arg-type]
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]

    # ── 出站 ────────────────────────────────────────────────────────────
    async def send(self, chat_key: str, text: str,
                   *, reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        scene, peer = parse_chat_key(chat_key)
        pid = _int_or_none(peer)
        if pid is None:
            return {"delivered": False, "error": f"bad chat_key: {chat_key}"}
        if scene == "temp":
            return {"delivered": False, "error_kind": "unsupported",
                    "error": "Milky 无临时会话发送接口（只收不发）"}
        reply_seq = _int_or_none((reply_to or {}).get("id") or (reply_to or {}).get("msg_id"))
        segs = build_text_segments(text, reply_seq=reply_seq)
        try:
            if scene == "group":
                data = await self.client.call("send_group_message",
                                              {"group_id": pid, "message": segs})
            else:
                data = await self.client.call("send_private_message",
                                              {"user_id": pid, "message": segs})
        except MilkyError as ex:
            if ex.not_logged_in:
                self._health_ok, self._health_ts = False, time.time()
                self._report("needs_login", detail="not_logged_in")
            return {"delivered": False, "error": str(ex)[:200],
                    "error_kind": "invalid_token" if ex.not_logged_in else "unknown"}
        except Exception as ex:  # noqa: BLE001
            return {"delivered": False, "error": f"qq send failed: {str(ex)[:160]}",
                    "error_kind": "transient"}
        return {"delivered": True, "message_id": str(data.get("message_seq") or ""),
                "quote_applied": reply_seq is not None}

    async def send_media(self, chat_key: str, *, media_path: str, media_type: str,
                         caption: str = "") -> Dict[str, Any]:
        """出站图/语音/视频（协议端负责 silk 转码与上传）；文件类走 ``upload_*_file``（无消息段）。"""
        scene, peer = parse_chat_key(chat_key)
        pid = _int_or_none(peer)
        if pid is None or scene == "temp":
            return {"delivered": False, "error_kind": "unsupported", "error": f"bad target: {chat_key}"}
        if str(media_type or "").lower() in ("document", "file"):
            return await self._send_file(scene, pid, media_path, caption=caption)
        try:
            segs = build_media_segments(media_type, media_path, caption=caption)
        except ValueError as ex:
            return {"delivered": False, "error_kind": "not_supported", "error": str(ex)}
        except OSError as ex:
            return {"delivered": False, "error_kind": "unsupported", "error": f"media unreadable: {ex}"}
        try:
            if scene == "group":
                data = await self.client.call("send_group_message",
                                              {"group_id": pid, "message": segs}, timeout=120.0)
            else:
                data = await self.client.call("send_private_message",
                                              {"user_id": pid, "message": segs}, timeout=120.0)
        except MilkyError as ex:
            return {"delivered": False, "error": str(ex)[:200],
                    "error_kind": "invalid_token" if ex.not_logged_in else "unknown"}
        except Exception as ex:  # noqa: BLE001
            return {"delivered": False, "error": f"qq send_media failed: {str(ex)[:160]}",
                    "error_kind": "transient"}
        return {"delivered": True, "message_id": str(data.get("message_seq") or "")}

    async def _send_file(self, scene: str, pid: int, media_path: str, *,
                         caption: str = "") -> Dict[str, Any]:
        """文件类出站：Milky 无「文件」消息段，走 ``upload_private_file`` / ``upload_group_file``；
        配文（若有）另发一条文本（与 TG/WA「文件 + 说明」的观感一致）。"""
        try:
            uri = media_uri(media_path)
        except OSError as ex:
            return {"delivered": False, "error_kind": "unsupported", "error": f"media unreadable: {ex}"}
        file_name = os.path.basename(str(media_path or "")) or "file"
        if scene == "group":
            api, params = "upload_group_file", {"group_id": pid, "file_uri": uri,
                                                "file_name": file_name, "parent_folder_id": "/"}
        else:
            api, params = "upload_private_file", {"user_id": pid, "file_uri": uri,
                                                  "file_name": file_name}
        try:
            data = await self.client.call(api, params, timeout=300.0)
        except MilkyError as ex:
            return {"delivered": False, "error": str(ex)[:200],
                    "error_kind": "invalid_token" if ex.not_logged_in else "unknown"}
        except Exception as ex:  # noqa: BLE001
            return {"delivered": False, "error": f"qq {api} failed: {str(ex)[:160]}",
                    "error_kind": "transient"}
        out: Dict[str, Any] = {"delivered": True, "message_id": str(data.get("file_id") or ""),
                               "file_id": str(data.get("file_id") or "")}
        if str(caption or "").strip():
            cap = await self.send(make_chat_key(scene, pid), str(caption).strip())
            out["caption_delivered"] = bool(cap.get("delivered"))
        return out

    async def mark_read(self, chat_key: str) -> bool:
        """已读回执：标到该会话末条入站 seq（重启后未收过消息的会话不猜，返回 False）。"""
        seq = self._last_in_seq.get(str(chat_key or ""))
        if seq is None:
            return False
        scene, peer = parse_chat_key(chat_key)
        pid = _int_or_none(peer)
        if pid is None:
            return False
        try:
            await self.client.call("mark_message_as_read",
                                   {"message_scene": scene, "peer_id": pid, "message_seq": int(seq)})
            return True
        except Exception:
            logger.debug("[qq-milky] mark_read 失败", exc_info=True)
            return False

    async def delete_messages(self, chat_key: str, message_ids: List[str],
                              *, revoke: bool = True) -> Dict[str, Any]:
        """撤回若干条自己发出的消息（编排器 ``delete_messages`` 统一签名；message_id=message_seq）。

        QQ 只有「撤回＝对所有人」一种语义（约 2 分钟时限，超时协议端/服务端拒绝），
        ``revoke`` 形参仅为统一签名。逐条调用，单条失败不阻断其余。
        """
        scene, peer = parse_chat_key(chat_key)
        pid = _int_or_none(peer)
        if pid is None or scene == "temp":
            return {"ok": False, "reason": f"bad target: {chat_key}"}
        api = "recall_group_message" if scene == "group" else "recall_private_message"
        key = "group_id" if scene == "group" else "user_id"
        ok_n, last_err = 0, ""
        for mid in (message_ids or []):
            seq = _int_or_none(mid)
            if seq is None:
                continue
            try:
                await self.client.call(api, {key: pid, "message_seq": seq})
                ok_n += 1
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:120]
                logger.debug("[qq-milky] %s 失败 seq=%s", api, seq, exc_info=True)
        if ok_n <= 0:
            return {"ok": False, "reason": last_err or "recall_failed"}
        return {"ok": True, "deleted": ok_n}

    # ── 群管理（GROUP_ADMIN_METHODS 预留契约名，实现即自动进能力矩阵） ──────────
    async def kick_group_member(self, chat_key: str, user_id: Any,
                                *, reject_add_request: bool = False) -> bool:
        scene, gid = parse_chat_key(chat_key)
        g, u = _int_or_none(gid), _int_or_none(user_id)
        if scene != "group" or g is None or u is None:
            return False
        try:
            await self.client.call("kick_group_member",
                                   {"group_id": g, "user_id": u,
                                    "reject_add_request": bool(reject_add_request)})
            return True
        except Exception:
            logger.debug("[qq-milky] kick_group_member 失败", exc_info=True)
            return False

    async def rename_group(self, chat_key: str, new_name: str) -> bool:
        scene, gid = parse_chat_key(chat_key)
        g = _int_or_none(gid)
        if scene != "group" or g is None or not str(new_name or "").strip():
            return False
        try:
            await self.client.call("set_group_name",
                                   {"group_id": g, "new_group_name": str(new_name).strip()})
            return True
        except Exception:
            logger.debug("[qq-milky] set_group_name 失败", exc_info=True)
            return False


__all__ = [
    "PLATFORM", "DEFAULT_MILKY_URL", "INLINE_MEDIA_MAX_BYTES", "RETCODE_NOT_LOGGED_IN",
    "protocol_enabled", "service_base_url", "service_token", "auto_accept_friend", "avatar_url_for",
    "make_chat_key", "parse_chat_key",
    "MilkyError", "MilkyClient",
    "render_segments", "build_text_segments", "build_media_segments", "media_uri",
    "normalize_message_event", "QQPersonalWorker",
]
