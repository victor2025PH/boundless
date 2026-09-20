"""每日成本对账规则引擎门禁（2026-09-08 成本对账 P1）。

六组构造数据各触发对应结论：对得上 / 差 15% / 尖峰 / 未标注 / 余额不足 / 真值缺失；
再钉预算闸、通知分级与冷却、run_recon 落库、webhook 文案。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai import cost_recon as cr  # noqa: E402
from src.ai.cost_ledger import CostLedger, day_of  # noqa: E402

DAY = "2026-09-08"
P = "siliconflow"


def _ts(day: str, hour: int = 12) -> float:
    return time.mktime(time.strptime(day, "%Y-%m-%d")) + hour * 3600


def _prev(day: str, n: int) -> str:
    return day_of(_ts(day) - n * 86400)


def _ledger(tmp_path) -> CostLedger:
    return CostLedger(str(tmp_path / "c.db"))


def _spend(led, day, cost, purpose="customer_reply", calls=1, suspected=False, provider=P):
    for _ in range(calls):
        led.record_usage({"ts": _ts(day), "provider": provider, "model": "m", "purpose": purpose,
                          "prompt_tokens": 1000, "completion_tokens": 10,
                          "cost": cost / calls, "suspected": suspected})


def _cfg(**over):
    base = {"ai": {"cost_guard": {"mismatch_pct": 10, "mismatch_abs_cny": 2,
                                  "spike_ratio": 2.0, "spike_min_cny": 5,
                                  "runway_warn_days": 7, "truth_stale_days": 3}}}
    base["ai"]["cost_guard"].update(over)
    return base


def _rules(r):
    return {x["rule"]: x["level"] for x in r["reasons"]}


def test_ok_when_internal_matches_truth(tmp_path):
    led = _ledger(tmp_path)
    _spend(led, DAY, 1.20)
    led.set_truth(DAY, P, 1.25, source="csv")
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(_cfg()), provider=P)
    assert r["verdict"] == "ok" and "mismatch" not in _rules(r)
    assert r["light"] == "✅" and "对账 ✅" in r["summary"]


def test_mismatch_requires_both_pct_and_abs(tmp_path):
    led = _ledger(tmp_path)
    _spend(led, DAY, 8.0)
    led.set_truth(DAY, P, 12.0, source="csv")       # 差 33%、差 ¥4 → 不对账
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(_cfg()), provider=P)
    assert r["verdict"] == "mismatch" and _rules(r)["mismatch"] == "warn"
    assert "比账单" in r["reasons"][0]["message"] and "少" in r["reasons"][0]["message"]
    # 差 15% 但只差 ¥0.15 → 放过（小额抖动不打扰）
    (tmp_path / "b").mkdir(exist_ok=True)
    led2 = CostLedger(str(tmp_path / "b" / "c.db"))
    _spend(led2, DAY, 0.85)
    led2.set_truth(DAY, P, 1.0, source="manual")
    assert cr.evaluate_day(DAY, led2, cr.guard_cfg(_cfg()), provider=P)["verdict"] == "ok"


def test_no_internal_data_is_not_a_match(tmp_path):
    """账本装载前的日子：内部 0 vs 账单 1.25 不能判 ✅（0909 首次装载实锤）。"""
    led = _ledger(tmp_path)
    led.set_truth(DAY, P, 1.25, source="manual")
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(_cfg()), provider=P)
    assert r["verdict"] == "no_data" and r["light"] == "⚪"
    assert _rules(r)["no_internal"] == "info" and "账本无当日记录" in r["summary"]
    from src.inbox import webhook_notifier as wn
    _, text = wn._build_message("ai_cost_report", {
        "day": DAY, "provider": P, "internal": 0, "truth": 1.25, "verdict": "no_data",
        "light": "⚪", "by_purpose": {}, "reasons": []})
    assert "账本无当日记录" in text


def test_spike_against_7day_baseline_names_top_purposes(tmp_path):
    led = _ledger(tmp_path)
    for i in range(1, 8):
        _spend(led, _prev(DAY, i), 2.0)
    _spend(led, DAY, 9.0, purpose="drill")
    _spend(led, DAY, 3.0, purpose="customer_reply")
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(_cfg()), provider=P)
    assert _rules(r).get("spike") == "warn"
    msg = next(x["message"] for x in r["reasons"] if x["rule"] == "spike")
    assert "夜间演练" in msg and "6.0 倍" in msg


def test_unknown_purpose_and_suspected_flagged(tmp_path):
    led = _ledger(tmp_path)
    _spend(led, DAY, 0.5, purpose="unknown", calls=3)
    _spend(led, DAY, 0.3, purpose="customer_reply", suspected=True)
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(_cfg()), provider=P)
    rules = _rules(r)
    assert rules["unknown_purpose"] == "warn" and rules["suspected"] == "info"


def test_runway_from_manual_balance_and_from_recharge(tmp_path):
    led = _ledger(tmp_path)
    for i in range(0, 7):
        _spend(led, _prev(DAY, i), 2.0)
    led.set_truth(_prev(DAY, 2), P, 2.0, balance=10.0, source="manual")
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(_cfg()), provider=P)
    # 10 − 后两天各 2 = 6 元，均燃 2/天 → 3 天 → warn（<7），且 3 不算 crit（<3 才 crit）
    assert r["balance"] == 6.0 and r["runway_days"] == 3.0 and _rules(r)["runway"] == "warn"
    # 只有充值流水：300 − 累计 14 = 286 → 143 天，不告警
    led2_dir = tmp_path / "r"
    led2_dir.mkdir()
    led2 = CostLedger(str(led2_dir / "c.db"))
    for i in range(0, 7):
        _spend(led2, _prev(DAY, i), 2.0)
    led2.add_recharge(P, 300, ts=_ts(_prev(DAY, 6)))
    r2 = cr.evaluate_day(DAY, led2, cr.guard_cfg(_cfg()), provider=P)
    assert r2["balance"] == 286.0 and r2["balance_basis"] == "recharge"
    assert "runway" not in _rules(r2)


def test_truth_missing_after_n_days(tmp_path):
    led = _ledger(tmp_path)
    for i in range(0, 5):
        _spend(led, _prev(DAY, i), 1.0)
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(_cfg()), provider=P)
    assert r["verdict"] == "no_truth" and _rules(r)["truth_missing"] == "warn"
    # 昨天有真值 → 不算缺失
    led.set_truth(_prev(DAY, 1), P, 1.0)
    r2 = cr.evaluate_day(DAY, led, cr.guard_cfg(_cfg()), provider=P)
    assert r2["verdict"] == "no_truth" and "truth_missing" not in _rules(r2)


def test_budget_levels_and_allow_purpose(tmp_path):
    led = _ledger(tmp_path)
    _spend(led, DAY, 8.5)
    cfg = _cfg(daily_budget_cny=10, monthly_budget_cny=100)
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(cfg), provider=P)
    assert _rules(r)["budget_daily"] == "warn"
    _spend(led, DAY, 2.0)
    r = cr.evaluate_day(DAY, led, cr.guard_cfg(cfg), provider=P)
    assert _rules(r)["budget_daily"] == "crit" and r["light"] == "🔴"
    # 预算闸：当日超预算 → 非关键用途拒、关键用途放；无预算/无账本恒放
    now = _ts(DAY, 15)
    assert cr.allow_purpose("memory_extract", cfg, led, now=now) is False
    assert cr.allow_purpose("customer_reply", cfg, led, now=now) is True
    assert cr.allow_purpose("memory_extract", _cfg(), led, now=now) is True
    assert cr.allow_purpose("memory_extract", cfg, None, now=now) is True
    cr.configure_cost_guard(lambda: cfg)
    try:
        assert cr.allow_purpose("drill", None, led, now=now) is False
    finally:
        cr.configure_cost_guard(None)


def test_dispatch_alerts_levels_and_cooldown(tmp_path, monkeypatch):
    published = []
    hosted = []
    monkeypatch.setattr(cr, "_publish", lambda et, data: published.append((et, data)))

    import src.utils.host_alert as ha
    monkeypatch.setattr(ha, "notify_host", lambda *a, **k: hosted.append((a, k)) or True)
    cr._LAST_ALERT.clear()

    result = {
        "day": DAY, "provider": P, "internal": 12.0, "truth": None, "diff_pct": None,
        "verdict": "no_truth", "light": "🔴", "summary": "s", "by_purpose": {"drill": {"cost": 9.0}},
        "balance": 5.0, "runway_days": 2.0,
        "reasons": [
            {"rule": "spike", "level": "warn", "message": "尖峰"},
            {"rule": "suspected", "level": "info", "message": "估算"},
            {"rule": "runway", "level": "crit", "message": "余额不足"},
        ],
    }
    cfg = cr.guard_cfg(_cfg())
    sent = cr.dispatch_alerts(result, cfg, now=1000.0)
    assert sent == {"summary": True, "warn": 1, "crit": 1}
    types = [t for t, _ in published]
    assert types == ["ai_cost_report", "billing_alert"]
    assert published[0][1]["reasons"] == ["尖峰", "余额不足"]      # info 不进摘要
    assert len(hosted) == 1 and "余额不足" in hosted[0][0][1]
    # 冷却窗内同样的 warn/crit 不再重复，摘要照发
    sent2 = cr.dispatch_alerts(result, cfg, now=1000.0 + 60)
    assert sent2 == {"summary": True, "warn": 0, "crit": 0}
    sent3 = cr.dispatch_alerts(result, cfg, now=1000.0 + cfg["alert_cooldown_sec"] + 1)
    assert sent3["warn"] == 1 and sent3["crit"] == 1


def test_run_recon_persists_and_skips_lan(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    _spend(led, DAY, 1.0)
    _spend(led, DAY, 0.0, provider="lan")
    monkeypatch.setattr(cr, "_publish", lambda *a, **k: None)
    out = cr.run_recon(_cfg(), led, day=DAY, notify=True, now=_ts(DAY, 10))
    assert [r["provider"] for r in out] == [P]
    rows = led.recon_rows(P)
    assert rows and rows[0]["day"] == DAY and rows[0]["verdict"] == "no_truth"
    # 缺省对「昨天」跑
    _spend(led, _prev(DAY, 1), 1.0)
    out2 = cr.run_recon(_cfg(), led, notify=False, now=_ts(DAY, 10))
    assert out2[0]["day"] == _prev(DAY, 1)


def test_refresh_pricing_hot_applies(monkeypatch):
    from src.ai import llm_cost as lc
    t = lc.LlmCostTracker()
    monkeypatch.setattr(lc, "_SINGLETON", t)
    cr.refresh_pricing({"ai": {"pricing": {"x": {"prompt": 1, "completion": 2}}}})
    assert t.has_pricing() and abs(t.estimate_cost("x", 1000, 1000) - 3.0) < 1e-9


def test_loop_schedules_next_run_after_target_time():
    loop = cr.CostReconLoop(lambda: _cfg(recon_hour=9, recon_minute=40), lambda: None)
    lt = time.localtime(_ts(DAY, 8))
    now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 8, 0, 0, 0, 0, -1))
    assert abs(loop._seconds_until_next(now) - 100 * 60) < 2
    loop._last_run_day = DAY
    assert loop._seconds_until_next(now) > 20 * 3600


def test_webhook_message_for_cost_report_and_catalog():
    from src.inbox import webhook_notifier as wn
    assert "ai_cost_report" in wn._EVENT_ALIASES and "ai_cost_report" in wn._BUSINESS_ALERTS
    assert wn._CARD_META["ai_cost_report"] == ("📊 日报", "成本")
    title, text = wn._build_message("ai_cost_report", {
        "day": DAY, "provider": P, "internal": 2.1, "truth": 2.0, "diff_pct": 5.0,
        "verdict": "ok", "light": "✅", "by_purpose": {"drill": 1.2, "customer_reply": 0.3},
        "balance": 96.0, "runway_days": 45.0, "reasons": [],
    })
    assert "AI 花费日报" in title and "¥2.10" in text and "夜间演练 ¥1.20" in text
    assert "一致" in text and "45" in text and "要处理**: 无" in text
