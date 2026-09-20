"""目标节奏档（pace.py）纯函数门禁。

不变量：natural 仍 1–180 天；session/today 是子日区间；关系模板拒限时；
slot_key 三档互不串；cap 计数 today 只算当日；sprint_push 不覆盖陪伴日 none。
"""

from __future__ import annotations

import time

from src.companion.goals.pace import (
    PACES,
    SPRINT_OK,
    beat_cap,
    clamp_deadline_days,
    count_beats_for_cap,
    default_pace_for_deadline,
    infer_pace_from_seconds,
    is_sprint,
    normalize_pace,
    pace_allowed,
    planner_thresholds,
    remaining_sec,
    resolve_pace,
    slot_key,
    sprint_ok,
    sprint_push,
    total_sec,
)
from src.companion.goals.planner import day_key


def test_normalize_and_allowlist():
    assert PACES == ("natural", "today", "session")
    assert normalize_pace("") == "natural"
    assert normalize_pace("SESSION") == "session"
    assert sprint_ok("custom") and sprint_ok("conversion_unlock")
    assert not sprint_ok("relationship_intimacy")
    assert not sprint_ok("engagement_reactivate")
    assert pace_allowed("custom", "session")
    assert not pace_allowed("relationship_stage", "session")
    assert pace_allowed("relationship_stage", "natural")
    assert is_sprint("today") and not is_sprint("natural")


def test_clamp_deadline_days_three_bands():
    # natural：沿用 1–180
    assert clamp_deadline_days("natural", 0, default_days=14) == 14.0
    assert clamp_deadline_days("natural", 0.04) == 1.0
    assert clamp_deadline_days("natural", 999) == 180.0
    # session：15–120 分钟
    sixty = clamp_deadline_days("session", 60 / 1440.0)
    assert abs(sixty - 60 / 1440.0) < 1e-9
    assert clamp_deadline_days("session", 0) == 60 / 1440.0
    lo = clamp_deadline_days("session", 1 / 1440.0)
    assert abs(lo - 15 / 1440.0) < 1e-9
    hi = clamp_deadline_days("session", 14)
    assert abs(hi - 120 / 1440.0) < 1e-9
    # today：2–12 小时
    eight = clamp_deadline_days("today", 8 / 24.0)
    assert abs(eight - 8 / 24.0) < 1e-9
    assert abs(clamp_deadline_days("today", 0) - 8 / 24.0) < 1e-9
    assert abs(clamp_deadline_days("today", 48) - 12 / 24.0) < 1e-9


def test_slot_key_natural_matches_planner():
    now = time.time()
    assert slot_key("natural", now) == day_key(now)


def test_slot_key_today_is_hour():
    now = time.mktime((2026, 8, 29, 14, 30, 0, 0, 0, -1))
    assert slot_key("today", now) == "2026-08-29T14"
    assert slot_key("today", now + 600) == "2026-08-29T14"
    later = time.mktime((2026, 8, 29, 15, 1, 0, 0, 0, -1))
    assert slot_key("today", later) == "2026-08-29T15"


def test_slot_key_session_follows_inbound():
    now = time.time()
    assert slot_key("session", now, 0) == "s:0"
    assert slot_key("session", now, 1710000000.9) == "s:1710000000"
    assert slot_key("session", now, 1710000000) != slot_key("session", now, 1710000060)


def test_infer_pace_from_span():
    assert infer_pace_from_seconds(45 * 60) == "session"
    assert infer_pace_from_seconds(8 * 3600) == "today"
    assert infer_pace_from_seconds(14 * 86400) == "natural"


def test_default_pace_for_deadline_create_entry():
    """#65 C1：建目标没给 pace → 按跨度落档；白名单外打回 natural。
    显式 pace（含显式 natural）尊重客户端。"""
    assert default_pace_for_deadline("custom", 60 / 1440.0) == "session"
    assert default_pace_for_deadline("custom", 8 / 24.0) == "today"
    assert default_pace_for_deadline("custom", 14.0) == "natural"
    assert default_pace_for_deadline("relationship_intimacy", 0.04) == "natural"
    assert default_pace_for_deadline("custom", 0.04, explicit="natural") == "natural"
    assert default_pace_for_deadline("custom", 14.0, explicit="session") == "session"


def test_resolve_pace_params_win_then_infer():
    g = {
        "template": "custom",
        "params": {"pace": "session"},
        "start_ts": 1.0,
        "deadline_ts": 1.0 + 14 * 86400,
    }
    assert resolve_pace(g) == "session"
    # 关系模板即使写了 session 也打回 natural
    g["template"] = "relationship_intimacy"
    assert resolve_pace(g) == "natural"
    # 无 params、子日跨度、白名单模板 → 回推
    g2 = {
        "template": "custom",
        "params": {},
        "start_ts": 100.0,
        "deadline_ts": 100.0 + 3600,
    }
    assert resolve_pace(g2) == "session"


def test_cap_and_thresholds():
    assert beat_cap("natural") == 0
    assert beat_cap("session") == 3
    assert beat_cap("today") == 4
    rows = [
        {"day": "2026-08-29T10"},
        {"day": "2026-08-29T11"},
        {"day": "2026-08-28T23"},
        {"day": "s:1"},
    ]
    now = time.mktime((2026, 8, 29, 12, 0, 0, 0, 0, -1))
    assert count_beats_for_cap(rows, "session", now) == 4
    assert count_beats_for_cap(rows, "today", now) == 2
    assert planner_thresholds("session") == {"backoff_after": 1, "halt_after": 2}
    assert planner_thresholds("natural") == {}


def test_sprint_push_does_not_override_care_none():
    assert sprint_push("session", 0, "soft") == "soft"
    assert sprint_push("session", 1, "soft") == "direct"
    assert sprint_push("session", 0, "none") == "none"
    assert sprint_push("today", 2, "soft") == "direct"
    assert sprint_push("natural", 5, "soft") == "soft"


def test_remaining_and_total_sec():
    now = 1_000_000.0
    g = {"start_ts": now - 600, "deadline_ts": now + 1800}
    assert abs(remaining_sec(g, now) - 1800) < 1e-6
    assert abs(total_sec(g) - 2400) < 1e-6
    assert remaining_sec({"deadline_ts": now - 10}, now) == 0.0


# ── P1 2026-08-30 推进力加度 ─────────────────────────────────────────────
def test_sprint_push_escalates_to_close_by_remaining():
    from src.companion.goals.pace import sprint_push as sp
    # 剩余 <35% → close，不论拍序（第 1 拍也收口）
    assert sp("today", 0, "soft", remaining_ratio=0.2) == "close"
    assert sp("session", 3, "soft", remaining_ratio=0.34) == "close"
    # 剩余充足 → 维持拍序档
    assert sp("today", 0, "soft", remaining_ratio=0.8) == "soft"
    assert sp("today", 1, "soft", remaining_ratio=0.8) == "direct"
    # 阈值可配
    assert sp("today", 0, "soft", remaining_ratio=0.5,
              escalate_at=0.6) == "close"
    # none（退避陪伴日）任何情况不覆盖
    assert sp("today", 0, "none", remaining_ratio=0.1) == "none"
    # natural 不参与
    assert sp("natural", 0, "soft", remaining_ratio=0.1) == "soft"
    # 坏 remaining 值 → 按无值处理
    assert sp("today", 1, "soft", remaining_ratio="x") == "direct"


def test_sprint_push_max_mode_first_beat_direct():
    from src.companion.goals.pace import sprint_push as sp
    assert sp("today", 0, "soft", mode="max") == "direct"
    assert sp("session", 0, "soft", mode="MAX") == "direct"
    # max 不影响收口升档与 none 让路
    assert sp("today", 0, "soft", remaining_ratio=0.1, mode="max") == "close"
    assert sp("today", 0, "none", mode="max") == "none"


def test_planner_thresholds_overrides_and_max():
    assert planner_thresholds(
        "today", overrides={"backoff_after": 2, "halt_after": 5},
    ) == {"backoff_after": 2, "halt_after": 5}
    # 部分覆写：只给 halt
    assert planner_thresholds(
        "session", overrides={"halt_after": 4},
    ) == {"backoff_after": 1, "halt_after": 4}
    # 坏值回基线
    assert planner_thresholds(
        "today", overrides={"backoff_after": "x"},
    ) == {"backoff_after": 1, "halt_after": 3}
    # 全力模式：退避/熔断双关（0=planner 的关闭语义）；natural 恒空
    assert planner_thresholds("today", mode="max") == {
        "backoff_after": 0, "halt_after": 0}
    assert planner_thresholds("natural", mode="max") == {}


def test_effective_beat_cap_overrides_and_max():
    from src.companion.goals.pace import effective_beat_cap as ec
    assert ec("today") == 4
    assert ec("session") == 3
    assert ec("natural") == 0
    assert ec("today", overrides={"today_cap": 6}) == 6
    assert ec("session", overrides={"session_cap": 5}) == 5
    assert ec("today", mode="max") == 6           # +2
    assert ec("today", overrides={"today_cap": 3}, mode="max") == 5
    assert ec("today", overrides={"today_cap": "bad"}) == 4


def test_slot_key_closing_half_hour_bucket():
    t1 = time.mktime((2026, 8, 30, 18, 10, 0, 0, 0, -1))
    t2 = time.mktime((2026, 8, 30, 18, 40, 0, 0, 0, -1))
    assert slot_key("today", t1).endswith("T18")
    assert slot_key("today", t1, closing=True).endswith("T18h0")
    assert slot_key("today", t2, closing=True).endswith("T18h1")
    # session/natural 不受 closing 影响
    assert slot_key("session", t1, 5.0, closing=True) == "s:5"
