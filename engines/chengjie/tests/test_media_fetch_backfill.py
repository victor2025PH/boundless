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
