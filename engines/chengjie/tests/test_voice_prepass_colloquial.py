"""分条语音「整段只口语化一次」+ text-first 防错序（2026-07-27 实测事故驱动）。

背景（真机 16:28 实录）：A 线分条语音逐条各打一次 LLM 口语化（云端一次往返 ~5s ×N），
2 条即把整链推过 text-first 25s 预算 → 客户先收到占位文字；更糟的是占位文字发在
**第一条语音已送达之后**（语音→「收到收到，马上回你」→语音），语义彻底错乱。

本文件守两条不变量：
1. ``TTSPipeline.prepass_colloquial_llm``：整段改写一次；不满足条件/失败一律 None
   （调用方回落旧链逐条改写，行为不劣于旧版）。
2. ``skip_llm_colloquial`` 透传 + 分条不再逐条打 LLM；``already_sent`` 为真时
   text-first 不再补占位文字。
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from src.ai.tts_pipeline import TTSPipeline
from src.client.sender import TelegramSenderMixin

_LOG = logging.getLogger("test_voice_prepass")
_SRC = "因此我认为今天的安排无需调整您可以按原计划过来"
_SPEC = SimpleNamespace(emotion="warm", intensity=0.6)


def _cfg(**col):
    """avatar_clone 人设 + colloquial 配置（默认 llm 档、已开）。"""
    base = {"enabled": True, "mode": "llm", "min_chars": 12,
            "rewrite_intensity": "vivid", "provider": "cloud"}
    base.update(col)
    return {
        "enabled": True, "backend": "avatar_clone",
        "avatar_voice": {"enabled": True, "colloquial": base},
        "voice_profile": {"enabled": True, "owner_consent": True,
                          "backend": "avatar_clone", "instruct_style": "撒娇"},
    }


def _patch_llm(monkeypatch, out, spy=None):
    async def fake(text, **kwargs):
        if spy is not None:
            spy.append(kwargs)
        return out(text) if callable(out) else out

    import src.ai.voice_colloquial_llm as _m
    monkeypatch.setattr(_m, "llm_colloquialize", fake)


# ── 1. prepass 纯行为 ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_prepass_returns_rewritten_text(monkeypatch):
    spy = []
    _patch_llm(monkeypatch, "我觉得今天不用改啦，你照原来的时间过来就行～", spy)
    out = await TTSPipeline(_cfg()).prepass_colloquial_llm(_SRC, spec=_SPEC)
    assert out and out != _SRC
    assert len(spy) == 1                      # 整段只打一次
    assert spy[0]["intensity"] == "vivid"     # 力度/供应商如实透传
    assert spy[0]["provider"] == "cloud"
    assert spy[0]["lead"] is True


@pytest.mark.asyncio
async def test_prepass_passes_persona_style(monkeypatch):
    spy = []
    _patch_llm(monkeypatch, "改写后的口语文本内容够长了哦", spy)
    await TTSPipeline(_cfg()).prepass_colloquial_llm(_SRC, spec=_SPEC)
    assert "撒娇" in str(spy[0].get("style") or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("kw,spec", [
    ({}, None),                              # 无情绪档（口语化整链不启）
    ({"enabled": False}, _SPEC),             # 口语化关
    ({"mode": "rule"}, _SPEC),               # 规则档：免费，无需 prepass
])
async def test_prepass_gated_off(monkeypatch, kw, spec):
    spy = []
    _patch_llm(monkeypatch, "不该被用到的改写结果啊啊啊", spy)
    assert await TTSPipeline(_cfg(**kw)).prepass_colloquial_llm(
        _SRC, spec=spec) is None
    assert spy == []                          # 一次都不该打 LLM


@pytest.mark.asyncio
async def test_prepass_persona_opt_out(monkeypatch):
    spy = []
    _patch_llm(monkeypatch, "不该被用到的改写结果啊啊啊", spy)
    cfg = _cfg()
    cfg["voice_profile"]["colloquial"] = False
    assert await TTSPipeline(cfg).prepass_colloquial_llm(
        _SRC, spec=_SPEC) is None
    assert spy == []


@pytest.mark.asyncio
@pytest.mark.parametrize("out", [None, "", lambda t: t])
async def test_prepass_failure_or_noop_returns_none(monkeypatch, out):
    """LLM 失败/返空/原样返回 → None：调用方按旧链逐条改写。"""
    _patch_llm(monkeypatch, out)
    assert await TTSPipeline(_cfg()).prepass_colloquial_llm(
        _SRC, spec=_SPEC) is None


@pytest.mark.asyncio
async def test_prepass_never_raises(monkeypatch):
    async def boom(text, **kwargs):
        raise RuntimeError("cloud down")

    import src.ai.voice_colloquial_llm as _m
    monkeypatch.setattr(_m, "llm_colloquialize", boom)
    assert await TTSPipeline(_cfg()).prepass_colloquial_llm(
        _SRC, spec=_SPEC) is None


# ── 2. skip_llm_colloquial 透传契约 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_synthesize_passes_skip_llm_colloquial(monkeypatch):
    seen = {}

    async def fake_uncached(text, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            ok=True, audio_path="x.ogg", duration_sec=1.0, error="",
            provider="avatar_clone", voice="v", extra={}, latency_ms=1,
            text=text, duration_source="probe")

    tts = TTSPipeline(_cfg())
    monkeypatch.setattr(tts, "_synthesize_uncached", fake_uncached)
    monkeypatch.setattr(tts, "_maybe_apply_rvc", lambda rv: _ident(rv))
    monkeypatch.setattr(tts, "_maybe_apply_ambience", lambda rv: _ident(rv))
    await tts.synthesize("这是一句足够长的测试文本内容啊", skip_llm_colloquial=True)
    assert seen.get("skip_llm_colloquial") is True
    await tts.synthesize("这是另一句足够长的测试文本内容", skip_llm_colloquial=False)
    assert seen.get("skip_llm_colloquial") is False


async def _ident(rv):
    return rv


# ── 3. 分条：整段已改写 → 各条不打 LLM、不重复起头 ──────────────────────────
class _PartsHost:
    def __init__(self):
        self.logger = SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None)
        self.client = SimpleNamespace(send_chat_action=_anoop)
        self.config = SimpleNamespace(config={})
        self.mirrored = []
        self.sent_notes = 0

    async def _presend_pace(self):
        return None

    def _postsend_record_count(self):
        return None

    def _postsend_mirror_and_record(self, chat_id, text):
        self.mirrored.append(text)

    def _reply_to_message_id_for_send(self, msg):
        return 7

    async def _voice_recording_action(self, chat_id):
        return None

    async def _voice_recording_gap(self, chat_id, gap_sec):
        return None


async def _anoop(*a, **k):
    return None


def _fake_tts(tmp_path):
    calls = {"leads": [], "skips": [], "n": 0}

    async def synthesize(text, *, timeout_sec=30.0, emotion=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        calls["leads"].append(kwargs.get("colloquial_lead", True))
        calls["skips"].append(kwargs.get("skip_llm_colloquial", False))
        p = tmp_path / f"p{i}.ogg"
        p.write_bytes(b"OggS-part")
        return SimpleNamespace(
            ok=True, audio_path=str(p), duration_sec=3.0, error="",
            provider="hub_fish", extra={})

    return SimpleNamespace(synthesize=synthesize), calls


@pytest.mark.asyncio
async def test_parts_skip_llm_and_notify_progress(tmp_path, monkeypatch):
    import src.client.sender as _s
    monkeypatch.setattr(_s, "send_telegram_voice", _true_send, raising=False)
    import src.client.voice_sender as _vs
    monkeypatch.setattr(_vs, "send_telegram_voice", _true_send)

    host = _PartsHost()
    tts, calls = _fake_tts(tmp_path)
    notes = []
    msg = SimpleNamespace(chat=SimpleNamespace(id=777))
    ok = await TelegramSenderMixin._send_voice_reply_parts(
        host, msg, ["第一条内容", "第二条内容"], tts,
        {"emotion": None, "persona_id": "p1"}, {}, {},
        timeout_sec=5.0, skip_llm_colloquial=True,
        on_part_sent=lambda: notes.append(1))
    assert ok is True
    assert calls["skips"] == [True, True]     # 各条都不打 LLM
    assert calls["leads"] == [False, False]   # 整段已起头 → 不再逐条起头
    assert len(notes) == 2                    # 每条送达都通报进度


@pytest.mark.asyncio
async def test_parts_default_keeps_old_lead_semantics(tmp_path, monkeypatch):
    import src.client.voice_sender as _vs
    monkeypatch.setattr(_vs, "send_telegram_voice", _true_send)
    host = _PartsHost()
    tts, calls = _fake_tts(tmp_path)
    msg = SimpleNamespace(chat=SimpleNamespace(id=777))
    await TelegramSenderMixin._send_voice_reply_parts(
        host, msg, ["第一条内容", "第二条内容"], tts,
        {"emotion": None, "persona_id": "p1"}, {}, {}, timeout_sec=5.0)
    assert calls["skips"] == [False, False]
    assert calls["leads"] == [True, False]    # 旧语义：仅首条起头


async def _true_send(*a, **k):
    return True


# ── 4. text-first：已发语音 → 不补占位文字（防错序）────────────────────────
class _Probe:
    def __init__(self):
        self.filler = 0
        self.fallback = 0

    async def send_filler(self):
        self.filler += 1

    async def send_fallback(self):
        self.fallback += 1


@pytest.mark.asyncio
async def test_no_filler_when_a_voice_part_already_sent():
    probe = _Probe()
    progress = {"sent": False}

    async def flow():
        progress["sent"] = True      # 第一条语音送达
        await asyncio.sleep(0.15)
        return True

    task = asyncio.create_task(flow())
    ok = await TelegramSenderMixin.race_voice_with_text_first(
        task, budget_sec=0.03, send_filler=probe.send_filler,
        send_fallback_text=probe.send_fallback, logger=_LOG,
        already_sent=lambda: progress["sent"])
    assert ok is True
    await asyncio.sleep(0.25)
    assert probe.filler == 0          # 关键：语音已在路上，不再插占位文字
    assert probe.fallback == 0


@pytest.mark.asyncio
async def test_filler_still_sent_when_nothing_sent_yet():
    probe = _Probe()

    async def flow():
        await asyncio.sleep(0.15)
        return True

    task = asyncio.create_task(flow())
    ok = await TelegramSenderMixin.race_voice_with_text_first(
        task, budget_sec=0.03, send_filler=probe.send_filler,
        send_fallback_text=probe.send_fallback, logger=_LOG,
        already_sent=lambda: False)
    assert ok is True
    await asyncio.sleep(0.25)
    assert probe.filler == 1          # 旧行为不变：一条语音都没发才补占位
    assert probe.fallback == 0


@pytest.mark.asyncio
async def test_already_sent_probe_exception_falls_back_to_filler():
    probe = _Probe()

    def boom():
        raise RuntimeError("probe broken")

    async def flow():
        await asyncio.sleep(0.15)
        return False

    task = asyncio.create_task(flow())
    ok = await TelegramSenderMixin.race_voice_with_text_first(
        task, budget_sec=0.03, send_filler=probe.send_filler,
        send_fallback_text=probe.send_fallback, logger=_LOG,
        already_sent=boom)
    assert ok is True
    await asyncio.sleep(0.25)
    assert probe.filler == 1          # 探测异常不许吞掉占位（保守回旧行为）
    assert probe.fallback == 1        # 语音最终失败 → 补发完整文字
