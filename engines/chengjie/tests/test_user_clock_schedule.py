"""用户时钟接管主动触达调度：三个规划器的注入式改造门禁（纯函数、零 IO）。

覆盖两件事，缺一不可：
1. **向后兼容**——不传新参数时三个规划器逐位等价改造前（全局服务器钟早退、服务器
   day_key、服务器整点、服务器节日日历）；
2. **跨时区正确性**——传了 provider 后每会话各按对方的钟判晨/晚档、安静时段、节点整点、
   节日与生日日期，且 ``trust`` 三档的安全语义（narrow 只收窄、advisory 不参与调度）
   在规划器层面真的生效。

**机器无关**：不假设本机时区。所有「用户钟」按「相对服务器钟偏移 N 小时」的固定偏移时钟
构造（``_clock``），now 一律用 ``time.mktime`` 从**服务器本地**年月日时构造——于是
「服务器 14:00 而对方 07:00」这类断言在任何时区的机器上都成立。唯一用真 IANA 名
（Asia/Bangkok）的用例自带同偏移跳过守卫。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.companion.user_clock import (
    TRUST_ADVISORY,
    TRUST_NARROW,
    TRUST_REPLACE,
    UserClock,
)
from src.integrations.companion_proactive import plan_proactive_sends
from src.utils.daily_ritual import MORNING, NIGHT, plan_daily_rituals
from src.utils.milestone_ritual import plan_milestone_rituals

_H = 3600.0
_BKK = ZoneInfo("Asia/Bangkok")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _ts(hour, *, minute=0, day=19, month=6, year=2026):
    """服务器本地某年月日时的 epoch（``time.localtime(ts).tm_hour == hour``）。"""
    return time.mktime(time.struct_time(
        (year, month, day, hour, minute, 0, 0, 0, -1)))


def _server_offset(now):
    """该时刻服务器本地时区的 UTC 偏移（小时，DST 正确）。"""
    off = datetime.fromtimestamp(now, tz=timezone.utc).astimezone().utcoffset()
    return off.total_seconds() / 3600.0 if off else 0.0


def _server_day_key(now):
    return time.strftime("%Y%m%d", time.localtime(now))


def _fixed_name(off):
    sign = "+" if off >= 0 else "-"
    total = int(round(abs(float(off)) * 60))
    return f"UTC{sign}{total // 60:02d}:{total % 60:02d}"


def _clock(now, delta_hours, *, trust=TRUST_REPLACE, source="stated_city",
           country="TH"):
    """比服务器钟**快/慢 delta_hours 小时**的用户时钟（固定偏移，机器无关）。"""
    off = _server_offset(now) + float(delta_hours)
    return UserClock(
        tz_name=_fixed_name(off), offset_hours=off, source=source,
        confidence=0.9, country=country, city_slug="", trust=trust)


def _conv(cid="c1", *, intimacy=60.0, last_ts=None, now=None, archived=False,
          first_seen_ts=0.0, language="zh"):
    base_now = now if now is not None else time.time()
    return {
        "conversation_id": cid, "platform": "telegram", "account_id": "acc1",
        "chat_key": "123", "last_ts": (base_now - 48 * _H if last_ts is None
                                       else last_ts),
        "last_direction": "out", "archived": archived, "memory_key": "u:1",
        "stage": "steady", "intimacy": intimacy, "last_emotion": "",
        "first_seen_ts": first_seen_ts, "language": language,
    }


def _ritual_opener(*, slot, memory_key, stage, intimacy, last_emotion="",
                   last_emotion_intensity=-1.0, contact_key=""):
    return {"mode": f"ritual_{slot}", "directive": f"{slot} 问候", "fact": ""}


def _milestone_opener(**kw):
    et = str(kw.get("event_type") or "")
    if not et:
        return {"mode": "", "directive": ""}
    return {"mode": f"milestone_{et}", "directive": f"{et} 问候",
            "fact": "", "context_facts": []}


def _silence_opener(**kw):
    return {"mode": "gentle_checkin", "directive": "好久不见", "fact": ""}


def _boom(_cid):
    raise RuntimeError("provider 炸了")


# ---------------------------------------------------------------------------
# 一、向后兼容三连（不传新参数 = 逐位旧行为）
# ---------------------------------------------------------------------------

def test_daily_legacy_non_ritual_hour_returns_empty():
    now = _ts(14)
    assert plan_daily_rituals(
        [_conv(now=now)], ritual_sent={}, opener_fn=_ritual_opener, now=now) == []


def test_daily_legacy_morning_uses_server_clock_and_server_day_key():
    now = _ts(7)
    plans = plan_daily_rituals(
        [_conv(now=now)], ritual_sent={}, opener_fn=_ritual_opener, now=now)
    assert len(plans) == 1
    assert plans[0]["slot"] == MORNING
    assert plans[0]["ritual_key"] == f"c1:{_server_day_key(now)}:morning"
    # 观测字段：无时钟 → server / 0.0 / 服务器小时
    assert plans[0]["clock_source"] == "server"
    assert plans[0]["clock_offset"] == 0.0
    assert plans[0]["local_hour"] == 7


def test_daily_legacy_night_window_unchanged():
    now = _ts(22)
    plans = plan_daily_rituals(
        [_conv(now=now)], ritual_sent={}, opener_fn=_ritual_opener, now=now,
        night_window=(22, 24))
    assert len(plans) == 1 and plans[0]["slot"] == NIGHT


def test_proactive_legacy_quiet_hours_empty_whole_tick():
    now = _ts(2)  # 服务器 02:00，安静时段 23..8
    assert plan_proactive_sends(
        [_conv(now=now)], cooldown_map={}, opener_fn=_silence_opener,
        now=now) == []


def test_proactive_legacy_awake_hour_fires_with_server_observability():
    now = _ts(14)
    plans = plan_proactive_sends(
        [_conv(now=now)], cooldown_map={}, opener_fn=_silence_opener, now=now)
    assert len(plans) == 1
    assert plans[0]["clock_source"] == "server"
    assert plans[0]["clock_offset"] == 0.0
    assert plans[0]["local_hour"] == 14


def test_milestone_legacy_wrong_greet_hour_returns_empty():
    now = _ts(15, day=25, month=12)
    assert plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=10) == []


def test_milestone_legacy_holiday_from_config_calendar():
    now = _ts(10, day=25, month=12)
    plans = plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=10)
    assert len(plans) == 1
    assert plans[0]["ritual_key"] == "c1:ms:holiday:2026:12-25"
    assert plans[0]["event_label"] == "圣诞节"
    assert plans[0]["clock_source"] == "server"
    assert plans[0]["local_hour"] == 10


# ---------------------------------------------------------------------------
# 二、每日仪式：用户钟接管择时（本次改造的核心价值）
# ---------------------------------------------------------------------------

def test_daily_only_the_user_inside_own_morning_window_gets_plan():
    """服务器 14:00（谁都不该收早安）；A 那边 07:00 → **只有 A 出计划**。"""
    now = _ts(14)
    a_clock = _clock(now, -7)  # 比服务器慢 7 小时 → 本地 07:00
    plans = plan_daily_rituals(
        [_conv("A", now=now), _conv("B", now=now)],
        ritual_sent={}, opener_fn=_ritual_opener, now=now,
        user_clock_provider=lambda cid: a_clock if cid == "A" else None)
    assert [p["conversation_id"] for p in plans] == ["A"]
    assert plans[0]["slot"] == MORNING
    assert plans[0]["local_hour"] == 7
    assert plans[0]["clock_source"] == "stated_city"
    assert plans[0]["clock_offset"] == round(_server_offset(now) - 7, 1)


def test_daily_bangkok_replace_clock_real_iana_zone():
    now = _ts(14)
    bkk_hour = datetime.fromtimestamp(now, tz=_BKK).hour
    if bkk_hour == 14:
        pytest.skip("本机时区与曼谷同偏移，本用例无区分度")
    win = (bkk_hour, (bkk_hour + 1) % 24)
    clk = UserClock(
        tz_name="Asia/Bangkok", offset_hours=7.0, source="stated_city",
        confidence=0.92, country="TH", city_slug="bangkok", trust=TRUST_REPLACE)
    plans = plan_daily_rituals(
        [_conv("A", now=now), _conv("B", now=now)],
        ritual_sent={}, opener_fn=_ritual_opener, now=now,
        morning_window=win, night_window=win,
        user_clock_provider=lambda cid: clk if cid == "A" else None)
    assert [p["conversation_id"] for p in plans] == ["A"]
    assert plans[0]["local_hour"] == bkk_hour
    assert plans[0]["clock_offset"] == 7.0


def test_daily_narrow_clock_also_used_for_slot_selection():
    """narrow（行为推断）择时同样跟用户钟——越界防线在安静时段而非择时。"""
    now = _ts(14)
    clk = _clock(now, -7, trust=TRUST_NARROW, source="behavior")
    plans = plan_daily_rituals(
        [_conv("A", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=lambda cid: clk)
    assert len(plans) == 1 and plans[0]["clock_source"] == "behavior"


def test_daily_advisory_clock_does_not_move_the_window():
    """advisory（语种猜国家）不参与调度 → 仍按服务器钟，14:00 谁也不发。"""
    now = _ts(14)
    clk = _clock(now, -7, trust=TRUST_ADVISORY, source="lang_default")
    assert plan_daily_rituals(
        [_conv("A", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=lambda cid: clk) == []


def test_daily_ritual_key_uses_user_day_key_across_midnight():
    """服务器已是次日 00:30，对方仍是当日 23:30 → 去重键按**对方的那一天**。"""
    now = _ts(0, minute=30, day=28, month=7)
    assert _server_day_key(now) == "20260728"
    clk = _clock(now, -1)  # 慢 1 小时 → 07-27 23:30
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, night_window=(23, 24),
        user_clock_provider=lambda cid: clk)
    assert len(plans) == 1
    assert plans[0]["ritual_key"] == "c1:20260727:night"
    assert plans[0]["local_hour"] == 23


def test_daily_user_day_key_dedup_blocks_second_greeting():
    now = _ts(0, minute=30, day=28, month=7)
    clk = _clock(now, -1)
    assert plan_daily_rituals(
        [_conv("c1", now=now)],
        ritual_sent={"c1:20260727:night": now - _H},
        opener_fn=_ritual_opener, now=now, night_window=(23, 24),
        user_clock_provider=lambda cid: clk) == []
    # 服务器那一天的键不该顶掉对方那一天的问候（口径已换成用户钟）
    assert len(plan_daily_rituals(
        [_conv("c1", now=now)],
        ritual_sent={"c1:20260728:night": now - _H},
        opener_fn=_ritual_opener, now=now, night_window=(23, 24),
        user_clock_provider=lambda cid: clk)) == 1


# ---------------------------------------------------------------------------
# 三、每日仪式：个性化择时走 UTC 小时 → 用户钟本地小时
# ---------------------------------------------------------------------------

def test_daily_active_utc_hours_shifted_into_user_clock():
    """喂 UTC 小时：换算到对方本地 08:00 → 只在对方 08:00 问候。"""
    now = _ts(9)
    clk = _clock(now, -1)                      # 对方本地 08:00
    utc_h = int((8 - clk.offset_hours) % 24)   # 该 UTC 小时在对方钟下正是 08 点
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=lambda cid: clk,
        active_utc_hours_provider=lambda cid: [utc_h, utc_h, utc_h])
    assert len(plans) == 1
    assert plans[0]["local_hour"] == 8


def test_daily_active_utc_hours_not_at_target_hour_skipped():
    """同样样本，但此刻对方本地 07:00（窗口内、非目标点）→ 不发。"""
    now = _ts(8)
    clk = _clock(now, -1)                      # 对方本地 07:00
    utc_h = int((8 - clk.offset_hours) % 24)   # 目标点仍是对方 08:00
    assert plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=lambda cid: clk,
        active_utc_hours_provider=lambda cid: [utc_h, utc_h, utc_h]) == []


def test_daily_utc_provider_takes_precedence_over_legacy_provider():
    """两个 provider 同时给 → UTC 版优先（遗留服务器小时口径跨时区不正确）。"""
    now = _ts(9)
    clk = _clock(now, -1)                      # 对方本地 08:00
    utc_h = int((8 - clk.offset_hours) % 24)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=lambda cid: clk,
        active_hours_provider=lambda cid: [7, 7, 7],       # 会把目标点定到 7
        active_utc_hours_provider=lambda cid: [utc_h] * 3)  # 实际目标点 8
    assert len(plans) == 1 and plans[0]["local_hour"] == 8


def test_daily_utc_provider_empty_samples_falls_back_to_window_start():
    now = _ts(14)
    clk = _clock(now, -7)  # 对方本地 07:00 = 窗口起点
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=lambda cid: clk,
        active_utc_hours_provider=lambda cid: [])
    assert len(plans) == 1 and plans[0]["local_hour"] == 7


# ---------------------------------------------------------------------------
# 四、沉默回访：安静时段的 trust 三档语义
# ---------------------------------------------------------------------------

def test_proactive_narrow_clock_server_quiet_user_awake_still_skipped():
    """narrow **只收窄不新开**：服务器安静就算对方清醒也不发（安全语义）。"""
    now = _ts(2)
    clk = _clock(now, 12, trust=TRUST_NARROW, source="behavior")  # 对方 14:00
    assert plan_proactive_sends(
        [_conv(now=now)], cooldown_map={}, opener_fn=_silence_opener,
        now=now, user_clock_provider=lambda cid: clk) == []


def test_proactive_replace_clock_server_quiet_user_awake_can_send():
    """replace（显式信号）只看用户钟：服务器半夜但对方 14:00 → 可发。"""
    now = _ts(2)
    clk = _clock(now, 12)
    plans = plan_proactive_sends(
        [_conv(now=now)], cooldown_map={}, opener_fn=_silence_opener,
        now=now, user_clock_provider=lambda cid: clk)
    assert len(plans) == 1
    assert plans[0]["local_hour"] == 14
    assert plans[0]["clock_source"] == "stated_city"
    assert plans[0]["clock_offset"] == round(_server_offset(now) + 12, 1)


def test_proactive_advisory_clock_ignored_and_server_quiet_wins():
    now = _ts(2)
    clk = _clock(now, 12, trust=TRUST_ADVISORY, source="lang_default")
    assert plan_proactive_sends(
        [_conv(now=now)], cooldown_map={}, opener_fn=_silence_opener,
        now=now, user_clock_provider=lambda cid: clk) == []


def test_proactive_replace_clock_user_quiet_server_awake_skipped():
    """反向：服务器白天但对方凌晨 02:00 → 跳过（这正是本次改造要修的骚扰）。"""
    now = _ts(14)
    clk = _clock(now, 12)  # 对方 02:00
    assert plan_proactive_sends(
        [_conv(now=now)], cooldown_map={}, opener_fn=_silence_opener,
        now=now, user_clock_provider=lambda cid: clk) == []
    # 对照：不传 provider 时同一时刻是会发的（证明差异来自用户钟而非别的护栏）
    assert len(plan_proactive_sends(
        [_conv(now=now)], cooldown_map={}, opener_fn=_silence_opener,
        now=now)) == 1


def test_proactive_per_conversation_quiet_is_independent():
    """同一 tick 内各会话各判：对方白天的发、对方半夜的不发。"""
    now = _ts(14)
    awake = _clock(now, 0)     # 与服务器同步 → 14:00
    asleep = _clock(now, 12)   # 02:00
    plans = plan_proactive_sends(
        [_conv("A", now=now), _conv("B", now=now)], cooldown_map={},
        opener_fn=_silence_opener, now=now,
        user_clock_provider=lambda cid: awake if cid == "A" else asleep)
    assert [p["conversation_id"] for p in plans] == ["A"]


def test_proactive_silent_hours_and_pacing_untouched_by_clock():
    """时长差类护栏与时区无关：沉默不足仍不发（用户钟不该放宽它）。"""
    now = _ts(14)
    clk = _clock(now, 0)
    assert plan_proactive_sends(
        [_conv(now=now, last_ts=now - 2 * _H)], cooldown_map={},
        opener_fn=_silence_opener, now=now, min_silent_hours=24,
        user_clock_provider=lambda cid: clk) == []


# ---------------------------------------------------------------------------
# 五、纪念日/节日：地区节日 + 用户钟日期
# ---------------------------------------------------------------------------

def test_milestone_locale_holiday_hit_uses_key_and_localized_name():
    now = _ts(10, day=13, month=4)  # 非配置日历里的日子
    plans = plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=10,
        locale_holiday_provider=lambda cid: ("songkran", "宋干节"))
    assert len(plans) == 1
    assert plans[0]["event_type"] == "holiday"
    assert plans[0]["ritual_key"] == "c1:ms:holiday:2026:songkran"
    assert plans[0]["event_label"] == "宋干节"


def test_milestone_locale_holiday_none_falls_back_to_config_calendar():
    now = _ts(10, day=25, month=12)
    plans = plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=10,
        locale_holiday_provider=lambda cid: None)
    assert len(plans) == 1
    assert plans[0]["ritual_key"] == "c1:ms:holiday:2026:12-25"
    assert plans[0]["event_label"] == "圣诞节"


def test_milestone_locale_holiday_junk_return_falls_back():
    now = _ts(10, day=25, month=12)
    for junk in (("", ""), ("only-key",), "not-a-pair", 0):
        plans = plan_milestone_rituals(
            [_conv(now=now, first_seen_ts=now - 5 * 86400)], ritual_sent={},
            opener_fn=_milestone_opener, now=now, greet_hour=10,
            locale_holiday_provider=lambda cid, _j=junk: _j)
        assert len(plans) == 1, junk
        assert plans[0]["ritual_key"] == "c1:ms:holiday:2026:12-25"


def test_milestone_locale_provider_alone_keeps_server_hour_gate():
    """只给节日 provider（无时钟）→ 整点仍按服务器钟判，非整点不发。"""
    now = _ts(15, day=13, month=4)
    assert plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=10,
        locale_holiday_provider=lambda cid: ("songkran", "宋干节")) == []


def test_milestone_greet_hour_judged_per_conversation():
    """服务器 13:00：对方 10:00 的会话发，无时钟的会话不发（全局早退已下沉）。"""
    now = _ts(13)
    first = now - 100 * 86400
    clk = _clock(now, -3)
    plans = plan_milestone_rituals(
        [_conv("A", now=now, first_seen_ts=first),
         _conv("B", now=now, first_seen_ts=first)],
        ritual_sent={}, opener_fn=_milestone_opener, now=now, greet_hour=10,
        user_clock_provider=lambda cid: clk if cid == "A" else None)
    assert [p["conversation_id"] for p in plans] == ["A"]
    assert plans[0]["event_type"] == "anniversary"
    assert plans[0]["ritual_key"] == "A:ms:anniversary:100"
    assert plans[0]["local_hour"] == 10
    assert plans[0]["clock_source"] == "stated_city"


def test_milestone_holiday_date_follows_user_clock_across_midnight():
    """服务器已是 12-26 00:xx，对方仍是 12-25 23:xx → 按对方的日子过圣诞。"""
    now = _ts(0, minute=20, day=26, month=12)
    clk = _clock(now, -1)
    plans = plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=23,
        user_clock_provider=lambda cid: clk)
    assert len(plans) == 1
    assert plans[0]["ritual_key"] == "c1:ms:holiday:2026:12-25"


def test_milestone_birthday_judged_by_user_clock():
    """服务器已过生日次日 00:30，对方还在生日当天 23:30 → 仍该庆生。"""
    now = _ts(0, minute=30, day=6, month=3)
    clk = _clock(now, -1)
    plans = plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 50 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=23,
        birthday_provider=lambda mk: (3, 5),
        user_clock_provider=lambda cid: clk)
    assert len(plans) == 1
    assert plans[0]["event_type"] == "birthday"
    assert plans[0]["ritual_key"] == "c1:ms:birthday:2026"


def test_milestone_birthday_server_clock_would_have_missed_it():
    """同一时刻不传时钟 → 服务器已是 3/6，按旧路径就漏了（对照组）。"""
    now = _ts(0, minute=30, day=6, month=3)
    assert plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 50 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=0,
        birthday_provider=lambda mk: (3, 5)) == []


def test_milestone_birthday_leap_day_defers_to_28_in_common_year():
    now = _ts(10, day=28, month=2, year=2027)  # 2027 平年
    clk = _clock(now, 0)
    plans = plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 50 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=10,
        birthday_provider=lambda mk: (2, 29),
        user_clock_provider=lambda cid: clk)
    assert len(plans) == 1 and plans[0]["event_type"] == "birthday"
    assert plans[0]["ritual_key"] == "c1:ms:birthday:2027"


def test_milestone_birthday_leap_day_not_deferred_in_leap_year():
    now = _ts(10, day=28, month=2, year=2028)  # 2028 闰年 → 2/29 真实存在
    clk = _clock(now, 0)
    assert plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 50 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=10,
        birthday_provider=lambda mk: (2, 29),
        user_clock_provider=lambda cid: clk) == []


def test_milestone_days_known_is_timezone_independent():
    """认识天数是纯时长差：用户钟偏移多少都不改 100 天这个事实。"""
    now = _ts(13)
    first = now - 100 * 86400
    for delta in (-3, -3 - 24 * 0):  # 同一偏移两种写法，值必须一致
        plans = plan_milestone_rituals(
            [_conv(now=now, first_seen_ts=first)], ritual_sent={},
            opener_fn=_milestone_opener, now=now, greet_hour=10,
            user_clock_provider=lambda cid, _d=delta: _clock(now, _d))
        assert len(plans) == 1
        assert plans[0]["ritual_key"] == "c1:ms:anniversary:100"


# ---------------------------------------------------------------------------
# 六、provider 抛异常 → 按「无时钟」处理，绝不炸整个 tick
# ---------------------------------------------------------------------------

def test_daily_clock_provider_exception_degrades_to_server_clock():
    now = _ts(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=_boom)
    assert len(plans) == 1
    assert plans[0]["clock_source"] == "server"
    assert plans[0]["ritual_key"] == f"c1:{_server_day_key(now)}:morning"


def test_daily_utc_hours_provider_exception_falls_back_to_window_start():
    now = _ts(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=lambda cid: None,
        active_utc_hours_provider=_boom)
    assert len(plans) == 1 and plans[0]["local_hour"] == 7


def test_proactive_clock_provider_exception_degrades_to_server_clock():
    now = _ts(14)
    plans = plan_proactive_sends(
        [_conv(now=now)], cooldown_map={}, opener_fn=_silence_opener,
        now=now, user_clock_provider=_boom)
    assert len(plans) == 1 and plans[0]["clock_source"] == "server"
    # 服务器安静时段同样按服务器钟拦下（退化 = 旧行为，不是「一律放行」）
    quiet = _ts(2)
    assert plan_proactive_sends(
        [_conv(now=quiet)], cooldown_map={}, opener_fn=_silence_opener,
        now=quiet, user_clock_provider=_boom) == []


def test_milestone_providers_exception_degrade_to_server_and_calendar():
    now = _ts(10, day=25, month=12)
    plans = plan_milestone_rituals(
        [_conv(now=now, first_seen_ts=now - 5 * 86400)], ritual_sent={},
        opener_fn=_milestone_opener, now=now, greet_hour=10,
        user_clock_provider=_boom, locale_holiday_provider=_boom)
    assert len(plans) == 1
    assert plans[0]["ritual_key"] == "c1:ms:holiday:2026:12-25"
    assert plans[0]["clock_source"] == "server"


def test_all_guards_still_apply_under_user_clock():
    """接管调度不等于放宽护栏：归档/低亲密/care 让路/危机拦下逐一仍生效。"""
    now = _ts(14)
    clk = _clock(now, -7)  # 对方 07:00，本该发
    kw = dict(ritual_sent={}, opener_fn=_ritual_opener, now=now,
              user_clock_provider=lambda cid: clk)
    assert plan_daily_rituals([_conv("c1", now=now, archived=True)], **kw) == []
    assert plan_daily_rituals([_conv("c1", now=now, intimacy=5.0)], **kw) == []
    assert plan_daily_rituals(
        [_conv("c1", now=now, last_ts=now - 1 * _H)], **kw) == []
    assert plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_ritual_opener,
        now=now, user_clock_provider=lambda cid: clk,
        has_pending_care=lambda cid: True) == []
    assert plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={},
        opener_fn=lambda **_k: {"mode": "", "directive": "",
                                "blocked": "crisis_severe"},
        now=now, user_clock_provider=lambda cid: clk) == []


# ---------------------------------------------------------------------------
# 七、胶水层（proactive_topic）：provider 接线不能有 NameError / 漏传
# ---------------------------------------------------------------------------

_GLUE_SRC = (Path(__file__).resolve().parents[1]
             / "src" / "companion" / "proactive_topic.py").read_text("utf-8")


def test_glue_passes_providers_at_all_call_sites():
    """三处规划器调用点都要接上新 provider，且都以 ``_uc_enabled`` 为闸门。"""
    assert '"user_clock_provider": _user_clock' in _GLUE_SRC       # 每日仪式 + 节点
    assert '"active_utc_hours_provider"' in _GLUE_SRC              # 个性化择时走 UTC
    assert '"locale_holiday_provider": _locale_holiday' in _GLUE_SRC
    assert _GLUE_SRC.count("if _uc_enabled else {}") == 2          # 两个 ritual 规划器
    # 沉默回访：预览与真发（Loop）两处都传
    assert _GLUE_SRC.count(
        "user_clock_provider=_user_clock if _uc_enabled else None") == 2
    # 遗留 provider 必须保留（未启用用户时钟时那条路仍要个性化择时）
    assert "active_hours_provider=_active_hours if _personalize else None" in _GLUE_SRC
    # UTC 小时不许用已弃用的 utcfromtimestamp
    assert "utcfromtimestamp" not in _GLUE_SRC
    assert "datetime.fromtimestamp(ts, tz=timezone.utc).hour" in _GLUE_SRC


def test_glue_survives_user_clock_enabled(tmp_path):
    """开着 user_clock 时胶水层必须不炸（解析模块在位或缺失都一样）：
    预览照常挂上、候选为空（假 store 取不到会话）。守的是接线里的 NameError/签名错配
    ——那类错误会被 provider 的 try/except 吞成「永远推不出时钟」，静默退回旧行为。"""
    from src.companion.proactive_topic import maybe_start_companion_proactive

    a = MagicMock()
    a.config.config = {
        "companion": {
            "proactive_topic": {"enabled": False},
            "user_clock": {"enabled": True, "schedule": True, "min_samples": 12},
            "locale_holidays": {"enabled": True, "greet_user_side": True},
        },
    }
    a.config.config_path = str(tmp_path / "config.yaml")
    asyncio.run(maybe_start_companion_proactive(a))
    preview = a._web_app.state.companion_proactive_preview
    out = preview(limit=5)
    assert out["plans"] == [] and out["enabled"] is False
