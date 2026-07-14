# -*- coding: utf-8 -*-
"""P11b `scripts/messenger_production_dryrun.py` 的 meta 测试。

不调真 LLM, 不碰真机。验 run_dryrun 完整流程串联正确:
  memory (DB 读) → intent (rule/llm fallback) → LLM (mock) → gate → 决策
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# P2-⑫: spawn 子 Python 进程时强制 UTF-8 防 Windows cp936 emoji 解码挂.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "messenger_production_dryrun.py"


# ─── CLI 基础 ────────────────────────────────────────────────────────────────

class TestCli:
    def test_no_incoming_and_no_from_inbox_rejected(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--device", "d1", "--peer", "x", "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 2
        assert "--incoming" in (r.stderr + r.stdout)

    def test_missing_required_args(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 2


# ─── DryRunResult dataclass ──────────────────────────────────────────────────

class TestDryRunResult:
    def test_default_empty(self):
        from scripts.messenger_production_dryrun import DryRunResult
        r = DryRunResult()
        assert r.intent == ""
        assert r.decision == ""
        assert r.errors == []
        assert r.gate_reasons == []

    def test_to_dict_roundtrip(self):
        from scripts.messenger_production_dryrun import DryRunResult
        r = DryRunResult(device_id="d1", peer_name="Alice",
                         intent="buying", gate_score=3)
        d = r.to_dict()
        assert d["device_id"] == "d1"
        assert d["intent"] == "buying"
        assert d["gate_score"] == 3


# ─── fetch_latest_incoming ───────────────────────────────────────────────────

class TestFetchLatestIncoming:
    def test_empty_db(self, tmp_db):
        from scripts.messenger_production_dryrun import fetch_latest_incoming
        assert fetch_latest_incoming("devA", "Alice") is None

    def test_returns_latest(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_production_dryrun import fetch_latest_incoming
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="first")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="second")
        assert fetch_latest_incoming("devA", "Alice") == "second"

    def test_peer_isolated(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_production_dryrun import fetch_latest_incoming
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="m_alice")
        record_inbox_message("devA", "Bob", direction="incoming",
                             message_text="m_bob")
        assert fetch_latest_incoming("devA", "Alice") == "m_alice"
        assert fetch_latest_incoming("devA", "Bob") == "m_bob"

    def test_outgoing_not_returned(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_production_dryrun import fetch_latest_incoming
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="bot said")
        assert fetch_latest_incoming("devA", "Alice") is None


# ─── run_dryrun 主流程 ──────────────────────────────────────────────────────

class TestRunDryrunNoLLM:
    def test_referral_ask_triggers_hard_allow_wa_referral(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_production_dryrun import run_dryrun
        # 种 incoming
        record_inbox_message("devA", "Alice", direction="incoming",
                             peer_type="friend",
                             message_text="LINE でも連絡できますか?",
                             language_detected="ja")
        r = run_dryrun(
            device_id="devA", peer_name="Alice",
            from_inbox=True,
            referral_contact="line:xyz",
            persona_key="jp_female_midlife",
            use_llm=False,
        )
        assert r.incoming_text.startswith("LINE")
        assert r.intent == "referral_ask"
        assert r.intent_source == "rule"
        assert r.gate_level == "hard_allow"
        assert r.decision == "wa_referral"
        assert r.errors == []

    def test_buying_triggers_hard_allow(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_production_dryrun import run_dryrun
        record_inbox_message("devA", "Bob", direction="incoming",
                             message_text="how much does it cost?")
        r = run_dryrun(
            device_id="devA", peer_name="Bob",
            from_inbox=True,
            referral_contact="wa:+81900",
            use_llm=False,
        )
        assert r.intent == "buying"
        assert r.decision == "wa_referral"

    def test_smalltalk_without_contact_does_not_refer(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_production_dryrun import run_dryrun
        # 加 prior history 让 intent 非 opening
        record_inbox_message("devA", "Carol", direction="outgoing",
                             message_text="hi")
        record_inbox_message("devA", "Carol", direction="incoming",
                             message_text="nice weather today")
        r = run_dryrun(
            device_id="devA", peer_name="Carol",
            from_inbox=True,
            referral_contact="",  # 无引流 contact
            use_llm=False,
        )
        assert r.decision in ("reply", "skip")
        assert r.gate_level == "hard_block"  # no contact → hard_block

    def test_explicit_incoming_overrides_from_inbox(self, tmp_db):
        """explicit incoming 参数跳过 DB 查, 但 history 仍从 DB 读。"""
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_production_dryrun import run_dryrun
        # seed 一点 history 让 peer_turns 非空, 避免 opening rule 吞掉判断
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="greeting")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="hello")
        r = run_dryrun(
            device_id="devA", peer_name="Alice",
            incoming_text="加你 WhatsApp 吧",
            from_inbox=False,
            referral_contact="wa:+86",
            use_llm=False,
        )
        assert r.incoming_text == "加你 WhatsApp 吧"  # 用了 explicit
        assert r.intent == "referral_ask"
        assert r.decision == "wa_referral"

    def test_empty_peer_returns_error(self, tmp_db):
        from scripts.messenger_production_dryrun import run_dryrun
        r = run_dryrun(
            device_id="devA", peer_name="GhostPeer",
            from_inbox=True,
            use_llm=False,
        )
        assert "无 incoming" in r.errors[0]

    def test_no_incoming_provided_and_not_from_inbox(self, tmp_db):
        from scripts.messenger_production_dryrun import run_dryrun
        r = run_dryrun(
            device_id="devA", peer_name="Alice",
            incoming_text="",
            from_inbox=False,
            use_llm=False,
        )
        assert any("未提供" in e for e in r.errors)


class TestRunDryrunWithLLM:
    def test_mock_llm_generates_reply(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.messenger_production_dryrun import run_dryrun
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="tell me more about your service")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="(prev greeting)")
        # Mock ChatBrain
        mock_result = MagicMock()
        mock_result.message = "Sure, let me share details..."
        mock_result.referral_score = 0.3  # 不触发 hard
        fake_brain = MagicMock()
        fake_brain.generate_reply = MagicMock(return_value=mock_result)
        fake_brain_cls = MagicMock()
        fake_brain_cls.get_instance = MagicMock(return_value=fake_brain)
        with patch.dict("sys.modules", {
            "src.ai.chat_brain": MagicMock(
                ChatBrain=fake_brain_cls,
                UserProfile=MagicMock(),
            ),
        }):
            r = run_dryrun(
                device_id="devA", peer_name="Alice",
                from_inbox=True,
                use_llm=True,
            )
        assert r.llm_called is True
        assert "Sure" in r.llm_reply_text
        assert r.llm_referral_score == 0.3


# ─── render ──────────────────────────────────────────────────────────────────

class TestRender:
    def test_render_contains_all_sections(self):
        from scripts.messenger_production_dryrun import render, DryRunResult
        r = DryRunResult(
            device_id="d1", peer_name="Alice",
            incoming_text="hi",
            history_turns=3,
            intent="buying", intent_source="rule", intent_confidence=0.85,
            gate_level="hard_allow", gate_score=5, gate_threshold=3,
            gate_reasons=["intent=buying"],
            decision="wa_referral", final_reply="加 LINE: abc",
            referral_channel="line",
        )
        txt = render(r)
        assert "Dry-Run" in txt
        assert "Memory" in txt
        assert "Intent" in txt
        assert "Gate" in txt
        assert "Final Decision" in txt
        assert "buying" in txt
        assert "wa_referral" in txt
        assert "[DRY-RUN]" in txt
        assert "加 LINE" in txt

    def test_render_with_errors(self):
        from scripts.messenger_production_dryrun import render, DryRunResult
        r = DryRunResult(device_id="d1", peer_name="Alice",
                         errors=["LLM failed", "gate failed"])
        txt = render(r)
        assert "Errors" in txt
        assert "LLM failed" in txt
