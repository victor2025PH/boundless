# -*- coding: utf-8 -*-
"""N-3 #240（老板决策 D-N2，2026-09-08）：KB 系统话术种子按域播 + 存量一键清除。

skuio ZNW5CN 实录：1.0.76 **全新安装**知识库仍预置「系统话术·直接」GXP 支付话术 13 条
（gxp_hint_utr_query / gxp_ask_what / gxp_expired …）全部默认启用、用 0 次、未翻译。
J-9 #184 只挡了 knowledge_base.db 随包，没挡 ``kb_store.seed_system_replies`` 每次启动
无条件播进库的 23 条（admin.py + kb_registry 两路都调）；purge-source 有 system 口但 KB 页
只给了 vendor 按钮，且清了下次启动又长回来。

钉住：
- 非支付域（conversion 包：陪伴 / 销售）**不导入** PAYMENT_SEED_KEYS（13 条 gxp_* + 订单 /
  费率 / 通道 / 状态兜底）；payment 域包照旧全播；
- 陪伴域导入的通用兜底 ``source=system`` 且 ``enabled=0``（客服腔先停用）；销售域 enabled=1；
- 给不出配置（admin.py 那路）按「非支付」；桌面 env 推出陪伴；
- ``purge_by_source("system")`` 后打标不再灌回；``purge_payment_seeds`` 只删支付系列并打标；
- ``stats().entries_system_payment`` 计数 → KB 页横幅 / 启动 WARNING 同源；
- kb_routes 有 purge-payment-seeds 端点 + 升级提示；knowledge.html 有横幅；i18n zh/en 齐。
- 「帮助语料自动播种 291 条」是悬浮球 AI 助手语料（assistant.seed），不是客户 KB——本文件
  顺带钉住它不写 kb_entries（698J28 第三次澄清的证据）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.utils import business_domain as bdm
from src.utils import kb_store as kbs
from src.utils.kb_store import (
    KB_PAYMENT_SEEDS_PURGED_KEY,
    KB_SYSTEM_SEEDS_PURGED_KEY,
    PAYMENT_SEED_KEYS,
    SYSTEM_REPLY_SEEDS,
    KnowledgeBaseStore,
    purge_payment_seeds,
    seed_system_replies,
    system_seed_plan,
)

REPO = Path(__file__).resolve().parents[1]
ALL_KEYS = {e["template_key"] for e in SYSTEM_REPLY_SEEDS}
GENERIC_KEYS = ALL_KEYS - PAYMENT_SEED_KEYS
CFG_COMPANION = {"domain": "conversion", "business_domain": "companion"}
CFG_SALES = {"domain": "conversion", "business_domain": "sales"}
CFG_PAYMENT = {"domain": "payment", "domain_plugins": {"payment": {"enabled": True}}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    bdm.reset_active_business_domain()
    yield
    bdm.reset_active_business_domain()


@pytest.fixture
def store(tmp_path):
    return KnowledgeBaseStore(tmp_path / "kb.db")


def _rows(store):
    with store._conn() as c:
        return {r["template_key"]: dict(r) for r in c.execute(
            "SELECT template_key, enabled, source FROM kb_entries WHERE template_key != ''")}


def test_payment_seed_keys_cover_every_gxp_seed_and_nothing_generic():
    gxp = {k for k in ALL_KEYS if k.startswith("gxp_")}
    assert len(gxp) == 14 and gxp <= PAYMENT_SEED_KEYS     # 报告说 13，实数 14（含 processing_fallback）
    assert PAYMENT_SEED_KEYS <= ALL_KEYS
    assert GENERIC_KEYS == {"global_fallback", "greeting_fallback", "complaint_fallback",
                            "small_talk_fallback", "test_reply"}


def test_plan_by_domain_and_without_config(monkeypatch):
    assert system_seed_plan(CFG_COMPANION) == {"business_domain": "companion", "payment": False,
                                               "enabled_default": 0}
    assert system_seed_plan(CFG_SALES) == {"business_domain": "sales", "payment": False,
                                           "enabled_default": 1}
    assert system_seed_plan(CFG_PAYMENT)["payment"] is True
    # 没配置（admin.py 那路）：按非支付；业务域读进程级 active / env
    assert system_seed_plan(None) == {"business_domain": "sales", "payment": False,
                                      "enabled_default": 1}
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    assert system_seed_plan(None)["business_domain"] == "companion"
    bdm.set_active_business_domain("sales")
    assert system_seed_plan(None)["business_domain"] == "sales"
    assert system_seed_plan("junk")["payment"] is False    # 绝不抛


def test_companion_fresh_install_seeds_zero_gxp_and_generic_disabled(store):
    r = seed_system_replies(store, CFG_COMPANION)
    assert r["business_domain"] == "companion"
    assert r["added"] == len(GENERIC_KEYS) and r["skipped_domain"] == len(PAYMENT_SEED_KEYS)
    rows = _rows(store)
    assert set(rows) == GENERIC_KEYS
    assert all(v["enabled"] == 0 and v["source"] == "system" for v in rows.values())
    assert not any(k.startswith("gxp_") for k in rows)
    assert store.stats()["entries_system_payment"] == 0
    # 停用态 → 兜底拿不到 → 调用方「本轮不回复」，不再冒客服腔
    assert store.get_fallback("greeting") is None
    # 幂等：再播不加
    r2 = seed_system_replies(store, CFG_COMPANION)
    assert r2["added"] == 0 and r2["skipped"] == len(GENERIC_KEYS)


def test_sales_server_keeps_generic_enabled_but_no_payment_seeds(store):
    r = seed_system_replies(store, CFG_SALES)
    rows = _rows(store)
    assert set(rows) == GENERIC_KEYS and r["skipped_domain"] == len(PAYMENT_SEED_KEYS)
    assert all(v["enabled"] == 1 for v in rows.values())
    assert store.get_fallback("greeting")


def test_payment_domain_seeds_everything_enabled(store):
    r = seed_system_replies(store, CFG_PAYMENT)
    rows = _rows(store)
    assert set(rows) == ALL_KEYS and r["skipped_domain"] == 0
    assert all(v["enabled"] == 1 for v in rows.values())
    assert store.get_direct_reply("gxp_hint_utr_query")
    assert store.stats()["entries_system_payment"] == len(PAYMENT_SEED_KEYS)


def test_no_config_call_never_seeds_payment(store):
    r = seed_system_replies(store)            # admin.py 那路
    assert set(_rows(store)) == GENERIC_KEYS and r["skipped_domain"] == len(PAYMENT_SEED_KEYS)


def test_purge_system_suppresses_reseed(store):
    seed_system_replies(store, CFG_SALES)
    assert store.purge_by_source("system") == len(GENERIC_KEYS)
    assert store.get_meta(KB_SYSTEM_SEEDS_PURGED_KEY)
    r = seed_system_replies(store, CFG_PAYMENT)
    assert r["added"] == 0 and r.get("suppressed_purged") is True
    assert _rows(store) == {}


def test_purge_payment_seeds_only_removes_payment_series_and_blocks_reseed(store):
    # 模拟 1.0.76 前的存量库：23 条全在、全启用（不带域过滤）
    for e in SYSTEM_REPLY_SEEDS:
        store.add_entry(dict(e))
    store.add_entry({"title": "我的知识", "category": "常规咨询", "triggers": ["x"],
                     "example_reply_zh": "y", "source": "user"})
    assert store.stats()["entries_system_payment"] == len(PAYMENT_SEED_KEYS)
    n = purge_payment_seeds(store)
    assert n == len(PAYMENT_SEED_KEYS)
    rows = _rows(store)
    assert set(rows) == GENERIC_KEYS                      # 通用兜底与用户条目不动
    assert store.stats()["entries_user"] == 1
    assert store.stats()["entries_system_payment"] == 0
    assert store.get_meta(KB_PAYMENT_SEEDS_PURGED_KEY)
    # 即便后来切到支付域，也不再灌回（用户显式清过）
    r = seed_system_replies(store, CFG_PAYMENT)
    assert r["added"] == 0 and r["skipped_domain"] == len(PAYMENT_SEED_KEYS)
    assert purge_payment_seeds(store) == 0                # 幂等


def test_registry_passes_config_and_routes_page_i18n_wired():
    reg = (REPO / "src" / "utils" / "kb_registry.py").read_text(encoding="utf-8")
    assert "seed_system_replies(kb, config)" in reg
    routes = (REPO / "src" / "web" / "routes" / "kb_routes.py").read_text(encoding="utf-8")
    assert '"/api/kb/entries/purge-payment-seeds"' in routes
    assert "purge_payment_seeds(_kb_store)" in routes
    assert "entries_system_payment" in routes            # 升级提示 WARNING
    html = (REPO / "src" / "web" / "templates" / "knowledge.html").read_text(encoding="utf-8")
    for needle in ('id="kb-syspay-notice"', "stats.entries_system_payment",
                   "/api/kb/entries/purge-payment-seeds", "_syncSysPayNotice(s)",
                   "kbPurgePaymentSeeds"):
        assert needle in html, needle
    from src.web.i18n_packs.kb_page import EN, ZH
    for key in ("kb2_syspay_lead", "kb2_syspay_lead2", "kb2_syspay_purge", "kb2_syspay_none",
                "kb2_syspay_purge_confirm", "kb2_syspay_purged"):
        assert key in ZH and key in EN, key


def test_assistant_help_corpus_is_not_customer_kb():
    """698J28 第三次把「帮助语料自动播种 291 条」当 KB 预置：那是悬浮球助手语料
    （assistant.seed），写的不是 kb_entries。钉住模块归属，回访口径交 N-5。"""
    seed_src = (REPO / "src" / "assistant" / "seed_corpus.py")
    assert seed_src.exists()
    txt = seed_src.read_text(encoding="utf-8")
    assert "帮助语料自动播种" in txt
    assert "kb_entries" not in txt and "KnowledgeBaseStore" not in txt
