# -*- coding: utf-8 -*-
"""微信客服（企业微信）API 客户端与消息归一（实施97 线 A，2026-09-07）。

微信客服＝企业微信下的官方客服通道：任何微信用户扫「客服链接/二维码」即可与企业的客服账号
（``open_kfid``）聊天，**不用加好友**。本模块只做三件事，全部可离线单测：

1. **HTTP 客户端** :class:`WeChatKfClient` —— ``gettoken`` 缓存/过期刷新、``kf/sync_msg`` 拉消息、
   ``kf/send_msg`` 发消息、媒体上传下载、会话状态查询/变更、客服账号/接待人员/客服链接。
   所有方法**永不抛**，统一返回 ``{"ok", "data", "errcode", "errmsg", "error_kind"}``（与 zalo/fb 官方
   助手同一口径，上层按 ``error_kind`` 分流）。
2. **回调加解密** :class:`KfCallbackCrypto` —— 企微 WXBizMsgCrypt（AES-256-CBC + SHA1 签名 +
   PKCS#7/32 填充 + ``random(16) | msg_len(4) | msg | receiveid`` 明文布局）。
3. **归一** :func:`normalize_kf_message` —— 把 ``sync_msg`` 单条（文本/图片/语音/视频/文件/位置/链接/
   名片/小程序/菜单回复/事件）压成 worker 能直接消费的扁平 dict。

平台契约（与 worker / 收件箱 / 守卫共用，改这里等于改协议）：
- ``PLATFORM = "wechat_kf"``；``account_id = open_kfid``；
- ``chat_key = "wxkf:user:<external_userid>"``（:func:`chat_key_for` / :func:`external_userid_from_chat_key`）。

官方文档（2026-09 核实）：接收消息与事件 /document/path/94699、发送消息 94677、分配会话 96425、
事件响应消息 95122、客服账号 94688。硬规则：用户最后一条消息后 **48h 内最多 5 条**、仅会话状态 0/1
可由 API 发送、``send_msg`` 成功≠送达（失败经 ``msg_send_fail`` 事件回来）、无 typing / 已读回执。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PLATFORM = "wechat_kf"
API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
CHAT_KEY_PREFIX = "wxkf:user:"

#: ``sync_msg`` 的 ``origin``：3 微信客户发 / 4 系统事件 / 5 接待人员在企微客户端发
ORIGIN_CUSTOMER = 3
ORIGIN_SYSTEM = 4
ORIGIN_SERVICER = 5

#: 会话状态（``kf/service_state``）
STATE_UNHANDLED = 0        # 未处理（新接入待处理）——API 可发
STATE_ASSISTANT = 1        # 由智能助手接待——API 可发（智聊接管时保持在此态）
STATE_QUEUED = 2           # 待接入池排队中
STATE_HUMAN = 3            # 由人工接待（企微客户端）——API 不可发
STATE_CLOSED = 4           # 已结束
API_SENDABLE_STATES = frozenset({STATE_UNHANDLED, STATE_ASSISTANT})

#: ``msg_send_fail.fail_type`` → 人话（前端/日志直接用）
FAIL_TYPE_LABELS: Dict[int, str] = {
    0: "未知原因", 1: "客服账号已删除", 2: "应用已关闭",
    4: "会话已过期（超过 48 小时）", 5: "会话已关闭", 6: "超过 5 条限制",
    8: "企业主体未验证", 10: "用户拒收", 11: "企业无成员登录企业微信 App",
    12: "客服组件禁发的消息类型", 13: "安全限制",
}
#: 这些失败＝本轮窗口对该客户已关闭，继续发只会继续失败（守卫据此挂 window_closed）
WINDOW_CLOSING_FAIL_TYPES = frozenset({4, 5, 6, 10})

#: 平台媒体消息类型 → 收件箱 media_type
_MEDIA_TYPES = {"image": "image", "voice": "voice", "video": "video", "file": "file"}

#: 语音格式参数（sync_msg.voice_format）：0=AMR 1=Silk。AMR 有现成解码路径（ffmpeg），默认 0。
VOICE_FORMAT_AMR = 0
VOICE_FORMAT_SILK = 1

#: 单条文本上限（官方 2048 字节；按 UTF-8 保守裁到 ~600 字）
TEXT_MAX_CHARS = 600


# ── 键契约 ─────────────────────────────────────────────────────────────────────

def chat_key_for(external_userid: str) -> str:
    """``external_userid`` → 收件箱 chat_key（幂等：已带前缀原样返回）。"""
    s = str(external_userid or "").strip()
    if not s:
        return ""
    return s if s.startswith(CHAT_KEY_PREFIX) else CHAT_KEY_PREFIX + s


def external_userid_from_chat_key(chat_key: str) -> str:
    """收件箱 chat_key（或裸 external_userid）→ 官方 API 用的裸 ``external_userid``。"""
    s = str(chat_key or "").strip()
    if s.startswith(CHAT_KEY_PREFIX):
        return s[len(CHAT_KEY_PREFIX):]
    # 其它带冒号的前缀形态（防御：上层偶有 "wechat_kf:user:x"）取最后一段
    return s.rsplit(":", 1)[-1] if ":" in s else s


def truncate_text(text: str, limit: int = TEXT_MAX_CHARS) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ── errcode 归类 ───────────────────────────────────────────────────────────────

#: 令牌类错误：刷新 access_token 后重试一次即可
TOKEN_ERRCODES = frozenset({40001, 40014, 41001, 42001})
#: 已知 errcode → error_kind（前端按 kind 取本地化提示；未知一律 api_error）
_ERRCODE_KINDS: Dict[int, str] = {
    40013: "invalid_corpid",
    60020: "ip_not_allowed",
    60011: "no_privilege",
    95014: "servicer_not_active",
    48002: "api_forbidden",
}


def classify_errcode(errcode: Any) -> str:
    try:
        code = int(errcode)
    except (TypeError, ValueError):
        return "api_error"
    if code == 0:
        return ""
    if code in TOKEN_ERRCODES:
        return "token"
    return _ERRCODE_KINDS.get(code, "api_error")


# ── 消息归一（纯函数） ─────────────────────────────────────────────────────────

def _s(v: Any) -> str:
    return str(v if v is not None else "").strip()


def normalize_kf_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """``sync_msg.msg_list[i]`` → 扁平 dict。

    返回键：``kind``（message | event | ignore）、``msgid``、``open_kfid``、``external_userid``、
    ``ts``、``origin``、``msgtype``、``text``、``media_id``、``media_type``、``servicer_userid``、
    ``menu_id``、``event_type``、``event``（原事件对象）。非文字类型给出 ``[位置]`` 等占位正文，
    媒体类型正文为空（占位由收件箱 media_type 承载，不污染 auto-draft）。
    """
    m = msg if isinstance(msg, dict) else {}
    mt = _s(m.get("msgtype")).lower()
    out: Dict[str, Any] = {
        "kind": "message",
        "msgid": _s(m.get("msgid")),
        "open_kfid": _s(m.get("open_kfid")),
        "external_userid": _s(m.get("external_userid")),
        "ts": float(m.get("send_time") or 0) or 0.0,
        "origin": int(m.get("origin") or 0) if str(m.get("origin") or "").isdigit() else 0,
        "servicer_userid": _s(m.get("servicer_userid")),
        "msgtype": mt,
        "text": "",
        "media_id": "",
        "media_type": "",
        "menu_id": "",
        "event_type": "",
        "event": {},
    }
    if mt == "event":
        ev = m.get("event") or {}
        out["kind"] = "event"
        out["event_type"] = _s(ev.get("event_type"))
        out["event"] = dict(ev) if isinstance(ev, dict) else {}
        out["external_userid"] = out["external_userid"] or _s(ev.get("external_userid"))
        out["open_kfid"] = out["open_kfid"] or _s(ev.get("open_kfid"))
        return out
    if mt == "text":
        body = m.get("text") or {}
        out["text"] = _s(body.get("content"))
        out["menu_id"] = _s(body.get("menu_id"))
        return out
    if mt in _MEDIA_TYPES:
        body = m.get(mt) or {}
        out["media_id"] = _s(body.get("media_id"))
        out["media_type"] = _MEDIA_TYPES[mt]
        return out
    if mt == "location":
        body = m.get("location") or {}
        name = _s(body.get("name"))
        addr = _s(body.get("address"))
        lat, lng = body.get("latitude"), body.get("longitude")
        coord = f"({lat},{lng})" if lat is not None and lng is not None else ""
        out["text"] = " ".join(x for x in ("[位置]", name, addr, coord) if x)
        return out
    if mt == "link":
        body = m.get("link") or {}
        out["text"] = " ".join(x for x in ("[链接]", _s(body.get("title")),
                                            _s(body.get("url"))) if x)
        return out
    if mt == "business_card":
        body = m.get("business_card") or {}
        out["text"] = " ".join(x for x in ("[名片]", _s(body.get("userid"))) if x)
        return out
    if mt == "miniprogram":
        body = m.get("miniprogram") or {}
        out["text"] = " ".join(x for x in ("[小程序]", _s(body.get("title")),
                                            _s(body.get("appid"))) if x)
        return out
    if mt == "msgmenu":
        body = m.get("msgmenu") or {}
        head = _s(body.get("head_content"))
        items = body.get("list") or []
        labels = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            for k in ("click", "view", "miniprogram", "text"):
                sub = it.get(k)
                if isinstance(sub, dict):
                    labels.append(_s(sub.get("content") or sub.get("title")))
                    break
        out["text"] = " ".join(x for x in ("[菜单]", head, " / ".join(l for l in labels if l))
                               if x)
        return out
    if mt in ("channels_shop_product", "channels_shop_order", "merged_msg", "channels",
              "note", "voice_text"):
        out["text"] = f"[{mt}]"
        return out
    out["kind"] = "ignore" if not mt else "message"
    if mt:
        out["text"] = f"[{mt}]"
    return out


def parse_sync_response(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str, bool]:
    """``sync_msg`` 响应 → ``(归一消息列表, next_cursor, has_more)``。坏形状一律空。"""
    d = data if isinstance(data, dict) else {}
    raw = d.get("msg_list") or []
    items = [normalize_kf_message(x) for x in raw if isinstance(x, dict)]
    return items, _s(d.get("next_cursor")), bool(d.get("has_more"))


# ── 回调加解密（企微 WXBizMsgCrypt） ─────────────────────────────────────────

class KfCallbackCrypto:
    """企业微信回调加解密（AES-256-CBC）。

    ``encoding_aes_key`` 为后台生成的 43 位串（补 ``=`` 后 base64 解得 32 字节密钥，IV=密钥前 16 字节）。
    ``receive_id`` 校验用 corpid（企业自建应用回调 ToUserName=CorpID）；留空则不校验。
    """

    def __init__(self, token: str, encoding_aes_key: str, receive_id: str = "") -> None:
        self.token = str(token or "")
        self.receive_id = str(receive_id or "")
        key_b64 = str(encoding_aes_key or "").strip()
        if len(key_b64) != 43:
            raise ValueError("EncodingAESKey 必须是 43 位")
        self.key = base64.b64decode(key_b64 + "=")
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey 解码后必须是 32 字节")
        self.iv = self.key[:16]

    # 签名：sha1(sorted([token, timestamp, nonce, encrypt]) 拼接)
    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        parts = sorted([self.token, str(timestamp), str(nonce), str(encrypt)])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def _cipher(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        return Cipher(algorithms.AES(self.key), modes.CBC(self.iv))

    @staticmethod
    def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
        n = block - (len(data) % block)
        if n == 0:
            n = block
        return data + bytes([n]) * n

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        if not data:
            return data
        n = data[-1]
        if n < 1 or n > 32:
            n = 0
        return data[:-n] if n else data

    def decrypt(self, encrypt_b64: str) -> Tuple[bytes, str]:
        """密文 → ``(明文消息字节, receive_id)``。布局：random16 | len(4,big) | msg | receiveid。"""
        raw = base64.b64decode(str(encrypt_b64 or ""))
        dec = self._cipher().decryptor()
        plain = self._pkcs7_unpad(dec.update(raw) + dec.finalize())
        if len(plain) < 20:
            raise ValueError("密文过短")
        msg_len = struct.unpack("!I", plain[16:20])[0]
        msg = plain[20:20 + msg_len]
        rid = plain[20 + msg_len:].decode("utf-8", errors="replace")
        if self.receive_id and rid != self.receive_id:
            raise ValueError("receive_id 不匹配")
        return msg, rid

    def encrypt(self, msg: bytes, receive_id: str = "", random16: Optional[bytes] = None) -> str:
        """明文 → 密文 base64（供单测回环与将来回包用）。"""
        rnd = random16 if random16 is not None else os.urandom(16)
        rid = (receive_id or self.receive_id).encode("utf-8")
        plain = rnd + struct.pack("!I", len(msg)) + msg + rid
        enc = self._cipher().encryptor()
        return base64.b64encode(enc.update(self._pkcs7_pad(plain)) + enc.finalize()).decode("ascii")

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> Optional[str]:
        """GET 校验：签名对上 → 返回解密后的 echostr 明文（原样回包）；否则 None。"""
        try:
            if self.signature(timestamp, nonce, echostr) != str(msg_signature or ""):
                return None
            msg, _rid = self.decrypt(echostr)
            return msg.decode("utf-8")
        except Exception:
            return None

    def decrypt_callback(self, body: bytes, msg_signature: str, timestamp: str,
                         nonce: str) -> Optional[Dict[str, str]]:
        """POST 回调：解 XML 外壳 → 验签 → 解密内层 XML → 扁平 dict（Event/Token/OpenKfId…）。

        任何一步失败返回 None（调用方回 4xx）。
        """
        try:
            outer = ET.fromstring(body)
            encrypt = (outer.findtext("Encrypt") or "").strip()
            if not encrypt:
                return None
            if self.signature(timestamp, nonce, encrypt) != str(msg_signature or ""):
                return None
            plain, _rid = self.decrypt(encrypt)
            inner = ET.fromstring(plain)
            return {child.tag: (child.text or "").strip() for child in inner}
        except Exception:
            logger.debug("[wechat_kf] 回调解密失败", exc_info=True)
            return None


# ── HTTP 客户端 ───────────────────────────────────────────────────────────────

def _local_ip_hint() -> str:
    """出站 IP 提示（可信 IP 白名单排错用；取不到返回空）。"""
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return ""


class WeChatKfClient:
    """企业微信 · 微信客服 API 客户端（aiohttp；永不抛；token 自管）。

    ``session_factory``/``request_fn`` 可注入以便单测：``request_fn(method, url, *, params, json,
    data, headers, timeout) -> (status:int, body:bytes, content_type:str, headers:dict)``。
    """

    TOKEN_TTL_MARGIN = 300.0

    def __init__(
        self,
        corpid: str,
        secret: str,
        *,
        base_url: str = API_BASE,
        timeout_sec: float = 20.0,
        request_fn: Optional[Callable[..., Any]] = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.corpid = str(corpid or "").strip()
        self.secret = str(secret or "").strip()
        self.base_url = str(base_url or API_BASE).rstrip("/")
        self.timeout_sec = float(timeout_sec or 20.0)
        self._request_fn = request_fn
        self._now = now
        self._token = ""
        self._token_expire_at = 0.0
        self.last_error: str = ""

    # ── 底层请求 ──
    async def _http(self, method: str, url: str, *, params: Optional[Dict[str, Any]] = None,
                    json_body: Any = None, data: Any = None,
                    headers: Optional[Dict[str, str]] = None,
                    timeout: Optional[float] = None) -> Tuple[int, bytes, str, Dict[str, str]]:
        if self._request_fn is not None:
            res = self._request_fn(method, url, params=params, json=json_body, data=data,
                                   headers=headers, timeout=timeout or self.timeout_sec)
            if hasattr(res, "__await__"):
                res = await res
            return res  # type: ignore[return-value]
        import aiohttp
        tmo = aiohttp.ClientTimeout(total=timeout or self.timeout_sec)
        async with aiohttp.ClientSession(timeout=tmo) as session:
            async with session.request(method, url, params=params, json=json_body,
                                       data=data, headers=headers) as resp:
                body = await resp.read()
                return (resp.status, body, str(resp.headers.get("Content-Type") or ""),
                        {k: v for k, v in resp.headers.items()})

    @staticmethod
    def _parse_json(body: bytes) -> Dict[str, Any]:
        try:
            d = json.loads(body.decode("utf-8", errors="replace") or "{}")
            return d if isinstance(d, dict) else {"raw": d}
        except Exception:
            return {"raw": body[:300].decode("utf-8", errors="replace")}

    def _fail(self, error: str, *, kind: str = "network", errcode: int = -1,
              data: Any = None) -> Dict[str, Any]:
        self.last_error = error
        return {"ok": False, "errcode": errcode, "errmsg": error, "error_kind": kind,
                "data": data if data is not None else {}}

    async def get_token(self, force: bool = False) -> Dict[str, Any]:
        """取 access_token（缓存到过期前 5 分钟）。返回统一结果，``data.access_token``。"""
        now = self._now()
        if not force and self._token and now < self._token_expire_at:
            return {"ok": True, "errcode": 0, "errmsg": "", "error_kind": "",
                    "data": {"access_token": self._token, "cached": True}}
        if not (self.corpid and self.secret):
            return self._fail("缺少 corpid/secret", kind="creds_missing")
        try:
            status, body, _ct, _h = await self._http(
                "GET", f"{self.base_url}/gettoken",
                params={"corpid": self.corpid, "corpsecret": self.secret})
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"gettoken 网络失败: {exc}")
        d = self._parse_json(body)
        code = int(d.get("errcode") or 0)
        if status != 200 or code != 0 or not d.get("access_token"):
            kind = classify_errcode(code) or "api_error"
            if kind == "token":
                kind = "invalid_secret"
            hint = ""
            if kind == "ip_not_allowed":
                hint = f"（本机出站 IP 可能是 {_local_ip_hint()}，需加入企微应用「可信 IP」）"
            return self._fail(f"gettoken 失败 errcode={code} {d.get('errmsg', '')}{hint}",
                              kind=kind, errcode=code, data=d)
        self._token = str(d["access_token"])
        ttl = float(d.get("expires_in") or 7200)
        self._token_expire_at = now + max(60.0, ttl - self.TOKEN_TTL_MARGIN)
        return {"ok": True, "errcode": 0, "errmsg": "", "error_kind": "",
                "data": {"access_token": self._token, "cached": False}}

    def invalidate_token(self) -> None:
        self._token = ""
        self._token_expire_at = 0.0

    async def api(self, path: str, *, json_body: Any = None,
                  params: Optional[Dict[str, Any]] = None, method: str = "POST",
                  _retry: bool = True) -> Dict[str, Any]:
        """带 access_token 的 JSON 接口调用；令牌失效自动刷新重试一次。"""
        tok = await self.get_token()
        if not tok.get("ok"):
            return tok
        q = dict(params or {})
        q["access_token"] = tok["data"]["access_token"]
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            status, body, _ct, _h = await self._http(
                method, url, params=q,
                json_body=json_body if method.upper() == "POST" else None)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"{path} 网络失败: {exc}")
        d = self._parse_json(body)
        code = int(d.get("errcode") or 0)
        if code in TOKEN_ERRCODES and _retry:
            self.invalidate_token()
            return await self.api(path, json_body=json_body, params=params,
                                  method=method, _retry=False)
        if status != 200 or code != 0:
            return self._fail(f"{path} errcode={code} {d.get('errmsg', '')}",
                              kind=classify_errcode(code) or "api_error",
                              errcode=code, data=d)
        return {"ok": True, "errcode": 0, "errmsg": "", "error_kind": "", "data": d}

    # ── 客服账号 / 链接 / 接待人员 ──
    async def list_accounts(self, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
        return await self.api("kf/account/list",
                              json_body={"offset": int(offset), "limit": int(limit)})

    async def add_contact_way(self, open_kfid: str, scene: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"open_kfid": open_kfid}
        if scene:
            body["scene"] = str(scene)[:64]
        return await self.api("kf/add_contact_way", json_body=body)

    async def list_servicers(self, open_kfid: str) -> Dict[str, Any]:
        return await self.api("kf/servicer/list", params={"open_kfid": open_kfid}, method="GET")

    async def batch_get_customers(self, external_userids: List[str],
                                  need_context: bool = True) -> Dict[str, Any]:
        """``kf/customer/batchget`` → ``data.customer_list[{external_userid, nickname, avatar,
        gender, unionid, enter_session_context{scene, scene_param}}]``（≤100 个/次）。"""
        ids = [str(x) for x in (external_userids or []) if str(x or "").strip()][:100]
        if not ids:
            return {"ok": True, "errcode": 0, "errmsg": "", "error_kind": "",
                    "data": {"customer_list": []}}
        return await self.api("kf/customer/batchget",
                              json_body={"external_userid_list": ids,
                                         "need_enter_session_context": 1 if need_context else 0})

    # ── 收发 ──
    async def sync_msg(self, open_kfid: str, *, cursor: str = "", token: str = "",
                       limit: int = 1000, voice_format: int = VOICE_FORMAT_AMR) -> Dict[str, Any]:
        body: Dict[str, Any] = {"open_kfid": open_kfid, "limit": max(1, min(1000, int(limit))),
                                "voice_format": int(voice_format)}
        if cursor:
            body["cursor"] = cursor
        if token:
            body["token"] = token
        return await self.api("kf/sync_msg", json_body=body)

    async def send_text(self, external_userid: str, open_kfid: str, text: str,
                        *, msgid: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "touser": external_userid_from_chat_key(external_userid), "open_kfid": open_kfid,
            "msgtype": "text", "text": {"content": truncate_text(text)},
        }
        if msgid:
            body["msgid"] = str(msgid)[:32]
        return await self.api("kf/send_msg", json_body=body)

    async def send_media(self, external_userid: str, open_kfid: str, media_type: str,
                         media_id: str) -> Dict[str, Any]:
        mt = str(media_type or "").lower()
        mt = {"document": "file", "photo": "image", "audio": "voice"}.get(mt, mt)
        if mt not in ("image", "voice", "video", "file"):
            return self._fail(f"不支持的媒体类型 {media_type}", kind="not_supported")
        body = {"touser": external_userid_from_chat_key(external_userid), "open_kfid": open_kfid,
                "msgtype": mt, mt: {"media_id": media_id}}
        return await self.api("kf/send_msg", json_body=body)

    async def send_msgmenu(self, external_userid: str, open_kfid: str, head: str,
                           items: List[str], tail: str = "") -> Dict[str, Any]:
        """菜单消息（click 项，客户点选后以 text+menu_id 回来）。"""
        menu = [{"type": "click", "click": {"id": f"m{i + 1}", "content": str(it)[:128]}}
                for i, it in enumerate(items[:10])]
        body: Dict[str, Any] = {
            "touser": external_userid_from_chat_key(external_userid), "open_kfid": open_kfid,
            "msgtype": "msgmenu",
            "msgmenu": {"head_content": truncate_text(head, 1024), "list": menu},
        }
        if tail:
            body["msgmenu"]["tail_content"] = truncate_text(tail, 1024)
        return await self.api("kf/send_msg", json_body=body)

    async def send_msg_on_event(self, code: str, text: str) -> Dict[str, Any]:
        return await self.api("kf/send_msg_on_event",
                              json_body={"code": code, "msgtype": "text",
                                         "text": {"content": truncate_text(text)}})

    # ── 会话状态 ──
    async def get_service_state(self, open_kfid: str, external_userid: str) -> Dict[str, Any]:
        return await self.api("kf/service_state/get",
                              json_body={"open_kfid": open_kfid,
                                         "external_userid": external_userid_from_chat_key(external_userid)})

    async def trans_service_state(self, open_kfid: str, external_userid: str, state: int,
                                  servicer_userid: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"open_kfid": open_kfid,
                                "external_userid": external_userid_from_chat_key(external_userid),
                                "service_state": int(state)}
        if servicer_userid:
            body["servicer_userid"] = servicer_userid
        return await self.api("kf/service_state/trans", json_body=body)

    # ── 媒体 ──
    async def upload_media(self, path: str, media_type: str) -> Dict[str, Any]:
        """``media/upload`` 临时素材（3 天）→ ``data.media_id``。"""
        mt = {"document": "file", "photo": "image", "audio": "voice"}.get(
            str(media_type or "").lower(), str(media_type or "").lower())
        if mt not in ("image", "voice", "video", "file"):
            return self._fail(f"不支持的媒体类型 {media_type}", kind="not_supported")
        tok = await self.get_token()
        if not tok.get("ok"):
            return tok
        try:
            with open(path, "rb") as fh:
                payload = fh.read()
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"读取媒体失败: {exc}", kind="media_missing")
        if self._request_fn is None:
            import aiohttp
            form = aiohttp.FormData()
            form.add_field("media", payload, filename=os.path.basename(path),
                           content_type="application/octet-stream")
            data: Any = form
        else:
            data = {"media": payload, "filename": os.path.basename(path)}
        url = f"{self.base_url}/media/upload"
        try:
            status, body, _ct, _h = await self._http(
                "POST", url, params={"access_token": tok["data"]["access_token"], "type": mt},
                data=data, timeout=max(self.timeout_sec, 60.0))
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"media/upload 网络失败: {exc}")
        d = self._parse_json(body)
        code = int(d.get("errcode") or 0)
        if status != 200 or code != 0 or not d.get("media_id"):
            return self._fail(f"media/upload errcode={code} {d.get('errmsg', '')}",
                              kind=classify_errcode(code) or "api_error", errcode=code, data=d)
        return {"ok": True, "errcode": 0, "errmsg": "", "error_kind": "", "data": d}

    async def download_media(self, media_id: str) -> Dict[str, Any]:
        """``media/get`` → ``data = {bytes, content_type, filename}``；JSON 错误体按失败处理。"""
        tok = await self.get_token()
        if not tok.get("ok"):
            return tok
        url = f"{self.base_url}/media/get"
        try:
            status, body, ct, headers = await self._http(
                "GET", url, params={"access_token": tok["data"]["access_token"],
                                    "media_id": media_id},
                timeout=max(self.timeout_sec, 60.0))
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"media/get 网络失败: {exc}")
        if "json" in (ct or "").lower() or body[:1] == b"{":
            d = self._parse_json(body)
            code = int(d.get("errcode") or -1)
            return self._fail(f"media/get errcode={code} {d.get('errmsg', '')}",
                              kind=classify_errcode(code) or "api_error", errcode=code, data=d)
        if status != 200:
            return self._fail(f"media/get HTTP {status}", kind="api_error")
        filename = ""
        cd = ""
        for k, v in (headers or {}).items():
            if str(k).lower() == "content-disposition":
                cd = str(v)
        if "filename=" in cd:
            filename = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
        return {"ok": True, "errcode": 0, "errmsg": "", "error_kind": "",
                "data": {"bytes": body, "content_type": ct or "", "filename": filename}}


def media_ext_for(content_type: str, media_type: str, filename: str = "") -> str:
    """媒体落盘扩展名：文件名有后缀用之；否则按 Content-Type/类型兜底。"""
    fn = str(filename or "")
    ext = os.path.splitext(fn)[1].lower()
    if ext:
        return ext
    ct = str(content_type or "").lower()
    table = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "audio/amr": ".amr", "audio/silk": ".silk", "video/mp4": ".mp4",
        "application/pdf": ".pdf",
    }
    for k, v in table.items():
        if k in ct:
            return v
    return {"image": ".jpg", "voice": ".amr", "video": ".mp4"}.get(str(media_type or ""), ".bin")


__all__ = [
    "PLATFORM", "API_BASE", "CHAT_KEY_PREFIX",
    "ORIGIN_CUSTOMER", "ORIGIN_SYSTEM", "ORIGIN_SERVICER",
    "STATE_UNHANDLED", "STATE_ASSISTANT", "STATE_QUEUED", "STATE_HUMAN", "STATE_CLOSED",
    "API_SENDABLE_STATES", "FAIL_TYPE_LABELS", "WINDOW_CLOSING_FAIL_TYPES",
    "chat_key_for", "external_userid_from_chat_key", "truncate_text", "classify_errcode",
    "normalize_kf_message", "parse_sync_response", "KfCallbackCrypto", "WeChatKfClient",
    "media_ext_for",
]
