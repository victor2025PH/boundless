# -*- coding: utf-8 -*-
"""QQ 机器人（QQ 开放平台官方 API，platform=qqbot）接入门禁（2026-09-07，QQ 双轨·官方轨）。

守四件事：
1. **平台事实不变量**：被动回复窗口（单聊 60min/4 条、群 5min/5 条）由账本 fail-closed 执行，
   无锚点绝不撞主动消息接口；Ed25519 回调验证与官方文档给出的向量逐字一致。
2. **登记面完整**：platform_login / readiness / channel_setup / official worker / webhook 台账 /
   前端常驻清单 / i18n 一处不漏（漏一处＝「接了后端、面板不亮」的静默残缺）。
3. **向导字段 ↔ 配置键逐字对齐**（填了也不通是最坏的故障形态）。
4. **能力位与运行时分支一致**：qqbot 媒体本批 not_supported，caps 必须同口径。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.integrations import platform_readiness as PR
from src.integrations import qq_official as Q
from src.integrations.official_api_worker import (
    OFFICIAL_PLATFORMS,
    OfficialApiWorker,
    QQBotOfficialWorker,
    official_send_caps,
    official_worker_factory,
)
from src.integrations.platform_login import (
    DEFAULT_PLATFORM_MODES,
    PLATFORM_INSTRUCTION_KEYS,
    SUPPORTED_PLATFORMS,
    list_modes,
    login_kind,
)
from src.integrations.shared.official_send_error import classify_official_send_error
from src.utils.channel_setup import channel_status, get_channel

_ROOT = Path(__file__).resolve().parents[1]

# 官方文档「回调地址验证」示例向量（appid 11111111 / secret DG5g3B4j9X2KOErG）
_DOC_SECRET = "DG5g3B4j9X2KOErG"
_DOC_PLAIN = "Arq0D5A61EgUu4OxUvOp"
_DOC_TS = "1725442341"
_DOC_SIG = ("87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d0"
            "1bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706")


@pytest.fixture(autouse=True)
def _reset():
    Q.reset_for_tests()
    yield
    Q.reset_for_tests()


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, sec: float) -> None:
        self.t += sec


# ── chat_key 约定 ─────────────────────────────────────────────────────────────

def test_chat_key_roundtrip_and_bare_id():
    assert Q.make_chat_key("c2c", "U1") == "qqbot:c2c:U1"
    assert Q.make_chat_key("group", "G1") == "qqbot:group:G1"
    assert Q.parse_chat_key("qqbot:c2c:U1") == ("c2c", "U1")
    assert Q.parse_chat_key("qqbot:group:G1") == ("group", "G1")
    # 裸 openid（companion 主管道直传）→ 按单聊
    assert Q.parse_chat_key("ABCDEF") == ("c2c", "ABCDEF")


def test_infer_chat_type_recognises_qq_group_keys():
    from src.inbox.normalizer import infer_chat_type
    assert infer_chat_type("qqbot", "qqbot:group:G1") == "group"
    assert infer_chat_type("qqbot", "qqbot:c2c:U1") == "private"
    assert infer_chat_type("qq", "qq:group:123456") == "group"
    assert infer_chat_type("qq", "qq:friend:123456") == "private"


# ── 被动回复窗口账本 ───────────────────────────────────────────────────────────

def test_ledger_c2c_four_replies_then_blocked():
    clk = _Clock()
    led = Q.PassiveReplyLedger(now=clk)
    ck = Q.make_chat_key("c2c", "U1")
    led.note_inbound(ck, "m1")
    got = [led.reserve(ck) for _ in range(5)]
    assert [g and g["msg_seq"] for g in got[:4]] == [1, 2, 3, 4]
    assert got[4] is None, "第 5 条必须被本地拦下（平台上限 4）"
    assert led.stats["blocked"] == 1 and led.stats["reserved"] == 4
    st = led.status(ck)
    assert st["remaining"] == 0


def test_ledger_window_expiry_and_newest_anchor_first():
    clk = _Clock()
    led = Q.PassiveReplyLedger(now=clk)
    ck = Q.make_chat_key("c2c", "U1")
    led.note_inbound(ck, "old")
    clk.tick(10)
    led.note_inbound(ck, "new")
    assert led.reserve(ck) == {"msg_id": "new", "msg_seq": 1}
    clk.tick(Q.C2C_WINDOW_SEC + 1)   # 两条都过期
    assert led.reserve(ck) is None
    assert led.status(ck) == {"remaining": 0, "expires_in": 0, "anchor": ""}


def test_ledger_group_limits_and_event_anchor():
    clk = _Clock()
    led = Q.PassiveReplyLedger(now=clk)
    gk = Q.make_chat_key("group", "G1")
    led.note_inbound(gk, "g1", group=True)
    assert [led.reserve(gk)["msg_seq"] for _ in range(5)] == [1, 2, 3, 4, 5]
    assert led.reserve(gk) is None
    clk.tick(Q.GROUP_WINDOW_SEC + 1)
    led.note_event(gk, "evt-1")
    assert led.reserve(gk) == {"event_id": "evt-1"}
    assert led.reserve(gk) is None, "事件锚点只许回一条"
    assert led.status(gk)["remaining"] == 0


def test_ledger_dedups_same_anchor_and_caps_per_chat():
    led = Q.PassiveReplyLedger(now=_Clock())
    ck = Q.make_chat_key("c2c", "U1")
    for _ in range(3):
        led.note_inbound(ck, "same")
    assert led.stats["anchored"] == 1
    for i in range(20):
        led.note_inbound(ck, f"m{i}")
    assert len(led._chats[ck].anchors) == led.MAX_ANCHORS_PER_CHAT


# ── 事件归一 ─────────────────────────────────────────────────────────────────

def _c2c_payload(**over) -> Dict[str, Any]:
    # 时间戳取「现在」（ISO8601 带时区，平台真实形态）：账本按 60 分钟窗判锚点是否可用，
    # 写死日期会在当天 11:00 之后全部过期 → 测试随钟表变红。
    import time as _t
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now_iso = _dt.fromtimestamp(_t.time(), tz=_tz(_td(hours=8))).isoformat(timespec="seconds")
    d = {
        "author": {"id": "A1", "user_openid": "OPEN1"},
        "content": "你好 机器人",
        "id": "ROBOT1.0_abc",
        "timestamp": now_iso,
        "attachments": [{"content_type": "image/jpeg", "filename": "a.jpg",
                         "url": "gchat.qpic.cn/x.jpg", "size": 10, "width": 1, "height": 1}],
    }
    d.update(over)
    return {"op": 0, "s": 7, "t": "C2C_MESSAGE_CREATE", "id": "EVT1", "d": d}


def test_extract_c2c_message_with_attachment():
    import time as _t
    evs = Q.extract_qqbot_events(_c2c_payload())
    assert len(evs) == 1
    ev = evs[0]
    assert ev["kind"] == "message" and ev["scene"] == "c2c"
    assert ev["chat_key"] == "qqbot:c2c:OPEN1" and ev["peer_openid"] == "OPEN1"
    assert ev["msg_id"] == "ROBOT1.0_abc" and ev["event_id"] == "EVT1"
    assert ev["media"] == [{"media_type": "image", "url": "https://gchat.qpic.cn/x.jpg"}]
    assert abs(ev["ts"] - _t.time()) < 5
    # 固定向量：ISO8601 带时区 / 纯数字秒 / 解不出回落 now
    fixed = Q.extract_qqbot_events(_c2c_payload(timestamp="2026-09-07T10:00:00+08:00"))[0]
    assert abs(fixed["ts"] - 1788746400.0) < 1
    assert Q._parse_ts("1725442341") == 1725442341.0
    assert abs(Q._parse_ts("garbage") - _t.time()) < 5


def test_extract_group_at_message_strips_leading_space_and_anchor_events():
    p = {"op": 0, "s": 1, "t": "GROUP_AT_MESSAGE_CREATE", "id": "E2",
         "d": {"author": {"member_openid": "M1"}, "group_openid": "G1",
               "content": " 在吗", "id": "gm1", "timestamp": "1725442341"}}
    ev = Q.extract_qqbot_events(p)[0]
    assert ev["scene"] == "group" and ev["chat_key"] == "qqbot:group:G1"
    assert ev["text"] == "在吗" and ev["peer_openid"] == "M1"
    fa = Q.extract_qqbot_events({"op": 0, "t": "FRIEND_ADD", "id": "E3",
                                 "d": {"openid": "OPEN9", "timestamp": 1}})[0]
    assert fa["kind"] == "anchor" and fa["chat_key"] == "qqbot:c2c:OPEN9"
    ga = Q.extract_qqbot_events({"op": 0, "t": "GROUP_ADD_ROBOT", "id": "E4",
                                 "d": {"group_openid": "G9", "op_member_openid": "M9"}})[0]
    assert ga["kind"] == "anchor" and ga["chat_key"] == "qqbot:group:G9"
    # 非 Dispatch / 不认识的事件 → 空
    assert Q.extract_qqbot_events({"op": 10, "d": {}}) == []
    assert Q.extract_qqbot_events({"op": 0, "t": "MESSAGE_AUDIT_PASS", "d": {}}) == []


def test_attachment_media_kinds():
    assert Q.attachment_media({"content_type": "voice", "url": "x"}) == ("voice", "https://x")
    assert Q.attachment_media({"content_type": "video/mp4", "url": "https://v"}) == ("video", "https://v")
    assert Q.attachment_media({"content_type": "file", "url": "https://f"}) == ("file", "https://f")
    assert Q.attachment_media({"content_type": "", "url": ""}) == ("", "")


# ── Ed25519 回调验证：与官方文档向量逐字一致 ─────────────────────────────────

def test_sign_validation_matches_official_doc_vector():
    assert Q.sign_validation(_DOC_SECRET, _DOC_PLAIN, _DOC_TS) == _DOC_SIG


def test_verify_webhook_signature_roundtrip_and_reject():
    body = json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {}}).encode("utf-8")
    ts = "1725442341"
    sig = Q._private_key(_DOC_SECRET).sign(ts.encode("utf-8") + body).hex()
    assert Q.verify_webhook_signature(_DOC_SECRET, sig, ts, body) is True
    assert Q.verify_webhook_signature(_DOC_SECRET, sig, "1725442342", body) is False
    assert Q.verify_webhook_signature(_DOC_SECRET, "zz", ts, body) is False
    assert Q.verify_webhook_signature("otherSecret", sig, ts, body) is False


# ── token 管理 ───────────────────────────────────────────────────────────────

async def test_token_manager_caches_and_refreshes(monkeypatch):
    clk = _Clock()
    calls: List[Dict[str, Any]] = []

    async def fake_http(method, url, *, headers=None, payload=None, timeout=20.0):
        calls.append({"url": url, "payload": payload})
        return 200, {"access_token": f"tok{len(calls)}", "expires_in": "7200"}

    monkeypatch.setattr(Q, "_http_json", fake_http)
    tm = Q.QQBotTokenManager(now=clk)
    assert await tm.get("app", "sec") == "tok1"
    assert await tm.get("app", "sec") == "tok1"      # 缓存命中
    assert calls[0]["url"] == Q.QQBOT_TOKEN_URL
    assert calls[0]["payload"] == {"appId": "app", "clientSecret": "sec"}
    clk.tick(7200 - 60)                               # 进入刷新窗
    assert await tm.get("app", "sec") == "tok2"
    assert len(calls) == 2
    assert await tm.get("", "sec") == ""


async def test_token_manager_failure_not_cached(monkeypatch):
    async def bad(method, url, **kw):
        return 401, {"message": "invalid appid"}
    monkeypatch.setattr(Q, "_http_json", bad)
    tm = Q.QQBotTokenManager(now=_Clock())
    assert await tm.get("app", "sec") == ""
    assert tm.peek("app") == ""


# ── 发送：被动窗口 fail-closed + 请求体 + 错误分类 ────────────────────────────

def _cfg(**over):
    c = {"qqbot": {"enabled": True, "app_id": "APP", "app_secret": "SEC"}}
    c["qqbot"].update(over)
    return c


@pytest.fixture
def token_ok(monkeypatch):
    async def fake_get(self, app_id, app_secret):
        return "TOKEN"
    monkeypatch.setattr(Q.QQBotTokenManager, "get", fake_get)


async def test_send_blocked_without_anchor(token_ok):
    led = Q.PassiveReplyLedger()
    out = await Q.qqbot_send_text("qqbot:c2c:U1", "hi", config=_cfg(),
                                  check_kill_switch=False, ledger=led)
    assert out["ok"] is False
    assert out["error_kind"] == "window_expired"
    assert out["blocked"] == "qq_passive_window"
    assert out["retriable"] is False


async def test_send_uses_passive_anchor_and_endpoint(monkeypatch, token_ok):
    sent: List[Dict[str, Any]] = []

    async def fake_http(method, url, *, headers=None, payload=None, timeout=20.0):
        sent.append({"method": method, "url": url, "headers": headers, "payload": payload})
        return 200, {"id": "OUT1", "timestamp": 1}

    monkeypatch.setattr(Q, "_http_json", fake_http)
    led = Q.PassiveReplyLedger()
    led.note_inbound("qqbot:c2c:U1", "IN1")
    out = await Q.qqbot_send_text("qqbot:c2c:U1", "你好", config=_cfg(), reply_to_msg_id="IN1",
                                  check_kill_switch=False, ledger=led)
    assert out["ok"] is True and out["data"]["id"] == "OUT1"
    req = sent[0]
    assert req["url"] == f"{Q.QQBOT_API_BASE}/v2/users/U1/messages"
    assert req["headers"]["Authorization"] == "QQBot TOKEN"
    assert req["payload"]["msg_type"] == 0 and req["payload"]["content"] == "你好"
    assert req["payload"]["msg_id"] == "IN1" and req["payload"]["msg_seq"] == 1
    assert req["payload"]["message_reference"] == {"message_id": "IN1"}
    # 群走群接口；沙箱换域名
    led.note_inbound("qqbot:group:G1", "GIN", group=True)
    await Q.qqbot_send_text("qqbot:group:G1", "hey", config=_cfg(sandbox=True),
                            check_kill_switch=False, ledger=led)
    assert sent[1]["url"] == f"{Q.QQBOT_SANDBOX_API_BASE}/v2/groups/G1/messages"


async def test_send_classifies_platform_errors(monkeypatch, token_ok):
    async def rate_limited(method, url, **kw):
        return 429, {"message": "频率限制", "code": 50002, "err_code": 50002}
    monkeypatch.setattr(Q, "_http_json", rate_limited)
    led = Q.PassiveReplyLedger()
    led.note_inbound("qqbot:c2c:U1", "IN1")
    out = await Q.qqbot_send_text("qqbot:c2c:U1", "x", config=_cfg(),
                                  check_kill_switch=False, ledger=led)
    assert out["ok"] is False and out["error_kind"] == "rate_limited" and out["retriable"] is True

    async def whitelist(method, url, **kw):
        return 403, {"message": "ip not in whitelist", "code": 11244}
    monkeypatch.setattr(Q, "_http_json", whitelist)
    led.note_inbound("qqbot:c2c:U2", "IN2")
    out = await Q.qqbot_send_text("qqbot:c2c:U2", "x", config=_cfg(),
                                  check_kill_switch=False, ledger=led)
    assert out["error_kind"] == "invalid_token"


async def test_send_missing_creds_and_empty_text(token_ok):
    led = Q.PassiveReplyLedger()
    led.note_inbound("qqbot:c2c:U1", "IN1")
    out = await Q.qqbot_send_text("qqbot:c2c:U1", "x", config={"qqbot": {}},
                                  check_kill_switch=False, ledger=led)
    assert out["error_kind"] == "invalid_token"
    out = await Q.qqbot_send_text("qqbot:c2c:U1", "   ", config=_cfg(),
                                  check_kill_switch=False, ledger=led)
    assert out["ok"] is True and out["data"] == {"skipped": "empty"}


def test_classify_qqbot_error_text_heuristics():
    win = classify_official_send_error(
        "qqbot", status=400, body={"message": "msg_id 已过期或回复次数已用完", "code": 40034001})
    assert win["kind"] == "window_expired"
    tok = classify_official_send_error("qqbot", status=401, body={"message": "token 鉴权失败", "code": 11243})
    assert tok["kind"] == "invalid_token"
    tr = classify_official_send_error("qqbot", status=500, body={"message": "服务内部错误", "code": 50001})
    assert tr["kind"] == "transient" and tr["retriable"] is True
    # 顶层 code 的挖取不影响 Zalo / Graph 风格
    z = classify_official_send_error("zalo", status=200, body={"error": -213, "message": "x"})
    assert z["kind"] == "window_expired"


# ── 网关 op 分发（纯逻辑） ───────────────────────────────────────────────────

async def test_gateway_handle_payload_identify_ready_dispatch_and_controls():
    got: List[Dict[str, Any]] = []
    sent: List[Dict[str, Any]] = []

    async def on_event(p):
        got.append(p)

    async def send(obj):
        sent.append(obj)

    gw = Q.QQBotGateway(app_id="A", app_secret="S", sandbox=False,
                        intents=Q.INTENT_GROUP_AND_C2C, on_event=on_event)
    assert await gw.handle_payload({"op": 10, "d": {"heartbeat_interval": 45000}}, send, "T") is None
    assert sent[-1]["op"] == 2 and sent[-1]["d"]["token"] == "QQBot T"
    assert sent[-1]["d"]["intents"] == Q.INTENT_GROUP_AND_C2C
    await gw.handle_payload({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "SID"}}, send, "T")
    assert gw.connected is True and gw.session_id == "SID"
    await gw.handle_payload(_c2c_payload() | {"s": 9}, send, "T")
    assert got and gw.last_seq == 9 and gw.events_total == 1
    assert await gw.handle_payload({"op": 7}, send, "T") == "reconnect"
    # 再来 Hello 时已有 session → resume 而不是 identify
    await gw.handle_payload({"op": 10, "d": {"heartbeat_interval": 45000}}, send, "T")
    assert sent[-1]["op"] == 6 and sent[-1]["d"]["session_id"] == "SID" and sent[-1]["d"]["seq"] == 9
    assert await gw.handle_payload({"op": 9}, send, "T") == "reidentify"
    assert gw.session_id == "" and gw.last_seq is None
    assert await gw.handle_payload({"op": 11}, send, "T") is None


async def test_gateway_run_once_with_fake_ws(monkeypatch):
    """假 ws：Hello → READY → 一条消息 → 关闭(4009)。验证心跳任务启动、事件回调、收尾。"""
    events: List[Dict[str, Any]] = []

    async def on_event(p):
        events.append(p)

    class _FakeWS:
        close_code = 4009

        def __init__(self):
            self.sent: List[Dict[str, Any]] = []
            self._frames = [
                json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}}),
                json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1"}}),
                json.dumps(_c2c_payload() | {"s": 2}),
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._frames:
                raise StopAsyncIteration
            return self._frames.pop(0)

        async def send_json(self, obj):
            self.sent.append(obj)

        async def close(self):
            pass

    class _FakeSession:
        async def close(self):
            pass

    fake_ws = _FakeWS()

    async def ws_connect(url, headers):
        assert url == "wss://gw.example/websocket/"
        assert headers["Authorization"] == "QQBot TOK"
        return _FakeSession(), fake_ws

    async def fake_http(method, url, *, headers=None, payload=None, timeout=20.0):
        if url.endswith("/gateway"):
            return 200, {"url": "wss://gw.example/websocket/"}
        return 200, {"access_token": "TOK", "expires_in": 7200}

    monkeypatch.setattr(Q, "_http_json", fake_http)
    gw = Q.QQBotGateway(app_id="A", app_secret="S", sandbox=False, intents=1 << 25,
                        on_event=on_event, ws_connect=ws_connect)
    reason = await gw.run_once()
    assert reason == "closed:4009"
    assert fake_ws.sent[0]["op"] == 2
    assert len(events) == 1 and gw.last_seq == 2 and gw.fatal_code == 0
    assert gw.connected is False  # 连接结束后如实置 False


async def test_gateway_fatal_close_codes_stop_reconnect(monkeypatch):
    class _WS:
        close_code = 4915

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def send_json(self, obj):
            pass

        async def close(self):
            pass

    class _S:
        async def close(self):
            pass

    async def ws_connect(url, headers):
        return _S(), _WS()

    async def fake_http(method, url, **kw):
        if url.endswith("/gateway"):
            return 200, {"url": "wss://x"}
        return 200, {"access_token": "TOK", "expires_in": 7200}

    monkeypatch.setattr(Q, "_http_json", fake_http)
    gw = Q.QQBotGateway(app_id="A", app_secret="S", sandbox=True, intents=1,
                        on_event=lambda p: None, ws_connect=ws_connect)
    await gw.run()   # fatal → 立即退出，不重连
    assert gw.fatal_code == 4915 and "banned" in gw.last_error


# ── 入站处理：锚点 + 镜像 + 让位 / 自答 ─────────────────────────────────────

async def test_handle_event_notes_anchor_and_delegates(monkeypatch):
    seen: List[Dict[str, Any]] = []
    media: List[Dict[str, Any]] = []

    async def fake_inbound(**kw):
        seen.append(kw)
        return True   # System Z / 主管道已托管

    def fake_media(**kw):
        media.append(kw)
        return True

    import src.integrations.shared.official_inbound as OI
    monkeypatch.setattr(OI, "process_official_inbound", fake_inbound)
    monkeypatch.setattr(OI, "mirror_inbound_media", fake_media)
    led = Q.PassiveReplyLedger()
    n = await Q.handle_qqbot_event(_c2c_payload(), config=_cfg(), account_id="APP",
                                   use_pipeline=False, ledger=led)
    assert n == 1
    assert seen[0]["platform"] == "qqbot" and seen[0]["chat_key"] == "qqbot:c2c:OPEN1"
    assert seen[0]["msg_id"] == "ROBOT1.0_abc" and seen[0]["text"] == "你好 机器人"
    assert media[0]["media_type"] == "image" and media[0]["media_ref"].startswith("https://")
    assert led.status("qqbot:c2c:OPEN1")["remaining"] == Q.C2C_MAX_REPLIES


async def test_handle_event_self_answer_path_sends_passively(monkeypatch, token_ok):
    async def not_handed(**kw):
        return False

    import src.integrations.shared.official_inbound as OI
    monkeypatch.setattr(OI, "process_official_inbound", not_handed)
    monkeypatch.setattr(OI, "mirror_inbound_media", lambda **kw: True)
    mirrored: List[Dict[str, Any]] = []

    async def fake_mirror_out(**kw):
        mirrored.append(kw)
    monkeypatch.setattr(OI, "mirror_official_outbound", fake_mirror_out)
    sent: List[Dict[str, Any]] = []

    async def fake_http(method, url, *, headers=None, payload=None, timeout=20.0):
        sent.append(payload)
        return 200, {"id": "OUT"}
    monkeypatch.setattr(Q, "_http_json", fake_http)

    class _SM:
        async def process_message(self, text, user_id, context):
            assert user_id == "qqbot:OPEN1" and context["channel"] == "qqbot"
            return "回你一句"

    led = Q.PassiveReplyLedger()
    await Q.handle_qqbot_event(_c2c_payload(attachments=[]), config=_cfg(), account_id="APP",
                               use_pipeline=False, ledger=led, sm=_SM())
    assert sent and sent[0]["msg_id"] == "ROBOT1.0_abc" and sent[0]["msg_seq"] == 1
    assert mirrored and mirrored[0]["chat_key"] == "qqbot:c2c:OPEN1"


async def test_handle_event_group_message_not_self_answered(monkeypatch):
    async def not_handed(**kw):
        return False

    import src.integrations.shared.official_inbound as OI
    monkeypatch.setattr(OI, "process_official_inbound", not_handed)

    class _SM:
        async def process_message(self, **kw):
            raise AssertionError("群消息不该走自答")

    p = {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "id": "E",
         "d": {"author": {"member_openid": "M1"}, "group_openid": "G1", "content": " hi", "id": "g1"}}
    await Q.handle_qqbot_event(p, config=_cfg(), account_id="APP", use_pipeline=False,
                               ledger=Q.PassiveReplyLedger(), sm=_SM())


# ── 官方 worker 分支 ─────────────────────────────────────────────────────────

def test_worker_creds_and_factory():
    w = OfficialApiWorker({"platform": "qqbot", "account_id": "APP",
                           "meta": {"app_id": "APP", "app_secret": "S"}}, {})
    assert w._creds_ok() is True
    w2 = OfficialApiWorker({"platform": "qqbot", "account_id": "APP", "meta": {}}, _cfg())
    assert w2._creds() == {"app_id": "APP", "app_secret": "SEC"}
    assert OfficialApiWorker({"platform": "qqbot", "account_id": "x", "meta": {}}, {})._creds_ok() is False
    f = official_worker_factory("qqbot")
    assert isinstance(f({"platform": "qqbot", "account_id": "APP", "meta": {}}, _cfg()),
                      QQBotOfficialWorker)
    assert type(official_worker_factory("zalo")({"platform": "zalo", "account_id": "o", "meta": {}}, {})) is OfficialApiWorker


async def test_worker_send_routes_to_qqbot_and_marks_quote(monkeypatch):
    calls: List[Dict[str, Any]] = []

    async def fake_send(chat_key, text, *, config, meta=None, account_id="official",
                        reply_to_msg_id="", check_kill_switch=True, ledger=None):
        calls.append({"chat_key": chat_key, "text": text, "ref": reply_to_msg_id,
                      "account_id": account_id})
        return {"ok": True, "data": {"id": "OUT9"}}

    monkeypatch.setattr(Q, "qqbot_send_text", fake_send)
    w = official_worker_factory("qqbot")({"platform": "qqbot", "account_id": "APP", "meta": {}}, _cfg())
    res = await w.send("qqbot:c2c:U1", "hi", reply_to={"id": "IN1", "text": "q"})
    assert res["delivered"] is True and res["message_id"] == "OUT9" and res["quote_applied"] is True
    assert calls[0]["ref"] == "IN1" and calls[0]["account_id"] == "APP"
    res2 = await w.send("qqbot:c2c:U1", "hi")
    assert res2["quote_applied"] is False


async def test_worker_send_blocked_surfaces_window_kind(monkeypatch):
    async def blocked(chat_key, text, **kw):
        return {"ok": False, "error": "no anchor", "error_kind": "window_expired",
                "blocked": "qq_passive_window", "retriable": False}
    monkeypatch.setattr(Q, "qqbot_send_text", blocked)
    w = QQBotOfficialWorker({"platform": "qqbot", "account_id": "APP", "meta": {}}, _cfg())
    res = await w.send("qqbot:c2c:U1", "hi")
    assert res["delivered"] is False and res["error_kind"] == "window_expired"
    assert res["blocked"] == "qq_passive_window"


async def test_qqbot_media_caps_agree_with_runtime():
    caps = official_send_caps("qqbot", {})
    assert caps["can_media"] is False and caps["can_voice"] is False
    assert caps["reason"] == "qqbot_media_pending"
    w = OfficialApiWorker({"platform": "qqbot", "account_id": "APP", "meta": {}}, _cfg())
    out = await w.send_media("qqbot:c2c:U1", media_path="x.jpg", media_type="image")
    assert out["delivered"] is False and out["error_kind"] == "not_supported"


async def test_qqbot_worker_webhook_mode_is_stateless_and_healthy():
    w = QQBotOfficialWorker({"platform": "qqbot", "account_id": "APP", "meta": {}},
                            _cfg(connect_mode="webhook"))
    await w.start()
    assert w.state == "running" and await w.healthy() is True
    st = w.status()
    assert st["connect_mode"] == "webhook" and "passive_ledger" in st
    await w.stop()
    assert w.state == "stopped"


async def test_qqbot_worker_websocket_mode_spawns_gateway(monkeypatch):
    started = {"n": 0}

    async def fake_run(self):
        started["n"] += 1
        self.connected = True
        try:
            while True:
                await __import__("asyncio").sleep(3600)
        except __import__("asyncio").CancelledError:
            raise

    monkeypatch.setattr(Q.QQBotGateway, "run", fake_run)
    w = QQBotOfficialWorker({"platform": "qqbot", "account_id": "APP", "meta": {}}, _cfg())
    await w.start()
    await __import__("asyncio").sleep(0)
    assert started["n"] == 1 and w.detail == "websocket"
    assert await w.healthy() is True
    assert w.status()["gateway"]["connected"] is True
    # 致命码 → 不健康且带原因
    w._gateway.fatal_code = 4915
    w._gateway.last_error = "bot banned (4915)"
    assert await w.healthy() is False and "banned" in w.detail
    await w.stop()
    assert w._task is None


# ── 账号门禁：被动窗口拒发不算「通道故障」 ─────────────────────────────────

def test_passive_window_block_does_not_degrade_account(monkeypatch, tmp_path):
    """QQ 机器人被动窗口用尽 → error_kind=window_expired；连拦 N 次也不得把账号降成
    半自动 + 红标「通道异常」（M-2 D-M1 ⑦ 的连续失败降级只针对真实通道失败）。"""
    from src.inbox import account_channel_gate as G
    # 持久化落 tmp（conftest 已把 AITR_DATA_DIR 指向进程级 tmp，这里再钉一层防串写）
    monkeypatch.setattr(G, "_state_path", lambda: tmp_path / "gate.json")
    assert "window_expired" in G.POLICY_ERROR_KINDS
    plat, acct = "qqbot", f"APP{tmp_path.name}"
    for _ in range(5):
        r = G.note_send_fail(plat, acct, error_kind="window_expired")
        assert r["policy_block"] is True and r["degraded_now"] is False
    assert r["streak"] == 0
    assert G.degraded_state(plat, acct) is None
    # 真实失败仍照旧计数（不因本改动放水）
    r2 = G.note_send_fail(plat, acct, error_kind="exception")
    assert r2["streak"] == 1 and "policy_block" not in r2


# ── 登记面完整性 ─────────────────────────────────────────────────────────────

def test_qqbot_registered_everywhere():
    assert "qqbot" in SUPPORTED_PLATFORMS
    assert DEFAULT_PLATFORM_MODES["qqbot"] == {"modes": ["official"], "default": "official"}
    assert login_kind("qqbot", "official") == "credentials"
    assert ("qqbot", "official") in PR._IMPLEMENTED_MODES
    assert "qqbot" in OFFICIAL_PLATFORMS
    assert PLATFORM_INSTRUCTION_KEYS["qqbot"] == "inbox.connect.instr_qqbot"
    modes = list_modes("qqbot")
    assert [m["mode"] for m in modes] == ["official"] and modes[0]["login_kind"] == "credentials"
    from src.utils.account_scope_migration import KNOWN_PLATFORMS as K1
    from src.utils.episodic_identity_display import KNOWN_PLATFORMS as K2
    assert "qqbot" in K1 and "qqbot" in K2
    from src.integrations import official_webhook_stats as ows
    assert ows.PLATFORM_CONFIG_BLOCKS["qqbot"] == "qqbot" and ows.HAS_GET_VERIFY["qqbot"] is False
    assert ows.creds_ok("qqbot", _cfg()) is True and ows.creds_ok("qqbot", {}) is False


def test_qqbot_channel_fields_match_module_config_reads():
    ch = get_channel("qqbot")
    assert ch is not None, "qqbot 渠道声明缺席（向导不会出现该卡片）"
    assert ch.enable_key == "qqbot.enabled"
    assert ch.official_platform == "qqbot" and ch.official_account_id_key == "qqbot.app_id"
    assert ch.console_url == Q.QQBOT_CONSOLE_URL
    src = (_ROOT / "src" / "integrations" / "qq_official.py").read_text(encoding="utf-8")
    for f in ch.fields:
        block, _, key = f.key.partition(".")
        assert block == "qqbot", f.key
        assert f'cfg.get("{key}")' in src, (
            f"向导字段 {f.key} 在 qq_official.py 里找不到对应的 cfg.get —— 键名漂移，填了也不通")
    # 无登录路径（个人号是另一个平台 qq）、api 路径是真通道、空配置不误报就绪
    st = next(c for c in channel_status({}) if c["id"] == "qqbot")
    assert st["paths"]["login"] is None
    assert st["paths"]["api"]["is_transport"] is True and st["ready"] is False
    ready = next(c for c in channel_status(_cfg()) if c["id"] == "qqbot")
    assert ready["ready"] is True and ready["ready_by"] == "api"


def test_qqbot_provision_official_account_uses_app_id():
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_setup_routes import _provision_official_account
    assert _provision_official_account("qqbot", _cfg()) == "APP"
    row = get_account_registry().get("qqbot", "APP")
    assert row is not None and row.get("mode") == "official" and row.get("status") == "online"
    assert _provision_official_account("qqbot", {"qqbot": {"app_id": "onlyid"}}) == ""


def test_qqbot_readiness_points_to_wizard_then_ready():
    d = PR.diagnose_mode("qqbot", "official", {})
    assert d["ready"] is False and d["reason_code"] == PR.BLOCK_OFFICIAL_CREDS
    blk = next(b for b in d["blockers"] if b["code"] == PR.BLOCK_OFFICIAL_CREDS)
    assert "AppID" in str((blk.get("params") or {}).get("field") or "")
    ok = PR.diagnose_mode("qqbot", "official", _cfg())
    assert not [b for b in ok["blockers"] if b["severity"] == PR.SEV_BLOCK]


def test_frontend_tables_and_console_fallback_carry_qqbot():
    tpl = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    assert "qqbot:'https://q.qq.com/'" in tpl
    assert "'qqbot'" in tpl.split("const FIXED_PLATS", 1)[1].split("]", 1)[0]
    assert "'qqbot'" in tpl.split("const _CONNECT_PLATS", 1)[1].split("]", 1)[0]
    assert "if(r==='qqbot_media_pending')" in tpl
    wiz = (_ROOT / "src" / "web" / "templates" / "setup_wizard.html").read_text(encoding="utf-8")
    assert "qqbot:1" in wiz
    icons = (_ROOT / "src" / "web" / "static" / "platform_icons.js").read_text(encoding="utf-8")
    assert "qqbot:" in icons and "pfg-qqb" in icons and "qq:" in icons


def test_i18n_keys_for_qqbot_exist_bilingually():
    from src.web.web_i18n import get_translations
    needed = (
        "inbox.plat.qqbot_desc", "inbox.acct.note_qqbot", "inbox.connect.instr_qqbot",
        "inbox.connect.win_note_qqbot", "inbox.caps.qqbot_media_pending",
    )
    for lang in ("zh", "en"):
        tr = get_translations(lang)
        for key in needed:
            assert key in tr, f"{lang} 缺 i18n 键 {key}"


def test_admin_registers_qqbot_routes():
    src = (_ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    assert "register_qqbot_routes(app, config_manager, telegram_client)" in src


# ── Webhook 路由端到端（op=13 验证 / 验签 / 事件） ────────────────────────────

def _make_app(monkeypatch):
    from fastapi import FastAPI

    class _CM:
        config = {"qqbot": {"enabled": True, "app_id": "11111111", "app_secret": _DOC_SECRET,
                            "connect_mode": "webhook"}}

    class _TC:
        skill_manager = None

    app = FastAPI()
    Q.register_qqbot_routes(app, _CM(), _TC())
    return app


def test_webhook_validation_and_event_signature(monkeypatch):
    from starlette.testclient import TestClient
    from src.integrations import official_webhook_stats as ows
    ows.reset_for_tests(Path(__file__).resolve().parent / "_tmp_qqbot_ows.json")
    app = _make_app(monkeypatch)
    assert app.state.qqbot_webhook_path == "/qqbot/webhook"
    handled: List[Dict[str, Any]] = []

    async def fake_handle(payload, **kw):
        handled.append(payload)
        return 1
    monkeypatch.setattr(Q, "handle_qqbot_event", fake_handle)

    with TestClient(app) as c:
        r = c.post("/qqbot/webhook", content=json.dumps(
            {"d": {"plain_token": _DOC_PLAIN, "event_ts": _DOC_TS}, "op": 13}).encode())
        assert r.status_code == 200
        assert r.json() == {"plain_token": _DOC_PLAIN, "signature": _DOC_SIG}
        body = json.dumps(_c2c_payload()).encode("utf-8")
        bad = c.post("/qqbot/webhook", content=body,
                     headers={"X-Signature-Ed25519": "00", "X-Signature-Timestamp": "1"})
        assert bad.status_code == 401 and not handled
        ts = "1725442341"
        sig = Q._private_key(_DOC_SECRET).sign(ts.encode() + body).hex()
        ok = c.post("/qqbot/webhook", content=body,
                    headers={"X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts})
        assert ok.status_code == 200 and ok.json() == {"op": 12}
        assert len(handled) == 1 and handled[0]["t"] == "C2C_MESSAGE_CREATE"
        assert c.post("/qqbot/webhook", content=b"not json").status_code == 400
    snap = ows.snapshot().get("qqbot") or {}
    assert int(snap.get("events_total") or 0) >= 2
    assert snap.get("last_error_kind") in ("bad_signature", "bad_json")
    ows.reset_for_tests(None)
    try:
        (Path(__file__).resolve().parent / "_tmp_qqbot_ows.json").unlink()
    except OSError:
        pass


def test_register_skips_when_disabled_or_missing_creds():
    from fastapi import FastAPI

    class _CM:
        config = {"qqbot": {"enabled": True, "app_id": "", "app_secret": ""}}

    app = FastAPI()
    Q.register_qqbot_routes(app, _CM(), None)
    assert not hasattr(app.state, "qqbot_webhook_path")

    class _CM2:
        config = {"qqbot": {"enabled": False, "app_id": "a", "app_secret": "b"}}

    app2 = FastAPI()
    Q.register_qqbot_routes(app2, _CM2(), None)
    assert not hasattr(app2.state, "qqbot_webhook_path")
