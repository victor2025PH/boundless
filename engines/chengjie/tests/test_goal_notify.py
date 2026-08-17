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
    assert payload["rate_key"] == "goal_done:a1"
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
