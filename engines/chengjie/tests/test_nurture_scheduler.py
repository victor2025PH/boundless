"""智能养号 · 纯调度核心门禁（P1，2026-08-21）。"""
from datetime import datetime

from src.nurture.nurture_scheduler import (
    account_status, daily_budget, enabled_behaviors, in_active_window,
    lint_nurture_config, min_gap_sec, parse_hours_windows, plan_due_actions,
    profile_of, risk_backoff,
)


def _codes(findings):
    return {f["code"] for f in findings}


def _ts(hour: int, minute: int = 0) -> float:
    return datetime(2026, 8, 21, hour, minute, 0).timestamp()


_ACTIVE = _ts(10)   # 10:00 落在默认 9-11 窗
_QUIET = _ts(4)     # 04:00 不在任何窗


def _acct(key="telegram:a", *, enabled=True, profile="balanced", behaviors=None):
    return {
        "key": key, "platform": "telegram", "account_id": key.split(":", 1)[1],
        "plan": {"enabled": enabled, "profile": profile, "ramp_days": 14,
                 "behaviors": behaviors or {"read": True}},
    }


def test_hours_windows_parse_and_default():
    assert parse_hours_windows({"hours": ["9-11", "18-21"]}) == [[9, 11], [18, 21]]
    assert parse_hours_windows({}) == [[9, 11], [13, 14], [18, 21]]
    assert parse_hours_windows({"hours": []}) == [[0, 24]]  # 空=全天
    assert parse_hours_windows({"hours": ["bad", "20-19"]}) == [[0, 24]]  # 非法剔除→回落全天


def test_active_window():
    w = [[9, 11], [18, 21]]
    assert in_active_window(_ts(10), w) is True
    assert in_active_window(_ts(11), w) is False   # 右开区间
    assert in_active_window(_ts(4), w) is False


def test_quiet_hours_no_actions():
    due = plan_due_actions([_acct()], {}, {"telegram:a": {"stage": "active"}}, _QUIET, {})
    assert due == []


def test_disabled_account_skipped():
    due = plan_due_actions([_acct(enabled=False)], {}, {}, _ACTIVE, {})
    assert due == []


def test_risk_stage_skipped():
    for stage in ("banned", "restricted", "pending", "offline"):
        due = plan_due_actions([_acct()], {}, {"telegram:a": {"stage": stage}}, _ACTIVE, {})
        assert due == [], stage


def test_banned_or_circuit_skipped():
    due = plan_due_actions([_acct()], {}, {"telegram:a": {"stage": "active", "banned": True}}, _ACTIVE, {})
    assert due == []
    due = plan_due_actions([_acct()], {}, {"telegram:a": {"stage": "active", "circuit_open": True}}, _ACTIVE, {})
    assert due == []


def test_daily_budget_exhausted():
    led = {"telegram:a": {"count_today": 15, "last_ts": 0.0}}   # balanced 预算 15
    due = plan_due_actions([_acct()], led, {"telegram:a": {"stage": "active"}}, _ACTIVE, {})
    assert due == []


def test_min_gap_blocks():
    # last_ts 就在刚才 → 未过间隔 → 不排
    led = {"telegram:a": {"count_today": 1, "last_ts": _ACTIVE - 60}}
    due = plan_due_actions([_acct()], led, {"telegram:a": {"stage": "active"}}, _ACTIVE, {})
    assert due == []
    # last_ts 很久以前 → 过间隔 → 排
    led2 = {"telegram:a": {"count_today": 1, "last_ts": _ACTIVE - 99999}}
    due2 = plan_due_actions([_acct()], led2, {"telegram:a": {"stage": "active"}}, _ACTIVE, {})
    assert len(due2) == 1 and due2[0]["kind"] == "read"


def test_only_enabled_behaviors():
    a = _acct(behaviors={"read": True, "self_chat": True})
    # self_chat 总闸关 → 只会选 read
    due = plan_due_actions([a], {}, {"telegram:a": {"stage": "active"}}, _ACTIVE,
                           {"self_chat": {"enabled": False}})
    assert due and due[0]["kind"] == "read"


def test_self_chat_requires_global_switch():
    a = _acct(behaviors={"self_chat": True})   # 只启用 self_chat
    # 总闸关 → 无可用行为 → 不排
    assert plan_due_actions([a], {}, {"telegram:a": {"stage": "active"}}, _ACTIVE, {}) == []
    # 总闸开 → 排 self_chat
    due = plan_due_actions([a], {}, {"telegram:a": {"stage": "active"}}, _ACTIVE,
                           {"self_chat": {"enabled": True}})
    assert due and due[0]["kind"] == "self_chat"


def test_warming_ramp_scales_budget():
    plan = {"profile": "balanced", "ramp_days": 14}
    b_new = daily_budget(plan, {"stage": "warming", "age_days": 1})
    b_mid = daily_budget(plan, {"stage": "warming", "age_days": 7})
    b_active = daily_budget(plan, {"stage": "active"})
    assert b_new < b_mid < b_active == 15   # 新号 < 半程 < 成熟(满额)


def test_min_gap_deterministic_and_jittered():
    g1 = min_gap_sec({"profile": "balanced"}, "telegram:a", _ACTIVE)
    g2 = min_gap_sec({"profile": "balanced"}, "telegram:a", _ACTIVE)
    assert g1 == g2                       # 确定性
    base = 40 * 60
    assert base * 0.75 <= g1 <= base * 1.25  # 抖动区间


def test_one_action_per_account_per_tick():
    accts = [_acct("telegram:a"), _acct("telegram:b")]
    sig = {"telegram:a": {"stage": "active"}, "telegram:b": {"stage": "active"}}
    due = plan_due_actions(accts, {}, sig, _ACTIVE, {})
    keys = [d["key"] for d in due]
    assert sorted(keys) == ["telegram:a", "telegram:b"]  # 每号至多一条


# ── risk_backoff（P2 安全默认）───────────────────────────────────────────────

def test_risk_backoff_default_on_flood():
    # 默认开：24h 内有 FLOOD_WAIT → 退避
    assert risk_backoff({"flood_waits_24h": 1}, {}) == "risk_flood"
    assert risk_backoff({"errors_24h": 5}, {}) == "risk_errors"
    assert risk_backoff({"flood_waits_24h": 0, "errors_24h": 2}, {}) is None


def test_risk_backoff_disabled():
    assert risk_backoff({"flood_waits_24h": 9}, {"risk_backoff": {"enabled": False}}) is None


def test_risk_backoff_thresholds_configurable():
    cfg = {"risk_backoff": {"flood_threshold": 3, "error_threshold": 10}}
    assert risk_backoff({"flood_waits_24h": 2}, cfg) is None
    assert risk_backoff({"flood_waits_24h": 3}, cfg) == "risk_flood"
    assert risk_backoff({"errors_24h": 9}, cfg) is None
    assert risk_backoff({"errors_24h": 10}, cfg) == "risk_errors"


def test_plan_skips_risky_account():
    # 有 flood → plan_due_actions 退避（不排动作）
    sig = {"telegram:a": {"stage": "active", "flood_waits_24h": 2}}
    assert plan_due_actions([_acct()], {}, sig, _ACTIVE, {}) == []
    # flood 清零 → 恢复
    sig2 = {"telegram:a": {"stage": "active", "flood_waits_24h": 0}}
    assert len(plan_due_actions([_acct()], {}, sig2, _ACTIVE, {})) == 1


# ── account_status 可解释性（CLI/看板用）─────────────────────────────────────

def test_account_status_reasons():
    a = _acct()
    # 停用
    assert account_status(_acct(enabled=False), {}, {"stage": "active"}, _ACTIVE, {})["reason"] == "disabled"
    # 安静时段
    assert account_status(a, {}, {"stage": "active"}, _QUIET, {})["reason"] == "off_hours"
    # 风险
    assert account_status(a, {}, {"stage": "active", "flood_waits_24h": 1}, _ACTIVE, {})["reason"] == "risk_flood"
    # 预算耗尽
    led = {"telegram:a": {"count_today": 15, "last_ts": 0.0}}
    assert account_status(a, led, {"stage": "active"}, _ACTIVE, {})["reason"] == "budget_exhausted"
    # 到期可养
    st = account_status(a, {}, {"stage": "active"}, _ACTIVE, {})
    assert st["would_act"] is True and st["reason"] == "due" and st["next_kind"] == "read"


def test_account_status_stage_and_gap():
    a = _acct()
    assert account_status(a, {}, {"stage": "banned"}, _ACTIVE, {})["reason"] == "stage_banned"
    led = {"telegram:a": {"count_today": 1, "last_ts": _ACTIVE - 60}}
    assert account_status(a, led, {"stage": "active"}, _ACTIVE, {})["reason"] == "min_gap"


# ── lint_nurture_config（go_live 防呆）───────────────────────────────────────

def test_lint_go_live_no_canary():
    cfg = {"enabled": True, "dry_run": False, "canary_accounts": [], "accounts": {}}
    assert "go_live_no_canary" in _codes(lint_nurture_config(cfg))
    # dry_run 时不报（dry 本就不真动作）
    assert "go_live_no_canary" not in _codes(
        lint_nurture_config({"enabled": True, "dry_run": True}))


def test_lint_canary_stale_and_disabled():
    cfg = {"enabled": True, "dry_run": False,
           "canary_accounts": ["telegram:x", "telegram:y"],
           "accounts": {"telegram:y": {"enabled": False, "profile": "balanced"}}}
    codes = _codes(lint_nurture_config(cfg))
    assert "canary_stale" in codes      # telegram:x 无方案
    assert "canary_disabled" in codes   # telegram:y 方案未启用


def test_lint_risk_backoff_off():
    assert "risk_backoff_off" in _codes(
        lint_nurture_config({"risk_backoff": {"enabled": False}}))
    assert "risk_backoff_off" not in _codes(lint_nurture_config({}))  # 默认开=不报


def test_lint_self_chat_gated():
    cfg = {"accounts": {"telegram:a": {"enabled": True, "behaviors": {"self_chat": True}}}}
    # 全局 self_chat 总闸未开 → info
    assert "self_chat_gated" in _codes(lint_nurture_config(cfg))
    cfg2 = dict(cfg); cfg2["self_chat"] = {"enabled": True}
    assert "self_chat_gated" not in _codes(lint_nurture_config(cfg2))


def test_lint_stale_account_needs_registry():
    cfg = {"accounts": {"telegram:gone": {"enabled": True}}}
    # 不给 registry_keys → 不查陈旧号
    assert "stale_account" not in _codes(lint_nurture_config(cfg))
    # 给了且不在册 → 报
    assert "stale_account" in _codes(lint_nurture_config(cfg, registry_keys=["telegram:other"]))
    # 在册 → 不报
    assert "stale_account" not in _codes(lint_nurture_config(cfg, registry_keys=["telegram:gone"]))


def test_lint_clean_config():
    cfg = {"enabled": True, "dry_run": False, "canary_accounts": ["telegram:a"],
           "accounts": {"telegram:a": {"enabled": True, "profile": "balanced"}},
           "risk_backoff": {"enabled": True}}
    assert lint_nurture_config(cfg, registry_keys=["telegram:a"]) == []  # 零防呆
