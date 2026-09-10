# -*- coding: utf-8 -*-
"""ChatX 官网中继（relay.bd2026.cc）——把公网 HTTPS 回调带到 NAT 后的智聊实例。

为什么需要：企业微信（微信客服回调 / 成员扫码登录）只接受 **HTTPS 可信域名**；客户的智聊实例跑在自己电脑上
（127.0.0.1 / 局域网），拿不到域名和证书。中继给每台设备一个公网前缀：

    https://relay.bd2026.cc/d/<device_id>/wechat/kf/callback     ← 企微「接收消息服务器 URL」
    https://relay.bd2026.cc/d/<device_id>/login/wecom/callback    ← 企业微信登录 redirect_uri

工作方式：
- 设备（智聊实例）**主动出站**连 ``wss://relay.bd2026.cc/ws/device``，带 device_id + secret（首次即注册，TOFU；
  可选 ``RELAY_REGISTER_KEY`` 限制谁能注册）。连接保持，断了重连。
- 公网请求到 ``/d/<device_id>/<path>``：
  * 成员登录回跳（``login/wecom/callback``）且 state 里带了**签名过的浏览器来源**（私网/回环地址）→ 直接 302 把浏览器
    送回本地实例——浏览器本来就和实例在同一台机/同一局域网，根本不需要过隧道；
  * 其它允许的路径（企微回调、官方 webhook、ping）→ 打包成 JSON 经 WebSocket 转给设备，等设备回 HTTP 响应（≤8s）。
    设备不在线 → 503（企微会重试；设备侧还有轮询兜底）。
- 只转**白名单路径**（回调/webhook/ping），不是通用反向代理——不把工作台整个暴露到公网。
- ``/WW_verify_*.txt`` / ``/MP_verify_*.txt``：企微「可信域名」归属验证文件，从 ``RELAY_VERIFY_DIR`` 提供。

无数据库：设备表是一个 JSON 文件（``RELAY_STATE``），只存 secret 的 sha256 与时间戳。
运行：``uvicorn app:app --host 127.0.0.1 --port 18790``（nginx 在 443 终止 TLS 并透传 WebSocket）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

logger = logging.getLogger("chatx_relay")

STATE_PATH = Path(os.environ.get("RELAY_STATE") or "relay_devices.json")
VERIFY_DIR = Path(os.environ.get("RELAY_VERIFY_DIR") or "verify")
REGISTER_KEY = str(os.environ.get("RELAY_REGISTER_KEY") or "").strip()
FORWARD_TIMEOUT_SEC = float(os.environ.get("RELAY_FORWARD_TIMEOUT") or 8.0)
MAX_BODY = 512 * 1024

#: 只转这些前缀（回调 / webhook / 探活），不做通用反代
ALLOW_PREFIXES: Tuple[str, ...] = (
    "wechat/kf/callback", "login/wecom/callback", "api/desktop/ping",
    "webhook/", "fb/webhook", "line/webhook", "ig/webhook", "zalo/webhook", "qq/webhook",
)
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_VERIFY_RE = re.compile(r"^(WW|MP)_verify_[A-Za-z0-9]{4,64}\.txt$")
_FWD_REQ_HEADERS = ("content-type", "user-agent", "x-forwarded-for", "x-real-ip", "accept")
_FWD_RESP_HEADERS = ("content-type", "location", "cache-control")

app = FastAPI(title="ChatX Relay", docs_url=None, redoc_url=None, openapi_url=None)


def _sha(s: str) -> str:
    return hashlib.sha256(str(s or "").encode("utf-8")).hexdigest()


class DeviceRegistry:
    """设备表：``{device_id: {secret_sha256, created_at, last_seen}}``，JSON 落盘，锁内读写。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = d if isinstance(d, dict) else {}
        except Exception:
            self._data = {}

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            logger.debug("relay state flush failed", exc_info=True)

    async def authenticate(self, device_id: str, secret: str, register_key: str) -> Tuple[bool, str]:
        """已注册 → 校 secret；未注册 → 若开了注册钥则校钥，然后登记（首次即信任）。返回 (ok, reason)。"""
        if not _DEVICE_ID_RE.match(device_id or "") or len(secret or "") < 16:
            return False, "bad_identity"
        async with self._lock:
            row = self._data.get(device_id)
            if row is None:
                if REGISTER_KEY and not secrets.compare_digest(register_key or "", REGISTER_KEY):
                    return False, "register_key_required"
                self._data[device_id] = {"secret_sha256": _sha(secret), "created_at": time.time(), "last_seen": time.time()}
                self._flush()
                return True, "registered"
            if not secrets.compare_digest(row.get("secret_sha256", ""), _sha(secret)):
                return False, "bad_secret"
            row["last_seen"] = time.time()
            self._flush()
            return True, "ok"

    def known(self, device_id: str) -> bool:
        return device_id in self._data


class Hub:
    """在线设备 ↔ WebSocket；每个转发请求一个 Future。"""

    def __init__(self) -> None:
        self.sockets: Dict[str, WebSocket] = {}
        self.pending: Dict[str, Dict[str, asyncio.Future]] = {}
        self.meta: Dict[str, Dict[str, Any]] = {}

    def online(self, device_id: str) -> bool:
        return device_id in self.sockets

    async def attach(self, device_id: str, ws: WebSocket, info: Dict[str, Any]) -> None:
        old = self.sockets.get(device_id)
        if old is not None and old is not ws:
            try:
                await old.close(code=4001)
            except Exception:
                pass
        self.sockets[device_id] = ws
        self.pending.setdefault(device_id, {})
        self.meta[device_id] = {"connected_at": time.time(), **{k: str(v)[:80] for k, v in (info or {}).items()}}

    def detach(self, device_id: str, ws: WebSocket) -> None:
        if self.sockets.get(device_id) is ws:
            self.sockets.pop(device_id, None)
            self.meta.pop(device_id, None)
            for fut in self.pending.pop(device_id, {}).values():
                if not fut.done():
                    fut.set_exception(ConnectionError("device_disconnected"))

    async def forward(self, device_id: str, msg: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        ws = self.sockets.get(device_id)
        if ws is None:
            raise ConnectionError("device_offline")
        req_id = secrets.token_hex(8)
        msg = {**msg, "type": "req", "id": req_id}
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending.setdefault(device_id, {})[req_id] = fut
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.get(device_id, {}).pop(req_id, None)

    def resolve(self, device_id: str, msg: Dict[str, Any]) -> None:
        fut = self.pending.get(device_id, {}).get(str(msg.get("id") or ""))
        if fut is not None and not fut.done():
            fut.set_result(msg)


registry = DeviceRegistry(STATE_PATH)
hub = Hub()


def path_allowed(path: str) -> bool:
    """白名单：以 ``/`` 结尾的项按前缀匹配，其它精确匹配（query 不在 path 里）。"""
    p = str(path or "").lstrip("/")
    for a in ALLOW_PREFIXES:
        if a.endswith("/"):
            if p.startswith(a):
                return True
        elif p == a:
            return True
    return False


def origin_from_state(state: str) -> str:
    """企业微信登录 state 第 4 段＝base64url(浏览器来源)（由设备签名）；没有 → 空串。"""
    parts = str(state or "").split(".")
    if len(parts) != 4 or not parts[3]:
        return ""
    try:
        pad = "=" * (-len(parts[3]) % 4)
        return base64.urlsafe_b64decode(parts[3] + pad).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""


def origin_is_private(origin: str) -> bool:
    """只允许把浏览器送回私网/回环地址（客户机器 / 局域网），不做公网开放重定向。"""
    try:
        u = urlsplit(origin)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        host = u.hostname.lower()
        if host in ("localhost",) or host.endswith(".local") or host.endswith(".lan"):
            return True
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


@app.get("/healthz")
async def healthz():
    return {"ok": True, "devices_online": len(hub.sockets), "ts": time.time()}


@app.get("/api/device/{device_id}/status")
async def device_status(device_id: str):
    if not registry.known(device_id):
        return JSONResponse({"ok": False, "known": False, "online": False}, status_code=404)
    return {"ok": True, "known": True, "online": hub.online(device_id), "meta": hub.meta.get(device_id, {})}


@app.get("/{name}")
async def verify_file(name: str):
    """企微可信域名归属验证文件（WW_verify_xxx.txt / MP_verify_xxx.txt）。"""
    if not _VERIFY_RE.match(name):
        return PlainTextResponse("not found", status_code=404)
    p = VERIFY_DIR / name
    if not p.is_file():
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))


@app.websocket("/ws/device")
async def ws_device(ws: WebSocket):
    await ws.accept()
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        auth = json.loads(raw)
    except Exception:
        await ws.close(code=4400)
        return
    device_id = str((auth or {}).get("device_id") or "")
    ok, why = await registry.authenticate(device_id, str((auth or {}).get("secret") or ""),
                                          str((auth or {}).get("register_key") or ""))
    if not ok:
        await ws.send_text(json.dumps({"type": "auth", "ok": False, "reason": why}))
        await ws.close(code=4403)
        return
    await hub.attach(device_id, ws, {"app": (auth or {}).get("app"), "version": (auth or {}).get("version")})
    await ws.send_text(json.dumps({"type": "auth", "ok": True, "reason": why, "device_id": device_id,
                                   "public_base": f"/d/{device_id}"}))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = str((msg or {}).get("type") or "")
            if t == "resp":
                hub.resolve(device_id, msg)
            elif t == "ping":
                await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
            elif t == "verify_file":
                # 设备发布企微「可信域名」归属验证文件（每个企业各自一份，文件名带企业哈希，不会互撞）
                name = str(msg.get("name") or "")
                content = str(msg.get("content") or "").strip()
                ok = bool(_VERIFY_RE.match(name)) and 0 < len(content) <= 256
                if ok:
                    try:
                        VERIFY_DIR.mkdir(parents=True, exist_ok=True)
                        (VERIFY_DIR / name).write_text(content, encoding="utf-8")
                    except Exception:
                        ok = False
                await ws.send_text(json.dumps({"type": "verify_file_ok" if ok else "verify_file_err", "name": name}))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("ws loop error", exc_info=True)
    finally:
        hub.detach(device_id, ws)


@app.api_route("/d/{device_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
async def forward(device_id: str, path: str, request: Request):
    if not _DEVICE_ID_RE.match(device_id):
        return JSONResponse({"error": "bad_device_id"}, status_code=400)
    if not path_allowed(path):
        return JSONResponse({"error": "path_not_allowed"}, status_code=404)
    # 成员登录回跳：浏览器与本地实例同机/同网 → 直接送回去，不过隧道
    if path == "login/wecom/callback":
        origin = origin_from_state(request.query_params.get("state", ""))
        if origin:
            if not origin_is_private(origin):
                return JSONResponse({"error": "origin_not_private"}, status_code=400)
            q = request.url.query
            return RedirectResponse(f"{origin.rstrip('/')}/login/wecom/callback" + (f"?{q}" if q else ""), status_code=302)
    if not registry.known(device_id):
        return JSONResponse({"error": "unknown_device"}, status_code=404)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"error": "body_too_large"}, status_code=413)
    msg = {
        "method": request.method, "path": "/" + path, "query": request.url.query,
        "headers": {k: v for k, v in request.headers.items() if k.lower() in _FWD_REQ_HEADERS},
        "body_b64": base64.b64encode(body).decode("ascii") if body else "",
    }
    try:
        resp = await hub.forward(device_id, msg, FORWARD_TIMEOUT_SEC)
    except ConnectionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "device_timeout"}, status_code=504)
    try:
        content = base64.b64decode(str(resp.get("body_b64") or "")) if resp.get("body_b64") else b""
    except (binascii.Error, ValueError):
        content = b""
    headers = {k: str(v) for k, v in (resp.get("headers") or {}).items() if str(k).lower() in _FWD_RESP_HEADERS}
    return Response(content=content, status_code=int(resp.get("status") or 502), headers=headers)


__all__ = ["app", "registry", "hub", "path_allowed", "origin_from_state", "origin_is_private"]
