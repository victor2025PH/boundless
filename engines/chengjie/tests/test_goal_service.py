"""目标服务编排层（service）门禁。

覆盖：resolve_db_path 三态（:memory: / 显式相对挂 config 目录 / 缺省）、
goals_enabled / is_negative_emotion、refresh_goal 端到端（当日拍幂等规划、
负面情绪 hold 不建拍、过 deadline 结算成 expired + record_terminal）、
build_block_for_chat（disabled / 无目标 / observe 档 → None；有目标 → 组块 +
拍标记 consumed + record_injected）。

隔离约定：collect_signals 走真实降级路径——autouse fixture 清空
companion_context providers 与 protocol_bridge inbox getter，保证「信号未知」
保守行为确定；stats 是进程单例 → 全部用**增量**断言；goal store 单例每测复位。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.companion.goals.planner import day_key
from src.companion.goals.service import (
    DEFAULT_DB_NAME,
    build_block_for_chat,
    get_configured_store,
    goal_view,
    goals_enabled,
    is_negative_emotion,
    refresh_goal,
    resolve_db_path,
    resolve_goals_cfg,
)
from src.companion.goals.stats import get_goal_stats
from src.companion.goals.store import GoalStore, reset_goal_store


@pytest.fixture(autouse=True)
def _hermetic_goal_env(monkeypatch):
    """信号源清零 + 目标库单例复位——保证「信号未知」的确定性保守路径。"""
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


@pytest.fixture()
def mem_store():
    s = GoalStore(":memory:")
    yield s
    s.close()


def _enabled_cfg():
    return {"companion": {"goals": {"enabled": True}}}


def _new_goal(store, *, template="conversion_unlock", chat_key="500", **kw):
    conv = f"telegram:a1:{chat_key}"
    g = store.create_goal(
        conversation_id=conv, platform="telegram", account_id="a1",
        chat_key=chat_key, template=template, deadline_days=14, **kw)
    assert g is not None
    return g


# ── 配置解析 ────────────────────────────────────────────────────────────────

class TestResolveDbPath:
    def test_memory_passthrough(self):
        cfg = {"companion": {"goals": {"db_path": ":memory:"}}}
        assert resolve_db_path(cfg, "D:/somewhere/config.yaml") == ":memory:"

    def test_relative_mounts_under_config_dir(self, tmp_path):
        cfg = {"companion": {"goals": {"db_path": "goals.db"}}}
        out = resolve_db_path(cfg, str(tmp_path / "config.yaml"))
        assert out == str(tmp_path / "goals.db")

    def test_absolute_kept_verbatim(self, tmp_path):
        abs_db = tmp_path / "elsewhere" / "abs.db"
        cfg = {"companion": {"goals": {"db_path": str(abs_db)}}}
        out = resolve_db_path(cfg, str(tmp_path / "config.yaml"))
        assert out == str(abs_db)

    def test_default_lands_next_to_config(self, tmp_path):
        out = resolve_db_path({"companion": {"goals": {}}},
                              str(tmp_path / "config.yaml"))
        assert out == str(tmp_path / DEFAULT_DB_NAME)

    def test_default_without_config_path(self):
        assert resolve_db_path({}) == str(Path("config") / DEFAULT_DB_NAME)


def test_goals_enabled_and_cfg_resolution():
    assert goals_enabled({"companion": {"goals": {"enabled": True}}}) is True
    assert goals_enabled({"companion": {"goals": {}}}) is False
    assert goals_enabled({}) is False
    assert goals_enabled(None) is False
    assert goals_enabled("garbage") is False
    assert resolve_goals_cfg({"companion": {"goals": {"x": 1}}}) == {"x": 1}
    assert resolve_goals_cfg(None) == {}


def test_is_negative_emotion_vocabulary():
    assert is_negative_emotion("sad") is True
    assert is_negative_emotion(" ANGRY ") is True     # 大小写/空白归一
    assert is_negative_emotion("happy") is False
    assert is_negative_emotion("") is False
    assert is_negative_emotion(None) is False


# ── refresh_goal 端到端（:memory: store + enabled cfg） ────────────────────

class TestRefreshGoal:
    def test_plans_today_beat_idempotently(self, mem_store):
        now = time.time()
        goal = _new_goal(mem_store)
        stats = get_goal_stats()
        planned0 = stats.dump()["beats"]["planned"]

        res = refresh_goal(mem_store, _enabled_cfg(), goal, now=now)
        assert res["hold"] is None
        act = res["action"]
        assert act is not None and act["intent"]
        assert act["day"] == day_key(now)
        assert act["push_level"] == "none"          # conversion_unlock 里程碑 0
        assert stats.dump()["beats"]["planned"] == planned0 + 1

        # 幂等：同日再 refresh → 同一拍、不重复建行、不重复计数
        res2 = refresh_goal(mem_store, _enabled_cfg(), res["goal"], now=now)
        assert res2["action"]["action_id"] == act["action_id"]
        assert len(mem_store.list_actions(goal["goal_id"])) == 1
        assert stats.dump()["beats"]["planned"] == planned0 + 1

    def test_negative_emotion_holds_and_creates_no_beat(self, mem_store):
        now = time.time()
        goal = _new_goal(mem_store, chat_key="501")
        stats = get_goal_stats()
        h0 = stats.dump()["beats"]["hold_emotion"]

        res = refresh_goal(mem_store, _enabled_cfg(), goal,
                           negative_emotion=True, now=now)
        assert res["hold"] == "emotion"
        assert res["action"] is None
        assert mem_store.get_action(goal["goal_id"], day_key(now)) is None
        assert stats.dump()["beats"]["hold_emotion"] == h0 + 1

    def test_past_deadline_expires_and_records_terminal(self, mem_store):
        now = time.time()
        goal = _new_goal(mem_store, chat_key="502")
        assert mem_store.update_goal_fields(goal["goal_id"],
                                            deadline_ts=now - 3600.0)
        goal = mem_store.get_goal(goal["goal_id"])
        stats = get_goal_stats()
        e0 = stats.dump()["terminal"]["expired"]

        res = refresh_goal(mem_store, _enabled_cfg(), goal, now=now)
        assert res["goal"]["status"] == "expired"
        assert res["action"] is None and res["hold"] is None
        cur = mem_store.get_goal(goal["goal_id"])
        assert cur["status"] == "expired" and cur["done_at"] > 0
        assert stats.dump()["terminal"]["expired"] == e0 + 1
        kinds = {e["kind"] for e in mem_store.list_events(goal["goal_id"])}
        assert "status" in kinds                    # 状态变迁进事件台账


# ── P2：坐席驳回回流（事件 → planner 降档；驳回日停注入）────────────────────

class TestRejectFeedbackFlow:
    def _direct_goal(self, mem_store, chat_key):
        goal = _new_goal(mem_store, chat_key=chat_key)
        # conversion_unlock 里程碑 2 = direct 档（settle 单调不回退，信号未知不动）
        assert mem_store.update_goal_fields(goal["goal_id"], milestone_idx=2)
        return mem_store.get_goal(goal["goal_id"])

    def test_refresh_caps_push_after_one_reject(self, mem_store):
        now = time.time()
        goal = self._direct_goal(mem_store, "601")
        mem_store.add_event(goal["goal_id"], "beat_rejected", "too_pushy")
        res = refresh_goal(mem_store, _enabled_cfg(), goal, now=now)
        assert res["action"]["push_level"] == "soft"      # direct 被封顶

    def test_refresh_two_rejects_forces_care_day(self, mem_store):
        now = time.time()
        goal = self._direct_goal(mem_store, "602")
        mem_store.add_event(goal["goal_id"], "beat_rejected", "a")
        mem_store.add_event(goal["goal_id"], "beat_rejected", "b")
        res = refresh_goal(mem_store, _enabled_cfg(), goal, now=now)
        assert res["action"]["push_level"] == "none"      # 退避陪伴日

    def test_rejects_outside_lookback_ignored(self, mem_store):
        now = time.time()
        goal = self._direct_goal(mem_store, "603")
        # 30 天前的驳回（超出默认 7 天回看窗）→ 不影响今天
        mem_store._conn.execute(
            "INSERT INTO goal_events (goal_id, kind, detail, ts)"
            " VALUES (?,?,?,?)",
            (goal["goal_id"], "beat_rejected", "old", now - 30 * 86400.0))
        mem_store._conn.commit()
        res = refresh_goal(mem_store, _enabled_cfg(), goal, now=now)
        assert res["action"]["push_level"] == "direct"

    def test_rejected_today_blocks_injection(self):
        now = time.time()
        cfg = _cfg_obj()
        store = get_configured_store(cfg.config, None)
        goal = store.create_goal(
            conversation_id="telegram:a1:604", platform="telegram",
            account_id="a1", chat_key="604", template="conversion_unlock",
            deadline_days=14)
        kw = dict(platform="telegram", chat_key="604", account_id="a1",
                  conversation_id="telegram:a1:604", now=now)
        assert build_block_for_chat(cfg, **kw) is not None
        act = store.get_action(goal["goal_id"], day_key(now))
        store.mark_action(act["action_id"], "skipped", detail="rejected:agent")
        assert build_block_for_chat(cfg, **kw) is None    # 驳回日彻底停注入


def test_goal_view_today_includes_detail(mem_store):
    now = time.time()
    goal = _new_goal(mem_store, chat_key="605")
    act = mem_store.upsert_action(goal["goal_id"], day_key(now), intent="x")
    mem_store.mark_action(act["action_id"], "planned", detail="adopted")
    act = mem_store.get_action(goal["goal_id"], day_key(now))
    view = goal_view(goal, act, now=now)
    assert view["today"]["detail"] == "adopted"           # 卡片 ✓ 已采纳标记


def test_goal_view_shape_and_title_fallback(mem_store):
    now = time.time()
    goal = _new_goal(mem_store, chat_key="503",
                     params={"item_label": "八字详批"}, now=now)
    view = goal_view(goal, None, None, now=now)
    assert view["goal_id"] == goal["goal_id"]
    assert view["template"] == "conversion_unlock"
    assert view["title"] == "付费解锁转化：八字详批"   # 无标题 → 模板名+说法
    assert view["milestone_label"] == "破冰回暖"
    assert view["total_days"] == 14 and view["day_index"] >= 1
    assert view["today"] is None
    act = mem_store.upsert_action(goal["goal_id"], day_key(now), intent="意图x",
                                  push_level="soft")
    view2 = goal_view(goal, act, now=now)
    assert view2["today"]["intent"] == "意图x"
    assert view2["today"]["status"] == "planned"


# ── build_block_for_chat 注入口 ─────────────────────────────────────────────

def _cfg_obj(enabled=True):
    return SimpleNamespace(
        config={"companion": {"goals": {"enabled": enabled,
                                        "db_path": ":memory:"}}},
        config_path=None)


class TestBuildBlockForChat:
    def test_disabled_returns_none(self):
        assert build_block_for_chat(_cfg_obj(enabled=False),
                                    platform="telegram", chat_key="1") is None

    def test_enabled_without_goal_returns_none(self):
        assert build_block_for_chat(_cfg_obj(),
                                    platform="telegram", chat_key="1") is None

    def test_active_goal_yields_block_and_consumes_beat(self):
        now = time.time()
        cfg = _cfg_obj()
        # 单例已由 autouse fixture 复位 → service 自建，再拿同一实例种目标
        store = get_configured_store(cfg.config, None)
        goal = store.create_goal(
            conversation_id="telegram:a1:555", platform="telegram",
            account_id="a1", chat_key="555", template="conversion_unlock",
            title="冲一单详批", deadline_days=14)
        stats = get_goal_stats()
        r0 = stats.dump()["injected"]["reply"]

        block = build_block_for_chat(
            cfg, platform="telegram", chat_key="555", account_id="a1",
            conversation_id="telegram:a1:555", now=now)
        assert block is not None
        assert block.startswith("【工作目标】") and "冲一单详批" in block
        assert "【今日意图】" in block and "【推进纪律】" in block
        act = store.get_action(goal["goal_id"], day_key(now))
        assert act["status"] == "consumed"          # 拍已标记进入生成
        assert stats.dump()["injected"]["reply"] == r0 + 1

    def test_observe_autonomy_returns_none_without_beat(self):
        now = time.time()
        cfg = _cfg_obj()
        store = get_configured_store(cfg.config, None)
        goal = store.create_goal(
            conversation_id="telegram:a1:556", platform="telegram",
            account_id="a1", chat_key="556", template="custom",
            autonomy="observe", deadline_days=14)
        assert build_block_for_chat(
            cfg, platform="telegram", chat_key="556", account_id="a1",
            conversation_id="telegram:a1:556", now=now) is None
        # observe 档在 refresh 之前就返回 → 连当日拍都不建
        assert store.get_action(goal["goal_id"], day_key(now)) is None

    def test_negative_emotion_hint_holds_block(self):
        now = time.time()
        cfg = _cfg_obj()
        store = get_configured_store(cfg.config, None)
        goal = store.create_goal(
            conversation_id="telegram:a1:557", platform="telegram",
            account_id="a1", chat_key="557", template="conversion_unlock",
            deadline_days=14)
        block = build_block_for_chat(
            cfg, platform="telegram", chat_key="557", account_id="a1",
            conversation_id="telegram:a1:557",
            user_context={"user_emotion_hint": "sad"}, now=now)
        assert block is None                        # 情绪 hold → 不注入
        assert store.get_action(goal["goal_id"], day_key(now)) is None


class _InboxTs:
    def __init__(self, ts):
        self._ts = ts

    def list_recent_messages(self, conversation_id, limit=30):
        return [{"direction": "in", "ts": self._ts}]


class TestSprintPaceRefresh:
    def test_session_two_inbounds_two_slots(self):
        now = time.time()
        cfg = _enabled_cfg()
        cfg["companion"]["goals"]["db_path"] = ":memory:"
        store = get_configured_store(cfg, None)
        goal = store.create_goal(
            conversation_id="telegram:a1:801", platform="telegram",
            account_id="a1", chat_key="801", template="custom",
            params={"pace": "session"}, deadline_days=60 / 1440.0, now=now)
        t1, t2 = now - 120, now - 30
        r1 = refresh_goal(store, cfg, goal, inbox_store=_InboxTs(t1), now=now)
        r2 = refresh_goal(store, cfg, goal, inbox_store=_InboxTs(t2), now=now)
        assert r1["action"]["day"] == f"s:{int(t1)}"
        assert r2["action"]["day"] == f"s:{int(t2)}"
        assert r1["action"]["action_id"] != r2["action"]["action_id"]

    def test_session_cap_holds_pace_cap(self):
        now = time.time()
        cfg = _enabled_cfg()
        cfg["companion"]["goals"]["db_path"] = ":memory:"
        store = get_configured_store(cfg, None)
        goal = store.create_goal(
            conversation_id="telegram:a1:802", platform="telegram",
            account_id="a1", chat_key="802", template="custom",
            params={"pace": "session"}, deadline_days=90 / 1440.0, now=now)
        for i in range(3):
            ts = now - 300 + i * 10
            res = refresh_goal(store, cfg, goal, inbox_store=_InboxTs(ts), now=now)
            assert res.get("hold") is None and res.get("action")
        res4 = refresh_goal(
            store, cfg, goal, inbox_store=_InboxTs(now - 5), now=now)
        assert res4.get("hold") == "pace_cap"
        assert res4.get("action") is None

    def test_sprint_beat_uses_sprint_intent_pool(self):
        """限时档首拍意图来自 intents_sprint（与 pick_sprint_intent 同参
        逐字一致）——日历池的「隔天补一句」不得进 60 分钟目标。"""
        from src.companion.goals.templates import (
            TEMPLATES,
            pick_sprint_intent,
        )
        now = time.time()
        cfg = _enabled_cfg()
        cfg["companion"]["goals"]["db_path"] = ":memory:"
        store = get_configured_store(cfg, None)
        goal = store.create_goal(
            conversation_id="telegram:a1:803", platform="telegram",
            account_id="a1", chat_key="803", template="custom",
            params={"pace": "session", "note": "要到TA的微信"},
            deadline_days=60 / 1440.0, now=now)
        t1 = now - 60
        res = refresh_goal(store, cfg, goal, inbox_store=_InboxTs(t1), now=now)
        act = res["action"]
        assert act is not None
        expect = pick_sprint_intent(
            TEMPLATES["custom"], 0, goal["goal_id"], f"s:{int(t1)}",
            params=goal["params"])
        assert act["intent"] == expect
        assert "「要到TA的微信」" in act["intent"]

    def test_sprint_care_day_keeps_care_intent(self):
        """连续驳回 ≥2 → 退避陪伴日：限时档不得把陪伴意图/none 力度覆盖成
        sprint 收口话术（覆盖了＝拆掉让路）。"""
        from src.companion.goals.templates import CARE_INTENTS
        now = time.time()
        cfg = _enabled_cfg()
        cfg["companion"]["goals"]["db_path"] = ":memory:"
        store = get_configured_store(cfg, None)
        goal = store.create_goal(
            conversation_id="telegram:a1:804", platform="telegram",
            account_id="a1", chat_key="804", template="custom",
            params={"pace": "session", "note": "要到TA的微信"},
            deadline_days=60 / 1440.0, now=now)
        store.add_event(goal["goal_id"], "beat_rejected", "t1")
        store.add_event(goal["goal_id"], "beat_rejected", "t2")
        res = refresh_goal(
            store, cfg, goal, inbox_store=_InboxTs(now - 60), now=now)
        act = res["action"]
        assert act is not None
        assert act["push_level"] == "none"
        assert act["intent"] in CARE_INTENTS


def test_outcome_report_by_pace(mem_store):
    """报表按节奏拆终态：params.pace/期限跨度 → natural/session 分桶，
    done_rate/won/avg_hours_to_done 各算各的。"""
    now = time.time()
    g1 = _new_goal(mem_store, chat_key="901", template="custom",
                   now=now - 7200)
    assert mem_store.update_goal_fields(
        g1["goal_id"], status="done", done_at=now - 60,
        result="manual:won", progress=1.0)
    g2 = mem_store.create_goal(
        conversation_id="telegram:a1:902", platform="telegram",
        account_id="a1", chat_key="902", template="custom",
        params={"pace": "session"}, deadline_days=60 / 1440.0,
        now=now - 3600)
    assert mem_store.update_goal_fields(
        g2["goal_id"], status="expired", done_at=now - 30)
    rep = mem_store.outcome_report(now - 86400, now=now)
    bp = rep["by_pace"]
    assert bp["natural"]["done"] == 1 and bp["natural"]["won"] == 1
    assert bp["natural"]["done_rate"] == 1.0
    assert bp["natural"]["avg_hours_to_done"] and \
        bp["natural"]["avg_hours_to_done"] > 0
    assert "_done_hours" not in bp["natural"]
    assert bp["session"]["expired"] == 1
    assert bp["session"]["done_rate"] == 0.0
    assert "_done_hours" not in bp["session"]


def test_outcome_report_outcome_signals(mem_store):
    """达成信号读数：正式轨/影子轨 × 目标现状分桶——影子轨 done 占比是
    「检测器要不要升级为正式提示」的判据。"""
    now = time.time()
    g1 = _new_goal(mem_store, chat_key="911", template="custom")
    mem_store.add_event(g1["goal_id"], "outcome_signal", "contact:abc")
    assert mem_store.update_goal_fields(
        g1["goal_id"], status="done", done_at=now, result="manual:won")
    g2 = _new_goal(mem_store, chat_key="912", template="custom")
    mem_store.add_event(g2["goal_id"], "outcome_shadow_appointment", "明天3点")
    rep = mem_store.outcome_report(now - 3600, now=now)
    sig = rep["outcome_signals"]
    assert sig["outcome_signal"] == {"n": 1, "done": 1}
    assert sig["outcome_shadow_appointment"] == {"n": 1, "active": 1}
    assert "outcome_shadow_payment" not in sig
