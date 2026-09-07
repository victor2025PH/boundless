# -*- coding: utf-8 -*-
"""抖音演示 worker（实施96 P0-3）：默认关零痕迹；开着时假传输真链路——出站镜像 / 策略拦截 /
回声入站都落进统一收件箱。"""
from __future__ import annotations

import asyncio

import pytest

from src.inbox.store import InboxStore
from src.integrations import account_orchestrator as ao
from src.integrations import douyin_mock_worker as dm
from src.integrations import protocol_bridge as pb


class _Reg:
    def __init__(self):
        self.rows = []

    def upsert(self, platform, account_id, **kw):
        self.rows.append((platform, account_id, kw))
        return {"platform": platform, "account_id": account_id, **kw}


@pytest.fixture(autouse=True)
def _clean_factory(monkeypatch):
    monkeypatch.delitem(ao._WORKER_FACTORIES, "douyin:web", raising=False)
    pb.register_inbox_sink(None)
    yield
    monkeypatch.delitem(ao._WORKER_FACTORIES, "douyin:web", raising=False)
    pb.register_inbox_sink(None)


def test_disabled_by_default_leaves_no_trace():
    reg = _Reg()
    assert dm.register_douyin_mock_worker({}, registry=reg) is False
    assert dm.register_douyin_mock_worker({"platform_login": {"douyin": {}}}, registry=reg) is False
    assert ao.get_worker_factory("douyin", "web") is None
    assert reg.rows == []
    assert dm.mock_enabled(None) is False


def test_enabled_registers_factory_and_demo_account_idempotently():
    reg = _Reg()
    cfg = {"platform_login": {"douyin": {"mock_enabled": True}}}
    assert dm.register_douyin_mock_worker(cfg, registry=reg) is True
    assert dm.register_douyin_mock_worker(cfg, registry=reg) is True
    assert ao.get_worker_factory("douyin", "web") is not None
    assert ao.worker_supported("douyin", "web") is True
    assert reg.rows and reg.rows[0][:2] == ("douyin", "demo")
    assert reg.rows[0][2]["status"] == "online" and reg.rows[0][2]["mode"] == "web"
    w = ao.get_worker_factory("douyin", "web")({"account_id": "demo", "meta": {}}, cfg)
    assert isinstance(w, dm.DouyinMockWorker)
    cfg2 = {"platform_login": {"douyin": {"mock_enabled": True, "mock_auto_account": False,
                                           "mock_account_id": "x"}}}
    reg2 = _Reg()
    assert dm.register_douyin_mock_worker(cfg2, registry=reg2) is True
    assert reg2.rows == []


def test_capabilities_match_orchestrator_hasattr_contract():
    w = dm.DouyinMockWorker({"account_id": "demo"}, {})
    from src.integrations.platform_capabilities import worker_capabilities
    caps = worker_capabilities(w)
    assert caps == {"send_text": True, "send_media": True, "mark_read": True, "typing": True}


async def test_full_chain_send_mirror_echo_and_policy(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    pb.register_inbox_sink(lambda m: pb.ingest_incoming(store, **m))
    cfg = {"platform_login": {"douyin": {"mock_enabled": True, "mock_echo_delay_sec": 0}}}
    orch = ao.AccountOrchestrator(config=cfg)
    w = dm.DouyinMockWorker({"account_id": "demo", "meta": {}}, cfg)
    await w.start()
    key = ao.account_key("douyin", "demo")
    orch._managed[key] = ao._Managed(key=key, platform="douyin", account_id="demo",
                                     mode="web", worker=w, state="running")
    chat = "douyin:user:u1"
    # 客户进线（演示注入）→ 落库 in
    w.simulate_inbound("你们这个多少钱", chat_key=chat)
    # 策略拦截：外链不到 worker
    blocked = await orch.send("douyin", "demo", chat, "看这里 https://bd2026.cc/order", origin="manual")
    assert blocked["delivered"] is False and blocked["blocked"] == "policy_link_denied"
    assert w.sent == []
    # 正常回复 → delivered + 出站镜像 + 回声入站
    res = await orch.send("douyin", "demo", chat, "亲，这款 99 元，今天有活动", origin="manual")
    assert res["delivered"] is True and res["message_id"].startswith("mock-demo-")
    await asyncio.sleep(0.05)
    await w.stop()
    conv_id = f"douyin:demo:{chat}"
    conv = store.get_conversation(conv_id)
    assert conv is not None and conv["platform"] == "douyin"
    msgs = store.list_messages(conv_id, limit=20)
    texts = [(m["direction"], m["text"]) for m in msgs]
    assert ("in", "你们这个多少钱") in texts
    assert ("out", "亲，这款 99 元，今天有活动") in texts
    assert any(d == "in" and t.startswith("[抖音演示] 收到：") for d, t in texts)
    assert w.read_marks == [] and await w.mark_read(chat) is True and w.read_marks == [chat]
    assert await w.send_chat_action(chat) is True and w.status()["mock"] is True
