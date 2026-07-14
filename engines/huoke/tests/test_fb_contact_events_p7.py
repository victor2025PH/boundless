# -*- coding: utf-8 -*-
"""P7 INTEGRATION_CONTRACT §7.1 B 机 fb_contact_events 回写契约测试。

覆盖 3 个触发点 (add_friend_accepted 的 P1 append commit 单独测):
  * greeting_replied: _open_and_read_conversation 读到 incoming 时
  * message_received: check_messenger_inbox / check_message_requests loop 末尾
  * wa_referral_sent: _ai_reply_and_send decision='wa_referral' 发送成功后

依赖 A 的 Phase 5 (fb_store.record_contact_event + CONTACT_EVT_*) 未合入前
feature-detect 静默 skip。用 patch.dict 模拟 Phase 5 合入场景验写入。
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
from unittest.mock import MagicMock, patch

import pytest


# ─── _emit_contact_event_safe 基础 ──────────────────────────────────────────

class TestEmitContactEventSafe:
    def test_empty_args_noop(self):
        from src.app_automation import facebook as fb_mod
        # 空参数应立即返回 (不抛异常即通过)
        fb_mod._emit_contact_event_safe("", "Alice", "greeting_replied")
        fb_mod._emit_contact_event_safe("devA", "", "greeting_replied")
        fb_mod._emit_contact_event_safe("devA", "Alice", "")

    def test_phase5_not_merged_graceful(self):
        """record_contact_event 不存在时 (Phase 5 未 merge) 静默 skip。"""
        from src.app_automation import facebook as fb_mod
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if "record_contact_event" in str(kw.get("fromlist") or []):
                raise ImportError("not yet")
            if name == "src.host.fb_store" and "record_contact_event" in (kw.get("fromlist") or []):
                raise ImportError("not yet")
            return real_import(name, *a, **kw)

        # 更直接: mock fb_store 模块让 record_contact_event 缺席
        fake_fb_store = MagicMock(spec=[])  # 没有任何 attr
        with patch.dict("sys.modules", {"src.host.fb_store": fake_fb_store}):
            # 不抛异常即通过
            fb_mod._emit_contact_event_safe("devA", "Alice", "greeting_replied")

    def test_phase5_merged_calls_record(self):
        from src.app_automation import facebook as fb_mod
        fake_record = MagicMock()
        fake_fb_store = MagicMock()
        fake_fb_store.record_contact_event = fake_record
        with patch.dict("sys.modules", {"src.host.fb_store": fake_fb_store}):
            fb_mod._emit_contact_event_safe(
                "devA", "Alice", "greeting_replied",
                preset_key="jp_growth",
                meta={"via": "test"},
            )
        fake_record.assert_called_once()
        args, kwargs = fake_record.call_args
        assert args[0] == "devA"
        assert args[1] == "Alice"
        assert args[2] == "greeting_replied"
        assert kwargs["preset_key"] == "jp_growth"
        assert kwargs["meta"] == {"via": "test"}

    def test_phase5_record_raises_graceful(self):
        from src.app_automation import facebook as fb_mod
        fake_record = MagicMock(side_effect=RuntimeError("db offline"))
        fake_fb_store = MagicMock()
        fake_fb_store.record_contact_event = fake_record
        with patch.dict("sys.modules", {"src.host.fb_store": fake_fb_store}):
            # 不应抛异常
            fb_mod._emit_contact_event_safe("devA", "Alice", "greeting_replied")


# ─── _open_and_read_conversation greeting_replied 触发 ─────────────────────

class TestOpenConvGreetingReplied:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        fb.hb.tap = MagicMock()
        return fb

    def test_calls_mark_greeting_replied_back_when_incoming(self):
        fb = self._make_fb()
        fake_u2 = MagicMock()
        conv = {"name": "Alice", "bounds": (100, 200, 300, 400)}

        calls = []

        def _fake_mark(*a, **kw):
            calls.append({"args": a, "kwargs": kw})
            return 1

        fake_fb_store = MagicMock()
        fake_fb_store.record_inbox_message = MagicMock(return_value=1)
        fake_fb_store.mark_greeting_replied_back = _fake_mark

        with patch.object(fb, "_detect_risk_dialog",
                          return_value=(False, "")), \
             patch.object(fb, "_extract_latest_incoming_message",
                          return_value="hello there"), \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store}), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            result = fb._open_and_read_conversation(fake_u2, conv, "devA")

        assert result is not None
        assert result["incoming_text"] == "hello there"
        assert len(calls) == 1
        assert calls[0]["args"][0] == "devA"
        assert calls[0]["args"][1] == "Alice"
        assert calls[0]["kwargs"].get("window_days") == 7

    def test_skip_when_empty_incoming(self):
        fb = self._make_fb()
        fake_u2 = MagicMock()
        conv = {"name": "Alice", "bounds": (100, 200, 300, 400)}

        fake_fb_store = MagicMock()
        fake_fb_store.record_inbox_message = MagicMock(return_value=1)
        fake_fb_store.mark_greeting_replied_back = MagicMock()

        with patch.object(fb, "_detect_risk_dialog",
                          return_value=(False, "")), \
             patch.object(fb, "_extract_latest_incoming_message",
                          return_value=""), \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store}), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            fb._open_and_read_conversation(fake_u2, conv, "devA")

        fake_fb_store.mark_greeting_replied_back.assert_not_called()

    def test_p0_not_merged_graceful(self):
        """mark_greeting_replied_back 不存在时 (P0 未 merge) 静默 skip。"""
        fb = self._make_fb()
        fake_u2 = MagicMock()
        conv = {"name": "Alice", "bounds": (100, 200, 300, 400)}

        # fb_store 没有 mark_greeting_replied_back attr
        fake_fb_store = MagicMock(spec=["record_inbox_message"])
        fake_fb_store.record_inbox_message = MagicMock(return_value=1)

        with patch.object(fb, "_detect_risk_dialog",
                          return_value=(False, "")), \
             patch.object(fb, "_extract_latest_incoming_message",
                          return_value="hello"), \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store}), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            # 不抛异常即通过
            result = fb._open_and_read_conversation(fake_u2, conv, "devA")
        assert result is not None


# ─── check_messenger_inbox / check_message_requests message_received ──────

class TestCheckInboxMessageReceived:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        fb.hb.wait_think = MagicMock()
        return fb

    def test_messenger_inbox_writes_message_received_per_conv(self):
        fb = self._make_fb()
        events = []

        def _fake_record(device_id, peer_name, event_type, **kw):
            events.append({
                "device_id": device_id, "peer_name": peer_name,
                "event_type": event_type, **kw,
            })
            return 1

        fake_fb_store = MagicMock()
        fake_fb_store.record_contact_event = _fake_record

        fake_u2 = MagicMock()
        convs = [
            {"name": "Alice", "unread": True, "bounds": None},
            {"name": "Bob", "unread": True, "bounds": None},
        ]

        with patch.object(fb, "_did", return_value="devA"), \
             patch.object(fb, "_u2", return_value=fake_u2), \
             patch.object(fb, "_dismiss_dialogs"), \
             patch.object(fb, "_detect_risk_dialog",
                          return_value=(False, "")), \
             patch.object(fb, "_list_messenger_conversations",
                          return_value=convs), \
             patch.object(fb, "_open_and_read_conversation",
                          side_effect=lambda d, c, did, **kw:
                              {"peer_name": c["name"], "incoming_text": "hi"}), \
             patch.object(fb, "_ai_reply_and_send",
                          return_value=("reply text", "reply")), \
             patch("src.app_automation.facebook._resolve_phase_and_cfg",
                   return_value=("growth", {})), \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store}), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            fb.check_messenger_inbox(auto_reply=True, max_conversations=5,
                                     preset_key="jp_growth")

        mr_events = [e for e in events if e["event_type"] == "message_received"]
        assert len(mr_events) == 2
        names = {e["peer_name"] for e in mr_events}
        assert names == {"Alice", "Bob"}
        # decision = "reply" because _ai_reply_and_send returned reply
        for e in mr_events:
            assert e["meta"]["decision"] == "reply"
            assert e["preset_key"] == "jp_growth"

    def test_messenger_inbox_read_only_when_auto_reply_false(self):
        fb = self._make_fb()
        events = []
        fake_fb_store = MagicMock()
        fake_fb_store.record_contact_event = lambda *a, **kw: (
            events.append({"args": a, "kw": kw}) or 1)

        fake_u2 = MagicMock()

        with patch.object(fb, "_did", return_value="devA"), \
             patch.object(fb, "_u2", return_value=fake_u2), \
             patch.object(fb, "_dismiss_dialogs"), \
             patch.object(fb, "_detect_risk_dialog",
                          return_value=(False, "")), \
             patch.object(fb, "_list_messenger_conversations",
                          return_value=[{"name": "Alice", "unread": True,
                                         "bounds": None}]), \
             patch.object(fb, "_open_and_read_conversation",
                          return_value={"peer_name": "Alice",
                                        "incoming_text": "hi"}), \
             patch.object(fb, "_ai_reply_and_send") as m_ai, \
             patch("src.app_automation.facebook._resolve_phase_and_cfg",
                   return_value=("growth", {})), \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store}), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            fb.check_messenger_inbox(auto_reply=False, max_conversations=5)

        m_ai.assert_not_called()
        mr = [e for e in events
              if e["args"][2] == "message_received"]
        assert len(mr) == 1
        assert mr[0]["kw"]["meta"]["decision"] == "read_only"

    def test_message_requests_tags_peer_type_stranger(self):
        fb = self._make_fb()
        events = []
        fake_fb_store = MagicMock()
        fake_fb_store.record_contact_event = lambda *a, **kw: (
            events.append({"args": a, "kw": kw}) or 1)

        fake_u2 = MagicMock()
        fake_u2.app_start = MagicMock()
        fake_u2.press = MagicMock()

        with patch.object(fb, "_did", return_value="devA"), \
             patch.object(fb, "_u2", return_value=fake_u2), \
             patch.object(fb, "_dismiss_dialogs"), \
             patch.object(fb, "_detect_risk_dialog",
                          return_value=(False, "")), \
             patch.object(fb, "smart_tap", return_value=True), \
             patch.object(fb, "_open_message_requests_fallback",
                          return_value=True), \
             patch.object(fb, "_list_messenger_conversations",
                          return_value=[{"name": "Carol", "bounds": None}]), \
             patch.object(fb, "_open_and_read_conversation",
                          return_value={"peer_name": "Carol",
                                        "incoming_text": "hi"}), \
             patch.object(fb, "_ai_reply_and_send",
                          return_value=("r", "wa_referral")), \
             patch("src.app_automation.facebook._resolve_phase_and_cfg",
                   return_value=("growth", {})), \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store}), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            fb.check_message_requests(auto_reply=True, max_requests=5)

        mr = [e for e in events if e["args"][2] == "message_received"]
        assert len(mr) == 1
        meta = mr[0]["kw"]["meta"]
        assert meta["decision"] == "wa_referral"
        assert meta["peer_type"] == "stranger"


# ─── _ai_reply_and_send wa_referral_sent ─────────────────────────────────

class TestAiReplyWaReferralSent:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        fb.hb.tap = MagicMock()
        fb.hb.type_text = MagicMock()
        fb.hb.wait_think = MagicMock()
        return fb

    def _run_send(self, fb, decision, **kwargs):
        """Run _ai_reply_and_send with mocked ChatBrain + gate forced to decision."""
        fake_u2 = MagicMock()
        fake_el = MagicMock()
        fake_el.exists = MagicMock(return_value=True)
        fake_u2.__call__ = lambda **kw: fake_el
        fake_u2.return_value = fake_el

        mock_result = MagicMock()
        mock_result.message = "hello reply"
        mock_result.referral_score = 0.9

        fake_brain = MagicMock()
        fake_brain.generate_reply = MagicMock(return_value=mock_result)
        fake_brain_cls = MagicMock()
        fake_brain_cls.get_instance = MagicMock(return_value=fake_brain)

        # 强制 gate 决策 — 绕开 soft_score 对测试的干扰
        from src.ai.referral_gate import GateDecision
        fake_gate_decision = GateDecision(
            refer=(decision == "wa_referral"),
            level="hard_allow" if decision == "wa_referral" else "soft_fail",
            score=5 if decision == "wa_referral" else 0,
            threshold=3,
            reasons=["forced by test"],
        )

        events = []
        fake_fb_store = MagicMock()
        fake_fb_store.record_inbox_message = MagicMock(return_value=1)
        fake_fb_store.record_contact_event = lambda *a, **kw: (
            events.append({"args": a, "kw": kw}) or 1)

        stack = ExitStack()
        stack.enter_context(patch.object(fb, "_el_center",
                                         return_value=(100, 200)))
        stack.enter_context(patch.object(fb, "smart_tap",
                                         return_value=True))
        # Mock ChatBrain + gate
        stack.enter_context(patch.dict("sys.modules", {
            "src.ai.chat_brain": MagicMock(
                ChatBrain=fake_brain_cls,
                UserProfile=MagicMock(),
            ),
            "src.host.fb_store": fake_fb_store,
        }))
        stack.enter_context(patch("src.ai.referral_gate.should_refer",
                                  return_value=fake_gate_decision))
        # Mock persona metadata
        stack.enter_context(patch(
            "src.host.fb_target_personas.get_persona_display",
            return_value={"language": "ja", "short_label": "jp"},
            create=True,
        ))
        stack.enter_context(patch("src.app_automation.facebook.time.sleep"))
        stack.enter_context(patch("src.app_automation.facebook.random.uniform",
                                   return_value=0.0))
        try:
            with stack:
                reply, dec = fb._ai_reply_and_send(
                    fake_u2, "devA",
                    peer_name="Alice",
                    incoming_text="hi",
                    referral_contact=kwargs.get("referral_contact", "line:abc123"),
                    preset_key="jp_growth",
                    persona_key="jp_female_midlife",
                    peer_type=kwargs.get("peer_type", "friend"),
                )
        finally:
            pass
        return reply, dec, events

    def test_wa_referral_writes_event(self):
        fb = self._make_fb()
        reply, dec, events = self._run_send(fb, decision="wa_referral")
        wa_events = [e for e in events
                     if e["args"][2] == "wa_referral_sent"]
        assert len(wa_events) == 1
        e = wa_events[0]
        assert e["args"][0] == "devA"
        assert e["args"][1] == "Alice"
        assert e["kw"]["preset_key"] == "jp_growth"
        meta = e["kw"]["meta"]
        assert meta["channel"]  # 非空
        assert meta["peer_type"] == "friend"

    def test_reply_decision_does_not_write_wa_referral(self):
        fb = self._make_fb()
        reply, dec, events = self._run_send(fb, decision="reply")
        wa = [e for e in events if e["args"][2] == "wa_referral_sent"]
        assert wa == []
