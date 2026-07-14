# -*- coding: utf-8 -*-
"""通话用量存储 + 上下文组装器门禁：
- CallUsageStore：滚动 24h 次数/分钟计数、跨窗口衰减、空键/异常安全；
- assemble_call_context：缺信号保守默认、陌生人判定、各 lookup 接线正确。
"""
from src.voicecall.call_usage_store import CallUsageStore
from src.voicecall.wiring import (
    assemble_call_context,
    call_account_key,
    call_memory_key,
)


# ── CallUsageStore ──────────────────────────────────────────────────────────
def test_usage_records_and_counts():
    s = CallUsageStore(":memory:")
    t = 1_000_000.0
    s.record_call("telegram:acc1", 120.0, now=t)          # 2 min
    s.record_call("telegram:acc1", 180.0, now=t + 100)    # 3 min
    calls, minutes = s.usage_since("telegram:acc1", t - 10)
    assert calls == 2
    assert minutes == 5.0                                  # 120+180s = 5min


def test_usage_rolling_24h_window():
    s = CallUsageStore(":memory:")
    now = 2_000_000.0
    s.record_call("telegram:acc1", 60.0, now=now - 86400 - 100)   # 25h 前，窗外
    s.record_call("telegram:acc1", 60.0, now=now - 3600)          # 1h 前，窗内
    calls, minutes = s.usage_today("telegram:acc1", now=now)
    assert calls == 1                                     # 只数窗内
    assert minutes == 1.0


def test_usage_per_account_isolation():
    s = CallUsageStore(":memory:")
    t = 1_000_000.0
    s.record_call("telegram:a", 60.0, now=t)
    s.record_call("telegram:b", 120.0, now=t)
    assert s.usage_today("telegram:a", now=t)[0] == 1
    assert s.usage_today("telegram:b", now=t)[1] == 2.0


def test_usage_empty_key_and_missing():
    s = CallUsageStore(":memory:")
    s.record_call("", 60.0)                               # 空键 → 不记
    assert s.usage_today("telegram:none")[0] == 0
    assert s.usage_since("", 0)[0] == 0


# ── assemble_call_context ───────────────────────────────────────────────────
def test_keys_helpers():
    assert call_account_key("telegram", "acc1") == "telegram:acc1"
    assert call_memory_key("telegram", "555") == "telegram:555"


def test_assemble_full_signals():
    ctx = assemble_call_context(
        555, "acc1", platform="telegram",
        conversation_lookup=lambda a, c: {"language": "en", "automation_mode": "auto_ai",
                                          "intimacy": 72, "has_conversation": True,
                                          "peer_known": True},
        usage_lookup=lambda k: (3, 12.5),
        account_light_lookup=lambda k: "amber",
        kill_switch_lookup=lambda p, a: False,
        memory_lookup=lambda k: "喜欢猫\n在深圳",
        host_warm=True, hour=15, concurrent_active=0)
    assert ctx.chat_id == 555 and ctx.account_id == "acc1"
    assert ctx.conversation_language == "en"
    assert ctx.intimacy == 72
    assert ctx.has_conversation is True and ctx.peer_known is True
    assert ctx.calls_today == 3 and ctx.minutes_today == 12.5
    assert ctx.account_light == "amber"
    assert "喜欢猫" in ctx.memory_bullets


def test_assemble_no_conversation_is_stranger():
    # 查无会话 → has_conversation/peer_known 全 False（decide 走静默拒接）
    ctx = assemble_call_context(999, "acc1", conversation_lookup=lambda a, c: None)
    assert ctx.has_conversation is False
    assert ctx.peer_known is False


def test_assemble_missing_lookups_safe_defaults():
    # 全部 lookup 缺省 → 保守默认（不因缺信号崩，也不误判）
    ctx = assemble_call_context(1, "acc1")
    assert ctx.calls_today == 0 and ctx.minutes_today == 0.0
    assert ctx.account_light == "green"
    assert ctx.kill_switch_frozen is False
    assert ctx.memory_bullets == ""
    assert ctx.has_conversation is False          # 无 conversation_lookup → 陌生人


def test_assemble_lookup_exception_degrades():
    # 某个 lookup 抛异常 → 该信号退默认，其余照常组装（不整体崩）
    def _boom(*a):
        raise RuntimeError("db down")
    ctx = assemble_call_context(
        7, "acc1",
        conversation_lookup=lambda a, c: {"has_conversation": True, "peer_known": True,
                                          "intimacy": 50},
        usage_lookup=_boom, account_light_lookup=_boom, kill_switch_lookup=_boom)
    assert ctx.has_conversation is True           # 会话信号正常
    assert ctx.calls_today == 0                    # 用量 lookup 崩 → 保守 0
    assert ctx.account_light == "green"            # 健康 lookup 崩 → 保守 green
    assert ctx.kill_switch_frozen is False         # kill lookup 崩 → 保守不冻结


def test_assemble_kill_switch_frozen():
    ctx = assemble_call_context(
        3, "acc1",
        conversation_lookup=lambda a, c: {"has_conversation": True, "peer_known": True},
        kill_switch_lookup=lambda p, a: True)
    assert ctx.kill_switch_frozen is True
