"""Stage L 每日仪式感主动问候：daily_ritual 纯函数 + 循环集成。"""

from __future__ import annotations

import time

import pytest

from src.integrations.companion_proactive import (
    CompanionProactiveLoop,
    JsonCooldownStore,
)
from src.utils.daily_ritual import (
    MORNING,
    NIGHT,
    current_slot,
    infer_active_hour,
    plan_daily_rituals,
    window_hours,
)

_H = 3600.0


def _at(hour, *, minute=0, day=19, month=6, year=2026):
    """构造本地时区某天某小时的时间戳（localtime(ts).tm_hour == hour）。"""
    return time.mktime(time.struct_time(
        (year, month, day, hour, minute, 0, 0, 0, -1)))


def _daykey(ts):
    return time.strftime("%Y%m%d", time.localtime(ts))


def _conv(cid, *, intimacy=40.0, last_ts=None, archived=False,
          memory_key="u:1", stage="steady", now=None):
    if last_ts is None:
        last_ts = (now or time.time()) - 48 * _H
    return {
        "conversation_id": cid, "platform": "telegram", "account_id": "acc1",
        "chat_key": "123", "last_ts": last_ts, "last_direction": "out",
        "archived": archived, "memory_key": memory_key, "stage": stage,
        "intimacy": intimacy, "last_emotion": "",
    }


def _opener(*, slot, memory_key, stage, intimacy, last_emotion="",
            last_emotion_intensity=-1.0, contact_key=""):
    return {"mode": f"ritual_{slot}", "directive": f"{slot} 问候", "fact": ""}


def _opener_block(**_kw):
    return {"mode": "", "directive": "", "blocked": "crisis_severe"}


def test_last_emotion_intensity_threaded_to_opener():
    # R：conv 的 last_emotion_intensity 透传进 opener_fn（供护栏强度分级）
    seen = {}

    def _cap(*, slot, memory_key, stage, intimacy, last_emotion="",
             last_emotion_intensity=-9.0, contact_key=""):
        seen["ei"] = last_emotion_intensity
        return {"mode": f"ritual_{slot}", "directive": "x", "fact": ""}

    now = _at(7)
    conv = _conv("c1", now=now)
    conv["last_emotion_intensity"] = 0.35
    plan_daily_rituals([conv], ritual_sent={}, opener_fn=_cap, now=now)
    assert seen.get("ei") == 0.35


# ── window_hours ─────────────────────────────────────────────────────────────

def test_window_hours_basic():
    assert window_hours((7, 10), default_start=7, default_end=10) == [7, 8, 9]
    assert window_hours((21, 24), default_start=21, default_end=24) == [21, 22, 23]


def test_window_hours_wrap_midnight():
    assert window_hours((23, 2), default_start=7, default_end=10) == [23, 0, 1]


def test_window_hours_bad_falls_back_to_default():
    assert window_hours(None, default_start=7, default_end=10) == [7, 8, 9]
    assert window_hours(("x",), default_start=21, default_end=24) == [21, 22, 23]


# ── current_slot ─────────────────────────────────────────────────────────────

def test_current_slot_morning_night_neither():
    assert current_slot(8, morning_window=(7, 10), night_window=(21, 24)) == MORNING
    assert current_slot(22, morning_window=(7, 10), night_window=(21, 24)) == NIGHT
    assert current_slot(14, morning_window=(7, 10), night_window=(21, 24)) is None


# ── infer_active_hour ────────────────────────────────────────────────────────

def test_infer_active_hour_morning_picks_modal_earliest_on_tie():
    # 7 与 8 各两次（并列）→ 晨档取最早 7
    assert infer_active_hour([7, 7, 8, 8, 14], MORNING) == 7


def test_infer_active_hour_night_picks_modal_latest_on_tie():
    # 21 与 23 各两次（并列）→ 晚档取最晚 23
    assert infer_active_hour([21, 21, 23, 23, 9], NIGHT) == 23


def test_infer_active_hour_no_samples_in_band():
    assert infer_active_hour([13, 14, 15], MORNING) is None
    assert infer_active_hour([], NIGHT) is None


# ── plan_daily_rituals：基础 ──────────────────────────────────────────────────

def test_morning_greeting_at_window_start_without_provider():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now)
    assert len(plans) == 1
    assert plans[0]["slot"] == MORNING
    assert plans[0]["mode"] == "ritual_morning"
    assert plans[0]["ritual_key"] == f"c1:{_daykey(now)}:morning"


def test_night_greeting():
    now = _at(21)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now)
    assert len(plans) == 1
    assert plans[0]["slot"] == NIGHT


def test_non_ritual_hour_returns_empty():
    now = _at(14)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now)
    assert plans == []


# ── plan_daily_rituals：护栏 ──────────────────────────────────────────────────

def test_low_intimacy_skipped():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", intimacy=5.0, now=now)], ritual_sent={},
        opener_fn=_opener, now=now, min_intimacy=20.0)
    assert plans == []


def test_already_greeted_this_slot_today_skipped():
    now = _at(7)
    key = f"c1:{_daykey(now)}:morning"
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={key: now - 1 * _H},
        opener_fn=_opener, now=now)
    assert plans == []


def test_recent_interaction_skipped():
    now = _at(7)
    # 1 小时前刚聊过 → 不必道早安（gap < 3h）
    plans = plan_daily_rituals(
        [_conv("c1", last_ts=now - 1 * _H, now=now)], ritual_sent={},
        opener_fn=_opener, now=now, min_quiet_gap_hours=3.0)
    assert plans == []


def test_pending_care_skipped():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now,
        has_pending_care=lambda cid: cid == "c1")
    assert plans == []


def test_archived_and_blank_cid_skipped():
    now = _at(7)
    convs = [_conv("c1", archived=True, now=now), _conv("", now=now)]
    plans = plan_daily_rituals(
        convs, ritual_sent={}, opener_fn=_opener, now=now)
    assert plans == []


def test_crisis_blocked_opener_skipped():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener_block, now=now)
    assert plans == []


# ── plan_daily_rituals：个性化择时 ───────────────────────────────────────────

def test_personalized_hour_fires_only_at_inferred_hour():
    # 历史活跃在 8 点 → 目标 8 点；当前 7 点（窗口内但非目标）→ 不发
    now7 = _at(7)
    plans7 = plan_daily_rituals(
        [_conv("c1", now=now7)], ritual_sent={}, opener_fn=_opener, now=now7,
        active_hours_provider=lambda cid: [8, 8, 8])
    assert plans7 == []
    # 当前 8 点 == 目标 → 发
    now8 = _at(8)
    plans8 = plan_daily_rituals(
        [_conv("c1", now=now8)], ritual_sent={}, opener_fn=_opener, now=now8,
        active_hours_provider=lambda cid: [8, 8, 8])
    assert len(plans8) == 1


def test_personalized_no_samples_falls_back_to_window_start():
    now = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now)], ritual_sent={}, opener_fn=_opener, now=now,
        active_hours_provider=lambda cid: [])
    assert len(plans) == 1  # 无历史 → 退回窗口起点 7 点，照常发


def test_personalized_inferred_outside_window_uses_window_start():
    # 推断活跃点 5 点（晨带内但不在问候窗口 [7,10)）→ 退回窗口起点 7
    now7 = _at(7)
    plans = plan_daily_rituals(
        [_conv("c1", now=now7)], ritual_sent={}, opener_fn=_opener, now=now7,
        active_hours_provider=lambda cid: [5, 5, 5])
    assert len(plans) == 1


# ── plan_daily_rituals：排序/截断 ────────────────────────────────────────────

def test_max_per_tick_and_intimacy_desc():
    now = _at(7)
    convs = [
        _conv("c1", intimacy=30.0, now=now),
        _conv("c2", intimacy=80.0, now=now),
        _conv("c3", intimacy=50.0, now=now),
    ]
    plans = plan_daily_rituals(
        convs, ritual_sent={}, opener_fn=_opener, now=now, max_per_tick=2)
    assert [p["conversation_id"] for p in plans] == ["c2", "c3"]


# ── 循环集成 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_sends_ritual_and_marks_ritual_cooldown(tmp_path):
    now = _at(7)
    sent = []

    async def _send(plan):
        sent.append(plan["conversation_id"])
        return True

    silence_cd = JsonCooldownStore(tmp_path / "cd.json")
    ritual_cd = JsonCooldownStore(tmp_path / "ritual.json")
    convs = [_conv("c1", now=now)]

    def _ritual_fn(cs, now_ts):
        return plan_daily_rituals(
            cs, ritual_sent=ritual_cd.snapshot(), opener_fn=_opener, now=now_ts)

    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=lambda **_k: {"mode": "", "directive": ""},  # 沉默路径不出
        send_fn=_send,
        cooldown_store=silence_cd,
        ritual_fn=_ritual_fn,
        ritual_cooldown=ritual_cd,
        now=lambda: now,
    )
    res = await loop.run_once()
    assert res == {"planned": 1, "sent": 1}
    assert sent == ["c1"]
    # 仪式冷却被记（每日每档键），沉默冷却未被记
    assert ritual_cd.snapshot().get(f"c1:{_daykey(now)}:morning") == now
    assert silence_cd.snapshot() == {}
    # 再跑一次：当日该档已发 → 不再发
    res2 = await loop.run_once()
    assert res2 == {"planned": 0, "sent": 0}


@pytest.mark.asyncio
async def test_loop_ritual_takes_priority_over_silence(tmp_path):
    """同一会话本 tick 既到仪式点又够沉默 → 只发一次（仪式优先）。"""
    now = _at(9)  # 9 点：非安静时段，沉默路径也会命中
    sent = []

    async def _send(plan):
        sent.append((plan["conversation_id"], plan["mode"]))
        return True

    silence_cd = JsonCooldownStore(tmp_path / "cd.json")
    ritual_cd = JsonCooldownStore(tmp_path / "ritual.json")
    convs = [_conv("c1", now=now)]

    def _silence_opener(*, memory_key, silent_hours, stage, intimacy, **_kw):
        return {"mode": "follow_up", "directive": "回访", "fact": "x"}

    def _ritual_fn(cs, now_ts):
        return plan_daily_rituals(
            cs, ritual_sent=ritual_cd.snapshot(), opener_fn=_opener, now=now_ts,
            morning_window=(9, 10))  # 让目标点 == 9

    loop = CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_silence_opener,
        send_fn=_send,
        cooldown_store=silence_cd,
        ritual_fn=_ritual_fn,
        ritual_cooldown=ritual_cd,
        now=lambda: now,
    )
    res = await loop.run_once()
    assert res["sent"] == 1
    assert sent == [("c1", "ritual_morning")]  # 仪式优先，不重复打扰


# ── 仪式未回退避（2026-08-18）─────────────────────────────────────────────────
# 语义：连续 ≥3 个仪式日零回复 → 阶梯降频（隔天→每4天→每周）+ 降频期每天至多
# 一档；对方任何入站即恢复每日节奏。streak 从既有冷却表 + 快照 last_in_ts 推导，
# 零新增持久化。

from src.utils.daily_ritual import (  # noqa: E402
    DEFAULT_BACKOFF_TIERS,
    parse_ritual_backoff_cfg,
    ritual_backoff_allows,
    ritual_backoff_stride,
    ritual_reply_streak_days,
)

_BK = {"enabled": True, "tiers": DEFAULT_BACKOFF_TIERS}


def _sent_past_days(cid, n_days, now, *, slot=MORNING):
    """构造冷却表：cid 在 now 往前 1..n_days 天各发过一档仪式。"""
    out = {}
    for d in range(1, int(n_days) + 1):
        ts = now - d * 86400.0
        out[f"{cid}:{_daykey(ts)}:{slot}"] = ts
    return out


def _cid_with_gate(day_key, stride, want, prefix="cw"):
    """找一个当日掷签结果确定为 want 的会话 id（确定性，绝不 flaky）。"""
    for i in range(200):
        cid = f"{prefix}{i}"
        if ritual_backoff_allows(cid, day_key, stride) is bool(want):
            return cid
    raise AssertionError("50/50 掷签 200 连败不可能：实现坏了")


def test_parse_backoff_cfg_defaults_enabled():
    cfg = parse_ritual_backoff_cfg({})
    assert cfg["enabled"] is True
    assert cfg["tiers"] == DEFAULT_BACKOFF_TIERS
    off = parse_ritual_backoff_cfg({"no_reply_backoff": {"enabled": False}})
    assert off["enabled"] is False


def test_parse_backoff_cfg_custom_tiers_filters_invalid():
    cfg = parse_ritual_backoff_cfg({"no_reply_backoff": {
        "tiers": [[5, 3], ["x"], [2, 1], [10, 5]]}})
    # stride 必须 >1、天数 >0；按天数降序
    assert cfg["tiers"] == ((10, 5), (5, 3))
    # 全非法 → 回默认阶梯
    bad = parse_ritual_backoff_cfg({"no_reply_backoff": {"tiers": [[0, 0]]}})
    assert bad["tiers"] == DEFAULT_BACKOFF_TIERS


def test_streak_counts_distinct_days_after_last_inbound():
    now = _at(7)
    entries = [(now - 86400.0, "20260618"), (now - 86400.0 + 60, "20260618"),
               (now - 2 * 86400.0, "20260617")]
    # 早晚双发同一天只算一天
    assert ritual_reply_streak_days(entries, 0.0) == 2
    # 对方在两天前之后回过话 → 只有更晚那天计入
    assert ritual_reply_streak_days(entries, now - 1.5 * 86400.0) == 1
    # 回话晚于全部发送 → 0
    assert ritual_reply_streak_days(entries, now) == 0
    assert ritual_reply_streak_days([], 0.0) == 0


def test_stride_ladder():
    for d, want in ((0, 1), (2, 1), (3, 2), (6, 2), (7, 4), (13, 4),
                    (14, 7), (100, 7)):
        assert ritual_backoff_stride(d) == want, d


def test_backoff_allows_deterministic_and_spreads():
    dk = "20260618"
    a = ritual_backoff_allows("c1", dk, 2)
    assert a == ritual_backoff_allows("c1", dk, 2)  # 同输入恒同结果
    assert ritual_backoff_allows("c1", dk, 1) is True  # 未降频恒放行
    assert ritual_backoff_allows("c1", "garbage", 7) is True  # 坏日期按放行
    # stride=7：任意连续 7 天里恰好一天放行
    import datetime as _d
    base = _d.date(2026, 6, 10)
    allowed = [
        ritual_backoff_allows("c9", (base + _d.timedelta(days=i)).strftime("%Y%m%d"), 7)
        for i in range(7)
    ]
    assert sum(allowed) == 1


def test_plan_backoff_below_threshold_daily_unchanged():
    now = _at(7)
    conv = _conv("cb1", now=now)
    conv["last_in_ts"] = 0.0
    sent = _sent_past_days("cb1", 2, now)  # 才 2 天，未达 3 天档
    plans = plan_daily_rituals(
        [conv], ritual_sent=sent, opener_fn=_opener, now=now, backoff_cfg=_BK)
    assert [p["conversation_id"] for p in plans] == ["cb1"]
    assert plans[0]["reply_streak_days"] == 2


def test_plan_backoff_stride_gates_by_day():
    now = _at(7)
    dk = _daykey(now)
    cid_no = _cid_with_gate(dk, 2, False)
    cid_yes = _cid_with_gate(dk, 2, True)
    for cid, want in ((cid_no, 0), (cid_yes, 1)):
        conv = _conv(cid, now=now)
        conv["last_in_ts"] = 0.0
        sent = _sent_past_days(cid, 3, now)  # streak=3 → 隔天档
        plans = plan_daily_rituals(
            [conv], ritual_sent=sent, opener_fn=_opener, now=now,
            backoff_cfg=_BK)
        assert len(plans) == want, (cid, want)


def test_plan_backoff_one_slot_per_day():
    now = _at(21)  # 晚安档目标小时=窗口起点 21（无活跃样本时）
    dk = _daykey(now)
    cid = _cid_with_gate(dk, 2, True)  # 今天轮到（不被掷签挡）
    conv = _conv(cid, now=now)
    conv["last_in_ts"] = 0.0
    sent = _sent_past_days(cid, 3, now)
    sent[f"{cid}:{dk}:{MORNING}"] = now - 12 * _H  # 今晨已发过
    plans = plan_daily_rituals(
        [conv], ritual_sent=sent, opener_fn=_opener, now=now, backoff_cfg=_BK)
    assert plans == []  # 降频期不再早晚双发
    # 退避关（None）＝旧行为：晨档已发不挡晚档
    plans_off = plan_daily_rituals(
        [conv], ritual_sent=sent, opener_fn=_opener, now=now, backoff_cfg=None)
    assert [p["conversation_id"] for p in plans_off] == [cid]


def test_plan_backoff_inbound_resets_to_daily():
    now = _at(7)
    dk = _daykey(now)
    cid = _cid_with_gate(dk, 2, False)  # 掷签今天本不轮到
    conv = _conv(cid, now=now)
    conv["last_in_ts"] = now - 1800.0  # 但对方半小时前刚回过话 → streak=0
    conv["last_ts"] = now - 4 * _H  # 且已过 quiet gap
    sent = _sent_past_days(cid, 5, now)
    plans = plan_daily_rituals(
        [conv], ritual_sent=sent, opener_fn=_opener, now=now, backoff_cfg=_BK)
    assert [p["conversation_id"] for p in plans] == [cid]
    assert plans[0]["reply_streak_days"] == 0


def test_plan_backoff_none_is_old_behavior():
    now = _at(7)
    dk = _daykey(now)
    cid = _cid_with_gate(dk, 7, False)  # 重退避档也不轮到的日子
    conv = _conv(cid, now=now)
    conv["last_in_ts"] = 0.0
    sent = _sent_past_days(cid, 20, now)  # 20 天零回复
    plans = plan_daily_rituals(
        [conv], ritual_sent=sent, opener_fn=_opener, now=now, backoff_cfg=None)
    assert [p["conversation_id"] for p in plans] == [cid]  # 不退避＝照发
