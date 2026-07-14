# -*- coding: utf-8 -*-
"""customer_sync_bridge 单测 — mock central_push_client, 验证 facebook 事件
正确翻译为 push 调用 (canonical_id / event_type / channel 映射).

bridge 设计是 fire_and_forget + 全 try/except 静默, 单测要确认:
1. canonical_source = "facebook_name", canonical_id = f"{device}::{peer}"
2. friend_request status=sent → event_type=friend_request_sent
3. friend_request status=risk → event_type=friend_request_risk
4. greeting_sent fallback=False → event_type=greeting_sent + record_chat outgoing
5. greeting_sent fallback=True → event_type=greeting_fallback
6. push_client 内部抛异常时 bridge 静默返 None (不影响 caller)
7. 空 device_id / peer_name 直接早退
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.host import customer_sync_bridge as bridge


@pytest.fixture
def mock_pc(monkeypatch):
    """mock central_push_client 模块的 upsert / record_event / record_chat."""
    pc = MagicMock()
    pc.upsert_customer = MagicMock(return_value="cust-uuid-mocked")
    pc.record_event = MagicMock(return_value=None)
    pc.record_chat = MagicMock(return_value=None)

    # bridge 用 lazy import (`from src.host.central_push_client import ...`),
    # 所以要 patch 模块本身的属性.
    import src.host.central_push_client as _cp
    monkeypatch.setattr(_cp, "upsert_customer", pc.upsert_customer)
    monkeypatch.setattr(_cp, "record_event", pc.record_event)
    monkeypatch.setattr(_cp, "record_chat", pc.record_chat)
    # 强制 worker_id 稳定可断言
    monkeypatch.setattr(bridge, "_safe_worker_id", lambda: "w-test")
    return pc


# ── canonical_id 约定 ────────────────────────────────────────────────
def test_canonical_id_composes_device_and_peer():
    cid = bridge._build_canonical_id("d-abc", "Alice")
    assert cid == "d-abc::Alice"


# ── friend_request_sent ──────────────────────────────────────────────
def test_friend_request_sent_calls_upsert_then_event(mock_pc):
    cid = bridge.sync_friend_request_sent(
        "d1", "Alice",
        status="sent",
        persona_key="hostess_jp",
        preset_key="growth_v2",
        source="search",
        note="hi there",
    )
    assert cid == "cust-uuid-mocked"

    mock_pc.upsert_customer.assert_called_once()
    upsert_kwargs = mock_pc.upsert_customer.call_args.kwargs
    assert upsert_kwargs["canonical_source"] == "facebook_name"
    assert upsert_kwargs["canonical_id"] == "d1::Alice"
    assert upsert_kwargs["primary_name"] == "Alice"
    assert upsert_kwargs["worker_id"] == "w-test"
    assert upsert_kwargs["device_id"] == "d1"
    assert upsert_kwargs["fire_and_forget"] is True
    # Phase-3 加了 ab_variant 自动注入, 验 persona_key 仍存在 + ab_variant 是 v1/v2
    assert upsert_kwargs["ai_profile"]["persona_key"] == "hostess_jp"
    assert upsert_kwargs["ai_profile"]["ab_variant"] in ("v1", "v2")

    mock_pc.record_event.assert_called_once()
    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "friend_request_sent"
    assert evt_kwargs["customer_id"] == "cust-uuid-mocked"
    assert evt_kwargs["device_id"] == "d1"
    assert evt_kwargs["worker_id"] == "w-test"
    assert evt_kwargs["meta"]["persona_key"] == "hostess_jp"
    assert evt_kwargs["meta"]["has_note"] is True


def test_friend_request_risk_status_maps_to_risk_event(mock_pc):
    bridge.sync_friend_request_sent("d1", "Bob", status="risk")
    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "friend_request_risk"


def test_friend_request_no_persona_key_no_ai_profile(mock_pc):
    bridge.sync_friend_request_sent("d1", "Carol", status="sent")
    upsert_kwargs = mock_pc.upsert_customer.call_args.kwargs
    # Phase-3: ai_profile 不再可能为 None (ab_variant 始终注入), 只验没 persona_key
    profile = upsert_kwargs["ai_profile"] or {}
    assert "persona_key" not in profile or not profile.get("persona_key")
    assert profile.get("ab_variant") in ("v1", "v2")


# ── greeting_sent ────────────────────────────────────────────────────
def test_greeting_sent_writes_event_and_chat(mock_pc):
    cid = bridge.sync_greeting_sent(
        "d1", "Alice",
        greeting="こんにちは!",
        template_id="jp:3",
        preset_key="growth_v2",
        persona_key="hostess_jp",
        phase="warmup",
    )
    assert cid == "cust-uuid-mocked"

    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "greeting_sent"
    assert evt_kwargs["meta"]["template_id"] == "jp:3"
    assert evt_kwargs["meta"]["msg_len"] == len("こんにちは!")

    chat_kwargs = mock_pc.record_chat.call_args.kwargs
    assert chat_kwargs["channel"] == "facebook"
    assert chat_kwargs["direction"] == "outgoing"
    assert chat_kwargs["content"] == "こんにちは!"
    assert chat_kwargs["template_id"] == "jp:3"
    assert chat_kwargs["ai_generated"] is False
    assert chat_kwargs["device_id"] == "d1"


def test_greeting_fallback_uses_fallback_event(mock_pc):
    bridge.sync_greeting_sent(
        "d1", "Alice", greeting="hi", fallback=True,
    )
    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "greeting_fallback"


# ── 早退保护 ─────────────────────────────────────────────────────────
def test_empty_device_id_early_returns_none(mock_pc):
    assert bridge.sync_friend_request_sent("", "Alice") is None
    assert bridge.sync_greeting_sent("", "Alice", greeting="hi") is None
    mock_pc.upsert_customer.assert_not_called()


def test_empty_peer_name_early_returns_none(mock_pc):
    assert bridge.sync_friend_request_sent("d1", "") is None
    assert bridge.sync_greeting_sent("d1", "", greeting="hi") is None
    mock_pc.upsert_customer.assert_not_called()


def test_empty_greeting_early_returns_none(mock_pc):
    assert bridge.sync_greeting_sent("d1", "Alice", greeting="") is None
    mock_pc.upsert_customer.assert_not_called()


# ── 异常静默 ─────────────────────────────────────────────────────────
def test_upsert_exception_returns_none_no_event(monkeypatch):
    """upsert 抛异常: bridge 静默返 None, 后续 record_event 不调."""
    import src.host.central_push_client as _cp

    def _raise(**_):
        raise RuntimeError("network down")

    monkeypatch.setattr(_cp, "upsert_customer", _raise)
    record_event_calls = []
    monkeypatch.setattr(
        _cp, "record_event",
        lambda **kw: record_event_calls.append(kw),
    )

    result = bridge.sync_friend_request_sent("d1", "Alice", status="sent")
    assert result is None
    assert record_event_calls == []


def test_record_event_exception_does_not_propagate(monkeypatch):
    """upsert OK 但 record_event 抛异常: bridge 静默, 仍返回 customer_id."""
    import src.host.central_push_client as _cp
    monkeypatch.setattr(_cp, "upsert_customer", lambda **kw: "cust-x")
    monkeypatch.setattr(
        _cp, "record_event",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    cid = bridge.sync_friend_request_sent("d1", "Alice", status="sent")
    assert cid == "cust-x"


# ── messenger 入站 ───────────────────────────────────────────────────
def test_messenger_incoming_writes_event_chat_and_upgrades_status(mock_pc):
    cid = bridge.sync_messenger_incoming(
        "d1", "Alice",
        content="こんにちは!",
        peer_type="stranger",
    )
    assert cid == "cust-uuid-mocked"

    upsert_kwargs = mock_pc.upsert_customer.call_args.kwargs
    assert upsert_kwargs["status"] == "in_messenger"

    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "message_received"
    assert evt_kwargs["meta"]["peer_type"] == "stranger"

    chat_kwargs = mock_pc.record_chat.call_args.kwargs
    assert chat_kwargs["channel"] == "messenger"
    assert chat_kwargs["direction"] == "incoming"
    assert chat_kwargs["ai_generated"] is False


def test_messenger_incoming_auto_lang_detect(mock_pc, monkeypatch):
    """content_lang 不传时调 detect_language."""
    monkeypatch.setattr(bridge, "_detect_lang_safe", lambda t: "ja")
    bridge.sync_messenger_incoming("d1", "Alice", content="こんにちは")
    chat_kwargs = mock_pc.record_chat.call_args.kwargs
    assert chat_kwargs["content_lang"] == "ja"


def test_messenger_incoming_explicit_lang_overrides_detect(mock_pc, monkeypatch):
    """content_lang 显式传入时不调 detect_language."""
    detect_called = []
    monkeypatch.setattr(
        bridge, "_detect_lang_safe",
        lambda t: detect_called.append(t) or "WRONG",
    )
    bridge.sync_messenger_incoming(
        "d1", "Alice", content="hi", content_lang="zh",
    )
    chat_kwargs = mock_pc.record_chat.call_args.kwargs
    assert chat_kwargs["content_lang"] == "zh"
    assert detect_called == []  # 显式 lang 走短路


def test_messenger_incoming_empty_content_early_return(mock_pc):
    assert bridge.sync_messenger_incoming("d1", "Alice", content="") is None
    mock_pc.upsert_customer.assert_not_called()


# ── messenger 出站 ───────────────────────────────────────────────────
def test_messenger_outgoing_ai_reply_default_ai_generated_true(mock_pc):
    cid = bridge.sync_messenger_outgoing(
        "d1", "Alice",
        content="今晩はカレーにする?",
        ai_decision="reply",
        intent_tag="invite_dinner",
    )
    assert cid == "cust-uuid-mocked"

    upsert_kwargs = mock_pc.upsert_customer.call_args.kwargs
    assert upsert_kwargs["status"] == "in_messenger"

    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "messenger_message_sent"
    assert evt_kwargs["meta"]["ai_decision"] == "reply"
    assert evt_kwargs["meta"]["ai_generated"] is True
    assert evt_kwargs["meta"]["intent_tag"] == "invite_dinner"

    chat_kwargs = mock_pc.record_chat.call_args.kwargs
    assert chat_kwargs["direction"] == "outgoing"
    assert chat_kwargs["ai_generated"] is True


def test_messenger_outgoing_template_path_ai_generated_false(mock_pc):
    """模板/snippet 回复 ai_generated=False."""
    bridge.sync_messenger_outgoing(
        "d1", "Alice",
        content="hi from template",
        ai_decision="reply",
        ai_generated=False,
        template_id="jp:5",
    )
    chat_kwargs = mock_pc.record_chat.call_args.kwargs
    assert chat_kwargs["ai_generated"] is False
    assert chat_kwargs["template_id"] == "jp:5"


# ── wa_referral ──────────────────────────────────────────────────────
def test_wa_referral_does_not_upgrade_status(mock_pc):
    """wa_referral 只记 event, 不升级 status (handoff_to_line 才升)."""
    bridge.sync_wa_referral_sent(
        "d1", "Alice",
        channel="line",
        content="加我 LINE: xxx",
        intent_tag="referral_line",
    )
    upsert_kwargs = mock_pc.upsert_customer.call_args.kwargs
    assert upsert_kwargs["status"] is None  # 不传 status

    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "wa_referral_sent"
    assert evt_kwargs["meta"]["channel"] == "line"
    assert evt_kwargs["meta"]["intent_tag"] == "referral_line"

    # 不写 chat (chat 已由 messenger_outgoing 写过)
    mock_pc.record_chat.assert_not_called()


# ── handoff_to_line ──────────────────────────────────────────────────
def test_handoff_to_line_upgrades_status_and_calls_initiate(mock_pc, monkeypatch):
    import src.host.central_push_client as _cp
    monkeypatch.setattr(_cp, "initiate_handoff", lambda **kw: "ho-uuid-mocked")

    hid = bridge.sync_handoff_to_line(
        "d1", "Alice",
        ai_summary="persona=hostess_jp | last_in: 加我LINE | last_out: 我的ID...",
    )
    assert hid == "ho-uuid-mocked"

    upsert_kwargs = mock_pc.upsert_customer.call_args.kwargs
    assert upsert_kwargs["status"] == "in_line"


def test_handoff_to_line_empty_ai_summary_early_return(mock_pc):
    """ai_summary 必填 — 没传不发起 handoff."""
    assert bridge.sync_handoff_to_line("d1", "Alice", ai_summary="") is None
    mock_pc.upsert_customer.assert_not_called()


# ── build_simple_summary ─────────────────────────────────────────────
def test_build_simple_summary_full_fields():
    s = bridge.build_simple_summary(
        persona_key="hostess_jp",
        intent_tag="invite_line",
        last_incoming="LINE 教えて",
        last_outgoing="OK!",
    )
    assert "persona=hostess_jp" in s
    assert "intent=invite_line" in s
    assert "last_in: LINE 教えて" in s
    assert "last_out: OK!" in s


def test_build_simple_summary_truncates_snippet():
    long_text = "x" * 500
    s = bridge.build_simple_summary(last_incoming=long_text, max_snippet=80)
    assert "x" * 80 in s
    assert "x" * 81 not in s


def test_build_simple_summary_empty_fields_returns_no_context():
    assert bridge.build_simple_summary() == "(no context)"


# ── lang_detect 故障静默 ─────────────────────────────────────────────
def test_detect_lang_safe_returns_none_on_exception(monkeypatch):
    """detect_language 抛异常时 _detect_lang_safe 静默返 None."""
    import src.ai.lang_detect as _ld
    monkeypatch.setattr(
        _ld, "detect_language",
        lambda t: (_ for _ in ()).throw(RuntimeError("oops")),
    )
    assert bridge._detect_lang_safe("any") is None


def test_detect_lang_safe_empty_text_returns_none():
    assert bridge._detect_lang_safe("") is None
    assert bridge._detect_lang_safe(None) is None  # type: ignore
