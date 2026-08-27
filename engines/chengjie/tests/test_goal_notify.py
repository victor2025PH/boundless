# -*- coding: utf-8 -*-
"""目标「发现→提醒」闭环门禁（P0 2026-08-09）。

覆盖：
- 配置解析（默认关 / 数值夹紧）；
- 定时结算扫描 settle_sweep（开关闸门 / 只扫陈旧 active / 预算 / 真把过期目标
  结算成 expired——settle-on-read 补丁的核心承诺）；
- 完成通知扫描 scan_and_notify（开关闸门 / **幂等只发一次** / lookback 窗 /
  每轮上限 / payload 契约：客户名富化 + won_meta 金额 + result_kind）；
- 接线完整性：webhook 别名 goal_complete（business 受众）/ formatter 有专属
  文案（不落通用兜底）/ SSE + 坐席铃铛事件集包含 goal_completed_alert /
  ScheduledReporter 的 tick 节奏（notify 每 tick、sweep 按 interval_ticks）。

store 用独立 ``GoalStore(":memory:")`` 实例（不碰进程单例）；reporter 接线
用例走单例故 autouse 复位。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from src.companion.goals import notify as goal_notify
from src.companion.goals.store import GoalStore, reset_goal_store

NOW = time.time()


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_goal_store()
    yield
    reset_goal_store()


def _cfg(goals_extra=None, sweep=None, notify=None):
    goals = {"enabled": True, "db_path": ":memory:"}
    goals.update(goals_extra or {})
    if sweep is not None:
        goals["sweep"] = sweep
    if notify is not None:
        goals["notify"] = notify
    return {"companion": {"goals": goals}}


def _mk_store() -> GoalStore:
    return GoalStore(":memory:")


def _mk_goal(store, *, template="conversion_unlock", account_id="a1",
             chat_key="100", deadline_days=7.0, now=None):
    g = store.create_goal(
        conversation_id=f"telegram:{account_id}:{chat_key}",
        platform="telegram", account_id=account_id, chat_key=chat_key,
        template=template, deadline_days=deadline_days, now=now)
    assert g is not None
    return g


class _FakeInbox:
    """报表/通知富化用的最小 inbox store 假体。"""

    def __init__(self, names=None, out_ts=None):
        self._names = names or {}
        self._out = out_ts or {}

    def get_conversations_for_ids(self, ids):
        return {cid: {"display_name": self._names[cid]}
                for cid in ids if cid in self._names}

    def last_outbound_ts_map(self, ids):
        return {cid: self._out[cid] for cid in ids if cid in self._out}


# ── 配置解析 ────────────────────────────────────────────────────────────────

def test_cfg_defaults_off():
    assert goal_notify.resolve_sweep_cfg({})["enabled"] is False
    assert goal_notify.resolve_notify_cfg({})["enabled"] is False
    # goals 开但没配 sweep/notify 段 → 仍是关（新子系统默认关约定）
    assert goal_notify.resolve_sweep_cfg(_cfg())["enabled"] is False
    assert goal_notify.resolve_notify_cfg(_cfg())["enabled"] is False


def test_cfg_clamps():
    c = goal_notify.resolve_sweep_cfg(_cfg(sweep={
        "enabled": True, "budget": 99999, "interval_ticks": 0}))
    assert c["budget"] == 200
    assert c["interval_ticks"] == 5      # 0=无意义 → 回落默认（or 5 语义）
    n = goal_notify.resolve_notify_cfg(_cfg(notify={
        "enabled": True, "lookback_hours": 0, "max_per_scan": 9999}))
    assert n["lookback_hours"] >= 1 and n["max_per_scan"] == 50


# ── store 查询 ──────────────────────────────────────────────────────────────

def test_list_active_page_stable_order_and_paging():
    store = _mk_store()
    gids = sorted(_mk_goal(store, chat_key=str(i))["goal_id"]
                  for i in range(5))
    p1 = [r["goal_id"] for r in store.list_active_page(offset=0, limit=2)]
    p2 = [r["goal_id"] for r in store.list_active_page(offset=2, limit=2)]
    p3 = [r["goal_id"] for r in store.list_active_page(offset=4, limit=2)]
    assert p1 + p2 + p3 == gids          # 稳定序全覆盖、无重叠
    assert store.list_active_page(offset=10, limit=2) == []


def test_list_done_unnotified_anti_join():
    store = _mk_store()
    g = _mk_goal(store)
    store.update_goal_fields(
        g["goal_id"], status="done", done_at=time.time(), result="manual:agent")
    rows = store.list_done_unnotified(since_ts=time.time() - 60, limit=10)
    assert [r["goal_id"] for r in rows] == [g["goal_id"]]
    store.add_event(g["goal_id"], goal_notify.NOTIFIED_EVENT_KIND, "")
    assert store.list_done_unnotified(since_ts=time.time() - 60, limit=10) == []
    assert store.event_exists(g["goal_id"], goal_notify.NOTIFIED_EVENT_KIND)


# ── settle_sweep ────────────────────────────────────────────────────────────

def test_sweep_disabled_returns_zero():
    store = _mk_store()
    _mk_goal(store)
    out = goal_notify.settle_sweep(store, _cfg())               # 段缺省=关
    assert out == {"settled": 0, "next_cursor": 0}
    out = goal_notify.settle_sweep(
        store, {"companion": {"goals": {"enabled": False,
                                        "sweep": {"enabled": True}}}})
    assert out["settled"] == 0


def test_sweep_settles_expired_goal_without_anyone_reading():
    """核心承诺：没人打开会话，过期/完成也会被扫描发现。"""
    store = _mk_store()
    cfg = _cfg(sweep={"enabled": True})
    g = _mk_goal(store, deadline_days=1.0)
    store.update_goal_fields(g["goal_id"], deadline_ts=time.time() - 3600)
    out = goal_notify.settle_sweep(store, cfg)
    assert out["settled"] == 1
    assert store.get_goal(g["goal_id"])["status"] == "expired"


def test_sweep_cursor_rotates_without_starvation():
    """饿死回归钉（2026-08-09 首版实锤）：refresh 无变化不写库 → updated_at
    排序会永远重扫同一批；稳定序游标必须让 5 条目标在预算 2 下三轮全覆盖。"""
    store = _mk_store()
    cfg = _cfg(sweep={"enabled": True})
    for i in range(5):
        _mk_goal(store, chat_key=str(i))
    seen: list = []
    real_refresh = None
    from src.companion.goals import service as goal_service
    real_refresh = goal_service.refresh_goal

    def _spy(store_, cfg_, goal, **kw):
        seen.append(goal["goal_id"])
        return real_refresh(store_, cfg_, goal, **kw)

    goal_service.refresh_goal, _bak = _spy, real_refresh
    try:
        cur = 0
        for _ in range(3):
            out = goal_notify.settle_sweep(store, cfg, cursor=cur, budget=2)
            cur = out["next_cursor"]
    finally:
        goal_service.refresh_goal = _bak
    assert len(set(seen)) == 5           # 三轮（2+2+1）覆盖全部，无饿死
    assert cur == 0                      # 扫完队尾回卷


def test_sweep_wraps_past_end():
    store = _mk_store()
    cfg = _cfg(sweep={"enabled": True})
    g = _mk_goal(store)
    out = goal_notify.settle_sweep(store, cfg, cursor=99, budget=2)
    assert out["settled"] == 1 and out["next_cursor"] == 0
    assert g["goal_id"]


# ── scan_and_notify ─────────────────────────────────────────────────────────

def _done_goal(store, *, account_id="a1", chat_key="100", result="manual:agent",
               won_amount=None):
    g = _mk_goal(store, account_id=account_id, chat_key=chat_key)
    store.update_goal_fields(
        g["goal_id"], status="done", done_at=time.time(), result=result,
        progress=1.0)
    if won_amount is not None:
        import json
        store.add_event(g["goal_id"], "won_meta",
                        json.dumps({"amount": won_amount, "product": "pro"}))
    return store.get_goal(g["goal_id"])


def test_notify_disabled_publishes_nothing():
    store = _mk_store()
    _done_goal(store)
    sent = []
    out = goal_notify.scan_and_notify(
        store, _cfg(), publish=lambda t, p: sent.append((t, p)))
    assert out == {"scanned": 0, "notified": 0} and sent == []


def test_notify_publishes_once_and_is_idempotent():
    store = _mk_store()
    cfg = _cfg(notify={"enabled": True})
    g = _done_goal(store, result="order:pro:ORD-9", won_amount=199)
    inbox = _FakeInbox(names={g["conversation_id"]: "小美"})
    sent = []
    out = goal_notify.scan_and_notify(
        store, cfg, inbox_store=inbox,
        publish=lambda t, p: sent.append((t, p)))
    assert out["notified"] == 1 and len(sent) == 1
    etype, payload = sent[0]
    assert etype == "goal_completed_alert"
    assert payload["goal_id"] == g["goal_id"]
    assert payload["contact_name"] == "小美"
    assert payload["result_kind"] == "order" and payload["won"] is True
    assert payload["amount"] == 199 and payload["product"] == "pro"
    # rate_key 按「账号+目标」粒度（2026-08-18）：同账号第二个客户的成交不再被
    # webhook 1h 限流窗吞掉；同目标重发（标记写失败）仍共享同 key 被正确压住。
    assert payload["rate_key"] == f"goal_done:a1:{g['goal_id']}"
    assert payload["template_name"]          # 模板中文名非空
    # 幂等：第二轮零发布（goal_events 标记挡住）
    out2 = goal_notify.scan_and_notify(
        store, cfg, inbox_store=inbox,
        publish=lambda t, p: sent.append((t, p)))
    assert out2["notified"] == 0 and len(sent) == 1


def test_notify_lookback_excludes_old_done():
    store = _mk_store()
    cfg = _cfg(notify={"enabled": True, "lookback_hours": 1})
    g = _done_goal(store)
    store.update_goal_fields(g["goal_id"], done_at=time.time() - 7200)
    sent = []
    out = goal_notify.scan_and_notify(
        store, cfg, publish=lambda t, p: sent.append((t, p)))
    assert out["notified"] == 0 and sent == []


def test_notify_max_per_scan_caps():
    store = _mk_store()
    cfg = _cfg(notify={"enabled": True, "max_per_scan": 2})
    for i in range(4):
        _done_goal(store, chat_key=str(i))
    sent = []
    out = goal_notify.scan_and_notify(
        store, cfg, publish=lambda t, p: sent.append((t, p)))
    assert out["notified"] == 2 and len(sent) == 2
    # 下一轮把剩下的补完（drain 语义，不丢）
    goal_notify.scan_and_notify(
        store, cfg, publish=lambda t, p: sent.append((t, p)))
    assert len(sent) == 4


def test_result_kind_mapping():
    assert goal_notify.result_kind("order:pro:1") == "order"
    assert goal_notify.result_kind("manual:agent") == "manual"
    assert goal_notify.result_kind("unlocked:item") == "auto"
    assert goal_notify.result_kind("") == ""


# ── 失守聚合日报（P1）───────────────────────────────────────────────────────

def _missed_goal(store, *, status="expired", chat_key="100", template=None,
                 account_id="a1", done_ago_h=1.0):
    g = _mk_goal(store, account_id=account_id, chat_key=chat_key,
                 template=template or "conversion_unlock")
    store.update_goal_fields(
        g["goal_id"], status=status, done_at=time.time() - done_ago_h * 3600)
    return store.get_goal(g["goal_id"])


def test_miss_digest_disabled_paths():
    store = _mk_store()
    _missed_goal(store)
    sent = []
    # 父开关关
    out = goal_notify.scan_miss_digest(
        store, _cfg(), publish=lambda t, p: sent.append((t, p)))
    assert out == {"pending": 0, "digested": 0} and sent == []
    # 父开 miss_digest 显式关
    out = goal_notify.scan_miss_digest(
        store, _cfg(notify={"enabled": True, "miss_digest": False}),
        publish=lambda t, p: sent.append((t, p)))
    assert out["digested"] == 0 and sent == []


def test_miss_digest_below_threshold_waits():
    store = _mk_store()
    _missed_goal(store, done_ago_h=1.0)     # 1 条且还年轻
    sent = []
    out = goal_notify.scan_miss_digest(
        store, _cfg(notify={"enabled": True, "miss_min_count": 3,
                            "miss_max_age_hours": 24}),
        publish=lambda t, p: sent.append((t, p)))
    assert out["pending"] == 1 and out["digested"] == 0 and sent == []


def test_miss_digest_aggregates_once_and_is_idempotent():
    store = _mk_store()
    cfg = _cfg(notify={"enabled": True, "miss_min_count": 3})
    _missed_goal(store, status="failed", chat_key="1")
    _missed_goal(store, status="expired", chat_key="2")
    _missed_goal(store, status="expired", chat_key="3",
                 template="conversion_subscribe", account_id="b2")
    sent = []
    out = goal_notify.scan_miss_digest(
        store, cfg, publish=lambda t, p: sent.append((t, p)))
    assert out["digested"] == 3 and len(sent) == 1        # 聚合成一条
    etype, payload = sent[0]
    assert etype == "goal_miss_alert"
    assert payload["count"] == 3
    assert payload["failed"] == 1 and payload["expired"] == 2
    assert sum(payload["by_template"].values()) == 3
    assert sum(payload["by_account"].values()) == 3
    assert payload["rate_key"] == "goal_miss:digest"
    # 幂等：第二轮零动作
    out2 = goal_notify.scan_miss_digest(
        store, cfg, publish=lambda t, p: sent.append((t, p)))
    assert out2["digested"] == 0 and len(sent) == 1


def test_miss_digest_age_override_fires_small_batch():
    """小流量也按天出账：1 条但压龄超 max_age → 照发。"""
    store = _mk_store()
    _missed_goal(store, done_ago_h=30.0)
    sent = []
    out = goal_notify.scan_miss_digest(
        store, _cfg(notify={"enabled": True, "miss_min_count": 3,
                            "miss_max_age_hours": 24}),
        publish=lambda t, p: sent.append((t, p)))
    assert out["digested"] == 1 and len(sent) == 1


def test_triage_missed_three_way():
    """P5 三分法：offered（direct 拍真发过，硬证据）> engaged（窗口内入站≥2）
    > silent。里程碑 idx 刻意不参与——时间兑底让它judge不了真实进展。"""
    store = _mk_store()
    g_off = _missed_goal(store, chat_key="1")
    g_eng = _missed_goal(store, chat_key="2")
    g_sil = _missed_goal(store, chat_key="3")
    # offered：开价拍已耗（哪怕客户也回过话，offered 优先——最该复核）
    store.upsert_action(g_off["goal_id"], "2026-08-08",
                        push_level="direct", status="consumed")

    class _Inbox:
        def count_inbound_between(self, cid, since, until):
            return {"telegram:a1:1": 5, "telegram:a1:2": 3,
                    "telegram:a1:3": 1}.get(cid, 0)

    tri = goal_notify.triage_missed(store, _Inbox(), [g_off, g_eng, g_sil])
    assert tri[g_off["goal_id"]] == "offered"
    assert tri[g_eng["goal_id"]] == "engaged"
    assert tri[g_sil["goal_id"]] == "silent"     # 仅 1 条入站=触发消息本身
    # inbox 不可用 → offered 仍判得出，其余保守 silent
    tri2 = goal_notify.triage_missed(store, None, [g_off, g_eng])
    assert tri2[g_off["goal_id"]] == "offered"
    assert tri2[g_eng["goal_id"]] == "silent"


def test_miss_digest_payload_carries_triage():
    store = _mk_store()
    cfg = _cfg(notify={"enabled": True, "miss_min_count": 1})
    g = _missed_goal(store, chat_key="9")
    store.upsert_action(g["goal_id"], "2026-08-08",
                        push_level="direct", status="sent")
    sent = []
    goal_notify.scan_miss_digest(
        store, cfg, publish=lambda t, p: sent.append((t, p)))
    assert sent and sent[0][1]["triage"] == {
        "offered": 1, "engaged": 0, "silent": 0}


def test_miss_digest_done_goals_not_included():
    store = _mk_store()
    _done_goal(store)                       # done 不属失守
    sent = []
    out = goal_notify.scan_miss_digest(
        store, _cfg(notify={"enabled": True, "miss_min_count": 1}),
        publish=lambda t, p: sent.append((t, p)))
    assert out["pending"] == 0 and sent == []


# ── 接线完整性 ──────────────────────────────────────────────────────────────

def test_webhook_alias_and_audience_registered():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, alert_audience
    rule = _EVENT_ALIASES.get("goal_complete")
    assert rule and rule["types"] == {"goal_completed_alert"}
    assert alert_audience("goal_complete") == "business"
    miss = _EVENT_ALIASES.get("goal_miss")
    assert miss and miss["types"] == {"goal_miss_alert"}
    assert alert_audience("goal_miss") == "business"


def test_webhook_formatter_miss_digest_branch():
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("goal_miss_alert", {
        "count": 4, "failed": 1, "expired": 3,
        "by_template": {"付费解锁": 2, "会员订阅": 2},
        "by_account": {"telegram:a1": 3, "telegram:b2": 1},
        "oldest_hours": 26.5, "window_hours": 72,
    })
    assert "失守" in title and "4" in title
    assert "付费解锁" in text and "/workspace/goal-report" in text


def test_webhook_formatter_has_dedicated_branch():
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("goal_completed_alert", {
        "contact_name": "小美", "template_name": "会员订阅",
        "title": "会员订阅", "platform": "telegram", "account_id": "a1",
        "result_kind": "order", "amount": 199, "days_to_done": 3.5,
    })
    assert "目标达成" in title and "小美" in title
    assert "/workspace/goal-report" in text and "$" not in title


def test_sse_and_bell_event_types_include_goal_completed():
    from src.web.routes.unified_inbox_realtime_routes import (
        _NOTIF_EVENT_TYPES,
        _SSE_EVENT_TYPES,
    )
    assert "goal_completed_alert" in _SSE_EVENT_TYPES
    assert "goal_completed_alert" in _NOTIF_EVENT_TYPES


# ── 常备扫描循环节奏（P2 重构：不再挂 ScheduledReporter——那个调度器受
#    report.enabled 闸生产常年关，P0 首版挂那里导致扫描从未运行）────────────

def test_scan_tick_runs_notify_every_tick_and_sweep_on_interval(monkeypatch):
    calls = {"sweep": 0, "notify": 0, "miss": 0}
    cursors: list = []
    monkeypatch.setattr(
        goal_notify, "settle_sweep",
        lambda *a, **k: (cursors.append(k.get("cursor")),
                         calls.__setitem__("sweep", calls["sweep"] + 1),
                         {"settled": 1, "next_cursor": 7})[-1])
    monkeypatch.setattr(
        goal_notify, "scan_and_notify",
        lambda *a, **k: (calls.__setitem__("notify", calls["notify"] + 1)
                         or {"scanned": 0, "notified": 0}))
    monkeypatch.setattr(
        goal_notify, "scan_miss_digest",
        lambda *a, **k: (calls.__setitem__("miss", calls["miss"] + 1)
                         or {"pending": 0, "digested": 0}))
    cfg = _cfg(sweep={"enabled": True, "interval_ticks": 2},
               notify={"enabled": True})
    cm = SimpleNamespace(config=cfg, config_path=None)
    state: dict = {}
    for _ in range(4):
        goal_notify.scan_tick(state, cm)
    assert calls["notify"] == 4          # 每 tick
    assert calls["miss"] == 4            # 随 notify 每 tick（内部自带出账门槛）
    assert calls["sweep"] == 2           # 每 2 tick 一次
    assert cursors == [0, 7]             # 游标在 state 里轮转传递
    assert state["ticks"] == 4 and state["gated"] == ""
    assert state["sweep_cursor"] == 7 and state["settled_total"] == 2


def test_scan_tick_gated_states():
    cm = SimpleNamespace(config=_cfg(), config_path=None)
    state: dict = {}
    goal_notify.scan_tick(state, cm)     # goals 开但两段扫描全关
    assert state["gated"] == "scans_disabled"
    cm2 = SimpleNamespace(
        config={"companion": {"goals": {"enabled": False}}}, config_path=None)
    goal_notify.scan_tick(state, cm2)
    assert state["gated"] == "goals_disabled"
    # 全关时不碰 store 单例（零开销承诺）
    from src.companion.goals.store import peek_goal_store
    assert peek_goal_store() is None
    # 心跳字段在（「没跑」和「没货」从外面要分得出来——本次事故的教训）
    assert state["ticks"] == 2 and state["last_tick_ts"] > 0


# ── P2（2026-08-18）：责任坐席定向副本 ──────────────────────────────────────
# 解析链：目标创建人（人建）→ 会话认领坐席 → 无；命中者须启用中且绑定了
# Telegram 通知号。payload 注入受 push_agent 开关闸；webhook 侧每事件至多
# 加发一份、与渠道同号不重发。


class _FakeUsers:
    def __init__(self, users):
        self._u = users

    def get_user(self, name):
        return self._u.get(name)


class _FakeClaimInbox(_FakeInbox):
    def __init__(self, claims=None, **kw):
        super().__init__(**kw)
        self._claims = claims or {}

    def get_conversation_claim(self, cid):
        return self._claims.get(cid)


def test_resolve_agent_target_creator_first():
    users = _FakeUsers({"amy": {"enabled": 1, "notify_tg_chat_id": "42"}})
    out = goal_notify.resolve_agent_push_target(
        {"created_by": "amy", "conversation_id": "c1"}, user_store=users)
    assert out == {"agent_chat_id": "42", "agent_username": "amy"}
    # 批量口后缀剥掉后同样命中
    out2 = goal_notify.resolve_agent_push_target(
        {"created_by": "amy:batch", "conversation_id": "c1"}, user_store=users)
    assert out2["agent_username"] == "amy"


def test_resolve_agent_target_auto_origin_falls_to_claim():
    users = _FakeUsers({"bob": {"enabled": 1, "notify_tg_chat_id": "-77"}})
    inbox = _FakeClaimInbox(claims={"c1": {"agent_id": "bob"}})
    for origin in ("auto_create", "winback_auto", "retention_auto",
                   "reconvert_auto", "batch", ""):
        out = goal_notify.resolve_agent_push_target(
            {"created_by": origin, "conversation_id": "c1"},
            inbox_store=inbox, user_store=users)
        assert out["agent_username"] == "bob", origin


def test_resolve_agent_target_honest_empty():
    users = _FakeUsers({
        "nobind": {"enabled": 1, "notify_tg_chat_id": ""},
        "gone": {"enabled": 0, "notify_tg_chat_id": "99"},
    })
    # 未绑定 / 已禁用 / 查无此人 / 没给 user_store → 一律空 dict（回落管理员渠道）
    for creator in ("nobind", "gone", "ghost"):
        assert goal_notify.resolve_agent_push_target(
            {"created_by": creator, "conversation_id": "c"},
            user_store=users) == {}
    assert goal_notify.resolve_agent_push_target(
        {"created_by": "amy", "conversation_id": "c"}) == {}
    # claim getter 抛异常不拖垮解析（creator 空 → 空 dict）
    class _Boom:
        def get_conversation_claim(self, cid):
            raise RuntimeError("db locked")
    assert goal_notify.resolve_agent_push_target(
        {"created_by": "auto_create", "conversation_id": "c"},
        inbox_store=_Boom(), user_store=users) == {}


def test_scan_and_notify_agent_fields_gated_by_push_agent():
    users = _FakeUsers({"amy": {"enabled": 1, "notify_tg_chat_id": "42"}})

    def _done_by_amy(store):
        g = store.create_goal(
            conversation_id="telegram:a1:900", platform="telegram",
            account_id="a1", chat_key="900", template="conversion_unlock",
            created_by="amy")
        store.update_goal_fields(
            g["goal_id"], status="done", done_at=time.time(),
            result="manual:amy", progress=1.0)
        return g

    # 开关开 + user_store 给了 → payload 带坐席收件地址
    store = _mk_store()
    _done_by_amy(store)
    sent = []
    goal_notify.scan_and_notify(
        store, _cfg(notify={"enabled": True, "push_agent": True}),
        publish=lambda t, p: sent.append(p), user_store=users)
    assert len(sent) == 1
    assert sent[0]["agent_chat_id"] == "42"
    assert sent[0]["agent_username"] == "amy"

    # 开关关（缺省）→ 同样的目标 payload 不带坐席字段
    store2 = _mk_store()
    _done_by_amy(store2)
    sent2 = []
    goal_notify.scan_and_notify(
        store2, _cfg(notify={"enabled": True}),
        publish=lambda t, p: sent2.append(p), user_store=users)
    assert len(sent2) == 1
    assert "agent_chat_id" not in sent2[0]


def test_notify_cfg_push_agent_default_off():
    assert goal_notify.resolve_notify_cfg(
        _cfg(notify={"enabled": True}))["push_agent"] is False
    assert goal_notify.resolve_notify_cfg(
        _cfg(notify={"enabled": True, "push_agent": True}))["push_agent"] is True


async def test_dispatch_sends_agent_copy_once_and_dedupes():
    from src.inbox.webhook_notifier import WebhookNotifier
    n = WebhookNotifier(config=[{
        "name": "tg-ops", "format": "telegram", "token": "T",
        "target": "111", "events": ["goal_complete"], "enabled": True}])
    sent = []

    async def _rec(m, etype, data):
        sent.append((m["name"], m["target"]))
    n._send = _rec

    # 带坐席地址 → 渠道一份 + 坐席副本一份（借同一渠道 bot）
    await n._dispatch({"type": "goal_completed_alert",
                       "data": {"rate_key": "goal_done:a1:g1",
                                "agent_chat_id": "222"}})
    assert sent == [("tg-ops", "111"), ("tg-ops+agent", "222")]

    # 坐席号==渠道号（老板自己建的目标）→ 只发渠道那份，不双推
    sent.clear()
    await n._dispatch({"type": "goal_completed_alert",
                       "data": {"rate_key": "goal_done:a1:g2",
                                "agent_chat_id": "111"}})
    assert sent == [("tg-ops", "111")]

    # 无坐席字段（旧 payload / 开关关）→ 行为与 P0 完全一致
    sent.clear()
    await n._dispatch({"type": "goal_completed_alert",
                       "data": {"rate_key": "goal_done:a1:g3"}})
    assert sent == [("tg-ops", "111")]

    # 主发被限流窗压住（同 rate_key 重放）→ 坐席副本一并不发
    sent.clear()
    await n._dispatch({"type": "goal_completed_alert",
                       "data": {"rate_key": "goal_done:a1:g1",
                                "agent_chat_id": "222"}})
    assert sent == []


async def test_dispatch_agent_copy_skips_non_telegram_channel():
    from src.inbox.webhook_notifier import WebhookNotifier
    n = WebhookNotifier(config=[{
        "name": "hook", "format": "json", "url": "http://x/h",
        "events": ["goal_complete"], "enabled": True}])
    sent = []

    async def _rec(m, etype, data):
        sent.append((m["name"], m["target"]))
    n._send = _rec
    await n._dispatch({"type": "goal_completed_alert",
                       "data": {"rate_key": "goal_done:a1:g9",
                                "agent_chat_id": "222"}})
    # chat_id 是 Telegram 语义，json 渠道绝不复制
    assert sent == [("hook", "")]


# ── P3（2026-08-18）：逐目标点名收件人 + 摸底要点出境 ───────────────────────
# params.notify_extra（用户名或裸 chat_id）→ scan payload extra_chat_ids →
# webhook 借 telegram 渠道逐个加发；slots_brief 受 include_profile 显式 opt-in
# （画像值出境到外部 IM 缺省关）。


def test_sanitize_notify_extra_shapes():
    f = goal_notify.sanitize_notify_extra
    assert f(None) == [] and f("") == [] and f([]) == []
    # 逗号/顿号/分号混分隔 + 保序去重
    assert f("amy, 12345678，bob；12345678") == ["amy", "12345678", "bob"]
    # 单条截 64 字、总量封顶 5
    assert f(["a" * 99]) == ["a" * 64]
    assert f([f"u{i}" for i in range(9)]) == ["u0", "u1", "u2", "u3", "u4"]


def test_resolve_extra_push_targets_mixed():
    users = _FakeUsers({
        "amy": {"enabled": 1, "notify_tg_chat_id": "4242"},
        "nobind": {"enabled": 1, "notify_tg_chat_id": ""},
        "gone": {"enabled": 0, "notify_tg_chat_id": "99"},
    })
    goal = {"params": {"notify_extra": [
        "amy", "-1001234567", "nobind", "gone", "ghost"]}}
    # 用户名→绑定号、裸 chat_id 直用；未绑/禁用/查无一律静默跳过
    assert goal_notify.resolve_extra_push_targets(
        goal, user_store=users) == ["4242", "-1001234567"]
    # 无 user_store：用户名条目全跳过，chat_id 条目不受影响
    assert goal_notify.resolve_extra_push_targets(
        goal, user_store=None) == ["-1001234567"]
    # 短数字串（<4 位）不当 chat_id（防「123」误判）→ 按用户名查、查无跳过
    assert goal_notify.resolve_extra_push_targets(
        {"params": {"notify_extra": ["123"]}}, user_store=users) == []
    assert goal_notify.resolve_extra_push_targets({}, user_store=users) == []


def _done_discovery(store, *, notify_extra=None):
    params = {"slots": "age,interests"}
    if notify_extra is not None:
        params["notify_extra"] = notify_extra
    g = store.create_goal(
        conversation_id="telegram:a1:900", platform="telegram",
        account_id="a1", chat_key="900", template="profile_discovery",
        params=params)
    store.upsert_customer_profile(
        "telegram", "900", {"age": "28岁"}, source="auto")
    store.update_goal_fields(
        g["goal_id"], status="done", done_at=time.time(),
        result="slots_filled", progress=1.0)
    return g


def test_scan_payload_extra_targets_and_brief_gating():
    users = _FakeUsers({"amy": {"enabled": 1, "notify_tg_chat_id": "4242"}})
    store = _mk_store()
    _done_discovery(store, notify_extra=["amy", "-100777"])
    sent = []
    goal_notify.scan_and_notify(
        store, _cfg(notify={"enabled": True}),
        publish=lambda t, p: sent.append(p), user_store=users)
    assert len(sent) == 1
    assert sent[0]["extra_chat_ids"] == ["4242", "-100777"]
    # include_profile 默认关=画像值不出境
    assert "slots_brief" not in sent[0]

    # include_profile 显式开 → 带 facts_line 口径的摸底要点
    store2 = _mk_store()
    _done_discovery(store2)
    sent2 = []
    goal_notify.scan_and_notify(
        store2, _cfg(notify={"enabled": True, "include_profile": True}),
        publish=lambda t, p: sent2.append(p), user_store=users)
    assert sent2 and "28岁" in sent2[0].get("slots_brief", "")
    # 无点名收件人的目标不带 extra 字段（旧 payload 形状不变）
    assert "extra_chat_ids" not in sent2[0]


def test_notify_cfg_include_profile_default_off():
    assert goal_notify.resolve_notify_cfg(
        _cfg(notify={"enabled": True}))["include_profile"] is False
    assert goal_notify.resolve_notify_cfg(
        _cfg(notify={"enabled": True, "include_profile": True})
    )["include_profile"] is True


async def test_dispatch_sends_extra_copies_and_dedupes():
    from src.inbox.webhook_notifier import WebhookNotifier
    n = WebhookNotifier(config=[{
        "name": "tg-ops", "format": "telegram", "token": "T",
        "target": "111", "events": ["goal_complete"], "enabled": True}])
    sent = []

    async def _rec(m, etype, data):
        sent.append((m["name"], m["target"]))
    n._send = _rec

    # 点名收件人与 渠道主号/坐席副本 各自去重，每事件只发一轮
    await n._dispatch({"type": "goal_completed_alert",
                       "data": {"rate_key": "goal_done:a1:x1",
                                "agent_chat_id": "222",
                                "extra_chat_ids": ["222", "333", "111"]}})
    assert sent == [("tg-ops", "111"), ("tg-ops+agent", "222"),
                    ("tg-ops+extra", "333")]

    # 仅点名、无坐席副本 → 只出 extra 副本
    sent.clear()
    await n._dispatch({"type": "goal_completed_alert",
                       "data": {"rate_key": "goal_done:a1:x2",
                                "extra_chat_ids": ["333"]}})
    assert sent == [("tg-ops", "111"), ("tg-ops+extra", "333")]

    # 非 telegram 渠道绝不复制（chat_id 是 Telegram 语义）
    n2 = WebhookNotifier(config=[{
        "name": "hook", "format": "json", "url": "http://x/h",
        "events": ["goal_complete"], "enabled": True}])
    sent2 = []

    async def _rec2(m, etype, data):
        sent2.append((m["name"], m["target"]))
    n2._send = _rec2
    await n2._dispatch({"type": "goal_completed_alert",
                        "data": {"rate_key": "goal_done:a1:x3",
                                 "extra_chat_ids": ["333"]}})
    assert sent2 == [("hook", "")]


def test_webhook_formatter_brief_and_conv_link():
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("goal_completed_alert", {
        "contact_name": "小美", "template_name": "客户摸底",
        "title": "客户摸底", "platform": "telegram", "account_id": "a1",
        "conversation_id": "telegram:a1:900", "result_kind": "auto",
        "slots_brief": "年龄:28岁｜兴趣:钓鱼",
    })
    assert "目标达成" in title
    assert "/workspace?conv=telegram%3Aa1%3A900" in text   # 会话深链（已 quote）
    assert "钓鱼" in text                                   # 摸底要点行
    # 旧 payload（无 brief/无会话 id）：不出现空「摸底要点」行与空链接
    _t2, text2 = _build_message("goal_completed_alert", {
        "contact_name": "x", "conversation_id": ""})
    assert "摸底要点" not in text2 and "打开会话" not in text2
