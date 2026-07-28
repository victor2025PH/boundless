"""每日拍规划器（planner.plan_beat / day_key）+ 结算器（ledger.settle_goal）门禁。

纯函数零 IO：goal 用手工 dict、信号用手工 GoalSignals（signals 的 entitlement
识别辅助函数一并在此覆盖）。重点不变量：

- plan_beat 四态：正常拍 / 强负面情绪 hold（intensity 未知=-1 也 hold）/
  沉默熔断 halt / 退避纯陪伴拍；backoff_after=0 关退避；同 goal+day 确定性。
- settle_goal 每模板完成判定、deadline 过期分流（唤回=failed 其余=expired、
  done 优先于过期）、里程碑/进度**单调不回退**、changed 语义。
"""

from __future__ import annotations

import re
import time

from src.companion.goals.ledger import settle_goal
from src.companion.goals.planner import NEGATIVE_INTENSITY_HOLD, day_key, plan_beat
from src.companion.goals.signals import (
    GoalSignals,
    entitlement_tier,
    entitlement_unlocked,
)
from src.companion.goals.templates import CARE_INTENTS, get_template

NOW = 1_800_000_000.0
_DAY = 86400.0


def _goal(template="conversion_unlock", **kw):
    g = {
        "goal_id": "g1", "template": template, "status": "active",
        "milestone_idx": 0, "progress": 0.0, "result": "", "params": {},
        "start_ts": NOW - 3 * _DAY, "deadline_ts": NOW + 11 * _DAY,
    }
    g.update(kw)
    return g


def _settle(goal, signals, **kw):
    tid = goal["template"]
    return settle_goal(template_id=tid, template=get_template(tid) or {},
                       goal=goal, signals=signals, now=NOW, **kw)


def _sig(**kw):
    return GoalSignals(now=NOW, **kw)


# ── signals：识别辅助（纯函数） ─────────────────────────────────────────────

def test_goal_signals_unknown_sentinels():
    s = GoalSignals()
    assert s.intimacy == -1.0 and s.emotion_intensity == -1.0
    assert s.last_ts == 0.0 and s.last_inbound_ts == 0.0
    assert s.entitlement is None and s.funnel_stage == ""


def test_entitlement_unlocked_accepts_list_and_dict():
    assert entitlement_unlocked({"unlocked": ["bazi_reading"]}, "bazi_reading")
    assert entitlement_unlocked({"unlocked": {"bazi_reading": 1}}, "bazi_reading")
    assert entitlement_unlocked({"grants": {"bazi_reading": {"ts": 1}}}, "bazi_reading")
    assert entitlement_unlocked({"grants": ("bazi_reading",)}, "bazi_reading")
    assert not entitlement_unlocked({"unlocked": ["other"]}, "bazi_reading")
    assert not entitlement_unlocked(None, "bazi_reading")
    assert not entitlement_unlocked({"unlocked": ["x"]}, "")
    assert not entitlement_unlocked("not-a-dict", "x")


def test_entitlement_tier_normalizes():
    assert entitlement_tier({"tier": " VIP "}) == "vip"
    assert entitlement_tier({}) == ""
    assert entitlement_tier(None) == ""


# ── planner：day_key ────────────────────────────────────────────────────────

def test_day_key_format_and_value():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_key())
    ts = 1_700_000_000.0
    lt = time.localtime(ts)
    assert day_key(ts) == f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


# ── planner：plan_beat 四态 ─────────────────────────────────────────────────

class TestPlanBeat:
    def _plan(self, goal=None, signals=None, **kw):
        g = goal or _goal()
        return plan_beat(template=get_template(g["template"]), goal=g,
                         signals=signals or _sig(), day="2026-07-01", **kw)

    def test_normal_beat_intent_and_push(self):
        beat = self._plan()
        assert set(beat) == {"intent", "push_level"}
        assert beat["intent"]
        assert beat["push_level"] == "none"     # conversion_unlock 里程碑 0
        # 里程碑 2 + 参数代入 → direct 力度、意图含解锁项说法
        beat2 = self._plan(goal=_goal(milestone_idx=2,
                                      params={"item_label": "八字详批"}))
        assert beat2["push_level"] == "direct"
        assert "八字详批" in beat2["intent"]

    def test_deterministic_same_goal_same_day(self):
        assert self._plan() == self._plan()

    def test_negative_emotion_holds_including_unknown_intensity(self):
        # intensity 未知（-1 哨兵）也 hold —— 保守优先
        beat = self._plan(signals=_sig(negative_emotion=True,
                                       emotion_intensity=-1.0))
        assert beat == {"hold": "emotion"}
        # 达阈值 hold
        beat = self._plan(signals=_sig(negative_emotion=True,
                                       emotion_intensity=NEGATIVE_INTENSITY_HOLD))
        assert beat == {"hold": "emotion"}
        # 轻度负面（0 <= intensity < 0.5）→ 不 hold，正常出拍
        beat = self._plan(signals=_sig(negative_emotion=True,
                                       emotion_intensity=0.3))
        assert beat.get("intent")

    def test_silent_halt_at_threshold(self):
        assert self._plan(engaged_since_inbound=4) == {"hold": "silent"}
        assert self._plan(engaged_since_inbound=9) == {"hold": "silent"}

    def test_backoff_care_day(self):
        beat = self._plan(engaged_since_inbound=2)
        assert beat["push_level"] == "none"
        assert beat["intent"] in CARE_INTENTS
        assert self._plan(engaged_since_inbound=3)["intent"] in CARE_INTENTS

    def test_backoff_zero_disables_backoff(self):
        beat = self._plan(engaged_since_inbound=3, backoff_after=0)
        assert beat["intent"] not in CARE_INTENTS   # 不退避 → 正常模板意图
        # halt_after=0 同理关沉默熔断
        beat2 = self._plan(engaged_since_inbound=99, backoff_after=0, halt_after=0)
        assert beat2.get("intent")

    def test_empty_template_holds_no_intent(self):
        beat = plan_beat(template={}, goal=_goal(), signals=_sig(),
                         day="2026-07-01")
        assert beat == {"hold": "no_intent"}

    # ── P2：坐席驳回回流（recent_rejects 只降不升）──────────────────────────

    def test_reject_once_caps_direct_to_soft(self):
        g = _goal(milestone_idx=2)                  # conversion_unlock m2 = direct
        base = self._plan(goal=g)
        assert base["push_level"] == "direct"
        capped = self._plan(goal=g, recent_rejects=1)
        assert capped["push_level"] == "soft"
        assert capped["intent"] == base["intent"]   # 只降力度，不换今日意图

    def test_reject_once_leaves_non_direct_untouched(self):
        base = self._plan()                         # m0 = none
        assert self._plan(recent_rejects=1) == base
        g1 = _goal(milestone_idx=1)                 # m1 = soft
        assert self._plan(goal=g1, recent_rejects=1) == self._plan(goal=g1)

    def test_reject_twice_forces_care_day(self):
        beat = self._plan(goal=_goal(milestone_idx=2), recent_rejects=2)
        assert beat["push_level"] == "none"
        assert beat["intent"] in CARE_INTENTS

    def test_reject_does_not_override_holds(self):
        # 情绪红线/沉默熔断优先级不受回流影响（驳回是降档信号不是放行信号）
        beat = self._plan(signals=_sig(negative_emotion=True,
                                       emotion_intensity=0.9),
                          recent_rejects=1)
        assert beat == {"hold": "emotion"}
        assert self._plan(engaged_since_inbound=4,
                          recent_rejects=2) == {"hold": "silent"}


# ── ledger：conversion_unlock ───────────────────────────────────────────────

class TestSettleConversionUnlock:
    def test_unlock_arrival_completes(self):
        goal = _goal(params={"item_id": "bazi_reading"})
        for ent in ({"unlocked": ["bazi_reading"]},          # list 形
                    {"unlocked": {"bazi_reading": 1}},       # dict 形
                    {"grants": {"bazi_reading": {"n": 1}}}):
            res = _settle(goal, _sig(entitlement=ent))
            assert res["status"] == "done"
            assert res["progress"] == 1.0
            assert res["milestone_idx"] == 3
            assert res["result"] == "unlocked:bazi_reading"
            assert res["changed"] is True
            assert ("status", "done:unlocked:bazi_reading") in res["events"]

    def test_intimacy_milestones_25_40(self):
        goal = _goal(params={"item_id": "x"})
        res = _settle(goal, _sig(intimacy=25.0))
        assert res["status"] == "active" and res["milestone_idx"] == 1
        assert res["progress"] == round(1 / 4 * 0.9, 3)
        assert ("milestone", "0->1") in res["events"]
        res = _settle(goal, _sig(intimacy=40.0))
        assert res["milestone_idx"] == 2
        assert res["progress"] == round(2 / 4 * 0.9, 3)

    def test_funnel_engaged_advances_mi2(self):
        res = _settle(_goal(params={"item_id": "x"}), _sig(funnel_stage="engaged"))
        assert res["milestone_idx"] == 2

    def test_direct_beat_engaged_pushes_mi3(self):
        res = _settle(_goal(params={"item_id": "x"}), _sig(),
                      direct_beat_engaged=True)
        assert res["status"] == "active" and res["milestone_idx"] == 3
        assert res["progress"] == round(3 / 4 * 0.9, 3)

    def test_deadline_passed_expires(self):
        goal = _goal(params={"item_id": "x"}, deadline_ts=NOW - _DAY)
        res = _settle(goal, _sig())
        assert res["status"] == "expired"
        assert res["result"] == "deadline"
        assert ("status", "expired:deadline") in res["events"]
        assert res["changed"] is True

    def test_done_wins_over_expiry(self):
        goal = _goal(params={"item_id": "x"}, deadline_ts=NOW - _DAY)
        res = _settle(goal, _sig(entitlement={"unlocked": ["x"]}))
        assert res["status"] == "done" and res["progress"] == 1.0


# ── ledger：conversion_subscribe ────────────────────────────────────────────

class TestSettleConversionSubscribe:
    def test_tier_match_completes_case_insensitive(self):
        goal = _goal("conversion_subscribe", params={"tier": "vip"})
        res = _settle(goal, _sig(entitlement={"tier": "VIP"}))
        assert res["status"] == "done" and res["result"] == "subscribed:vip"

    def test_tier_mismatch_keeps_active_with_milestones(self):
        goal = _goal("conversion_subscribe", params={"tier": "vip"})
        res = _settle(goal, _sig(entitlement={"tier": "basic"}, intimacy=25.0))
        assert res["status"] == "active" and res["milestone_idx"] == 1


# ── ledger：relationship_stage ──────────────────────────────────────────────

class TestSettleRelationshipStage:
    def test_exact_stage_completes(self):
        goal = _goal("relationship_stage", params={"target_stage": "qualified"})
        res = _settle(goal, _sig(funnel_stage="qualified"))
        assert res["status"] == "done" and res["result"] == "stage:qualified"

    def test_beyond_target_completes(self):
        goal = _goal("relationship_stage", params={"target_stage": "qualified"})
        res = _settle(goal, _sig(funnel_stage="handed_off"))
        assert res["status"] == "done" and res["result"] == "stage:handed_off"

    def test_partial_progress_fraction(self):
        goal = _goal("relationship_stage", params={"target_stage": "qualified"})
        res = _settle(goal, _sig(funnel_stage="engaged"))   # idx 2 / 目标 idx 3
        assert res["status"] == "active"
        assert res["milestone_idx"] == 2                    # int(2/3*4)=2
        assert res["progress"] == round(2 / 3, 3)

    def test_unknown_stage_no_movement(self):
        goal = _goal("relationship_stage", params={"target_stage": "qualified"})
        res = _settle(goal, _sig())
        assert res["status"] == "active" and res["milestone_idx"] == 0
        assert res["progress"] == 0.0 and res["changed"] is False


# ── ledger：relationship_intimacy ───────────────────────────────────────────

class TestSettleRelationshipIntimacy:
    def test_target_reached_completes(self):
        goal = _goal("relationship_intimacy", params={"target_score": 55})
        res = _settle(goal, _sig(intimacy=55.0))
        assert res["status"] == "done"
        assert res["result"] == "intimacy:55"
        assert res["progress"] == 1.0

    def test_ninety_percent_reaches_mi3(self):
        goal = _goal("relationship_intimacy", params={"target_score": 50})
        res = _settle(goal, _sig(intimacy=46.0))    # 0.92 ≥ 0.9
        assert res["status"] == "active" and res["milestone_idx"] == 3
        assert res["progress"] == 0.92

    def test_partial_fraction_milestones(self):
        goal = _goal("relationship_intimacy", params={"target_score": 50})
        res = _settle(goal, _sig(intimacy=21.0))    # frac 0.42 → mi1
        assert res["milestone_idx"] == 1 and res["progress"] == 0.42
        assert ("milestone", "0->1") in res["events"]

    def test_unknown_intimacy_untouched(self):
        goal = _goal("relationship_intimacy", params={"target_score": 50})
        res = _settle(goal, _sig())                 # intimacy=-1 哨兵
        assert res["status"] == "active" and res["milestone_idx"] == 0
        assert res["progress"] == 0.0 and res["changed"] is False


# ── ledger：engagement_reactivate ───────────────────────────────────────────

class TestSettleEngagementReactivate:
    def test_inbound_after_start_completes_replied(self):
        goal = _goal("engagement_reactivate")               # start = NOW-3d
        res = _settle(goal, _sig(last_inbound_ts=NOW - _DAY))
        assert res["status"] == "done" and res["result"] == "replied"

    def test_inbound_before_start_not_done_time_progress(self):
        goal = _goal("engagement_reactivate", start_ts=NOW - 3 * _DAY,
                     deadline_ts=NOW + 7 * _DAY)            # 总 10 天，已过 3
        res = _settle(goal, _sig(last_inbound_ts=NOW - 5 * _DAY))
        assert res["status"] == "active"
        assert res["milestone_idx"] == 1                    # int(0.3*4)=1
        assert res["progress"] == round(0.3 * 0.8, 3)

    def test_deadline_passed_is_failed_not_expired(self):
        goal = _goal("engagement_reactivate", start_ts=NOW - 11 * _DAY,
                     deadline_ts=NOW - _DAY)
        res = _settle(goal, _sig())
        assert res["status"] == "failed"
        assert res["result"] == "deadline"
        assert ("status", "failed:deadline") in res["events"]


# ── ledger：custom ──────────────────────────────────────────────────────────

class TestSettleCustom:
    def test_time_only_progress_never_auto_done(self):
        # 无 deadline：走 default_days=14；已过 100 天 → 进度封顶 0.95、仍 active
        goal = _goal("custom", start_ts=NOW - 100 * _DAY, deadline_ts=0.0)
        res = _settle(goal, _sig())
        assert res["status"] == "active"                    # 绝不自动 done
        assert res["milestone_idx"] == 3
        assert res["progress"] == 0.95

    def test_custom_expires_after_deadline(self):
        goal = _goal("custom", start_ts=NOW - 20 * _DAY,
                     deadline_ts=NOW - 6 * _DAY)
        res = _settle(goal, _sig())
        assert res["status"] == "expired"                   # 过期而非 done


# ── ledger：retention_expand（P5 留存环，相位 7/15/24/30） ──────────────────

class TestSettleRetentionExpand:
    def test_activate_on_inbound_and_phase_floor(self):
        # 第 8 天购后开口 → 激活段推进 m1（信号与相位兑底同向）
        goal = _goal("retention_expand", start_ts=NOW - 8 * _DAY,
                     deadline_ts=NOW + 22 * _DAY)
        res = _settle(goal, _sig(last_inbound_ts=NOW - _DAY))
        assert res["status"] == "active"
        assert res["milestone_idx"] == 1
        # 全程没开口，第 16 天 → 纯相位兑底到「深化种草」段
        goal2 = _goal("retention_expand", start_ts=NOW - 16 * _DAY,
                      deadline_ts=NOW + 14 * _DAY)
        res2 = _settle(goal2, _sig())
        assert res2["milestone_idx"] == 2

    def test_direct_engaged_capped_by_phase_then_released(self):
        # 第 8 天 direct 拍被接：意愿到收口，但相位封顶（lookahead 1）压回 2
        goal = _goal("retention_expand", start_ts=NOW - 8 * _DAY,
                     deadline_ts=NOW + 22 * _DAY)
        res = _settle(goal, _sig(last_inbound_ts=NOW - _DAY),
                      direct_beat_engaged=True)
        assert res["milestone_idx"] == 2
        # 第 25 天同信号 → 续费收口段放行
        goal2 = _goal("retention_expand", start_ts=NOW - 25 * _DAY,
                      deadline_ts=NOW + 5 * _DAY)
        res2 = _settle(goal2, _sig(last_inbound_ts=NOW - _DAY),
                       direct_beat_engaged=True)
        assert res2["milestone_idx"] == 3

    def test_never_auto_done_expires_honestly(self):
        # 续费信号在站外（settle_order_ref/手动标成交），信号再热 ledger 也绝不
        # 自动 done；到期没续 = expired（诚实流失记录，不是 failed）
        goal = _goal("retention_expand", start_ts=NOW - 31 * _DAY,
                     deadline_ts=NOW - _DAY)
        res = _settle(goal, _sig(last_inbound_ts=NOW - 2 * _DAY, intimacy=90.0),
                      direct_beat_engaged=True)
        assert res["status"] == "expired"
        assert res["result"] == "deadline"


# ── ledger：单调性 + changed 语义 ───────────────────────────────────────────

class TestMonotonicityAndChanged:
    def test_progress_and_milestone_never_regress(self):
        # 里程碑/进度已在前（3 / 0.9），信号变弱 → 不回退、changed=False
        goal = _goal(milestone_idx=3, progress=0.9, params={"item_id": "x"})
        res = _settle(goal, _sig())                         # 全弱信号
        assert res["milestone_idx"] == 3
        assert res["progress"] == 0.9
        assert res["status"] == "active"
        assert res["changed"] is False
        assert res["events"] == []

    def test_changed_flips_then_settles_idempotent(self):
        goal = _goal("relationship_intimacy", params={"target_score": 50})
        res = _settle(goal, _sig(intimacy=21.0))
        assert res["changed"] is True
        # 把结算结果写回 goal 再结算一次 → 幂等，changed=False
        goal.update({k: res[k] for k in
                     ("status", "milestone_idx", "progress", "result")})
        res2 = _settle(goal, _sig(intimacy=21.0))
        assert res2["changed"] is False and res2["events"] == []
