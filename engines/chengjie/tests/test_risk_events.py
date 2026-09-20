"""反封号反馈闭环门禁（P0，2026-08-05）。

守的不变量：``ban_signal`` 记的 flood/error 事件，经 24h 滚动计数被
``build_account_signals`` 读回、喂 ``account_health`` 扣分——即「风控压力 →
健康分下降 → 自动降 recommended_cap」这条环真的接通，而不是各写各的。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ops.ban_signal import handle_send_exception, risk_kind_for
from src.ops.risk_events import (
    KIND_ERROR,
    KIND_FLOOD,
    RiskEventStore,
    account_key,
)
from src.skills.account_health import account_health
from src.skills.account_signals import build_account_signals


class _FakeFlood(Exception):
    """伪 pyrogram FloodWait（classify 按类名 + value 秒数判 backoff）。"""

    def __init__(self, seconds: int = 30) -> None:
        super().__init__(f"FloodWait {seconds}")
        self.value = seconds


_FakeFlood.__name__ = "FloodWait"


class _FakeSendError(Exception):
    pass


_FakeSendError.__name__ = "SomeTransientError"


# ── 1. 存储：滚动窗口 + 分类计数 + 清理 ────────────────────────────────────

def test_store_counts_within_window(tmp_path):
    s = RiskEventStore(tmp_path / "risk.db")
    k = account_key("telegram", "acc1")
    s.record(k, KIND_FLOOD, ts=1000.0)
    s.record(k, KIND_FLOOD, ts=1001.0)
    s.record(k, KIND_ERROR, ts=1002.0)
    got = s.counts_since(k, 900.0)
    assert got == {"flood": 2, "error": 1}


def test_store_window_excludes_old(tmp_path):
    s = RiskEventStore(tmp_path / "risk.db")
    k = account_key("telegram", "acc1")
    s.record(k, KIND_FLOOD, ts=100.0)     # 窗口外
    s.record(k, KIND_FLOOD, ts=5000.0)    # 窗口内
    assert s.counts_since(k, 4000.0) == {"flood": 1}


def test_store_isolates_accounts(tmp_path):
    s = RiskEventStore(tmp_path / "risk.db")
    s.record(account_key("telegram", "a"), KIND_FLOOD, ts=1000.0)
    s.record(account_key("telegram", "b"), KIND_ERROR, ts=1000.0)
    assert s.counts_since(account_key("telegram", "a"), 0.0) == {"flood": 1}
    assert s.counts_since(account_key("telegram", "b"), 0.0) == {"error": 1}


# ── 2. 分类 → 风控类别映射（该记的记、不该记的别记） ────────────────────

def test_risk_kind_mapping():
    assert risk_kind_for("backoff", "FloodWait:30s") == KIND_FLOOD
    assert risk_kind_for("pause", "PeerFlood") == KIND_FLOOD
    assert risk_kind_for("none", "SomeTransientError") == KIND_ERROR
    # 我方控制流 / 对端注销：绝不算本号风控（防误伤，2026-07-27 那类事故）
    assert risk_kind_for("none", "own_control_flow") is None
    assert risk_kind_for("none", "peer_side:InputUserDeactivated") is None
    # ban 由 meta.banned 直接红灯，不重复计
    assert risk_kind_for("ban", "Unauthorized") is None


# ── 3. handle_send_exception 记录（注入 recorder，不碰真库） ────────────────

def test_handle_flood_records_flood():
    rec = []
    out = handle_send_exception(
        "telegram", "acc1", _FakeFlood(30),
        risk_recorder=lambda p, a, k, now=None: rec.append((p, a, k)))
    assert out["kind"] == "backoff"          # 行为不变：仍是退避不停号
    assert rec == [("telegram", "acc1", KIND_FLOOD)]


def test_handle_unknown_error_records_error():
    rec = []
    out = handle_send_exception(
        "telegram", "acc1", _FakeSendError("boom"),
        risk_recorder=lambda p, a, k, now=None: rec.append((p, a, k)))
    assert out["kind"] == "none"
    assert rec == [("telegram", "acc1", KIND_ERROR)]


def test_handle_own_control_flow_records_nothing():
    rec = []
    handle_send_exception(
        "telegram", "acc1", RuntimeError("send_gate_blocked: warmup"),
        risk_recorder=lambda p, a, k, now=None: rec.append((p, a, k)))
    assert rec == []


def test_recorder_failure_never_breaks(monkeypatch):
    """记账抛异常也不能掩盖/改变原始处置。"""
    def _boom(*a, **k):
        raise RuntimeError("db down")
    out = handle_send_exception(
        "telegram", "acc1", _FakeFlood(30), risk_recorder=_boom)
    assert out["kind"] == "backoff"


# ── 4. 端到端闭环：build_account_signals 读回 → 健康分真的跌 ───────────────

def test_signals_read_risk_counts(tmp_path):
    s = RiskEventStore(tmp_path / "risk.db")
    k = account_key("telegram", "acc1")
    for _ in range(3):
        s.record(k, KIND_FLOOD, ts=10_000.0)
    s.record(k, KIND_ERROR, ts=10_000.0)

    sig = build_account_signals(
        "telegram", "acc1", now=10_050.0,
        risk_source=lambda p, a, now=None: s.counts_since(
            account_key(p, a), (now or 0) - 86400.0),
    )
    assert sig["flood_waits_24h"] == 3
    assert sig["errors_24h"] == 1


def test_floods_drive_health_down_end_to_end():
    """无 flood 绿灯；喂进 3 次 flood 后 build→health 掉到 amber/red。"""
    clean = build_account_signals(
        "telegram", "acc1", now=100.0,
        risk_source=lambda p, a, now=None: {})
    assert account_health({**clean, "age_days": 30, "proxy_bound": True})["light"] == "green"

    risky = build_account_signals(
        "telegram", "acc1", now=100.0,
        risk_source=lambda p, a, now=None: {"flood": 3, "error": 2})
    h = account_health({**risky, "age_days": 30, "proxy_bound": True})
    assert risky["flood_waits_24h"] == 3 and risky["errors_24h"] == 2
    assert h["light"] in ("amber", "red")
    assert any("限频" in r for r in h["reasons"])


def test_signals_missing_risk_source_is_benign():
    """读风控计数异常 → 不填字段（缺数据视为良性，健康分不误伤）。"""
    def _boom(p, a, now=None):
        raise RuntimeError("store down")
    sig = build_account_signals(
        "telegram", "acc1", now=100.0, risk_source=_boom)
    assert "flood_waits_24h" not in sig
    assert "errors_24h" not in sig


# ── 5. RPA 接线（P0 补全）：Messenger 页面风控态桥进同一反馈环 ─────────────

def test_messenger_rpa_bridges_risk_hit_into_feedback_loop():
    """Messenger RPA `_handle_risk_hit` 必须把风控态记进 risk_events（与协议线同口径）。

    runner 巨大且难实例化 → 用源码接线断言守这条缝（对齐仓内 _webhook_source 门禁风格）。
    """
    src = (Path(__file__).resolve().parent.parent
           / "src" / "integrations" / "messenger_rpa" / "runner.py"
           ).read_text(encoding="utf-8")
    # 桥接必须落在 _handle_risk_hit 内（record_risk_hit 之后）
    assert "record_risk_event" in src, "Messenger RPA 未桥接风控态到 risk_events"
    idx_hit = src.find("def _handle_risk_hit")
    idx_bridge = src.find("record_risk_event", idx_hit)
    idx_next_def = src.find("\n    def ", idx_hit + 10)
    assert idx_hit != -1 and idx_bridge != -1
    assert idx_bridge < idx_next_def, "record_risk_event 必须在 _handle_risk_hit 方法体内"
    assert 'record_risk_event("messenger"' in src
