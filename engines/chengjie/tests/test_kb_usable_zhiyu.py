"""KB 可用化（2026-10-08 智语）：命中率口径、启用条目 100% 向量化任务、三语评测基线。

背景（蛋博士计划 P1-4，173 实测）：107 条、启用 22、向量化 1；7 天 241 次查询 241 次命中。
- 命中口径：旧 ``hit = bool(kb_ctx)``，kb_ctx 恒带全局规则 → 恒真。新口径 ``judge_kb_hit``；
- 他加禄 / 英文功能词：paano / po / how 不再算实词，拉丁词整词比对（po ⊄ deposit）；
- 向量化：三处各写各的收成 ``kb_embed_job``，批失败逐条重试，CLI 默认 dry-run；
- 评测：zh/en/tl 50 条，BM25-only 基线 86%（旧口径命中率 100%）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from src.utils.kb_gate import (
    DEFAULT_VEC_HIT_MIN_SIM,
    content_tokens,
    judge_kb_hit,
    lexical_overlap_ok,
)
from src.utils.kb_store import KnowledgeBaseStore, set_vendor_retrieval_excluded

ENGINE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_vendor_override(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("AITR_KB_EXCLUDE_VENDOR", raising=False)
    set_vendor_retrieval_excluded(None)
    yield
    set_vendor_retrieval_excluded(None)


def _entry(eid, title, triggers, **kw):
    d = {"id": eid, "title": title, "category": "常规咨询", "triggers": triggers,
         "scenario": title, "steps": "s", "principles": "", "example_reply_zh": title + " 回复",
         "reply_mode": "ai_guided", "enabled": 1}
    d.update(kw)
    return d


# ── 命中口径 ────────────────────────────────────────────────────────────


def test_rules_only_context_is_not_a_hit():
    res = {"entries": [], "rules": [{"constraint_text": "不承诺价格"}], "examples": [{"x": 1}]}
    assert judge_kb_hit("随便问问", res) == (False, "no_entries")


def test_lexical_hit_and_weak_miss():
    top = {"title": "物流配送", "triggers": "发货 物流 运费 shipping delivery", "scenario": "几天到"}
    ok, why = judge_kb_hit("How long does shipping take?", {"entries": [top]})
    assert ok and why.startswith("lexical:")
    ok2, why2 = judge_kb_hit("I love this song", {"entries": [top]})
    assert not ok2 and why2.startswith("weak:")


def test_vector_similarity_hit_threshold():
    top = {"title": "退款", "triggers": "退款 refund", "_vec_sim": DEFAULT_VEC_HIT_MIN_SIM + 0.1}
    ok, why = judge_kb_hit("ibalik nyo pera ko", {"entries": [top]})
    assert ok and why.startswith("vector:")
    top_low = dict(top, _vec_sim=DEFAULT_VEC_HIT_MIN_SIM - 0.1)
    ok2, why2 = judge_kb_hit("ibalik nyo pera ko", {"entries": [top_low]})
    assert not ok2 and "sim=" in why2
    assert judge_kb_hit("ibalik nyo pera ko", {"entries": [top]}, vec_min_sim=0.99)[0] is False


def test_tagalog_function_words_are_not_content():
    toks = content_tokens("Paano po ba mag-order? Salamat po")
    assert "paano" not in toks and "po" not in toks and "salamat" not in toks
    assert "order" in toks
    assert "magkano" in content_tokens("Magkano po?")  # 真信息词不收


def test_latin_tokens_match_whole_words_not_substrings():
    refund = {"title": "退款", "triggers": "refund po deposit promo", "scenario": ""}
    # 旧口径：salamat/po/kwento 中 "po" 子串命中 deposit/promo + 整词 po → overlap=2 误放行
    assert lexical_overlap_ok("Salamat po sa kwento mo", refund)[0] is False
    assert lexical_overlap_ok("I need a refund", refund)[0] is True
    assert lexical_overlap_ok("refunds please", refund)[0] is True  # 单复数容忍


def test_chinese_overlap_unchanged():
    usdt = {"title": "怎么付款：USDT 结算", "triggers": "怎么付款 付款方式 USDT 结算 收款地址",
            "scenario": "客户询问如何付款、用什么结算", "category": "商务合作"}
    assert lexical_overlap_ok("怎麼不恢復我了呢", usdt)[0] is False
    assert lexical_overlap_ok("付款方式有哪些，USDT 可以吗", usdt)[0] is True


def test_hybrid_search_carries_vector_similarity(tmp_path):
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    kb.add_entry(_entry("v1", "退款", ["退款", "refund"]))
    kb.add_entry(_entry("v2", "发货", ["发货", "shipping"]))
    kb.set_single_embedding("v1", [1.0, 0.0, 0.0])
    kb.set_single_embedding("v2", [0.0, 1.0, 0.0])
    res = kb.search("ibalik nyo pera ko", top_k=2, query_vec=[0.95, 0.05, 0.0])
    assert res["search_mode"] == "hybrid"
    top = res["entries"][0]
    assert top["id"] == "v1" and top["_vec_sim"] > 0.9
    assert judge_kb_hit("ibalik nyo pera ko", res)[0] is True
    bm = kb.search("refund", top_k=2)
    assert all("_vec_sim" not in e for e in bm["entries"])


def test_health_reports_hit_rate(tmp_path):
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    kb.log_query("a", hit=True)
    kb.log_query("b", hit=False)
    h = kb.health()
    assert h["queries_7d"] == 2 and h["hits_7d"] == 1 and h["hit_rate_7d"] == 0.5


def test_skill_manager_logs_quality_hit_once():
    src = (ENGINE / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    import re
    assert "judge_kb_hit(text, _search_result" in src
    # 查询日志只记统一口径（_kb_after_search 的流程控制仍用 _hit，行为不变）
    logged = re.findall(r"log_query\(\s*\w+,\s*hit=([^,]+),", src)
    assert logged == ["_hit_q", "_hit_q"], logged
    assert "if not _kb_logged:" in src and "_kb_logged = True" in src


# ── 向量化任务 ──────────────────────────────────────────────────────────


def test_build_embed_text_single_source():
    from src.utils.kb_embed_job import build_embed_text
    t = build_embed_text({"triggers": '["退款", "refund"]', "title": "退款", "scenario": "x" * 2000})
    assert t.startswith("退款 refund 退款 refund 退款") and len(t) <= 800
    assert build_embed_text({"triggers": ["a", "b"], "title": "T"}) == "a b a b T"
    assert build_embed_text(None) == ""


def _seed(kb, n=5):
    for i in range(n):
        kb.add_entry(_entry(f"e{i}", f"条目{i}", [f"词{i}"]))
    kb.add_entry(_entry("off", "停用条目", ["停用"], enabled=0))


def test_embed_pending_reaches_full_coverage_with_per_entry_retry(tmp_path):
    from src.utils.kb_embed_job import embed_pending_entries
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    _seed(kb)
    kb.add_entry(_entry("bad", "FAILME", ["x"]))
    calls = []

    async def flaky(texts):
        calls.append(len(texts))
        if len(texts) > 1:
            return [[0.1, 0.2]]  # 批次返回数对不上 → 逐条重试
        return [] if "FAILME" in texts[0] else [[0.3, 0.4]]

    st = asyncio.run(embed_pending_entries(kb, flaky, batch_size=10))
    assert st["pending"] == 6 and st["done"] == 5 and st["failed"] == 1
    assert st["failed_ids"] == ["bad"]
    assert st["coverage_after"]["pending"] == 1  # 停用条目不计入
    assert calls[0] == 6 and calls[1:] == [1] * 6

    async def good(texts):
        return [[0.5, 0.5] for _ in texts]

    st2 = asyncio.run(embed_pending_entries(kb, good))
    assert st2["done"] == 1 and st2["coverage_after"]["pct"] == 100
    assert kb.embedding_coverage()["total"] == 6

    async def must_not_call(texts):
        raise AssertionError("幂等：已向量化的不应再调")

    st3 = asyncio.run(embed_pending_entries(kb, must_not_call))
    assert st3["pending"] == 0 and st3["done"] == 0


def test_embed_job_survives_endpoint_exception(tmp_path):
    from src.utils.kb_embed_job import embed_pending_entries
    kb = KnowledgeBaseStore(tmp_path / "kb.db")
    _seed(kb, 3)

    async def boom(texts):
        raise RuntimeError("endpoint down")

    st = asyncio.run(embed_pending_entries(kb, boom))
    assert st["done"] == 0 and st["failed"] == 3 and st["coverage_after"]["pct"] == 0


def test_endpoint_diagnosis_never_echoes_key():
    from src.utils.kb_embed_job import describe_embedding_endpoint
    secret = "dummy-key-for-unit-test-only"
    d = describe_embedding_endpoint({"base_url": "https://api.deepseek.com", "api_key": secret})
    assert d["kind"] == "chat_fallback" and d["likely_supports_embeddings"] is False and d["hint"]
    assert secret not in json.dumps(d)
    d2 = describe_embedding_endpoint({"embedding_base_url": "http://10.0.0.5:11434",
                                      "embedding_api_key": secret})
    assert d2["kind"] == "dedicated" and d2["model"] == "bge-m3" and d2["likely_supports_embeddings"]
    assert secret not in json.dumps(d2)
    d3 = describe_embedding_endpoint({"base_url": "https://example.test/api/ai/v1"})
    assert d3["kind"] == "gateway"


def test_all_embed_paths_share_the_job():
    routes = (ENGINE / "src" / "web" / "routes" / "kb_routes.py").read_text(encoding="utf-8")
    boot = (ENGINE / "src" / "bootstrap" / "background_tasks.py").read_text(encoding="utf-8")
    assert "embed_pending_entries" in routes and "call_embedding_api" in routes
    assert "build_embed_text" in routes
    assert "embed_pending_entries" in boot


def _load_cli():
    spec = importlib.util.spec_from_file_location("kb_embed_all", ENGINE / "scripts" / "kb_embed_all.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_dry_run_does_not_write_and_apply_fills(tmp_path, monkeypatch, capsys):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        "ai:\n  embedding_base_url: http://127.0.0.1:9\n", encoding="utf-8")
    kb = KnowledgeBaseStore(cfg_dir / "knowledge_base.db")
    _seed(kb, 4)
    cli = _load_cli()
    import src.utils.kb_embed_job as job

    async def must_not_call(ai_cfg, texts, **kw):
        raise AssertionError("dry-run 不得调用嵌入端点")

    monkeypatch.setattr(job, "call_embedding_api", must_not_call)
    assert cli.main(["--config", str(cfg_dir / "config.yaml")]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "0/4" in out
    assert kb.embedding_coverage()["done"] == 0

    async def fake(ai_cfg, texts, **kw):
        return [[0.1, 0.9] for _ in texts]

    monkeypatch.setattr(job, "call_embedding_api", fake)
    assert cli.main(["--config", str(cfg_dir / "config.yaml"), "--apply", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["coverage_after"]["pct"] == 100 and rep["failed"] == 0

    async def dead(ai_cfg, texts, **kw):
        return []

    kb.add_entry(_entry("new", "新条目", ["新"]))
    monkeypatch.setattr(job, "call_embedding_api", dead)
    assert cli.main(["--config", str(cfg_dir / "config.yaml"), "--apply"]) == 2


def test_cli_dry_run_opens_db_read_only(tmp_path, capsys):
    """线上库预演：dry-run 只读打开（mode=ro），不实例化 KnowledgeBaseStore，库文件字节不变。"""
    import hashlib
    import os
    import stat
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    db = cfg_dir / "knowledge_base.db"
    kb = KnowledgeBaseStore(db)
    _seed(kb, 3)
    kb.set_single_embedding(kb.get_entries_without_embedding()[0]["id"], [0.3, 0.7])
    expect = kb.embedding_coverage()
    del kb
    cli = _load_cli()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    os.chmod(db, stat.S_IREAD)
    try:
        cov = cli.ro_coverage(db)
        assert (cov["total"], cov["done"], cov["pending"]) == (expect["total"], expect["done"], expect["pending"])
        assert cov["opened"] == "mode=ro"
        assert cli.main(["--config", str(cfg_dir / "missing.yaml"), "--db", str(db), "--json", "--summary"]) == 0
        rep = json.loads(capsys.readouterr().out)
        assert rep["mode"] == "dry-run" and rep["pending"] == []
        assert rep["coverage"]["done"] == 1 and rep["coverage"]["total"] == 3
    finally:
        os.chmod(db, stat.S_IREAD | stat.S_IWRITE)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    src = (ENGINE / "scripts" / "kb_embed_all.py").read_text(encoding="utf-8")
    dry = src.split("if not a.apply:", 1)[1].split("else:", 1)[0]
    assert "KnowledgeBaseStore(" not in dry


# ── 三语评测集与基线 ──────────────────────────────────────────────────────

# 基线（2026-10-08，BM25-only，无嵌入端点）：accuracy 0.86；应命中 top1 0.85 / 召回 0.875；
# 闲聊误命中 0.10；zh 0.76 / en 0.88 / tl 0.94。改词表前（无拉丁停用词、子串比对）0.84 / 误命中 0.20。
BASELINE_ACCURACY = 0.86
BASELINE_FALSE_HIT = 0.10


def test_eval_set_shape():
    from src.eval.kb_hit_eval import load_eval_set
    data = load_eval_set()
    assert data["schema"] == "zhiliao.kb_eval.v1"
    cases = data["cases"]
    assert len(cases) == 50
    ids = {e["id"] for e in data["seed_entries"]}
    by = {}
    for c in cases:
        by[c["lang"]] = by.get(c["lang"], 0) + 1
        assert c["expect"] is None or c["expect"] in ids
    assert set(by) == {"zh", "en", "tl"} and min(by.values()) >= 16
    assert sum(1 for c in cases if c["expect"] is None) == 10
    assert len({c["q"] for c in cases}) == 50


def test_eval_baseline_ratchet(tmp_path):
    from src.eval.kb_hit_eval import evaluate_default
    rep = evaluate_default(work_dir=tmp_path)
    assert rep["n"] == 50 and rep["mode"] == "bm25"
    # 旧口径（bool(kb_ctx)）在带全局规则的库上恒为 100%——这就是 hits ≡ queries 的来源
    assert rep["legacy_hit_rate"] == 1.0
    assert rep["hit_rate"] < 1.0
    assert rep["accuracy"] >= BASELINE_ACCURACY, rep["errors"]
    assert rep["out_of_scope"]["false_hit_rate"] <= BASELINE_FALSE_HIT, rep["errors"]
    assert set(rep["by_lang"]) == {"zh", "en", "tl"}


def test_eval_hybrid_mode_with_fake_embedder(tmp_path):
    """接嵌入后的同一评测：用确定性假向量验证管线（按期望条目给向量），不代表真实模型分数。"""
    from src.eval.kb_hit_eval import evaluate_default, load_eval_set
    data = load_eval_set()
    seed_ids = [e["id"] for e in data["seed_entries"]]
    dim = len(seed_ids) + 1
    q2e = {c["q"]: c["expect"] for c in data["cases"]}
    title2id = {e["title"]: e["id"] for e in data["seed_entries"]}

    def fake_embed(text):
        v = [0.0] * dim
        eid = q2e.get(text)
        if eid is None:
            for t, i in title2id.items():
                if t in text:
                    eid = i
                    break
        v[seed_ids.index(eid) if eid else dim - 1] = 1.0
        return v

    rep = evaluate_default(embed_fn=fake_embed, work_dir=tmp_path)
    assert rep["mode"] == "hybrid"
    assert rep["in_scope"]["hit_recall"] == 1.0
    assert rep["accuracy"] >= BASELINE_ACCURACY
