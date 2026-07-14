# -*- coding: utf-8 -*-
"""P4 `src/ai/chat_intent.py` 单元测试 — Messenger 聊天意图分类器。

覆盖:
  * INTENTS 公开契约不变
  * _is_cold: 边界 (空/表情/单字/贫瘠 token/混合/长文)
  * _rule_classify: opening / cold / referral_ask / buying / closing 的多语言命中
  * classify_intent: rule 路径, LLM fallback 路径 (mock), 保底路径
  * should_trigger_referral / format_intent_for_llm_hint

LLM 调用全用 mock,不打真网;rule 路径真跑正则。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ─── 契约 ────────────────────────────────────────────────────────────────────

class TestContractIntents:
    def test_intents_tuple_is_exact_set(self):
        from src.ai.chat_intent import INTENTS
        assert INTENTS == (
            "opening", "smalltalk", "interest", "objection",
            "buying", "referral_ask", "closing", "cold",
        )

    def test_chat_intent_result_dataclass(self):
        from src.ai.chat_intent import ChatIntentResult
        r = ChatIntentResult(intent="buying", confidence=0.9, source="rule")
        assert r.intent == "buying"
        assert r.confidence == 0.9
        assert r.source == "rule"
        assert r.reason == ""


# ─── _is_cold ────────────────────────────────────────────────────────────────

class TestIsCold:
    def test_empty_is_cold(self):
        from src.ai.chat_intent import _is_cold
        assert _is_cold("") is True
        assert _is_cold("   ") is True
        assert _is_cold(None) is True

    def test_single_char_cold_token(self):
        from src.ai.chat_intent import _is_cold
        assert _is_cold("嗯") is True
        assert _is_cold("ok") is True
        assert _is_cold("Hmm") is True  # 大小写无关

    def test_emoji_only_cold(self):
        from src.ai.chat_intent import _is_cold
        assert _is_cold("😀😊") is True
        assert _is_cold("👍") is True

    def test_pure_punctuation_short_cold(self):
        from src.ai.chat_intent import _is_cold
        assert _is_cold("...") is True
        assert _is_cold("。。") is True

    def test_short_normal_text_not_cold(self):
        from src.ai.chat_intent import _is_cold
        # 超过 3 字符的实质内容不算冷
        assert _is_cold("你好啊朋友") is False
        assert _is_cold("nice to meet") is False

    def test_all_cold_tokens_combined(self):
        from src.ai.chat_intent import _is_cold
        # 多个贫瘠 token 拼接也算冷
        assert _is_cold("ok ok") is True
        assert _is_cold("嗯嗯") is True
        assert _is_cold("yes no") is True

    def test_emoji_plus_substantial_text_not_cold(self):
        from src.ai.chat_intent import _is_cold
        assert _is_cold("😀 How are you doing today") is False

    def test_long_text_not_cold(self):
        from src.ai.chat_intent import _is_cold
        assert _is_cold("今天天气真好,我们出去玩吧") is False


# ─── _rule_classify ─────────────────────────────────────────────────────────

class TestRuleClassify:
    def test_empty_history_returns_opening(self):
        from src.ai.chat_intent import _rule_classify
        r = _rule_classify("你好", history=[])
        assert r is not None
        assert r.intent == "opening"
        assert r.source == "rule"
        assert r.confidence >= 0.9

    def test_history_without_incoming_returns_opening(self):
        """只有 outgoing greeting,peer 还没发言 → opening。"""
        from src.ai.chat_intent import _rule_classify
        hist = [{"direction": "outgoing", "message_text": "hi"}]
        r = _rule_classify("hello", history=hist)
        assert r is not None and r.intent == "opening"

    def test_cold_overrides_when_history_exists(self):
        from src.ai.chat_intent import _rule_classify
        hist = [{"direction": "incoming", "message_text": "yo"}]
        r = _rule_classify("ok", history=hist)
        assert r is not None and r.intent == "cold"

    def test_buying_multilingual(self):
        from src.ai.chat_intent import _rule_classify
        hist = [{"direction": "incoming", "message_text": "prior"}]
        for text in [
            "how much is it?",
            "多少钱",
            "价格是多少",
            "quanto costa?",
            "値段教えてください",
            "I'd like to purchase",
            "怎么下单",
        ]:
            r = _rule_classify(text, history=hist)
            assert r is not None, f"未命中 buying: {text!r}"
            assert r.intent == "buying", \
                f"{text!r} 应归 buying,实得 {r.intent}"

    def test_referral_ask_multilingual(self):
        from src.ai.chat_intent import _rule_classify
        hist = [{"direction": "incoming", "message_text": "prior"}]
        for text in [
            "Do you have WhatsApp?",
            "加你微信",
            "LINE でも連絡できますか?",
            "Qual è il tuo contatto?",
            "exchange contact",
            "加你的 LINE",
        ]:
            r = _rule_classify(text, history=hist)
            assert r is not None, f"未命中 referral_ask: {text!r}"
            assert r.intent == "referral_ask", \
                f"{text!r} 应归 referral_ask,实得 {r.intent}"

    def test_closing_multilingual(self):
        from src.ai.chat_intent import _rule_classify
        hist = [{"direction": "incoming", "message_text": "prior"}]
        for text in [
            "bye",
            "再见,明天聊",
            "またね",
            "ciao a presto",
            "Good night",
            "ok bye see you later",  # ok 会被 cold 吞掉 — 本测试确认先判 cold
        ]:
            r = _rule_classify(text, history=hist)
            assert r is not None, f"未命中: {text!r}"
            assert r.intent in ("closing", "cold"), \
                f"{text!r} 应归 closing 或 cold,实得 {r.intent}"

    def test_smalltalk_like_returns_none_for_llm(self):
        from src.ai.chat_intent import _rule_classify
        hist = [{"direction": "incoming", "message_text": "prior"}]
        # 这些是 LLM 才能分辨的模糊语义 — rule 不给答案
        for text in [
            "今天天气真好",
            "Your product looks interesting, can you tell me more features",
            "I'm not sure if it fits my team size",
        ]:
            r = _rule_classify(text, history=hist)
            assert r is None, f"{text!r} 应让 LLM 判,rule 不应命中 ({r})"

    def test_referral_beats_buying_when_both_present(self):
        """referral_ask 优先级 > buying (顺序第 3 > 第 4)。"""
        from src.ai.chat_intent import _rule_classify
        hist = [{"direction": "incoming", "message_text": "prior"}]
        text = "how much? can you share your WhatsApp"
        r = _rule_classify(text, history=hist)
        assert r.intent == "referral_ask"


# ─── classify_intent (主入口) ───────────────────────────────────────────────

class TestClassifyIntent:
    def test_rule_path_returns_rule_source(self):
        from src.ai.chat_intent import classify_intent
        r = classify_intent("WhatsApp?", history=[
            {"direction": "incoming", "message_text": "prior"}
        ])
        assert r.intent == "referral_ask"
        assert r.source == "rule"

    def test_empty_history_opening_no_llm_call(self):
        from src.ai.chat_intent import classify_intent
        # opening 是 rule 命中,应当不调 LLM
        with patch("src.ai.llm_client.LLMClient") as mock_llm:
            r = classify_intent("hello", history=[])
            mock_llm.assert_not_called()
        assert r.intent == "opening"

    def test_llm_fallback_path(self):
        from src.ai.chat_intent import classify_intent
        fake_client = MagicMock()
        fake_client.chat_with_system.return_value = (
            '{"intent": "interest", "confidence": 0.8,'
            ' "reason": "asks for details"}'
        )
        with patch("src.ai.llm_client.LLMClient",
                   return_value=fake_client):
            r = classify_intent(
                "can you tell me more about what you offer",
                history=[{"direction": "incoming", "message_text": "prior"}],
            )
        assert r.intent == "interest"
        assert r.source == "llm"
        assert 0.0 <= r.confidence <= 1.0

    def test_llm_returns_bogus_intent_falls_back(self):
        from src.ai.chat_intent import classify_intent
        fake_client = MagicMock()
        # LLM 回了不在 3 选 1 的 intent
        fake_client.chat_with_system.return_value = (
            '{"intent": "maybe", "confidence": 0.5}'
        )
        with patch("src.ai.llm_client.LLMClient",
                   return_value=fake_client):
            r = classify_intent(
                "I'm not sure",
                history=[{"direction": "incoming", "message_text": "x"}],
            )
        assert r.intent == "smalltalk"
        assert r.source == "fallback"

    def test_llm_parse_error_falls_back(self):
        from src.ai.chat_intent import classify_intent
        fake_client = MagicMock()
        fake_client.chat_with_system.return_value = "not json at all"
        with patch("src.ai.llm_client.LLMClient",
                   return_value=fake_client):
            r = classify_intent(
                "ambiguous text",
                history=[{"direction": "incoming", "message_text": "x"}],
            )
        assert r.source == "fallback"
        assert r.intent == "smalltalk"

    def test_use_llm_fallback_false_skips_llm(self):
        from src.ai.chat_intent import classify_intent
        with patch("src.ai.llm_client.LLMClient") as mock_llm:
            r = classify_intent(
                "some ambiguous thing",
                history=[{"direction": "incoming", "message_text": "x"}],
                use_llm_fallback=False,
            )
            mock_llm.assert_not_called()
        assert r.intent == "smalltalk"
        assert r.source == "fallback"

    def test_llm_import_error_graceful(self):
        from src.ai.chat_intent import classify_intent
        with patch("src.ai.llm_client.LLMClient",
                   side_effect=RuntimeError("llm client broken")):
            r = classify_intent(
                "ambiguous",
                history=[{"direction": "incoming", "message_text": "x"}],
            )
        assert r.source == "fallback"

    def test_wrapped_json_in_code_fence_parsed(self):
        """LLM 有时包 markdown ``` — 解析应容错。"""
        from src.ai.chat_intent import classify_intent
        fake_client = MagicMock()
        fake_client.chat_with_system.return_value = (
            '```json\n{"intent": "objection", "confidence": 0.75}\n```'
        )
        with patch("src.ai.llm_client.LLMClient",
                   return_value=fake_client):
            r = classify_intent(
                "too expensive",
                history=[{"direction": "incoming", "message_text": "x"}],
            )
        assert r.intent == "objection"

    def test_confidence_clamped(self):
        from src.ai.chat_intent import classify_intent
        fake_client = MagicMock()
        fake_client.chat_with_system.return_value = (
            '{"intent": "interest", "confidence": 1.5}'
        )
        with patch("src.ai.llm_client.LLMClient",
                   return_value=fake_client):
            r = classify_intent(
                "tell me more",
                history=[{"direction": "incoming", "message_text": "x"}],
            )
        assert 0.0 <= r.confidence <= 1.0

    def test_returns_result_even_on_total_failure(self):
        from src.ai.chat_intent import classify_intent, ChatIntentResult
        # 最 corner case: rule 返 None + LLM 也 None
        with patch("src.ai.chat_intent._rule_classify", return_value=None), \
             patch("src.ai.chat_intent._llm_classify", return_value=None):
            r = classify_intent("whatever", history=[])
        assert isinstance(r, ChatIntentResult)
        assert r.intent == "smalltalk"


# ─── 下游决策辅助 ────────────────────────────────────────────────────────────

class TestDownstreamHelpers:
    def test_should_trigger_referral(self):
        from src.ai.chat_intent import should_trigger_referral
        assert should_trigger_referral("buying") is True
        assert should_trigger_referral("referral_ask") is True
        for i in ("opening", "smalltalk", "interest", "objection",
                  "closing", "cold"):
            assert should_trigger_referral(i) is False

    def test_format_hint_smalltalk_is_empty(self):
        from src.ai.chat_intent import (
            format_intent_for_llm_hint, ChatIntentResult,
        )
        r = ChatIntentResult(intent="smalltalk", confidence=0.5, source="llm")
        assert format_intent_for_llm_hint(r) == ""

    def test_format_hint_populated_for_others(self):
        from src.ai.chat_intent import (
            format_intent_for_llm_hint, ChatIntentResult, INTENTS,
        )
        for i in INTENTS:
            if i == "smalltalk":
                continue
            r = ChatIntentResult(intent=i, confidence=0.5, source="rule")
            txt = format_intent_for_llm_hint(r)
            assert txt, f"{i} 应有 hint 文本"
            assert i in txt or "当前轮意图" in txt

    def test_format_hint_buying_mentions_referral(self):
        from src.ai.chat_intent import (
            format_intent_for_llm_hint, ChatIntentResult,
        )
        r = ChatIntentResult(intent="buying", confidence=0.9, source="rule")
        txt = format_intent_for_llm_hint(r)
        assert "引流" in txt or "LINE" in txt or "WhatsApp" in txt

    def test_format_hint_none_returns_empty(self):
        from src.ai.chat_intent import format_intent_for_llm_hint
        assert format_intent_for_llm_hint(None) == ""
