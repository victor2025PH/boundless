"""SLA watcher 二级升级 + 越线快照门禁（2026-07-29）。

补 K2 盲区：K2 只再分配「已认领 + 坐席断线」的草稿，从没被认领的无主草稿
（`claim is None → continue`）永远跳过——正是「L3 草稿 6.3 天没人碰」的类型。
新增：
- 观测扩展（无 flag）：status_snapshot 出 breaching_now / max_wait_min（此刻最惨状况）；
- escalate（flag escalate_hours 默认 0=关）：越过二级阈值 → 发 draft_sla_escalated，
  **特别标记无主草稿**（unclaimed），一次性不刷屏，草稿处置后可重新升级。
"""
from __future__ import annotations

import time
from unittest.mock import patch

from src.inbox.drafts import DraftService
from src.inbox.sla_watcher import SLAWatcher
from src.inbox.store import InboxStore
from src.inbox.template_seeds import SEED_TEMPLATES


def _store() -> InboxStore:
    s = InboxStore(":memory:")
    s.seed_templates(SEED_TEMPLATES)
    return s


def _svc(store: InboxStore) -> DraftService:
    return DraftService(inbox_store=store, line_services=[], wa_services=[],
                        messenger_service=None)


def _insert(store, draft_id, conv_id, *, level="L3", created_ago_sec=0.0):
    ts = time.time() - created_ago_sec
    with store._lock:
        store._conn.execute(
            "INSERT OR REPLACE INTO reply_drafts "
            "(draft_id,conversation_id,platform,account_id,chat_key,"
            "source_kind,source_id,autopilot_level,risk_level,draft_text,"
            "peer_text,status,risk_reasons_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (draft_id, conv_id, "line", "acc1", "u1", "inbox", conv_id, level,
             "medium", "d", "msg", "pending", "[]", ts, ts))
        store._conn.commit()


def _run_breach(watcher):
    published = []
    with patch("src.integrations.shared.event_bus.get_event_bus") as mb:
        mb.return_value.publish = lambda et, data: published.append((et, data))
        watcher._check_sla_breach()
    return published


# ── 观测扩展（无 flag）──────────────────────────────────────────

def test_snapshot_reflects_current_worst():
    store = _store()
    w = SLAWatcher(draft_service=_svc(store), inbox_store=store,
                   config={"sla_hours": 0.001})
    _insert(store, "d1", "c1", created_ago_sec=600)   # 越线 10 分钟
    _insert(store, "d2", "c2", created_ago_sec=120)   # 越线 2 分钟
    _run_breach(w)
    snap = w.status_snapshot()
    assert snap["breaching_now"] == 2
    assert snap["max_wait_min"] >= 10                 # 最惨那条 ≈10 分钟
    assert "escalate_hours" in snap


def test_recovered_draft_clears_snapshot():
    store = _store()
    w = SLAWatcher(draft_service=_svc(store), inbox_store=store,
                   config={"sla_hours": 0.001})
    _insert(store, "d1", "c1", created_ago_sec=600)
    _run_breach(w)
    assert w.status_snapshot()["breaching_now"] == 1
    # 处置掉（删除 pending）后再 tick → 快照归零
    with store._lock:
        store._conn.execute("DELETE FROM reply_drafts WHERE draft_id='d1'")
        store._conn.commit()
    _run_breach(w)
    assert w.status_snapshot()["breaching_now"] == 0
    assert w.status_snapshot()["max_wait_min"] == 0


# ── escalate 默认关 ─────────────────────────────────────────────

def test_escalate_off_by_default():
    store = _store()
    w = SLAWatcher(draft_service=_svc(store), inbox_store=store,
                   config={"sla_hours": 0.001})            # 无 escalate_hours
    _insert(store, "d1", "c1", created_ago_sec=99999)
    published = _run_breach(w)
    assert not any(e[0] == "draft_sla_escalated" for e in published)
    assert w.status_snapshot()["escalating_now"] == 0


# ── escalate 开：无主草稿标记 unclaimed ─────────────────────────

def test_escalate_marks_unclaimed():
    store = _store()
    w = SLAWatcher(draft_service=_svc(store), inbox_store=store,
                   config={"sla_hours": 0.001, "escalate_hours": 0.01})  # 36s 二级线
    _insert(store, "d1", "c1", created_ago_sec=600)        # 越 escalate + 无 claim
    published = _run_breach(w)
    esc = [d for et, d in published if et == "draft_sla_escalated"]
    assert len(esc) == 1
    assert esc[0]["draft_id"] == "d1"
    assert esc[0]["unclaimed"] is True                     # 无主被点名
    snap = w.status_snapshot()
    assert snap["escalating_now"] == 1
    assert snap["unclaimed_escalating"] == 1


def test_escalate_claimed_not_flagged_unclaimed():
    store = _store()
    w = SLAWatcher(draft_service=_svc(store), inbox_store=store,
                   config={"sla_hours": 0.001, "escalate_hours": 0.01})
    _insert(store, "d1", "c1", created_ago_sec=600)
    store.set_conversation_claim("c1", "agent7", agent_name="A7",
                                 ttl_sec=7200, force=True)   # 已认领
    published = _run_breach(w)
    esc = [d for et, d in published if et == "draft_sla_escalated"]
    assert len(esc) == 1
    assert esc[0]["unclaimed"] is False                    # 有主不误标
    assert w.status_snapshot()["unclaimed_escalating"] == 0


def test_escalate_deduped_once():
    store = _store()
    w = SLAWatcher(draft_service=_svc(store), inbox_store=store,
                   config={"sla_hours": 0.001, "escalate_hours": 0.01})
    _insert(store, "d1", "c1", created_ago_sec=600)
    p1 = _run_breach(w)
    p2 = _run_breach(w)                                    # 第二 tick 不再刷屏
    assert sum(1 for et, _ in p1 if et == "draft_sla_escalated") == 1
    assert sum(1 for et, _ in p2 if et == "draft_sla_escalated") == 0
    # 但快照仍反映它还在升级态（可查）
    assert w.status_snapshot()["escalating_now"] == 1


def test_escalate_reescalates_after_disposal():
    store = _store()
    w = SLAWatcher(draft_service=_svc(store), inbox_store=store,
                   config={"sla_hours": 0.001, "escalate_hours": 0.01})
    _insert(store, "d1", "c1", created_ago_sec=600)
    _run_breach(w)
    # 处置掉 → escalated 去重集应收敛，同 id 将来重新越线能再升级
    with store._lock:
        store._conn.execute("DELETE FROM reply_drafts WHERE draft_id='d1'")
        store._conn.commit()
    _run_breach(w)
    assert "d1" not in w._escalated_ids
    _insert(store, "d1", "c1", created_ago_sec=600)        # 又出现（重开）
    p3 = _run_breach(w)
    assert sum(1 for et, _ in p3 if et == "draft_sla_escalated") == 1


def test_is_unclaimed_soft_fail():
    """get_conversation_claim 抛异常 → 按已认领处理（保守不制造噪声）。"""
    store = _store()
    w = SLAWatcher(draft_service=_svc(store), inbox_store=store, config={})

    def _boom(_):
        raise RuntimeError("db down")
    store.get_conversation_claim = _boom
    assert w._is_unclaimed("c1") is False
    assert w._is_unclaimed("") is False
