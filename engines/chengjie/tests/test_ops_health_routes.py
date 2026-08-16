"""实施31：反封号健康聚合 account_health_row 纯函数单测（不依赖 FastAPI/真号）。"""
from __future__ import annotations

from src.web.routes.ops_health_routes import account_health_row

CFG = {"companion_send_gate": {"enabled": True, "target_cap": 120,
                               "warmup_start_cap": 40, "warmup_ramp_days": 7}}


def _sig(**kw):
    base = {"account_id": "A", "proxy_bound": True, "banned": False, "sends_today": 0}
    base.update(kw)
    return base


def test_row_ok_when_running_and_green():
    row = account_health_row(
        "telegram", "A", cfg=CFG, signals=_sig(age_days=10, sends_today=3),
        rate_snapshot={"hour_used": 3, "hour_limit": 40, "day_used": 3, "day_limit": 150},
        worker_state="running", events_summary={"total": 0, "by_kind": {}},
        frozen=False,
    )
    assert row["overall"] == "ok"
    assert row["running"] is True
    assert row["health"]["light"] == "green"
    assert row["rate"]["day_limit"] == 150


def test_row_stopped_when_worker_not_running():
    row = account_health_row(
        "telegram", "A", cfg=CFG, signals=_sig(age_days=10),
        worker_state="stopped", frozen=False,
    )
    assert row["overall"] == "stopped"
    assert row["running"] is False


def test_row_frozen_takes_priority():
    row = account_health_row(
        "telegram", "A", cfg=CFG, signals=_sig(age_days=10),
        worker_state="running", frozen=True,
    )
    assert row["overall"] == "frozen"
    assert row["frozen"] is True


def test_row_at_risk_when_banned():
    # banned → account_health 判红 → allowed False → at_risk
    row = account_health_row(
        "telegram", "A", cfg=CFG, signals=_sig(banned=True),
        worker_state="running", frozen=False,
    )
    assert row["overall"] == "at_risk"
    assert row["health"]["banned"] is True


def test_row_events_passthrough():
    row = account_health_row(
        "telegram", "A", cfg=CFG, signals=_sig(age_days=10),
        worker_state="running",
        events_summary={"total": 3, "by_kind": {"account_paused": 2, "account_banned": 1}},
    )
    assert row["events_7d"]["total"] == 3
    assert row["events_7d"]["by_kind"]["account_paused"] == 2


def test_row_never_raises_on_bad_signals():
    # 传入奇怪信号也不抛（端点永不 500）
    row = account_health_row("telegram", "A", cfg={}, signals={}, worker_state="")
    assert "overall" in row and "health" in row


def test_row_ungated_when_gate_disabled():
    # 闸门未开 → 灯标 ungated（不给「green=已验证健康」的假安全感），运行中仍算 ok
    row = account_health_row(
        "telegram", "A", cfg={"companion_send_gate": {"enabled": False}},
        signals=_sig(age_days=10), worker_state="running",
    )
    assert row["health"]["light"] == "ungated"
    assert row["overall"] == "ok"          # ungated 但在跑 = 正常接待
