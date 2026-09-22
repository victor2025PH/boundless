"""J-10 A3（#183）：召回记账——注入一次 → episodic_recall_log 一行/条、recall_count 递增、
/api/episodic-memory/used 按会话返回；skill_manager 注入点接线（真 store + 绑定真方法）。
全部 tmp_path。
"""
from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore


@pytest.fixture
def store(tmp_path):
    s = EpisodicMemoryStore(tmp_path / "t.db")
    yield s
    s.close()


def _rc(store, rid):
    r = store._conn.execute(
        "SELECT COALESCE(recall_count,0), COALESCE(last_recalled_ts,0) FROM episodic_memory"
        " WHERE id=?", (rid,)).fetchone()
    return int(r[0]), float(r[1])


def test_bullets_with_ids_matches_text(store: EpisodicMemoryStore):
    uid = "k1"
    a = store.add_fact(uid, "用户喜欢喝燕麦拿铁")
    b = store.add_fact(uid, "用户养了一只叫旺财的狗")
    c = store.add_fact(uid, "用户在银行上班")
    txt, ids = store.get_bullets_with_ids(uid, 2, 800)
    assert txt.count("\n") == 1 and len(ids) == 2
    assert set(ids) <= {a, b, c}
    # 顺序＝bullet 顺序（新近优先：c, b）
    assert ids == [c, b]
    # 旧口径不变：字符串
    assert store.get_bullets_for_prompt(uid, 2, 800) == txt
    # 关键词重排：拿铁 → a 排第一
    txt2, ids2 = store.get_bullets_with_ids(uid, 3, 800, query_text="拿铁", rerank_keywords=True)
    assert ids2[0] == a and txt2.startswith("- 用户喜欢喝燕麦拿铁")
    # 空
    assert store.get_bullets_with_ids("nobody") == ("", [])


def test_record_recall_logs_and_increments(store: EpisodicMemoryStore):
    uid = "k2"
    a = store.add_fact(uid, "用户喜欢喝燕麦拿铁")
    b = store.add_fact(uid, "用户养了一只叫旺财的狗")
    n = store.record_recall([a, b, a, "x", 0, None], memory_key=uid,
                            conversation_id="whatsapp:acct:1", chain="inbox",
                            inbound_msg_id="m1")
    assert n == 2
    assert _rc(store, a)[0] == 1 and _rc(store, b)[0] == 1 and _rc(store, a)[1] > 0
    store.record_recall([a], memory_key=uid, conversation_id="whatsapp:acct:1", chain="inbox")
    assert _rc(store, a)[0] == 2 and _rc(store, b)[0] == 1
    rows = store._conn.execute("SELECT COUNT(*), COUNT(DISTINCT batch_id) FROM episodic_recall_log").fetchone()
    assert (rows[0], rows[1]) == (3, 2)
    assert store.record_recall([], memory_key=uid) == 0
    assert store.record_recall(["bad"], memory_key=uid) == 0
    st = store.recall_stats(days=7)
    assert st == {"window_days": 7, "injections": 2, "rows": 3, "distinct_rows": 2,
                  "conversations": 1}
    assert store.admin_summary(days=7)["recall"]["injections"] == 2


def test_recent_recalls_by_conversation_and_key(store: EpisodicMemoryStore):
    uid = "k3"
    a = store.add_fact(uid, "用户喜欢喝燕麦拿铁")
    b = store.add_fact(uid, "用户养了一只叫旺财的狗")
    t0 = time.time() - 60
    store.record_recall([a, b], memory_key=uid, conversation_id="c1", chain="inbox",
                        inbound_msg_id="m1", now=t0)
    store.record_recall([b], memory_key=uid, conversation_id="c1", chain="inbox",
                        inbound_msg_id="m2", now=t0 + 30)
    store.record_recall([a], memory_key=uid, conversation_id="c2", chain="direct", now=t0 + 40)
    out = store.recent_recalls(conversation_id="c1", batches=1)
    assert len(out) == 1
    assert out[0]["inbound_msg_id"] == "m2" and out[0]["chain"] == "inbox"
    assert [i["id"] for i in out[0]["items"]] == [b]
    assert out[0]["items"][0]["content"] == "用户养了一只叫旺财的狗"
    assert out[0]["items"][0]["recall_count"] == 2
    out2 = store.recent_recalls(conversation_id="c1", batches=5)
    assert [o["inbound_msg_id"] for o in out2] == ["m2", "m1"]
    assert [i["id"] for i in out2[1]["items"]] == [a, b]      # 顺序＝注入顺序
    # 按记忆键：三批都在，最新在前
    byk = store.recent_recalls(memory_key=uid, batches=10)
    assert [o["conversation_id"] for o in byk] == ["c2", "c1", "c1"]
    # 硬删后的行只剩 id
    store.delete_by_id(a)
    gone = store.recent_recalls(conversation_id="c2")[0]["items"][0]
    assert gone == {"id": a, "deleted": True}
    assert store.recent_recalls() == []
    assert store.recent_recalls(conversation_id="nope") == []


def test_skill_manager_injection_records_recall(store: EpisodicMemoryStore):
    """注入点接线：真 store + 绑定 SkillManager._inject_episodic_into_context。"""
    from src.skills.skill_manager import SkillManager

    class _Stub:
        logger = logging.getLogger("t")
        _memory_cfg = {"enabled": True, "scope": "user", "inject_max_items": 8,
                       "inject_max_chars": 1200}
        _cpi = None
        _episodic_store = store
        _episodic_storage_key = SkillManager._episodic_storage_key
        _inject_episodic_into_context = SkillManager._inject_episodic_into_context

    s = _Stub()
    uid = "77001"
    a = store.add_fact(uid, "用户喜欢喝燕麦拿铁")
    b = store.add_fact(uid, "用户养了一只叫旺财的狗")
    ctx = {"conversation_id": "telegram:acct:77001", "user_msg_id": "555"}
    s._inject_episodic_into_context(ctx, uid, uid, current_user_text="拿铁", platform="telegram")
    assert "拿铁" in ctx["_episodic_memory_text"]
    assert _rc(store, a)[0] == 1 and _rc(store, b)[0] == 1
    used = store.recent_recalls(conversation_id="telegram:acct:77001")
    assert len(used) == 1 and used[0]["chain"] == "inbox" and used[0]["inbound_msg_id"] == "555"
    assert {i["id"] for i in used[0]["items"]} == {a, b}
    # A 线（无 conversation_id）→ chain=direct，按记忆键可查
    ctx2 = {}
    s._inject_episodic_into_context(ctx2, uid, uid, current_user_text="狗", platform="telegram")
    byk = store.recent_recalls(memory_key=uid, batches=5)
    assert byk[0]["chain"] == "direct" and _rc(store, b)[0] == 2
    # P3 #341：proactive=True → 只拿 user_stated；ai_inferred 整行不进主动话术
    c = store.add_fact(uid, "用户可能在考虑换工作", source="ai_inferred")
    ctx4 = {}
    s._inject_episodic_into_context(ctx4, uid, uid, platform="telegram", proactive=True)
    assert "拿铁" in ctx4["_episodic_memory_text"] and "换工作" not in ctx4["_episodic_memory_text"]
    ctx5 = {}
    s._inject_episodic_into_context(ctx5, uid, uid, platform="telegram")
    assert "换工作" in ctx5["_episodic_memory_text"], "回复链不受影响"
    s3 = _Stub(); s3._memory_cfg = dict(_Stub._memory_cfg, proactive_stated_only=False)
    ctx6 = {}
    s3._inject_episodic_into_context(ctx6, uid, uid, platform="telegram", proactive=True)
    assert "换工作" in ctx6["_episodic_memory_text"], "开关可关"
    assert _rc(store, c)[0] == 2
    # 旧/假 store（无 with_ids / record_recall）→ 旧口径，不炸
    legacy = MagicMock(spec=["get_bullets_for_prompt"])
    legacy.get_bullets_for_prompt.return_value = "- x"
    s2 = _Stub(); s2._episodic_store = legacy
    ctx3 = {}
    s2._inject_episodic_into_context(ctx3, uid, uid, platform="telegram")
    assert ctx3["_episodic_memory_text"] == "- x"


def test_used_route(tmp_path):
    from starlette.testclient import TestClient
    from src.utils.audit_store import AuditStore
    from src.web.admin import create_app
    from tests.test_web_episodic_memory_api import _load_cm, _run_async

    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    st = EpisodicMemoryStore(tmp_path / "mem.db")
    uid = "acct:900"
    a = st.add_fact(uid, "用户喜欢喝燕麦拿铁")
    st.record_recall([a], memory_key=uid, conversation_id="whatsapp:acct:900", chain="inbox",
                     inbound_msg_id="m9")
    sm = MagicMock()
    sm._episodic_store = st
    tc = MagicMock()
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        r = client.get("/api/episodic-memory/used", params={"conversation_id": "whatsapp:acct:900"})
        assert r.status_code == 200
        d = r.json()
        assert d["count"] == 1
        b0 = d["batches"][0]
        assert b0["chain"] == "inbox" and b0["inbound_msg_id"] == "m9"
        assert b0["items"][0]["id"] == a and b0["items"][0]["content"] == "用户喜欢喝燕麦拿铁"
        assert {"source", "tier", "review_reason", "impact", "source_quote", "status",
                "recall_count"} <= set(b0["items"][0])
        r2 = client.get("/api/episodic-memory/used", params={"memory_key": uid, "limit": 5})
        assert r2.status_code == 200 and r2.json()["count"] == 1
        assert client.get("/api/episodic-memory/used").status_code == 400
        assert client.get("/api/episodic-memory/used",
                          params={"conversation_id": "nope"}).json()["count"] == 0
    st.close()
