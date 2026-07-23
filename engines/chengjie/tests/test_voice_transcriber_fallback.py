"""语音转录级联/回落门禁。

覆盖「升级到 Qwen3-ASR（OpenAI 兼容本机端点）+ faster-whisper 兜底」的不变量：
  - 无 fallback 配置 → 工厂返回单个转录器（行为不变）。
  - provider=openai/qwen3_asr 等 → 走 OpenAI 兼容转录器（同一契约）。
  - 有 fallback → 返回 FallbackTranscriber，主机返空/抛错时无缝回落，绝不阻塞理解链。
纯逻辑，不触网、不加载任何模型（各转录器 __init__ 惰性加载）。
"""
import pytest

from src.voice_transcriber import (
    FallbackTranscriber,
    FasterWhisperTranscriber,
    OpenAITranscriber,
    SenseVoiceTranscriber,
    VoiceTranscriber,
    VoiceTranscriberFactory,
)


class _FakeTranscriber(VoiceTranscriber):
    """可编排返回值/异常的假转录器（记录是否被调用）。"""

    def __init__(self, *, result=None, raises=None):
        super().__init__({"temp_dir": "./temp/test_voice_fb"})
        self._result = result
        self._raises = raises
        self.called = False

    async def transcribe_voice_message(self, voice_file_path, language="zh"):
        self.called = True
        if self._raises is not None:
            raise self._raises
        return self._result

    async def _transcribe_impl(self, voice_file_path, language):  # pragma: no cover
        return self._result


def test_factory_without_fallback_returns_single():
    t = VoiceTranscriberFactory.create_transcriber(
        {"provider": "faster_whisper", "whisper": {"model_size": "small"}}
    )
    assert isinstance(t, FasterWhisperTranscriber)
    assert not isinstance(t, FallbackTranscriber)


def test_factory_openai_compatible_aliases_use_openai_transcriber():
    # 注意：funasr 别名已改指进程内 SenseVoiceTranscriber（见下一个测试）；
    # HTTP OpenAI 兼容语义由 funasr_api 承接。
    for provider in ("openai", "qwen3_asr", "funasr_api", "openai_compatible"):
        t = VoiceTranscriberFactory._create_one(
            {"provider": provider, "openai": {"base_url": "http://127.0.0.1:9200/v1",
                                              "api_key": "sk-local", "model": "qwen3-asr"}}
        )
        assert isinstance(t, OpenAITranscriber), provider
        assert t.base_url == "http://127.0.0.1:9200/v1"


def test_factory_sensevoice_aliases_use_inprocess_transcriber():
    """sensevoice/sense_voice/funasr → 进程内 SenseVoice（方言/粤语主力）。

    funasr 别名语义变更（2026-07-23）：原指 OpenAI 兼容 HTTP 服务（现由
    funasr_api 承接），改指进程内 funasr 库加载 SenseVoice-Small——生产
    overlay（zhiliao voice_recognition.provider: sensevoice）即此路径。
    构造惰性加载（不触网不载模型），单测安全。"""
    for provider in ("sensevoice", "sense_voice", "funasr"):
        t = VoiceTranscriberFactory._create_one(
            {"provider": provider,
             "sensevoice": {"model_dir": "iic/SenseVoiceSmall", "device": "cpu"}}
        )
        assert isinstance(t, SenseVoiceTranscriber), provider
        assert t.model is None  # 惰性：__init__ 不加载模型
        assert t.model_dir == "iic/SenseVoiceSmall"


def test_factory_with_fallback_builds_chain():
    t = VoiceTranscriberFactory.create_transcriber(
        {
            "provider": "openai",
            "openai": {"base_url": "http://127.0.0.1:9200/v1", "api_key": "sk-local",
                       "model": "qwen3-asr"},
            "fallback": {"provider": "faster_whisper", "whisper": {"model_size": "small"}},
        }
    )
    assert isinstance(t, FallbackTranscriber)
    assert len(t._chain) == 2
    assert isinstance(t._chain[0], OpenAITranscriber)
    assert isinstance(t._chain[1], FasterWhisperTranscriber)


async def test_fallback_uses_primary_when_ok():
    primary = _FakeTranscriber(result="hello from qwen")
    backup = _FakeTranscriber(result="hello from whisper")
    fb = FallbackTranscriber({}, [primary, backup])
    out = await fb.transcribe_voice_message("x.ogg", "auto")
    assert out == "hello from qwen"
    assert primary.called and not backup.called


async def test_fallback_on_empty_primary():
    primary = _FakeTranscriber(result=None)
    backup = _FakeTranscriber(result="hello from whisper")
    fb = FallbackTranscriber({}, [primary, backup])
    out = await fb.transcribe_voice_message("x.ogg", "auto")
    assert out == "hello from whisper"
    assert primary.called and backup.called


async def test_fallback_on_primary_exception():
    primary = _FakeTranscriber(raises=RuntimeError("connection refused"))
    backup = _FakeTranscriber(result="hello from whisper")
    fb = FallbackTranscriber({}, [primary, backup])
    out = await fb.transcribe_voice_message("x.ogg", "auto")
    assert out == "hello from whisper"
    assert primary.called and backup.called


async def test_fallback_all_fail_returns_none():
    primary = _FakeTranscriber(raises=RuntimeError("down"))
    backup = _FakeTranscriber(result=None)
    fb = FallbackTranscriber({}, [primary, backup])
    out = await fb.transcribe_voice_message("x.ogg", "auto")
    assert out is None
    assert primary.called and backup.called
