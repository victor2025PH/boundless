"""P1 主动触达桥（bridge）门禁。

三闸语义：goals.enabled + bridge.enabled（默认关）→ 目标 autonomy=="auto" →
会话档位 auto_ai（有 inbox_store 时）。全过才把「今日拍」并进 plan["directive"]
并留 _goal_action_id 回执钩；on_proactive_sent 落 sent + beat_sent 事件。

桥用的是**单例** get_goal_store —— 每测先 reset_goal_store() 再
get_goal_store(":memory:") 种目标，autouse fixture 收尾复位；stats 用增量断言。
"""

from __future__ import annotations

import time

import pytest

from src.companion.goals.bridge import (
    augment_plan_with_goal,
    bridge_enabled,
    on_proactive_sent,
)
from src.companion.goals.planner import day_key
from src.companion.goals.stats import get_goal_stats
from src.companion.goals.store import get_goal_store, reset_goal_store

CONV = "telegram:a1:42"


@pytest.fixture(autouse=True)
def _hermetic_goal_env(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


def _cfg(bridge=True, goals=True):
    return {"companion": {"goals": {
        "enabled": goals, "db_path": ":memory:",
        "bridge": {"enabled": bridge},
    }}}


def _seed_goal(autonomy="auto"):
    store = get_goal_store(":memory:")
    goal = store.create_goal(
        conversation_id=CONV, platform="telegram", account_id="a1",
        chat_key="42", template="conversion_unlock", autonomy=autonomy,
        deadline_days=14)
    assert goal is not None
    return store, goal


def _plan():
    return {"conversation_id": CONV, "platform": "telegram",
            "account_id": "a1", "chat_key": "42", "directive": "基础指令"}


class _FakeInbox:
    """只带 get_automation_mode 的假 inbox store（其余方法缺失走软失败路径）。"""

    def __init__(self, mode):
        self._mode = mode

    def get_automation_mode(self, conversation_id):
        return self._mode


# ── bridge_enabled 三闸第一闸 ───────────────────────────────────────────────

def test_bridge_enabled_flags():
    assert bridge_enabled(_cfg(bridge=True, goals=True)) is True
    assert bridge_enabled(_cfg(bridge=False, goals=True)) is False   # 桥默认关
    assert bridge_enabled(_cfg(bridge=True, goals=False)) is False   # 总闸优先
    assert bridge_enabled({}) is False
    assert bridge_enabled(None) is False


# ── augment：增广与跳过 ─────────────────────────────────────────────────────

def test_bridge_disabled_leaves_plan_untouched():
    _seed_goal(autonomy="auto")
    plan = _plan()
    augment_plan_with_goal(_cfg(bridge=False), None, plan)
    assert plan["directive"] == "基础指令"
    assert "_goal_action_id" not in plan and "_goal_id" not in plan


def test_augment_auto_goal_appends_directive_and_consumes_beat():
    now = time.time()
    store, goal = _seed_goal(autonomy="auto")
    stats = get_goal_stats()
    p0 = stats.dump()["injected"]["proactive"]

    plan = _plan()
    augment_plan_with_goal(_cfg(), None, plan, now=now)
    assert plan["directive"].startswith("基础指令")     # 原 directive 保留
    assert "【工作目标衔接】" in plan["directive"]
    # 里程碑 0 push=none → 只陪伴力度提示
    assert "（今天只陪伴，营销内容只字不提）" in plan["directive"]
    assert plan["_goal_id"] == goal["goal_id"]
    act = store.get_action(goal["goal_id"], day_key(now))
    assert plan["_goal_action_id"] == act["action_id"]
    assert act["status"] == "consumed"
    assert stats.dump()["injected"]["proactive"] == p0 + 1


def test_suggest_autonomy_skipped():
    store, goal = _seed_goal(autonomy="suggest")
    plan = _plan()
    augment_plan_with_goal(_cfg(), None, plan)
    assert "_goal_action_id" not in plan
    assert plan["directive"] == "基础指令"
    assert store.list_actions(goal["goal_id"]) == []    # 档位闸在 refresh 之前


def test_automation_mode_review_skipped():
    _seed_goal(autonomy="auto")
    plan = _plan()
    augment_plan_with_goal(_cfg(), None, plan, inbox_store=_FakeInbox("review"))
    assert "_goal_action_id" not in plan
    assert plan["directive"] == "基础指令"


def test_automation_mode_auto_ai_passes():
    now = time.time()
    _seed_goal(autonomy="auto")
    plan = _plan()
    augment_plan_with_goal(_cfg(), None, plan,
                           inbox_store=_FakeInbox("auto_ai"), now=now)
    assert plan.get("_goal_action_id")
    assert "【工作目标衔接】" in plan["directive"]


def test_same_day_sent_action_not_reused():
    now = time.time()
    store, goal = _seed_goal(autonomy="auto")
    plan1 = _plan()
    augment_plan_with_goal(_cfg(), None, plan1, now=now)
    assert plan1.get("_goal_action_id")
    on_proactive_sent(plan1)                            # 今日拍已发出

    plan2 = _plan()
    augment_plan_with_goal(_cfg(), None, plan2, now=now)
    assert "_goal_action_id" not in plan2               # 同日不重复带
    assert "【工作目标衔接】" not in plan2["directive"]
    assert store.get_action(goal["goal_id"], day_key(now))["status"] == "sent"


def test_missing_conversation_keys_noop():
    _seed_goal(autonomy="auto")
    plan = {"directive": "基础指令"}                    # 无会话定位键
    augment_plan_with_goal(_cfg(), None, plan)
    assert "_goal_action_id" not in plan
    assert plan["directive"] == "基础指令"


# ── on_proactive_sent 回执 ──────────────────────────────────────────────────

def test_on_proactive_sent_marks_sent_and_logs_event():
    now = time.time()
    store, goal = _seed_goal(autonomy="auto")
    stats = get_goal_stats()
    s0 = stats.dump()["beats"]["sent_proactive"]

    plan = _plan()
    augment_plan_with_goal(_cfg(), None, plan, now=now)
    on_proactive_sent(plan)
    act = store.get_action(goal["goal_id"], day_key(now))
    assert act["status"] == "sent" and act["detail"] == "proactive"
    kinds = [e["kind"] for e in store.list_events(goal["goal_id"])]
    assert "beat_sent" in kinds
    assert stats.dump()["beats"]["sent_proactive"] == s0 + 1


def test_on_proactive_sent_noop_paths():
    on_proactive_sent({})                               # 无回执钩 → 静默
    on_proactive_sent(None)
    reset_goal_store()                                  # 单例未初始化 → 静默
    on_proactive_sent({"_goal_action_id": "x", "_goal_id": "g"})


# ── plan_priority 排序增益（只改顺序不改准入） ──────────────────────────────

def test_plan_priority_auto_goal_scores_one_plus_priority():
    from src.companion.goals.bridge import plan_priority
    store, goal = _seed_goal(autonomy="auto")
    assert plan_priority(_cfg(), None, _plan()) == 1.0 + 1.0   # 默认 priority=1
    assert store.update_goal_fields(goal["goal_id"], priority=3)
    assert plan_priority(_cfg(), None, _plan()) == 4.0


def test_plan_priority_zero_paths():
    from src.companion.goals.bridge import plan_priority
    # 桥关 / 总闸关 → 0
    _seed_goal(autonomy="auto")
    assert plan_priority(_cfg(bridge=False), None, _plan()) == 0.0
    assert plan_priority(_cfg(goals=False), None, _plan()) == 0.0
    # suggest 档不占名额
    reset_goal_store()
    _seed_goal(autonomy="suggest")
    assert plan_priority(_cfg(), None, _plan()) == 0.0
    # 无会话定位键 / 无目标 → 0
    assert plan_priority(_cfg(), None, {"directive": "x"}) == 0.0
    reset_goal_store()
    get_goal_store(":memory:")
    assert plan_priority(_cfg(), None, _plan()) == 0.0


def test_plan_priority_clamps_out_of_range():
    from src.companion.goals.bridge import plan_priority
    store, goal = _seed_goal(autonomy="auto")
    assert store.update_goal_fields(goal["goal_id"], priority=99)   # 越界钳到 9
    assert plan_priority(_cfg(), None, _plan()) == 10.0


def test_plan_priority_zero_when_today_beat_spent_or_rejected():
    """今日拍已发/被驳回 → 今天带不了意图，不再占名额增益（P2）。"""
    from src.companion.goals.bridge import plan_priority
    now = time.time()
    store, goal = _seed_goal(autonomy="auto")
    assert plan_priority(_cfg(), None, _plan()) == 2.0
    act = store.upsert_action(goal["goal_id"], day_key(now), intent="i")
    for st in ("sent", "skipped", "blocked"):
        store.mark_action(act["action_id"], st)
        assert plan_priority(_cfg(), None, _plan()) == 0.0
    store.mark_action(act["action_id"], "planned")       # 未消耗 → 增益恢复
    assert plan_priority(_cfg(), None, _plan()) == 2.0


def test_augment_skips_rejected_today_beat():
    """坐席驳回今日拍 → 主动开场当天不带目标意图（P2）。"""
    now = time.time()
    store, goal = _seed_goal(autonomy="auto")
    act = store.upsert_action(goal["goal_id"], day_key(now), intent="i")
    store.mark_action(act["action_id"], "skipped", detail="rejected:agent")
    plan = _plan()
    augment_plan_with_goal(_cfg(), None, plan, now=now)
    assert "_goal_action_id" not in plan
    assert plan["directive"] == "基础指令"


def test_plan_proactive_sends_priority_fn_reorders_not_admits():
    """目标会话优先占每 tick 名额（同名额下挤掉更沉默的普通会话），
    但准入护栏（沉默阈值）照旧——目标不豁免任何过滤。"""
    import time as _t

    from src.integrations.companion_proactive import plan_proactive_sends

    lt = _t.localtime()
    noon = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0,
                      lt.tm_wday, lt.tm_yday, -1))

    def _conv(cid, silent_h):
        return {"conversation_id": cid, "platform": "telegram",
                "account_id": "a1", "chat_key": cid.rsplit(":", 1)[-1],
                "last_ts": noon - silent_h * 3600.0, "last_direction": "out",
                "archived": False, "memory_key": cid}

    def _op(**kw):
        return {"mode": "gentle_checkin", "directive": "hi", "fact": ""}

    convs = [_conv("tg:a1:42", 6.0),     # 目标会话，沉默更短
             _conv("tg:a1:77", 30.0)]    # 普通会话，沉默更长

    def _prio(plan):
        return 2.0 if plan["conversation_id"] == "tg:a1:42" else 0.0

    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_op, now=noon,
        min_silent_hours=4, cooldown_hours=6, max_per_tick=1,
        quiet_start_hour=23, quiet_end_hour=8, priority_fn=_prio)
    assert [p["conversation_id"] for p in plans] == ["tg:a1:42"]
    assert plans[0]["goal_priority"] == 2.0

    # 准入不放宽：目标会话沉默 2h < 阈值 4h → 照样出局
    plans = plan_proactive_sends(
        [_conv("tg:a1:42", 2.0), _conv("tg:a1:77", 30.0)],
        cooldown_map={}, opener_fn=_op, now=noon,
        min_silent_hours=4, cooldown_hours=6, max_per_tick=1,
        quiet_start_hour=23, quiet_end_hour=8, priority_fn=_prio)
    assert [p["conversation_id"] for p in plans] == ["tg:a1:77"]

    # 回调抛异常 → 按 0 处理，退化为沉默时长降序
    def _boom(plan):
        raise RuntimeError("boom")

    plans = plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_op, now=noon,
        min_silent_hours=4, cooldown_hours=6, max_per_tick=2,
        quiet_start_hour=23, quiet_end_hour=8, priority_fn=_boom)
    assert [p["conversation_id"] for p in plans] == ["tg:a1:77", "tg:a1:42"]
    assert all(p["goal_priority"] == 0.0 for p in plans)
