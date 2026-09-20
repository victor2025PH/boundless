"""J-10 A1（#183）：接地护栏丢弃记账 + 引文式 source_quote 的 #146 对端删消息联动。

此前「外语客户零记忆」只在日志 WARNING 里，页面完全看不出来——现在按原因落表，
``admin_summary`` 出 7 天读数供页面「有 N 条因无法核对原话未记录」。全部 tmp_path。
"""
from __future__ import annotations

import time

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore


@pytest.fixture
def store(tmp_path):
    s = EpisodicMemoryStore(tmp_path / "t.db")
    yield s
    s.close()


def _drops():
    return [
        {"fact": "客户想去大阪玩", "evidence": "想去大阪玩", "reason": "evidence_mismatch"},
        {"fact": "客户明天不用上班", "evidence": "", "reason": "no_evidence"},
        {"fact": "客户想去大阪玩", "evidence": "好呀好呀", "reason": "fact_unanchored"},
        {"fact": "", "evidence": "", "reason": ""},          # 无原因 → 跳过
        "not-a-dict",                                        # 脏输入 → 跳过
    ]


def test_record_and_summarize_by_reason(store: EpisodicMemoryStore):
    assert store.record_grounding_drops("telegram:acct:u1", _drops()) == 3
    assert store.record_grounding_drops("telegram:acct:u2", [
        {"fact": "客户来自日本", "evidence": "you're from Japan", "reason": "evidence_mismatch"},
    ]) == 1
    s = store.grounding_drop_summary(days=7)
    assert s["total"] == 4
    assert s["by_reason"] == {"no_evidence": 1, "evidence_mismatch": 2, "fact_unanchored": 1}
    assert len(s["recent"]) == 4
    assert s["recent"][0]["memory_key"] == "telegram:acct:u2"      # 最新在前
    assert {"ts", "memory_key", "reason", "fact", "evidence"} <= set(s["recent"][0])
    # 按记忆键过滤（客户档案抽屉）
    per = store.grounding_drop_summary(days=7, user_id="telegram:acct:u1", recent=2)
    assert per["total"] == 3 and len(per["recent"]) == 2
    # 空输入 / 零读数不炸
    assert store.record_grounding_drops("k", []) == 0
    assert store.grounding_drop_summary(days=7, user_id="nobody")["total"] == 0


def test_window_and_retention(store: EpisodicMemoryStore):
    now = time.time()
    old = now - 10 * 86400
    store.record_grounding_drops("k", [
        {"fact": "旧", "evidence": "x", "reason": "no_evidence"}], now=old)
    store.record_grounding_drops("k", [
        {"fact": "新", "evidence": "y", "reason": "no_evidence"}], now=now)
    assert store.grounding_drop_summary(days=7)["total"] == 1
    assert store.grounding_drop_summary(days=30)["total"] == 2
    # 30 天外的行在下一次写入时被清掉
    store.record_grounding_drops("k", [
        {"fact": "远古", "evidence": "z", "reason": "no_evidence"}], now=now - 40 * 86400)
    store.record_grounding_drops("k", [
        {"fact": "再新", "evidence": "w", "reason": "no_evidence"}], now=now)
    assert store.grounding_drop_summary(days=90)["total"] == 3   # 远古那条已被清


def test_admin_summary_carries_grounding_drops(store: EpisodicMemoryStore):
    store.add_fact("k1", "客户有一个女儿", source="ai_inferred",
                   source_quote="I have a daughter")
    store.record_grounding_drops("k1", _drops())
    out = store.admin_summary(days=7)
    assert out["new_count"] == 1
    gd = out["grounding_drops"]
    assert gd["total"] == 3 and gd["window_days"] == 7
    assert gd["by_reason"]["evidence_mismatch"] == 1


def test_evidence_source_quote_matches_peer_delete(store: EpisodicMemoryStore):
    """LLM 事实的 source_quote 现在是原话里的一段引文；对端删了那句 → 事实也删（#146）。"""
    key = "whatsapp:acct:639001"
    msg = "I've only had three boyfriends in my whole life. And I was married once."
    rid = store.add_fact(key, "客户结过一次婚", source="ai_inferred",
                         source_quote="I was married once")
    assert rid is not None
    # 启发式事实仍存整句（旧口径，前 200 字相等）
    rid2 = store.add_fact(key, "客户一生只交过三个男朋友", source="user_stated",
                          source_quote=msg[:200])
    assert rid2 is not None
    # 短引文（归一后 <6 字）不做子串匹配，防撞遍全部消息
    rid3 = store.add_fact(key, "客户25岁", source="ai_inferred", source_quote="25")
    assert rid3 is not None
    deleted = store.delete_by_source_quotes("639001", [msg])
    assert deleted == 2
    assert store.count(key) == 1
    assert store.get_row_brief(rid3) is not None
