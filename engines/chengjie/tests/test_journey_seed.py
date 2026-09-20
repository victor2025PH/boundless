# -*- coding: utf-8 -*-
"""RH-P2 历史旅程种子化门禁（src/contacts/journey_seed.py）。

覆盖：候选筛选（private/平台/群残留）、dry-run 零写、apply 建档+事件历史时间戳、
created_at 回溯、intimacy 物化、contact_id 精确回写（不覆盖既有值）、重跑幂等、
每会话事件上限、平台过滤，以及端到端「种子化 → 流失预警榜出真实沉默天数」。
"""
from __future__ import annotations

import sys
import time as _t
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.journey_seed import (
    DEFAULT_PER_CONV_CAP,
    list_seed_candidates,
    run_seed,
)
from src.contacts.merge import MergeService
from src.contacts.store import ContactStore
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.skills.intimacy_engine import IntimacyEngine

NOW = int(_t.time())
DAY = 86400


def _mk_inbox(tmp_path: Path) -> Path:
    """造一个含多形态会话的 inbox.db 文件（真文件——种子器走独立只读连接）。"""
    db = tmp_path / "inbox.db"
    store = InboxStore(db)

    def conv(cid, platform, account, chat_key, *, chat_type="private",
             name="", contact_id="", language=""):
        store.upsert_conversation(InboxConversation(
            conversation_id=cid, platform=platform, account_id=account,
            chat_key=chat_key, display_name=name, chat_type=chat_type,
            contact_id=contact_id, language=language or "unknown",
            last_ts=float(NOW)))

    # A：telegram 私聊，30 天跨度 12 条、最后一条 10 天前（榜上应显示 ~10 天沉默）
    conv("telegram:acc1:9001", "telegram", "acc1", "9001", name="Alice")
    # B：telegram 群（chat_type=group）——必须排除
    conv("telegram:acc1:-100777", "telegram", "acc1", "-100777",
         chat_type="group", name="某群")
    # C：telegram 私聊但 chat_key 负数（群残留脏数据）——必须排除
    conv("telegram:acc1:-42", "telegram", "acc1", "-42", name="残留")
    # D：whatsapp 私聊 4 条、最近活跃
    conv("whatsapp:acc2:66801", "whatsapp", "acc2", "66801", name="Bob")
    # E：私聊但 0 消息——JOIN 后天然不出现
    conv("web:site:visitor-1", "web", "site", "visitor-1", name="访客")
    # F：已带 contact_id 的私聊——回写不得覆盖
    conv("telegram:acc1:9002", "telegram", "acc1", "9002",
         name="Carol", contact_id="ct_existing")

    rows = []

    def msgs(cid, n, *, first_off_d, last_off_d, prefix):
        span = max(1, int((first_off_d - last_off_d) * DAY))
        for i in range(n):
            ts = NOW - last_off_d * DAY - int(span * i / max(1, n - 1)) if n > 1 \
                else NOW - last_off_d * DAY
            rows.append((f"{prefix}_{i}", cid,
                         "in" if i % 2 == 0 else "out", float(ts), float(NOW)))

    msgs("telegram:acc1:9001", 12, first_off_d=30, last_off_d=10, prefix="ma")
    msgs("telegram:acc1:-100777", 6, first_off_d=5, last_off_d=1, prefix="mb")
    msgs("telegram:acc1:-42", 4, first_off_d=5, last_off_d=1, prefix="mc")
    msgs("whatsapp:acc2:66801", 4, first_off_d=3, last_off_d=0.2, prefix="md")
    msgs("telegram:acc1:9002", 3, first_off_d=6, last_off_d=2, prefix="mf")

    with store._lock:  # noqa: SLF001
        store._conn.executemany(  # noqa: SLF001
            "INSERT INTO messages(message_id, conversation_id, direction, ts, "
            "ingested_at) VALUES (?, ?, ?, ?, ?)", rows)
        store._conn.commit()  # noqa: SLF001
    store.close()
    return db


@pytest.fixture
def env(tmp_path):
    inbox_db = _mk_inbox(tmp_path)
    cstore = ContactStore(db_path=tmp_path / "contacts.db")
    engine = IntimacyEngine(cstore)
    yield {"inbox": inbox_db, "store": cstore, "engine": engine,
           "tmp": tmp_path}
    cstore.close()


def _inbox_contact_id(inbox_db: Path, cid: str) -> str:
    import sqlite3
    con = sqlite3.connect(str(inbox_db))
    try:
        row = con.execute(
            "SELECT contact_id FROM conversations WHERE conversation_id=?",
            (cid,)).fetchone()
        return row[0] if row else ""
    finally:
        con.close()


def test_candidates_filter_private_and_group_like(env):
    import sqlite3
    con = sqlite3.connect(f"file:{env['inbox'].as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cands, skipped = list_seed_candidates(con)
    finally:
        con.close()
    ids = {c["conversation_id"] for c in cands}
    # A/D/F 入选；B（group）被 SQL 排除；C（负 chat_key）计入 group_like 跳过
    assert ids == {"telegram:acc1:9001", "whatsapp:acc2:66801",
                   "telegram:acc1:9002"}
    assert skipped["group_like"] == 1


def test_dry_run_writes_nothing(env):
    s = run_seed(env["inbox"], env["store"], env["engine"], apply=False)
    assert s["apply"] is False
    assert s["candidates"] == 3
    assert s["conversations_seeded"] == 3
    assert s["events_planned"] == 12 + 4 + 3
    assert s["events_inserted"] == 0
    assert env["store"].count_contacts() == 0
    assert _inbox_contact_id(env["inbox"], "telegram:acc1:9001") == ""


def test_apply_seeds_with_history_and_intimacy(env):
    s = run_seed(env["inbox"], env["store"], env["engine"], apply=True)
    assert s["conversations_seeded"] == 3
    assert s["contacts_created"] == 3
    assert s["events_inserted"] == s["events_planned"] == 19
    assert s["intimacy_refreshed"] == 3
    assert s["errors"] == 0
    store = env["store"]
    assert store.count_contacts() == 3

    # 事件按真实历史 ts 落库；journey.created_at 回溯到首条消息
    with store._lock:  # noqa: SLF001
        j = store._conn.execute(  # noqa: SLF001
            "SELECT j.journey_id, j.created_at, j.intimacy_score "
            "FROM journeys j JOIN channel_identities ci "
            "ON ci.contact_id = j.contact_id "
            "WHERE ci.channel='telegram' AND ci.external_id='9001'",
        ).fetchone()
        n_ev, min_ts, max_ts = store._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM journey_events "
            "WHERE journey_id=? AND event_type IN ('msg_in','msg_out')",
            (j["journey_id"],)).fetchone()
    assert n_ev == 12
    assert abs(min_ts - (NOW - 30 * DAY)) < 2 * DAY
    assert abs(max_ts - (NOW - 10 * DAY)) < 2 * DAY
    assert abs(j["created_at"] - min_ts) <= 1
    assert j["intimacy_score"] > 0

    # contact_id 精确回写：空值写入、既有值不覆盖
    assert _inbox_contact_id(env["inbox"], "telegram:acc1:9001") != ""
    assert _inbox_contact_id(
        env["inbox"], "telegram:acc1:9002") == "ct_existing"
    # F 会话的 contact 建了档，但回写被既有值挡住 → 统计只含 A/D
    assert s["contact_ids_written"] == 2


def test_apply_rerun_is_idempotent(env):
    run_seed(env["inbox"], env["store"], env["engine"], apply=True)
    s2 = run_seed(env["inbox"], env["store"], env["engine"], apply=True)
    assert s2["contacts_created"] == 0
    assert s2["events_inserted"] == 0
    assert s2["contact_ids_written"] == 0
    assert env["store"].count_contacts() == 3


def test_per_conv_cap_limits_events(env):
    s = run_seed(env["inbox"], env["store"], env["engine"],
                 apply=True, per_conv_cap=10)
    # A 有 12 条 → 截到 10（取最近 10 条）；D 4 条、F 3 条不受影响
    assert s["events_planned"] == 10 + 4 + 3
    assert s["events_inserted"] == 17


def test_platform_filter(env):
    s = run_seed(env["inbox"], env["store"], env["engine"],
                 apply=False, platforms=["whatsapp"])
    assert s["candidates"] == 1
    assert s["events_planned"] == 4


def test_default_cap_matches_intimacy_replay_window():
    assert DEFAULT_PER_CONV_CAP == 500


def test_board_end_to_end_after_seed(env):
    """种子化后，流失预警榜给出真实沉默天数与非零亲密度（整链验收）。"""
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from src.web.routes.contacts_routes import register_contacts_routes

    run_seed(env["inbox"], env["store"], env["engine"], apply=True)

    app = FastAPI()
    register_contacts_routes(
        app, api_auth=lambda: None, contacts_store=env["store"],
        merge_service=MergeService(env["store"]),
        intimacy_engine=env["engine"])
    tc = TestClient(app)
    d = tc.get("/api/relations/health-board?limit=10&scan=100&force=1").json()
    assert d["ok"] is True
    assert d["count"] >= 2
    by_silent = {round(it["days_since_last_msg"] or 0): it for it in d["items"]}
    # A 最后消息 ~10 天前
    assert any(9 <= k <= 11 for k in by_silent)
    assert all(it["intimacy_score"] > 0 for it in d["items"])
