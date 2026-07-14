"""P1-2 知识库冷启动起步包测试：播种 / 去重 / readiness / 路由契约。"""
from __future__ import annotations

from src.utils.kb_store import KnowledgeBaseStore
from src.utils.kb_starter import (
    get_starter_pack,
    kb_readiness,
    list_starter_packs,
    seed_starter_pack,
)


def _fresh(tmp_path) -> KnowledgeBaseStore:
    return KnowledgeBaseStore(tmp_path / "kb.db")


def test_list_packs_have_domains_and_entries():
    packs = {p["id"]: p for p in list_starter_packs()}
    for dom in ("ecommerce", "payment", "outreach", "general", "bazi"):
        assert dom in packs
        assert packs[dom]["count"] >= 3


def test_bazi_pack_safety_and_coverage(tmp_path):
    """命理包：高危话题（生死/赌博）须有安全话术条目；核心概念齐备；播种可检索。"""
    pack = get_starter_pack("bazi")
    by_title = {e["title"]: e for e in pack["entries"]}
    assert len(pack["entries"]) >= 14
    # 高危话题安全话术：不预测生死、不背书赌博
    death = by_title["问生死病灾怎么答"]["example_reply"]
    assert "医生" in death and "不算" in death
    gamble = by_title["赌博彩票财运怎么答"]["example_reply"]
    assert "保不了" in gamble or "十有九亏" in gamble
    # 反推销口径（不卖开运神物）
    assert "不推荐花钱" in by_title["怎么改运"]["example_reply"] or \
        "冤枉钱" in by_title["五行缺什么怎么办"]["example_reply"]
    # 全部归独立「命理」分类（与客服话术隔离）
    assert all(e.get("category") == "命理" for e in pack["entries"])
    # 播种后 BM25 能按触发词命中
    kb = _fresh(tmp_path)
    added, _, _ = seed_starter_pack(kb, "bazi")
    assert added == len(pack["entries"])
    res = kb.search("喜用神是什么意思", top_k=3)
    titles = [e.get("title") for e in (res.get("entries") or [])]
    assert "喜用神大白话" in titles


def test_get_starter_pack_unknown_falls_back_general():
    assert get_starter_pack("nope")["name"] == get_starter_pack("general")["name"]


def test_readiness_cold_on_empty_then_warm_after_seed(tmp_path):
    kb = _fresh(tmp_path)
    r0 = kb_readiness(kb)
    assert r0["available"] is True
    cold0 = r0["is_cold"]
    added, skipped, titles = seed_starter_pack(kb, "ecommerce")
    assert added >= 3
    assert skipped == 0
    assert len(titles) == added
    r1 = kb_readiness(kb)
    assert r1["enabled_entries"] >= added
    # 播种足量后应不再「冷」（阈值内）
    assert r1["enabled_entries"] >= r1["cold_threshold"]
    assert r1["is_cold"] is False
    assert cold0 is True or r0["enabled_entries"] < r0["cold_threshold"]


def test_seed_is_idempotent_by_title(tmp_path):
    kb = _fresh(tmp_path)
    a1, s1, _ = seed_starter_pack(kb, "payment")
    a2, s2, _ = seed_starter_pack(kb, "payment")  # 再播一次
    assert a1 >= 3
    assert a2 == 0           # 全部因标题已存在被跳过
    assert s2 == a1
    # 条目数未翻倍
    total = kb.stats()["total_entries"]
    assert total >= a1


def test_seeded_entries_have_reply_and_triggers(tmp_path):
    kb = _fresh(tmp_path)
    seed_starter_pack(kb, "outreach")
    rows = kb.list_entries()
    seeded = [e for e in rows if "你们是做什么的" in str(e.get("title", ""))]
    assert seeded
    e = seeded[0]
    # 话术正文落到 example_reply_zh（不是被丢弃）
    assert str(e.get("example_reply_zh") or "").strip()
    # triggers 落库为 JSON 字符串，含内容
    import json
    trg = json.loads(e.get("triggers") or "[]")
    assert isinstance(trg, list) and trg


def test_readiness_handles_none_store():
    r = kb_readiness(None)
    assert r["available"] is False
    assert r["is_cold"] is True


def test_kb_cold_start_routes_registered():
    """冷启动端点随 register_kb_routes 挂载。"""
    import inspect
    from src.web.routes import kb_routes
    src = inspect.getsource(kb_routes.register_kb_routes)
    assert '/api/kb/cold-start' in src
    assert '/api/kb/seed-pack' in src


def test_kb_improvement_routes_registered():
    """P3-2：质量→KB 改进闭环端点随 register_kb_routes 挂载。"""
    import inspect
    from src.web.routes import kb_routes
    src = inspect.getsource(kb_routes.register_kb_routes)
    assert '/api/kb/improvements' in src
    assert '/api/kb/improvements/convert' in src


def test_improvement_candidates_pair_question_and_reply(tmp_path):
    """P3-2：get_kb_improvement_candidates 关联客户问句 + 改写后答案。"""
    import time as _t
    from src.inbox.models import InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "web:web:imp"
    now = _t.time()
    # 客户问句（in）→ 坐席改写后答案（out），其间记一条 edit_send 审计
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m1", direction="in",
        text="你们支持货到付款吗？", ts=now - 10))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m2", direction="out",
        text="支持的，下单时选择货到付款即可～", ts=now + 5))
    store.record_draft_audit("d1", action="edit_send", autopilot_level="L3",
                             agent_id="a1", conversation_id=cid, ts=now)
    cands = store.get_kb_improvement_candidates(0.0)
    assert len(cands) == 1
    c = cands[0]
    assert c["action"] == "edit_send"
    assert "货到付款" in c["question"]
    assert "下单时选择" in c["suggested_reply"]
    store.close()


def test_improvement_candidates_skip_no_question(tmp_path):
    """P3-2：取不到客户问句的候选被跳过（不产生空 trigger 条目）。"""
    import time as _t
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    store.record_draft_audit("d1", action="rejected", autopilot_level="L2",
                             agent_id="a1", conversation_id="web:web:x",
                             ts=_t.time())
    cands = store.get_kb_improvement_candidates(0.0)
    assert cands == []
    store.close()
