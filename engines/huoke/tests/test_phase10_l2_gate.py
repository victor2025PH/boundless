# -*- coding: utf-8 -*-
"""Phase 10 prep: L2 VLM gate 单测 (2026-04-24).

覆盖 ``_phase10_l2_gate`` helper + ``_add_friend_safe_interaction_on_profile``
集成路径, 全部 mock VLM (``classify``) 和 ``capture_profile_snapshots``,
不依赖真机/ollama endpoint:

  * ``_phase10_l2_gate`` helper: PASS / FAIL / classify 异常 / capture 异常 /
    PASS 写 persona_classified journey (5 case)
  * ``_add_friend_safe_interaction_on_profile`` 集成:
    do_l2_gate=False (默认) → 不调 L2 / do_l2_gate=True + L2 PASS → 走 Add Friend /
    do_l2_gate=True + L2 BLOCK → early-return, 不点 Add Friend (3 case)

总 8 case. 依赖 fixture ``tmp_db`` (来自 conftest, 提供干净的 lead_mesh DB).
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


def _stub_fb():
    """构建一个最小可用的 FacebookAutomation 实例 (跳过 __init__ 的真机依赖)."""
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._current_device = "D_p10"
    fb._last_greet_skip_reason = ""
    fb._current_lead_cid = ""
    fb._messenger_installed_cache = {}
    fb._did = lambda dev=None: dev or "D_p10"
    fb._u2 = lambda dev=None: MagicMock()
    return fb


@pytest.fixture(autouse=True)
def _patch_sleep():
    """全测试 patch time.sleep + random 防 _add_friend_safe_interaction 路径
    跑真 sleep 拖慢用例 (该方法体含多处 random.uniform 2-4s 的 time.sleep)."""
    with patch("src.app_automation.facebook.time.sleep"), \
         patch("src.app_automation.facebook.random.randint", return_value=1), \
         patch("src.app_automation.facebook.random.uniform", return_value=0.01):
        yield


# ─── _phase10_l2_gate helper unit tests ─────────────────────────────────────

class TestPhase10L2GateHelper:
    def test_l2_pass_returns_false_proceeds(self, tmp_db):
        """L2 命中 → helper 返回 False (允许继续 add_friend)."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/fake1.png"],
            "shot_count": 1, "save_dir": "/tmp",
            "display_name": "山田花子", "bio_text": "",
        }
        fake_result = {
            "ok": True, "match": True,
            "l2": {"pass": True, "score": 80,
                   "reasons": ["女性头像", "日文 bio"]},
        }
        with patch("src.host.fb_profile_classifier.classify",
                   return_value=fake_result):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10",
                profile_name="山田花子",
                persona_key="jp_female_midlife",
            )
        assert blocked is False

    def test_l2_fail_returns_true_blocks_with_journey(self, tmp_db):
        """L2 不命中 → helper 返 True (阻止) + journey add_friend_blocked."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/fake.png"], "shot_count": 1,
            "save_dir": "/tmp", "display_name": "John Smith", "bio_text": "",
        }
        fake_result = {
            "ok": True, "match": False,
            "l2": {"pass": False, "score": 10, "reasons": ["男性头像"]},
        }
        with patch("src.host.fb_profile_classifier.classify",
                   return_value=fake_result):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10",
                profile_name="John Smith",
                persona_key="jp_female_midlife",
            )
        assert blocked is True

        # 校验 journey
        from src.host.lead_mesh import resolve_identity, get_journey
        cid = resolve_identity(platform="facebook",
                               account_id="fb:John Smith",
                               display_name="John Smith")
        events = get_journey(cid)
        blocked_events = [e for e in events
                          if e["action"] == "add_friend_blocked"]
        assert any(e["data"].get("reason") == "persona_l2_rejected"
                   for e in blocked_events), \
            f"期望 persona_l2_rejected event, 实际: {blocked_events}"

    def test_classify_exception_fail_open(self, tmp_db):
        """classify 抛异常 → 保守放行 (返 False, 与 L1 gate 一致)."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/x.png"], "shot_count": 1,
            "save_dir": "/tmp", "display_name": "X", "bio_text": "",
        }
        with patch("src.host.fb_profile_classifier.classify",
                   side_effect=RuntimeError("VLM endpoint down")):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10",
                profile_name="Anyone",
                persona_key="jp_female_midlife",
            )
        assert blocked is False, "classify 异常应保守放行 (fail-open)"

    def test_capture_exception_fail_open(self, tmp_db):
        """capture_profile_snapshots 抛异常 → 保守放行 (没截图就没法 L2)."""
        fb = _stub_fb()

        def _broken_capture(*a, **kw):
            raise RuntimeError("scrcpy unavailable")

        fb.capture_profile_snapshots = _broken_capture
        with patch("src.host.fb_profile_classifier.classify"):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10",
                profile_name="X", persona_key="jp_female_midlife",
            )
        assert blocked is False, "capture 异常应保守放行"

    def test_l2_pass_writes_persona_classified_journey(self, tmp_db):
        """L2 PASS 时写 persona_classified{stage='L2'} journey (供 funnel)."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/x.png"], "shot_count": 1,
            "save_dir": "/tmp", "display_name": "佐藤花子", "bio_text": "",
        }
        fake_result = {
            "ok": True, "match": True,
            "l2": {"pass": True, "score": 75,
                   "reasons": ["头像女性", "bio 日本语"]},
        }
        with patch("src.host.fb_profile_classifier.classify",
                   return_value=fake_result):
            fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10",
                profile_name="佐藤花子",
                persona_key="jp_female_midlife",
            )

        from src.host.lead_mesh import resolve_identity, get_journey
        cid = resolve_identity(platform="facebook",
                               account_id="fb:佐藤花子",
                               display_name="佐藤花子")
        events = get_journey(cid)
        classified = [e for e in events
                      if e["action"] == "persona_classified"]
        assert len(classified) >= 1
        assert classified[0]["data"]["stage"] == "L2"
        assert classified[0]["data"]["match"] is True
        assert classified[0]["data"]["score"] == 75


# ─── _add_friend_safe_interaction_on_profile 集成测试 ─────────────────────

class TestAddFriendSafeWithL2Gate:
    def test_default_off_does_not_call_l2_gate(self, tmp_db):
        """do_l2_gate=False (默认) → 不调 _phase10_l2_gate, 现行行为不变."""
        fb = _stub_fb()
        fb._detect_risk_dialog = lambda d: (False, "")
        fb.hb = MagicMock()
        fb.smart_tap = MagicMock(return_value=True)
        fb._record_friend_request_safely = MagicMock()
        fb._el_center = MagicMock(return_value=(100, 100))
        # spy
        l2_called = {"flag": False}
        fb._phase10_l2_gate = lambda *a, **kw: (
            l2_called.update(flag=True) or False)

        fb._add_friend_safe_interaction_on_profile(
            d=MagicMock(), did="D_p10",
            profile_name="Anyone", note="",
            persona_key="jp_female_midlife",
            source="", preset_key="",
            # do_l2_gate 不传, 默认 False
        )
        assert l2_called["flag"] is False, \
            "默认 do_l2_gate=False 不应调 _phase10_l2_gate"

    def test_l2_block_returns_false_no_add_friend_tap(self, tmp_db):
        """do_l2_gate=True + L2 阻止 → 早退 + 不点 Add Friend 按钮."""
        fb = _stub_fb()
        fb._detect_risk_dialog = lambda d: (False, "")
        fb._phase10_l2_gate = lambda *a, **kw: True  # L2 阻止
        fb.hb = MagicMock()
        smart_tap_calls = []
        fb.smart_tap = lambda name, **kw: (smart_tap_calls.append(name) or True)
        fb._record_friend_request_safely = MagicMock()

        result = fb._add_friend_safe_interaction_on_profile(
            d=MagicMock(), did="D_p10",
            profile_name="Anyone", note="",
            persona_key="jp_female_midlife",
            source="", preset_key="",
            do_l2_gate=True,
        )
        assert result is False
        assert smart_tap_calls == [], \
            "L2 阻止后应 early-return, 绝不能调 smart_tap (Add Friend)"
        fb._record_friend_request_safely.assert_not_called()

    def test_l2_pass_proceeds_to_add_friend_tap(self, tmp_db):
        """do_l2_gate=True + L2 PASS → 进入 Add Friend 按钮 + 入库流程."""
        fb = _stub_fb()
        fb._detect_risk_dialog = lambda d: (False, "")
        fb._phase10_l2_gate = lambda *a, **kw: False  # L2 通过
        fb.hb = MagicMock()
        smart_tap_calls = []
        fb.smart_tap = lambda name, **kw: (smart_tap_calls.append(name) or True)
        fb._record_friend_request_safely = MagicMock()

        result = fb._add_friend_safe_interaction_on_profile(
            d=MagicMock(), did="D_p10",
            profile_name="Anyone", note="",
            persona_key="jp_female_midlife",
            source="", preset_key="",
            do_l2_gate=True,
        )
        assert result is True
        # smart_tap 至少被调一次去点 Add Friend
        assert any("Add Friend" in c for c in smart_tap_calls), \
            f"L2 PASS 后应调 smart_tap('Add Friend...'), 实际调用: {smart_tap_calls}"
        fb._record_friend_request_safely.assert_called_once()
