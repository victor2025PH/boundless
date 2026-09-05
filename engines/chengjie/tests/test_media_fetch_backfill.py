# -*- coding: utf-8 -*-
"""媒体按需拉取（P1，2026-08-04）：历史/超限/存量占位行的「拉取原件」闭环门禁。

三层各自钉死：

1. **store 回填原语** ``update_message_media``：与 ``update_message_text`` 同族的
   幂等回写——只在 ``media_ref`` 为空时写（绝不踩掉已有归档，防镜像/重复点击竞态）、
   定位支持主键与 ``(conversation_id, platform_msg_id)`` 双口径、``media_type``
   仅非空覆盖（存量「[图片]」纯文字行借此升级成结构化媒体行）。
2. **历史同步结构化** ``history_message_obj``：有媒体的历史行落 ``media_type``
   （本体仍不下载）——工作台从纯占位文字变成形态卡 + 「拉取原件」入口。
3. **fetch-media 路由**端到端（最小 app + 假 pyrogram loop）：配置闸（默认关）、
   幂等短路、护栏（单飞/账号校验/哈希键行）、下载成败/超限全路径——失败绝不写行。
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb
from src.web.routes import unified_inbox_account_routes as uar

ACCT = "accTG"
PEER = "5433982810"
_URL = "/static/protocol_media/telegram/out_accTG_f00d42.jpg"


def _cid() -> str:
    return f"telegram:{ACCT}:{PEER}"


def _ingest_row(store, *, msg_id: str = "9001", text: str = "[图片]",
                media_type: str = "", media_ref: str = "") -> str:
    """落一条出站行，返回其 store 行主键 message_id。"""
    pb.ingest_incoming(
        store, platform="telegram", account_id=ACCT, chat_key=PEER,
        text=text, direction="out", msg_id=msg_id,
        media_type=media_type, media_ref=media_ref, ts=time.time())
    rows = store.list_messages(_cid())
    return str(rows[-1]["message_id"])


# ═════════ 1) store 回填原语 ═════════════════════════════════════════════════

def test_get_message_roundtrip(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    mid = _ingest_row(store)
    row = store.get_message(mid)
    assert row and row["platform_msg_id"] == "9001"
    assert store.get_message("nope") is None
    assert store.get_message("") is None


def test_update_media_by_message_id_and_idempotent(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    mid = _ingest_row(store)
    assert store.update_message_media(
        _cid(), media_type="image", media_ref=_URL, message_id=mid) is True
    row = store.get_message(mid)
    assert row["media_type"] == "image" and row["media_ref"] == _URL
    # 第二次（only_if_empty 默认）：ref 已非空 → 不更新、不踩值
    assert store.update_message_media(
        _cid(), media_type="video", media_ref="/static/x.mp4",
        message_id=mid) is False
    row = store.get_message(mid)
    assert row["media_type"] == "image" and row["media_ref"] == _URL


def test_update_media_by_platform_msg_id(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    mid = _ingest_row(store, msg_id="777")
    assert store.update_message_media(
        _cid(), media_type="image", media_ref=_URL,
        platform_msg_id="777") is True
    assert store.get_message(mid)["media_ref"] == _URL


def test_update_media_requires_ref_and_locator(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    mid = _ingest_row(store)
    assert store.update_message_media(
        _cid(), media_type="image", media_ref="", message_id=mid) is False
    assert store.update_message_media(
        _cid(), media_type="image", media_ref=_URL) is False   # 无任何定位键
    assert store.get_message(mid)["media_ref"] == ""


def test_update_media_keeps_existing_type_when_new_empty(tmp_path):
    """media_type 传空 → 保留行上已有类型（只补 ref 的场景）。"""
    store = InboxStore(tmp_path / "inbox.db")
    mid = _ingest_row(store, text="", media_type="image")
    assert store.update_message_media(
        _cid(), media_type="", media_ref=_URL, message_id=mid) is True
    row = store.get_message(mid)
    assert row["media_type"] == "image" and row["media_ref"] == _URL


# ═════════ 2) 历史同步媒体行结构化 ═══════════════════════════════════════════

def _hist_msg(**kw):
    base = dict(photo=None, voice=None, audio=None, video=None,
                video_note=None, animation=None, sticker=None, document=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_history_obj_media_without_caption_gets_type():
    obj = pb.history_message_obj(
        {"text": "", "msg_id": "51", "ts": 1.0, "direction": "in"},
        _hist_msg(photo=object()))
    assert obj["text"] == "[图片]"            # 占位保留（预览/旧口径一致）
    assert obj["media_type"] == "image"       # 新增：结构化形态
    assert obj["media_ref"] == ""             # 本体仍不下载


def test_history_obj_caption_keeps_text_and_type():
    obj = pb.history_message_obj(
        {"text": "看这个", "msg_id": "52", "ts": 1.0, "direction": "in"},
        _hist_msg(photo=object()))
    assert obj["text"] == "看这个"
    assert obj["media_type"] == "image"


def test_history_obj_text_only_has_no_media_type():
    obj = pb.history_message_obj(
        {"text": "在吗", "msg_id": "53", "ts": 1.0, "direction": "in"},
        _hist_msg())
    assert obj["media_type"] == ""


def test_history_obj_service_message_still_skipped():
    assert pb.history_message_obj(
        {"text": "", "msg_id": "54", "ts": 1.0, "direction": "in"},
        _hist_msg()) == {}


# ═════════ 3) fetch-media 路由端到端 ═════════════════════════════════════════

@pytest.fixture()
def rig(tmp_path):
    """最小 app：真 store + 假 pyrogram（后台真事件循环）+ 注入式 api_auth。"""
    store = InboxStore(tmp_path / "inbox.db")
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    fake_msg = SimpleNamespace(photo=object(), empty=False, id=9001)
    pyro = SimpleNamespace(
        loop=loop,
        get_chat=lambda *a, **k: None,          # _extract_pyro 鸭子判据
        get_messages=AsyncMock(return_value=fake_msg),
    )
    cfgm = SimpleNamespace(config={
        "telegram": {"media_fetch": {"enabled": True, "max_mb": 50}},
    })
    app = FastAPI()
    uar.register_account_routes(app, api_auth=lambda r: None, config_manager=cfgm)
    app.state.inbox_store = store
    app.state.telegram_client = pyro
    client = TestClient(app)
    uar._MEDIA_FETCH_INFLIGHT.clear()
    try:
        yield SimpleNamespace(app=app, client=client, store=store,
                              pyro=pyro, cfgm=cfgm, fake_msg=fake_msg)
    finally:
        uar._MEDIA_FETCH_INFLIGHT.clear()
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()


def _post(rig_, mid: str, acct: str = ACCT):
    return rig_.client.post(
        f"/api/platforms/telegram/{acct}/fetch-media",
        json={"message_id": mid})


def test_fetch_disabled_by_default(rig, monkeypatch):
    rig.cfgm.config = {"telegram": {}}        # 键缺省 = 关（新子系统约定）
    mid = _ingest_row(rig.store)
    d = _post(rig, mid).json()
    assert d == {"ok": False, "reason": "disabled"}


def test_fetch_happy_path_backfills_row(rig, monkeypatch):
    dl = AsyncMock(return_value=("image", _URL))
    monkeypatch.setattr(pb, "download_tg_media", dl)
    mid = _ingest_row(rig.store)              # 存量「[图片]」占位行
    d = _post(rig, mid).json()
    assert d["ok"] is True and d["media_ref"] == _URL
    row = rig.store.get_message(mid)
    assert row["media_type"] == "image" and row["media_ref"] == _URL
    assert row["text"] == "[图片]"            # 正文不动（气泡层会剥裸占位）
    # 下载参数：体积上限透传
    assert dl.await_args.kwargs.get("max_bytes") == 50 * 1024 * 1024
    # 云端定位用的是行上的 platform_msg_id
    assert rig.pyro.get_messages.await_args.args == (int(PEER), 9001)


def test_fetch_idempotent_short_circuit(rig, monkeypatch):
    dl = AsyncMock(return_value=("image", _URL))
    monkeypatch.setattr(pb, "download_tg_media", dl)
    mid = _ingest_row(rig.store)
    assert _post(rig, mid).json()["ok"] is True
    d = _post(rig, mid).json()                # 第二次：行已有 ref → 直接回现值
    assert d["ok"] is True and d.get("already") is True
    assert dl.await_count == 1                # 没有第二次下载 RPC


def test_fetch_download_failure_leaves_row_untouched(rig, monkeypatch):
    monkeypatch.setattr(pb, "download_tg_media",
                        AsyncMock(return_value=("", "")))
    mid = _ingest_row(rig.store)
    d = _post(rig, mid).json()
    assert d == {"ok": False, "reason": "download_failed"}
    assert rig.store.get_message(mid)["media_ref"] == ""


def test_fetch_oversize_reason(rig, monkeypatch):
    monkeypatch.setattr(pb, "download_tg_media",
                        AsyncMock(return_value=("video", "")))
    mid = _ingest_row(rig.store)
    assert _post(rig, mid).json() == {"ok": False, "reason": "oversize"}


def test_fetch_message_gone_on_cloud(rig, monkeypatch):
    rig.pyro.get_messages = AsyncMock(
        return_value=SimpleNamespace(empty=True))
    mid = _ingest_row(rig.store)
    assert _post(rig, mid).json() == {"ok": False,
                                      "reason": "message_not_found"}


def test_fetch_guards(rig, monkeypatch):
    dl = AsyncMock(return_value=("image", _URL))
    monkeypatch.setattr(pb, "download_tg_media", dl)
    # 行不存在
    assert _post(rig, "nope").json()["reason"] == "message_not_found"
    # message_id 缺失 → 400
    r = rig.client.post(f"/api/platforms/telegram/{ACCT}/fetch-media", json={})
    assert r.status_code == 400
    # 账号不匹配（路径账号 ≠ 行所属账号）
    mid = _ingest_row(rig.store)
    assert _post(rig, mid, acct="other").json()["reason"] == "account_mismatch"
    # 哈希兜底键（无平台消息 id）→ 云端无从定位
    mid2 = _ingest_row(rig.store, msg_id="")
    assert _post(rig, mid2).json()["reason"] == "no_platform_msg_id"
    # 行级单飞：in-flight 中直接 busy
    uar._MEDIA_FETCH_INFLIGHT.add(mid)
    try:
        assert _post(rig, mid).json()["reason"] == "busy"
    finally:
        uar._MEDIA_FETCH_INFLIGHT.discard(mid)
    assert dl.await_count == 0                # 全部护栏路径零下载 RPC


# ═════════ 4) LINE fetch-media（#101：同一按钮/同一响应契约的 LINE 版）═════════
# 入站下载瞬态失败/token 陈旧 → 行落成「无音频存档」（media_type 有、ref 空）；
# OBS 对象在 LINE 服务端仍在（真机取证 ≥2 天可回取）→ 本端点按 platform_msg_id
# 走生产同一条 download_line_media 回取回填。

LACCT = "Uacc1"
LPEER = "Upeer1"
LMSGID = "629370458785710302"
_LURL = f"/static/protocol_media/line/{LACCT}_{LMSGID}.m4a"


def _lcid() -> str:
    return f"line:{LACCT}:{LPEER}"


def _ingest_line_row(store, *, msg_id: str = LMSGID, text: str = "",
                     media_type: str = "voice") -> str:
    pb.ingest_incoming(
        store, platform="line", account_id=LACCT, chat_key=LPEER,
        text=text, direction="in", msg_id=msg_id,
        media_type=media_type, media_ref="", ts=time.time())
    rows = store.list_messages(_lcid())
    return str(rows[-1]["message_id"])


@pytest.fixture()
def line_rig(tmp_path, monkeypatch):
    """最小 app：真 store + 假编排器（运行中的 LINE worker 带假 okline client）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cfgm = SimpleNamespace(config={})   # platform_login.line.media.inbound 默认开
    app = FastAPI()
    uar.register_account_routes(app, api_auth=lambda r: None, config_manager=cfgm)
    app.state.inbox_store = store
    client = TestClient(app)
    uar._MEDIA_FETCH_INFLIGHT.clear()
    fake_worker = SimpleNamespace(client=SimpleNamespace(tag="okline"))
    import src.integrations.account_orchestrator as ao
    monkeypatch.setattr(ao, "get_orchestrator", lambda: SimpleNamespace(
        worker_for=lambda p, a: fake_worker
        if (p, a) == ("line", LACCT) else None))
    try:
        yield SimpleNamespace(app=app, client=client, store=store,
                              cfgm=cfgm, worker=fake_worker)
    finally:
        uar._MEDIA_FETCH_INFLIGHT.clear()


def _lpost(rig_, mid: str, acct: str = LACCT):
    return rig_.client.post(
        f"/api/platforms/line/{acct}/fetch-media", json={"message_id": mid})


def test_line_fetch_happy_path_backfills_row(line_rig, monkeypatch):
    import src.integrations.line_media as LM
    seen = {}

    def _dl(client, message, account_id, *, cfg=None, out=None):
        seen["msg"] = dict(message)
        seen["acct"] = account_id
        seen["client"] = client
        return "voice", _LURL

    monkeypatch.setattr(LM, "download_line_media", _dl)
    mid = _ingest_line_row(line_rig.store)
    d = _lpost(line_rig, mid).json()
    assert d["ok"] is True and d["media_ref"] == _LURL
    # 回取用行上的 platform_msg_id + 按 media_type 推的 contentType（voice→3）；
    # 假 client 无 get_recent_messages → 回落 fake，并带行 ts 推的 createdTime
    # （#172：龄判定才有依据，404 不会被误判成「已过期」）
    msg = seen["msg"]
    assert msg["id"] == LMSGID and msg["contentType"] == 3
    assert msg["contentMetadata"] == {}
    assert str(msg.get("createdTime") or "").isdigit()
    assert seen["acct"] == LACCT
    assert seen["client"] is line_rig.worker.client, "必须用运行中 worker 的 client"
    row = line_rig.store.get_message(mid)
    assert row["media_type"] == "voice" and row["media_ref"] == _LURL


def test_line_fetch_disabled_by_inbound_switch(line_rig):
    line_rig.cfgm.config = {
        "platform_login": {"line": {"media": {"inbound": False}}}}
    mid = _ingest_line_row(line_rig.store)
    assert _lpost(line_rig, mid).json() == {"ok": False, "reason": "disabled"}


def test_line_fetch_idempotent_short_circuit(line_rig, monkeypatch):
    import src.integrations.line_media as LM
    calls = []
    monkeypatch.setattr(
        LM, "download_line_media",
        lambda *a, **k: calls.append(1) or ("voice", _LURL))
    mid = _ingest_line_row(line_rig.store)
    assert _lpost(line_rig, mid).json()["ok"] is True
    d = _lpost(line_rig, mid).json()
    assert d["ok"] is True and d.get("already") is True
    assert len(calls) == 1, "行已有 ref → 不该再打第二次 OBS"


def test_line_fetch_download_failure_leaves_row_untouched(line_rig, monkeypatch):
    import src.integrations.line_media as LM
    monkeypatch.setattr(LM, "download_line_media", lambda *a, **k: ("voice", ""))
    mid = _ingest_line_row(line_rig.store)
    d = _lpost(line_rig, mid).json()
    assert d["ok"] is False and d["reason"] == "download_failed"
    # J-3 交办：miss 细因透传（假下载器没写 out → 空串；旧口径 reason 不变）
    assert d["miss_reason"] == "" and d["real_message"] is False
    assert line_rig.store.get_message(mid)["media_ref"] == ""


def test_line_fetch_uses_real_message_when_worker_has_it(line_rig, monkeypatch):
    """#172 / J-3 交办：Letter Sealing 媒体要 SID/OID/chunks——worker 最近消息里
    有同 id 真消息就原样传给 download_line_media，不再用空壳 fake；miss 时把
    out.reason 透传成 miss_reason（前端重试按钮 tooltip）。"""
    import src.integrations.line_media as LM
    real = {"id": LMSGID, "contentType": 3, "createdTime": "1757000000000",
            "contentMetadata": {"SID": "s1", "OID": "o1", "e2eeVersion": "2"},
            "chunks": ["k1", "k2"]}
    other = {"id": "1", "contentType": 1, "text": "hi"}
    line_rig.worker.client = SimpleNamespace(
        tag="okline",
        get_recent_messages=lambda box, n: [other, real] if box == LPEER else [])
    seen = {}

    def _dl(client, message, account_id, *, cfg=None, out=None):
        seen["msg"] = message
        if out is not None:
            out.update({"reason": "e2ee_no_key", "retryable": True,
                        "expired": False})
        return "voice", ""

    monkeypatch.setattr(LM, "download_line_media", _dl)
    mid = _ingest_line_row(line_rig.store)
    d = _lpost(line_rig, mid).json()
    assert seen["msg"]["chunks"] == ["k1", "k2"], "必须传真消息（含 chunks）"
    assert seen["msg"]["contentMetadata"]["SID"] == "s1"
    assert d == {"ok": False, "reason": "download_failed",
                 "miss_reason": "e2ee_no_key", "expired": False,
                 "retryable": True, "real_message": True}


def test_line_find_recent_message_is_soft():
    """找真消息的任何失败都回 None（回落 fake），绝不炸整条重试链。"""
    f = uar._line_find_recent_message
    assert f(SimpleNamespace(), "U1", "9") is None                    # 无方法
    boom = SimpleNamespace(get_recent_messages=lambda *a: 1 / 0)
    assert f(boom, "U1", "9") is None                                 # 抛异常
    weird = SimpleNamespace(get_recent_messages=lambda *a: "nope")
    assert f(weird, "U1", "9") is None                                # 形态不符
    ok = SimpleNamespace(get_recent_messages=lambda *a: {"messages": [{"id": 9}]})
    assert f(ok, "U1", "9") == {"id": 9}                              # dict 包裹形态
    assert f(ok, "", "9") is None and f(ok, "U1", "") is None


def test_line_fetch_backfills_transcript(line_rig, monkeypatch):
    """#101 P2：救活语音后顺带转写回填——坐席能读、AI 能看，不是只修播放器。"""
    import src.inbox.media_enrich as ME
    import src.integrations.line_media as LM
    monkeypatch.setattr(LM, "download_line_media",
                        lambda *a, **k: ("voice", _LURL))
    seen = {}

    async def _fake_enrich(**kw):
        seen.update(kw)
        return "hello from the voice", "hello from the voice"

    monkeypatch.setattr(ME, "enrich_inbound_media_text", _fake_enrich)
    mid = _ingest_line_row(line_rig.store)
    d = _lpost(line_rig, mid).json()
    assert d["ok"] is True
    assert d.get("text") == "hello from the voice"
    assert seen["media_type"] == "voice" and seen["media_ref"] == _LURL
    assert line_rig.store.get_message(mid)["text"] == "hello from the voice"


def test_line_fetch_transcript_placeholder_not_written(line_rig, monkeypatch):
    """enrich 识别不出回吐「[语音]」占位——那不是转写，不写行不回传。"""
    import src.inbox.media_enrich as ME
    import src.integrations.line_media as LM
    monkeypatch.setattr(LM, "download_line_media",
                        lambda *a, **k: ("voice", _LURL))

    async def _fake_enrich(**kw):
        return "[语音]", ""

    monkeypatch.setattr(ME, "enrich_inbound_media_text", _fake_enrich)
    mid = _ingest_line_row(line_rig.store)
    d = _lpost(line_rig, mid).json()
    assert d["ok"] is True and "text" not in d
    assert line_rig.store.get_message(mid)["text"] == ""


def test_line_fetch_transcript_failure_still_ok(line_rig, monkeypatch):
    """转写是锦上添花：ASR 挂了不影响「媒体已回填」的成功语义。"""
    import src.inbox.media_enrich as ME
    import src.integrations.line_media as LM
    monkeypatch.setattr(LM, "download_line_media",
                        lambda *a, **k: ("voice", _LURL))

    async def _fake_enrich(**kw):
        raise RuntimeError("asr down")

    monkeypatch.setattr(ME, "enrich_inbound_media_text", _fake_enrich)
    mid = _ingest_line_row(line_rig.store)
    d = _lpost(line_rig, mid).json()
    assert d["ok"] is True and d["media_ref"] == _LURL and "text" not in d


def test_line_fetch_transcript_never_stomps_real_text(line_rig, monkeypatch):
    """行上已有真实正文（客户配文）→ 转写绝不覆盖（update_message_text 守卫）。"""
    import src.inbox.media_enrich as ME
    import src.integrations.line_media as LM
    monkeypatch.setattr(LM, "download_line_media",
                        lambda *a, **k: ("voice", _LURL))

    async def _fake_enrich(**kw):
        return "transcribed words", ""

    monkeypatch.setattr(ME, "enrich_inbound_media_text", _fake_enrich)
    mid = _ingest_line_row(line_rig.store, text="客户自己的配文")
    d = _lpost(line_rig, mid).json()
    assert d["ok"] is True and "text" not in d
    assert line_rig.store.get_message(mid)["text"] == "客户自己的配文"


def test_line_fetch_guards(line_rig, monkeypatch):
    import src.integrations.account_orchestrator as ao
    import src.integrations.line_media as LM
    calls = []
    monkeypatch.setattr(
        LM, "download_line_media",
        lambda *a, **k: calls.append(1) or ("voice", _LURL))
    # 纯文本占位行（media_type 空）→ LINE 推不出 contentType，如实 no_media
    bare = _ingest_line_row(line_rig.store, msg_id="629370458785710999",
                            text="[图片]", media_type="")
    assert _lpost(line_rig, bare).json()["reason"] == "no_media"
    # 哈希兜底键（无平台消息 id）→ OBS 无从定位
    nokey = _ingest_line_row(line_rig.store, msg_id="")
    assert _lpost(line_rig, nokey).json()["reason"] == "no_platform_msg_id"
    # 账号不匹配（路径账号 ≠ 行所属账号）
    mid = _ingest_line_row(line_rig.store, msg_id="629370458785711000")
    assert _lpost(line_rig, mid, acct="other").json()["reason"] == \
        "account_mismatch"
    # TG 会话的行投到 LINE 端点 → 平台如实拒绝
    tg_mid = _ingest_row(line_rig.store, media_type="image")
    assert _lpost(line_rig, tg_mid).json()["reason"] == "unsupported_platform"
    # worker 不在线（编排器无该账号）→ client_unavailable
    monkeypatch.setattr(ao, "get_orchestrator", lambda: SimpleNamespace(
        worker_for=lambda p, a: None))
    assert _lpost(line_rig, mid).json()["reason"] == "client_unavailable"
    # 行级单飞：in-flight 中直接 busy
    monkeypatch.setattr(ao, "get_orchestrator", lambda: SimpleNamespace(
        worker_for=lambda p, a: line_rig.worker))
    uar._MEDIA_FETCH_INFLIGHT.add(mid)
    try:
        assert _lpost(line_rig, mid).json()["reason"] == "busy"
    finally:
        uar._MEDIA_FETCH_INFLIGHT.discard(mid)
    assert calls == [], "全部护栏路径零 OBS 下载"
