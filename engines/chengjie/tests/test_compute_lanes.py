# -*- coding: utf-8 -*-
"""三路算力互相顶班 + 3 分钟催运维群（2026-09-18）。"""
from __future__ import annotations

from types import SimpleNamespace

from src.ai import compute_lanes as cl
from src.ai.ai_client import AIClient
from src.inbox.health_watchdog import HealthWatchdog
from src.inbox.remind_ledger import RemindLedger
from tests.test_ai_key_pool import (
    _FakeChatClient, _client, _pool_entry, _silence_notifications,
)


def test_failover_order_skips_quota_cloud_then_still_tries_it_last():
    cl.note_fail("cloud", "quota", "no money", now=1000.0)
    order = cl.failover_order("local", now=1000.0)
    assert order[0] == "local"
    assert order[1] == "pool"
    assert order[-1] == "cloud"


def test_failover_order_cloud_primary_skips_dead_cloud():
    cl.note_fail("cloud", "quota", now=1.0)
    assert cl.failover_order("cloud", now=1.0)[0] == "pool"


def test_local_only_never_lists_cloud_or_pool():
    assert cl.failover_order("local_only") == ["local"]


def test_note_ok_clears_skip():
    cl.note_fail("cloud", "quota", now=10.0)
    assert cl.should_skip("cloud", now=11.0)
    cl.note_ok("cloud", now=12.0)
    assert not cl.should_skip("cloud", now=13.0)


def test_classify_402_is_quota():
    class E(Exception):
        status_code = 402
    assert AIClient._classify_ai_error(E("Payment Required")) == "quota"
    assert AIClient._classify_ai_error(Exception("Error code: 402 - Insufficient Balance")) == "quota"


async def test_local_down_skips_broke_cloud_and_uses_pool(monkeypatch):
    import time
    _silence_notifications(monkeypatch)
    cl.note_fail("cloud", "quota", "empty", now=time.time())
    cloud = _FakeChatClient(reply="不该先打 DeepSeek")
    local = _FakeChatClient(fail=Exception("connection reset"))
    pool = _FakeChatClient(reply="硅基顶上")
    c = _client(cloud)
    c._primary_mode = "local"
    c._fb_client = local
    c._fb_model = "chatx"
    c._pool_entries = [_pool_entry("siliconflow-v4-flash", pool, model="DeepSeek-V4-Flash")]
    out = await c._generate_reply_openai_compat("在吗", context={"reply_lang": "zh"})
    assert out == "硅基顶上"
    assert pool.calls == 1
    assert cloud.calls == 0, "欠费冷却中不应先打 DeepSeek"


def _watchdog(cfg, ledger=None):
    w = HealthWatchdog.__new__(HealthWatchdog)
    w._config_manager = SimpleNamespace(config=cfg, config_path="")
    w._last_compute_lane_ts = 0.0
    w.total_compute_lane_reminders = 0
    w.__dict__["_remind_ledger"] = ledger or RemindLedger(None)
    return w


def test_watchdog_nags_every_3_minutes_until_ack_or_repair(monkeypatch):
    cfg = {"health_watchdog": {"compute_lane_remind": {"enabled": True, "interval_sec": 180}}}
    published = []

    class _Bus:
        def publish(self, name, data):
            published.append((name, dict(data)))

    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: _Bus())

    def _probe(lane, config):
        if lane == "cloud":
            return {"ok": False, "kind": "quota", "detail": "余额为 0"}
        return {"ok": True, "kind": "", "detail": ""}

    monkeypatch.setattr(cl, "probe_lane", _probe)
    w = _watchdog(cfg)
    t0 = 5_000_000.0
    w._check_compute_lanes(now=t0)
    assert w.total_compute_lane_reminders == 1
    assert published[0][0] == "compute_lane_alert"
    assert published[0][1]["kind"] == "quota"
    assert "173" in "、".join(published[0][1].get("standins") or []) or published[0][1].get("standins")

    published.clear()
    w._check_compute_lanes(now=t0 + 60)
    assert published == []  # 未到 3 分钟

    w._check_compute_lanes(now=t0 + 181)
    assert w.total_compute_lane_reminders == 2
    assert published[0][1].get("reminder") is True

    # 认领：同指纹不再催
    published.clear()
    w._remind.ack(cl.remind_key("cloud"), by="ops", now=t0 + 182)
    w._check_compute_lanes(now=t0 + 181 + 180)
    assert published == []

    # 修好：发恢复
    def _ok(lane, config):
        return {"ok": True, "kind": "", "detail": ""}
    monkeypatch.setattr(cl, "probe_lane", _ok)
    w._check_compute_lanes(now=t0 + 400)
    assert any(d.get("recovered") for _, d in published)


def test_watchdog_disabled_on_explicit_flag(monkeypatch):
    called = []
    monkeypatch.setattr(cl, "probe_lane", lambda *a, **k: called.append(1) or {"ok": True})
    w = _watchdog({"health_watchdog": {"compute_lane_remind": {"enabled": False}}})
    w._check_compute_lanes(now=1.0)
    assert called == []


def test_ops_card_has_action_link_and_plain_text():
    from src.inbox.webhook_notifier import _build_card, _build_message, card_body_lines
    from src.utils import ops_glance_token as ogt
    ogt._reset_for_tests()
    ogt.configure("unit-test-secret-0123456789")
    data = {
        "remind_key": "compute_lane:cloud",
        "lane": "cloud",
        "label": "DeepSeek 官方",
        "kind": "quota",
        "kind_zh": "没有费用 / 余额不可用",
        "detail": "余额为 0",
        "standins": ["173 本地 vLLM", "硅基流动备用"],
        "down_minutes": 9,
        "reminder": True,
        "unchanged": True,
    }
    title, text = _build_message("compute_lane_alert", data)
    card = _build_card("compute_lane_alert", data, title, text, "https://katie.example.cc", "login")
    assert "DeepSeek" in card and "没有费用" in card
    assert "每 3 分钟" in card
    assert "173 本地 vLLM" in card
    assert "🛠 处置" in card
    assert "**" not in card
    assert len(card_body_lines(card)) <= 12
    rec_title, rec_text = _build_message("compute_lane_alert", {
        "recovered": True, "lane": "cloud", "label": "DeepSeek 官方"})
    rec = _build_card("compute_lane_alert", {"recovered": True, "lane": "cloud",
                                            "label": "DeepSeek 官方",
                                            "remind_key": "compute_lane:cloud"},
                      rec_title, rec_text, "https://katie.example.cc", "login")
    assert rec.startswith("✅ 恢复") and "🛠" not in rec
