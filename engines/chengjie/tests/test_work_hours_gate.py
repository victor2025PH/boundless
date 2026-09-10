"""工作时间闸门判定核心（src/inbox/work_hours_gate.py）。

重点钉住安全语义：fail-open（闸门故障绝不闸死自动回复）、危机豁免、
跨午夜班次归属开始日、确定性边界抖动、账号覆写/豁免。
时间全部用显式时区 + 固定日期构造（2026-08-05 为周三），与跑测试的机器时区无关。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.inbox.work_hours_gate import (
    HOLD_REASON_OFF_HOURS,
    _jitter,
    in_work_hours,
    off_hours_cfg,
    resolve_entry,
    schedule_state,
    should_hold_auto_reply,
    work_schedule_cfg,
)

_SH = "Asia/Shanghai"


def _ts(y, m, d, hh, mm, tz=_SH):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp()


def _cfg(**over):
    cfg = {
        "enabled": True,
        "timezone": _SH,
        "default": {
            "workdays": [1, 2, 3, 4, 5, 6, 7],
            "start": "09:00",
            "end": "23:00",
        },
        "edge_jitter_min": 0,
        "crisis_bypass": True,
    }
    cfg.update(over)
    return cfg


# ── 总闸与基本窗口 ──────────────────────────────────────────


def test_disabled_never_holds():
    ws = _cfg(enabled=False)
    ts = _ts(2026, 8, 5, 3, 0)  # 凌晨 3 点
    assert in_work_hours(ws, "telegram", "default", ts) is True
    assert should_hold_auto_reply(ws, "telegram", "default", "在吗", ts) == ""


def test_same_day_window():
    ws = _cfg()
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 12, 0)) is True
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 9, 0)) is True  # 起点含
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 3, 0)) is False
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 8, 59)) is False
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 23, 0)) is False  # 终点不含
    assert should_hold_auto_reply(
        ws, "telegram", "a", "早", _ts(2026, 8, 5, 3, 0)) == HOLD_REASON_OFF_HOURS


def test_cross_midnight_window():
    ws = _cfg()
    ws["default"] = {"start": "22:00", "end": "06:00"}
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 23, 30)) is True
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 6, 3, 0)) is True  # 昨日班尾巴
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 6, 6, 0)) is False
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 12, 0)) is False


def test_cross_midnight_belongs_to_start_day():
    # 只有周五（2026-08-07）上班，班 22:00-06:00：
    # 周六凌晨 4 点是周五班次的尾巴=在班；周六晚 23 点不在班；周五凌晨 4 点不在班。
    ws = _cfg()
    ws["default"] = {"workdays": [5], "start": "22:00", "end": "06:00"}
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 8, 4, 0)) is True
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 8, 23, 0)) is False
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 7, 4, 0)) is False


def test_workdays_filter():
    ws = _cfg()
    ws["default"] = {"workdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 12, 0)) is True  # 周三
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 9, 12, 0)) is False  # 周日


def test_start_equals_end_means_always_open():
    ws = _cfg()
    ws["default"] = {"start": "00:00", "end": "00:00"}
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 3, 0)) is True
    assert resolve_entry(ws, "telegram", "a")["gated"] is False


# ── fail-open ───────────────────────────────────────────────


def test_fail_open_on_bad_window():
    ws = _cfg()
    ws["default"] = {"start": "9am", "end": "late"}
    ts = _ts(2026, 8, 5, 3, 0)
    assert in_work_hours(ws, "telegram", "a", ts) is True
    assert should_hold_auto_reply(ws, "telegram", "a", "hi", ts) == ""


def test_fail_open_on_garbage_cfg():
    ts = _ts(2026, 8, 5, 3, 0)
    assert in_work_hours(None, "telegram", "a", ts) is True
    assert in_work_hours("not-a-dict", "telegram", "a", ts) is True
    assert should_hold_auto_reply([], "telegram", "a", "hi", ts) == ""
    # 缺 default 块（enabled 开但没配班表）→ 不闸
    assert in_work_hours({"enabled": True}, "telegram", "a", ts) is True


def test_bad_timezone_is_deterministic_not_crashing():
    ws = _cfg(timezone="Mars/Olympus")
    ts = _ts(2026, 8, 5, 3, 0)
    r1 = in_work_hours(ws, "telegram", "a", ts)
    r2 = in_work_hours(ws, "telegram", "a", ts)
    assert isinstance(r1, bool) and r1 == r2  # 回落服务器本地钟，同刻同判


# ── 时区 ────────────────────────────────────────────────────


def test_timezone_resolution():
    # 同一物理时刻：UTC 01:00 = 上海 09:00。
    ts = _ts(2026, 8, 5, 1, 0, tz="UTC")
    assert in_work_hours(_cfg(timezone=_SH), "telegram", "a", ts) is True
    assert in_work_hours(_cfg(timezone="UTC"), "telegram", "a", ts) is False


def test_entry_timezone_overrides_global():
    ws = _cfg(timezone="UTC")
    ws["accounts"] = {"telegram:sh": {"timezone": _SH}}
    ts = _ts(2026, 8, 5, 1, 0, tz="UTC")  # 上海 09:00 / UTC 01:00
    assert in_work_hours(ws, "telegram", "sh", ts) is True
    assert in_work_hours(ws, "telegram", "other", ts) is False


# ── 账号覆写 ────────────────────────────────────────────────


def test_account_override_and_source():
    ws = _cfg()
    ws["accounts"] = {"telegram:night": {"start": "00:00", "end": "08:00"}}
    ts = _ts(2026, 8, 5, 3, 0)
    assert in_work_hours(ws, "telegram", "night", ts) is True
    assert in_work_hours(ws, "telegram", "default", ts) is False
    assert resolve_entry(ws, "telegram", "night")["source"] == "account"
    assert resolve_entry(ws, "telegram", "default")["source"] == "default"
    # 平台名大小写归一
    assert in_work_hours(ws, "Telegram", "night", ts) is True


def test_account_exempt():
    ws = _cfg()
    ws["accounts"] = {"telegram:vip": {"enabled": False}}
    ts = _ts(2026, 8, 5, 3, 0)
    assert in_work_hours(ws, "telegram", "vip", ts) is True
    assert should_hold_auto_reply(ws, "telegram", "vip", "在吗", ts) == ""


# ── 确定性边界抖动 ──────────────────────────────────────────


def test_jitter_deterministic_and_bounded():
    for day in ("2026-08-05", "2026-08-06", "2026-08-07"):
        for edge in ("start", "end"):
            j1 = _jitter("telegram:a", day, edge, 20)
            j2 = _jitter("telegram:a", day, edge, 20)
            assert j1 == j2
            assert -20 <= j1 <= 20
    assert _jitter("telegram:a", "2026-08-05", "start", 0) == 0
    # 不同日/不同账号的抖动应当有差异（确定性但非常量；固定种子下可断言）
    vals = {
        _jitter(f"telegram:acct{i}", f"2026-08-{5 + i:02d}", "start", 20)
        for i in range(8)
    }
    assert len(vals) > 1


def test_jitter_bounds_behavior():
    ws = _cfg(edge_jitter_min=20)
    # 抖动幅度 ±20：09:21 无论抖到哪都已开班；08:39 无论抖到哪都未开班
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 9, 21)) is True
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 8, 39)) is False
    # 23:21 无论抖到哪都已收班；22:39 无论抖到哪都未收班
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 23, 21)) is False
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 22, 39)) is True


def test_jitter_cross_midnight_no_flap():
    # 跨午夜班（22:00-06:00）抖动按开始日取键：午夜前后同一班次判定连续。
    ws = _cfg(edge_jitter_min=20)
    ws["default"] = {"start": "22:00", "end": "06:00"}
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 5, 23, 59)) is True
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 6, 0, 1)) is True
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 6, 5, 39)) is True
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 6, 6, 21)) is False


# ── 危机豁免 ────────────────────────────────────────────────


def test_crisis_bypass_severe_and_elevated():
    ws = _cfg()
    ts = _ts(2026, 8, 5, 3, 0)  # 休息中
    assert should_hold_auto_reply(ws, "telegram", "a", "我不想活了", ts) == ""
    assert should_hold_auto_reply(ws, "telegram", "a", "真的撑不下去了", ts) == ""
    assert should_hold_auto_reply(
        ws, "telegram", "a", "早安呀", ts) == HOLD_REASON_OFF_HOURS
    # 惯用语不豁免（累死了=日常夸张）
    assert should_hold_auto_reply(
        ws, "telegram", "a", "今天累死了", ts) == HOLD_REASON_OFF_HOURS


def test_crisis_bypass_can_be_disabled():
    ws = _cfg(crisis_bypass=False)
    ts = _ts(2026, 8, 5, 3, 0)
    assert should_hold_auto_reply(
        ws, "telegram", "a", "我不想活了", ts) == HOLD_REASON_OFF_HOURS


def test_in_hours_never_holds_regardless_of_text():
    ws = _cfg()
    ts = _ts(2026, 8, 5, 12, 0)
    assert should_hold_auto_reply(ws, "telegram", "a", "我不想活了", ts) == ""
    assert should_hold_auto_reply(ws, "telegram", "a", "", ts) == ""


# ── 快照（explain / status / watchdog 口径）─────────────────


def test_schedule_state_inside():
    ws = _cfg()
    ts = _ts(2026, 8, 5, 12, 0)
    st = schedule_state(ws, "telegram", "a", ts)
    assert st["enabled"] and st["gated"] and st["in_hours"]
    assert st["window"] == "09:00-23:00"
    assert st["next_change_kind"] == "close"
    assert st["shift_started_ts"] <= ts < st["next_change_ts"]
    assert st["timezone"] == _SH
    assert st["now_local"] == "12:00"


def test_schedule_state_outside():
    ws = _cfg()
    ts = _ts(2026, 8, 5, 3, 0)
    st = schedule_state(ws, "telegram", "a", ts)
    assert st["in_hours"] is False
    assert st["next_change_kind"] == "open"
    assert st["next_change_ts"] > ts
    # 下次开班应是今天 09:00（抖动 0）
    assert abs(st["next_change_ts"] - _ts(2026, 8, 5, 9, 0)) < 61


def test_schedule_state_disabled_or_exempt():
    st = schedule_state(_cfg(enabled=False), "telegram", "a", _ts(2026, 8, 5, 3, 0))
    assert st["enabled"] is False and st["gated"] is False and st["in_hours"] is True
    ws = _cfg()
    ws["accounts"] = {"telegram:vip": {"enabled": False}}
    st2 = schedule_state(ws, "telegram", "vip", _ts(2026, 8, 5, 3, 0))
    assert st2["gated"] is False and st2["in_hours"] is True


def test_schedule_state_workday_gap():
    # 周五 22:00-06:00 班；周六 12:00 处于休息，下次开班=周五…下周五 22:00 前后。
    ws = _cfg()
    ws["default"] = {"workdays": [5], "start": "22:00", "end": "06:00"}
    ts = _ts(2026, 8, 8, 12, 0)  # 周六中午
    st = schedule_state(ws, "telegram", "a", ts)
    assert st["in_hours"] is False
    assert st["next_change_kind"] == "open"
    assert abs(st["next_change_ts"] - _ts(2026, 8, 14, 22, 0)) < 61


# ── 配置解析辅助 ────────────────────────────────────────────


def test_work_schedule_cfg_dig():
    root = {"inbox": {"work_schedule": {"enabled": True}}}
    assert work_schedule_cfg(root) == {"enabled": True}
    assert work_schedule_cfg({}) == {}
    assert work_schedule_cfg(None) == {}
    assert work_schedule_cfg({"inbox": "oops"}) == {}


def test_off_hours_cfg_defaults_and_overrides():
    d = off_hours_cfg({})
    # Q-4（#267）：默认 0 = 过夜积压全部作废重写
    assert d == {
        "generate_drafts": True, "catch_up": True,
        "catch_up_regenerate_hours": 0.0, "catch_up_regenerate_all": True,
    }
    o = off_hours_cfg({"off_hours": {
        "generate_drafts": False, "catch_up": False,
        "catch_up_regenerate_hours": "6",
    }})
    assert o["generate_drafts"] is False
    assert o["catch_up"] is False
    assert o["catch_up_regenerate_hours"] == 6.0
    assert o["catch_up_regenerate_all"] is False
    assert off_hours_cfg({"off_hours": {
        "catch_up_regenerate_hours": "bad"}})["catch_up_regenerate_hours"] == 0.0


def test_workdays_empty_means_all_days():
    ws = _cfg()
    ws["default"] = {"workdays": [], "start": "09:00", "end": "23:00"}
    assert in_work_hours(ws, "telegram", "a", _ts(2026, 8, 9, 12, 0)) is True  # 周日
