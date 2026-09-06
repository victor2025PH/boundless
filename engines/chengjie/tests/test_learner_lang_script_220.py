# -*- coding: utf-8 -*-
"""M-5 E（#220，2026-09-06）：学习队列语种按文字系统判 + 未译不留空 + 同题合并。

DMS45P / RJF9N7 实录：日语条目「そっか、向こうはもう物語の世界に戻るのね…」原文与建议
答案均无译文——页面按「含汉字即中文」跳过翻译请求；英文条目译文标「原语: Chinese」
（原语靠猜）；同一日语条目出现两次都标「重复」却并列待审。钉住：

- ``query_lang`` 按脚本块判（假名→ja、谚文→ko、泰文→th、越南声调→vi），与
  translation_service.detect_language 同一函数；``needs_translation`` 非 zh/unknown 才译；
- ``normalize_query_key``：同题不同标点/空格同键；
- ``save_drafts`` 同题合并（hit_count 累加，不新建）；``merge_duplicate_pending`` 把存量
  并列待审合并为一条（其余 rejected + reviewed_by=system:m5_dup_merge）；构造期幂等回填；
- 路由 ``/api/learner/drafts/translate``：``src_langs`` 真实检测结果、``source_lang`` 显式传
  给翻译服务、失败进 ``failed``、中文进 ``skipped``（L-4 F 缓存契约不变）；
- 页面：按脚本判要不要请求、译文行带原语、失败标「未译 · 重试」；i18n 双语。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request

from src.utils.daily_learner import (DUP_MERGE_BACKFILL_META_KEY,
                                     DUP_MERGE_REVIEWER, DailyLearner,
                                     needs_translation, normalize_query_key,
                                     query_lang)
from tests.test_learner_triage_201 import _KB, _draft

REPO = Path(__file__).resolve().parents[1]
JA = "そっか、向こうはもう物語の世界に戻るのね…"


# ── 纯函数 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,lang", [
    (JA, "ja"),                                   # 假名 + 汉字：不再按汉字比例判中文
    ("物語の世界", "ja"),
    ("안녕하세요, 오늘 날씨 어때요?", "ko"),
    ("สวัสดีครับ ราคาเท่าไหร่", "th"),
    ("Xin chào, bạn có khỏe không?", "vi"),
    ("今天天气不错，多少钱？", "zh"),
    ("How much is the VIP plan?", "en"),
    ("😀👍", "unknown"),
    ("", "unknown"),
])
def test_query_lang_by_script(text, lang):
    assert query_lang(text) == lang


def test_query_lang_is_the_shared_detector():
    from src.ai.translation_service import detect_language
    for t in (JA, "안녕하세요", "สวัสดี", "Xin chào bạn", "你好吗", "hello there"):
        assert query_lang(t) == detect_language(t)


@pytest.mark.parametrize("text,need", [
    (JA, True), ("안녕하세요", True), ("สวัสดีครับ", True), ("Xin chào bạn", True),
    ("How much?", True), ("怎么充值VIP？", False), ("😀", False), ("", False),
])
def test_needs_translation(text, need):
    assert needs_translation(text) is need


def test_normalize_query_key_collapses_punct_space_case():
    assert normalize_query_key(JA) == normalize_query_key("そっか 向こうはもう物語の世界に戻るのね")
    assert normalize_query_key("How much is it?") == normalize_query_key("  how much   is it ")
    assert normalize_query_key("多少钱？") == normalize_query_key("多少钱") == "多少钱"
    assert normalize_query_key("退款要几天到账？") != normalize_query_key("退款要几天？")
    assert normalize_query_key("") == "" and normalize_query_key("？！…") == ""


# ── 学习器：同题合并 ────────────────────────────────────────────────────────

@pytest.fixture
def learner(tmp_path):
    kb = _KB(tmp_path)
    return DailyLearner(kb, None, db_path=tmp_path / "drafts.db"), kb


def test_save_drafts_merges_same_key_into_existing_pending(learner):
    dl, _ = learner
    assert dl.save_drafts([_draft(JA, hit_count=2)]) == 1
    # 第二轮定时采集又生成同题（标点略有不同）→ 合并进第一条，不新建
    assert dl.save_drafts([_draft("そっか、向こうはもう物語の世界に戻るのね", hit_count=3)]) == 0
    rows = dl.list_drafts()
    assert len(rows) == 1 and rows[0]["hit_count"] == 5
    # 同一批里两份同题也只落一条
    assert dl.save_drafts([_draft("How much is it?", hit_count=1),
                           _draft("how much is it", hit_count=1)]) == 1
    assert sorted(r["query"] for r in dl.list_drafts()) == sorted([JA, "How much is it?"])
    assert [r for r in dl.list_drafts() if r["query"] == "How much is it?"][0]["hit_count"] == 2


def test_merge_duplicate_pending_keeps_earliest_and_rejects_rest(learner):
    dl, kb = learner
    # 直接插两条并列待审（模拟 1.0.75 之前的存量）
    with dl._conn() as c:
        c.execute("INSERT INTO kb_drafts (id,source,query,hit_count,category,title,triggers,"
                  "example_reply,ai_reasoning,status,created_at,confidence,query_zh) "
                  "VALUES ('a1','miss',?,2,'其他','t','x','r','', 'pending','2026-09-05T10:00:00',60,'')", (JA,))
        c.execute("INSERT INTO kb_drafts (id,source,query,hit_count,category,title,triggers,"
                  "example_reply,ai_reasoning,status,created_at,confidence,query_zh) "
                  "VALUES ('a2','miss',?,3,'其他','t','x','r','', 'pending','2026-09-06T10:00:00',60,'是啊…')",
                  (JA.rstrip("…"),))
        c.execute("INSERT INTO kb_drafts (id,source,query,hit_count,category,title,triggers,"
                  "example_reply,ai_reasoning,status,created_at,confidence,query_zh) "
                  "VALUES ('b1','miss','退款要几天？',1,'其他','t','x','r','', 'pending','2026-09-06T11:00:00',60,'')")
    stats = dl.merge_duplicate_pending()
    assert stats == {"groups": 1, "merged": 1}
    kept = dl.get_draft("a1")
    assert kept["status"] == "pending" and kept["hit_count"] == 5
    assert kept["query_zh"] == "是啊…", "译文从被合并的那条带过来"
    gone = dl.get_draft("a2")
    assert gone["status"] == "rejected" and gone["reviewed_by"] == DUP_MERGE_REVIEWER
    assert dl.get_draft("b1")["status"] == "pending"
    assert [d["id"] for d in dl.list_drafts()] and len(dl.list_drafts()) == 2
    # 幂等
    assert dl.merge_duplicate_pending() == {"groups": 0, "merged": 0}


def test_dup_merge_backfill_runs_once_on_construct(tmp_path):
    kb = _KB(tmp_path)
    dl = DailyLearner(kb, None, db_path=tmp_path / "drafts.db")
    assert DUP_MERGE_BACKFILL_META_KEY in kb.meta
    with dl._conn() as c:
        for i in range(2):
            c.execute("INSERT INTO kb_drafts (id,source,query,hit_count,category,title,triggers,"
                      "example_reply,ai_reasoning,status,created_at,confidence) "
                      "VALUES (?,'miss','How much?',1,'其他','t','x','r','','pending',?,60)",
                      (f"d{i}", f"2026-09-0{i + 1}T10:00:00"))
    # meta 已标 → 再构造不重跑（存量清理只跑一次；新入队由 save_drafts 兜住）
    DailyLearner(kb, None, db_path=tmp_path / "drafts.db")
    assert len(dl.list_drafts()) == 2
    kb.meta.pop(DUP_MERGE_BACKFILL_META_KEY)
    DailyLearner(kb, None, db_path=tmp_path / "drafts.db")
    assert len(dl.list_drafts()) == 1 and dl.list_drafts()[0]["hit_count"] == 2


# ── 路由：原语 + 显式 source_lang + 失败不留空 ──────────────────────────────

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


class _Res:
    def __init__(self, text, provider="ai", ok=True, error=""):
        self.ok = ok
        self.translated_text = text
        self.provider = provider
        self.error = error


def _install_svc(client, fn):
    svc = MagicMock()
    svc.translate = fn
    from src.ai.translation_service import TranslationService
    svc.__class__ = TranslationService
    client.app.state.translation_service = svc


def test_route_translate_reports_src_lang_and_passes_source(tmp_path):
    client, learner, _ = _mk_app(tmp_path)
    learner.save_drafts([_draft(JA), _draft("How much is the VIP plan?"), _draft("怎么充值VIP？"),
                         _draft("สวัสดีครับ ราคาเท่าไหร่")])
    rows = {d["query"]: d["id"] for d in learner.list_drafts()}
    seen = {}

    async def _translate(text, **kw):
        seen[text] = kw
        if text == JA:
            return _Res("是啊，对方已经回到故事的世界里了…")
        if text.startswith("How"):
            return _Res("VIP 套餐多少钱？")
        return _Res("", ok=False, error="ai:target_lang_mismatch")

    _install_svc(client, _translate)
    r = client.post("/api/learner/drafts/translate", json={"ids": list(rows.values())}).json()
    assert r["ok"]
    assert r["src_langs"][rows[JA]] == "ja"
    assert r["src_langs"][rows["How much is the VIP plan?"]] == "en"
    assert r["src_langs"][rows["怎么充值VIP？"]] == "zh"
    assert r["src_langs"][rows["สวัสดีครับ ราคาเท่าไหร่"]] == "th"
    assert r["src_lang_labels"][rows[JA]] == "日语"
    # 显式 source_lang（引擎 prompt 不再猜），中文原文不调服务
    assert seen[JA]["source_lang"] == "ja" and seen[JA]["target_lang"] == "zh"
    assert "怎么充值VIP？" not in seen
    assert r["translations"][rows[JA]].startswith("是啊")
    assert learner.get_draft(rows[JA])["query_zh"].startswith("是啊")
    assert r["skipped"] == {rows["怎么充值VIP？"]: "zh"}
    # 校验打回 → failed 带原因，不再静默留空
    assert r["failed"] == {rows["สวัสดีครับ ราคาเท่าไหร่"]: "ai:target_lang_mismatch"}


def test_route_translate_en_ui_labels_and_service_missing(tmp_path):
    client, learner, _ = _mk_app(tmp_path)
    learner.save_drafts([_draft("Xin chào, bạn có khỏe không?")])
    did = learner.list_drafts()[0]["id"]
    client.app.state.translation_service = None
    r = client.post("/api/learner/drafts/translate?lang=en", json={"ids": [did]}).json()
    assert r["src_langs"][did] == "vi"
    # 服务缺席也不留空：failed 标原因（页面显「未译 · 重试」）
    assert did in r["failed"]


# ── 页面 / i18n ─────────────────────────────────────────────────────────────

def test_template_requests_by_script_and_never_leaves_blank():
    tpl = (REPO / "src" / "web" / "templates" / "learner.html").read_text(encoding="utf-8")
    assert "function _needsTranslation(d)" in tpl
    assert "!/[\\u4e00-\\u9fff]/.test(q)&&" not in tpl, "「含汉字即中文」的旧闸必须消失"
    assert "\\u3040-\\u30FF\\uAC00-\\uD7AF\\u0E00-\\u0E7F" in tpl      # 假名/谚文/泰文即请求
    assert "data-zh-src-for" in tpl and "lr5_src_lang" in tpl
    assert "lr5_untranslated" in tpl and "retryTranslate(" in tpl and "zh-failed" in tpl
    assert "src_lang_labels" in tpl and "failed" in tpl


def test_i18n_bilingual_for_lang_keys():
    from src.web.i18n_packs.learner_page import EN, ZH
    for k in ("lr5_src_lang", "lr5_untranslated", "lr5_retry", "lr5_retrying"):
        assert ZH.get(k) and EN.get(k), k
    assert "{lang}" in ZH["lr5_src_lang"] and "{lang}" in EN["lr5_src_lang"]
    from src.web.routes.learner_routes import _lang_label
    assert _lang_label("ja", "zh") == "日语" and _lang_label("ja", "en") == "Japanese"
    assert _lang_label("xx", "zh") == "xx" and _lang_label("", "zh") == ""
