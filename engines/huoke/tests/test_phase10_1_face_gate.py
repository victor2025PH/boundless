# -*- coding: utf-8 -*-
"""Phase 10.1: _evaluate_match require_has_face / require_is_profile_page 早期门禁单测.

背景 (docs/PHASE10_PARTIAL_SMOKE_2026-04-24.md):
2026-04-24 partial smoke 实测 launcher 截图被 VLM 误判 score=95.5 PASS,
因 prompt 没强制要求"看到人脸"+"是 profile UI", VLM 看到 CJK ROM 文字
就把 is_japanese=true + 推测 age/gender. Phase 10.1 加 has_face/
is_profile_page 字段 + 早期 reject.
"""
from __future__ import annotations

import pytest

from src.host.fb_profile_classifier import _evaluate_match


class TestRequireHasFace:
    def test_no_face_field_with_require_rejects(self):
        """has_face 字段缺失 + require_has_face=True → REJECT (默认严格)."""
        persona = {"match_criteria": {"require_has_face": True}}
        ok, reasons = _evaluate_match(persona, {})
        assert ok is False
        assert "has_face=false" in reasons[0]

    def test_face_false_with_require_rejects(self):
        persona = {"match_criteria": {"require_has_face": True}}
        ok, reasons = _evaluate_match(persona, {"has_face": False})
        assert ok is False
        assert "没看到清晰人脸" in reasons[0]

    def test_face_true_with_require_proceeds(self):
        """has_face=True 时 require_has_face 不阻塞, 进入下游 age/gender 等检查."""
        persona = {"match_criteria": {
            "require_has_face": True,
            "age_bands_allowed": ["40s"],
            "genders_allowed": ["female"],
            "min_overall_confidence": 0.5,
        }}
        insights = {
            "has_face": True,
            "age_band": "40s",
            "gender": "female",
            "overall_confidence": 0.8,
        }
        ok, _ = _evaluate_match(persona, insights)
        assert ok is True

    def test_no_require_face_backward_compat(self):
        """match_criteria 无 require_has_face → 跳过新检查 (老 persona 不变行为)."""
        persona = {"match_criteria": {
            "age_bands_allowed": ["40s"],
            "genders_allowed": ["female"],
            "min_overall_confidence": 0.5,
        }}
        insights = {  # 故意缺 has_face 字段
            "age_band": "40s",
            "gender": "female",
            "overall_confidence": 0.8,
        }
        ok, _ = _evaluate_match(persona, insights)
        assert ok is True, \
            "老 persona 没设 require_has_face 时不应受新规则影响"


class TestRequireIsProfilePage:
    def test_not_profile_page_rejects(self):
        persona = {"match_criteria": {"require_is_profile_page": True}}
        ok, reasons = _evaluate_match(persona, {"is_profile_page": False})
        assert ok is False
        assert "is_profile_page=false" in reasons[0]
        assert "FB/IG profile UI" in reasons[0]

    def test_is_profile_page_true_proceeds(self):
        persona = {"match_criteria": {
            "require_is_profile_page": True,
            "age_bands_allowed": ["40s"],
            "genders_allowed": ["female"],
            "min_overall_confidence": 0.5,
        }}
        insights = {
            "is_profile_page": True,
            "age_band": "40s",
            "gender": "female",
            "overall_confidence": 0.8,
        }
        ok, _ = _evaluate_match(persona, insights)
        assert ok is True


class TestEarlyRejectShortCircuit:
    """has_face / is_profile_page 应在 age/gender/japanese 检查**之前** reject,
    避免错误 reasons 污染 (e.g. 'age_band=unknown 不在允许范围' 实际根因是没脸)."""

    def test_no_face_short_circuits_before_age_check(self):
        """has_face=False 时, reasons 只含 has_face 信息, 不报 age_band 错."""
        persona = {"match_criteria": {
            "require_has_face": True,
            "age_bands_allowed": ["40s"],
            "genders_allowed": ["female"],
        }}
        insights = {
            "has_face": False,
            "age_band": "unknown",  # VLM 应在 has_face=false 时返 unknown
            "gender": "unknown",
        }
        ok, reasons = _evaluate_match(persona, insights)
        assert ok is False
        assert len(reasons) == 1, \
            f"早 reject 后应只 1 条 reason, 实际 {len(reasons)}: {reasons}"
        assert "has_face" in reasons[0]


class TestPhase10_1_RegressionScenario:
    """Reproduce 2026-04-24 partial smoke 现场: launcher 截图被误判 PASS."""

    def test_launcher_screenshot_now_rejected(self):
        """模拟 partial smoke 当时 VLM 输出 (在没 has_face 字段时 PASS)
        + Phase 10.1 jp_female_midlife persona 加 require_has_face=True → REJECT."""
        # partial smoke 当时 VLM 真返回 (有 score 95.5 / passed:true 的那次):
        #   age_band=40s, gender=female, is_japanese_confidence=0.95
        # 但**真实情况**是 launcher 截图, 没人脸
        # Phase 10.1 后 VLM 应返 has_face=False (按新 prompt 规则)
        # 此测试模拟 VLM 正确遵循新 prompt:
        insights_new_prompt = {
            "has_face": False,         # ← 新 prompt 要求 VLM 这里返 false
            "is_profile_page": False,  # ← launcher 不是 profile page
            "age_band": "unknown",     # ← 新 prompt 要求 unknown
            "gender": "unknown",
            "is_japanese": False,
            "is_japanese_confidence": 0.0,
            "overall_confidence": 0.0,
        }

        # jp_female_midlife persona 现状 (Phase 10.1 yaml 改动)
        persona = {"match_criteria": {
            "require_has_face": True,
            "require_is_profile_page": True,
            "age_bands_allowed": ["30s", "40s", "50s", "60s"],
            "genders_allowed": ["female"],
            "require_is_japanese": True,
            "min_overall_confidence": 0.55,
            "min_japanese_confidence": 0.50,
        }}
        ok, reasons = _evaluate_match(persona, insights_new_prompt)
        assert ok is False, "launcher 截图应被 has_face gate REJECT (Phase 10.1 fix)"
        assert "has_face=false" in reasons[0]
