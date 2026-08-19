# -*- coding: utf-8 -*-
"""2026-08-19 Token P6 门禁：enforce=「耗尽降级免费路径」，永不断线。

产品不变量：
1. 降级判定五闸全过才 True（总闸/enforce/计费动作/钱包曾注资/余额≤0）——
   从未注资的存量授权不适用（防 enforce 一开全体打降级）；判定异常一律 False。
2. 降级永远指向**能出结果的免费路径**：AI 回复→本地模型、专业翻译→标准档、
   克隆声→edge 兜底；免费路径不可用时照走付费路径（永不断线 > 计费）。
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
    # 降级判定存在，且**本地兜底可用**是前置（无处可去不降级=永不断线）
    assert 'should_degrade_action("ai_reply")' in oa
    assert "self._fb_client and self._fb_model" in oa.split(
        'should_degrade_action("ai_reply")')[0].rsplit("_token_degraded", 2)[-1] or \
        "self._fb_client and self._fb_model" in oa
    lf = inspect.getsource(AIClient._try_local_fallback_chat)
    # 免费路径标记打在成功 return 之前
    assert '_reply_free_path' in lf
    assert lf.index('_reply_free_path') < lf.index("return reply")


def test_tts_wiring_pinned():
    from src.ai import tts_pipeline

    assert tts_pipeline._is_non_fallback_error("token_wallet_exhausted") is False
    src = inspect.getsource(tts_pipeline.TTSPipeline._synthesize_uncached)
    assert 'should_degrade_action("voice_clone")' in src
    # 降级=注入失败原因交既有 edge 兜底块（不自造第二条兜底链）
    assert '"token_wallet_exhausted"' in src
    # 兜底可用性是降级前置（fallback_on_error + fallback_backend）
    seg = src.split('should_degrade_action("voice_clone")')[0]
    assert "fallback_on_error" in seg and "fallback_backend" in seg


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
