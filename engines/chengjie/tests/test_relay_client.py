# -*- coding: utf-8 -*-
"""官网中继设备端（实施97）——纯逻辑 + 与真中继服务（relay/app.py 起 uvicorn）的往返。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from src.integrations import relay_client as RC
from src.integrations import wecom_sso as W

RELAY_DIR = Path(__file__).resolve().parents[3] / "relay"


def test_relay_config_and_public_base():
    rc = RC.relay_config({"relay": {"enabled": True, "url": "wss://relay.bd2026.cc/", "device_id": "dev-a", "secret": "s"}})
    assert rc["enabled"] and rc["url"] == "wss://relay.bd2026.cc" and rc["device_id"] == "dev-a"
    assert RC.public_base_for("wss://relay.bd2026.cc", "dev-a") == "https://relay.bd2026.cc/d/dev-a"
    assert RC.public_base_for("ws://127.0.0.1:18790", "dev-a") == "http://127.0.0.1:18790/d/dev-a"
    assert RC.public_base_for("", "dev-a") == "" and RC.ws_endpoint("wss://relay.bd2026.cc") == "wss://relay.bd2026.cc/ws/device"
    assert RC.relay_config({})["enabled"] is False and RC.relay_config({})["url"] == RC.DEFAULT_RELAY_URL
    did, sec = RC.new_identity()
    assert did.startswith("dev-") and len(did) == 20 and len(sec) >= 32


def test_path_allowlist_matches_relay_server():
    sys.path.insert(0, str(RELAY_DIR))
    import app as relay_app  # noqa: E402
    assert tuple(relay_app.ALLOW_PREFIXES) == tuple(RC.ALLOW_PREFIXES), "设备端与中继端白名单必须同一份口径"
    for p in ("/wechat/kf/callback", "login/wecom/callback", "/api/desktop/ping", "/webhook/x/y", "/fb/webhook"):
        assert RC.path_allowed(p) and relay_app.path_allowed(p.lstrip("/"))
    for p in ("/workspace", "/login", "/api/unified-inbox/chats", "/wechat/kf/callback2", ""):
        assert not RC.path_allowed(p) and not relay_app.path_allowed(p.lstrip("/"))


def test_handle_request_replays_on_loopback_and_rejects_off_list():
    calls = []

    async def fetch(method, url, headers, body):
        calls.append((method, url, headers, body))
        return 200, {"content-type": "text/plain", "set-cookie": "no"}, b"success"

    c = RC.RelayClient(relay_url="wss://relay.bd2026.cc", device_id="dev-a", secret="x" * 32,
                       local_base="http://127.0.0.1:18799/", fetch=fetch)
    resp = asyncio.run(c.handle_request({"id": "r1", "method": "POST", "path": "/wechat/kf/callback",
                                         "query": "msg_signature=s&nonce=n", "headers": {"content-type": "text/xml"},
                                         "body_b64": base64.b64encode(b"<xml/>").decode()}))
    assert resp["status"] == 200 and base64.b64decode(resp["body_b64"]) == b"success"
    assert resp["headers"] == {"content-type": "text/plain"}, "只回白名单响应头（不带 set-cookie）"
    m, url, headers, body = calls[0]
    assert m == "POST" and url == "http://127.0.0.1:18799/wechat/kf/callback?msg_signature=s&nonce=n" and body == b"<xml/>"
    assert headers["content-type"] == "text/xml" and headers["X-Relay-Device"] == "dev-a"
    bad = asyncio.run(c.handle_request({"id": "r2", "method": "GET", "path": "/workspace"}))
    assert bad["status"] == 404 and len(calls) == 1 and c.rejected == 1 and c.served == 1
    assert c.public_base == "https://relay.bd2026.cc/d/dev-a" and c.status()["connected"] is False


def test_ensure_identity_persists_to_overlay():
    class _CM:
        def __init__(self):
            self.config = {"relay": {"enabled": True}}
            self.patches = []

        def save_overlay_patch(self, patch):
            self.patches.append(patch)
            self.config.setdefault("relay", {}).update(patch["relay"])
            return True
    cm = _CM()
    did, sec = RC.ensure_identity(cm)
    assert did.startswith("dev-") and cm.patches and cm.patches[0]["relay"]["device_id"] == did
    assert RC.ensure_identity(cm) == (did, sec), "第二次读回同一身份，不再生成"


def test_state_with_origin_roundtrip():
    st = W.sign_state("k", now=1000.0, nonce="n", origin="http://192.168.0.149:18899")
    assert st.count(".") == 3 and W.verify_state("k", st, now=1100.0) and W.state_origin(st) == "http://192.168.0.149:18899"
    # 改来源就是改签名内容 → 拒
    parts = st.split(".")
    parts[3] = W._b64u("http://evil.example.com")
    assert not W.verify_state("k", ".".join(parts), now=1100.0)
    # 旧 3 段 state 仍然有效（兼容）
    old = W.sign_state("k", now=1000.0, nonce="n")
    assert old.count(".") == 2 and W.verify_state("k", old, now=1100.0) and W.state_origin(old) == ""
    assert W.redirect_uri_for("", "http://127.0.0.1:18898", "https://relay.bd2026.cc/d/dev-a") == "https://relay.bd2026.cc/d/dev-a/login/wecom/callback"
    assert W.redirect_uri_for("https://katie.bd2026.cc", "http://x", "https://relay/d/a") == "https://katie.bd2026.cc/login/wecom/callback"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def live_relay(tmp_path):
    """真起中继（relay/app.py）：随机端口 + 独立状态目录。"""
    import importlib
    import uvicorn
    os.environ["RELAY_STATE"] = str(tmp_path / "devices.json")
    os.environ["RELAY_VERIFY_DIR"] = str(tmp_path / "verify")
    os.environ["RELAY_FORWARD_TIMEOUT"] = "3.0"
    sys.path.insert(0, str(RELAY_DIR))
    import app as relay_app
    importlib.reload(relay_app)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(relay_app.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    yield {"http": f"http://127.0.0.1:{port}", "ws": f"ws://127.0.0.1:{port}", "port": port}
    server.should_exit = True
    th.join(timeout=5)


def test_client_roundtrip_against_live_relay(live_relay):
    """设备端连上真中继 → 公网侧 POST 回调 → 经 WebSocket 到设备端 → 本机重放（注入 fetch）→ 响应回到公网侧。"""
    seen = []

    async def fetch(method, url, headers, body):
        seen.append((method, url, body))
        return 200, {"content-type": "application/json"}, json.dumps({"ok": True, "app": "chengjie", "echo": body.decode()}).encode()

    c = RC.RelayClient(relay_url=live_relay["ws"], device_id="dev-live-01", secret="p" * 32,
                       local_base="http://127.0.0.1:18898", fetch=fetch, ping_sec=5)
    th = threading.Thread(target=lambda: asyncio.run(c.run_forever()), daemon=True)
    th.start()
    deadline = time.time() + 8
    while time.time() < deadline and not c.connected:
        time.sleep(0.05)
    assert c.connected, c.last_error
    assert c.public_base == f"http://127.0.0.1:{live_relay['port']}/d/dev-live-01"
    st = httpx.get(live_relay["http"] + "/api/device/dev-live-01/status").json()
    assert st["online"] is True and st["meta"]["app"] == "chengjie"
    r = httpx.post(c.public_base + "/wechat/kf/callback?nonce=1", content=b"<xml>k</xml>", timeout=10)
    assert r.status_code == 200 and r.json()["echo"] == "<xml>k</xml>"
    assert seen[0][0] == "POST" and seen[0][1] == "http://127.0.0.1:18898/wechat/kf/callback?nonce=1"
    r = httpx.get(c.public_base + "/api/desktop/ping", timeout=10)
    assert r.status_code == 200 and r.json()["app"] == "chengjie"
    assert httpx.get(c.public_base + "/workspace", timeout=10).status_code == 404, "中继侧白名单先拦"
    c.stop()
