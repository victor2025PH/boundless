# -*- coding: utf-8 -*-
"""L2 中央客户画像 store 单测 (真实 PG, db=openclaw_test)."""
from __future__ import annotations

import os
import time

import pytest

# 跳过条件: 无 PG 环境或非主控
_PG_HOST = os.environ.get("OPENCLAW_PG_HOST", "127.0.0.1")
_PG_TEST_DB = "openclaw_test"
_PG_USER = os.environ.get("OPENCLAW_PG_USER", "openclaw_app")
_PG_PW = os.environ.get("OPENCLAW_PG_PASSWORD", "")


def _pg_available() -> bool:
    if not _PG_PW:
        return False
    try:
        import psycopg2
        c = psycopg2.connect(
            host=_PG_HOST, dbname=_PG_TEST_DB, user=_PG_USER, password=_PG_PW,
            connect_timeout=2,
        )
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="需要 PG 环境 (openclaw_test db + OPENCLAW_PG_PASSWORD)",
)


@pytest.fixture
def store():
    """每个测试 truncate 全表 + 用 openclaw_test DB."""
    from src.host.central_customer_store import CentralCustomerStore
    s = CentralCustomerStore(
        host=_PG_HOST,
        dbname=_PG_TEST_DB,
        user=_PG_USER,
        password=_PG_PW,
    )
    # truncate
    with s._cursor() as cur:
        cur.execute(
            "TRUNCATE customer_handoffs, customer_chats, customer_events, "
            "customers RESTART IDENTITY CASCADE"
        )
    yield s
    s.close()


# ── customers ─────────────────────────────────────────────────────────
def test_upsert_customer_new(store):
    cid = store.upsert_customer(
        canonical_id="fb_user_001",
        canonical_source="facebook",
        primary_name="Alice",
        age_band="30s",
        gender="female",
        country="JP",
        interests=["food", "travel"],
        ai_profile={"topics": ["cooking"], "tone": "polite"},
        worker_id="worker-test",
    )
    assert cid

    customers = store.list_customers()
    assert len(customers) == 1
    c = customers[0]
    assert c["primary_name"] == "Alice"
    assert c["age_band"] == "30s"
    assert c["country"] == "JP"
    assert c["interests"] == ["food", "travel"]
    assert c["ai_profile"]["topics"] == ["cooking"]
    assert c["status"] == "in_funnel"


def test_upsert_customer_idempotent(store):
    cid1 = store.upsert_customer(
        canonical_id="fb_user_002", canonical_source="facebook",
        primary_name="Bob", worker_id="w1",
    )
    cid2 = store.upsert_customer(
        canonical_id="fb_user_002", canonical_source="facebook",
        primary_name="Bob (updated)", worker_id="w2",
    )
    assert cid1 == cid2

    customers = store.list_customers()
    assert len(customers) == 1
    assert customers[0]["primary_name"] == "Bob (updated)"
    assert customers[0]["last_worker_id"] == "w2"


def test_upsert_merge_ai_profile(store):
    """ai_profile 应 merge (jsonb concat), 不是 replace."""
    cid = store.upsert_customer(
        canonical_id="x", canonical_source="facebook",
        ai_profile={"topics": ["food"]}, worker_id="w1",
    )
    store.upsert_customer(
        canonical_id="x", canonical_source="facebook",
        ai_profile={"tone": "polite"}, worker_id="w2",
    )
    full = store.get_customer(cid)
    # 两个字段都应在
    assert full["ai_profile"]["topics"] == ["food"]
    assert full["ai_profile"]["tone"] == "polite"


# ── events ────────────────────────────────────────────────────────────
def test_record_event(store):
    cid = store.upsert_customer(
        canonical_id="evt-test", canonical_source="facebook",
        worker_id="w1",
    )
    eid = store.record_event(
        cid, "greeting_sent", worker_id="w1", device_id="d1",
        meta={"template_id": "jp_v3"},
    )
    assert eid

    full = store.get_customer(cid)
    assert len(full["events"]) == 1
    assert full["events"][0]["event_type"] == "greeting_sent"
    assert full["events"][0]["meta"]["template_id"] == "jp_v3"


# ── chats ─────────────────────────────────────────────────────────────
def test_record_chat(store):
    cid = store.upsert_customer(
        canonical_id="chat-test", canonical_source="facebook",
        worker_id="w1",
    )
    store.record_chat(
        cid, channel="messenger", direction="outgoing",
        content="こんにちは", content_lang="ja", ai_generated=True,
        worker_id="w1",
    )
    store.record_chat(
        cid, channel="messenger", direction="incoming",
        content="こんにちは!お元気ですか?", content_lang="ja",
        worker_id="w1",
    )

    full = store.get_customer(cid)
    chats = full["chats"]
    assert len(chats) == 2
    # ts DESC, latest first
    assert chats[0]["direction"] == "incoming"
    assert chats[1]["direction"] == "outgoing"
    assert chats[1]["ai_generated"] is True


def test_chat_invalid_channel_rejected(store):
    cid = store.upsert_customer(
        canonical_id="ch-rej", canonical_source="facebook", worker_id="w1",
    )
    with pytest.raises(ValueError, match="invalid channel"):
        store.record_chat(cid, channel="myspace", direction="outgoing", content="x")


def test_chat_invalid_direction_rejected(store):
    cid = store.upsert_customer(
        canonical_id="dir-rej", canonical_source="facebook", worker_id="w1",
    )
    with pytest.raises(ValueError, match="invalid direction"):
        store.record_chat(cid, channel="messenger", direction="sideways", content="x")


# ── handoffs ──────────────────────────────────────────────────────────
def test_handoff_initiate_accept_complete(store):
    cid = store.upsert_customer(
        canonical_id="ho", canonical_source="facebook", worker_id="w1",
    )
    hid = store.initiate_handoff(
        cid, from_stage="messenger", to_stage="line",
        initiating_worker_id="w-175",
        ai_summary="客户 30s 日本女性, 对料理感兴趣, 已聊 5 轮",
    )
    assert hid

    pending = store.list_pending_handoffs()
    assert len(pending) == 1
    assert pending[0]["handoff_id"] == hid
    assert "料理" in pending[0]["ai_summary"]

    # 接管
    accepted = store.accept_handoff(hid, accepted_by_human="seat-001")
    assert accepted is True

    # 二次接管: idempotent, 返 False (已被别人抢了)
    accepted2 = store.accept_handoff(hid, accepted_by_human="seat-002")
    assert accepted2 is False

    # pending 列表已不含
    pending = store.list_pending_handoffs()
    assert len(pending) == 0

    # 完成 (转化)
    completed = store.complete_handoff(hid, outcome="converted")
    assert completed is True


def test_handoff_invalid_outcome_rejected(store):
    cid = store.upsert_customer(
        canonical_id="ho2", canonical_source="facebook", worker_id="w1",
    )
    hid = store.initiate_handoff(
        cid, from_stage="messenger", to_stage="line",
        initiating_worker_id="w1",
    )
    with pytest.raises(ValueError, match="invalid outcome"):
        store.complete_handoff(hid, outcome="banana")


# ── 查询 ──────────────────────────────────────────────────────────────
def test_list_customers_filters(store):
    for i, st in enumerate(["in_funnel", "in_messenger", "in_line"]):
        store.upsert_customer(
            canonical_id=f"u{i}", canonical_source="facebook",
            status=st, country="JP" if i % 2 == 0 else "TW",
            worker_id="w1",
        )

    assert len(store.list_customers()) == 3
    assert len(store.list_customers(status="in_line")) == 1
    assert len(store.list_customers(country="JP")) == 2
    assert len(store.list_customers(status="in_messenger", country="TW")) == 1


def test_get_customer_full_profile(store):
    cid = store.upsert_customer(
        canonical_id="full-test", canonical_source="facebook",
        primary_name="X", worker_id="w1",
    )
    store.record_event(cid, "greeting_sent", worker_id="w1")
    store.record_chat(cid, channel="messenger", direction="outgoing", content="hi")
    store.initiate_handoff(cid, from_stage="messenger", to_stage="line",
                            initiating_worker_id="w1")

    full = store.get_customer(cid)
    assert full is not None
    assert full["primary_name"] == "X"
    assert len(full["events"]) == 1
    assert len(full["chats"]) == 1
    assert len(full["handoffs"]) == 1


def test_get_customer_missing(store):
    import uuid
    fake = str(uuid.uuid4())
    assert store.get_customer(fake) is None


def test_funnel_stats(store):
    for status in ["in_funnel", "in_funnel", "in_messenger", "converted"]:
        store.upsert_customer(
            canonical_id=f"fs-{status}-{time.time_ns()}",
            canonical_source="facebook",
            status=status, worker_id="w1",
        )
    cid = store.list_customers()[0]["customer_id"]
    store.record_event(cid, "greeting_sent", worker_id="w1")
    store.record_event(cid, "greeting_sent", worker_id="w1")
    store.record_event(cid, "greeting_replied", worker_id="w1")

    stats = store.funnel_stats(days=1)
    assert stats["customers_by_status"]["in_funnel"] == 2
    assert stats["customers_by_status"]["converted"] == 1
    assert stats["events_by_type"]["greeting_sent"] == 2
    assert stats["events_by_type"]["greeting_replied"] == 1


# ── 并发 ──────────────────────────────────────────────────────────────
def test_concurrent_upsert_same_canonical_id(store):
    """20 并发同 canonical_id, 应只 1 行 (UNIQUE 约束)."""
    import threading

    cids = []
    errors = []

    def w(i):
        try:
            cid = store.upsert_customer(
                canonical_id="concurrent-1", canonical_source="facebook",
                primary_name=f"v-{i}",
                worker_id=f"w-{i}",
            )
            cids.append(cid)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=w, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 全部成功 (UPSERT 不冲突)
    assert len(errors) == 0
    assert len(cids) == 20
    # 全是同一个 customer_id
    assert len(set(cids)) == 1
    # DB 只 1 行
    assert len(store.list_customers()) == 1
