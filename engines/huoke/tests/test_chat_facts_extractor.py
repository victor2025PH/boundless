# -*- coding: utf-8 -*-
"""P10b `src/ai/chat_facts_extractor.py` 单元测试。

覆盖:
  * ExtractConfig 默认关 (enabled=False)
  * should_run_extraction gate 5 层检查
  * _call_llm_for_extraction JSON 容错
  * _persist_extraction 写 fb_contact_events
  * run_facts_extraction 一站式流程 (gate skip / LLM 失败 / 空更新 / 完整流程)

Phase 5 未 merge 时 (record_contact_event / fb_contact_events 表不存在)
大部分路径走 graceful skip。
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock, patch

import pytest


# ─── ExtractConfig 默认 ──────────────────────────────────────────────────────

class TestExtractConfigDefaults:
    def test_enabled_default_false(self):
        from src.ai.chat_facts_extractor import DEFAULT_CONFIG
        assert DEFAULT_CONFIG.enabled is False

    def test_budget_caps_reasonable(self):
        from src.ai.chat_facts_extractor import DEFAULT_CONFIG
        assert 1 <= DEFAULT_CONFIG.daily_cap_per_device <= 100
        assert 1 <= DEFAULT_CONFIG.per_peer_min_hours <= 168
        assert DEFAULT_CONFIG.min_incomings_for_extraction >= 1
        assert DEFAULT_CONFIG.max_incomings_per_call >= 3


# ─── should_run_extraction gate ──────────────────────────────────────────────

class TestShouldRunExtraction:
    def test_disabled_config_blocks(self, tmp_db):
        from src.ai.chat_facts_extractor import (
            should_run_extraction, ExtractConfig,
        )
        cfg = ExtractConfig(enabled=False)
        d = should_run_extraction("devA", "Alice", config=cfg)
        assert d.should_run is False
        assert d.reason == "extraction_disabled"

    def test_empty_args_block(self, tmp_db):
        from src.ai.chat_facts_extractor import (
            should_run_extraction, ExtractConfig,
        )
        cfg = ExtractConfig(enabled=True)
        assert should_run_extraction("", "Alice", config=cfg).should_run is False
        assert should_run_extraction("d1", "", config=cfg).should_run is False

    def test_too_few_incomings_block(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_facts_extractor import (
            should_run_extraction, ExtractConfig,
        )
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=3)
        # 只有 2 条 incoming, 不够 3 条
        record_inbox_message("d1", "Alice", direction="incoming",
                             message_text="hi")
        record_inbox_message("d1", "Alice", direction="incoming",
                             message_text="hello")
        d = should_run_extraction("d1", "Alice", config=cfg)
        assert d.should_run is False
        assert "too_few_incomings" in d.reason

    def test_enough_incomings_passes(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_facts_extractor import (
            should_run_extraction, ExtractConfig,
        )
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=2)
        for m in ["hi", "how are you", "nice weather today"]:
            record_inbox_message("d1", "Alice", direction="incoming",
                                 message_text=m)
        d = should_run_extraction("d1", "Alice", config=cfg)
        assert d.should_run is True
        assert d.reason == "gate_pass"
        assert d.incoming_count == 3


# ─── _call_llm_for_extraction JSON 容错 ─────────────────────────────────────

class TestCallLlmForExtraction:
    def test_llm_unavailable_returns_none(self):
        from src.ai.chat_facts_extractor import (
            _call_llm_for_extraction, DEFAULT_CONFIG,
        )
        # Patch LLMClient to raise ImportError/Exception
        with patch("src.ai.llm_client.LLMClient",
                   side_effect=RuntimeError("no LLM configured")):
            r = _call_llm_for_extraction(
                [{"text": "hello", "seen_at": "2026-04-23"}],
                {}, DEFAULT_CONFIG)
        assert r is None

    def test_valid_json_parsed(self):
        from src.ai.chat_facts_extractor import (
            _call_llm_for_extraction, DEFAULT_CONFIG,
        )
        fake = MagicMock()
        fake.chat_with_system.return_value = (
            '{"updated_facts": {"occupation": "designer"},'
            ' "reason": "mentioned job"}'
        )
        with patch("src.ai.llm_client.LLMClient", return_value=fake):
            r = _call_llm_for_extraction(
                [{"text": "I work as a designer", "seen_at": ""}],
                {}, DEFAULT_CONFIG)
        assert r is not None
        assert r["updated_facts"] == {"occupation": "designer"}
        assert "mentioned" in r["reason"]

    def test_code_fence_wrapped_json_parsed(self):
        from src.ai.chat_facts_extractor import (
            _call_llm_for_extraction, DEFAULT_CONFIG,
        )
        fake = MagicMock()
        fake.chat_with_system.return_value = (
            '```json\n{"updated_facts": {"interests": ["photo"]},'
            ' "reason": "x"}\n```'
        )
        with patch("src.ai.llm_client.LLMClient", return_value=fake):
            r = _call_llm_for_extraction(
                [{"text": "I love photography", "seen_at": ""}],
                {}, DEFAULT_CONFIG)
        assert r["updated_facts"]["interests"] == ["photo"]

    def test_non_json_response_returns_none(self):
        from src.ai.chat_facts_extractor import (
            _call_llm_for_extraction, DEFAULT_CONFIG,
        )
        fake = MagicMock()
        fake.chat_with_system.return_value = "I don't know"
        with patch("src.ai.llm_client.LLMClient", return_value=fake):
            r = _call_llm_for_extraction(
                [{"text": "meh", "seen_at": ""}], {}, DEFAULT_CONFIG)
        assert r is None

    def test_updated_facts_not_dict_returns_none(self):
        from src.ai.chat_facts_extractor import (
            _call_llm_for_extraction, DEFAULT_CONFIG,
        )
        fake = MagicMock()
        fake.chat_with_system.return_value = (
            '{"updated_facts": "not a dict", "reason": "x"}'
        )
        with patch("src.ai.llm_client.LLMClient", return_value=fake):
            r = _call_llm_for_extraction(
                [{"text": "hi", "seen_at": ""}], {}, DEFAULT_CONFIG)
        assert r is None


# ─── run_facts_extraction 一站式 ────────────────────────────────────────────

class TestRunFactsExtraction:
    def test_default_config_disabled_skips(self, tmp_db):
        from src.ai.chat_facts_extractor import run_facts_extraction
        r = run_facts_extraction("d1", "Alice")
        assert r.ran is False
        assert r.decision_reason == "extraction_disabled"
        assert r.updated_facts == {}
        assert r.persisted is False

    def test_gate_skip_too_few_incomings(self, tmp_db):
        from src.ai.chat_facts_extractor import (
            run_facts_extraction, ExtractConfig,
        )
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=3)
        r = run_facts_extraction("d1", "Alice", config=cfg)
        assert r.ran is False
        assert "too_few_incomings" in r.decision_reason

    def test_full_path_with_mocked_llm(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_facts_extractor import (
            run_facts_extraction, ExtractConfig,
        )
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=2)
        for m in ["I'm a designer based in Tokyo",
                  "Been doing photography for 5 years",
                  "Trying to find a new client"]:
            record_inbox_message("d1", "Alice", direction="incoming",
                                 message_text=m)
        fake = MagicMock()
        fake.chat_with_system.return_value = (
            '{"updated_facts":'
            ' {"occupation": "designer", "location": "Tokyo",'
            '  "interests": ["photography"]},'
            ' "reason": "extracted from recent messages"}'
        )
        with patch("src.ai.llm_client.LLMClient", return_value=fake):
            r = run_facts_extraction("d1", "Alice", config=cfg,
                                     preset_key="jp_growth")

        assert r.ran is True
        assert r.decision_reason == "gate_pass"
        assert r.updated_facts.get("occupation") == "designer"
        assert r.updated_facts.get("location") == "Tokyo"
        # Phase 5 未 merge 时 persisted=False (graceful)
        # merge 后 persisted=True

    def test_empty_updated_facts_no_persist(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_facts_extractor import (
            run_facts_extraction, ExtractConfig,
        )
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=2)
        for m in ["hi", "hello", "how are you"]:
            record_inbox_message("d1", "Alice", direction="incoming",
                                 message_text=m)
        fake = MagicMock()
        fake.chat_with_system.return_value = (
            '{"updated_facts": {}, "reason": "no new info"}'
        )
        with patch("src.ai.llm_client.LLMClient", return_value=fake):
            r = run_facts_extraction("d1", "Alice", config=cfg)
        assert r.ran is True
        assert r.updated_facts == {}
        assert r.persisted is False
        assert r.llm_reason == "no new info"

    def test_llm_call_failed_returns_decision_reason(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_facts_extractor import (
            run_facts_extraction, ExtractConfig,
        )
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=2)
        for m in ["a", "b", "c"]:
            record_inbox_message("d1", "Alice", direction="incoming",
                                 message_text=m)
        with patch("src.ai.llm_client.LLMClient",
                   side_effect=RuntimeError("no LLM")):
            r = run_facts_extraction("d1", "Alice", config=cfg)
        assert r.ran is False
        assert r.decision_reason == "llm_call_failed"


# ─── _persist_extraction ─────────────────────────────────────────────────────

class TestPersistExtraction:
    def test_returns_false_when_record_contact_event_unavailable(self, tmp_db):
        """Graceful-degradation: `record_contact_event` ImportError → return
        False 不抛。原 "phase5 未 merge" 场景 — phase5 merge 后 import 总能成功,
        本测试用 import 层 patch 模拟"infra 缺失" case 以覆盖 except ImportError
        分支 (生产里理论上不触发, 但 defensive code 值得有 test)。"""
        from src.ai import chat_facts_extractor
        # 模拟 src.host.fb_store.record_contact_event 导入失败
        import sys
        with patch.dict(sys.modules, {"src.host.fb_store": None}):
            assert chat_facts_extractor._persist_extraction(
                "d1", "Alice", {"occupation": "x"}, "reason") is False

    def test_persists_when_record_contact_event_available(self, tmp_db,
                                                           monkeypatch):
        from src.ai import chat_facts_extractor
        from src.host import fb_store
        calls = []
        monkeypatch.setattr(fb_store, "record_contact_event",
                            lambda *a, **kw: (calls.append((a, kw)) or 1),
                            raising=False)
        r = chat_facts_extractor._persist_extraction(
            "d1", "Alice",
            {"occupation": "designer", "location": "Tokyo"},
            "short reason",
            preset_key="jp_growth",
        )
        assert r is True
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] == "d1"
        assert args[1] == "Alice"
        assert args[2] == "facts_extracted"
        assert kwargs["preset_key"] == "jp_growth"
        meta = kwargs["meta"]
        assert meta["extracted_facts"]["occupation"] == "designer"
        assert meta["reason"] == "short reason"


# ─── Cooldown 逻辑 (_last_extraction_at + per_peer_min_hours) ───────────────

class TestPeerCooldown:
    def test_cooldown_blocks_second_extraction(self, tmp_db, monkeypatch):
        """如果 _last_extraction_at 返最近 1 小时前, min_hours=20 应阻塞。"""
        from src.ai import chat_facts_extractor
        from src.ai.chat_facts_extractor import (
            should_run_extraction, ExtractConfig,
        )
        from src.host.fb_store import record_inbox_message
        for m in ["hi", "a", "b"]:
            record_inbox_message("d1", "Alice", direction="incoming",
                                 message_text=m)
        recent = _dt.datetime.utcnow() - _dt.timedelta(hours=1)
        monkeypatch.setattr(chat_facts_extractor, "_last_extraction_at",
                            lambda did, peer: recent)
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=2,
                            per_peer_min_hours=20)
        d = should_run_extraction("d1", "Alice", config=cfg)
        assert d.should_run is False
        assert "peer_cooldown" in d.reason

    def test_cooldown_expired_allows(self, tmp_db, monkeypatch):
        from src.ai import chat_facts_extractor
        from src.ai.chat_facts_extractor import (
            should_run_extraction, ExtractConfig,
        )
        from src.host.fb_store import record_inbox_message
        for m in ["hi", "a", "b"]:
            record_inbox_message("d1", "Alice", direction="incoming",
                                 message_text=m)
        old = _dt.datetime.utcnow() - _dt.timedelta(hours=48)
        monkeypatch.setattr(chat_facts_extractor, "_last_extraction_at",
                            lambda did, peer: old)
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=2,
                            per_peer_min_hours=20)
        d = should_run_extraction("d1", "Alice", config=cfg)
        assert d.should_run is True


# ─── Daily cap ───────────────────────────────────────────────────────────────

class TestDailyCap:
    def test_cap_reached_blocks(self, tmp_db, monkeypatch):
        from src.ai import chat_facts_extractor
        from src.ai.chat_facts_extractor import (
            should_run_extraction, ExtractConfig,
        )
        from src.host.fb_store import record_inbox_message
        for m in ["hi", "a", "b"]:
            record_inbox_message("d1", "Alice", direction="incoming",
                                 message_text=m)
        monkeypatch.setattr(chat_facts_extractor,
                            "_count_extractions_today",
                            lambda did: 10)
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=2,
                            daily_cap_per_device=10)
        d = should_run_extraction("d1", "Alice", config=cfg)
        assert d.should_run is False
        assert "daily_cap_reached" in d.reason

    def test_cap_not_reached_allows(self, tmp_db, monkeypatch):
        from src.ai import chat_facts_extractor
        from src.ai.chat_facts_extractor import (
            should_run_extraction, ExtractConfig,
        )
        from src.host.fb_store import record_inbox_message
        for m in ["hi", "a", "b"]:
            record_inbox_message("d1", "Alice", direction="incoming",
                                 message_text=m)
        monkeypatch.setattr(chat_facts_extractor,
                            "_count_extractions_today",
                            lambda did: 3)  # 3 < 10
        cfg = ExtractConfig(enabled=True, min_incomings_for_extraction=2,
                            daily_cap_per_device=10)
        d = should_run_extraction("d1", "Alice", config=cfg)
        assert d.should_run is True
