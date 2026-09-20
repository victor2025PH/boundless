# -*- coding: utf-8 -*-
"""降出全自动：立即取消 L2 + 批量降档。"""
from src.inbox.store import InboxStore


def _seed_l2(store: InboxStore, cid: str, draft_id: str, status: str = "pending"):
    store.upsert_draft({
        "draft_id": draft_id,
        "conversation_id": cid,
        "platform": "telegram",
        "account_id": "katie",
        "chat_key": "1",
        "source_kind": "inbox",
        "source_id": draft_id,
        "peer_text": "hi",
        "draft_text": "hello",
        "autopilot_level": "L2",
        "status": status,
        "risk_level": "low",
    })


def test_cancel_pending_l2_on_mode_downgrade(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:katie:1"
    store.set_automation_mode(cid, "auto_ai")
    _seed_l2(store, cid, "d-pending", "pending")
    _seed_l2(store, cid, "d-enrich", "enriching")
    _seed_l2(store, cid, "d-l1", "pending")
    # 把第三条改成 L1（upsert 后直接 SQL 或再 upsert）
    store._conn.execute(
        "UPDATE reply_drafts SET autopilot_level='L1' WHERE draft_id=?",
        ("d-l1",),
    )
    store._conn.commit()

    n = store.cancel_pending_l2_drafts(cid, decided_by="mode_downgraded")
    assert n == 2
    rows = {d["draft_id"]: d for d in store.list_drafts(conversation_id=cid, limit=20)}
    assert rows["d-pending"]["status"] == "cancelled"
    assert rows["d-enrich"]["status"] == "cancelled"
    assert rows["d-pending"]["decided_by"] == "mode_downgraded"
    assert rows["d-l1"]["status"] == "pending"  # L1 留给人审
    store.close()


def test_bulk_set_automation_mode_returns_cids(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.set_automation_mode("telegram:a:1", "auto_ai")
    store.set_automation_mode("telegram:a:2", "auto_ai")
    store.set_automation_mode("telegram:a:3", "manual")
    cids = store.bulk_set_automation_mode("auto_ai", "review")
    assert sorted(cids) == ["telegram:a:1", "telegram:a:2"]
    assert store.get_automation_mode_if_set("telegram:a:1") == "review"
    assert store.get_automation_mode_if_set("telegram:a:3") == "manual"
    store.close()
