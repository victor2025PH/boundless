# -*- coding: utf-8 -*-
"""学习队列止血四件（L-4 F，#201，D-L5，2026-09-06）门禁。

skuio 机实录：166 待审 / 165「疑似重复 99%」/ 寒暄与「语音消息-下载失败」入队 /
外语无译文 / 「全部通过」一键 166 条无确认 / 通过即进共享 KB（跨客户串记忆）。
钉住：
1. 「全部通过」→ 通过已选：approve-all 须 ids + confirm=1，每批 ≤ 20；
2. 入队收窄：寒暄 / 占位 / 系统文本不入队，只收事实类提问；
3. 私事条目通过 → 写该客户 AI 记忆、不进 KB；无客户归属 → 拒绝并给原因；
   进 KB 的条目 source=learner；
4. BM25 命中不再折算 99%（score=0 / method=bm25 → 「相似度未计算」），vendor 不比对；
5. 存量一次性清标（噪音自动拒 / 假重复重算 / 补私事标）幂等。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
# 模块级导入：本文件启用了 from __future__ import annotations，FastAPI 要靠模块全局
# 解析依赖函数的 "Request" 字符串注解——放在函数内 import 会被当成 query 参数（422）。
from fastapi import FastAPI, Request

from src.utils.daily_learner import (APPROVE_BATCH_LIMIT, TRIAGE_BACKFILL_META_KEY,
                                     DailyLearner, LearnerApproveError,
                                     is_fact_question, is_noise_query, private_kinds)

REPO = Path(__file__).resolve().parents[1]


# ── 纯函数：入队收窄 / 私事识别 ─────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "[语音消息 - 下载失败]", "语音消息-下载失败", "【图片】", "[TRANSLATE:hello]",
    "你好", "hi", "Good morning!", "在吗", "😊😊", "?", "",
])
def test_noise_not_enqueued(text):
    assert is_noise_query(text) is True
    assert is_fact_question(text) is False


@pytest.mark.parametrize("text", [
    "怎么充值VIP？", "How much does the premium plan cost?", "退款多久能到账？",
])
def test_fact_questions_pass(text):
    assert is_noise_query(text) is False
    assert is_fact_question(text) is True


def test_private_kinds_detects_customer_private_details():
    assert "money" in private_kinds("可以先转账 5000 元给你吗")
    assert "meet" in private_kinds("下周我飞过来见你，接机吗")
    assert "name" in private_kinds("My name is Maria, remember me")
    assert "family" in private_kinds("我女儿下个月结婚")
    assert "commitment" in private_kinds("你答应周末来找我的")
    # 普通知识提问不是私事
    assert private_kinds("怎么充值VIP？") == []
    assert private_kinds("退款要几天到账") == []


# ── 学习器：库内行为 ────────────────────────────────────────────────────────

class _KB:
    """最小 kb_store 替身：miss 池 / add_entry 记录 source / meta / 无 KB 条目。"""

    def __init__(self, tmp_path):
        self.db_path = tmp_path / "kb.db"
        self.added = []
        self.meta = {}
        self.miss = []

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def add_entry(self, data):
        self.added.append(dict(data))
        return f"kb-{len(self.added)}"

    def get_miss_stats(self, top_k=10):
        return list(self.miss)[:top_k]

    def list_feedback(self, limit=50):
        return []

    def get_auto_suggestions(self, **kw):
        return []

    def search(self, query, top_k=3, include_vendor=None):
        return {"entries": []}

    def _conn(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE kb_entries (id TEXT, title TEXT, triggers TEXT, enabled INT)")
        return c

    def delete_miss_entry(self, q):
        pass


def _draft(query, **kw):
    d = {"source": "miss", "query": query, "hit_count": 2, "category": "其他",
         "title": query[:20], "triggers": ["测试"], "example_reply": "示例回复",
         "confidence": 60}
    d.update(kw)
    return d


@pytest.fixture
def learner(tmp_path):
    kb = _KB(tmp_path)
    dl = DailyLearner(kb, None, db_path=tmp_path / "drafts.db")
    return dl, kb


def test_schema_has_triage_columns(learner):
    dl, _ = learner
    with dl._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(kb_drafts)").fetchall()}
    assert {"query_zh", "private_kinds", "dup_method"} <= cols


def test_collect_filters_noise_and_non_questions(learner):
    dl, kb = learner
    kb.miss = [
        {"query": "语音消息-下载失败", "cnt": 9, "last_at": "t"},
        {"query": "你好", "cnt": 5, "last_at": "t"},
        {"query": "[图片]", "cnt": 4, "last_at": "t"},
        {"query": "今天天气真好", "cnt": 3, "last_at": "t"},       # 非问题样式
        {"query": "怎么充值VIP？", "cnt": 3, "last_at": "t"},
        {"query": "退款要几天？", "cnt": 1, "last_at": "t"},        # 低于门槛
    ]
    materials, funnel = dl.collect_with_funnel(min_miss_count=2)
    assert [m["query"] for m in materials] == ["怎么充值VIP？"]
    assert funnel["miss_placeholder"] == 3
    assert funnel["miss_not_question"] == 1
    assert funnel["miss_below_threshold"] == 1


def test_save_marks_private_kinds_and_approve_writes_memory_not_kb(learner):
    dl, kb = learner
    dl.save_drafts([_draft("我下周飞过来见你，先转 5000 块给你",
                           source_ref="conv:telegram:acct1:chat9")])
    d = dl.list_drafts()[0]
    assert set(d["private_kinds"].split(",")) >= {"money", "meet"}

    written = []

    def _writer(conv, content, quote):
        written.append((conv, content, quote))
        return {"key": "telegram:acct1:chat9", "row_id": 7}

    dl.set_memory_writer(_writer)
    ref = dl.approve_draft(d["id"], operator="tester")
    assert ref == "mem:telegram:acct1:chat9"
    assert written and written[0][0] == "telegram:acct1:chat9"
    assert kb.added == [], "私事条目不得进共享 KB"
    row = dl.get_draft(d["id"])
    assert row["status"] == "approved" and row["entry_id"].startswith("mem:")


def test_private_without_customer_is_refused_and_stays_pending(learner):
    dl, kb = learner
    dl.save_drafts([_draft("我女儿下个月结婚，你来吗")])   # 无 source_ref
    d = dl.list_drafts()[0]
    dl.set_memory_writer(lambda *a: {"key": "x"})
    with pytest.raises(LearnerApproveError) as ei:
        dl.approve_draft(d["id"], operator="tester")
    assert ei.value.reason == "private_no_customer"
    assert dl.get_draft(d["id"])["status"] == "pending"
    assert kb.added == []


def test_private_without_memory_writer_is_refused(learner):
    dl, _ = learner
    dl.save_drafts([_draft("可以借我 2000 元吗", source_ref="conv:telegram:a:b")])
    d = dl.list_drafts()[0]
    with pytest.raises(LearnerApproveError) as ei:
        dl.approve_draft(d["id"])
    assert ei.value.reason == "memory_unavailable"


def test_normal_draft_enters_kb_with_source_learner(learner):
    dl, kb = learner
    dl.save_drafts([_draft("怎么充值VIP？")])
    d = dl.list_drafts()[0]
    assert d["private_kinds"] == ""
    eid = dl.approve_draft(d["id"], operator="tester")
    assert eid == "kb-1"
    assert kb.added[0]["source"] == "learner", "进 KB 的条目要接 J-9 source 字段"


def test_kb_source_learner_is_a_known_source():
    from src.utils.kb_store import KB_SOURCES, normalize_kb_source
    assert "learner" in KB_SOURCES
    assert normalize_kb_source("learner") == "learner"


def test_approve_all_pending_requires_ids_and_caps_batch(learner):
    dl, kb = learner
    dl.save_drafts([_draft(f"问题{i}怎么办？", title=f"t{i}") for i in range(APPROVE_BATCH_LIMIT + 5)])
    assert dl.approve_all_pending(operator="x") == 0, "不传 ids 不得再整队列一键通过"
    ids = [d["id"] for d in dl.list_drafts(limit=200)]
    res = dl.batch_action(ids, "approve", operator="x")
    assert res["approved"] == APPROVE_BATCH_LIMIT
    assert len(res["skipped"]) == 5 and res["limit"] == APPROVE_BATCH_LIMIT
    assert dl.stats()["pending"] == 5


def test_batch_action_reports_private_failure_reasons(learner):
    dl, _ = learner
    dl.save_drafts([_draft("我叫小美，记住我"), _draft("怎么充值VIP？")])
    ids = [d["id"] for d in dl.list_drafts()]
    res = dl.batch_action(ids, "approve", operator="x")
    assert res["approved"] == 1
    assert list(res["failed_reasons"].values()) == ["private_no_customer"]


# ── 相似度：BM25 不再折算 99% ────────────────────────────────────────────────

class _KBWithSearch(_KB):
    def __init__(self, tmp_path, hits):
        super().__init__(tmp_path)
        self.hits = hits
        self.search_kwargs = None

    def search(self, query, top_k=3, include_vendor=None):
        self.search_kwargs = {"include_vendor": include_vendor}
        return {"entries": list(self.hits)}


def test_bm25_hit_is_uncomputed_similarity_not_99(tmp_path):
    kb = _KBWithSearch(tmp_path, [
        {"id": "e1", "title": "VIP 充值说明", "triggers": ["充值", "VIP"],
         "category": "常规咨询", "_score": 40.0},
    ])
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    dup = dl.check_duplicate({"title": "VIP充值", "triggers": ["会员"], "query": "怎么充值VIP"})
    assert dup is not None and dup["method"] == "bm25"
    assert dup["score"] == 0.0, "BM25 分不得再折算成 99% 相似度"
    assert kb.search_kwargs == {"include_vendor": False}, "查重不与厂商预置比对"


def test_bm25_hit_without_content_overlap_is_not_duplicate(tmp_path):
    # 高分但只有虚词重叠（skuio 机 165 条假重复的形态）→ 不算重复
    kb = _KBWithSearch(tmp_path, [
        {"id": "e1", "title": "客户端安装指引", "triggers": ["安装", "客户端"],
         "category": "产品支持", "_score": 180.0},
    ])
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    assert dl.check_duplicate({"title": "退款", "triggers": ["退款"],
                               "query": "怎么才可以退款呢"}) is None


def test_stats_counts_bm25_flag_as_dup_flagged(tmp_path):
    kb = _KBWithSearch(tmp_path, [
        {"id": "e1", "title": "VIP 充值说明", "triggers": ["充值", "VIP"],
         "category": "常规咨询", "_score": 40.0},
    ])
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    dl.save_drafts([_draft("怎么充值VIP", title="VIP充值", triggers=["会员"])])
    d = dl.list_drafts()[0]
    assert d["dup_entry_id"] == "e1" and d["dup_score"] == 0 and d["dup_method"] == "bm25"
    assert dl.stats()["dup_flagged"] == 1


# ── 存量一次性清标 ───────────────────────────────────────────────────────────

def test_backfill_rejects_noise_recalcs_dups_and_tags_private(tmp_path):
    db = tmp_path / "d.db"
    kb = _KB(tmp_path)
    dl = DailyLearner(kb, None, db_path=db)
    # 造存量：噪音 / 假重复 99% / 私事，三条都 pending、都没有新列的值
    dl.save_drafts([_draft("语音消息-下载失败"), _draft("怎么充值VIP？"),
                    _draft("我下周飞过来见你")])
    with dl._conn() as c:
        c.execute("UPDATE kb_drafts SET dup_entry_id='v1', dup_entry_title='厂商', dup_score=0.99 "
                  "WHERE query='怎么充值VIP？'")
        c.execute("UPDATE kb_drafts SET private_kinds='' ")
    kb.meta.pop(TRIAGE_BACKFILL_META_KEY, None)
    # 第二次构造 = 装载后首次运行
    dl2 = DailyLearner(kb, None, db_path=db)
    rows = {r["query"]: r for r in dl2.list_drafts(status="all", limit=50)}
    assert rows["语音消息-下载失败"]["status"] == "rejected"
    assert rows["语音消息-下载失败"]["reviewed_by"] == "system:l4_noise"
    assert rows["怎么充值VIP？"]["dup_entry_id"] == "" and rows["怎么充值VIP？"]["dup_score"] == 0
    assert "meet" in rows["我下周飞过来见你"]["private_kinds"]
    assert kb.meta.get(TRIAGE_BACKFILL_META_KEY), "清标要落幂等键"
    # 幂等：再构造不重复干活（噪音行不会被再改）
    stamp = kb.meta[TRIAGE_BACKFILL_META_KEY]
    DailyLearner(kb, None, db_path=db)
    assert kb.meta[TRIAGE_BACKFILL_META_KEY] == stamp


# ── 路由：approve-all 须 confirm + 上限；translate 端点 ───────────────────────

def _mk_app(tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from src.web.routes.learner_routes import register_learner_routes

    kb = _KB(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("ai: {}\n", encoding="utf-8")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    async def api_auth(request: Request):
        return True

    ctx = SimpleNamespace(config_manager=SimpleNamespace(config={}, config_path=str(cfg_path)),
                          kb_store=kb, telegram_client=None, audit_store=None,
                          api_auth=api_auth)
    register_learner_routes(app, ctx)
    learner = DailyLearner(kb, None, db_path=tmp_path / "knowledge_base.db")
    app.state._daily_learner = learner
    return TestClient(app), learner, kb


def test_route_approve_all_requires_confirm_ids_and_limit(tmp_path):
    client, learner, kb = _mk_app(tmp_path)
    learner.save_drafts([_draft(f"问题{i}怎么办？", title=f"t{i}") for i in range(3)])
    ids = [d["id"] for d in learner.list_drafts()]
    assert client.post("/api/learner/drafts/approve-all", json={}).status_code == 400
    assert client.post("/api/learner/drafts/approve-all", json={"ids": ids}).status_code == 428
    assert kb.added == [], "未确认不得入库"
    too_many = ids + [f"x{i}" for i in range(APPROVE_BATCH_LIMIT)]
    r = client.post("/api/learner/drafts/approve-all", json={"ids": too_many, "confirm": 1})
    assert r.status_code == 400
    r = client.post("/api/learner/drafts/approve-all", json={"ids": ids, "confirm": 1})
    assert r.status_code == 200 and r.json()["approved"] == 3
    assert len(kb.added) == 3 and all(e["source"] == "learner" for e in kb.added)


def test_route_single_approve_private_gives_readable_reason(tmp_path):
    client, learner, kb = _mk_app(tmp_path)
    learner.save_drafts([_draft("我叫 Anna，记得我吗")])
    did = learner.list_drafts()[0]["id"]
    r = client.post(f"/api/learner/drafts/{did}/approve")
    assert r.status_code == 400
    assert "私事" in r.json()["detail"] or "private" in r.json()["detail"].lower()
    assert kb.added == []


def test_route_translate_uses_translation_service_and_caches(tmp_path):
    client, learner, kb = _mk_app(tmp_path)
    learner.save_drafts([_draft("How much is the VIP plan?"), _draft("怎么充值VIP？")])
    rows = {d["query"]: d["id"] for d in learner.list_drafts()}

    class _Res:
        def __init__(self, text, provider):
            self.ok = True
            self.translated_text = text
            self.provider = provider

    svc = MagicMock()

    async def _translate(text, **kw):
        if "VIP plan" in text:
            return _Res("VIP 套餐多少钱？", "hymt")
        return _Res(text, "identity")

    svc.translate = _translate
    from src.ai.translation_service import TranslationService
    svc.__class__ = TranslationService
    client.app.state.translation_service = svc

    r = client.post("/api/learner/drafts/translate", json={"ids": list(rows.values())})
    assert r.status_code == 200
    tr = r.json()["translations"]
    assert tr == {rows["How much is the VIP plan?"]: "VIP 套餐多少钱？"}, "中文原文不产译文"
    assert learner.get_draft(rows["How much is the VIP plan?"])["query_zh"] == "VIP 套餐多少钱？"
    # 第二次直接走草稿行缓存（不再调服务）
    client.app.state.translation_service = None
    r2 = client.post("/api/learner/drafts/translate", json={"ids": list(rows.values())})
    assert r2.json()["translations"] == tr


# ── 模板接线（静态） ────────────────────────────────────────────────────────

def test_template_wires_selected_approve_and_uncomputed_similarity():
    tpl = (REPO / "src" / "web" / "templates" / "learner.html").read_text(encoding="utf-8")
    assert "approveSelected()" in tpl and "confirmApproveSelected()" in tpl
    assert "confirm:1" in tpl, "确认后才带 confirm=1"
    assert "approveAll()" not in tpl, "「全部通过」一键入口必须消失"
    assert "lr4_dup_uncomputed" in tpl and "data-zh-for" in tpl
    assert "/api/learner/drafts/translate" in tpl
    assert "_privBadge(d)" in tpl


def test_i18n_bilingual_for_triage_keys():
    from src.web.i18n_packs.learner_page import EN, ZH
    for k in ("lr4_approve_selected", "lr4_confirm_title", "lr4_private_badge",
              "lr4_dup_uncomputed", "lr4_zh_label", "lr4_mem_written",
              "err.learner.confirm_required", "err.learner.batch_limit",
              "err.learner.private_no_customer"):
        assert ZH.get(k) and EN.get(k), f"{k} 缺双语"
    for k in ("name", "money", "meet", "address", "identity", "family", "health", "commitment"):
        assert ZH.get(f"lr4_private_kind_{k}") and EN.get(f"lr4_private_kind_{k}")
