# -*- coding: utf-8 -*-
"""官网中继设备端（实施97 · 中继）：智聊实例主动出站连 ``wss://relay.bd2026.cc/ws/device``，把中继转来的
公网回调（企微 ``/wechat/kf/callback``、成员登录 ``/login/wecom/callback``、官方 webhook、探活）在本机回环上
重放给自己的 HTTP 服务，再把响应送回中继。

- 只服务白名单路径（与中继同一份口径，双保险）；
- 断线指数退避重连（2s → 60s），每 25s 应用层 ping；
- ``public_base`` = ``https://relay.bd2026.cc/d/<device_id>``——企微后台「接收消息服务器 URL」/ 登录 redirect_uri 都填它下面的路径；
- ``device_id``/``secret`` 首次生成后写回 overlay（``relay.device_id/secret``），重启不变；
- ``fetch`` 可注入（单测不起 HTTP 服务）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

DEFAULT_RELAY_URL = "wss://relay.bd2026.cc"
ALLOW_PREFIXES: Tuple[str, ...] = (
    "wechat/kf/callback", "login/wecom/callback", "api/desktop/ping",
    "webhook/", "fb/webhook", "line/webhook", "ig/webhook", "zalo/webhook", "qq/webhook",
)
FetchFn = Callable[[str, str, Dict[str, str], bytes], Awaitable[Tuple[int, Dict[str, str], bytes]]]


def relay_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    blk = (config or {}).get("relay") if isinstance((config or {}).get("relay"), dict) else {}
    url = str(blk.get("url") or DEFAULT_RELAY_URL).strip().rstrip("/")
    return {
        "enabled": bool(blk.get("enabled", False)),
        "url": url,
        "device_id": str(blk.get("device_id") or "").strip(),
        "secret": str(blk.get("secret") or "").strip(),
        "register_key": str(blk.get("register_key") or "").strip(),
        "ping_sec": float(blk.get("ping_sec") or 25.0),
    }


def public_base_for(url: str, device_id: str) -> str:
    """``wss://relay.bd2026.cc`` + device → ``https://relay.bd2026.cc/d/<id>``（ws→http、wss→https）。"""
    if not url or not device_id:
        return ""
    u = urlsplit(url)
    scheme = {"wss": "https", "ws": "http"}.get(u.scheme, u.scheme or "https")
    return urlunsplit((scheme, u.netloc, "", "", "")).rstrip("/") + f"/d/{device_id}"


def ws_endpoint(url: str) -> str:
    return url.rstrip("/") + "/ws/device"


def path_allowed(path: str) -> bool:
    p = str(path or "").lstrip("/")
    for a in ALLOW_PREFIXES:
        if a.endswith("/"):
            if p.startswith(a):
                return True
        elif p == a:
            return True
    return False


def new_identity() -> Tuple[str, str]:
    return "dev-" + secrets.token_hex(8), secrets.token_urlsafe(32)


async def _default_fetch(method: str, url: str, headers: Dict[str, str], body: bytes) -> Tuple[int, Dict[str, str], bytes]:
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.request(method, url, headers=headers, data=body or None, allow_redirects=False) as r:
            data = await r.read()
            return r.status, {k: v for k, v in r.headers.items()}, data


class RelayClient:
    def __init__(self, *, relay_url: str, device_id: str, secret: str, local_base: str,
                 register_key: str = "", fetch: Optional[FetchFn] = None, ping_sec: float = 25.0,
                 app_version: str = "") -> None:
        self.relay_url = str(relay_url or DEFAULT_RELAY_URL).rstrip("/")
        self.device_id = device_id
        self.secret = secret
        self.local_base = str(local_base or "http://127.0.0.1:18799").rstrip("/")
        self.register_key = register_key
        self._fetch: FetchFn = fetch or _default_fetch
        self.ping_sec = max(5.0, float(ping_sec or 25.0))
        self.app_version = app_version
        self.connected = False
        self.last_error = ""
        self.last_connected_at = 0.0
        self.served = 0
        self.rejected = 0
        self._stop = False
        self._ws = None

    @property
    def public_base(self) -> str:
        return public_base_for(self.relay_url, self.device_id)

    def status(self) -> Dict[str, Any]:
        return {"enabled": True, "connected": self.connected, "public_base": self.public_base,
                "device_id": self.device_id, "last_error": self.last_error,
                "last_connected_at": self.last_connected_at, "served": self.served, "rejected": self.rejected}

    async def handle_request(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """一条中继转发 → 本机回环重放 → 响应消息。白名单外直接 404（不打本机）。"""
        path = str(msg.get("path") or "/")
        if not path_allowed(path):
            self.rejected += 1
            return {"type": "resp", "id": msg.get("id"), "status": 404,
                    "headers": {"content-type": "application/json"},
                    "body_b64": base64.b64encode(b'{"error":"path_not_allowed"}').decode("ascii")}
        url = self.local_base + path + (f"?{msg['query']}" if msg.get("query") else "")
        headers = {str(k): str(v) for k, v in (msg.get("headers") or {}).items()}
        headers["X-Relay-Device"] = self.device_id
        try:
            body = base64.b64decode(str(msg.get("body_b64") or "")) if msg.get("body_b64") else b""
        except Exception:
            body = b""
        try:
            status, rheaders, rbody = await self._fetch(str(msg.get("method") or "GET").upper(), url, headers, body)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[relay] 本机重放失败 %s", exc, exc_info=True)
            status, rheaders, rbody = 502, {"content-type": "text/plain"}, b"local_replay_failed"
        self.served += 1
        keep = {k: v for k, v in (rheaders or {}).items() if k.lower() in ("content-type", "location", "cache-control")}
        return {"type": "resp", "id": msg.get("id"), "status": int(status), "headers": keep,
                "body_b64": base64.b64encode(rbody or b"").decode("ascii")}

    async def _session(self) -> None:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.ws_connect(ws_endpoint(self.relay_url), heartbeat=self.ping_sec, max_msg_size=2 * 1024 * 1024) as ws:
                await ws.send_str(json.dumps({"device_id": self.device_id, "secret": self.secret,
                                              "register_key": self.register_key, "app": "chengjie",
                                              "version": self.app_version}))
                first = await asyncio.wait_for(ws.receive(), timeout=15)
                auth = json.loads(first.data) if first.type == aiohttp.WSMsgType.TEXT else {}
                if not auth.get("ok"):
                    self.last_error = f"auth_rejected:{auth.get('reason', 'unknown')}"
                    raise PermissionError(self.last_error)
                self.connected = True
                self._ws = ws
                self.last_connected_at = time.time()
                self.last_error = ""
                logger.info("[relay] 已连接 %s，公网前缀 %s", self.relay_url, self.public_base)
                async for m in ws:
                    if m.type != aiohttp.WSMsgType.TEXT:
                        if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    try:
                        msg = json.loads(m.data)
                    except Exception:
                        continue
                    if msg.get("type") == "req":
                        resp = await self.handle_request(msg)
                        await ws.send_str(json.dumps(resp, ensure_ascii=False))

    async def run_forever(self) -> None:
        backoff = 2.0
        while not self._stop:
            try:
                await self._session()
                backoff = 2.0
            except PermissionError:
                backoff = min(300.0, backoff * 2)   # 鉴权被拒：慢慢重试，等运营改配置
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)[:200]
                logger.debug("[relay] 会话结束：%s", exc)
            finally:
                self.connected = False
                self._ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 1.7)

    async def publish_verify_file(self, name: str, content: str) -> bool:
        """把企微「可信域名」归属验证文件发布到中继根目录（https://relay.bd2026.cc/WW_verify_xxx.txt）。
        只发送不等回执（中继落盘失败会回 verify_file_err，由日志可见）；未连接 → False。"""
        ws = self._ws
        if ws is None or not self.connected:
            return False
        try:
            await ws.send_str(json.dumps({"type": "verify_file", "name": str(name), "content": str(content)}))
            return True
        except Exception:
            logger.debug("[relay] 发布验证文件失败", exc_info=True)
            return False

    def stop(self) -> None:
        self._stop = True


def ensure_identity(config_manager: Any) -> Tuple[str, str]:
    """读 ``relay.device_id/secret``；没有就生成并写回 overlay（重启不变）。"""
    cfg = dict(getattr(config_manager, "config", None) or {})
    rc = relay_config(cfg)
    if rc["device_id"] and rc["secret"]:
        return rc["device_id"], rc["secret"]
    device_id, secret = new_identity()
    try:
        fn = getattr(config_manager, "save_overlay_patch", None)
        if callable(fn):
            fn({"relay": {"device_id": device_id, "secret": secret}})
    except Exception:
        logger.debug("[relay] 写回设备身份失败（本次进程内有效）", exc_info=True)
    return device_id, secret


__all__ = ["RelayClient", "relay_config", "public_base_for", "ws_endpoint", "path_allowed", "new_identity",
           "ensure_identity", "DEFAULT_RELAY_URL", "ALLOW_PREFIXES"]
