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
