# -*- coding: utf-8 -*-
"""2026-08-19 Token P6 门禁：enforce=「耗尽降级免费路径」。

产品不变量：
1. 降级判定五闸全过才 True（总闸/enforce/计费动作/钱包曾注资/余额≤0）——
   从未注资的存量授权不适用（防 enforce 一开全体打降级）；判定异常一律 False。
2. 降级先走免费路径：AI 回复→本地模型、专业翻译→标准档、克隆声有兜底声→edge。
   免费档守卫开（缺省）且本地没有结果：对客回复本轮不自动回复，克隆没有
   免费兜底声则改发文字，不改打付费接口。显式 legacy 才保留付费回落。
   工具调用不在这句对客口径里。
3. 免费路径出话不计费：本地模型回复带 _reply_free_path 标记，计费钩 pop 消费
   （context 跨轮复用，残留标记会豁免下一条云端回复——取走即清是硬约束）。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from src.licensing.token_ledger import (
    TokenLedgerStore,
    configure_token_ledger,
    reset_token_ledger,
    shadow_snapshot,
    should_degrade_action,
    wallet_snapshot,
)


@pytest.fixture(autouse=True)
def _isolated_ledger():
    reset_token_ledger()
    store = TokenLedgerStore(":memory:")
    configure_token_ledger(enabled=True, enforce=True, store=store)
    yield store
    reset_token_ledger()


def _st(customer="tg:@boss", monthly=0):
    return SimpleNamespace(
        licensed=True, lic_id="L1", customer=customer, included_chars=0,
        included_tokens_monthly=monthly, enforce=False, state="active",
    )


# ── 1) 降级判定语义 ─────────────────────────────────────────────────────────

def test_degrade_requires_all_gates(_isolated_ledger):
    s = _isolated_ledger
    st = _st()
    # 从未注资 → 不降级（存量授权不适用 Token 语义）
    assert should_degrade_action("ai_reply", lic_status=st) is False
    # 注资且有余额 → 不降级
    s.grant_pack("tg:boss", 100, ref="o1")
    assert should_degrade_action("ai_reply", lic_status=st) is False
    # 耗尽 → 降级 + 影子计数
    s.record_spend("tg:boss", "ai_reply", 100)
    assert should_degrade_action("ai_reply", lic_status=st) is True
    assert shadow_snapshot().get("enforce_degrade_ai_reply", 0) >= 1
    # 免费动作永不降级（std_translate 费率 0）
    assert should_degrade_action("std_translate", lic_status=st) is False
    # 未知动作不降级
    assert should_degrade_action("nope", lic_status=st) is False


def test_degrade_off_when_enforce_or_ledger_off(_isolated_ledger):
    s = _isolated_ledger
    st = _st()
    s.grant_pack("tg:boss", 10, ref="o1")
    s.record_spend("tg:boss", "ai_reply", 10)
    configure_token_ledger(enforce=False)
    assert should_degrade_action("ai_reply", lic_status=st) is False
    configure_token_ledger(enabled=False, enforce=True)
    assert should_degrade_action("ai_reply", lic_status=st) is False


def test_degrade_monthly_grant_refills(_isolated_ledger):
    """订阅月含量档：耗尽后进入新月份，判定内 ensure_monthly 自动续杯 → 不再降级。"""
    s = _isolated_ledger
    st = _st(monthly=1_000)
    import time as _t
    now = _t.time()
    assert should_degrade_action("ai_reply", lic_status=st, now=now) is False  # 首查即入账
    s.record_spend("tg:boss", "ai_reply", 1_000, now=now)
    assert should_degrade_action("ai_reply", lic_status=st, now=now) is True
    next_month = now + 40 * 86400
    assert should_degrade_action("ai_reply", lic_status=st, now=next_month) is False


def test_snapshot_exposes_enforce(_isolated_ledger):
    assert wallet_snapshot(_st())["enforce"] is True
    configure_token_ledger(enforce=False)
    assert wallet_snapshot(_st())["enforce"] is False


# ── 2) 三条消费链的接线契约（源码钉：时序/标记/兜底语义不许回退）──────────────

def test_ai_client_wiring_pinned():
    from src.ai.ai_client import AIClient

    gen = inspect.getsource(AIClient.generate_reply)
    # 计费钩必须 pop 消费免费路径标记（读而不清 = 残留豁免下一条云端回复）
    assert 'context.pop("_reply_free_path"' in gen
    oa = inspect.getsource(AIClient._generate_reply_openai_compat)
    # 降级判定存在；有本地端点才先试本地。本地没有结果时守卫决定是否停住。
    assert 'should_degrade_action("ai_reply")' in oa
    assert "self._fb_client and self._fb_model" in oa
    assert "_paid_ai_blocked_after_local_miss" in oa
    assert "_stop_paid_ai" in oa
    lf = inspect.getsource(AIClient._try_local_fallback_chat)
    # 免费路径标记打在成功 return 之前
    assert '_reply_free_path' in lf
    assert lf.index('_reply_free_path') < lf.index("return reply")


def test_tts_wiring_pinned():
    from src.ai import tts_pipeline

    assert tts_pipeline._is_non_fallback_error("token_wallet_exhausted") is False
    src = inspect.getsource(tts_pipeline.TTSPipeline._synthesize_uncached)
    assert 'should_degrade_action("voice_clone")' in src
    # 有兜底声：注入失败原因交既有 edge 块。没有兜底声：守卫开则改发文字。
    assert '"token_wallet_exhausted"' in src
    assert "fallback_on_error" in src and "fallback_backend" in src
    assert "_token_block_unbacked_clone" in src
    assert 'rv.extra["degrade_to_text"] = True' in src


def test_block_paid_when_local_has_no_result():
    """守卫开 + 对客 + 余额用尽 → 拦住。legacy、工具调用、余额还在 → 不拦。"""
    from src.ai.ai_client import AIClient
    from src.ai.translation_engines import (
        block_paid_when_local_miss,
        free_tier_blocks_paid_fallback,
        set_free_tier_guard_process,
    )

    assert block_paid_when_local_miss(
        wallet_empty=True, guard=True, customer_facing=True) is True
    assert block_paid_when_local_miss(
        wallet_empty=True, guard=False, customer_facing=True) is False
    assert block_paid_when_local_miss(
        wallet_empty=False, guard=True, customer_facing=True) is False
    assert block_paid_when_local_miss(
        wallet_empty=True, guard=True, customer_facing=False) is False
    assert free_tier_blocks_paid_fallback(
        {"translation": {"free_tier_zero_cost": "legacy"}}) is False
    assert free_tier_blocks_paid_fallback(
        {"translation": {"free_tier_zero_cost": "enforce"}}) is True
    set_free_tier_guard_process(False)
    try:
        assert free_tier_blocks_paid_fallback(None) is False
    finally:
        set_free_tier_guard_process(None)
    assert free_tier_blocks_paid_fallback(None) is True

    client = SimpleNamespace(config=SimpleNamespace(config={
        "translation": {"free_tier_zero_cost": "enforce"}}))
    assert AIClient._paid_ai_blocked_after_local_miss(client, {}, True) is True
    assert AIClient._paid_ai_blocked_after_local_miss(
        client, {"_tool_call": True}, True) is False
    assert AIClient._paid_ai_blocked_after_local_miss(
        client, {"_bare_system": "translate"}, True) is False
    assert AIClient._paid_ai_blocked_after_local_miss(client, {}, False) is False
    client.config.config["translation"]["free_tier_zero_cost"] = "legacy"
    assert AIClient._paid_ai_blocked_after_local_miss(client, {}, True) is False


@pytest.mark.asyncio
async def test_unbacked_clone_degrades_to_text_instead_of_paid(tmp_path, monkeypatch):
    """没有免费兜底声时，余额用尽不调用付费克隆，改发文字。legacy 才去调克隆。"""
    from src.ai.tts_pipeline import TTSPipeline

    monkeypatch.setattr(
        "src.licensing.token_ledger.should_degrade_action", lambda *_a, **_k: True)

    async def _no_clone(*_a, **_k):
        raise AssertionError("paid clone must not run")

    pipe = TTSPipeline({
        "enabled": True,
        "backend": "avatar_clone",
        "fallback_on_error": False,
        "out_dir": str(tmp_path),
    })
    monkeypatch.setattr(pipe, "_should_try_lan", lambda: False)
    monkeypatch.setattr(pipe, "_clone_lang_blocked", lambda *_a, **_k: "")
    monkeypatch.setattr(pipe, "_try_avatar_clone", _no_clone)
    rv = await pipe._synthesize_uncached("你好，今天天气不错，我们晚点再聊。")
    assert rv.ok is False
    assert rv.extra.get("degrade_to_text") is True
    assert "token_wallet_exhausted" in str(rv.error or "")

    called = {"n": 0}

    async def _yes_clone(rv, *_a, **_k):
        called["n"] += 1
        rv.ok = True
        return rv

    legacy = TTSPipeline({
        "enabled": True,
        "backend": "avatar_clone",
        "fallback_on_error": False,
        "free_tier_zero_cost": "legacy",
        "out_dir": str(tmp_path),
    })
    monkeypatch.setattr(legacy, "_should_try_lan", lambda: False)
    monkeypatch.setattr(legacy, "_clone_lang_blocked", lambda *_a, **_k: "")
    monkeypatch.setattr(legacy, "_try_avatar_clone", _yes_clone)
    rv2 = await legacy._synthesize_uncached("你好，今天天气不错，我们晚点再聊。")
    assert called["n"] == 1 and rv2.ok is True


def test_translate_wiring_pinned():
    from src.ai.translation_service import TranslationService

    src = inspect.getsource(TranslationService.translate)
    # 降级检查必须在 certified→deepl 引擎偏好**之前**（降到 std 就不该再偏好 DeepL）
    degrade_pos = src.index('should_degrade_action("pro_translate")')
    pref_pos = src.index('tier == "certified" and not pref_engine')
    assert degrade_pos < pref_pos
    # 降级动作=改层级为 std（服务照做），绝无 return/阻断
    seg = src[degrade_pos:degrade_pos + 400]
    assert 'tier = "std"' in seg and "return" not in seg
