# -*- coding: utf-8 -*-
"""告警链路健康灯门禁（probe_alert_link → build_health(alert_link=) 全链）。

背景：告警自检聚合器（collect_alert_link_status）与只读 API/ops 卡同批落地后，
健康灯接线（2026-08-02 同树接力批次）没有配套门禁——本文件补钉四层：
1. probe 开关语义：``ops.alert_link_health`` 默认**开**（「报进虚空」正是最该被
   看见的出货默认态）；显式 false / {enabled: false} → None（刻意不配外发的部署
   不必长期黄灯）；
2. probe 返回子集契约（verdict + 计数字段，健康灯只要最小集，明细归 ops 卡）；
3. build_health 组件映射：healthy→ok、其余四档→warn（软性——断链系统照跑，
   绝不 fail），None→组件缺席；
4. 聚合器直调路径：notifier=None（测试/最小部署）回落文件真相、engine_root
   诱饵检测（引擎根有 overlay、数据根没有 → 必须点名）。
"""
from pathlib import Path

import pytest

from src.inbox.health_watchdog import probe_alert_link
from src.integrations import notify_webhooks_store as store
from src.integrations.alert_link_status import collect_alert_link_status
from src.utils.health import build_health


@pytest.fixture()
def tmp_store(tmp_path):
    p = tmp_path / "notify_webhooks.json"
    store.set_store_path(p)
    yield p
    store.set_store_path(None)


class _State:
    webhook_notifier = None


def _mk(events=None):
    return {"name": "boss-tg", "format": "telegram", "url": "", "token": "t",
            "target": "-100", "events": events or ["all"], "enabled": True}


# ── 1/2. probe 开关语义 + 返回子集契约 ──────────────────────────────────────

def test_probe_default_on_and_subset_contract(tmp_store):
    store.save_list([])
    out = probe_alert_link(_State(), {})
    assert out is not None, "默认必须开——0 通道正是最该被看见的形态"
    assert out["verdict"] == "no_channel"
    for k in ("channels_enabled", "covered", "focus", "uncovered", "total_errors"):
        assert isinstance(out[k], int), k


def test_probe_knob_off_variants(tmp_store):
    store.save_list([])
    assert probe_alert_link(_State(), {"ops": {"alert_link_health": False}}) is None
    assert probe_alert_link(
        _State(), {"ops": {"alert_link_health": {"enabled": False}}}) is None
    assert probe_alert_link(
        _State(), {"ops": {"alert_link_health": {"enabled": True}}}) is not None


def test_probe_healthy_with_synced_running_notifier(tmp_store):
    saved = store.save_list([_mk()])
    from src.inbox.webhook_notifier import WebhookNotifier
    st = _State()
    st.webhook_notifier = WebhookNotifier(config=saved)
    st.webhook_notifier._running = True
    out = probe_alert_link(st, {})
    assert out["verdict"] == "healthy"
    assert out["channels_enabled"] == 1
    assert out["uncovered"] == 0 and out["covered"] == out["focus"] > 0


# ── 3. build_health 组件映射 ────────────────────────────────────────────────

_BASE = dict(db_ok=True, ai_provider="deepseek", ai_key_ok=True,
             channels_ready=1, channels_configured=1, channels_total=1)


def _alert_comp(health):
    return next((c for c in health["components"] if c.get("id") == "alert_link"), None)


def test_build_health_absent_when_probe_disabled():
    assert _alert_comp(build_health(**_BASE, alert_link=None)) is None


def test_build_health_verdict_status_mapping():
    ok_case = build_health(**_BASE, alert_link={
        "verdict": "healthy", "channels_enabled": 1, "covered": 11,
        "focus": 11, "uncovered": 0, "total_errors": 0})
    comp = _alert_comp(ok_case)
    assert comp and comp["status"] == "ok"

    for v in ("no_channel", "not_running", "divergent", "uncovered"):
        h = build_health(**_BASE, alert_link={
            "verdict": v, "channels_enabled": 0, "covered": 0,
            "focus": 11, "uncovered": 11, "total_errors": 0})
        comp = _alert_comp(h)
        assert comp and comp["status"] == "warn", v
        # 软性不变量：断链绝不把整体打成 fail/red（告警链路不该反过来吓停业务）
        assert h["healthy"] is True and h["light"] != "red", v


def test_build_health_unknown_verdict_conservative_warn():
    comp = _alert_comp(build_health(**_BASE, alert_link={"verdict": "???"}))
    assert comp and comp["status"] == "warn"


# ── 4. 聚合器直调路径 ───────────────────────────────────────────────────────

def test_collector_without_notifier_falls_back_to_file_truth(tmp_store):
    store.save_list([_mk(events=["draft_backlog"])])
    s = collect_alert_link_status({}, None)
    assert s["process"] == {"present": False}
    assert s["verdict"] == "uncovered"  # 无进程真相时不误判 not_running/divergent
    assert s["divergence"] is False


def test_collector_orphan_detection(tmp_path):
    eng = tmp_path / "engine"
    (eng / "config").mkdir(parents=True)
    (eng / "config" / "notify_webhooks.json").write_text("[]", encoding="utf-8")
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    store.set_store_path(data / "config" / "notify_webhooks.json")
    try:
        s = collect_alert_link_status({}, None, engine_root=eng)
        assert s["orphan"] is not None
        assert str(eng) in s["orphan"]["engine_file"]
        assert s["verdict"] == "no_channel"
    finally:
        store.set_store_path(None)
