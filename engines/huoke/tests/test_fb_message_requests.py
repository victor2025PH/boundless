# -*- coding: utf-8 -*-
"""P6 `check_message_requests` 陌生人自动回复测试。

覆盖:
  * auto_reply=False 维持 Sprint 2 只读行为 (不调 _ai_reply_and_send)
  * auto_reply=True 走完整读+回+引流链路
  * stats 计数: replies_sent / wa_referrals / reply_skipped / errors
  * peer_type='stranger' 正确传进 _ai_reply_and_send
  * _ai_reply_and_send 的 peer_type='stranger' 触发保守 gate 配置
  * 风控对话框 → 提前返回
  * 入口未找到 → error + 提前返回
  * auto_reply 的 playbook 覆写

所有设备层 adb/u2/smart_tap/_ai_reply_and_send 都 mock。
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
from unittest.mock import MagicMock, patch

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.hb = MagicMock()
    fb.hb.wait_think = MagicMock()
    return fb


@pytest.fixture
def fb_env():
    """共用 mock 环境: 所有设备/UI 交互 no-op,_ai_reply_and_send 可通过 knobs 控制。"""
    fb = _make_fb()
    knobs = {
        "smart_tap_results": {},
        "requests_list": [],       # [{"name": ..., "bounds": ...}]
        "read_results": {},        # peer_name → {"incoming_text": ..., "risk": ...}
        "reply_results": {},       # peer_name → (reply, decision)
        "risk_dialog": (False, ""),
        "entry_found": True,
        "phase_cfg": ("growth", {}),
    }
    ai_reply_calls = []  # 记录所有 _ai_reply_and_send 调用

    fake_u2 = MagicMock()
    fake_u2.app_start = MagicMock()
    fake_u2.press = MagicMock()

    def _smart_tap(name, device_id=None, **kw):
        for k, v in knobs["smart_tap_results"].items():
            if k in name:
                return v
        return True

    def _ai_reply(d, did, *, peer_name, incoming_text, referral_contact="",
                 preset_key="", persona_key=None, peer_type="friend"):
        ai_reply_calls.append({
            "peer_name": peer_name,
            "incoming_text": incoming_text,
            "peer_type": peer_type,
            "referral_contact": referral_contact,
            "preset_key": preset_key,
            "persona_key": persona_key,
        })
        return knobs["reply_results"].get(peer_name, ("mock_reply", "reply"))

    def _open_and_read(d, conv, did, peer_type="friend", preset_key=""):
        name = conv["name"]
        return knobs["read_results"].get(name, {
            "peer_name": name, "incoming_text": f"from_{name}"})

    stack = ExitStack()
    stack.enter_context(patch.object(fb, "_did", return_value="devA"))
    stack.enter_context(patch.object(fb, "_u2", return_value=fake_u2))
    stack.enter_context(patch.object(fb, "smart_tap", side_effect=_smart_tap))
    stack.enter_context(patch.object(fb, "_dismiss_dialogs"))
    stack.enter_context(patch.object(
        fb, "_detect_risk_dialog", side_effect=lambda d: knobs["risk_dialog"]
    ))
    stack.enter_context(patch.object(
        fb, "_open_message_requests_fallback",
        side_effect=lambda d: knobs["entry_found"]
    ))
    stack.enter_context(patch.object(
        fb, "_list_messenger_conversations",
        side_effect=lambda d, n: list(knobs["requests_list"][:n])
    ))
    stack.enter_context(patch.object(fb, "_open_and_read_conversation",
                                     side_effect=_open_and_read))
    stack.enter_context(patch.object(fb, "_ai_reply_and_send",
                                     side_effect=_ai_reply))
    stack.enter_context(patch.object(fb, "guarded",
                                     return_value=nullcontext()))
    stack.enter_context(patch(
        "src.app_automation.facebook._resolve_phase_and_cfg",
        side_effect=lambda section, device_id=None, phase_override=None:
            knobs["phase_cfg"]
    ))
    stack.enter_context(patch("src.app_automation.facebook.time.sleep"))
    stack.enter_context(patch("src.app_automation.facebook.random.uniform",
                              return_value=0.0))
    try:
        yield fb, knobs, ai_reply_calls
    finally:
        stack.close()


# ─── auto_reply=False 向后兼容 ───────────────────────────────────────────────

class TestReadOnlyBehavior:
    def test_auto_reply_false_does_not_call_ai_reply(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}, {"name": "Bob"}]
        stats = fb.check_message_requests(auto_reply=False, max_requests=5)
        assert len(calls) == 0
        assert stats["messages_collected"] == 2
        assert stats["replies_sent"] == 0
        assert stats["auto_reply"] is False


# ─── auto_reply=True 主路径 ─────────────────────────────────────────────────

class TestAutoReplyEnabled:
    def test_reply_count_and_peer_type(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}, {"name": "Bob"}]
        stats = fb.check_message_requests(auto_reply=True, max_requests=5)
        assert len(calls) == 2
        # 全部 peer_type 应为 stranger (不是 friend)
        for c in calls:
            assert c["peer_type"] == "stranger"
        assert stats["replies_sent"] == 2
        assert stats["wa_referrals"] == 0
        assert stats["auto_reply"] is True

    def test_wa_referral_counted(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}]
        knobs["reply_results"] = {"Alice": ("加我 LINE", "wa_referral")}
        stats = fb.check_message_requests(auto_reply=True, max_requests=5)
        assert stats["replies_sent"] == 1
        assert stats["wa_referrals"] == 1

    def test_skip_counted_on_llm_none(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}]
        knobs["reply_results"] = {"Alice": (None, "skip")}
        stats = fb.check_message_requests(auto_reply=True, max_requests=5)
        assert stats["reply_skipped"] == 1
        assert stats["replies_sent"] == 0

    def test_referral_contact_propagated(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}]
        fb.check_message_requests(auto_reply=True, max_requests=5,
                                  referral_contact="line:abc123")
        assert calls[0]["referral_contact"] == "line:abc123"

    def test_skip_reply_when_risk_in_detail(self, fb_env):
        """_open_and_read_conversation 返回 risk 信号 → 不回。"""
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}]
        knobs["read_results"]["Alice"] = {
            "peer_name": "Alice", "incoming_text": "msg",
            "risk": "login_challenge",
        }
        stats = fb.check_message_requests(auto_reply=True, max_requests=5)
        assert len(calls) == 0  # 没调 _ai_reply_and_send
        assert stats["replies_sent"] == 0

    def test_empty_incoming_skipped(self, fb_env):
        """incoming_text 空则不回 (Sprint 2 契约: 只统计有内容的)。"""
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}]
        knobs["read_results"]["Alice"] = {
            "peer_name": "Alice", "incoming_text": "",
        }
        stats = fb.check_message_requests(auto_reply=True, max_requests=5)
        assert stats["messages_collected"] == 0
        assert len(calls) == 0


# ─── 错误路径 ───────────────────────────────────────────────────────────────

class TestErrorPaths:
    def test_entry_not_found_returns_early(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["smart_tap_results"]["Message Requests entry"] = False
        knobs["entry_found"] = False
        stats = fb.check_message_requests()
        assert stats["opened"] is False
        assert "error" in stats

    def test_risk_dialog_aborts(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["risk_dialog"] = (True, "login challenge")
        stats = fb.check_message_requests()
        assert stats["opened"] is True
        assert stats["risk_detected"] == "login challenge"

    def test_single_conv_error_counted(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}, {"name": "Bob"}]
        with patch.object(fb, "_open_and_read_conversation",
                          side_effect=[RuntimeError("oops"),
                                       {"peer_name": "Bob", "incoming_text": "hi"}]):
            stats = fb.check_message_requests(auto_reply=True)
        assert stats["errors"] == 1
        # Bob 仍被处理
        assert stats["messages_collected"] == 1


# ─── playbook 覆写 ──────────────────────────────────────────────────────────

class TestPlaybookOverrides:
    def test_max_requests_from_phase_cfg(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["phase_cfg"] = ("cold_start", {"max_requests": 3})
        knobs["requests_list"] = [{"name": f"p{i}"} for i in range(8)]
        stats = fb.check_message_requests()  # max_requests=20 默认
        # playbook 里 cold_start 下降到 3
        assert stats["max_requests"] == 3

    def test_auto_reply_stranger_playbook_override(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["phase_cfg"] = ("cold_start", {"auto_reply_stranger": False})
        knobs["requests_list"] = [{"name": "Alice"}]
        # 调用时默认 auto_reply=True,但 playbook 压成 False
        stats = fb.check_message_requests()
        assert stats["auto_reply"] is False
        assert len(calls) == 0


# ─── peer_type 传递性测试 ───────────────────────────────────────────────────

class TestPeerTypePropagation:
    def test_ai_reply_and_send_default_peer_type_is_friend(self):
        """_ai_reply_and_send 默认 peer_type='friend' — 不传参时走 friend 语义。"""
        import inspect
        from src.app_automation.facebook import FacebookAutomation
        sig = inspect.signature(FacebookAutomation._ai_reply_and_send)
        assert sig.parameters["peer_type"].default == "friend"

    def test_check_message_requests_explicitly_passes_stranger(self, fb_env):
        fb, knobs, calls = fb_env
        knobs["requests_list"] = [{"name": "Alice"}, {"name": "Bob"}]
        fb.check_message_requests(auto_reply=True)
        for call in calls:
            assert call["peer_type"] == "stranger", \
                "check_message_requests 必须传 peer_type='stranger'"
