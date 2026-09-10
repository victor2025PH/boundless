# -*- coding: utf-8 -*-
"""微信客服 worker / 注册门控 / 回调路由 门禁（实施97 线 A）。全部离线（假客户端 + 内存状态库）。"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox.kf_window_guard import KfStateStore
from src.integrations import wechat_kf as WK
from src.integrations.wechat_kf_worker import (
    WeChatKfWorker, register_wechat_kf_worker, wechat_kf_enabled,
)


def _ok(data=None):
    return {"ok": True, "errcode": 0, "errmsg": "", "error_kind": "", "data": data or {}}


def _fail(msg="boom", kind="api_error", code=1):
    return {"ok": False, "errcode": code, "errmsg": msg, "error_kind": kind, "data": {}}


class FakeClient:
    """脚本化假客户端：``sync_pages`` 逐次弹出；记录所有调用。"""

    def __init__(self):
        self.calls: List[Any] = []
        self.token_ok = True
        self.sync_pages: List[Dict[str, Any]] = []
        self.accounts = [{"open_kfid": "wkKF", "manage_privilege": 1}]
        self.customers = {"wmU1": {"nickname": "小明", "avatar": "https://a/b.png"}}
        self.media_ok = True

    async def get_token(self, force=False):
        self.calls.append(("get_token", force))
        return _ok({"access_token": "T"}) if self.token_ok else _fail("bad secret", "invalid_secret", 40001)

    async def list_accounts(self, offset=0, limit=100):
        self.calls.append(("list_accounts",))
        return _ok({"account_list": self.accounts})

    async def sync_msg(self, open_kfid, *, cursor="", token="", limit=1000, voice_format=0):
        self.calls.append(("sync_msg", open_kfid, cursor, token))
        if not self.sync_pages:
            return _ok({"msg_list": [], "next_cursor": cursor, "has_more": 0})
        page = self.sync_pages.pop(0)
        if page.get("_error"):
            return _fail(page["_error"])
        return _ok(page)

    async def batch_get_customers(self, ids, need_context=True):
        self.calls.append(("batch_get_customers", tuple(ids)))
        out = []
        for i in ids:
            if i in self.customers:
                out.append({"external_userid": i, **self.customers[i]})
        return _ok({"customer_list": out})

    async def download_media(self, media_id):
        self.calls.append(("download_media", media_id))
        if not self.media_ok:
            return _fail("invalid media_id", code=40007)
        return _ok({"bytes": b"\xff\xd8JPEG", "content_type": "image/jpeg", "filename": ""})

    async def send_text(self, uid, open_kfid, text, *, msgid=""):
        self.calls.append(("send_text", uid, open_kfid, text))
        return _ok({"msgid": f"M-{len(self.calls)}"})

    async def upload_media(self, path, media_type):
        self.calls.append(("upload_media", path, media_type))
        return _ok({"media_id": "MID1"})

    async def send_media(self, uid, open_kfid, media_type, media_id):
        self.calls.append(("send_media", uid, open_kfid, media_type, media_id))
        return _ok({"msgid": "MM1"})

    async def send_msg_on_event(self, code, text):
        self.calls.append(("send_msg_on_event", code, text))
        return _ok({"msgid": "W1"})

    async def trans_service_state(self, open_kfid, uid, state, servicer_userid=""):
        self.calls.append(("trans", open_kfid, uid, state, servicer_userid))
        return _ok({"msg_code": "MC"})

    async def get_service_state(self, open_kfid, uid):
        self.calls.append(("state", open_kfid, uid))
        return _ok({"service_state": 1})


def _msg(**kw):
    d = {"msgid": kw.pop("msgid", "m1"), "open_kfid": "wkKF", "external_userid": "wmU1",
         "send_time": 1700000000, "origin": 3, "msgtype": "text", "text": {"content": "你好"}}
    d.update(kw)
    return d


def _mk(config=None, account=None, client=None):
    emitted: List[Dict[str, Any]] = []
    replied: List[Dict[str, Any]] = []

    async def _auto(payload):
        replied.append(payload)

    store = KfStateStore(":memory:")
    cl = client or FakeClient()
    w = WeChatKfWorker(
        account or {"account_id": "wkKF", "meta": {"corpid": "corp", "secret": "sec"}},
        config or {},
        client=cl, state_store=store, emit=emitted.append, auto_reply=_auto,
        sleep=lambda s: asyncio.sleep(0),
    )
    return w, cl, store, emitted, replied


# ── 注册门控 ───────────────────────────────────────────────────────────────

def test_enabled_switch_two_sources():
    assert not wechat_kf_enabled({})
    assert wechat_kf_enabled({"wechat_kf": {"enabled": True}})
    assert wechat_kf_enabled({"platform_login": {"wechat_kf": {"enabled": True}}})


def test_register_is_gated_and_idempotent():
    from src.integrations import account_orchestrator as AO
    AO._WORKER_FACTORIES.pop("wechat_kf:official", None)
    assert register_wechat_kf_worker({}) is False
    assert AO.get_worker_factory("wechat_kf", "official") is None
    assert register_wechat_kf_worker({"wechat_kf": {"enabled": True}}) is True
    f = AO.get_worker_factory("wechat_kf", "official")
    assert f is not None
    assert register_wechat_kf_worker({"wechat_kf": {"enabled": True}}) is True
    assert AO.get_worker_factory("wechat_kf", "official") is f
    w = f({"account_id": "wkKF", "meta": {}}, {"wechat_kf": {"corpid": "c", "secret": "s"}})
    assert isinstance(w, WeChatKfWorker) and w.open_kfid == "wkKF"
    # 能力面：有 send/send_media，刻意没有 mark_read / typing（协议层硬限制）
    assert hasattr(w, "send") and hasattr(w, "send_media")
    assert not hasattr(w, "mark_read") and not hasattr(w, "send_chat_action")
    AO._WORKER_FACTORIES.pop("wechat_kf:official", None)


# ── 启动 ────────────────────────────────────────────────────────────────────

async def test_start_requires_creds_and_valid_token():
    w, cl, *_ = _mk(account={"account_id": "official", "meta": {}})
    with pytest.raises(RuntimeError, match="corpid/secret"):
        await w.start()
    w2, cl2, *_ = _mk()
    cl2.token_ok = False
    with pytest.raises(RuntimeError, match="token"):
        await w2.start()
    assert w2.state == "stopped"


async def test_start_resolves_open_kfid_from_account_list(monkeypatch):
    w, cl, store, *_ = _mk(account={"account_id": "official",
                                    "meta": {"corpid": "corp", "secret": "sec"}})
    written = {}

    class _Reg:
        def upsert(self, platform, account_id, **kw):
            written[(platform, account_id)] = kw

    monkeypatch.setattr("src.integrations.account_registry.get_account_registry", lambda: _Reg())
    await w.start()
    try:
        assert w.open_kfid == "wkKF" and w.state == "running"
        assert written[("wechat_kf", "official")]["meta"] == {"open_kfid": "wkKF"}
        assert written[("wechat_kf", "official")]["merge_meta"] is True
        assert await w.healthy()
    finally:
        await w.stop()
    assert w.state == "stopped"


# ── 入站 ────────────────────────────────────────────────────────────────────

async def test_sync_customer_text_emits_payload_and_opens_window():
    w, cl, store, emitted, replied = _mk()
    cl.sync_pages = [{"msg_list": [_msg()], "next_cursor": "C1", "has_more": 0}]
    n = await w.sync_once()
    assert n == 1 and len(emitted) == 1 and len(replied) == 1
    p = emitted[0]
    assert p["platform"] == "wechat_kf" and p["account_id"] == "wkKF"
    assert p["chat_key"] == "wxkf:user:wmU1" and p["text"] == "你好" and p["direction"] == "in"
    assert p["msg_id"] == "m1" and p["name"] == "小明" and p["avatar_url"] == "https://a/b.png"
    assert p["source"]["message_id"] == "m1" and p["source"]["open_kfid"] == "wkKF"
    assert store.get_cursor("wkKF") == "C1"
    turn = store.get_turn("wkKF", "wmU1")
    assert turn and turn["last_inbound_ts"] == 1700000000 and turn["sent_since_inbound"] == 0
    # 同 msgid 再来（游标重放）→ 不重复投递
    cl.sync_pages = [{"msg_list": [_msg()], "next_cursor": "C1", "has_more": 0}]
    await w.sync_once()
    assert len(emitted) == 1
    # 客户资料只拉一次（进程内缓存）
    assert sum(1 for c in cl.calls if c[0] == "batch_get_customers") == 1


async def test_sync_follows_has_more_and_uses_callback_token_once():
    w, cl, store, emitted, _ = _mk()
    cl.sync_pages = [
        {"msg_list": [_msg(msgid="a")], "next_cursor": "C1", "has_more": 1},
        {"msg_list": [_msg(msgid="b")], "next_cursor": "C2", "has_more": 0},
    ]
    n = await w.sync_once(token="TOK")
    assert n == 2 and store.get_cursor("wkKF") == "C2"
    syncs = [c for c in cl.calls if c[0] == "sync_msg"]
    assert syncs[0][2:] == ("", "TOK") and syncs[1][2:] == ("C1", "")


async def test_sync_error_counts_and_keeps_cursor():
    w, cl, store, *_ = _mk()
    store.set_cursor("wkKF", "C0")
    cl.sync_pages = [{"_error": "frequency limit"}]
    assert await w.sync_once() == 0
    assert w.consecutive_errors == 1 and "frequency" in w.detail
    assert store.get_cursor("wkKF") == "C0"
    cl.sync_pages = [{"msg_list": [], "next_cursor": "C0", "has_more": 0}]
    await w.sync_once()
    assert w.consecutive_errors == 0


async def test_servicer_reply_mirrors_as_human_outbound_without_quota():
    w, cl, store, emitted, replied = _mk()
    cl.sync_pages = [{"msg_list": [
        _msg(msgid="c1"),
        _msg(msgid="s1", origin=5, servicer_userid="lisi", text={"content": "人工回复"}),
    ], "next_cursor": "C1", "has_more": 0}]
    await w.sync_once()
    outs = [p for p in emitted if p["direction"] == "out"]
    assert len(outs) == 1 and outs[0]["text"] == "人工回复"
    assert outs[0]["source"]["sender_name"] == "lisi" and outs[0]["source"]["human_agent"] is True
    assert len(replied) == 1, "接待人员的回复绝不触发 AI 自动回复"
    assert store.get_turn("wkKF", "wmU1")["sent_since_inbound"] == 0


async def test_media_inbound_saved_with_media_ref(monkeypatch, tmp_path):
    from src.integrations import wechat_kf_worker as WW

    def _paths(platform, name, ext):
        dest = tmp_path / f"{name}{ext}"
        return dest, f"/static/protocol_media/{platform}/{name}{ext}"

    monkeypatch.setattr("src.integrations.protocol_bridge.media_paths", _paths)
    w, cl, store, emitted, _ = _mk()
    cl.sync_pages = [{"msg_list": [_msg(msgid="img1", msgtype="image", image={"media_id": "MID"})],
                      "next_cursor": "C1", "has_more": 0}]
    await w.sync_once()
    p = emitted[0]
    assert p["media_type"] == "image" and p["media_ref"] == "/static/protocol_media/wechat_kf/wkKF_img1.jpg"
    assert (tmp_path / "wkKF_img1.jpg").read_bytes().startswith(b"\xff\xd8")
    assert p["text"] == ""
    # 下载失败：仍落库（media_type 保留、media_ref 空），不丢消息
    cl.media_ok = False
    cl.sync_pages = [{"msg_list": [_msg(msgid="img2", msgtype="image", image={"media_id": "MID2"})],
                      "next_cursor": "C2", "has_more": 0}]
    await w.sync_once()
    assert emitted[1]["media_type"] == "image" and emitted[1]["media_ref"] == ""
    assert WW.PLATFORM == "wechat_kf"


# ── 事件 ────────────────────────────────────────────────────────────────────

async def test_send_fail_event_closes_window_and_marks_message(monkeypatch):
    marked = []
    monkeypatch.setattr("src.integrations.protocol_bridge.report_message_status",
                        lambda *a: marked.append(a) or True)
    w, cl, store, *_ = _mk()
    store.record_inbound("wkKF", "wmU1", 1700000000)
    cl.sync_pages = [{"msg_list": [{"msgtype": "event", "event": {
        "event_type": "msg_send_fail", "open_kfid": "wkKF", "external_userid": "wmU1",
        "fail_msgid": "M-9", "fail_type": 4}}], "next_cursor": "C1", "has_more": 0}]
    await w.sync_once()
    assert store.get_turn("wkKF", "wmU1")["closed_reason"] == "fail_type_4"
    assert marked and marked[0][:4] == ("wechat_kf", "wkKF", "wxkf:user:wmU1", "M-9") and marked[0][4] == "failed"
    assert w.stats["send_fail_events"] == 1


async def test_enter_session_sends_welcome_when_configured():
    w, cl, store, emitted, _ = _mk(config={"wechat_kf": {"welcome_text": "您好，有什么可以帮您？"}})
    cl.sync_pages = [{"msg_list": [{"msgtype": "event", "event": {
        "event_type": "enter_session", "open_kfid": "wkKF", "external_userid": "wmU1",
        "scene": "official", "welcome_code": "WC1"}}], "next_cursor": "C1", "has_more": 0}]
    await w.sync_once()
    assert ("send_msg_on_event", "WC1", "您好，有什么可以帮您？") in cl.calls
    assert emitted and emitted[0]["direction"] == "out" and emitted[0]["source"]["kf_event"] == "welcome"
    # 未配欢迎语 → 不发
    w2, cl2, *_ = _mk()
    cl2.sync_pages = [{"msg_list": [{"msgtype": "event", "event": {
        "event_type": "enter_session", "external_userid": "wmU1", "welcome_code": "WC2"}}],
        "next_cursor": "C1", "has_more": 0}]
    await w2.sync_once()
    assert not any(c[0] == "send_msg_on_event" for c in cl2.calls)


async def test_recall_event_reports_deleted(monkeypatch):
    seen = []
    monkeypatch.setattr("src.integrations.protocol_bridge.report_deleted_messages",
                        lambda *a, **k: seen.append((a, k)) or 1)
    w, cl, *_ = _mk()
    cl.sync_pages = [{"msg_list": [{"msgtype": "event", "event": {
        "event_type": "user_recall_msg", "open_kfid": "wkKF", "external_userid": "wmU1",
        "recall_msgid": "R1"}}], "next_cursor": "C1", "has_more": 0}]
    await w.sync_once()
    assert seen and seen[0][0] == ("wechat_kf", "wkKF", ["R1"]) and seen[0][1] == {"chat_key": "wxkf:user:wmU1"}


# ── 出站 ────────────────────────────────────────────────────────────────────

async def test_send_records_quota_and_returns_message_id():
    w, cl, store, *_ = _mk()
    store.record_inbound("wkKF", "wmU1", 1700000000)
    res = await w.send("wxkf:user:wmU1", "回复")
    assert res["delivered"] is True and res["message_id"].startswith("M-")
    assert cl.calls[-1][:3] == ("send_text", "wmU1", "wkKF")
    assert store.get_turn("wkKF", "wmU1")["sent_since_inbound"] == 1


async def test_send_media_uploads_then_sends_and_counts_caption():
    w, cl, store, *_ = _mk()
    store.record_inbound("wkKF", "wmU1", 1700000000)
    res = await w.send_media("wmU1", media_path="x.jpg", media_type="image", caption="看这张")
    assert res["delivered"] is True and res["message_id"] == "MM1"
    kinds = [c[0] for c in cl.calls]
    assert kinds[-3:] == ["upload_media", "send_media", "send_text"]
    assert store.get_turn("wkKF", "wmU1")["sent_since_inbound"] == 2


async def test_send_voice_converts_to_amr_or_fails_honestly(monkeypatch, tmp_path):
    from src.integrations import wechat_kf_worker as WW
    orig_convert = WW.convert_voice_to_amr
    w, cl, store, *_ = _mk()
    store.record_inbound("wkKF", "wmU1", 1700000000)
    # 转码不可用 → 诚实失败，不上传
    monkeypatch.setattr(WW, "convert_voice_to_amr", lambda p: ("", "amr_encoder_unavailable"))
    res = await w.send_media("wmU1", media_path="reply.ogg", media_type="voice")
    assert res["delivered"] is False and res["error_kind"] == "voice_format_unsupported"
    assert not any(c[0] == "upload_media" for c in cl.calls)
    # 转码成功 → 上传的是 .amr 临时文件，且发完即删
    amr = tmp_path / "x.amr"
    amr.write_bytes(b"#!AMR\n")
    monkeypatch.setattr(WW, "convert_voice_to_amr", lambda p: (str(amr), ""))
    res = await w.send_media("wmU1", media_path="reply.ogg", media_type="voice")
    assert res["delivered"] is True
    up = [c for c in cl.calls if c[0] == "upload_media"][-1]
    assert up[1] == str(amr) and up[2] == "voice" and not amr.exists()
    # 已是 amr → 直传原文件（恢复真实转换器：.amr 后缀直接短路，不碰 ffmpeg）
    monkeypatch.setattr(WW, "convert_voice_to_amr", orig_convert)
    res = await w.send_media("wmU1", media_path="ready.amr", media_type="voice")
    assert res["delivered"] is True
    assert [c for c in cl.calls if c[0] == "upload_media"][-1][1] == "ready.amr"


def test_convert_voice_to_amr_real_ffmpeg_when_encoder_present(tmp_path):
    """本机有 ffmpeg + libopencore_amrnb 时做一次真实转码；缺编码器时必须给出原因而非假成功。"""
    from src.integrations.wechat_kf_worker import convert_voice_to_amr
    from src.utils.ffmpeg_resolver import ffmpeg_path
    ff = ffmpeg_path()
    if not ff:
        assert convert_voice_to_amr(str(tmp_path / "a.ogg")) == ("", "ffmpeg_missing")
        return
    import subprocess
    src = tmp_path / "tone.wav"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    str(src)], check=True, capture_output=True, timeout=60)
    out, err = convert_voice_to_amr(str(src))
    try:
        if out:
            data = open(out, "rb").read(6)
            assert data.startswith(b"#!AMR"), data
        else:
            assert err in ("amr_encoder_unavailable",) or err.startswith("ffmpeg_failed"), err
    finally:
        if out and os.path.exists(out):
            os.remove(out)


async def test_send_failure_surfaces_error_kind():
    class _Cl(FakeClient):
        async def send_text(self, uid, open_kfid, text, *, msgid=""):
            return _fail("not allow", "ip_not_allowed", 60020)

    w, cl, store, *_ = _mk(client=_Cl())
    res = await w.send("wmU1", "x")
    assert res["delivered"] is False and res["error_kind"] == "ip_not_allowed"
    assert store.get_turn("wkKF", "wmU1") is None


async def test_session_actions_call_state_api():
    w, cl, *_ = _mk()
    await w.transfer_to_human("wxkf:user:wmU1", "lisi")
    await w.close_session("wmU1")
    assert ("trans", "wkKF", "wmU1", 3, "lisi") in cl.calls
    assert ("trans", "wkKF", "wmU1", 4, "") in cl.calls


async def test_kick_wakes_loop_with_token():
    w, cl, store, *_ = _mk()
    cl.sync_pages = [{"msg_list": [], "next_cursor": "", "has_more": 0}] * 5
    await w.start()
    try:
        w.kick("CBTOK")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if any(c[0] == "sync_msg" and c[3] == "CBTOK" for c in cl.calls):
                break
        assert any(c[0] == "sync_msg" and c[3] == "CBTOK" for c in cl.calls)
        assert w.status()["type"] == "wechat_kf" and w.status()["open_kfid"] == "wkKF"
    finally:
        await w.stop()


# ── 回调路由 ────────────────────────────────────────────────────────────────

_TOKEN, _AES, _CORP = "QDG6eK", "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C", "corp1"


class _CM:
    def __init__(self, cfg):
        self.config = cfg


def test_routes_not_registered_without_callback_cfg():
    from src.integrations.wechat_kf_webhook import register_wechat_kf_routes
    app = FastAPI()
    assert register_wechat_kf_routes(app, _CM({"wechat_kf": {"enabled": True}})) is None
    assert register_wechat_kf_routes(app, _CM({})) is None


def test_routes_verify_and_dispatch(monkeypatch):
    from src.integrations import wechat_kf_webhook as WH
    app = FastAPI()
    cfg = {"wechat_kf": {"enabled": True, "corpid": _CORP,
                         "callback": {"token": _TOKEN, "encoding_aes_key": _AES}}}
    path = WH.register_wechat_kf_routes(app, _CM(cfg))
    assert path == "/wechat/kf/callback"
    crypto = WK.KfCallbackCrypto(_TOKEN, _AES, _CORP)
    client = TestClient(app)
    echo = crypto.encrypt(b"1234567890")
    sig = crypto.signature("1", "2", echo)
    r = client.get(path, params={"msg_signature": sig, "timestamp": "1", "nonce": "2", "echostr": echo})
    assert r.status_code == 200 and r.text == "1234567890"
    r = client.get(path, params={"msg_signature": "x", "timestamp": "1", "nonce": "2", "echostr": echo})
    assert r.status_code == 403
    got = {}
    monkeypatch.setattr(WH, "dispatch_event", lambda f: got.update(f) or True)
    inner = (f"<xml><ToUserName><![CDATA[{_CORP}]]></ToUserName><CreateTime>1</CreateTime>"
             "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[kf_msg_or_event]]></Event>"
             "<Token><![CDATA[CB-T]]></Token><OpenKfId><![CDATA[wkKF]]></OpenKfId></xml>").encode()
    enc = crypto.encrypt(inner)
    body = f"<xml><ToUserName><![CDATA[{_CORP}]]></ToUserName><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
    r = client.post(path, params={"msg_signature": crypto.signature("9", "8", enc), "timestamp": "9",
                                  "nonce": "8"}, content=body.encode("utf-8"))
    assert r.status_code == 200 and r.text == "success"
    assert got["Token"] == "CB-T" and got["OpenKfId"] == "wkKF"
    r = client.post(path, params={"msg_signature": "bad", "timestamp": "9", "nonce": "8"},
                    content=body.encode("utf-8"))
    assert r.status_code == 403


def test_dispatch_event_kicks_matching_worker(monkeypatch):
    from src.integrations import wechat_kf_webhook as WH

    class _W:
        open_kfid = "wkKF"
        account_id = "official"
        kicked = []

        def kick(self, token=""):
            self.kicked.append(token)

    class _M:
        def __init__(self, w):
            self.worker, self.state = w, "running"

    w = _W()

    class _Orch:
        _managed = {"wechat_kf:official": _M(w), "telegram:x": _M(object())}

    monkeypatch.setattr("src.integrations.account_orchestrator.get_orchestrator_if_running",
                        lambda: _Orch())
    assert WH.dispatch_event({"Event": "kf_msg_or_event", "OpenKfId": "wkKF", "Token": "T9"}) is True
    assert w.kicked == ["T9"]
    assert WH.dispatch_event({"Event": "kf_msg_or_event", "OpenKfId": "nope", "Token": "T"}) is False
    assert WH.dispatch_event({"Event": "other"}) is False


def test_pick_servicer_prefers_explicit_then_online_then_first():
    from src.integrations.wechat_kf_webhook import pick_servicer
    lst = [{"userid": "a", "status": 1}, {"userid": "b", "status": 0}, {"userid": "c", "status": 0}]
    assert pick_servicer(lst) == "b", "第一个接待中的"
    assert pick_servicer(lst, preferred=" zhao ") == "zhao", "显式指定优先"
    assert pick_servicer([{"userid": "a", "status": 1}]) == "a", "没有接待中的就第一个"
    assert pick_servicer([]) == "" and pick_servicer(None) == "" and pick_servicer([{"status": 0}]) == ""
