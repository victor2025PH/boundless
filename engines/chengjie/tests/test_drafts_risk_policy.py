# -*- coding: utf-8 -*-
"""#160 风险扣稿全放行（影子台账档）+ 分类器整词化 —— 金标门禁（2026-09-04）。

事故：客户一句完全正面的 "Yes you did because I was happy chatting with you
i didn't want to stop" 被 ``"stop" in t`` 子串命中 → 停止联系 → high → L4 扣稿，
干净的 AI 稿在全自动档下静默不发，用户只看到「全自动不工作」。

两层，**刻意不混在一个断言里**：
  第一层：分类器判定（``_detect_intent`` / ``_detect_risk`` / ``_SENSITIVE_PATTERNS``）
          ——全放行之后分类器的正确性反而更重要：台账数据要拿来定新规则、
          stop_contact 要即时推值守群（误报＝告警疲劳）、风险要转守卫提示喂生成侧。
  第二层：shadow 档的放行行为（``autosend_policy.decide``）——所有输入都放行 L2、
          判中的产出**恰好一条**台账、判为无风险的**零台账**、review 档仍挂起且不进台账。
"""
from __future__ import annotations

import pytest

from src.ai.chat_assistant_service import (
    _detect_intent,
    _detect_risk,
    _detect_risk_detailed,
    _stop_contact_hit,
    quick_analyze,
    quick_risk,
)
from src.inbox.drafts import keyword_risk_hits, keyword_risk_level


# ═══════════════════════════════════════════════════════════════════════
# 第一层：分类器判定
# ═══════════════════════════════════════════════════════════════════════

# 必须判为**无风险**（旧子串匹配的误伤样本 + 事故原句）
_NO_RISK = [
    "Yes you did because I was happy chatting with you i didn't want to stop",
    "i didn't want to stop",
    "can't stop thinking about you",
    "we talked non-stop last night",
    "you look sexy tonight",
    "monday is a bank holiday here",
    "what's your email address",
    "let's have hotpot this weekend",
    "你骗我啦",
    "haha don't stop, keep telling me",
    "I couldn't stop laughing at that",
]


@pytest.mark.parametrize("text", _NO_RISK)
def test_no_risk_samples_are_low(text):
    level, reasons = quick_risk(text)
    assert level == "low", (text, level, reasons)
    assert reasons == [], (text, reasons)
    assert keyword_risk_level(text) is None, (text, keyword_risk_level(text))
    assert _detect_intent(text.lower(), emotion="平稳") != "停止联系", text
    a = quick_analyze(text)
    assert a["risk_hits"] == [], (text, a["risk_hits"])


# 必须**判中**：(文本, 期望 reason, 期望命中词片段)
_MUST_HIT = [
    ("please stop messaging me", "stop_contact", "stop messaging me"),
    ("别再联系我", "stop_contact", "别再联系"),
    ("I want to kill myself", "self_harm", "kill myself"),
    ("send me your bank card number", "credential_or_payment_request", "bank card"),
]


@pytest.mark.parametrize("text,reason,hit_fragment", _MUST_HIT)
def test_must_hit_samples(text, reason, hit_fragment):
    level, reasons, hits = _detect_risk_detailed(
        text, emotion="平稳", intent=_detect_intent(text.lower(), emotion="平稳"))
    assert level == "high", (text, level)
    assert reason in reasons, (text, reasons)
    assert any(hit_fragment in h for h in hits), (text, hits)
    # reasons 与 hits 同序对齐：一条 reason 一个命中词
    assert len(hits) == len(reasons), (reasons, hits)
    # quick_analyze 透出同一批命中词（台账消费口）
    a = quick_analyze(text)
    assert reason in a["risk_reasons"]
    assert any(hit_fragment in h for h in a["risk_hits"]), a["risk_hits"]


def test_ai_reply_payment_phrase_is_high_with_hits():
    """AI 稿含付款/账号话术 → reply_risk=high，并报出命中词。"""
    lvl, hits = keyword_risk_hits("Just transfer to this account and I'll ship it")
    assert lvl == "high"
    assert any("transfer to" in h for h in hits), hits
    lvl2, hits2 = keyword_risk_hits("send the deposit to account 1234 first")
    assert lvl2 == "high"
    assert hits2, hits2
    assert keyword_risk_level("send the deposit to account 1234 first") == "high"


def test_english_terms_hit_when_glued_to_cjk():
    """Python `\\b` 把 CJK 当 \\w：中英混排「请问可以refund吗」必须仍判中（ASCII 边界）。"""
    assert keyword_risk_level("请问可以refund吗") == "high"
    assert quick_risk("他叫我transfer money给他")[0] == "high"
    # 边界仍防住英文粘连：refunds 之类的派生词不被 refund 子串吞掉
    assert keyword_risk_level("xrefundx") is None


def test_keyword_risk_hits_collects_all_and_takes_max():
    lvl, hits = keyword_risk_hits("有优惠吗？付款方式是什么")
    assert lvl == "high"          # 优惠=medium、付款=high → 取最高
    assert "优惠" in hits and "付款" in hits


def test_stop_contact_negation_scrubbed_before_positive_match():
    assert _stop_contact_hit("i didn't want to stop") == ""
    assert _stop_contact_hit("can't stop thinking about you") == ""
    assert _stop_contact_hit("please stop messaging me") == "stop messaging me" or \
        _stop_contact_hit("please stop messaging me").startswith("please stop")
    # 否定式与真正的拒绝同句：仍要抓到拒绝
    assert _stop_contact_hit("I can't stop crying. please stop contacting me") != ""


def test_stop_contact_polite_and_negative_forms():
    for s in ("please stop", "don't contact me again", "unsubscribe",
              "leave me alone", "stop texting me", "do not message me"):
        assert _detect_intent(s, emotion="平稳") == "停止联系", s
    for s in ("stop it you're making me blush", "the bus stop is far",
              "non-stop", "stop by my place sometime"):
        assert _detect_intent(s, emotion="平稳") != "停止联系", s


def test_legacy_two_tuple_detect_risk_kept():
    level, reasons = _detect_risk("I want to kill myself", emotion="平稳", intent="继续聊天")
    assert level == "high" and "self_harm" in reasons


def test_whole_word_boundaries_for_risk_terms():
    # sexy ≠ sex；hotpot 不含任何词；bank holiday ≠ bank card
    assert quick_risk("you look sexy tonight")[0] == "low"
    assert quick_risk("we sent nudes")[0] == "high"
    assert quick_risk("monday is a bank holiday")[0] == "low"
    assert quick_risk("send it to my bank account")[0] == "high"
    # 中文精确短语：「不想活动」不是「不想活」
    assert quick_risk("今天不想活动了，太累")[0] == "low"
    assert quick_risk("我不想活了")[0] == "high"
