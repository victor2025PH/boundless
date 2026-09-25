"""TTS 全链总预算门禁（2026-07-28 试听超时复盘）。

真机现象：坐席点「试听」→ 50s 后失败，app.log 只有一行
``[voice/tts-test] TTS error: ``（空）→ 日志巡检弹窗，无从排查。
根因＝链内各级预算之和（LLM 口语化 25s + hub 45+15s + 本机克隆 90s×2…）远超
调用方外闸 50s → 外层 ``wait_for`` 把协程掐死在不知道哪一级，TimeoutError str() 为空。

本文件守四条不变量：
1. **缺省零变化**：不传 ``total_budget_sec`` → 各级预算与旧链完全一致
   （B 线长文本慢 GPU 需要超预算跑完，见 6af80c3）。
2. **hub 按剩余预算收口**：剩余 <2s 直接跳过（省一次注定被掐死的往返）；
   strict 语义不因预算跳过而丢失。
3. **LLM 口语化让路**：剩余预算保不住合成本体（预留 12s）时跳过 LLM 走免费规则档。
4. **本机克隆诚实失败**：剩余 <3s 不再起合成，按配置回落/返回 ``avatar_clone_no_budget``
   ——绝不静默烧一次注定作废的 GPU。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.ai.tts_pipeline import TTSPipeline

_TEXT = "我今天去了海边，风有点大，但拍了好多照片。"


def _cfg(hub: dict | None = None, *, persona: str = "lin_xiaoyu",
         colloquial: dict | None = None, ref_path: str = "") -> dict:
    av: dict = {"enabled": True}
    if hub is not None:
        av["hub_fish"] = {"enabled": True, **hub}
    if colloquial is not None:
        av["colloquial"] = colloquial
    return {
        "enabled": True, "backend": "avatar_clone",
        "persona_id": persona,
        "avatar_voice": av,
        "voice_profile": {
            "enabled": True, "owner_consent": True, "backend": "avatar_clone",
            "reference_audio_path": ref_path or "config/voice_refs/lin_xiaoyu.wav",
        },
    }


def _rv(text: str = _TEXT) -> SimpleNamespace:
    return SimpleNamespace(text=text, extra={}, ok=False, provider="", format="",
                           audio_path="", duration_sec=-1.0,
                           duration_source="unknown", latency_ms=0, error="")


def _silent_wav(path: Path) -> str:
    """1 秒静音 WAV。管线只检查参考音文件存在，不提交真人声纹。"""
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    return str(path)


def _spy_hub(ok: bool = True):
    seen = {}

    def fake(base_url, profile, text, *, language="", emotion="",
             best_of=1, timeout_sec=30.0, audio_format="", tts_engine="",
             emo_text="", emo_alpha=None, **_extra):
        seen.update(profile=profile, text=text)
        if not ok:
            raise RuntimeError("hub down")
        return b"OggS" + b"\x00" * 64, "ogg"

    return fake, seen


# ── 1. hub：预算收口与跳过 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_hub_skipped_when_budget_exhausted(tmp_path):
    fake, seen = _spy_hub()
    tts = TTSPipeline(_cfg({}))
    rv = _rv()
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        out = await tts._try_hub_fish(
            rv, tmp_path / "o.wav", time.monotonic(), spec=None,
            budget_cap=1.0)
    assert out is None
    assert seen == {}          # 预算不足：一次往返都没打


@pytest.mark.asyncio
async def test_hub_budget_cap_none_keeps_old_behavior(tmp_path):
    fake, seen = _spy_hub()
    tts = TTSPipeline(_cfg({}, ref_path=_silent_wav(tmp_path / "ref.wav")))
    rv = _rv()
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        out = await tts._try_hub_fish(
            rv, tmp_path / "o.wav", time.monotonic(), spec=None)
    assert out is not None and out.ok
    assert seen["profile"] == "lin_xiaoyu"


@pytest.mark.asyncio
async def test_hub_generous_cap_still_attempts(tmp_path):
    fake, seen = _spy_hub()
    tts = TTSPipeline(_cfg({}, ref_path=_silent_wav(tmp_path / "ref.wav")))
    rv = _rv()
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        out = await tts._try_hub_fish(
            rv, tmp_path / "o.wav", time.monotonic(), spec=None,
            budget_cap=30.0)
    assert out is not None and out.ok


@pytest.mark.asyncio
async def test_strict_flag_survives_budget_skip(tmp_path):
    """strict 语义不因「没预算尝试 hub」而丢失——照样禁止本机克隆顶班。"""
    fake, _ = _spy_hub()
    cfg = _cfg({})
    cfg["avatar_voice"]["voice_consistency"] = "strict"
    tts = TTSPipeline(cfg)
    rv = _rv()
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        out = await tts._try_hub_fish(
            rv, tmp_path / "o.wav", time.monotonic(), spec=None,
            budget_cap=0.5)
    assert out is None
    assert rv.extra.get("hub_fish_required") is True


# ── 1b. hub：分条 best_of_parts（2026-08-01 GPU 减负）────────────────────────
def _spy_hub_bestof(ok: bool = True):
    seen = {}

    def fake(base_url, profile, text, *, language="", emotion="",
             best_of=1, timeout_sec=30.0, audio_format="", tts_engine="",
             emo_text="", emo_alpha=None, **_extra):
        seen.update(profile=profile, best_of=best_of)
        if not ok:
            raise RuntimeError("hub down")
        return b"OggS" + b"\x00" * 64, "ogg"

    return fake, seen


@pytest.mark.asyncio
async def test_hub_best_of_parts_applies_only_to_split_parts(tmp_path):
    """分条单条按 best_of_parts 取候选；整段发送不受影响（synth_verify 兜坏 take）。"""
    fake, seen = _spy_hub_bestof()
    tts = TTSPipeline(_cfg(
        {"best_of": 2, "best_of_parts": 1},
        ref_path=_silent_wav(tmp_path / "ref.wav")))
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        out = await tts._try_hub_fish(
            _rv(), tmp_path / "a.wav", time.monotonic(), spec=None,
            split_part=True)
        assert out is not None and seen["best_of"] == 1
        out = await tts._try_hub_fish(
            _rv(), tmp_path / "b.wav", time.monotonic(), spec=None,
            split_part=False)
        assert out is not None and seen["best_of"] == 2


@pytest.mark.asyncio
async def test_hub_best_of_parts_default_keeps_old_behavior(tmp_path):
    """未配 best_of_parts：分条也沿用 best_of——缺省零行为变化。"""
    fake, seen = _spy_hub_bestof()
    tts = TTSPipeline(_cfg({"best_of": 2}, ref_path=_silent_wav(tmp_path / "ref.wav")))
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        out = await tts._try_hub_fish(
            _rv(), tmp_path / "c.wav", time.monotonic(), spec=None,
            split_part=True)
    assert out is not None and seen["best_of"] == 2


# ── 2. LLM 口语化：预算不足让路给合成本体 ───────────────────────────────────
def _col_cfg() -> dict:
    return {"enabled": True, "mode": "llm", "llm_timeout_sec": 25}


@pytest.mark.asyncio
async def test_colloquial_llm_skipped_on_tight_budget(tmp_path):
    from src.ai.voice_emotion import coerce_emotion
    fake, seen = _spy_hub()
    tts = TTSPipeline(_cfg(
        {}, colloquial=_col_cfg(), ref_path=_silent_wav(tmp_path / "ref.wav")))
    rv = _rv()
    called = {"llm": 0}

    async def llm_spy(*a, **k):
        called["llm"] += 1
        return "改写后的口语版"

    # 剩余 ~10s：10 - 12(合成预留) < 2 → 必须跳过 LLM（规则档免费不受限）；
    # 但 10 - 6(hub 回落预留) ≥ 2 → hub 仍有预算尝试并命中。
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake), \
            patch("src.ai.voice_colloquial_llm.llm_colloquialize", llm_spy):
        out = await tts._try_avatar_clone(
            rv, tmp_path / "o.wav", time.monotonic(),
            spec=coerce_emotion("happy"),
            deadline=time.monotonic() + 10.0)
    assert out is not None and out.ok          # 合成本体不受影响（hub 命中）
    assert called["llm"] == 0                  # LLM 一次都没打
    assert not rv.extra.get("colloquial_llm")


@pytest.mark.asyncio
async def test_colloquial_llm_runs_without_deadline(tmp_path):
    from src.ai.voice_emotion import coerce_emotion
    fake, _ = _spy_hub()
    tts = TTSPipeline(_cfg(
        {}, colloquial=_col_cfg(), ref_path=_silent_wav(tmp_path / "ref.wav")))
    rv = _rv()
    called = {"llm": 0}

    async def llm_spy(*a, **k):
        called["llm"] += 1
        return "改写后的口语版"

    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake), \
            patch("src.ai.voice_colloquial_llm.llm_colloquialize", llm_spy):
        out = await tts._try_avatar_clone(
            rv, tmp_path / "o.wav", time.monotonic(),
            spec=coerce_emotion("happy"))
    assert out is not None and out.ok
    assert called["llm"] == 1                  # 缺省行为不变：照打 LLM
    assert rv.extra.get("colloquial_llm") is True


# ── 3. 本机克隆：没预算不起合成，诚实失败 ───────────────────────────────────
@pytest.mark.asyncio
async def test_local_clone_no_budget_fails_fast(tmp_path):
    from src.ai.avatar_voice import AvatarVoiceClient
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF" + b"\x00" * 64)
    cfg = _cfg(None, ref_path=str(ref))       # 无 hub：直奔本机克隆
    cfg["avatar_voice"]["cloud_fallback"] = False
    tts = TTSPipeline(cfg)
    rv = _rv()
    called = {"tts": 0}

    def tts_spy(*a, **k):
        called["tts"] += 1
        return b"RIFF" + b"\x00" * 64

    t0 = time.monotonic()
    with patch.object(AvatarVoiceClient, "health_ok", lambda self: True), \
            patch.object(AvatarVoiceClient, "tts", tts_spy):
        out = await tts._try_avatar_clone(
            rv, tmp_path / "o.wav", t0, spec=None,
            deadline=t0 + 1.0)                # 剩余 <3s
    assert out is not None and out.ok is False
    assert out.error == "avatar_clone_no_budget"
    assert called["tts"] == 0                  # 没白烧 GPU


@pytest.mark.asyncio
async def test_local_clone_runs_without_deadline(tmp_path):
    from src.ai.avatar_voice import AvatarVoiceClient
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF" + b"\x00" * 64)
    cfg = _cfg(None, ref_path=str(ref))
    tts = TTSPipeline(cfg)
    rv = _rv()

    def fake_tts(self, *a, **k):
        return b"RIFF" + b"\x00" * 2048

    with patch.object(AvatarVoiceClient, "health_ok", lambda self: True), \
            patch.object(AvatarVoiceClient, "tts", fake_tts):
        out = await tts._try_avatar_clone(
            rv, tmp_path / "o.wav", time.monotonic(), spec=None)
    assert out is not None and out.ok
    assert out.provider == "avatar_clone"


# ── 4. 端到端：synthesize(total_budget_sec=) 出诚实错误而非空 TimeoutError ──
@pytest.mark.asyncio
async def test_synthesize_total_budget_honest_error(tmp_path):
    from src.ai.avatar_voice import AvatarVoiceClient
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF" + b"\x00" * 64)
    cfg = _cfg(None, ref_path=str(ref))
    cfg["avatar_voice"]["cloud_fallback"] = False
    cfg["fallback_on_error"] = False
    cfg["cache"] = {"enabled": False}
    tts = TTSPipeline(cfg)

    with patch.object(AvatarVoiceClient, "health_ok", lambda self: True):
        rv = await tts.synthesize(_TEXT, total_budget_sec=0.05)
    assert rv.ok is False
    # 预算被明确归因到具体级，而不是外层空 TimeoutError
    assert rv.error == "avatar_clone_no_budget"


@pytest.mark.asyncio
async def test_synthesize_without_budget_unchanged(tmp_path):
    """不传 total_budget_sec：走完整旧链（本例 hub 命中出货）。"""
    fake, _ = _spy_hub()
    cfg = _cfg({}, ref_path=_silent_wav(tmp_path / "ref.wav"))
    tts = TTSPipeline(cfg)
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        rv = await tts.synthesize(_TEXT)
    assert rv.ok and rv.provider == "hub_fish"
