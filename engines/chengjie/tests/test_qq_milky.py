# -*- coding: utf-8 -*-
"""QQ 协议登录（个人号，platform=qq / mode=protocol，经 Milky 接口）门禁（2026-09-07 QQ 双轨·准入区轨）。

守四件事：
1. **Milky 契约**：``/api/{api}`` POST + Bearer、``{status,retcode,data}`` 语义（-403=未登录）、
   ``/event`` WS 事件流；消息段 ↔ 文本/媒体渲染与官方 IncomingSegment 定义逐字对齐。
2. **worker 行为**：入站落库（群/私聊/临时会话分流、已读 seq 记账、@提及、引用）、出站文字/媒体、
   已读、群管理（GROUP_ADMIN_METHODS 契约名）、未登录 → needs_login 会话健康登记。
3. **登录 provider**：协议端可达且已登录 → authorized + 注册表 meta 快照；未登录 → pending；
   不可达 → reason_code 早退。
4. **登记面完整**：platform_login / readiness / capabilities / channel_setup / 前端常驻清单 / i18n /
   参照系清单 / pacing / actions / group_show 一处不漏。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.integrations import platform_capabilities as PC
from src.integrations import platform_readiness as PR
from src.integrations import qq_milky as M
from src.integrations import qq_protocol_login as L
from src.integrations.platform_login import (
    DEFAULT_PLATFORM_MODES,
    PLATFORM_INSTRUCTION_KEYS,
    SUPPORTED_PLATFORMS,
    list_modes,
    login_kind,
)
from src.utils.channel_setup import channel_status, get_channel

_ROOT = Path(__file__).resolve().parents[1]


def _cfg(**over) -> Dict[str, Any]:
    qq = {"protocol_enabled": True, "milky_url": "http://127.0.0.1:3000", "milky_token": "TOK"}
    qq.update(over)
    return {"platform_login": {"qq": qq, "orchestrator_enabled": True}}


class _FakeHttp:
    """按 api 名返回预设响应的 Milky 假 HTTP；记录全部调用。"""

    def __init__(self, table: Dict[str, Any]) -> None:
        self.table = table
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, method, url, *, headers, payload, timeout):
        api = url.rsplit("/api/", 1)[-1]
        self.calls.append({"api": api, "url": url, "headers": headers, "payload": payload})
        res = self.table.get(api)
        if callable(res):
            res = res(payload)
        if res is None:
            return 404, {"status": "failed", "retcode": -404, "message": "no such api"}
        if isinstance(res, tuple):
            return res
        return 200, {"status": "ok", "retcode": 0, "data": res}


# ── 配置 / chat_key ──────────────────────────────────────────────────────────

def test_protocol_switch_is_tri_state_and_not_desktop_default(monkeypatch):
    from src.integrations.platform_login import _DESKTOP_LOGIN_DEFAULT_ON
    assert "platform_login.qq.protocol_enabled" not in _DESKTOP_LOGIN_DEFAULT_ON, \
        "非官方接入不得随桌面默认开"
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    assert M.protocol_enabled({}) is False
    assert M.protocol_enabled(_cfg()) is True
    assert M.protocol_enabled(_cfg(protocol_enabled=False)) is False


def test_service_url_token_meta_precedence():
    cfg = _cfg()
    assert M.service_base_url(cfg) == "http://127.0.0.1:3000"
    assert M.service_token(cfg) == "TOK"
    meta = {"milky_url": "http://10.0.0.5:3001/", "milky_token": "T2"}
    assert M.service_base_url(cfg, meta) == "http://10.0.0.5:3001"
    assert M.service_token(cfg, meta) == "T2"
    assert M.service_base_url({}) == M.DEFAULT_MILKY_URL


def test_chat_key_roundtrip():
    assert M.make_chat_key("friend", 123) == "qq:friend:123"
    assert M.make_chat_key("group", "999") == "qq:group:999"
    assert M.make_chat_key("bogus", 1) == "qq:friend:1"
    assert M.parse_chat_key("qq:group:999") == ("group", "999")
    assert M.parse_chat_key("qq:temp:5") == ("temp", "5")
    assert M.parse_chat_key("123456") == ("friend", "123456")
    assert M.avatar_url_for(10001) == "https://q1.qlogo.cn/g?b=qq&nk=10001&s=640"
    assert M.avatar_url_for("abc") == ""


# ── Milky 客户端 ─────────────────────────────────────────────────────────────

async def test_client_call_contract_and_errors():
    http = _FakeHttp({
        "get_login_info": {"uin": 10001, "nickname": "小Q"},
        "send_private_message": lambda p: {"message_seq": 77, "time": 1} if p["user_id"] == 1 else
        (200, {"status": "failed", "retcode": -404, "message": "好友不存在"}),
        "not_logged": (200, {"status": "failed", "retcode": -403, "message": "未处于登录状态"}),
        "unauth": (401, {}),
        "boom": (500, "oops"),
    })
    c = M.MilkyClient("http://x:3000/", "TOK", http=http)
    assert await c.call("get_login_info") == {"uin": 10001, "nickname": "小Q"}
    assert http.calls[0]["url"] == "http://x:3000/api/get_login_info"
    assert http.calls[0]["headers"]["Authorization"] == "Bearer TOK"
    assert http.calls[0]["payload"] == {}
    assert (await c.call("send_private_message", {"user_id": 1, "message": []}))["message_seq"] == 77
    with pytest.raises(M.MilkyError) as ei:
        await c.call("send_private_message", {"user_id": 2, "message": []})
    assert ei.value.retcode == -404 and not ei.value.not_logged_in
    with pytest.raises(M.MilkyError) as ei2:
        await c.call("not_logged")
    assert ei2.value.not_logged_in is True
    with pytest.raises(M.MilkyError) as ei3:
        await c.call("unauth")
    assert ei3.value.retcode == -401
    with pytest.raises(RuntimeError):
        await c.call("boom")
    assert c.ws_url() == "ws://x:3000/event"
    assert M.MilkyClient("https://h/").ws_url() == "wss://h/event"


# ── 消息段渲染（对齐官方 IncomingSegment） ────────────────────────────────────

def test_render_segments_text_mention_face_reply_media():
    segs = [
        {"type": "reply", "data": {"message_seq": 41}},
        {"type": "mention", "data": {"user_id": 10001, "name": "小Q"}},
        {"type": "text", "data": {"text": "看这个"}},
        {"type": "face", "data": {"face_id": "14"}},
        {"type": "image", "data": {"resource_id": "r1", "temp_url": "https://cdn/x.jpg",
                                   "sub_type": "normal"}},
        {"type": "record", "data": {"resource_id": "r2", "temp_url": "https://cdn/v.amr", "duration": 3}},
    ]
    r = M.render_segments(segs, self_id=10001)
    assert r["mentioned"] is True and r["reply_seq"] == 41
    assert r["text"] == "@小Q 看这个[表情][语音]"
    assert r["media"] == [{"media_type": "image", "url": "https://cdn/x.jpg", "resource_id": "r1"}]
    r2 = M.render_segments([{"type": "mention", "data": {"user_id": 5}}], self_id=10001)
    assert r2["mentioned"] is False and r2["text"] == "@5"
    r3 = M.render_segments([{"type": "image", "data": {"resource_id": "s", "temp_url": "u", "sub_type": "sticker"}}])
    assert r3["media"][0]["media_type"] == "sticker"
    r4 = M.render_segments([{"type": "file", "data": {"file_id": "f", "file_name": "a.pdf"}},
                            {"type": "forward", "data": {"forward_id": "x", "summary": "3 条"}}])
    assert r4["media"][0]["media_type"] == "file" and "[文件] a.pdf" in r4["text"] and "[合并转发]" in r4["text"]
    assert M.render_segments(None) == {"text": "", "mentioned": False, "media": [], "reply_seq": None}


def test_outgoing_segments_and_media_uri(tmp_path):
    assert M.build_text_segments("hi") == [{"type": "text", "data": {"text": "hi"}}]
    assert M.build_text_segments("hi", reply_seq=9)[0] == {"type": "reply", "data": {"message_seq": 9}}
    small = tmp_path / "a.jpg"
    small.write_bytes(b"\xff\xd8\xff" * 10)
    uri = M.media_uri(str(small))
    assert uri.startswith("base64://")
    big = tmp_path / "b.mp4"
    big.write_bytes(b"0" * 64)
    assert M.media_uri(str(big), inline_limit=10).startswith("file:///")
    segs = M.build_media_segments("voice", str(small), caption="忽略")
    assert segs == [{"type": "record", "data": {"uri": uri}}], "语音不带配文段"
    segs2 = M.build_media_segments("image", str(small), caption="看")
    assert segs2[0] == {"type": "text", "data": {"text": "看"}} and segs2[1]["type"] == "image"
    assert M.build_media_segments("sticker", str(small))[0]["data"]["sub_type"] == "sticker"
    with pytest.raises(ValueError):
        M.build_media_segments("file", str(small))


def test_normalize_message_event_scenes():
    ev = {"time": 1700000000, "self_id": 10001, "event_type": "message_receive",
          "data": {"message_scene": "group", "peer_id": 999, "message_seq": 5, "sender_id": 42,
                   "time": 1700000000, "segments": [{"type": "text", "data": {"text": "hi"}}],
                   "group": {"group_id": 999, "group_name": "测试群"},
                   "group_member": {"user_id": 42, "nickname": "阿强", "card": "强哥"}}}
    m = M.normalize_message_event(ev)
    assert m["scene"] == "group" and m["peer_id"] == "999" and m["sender_id"] == "42"
    assert m["name"] == "测试群" and m["sender_name"] == "强哥" and m["seq"] == 5
    assert m["avatar_url"].endswith("nk=42&s=640")
    fr = M.normalize_message_event({"event_type": "message_receive", "data": {
        "message_scene": "friend", "peer_id": 42, "sender_id": 42, "message_seq": 1,
        "segments": [], "friend": {"user_id": 42, "nickname": "阿强", "remark": "客户A"}}})
    assert fr["name"] == "客户A" and fr["sender_name"] == "客户A"
    assert M.normalize_message_event({"event_type": "bot_offline", "data": {}}) is None


# ── worker ───────────────────────────────────────────────────────────────────

def _worker(http: _FakeHttp, **meta) -> M.QQPersonalWorker:
    acc = {"platform": "qq", "account_id": "10001", "meta": {"uin": 10001, **meta}}
    w = M.QQPersonalWorker(acc, _cfg())
    w.client = M.MilkyClient(w.client.base_url, w.client.token, http=http)
    return w


async def test_worker_start_reports_authorized_and_status(monkeypatch):
    reports: List[Any] = []
    import src.integrations.platform_session_health as PSH
    monkeypatch.setattr(PSH, "report_session_transition",
                        lambda p, a, s, **kw: reports.append((p, a, s)) or {})
    http = _FakeHttp({"get_login_info": {"uin": 10001, "nickname": "小Q"},
                      "get_impl_info": {"impl_name": "NapCat", "impl_version": "4.17", "milky_version": "1.3"}})

    async def no_events(self, stop):
        if False:
            yield {}
    monkeypatch.setattr(M.MilkyClient, "events", no_events)
    w = _worker(http)
    await w.start()
    assert w.state == "running" and w.nickname == "小Q" and "NapCat" in w.detail
    assert ("qq", "10001", "authorized") in reports
    st = w.status()
    assert st["type"] == "qq_milky" and st["impl"]["impl_name"] == "NapCat"
    assert await w.healthy() is True   # TTL 内直接用 start 的结果
    await w.stop()
    assert w.state == "stopped" and w._task is None


async def test_worker_healthy_not_logged_in_reports_needs_login(monkeypatch):
    reports: List[Any] = []
    import src.integrations.platform_session_health as PSH
    monkeypatch.setattr(PSH, "report_session_transition",
                        lambda p, a, s, **kw: reports.append((p, a, s)) or {})
    http = _FakeHttp({"get_login_info": (200, {"status": "failed", "retcode": -403, "message": "未登录"})})
    w = _worker(http)
    assert await w.healthy() is False
    assert ("qq", "10001", "needs_login") in reports and "未登录" in w.detail
    http2 = _FakeHttp({"get_login_info": (0, {"error": "conn refused"})})
    w2 = _worker(http2)
    w2._health_ts = 0
    assert await w2.healthy() is False and "不可达" in w2.detail


async def test_worker_ingest_inbound_private_and_group(monkeypatch):
    emitted: List[Dict[str, Any]] = []
    replies: List[Dict[str, Any]] = []
    import src.integrations.protocol_bridge as PB
    monkeypatch.setattr(PB, "emit_incoming", lambda m: emitted.append(m))

    async def fake_auto(payload):
        replies.append(payload)
    monkeypatch.setattr(PB, "maybe_auto_reply", fake_auto)
    w = _worker(_FakeHttp({}))
    w._loop = asyncio.get_running_loop()
    persist_calls: List[Any] = []

    async def fake_persist(media_type, url, seq, *, file_name=""):
        persist_calls.append((media_type, url, seq))
        return ""   # 落盘失败 → 兜底保留临时 URL
    w._persist_media = fake_persist
    w._handle_event({"time": 1, "self_id": 10001, "event_type": "message_receive", "data": {
        "message_scene": "friend", "peer_id": 42, "sender_id": 42, "message_seq": 9, "time": 1,
        "segments": [{"type": "text", "data": {"text": "你好"}},
                     {"type": "image", "data": {"resource_id": "r", "temp_url": "https://cdn/p.jpg"}}],
        "friend": {"user_id": 42, "nickname": "阿强", "remark": ""}}})
    for _ in range(5):
        await asyncio.sleep(0)
    assert persist_calls == [("image", "https://cdn/p.jpg", 9)]
    assert len(emitted) == 1
    p = emitted[0]
    assert p["platform"] == "qq" and p["chat_key"] == "qq:friend:42" and p["name"] == "阿强"
    assert p["text"] == "你好" and p["media_type"] == "image" and p["media_ref"] == "https://cdn/p.jpg"
    assert p["msg_id"] == "9" and p["avatar_url"].endswith("nk=42&s=640") and "chat_type" not in p
    assert w._last_in_seq["qq:friend:42"] == 9
    assert replies and replies[0]["chat_key"] == "qq:friend:42"
    # 群：chat_type=group + 发言人字段 + @我 + 引用；群不触发私聊自动回复
    w._handle_event({"time": 2, "self_id": 10001, "event_type": "message_receive", "data": {
        "message_scene": "group", "peer_id": 999, "sender_id": 42, "message_seq": 3, "time": 2,
        "segments": [{"type": "reply", "data": {"message_seq": 1}},
                     {"type": "mention", "data": {"user_id": 10001, "name": "我"}},
                     {"type": "text", "data": {"text": "在？"}}],
        "group": {"group_id": 999, "group_name": "测试群"},
        "group_member": {"user_id": 42, "nickname": "阿强", "card": "强哥"}}})
    await asyncio.sleep(0)
    g = emitted[1]
    assert g["chat_key"] == "qq:group:999" and g["chat_type"] == "group" and g["name"] == "测试群"
    assert g["sender_id"] == "42" and g["sender_name"] == "强哥" and g["mentioned"] is True
    assert g["reply_to"] == {"id": "1"} and g["text"] == "@我 在？"
    assert len(replies) == 1
    # 自己发的（sender==uin）不入站；bot_offline → 会话健康登出
    w._handle_event({"event_type": "message_receive", "self_id": 10001, "data": {
        "message_scene": "friend", "peer_id": 42, "sender_id": 10001, "message_seq": 10, "segments": []}})
    assert len(emitted) == 2
    reports: List[Any] = []
    import src.integrations.platform_session_health as PSH
    monkeypatch.setattr(PSH, "report_session_transition",
                        lambda p, a, s, **kw: reports.append((p, a, s)) or {})
    w._handle_event({"event_type": "bot_offline", "data": {"reason": "kicked"}})
    assert ("qq", "10001", "logged_out") in reports and w._health_ok is False


async def test_worker_send_text_media_read_and_group_admin(tmp_path):
    http = _FakeHttp({
        "send_private_message": {"message_seq": 100, "time": 1},
        "send_group_message": {"message_seq": 200, "time": 1},
        "mark_message_as_read": {},
        "kick_group_member": {},
        "set_group_name": {},
        "upload_private_file": {"file_id": "F1"},
        "upload_group_file": {"file_id": "F2"},
        "recall_private_message": {},
        "recall_group_message": (200, {"status": "failed", "retcode": -500, "message": "too late"}),
    })
    w = _worker(http)
    r = await w.send("qq:friend:42", "hi", reply_to={"id": "9"})
    assert r["delivered"] is True and r["message_id"] == "100" and r["quote_applied"] is True
    assert http.calls[-1]["payload"] == {"user_id": 42, "message": [
        {"type": "reply", "data": {"message_seq": 9}}, {"type": "text", "data": {"text": "hi"}}]}
    r2 = await w.send("qq:group:999", "hey")
    assert r2["message_id"] == "200" and http.calls[-1]["payload"]["group_id"] == 999
    assert (await w.send("qq:temp:5", "x"))["error_kind"] == "unsupported"
    assert (await w.send("qq:friend:abc", "x"))["delivered"] is False
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG" * 4)
    rm = await w.send_media("qq:friend:42", media_path=str(img), media_type="image", caption="看")
    assert rm["delivered"] is True and http.calls[-1]["payload"]["message"][1]["type"] == "image"
    # 文件类：Milky 无文件消息段 → 走 upload_*_file；配文另发一条文本
    doc = tmp_path / "报价.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    rf = await w.send_media("qq:friend:42", media_path=str(doc), media_type="document", caption="报价单")
    assert rf["delivered"] is True and rf["file_id"] == "F1" and rf["caption_delivered"] is True
    up = [c for c in http.calls if c["url"].endswith("/api/upload_private_file")][-1]["payload"]
    assert up["user_id"] == 42 and up["file_name"] == "报价.pdf" and up["file_uri"].startswith("base64://")
    assert http.calls[-1]["payload"] == {"user_id": 42, "message": [{"type": "text", "data": {"text": "报价单"}}]}
    rg_ = await w.send_media("qq:group:999", media_path=str(doc), media_type="file")
    assert rg_["delivered"] is True and rg_["file_id"] == "F2"
    assert http.calls[-1]["payload"]["parent_folder_id"] == "/" and http.calls[-1]["payload"]["group_id"] == 999
    assert (await w.send_media("qq:friend:42", media_path=str(img), media_type="location"))["error_kind"] == "not_supported"
    # 撤回（编排器 delete_messages 契约）：message_id=message_seq；私聊成功、群聊被拒如实回 reason
    rr = await w.delete_messages("qq:friend:42", ["100", "x"])
    assert rr == {"ok": True, "deleted": 1}
    assert http.calls[-1]["payload"] == {"user_id": 42, "message_seq": 100}
    rr2 = await w.delete_messages("qq:group:999", ["200"])
    assert rr2["ok"] is False and "too late" in rr2["reason"]
    assert (await w.delete_messages("qq:temp:5", ["1"]))["ok"] is False
    # 已读：只认记过的入站 seq
    assert await w.mark_read("qq:friend:42") is False
    w._last_in_seq["qq:friend:42"] = 9
    assert await w.mark_read("qq:friend:42") is True
    assert http.calls[-1]["payload"] == {"message_scene": "friend", "peer_id": 42, "message_seq": 9}
    # 群管理契约名（GROUP_ADMIN_METHODS）
    assert await w.kick_group_member("qq:group:999", 42) is True
    assert http.calls[-1]["payload"] == {"group_id": 999, "user_id": 42, "reject_add_request": False}
    assert await w.rename_group("qq:group:999", " 新群名 ") is True
    assert http.calls[-1]["payload"] == {"group_id": 999, "new_group_name": "新群名"}
    assert await w.kick_group_member("qq:friend:42", 1) is False


async def test_worker_send_not_logged_in_reports(monkeypatch):
    reports: List[Any] = []
    import src.integrations.platform_session_health as PSH
    monkeypatch.setattr(PSH, "report_session_transition",
                        lambda p, a, s, **kw: reports.append((p, a, s)) or {})
    http = _FakeHttp({"send_private_message": (200, {"status": "failed", "retcode": -403, "message": "未登录"})})
    w = _worker(http)
    r = await w.send("qq:friend:42", "hi")
    assert r["delivered"] is False and r["error_kind"] == "invalid_token"
    assert ("qq", "10001", "needs_login") in reports


async def test_worker_event_stream_reconnects_then_stops(monkeypatch):
    frames = [json.dumps({"time": 1, "self_id": 10001, "event_type": "message_receive", "data": {
        "message_scene": "friend", "peer_id": 42, "sender_id": 42, "message_seq": 1,
        "segments": [{"type": "text", "data": {"text": "a"}}], "friend": {"nickname": "x"}}})]
    rounds = {"n": 0}

    class _WS:
        def __init__(self, items):
            self._items = list(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

        async def close(self):
            pass

    class _S:
        async def close(self):
            pass

    async def ws_connect(url, headers):
        rounds["n"] += 1
        assert url == "ws://127.0.0.1:3000/event" and headers["Authorization"] == "Bearer TOK"
        if rounds["n"] == 1:
            return _S(), _WS(frames)
        raise ConnectionError("down")

    emitted: List[Dict[str, Any]] = []
    import src.integrations.protocol_bridge as PB
    monkeypatch.setattr(PB, "emit_incoming", lambda m: emitted.append(m))

    async def fake_auto(payload):
        return None
    monkeypatch.setattr(PB, "maybe_auto_reply", fake_auto)
    monkeypatch.setattr(M.QQPersonalWorker, "BACKOFF_BASE", 0.01)
    monkeypatch.setattr(M.QQPersonalWorker, "BACKOFF_MAX", 0.02)
    w = _worker(_FakeHttp({}))
    w.client._ws_connect = ws_connect
    w._loop = asyncio.get_running_loop()
    w._stop = asyncio.Event()
    task = asyncio.create_task(w._run_events())
    # 首轮含 protocol_bridge 等惰性 import（冷启 ~150ms+，机器忙时更久）：轮询到第二轮连接
    # 发生为止（上限 5s），不用固定 sleep 赌时序
    deadline = time.time() + 5.0
    while rounds["n"] < 2 and time.time() < deadline:
        await asyncio.sleep(0.02)
    w._stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    assert emitted and emitted[0]["chat_key"] == "qq:friend:42"
    assert w.events_total == 1 and w.reconnects >= 1 and rounds["n"] >= 2


# ── 登录 provider ───────────────────────────────────────────────────────────

async def test_probe_login_states():
    ok = M.MilkyClient("http://x", http=_FakeHttp({"get_login_info": {"uin": 10001, "nickname": "小Q"}}))
    assert (await L.probe_login(ok))["state"] == "authorized"
    nl = M.MilkyClient("http://x", http=_FakeHttp(
        {"get_login_info": (200, {"status": "failed", "retcode": -403, "message": "未登录"})}))
    assert (await L.probe_login(nl))["state"] == "not_logged_in"
    down = M.MilkyClient("http://x", http=_FakeHttp({"get_login_info": (0, {"error": "refused"})}))
    assert (await L.probe_login(down))["state"] == "down"


async def test_provider_authorizes_and_persists_meta(monkeypatch):
    http = _FakeHttp({"get_login_info": {"uin": 10001, "nickname": "小Q"}})
    monkeypatch.setattr(L, "_client_factory", lambda cfg, meta=None: M.MilkyClient("http://x", "TOK", http=http))
    enriched: List[Dict[str, Any]] = []

    async def fake_enrich(platform, aid, *, name, avatar_url, config):
        enriched.append({"platform": platform, "aid": aid, "name": name, "avatar": avatar_url})
    import src.integrations.account_self_profile as ASP
    monkeypatch.setattr(ASP, "enrich_from_fields", fake_enrich)
    prov = L.make_provider(_cfg())
    info = await prov(None, "qq", "protocol", "")
    assert info["instruction_key"] == "inbox.connect.instr_qq" and callable(info["poll"])
    res = await info["poll"](None)
    assert res["status"] == "authorized" and res["account_id"] == "10001" and res["detail"] == "小Q"
    from src.integrations.account_registry import get_account_registry
    row = get_account_registry().get("qq", "10001")
    assert row is not None and row.get("mode") == "protocol" and row.get("status") == "online"
    meta = row.get("meta") or {}
    assert meta.get("milky_url") == "http://127.0.0.1:3000" and meta.get("milky_token") == "TOK"
    assert meta.get("uin") == "10001" and meta.get("nickname") == "小Q"
    assert enriched and enriched[0]["aid"] == "10001" and enriched[0]["avatar"].endswith("nk=10001&s=640")


async def test_provider_pending_when_not_logged_in_and_early_exit_when_down(monkeypatch):
    http = _FakeHttp({"get_login_info": (200, {"status": "failed", "retcode": -403, "message": "未登录"})})
    monkeypatch.setattr(L, "_client_factory", lambda cfg, meta=None: M.MilkyClient("http://x", http=http))
    info = await L.make_provider(_cfg())(None, "qq", "protocol", "")
    assert "reason_code" not in info
    res = await info["poll"](None)
    assert res["status"] == "pending" and "尚未登录" in res["detail"]
    down = _FakeHttp({"get_login_info": (0, {"error": "refused"})})
    monkeypatch.setattr(L, "_client_factory", lambda cfg, meta=None: M.MilkyClient("http://x", http=down))
    info2 = await L.make_provider(_cfg())(None, "qq", "protocol", "")
    assert info2["reason_code"] == L.REASON_SERVICE_DOWN and "poll" not in info2
    assert info2["instruction_key"] == "inbox.connect.instr_qq_down"


def test_maybe_register_gated_by_switch(monkeypatch):
    from src.integrations import platform_login as PL
    monkeypatch.setattr(L, "_registered", False)
    monkeypatch.setattr(PL, "_PROVIDERS", dict(PL._PROVIDERS))
    assert L.maybe_register({}) is False
    assert L.maybe_register(_cfg()) is True
    assert PL.get_login_provider("qq", "protocol") is not None


# ── 登记面完整性 ─────────────────────────────────────────────────────────────

def test_qq_registered_everywhere():
    assert "qq" in SUPPORTED_PLATFORMS
    assert DEFAULT_PLATFORM_MODES["qq"] == {"modes": ["protocol"], "default": "protocol"}
    assert login_kind("qq", "protocol") == "device", "扫码在协议端里完成，本窗口只等账号上线"
    assert ("qq", "protocol") in PR._IMPLEMENTED_MODES
    assert PLATFORM_INSTRUCTION_KEYS["qq"] == "inbox.connect.instr_qq"
    modes = list_modes("qq", {"protocol_enabled": True})
    assert [m["mode"] for m in modes] == ["protocol"]
    m = modes[0]
    assert m["label_key"] == "inbox.connect.mode_l_qq_protocol" and m["login_kind"] == "device"
    assert m["notice"] == {"key": "inbox.connect.notice_unofficial", "severity": "info"}
    # 能力矩阵：worker 登记 + 入站接线点可找到 + 群/媒体接线为真 + typing 属协议层硬限制
    assert ("qq", "protocol", "src.integrations.qq_milky", "QQPersonalWorker") in PC.WORKERS
    assert PC.inbound_media_wired("qq:protocol") is True
    assert PC.group_inbound_wired("qq:protocol") is True
    assert ("qq", "typing") in PC.HARD_LIMITS
    row = PC.capability_matrix({}).get("qq:protocol")
    assert row and row["available"] is True
    assert row["caps"] == {"send_text": True, "send_media": True, "mark_read": True, "typing": False}
    assert set(row["group_admin"]) == {"踢人", "改名"} and row["group_send"] is True
    from src.inbox.reply_pacing_settings import PLATFORMS
    from src.assistant.actions import ACTION_PLATFORMS
    from src.companion.group_show.platform_policy import KNOWN_PLATFORMS as GS
    from src.utils.account_scope_migration import KNOWN_PLATFORMS as K1
    from src.utils.episodic_identity_display import KNOWN_PLATFORMS as K2
    from src.integrations.platform_session_health import SIDECAR_PLATFORMS
    for name, coll in (("pacing", PLATFORMS), ("actions", ACTION_PLATFORMS), ("group_show", GS),
                       ("scope_migration", K1), ("identity_display", K2), ("sidecar", SIDECAR_PLATFORMS)):
        assert "qq" in coll, f"{name} 清单漏了 qq"


def test_qq_channel_card_and_readiness():
    ch = get_channel("qq")
    assert ch is not None and ch.login_platform == "qq" and ch.login_required is True
    assert ch.enable_key == "platform_login.qq.protocol_enabled"
    assert ch.login_notice_key == "inbox.connect.notice_unofficial" and ch.official_platform == ""
    keys = {f.key for f in ch.fields}
    assert keys == {"platform_login.qq.milky_url", "platform_login.qq.milky_token"}
    src = (_ROOT / "src" / "integrations" / "qq_milky.py").read_text(encoding="utf-8")
    for f in ch.fields:
        leaf = f.key.rsplit(".", 1)[-1]
        assert f'.get("{leaf}")' in src, f"向导字段 {f.key} 在 qq_milky.py 找不到对应读取"
    st = next(c for c in channel_status({}) if c["id"] == "qq")
    assert st["paths"]["login"] is not None and st["paths"]["api"]["is_transport"] is False
    assert st["ready"] is False
    on = next(c for c in channel_status(_cfg(), accounts_by_platform={"qq": 1}) if c["id"] == "qq")
    assert on["ready"] is True and on["ready_by"] == "login"
    d = PR.diagnose_mode("qq", "protocol", {})
    assert d["ready"] is False and d["reason_code"] == PR.BLOCK_NEEDS_SERVER_SETUP
    d2 = PR.diagnose_mode("qq", "protocol", _cfg(), service_ok=False)
    assert d2["reason_code"] == PR.BLOCK_SERVICE_DOWN
    d3 = PR.diagnose_mode("qq", "protocol", _cfg(), service_ok=True)
    assert d3["ready"] is True
    assert PR.service_probe_targets(_cfg())["qq"] == "http://127.0.0.1:3000"
    assert "qq" not in PR.service_probe_targets({})


async def test_diagnostics_probe_treats_rejections_as_reachable(monkeypatch):
    from src.integrations import protocol_diagnostics as PD
    assert "qq" in PD._SERVICE_PROBES
    monkeypatch.setattr(M, "_default_http", _FakeHttp({"get_impl_info": {"impl_name": "x"}}))
    assert await PD.check_qq_milky_reachable(_cfg()) is True
    monkeypatch.setattr(M, "_default_http", _FakeHttp(
        {"get_impl_info": (200, {"status": "failed", "retcode": -403, "message": "未登录"})}))
    assert await PD.check_qq_milky_reachable(_cfg()) is True
    monkeypatch.setattr(M, "_default_http", _FakeHttp({"get_impl_info": (0, {"error": "refused"})}))
    assert await PD.check_qq_milky_reachable(_cfg()) is False


def test_orchestrator_registers_qq_worker_when_enabled():
    from src.integrations import account_orchestrator as AO
    AO._WORKER_FACTORIES.pop("qq:protocol", None)
    AO.ensure_builtin_workers({})
    assert AO.get_worker_factory("qq", "protocol") is None
    AO.ensure_builtin_workers(_cfg())
    fac = AO.get_worker_factory("qq", "protocol")
    assert fac is not None
    w = fac({"platform": "qq", "account_id": "1", "meta": {}}, _cfg())
    assert isinstance(w, M.QQPersonalWorker)
    AO._WORKER_FACTORIES.pop("qq:protocol", None)


def test_frontend_and_i18n_carry_qq():
    tpl = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    fixed = tpl.split("const FIXED_PLATS", 1)[1].split("]", 1)[0]
    assert "'qq'" in fixed and "'qqbot'" in fixed
    assert "qq:window.T('inbox.plat.qq_desc')" in tpl and "qq:window.T('inbox.acct.note_qq')" in tpl
    assert "'qq'" in tpl.split("const _CONNECT_PLATS", 1)[1].split("]", 1)[0]
    from src.web.web_i18n import get_translations
    needed = ("inbox.plat.qq_desc", "inbox.acct.note_qq", "inbox.connect.instr_qq",
              "inbox.connect.instr_qq_setup", "inbox.connect.instr_qq_down",
              "inbox.connect.mode_l_qq_protocol", "inbox.connect.mode_d_qq_protocol")
    for lang in ("zh", "en"):
        tr = get_translations(lang)
        for key in needed:
            assert key in tr, f"{lang} 缺 i18n 键 {key}"
    routes = (_ROOT / "src" / "web" / "routes" / "unified_inbox_login_routes.py").read_text(encoding="utf-8")
    assert "qq_protocol_login import maybe_register" in routes
