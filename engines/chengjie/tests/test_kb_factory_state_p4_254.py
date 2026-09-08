# -*- coding: utf-8 -*-
"""P-4 B #254（老板决策 D-P6，2026-09-08）：陪伴域首装 KB 完全为空 + 「新建条目」预填模板 +
系统话术整类移到系统设置「兜底策略」+ 存量清理两步走（dry-run 先）。

skuio 32PTMK 实录（1.0.77 clean 装陪伴域）：KB 剩 8 条全是客服域残留——3 条【示例】
（价格 / 营业时间 / 退款；触发词「多少钱 / 退款 / refund」在陪伴场景是高敏词）+
complaint_fallback + 全局 / 问候 / 闲聊兜底 / 测试回复，全部停用态、用 0 次；示例的分类
「常规咨询 / 退款投诉」在陪伴分类表里根本不存在。N-3 #240 只做到「停用态播进去」。

钉住：
- ``system_seed_plan`` 陪伴域 seed_replies / seed_examples / seed_defaults 全 False；销售域全 True；
- 三个播种口（seed_system_replies / seed_kb_format_examples / seed_default_data）陪伴域一条不写；
  销售域行为与 N-3 完全一致（红线：销售域不变）；
- ``new_entry_templates``：陪伴三例（称呼偏好 / 忌聊话题 / 常聊话题）分类落在陪伴分类表内；
  销售沿用原三例（去【示例】前缀），分类不在表内的归「其他」；
- ``legacy_seed_residue`` 只列 source=system 且 enabled=0 且 use_count=0；``purge_legacy_seed_residue``
  只删清单交集、启用过 / 命中过的豁免；
- kb_routes 两个新端点 + 默认 dry_run；knowledge.html 默认排除 system 来源 + 模板条；
  settings.html 有「兜底策略」卡（开发者模式 / 内部版可见）+ 清除残留两步走；i18n zh/en 齐。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src.utils import business_domain as bdm
from src.utils.kb_store import (
    KB_FORMAT_EXAMPLE_PREFIX,
    KB_FORMAT_EXAMPLES,
    KB_NEW_ENTRY_TEMPLATES_COMPANION,
    KB_SYSTEM_SEEDS_PURGED_KEY,
    SYSTEM_REPLY_SEEDS,
    KnowledgeBaseStore,
    legacy_seed_residue,
    new_entry_templates,
    purge_legacy_seed_residue,
    seed_default_data,
    seed_kb_format_examples,
    seed_system_replies,
    system_seed_plan,
)

REPO = Path(__file__).resolve().parents[1]
CFG_COMPANION = {"domain": "conversion", "business_domain": "companion"}
CFG_SALES = {"domain": "conversion", "business_domain": "sales"}
COMPANION_CATS = ["人设背景", "日常话题", "情感回应", "关系推进", "边界与安全", "其他"]
SALES_CATS = ["产品价值", "常见问题", "转化话术", "异议处理", "命理", "其他"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    bdm.reset_active_business_domain()
    yield
    bdm.reset_active_business_domain()


@pytest.fixture
def store(tmp_path):
    return KnowledgeBaseStore(tmp_path / "kb.db")


def _count(store, sql):
    with store._conn() as c:
        return c.execute(sql).fetchone()[0]


# ── plan ────────────────────────────────────────────────────────────────────

def test_plan_gates_by_domain():
    p = system_seed_plan(CFG_COMPANION)
    assert (p["seed_replies"], p["seed_examples"], p["seed_defaults"]) == (False, False, False)
    s = system_seed_plan(CFG_SALES)
    assert (s["seed_replies"], s["seed_examples"], s["seed_defaults"]) == (True, True, True)


# ── 三个播种口：陪伴域 clean 装 KB = 0 ─────────────────────────────────────────

def test_companion_clean_install_kb_is_completely_empty(store, monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")            # 桌面首装：env 推出陪伴
    seed_default_data(store, CFG_COMPANION)
    r1 = seed_system_replies(store, CFG_COMPANION)
    r2 = seed_kb_format_examples(store, cfg=CFG_COMPANION)
    assert r1["added"] == 0 and r1.get("suppressed_companion") is True
    assert r2["added"] == 0 and r2["reason"] == "companion_empty_kb"
    assert _count(store, "SELECT COUNT(*) FROM kb_entries") == 0
    assert _count(store, "SELECT COUNT(*) FROM kb_error_codes") == 0
    assert _count(store, "SELECT COUNT(*) FROM kb_rules") == 0
    assert store.stats()["total_entries"] == 0
    # 没有任何直发兜底：调用方拿 None → 本轮静默（不冒客服腔）
    for intent in ("greeting", "complaint", "small_talk", "global", "anything"):
        assert store.get_fallback(intent) is None
    assert store.get_direct_reply("test_reply") is None


def test_sales_behaviour_unchanged(store, monkeypatch):
    """红线：销售域保留现有示例与兜底。"""
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    seed_default_data(store, CFG_SALES)
    r1 = seed_system_replies(store, CFG_SALES)
    r2 = seed_kb_format_examples(store, cfg=CFG_SALES)
    assert r1["added"] == 5                                  # 通用兜底五条（无支付系列）
    assert r2["added"] == len(KB_FORMAT_EXAMPLES) == 3
    assert _count(store, "SELECT COUNT(*) FROM kb_error_codes") > 0
    assert _count(store, "SELECT COUNT(*) FROM kb_rules") > 0
    assert store.get_fallback("greeting")                    # 销售域兜底启用


def test_seed_default_data_without_cfg_is_backward_compatible(store, tmp_path):
    """admin.py 那路仍是 seed_default_data(store)：无 cfg 按进程级业务域（默认销售）。"""
    seed_default_data(store)
    assert _count(store, "SELECT COUNT(*) FROM kb_error_codes") > 0
    bdm.set_active_business_domain("companion")
    s2 = KnowledgeBaseStore(tmp_path / "kb2.db")
    seed_default_data(s2)
    assert _count(s2, "SELECT COUNT(*) FROM kb_error_codes") == 0


# ── 「新建条目」预填模板 ─────────────────────────────────────────────────────

def test_companion_templates_three_examples_in_domain_categories():
    tpls = new_entry_templates("companion", COMPANION_CATS)
    assert [t["key"] for t in tpls] == ["companion_address", "companion_avoid", "companion_topics"]
    assert {t["category"] for t in tpls} <= set(COMPANION_CATS)
    assert {t["category"] for t in tpls} == {"人设背景", "边界与安全", "日常话题"}
    for t in tpls:
        assert t["title"] and t["triggers"] and t["scenario"] and t["example_reply_zh"]
        assert not t["title"].startswith(KB_FORMAT_EXAMPLE_PREFIX)
        assert t.get("reply_mode") == "ai_guided"
        # 陪伴模板里不许出现客服域高敏词
        blob = " ".join([t["title"], " ".join(t["triggers"]), t["scenario"]])
        for bad in ("退款", "refund", "多少钱", "报价", "营业时间", "客服电话"):
            assert bad not in blob, (t["key"], bad)
    assert len(KB_NEW_ENTRY_TEMPLATES_COMPANION) == 3


def test_sales_templates_reuse_original_examples_and_normalize_category():
    tpls = new_entry_templates("sales", SALES_CATS)
    assert len(tpls) == 3
    titles = [t["title"] for t in tpls]
    assert all(not x.startswith(KB_FORMAT_EXAMPLE_PREFIX) for x in titles)
    assert any("退款" in x for x in titles) and any("价格" in x for x in titles)
    # 原示例分类「常规咨询 / 退款投诉」不在销售分类表 → 归「其他」，不留孤儿分类
    assert {t["category"] for t in tpls} == {"其他"}
    assert all("id" not in t and "enabled" not in t for t in tpls)


def test_templates_fallback_domain_from_active(monkeypatch):
    bdm.set_active_business_domain("companion")
    assert new_entry_templates(None, COMPANION_CATS)[0]["key"] == "companion_address"
    bdm.set_active_business_domain("sales")
    assert new_entry_templates(None, SALES_CATS)[0]["key"].startswith("sales_")


# ── 清除客服域残留：两步走 ───────────────────────────────────────────────────

def _legacy_companion_box(store):
    """模拟 1.0.77 陪伴机存量：3 示例 + 5 通用兜底全播（N-3 的停用态），外加用户自己的条目。"""
    seed_kb_format_examples(store, force=True, business_domain="sales")
    seed_system_replies(store, CFG_SALES)
    with store._conn() as c:
        c.execute("UPDATE kb_entries SET enabled=0 WHERE COALESCE(source,'user')='system'")
    store.add_entry({"title": "我的知识", "category": "日常话题", "triggers": ["x"],
                     "example_reply_zh": "y", "source": "user", "enabled": 0})


def test_residue_lists_only_disabled_unused_system_rows(store):
    _legacy_companion_box(store)
    items = legacy_seed_residue(store)
    assert len(items) == 8
    assert all(i["source"] == "system" for i in items)
    assert {i["template_key"] for i in items if i["template_key"]} == {
        "global_fallback", "greeting_fallback", "complaint_fallback", "small_talk_fallback", "test_reply"}
    assert sum(1 for i in items if i["title"].startswith(KB_FORMAT_EXAMPLE_PREFIX)) == 3
    # 用户自己的停用条目不在清单里
    assert not any(i["title"] == "我的知识" for i in items)
    # 启用过 / 命中过的豁免
    with store._conn() as c:
        c.execute("UPDATE kb_entries SET enabled=1 WHERE template_key='greeting_fallback'")
        c.execute("UPDATE kb_entries SET use_count=2 WHERE template_key='test_reply'")
    ids = {i["template_key"] for i in legacy_seed_residue(store)}
    assert "greeting_fallback" not in ids and "test_reply" not in ids and len(ids) == 4  # 3 示例共 '' 键


def test_dry_run_deletes_nothing_and_purge_only_intersects_reviewed_ids(store):
    _legacy_companion_box(store)
    before = store.stats()["total_entries"]
    items = legacy_seed_residue(store)                       # dry-run
    assert store.stats()["total_entries"] == before
    reviewed = [i["id"] for i in items[:3]]
    # dry-run 到点删之间有人启用了其中一条 → 自动豁免
    with store._conn() as c:
        c.execute("UPDATE kb_entries SET enabled=1 WHERE id=?", (reviewed[0],))
    n = purge_legacy_seed_residue(store, reviewed)
    assert n == 2
    assert store.stats()["total_entries"] == before - 2
    assert store.stats()["entries_user"] == 1
    assert not store.get_meta(KB_SYSTEM_SEEDS_PURGED_KEY)     # 还有 system 行 → 不打标
    # 空 ids → 什么都不删
    assert purge_legacy_seed_residue(store, []) == 0
    # 清光剩余残留（含被豁免那条之外的）→ 仍有 1 条启用中的 system 行 → 不打标
    purge_legacy_seed_residue(store)
    with store._conn() as c:
        c.execute("UPDATE kb_entries SET enabled=0 WHERE COALESCE(source,'user')='system'")
    purge_legacy_seed_residue(store)
    assert _count(store, "SELECT COUNT(*) FROM kb_entries WHERE COALESCE(source,'user')='system'") == 0
    assert store.get_meta(KB_SYSTEM_SEEDS_PURGED_KEY)         # 全清 → 打标防灌回
    assert seed_system_replies(store, CFG_SALES)["added"] == 0
    assert store.stats()["entries_user"] == 1


# ── 接线：路由 / 页面 / i18n ─────────────────────────────────────────────────

def test_routes_wired_with_dry_run_default():
    from src.web.routes import kb_routes
    src = inspect.getsource(kb_routes)
    assert '"/api/kb/new-entry-templates"' in src
    assert '"/api/kb/entries/purge-legacy-seeds"' in src
    assert 'data.get("dry_run", True) is not False' in src   # 默认只列清单
    assert "purge_legacy_seed_residue(_kb_store, [str(i) for i in ids])" in src
    assert "seed_kb_format_examples(_kb_store, cfg=config_manager)" in src
    # 绝不在启动自检里自动跑清理
    assert "purge_legacy_seed_residue(_kb_store)" not in src.replace(
        "purge_legacy_seed_residue(_kb_store, [str(i) for i in ids])", "")


def test_knowledge_page_hides_system_scripts_and_offers_templates():
    html = (REPO / "src" / "web" / "templates" / "knowledge.html").read_text(encoding="utf-8")
    assert "const querySource = source || '-system';" in html
    assert '<option value="system" hidden>' in html
    assert 'id="kb-tpl-bar"' in html and "/api/kb/new-entry-templates" in html
    assert "_kbTplBarToggle(!id);" in html
    assert "get('edit')" in html                              # 兜底策略页「在知识库里编辑」深链
    from src.web.i18n_packs.kb_page import EN, ZH
    assert "kb2_tpl_lead" in ZH and "kb2_tpl_lead" in EN


def test_settings_fallback_policy_card_wired():
    html = (REPO / "src" / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
    assert 'id="card-fallback"' in html and "fallback:'body-fallback'" in html
    assert "{% if ui_developer_mode or not ui_client_hide %}" in html   # 开发者模式可见
    assert "/api/kb/entries?source=system" in html
    assert "/api/kb/entries/batch-update" in html
    assert "JSON.stringify({dry_run:true})" in html
    assert "JSON.stringify({dry_run:false,ids:pending})" in html
    assert "window.confirm(T_CONFIRM" in html
    from src.web.i18n_packs.settings_page import EN, ZH
    for key in ("set_fb_title", "set_fb_sub", "set_fb_companion_note", "set_fb_sales_note",
                "set_fb_empty", "set_fb_purge_title", "set_fb_purge_hint", "set_fb_purge_preview",
                "set_fb_purge_go", "set_fb_purge_confirm", "set_fb_purge_done", "set_fb_fail"):
        assert key in ZH and key in EN, key
    from src.web.i18n_packs.errors import EN as EEN, ZH as EZH
    assert "err.kb.purge_legacy_ids_required" in EZH and "err.kb.purge_legacy_ids_required" in EEN
    routes = (REPO / "src" / "web" / "routes" / "settings_routes.py").read_text(encoding="utf-8")
    assert '"fb_business_domain": fb_business_domain' in routes
