"""实施84 P0-6：care 安静时段按客户当地时间顺延门禁。

覆盖：带时钟的纯函数换算（在/不在安静窗、跨夜窗、目标折回 epoch 的
本地差值算法）、clock=None 完全等于旧行为、时钟异常回落服务器钟、
派发器 user_clock_provider 透传与 provider 异常不阻断。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from src.companion.user_clock import UserClock
from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import (
    CareDispatcher,
    shift_out_of_quiet_hours,
)
from src.contacts.care_schedule import CareScheduleStore

BKK = ZoneInfo("Asia/Bangkok")  # UTC+7，无 DST


def _clock(trust="replace"):
    return UserClock(tz_name="Asia/Bangkok", offset_hours=7.0,
                     source="stated_city", confidence=0.9, country="TH",
                     city_slug="bangkok", trust=trust)


def _bkk_ts(h, m=0, day=17):
    return datetime(2026, 6, day, h, m, tzinfo=BKK).timestamp()


def test_user_clock_daytime_not_shifted():
    ts = _bkk_ts(14, 0)  # 曼谷 14:00，不在 23-8 窗
    assert shift_out_of_quiet_hours(ts, start_hour=23, end_hour=8,
                                    clock=_clock()) == ts


def test_user_clock_late_night_shifts_to_user_morning():
    ts = _bkk_ts(23, 30)  # 曼谷 23:30 → 顺延到曼谷次日 08:00
    out = shift_out_of_quiet_hours(ts, start_hour=23, end_hour=8,
                                   clock=_clock())
    expected = _bkk_ts(8, 0, day=18)
    assert abs(out - expected) < 1


def test_user_clock_early_morning_shifts_same_day():
    ts = _bkk_ts(3, 0)  # 曼谷 03:00 → 当日曼谷 08:00
    out = shift_out_of_quiet_hours(ts, start_hour=23, end_hour=8,
                                   clock=_clock())
    assert abs(out - _bkk_ts(8, 0)) < 1


def test_clock_none_keeps_server_behavior():
    # 与既有服务器钟用例同断言（回归钉：默认参数=旧行为）
    late = datetime(2026, 6, 17, 23, 30, 0).timestamp()
    out = shift_out_of_quiet_hours(late, start_hour=23, end_hour=8)
    d = datetime.fromtimestamp(out)
    assert (d.month, d.day, d.hour) == (6, 18, 8)


def test_bad_clock_falls_back_to_server():
    class _Broken:
        pass  # user_now 对未知对象回落服务器本地 → 行为与 clock=None 一致

    late = datetime(2026, 6, 17, 23, 30, 0).timestamp()
    out = shift_out_of_quiet_hours(late, start_hour=23, end_hour=8,
                                   clock=_Broken())
    d = datetime.fromtimestamp(out)
    assert (d.month, d.day, d.hour) == (6, 18, 8)


# ── 派发器集成 ───────────────────────────────────────────────────────────────
class _AI:
    async def chat(self, prompt, **kw):
        return "记得你说的面试，加油！"


def _due_store(due=None):
    s = CareScheduleStore(":memory:")
    due = float(due if due is not None else _bkk_ts(23, 40) - 60)
    s.add_commitment(
        CareCommitment(due_at=due, event_at=due, topic="面试",
                       sentiment="neutral", anchor_text="x",
                       source_text="y", confidence=0.85),
        contact_key="tg:u1", platform="telegram", chat_key="u1")
    return s


async def test_dispatcher_defers_by_user_clock():
    s = _due_store()
    rec = []

    async def _send(channel, account_id, chat_name, reply, defer_until,
                    reason, staleness, extra):
        rec.append(defer_until)
        return 3

    now = _bkk_ts(23, 40)  # 曼谷深夜（服务器时区无关紧要——按用户钟判）
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_send,
                       context_provider=lambda ck: "ctx",
                       quiet_start_hour=23, quiet_end_hour=8,
                       send_jitter_sec=(1.0, 2.0),
                       user_clock_provider=lambda item: _clock())
    assert await d.run_once(now=now) == 1
    # 顺延落点＝曼谷次日 08:00（±抖动 2s）
    assert abs(rec[0] - _bkk_ts(8, 0, day=18)) < 5


async def test_dispatcher_clock_provider_exception_not_blocking():
    now = _bkk_ts(12, 0)
    s = _due_store(due=now - 60)  # 已到期
    rec = []

    async def _send(*a, **k):
        rec.append(a)
        return 3

    def _boom(item):
        raise RuntimeError("resolver down")

    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_send,
                       context_provider=lambda ck: "ctx",
                       user_clock_provider=_boom)
    assert await d.run_once(now=now) == 1  # 照发（时钟 fail-open）
