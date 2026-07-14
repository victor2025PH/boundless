"""
Tests for AI modules: LLMClient, MessageRewriter, AutoReply, VisionFallback.

These tests run WITHOUT a real LLM API key — they test internal logic,
caching, intent classification, offline rewriting, and budget management.
"""

import os
import sys
import time
import tempfile
import threading
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.ai.llm_client import LLMClient, LLMConfig, UsageStats
from src.ai.message_rewriter import MessageRewriter, RewriterConfig
from src.ai.auto_reply import (
    AutoReply, classify_intent, Intent, Persona,
    ConversationHistory, ReplyResult,
)
from src.ai.vision_fallback import VisionFallback, VisionConfig, VisionResult


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class TestLLMConfig:
    def test_default_config(self):
        cfg = LLMConfig()
        assert cfg.provider == "deepseek"
        assert "deepseek" in cfg.base_url
        assert cfg.model == "deepseek-chat"
        assert cfg.cache_enabled is True

    def test_openai_config(self):
        cfg = LLMConfig(provider="openai")
        assert "openai" in cfg.base_url
        assert cfg.model == "gpt-4o-mini"

    def test_local_config(self):
        cfg = LLMConfig(provider="local")
        assert "localhost" in cfg.base_url

    def test_custom_override(self):
        cfg = LLMConfig(provider="deepseek", model="custom-model", base_url="http://my-api/v1")
        assert cfg.model == "custom-model"
        assert cfg.base_url == "http://my-api/v1"


class TestUsageStats:
    def test_record(self):
        stats = UsageStats()
        stats.record(100, 50)
        assert stats.total_calls == 1
        assert stats.total_input_tokens == 100
        assert stats.total_output_tokens == 50
        assert stats.cached_calls == 0

    def test_cached_record(self):
        stats = UsageStats()
        stats.record(0, 0, cached=True)
        assert stats.total_calls == 1
        assert stats.cached_calls == 1
        assert stats.total_input_tokens == 0

    def test_error_count(self):
        stats = UsageStats()
        stats.record_error()
        stats.record_error()
        assert stats.errors == 2

    def test_snapshot(self):
        stats = UsageStats()
        stats.record(100, 50)
        stats.record(0, 0, cached=True)
        snap = stats.snapshot()
        assert snap["total_calls"] == 2
        assert snap["cached_calls"] == 1
        assert "50.0%" in snap["cache_hit_rate"]

    def test_thread_safety(self):
        stats = UsageStats()
        threads = []
        for _ in range(10):
            t = threading.Thread(target=lambda: [stats.record(10, 5) for _ in range(100)])
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        assert stats.total_calls == 1000


class TestLLMClientCache:
    def test_cache_init(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LLMConfig(cache_db_path=os.path.join(td, "test_cache.db"))
            client = LLMClient(cfg)
            client._init_cache()
            assert os.path.exists(cfg.cache_db_path)
            client.close()

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LLMConfig(cache_db_path=os.path.join(td, "test_cache.db"))
            client = LLMClient(cfg)
            key = "test_key_123"
            client._set_cache(key, "hello world")
            result = client._get_cache(key)
            assert result == "hello world"
            client.close()

    def test_cache_miss(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LLMConfig(cache_db_path=os.path.join(td, "test_cache.db"))
            client = LLMClient(cfg)
            result = client._get_cache("nonexistent_key")
            assert result is None
            client.close()

    def test_cache_key_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = LLMConfig(cache_db_path=os.path.join(td, "test_cache.db"))
            client = LLMClient(cfg)
            messages = [{"role": "user", "content": "hello"}]
            k1 = client._cache_key(messages, 0.7)
            k2 = client._cache_key(messages, 0.7)
            k3 = client._cache_key(messages, 0.9)
            assert k1 == k2
            assert k1 != k3
            client.close()


# ---------------------------------------------------------------------------
# MessageRewriter
# ---------------------------------------------------------------------------

class TestMessageRewriter:
    def test_offline_rewrite(self):
        cfg = RewriterConfig(offline_mode=True)
        rw = MessageRewriter(config=cfg)
        original = "Hi there, I'd love to connect with you about interesting work."
        result = rw.rewrite(original, platform="linkedin")
        assert isinstance(result, str)
        assert len(result) > 10

    def test_fill_template(self):
        result = MessageRewriter._fill_template(
            "Hi {name}, I noticed your work at {company}.",
            {"name": "Alice", "company": "Google"},
        )
        assert "Alice" in result
        assert "Google" in result

    def test_fill_template_no_context(self):
        template = "Hello {name}!"
        result = MessageRewriter._fill_template(template, None)
        assert result == template

    def test_parse_numbered_list(self):
        text = """1. Hello there, how are you?
2. Hey! What's going on?
3. Hi, hope you're doing well!"""
        variants = MessageRewriter._parse_numbered_list(text, 3)
        assert len(variants) == 3

    def test_parse_numbered_list_with_parens(self):
        text = """1) First variant here
2) Second variant here
3) Third variant"""
        variants = MessageRewriter._parse_numbered_list(text, 3)
        assert len(variants) == 3

    def test_offline_batch(self):
        cfg = RewriterConfig(offline_mode=True)
        rw = MessageRewriter(config=cfg)
        results = rw.rewrite_batch(
            "Hi {name}, let's connect!",
            [{"name": "Alice"}, {"name": "Bob"}, {"name": "Charlie"}],
            platform="linkedin",
        )
        assert len(results) == 3
        for r in results:
            assert isinstance(r, str)

    def test_pregenerate_offline(self):
        cfg = RewriterConfig(offline_mode=True)
        rw = MessageRewriter(config=cfg)
        count = rw.pregenerate("Hello, I'd love to connect!", count=5, platform="linkedin")
        assert count == 5
        status = rw.pool_status()
        assert sum(status.values()) == 5

    def test_pool_consumption(self):
        cfg = RewriterConfig(offline_mode=True)
        rw = MessageRewriter(config=cfg)
        rw.pregenerate("Hello!", count=3, platform="telegram")
        initial_pool = sum(rw.pool_status().values())
        assert initial_pool == 3
        rw.rewrite("Hello!", platform="telegram")
        after_pool = sum(rw.pool_status().values())
        assert after_pool == initial_pool - 1

    def test_pool_key_platform_isolation(self):
        cfg = RewriterConfig(offline_mode=True)
        rw = MessageRewriter(config=cfg)
        rw.pregenerate("Hello!", count=2, platform="telegram")
        rw.pregenerate("Hello!", count=3, platform="linkedin")
        status = rw.pool_status()
        assert sum(status.values()) == 5


# ---------------------------------------------------------------------------
# AutoReply — Intent Classification
# ---------------------------------------------------------------------------

class TestIntentClassification:
    def test_question_mark(self):
        assert classify_intent("How are you?") == Intent.NEEDS_REPLY
        assert classify_intent("你好吗？") == Intent.NEEDS_REPLY

    def test_question_words_en(self):
        assert classify_intent("What time is the meeting") == Intent.NEEDS_REPLY
        assert classify_intent("Can you help me with this") == Intent.NEEDS_REPLY
        assert classify_intent("How do I fix this") == Intent.NEEDS_REPLY

    def test_question_words_cn(self):
        assert classify_intent("你能不能帮我看一下") == Intent.NEEDS_REPLY
        assert classify_intent("请问这个怎么用") == Intent.NEEDS_REPLY
        assert classify_intent("有没有更好的方案") == Intent.NEEDS_REPLY

    def test_greetings(self):
        assert classify_intent("hello") == Intent.NEEDS_REPLY
        assert classify_intent("Hey there") == Intent.NEEDS_REPLY
        assert classify_intent("你好") == Intent.NEEDS_REPLY
        assert classify_intent("在吗") == Intent.NEEDS_REPLY

    def test_requests(self):
        assert classify_intent("Please send me the file") == Intent.NEEDS_REPLY
        assert classify_intent("帮我发一下那个文件") == Intent.NEEDS_REPLY
        assert classify_intent("Tell me about the project") == Intent.NEEDS_REPLY

    def test_no_reply_system(self):
        assert classify_intent("/start") == Intent.NO_REPLY
        assert classify_intent("Alice joined the group") == Intent.NO_REPLY
        assert classify_intent("Bob left the group") == Intent.NO_REPLY
        assert classify_intent("Admin pinned a message") == Intent.NO_REPLY

    def test_no_reply_empty(self):
        assert classify_intent("") == Intent.NO_REPLY
        assert classify_intent("a") == Intent.NO_REPLY

    def test_optional_short(self):
        assert classify_intent("nice") == Intent.OPTIONAL
        assert classify_intent("ok") == Intent.OPTIONAL

    def test_optional_ambiguous(self):
        result = classify_intent("That's a great idea, thanks for sharing")
        assert result == Intent.OPTIONAL


# ---------------------------------------------------------------------------
# AutoReply — ConversationHistory
# ---------------------------------------------------------------------------

class TestConversationHistory:
    def test_add_and_retrieve(self):
        h = ConversationHistory(max_messages=5)
        h.add("user", "Hello")
        h.add("assistant", "Hi there!")
        msgs = h.to_messages()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["content"] == "Hi there!"

    def test_max_messages(self):
        h = ConversationHistory(max_messages=3)
        for i in range(10):
            h.add("user", f"Message {i}")
        msgs = h.to_messages()
        assert len(msgs) == 3
        assert "Message 7" in msgs[0]["content"]

    def test_clear(self):
        h = ConversationHistory()
        h.add("user", "test")
        h.clear()
        assert h.length == 0

    def test_to_messages_limit(self):
        h = ConversationHistory()
        for i in range(20):
            h.add("user", f"msg {i}")
        msgs = h.to_messages(limit=5)
        assert len(msgs) == 5


# ---------------------------------------------------------------------------
# AutoReply — Persona
# ---------------------------------------------------------------------------

class TestPersona:
    def test_system_prompt_generation(self):
        p = Persona(
            name="TestBot", description="a test bot",
            tone="neutral", response_style="brief",
            platform="telegram",
        )
        prompt = p.to_system_prompt()
        assert "TestBot" in prompt
        assert "test bot" in prompt
        assert "telegram" in prompt

    def test_language_hint(self):
        p = Persona(name="Bot", description="bot", language="Chinese")
        prompt = p.to_system_prompt()
        assert "Chinese" in prompt

    def test_auto_language_no_hint(self):
        p = Persona(name="Bot", description="bot", language="auto")
        prompt = p.to_system_prompt()
        assert "Reply in" not in prompt


# ---------------------------------------------------------------------------
# AutoReply — ReplyResult
# ---------------------------------------------------------------------------

class TestReplyResult:
    def test_dataclass(self):
        r = ReplyResult(text="Hello!", intent="needs_reply", delay_sec=3.5, persona="casual")
        assert r.text == "Hello!"
        assert r.delay_sec == 3.5


# ---------------------------------------------------------------------------
# AutoReply — Core Logic (without LLM)
# ---------------------------------------------------------------------------

class TestAutoReplyCleanup:
    def test_clean_reply_persona_prefix(self):
        text = "Casual: Hello there!"
        cleaned = AutoReply._clean_reply(text, "Casual")
        assert cleaned == "Hello there!"

    def test_clean_reply_quotes(self):
        text = '"Hello there!"'
        cleaned = AutoReply._clean_reply(text, "Bot")
        assert cleaned == "Hello there!"

    def test_clean_reply_assistant_prefix(self):
        text = "Assistant: How can I help?"
        cleaned = AutoReply._clean_reply(text, "Bot")
        assert cleaned == "How can I help?"

    def test_calculate_delay_range(self):
        for _ in range(20):
            delay = AutoReply._calculate_delay("Short msg", "Reply here", Intent.NEEDS_REPLY)
            assert 2.0 <= delay <= 30.0


# ---------------------------------------------------------------------------
# VisionFallback
# ---------------------------------------------------------------------------

class TestVisionFallback:
    def test_budget_management(self):
        cfg = VisionConfig(hourly_budget=5)
        vf = VisionFallback(config=cfg)
        assert vf.budget_remaining == 5

        for _ in range(3):
            vf._record_call()
        assert vf.budget_remaining == 2

    def test_budget_exhausted(self):
        cfg = VisionConfig(hourly_budget=2)
        vf = VisionFallback(config=cfg)
        vf._record_call()
        vf._record_call()
        assert not vf._check_budget()
        assert vf.budget_remaining == 0

    def test_parse_response_coordinates(self):
        resp = "The Send button is at COORDINATES: 680, 1375"
        result = VisionFallback._parse_response(resp)
        assert result.coordinates == (680, 1375)
        assert result.confidence == "high"

    def test_parse_response_fallback_numbers(self):
        resp = "I can see the button at approximately 540, 820 on the screen"
        result = VisionFallback._parse_response(resp)
        assert result.coordinates == (540, 820)
        assert result.confidence == "medium"

    def test_parse_response_not_found(self):
        resp = "I cannot find the Send button on this screen. NOT_FOUND."
        result = VisionFallback._parse_response(resp)
        assert result.coordinates is None

    def test_cache(self):
        cfg = VisionConfig(cache_ttl_sec=60)
        vf = VisionFallback(config=cfg)
        key = vf._cache_key("Send button", "chat screen")
        test_result = VisionResult(coordinates=(100, 200), confidence="high")
        vf._set_cache(key, test_result)
        cached = vf._get_cache(key)
        assert cached is not None
        assert cached.coordinates == (100, 200)

    def test_cache_expiry(self):
        cfg = VisionConfig(cache_ttl_sec=0.1)
        vf = VisionFallback(config=cfg)
        key = vf._cache_key("button", "screen")
        vf._set_cache(key, VisionResult(coordinates=(1, 2)))
        time.sleep(0.2)
        assert vf._get_cache(key) is None

    def test_stats(self):
        cfg = VisionConfig(hourly_budget=10)
        vf = VisionFallback(config=cfg)
        vf._record_call()
        vf._record_call()
        s = vf.stats()
        assert s["hourly_used"] == 2
        assert s["hourly_budget"] == 10
        assert s["budget_remaining"] == 8

    def test_build_prompt(self):
        prompt = VisionFallback._build_prompt("Send button", "Telegram chat")
        assert "Send button" in prompt
        assert "Telegram chat" in prompt
        assert "COORDINATES" in prompt

    def test_build_prompt_no_context(self):
        prompt = VisionFallback._build_prompt("Search icon", "")
        assert "Search icon" in prompt
        assert "Context" not in prompt
