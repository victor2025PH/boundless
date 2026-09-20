"""发送闸门拦截可见性（2026-07-22 用户指令：限制必须有提示）单测。

真机事故：warmup_cap 静默拦下全部 WhatsApp 回复，运营排查半天才发现是限流。
铁律：任何 send_gate 拦截必须 (a) 不喂熔断 (b) 发告警（event_bus + ops_alert）
(c) 会话打「需人工」标签（send_error 已在 HANDOFF_REASONS）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.integrations import protocol_autoreply as pa


class _FakeRegistry:
    def get(self, platform, account_id):
        return {"platform": platform, "account_id": account_id,
                "meta": {"auto_reply": True, "persona_id": ""}}


def _payload(text="在吗", chat_key="100"):
    return {"platform": "whatsapp", "account_id": "639", "chat_key": chat_key,
            "text": text, "direction": "in"}


_CFG = {"protocol_autoreply": {"enabled": True}}


@pytest.fixture(autouse=True)
def _clear_state():
    pa._last_reply.clear()
    yield
    pa._last_reply.clear()


@pytest.mark.asyncio
async def test_send_gate_block_carries_error_and_skips_breaker():
    """send_gate_blocked 异常：res.error 带出前缀 + 不喂熔断计数。"""
    limiter = MagicMock()
    limiter.allow.return_value = (True, "ok")

    async def _gen(**kw):
        return "好的，马上回你～"

    async def _send(**kw):
        raise RuntimeError("send_gate_blocked:warmup_cap")

    res = await pa.run_autoreply(
        _payload(chat_key="vis-1"), registry=_FakeRegistry(), cfg=_CFG,
        generate=_gen, send=_send, risk_fn=lambda t: "low",
        limiter=limiter, now=9000.0,
    )
    assert res["reason"] == "send_error"
    assert str(res.get("error") or "").startswith("send_gate_blocked")
    limiter.record_failure.assert_not_called()  # 限流≠故障，绝不喂熔断
    assert pa.needs_handoff(res), "闸门拦截必须转人工（打需人工标签）"


@pytest.mark.asyncio
async def test_infra_send_error_still_feeds_breaker():
    """普通基础设施发送失败：熔断计数照旧（行为不回归）。"""
    limiter = MagicMock()
    limiter.allow.return_value = (True, "ok")
    limiter.record_failure.return_value = False

    async def _gen(**kw):
        return "好的"

    async def _send(**kw):
        raise RuntimeError("connection reset")

    res = await pa.run_autoreply(
        _payload(chat_key="vis-2"), registry=_FakeRegistry(), cfg=_CFG,
        generate=_gen, send=_send, risk_fn=lambda t: "low",
        limiter=limiter, now=9100.0,
    )
    assert res["reason"] == "send_error"
    limiter.record_failure.assert_called_once()


def test_publish_alert_forwards_to_ops_alert():
    """publish_alert 双通道：event_bus + ops_alert（TG 中继）。"""
    pa._alert_seen.clear()
    with patch("src.ops.ops_alert.notify") as mock_ops:
        sent = pa.publish_alert(
            "send_gate_blocked",
            {"platform": "whatsapp", "account_id": "639"},
            "发送被限流闸门拦截")
    assert sent
    mock_ops.assert_called_once()
    kind = mock_ops.call_args[0][0]
    assert kind == "send_gate_blocked"


def test_publish_alert_debounce_still_works():
    pa._alert_seen.clear()
    payload = {"platform": "whatsapp", "account_id": "639"}
    with patch("src.ops.ops_alert.notify"):
        assert pa.publish_alert("send_gate_blocked", payload, "x", now=1000.0)
        assert not pa.publish_alert("send_gate_blocked", payload, "x", now=1100.0)


def test_send_guard_notify_send_blocked_dual_channel():
    from src.integrations.shared import send_guard as sg
    pa._alert_seen.clear()
    with patch("src.ops.ops_alert.notify") as mock_ops, \
         patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
        sg.notify_send_blocked("whatsapp", "639", "send_gate:warmup_cap")
    assert mock_bus.called or mock_ops.called
    # ops_alert 至少被调用一次（publish_alert 内部一次 + notify_send_blocked 直调一次，
    # 各自有独立防抖；断言不锁死次数，只锁"有推送"）
    assert mock_ops.call_count >= 1
