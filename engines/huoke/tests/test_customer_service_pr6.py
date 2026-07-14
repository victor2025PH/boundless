# -*- coding: utf-8 -*-
"""真人客服接管 业务层 + schema migration 测试 (PR-6).

覆盖:
1. lead_handoffs 7 个新列 ALTER 安全 (旧 db 升级)
2. assign_to_human: 写字段 + 暂停 AI / 不接管同 handoff
3. record_human_reply: 追加到 replies_json / 非接管人不可写
4. record_internal_note: 追加 notes_json
5. record_outcome: converted/lost 终态释放 AI; pending_followup 不释放
6. list_assigned_to_user / get_handoff_full
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from src.host import ai_takeover_state
from src.host.lead_mesh import customer_service as cs


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """每测用独立 SQLite 文件, 不污染主 db."""
    import src.host.database as _db
    db_path = tmp_path / "test_pr6.db"
    monkeypatch.setattr(_db, "DB_PATH", db_path)  # Path 对象, 不是 str
    # 重新 init
    _db.init_db()
    ai_takeover_state.clear_for_tests()
    yield db_path
    ai_takeover_state.clear_for_tests()


def _make_handoff(canonical_id: str = "lead-001",
                  channel: str = "line") -> str:
    """直接 INSERT 一行 lead_handoffs, 返回 handoff_id."""
    from src.host.database import _connect
    handoff_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO lead_handoffs "
            "(handoff_id, canonical_id, source_agent, channel, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (handoff_id, canonical_id, "agent_a", channel, "pending"),
        )
        conn.commit()
    return handoff_id


# ── schema migration ─────────────────────────────────────────────────
def test_lead_handoffs_has_new_columns(fresh_db):
    """init_db 后 lead_handoffs 含新加的 7 列."""
    from src.host.database import _connect
    with _connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(lead_handoffs)")}
    expected = {
        "assigned_to_username", "assigned_at",
        "customer_service_replies_json", "internal_notes_json",
        "outcome", "outcome_notes", "outcome_at",
    }
    missing = expected - cols
    assert not missing, f"缺列: {missing}"


# ── assign_to_human ──────────────────────────────────────────────────
def test_assign_writes_username_and_marks_takeover(fresh_db):
    hid = _make_handoff()
    result = cs.assign_to_human(
        hid, "agent_zhang",
        peer_name_hint="Alice", device_id_hint="d1",
    )
    assert result["assigned_to_username"] == "agent_zhang"
    assert result["assigned_at"]

    # 数据库里确实写了
    rec = cs._get_handoff(hid)
    assert rec["assigned_to_username"] == "agent_zhang"

    # ai_takeover_state 标了
    assert ai_takeover_state.is_taken_over("Alice", "d1") is True


def test_assign_without_peer_hint_does_not_mark_takeover(fresh_db):
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_li")  # 没传 peer/device
    assert ai_takeover_state.is_taken_over("Alice", "d1") is False


def test_assign_blocks_other_user_from_re_assign(fresh_db):
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_zhang")
    with pytest.raises(RuntimeError, match="agent_zhang"):
        cs.assign_to_human(hid, "agent_li")


def test_assign_allows_same_user_re_assign(fresh_db):
    """同一个人重复点"我接手"应 idempotent."""
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_zhang")
    # 再次 assign 同 username 不应抛
    cs.assign_to_human(hid, "agent_zhang")


def test_assign_unknown_handoff_raises_keyerror(fresh_db):
    with pytest.raises(KeyError):
        cs.assign_to_human("nonexistent-id", "agent_zhang")


# ── record_human_reply ───────────────────────────────────────────────
def test_record_reply_appends_to_json(fresh_db):
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_zhang")
    cs.record_human_reply(hid, "agent_zhang", "ご無理しないでね")
    cs.record_human_reply(hid, "agent_zhang", "また話しましょう")

    rec = cs._get_handoff(hid)
    replies = json.loads(rec["customer_service_replies_json"])
    assert len(replies) == 2
    assert replies[0]["text"] == "ご無理しないでね"
    assert replies[0]["by"] == "agent_zhang"
    assert replies[1]["text"] == "また話しましょう"


def test_record_reply_blocks_non_assignee(fresh_db):
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_zhang")
    with pytest.raises(RuntimeError, match="不是当前接管人"):
        cs.record_human_reply(hid, "agent_li", "我也想回")


def test_record_reply_allows_unassigned(fresh_db):
    """没人 assign 时, 任何 username 都可写 (admin 兜底)."""
    hid = _make_handoff()
    cs.record_human_reply(hid, "admin", "supervisor 直接回")
    rec = cs._get_handoff(hid)
    replies = json.loads(rec["customer_service_replies_json"])
    assert len(replies) == 1


# ── record_internal_note ─────────────────────────────────────────────
def test_record_note_appends(fresh_db):
    hid = _make_handoff()
    cs.record_internal_note(hid, "agent_zhang", "客户提到孩子,关注亲子话题")
    cs.record_internal_note(hid, "agent_li", "上次LINE加了没回")
    rec = cs._get_handoff(hid)
    notes = json.loads(rec["internal_notes_json"])
    assert len(notes) == 2
    assert notes[0]["by"] == "agent_zhang"


def test_record_note_anyone_can_add(fresh_db):
    """note 任何人都可加 (vs reply 是接管人)."""
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_zhang")
    # 别的 username 也可加 note (主管视角)
    cs.record_internal_note(hid, "supervisor", "建议这个客户优先")


# ── record_outcome ──────────────────────────────────────────────────
def test_outcome_converted_releases_takeover(fresh_db):
    hid = _make_handoff()
    cs.assign_to_human(
        hid, "agent_zhang",
        peer_name_hint="Alice", device_id_hint="d1",
    )
    assert ai_takeover_state.is_taken_over("Alice", "d1") is True

    cs.record_outcome(
        hid, "agent_zhang", "converted",
        notes="客户加 LINE 成功转化",
        peer_name_hint="Alice", device_id_hint="d1",
    )
    rec = cs._get_handoff(hid)
    assert rec["outcome"] == "converted"
    assert rec["outcome_notes"] == "客户加 LINE 成功转化"
    # 终态释放 ai_takeover
    assert ai_takeover_state.is_taken_over("Alice", "d1") is False


def test_outcome_lost_releases_takeover(fresh_db):
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_zhang",
                       peer_name_hint="Bob", device_id_hint="d2")
    cs.record_outcome(
        hid, "agent_zhang", "lost",
        peer_name_hint="Bob", device_id_hint="d2",
    )
    assert ai_takeover_state.is_taken_over("Bob", "d2") is False


def test_outcome_pending_followup_keeps_takeover(fresh_db):
    """pending_followup 不释放 (还要继续跟进)."""
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_zhang",
                       peer_name_hint="Carol", device_id_hint="d3")
    cs.record_outcome(
        hid, "agent_zhang", "pending_followup",
        notes="周末再跟进",
        peer_name_hint="Carol", device_id_hint="d3",
    )
    assert ai_takeover_state.is_taken_over("Carol", "d3") is True


def test_outcome_invalid_value_raises(fresh_db):
    hid = _make_handoff()
    with pytest.raises(ValueError):
        cs.record_outcome(hid, "agent_zhang", "garbage")


# ── 查询 helpers ─────────────────────────────────────────────────────
def test_list_assigned_to_user(fresh_db):
    h1 = _make_handoff(canonical_id="lead-1")
    h2 = _make_handoff(canonical_id="lead-2")
    h3 = _make_handoff(canonical_id="lead-3")
    cs.assign_to_human(h1, "agent_zhang")
    cs.assign_to_human(h2, "agent_zhang")
    cs.assign_to_human(h3, "agent_li")

    rows = cs.list_assigned_to_user("agent_zhang")
    assert len(rows) == 2
    assert all(r["assigned_to_username"] == "agent_zhang" for r in rows)


def test_list_excludes_handoffs_with_outcome(fresh_db):
    """outcome 已标的不应再出现在 my queue."""
    h1 = _make_handoff()
    cs.assign_to_human(h1, "agent_zhang")
    cs.record_outcome(h1, "agent_zhang", "converted")

    rows = cs.list_assigned_to_user("agent_zhang")
    assert len(rows) == 0


def test_get_handoff_full_parses_json_fields(fresh_db):
    hid = _make_handoff()
    cs.assign_to_human(hid, "agent_zhang")
    cs.record_human_reply(hid, "agent_zhang", "test reply")
    cs.record_internal_note(hid, "agent_zhang", "test note")

    rec = cs.get_handoff_full(hid)
    assert isinstance(rec["customer_service_replies"], list)
    assert len(rec["customer_service_replies"]) == 1
    assert rec["customer_service_replies"][0]["text"] == "test reply"
    assert isinstance(rec["internal_notes"], list)
    assert rec["internal_notes"][0]["note"] == "test note"


def test_get_handoff_full_unknown_returns_none(fresh_db):
    assert cs.get_handoff_full("nonexistent") is None
