"""冷启动预热封顶（入站侧）门禁：`.198` 事故闭环的另一半。

主动外呼侧由 `test_outbound_gate` / `test_proactive_cold_start_wiring` 守；本文件守
「有人真的来消息时，新号能不能不经人看就自动回」。

最重要的不是「新号被拦住」（那条容易），而是**存量账号升级后一切照旧**——判据判不出
时必须放行。安全闸把老客户的自动回复静默降级成人审队列，比不做这个功能更糟。
"""
from __future__ import annotations

import time

import pytest

from src.inbox.account_connection import (
    AccountConnectionLog,
    reset_resolve_cache,
    resolve_account_connected_at,
)
from src.inbox.outbound_gate import automation_ceiling, resolve_cold_start_cfg

HOUR = 3600.0
NOW = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_resolve_cache()
    yield
    reset_resolve_cache()


# ── 纯函数：封顶判定 ────────────────────────────────────────────────────────
def test_new_account_capped_to_review():
    cfg = resolve_cold_start_cfg({})
    assert automation_ceiling(NOW - 5 * HOUR, NOW, cfg) == "review"


def test_account_past_warmup_not_capped():
    cfg = resolve_cold_start_cfg({})
    assert automation_ceiling(NOW - 100 * HOUR, NOW, cfg) is None


def test_unknown_connected_at_never_caps():
    """核心非回归：判不出账号年龄 → 绝不封顶。

    存量部署的账号注册表早于本功能、可能没有可用 created_at；若这里保守封顶，
    所有老客户升级后自动回复会被静默降到人审队列＝一次真实服务中断。
    """
    cfg = resolve_cold_start_cfg({})
    assert automation_ceiling(0.0, NOW, cfg) is None
    assert automation_ceiling(-1.0, NOW, cfg) is None


def test_switch_off_disables_cap():
    cfg = resolve_cold_start_cfg(
        {"companion": {"proactive_topic": {"cold_start": {"warmup_review": False}}}})
    assert automation_ceiling(NOW - 1 * HOUR, NOW, cfg) is None


def test_master_switch_off_disables_cap():
    cfg = resolve_cold_start_cfg(
        {"companion": {"proactive_topic": {"cold_start": {"enabled": False}}}})
    assert automation_ceiling(NOW - 1 * HOUR, NOW, cfg) is None


def test_zero_warmup_hours_disables_cap():
    cfg = resolve_cold_start_cfg(
        {"companion": {"proactive_topic": {"cold_start": {"warmup_hours": 0}}}})
    assert automation_ceiling(NOW - 1 * HOUR, NOW, cfg) is None


def test_warmup_review_defaults_on_when_config_absent():
    """安全 floor 不依赖配置：整段 cold_start 缺失也生效（.198 的 overlay 就没提）。"""
    assert resolve_cold_start_cfg(None)["warmup_review"] is True
    assert resolve_cold_start_cfg({})["warmup_review"] is True


def test_cap_is_a_ceiling_not_an_override():
    """封顶只降不升：manual 会话不会被抬成 review。"""
    from src.inbox.drafts import cap_automation_mode
    assert cap_automation_mode("auto_ai", "review") == "review"
    assert cap_automation_mode("manual", "review") == "manual"


# ── 接入时刻解析：注册表优先，判不出返回 0 ──────────────────────────────────
def test_resolve_prefers_registry_created_at(monkeypatch):
    import src.integrations.account_registry as reg

    class _R:
        def get(self, platform, account_id):
            return {"created_at": NOW - 2 * HOUR}

    monkeypatch.setattr(reg, "get_account_registry", lambda *a, **k: _R())
    assert resolve_account_connected_at("telegram", "123", now=NOW) == NOW - 2 * HOUR


def test_resolve_returns_zero_when_registry_missing_row(monkeypatch, tmp_path):
    import src.inbox.account_connection as ac
    import src.integrations.account_registry as reg

    class _R:
        def get(self, platform, account_id):
            return None

    monkeypatch.setattr(reg, "get_account_registry", lambda *a, **k: _R())
    monkeypatch.setattr(ac, "_shared", AccountConnectionLog(None))
    assert resolve_account_connected_at("telegram", "nobody", now=NOW) == 0.0


def test_resolve_falls_back_to_connection_log(monkeypatch):
    """注册表没这行时退回主动链自举出来的登记值。"""
    import src.inbox.account_connection as ac
    import src.integrations.account_registry as reg

    class _R:
        def get(self, platform, account_id):
            return {}

    log = AccountConnectionLog(None)
    log.observe("telegram", "777", NOW - 3 * HOUR, now=NOW)
    monkeypatch.setattr(reg, "get_account_registry", lambda *a, **k: _R())
    monkeypatch.setattr(ac, "_shared", log)
    assert resolve_account_connected_at("telegram", "777", now=NOW) == NOW - 3 * HOUR


def test_resolve_ignores_future_timestamps(monkeypatch):
    """时钟漂移到未来 → 当未知，而不是把人审窗拉长到天荒地老。"""
    import src.inbox.account_connection as ac
    import src.integrations.account_registry as reg

    class _R:
        def get(self, platform, account_id):
            return {"created_at": NOW + 999 * HOUR}

    monkeypatch.setattr(reg, "get_account_registry", lambda *a, **k: _R())
    monkeypatch.setattr(ac, "_shared", AccountConnectionLog(None))
    assert resolve_account_connected_at("telegram", "drift", now=NOW) == 0.0


def test_resolve_never_raises_on_broken_registry(monkeypatch):
    import src.inbox.account_connection as ac
    import src.integrations.account_registry as reg

    def _boom(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(reg, "get_account_registry", _boom)
    monkeypatch.setattr(ac, "_shared", AccountConnectionLog(None))
    assert resolve_account_connected_at("telegram", "x", now=NOW) == 0.0


def test_resolve_caches_within_ttl(monkeypatch):
    """热路每条消息都会问 → 必须命中缓存，不能每条都打一次 DB。"""
    import src.integrations.account_registry as reg
    calls = {"n": 0}

    class _R:
        def get(self, platform, account_id):
            calls["n"] += 1
            return {"created_at": NOW - HOUR}

    monkeypatch.setattr(reg, "get_account_registry", lambda *a, **k: _R())
    for _ in range(5):
        resolve_account_connected_at("telegram", "cached", now=NOW)
    assert calls["n"] == 1


# ── 静态接线：封顶必须真的挂在入站档位链上 ───────────────────────────────────
def test_wired_into_autodraft_mode_chain():
    """防「写了模块但没接线」——安全闸最常见的静默失效形态。

    2026-08-07 起封顶收口进 effective_automation 单一事实源（A 线/B 线/API/
    CLI 同源）：本门禁改为钉「B 线消费 resolver + resolver 串真判定源」，
    排序不变量（封顶早于双轨互斥）语义原样保留。
    """
    from pathlib import Path
    base = Path(__file__).resolve().parents[1] / "src" / "inbox"
    text = (base / "autodraft_helpers.py").read_text("utf-8")
    assert "compute_mode_caps" in text, "预热封顶未接入 autodraft 档位链"
    # 必须在 companion 双轨互斥判定之前封顶：否则 A 线不让位、System Z 也不拟稿
    # ＝198 那种「两边都让、无人拟稿」的静默丢回复。
    assert text.index("apply_mode_caps") < text.index(
        "allows_direct_autosend"), "预热封顶必须早于双轨互斥判定"
    # resolver 自身必须真的串到预热判定源（防收口后变成空壳）
    ea = (base / "effective_automation.py").read_text("utf-8")
    assert "automation_ceiling" in ea
    assert "resolve_account_connected_at" in ea
