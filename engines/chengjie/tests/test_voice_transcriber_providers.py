"""voice_transcriber 工厂/级联/新 provider（SenseVoice·方言）单测（无模型无网络）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.voice_transcriber import (
    FallbackTranscriber,
    FasterWhisperTranscriber,
    OpenAITranscriber,
    SenseVoiceTranscriber,
    VoiceTranscriberFactory,
)

_BASE = {"enabled": True, "temp_dir": "./temp/test_voice"}


def test_factory_sensevoice_provider():
    t = VoiceTranscriberFactory.create_transcriber(
        {**_BASE, "provider": "sensevoice",
         "sensevoice": {"device": "cpu"}})
    assert isinstance(t, SenseVoiceTranscriber)
    assert t.device == "cpu"


def test_factory_funasr_alias_maps_to_sensevoice():
    t = VoiceTranscriberFactory.create_transcriber(
        {**_BASE, "provider": "funasr", "sensevoice": {"device": "cpu"}})
    assert isinstance(t, SenseVoiceTranscriber)


def test_factory_funasr_api_still_openai_compatible():
    t = VoiceTranscriberFactory.create_transcriber(
        {**_BASE, "provider": "funasr_api", "api_key": "x"})
    assert isinstance(t, OpenAITranscriber)


def test_factory_cascade_sensevoice_with_whisper_fallback():
    t = VoiceTranscriberFactory.create_transcriber({
        **_BASE, "provider": "sensevoice",
        "sensevoice": {"device": "cpu"},
        "fallback": {"provider": "faster_whisper"},
    })
    assert isinstance(t, FallbackTranscriber)
    assert isinstance(t._chain[0], SenseVoiceTranscriber)
    assert isinstance(t._chain[1], FasterWhisperTranscriber)


@pytest.mark.asyncio
async def test_cascade_falls_back_on_empty(tmp_path):
    """主转录返空 → 自动走兜底级。"""
    f = tmp_path / "v.ogg"
    f.write_bytes(b"x" * 100)
    cfg = {**_BASE, "provider": "sensevoice", "fallback": {"provider": "faster_whisper"}}
    t = VoiceTranscriberFactory.create_transcriber(cfg)
    with patch.object(t._chain[0], "_transcribe_impl", new=AsyncMock(return_value=None)), \
         patch.object(t._chain[1], "_transcribe_impl", new=AsyncMock(return_value="兜底转出的话")):
        out = await t.transcribe_voice_message(str(f), "auto")
    assert out == "兜底转出的话"


@pytest.mark.asyncio
async def test_faster_whisper_hotwords_passed_as_initial_prompt(tmp_path):
    """hotwords 配置应作为 initial_prompt 传给 whisper（治专名同音误转）。"""
    f = tmp_path / "v.ogg"
    f.write_bytes(b"x" * 100)
    t = FasterWhisperTranscriber(
        {**_BASE, "provider": "faster_whisper",
         "hotwords": ["智聊ChatX", "无界科技"]})

    captured = {}

    class _Seg:
        text = "帮我介绍一下智聊的价格"

    class _FakeModel:
        def transcribe(self, **kw):
            captured.update(kw)
            return [_Seg()], object()

    t.model = _FakeModel()
    out = await t.transcribe_voice_message(str(f), "auto")
    assert out == "帮我介绍一下智聊的价格"
    assert captured.get("initial_prompt") == "智聊ChatX、无界科技"
    # language=auto 应传 None（勿映射成 zh：whisper 会把外语翻译成中文而非转写）
    assert captured.get("language") is None


def test_sensevoice_lang_map():
    t = SenseVoiceTranscriber(
        {**_BASE, "provider": "sensevoice", "sensevoice": {"device": "cpu"}})
    assert t._LANG_MAP.get("yue") == "yue"
    assert t._LANG_MAP.get("auto") == "auto"
    assert t._LANG_MAP.get("zh") == "zh"
