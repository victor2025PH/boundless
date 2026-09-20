# -*- coding: utf-8 -*-
"""assistant 悬浮球核心纯函数门禁：限频 / 问答台账 / 帮助库 / 上下文 RBAC / 语料生成。"""
from __future__ import annotations

import pytest

from src.assistant.context_builder import (
    agent_page_visible,
    build_context_block,
    resolve_page,
)
from src.assistant.help_kb import HelpKB
from src.assistant.qa_log import AssistantQALog, _norm_q
from src.assistant.rate_limit import AssistantRateLimiter, evaluate_window
from src.assistant.seed_corpus import (
    build_all_entries,
    build_page_entries,
    build_term_entries,
)
from src.assistant.stats import AssistantStats


# ────────────────────────────────────────────────────────── rate_limit
def test_rate_limit_allows_under_quota():
    v = evaluate_window([], now=1000.0, per_min=3, per_day=10)
    assert v.allowed and v.reason == ""


def test_rate_limit_blocks_per_minute_and_reports_retry():
    stamps = [999.0, 998.0, 997.0]
    v = evaluate_window(stamps, now=1000.0, per_min=3, per_day=100)
    assert not v.allowed and v.reason == "per_min"
    assert v.retry_after_sec >= 1


def test_rate_limit_blocks_per_day():
    stamps = [1000.0 - i * 600 for i in range(20)]  # 20 次都在 24h 内、每分钟不超
    v = evaluate_window(stamps, now=1001.0, per_min=30, per_day=20)
    assert not v.allowed and v.reason == "per_day"


def test_rate_limit_zero_means_unlimited():
    stamps = [1000.0 - i for i in range(500)]
    v = evaluate_window(stamps, now=1001.0, per_min=0, per_day=0)
    assert v.allowed


def test_rate_limiter_stateful_records_only_allowed():
    rl = AssistantRateLimiter()
    ok = 0
    for i in range(5):
        if rl.check_and_record("u1", per_min=3, per_day=10, now=1000.0 + i).allowed:
            ok += 1
    assert ok == 3  # 第 4/5 次被拦，且被拦的不占坑


def test_rate_limiter_users_isolated():
    rl = AssistantRateLimiter()
    assert rl.check_and_record("a", 1, 10, now=1.0).allowed
    assert not rl.check_and_record("a", 1, 10, now=2.0).allowed
    assert rl.check_and_record("b", 1, 10, now=2.0).allowed  # 别人不受牵连


# ────────────────────────────────────────────────────────── qa_log
def test_qa_log_record_and_stats(tmp_path):
    log = AssistantQALog(tmp_path / "assistant.db")
    qa1 = log.record(user_id="u1", role="agent", page="/workspace", q="怎么发语音?",
                     answered=True, top_score=2.5, latency_ms=800)
    qa2 = log.record(user_id="u1", role="agent", page="/workspace", q="xyz 未知功能",
                     answered=False)
    assert qa1 > 0 and qa2 > 0
    s = log.stats(days=7)
    assert s["n"] == 2 and s["answered"] == 1 and s["rate"] == 0.5


def test_qa_log_verdict_and_miss_list(tmp_path):
    log = AssistantQALog(tmp_path / "assistant.db")
    qa1 = log.record(user_id="u", role="agent", page="/x", q="怎么发语音？",
                     answered=True)
    log.record(user_id="u", role="agent", page="/x", q="怎么发语音 ",
               answered=False)
    log.record(user_id="u", role="agent", page="/x", q="没人懂的问题",
               answered=False)
    assert log.set_verdict(qa1, "down")
    assert not log.set_verdict(qa1, "weird")  # 非法 verdict 拒绝
    misses = log.miss_list(days=7, limit=10)
    # 「怎么发语音」归一化后聚合（down 的 + 未答的 = 2 次），排第一
    assert misses[0]["count"] == 2
    assert _norm_q("怎么发语音？") == _norm_q("怎么发语音 ")


def test_qa_log_top_questions_excludes_down(tmp_path):
    log = AssistantQALog(tmp_path / "assistant.db")
    for _ in range(3):
        log.record(user_id="u", role="agent", page="/x", q="怎么换人设", answered=True)
    bad = log.record(user_id="u", role="agent", page="/x", q="坏答案问题", answered=True)
    log.set_verdict(bad, "down")
    tops = log.top_questions(days=7, limit=5)
    assert "怎么换人设" in tops and "坏答案问题" not in tops


# ────────────────────────────────────────────────────────── help_kb
def _seed_kb(tmp_path) -> HelpKB:
    kb = HelpKB(tmp_path / "help.db")
    kb.upsert_entries([
        {"id": "t1", "title": "语音克隆", "title_en": "Voice clone",
         "content": "在收件箱右栏用 cp-voice 生成并发送克隆语音",
         "content_en": "Generate and send cloned voice from the copilot panel",
         "keywords": "语音 voice 克隆 发语音", "source": "seed:test", "path": "/workspace"},
        {"id": "t2", "title": "知识库", "title_en": "Knowledge base",
         "content": "在知识库页维护话术条目", "content_en": "Manage KB entries",
         "keywords": "知识库 kb", "source": "seed:test", "path": "/knowledge"},
    ])
    return kb


def test_help_kb_upsert_idempotent(tmp_path):
    kb = _seed_kb(tmp_path)
    assert kb.count() == 2
    kb.upsert_entries([{"id": "t1", "title": "语音克隆v2", "content": "新内容"}])
    assert kb.count() == 2  # 同 id 覆盖不新增


def test_help_kb_search_zh_and_en(tmp_path):
    kb = _seed_kb(tmp_path)
    hits = kb.search("怎么发语音", top_k=2)
    assert hits and hits[0]["id"] == "t1"
    hits_en = kb.search("voice clone", top_k=2, lang="en")
    assert hits_en and hits_en[0]["id"] == "t1"
    assert hits_en[0]["title"] == "Voice clone"  # en 字段生效


def test_help_kb_empty_query_and_no_hit(tmp_path):
    kb = _seed_kb(tmp_path)
    assert kb.search("") == []
    # 纯拉丁乱词零 token 重叠 → 零命中（中文单字 token 天然有弱重叠，
    # 「无关中文也可能低分命中」是 BM25 字级分词的已知语义，由路由层
    # min_score 地板负责 answered 判定，不在检索层硬拦）
    assert kb.search("qqxyzzy foobar zzzz", top_k=3) == []


def test_help_kb_isolated_instances_no_shared_index(tmp_path):
    """两个不同 db 的 HelpKB 实例索引互不污染（KBStore 类属性坑的反例钉子）。"""
    kb_a = _seed_kb(tmp_path / "a")
    kb_b = HelpKB(tmp_path / "b" / "help.db")
    kb_b.upsert_entries([
        {"id": "z1", "title": "命理", "content": "八字排盘", "keywords": "命理"},
    ])
    assert kb_b.search("命理", top_k=1)[0]["id"] == "z1"
    # A 库检索不受 B 库重建影响
    assert kb_a.search("怎么发语音", top_k=1)[0]["id"] == "t1"
    assert kb_a.search("命理", top_k=1) == []


# ────────────────────────────────────────────────────────── context_builder
def test_agent_page_visible_scopes():
    assert agent_page_visible("/workspace")
    assert agent_page_visible("/workspace/assets?x=1")
    assert not agent_page_visible("/admin/ops")
    assert not agent_page_visible("/knowledge")


def test_resolve_page_known_nav_item():
    info = resolve_page("/workspace")
    assert info.title  # 有标题（来自 nav/help_terms）
    assert info.path == "/workspace"


def test_context_block_agent_never_gets_admin_page_desc():
    block = build_context_block(page="/admin/ops", role="agent", lang="zh")
    assert "运营总览" not in block  # 管理页说明绝不注入 agent 上下文
    assert "用户角色：agent" in block
    # admin 同页则可见
    block_admin = build_context_block(page="/admin/ops", role="admin", lang="zh")
    assert "运营总览" in block_admin and "/admin/ops" in block_admin


def test_context_block_en_lang():
    block = build_context_block(page="/workspace", role="agent", lang="en",
                                ui_build="20260819")
    assert "User role: agent" in block and "UI build" in block


# ────────────────────────────────────────────────────────── seed_corpus
def test_seed_terms_shape_and_bilingual():
    entries = build_term_entries()
    assert len(entries) >= 100  # 158 词条量级
    sample = entries[0]
    for k in ("id", "title", "content", "keywords", "source"):
        assert k in sample
    assert sample["id"].startswith("term:")
    # 双语覆盖：绝大多数词条应有英文标题
    with_en = sum(1 for e in entries if e["title_en"])
    assert with_en / len(entries) > 0.9


def test_seed_pages_have_paths():
    entries = build_page_entries()
    assert len(entries) >= 15
    assert all(e["path"].startswith("/") for e in entries)
    assert all(e["id"].startswith("page:") for e in entries)


def test_seed_all_ids_unique():
    entries = build_all_entries()
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))


def test_seed_into_kb_and_probe(tmp_path):
    """端到端：真语料灌进库，高频问题能命中（语料质量地板）。"""
    kb = HelpKB(tmp_path / "help.db")
    n = kb.upsert_entries(build_all_entries())
    assert n >= 120
    hits = kb.search("坐席工作台", top_k=3)
    assert hits, "真语料应能命中「坐席工作台」"


# ────────────────────────────────────────────────────────── stats
def test_stats_counts_and_dump():
    st = AssistantStats()
    st.record_query(answered=True, latency_ms=100)
    st.record_query(answered=False)
    st.record_query(answered=False, error=True)
    st.record_rate_limited()
    st.record_report(dup=False)
    st.record_report(dup=True)
    st.record_feedback("up")
    st.record_feedback("down")
    d = st.dump()
    assert d["queries"] == 3 and d["answered"] == 1 and d["miss"] == 1
    assert d["errors"] == 1 and d["rate_limited"] == 1
    assert d["reports"] == 2 and d["reports_dup"] == 1
    assert d["feedback_up"] == 1 and d["feedback_down"] == 1
    assert d["avg_latency_ms"] == 100
    prom = st.dump_prom()
    assert "assistant_queries_total 3" in prom


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
