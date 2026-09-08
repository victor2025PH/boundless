# -*- coding: utf-8 -*-
"""P-4 D #254（MTRCH2 ④ + 日志标签，2026-09-08）：mb_trial_line None 分支 + 域包日志带业务域。

MTRCH2 实录：启动日志「🎁 首启体验档：生效中 · 100000 字符 / 0.0 小时窗口（剩 None 小时）」——
数据源：`licensing.trial.window_hours=0`（不限时）让 `evaluate()` 的 hours_left 恒 None，
两处渲染都把 None 当数字用；「KB categories set from domain 'conversion'」只打域包名，
分类表其实按 business_domain（companion）选的，让人误判域包没切。

钉住：
- 会员页三态：有数 → 「还剩 {h} 小时」；None 且窗口 0 → 「不限时」；None 且窗口 >0 → 「未开始计时」；
  quota_store / membership_routes 透传 trial_window_hours；i18n zh/en 齐；
- main.py 启动日志不再原样打 hours_left；
- skill_manager / ai_client 两条域包日志都带 business_domain。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.utils import business_domain as bdm

REPO = Path(__file__).resolve().parents[1]


def test_membership_template_three_branches_and_i18n():
    html = (REPO / "src" / "web" / "templates" / "membership.html").read_text(encoding="utf-8")
    assert "{% if is_trial_src and not q.exceeded %}" in html
    assert "{% if q.trial_hours_left is not none %}" in html
    assert "{% elif not q.trial_window_hours %}" in html
    assert "mb_trial_line_unlimited" in html and "mb_trial_line_not_started" in html
    from src.web.i18n_packs.membership import EN, ZH
    for k in ("mb_trial_line", "mb_trial_line_unlimited", "mb_trial_line_not_started"):
        assert k in ZH and k in EN, k
    assert "None" not in ZH["mb_trial_line_unlimited"]


def test_trial_window_hours_flows_from_quota_to_page():
    qs = (REPO / "src" / "licensing" / "quota_store.py").read_text(encoding="utf-8")
    assert 'out["trial_window_hours"] = snap.get("window_hours")' in qs
    mr = (REPO / "src" / "web" / "routes" / "membership_routes.py").read_text(encoding="utf-8")
    assert '"trial_window_hours": q.get("trial_window_hours")' in mr


def test_evaluate_none_hours_left_means_unlimited_window():
    from src.licensing.local_trial import LocalTrialState, evaluate
    st = LocalTrialState(first_seen=1000.0, last_seen=1000.0, window_hours=0.0, chars=100000)
    snap = evaluate(st, now=2000.0, used_chars=0)
    assert snap["hours_left"] is None and snap["active"] is True and snap["expired"] is False
    st2 = LocalTrialState(first_seen=1000.0, last_seen=1000.0, window_hours=48.0, chars=100000)
    assert evaluate(st2, now=2000.0, used_chars=0)["hours_left"] is not None


def test_main_startup_log_no_longer_prints_raw_none():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    block = src.split("🎁 首启体验档", 1)[0][-1400:] + src.split("🎁 首启体验档", 1)[1][:400]
    assert "剩 %s 小时" not in block
    assert '_win_txt = "不限时"' in block
    assert "未开始计时" in block


def test_domain_log_lines_carry_business_domain():
    sm = (REPO / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    assert "KB categories set from domain '%s' (business_domain=%s): %s" in sm
    ac = (REPO / "src" / "ai" / "ai_client.py").read_text(encoding="utf-8")
    assert "System prompt loaded from domain pack (business_domain=%s, %d chars)" in ac
    from src.skills.skill_manager import _business_domain_label
    bdm.reset_active_business_domain()
    try:
        bdm.set_active_business_domain("companion")
        assert _business_domain_label() == "companion/陪伴"
        bdm.set_active_business_domain("sales")
        assert _business_domain_label() == "sales/销售"
    finally:
        bdm.reset_active_business_domain()
