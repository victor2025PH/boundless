# -*- coding: utf-8 -*-
"""ChatX 官网中继门禁：设备注册/鉴权、白名单、WebSocket 转发往返、SSO 302 直回、离线/超时、归属验证文件。

真起 uvicorn（随机端口、后台线程），假设备用 websockets 客户端连上来应答——与生产形态一致。
"""
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
import uvicorn
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("relay")
    os.environ["RELAY_STATE"] = str(tmp / "devices.json")
    os.environ["RELAY_VERIFY_DIR"] = str(tmp / "verify")
    os.environ["RELAY_FORWARD_TIMEOUT"] = "2.0"
    (tmp / "verify").mkdir()
    (tmp / "verify" / "WW_verify_abc123.txt").write_text("abc123", encoding="utf-8")
    import importlib
    import app as relay_app
    importlib.reload(relay_app)
    port = _free_port()
    config = uvicorn.Config(relay_app.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    yield {"http": f"http://127.0.0.1:{port}", "ws": f"ws://127.0.0.1:{port}/ws/device", "mod": relay_app, "tmp": tmp}
    server.should_exit = True
    th.join(timeout=5)


class FakeDevice:
    """假智聊实例：连中继、应答转发请求（把收到的请求记下来，按 handler 回响应）。"""

    def __init__(self, ws_url: str, device_id: str, secret: str, handler=None, register_key: str = ""):
        self.ws_url, self.device_id, self.secret, self.register_key = ws_url, device_id, secret, register_key
        self.handler = handler or (lambda req: (200, {"content-type": "text/plain"}, b"success"))
        self.received = []
        self.auth_reply = None
        self._stop = asyncio.Event()

    async def run(self, ready: asyncio.Event):
        async with websockets.connect(self.ws_url) as ws:
            await ws.send(json.dumps({"device_id": self.device_id, "secret": self.secret,
                                      "register_key": self.register_key, "app": "chengjie", "version": "test"}))
            self.auth_reply = json.loads(await ws.recv())
            ready.set()
            if not self.auth_reply.get("ok"):
                return
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "req":
                    self.received.append(msg)
                    status, headers, body = self.handler(msg)
                    if status is None:
                        continue   # 故意不应答（测超时）
                    await ws.send(json.dumps({"type": "resp", "id": msg["id"], "status": status, "headers": headers,
                                              "body_b64": base64.b64encode(body).decode("ascii")}))

    def stop(self):
        self._stop.set()


def _run_device(dev: FakeDevice):
    """后台线程里跑假设备（自己的事件循环），等到鉴权应答到达再返回。"""
    def _t():
        try:
            asyncio.run(dev.run(asyncio.Event()))
        except Exception:
            pass

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    deadline = time.time() + 5
    while time.time() < deadline and dev.auth_reply is None:
        time.sleep(0.05)
    return th


def test_health_and_verify_file(relay):
    r = httpx.get(relay["http"] + "/healthz")
    assert r.status_code == 200 and r.json()["ok"]
    assert httpx.get(relay["http"] + "/WW_verify_abc123.txt").text == "abc123"
    assert httpx.get(relay["http"] + "/WW_verify_nope.txt").status_code == 404
    assert httpx.get(relay["http"] + "/etc_passwd.txt").status_code == 404


def test_forward_roundtrip_and_allowlist(relay):
    dev = FakeDevice(relay["ws"], "dev-roundtrip-01", "s" * 24,
                     handler=lambda req: (200, {"content-type": "text/plain", "x-secret": "no"}, b"echo:" + base64.b64decode(req["body_b64"] or "")))
    th = _run_device(dev)
    assert dev.auth_reply and dev.auth_reply["ok"] and dev.auth_reply["reason"] == "registered"
    st = httpx.get(relay["http"] + "/api/device/dev-roundtrip-01/status").json()
    assert st["online"] is True and st["meta"]["app"] == "chengjie"
    # 企微回调 POST 原样转发（方法/路径/查询串/正文/部分头）
    r = httpx.post(relay["http"] + "/d/dev-roundtrip-01/wechat/kf/callback?msg_signature=x&timestamp=1&nonce=2",
                   content=b"<xml>hi</xml>", headers={"content-type": "text/xml", "x-evil": "1"})
    assert r.status_code == 200 and r.text == "echo:<xml>hi</xml>" and r.headers["content-type"].startswith("text/plain")
    assert "x-secret" not in r.headers, "响应头只透传白名单"
    req = dev.received[-1]
    assert req["method"] == "POST" and req["path"] == "/wechat/kf/callback" and req["query"] == "msg_signature=x&timestamp=1&nonce=2"
    assert req["headers"].get("content-type") == "text/xml" and "x-evil" not in req["headers"]
    # 白名单外 → 404 且不转发
    n = len(dev.received)
    assert httpx.get(relay["http"] + "/d/dev-roundtrip-01/workspace").status_code == 404
    assert httpx.get(relay["http"] + "/d/dev-roundtrip-01/login").status_code == 404
    assert len(dev.received) == n
    # 探活转发
    assert httpx.get(relay["http"] + "/d/dev-roundtrip-01/api/desktop/ping").status_code == 200
    # 错 secret 再连 → 拒
    bad = FakeDevice(relay["ws"], "dev-roundtrip-01", "wrong" * 5)
    _run_device(bad)
    assert bad.auth_reply and bad.auth_reply["ok"] is False and bad.auth_reply["reason"] == "bad_secret"
    dev.stop()
    th.join(timeout=3)
    # 设备下线 → 503（企微会重试；设备侧还有轮询兜底）
    time.sleep(0.3)
    r = httpx.post(relay["http"] + "/d/dev-roundtrip-01/wechat/kf/callback", content=b"x")
    assert r.status_code == 503 and r.json()["error"] == "device_offline"
    assert httpx.get(relay["http"] + "/d/unknown-device-99/api/desktop/ping").status_code == 404


def test_device_publishes_domain_verify_file(relay):
    """设备经 WebSocket 发布 WW_verify_xxx.txt → 中继根目录可访问；坏名字/空内容拒。"""
    got = []

    class _Dev(FakeDevice):
        async def run(self, ready):
            async with websockets.connect(self.ws_url) as ws:
                await ws.send(json.dumps({"device_id": self.device_id, "secret": self.secret, "app": "chengjie"}))
                self.auth_reply = json.loads(await ws.recv())
                await ws.send(json.dumps({"type": "verify_file", "name": "WW_verify_dev04.txt", "content": "dev04-token"}))
                got.append(json.loads(await ws.recv()))
                await ws.send(json.dumps({"type": "verify_file", "name": "../etc/passwd", "content": "x"}))
                got.append(json.loads(await ws.recv()))
                await ws.send(json.dumps({"type": "verify_file", "name": "WW_verify_empty.txt", "content": ""}))
                got.append(json.loads(await ws.recv()))

    dev = _Dev(relay["ws"], "dev-verify-04", "v" * 24)
    th = _run_device(dev)
    th.join(timeout=5)
    assert [g["type"] for g in got] == ["verify_file_ok", "verify_file_err", "verify_file_err"], got
    assert httpx.get(relay["http"] + "/WW_verify_dev04.txt").text == "dev04-token"
    assert not (relay["tmp"] / "verify" / "passwd").exists()


def test_forward_timeout_when_device_silent(relay):
    dev = FakeDevice(relay["ws"], "dev-silent-02", "t" * 24, handler=lambda req: (None, None, None))
    th = _run_device(dev)
    r = httpx.post(relay["http"] + "/d/dev-silent-02/wechat/kf/callback", content=b"x", timeout=10)
    assert r.status_code == 504 and r.json()["error"] == "device_timeout"
    dev.stop()
    th.join(timeout=3)


def test_sso_callback_redirects_browser_back_to_private_origin(relay):
    from app import origin_from_state, origin_is_private
    origin = "http://192.168.0.149:18899"
    st = "1700.n.sig." + base64.urlsafe_b64encode(origin.encode()).decode().rstrip("=")
    assert origin_from_state(st) == origin and origin_is_private(origin)
    r = httpx.get(relay["http"] + f"/d/dev-any-03/login/wecom/callback?code=CODE&state={st}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"http://192.168.0.149:18899/login/wecom/callback?code=CODE&state={st}"
    # 公网来源不做开放重定向
    pub = "1700.n.sig." + base64.urlsafe_b64encode(b"https://evil.example.com").decode().rstrip("=")
    r = httpx.get(relay["http"] + f"/d/dev-any-03/login/wecom/callback?code=C&state={pub}", follow_redirects=False)
    assert r.status_code == 400
    # 没带来源的旧 state → 走转发；设备未注册 → 404
    r = httpx.get(relay["http"] + "/d/dev-any-03/login/wecom/callback?code=C&state=1700.n.sig", follow_redirects=False)
    assert r.status_code == 404
    assert not origin_is_private("http://8.8.8.8") and origin_is_private("http://localhost:18799") and origin_is_private("http://10.1.2.3")
    assert origin_from_state("bad") == "" and origin_from_state("a.b.c.!!!") == ""


def test_healthz_prunes_stale_offline_devices_and_reports_counts(relay):
    """设备表 24h 清理：离线且 last_seen 早于 STALE_SEC 的登记行被 /healthz 清掉并落盘；离线但未满期的留着并计入 devices_stale。"""
    mod = relay["mod"]
    reg = mod.registry
    now = time.time()
    # 直接注入两行离线设备：一行 25h 没心跳（该清），一行 1h 没心跳（该留、计 stale）
    reg._data["dev-stale-25h-aaa"] = {"secret_sha256": "x" * 64, "created_at": now - 30 * 3600, "last_seen": now - 25 * 3600}
    reg._data["dev-offline-1h-bbb"] = {"secret_sha256": "y" * 64, "created_at": now - 2 * 3600, "last_seen": now - 1 * 3600}
    reg._flush()
    before = reg.pruned_total
    h = httpx.get(relay["http"] + "/healthz").json()
    assert h["ok"] is True and h["stale_sec"] == mod.STALE_SEC == 86400.0
    assert "dev-stale-25h-aaa" in h["pruned_now"] and "dev-offline-1h-bbb" not in h["pruned_now"]
    assert h["devices_pruned_total"] == before + 1
    assert not reg.known("dev-stale-25h-aaa") and reg.known("dev-offline-1h-bbb")
    # devices_stale = 登记但此刻离线（1h 那行 + 前面用例留下的已下线设备），且 known >= stale
    assert h["devices_stale"] >= 1 and h["devices_known"] >= h["devices_stale"]
    assert h["devices_known"] == len(reg)
    # 清理已落盘：state 文件里没有 25h 那行
    disk = json.loads(Path(os.environ["RELAY_STATE"]).read_text(encoding="utf-8"))
    assert "dev-stale-25h-aaa" not in disk and "dev-offline-1h-bbb" in disk
    # 再打一次：无新清理、计数不变
    h2 = httpx.get(relay["http"] + "/healthz").json()
    assert h2["pruned_now"] == [] and h2["devices_pruned_total"] == before + 1


def test_online_device_never_pruned_and_ping_refreshes_last_seen(relay):
    """在线设备哪怕 last_seen 被做旧到 3 天前也不清；ping 心跳把 last_seen 刷回当前；断线时刻落盘为清理计时起点。"""
    mod = relay["mod"]
    reg = mod.registry
    pinged = []

    class _PingDev(FakeDevice):
        async def run(self, ready):
            async with websockets.connect(self.ws_url) as ws:
                await ws.send(json.dumps({"device_id": self.device_id, "secret": self.secret, "app": "chengjie"}))
                self.auth_reply = json.loads(await ws.recv())
                # 做旧 last_seen（3 天前），然后 healthz：在线 → 不清、不计 stale
                reg._data[self.device_id]["last_seen"] = time.time() - 3 * 86400
                h = httpx.get(relay["http"] + "/healthz").json()
                assert self.device_id not in h["pruned_now"] and reg.known(self.device_id)
                assert h["devices_online"] >= 1
                await ws.send(json.dumps({"type": "ping"}))
                pinged.append(json.loads(await ws.recv()))
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)

    dev = _PingDev(relay["ws"], "dev-online-ping-05", "p" * 24)
    th = _run_device(dev)
    deadline = time.time() + 5
    while time.time() < deadline and not pinged:
        time.sleep(0.05)
    assert pinged and pinged[0]["type"] == "pong"
    assert time.time() - reg._data["dev-online-ping-05"]["last_seen"] < 5, "ping 应把 last_seen 刷到当前"
    dev.stop()
    th.join(timeout=3)
    time.sleep(0.3)
    # 断线：last_seen 立即落盘（24h 计时起点），行仍在（离线未满期）→ 计入 devices_stale，不清
    disk = json.loads(Path(os.environ["RELAY_STATE"]).read_text(encoding="utf-8"))
    assert "dev-online-ping-05" in disk and time.time() - disk["dev-online-ping-05"]["last_seen"] < 5
    h = httpx.get(relay["http"] + "/healthz").json()
    assert "dev-online-ping-05" not in h["pruned_now"] and reg.known("dev-online-ping-05")
