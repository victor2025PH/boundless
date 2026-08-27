# -*- coding: utf-8 -*-
"""nurture_ui_review 裁决纯函数门禁（口径先钉死，数据后到——iflt_ 裁决同款纪律）。

守住的不变量：
1. 纪元日常量格式合法且 ≤ 今天（裁决窗从埋点上线日起算，不是数据首现日）；
2. 链路自证优先级最高：满窗全零 → 只出 check_link，绝不下「没人用」结论；
3. 未满窗 → window insufficient 行在场（勿早裁）；
4. Q1 确认框：样本闸门（ask<5 不出比值）、拦截档（取消率≥15%）、零取消档措辞诚实；
5. Q2 探针满窗零使用 → 「保持折叠/并入文档」而非「加曝光」（低频≠无用）；
6. Q3 试点零使用 → 指路 audit 台账分流「未采用 vs 入口没被发现」；
7. Q4 高级区打开 >> 模拟启动 → 点名「考虑提升出折叠」。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.nurture_ui_review import (  # noqa: E402
    NTR_EPOCH_DAY,
    aggregate,
    observed_days,
    verdicts,
)


def _topics(vs):
    return {v["topic"]: v for v in vs}


def test_epoch_day_is_valid_and_not_future():
    d = date.fromisoformat(NTR_EPOCH_DAY)   # 格式非法直接抛
    assert d <= date.today()


def test_observed_days_inclusive_and_safe():
    assert observed_days("2026-08-22", "2026-08-22") == 1
    assert observed_days("2026-09-05", "2026-08-22") == 15
    assert observed_days("bogus", "2026-08-22") == 0


def test_full_window_all_zero_is_check_link_only():
    vs = verdicts({}, observed=20, min_days=14)
    assert len(vs) == 1 and vs[0]["status"] == "check_link"
    assert "查链路" in vs[0]["verdict"]
    assert "没人用" not in vs[0]["verdict"].split("而不是")[0]


def test_under_window_flags_insufficient():
    vs = verdicts({"ntr_save": 3}, observed=5, min_days=14)
    t = _topics(vs)
    assert "window" in t and t["window"]["status"] == "insufficient"


def test_confirm_ratio_gate_and_branches():
    # 样本不足：ask < 5 不出比值
    t = _topics(verdicts({"ntr_golive_ask": 3, "ntr_golive_confirm": 3},
                         observed=20, min_days=14))
    assert t["golive_confirm"]["status"] == "insufficient"
    # 拦截档：取消率 ≥15% → 判「在拦真实误操作」
    t = _topics(verdicts({"ntr_golive_ask": 10, "ntr_golive_cancel": 3,
                          "ntr_golive_confirm": 7}, observed=20, min_days=14))
    assert t["golive_confirm"]["status"] == "verdict"
    assert "拦真实误操作" in t["golive_confirm"]["verdict"]
    assert t["golive_confirm"]["numbers"]["cancel_ratio"] == 0.3
    # 零取消档：措辞必须诚实（威慑测不出），且不得据此加重阻力
    t = _topics(verdicts({"ntr_golive_ask": 8, "ntr_golive_confirm": 8},
                         observed=20, min_days=14))
    assert "零取消" in t["golive_confirm"]["verdict"]
    assert "不再加重" in t["golive_confirm"]["verdict"]


def test_probe_zero_usage_keeps_fold_no_exposure_push():
    t = _topics(verdicts({"ntr_save": 5}, observed=20, min_days=14))
    assert t["probe"]["status"] == "verdict"
    assert "勿加曝光" in t["probe"]["verdict"]
    # 有使用：真跑>0 → 验证纪律在执行
    t = _topics(verdicts({"ntr_probe_pre": 4, "ntr_probe_run": 2},
                         observed=20, min_days=14))
    assert "纪律在执行" in t["probe"]["verdict"]


def test_pilot_zero_usage_points_to_audit_ledger():
    t = _topics(verdicts({"ntr_save": 5}, observed=20, min_days=14))
    assert "nurture.set_canary" in t["pilot_ui"]["verdict"]
    t = _topics(verdicts({"ntr_pilot_on": 3, "ntr_pilot_off": 1},
                         observed=20, min_days=14))
    assert "收口成功" in t["pilot_ui"]["verdict"]


def test_advanced_fold_heavy_open_names_promotion():
    t = _topics(verdicts({"ntr_adv_open": 30, "ntr_sim_start": 2},
                         observed=20, min_days=14))
    assert "提升出折叠" in t["advanced_fold"]["verdict"]
    t = _topics(verdicts({"ntr_adv_open": 2, "ntr_sim_start": 6},
                         observed=20, min_days=14))
    assert "工作正常" in t["advanced_fold"]["verdict"]


def test_aggregate_shape():
    rows = [{"day": "2026-08-22", "action": "ntr_save", "n": 2},
            {"day": "2026-08-23", "action": "ntr_save", "n": 1},
            {"day": "2026-08-23", "action": "ntr_probe_pre", "n": 4}]
    agg = aggregate(rows)
    assert agg["totals"] == {"ntr_save": 3, "ntr_probe_pre": 4}
    assert agg["grand_total"] == 7
    assert agg["by_day"]["2026-08-23"]["ntr_probe_pre"] == 4
