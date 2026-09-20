# -*- coding: utf-8 -*-
"""Q-10 A（#267，CV4E22 / RU89S6 / 8FSXX2）：坐席采用草稿并人工发送成功 → 草稿置 consumed。

现场：00:35 采用 L1 稿「Talk soon, take care!」并发出，之后无任何 auto_generate_draft，
面板却把同一条旧稿（status 仍 pending，单行复用 inbox:<cid>）重新顶回来——坐席每次发完
都得多点一次「忽略」。根因：前端 fire-and-forget cancel 与紧随的 GET pending 竞态。
契约：send 成功路径服务端同步 consume（``consume_drafts_on_agent_send``）；仅新入站再产新稿。
"""
from __future__ import annotations

import logging

from src.inbox.autodraft_helpers import (
    DRAFT_STATUS_CONSUMED,
    consume_drafts_on_agent_send,
)
from src.inbox.store import InboxStore

CID = "whatsapp:19892968016:15635715247"


def _seed_draft(store: InboxStore, cid: str = CID, status: str = "pending",
                text: str = "Talk soon, take care!") -> str:
    return store.upsert_draft({
        "draft_id": f"inbox:{cid}",
        "source_kind": "inbox", "source_id": cid,
        "conversation_id": cid, "platform": "whatsapp",
        "account_id": "19892968016", "chat_key": "15635715247",
        "peer_text": "Bye!", "draft_text": text, "status": status,
        "autopilot_level": "L1",
    })


def test_consume_marks_pending_draft_consumed_and_removes_from_pending(tmp_path, caplog):
    store = InboxStore(tmp_path / "inbox.db")
    did = _seed_draft(store)
    assert store.get_draft(did)["status"] == "pending"

    with caplog.at_level(logging.INFO, logger="ai_chat_assistant.autodraft"):
        consumed = consume_drafts_on_agent_send(store, CID, draft_id=did, by="agent_send")

    assert consumed == [did]
    row = store.get_draft(did)
    assert row["status"] == DRAFT_STATUS_CONSUMED == "consumed"
    assert row["decided_by"] == "agent_send"
    # 面板取数口径（/api/drafts?status=pending）再也看不到它
    assert store.list_drafts(status="pending", conversation_id=CID) == []
    # 指令要求的那一行日志
    assert any(
        f"[draft] consumed draft_id={did} by=agent_send" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_consume_without_draft_id_finds_canonical_row(tmp_path):
    """坐席自己另写一句（未 Tab 采用，body 无 draft_id）也算回过话：会话在途稿照样消费。"""
    store = InboxStore(tmp_path / "inbox.db")
    did = _seed_draft(store)
    consumed = consume_drafts_on_agent_send(store, CID)
    assert consumed == [did]
    assert store.get_draft(did)["status"] == "consumed"


def test_consume_is_idempotent_and_leaves_terminal_rows_alone(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    did = _seed_draft(store)
    assert consume_drafts_on_agent_send(store, CID) == [did]
    # 第二次发送：已 consumed，不再改写（update_draft_status 原子闸门只认 pending/enriching）
    assert consume_drafts_on_agent_send(store, CID) == []
    assert store.get_draft(did)["status"] == "consumed"
    # 已投递的终态稿不动
    other = "telegram:acc:peer2"
    did2 = _seed_draft(store, cid=other, status="approved")
    assert consume_drafts_on_agent_send(store, other) == []
    assert store.get_draft(did2)["status"] == "approved"


def test_new_inbound_revives_row_as_pending_after_consumed(tmp_path):
    """「仅新入站产生新稿」：单行复用，下一条入站 upsert 把 consumed 行重生为 pending。"""
    store = InboxStore(tmp_path / "inbox.db")
    did = _seed_draft(store)
    consume_drafts_on_agent_send(store, CID)
    assert store.get_draft(did)["status"] == "consumed"
    _seed_draft(store, text="Thanks, you too! I'm just about to grab lunch")
    row = store.get_draft(did)
    assert row["status"] == "pending"
    assert row["draft_text"].startswith("Thanks, you too!")


def test_consume_also_cancels_enriching_and_other_conversation_untouched(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    did = _seed_draft(store, status="enriching")
    other_cid = "whatsapp:19892968016:15550001111"
    did_other = _seed_draft(store, cid=other_cid)
    assert consume_drafts_on_agent_send(store, CID) == [did]
    assert store.get_draft(did)["status"] == "consumed"
    assert store.get_draft(did_other)["status"] == "pending"


def test_consume_best_effort_with_missing_store_or_methods():
    assert consume_drafts_on_agent_send(None, CID) == []

    class _NoMethods:
        pass

    assert consume_drafts_on_agent_send(_NoMethods(), CID, draft_id="inbox:x") == []

    class _Boom:
        def list_drafts(self, **_kw):
            raise RuntimeError("db gone")

        def update_draft_status(self, *_a, **_kw):
            raise RuntimeError("db gone")

    assert consume_drafts_on_agent_send(_Boom(), CID, draft_id="inbox:x") == []


def test_send_route_wires_consume_and_returns_consumed_drafts():
    """send 路由源码契约：_mark_send 里调 consume_drafts_on_agent_send（带 body.draft_id、
    by=agent_send），响应带 consumed_drafts（前端据此免二次 cancel）。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "web" / "routes" / \
        "unified_inbox_send_routes.py"
    txt = src.read_text(encoding="utf-8")
    assert "consume_drafts_on_agent_send(" in txt
    assert 'draft_id=str(body.get("draft_id") or "")' in txt
    assert 'by="agent_send"' in txt
    assert '"consumed_drafts": list(_consumed_drafts)' in txt


def test_workspace_template_sends_draft_id_and_skips_double_cancel():
    """工作台前端契约：采用的草稿 id 随 send body（两条发送路径）；服务端已 consumed 时
    不再补发 cancel POST。"""
    import pathlib
    tpl = pathlib.Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / \
        "unified_inbox.html"
    txt = tpl.read_text(encoding="utf-8")
    assert txt.count("body.draft_id=_cdraft.adoptedId") >= 2
    assert "_cdraftResolveAdopted(d.consumed_drafts)" in txt
    assert "_cdraftResolveAdopted(consumedSrv)" in txt
    assert "function _cdraftResolveAdopted(consumedSrv)" in txt
