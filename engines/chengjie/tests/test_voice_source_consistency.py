"""出站语音「音色单一事实源」门禁（2026-07-27 真机投诉驱动）。

真机现象：智聊回复的语音与幻影对话的语音不是同一个人（"太假"）。查明同一 hub 上
两个产品各跑各的：幻影对话 = 档 ``林小玲`` + moss_ttsd（声纹分 0.70~0.80）；智聊 =
档 ``lin_xiaoyu`` + fish_speech（历史 0.06~0.40），档名还是硬编 ``persona_id`` 指过去的。

本文件守三条不变量（与「具体用哪个音色」无关——那是配置/运营决定）：
1. **档名可映射**：``hub_fish.profile_map`` 决定人设用哪个 hub 档；缺省＝persona_id（旧行为）。
2. **引擎可钉住**：``hub_fish.tts_engine`` 显式下发；为空不下发（沿用 hub 档配置=旧行为）。
3. **兜底不换音色**：``voice_consistency: strict`` 时 hub 挂掉 → 拒发语音（调用方回落
   文字），绝不用本机另一份参考音的克隆顶班；默认 lenient 保持旧的贯穿回落。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.ai.avatar_voice import build_tts_only_payload
from src.ai.tts_pipeline import TTSPipeline

_TEXT = "我今天去了海边，风有点大，但拍了好多照片。"


def _cfg(hub: dict, *, consistency: str = "", persona: str = "lin_xiaoyu"):
    av = {"enabled": True, "hub_fish": {"enabled": True, **hub}}
    if consistency:
        av["voice_consistency"] = consistency
    return {
        "enabled": True, "backend": "avatar_clone",
        "persona_id": persona,
        "avatar_voice": av,
        "voice_profile": {
            "enabled": True, "owner_consent": True, "backend": "avatar_clone",
            "reference_audio_path": "config/voice_refs/lin_xiaoyu.wav",
        },
    }


# ── 1. 请求体：引擎键按需下发 ───────────────────────────────────────────────
def test_payload_omits_engine_by_default():
    body = json.loads(build_tts_only_payload("p1", _TEXT).decode("utf-8"))
    assert "tts_engine" not in body          # 空=沿用 hub 档配置（旧行为零变化）


def test_payload_carries_engine_when_pinned():
    body = json.loads(build_tts_only_payload(
        "p1", _TEXT, tts_engine="moss_ttsd").decode("utf-8"))
    assert body["tts_engine"] == "moss_ttsd"
    assert body["profile"] == "p1"


# ── 2. 档名映射 + 引擎钉住：真正传给 hub 的值 ───────────────────────────────
def _spy_hub(tmp_path: Path, ok: bool = True):
    """替 hub_fish_synthesize 打桩，记录实参。"""
    seen = {}

    def fake(base_url, profile, text, *, language="", emotion="",
             best_of=1, timeout_sec=30.0, audio_format="", tts_engine=""):
        seen.update(base_url=base_url, profile=profile, text=text,
                    tts_engine=tts_engine, audio_format=audio_format)
        if not ok:
            raise RuntimeError("hub down")
        return b"OggS" + b"\x00" * 64, "ogg"

    return fake, seen


@pytest.mark.asyncio
async def test_profile_map_redirects_persona_to_mapped_profile(tmp_path):
    fake, seen = _spy_hub(tmp_path)
    tts = TTSPipeline(_cfg({
        "profile_map": {"lin_xiaoyu": "chengjie_lin_xiaoyu"},
        "tts_engine": "moss_ttsd",
        "response_format": "ogg",
    }))
    rv = SimpleNamespace(text=_TEXT, extra={}, ok=False, provider="", format="",
                         audio_path="", duration_sec=-1.0,
                         duration_source="unknown", latency_ms=0, error="")
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        out = await tts._try_hub_fish(rv, tmp_path / "o.wav", 0.0, spec=None)
    assert out is not None and out.ok
    assert seen["profile"] == "chengjie_lin_xiaoyu"   # 映射生效
    assert seen["tts_engine"] == "moss_ttsd"          # 引擎钉住
    assert out.extra["hub_fish_profile"] == "chengjie_lin_xiaoyu"


@pytest.mark.asyncio
async def test_without_map_falls_back_to_persona_id(tmp_path):
    """缺省行为不变：没配映射就还是 persona_id 同名档、不下发引擎。"""
    fake, seen = _spy_hub(tmp_path)
    tts = TTSPipeline(_cfg({"response_format": "ogg"}))
    rv = SimpleNamespace(text=_TEXT, extra={}, ok=False, provider="", format="",
                         audio_path="", duration_sec=-1.0,
                         duration_source="unknown", latency_ms=0, error="")
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        out = await tts._try_hub_fish(rv, tmp_path / "o.wav", 0.0, spec=None)
    assert out is not None and out.ok
    assert seen["profile"] == "lin_xiaoyu"
    assert seen["tts_engine"] == ""


@pytest.mark.asyncio
async def test_allowlist_still_gates_before_mapping(tmp_path):
    """不在灰度名单 → 根本不打 hub（映射不该绕过名单）。"""
    fake, seen = _spy_hub(tmp_path)
    tts = TTSPipeline(_cfg({
        "persona_allowlist": ["someone_else"],
        "profile_map": {"lin_xiaoyu": "chengjie_lin_xiaoyu"},
    }))
    rv = SimpleNamespace(text=_TEXT, extra={}, ok=False, provider="", format="",
                         audio_path="", duration_sec=-1.0,
                         duration_source="unknown", latency_ms=0, error="")
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        assert await tts._try_hub_fish(
            rv, tmp_path / "o.wav", 0.0, spec=None) is None
    assert seen == {}


# ── 3. strict：hub 挂了不换音色顶班 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_strict_marks_hub_required_on_failure(tmp_path):
    fake, _ = _spy_hub(tmp_path, ok=False)
    tts = TTSPipeline(_cfg({}, consistency="strict"))
    rv = SimpleNamespace(text=_TEXT, extra={}, ok=False, provider="", format="",
                         audio_path="", duration_sec=-1.0,
                         duration_source="unknown", latency_ms=0, error="")
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        assert await tts._try_hub_fish(
            rv, tmp_path / "o.wav", 0.0, spec=None) is None
    # 标记留在 rv 上，供 _try_avatar_clone 决定「拒发」而非回落本机克隆
    assert rv.extra.get("hub_fish_required") is True


@pytest.mark.asyncio
async def test_lenient_does_not_mark_hub_required(tmp_path):
    fake, _ = _spy_hub(tmp_path, ok=False)
    tts = TTSPipeline(_cfg({}))          # 默认 lenient
    rv = SimpleNamespace(text=_TEXT, extra={}, ok=False, provider="", format="",
                         audio_path="", duration_sec=-1.0,
                         duration_source="unknown", latency_ms=0, error="")
    with patch("src.ai.avatar_voice.hub_fish_synthesize", fake):
        assert await tts._try_hub_fish(
            rv, tmp_path / "o.wav", 0.0, spec=None) is None
    assert "hub_fish_required" not in rv.extra


@pytest.mark.asyncio
async def test_strict_refuses_local_clone_takeover(tmp_path, monkeypatch):
    """端到端：strict + hub 挂 → synthesize 硬失败（不打本机 7852、不发 edge）。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF" + b"\x00" * 64)
    cfg = _cfg({}, consistency="strict")
    cfg["voice_profile"]["reference_audio_path"] = str(ref)
    cfg["fallback_on_error"] = False
    tts = TTSPipeline(cfg)

    def hub_down(*a, **k):
        raise RuntimeError("hub down")

    called = {"local": 0}

    async def local_spy(*a, **k):
        called["local"] += 1
        return None

    monkeypatch.setattr("src.ai.avatar_voice.hub_fish_synthesize", hub_down)
    from src.ai.avatar_voice import AvatarVoiceClient
    with patch.object(AvatarVoiceClient, "health_ok",
                      side_effect=lambda: called.__setitem__("local", 1) or True):
        rv = await tts.synthesize(_TEXT, emotion=None)
    assert rv.ok is False
    assert rv.error == "hub_voice_source_unavailable"
    assert called["local"] == 0          # 本机克隆链一步都没走
