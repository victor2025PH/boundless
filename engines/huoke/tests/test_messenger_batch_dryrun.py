# -*- coding: utf-8 -*-
"""P11c `scripts/messenger_batch_dryrun.py` 的 meta 测试。

不跑真设备, 但跑真 DB + 真 run_dryrun (只是 use_llm=False 跳过 LLM)。
主要验聚合统计 + 渲染 + CLI。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# P2-⑫: spawn 子 Python 进程时强制 UTF-8 防 Windows cp936 emoji 解码挂.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "messenger_batch_dryrun.py"


# ─── CLI ─────────────────────────────────────────────────────────────────────

class TestCli:
    def test_device_required(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 2
        assert "--device" in (r.stderr + r.stdout)


# ─── dataclasses ─────────────────────────────────────────────────────────────

class TestCaseResult:
    def test_default_empty(self):
        from scripts.messenger_batch_dryrun import CaseResult
        c = CaseResult(case_id="x", expected_intent="buying",
                       lang_hint="ja", incoming="t",
                       persona_key="", peer_type="friend",
                       contact_label="line")
        assert c.errors == []
        assert c.intent_match is None

    def test_to_dict(self):
        from scripts.messenger_batch_dryrun import CaseResult
        c = CaseResult(case_id="c1", expected_intent="buying",
                       lang_hint="ja", incoming="多少钱",
                       persona_key="", peer_type="friend",
                       contact_label="line",
                       actual_intent="buying")
        d = c.to_dict()
        assert d["case_id"] == "c1"
        assert d["expected_intent"] == "buying"


class TestMatrixReport:
    def test_to_dict_roundtrip(self):
        from scripts.messenger_batch_dryrun import MatrixReport
        r = MatrixReport(total_cases=10,
                          intent_distribution={"buying": 5, "cold": 5},
                          intent_match_rate=0.9)
        d = r.to_dict()
        assert d["total_cases"] == 10
        assert d["intent_match_rate"] == 0.9


# ─── aggregate ───────────────────────────────────────────────────────────────

class TestAggregate:
    def _c(self, **kw):
        from scripts.messenger_batch_dryrun import CaseResult
        defaults = dict(case_id="x", expected_intent="buying",
                        lang_hint="", incoming="", persona_key="",
                        peer_type="friend", contact_label="none")
        defaults.update(kw)
        return CaseResult(**defaults)

    def test_empty_cases(self):
        from scripts.messenger_batch_dryrun import aggregate
        r = aggregate([])
        assert r.total_cases == 0
        assert r.intent_match_rate == 0.0

    def test_intent_distribution(self):
        from scripts.messenger_batch_dryrun import aggregate
        cases = [
            self._c(actual_intent="buying"),
            self._c(actual_intent="buying"),
            self._c(actual_intent="cold"),
        ]
        r = aggregate(cases)
        assert r.intent_distribution == {"buying": 2, "cold": 1}

    def test_match_rate_computed(self):
        from scripts.messenger_batch_dryrun import aggregate
        cases = [
            self._c(actual_intent="buying", expected_intent="buying",
                    intent_match=True),
            self._c(actual_intent="cold", expected_intent="buying",
                    intent_match=False),
            self._c(actual_intent="buying", expected_intent="buying",
                    intent_match=True),
        ]
        r = aggregate(cases)
        # 2/3 ≈ 0.667
        assert abs(r.intent_match_rate - 0.667) < 0.01

    def test_mismatches_listed(self):
        from scripts.messenger_batch_dryrun import aggregate
        cases = [
            self._c(case_id="C1", actual_intent="cold",
                    expected_intent="buying", intent_match=False),
            self._c(case_id="C2", actual_intent="buying",
                    expected_intent="buying", intent_match=True),
        ]
        r = aggregate(cases)
        assert len(r.intent_mismatches) == 1
        assert "C1" in r.intent_mismatches[0]

    def test_errors_collected(self):
        from scripts.messenger_batch_dryrun import aggregate
        cases = [
            self._c(case_id="C1", errors=["boom"]),
            self._c(case_id="C2", errors=[]),
        ]
        r = aggregate(cases)
        assert len(r.errors_by_case) == 1
        assert r.errors_by_case[0][0] == "C1"

    def test_has_reply_counters(self):
        from scripts.messenger_batch_dryrun import aggregate
        cases = [
            self._c(has_llm_reply=True, final_reply_preview="yes"),
            self._c(has_llm_reply=False, final_reply_preview="placeholder"),
        ]
        r = aggregate(cases)
        assert r.cases_with_reply == 1
        assert r.cases_with_final_reply == 2


# ─── render ──────────────────────────────────────────────────────────────────

class TestRender:
    def test_render_contains_key_sections(self):
        from scripts.messenger_batch_dryrun import (
            render, aggregate, CaseResult,
        )
        cases = [
            CaseResult(case_id="C1", expected_intent="buying",
                       lang_hint="ja", incoming="値段",
                       persona_key="", peer_type="friend",
                       contact_label="line",
                       actual_intent="buying",
                       intent_source="rule",
                       gate_level="hard_allow",
                       decision="wa_referral",
                       intent_match=True,
                       final_reply_preview="加 LINE"),
        ]
        txt = render(cases, aggregate(cases))
        assert "Batch Dry-Run Matrix" in txt
        assert "Intent 识别准确率" in txt
        assert "Intent 分布" in txt
        assert "Decision 分布" in txt
        assert "100%" in txt  # 1/1


# ─── run_matrix_cases 集成 (真 DB + 真模块, 不碰设备) ───────────────────────

class TestRunMatrixCasesSmall:
    def test_small_sample_runs_without_crash(self, tmp_db):
        """跑 5 个 case 验模块链路完整, 无 crash。"""
        from scripts.messenger_batch_dryrun import (
            run_matrix_cases, aggregate,
        )
        cases = run_matrix_cases(
            device_id="test-dev",
            peer_name="MatrixTestPeer",
            use_llm=False,
            sample_limit=5,
        )
        assert len(cases) == 5
        # 每个 case 都应有基本字段
        for c in cases:
            assert c.case_id
            assert c.expected_intent
            assert c.actual_intent  # should fallback to smalltalk if nothing matches
        # errors 应为空 (无 UserProfile bug 类 silent failure)
        r = aggregate(cases)
        assert r.errors_by_case == []

    def test_rule_hits_match_expected(self, tmp_db):
        """buying/ja '値段教えてください' 应 rule 命中 buying。"""
        from scripts.messenger_batch_dryrun import run_matrix_cases
        cases = run_matrix_cases(
            device_id="test-dev",
            peer_name="MatrixTestPeer",
            use_llm=False,
            sample_limit=24,  # 一个 incoming × 4 persona × 2 peer_type × 3 contact
        )
        # 第 1 个 incoming 是 buying/ja
        buying_cases = [c for c in cases if c.expected_intent == "buying"
                         and c.lang_hint == "ja"]
        assert all(c.actual_intent == "buying" for c in buying_cases)
        assert all(c.intent_match for c in buying_cases)

    def test_referral_ask_italian_now_matches(self, tmp_db):
        """回归测试 P14: 2026-04-24 修的意大利语 referral_ask 规则。
        batch_dryrun 发现 'qual è il tuo numero' 被误判 smalltalk,
        扩 _REFERRAL_RE 后应命中 referral_ask。"""
        from src.ai.chat_intent import classify_intent
        history = [{"direction": "outgoing", "message_text": "greeting"},
                   {"direction": "incoming", "message_text": "prior"}]
        # 原 bug case
        r = classify_intent("qual è il tuo numero", history=history,
                             use_llm_fallback=False)
        assert r.intent == "referral_ask"
        assert r.source == "rule"
        # 其他常见意大利语表达也应命中
        for txt in ["il tuo numero di telefono per favore",
                    "dimmi il tuo contatto",
                    "qual è il tuo contatto WhatsApp"]:
            r = classify_intent(txt, history=history,
                                 use_llm_fallback=False)
            assert r.intent == "referral_ask", f"miss: {txt!r} → {r.intent}"
