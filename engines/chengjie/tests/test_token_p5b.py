# -*- coding: utf-8 -*-
"""2026-08-19 Token P5b+P4 门禁：翻译层级计费 / 生成图计费 / 公平使用 / 投递影子计数。

钉住的产品不变量：
1. **计费跟层级不跟引擎**：std/缺省永远 0 Token（本地引擎宕机回落云端也不扣）；
   pro=10/千字符；certified 只有真由 DeepL 交付才 40，回落按 pro 价 10——
   绝不按未交付的价值收费。
2. **生成收费、存货免费**：record_image_sent 只对 llm_directive/keyword（现场生成）
   计 ai_image；registry/相册顶包(_album)/bazi_kline（免费引流决策）零计费。
3. 公平使用（P4 warn-only）：按钱包×日累计、超限只提醒绝不拦截、跨钱包隔离。
4. 影子计数（投递点 vs 出稿点校准用）：enabled 关=零写；快照进 wallet_snapshot。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.licensing.token_ledger import (
    FAIR_USE_TRANSLATE_CHARS_PER_DAY,
    TokenLedgerStore,
    configure_token_ledger,
    note_translate_fair_use,
    record_shadow,
    reset_token_ledger,
    shadow_snapshot,
    wallet_snapshot,
)


@pytest.fixture(autouse=True)
def _isolated_ledger():
    reset_token_ledger()
    store = TokenLedgerStore(":memory:")
    configure_token_ledger(enabled=True, store=store)
    yield store
    reset_token_ledger()


def _st(customer="tg:@boss", lic_id="chatx-personal-X", monthly=0):
    return SimpleNamespace(
        licensed=True, lic_id=lic_id, customer=customer, included_chars=0,
        included_tokens_monthly=monthly, enforce=False, state="active",
    )


# ── 1) 翻译层级计费（经 TranslationService._record_license_quota 的真实实现）────

def _svc_record(src: str, tier: str, provider: str):
    """_record_license_quota 不读 self 状态 → 直接以哑 self 调真实现（零装配成本）。"""
    from src.ai.translation_service import TranslationService

    TranslationService._record_license_quota(
        SimpleNamespace(), src, tier=tier, provider=provider)


def test_translate_std_never_bills(_isolated_ledger, monkeypatch):
    st = _st()
    monkeypatch.setattr(
        "src.licensing.license_manager.get_license_manager",
        lambda: SimpleNamespace(status=lambda **k: st))
    _svc_record("x" * 5000, "", "deepl")        # 缺省层级 + 哪怕引擎是 deepl
    _svc_record("x" * 5000, "std", "ai")
    snap = wallet_snapshot(st)
    assert snap["by_action"] == {}               # 零计费
    assert snap["fair_use"]["today"] == 10_000   # 只走公平使用水表


def test_translate_pro_and_certified_billing(_isolated_ledger, monkeypatch):
    st = _st()
    monkeypatch.setattr(
        "src.licensing.license_manager.get_license_manager",
        lambda: SimpleNamespace(status=lambda **k: st))
    _svc_record("x" * 1500, "pro", "ai")             # ceil(1.5k)=2 档 × 10 = 20
    _svc_record("x" * 1000, "certified", "deepl")    # DeepL 真交付 → 40
    _svc_record("x" * 1000, "certified", "ollama_mt")  # 认证回落 → 按 pro 价 10
    snap = wallet_snapshot(st)
    assert snap["by_action"] == {"pro_translate": 30, "deepl_translate": 40}
    assert snap["fair_use"]["today"] == 0        # 计费翻译不占免费水表


def test_translate_tier_engine_preference():
    """certified 未显式指定引擎 → 缓存键前置换 deepl 桶（读源码钉住时序，防回退）。"""
    import inspect

    from src.ai.translation_service import TranslationService

    src = inspect.getsource(TranslationService.translate)
    pref_pos = src.index('tier == "certified" and not pref_engine')
    key_pos = src.index("self._cache_key(")
    assert pref_pos < key_pos, "certified 引擎偏好必须发生在缓存键计算之前（独立缓存桶）"


# ── 2) 生成图计费、存货免费 ─────────────────────────────────────────────────

def test_image_billing_generation_only(_isolated_ledger, monkeypatch):
    st = _st()
    monkeypatch.setattr(
        "src.licensing.license_manager.get_license_manager",
        lambda: SimpleNamespace(status=lambda **k: st))
    from src.inbox.image_autosend import record_image_sent

    record_image_sent("selfie", source="llm_directive")   # 现场生成 → 50
    record_image_sent("selfie", source="keyword")          # 现场生成 → 50
    record_image_sent("selfie", source="llm_directive_album")  # 相册顶包 → 免费
    record_image_sent("selfie", source="keyword_album")    # 相册顶包 → 免费
    record_image_sent("image", source="registry")          # 注册相册 → 免费
    record_image_sent("bazi_kline")                        # K线（免费引流决策）
    snap = wallet_snapshot(st)
    assert snap["by_action"] == {"ai_image": 100}


# ── 3) 公平使用（P4 warn-only）────────────────────────────────────────────────

def test_fair_use_accumulates_and_warns_not_blocks(_isolated_ledger):
    st = _st()
    r1 = note_translate_fair_use(1_500_000, lic_status=st)
    assert r1["today"] == 1_500_000 and r1["exceeded"] is False
    r2 = note_translate_fair_use(600_000, lic_status=st)
    assert r2["today"] == 2_100_000 and r2["exceeded"] is True
    assert r2["limit"] == FAIR_USE_TRANSLATE_CHARS_PER_DAY
    # 超限只进影子计数，绝无 allowed=False 语义（warn-only 契约）
    assert shadow_snapshot().get("fair_use_exceeded", 0) >= 1
    snap = wallet_snapshot(st)
    assert snap["fair_use"]["exceeded"] is True


def test_fair_use_isolated_per_wallet(_isolated_ledger):
    a = note_translate_fair_use(100, lic_status=_st(customer="a@b.c"))
    b = note_translate_fair_use(7, lic_status=_st(customer="x@y.z"))
    assert a["today"] == 100 and b["today"] == 7


def test_fair_use_limit_configurable(_isolated_ledger):
    configure_token_ledger(fair_use_translate_chars=1_000)
    r = note_translate_fair_use(1_001, lic_status=_st())
    assert r["limit"] == 1_000 and r["exceeded"] is True


# ── 4) 影子计数 ──────────────────────────────────────────────────────────────

def test_shadow_counters_and_disabled_noop(_isolated_ledger):
    record_shadow("ai_reply_delivered_auto")
    record_shadow("ai_reply_delivered_auto")
    record_shadow("ai_reply_delivered_human")
    assert shadow_snapshot() == {
        "ai_reply_delivered_auto": 2, "ai_reply_delivered_human": 1}
    snap = wallet_snapshot(_st())
    assert snap["shadow"]["ai_reply_delivered_auto"] == 2
    # 总闸关 → 影子零写、公平使用零写
    reset_token_ledger()
    configure_token_ledger(enabled=False, store=TokenLedgerStore(":memory:"))
    record_shadow("x")
    assert shadow_snapshot() == {}
    assert note_translate_fair_use(999, lic_status=_st())["today"] == 0


# ── 5) 观测三件套出口（metrics_snapshot / dump_prom）─────────────────────────────

def test_metrics_snapshot_light_shape(_isolated_ledger, monkeypatch):
    """metrics 面 = wallet_snapshot 轻量版：去 grants/rates 明细、保 enforce/shadow/fair_use。"""
    from src.licensing import token_ledger as tl

    st = _st()
    monkeypatch.setattr(
        "src.licensing.license_manager.get_license_manager",
        lambda: SimpleNamespace(get_status=lambda: st, status=lambda: st))
    wallet = tl.wallet_id_for_status(st)
    _isolated_ledger.grant_pack(wallet, tokens=500, ref="m1")
    _isolated_ledger.record_spend(wallet, "ai_reply", 30)
    record_shadow("ai_reply_delivered_auto")

    snap = tl.metrics_snapshot()
    assert snap["enabled"] is True
    assert snap["balance"] == 470 and snap["total_spend"] == 30
    assert snap["by_action"] == {"ai_reply": 30}
    assert snap["shadow"] == {"ai_reply_delivered_auto": 1}
    assert "grants" not in snap and "rates" not in snap
    assert "enforce" in snap and "fair_use" in snap


def test_dump_prom_enabled_and_disabled(_isolated_ledger, monkeypatch):
    from src.licensing import token_ledger as tl

    st = _st()
    monkeypatch.setattr(
        "src.licensing.license_manager.get_license_manager",
        lambda: SimpleNamespace(get_status=lambda: st, status=lambda: st))
    wallet = tl.wallet_id_for_status(st)
    _isolated_ledger.grant_pack(wallet, tokens=200, ref="m2")
    _isolated_ledger.record_spend(wallet, "pro_translate", 10)

    text = tl.dump_prom()
    assert "token_wallet_balance 190" in text
    assert 'token_spend_by_action_total{action="pro_translate"} 10' in text
    assert "token_fair_use_today_chars" in text

    # 总闸关 → 零输出（零流量零噪声，对齐 line_media/credpool 口径）
    configure_token_ledger(enabled=False)
    assert tl.dump_prom() == ""
