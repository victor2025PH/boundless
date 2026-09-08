"""QQ 双轨（qqbot 官方机器人 / qq 个人号协议登录）端到端可用性 + 与其他平台对齐门禁（2026-09-07）。

不打真实 QQ：本进程起 aiohttp **假服务**——
- 假 Milky 协议端（``POST /api/{api}`` + ``GET /event`` WS + 媒体文件）；
- 假 QQ 开放平台（token / gateway / WS 网关 Hello→Identify→READY→Dispatch / 消息收发 / 撤回）。

让**真实**的 worker、编排器、``protocol_bridge`` sink、``InboxStore`` 跑完整链路，覆盖其他平台
（TG/WA/LINE/Messenger/IG/Zalo）在系统里接的每个接口面：
入站落库（私聊/群/媒体落盘）→ 编排器 send / send_media / mark_read / delete_messages →
出站镜像 → 健康态 / 会话健康 → 登录 provider（真 HTTP 探测）→ readiness / 诊断 → 设置向导卡。

第二部分是「对齐」：把散在各处的平台表逐一读出来，断言 qq / qqbot 和参照平台在同一张表里。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from aiohttp import web

import src.integrations.protocol_bridge as pb
from src.inbox.store import InboxStore

ROOT = Path(__file__).resolve().parents[1]
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


async def _wait(pred, *, timeout: float = 8.0, step: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return bool(pred())


def _wire_inbox(monkeypatch, store: InboxStore) -> List[Dict[str, Any]]:
    """与 ``unified_inbox_account_routes._register_protocol_sink`` 同一接线：sink→ingest_incoming。"""
    seen: List[Dict[str, Any]] = []

    def _sink(m: Dict[str, Any]) -> None:
        seen.append(dict(m))
        pb.ingest_incoming(store, **m)

    monkeypatch.setattr(pb, "_sink", _sink, raising=False)
    monkeypatch.setattr(pb, "_inbox_store_getter", lambda: store, raising=False)
    return seen


def _managed(orch, worker, platform: str, account_id: str, mode: str):
    import src.integrations.account_orchestrator as ao
    m = ao._Managed(key=ao.account_key(platform, account_id), platform=platform,
                    account_id=account_id, mode=mode, worker=worker, state="running")
    orch._managed[m.key] = m
    return m


# ═════════════════════════════ 假 Milky 协议端 ═════════════════════════════

class FakeMilky:
    def __init__(self, *, token: str = "tok", uin: int = 10001, nickname: str = "测试号") -> None:
        self.token, self.uin, self.nickname = token, uin, nickname
        self.logged_in = True
        self.calls: List[Dict[str, Any]] = []
        self.pending: List[Dict[str, Any]] = []
        self.ws_clients: List[web.WebSocketResponse] = []
        self.seq = 5000
        self.media_hits = 0
        app = web.Application()
        app.router.add_post("/api/{api}", self._api)
        app.router.add_get("/event", self._ws)
        app.router.add_get("/media/{name}", self._media)
        self._runner = web.AppRunner(app)
        self.base = ""

    async def start(self) -> None:
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        await self._runner.cleanup()

    @staticmethod
    def _ok(data: Any) -> web.Response:
        return web.json_response({"status": "ok", "retcode": 0, "data": data})

    async def _api(self, request: web.Request) -> web.Response:
        api = request.match_info["api"]
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401)
        params = await request.json()
        self.calls.append({"api": api, "params": params})
        if api == "get_login_info":
            if not self.logged_in:
                return web.json_response({"status": "failed", "retcode": -403, "message": "not logged in"})
            return self._ok({"uin": self.uin, "nickname": self.nickname})
        if api == "get_impl_info":
            return self._ok({"impl_name": "FakeNapCat", "impl_version": "4.8.0",
                             "qq_protocol_version": "9.9.19", "qq_protocol_type": "windows",
                             "milky_version": "1.3"})
        if api in ("send_private_message", "send_group_message"):
            self.seq += 1
            return self._ok({"message_seq": self.seq, "time": int(time.time())})
        if api in ("upload_private_file", "upload_group_file"):
            return self._ok({"file_id": "FID1"})
        if api in ("get_private_file_download_url", "get_group_file_download_url"):
            return self._ok({"download_url": f"{self.base}/media/doc.pdf"})
        if api in ("mark_message_as_read", "recall_private_message", "recall_group_message",
                   "kick_group_member", "set_group_name", "accept_friend_request"):
            return self._ok({})
        if api == "x_get_login_qrcode":   # 智聊自研边车扩展：拉登录二维码
            self.qr_pulls = getattr(self, "qr_pulls", 0) + 1
            return self._ok({"qr_png_base64": "iVBORw0KGgo=", "qr_url": "x://qq/login", "expire_sec": 120})
        return web.Response(status=404)

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.append(ws)
        for ev in self.pending:
            await ws.send_json(ev)
        self.pending = []
        try:
            async for _ in ws:
                pass
        finally:
            if ws in self.ws_clients:
                self.ws_clients.remove(ws)
        return ws

    async def push(self, ev: Dict[str, Any]) -> None:
        for ws in list(self.ws_clients):
            await ws.send_json(ev)

    async def _media(self, request: web.Request) -> web.Response:
        self.media_hits += 1
        name = request.match_info["name"]
        if name.endswith(".pdf"):
            return web.Response(body=b"%PDF-1.4 fake", content_type="application/pdf")
        return web.Response(body=JPEG, content_type="image/jpeg")


def _friend_msg(seq: int, text: str, *, peer: int = 42, nick: str = "阿强",
                extra_segments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    segs = [{"type": "text", "data": {"text": text}}] if text else []
    segs += extra_segments or []
    return {"time": int(time.time()), "self_id": 10001, "event_type": "message_receive", "data": {
        "message_scene": "friend", "peer_id": peer, "sender_id": peer, "message_seq": seq,
        "time": int(time.time()), "segments": segs,
        "friend": {"user_id": peer, "nickname": nick, "remark": "", "sex": "male", "qid": ""}}}


def _group_msg(seq: int, text: str, *, gid: int = 777, sender: int = 42) -> Dict[str, Any]:
    return {"time": int(time.time()), "self_id": 10001, "event_type": "message_receive", "data": {
        "message_scene": "group", "peer_id": gid, "sender_id": sender, "message_seq": seq,
        "time": int(time.time()),
        "segments": [{"type": "mention", "data": {"user_id": 10001, "name": "测试号"}},
                     {"type": "text", "data": {"text": " " + text}}],
        "group": {"group_id": gid, "group_name": "测试群", "member_count": 3, "max_member_count": 200},
        "group_member": {"user_id": sender, "nickname": "阿强", "card": "强哥", "role": "member"}}}


@pytest.fixture
async def milky():
    srv = FakeMilky()
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def _qq_cfg(srv: FakeMilky) -> Dict[str, Any]:
    return {"platform_login": {"qq": {"protocol_enabled": True, "milky_url": srv.base,
                                      "milky_token": srv.token,
                                      # 一次性风险须知已确认（未确认 provider 走 needs_risk_ack，单测另测）
                                      "risk_acknowledged_at": 1_700_000_000}}}


# ──────────────────────── qq 个人号：入站→收件箱→出站 全链路 ────────────────────────

async def test_qq_personal_end_to_end_inbox_and_orchestrator(milky, tmp_path, monkeypatch):
    from src.integrations import account_orchestrator as ao
    from src.integrations.qq_milky import QQPersonalWorker

    store = InboxStore(tmp_path / "inbox.db")
    seen = _wire_inbox(monkeypatch, store)
    auto: List[Dict[str, Any]] = []

    async def _fake_auto(payload):
        auto.append(payload)
    monkeypatch.setattr(pb, "maybe_auto_reply", _fake_auto)

    cfg = _qq_cfg(milky)
    # 连上事件流就推：私聊文本 / 群 @我 / 私聊图片（临时 URL 指向假 CDN）
    milky.pending = [
        _friend_msg(1001, "在吗"),
        _group_msg(2001, "在？"),
        _friend_msg(1002, "", extra_segments=[{"type": "image", "data": {
            "resource_id": "r1", "temp_url": f"{milky.base}/media/p.jpg", "summary": "[图片]",
            "sub_type": "normal"}}]),
    ]
    acc = {"platform": "qq", "account_id": "10001", "mode": "protocol",
           "meta": {"milky_url": milky.base, "milky_token": milky.token, "uin": 10001}}
    w = QQPersonalWorker(acc, cfg)
    await w.start()
    try:
        assert w.state == "running" and w.uin == 10001 and w.nickname == "测试号"
        assert w.impl.get("impl_name") == "FakeNapCat"
        assert await _wait(lambda: len(seen) >= 3), f"入站只到 {len(seen)} 条: {seen}"

        convs = {c["chat_key"]: c for c in store.list_conversations(platform="qq", account_id="10001")}
        assert set(convs) == {"qq:friend:42", "qq:group:777"}
        friend, group = convs["qq:friend:42"], convs["qq:group:777"]
        assert friend["display_name"] == "阿强" and friend["chat_type"] == "private"
        assert group["chat_type"] == "group" and group["display_name"] == "测试群"
        fmsgs = store.list_messages(friend["conversation_id"])
        texts = [m["text"] for m in fmsgs]
        assert "在吗" in texts and "[图片]" in texts
        img = [m for m in fmsgs if m.get("media_type") == "image"][0]
        # 图片已从临时 URL 落到 protocol_media 根（稳定链接），且文件真在磁盘上
        assert img["media_ref"].startswith("/static/protocol_media/qq/10001_1002.")
        local = pb.protocol_media_root() / "qq" / img["media_ref"].rsplit("/", 1)[1]
        assert local.is_file() and local.read_bytes() == JPEG and milky.media_hits == 1
        gmsgs = store.list_messages(group["conversation_id"])
        assert gmsgs and gmsgs[-1]["text"].startswith("@测试号")
        assert (gmsgs[-1].get("sender_name") or "") == "强哥"
        # 私聊触发自动回复钩子（群不触发）
        assert {p["chat_key"] for p in auto} == {"qq:friend:42"}

        # ── 编排器：与 TG/WA/LINE 同一套 orch.* 入口 ──
        orch = ao.AccountOrchestrator()
        _managed(orch, w, "qq", "10001", "protocol")
        assert orch.owns("qq", "10001") and orch.owns_media("qq", "10001")
        assert orch.media_capability("qq", "10001")["owns"] is True
        res = await orch.send("qq", "10001", "qq:friend:42", "在的，您说")
        assert res["delivered"] is True and res["message_id"] == "5001"
        sent = [c for c in milky.calls if c["api"] == "send_private_message"][-1]["params"]
        assert sent == {"user_id": 42, "message": [{"type": "text", "data": {"text": "在的，您说"}}]}
        outs = [m for m in store.list_messages(friend["conversation_id"]) if m["direction"] == "out"]
        assert outs and outs[-1]["text"] == "在的，您说"
        # 出站行带平台侧 id（message_seq）——工作台「撤回」按钮就靠它找到要撤的那条
        assert outs[-1]["platform_msg_id"] == "5001"
        # 已读回执：只认记过的入站 seq（1002 是最后一条私聊）
        assert await orch.mark_read("qq", "10001", "qq:friend:42") is True
        assert milky.calls[-1] == {"api": "mark_message_as_read", "params": {
            "message_scene": "friend", "peer_id": 42, "message_seq": 1002}}
        # typing：协议层无 API（HARD_LIMITS 已声明），编排器如实回 False、不抛
        assert await orch.send_chat_action("qq", "10001", "qq:friend:42") is False
        # 媒体出站（图片进群，配文合并成段）
        pic = tmp_path / "out.png"
        pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        rm = await orch.send_media("qq", "10001", "qq:group:777", media_path=str(pic),
                                   media_url="/static/x/out.png", media_type="image",
                                   caption="看这张", origin="manual")
        assert rm["delivered"] is True
        gsend = [c for c in milky.calls if c["api"] == "send_group_message"][-1]["params"]
        assert gsend["group_id"] == 777 and [s["type"] for s in gsend["message"]] == ["text", "image"]
        assert gsend["message"][1]["data"]["uri"].startswith("base64://")
        # 文件出站：upload_private_file（Milky 无文件消息段）
        doc = tmp_path / "报价.pdf"
        doc.write_bytes(b"%PDF-1.4 x")
        rf = await orch.send_media("qq", "10001", "qq:friend:42", media_path=str(doc),
                                   media_url="/static/x/报价.pdf", media_type="document",
                                   caption="", origin="manual")
        assert rf["delivered"] is True
        assert milky.calls[-1]["api"] == "upload_private_file"
        assert milky.calls[-1]["params"]["file_name"] == "报价.pdf"
        # 撤回（与 TG/LINE 同一编排器入口）
        rr = await orch.delete_messages("qq", "10001", "qq:friend:42", ["5001"])
        assert rr == {"ok": True, "deleted": 1}
        assert milky.calls[-1] == {"api": "recall_private_message",
                                   "params": {"user_id": 42, "message_seq": 5001}}
        # 群管理（GROUP_ADMIN_METHODS 契约）
        assert await w.kick_group_member("qq:group:777", 42) is True
        assert await w.rename_group("qq:group:777", "新群名") is True

        # ── 健康态：正常 → 协议端掉登录 → needs_login 上报（账号栏「重新登录」）──
        assert await w.healthy() is True
        reports: List[Any] = []
        import src.integrations.platform_session_health as PSH
        monkeypatch.setattr(PSH, "report_session_transition",
                            lambda p, a, s, **kw: reports.append((p, a, s)) or {})
        milky.logged_in = False
        w._health_ts = 0.0
        assert await w.healthy() is False and ("qq", "10001", "needs_login") in reports
        st = w.status()
        assert st["uin"] == 10001 and st["events_total"] == 3 and st["milky_url"] == milky.base
    finally:
        await w.stop()
    assert w.state == "stopped"


async def test_qq_personal_live_push_after_start_and_file_inbound(milky, tmp_path, monkeypatch):
    """启动后（非连接瞬间）到达的事件同样入库；文件段先换下载链接再落盘。"""
    from src.integrations.qq_milky import QQPersonalWorker

    store = InboxStore(tmp_path / "inbox.db")
    seen = _wire_inbox(monkeypatch, store)

    async def _noop(payload):
        return None
    monkeypatch.setattr(pb, "maybe_auto_reply", _noop)
    acc = {"platform": "qq", "account_id": "10001", "mode": "protocol",
           "meta": {"milky_url": milky.base, "milky_token": milky.token}}
    w = QQPersonalWorker(acc, _qq_cfg(milky))
    await w.start()
    try:
        assert await _wait(lambda: bool(milky.ws_clients))
        await milky.push(_friend_msg(1101, "", extra_segments=[{"type": "file", "data": {
            "file_id": "FID9", "file_name": "合同.pdf", "file_size": 12, "file_hash": "h"}}]))
        assert await _wait(lambda: len(seen) >= 1)
        m = seen[0]
        assert m["media_type"] == "file" and m["text"].startswith("[文件] 合同.pdf")
        assert m["media_ref"].startswith("/static/protocol_media/qq/10001_1101.pdf")
        dl = [c for c in milky.calls if c["api"] == "get_private_file_download_url"][-1]["params"]
        assert dl == {"user_id": 42, "file_id": "FID9", "file_hash": "h"}
        # 自己发的（sender==uin）不入站
        await milky.push(_friend_msg(1102, "我发的", peer=42) | {"data": {
            **_friend_msg(1102, "我发的")["data"], "sender_id": 10001}})
        await asyncio.sleep(0.2)
        assert len(seen) == 1
    finally:
        await w.stop()


# ──────────────────────── qq 登录 provider / readiness / 向导 走真 HTTP ────────────────────────

async def test_qq_login_provider_readiness_and_wizard_over_real_http(milky, monkeypatch):
    from src.integrations import platform_readiness as PR
    from src.integrations import qq_protocol_login as L
    from src.integrations.account_registry import get_account_registry
    from src.integrations.protocol_diagnostics import probe_services

    cfg = _qq_cfg(milky)

    async def _fake_enrich(platform, aid, *, name, avatar_url, config):
        return None
    import src.integrations.account_self_profile as ASP
    monkeypatch.setattr(ASP, "enrich_from_fields", _fake_enrich)

    # 协议端在、QQ 已登录 → 一轮 poll 即 authorized，账号登记 online 且快照端点/Token 进 meta
    info = await L.make_provider(cfg)(None, "qq", "protocol", "")
    assert "reason_code" not in info and callable(info["poll"])
    res = await info["poll"](None)
    assert res["status"] == "authorized" and res["account_id"] == "10001"
    row = get_account_registry().get("qq", "10001")
    assert row and row["status"] == "online" and row["mode"] == "protocol"
    assert row["meta"]["milky_url"] == milky.base and row["meta"]["milky_token"] == "tok"

    # 边车在、QQ 未登录 → start 首帧就带二维码（login_kind=qr，扫码在本窗口）；poll 持续 pending 并刷新码
    milky.logged_in = False
    info2 = await L.make_provider(cfg)(None, "qq", "protocol", "")
    assert info2["qr_image"].startswith("data:image/png;base64,") and info2["qr_expire_sec"] == 120
    p = await info2["poll"](None)
    assert p["status"] == "pending" and p["qr_image"].startswith("data:image/png;base64,")
    assert getattr(milky, "qr_pulls", 0) >= 2
    milky.logged_in = True

    # 诊断探针 + readiness：真 HTTP 探到假协议端 → 可用；关掉服务 → service_down
    probes = await probe_services(cfg)
    assert probes.get("qq") is True
    d = PR.diagnose_mode("qq", "protocol", cfg, service_ok=probes["qq"])
    assert d["ready"] is True
    assert PR.service_probe_targets(cfg)["qq"] == milky.base
    await milky.stop()
    probes2 = await probe_services(cfg, force=True)
    assert probes2.get("qq") is False
    d2 = PR.diagnose_mode("qq", "protocol", cfg, service_ok=False)
    assert d2["ready"] is False and d2["reason_code"] == PR.BLOCK_SERVICE_DOWN

    # 设置向导：qq 卡（协议端地址/Token）与 qqbot 卡（AppID/Secret）都在，且与 zalo 卡同形
    from src.utils.channel_setup import CHANNELS
    by_id = {c.id: c for c in CHANNELS}
    assert {"qq", "qqbot", "zalo"} <= set(by_id)
    qq, qqbot, zalo = by_id["qq"], by_id["qqbot"], by_id["zalo"]
    assert qq.login_required and qq.login_platform == "qq"
    # 自研连接边车：用户不填地址/token，qq 卡无字段（扫码即用）
    assert [f.key for f in qq.fields] == []
    assert qqbot.official_platform == "qqbot" and qqbot.console_url == "https://q.qq.com/"
    assert {f.key for f in qqbot.fields} >= {"qqbot.app_id", "qqbot.app_secret"}
    assert type(qqbot) is type(zalo)


# ═════════════════════════════ 假 QQ 开放平台 ═════════════════════════════

class FakeQQOpen:
    def __init__(self, *, app_id: str = "102000001", secret: str = "S3CRET") -> None:
        self.app_id, self.secret = app_id, secret
        self.token = "TOKEN1"
        self.sent: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self.uploads: List[Dict[str, Any]] = []
        self.media_hits = 0
        self.identify: Optional[Dict[str, Any]] = None
        self.pending: List[Dict[str, Any]] = []
        self.ws_clients: List[web.WebSocketResponse] = []
        self.token_calls = 0
        app = web.Application()
        app.router.add_post("/app/getAppAccessToken", self._token)
        app.router.add_get("/gateway", self._gateway)
        app.router.add_get("/ws", self._ws)
        app.router.add_post("/v2/users/{openid}/messages", self._send)
        app.router.add_post("/v2/groups/{gid}/messages", self._send)
        app.router.add_post("/v2/users/{openid}/files", self._upload)
        app.router.add_post("/v2/groups/{gid}/files", self._upload)
        app.router.add_delete("/v2/users/{openid}/messages/{mid}", self._delete)
        app.router.add_delete("/v2/groups/{gid}/messages/{mid}", self._delete)
        app.router.add_get("/media/{name}", self._media)
        self._runner = web.AppRunner(app)
        self.base = ""

    async def start(self) -> None:
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        await self._runner.cleanup()

    def _authed(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"QQBot {self.token}"

    async def _token(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.token_calls += 1
        if body != {"appId": self.app_id, "clientSecret": self.secret}:
            return web.json_response({"code": 100005, "message": "bad secret"}, status=400)
        return web.json_response({"access_token": self.token, "expires_in": "7200"})

    async def _gateway(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"code": 11244, "message": "auth"}, status=401)
        return web.json_response({"url": self.base.replace("http://", "ws://") + "/ws"})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.append(ws)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 41250}})
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                p = json.loads(msg.data)
                if p.get("op") == 2:
                    self.identify = p["d"]
                    await ws.send_json({"op": 0, "s": 1, "t": "READY", "d": {
                        "version": 1, "session_id": "SESS1",
                        "user": {"id": "BOTID", "username": "测试机器人", "bot": True}, "shard": [0, 1]}})
                    for ev in self.pending:
                        await ws.send_json(ev)
                    self.pending = []
                elif p.get("op") == 1:
                    await ws.send_json({"op": 11})
        finally:
            if ws in self.ws_clients:
                self.ws_clients.remove(ws)
        return ws

    async def push(self, ev: Dict[str, Any]) -> None:
        for ws in list(self.ws_clients):
            await ws.send_json(ev)

    async def _send(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"code": 11244, "message": "auth"}, status=401)
        body = await request.json()
        body["_path"] = request.path
        self.sent.append(body)
        return web.json_response({"id": f"OUT{len(self.sent)}", "timestamp": int(time.time())})

    async def _delete(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"code": 11244, "message": "auth"}, status=401)
        self.deleted.append(request.path)
        return web.Response(status=200)

    async def _upload(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"code": 11244, "message": "auth"}, status=401)
        body = await request.json()
        body["_path"] = request.path
        self.uploads.append(body)
        if str(body.get("url") or "").endswith("unreachable.png"):
            return web.json_response({"code": 304082, "message": "upload media info fail"})
        return web.json_response({"file_uuid": f"FU{len(self.uploads)}",
                                  "file_info": f"FI{len(self.uploads)}", "ttl": 0})

    async def _media(self, request: web.Request) -> web.Response:
        self.media_hits += 1
        return web.Response(body=JPEG, content_type="image/jpeg")


def _c2c_event(seq: int, text: str, *, openid: str = "OPENID_A",
               attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    return {"op": 0, "s": seq, "t": "C2C_MESSAGE_CREATE", "id": f"EV{seq}", "d": {
        "id": f"IN{seq}", "content": text, "timestamp": ts,
        "author": {"user_openid": openid, "id": openid}, "attachments": attachments or []}}


@pytest.fixture
async def qqopen(monkeypatch):
    from src.integrations import qq_official as qo
    srv = FakeQQOpen()
    await srv.start()
    monkeypatch.setattr(qo, "QQBOT_TOKEN_URL", srv.base + "/app/getAppAccessToken")
    monkeypatch.setattr(qo, "QQBOT_API_BASE", srv.base)
    qo.reset_for_tests()
    try:
        yield srv
    finally:
        await srv.stop()
        qo.reset_for_tests()


def _qqbot_cfg(srv: FakeQQOpen) -> Dict[str, Any]:
    return {"qqbot": {"enabled": True, "app_id": srv.app_id, "app_secret": srv.secret,
                      "connect_mode": "websocket", "sandbox": False}}


async def test_qqbot_end_to_end_gateway_inbox_passive_window_and_recall(qqopen, tmp_path, monkeypatch):
    from src.integrations import account_orchestrator as ao
    from src.integrations.official_api_worker import QQBotOfficialWorker, official_worker_factory

    store = InboxStore(tmp_path / "inbox.db")
    seen = _wire_inbox(monkeypatch, store)
    cfg = _qqbot_cfg(qqopen)
    qqopen.pending = [_c2c_event(2, "你好，想了解课程")]

    factory = official_worker_factory("qqbot")
    acc = {"platform": "qqbot", "account_id": qqopen.app_id, "mode": "official",
           "meta": {"app_id": qqopen.app_id, "app_secret": qqopen.secret}}
    w = factory(acc, cfg)
    assert isinstance(w, QQBotOfficialWorker)
    await w.start()
    try:
        # 真 WS：token → gateway → Hello → Identify（intents 含 C2C/群）→ READY → Dispatch
        assert await _wait(lambda: w._gateway is not None and w._gateway.connected)
        assert qqopen.identify and qqopen.identify["token"] == "QQBot TOKEN1"
        assert qqopen.identify["intents"] & (1 << 25)
        assert await _wait(lambda: len(seen) >= 1)
        m = seen[0]
        assert m["platform"] == "qqbot" and m["account_id"] == qqopen.app_id
        assert m["chat_key"] == "qqbot:c2c:OPENID_A" and m["text"] == "你好，想了解课程"
        convs = store.list_conversations(platform="qqbot")
        assert len(convs) == 1 and convs[0]["chat_key"] == "qqbot:c2c:OPENID_A"
        assert convs[0]["chat_type"] == "private"
        assert await w.healthy() is True and w.status()["gateway"]["connected"] is True
        from src.integrations.qq_official import get_passive_ledger
        assert w.status()["passive_ledger"]["chats"] == 1
        assert get_passive_ledger().status("qqbot:c2c:OPENID_A")["remaining"] == 4

        # 编排器出站：被动回复带 msg_id/msg_seq。两层窗口判定叠着走、数值同源（60 min / 4）：
        # ① 编排器里的通用 window_guard（channel_policy，从收件箱事实现算）——自动链只用 3 条，
        #    第 4 条留给坐席；② 我方账本（按来话 msg_id 锚点 fail-closed）——第 5 条到 worker 也拦。
        orch = ao.AccountOrchestrator()
        _managed(orch, w, "qqbot", qqopen.app_id, "official")
        assert orch.owns("qqbot", qqopen.app_id) is True
        for i in range(1, 4):
            r = await orch.send("qqbot", qqopen.app_id, "qqbot:c2c:OPENID_A", f"回复{i}")
            assert r["delivered"] is True, r
            body = qqopen.sent[-1]
            assert body["_path"] == "/v2/users/OPENID_A/messages"
            assert body["content"] == f"回复{i}" and body["msg_id"] == "IN2" and body["msg_seq"] == i
        r4a = await orch.send("qqbot", qqopen.app_id, "qqbot:c2c:OPENID_A", "回复4")
        assert r4a["delivered"] is False and r4a["blocked"] == "policy_window_reserved_for_manual"
        r4 = await orch.send("qqbot", qqopen.app_id, "qqbot:c2c:OPENID_A", "回复4", origin="manual")
        assert r4["delivered"] is True and qqopen.sent[-1]["msg_seq"] == 4
        r5 = await orch.send("qqbot", qqopen.app_id, "qqbot:c2c:OPENID_A", "回复5", origin="manual")
        assert r5["delivered"] is False and r5["blocked"] == "policy_window_exhausted"
        r5w = await w.send("qqbot:c2c:OPENID_A", "回复5")   # 绕过编排器直到 worker：账本自身也 fail-closed
        assert r5w["delivered"] is False and r5w["blocked"] == "qq_passive_window"
        assert len(qqopen.sent) == 4
        # 会话头「本轮剩余 / 倒计时」chip 的数据源（send-caps reply_window）对 qqbot 成立，
        # 且与账本口径一致：4 条用完、窗口 60 分钟、人工与自动都不能再发
        from src.inbox.window_guard import snapshot as wg_snapshot
        rw = wg_snapshot("qqbot", qqopen.app_id, "qqbot:c2c:OPENID_A", store=store)
        assert rw["cap"] == 4 and rw["window_sec"] == 3600.0 and rw["sent"] == 4 and rw["remaining"] == 0
        assert rw["manual_allowed"] is False and rw["auto_allowed"] is False and rw["remaining_sec"] > 3500
        assert wg_snapshot("qq", "10001", "qq:friend:42", store=store) == {}   # 个人号无窗口规则
        outs = [x for x in store.list_messages(convs[0]["conversation_id"]) if x["direction"] == "out"]
        assert [x["text"] for x in outs] == ["回复1", "回复2", "回复3", "回复4"]
        assert [x["platform_msg_id"] for x in outs] == ["OUT1", "OUT2", "OUT3", "OUT4"]
        # 窗口拒发是平台政策不是通道故障：账号闸门不计连续失败
        from src.inbox import account_channel_gate as gate
        out_gate = gate.note_send_fail("qqbot", qqopen.app_id, error_kind="window_expired")
        assert out_gate.get("policy_block") is True and out_gate["streak"] == 0
        assert gate.degraded_state("qqbot", qqopen.app_id) is None

        # 新来一条 → 窗口重开
        await qqopen.push(_c2c_event(3, "还在吗"))
        assert await _wait(lambda: any(c["direction"] == "in" and c["msg_id"] == "IN3" for c in seen))
        r6 = await orch.send("qqbot", qqopen.app_id, "qqbot:c2c:OPENID_A", "在的")
        assert r6["delivered"] is True, r6
        assert qqopen.sent[-1]["msg_id"] == "IN3"

        # 撤回：DELETE /v2/users/{openid}/messages/{id}
        rr = await orch.delete_messages("qqbot", qqopen.app_id, "qqbot:c2c:OPENID_A", ["OUT5"])
        assert rr == {"ok": True, "deleted": 1}
        assert qqopen.deleted == ["/v2/users/OPENID_A/messages/OUT5"]
        # ── 富媒体 ──
        from src.integrations.official_api_worker import official_send_caps
        # 未配公网媒体 URL：能力位诚实、运行时同口径 no_public_url（按钮灰态有话说）
        assert official_send_caps("qqbot", cfg) == {
            "can_media": False, "can_voice": False, "reason": "needs_public_url"}
        pic = tmp_path / "out.jpg"
        pic.write_bytes(JPEG)
        rm0 = await w.send_media("qqbot:c2c:OPENID_A", media_path=str(pic), media_type="image",
                                 media_url="/static/protocol_media/qqbot/out.jpg")
        assert rm0["delivered"] is False and rm0["error_kind"] == "no_public_url"
        # 配上公网 URL（同 LINE/IG 的 official_media.public_base_url）→ 图片经编排器出站：
        # /files 换 file_info → msg_type=7 + 被动锚点；语音仍诚实不可（silk）
        cfg["official_media"] = {"public_base_url": "https://bot.example.com"}
        assert official_send_caps("qqbot", cfg) == {
            "can_media": True, "can_voice": False, "reason": "qqbot_media_pending"}
        remaining = get_passive_ledger().status("qqbot:c2c:OPENID_A")["remaining"]
        rm = await orch.send_media("qqbot", qqopen.app_id, "qqbot:c2c:OPENID_A",
                                   media_path=str(pic), media_url="/static/protocol_media/qqbot/out.jpg",
                                   media_type="image", caption="给您看下", origin="manual")
        assert rm["delivered"] is True, rm
        up = qqopen.uploads[-1]
        assert up["_path"] == "/v2/users/OPENID_A/files"
        assert up == {"file_type": 1, "url": "https://bot.example.com/static/protocol_media/qqbot/out.jpg",
                      "srv_send_msg": False, "_path": up["_path"]}
        body = qqopen.sent[-1]
        assert body["msg_type"] == 7 and body["media"] == {"file_info": "FI1"}
        assert body["content"] == "给您看下" and body["msg_id"] == "IN3" and body["msg_seq"] == 2
        assert get_passive_ledger().status("qqbot:c2c:OPENID_A")["remaining"] == remaining - 1
        outs2 = [x for x in store.list_messages(convs[0]["conversation_id"]) if x["direction"] == "out"]
        assert outs2[-1]["media_type"] == "image" and outs2[-1]["media_ref"].endswith("/out.jpg")
        # 上传被平台拒（304082 拉取失败）：如实失败且不白烧锚点
        bad = tmp_path / "unreachable.png"
        bad.write_bytes(b"\x89PNG")
        before = get_passive_ledger().status("qqbot:c2c:OPENID_A")["remaining"]
        rb = await w.send_media("qqbot:c2c:OPENID_A", media_path=str(bad), media_type="image",
                                media_url="/static/x/unreachable.png")
        assert rb["delivered"] is False and rb["error_kind"] != "window_expired"
        assert get_passive_ledger().status("qqbot:c2c:OPENID_A")["remaining"] == before
        # 入站附件：开放平台 CDN 临时链接先落 protocol_media 再入库（稳定链接，AI 识图可用）
        n_before = len(seen)
        await qqopen.push(_c2c_event(4, "", attachments=[{
            "content_type": "image/jpeg", "url": f"{qqopen.base}/media/in.jpg",
            "filename": "in.jpg", "size": len(JPEG)}]))
        assert await _wait(lambda: len(seen) > n_before)
        att = [c for c in seen[n_before:] if c.get("media_type") == "image"][0]
        assert att["media_ref"].startswith("/static/protocol_media/qqbot/") and qqopen.media_hits == 1
        local = pb.protocol_media_root() / "qqbot" / att["media_ref"].rsplit("/", 1)[1]
        assert local.is_file() and local.read_bytes() == JPEG
        # token 只取一次（缓存）
        assert qqopen.token_calls == 1
    finally:
        await w.stop()
    assert w._task is None


async def test_qqbot_webhook_mode_end_to_end_into_inbox(qqopen, tmp_path, monkeypatch):
    """Webhook 接法：op=13 验证握手 + Ed25519 验签事件 → 收件箱 → 被动回复可用。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.integrations import qq_official as qo

    store = InboxStore(tmp_path / "inbox.db")
    seen = _wire_inbox(monkeypatch, store)
    cfg = {"qqbot": {"enabled": True, "app_id": qqopen.app_id, "app_secret": qqopen.secret,
                     "connect_mode": "webhook", "webhook_path": "/qqbot/webhook"}}

    class _CM:
        config = cfg
    app = FastAPI()
    qo.register_qqbot_routes(app, _CM(), None)
    assert app.state.qqbot_webhook_path == "/qqbot/webhook"
    client = TestClient(app)
    # 平台验证握手
    r = client.post("/qqbot/webhook", json={"op": 13, "d": {"plain_token": "PT", "event_ts": "1725442341"}})
    assert r.status_code == 200 and r.json()["plain_token"] == "PT"
    assert r.json()["signature"] == qo.sign_validation(qqopen.secret, "PT", "1725442341")
    # 事件推送（签名正确）→ 200 op=12 且入库；签名错 → 401 不入库
    ev = _c2c_event(7, "webhook 来的")
    body = json.dumps(ev).encode()
    ts = str(int(time.time()))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sig = Ed25519PrivateKey.from_private_bytes(qo._ed25519_seed(qqopen.secret)).sign(
        ts.encode() + body).hex()
    r2 = client.post("/qqbot/webhook", content=body, headers={
        "X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts, "Content-Type": "application/json"})
    assert r2.status_code == 200 and r2.json() == {"op": 12}
    await asyncio.sleep(0)
    assert await _wait(lambda: len(seen) >= 1) and seen[0]["text"] == "webhook 来的"
    r3 = client.post("/qqbot/webhook", content=body, headers={
        "X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": ts,
        "Content-Type": "application/json"})
    assert r3.status_code == 401 and len(seen) == 1
    # 被动窗口已由 webhook 事件记锚 → 直接可回
    out = await qo.qqbot_send_text("qqbot:c2c:OPENID_A", "收到", config=cfg, account_id=qqopen.app_id)
    assert out["ok"] is True and qqopen.sent[-1]["msg_id"] == "IN7"


async def test_wizard_provisioning_to_orchestrator_pickup_for_both_platforms(milky, qqopen, tmp_path, monkeypatch):
    """向导保存凭证 → 注册表开通账号行 → 编排器监督 tick 真把 worker 拉起（两条轨都走一遍）。

    与 Zalo/IG 官方渠道同一条 ``_provision_official_account`` 路径；qq 个人号则由登录 provider
    写注册表。没有这一环，webhook/网关在收消息但 ``desired_accounts`` 里没它 → 永远发不出去。
    """
    from src.integrations import account_orchestrator as ao
    from src.integrations.account_registry import AccountRegistry
    from src.integrations.official_api_worker import QQBotOfficialWorker
    from src.integrations.qq_milky import QQPersonalWorker
    from src.web.routes.unified_inbox_setup_routes import _provision_official_account

    async def _noop(payload):
        return None
    monkeypatch.setattr(pb, "maybe_auto_reply", _noop)
    _wire_inbox(monkeypatch, InboxStore(tmp_path / "inbox.db"))
    reg = AccountRegistry(tmp_path / "accounts.db")
    monkeypatch.setattr(ao, "_WORKER_FACTORIES", dict(ao._WORKER_FACTORIES))
    import src.integrations.account_registry as AR
    monkeypatch.setattr(AR, "get_account_registry", lambda: reg)
    monkeypatch.setattr(ao, "get_account_registry", lambda: reg, raising=False)

    cfg = {**_qqbot_cfg(qqopen), **_qq_cfg(milky),
           "platform_login": {**_qq_cfg(milky)["platform_login"], "orchestrator_enabled": True}}
    cfg["qqbot"]["connect_mode"] = "webhook"   # 本用例只验拉起链路，不再开一条网关长连

    # ① 官方轨：向导保存凭证后的自动开户（account_id = qqbot.app_id，与入站镜像口径一致）
    assert _provision_official_account("qqbot", cfg) == qqopen.app_id
    row = reg.get("qqbot", qqopen.app_id)
    assert row and row["mode"] == "official" and row["status"] == "online"
    # 凭证只填一半 → 不开户（开了也起不来）
    half = {"qqbot": {"enabled": True, "app_id": "1", "app_secret": ""}}
    assert _provision_official_account("qqbot", half) == ""
    # ② 个人号轨：登录 provider 落的注册表行（这里直接写同形行）
    reg.upsert("qq", "10001", mode="protocol", status="online",
               meta={"milky_url": milky.base, "milky_token": milky.token, "uin": "10001"})

    # ③ 编排器：期望集含两行 → 监督循环一轮（sync 拉起 + tick 健康检查）两个真 worker 都 running
    ao.ensure_builtin_workers(cfg)
    orch = ao.AccountOrchestrator(registry=reg, config=cfg)
    desired = {(a["platform"], a["account_id"]) for a in orch.desired_accounts()}
    assert {("qqbot", qqopen.app_id), ("qq", "10001")} <= desired
    await orch.sync()
    await orch.tick()
    try:
        bot = orch._managed[ao.account_key("qqbot", qqopen.app_id)]
        qq = orch._managed[ao.account_key("qq", "10001")]
        assert bot.state == "running" and isinstance(bot.worker, QQBotOfficialWorker)
        assert qq.state == "running" and isinstance(qq.worker, QQPersonalWorker)
        assert qq.worker.uin == 10001 and qq.worker.nickname == "测试号"
        assert orch.owns("qq", "10001") and orch.owns("qqbot", qqopen.app_id)
        # 通道真相（账号栏 / 会话头「已连接」灯）对两条轨都成立
        for plat, aid in (("qq", "10001"), ("qqbot", qqopen.app_id)):
            ms = orch.managed_state(plat, aid)
            assert ms and ms["state"] == "running" and ms["gave_up"] is False
    finally:
        for key in list(orch._managed):
            await orch.stop_account(key)


# ═════════════════════════════ 对齐：和其他平台在同一张表里 ═════════════════════════════

def _js_set(src: str, decl_regex: str) -> set:
    m = re.search(decl_regex + r"\s*=\s*new Set\(\[(.*?)\]\)", src, re.S)
    assert m, decl_regex
    return set(re.findall(r"['\"]([a-z_\-]+)['\"]", m.group(1)))


def _js_array(src: str, decl_regex: str) -> List[str]:
    m = re.search(decl_regex + r"\s*=\s*\[(.*?)\]\s*;", src, re.S)
    assert m, decl_regex
    return re.findall(r"['\"]([a-z_\-]+)['\"]", m.group(1))


def _js_obj_keys(src: str, decl_regex: str) -> set:
    m = re.search(decl_regex + r"\s*=\s*\{(.*?)\}\s*;", src, re.S)
    assert m, decl_regex
    return set(re.findall(r"(?:^|[,{\s])['\"]?([A-Za-z_][A-Za-z0-9_\-]*)['\"]?\s*:", m.group(1)))


def test_parity_backend_platform_tables():
    """后端每张按平台分叉的表：qq / qqbot 与参照平台同在（按各表语义选参照）。"""
    from src.assistant.actions import ACTION_PLATFORMS
    from src.assistant.product_facts import CHANNEL_PLATFORM_KEYS, SUPPORTED_CHANNELS
    from src.companion.group_show.platform_policy import KNOWN_PLATFORMS as GS_KNOWN
    from src.contacts.reactivation_loop import _PLATFORM_LABELS
    from src.inbox.reply_pacing_settings import PLATFORMS as PACING
    from src.integrations import platform_registry as reg
    from src.utils.account_scope_migration import KNOWN_PLATFORMS as SCOPE_KNOWN
    from src.integrations.official_api_worker import OFFICIAL_PLATFORMS, official_send_caps
    from src.integrations.official_webhook_stats import PLATFORM_CONFIG_BLOCKS
    from src.integrations.platform_login import (
        DEFAULT_PLATFORM_MODES, PLATFORM_INSTRUCTION_KEYS, SUPPORTED_PLATFORMS,
    )
    from src.integrations.platform_readiness import _IMPLEMENTED_MODES
    from src.integrations.platform_session_health import SIDECAR_PLATFORMS
    from src.utils.channel_setup import CHANNELS
    from src.utils.episodic_identity_display import KNOWN_PLATFORMS as EID_KNOWN
    from src.web.routes.unified_inbox_send_routes import _PLATFORM_LABEL

    both = {"qq", "qqbot"}
    # 登录/接入登记：与 telegram（个人号）和 zalo（官方）同在
    assert both <= set(SUPPORTED_PLATFORMS) and {"telegram", "zalo"} <= set(SUPPORTED_PLATFORMS)
    assert both <= set(DEFAULT_PLATFORM_MODES) and both <= set(PLATFORM_INSTRUCTION_KEYS)
    impl = {p for p, _ in _IMPLEMENTED_MODES}
    assert both <= impl and {"telegram", "whatsapp", "line", "messenger", "instagram", "zalo"} <= impl
    assert both <= {c.id for c in CHANNELS}
    # 平台注册表（实施96 单一事实源）：两者都 implemented，qq 归准入区、qqbot 归主站
    assert both <= set(reg.implemented_ids())
    assert reg.get("qq").compliance == reg.COMPLIANCE_RESTRICTED
    assert reg.get("qqbot").compliance == reg.COMPLIANCE_MAIN
    # 官方家族（qqbot）：与 zalo/instagram 官方同在 OFFICIAL_PLATFORMS / webhook 台账 / send-caps
    assert {"qqbot", "zalo", "instagram"} <= set(OFFICIAL_PLATFORMS)
    assert {"qqbot", "zalo"} <= set(PLATFORM_CONFIG_BLOCKS)
    assert set(official_send_caps("qqbot", {})) == set(official_send_caps("zalo", {}))
    # 个人号家族（qq）：与 telegram 同在 节奏设置 / 助手动作 / 群展示策略 / 边车健康 / 唤醒话术
    for name, table in (("reply_pacing.PLATFORMS", PACING), ("ACTION_PLATFORMS", ACTION_PLATFORMS),
                        ("group_show.KNOWN_PLATFORMS", GS_KNOWN),
                        ("reactivation _PLATFORM_LABELS", _PLATFORM_LABELS)):
        assert "qq" in table and "telegram" in table, name
    assert {"qq", "zalo", "instagram"} <= set(SIDECAR_PLATFORMS)
    # 全平台参照系（身份展示 / 作用域迁移 / 媒体错误文案）
    for name, table in (("episodic_identity_display", EID_KNOWN),
                        ("account_scope_migration", SCOPE_KNOWN),
                        ("send_routes._PLATFORM_LABEL", _PLATFORM_LABEL)):
        assert both <= set(table) and {"telegram", "zalo"} <= set(table), name
    # 事实卡：两者都以对客叫法登记并映射回平台 id
    assert {"QQ 机器人", "QQ 个人号（协议登录）"} <= set(SUPPORTED_CHANNELS)
    assert CHANNEL_PLATFORM_KEYS["QQ 机器人"] == "qqbot"
    assert CHANNEL_PLATFORM_KEYS["QQ 个人号（协议登录）"] == "qq"


def test_parity_worker_contracts_match_reference_platforms():
    """worker 方法面：qq 个人号 ≥ Telegram companion（除协议层硬限制 typing/删整段历史）；
    qqbot ≥ 无状态官方 worker + 撤回。"""
    from src.integrations.account_orchestrator import LineProtocolWorker
    from src.integrations.official_api_worker import OfficialApiWorker, QQBotOfficialWorker
    from src.integrations.platform_capabilities import HARD_LIMITS, capability_matrix
    from src.integrations.qq_milky import QQPersonalWorker
    from src.integrations.telegram_companion_worker import TelegramCompanionWorker

    core = {"start", "stop", "healthy", "status", "send"}
    rich = core | {"send_media", "mark_read", "delete_messages"}
    qq_methods = {n for n in dir(QQPersonalWorker) if not n.startswith("_")}
    assert rich <= qq_methods
    assert {"kick_group_member", "rename_group"} <= qq_methods
    tg_methods = {n for n in dir(TelegramCompanionWorker) if not n.startswith("_")}
    line_methods = {n for n in dir(LineProtocolWorker) if not n.startswith("_")}
    # 参照平台有、qq 没有的能力，必须是登记过的协议层硬限制（typing）或 TG 独有（清对方设备）
    missing = (rich | {"send_chat_action"}) & (tg_methods | line_methods) - qq_methods
    assert missing <= {"send_chat_action"}, missing
    assert ("qq", "typing") in HARD_LIMITS
    matrix = capability_matrix({})
    qq_row, tg_row = matrix["qq:protocol"], matrix["telegram:protocol"]
    for cap in ("send_text", "send_media", "mark_read"):
        assert qq_row["caps"][cap] is True and tg_row["caps"][cap] is True, cap
    assert qq_row["recv_media"] is True and qq_row["recv_group"] is True and qq_row["group_send"] is True

    bot_methods = {n for n in dir(QQBotOfficialWorker) if not n.startswith("_")}
    base_methods = {n for n in dir(OfficialApiWorker) if not n.startswith("_")}
    assert base_methods <= bot_methods and "delete_messages" in bot_methods


def test_parity_msgops_revoke_and_persona_binding_and_channel_policy():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.unified_inbox_msgops_routes import register_msgops_routes

    app = FastAPI()
    register_msgops_routes(app, api_auth=lambda r: None, config_manager=None)
    meta = TestClient(app).get("/api/unified-inbox/message-ops/meta").json()
    rp = meta["revoke_platforms"]
    assert rp["qq"] is True and rp["qqbot"] is True and rp["telegram"] is True and rp["line"] is True
    # 人设绑定（registry 账号）端点白名单：与 line/zalo 同在
    src = _read("src/web/routes/persona_routes.py")
    m = re.search(r"_REGISTRY_ASSIGN_PLATFORMS\s*=\s*\{(.*?)\}", src, re.S)
    assert m and {"qq", "qqbot", "line", "zalo"} <= set(re.findall(r"['\"]([a-z_]+)['\"]", m.group(1)))
    # 渠道策略：qqbot 有窗口配额声明 → 分条封顶 1（拆条＝白烧 4 条额度）；qq 个人号无窗口
    from src.inbox.channel_policy import policy_for, window_rule
    from src.inbox.reply_split import cap_max_parts_for_platform
    assert window_rule("qqbot") == (3600.0, 4, 1)
    assert cap_max_parts_for_platform(3, "qqbot") == 1
    assert window_rule("qq") is None and cap_max_parts_for_platform(3, "qq") == 3
    assert policy_for("qqbot").buttons is False


def test_parity_billing_qq_personal_is_metered_like_every_platform():
    """按 Token 计费口径：AI 回复的计量在 ai_client 的单一出口（record_action_for_status("ai_reply")），
    与平台无关——QQ 个人号不需要也**不得**有专门分支/白名单。这里钉两件事：
    ① 费率表里 ai_reply 有价（10 Token/条）；② 计量出口不按平台分叉（源码里没有 platform 判定）。"""
    from src.licensing.token_ledger import TOKEN_RATES, tokens_for
    assert TOKEN_RATES["ai_reply"]["tokens"] > 0 and tokens_for("ai_reply", 1) == 10
    src = _read("src/ai/ai_client.py")
    i = src.index('record_action_for_status("ai_reply", 1)')
    window = src[max(0, i - 1200): i]
    for forbidden in ('platform ==', 'platform in', '"qq"', '"qqbot"', "'qq'"):
        assert forbidden not in window, f"ai_reply 计量出口不得按平台分叉：{forbidden}"
    # 风险须知与协议文案五要点齐全（ZH/EN），并明说「与其它渠道一致按 Token 计费」
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    for i in range(1, 6):
        assert f"inbox.connect.qq_risk_p{i}" in ZH and f"inbox.connect.qq_risk_p{i}" in EN
    assert "Token" in ZH["inbox.connect.qq_risk_p4"] and "Token" in EN["inbox.connect.qq_risk_p4"]
    assert (ROOT / "docs" / "QQ个人号接入协议与风险须知.md").is_file()


def test_parity_frontend_tables_and_i18n():
    """前端每张平台表（工作台栏 / 连接弹窗 / 徽标 / 图标 / 向导）与 i18n 键：两者与 zalo 同在。"""
    inbox = _read("src/web/templates/unified_inbox.html")
    both = {"qq", "qqbot"}
    fixed = set(_js_array(inbox, r"const FIXED_PLATS"))
    assert both <= fixed and "zalo" in fixed
    assert both <= _js_set(inbox, r"const _CONNECT_PLATS")
    for decl in (r"const PC", r"const PI", r"const PN", r"const PLAT_DESC", r"const PLAT_NOTE"):
        keys = _js_obj_keys(inbox, decl)
        assert both <= keys and "zalo" in keys, decl
    assert "qqbot" in _js_obj_keys(inbox, r"const _OFFICIAL_CONSOLE")
    wiz = _read("src/web/templates/setup_wizard.html")
    assert re.search(r"OFFICIAL_CHS\s*=\s*\{[^}]*qqbot", wiz) and re.search(r"REACH_PLATS\s*=\s*\{[^}]*qqbot", wiz)
    # 徽标/配色表（cases / cockpit / workspace_base）与 web+桌面图标副本
    assert both <= _js_obj_keys(_read("src/web/templates/cases.html"), r"var _PLAT_BADGE")
    assert both <= _js_obj_keys(_read("src/web/templates/cockpit.html"), r"var PLAT")
    assert both <= _js_obj_keys(_read("src/web/templates/workspace_base.html"), r"var PLAT_COLOR")
    for rel in ("src/web/static/platform_icons.js", "desktop/renderer/platform-icons.js"):
        js = _read(rel)
        assert both <= _js_obj_keys(js, r"var COLORS") and both <= _js_obj_keys(js, r"var NAMES"), rel
        assert "P.qq = " in js and "qqbot:" in js and '"pfg-qqb"' in js, rel
    # i18n：与 zalo 同一组键（描述 / 账号备注 / 连接说明）
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    for p in ("qq", "qqbot", "zalo"):
        for key in (f"inbox.plat.{p}_desc", f"inbox.acct.note_{p}", f"inbox.connect.instr_{p}"):
            assert key in ZH and key in EN, key
    for key in ("inbox.connect.instr_qq_setup", "inbox.connect.instr_qq_down",
                "inbox.connect.mode_l_qq_protocol", "inbox.connect.mode_d_qq_protocol",
                "inbox.connect.win_note_qqbot", "inbox.caps.qqbot_media_pending"):
        assert key in ZH and key in EN, key
