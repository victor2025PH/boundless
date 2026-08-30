"""实施84：care × 工作目标联通层门禁。

覆盖：P0-2 目标背景块（auto 档/驳回让路/observe·suggest 不注入/双闸）、
P1-1 捕获事件回流目标时间线（默认关/开后写 goal_events）、
P1-2 到期排期扫描（提前窗/幂等/autonomy 闸/过期不排/goal 事件回执）、
以及派发器对 goal 排期行的模板切换与 no_context 豁免。
"""
from datetime import datetime
from types import SimpleNamespace

from src.companion.goals.store import GoalStore
from src.contacts.care_dispatcher import CareDispatcher
from src.contacts.care_goal_link import (
    care_goal_hint,
    goal_care_topic_norm,
    record_capture_event,
    scan_goal_deadlines,
)
from src.contacts.care_schedule import GOAL_CARE_NORM_PREFIX, CareScheduleStore

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()
_DAY = 86400.0

CID = "telegram:default:u1"


def _cfg(goals_enabled=True, goal_hint=True, link_enabled=True,
         capture_events=True, days_before=3):
    return SimpleNamespace(config={
        "companion": {
            "goals": {"enabled": goals_enabled},
            "proactive_care": {
                "enabled": True,
                "goal_hint": goal_hint,
                "goal_link": {
                    "enabled": link_enabled,
                    "capture_events": capture_events,
                    "days_before": days_before,
                },
            },
        },
    }, config_path=None)


def _goal(gs: GoalStore, *, autonomy="auto", deadline_days=0.0,
          title="推进VIP解锁", now=NOW):
    return gs.create_goal(
        conversation_id=CID, platform="telegram", account_id="default",
        chat_key="u1", template="custom", title=title,
        autonomy=autonomy, deadline_days=deadline_days, now=now)


# ── P0-2：目标背景块 ─────────────────────────────────────────────────────────
def test_hint_with_auto_goal_contains_title():
    gs = GoalStore(":memory:")
    _goal(gs)
    out = care_goal_hint(_cfg(), conversation_id=CID, platform="telegram",
                         account_id="default", chat_key="u1", goals_store=gs)
    assert "推进VIP解锁" in out and out.startswith("【工作目标背景】")
    assert "不硬销" in out or "绝不硬销" in out


def test_hint_includes_today_intent():
    gs = GoalStore(":memory:")
    g = _goal(gs)
    from src.companion.goals.planner import day_key
    gs.upsert_action(g["goal_id"], day_key(NOW), intent="顺势聊到试用感受",
                     push_level="soft", now=NOW)
    out = care_goal_hint(_cfg(), conversation_id=CID, platform="telegram",
                         account_id="default", chat_key="u1", now=NOW,
                         goals_store=gs)
    assert "顺势聊到试用感受" in out


def test_hint_calm_only_when_beat_rejected_or_none_push():
    from src.companion.goals.planner import day_key
    for status, push in (("skipped", "soft"), ("planned", "none")):
        gs = GoalStore(":memory:")
        g = _goal(gs)
        gs.upsert_action(g["goal_id"], day_key(NOW), intent="x",
                         push_level=push, status=status, now=NOW)
        out = care_goal_hint(_cfg(), conversation_id=CID, platform="telegram",
                             account_id="default", chat_key="u1", now=NOW,
                             goals_store=gs)
        assert "只陪伴" in out
        assert "推进VIP解锁" not in out  # 驳回/none 日不带方向


def test_hint_empty_for_non_auto_or_disabled():
    gs = GoalStore(":memory:")
    _goal(gs, autonomy="suggest")
    # suggest 档：机发触达不带营销方向（与 bridge 同准入）
    assert care_goal_hint(_cfg(), conversation_id=CID, platform="telegram",
                          account_id="default", chat_key="u1",
                          goals_store=gs) == ""
    gs2 = GoalStore(":memory:")
    _goal(gs2)
    # goals 总闸关
    assert care_goal_hint(_cfg(goals_enabled=False), conversation_id=CID,
                          platform="telegram", account_id="default",
                          chat_key="u1", goals_store=gs2) == ""
    # care 侧 goal_hint 关
    assert care_goal_hint(_cfg(goal_hint=False), conversation_id=CID,
                          platform="telegram", account_id="default",
                          chat_key="u1", goals_store=gs2) == ""
    # 无目标
    assert care_goal_hint(_cfg(), conversation_id="telegram:default:nobody",
                          platform="telegram", account_id="default",
                          chat_key="nobody", goals_store=gs2) == ""


# ── P1-1：捕获事件回流 ───────────────────────────────────────────────────────
def test_capture_event_written_when_enabled():
    gs = GoalStore(":memory:")
    g = _goal(gs)
    ok = record_capture_event(_cfg(), conversation_id=CID, platform="telegram",
                              account_id="default", chat_key="u1",
                              text="我下周五面试，好紧张", count=1,
                              goals_store=gs)
    assert ok is True
    events = gs.list_events(g["goal_id"])
    assert any(e["kind"] == "care_capture" and "面试" in e["detail"]
               for e in events)


def test_capture_event_gated_off_by_default():
    gs = GoalStore(":memory:")
    g = _goal(gs)
    ok = record_capture_event(_cfg(link_enabled=False), conversation_id=CID,
                              platform="telegram", account_id="default",
                              chat_key="u1", text="x", goals_store=gs)
    assert ok is False
    assert not any(e["kind"] == "care_capture"
                   for e in gs.list_events(g["goal_id"]))


# ── P1-2：到期排期扫描 ───────────────────────────────────────────────────────
def test_scan_schedules_in_window_and_is_idempotent():
    gs = GoalStore(":memory:")
    g = _goal(gs, deadline_days=2.0)  # 2 天后到期，days_before=3 → 已进窗
    cs = CareScheduleStore(":memory:")
    n = scan_goal_deadlines(cs, _cfg(), now=NOW, goals_store=gs)
    assert n == 1
    row = cs.list_pending()[0]
    assert row["topic_norm"] == goal_care_topic_norm(g["goal_id"])
    assert row["topic_norm"].startswith(GOAL_CARE_NORM_PREFIX)
    assert row["contact_key"] == CID
    assert float(row["due_at"]) > NOW
    assert "到期" in row["source_text"]
    # goal 事件回执
    assert any(e["kind"] == "care_scheduled"
               for e in gs.list_events(g["goal_id"]))
    # 幂等：第二轮不重排
    assert scan_goal_deadlines(cs, _cfg(), now=NOW + 3600,
                               goals_store=gs) == 0
    # 发出后（任何终态）30 天内也不重排
    cs.mark_sent(int(row["id"]), note="deferred:1", sent_text="ok")
    assert scan_goal_deadlines(cs, _cfg(), now=NOW + 7200,
                               goals_store=gs) == 0


def test_scan_skips_out_of_window_non_auto_expired_and_disabled():
    cs = CareScheduleStore(":memory:")
    # 还没进窗（10 天后到期）
    gs1 = GoalStore(":memory:")
    _goal(gs1, deadline_days=10.0)
    assert scan_goal_deadlines(cs, _cfg(), now=NOW, goals_store=gs1) == 0
    # suggest 档不排
    gs2 = GoalStore(":memory:")
    _goal(gs2, autonomy="suggest", deadline_days=2.0)
    assert scan_goal_deadlines(cs, _cfg(), now=NOW, goals_store=gs2) == 0
    # 已过期不排
    gs3 = GoalStore(":memory:")
    _goal(gs3, deadline_days=1.0, now=NOW - 3 * _DAY)
    assert scan_goal_deadlines(cs, _cfg(), now=NOW, goals_store=gs3) == 0
    # 无 deadline 不排
    gs4 = GoalStore(":memory:")
    _goal(gs4, deadline_days=0.0)
    assert scan_goal_deadlines(cs, _cfg(), now=NOW, goals_store=gs4) == 0
    # 总闸关不排
    gs5 = GoalStore(":memory:")
    _goal(gs5, deadline_days=2.0)
    assert scan_goal_deadlines(cs, _cfg(link_enabled=False), now=NOW,
                               goals_store=gs5) == 0


# ── 派发器：goal 排期行的模板与 no_context 豁免 ─────────────────────────────
class _AI:
    def __init__(self, reply="最近怎么样呀？那件事咱们下一步弄起来？"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


async def test_goal_care_dispatch_uses_goal_prompt_and_skips_no_context():
    gs = GoalStore(":memory:")
    g = _goal(gs, deadline_days=2.0)
    cs = CareScheduleStore(":memory:")
    assert scan_goal_deadlines(cs, _cfg(), now=NOW, goals_store=gs) == 1
    # 把 due 提前到现在，让派发 tick 立即到期
    cs.bring_forward(int(cs.list_pending()[0]["id"]), now=NOW - 60)
    rec = []

    async def _send(channel, account_id, chat_name, reply, defer_until,
                    reason, staleness, extra):
        rec.append({"reason": reason, "reply": reply})
        return 7

    ai = _AI()
    d = CareDispatcher(store=cs, ai_client=ai, send_callback=_send,
                       context_provider=lambda ck: "",  # 无上下文
                       skip_if_no_context=True)
    n = await d.run_once(now=NOW)
    assert n == 1 and len(rec) == 1  # goal 行豁免 no_context skip
    p = ai.prompts[0]
    assert "正在推进的事" in p          # 用的是目标推进模板
    assert "推进VIP解锁" in p           # topic=目标标题
    assert "对方之前提到过" not in p     # 不是约定回访叙事
    assert cs.count(status="sent") == 1
    assert g["goal_id"]  # goal 仍在（派发不动 goals 库）
