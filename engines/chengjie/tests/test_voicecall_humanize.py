# -*- coding: utf-8 -*-
"""通话拟人编排 + 观测门禁：Humanizer（事件驱动+tick 决策）与 CallStats（计数/prom/reset）。

均为确定性纯逻辑（注入 now，不依赖真实 sleep）。
"""
import asyncio

from src.voicecall.call_stats import CallStats
from src.voicecall.core import CallsConfig
from src.voicecall.humanize import Humanizer


def _cfg(**human):
    base = {"telegram_calls": {"enabled": True, "humanize": {}}}
    base["telegram_calls"]["humanize"].update(human)
    return CallsConfig.from_config(base)


# ── Humanizer：思考填充 ──────────────────────────────────────────────────────
def test_humanizer_filler_fires_during_thinking_gap():
    fired_log = []

    async def _emit_filler():
        fired_log.append("f")
        return True

    h = Humanizer(_cfg(filler_after_ms=700, filler_min_gap_ms=2500),
                  emit_filler=_emit_filler)
    # 用户说完 → 开始等回复
    h.on_event({"type": "transcript.user", "text": "在吗"}, now=100.0)
    # 300ms 未到阈值，不填充
    assert asyncio.run(h.tick(100.3)) == []
    # 800ms 超阈值 → 填充
    assert asyncio.run(h.tick(100.8)) == ["filler"]
    assert h.filler_count == 1
    # 回复音频开始 → 本轮不再填充
    h.on_event({"type": "output_audio", "audio_b64": "AA=="}, now=101.0)
    assert asyncio.run(h.tick(101.5)) == []


def test_humanizer_no_filler_provider_silent():
    # 未注入 emit_filler → 填充能力静默关（无预渲染资产时的正确降级）
    h = Humanizer(_cfg(), emit_filler=None)
    h.on_event({"type": "transcript.user", "text": "x"}, now=0.0)
    assert asyncio.run(h.tick(5.0)) == []
    assert h.filler_count == 0


# ── Humanizer：倾听反馈 ──────────────────────────────────────────────────────
def test_humanizer_backchannel_during_long_speech():
    async def _emit_bc():
        return True

    h = Humanizer(_cfg(backchannel_after_sec=3.5, backchannel_gap_sec=4.0,
                       backchannel_max_per_turn=3), emit_backchannel=_emit_bc)
    h.on_user_speech_start(now=0.0)
    assert asyncio.run(h.tick(2.0)) == []          # 未到 after
    assert asyncio.run(h.tick(4.0)) == ["backchannel"]
    assert h.backchannel_count == 1


def test_humanizer_backchannel_suppressed_while_reply_active():
    async def _emit_bc():
        return True

    h = Humanizer(_cfg(), emit_backchannel=_emit_bc)
    h.on_user_speech_start(now=0.0)
    # 我方正在出声（reply_active）→ 即便对方还在说也不插话（插话=打断）
    h.on_event({"type": "output_audio", "audio_b64": "AA=="}, now=1.0)
    assert asyncio.run(h.tick(5.0)) == []
    assert h.backchannel_count == 0


def test_humanizer_emit_failure_does_not_count():
    async def _bad_emit():
        raise RuntimeError("tts down")

    h = Humanizer(_cfg(filler_after_ms=100), emit_filler=_bad_emit)
    h.on_event({"type": "transcript.user", "text": "x"}, now=0.0)
    fired = asyncio.run(h.tick(1.0))               # emit 抛异常 → 安全退化
    assert fired == []
    assert h.filler_count == 0                     # 未真正出声不计数


# ── CallStats ───────────────────────────────────────────────────────────────
def test_call_stats_decision_and_rates():
    s = CallStats()
    s.incoming(); s.incoming(); s.incoming()
    s.decided("accept", "ok")
    s.decided("decline_compensate", "low_intimacy", compensated=True)
    s.decided("decline_silent", "stranger")
    d = s.dump()
    assert d["attempts"] == 3
    assert d["accepted"] == 1
    assert d["declined"] == 2
    assert d["compensated"] == 1
    assert d["by_decline_reason"]["low_intimacy"] == 1
    assert d["by_decline_reason"]["stranger"] == 1
    assert d["accept_rate"] == round(1 / 3, 4)


def test_call_stats_connected_duration_and_peak():
    s = CallStats()
    s.connected(); s.connected()
    assert s.dump()["active"] == 2
    assert s.dump()["peak_active"] == 2
    s.ended("normal", was_connected=True, duration_sec=30.0)
    s.ended("normal", was_connected=True, duration_sec=90.0)
    d = s.dump()
    assert d["active"] == 0
    assert d["avg_duration_sec"] == 60.0
    assert d["max_duration_sec"] == 90.0
    assert d["by_end_reason"]["normal"] == 2


def test_call_stats_humanize_and_safety():
    s = CallStats()
    s.humanize(filler=3, backchannel=2)
    s.humanize(filler=1)
    s.safety_escalation("severe")
    s.safety_escalation("elevated")
    s.safety_escalation("bogus")          # 白名单外，忽略
    d = s.dump()
    assert d["filler_count"] == 4
    assert d["backchannel_count"] == 2
    assert d["safety_escalations"] == {"elevated": 1, "severe": 1}


def test_call_stats_prom_and_reset():
    s = CallStats()
    s.incoming()
    s.decided("decline_silent", "stranger")
    s.safety_escalation("severe")
    prom = s.dump_prom()
    assert "tg_call_attempts_total 1" in prom
    assert 'tg_call_declined_total{reason="stranger"} 1' in prom
    assert 'tg_call_safety_escalation_total{level="severe"} 1' in prom
    s.reset()
    assert s.dump()["attempts"] == 0
    assert s.dump()["by_decline_reason"] == {}
