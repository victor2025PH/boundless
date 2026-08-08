"""租户 AI 用量趋势台账门禁（src/ops/tenant_usage_trend.py 纯函数层）。

重点守三条：max 归并语义（网关按日累加，后采样恒 ≥ 先采样，乱序/回退不倒扣）、
升级判据的「不该误判」边界（非 IID 主体/无额度天/热天数不足）、提示 7 天去重。
全部内存字典，零文件零网络。
"""
from __future__ import annotations

import time

from src.ops import tenant_usage_trend as tut


def _ledger():
    return {"days": {}, "hints_sent": {}}


def test_upsert_max_semantics_and_budget_refresh():
    lg = _ledger()
    assert tut.upsert_sample(lg, "2026-08-08", "IID:a", 100, 1000) is True
    # 同日后采样更大 → 更新；更小（网关重置/乱序）→ 不倒扣
    assert tut.upsert_sample(lg, "2026-08-08", "IID:a", 500, 1000) is True
    assert tut.upsert_sample(lg, "2026-08-08", "IID:a", 300, 1000) is False
    assert lg["days"]["2026-08-08"]["IID:a"]["used"] == 500
    # 额度中途调整 → 记最新
    assert tut.upsert_sample(lg, "2026-08-08", "IID:a", 500, 2000) is True
    assert lg["days"]["2026-08-08"]["IID:a"]["budget"] == 2000
    # 空主体/坏数字拒收
    assert tut.upsert_sample(lg, "2026-08-08", "", 1, 1) is False
    assert tut.upsert_sample(lg, "2026-08-08", "IID:b", "x", 1) is False


def test_prune_keeps_recent_days():
    lg = _ledger()
    for i in range(70):
        day = f"2026-06-{i+1:02d}" if i < 30 else f"2026-07-{i-29:02d}"
        tut.upsert_sample(lg, day, "IID:a", 1, 100)
    removed = tut.prune_ledger(lg, "2026-07-40", keep_days=60)
    assert removed == 10 and len(lg["days"]) == 60
    # 掉的是最旧 10 天；「今天」是最新一天，必须保留
    assert "2026-06-01" not in lg["days"] and "2026-06-10" not in lg["days"]
    assert "2026-06-11" in lg["days"] and "2026-07-40" in lg["days"]


def test_upgrade_hints_judgement():
    lg = _ledger()
    days = [f"2026-08-{d:02d}" for d in range(1, 8)]  # 7 天窗
    for i, d in enumerate(days):
        # hot：4 天 ≥80%（850/1000），3 天 50%
        tut.upsert_sample(lg, d, "IID:hot", 850 if i < 4 else 500, 1000)
        # cool：天天 50%
        tut.upsert_sample(lg, d, "IID:cool", 500, 1000)
        # 非租户主体（装机指纹）永不提示
        tut.upsert_sample(lg, d, "D316-8D51-7107-F1CB", 999, 1000)
        # 无额度分母 → 不参与
        tut.upsert_sample(lg, d, "IID:nobudget", 999, 0)
    hints = tut.upgrade_hints(lg, "2026-08-07", days=7, hot_ratio=0.8, min_hot_days=3)
    assert [h["subject"] for h in hints] == ["IID:hot"]
    h = hints[0]
    assert h["hot_days"] == 4 and h["days_seen"] == 7 and h["budget"] == 1000
    # 热天数刚好差一天 → 不提示（min_hot_days=5）
    assert tut.upgrade_hints(lg, "2026-08-07", days=7, min_hot_days=5) == []


def test_hint_dedup_gap():
    lg = _ledger()
    now = time.time()
    assert tut.should_send_hint(lg, "IID:a", now) is True
    tut.mark_hint_sent(lg, "IID:a", now)
    assert tut.should_send_hint(lg, "IID:a", now + 6 * 86400) is False
    assert tut.should_send_hint(lg, "IID:a", now + 7.1 * 86400) is True
    assert tut.should_send_hint(lg, "IID:b", now) is True  # 各主体独立


def test_sample_due_throttle():
    lg = _ledger()
    now = time.time()
    assert tut.sample_due(lg, now) is True
    lg["last_sample_ts"] = int(now)
    assert tut.sample_due(lg, now + 60) is False
    assert tut.sample_due(lg, now + 1900) is True


def test_render_report_smoke():
    lg = _ledger()
    tut.upsert_sample(lg, "2026-08-07", "IID:acme", 900, 1000)
    tut.upsert_sample(lg, "2026-08-08", "IID:acme", 850, 1000)
    tut.upsert_sample(lg, "2026-08-06", "IID:acme", 820, 1000)
    out = tut.render_report(lg, "2026-08-08", days=7)
    assert "IID:acme" in out and "90%" in out
    assert "建议升级" in out  # 3 天 ≥80% 触发判据行
    assert "（台账为空" in tut.render_report(_ledger(), "2026-08-08")
