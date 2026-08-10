"""GoalStats 观测计数器门禁。

单例 get_goal_stats() 跨测试共享 → 本文件全部用 **GoalStats() 新实例** 测形状
与数值（单例只测「同一实例」契约不动数据）；dump()/dump_prom()/reset() 全覆盖。
"""

from __future__ import annotations

from src.companion.goals.stats import GoalStats, get_goal_stats


def test_fresh_dump_shape_and_inactive():
    st = GoalStats()
    d = st.dump()
    assert d["created"] == 0 and d["paused"] == 0
    assert d["terminal"] == {"done": 0, "failed": 0, "expired": 0, "cancelled": 0}
    assert d["beats"] == {"planned": 0, "hold_emotion": 0, "hold_silent": 0,
                          "sent_proactive": 0}
    assert d["injected"] == {"draft": 0, "reply": 0, "proactive": 0,
                             "opener": 0, "total": 0}
    assert d["settle_runs"] == 0 and d["milestones_advanced"] == 0
    assert d["since"] > 0
    assert d["active"] is False          # 零流量 → 看板卡可整卡隐藏


def test_record_created_and_terminal_buckets():
    st = GoalStats()
    st.record_created()
    st.record_created()
    st.record_terminal("done")
    st.record_terminal("failed")
    st.record_terminal("expired")
    st.record_terminal("cancelled")
    st.record_terminal("weird")          # 未知终态忽略不计
    d = st.dump()
    assert d["created"] == 2
    assert d["terminal"] == {"done": 1, "failed": 1, "expired": 1, "cancelled": 1}
    assert d["active"] is True


def test_record_beats_holds_and_paused():
    st = GoalStats()
    st.record_beat_planned()
    st.record_hold("emotion")
    st.record_hold("silent")
    st.record_hold("no_intent")          # 非 emotion 一律进 silent 桶
    st.record_beat_sent_proactive()
    st.record_paused()
    d = st.dump()
    assert d["beats"] == {"planned": 1, "hold_emotion": 1, "hold_silent": 2,
                          "sent_proactive": 1}
    assert d["paused"] == 1


def test_record_injected_chains_and_total():
    st = GoalStats()
    st.record_injected("draft")
    st.record_injected("proactive")
    st.record_injected("reply")
    st.record_injected("opener")         # P25：工坊「开新话题」链独立计数
    st.record_injected("")               # 未知链归 reply
    d = st.dump()
    assert d["injected"] == {"draft": 1, "reply": 2, "proactive": 1,
                             "opener": 1, "total": 5}
    assert d["active"] is True
    assert 'goals_injected_total{chain="opener"} 1' in st.dump_prom()


def test_record_settle_and_milestone_advances():
    st = GoalStats()
    st.record_settle(milestones_advanced=2)
    st.record_settle()
    st.record_settle(milestones_advanced=-3)   # 负数不倒扣
    d = st.dump()
    assert d["settle_runs"] == 3
    assert d["milestones_advanced"] == 2


def test_feedback_counters_and_prom():
    st = GoalStats()
    st.record_feedback("adopt")
    st.record_feedback("reject")
    st.record_feedback("reject")
    st.record_feedback("weird")            # 未知 verdict 忽略
    d = st.dump()
    assert d["feedback"] == {"adopt": 1, "reject": 2}
    prom = st.dump_prom()
    assert 'goals_beat_feedback_total{verdict="adopt"} 1' in prom
    assert 'goals_beat_feedback_total{verdict="reject"} 2' in prom
    st.reset()
    assert st.dump()["feedback"] == {"adopt": 0, "reject": 0}


def test_dump_prom_exposes_goal_metrics():
    st = GoalStats()
    st.record_created()
    st.record_beat_planned()
    st.record_injected("draft")
    st.record_terminal("done")
    prom = st.dump_prom()
    assert "goals_created_total 1" in prom
    assert 'goals_beats_total{kind="planned"} 1' in prom
    assert 'goals_injected_total{chain="draft"} 1' in prom
    assert 'goals_terminal_total{status="done"} 1' in prom
    assert "goals_milestones_advanced_total 0" in prom
    assert "# HELP goals_created_total" in prom
    assert "# TYPE goals_created_total counter" in prom
    assert prom.endswith("\n")


def test_reset_zeroes_everything():
    st = GoalStats()
    st.record_created()
    st.record_injected("draft")
    st.record_hold("emotion")
    st.record_settle(milestones_advanced=1)
    st.reset()
    d = st.dump()
    assert d["created"] == 0
    assert d["injected"]["total"] == 0
    assert d["beats"]["hold_emotion"] == 0
    assert d["settle_runs"] == 0 and d["milestones_advanced"] == 0
    assert d["active"] is False


def test_get_goal_stats_is_process_singleton():
    a = get_goal_stats()
    b = get_goal_stats()
    assert a is b
    assert isinstance(a, GoalStats)
