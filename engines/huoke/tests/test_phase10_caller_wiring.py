# -*- coding: utf-8 -*-
"""Phase 10 prep · do_l2_gate caller chain wiring 单测 (2026-04-24).

验证 do_l2_gate 参数从 add_friend_with_note 正确透传到
_add_friend_safe_interaction_on_profile (经 _add_friend_with_note_locked).

不动 _phase10_l2_gate 的逻辑 (已被 test_phase10_l2_gate.py cover).
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


def _stub_fb():
    from contextlib import nullcontext
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._current_device = "D_p10w"
    fb._current_account = "test_acct"  # base_automation.guarded() 需要
    fb._last_greet_skip_reason = ""
    fb._current_lead_cid = ""
    fb._messenger_installed_cache = {}
    fb._check_peer_blocklist = lambda *a, **kw: False
    fb._did = lambda dev=None: dev or "D_p10w"
    fb._u2 = lambda dev=None: MagicMock()
    # guarded 是 contextmanager, stub 成 nullcontext 跳过 ComplianceGuard
    fb.guarded = lambda *a, **kw: nullcontext()
    return fb


@pytest.fixture(autouse=True)
def _patch_sleep():
    """加速测试: facebook.py 内有真 time.sleep / random.uniform."""
    with patch("src.app_automation.facebook.time.sleep"), \
         patch("src.app_automation.facebook.random.uniform", return_value=0.01), \
         patch("src.app_automation.facebook.random.randint", return_value=1):
        yield


class TestDoL2GateWiringDefaults:
    def test_default_add_friend_with_note_passes_false(self, tmp_db):
        """add_friend_with_note 不传 do_l2_gate → 透传到 _add_friend_safe = False."""
        fb = _stub_fb()
        captured = {}

        def _spy(self, d, did, profile_name, note, *,
                 persona_key, source, preset_key, do_l2_gate=False):
            captured["do_l2_gate"] = do_l2_gate
            return True

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy):
            with patch("src.host.fb_concurrency.device_section_lock") as ml:
                ml.return_value.__enter__ = lambda *a: None
                ml.return_value.__exit__ = lambda *a: None
                fb._ensure_foreground = lambda *a, **kw: True
                # from_current_profile=True 让我们直接走到 _add_friend_safe
                # (跳过 search_people)
                fb._is_likely_fb_profile_page = lambda d: True
                fb.add_friend_with_note(
                    "Anyone",
                    persona_key=None,  # 跳过 persona L1 gate
                    phase="growth",
                    from_current_profile=True,
                    device_id="D_p10w",
                )
        assert captured.get("do_l2_gate") is False, \
            f"默认应透传 do_l2_gate=False, 实际: {captured}"

    def test_explicit_true_propagates(self, tmp_db):
        """add_friend_with_note(do_l2_gate=True) → 透传到 _add_friend_safe = True."""
        fb = _stub_fb()
        captured = {}

        def _spy(self, d, did, profile_name, note, *,
                 persona_key, source, preset_key, do_l2_gate=False):
            captured["do_l2_gate"] = do_l2_gate
            return True

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy):
            with patch("src.host.fb_concurrency.device_section_lock") as ml:
                ml.return_value.__enter__ = lambda *a: None
                ml.return_value.__exit__ = lambda *a: None
                fb._ensure_foreground = lambda *a, **kw: True
                fb._is_likely_fb_profile_page = lambda d: True
                fb.add_friend_with_note(
                    "Anyone",
                    persona_key=None,
                    phase="growth",
                    from_current_profile=True,
                    do_l2_gate=True,
                    device_id="D_p10w",
                )
        assert captured.get("do_l2_gate") is True, \
            f"应透传 do_l2_gate=True, 实际: {captured}"


class TestDoL2GateWiringSearchPath:
    """非 from_current_profile 路径 (搜索后进资料页) 也要透传."""

    def test_search_path_propagates_true(self, tmp_db):
        """safe_mode 搜索路径透传 do_l2_gate=True."""
        fb = _stub_fb()
        captured = {}

        def _spy(self, d, did, profile_name, note, *,
                 persona_key, source, preset_key, do_l2_gate=False):
            captured["do_l2_gate"] = do_l2_gate
            return True

        # mock search 流程
        fb.search_people = lambda *a, **kw: [{"name": "Anyone"}]
        fb._first_search_result_element = lambda d, query_hint: MagicMock()
        fb.hb = MagicMock()
        fb._el_center = lambda el: (100, 100)
        fb._is_likely_fb_profile_page_xml = lambda xml: True
        fb._search_result_name_plausible = lambda a, b: True
        fb._adb = lambda *a, **kw: True

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy):
            with patch("src.host.fb_concurrency.device_section_lock") as ml:
                ml.return_value.__enter__ = lambda *a: None
                ml.return_value.__exit__ = lambda *a: None
                fb._ensure_foreground = lambda *a, **kw: True
                fb.add_friend_with_note(
                    "Anyone",
                    persona_key=None,
                    phase="growth",
                    safe_mode=True,
                    do_l2_gate=True,
                    device_id="D_p10w",
                )
        assert captured.get("do_l2_gate") is True, \
            "搜索 safe_mode 路径应透传 do_l2_gate=True"


class TestDoL2GateWiringCombo:
    """add_friend_and_greet 是 combo 入口, 也得透传 do_l2_gate 到 add_friend_with_note."""

    def test_combo_propagates_to_add_friend_with_note(self, tmp_db):
        fb = _stub_fb()
        captured = {}

        def _spy_afwn(*args, **kwargs):
            captured["do_l2_gate"] = kwargs.get("do_l2_gate")
            return False  # 短路: add_friend_failed → 不进 send_greeting

        with patch.object(fb, "add_friend_with_note", side_effect=_spy_afwn):
            fb.add_friend_and_greet(
                "Anyone",
                persona_key=None,
                phase="growth",
                do_l2_gate=True,
                device_id="D_p10w",
            )
        assert captured.get("do_l2_gate") is True, \
            f"add_friend_and_greet 应透传 do_l2_gate=True 到 add_friend_with_note, 实际: {captured}"

    def test_combo_default_propagates_false(self, tmp_db):
        fb = _stub_fb()
        captured = {}

        def _spy_afwn(*args, **kwargs):
            captured["do_l2_gate"] = kwargs.get("do_l2_gate")
            return False

        with patch.object(fb, "add_friend_with_note", side_effect=_spy_afwn):
            fb.add_friend_and_greet(
                "Anyone",
                persona_key=None,
                phase="growth",
                device_id="D_p10w",
            )
        assert captured.get("do_l2_gate") is False, \
            "combo 默认应透传 do_l2_gate=False"
