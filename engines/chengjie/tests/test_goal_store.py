"""GoalStore（SQLite，:memory:）持久层门禁。

覆盖：goals CRUD 全链、find_active_goal 三级回落、update_goal_fields 白名单、
upsert_action (goal_id, day, kind) 幂等、mark_action 状态校验、
count_engaged_since 只数 consumed/sent、events 追加与 400 字截断、
count_active_for_conversation / list_goals / summary 聚合，及单例三件套。

注意：store 层**不校验模板名**（路由层才校验）——按实际行为断言未知模板仍会建。
"""

from __future__ import annotations

import time

import pytest

from src.companion.goals.store import (
    ACTION_STATUSES,
    MAX_ACTIVE_PER_CONVERSATION,
    GoalStore,
    get_goal_store,
    peek_goal_store,
    reset_goal_store,
)

NOW = 1_800_000_000.0
CONV = "telegram:a1:100"


@pytest.fixture()
def store():
    s = GoalStore(":memory:")
    yield s
    s.close()


def _mk(store, *, conv=CONV, platform="telegram", account_id="a1",
        chat_key="100", template="conversion_unlock", **kw):
    g = store.create_goal(
        conversation_id=conv, platform=platform, account_id=account_id,
        chat_key=chat_key, template=template, **kw)
    assert g is not None
    return g


# ── goals：create / get ─────────────────────────────────────────────────────

class TestCreateAndGet:
    def test_create_roundtrip_fields(self, store):
        g = _mk(store, title="冲一单", params={"item_id": "bazi_reading", "n": 1},
                autonomy="auto", priority=3, deadline_days=10, now=NOW)
        assert g["status"] == "active"
        assert g["template"] == "conversion_unlock"
        assert g["conversation_id"] == CONV
        assert g["platform"] == "telegram" and g["chat_key"] == "100"
        assert g["params"] == {"item_id": "bazi_reading", "n": 1}
        assert g["autonomy"] == "auto" and g["priority"] == 3
        assert g["milestone_idx"] == 0 and g["progress"] == 0.0
        assert g["start_ts"] == NOW
        assert g["deadline_ts"] == NOW + 10 * 86400.0
        assert store.get_goal(g["goal_id"]) == g

    def test_explicit_deadline_ts_wins_over_days(self, store):
        g = _mk(store, deadline_ts=NOW + 5.0, deadline_days=10, now=NOW)
        assert g["deadline_ts"] == NOW + 5.0

    def test_create_empty_template_rejected(self, store):
        assert store.create_goal(template="") is None
        assert store.create_goal(template="   ") is None

    def test_create_unknown_template_still_persists(self, store):
        # store 不校验模板名（路由层校验）——按实际行为断言
        g = _mk(store, template="no_such_template")
        assert g["template"] == "no_such_template" and g["status"] == "active"
        assert store.find_active_goal(conversation_id=CONV)["goal_id"] == g["goal_id"]

    def test_invalid_autonomy_falls_back_suggest(self, store):
        assert _mk(store, autonomy="banana")["autonomy"] == "suggest"

    def test_title_capped_and_created_event_logged(self, store):
        g = _mk(store, title="标" * 200)
        assert len(g["title"]) == 120
        events = store.list_events(g["goal_id"])
        assert [e["kind"] for e in events] == ["created"]
        assert events[0]["detail"] == "conversion_unlock"

    def test_get_goal_miss(self, store):
        assert store.get_goal("ghost") is None
        assert store.get_goal("") is None


# ── goals：find_active_goal 三级回落 ────────────────────────────────────────

class TestFindActiveGoal:
    def test_three_level_fallback(self, store):
        gid = _mk(store)["goal_id"]
        # 1) conversation_id 精确命中
        assert store.find_active_goal(conversation_id=CONV)["goal_id"] == gid
        # 2) platform + chat_key + account_id
        assert store.find_active_goal(
            platform="telegram", chat_key="100", account_id="a1")["goal_id"] == gid
        # 3) platform + chat_key（A 线拿不到 account_id）
        assert store.find_active_goal(
            platform="telegram", chat_key="100")["goal_id"] == gid

    def test_account_mismatch_falls_back_to_chat_scope(self, store):
        gid = _mk(store)["goal_id"]
        hit = store.find_active_goal(
            platform="telegram", chat_key="100", account_id="other")
        assert hit and hit["goal_id"] == gid

    def test_only_active_status_matches(self, store):
        g = _mk(store)
        store.update_goal_fields(g["goal_id"], status="paused")
        assert store.find_active_goal(conversation_id=CONV) is None
        assert store.find_active_goal(platform="telegram", chat_key="100") is None

    def test_prefers_higher_priority(self, store):
        _mk(store, priority=1)
        top = _mk(store, priority=5)
        assert store.find_active_goal(conversation_id=CONV)["goal_id"] == top["goal_id"]

    def test_no_criteria_returns_none(self, store):
        assert store.find_active_goal() is None


# ── goals：update_goal_fields 白名单 ────────────────────────────────────────

class TestUpdateGoalFields:
    def test_whitelist_updates_apply(self, store):
        g = _mk(store)
        assert store.update_goal_fields(
            g["goal_id"], status="paused", title="新标题", progress=0.5,
            milestone_idx=2, params={"a": 1}) is True
        cur = store.get_goal(g["goal_id"])
        assert cur["status"] == "paused" and cur["title"] == "新标题"
        assert cur["progress"] == 0.5 and cur["milestone_idx"] == 2
        assert cur["params"] == {"a": 1}
        assert cur["updated_at"] >= g["updated_at"]

    def test_non_whitelist_field_ignored(self, store):
        g = _mk(store)
        # chat_key 是真实列但不在白名单 → 一样被忽略
        assert store.update_goal_fields(g["goal_id"], chat_key="hijack") is False
        assert store.update_goal_fields(g["goal_id"], nonsense=1) is False
        assert store.get_goal(g["goal_id"])["chat_key"] == "100"

    def test_invalid_status_and_autonomy_dropped(self, store):
        g = _mk(store)
        assert store.update_goal_fields(g["goal_id"], status="bogus") is False
        assert store.update_goal_fields(g["goal_id"], autonomy="bogus") is False
        # 非法值混合法值：非法项被丢、合法项照常生效
        assert store.update_goal_fields(
            g["goal_id"], status="bogus", autonomy="bogus", title="T2") is True
        cur = store.get_goal(g["goal_id"])
        assert cur["status"] == "active" and cur["autonomy"] == "suggest"
        assert cur["title"] == "T2"

    def test_missing_goal_returns_false(self, store):
        assert store.update_goal_fields("ghost", title="x") is False


# ── goal_actions：每日拍 ────────────────────────────────────────────────────

class TestActions:
    def test_upsert_idempotent_keeps_first_row(self, store):
        gid = _mk(store)["goal_id"]
        a1 = store.upsert_action(gid, "2026-07-01", intent="one", push_level="none")
        a2 = store.upsert_action(gid, "2026-07-01", intent="two", push_level="direct")
        assert a1["action_id"] == a2["action_id"]
        assert a2["intent"] == "one" and a2["push_level"] == "none"   # 不覆盖
        assert len(store.list_actions(gid)) == 1

    def test_upsert_distinct_by_day_and_kind(self, store):
        gid = _mk(store)["goal_id"]
        store.upsert_action(gid, "2026-07-01", intent="b1")
        store.upsert_action(gid, "2026-07-02", intent="b2")
        store.upsert_action(gid, "2026-07-01", kind="care", intent="c1")
        acts = store.list_actions(gid)
        assert len(acts) == 3
        assert acts[0]["day"] == "2026-07-02"   # day DESC 排序

    def test_upsert_requires_goal_and_day(self, store):
        assert store.upsert_action("", "2026-07-01") is None
        assert store.upsert_action("gid", "") is None

    def test_upsert_invalid_status_normalized_to_planned(self, store):
        gid = _mk(store)["goal_id"]
        assert store.upsert_action(gid, "2026-07-01",
                                   status="exploded")["status"] == "planned"

    def test_mark_action_valid_invalid_and_missing(self, store):
        gid = _mk(store)["goal_id"]
        a = store.upsert_action(gid, "2026-07-01", intent="x")
        assert store.mark_action(a["action_id"], "banana") is False
        assert store.mark_action(a["action_id"], "consumed", detail="reply") is True
        cur = store.get_action(gid, "2026-07-01")
        assert cur["status"] == "consumed" and cur["detail"] == "reply"
        assert store.mark_action("ghost", "consumed") is False

    def test_get_action_scoped_by_kind(self, store):
        gid = _mk(store)["goal_id"]
        store.upsert_action(gid, "2026-07-01", kind="care", intent="c")
        assert store.get_action(gid, "2026-07-01") is None          # 默认 kind=beat
        assert store.get_action(gid, "2026-07-01", kind="care")["intent"] == "c"

    def test_count_engaged_since_only_consumed_or_sent(self, store):
        gid = _mk(store)["goal_id"]
        a1 = store.upsert_action(gid, "2026-07-01")
        a2 = store.upsert_action(gid, "2026-07-02")
        a3 = store.upsert_action(gid, "2026-07-03")
        store.upsert_action(gid, "2026-07-04")            # planned 不计
        store.mark_action(a1["action_id"], "consumed")
        store.mark_action(a2["action_id"], "sent")
        store.mark_action(a3["action_id"], "skipped")     # skipped 不计
        assert store.count_engaged_since(gid, 0.0) == 2
        assert store.count_engaged_since(gid, time.time() + 3600.0) == 0


# ── events / 聚合 ───────────────────────────────────────────────────────────

class TestEventsAndAggregates:
    def test_event_detail_truncated_to_400(self, store):
        gid = _mk(store)["goal_id"]
        store.add_event(gid, "note", "x" * 600)
        by_kind = {e["kind"]: e for e in store.list_events(gid)}
        assert len(by_kind["note"]["detail"]) == 400

    def test_events_append_and_limit(self, store):
        gid = _mk(store)["goal_id"]
        for i in range(5):
            store.add_event(gid, f"k{i}")
        assert len(store.list_events(gid, limit=3)) == 3
        kinds = {e["kind"] for e in store.list_events(gid, limit=50)}
        assert {"created", "k0", "k4"} <= kinds

    def test_count_active_for_conversation(self, store):
        g1 = _mk(store)
        _mk(store)
        assert store.count_active_for_conversation(CONV) == 2
        store.update_goal_fields(g1["goal_id"], status="cancelled")
        assert store.count_active_for_conversation(CONV) == 1
        assert store.count_active_for_conversation("other") == 0

    def test_list_goals_filters_and_limit(self, store):
        _mk(store)
        g2 = _mk(store, conv="line:a2:7", platform="line", account_id="a2",
                 chat_key="7", template="custom")
        store.update_goal_fields(g2["goal_id"], status="paused")
        assert len(store.list_goals()) == 2
        assert [g["goal_id"] for g in store.list_goals(status="paused")] == [g2["goal_id"]]
        assert [g["goal_id"] for g in store.list_goals(platform="line")] == [g2["goal_id"]]
        assert [g["goal_id"] for g in store.list_goals(account_id="a2")] == [g2["goal_id"]]
        assert len(store.list_goals(limit=1)) == 1

    def test_summary_shape(self, store):
        _mk(store)
        g2 = _mk(store, conv="line:a2:7", platform="line", chat_key="7",
                 template="custom")
        store.update_goal_fields(g2["goal_id"], status="done")
        s = store.summary()
        assert s["total"] == 2
        assert s["by_status"] == {"active": 1, "done": 1}
        assert s["active_by_template"] == {"conversion_unlock": 1}

    def test_constants(self):
        assert MAX_ACTIVE_PER_CONVERSATION == 3
        assert {"planned", "consumed", "sent", "skipped", "blocked"} == set(
            ACTION_STATUSES)


# ── P2：结果闭环（count_events_since / outcome_report）──────────────────────

class TestOutcomeReport:
    def test_count_events_since(self, store):
        gid = _mk(store)["goal_id"]
        store.add_event(gid, "beat_rejected", "too_pushy")
        store.add_event(gid, "beat_rejected", "off_tone")
        store.add_event(gid, "beat_adopted", "")
        assert store.count_events_since(gid, "beat_rejected", 0) == 2
        assert store.count_events_since(gid, "beat_adopted", 0) == 1
        assert store.count_events_since(gid, "beat_rejected",
                                        time.time() + 10) == 0
        assert store.count_events_since("ghost", "beat_rejected", 0) == 0

    def test_outcome_report_aggregates(self, store):
        now = time.time()
        # done：8 天达成（同模板再配一条 failed → done_rate 0.5）
        g1 = _mk(store, conv="c1", chat_key="1", now=now - 8 * 86400.0)
        store.update_goal_fields(g1["goal_id"], status="done", progress=1.0,
                                 done_at=now - 60)
        g2 = _mk(store, conv="c2", chat_key="2", now=now - 5 * 86400.0)
        store.update_goal_fields(g2["goal_id"], status="failed", progress=0.5,
                                 done_at=now - 30)
        # cancelled（运营叫停）：计数但**不进 done_rate 分母**
        g3 = _mk(store, conv="c3", chat_key="3", template="custom", now=now)
        store.update_goal_fields(g3["goal_id"], status="cancelled", done_at=now)
        # active：只进 active_now；窗口外终态：不计
        _mk(store, conv="c4", chat_key="4")
        g5 = _mk(store, conv="c5", chat_key="5", now=now - 90 * 86400.0)
        store.update_goal_fields(g5["goal_id"], status="done",
                                 done_at=now - 60 * 86400.0)
        store.upsert_action(g1["goal_id"], "2026-07-27", intent="i")
        store.add_event(g1["goal_id"], "beat_adopted", "")
        store.add_event(g2["goal_id"], "beat_rejected", "r")

        rep = store.outcome_report(now - 30 * 86400.0, now=now)
        assert rep["totals"] == {"done": 1, "failed": 1, "expired": 0,
                                 "cancelled": 1, "n": 3, "done_rate": 0.5,
                                 "avg_days_to_done": 8.0,
                                 "won": 0, "won_rate": 0.0}
        bt = rep["by_template"]["conversion_unlock"]
        assert bt["done"] == 1 and bt["failed"] == 1 and bt["n"] == 2
        assert bt["done_rate"] == 0.5
        assert bt["avg_days_to_done"] == 8.0
        # 终态里程碑分布（两条都停在 m0；cancelled 不计入）
        assert bt["milestone_dist"] == {"0": 2}
        assert "milestone_dist" not in rep["by_template"]["custom"]
        assert rep["by_template"]["custom"]["cancelled"] == 1
        assert rep["by_template"]["custom"]["done_rate"] == 0.0
        assert rep["beats"]["planned"] == 1
        assert rep["feedback"] == {"adopt": 1, "reject": 1}
        assert rep["active_now"] == 1
        # recent 按 done_at 降序，窗口外不入列
        ids = [r["goal_id"] for r in rep["recent"]]
        assert ids == [g3["goal_id"], g2["goal_id"], g1["goal_id"]]

    def test_outcome_report_empty_skeleton(self, store):
        rep = store.outcome_report(0)
        assert rep["totals"]["n"] == 0 and rep["totals"]["done_rate"] == 0.0
        assert rep["by_template"] == {} and rep["recent"] == []
        assert rep["feedback"] == {"adopt": 0, "reject": 0}
        # P23 新键在空库也保持骨架形状（消费方免判空分叉）
        assert rep["totals"]["won"] == 0 and rep["totals"]["won_rate"] == 0.0
        assert rep["drive_draft"] == {"total": 0, "by_source": {}}
        assert rep["profile_fills"] == {
            "total": 0, "by_src": {}, "by_track": {}}

    # ── P23：win-rate / 指令拟稿耐久事件 / 画像填充漏斗 ─────────────────────

    def test_outcome_report_won_rate_by_template(self, store):
        """won 只认 order:/manual:（winback done=回话≠成交不进 won）；
        分母与 done_rate 同（organic，排除 cancelled）。"""
        now = time.time()
        g1 = _mk(store, conv="c1", chat_key="1", now=now - 86400)
        store.update_goal_fields(g1["goal_id"], status="done",
                                 done_at=now - 60, result="order:pro:x1")
        g2 = _mk(store, conv="c2", chat_key="2", now=now - 86400)
        store.update_goal_fields(g2["goal_id"], status="done",
                                 done_at=now - 50, result="manual:agent")
        g3 = _mk(store, conv="c3", chat_key="3", now=now - 86400)
        store.update_goal_fields(g3["goal_id"], status="done",
                                 done_at=now - 40, result="replied")   # 回话≠成交
        g4 = _mk(store, conv="c4", chat_key="4", now=now - 86400)
        store.update_goal_fields(g4["goal_id"], status="failed",
                                 done_at=now - 30)
        rep = store.outcome_report(now - 7 * 86400.0, now=now)
        bt = rep["by_template"]["conversion_unlock"]
        assert bt["won"] == 2
        assert bt["won_rate"] == 0.5          # 2 won / 4 organic
        assert bt["done_rate"] == 0.75        # 3 done / 4 organic
        assert rep["totals"]["won"] == 2
        assert rep["totals"]["won_rate"] == 0.5

    def test_outcome_report_drive_draft_by_source(self, store):
        gid = _mk(store)["goal_id"]
        store.add_event(gid, "drive_draft", "beat")
        store.add_event(gid, "drive_draft", "beat")
        store.add_event(gid, "drive_draft", "hero")
        store.add_event(gid, "drive_draft", "")       # 空 detail 归 "-"
        store.add_event(gid, "beat_adopted", "")      # 别的 kind 不串
        rep = store.outcome_report(0)
        assert rep["drive_draft"]["total"] == 4
        assert rep["drive_draft"]["by_source"] == {
            "beat": 2, "hero": 1, "-": 1}
        # 窗口外不计
        rep2 = store.outcome_report(time.time() + 10)
        assert rep2["drive_draft"]["total"] == 0

    def test_outcome_report_profile_fills_window_and_buckets(self, store):
        now = time.time()
        # 窗口内：agent 手录 relation+bant 各一
        store.upsert_customer_profile(
            "telegram", "1", {"name": "阿龙", "need": "人手不够"},
            source="agent", overwrite=True, now=now - 60)
        # 窗口外：auto 采集（槽位自身 ts 判窗，不看行级 updated_at）
        store.upsert_customer_profile(
            "telegram", "2", {"location": "曼谷"},
            source="auto", now=now - 30 * 86400.0)
        rep = store.outcome_report(now - 7 * 86400.0, now=now)
        pf = rep["profile_fills"]
        assert pf["total"] == 2
        assert pf["by_src"] == {"agent": 2}
        assert pf["by_track"] == {"relation": 1, "bant": 1}


# ── 单例三件套 ──────────────────────────────────────────────────────────────

class TestSingletonTrio:
    def test_get_peek_reset(self):
        reset_goal_store()
        try:
            assert peek_goal_store() is None          # peek 从不创建
            s1 = get_goal_store(":memory:")
            assert get_goal_store() is s1             # 二次调用忽略参数返回同实例
            assert peek_goal_store() is s1
        finally:
            reset_goal_store()
        assert peek_goal_store() is None
