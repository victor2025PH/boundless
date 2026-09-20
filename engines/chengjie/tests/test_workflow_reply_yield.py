# -*- coding: utf-8 -*-
"""实施92 P0-1 门禁：客户回复即让路（工作链停止规则）。

不变量：
- 客户新入站晚于「启动+宽限 / 让路确认点」→ 按链 on_reply 处置
  （pause 默认 / complete / continue）；
- 暂停时记 ``reply_yield_ack_ts``＝触发入站的时间戳——同一条回复绝不
  重复触发；恢复（resume）把确认点推进到当下——暂停期间的旧回复不会让
  「恢复」秒变「再暂停」；只有**更新**的回复才再让路；
- 巡检先于步骤推进（tick 内先让路再推步）；
- ``reply_yield.enabled: false`` = 完全旧行为。
"""

import time
from types import SimpleNamespace

from src.inbox.store import InboxStore
from src.inbox.workflow_autorun import workflow_tick
from src.inbox.workflow_reply_yield import (
    effective_action,
    resolve_reply_yield_cfg,
    sweep,
)

_CFG_OFF = {"inbox": {"workflows": {"reply_yield": {"enabled": False}}}}


def _store(tmp_path):
    return InboxStore(tmp_path / "inbox.db")


def _mk_chain(store, chain_id="c1", *, on_reply="", steps=None):
    store.upsert_workflow_chain({
        "chain_id": chain_id,
        "name": chain_id,
        "steps": steps if steps is not None else [
            {"action_type": "template", "note": "跟一下", "delay_hours": 0},
            {"action_type": "template", "note": "再跟", "delay_hours": 24},
        ],
        "trigger_conditions": {},
        "on_reply": on_reply,
    })
    return chain_id


def _add_inbound(store, cid, ts, text="ok"):
    with store._lock:
        store._conn.execute(
            """INSERT INTO messages
               (message_id, conversation_id, direction, text, ts, ingested_at)
               VALUES (?,?,?,?,?,?)""",
            (f"{cid}:in:{ts}", cid, "in", text, float(ts), float(ts)),
        )
        store._conn.commit()


# ── 纯函数 ──────────────────────────────────────────────────────────────────

def test_effective_action_matrix():
    assert effective_action("", "pause") == "pause"
    assert effective_action("complete", "pause") == "complete"
    assert effective_action("continue", "pause") == "continue"
    assert effective_action("bogus", "complete") == "complete"
    assert effective_action("", "bogus") == "pause"


def test_cfg_default_on():
    cfg = resolve_reply_yield_cfg({})
    assert cfg["enabled"] is True and cfg["default_action"] == "pause"
    assert resolve_reply_yield_cfg(_CFG_OFF)["enabled"] is False


# ── 巡检语义 ────────────────────────────────────────────────────────────────

def test_reply_pauses_running_chain(tmp_path):
    store = _store(tmp_path)
    _mk_chain(store)
    now = time.time()
    eid = store.start_chain_execution("c1", "tg:a:u1", {})
    _add_inbound(store, "tg:a:u1", now + 30)
    st = sweep(store, {}, now=now + 60)
    assert st["paused"] == 1 and st["completed"] == 0
    ex = store.get_workflow_execution(eid)
    assert ex["status"] == "paused"
    import json
    ctx = json.loads(ex["context_json"])
    assert ctx["reply_yield_ack_ts"] == float(now + 30)


def test_reply_before_start_grace_ignored(tmp_path):
    """启动动作常由客户刚来的消息触发——那条不算「对链的回应」。"""
    store = _store(tmp_path)
    _mk_chain(store)
    now = time.time()
    _add_inbound(store, "tg:a:u2", now - 2)   # 启动前的消息
    store.start_chain_execution("c1", "tg:a:u2", {})
    st = sweep(store, {}, now=now + 60)
    assert st["paused"] == 0


def test_on_reply_complete_finishes_chain(tmp_path):
    store = _store(tmp_path)
    _mk_chain(store, "c_re", on_reply="complete")
    now = time.time()
    eid = store.start_chain_execution("c_re", "tg:a:u3", {})
    _add_inbound(store, "tg:a:u3", now + 10)
    hooks = []
    st = sweep(store, {}, now=now + 60,
               goal_event_hook=lambda cid, kind, d: hooks.append(kind))
    assert st["completed"] == 1
    assert store.get_workflow_execution(eid)["status"] == "completed"
    assert hooks == ["chain_completed"]


def test_on_reply_continue_untouched(tmp_path):
    store = _store(tmp_path)
    _mk_chain(store, "c_cont", on_reply="continue")
    now = time.time()
    eid = store.start_chain_execution("c_cont", "tg:a:u4", {})
    _add_inbound(store, "tg:a:u4", now + 10)
    st = sweep(store, {}, now=now + 60)
    assert st["paused"] == 0 and st["completed"] == 0
    assert store.get_workflow_execution(eid)["status"] == "running"


def test_disabled_config_is_noop(tmp_path):
    store = _store(tmp_path)
    _mk_chain(store)
    now = time.time()
    eid = store.start_chain_execution("c1", "tg:a:u5", {})
    _add_inbound(store, "tg:a:u5", now + 10)
    assert sweep(store, _CFG_OFF, now=now + 60) == {
        "checked": 0, "paused": 0, "completed": 0}
    assert store.get_workflow_execution(eid)["status"] == "running"


def test_resume_acks_and_only_newer_reply_repauses(tmp_path):
    store = _store(tmp_path)
    _mk_chain(store)
    now = time.time()
    eid = store.start_chain_execution("c1", "tg:a:u6", {})
    _add_inbound(store, "tg:a:u6", now + 10)
    sweep(store, {}, now=now + 20)
    assert store.get_workflow_execution(eid)["status"] == "paused"
    # 暂停期间客户又来一条 → 恢复动作本身是人工确认，不得被它秒暂停
    _add_inbound(store, "tg:a:u6", now + 30)
    assert store.resume_workflow_execution(eid, now=now + 40) is True
    st = sweep(store, {}, now=now + 50)
    assert st["paused"] == 0
    assert store.get_workflow_execution(eid)["status"] == "running"
    # 恢复之后的**新**回复 → 再让路
    _add_inbound(store, "tg:a:u6", now + 60)
    st2 = sweep(store, {}, now=now + 70)
    assert st2["paused"] == 1
    assert store.get_workflow_execution(eid)["status"] == "paused"


def test_same_reply_not_double_processed(tmp_path):
    store = _store(tmp_path)
    _mk_chain(store)
    now = time.time()
    eid = store.start_chain_execution("c1", "tg:a:u7", {})
    _add_inbound(store, "tg:a:u7", now + 10)
    sweep(store, {}, now=now + 20)
    # 手工把状态改回 running（模拟外部操作），同一条旧回复不再触发
    with store._lock:
        store._conn.execute(
            "UPDATE workflow_executions SET status='running' WHERE exec_id=?",
            (eid,))
        store._conn.commit()
    st = sweep(store, {}, now=now + 30)
    assert st["paused"] == 0


# ── tick 接线：先让路后推步 ─────────────────────────────────────────────────

def test_tick_yields_before_step_fires(tmp_path):
    """到期步与新回复同 tick 撞车：必须先暂停，步不得发出。"""
    store = _store(tmp_path)
    _mk_chain(store, "c_tick", steps=[
        {"action_type": "note", "note": "内部备注步", "delay_hours": 0},
        {"action_type": "note", "note": "第二步", "delay_hours": 0},
    ])
    now = time.time()
    eid = store.start_chain_execution("c_tick", "tg:a:u8", {})
    _add_inbound(store, "tg:a:u8", now + 10)   # 超出 5s 启动宽限＝真回复
    app_state = SimpleNamespace(
        inbox_store=store,
        config_manager=SimpleNamespace(config={}),
        contacts=None,
    )
    state = {}
    workflow_tick(state, app_state, now=now + 30)
    ex = store.get_workflow_execution(eid)
    assert ex["status"] == "paused"
    assert int(ex["current_step"] or 0) == 0, "让路必须先于步骤推进"
    assert state["reply_yield"]["paused_total"] == 1


def test_tick_normal_advance_without_reply(tmp_path):
    """没有客户回复时 tick 行为不变（零延迟步照走）。"""
    store = _store(tmp_path)
    _mk_chain(store, "c_tick2", steps=[
        {"action_type": "note", "note": "n1", "delay_hours": 0},
    ])
    eid = store.start_chain_execution("c_tick2", "tg:a:u9", {})
    app_state = SimpleNamespace(
        inbox_store=store,
        config_manager=SimpleNamespace(config={}),
        contacts=None,
    )
    workflow_tick({}, app_state, now=time.time() + 5)
    assert store.get_workflow_execution(eid)["status"] == "completed"
