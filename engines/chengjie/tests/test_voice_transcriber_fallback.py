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


# ── 启动预热（warmup_on_boot，2026-07-23）────────────────────────────
# 语义：消重启后首条语音的模型冷启动（~20-30s）。不变量：
#   ① 基类默认无操作（远程/云端型启动零外呼）；
#   ② 级联只预热主级——回落级懒加载省显存；
#   ③ SenseVoice 预热=复用 _ensure_model（幂等，已载直接返回）。


class _WarmFake(_FakeTranscriber):
    def __init__(self):
        super().__init__(result="x")
        self.warmed = 0

    async def warmup(self):
        self.warmed += 1


async def test_warmup_base_default_noop():
    t = _FakeTranscriber(result="ok")
    assert await t.warmup() is None  # 不抛不外呼即过


async def test_fallback_warmup_only_primary():
    primary, backup = _WarmFake(), _WarmFake()
    fb = FallbackTranscriber({}, [primary, backup])
    await fb.warmup()
    assert primary.warmed == 1 and backup.warmed == 0
    await FallbackTranscriber({}, []).warmup()  # 空链不炸


async def test_sensevoice_warmup_delegates_to_ensure_model(monkeypatch):
    t = SenseVoiceTranscriber(
        {"temp_dir": "./temp/test_voice_fb", "sensevoice": {"device": "cpu"}})
    calls = []

    async def fake_ensure():
        calls.append(1)
        t.model = object()

    monkeypatch.setattr(t, "_ensure_model", fake_ensure)
    await t.warmup()
    assert calls == [1]


# ── 中文转写繁→简归一（2026-09-12 GWJ2RZ）───────────────────────────────────
class _ImplFake(VoiceTranscriber):
    """走基类公有 transcribe_voice_message（含幻觉守卫 + 字形归一）的假实现。"""

    def __init__(self, result, **cfg):
        super().__init__({"temp_dir": "./temp/test_voice_fb", **cfg})
        self._result = result

    async def _transcribe_impl(self, voice_file_path, language):
        return self._result


def _voice_file(tmp_path, seed: bytes = b"\x00"):
    # seed：同一用例里喂**不同**假转录器时给不同字节——转写缓存按音频内容 sha1 命中
    # （ASR P0），同字节＝同一段音频＝理应同一转写，两个假实现互相「命中」不是 bug。
    p = tmp_path / f"v_{seed.hex()}.ogg"
    p.write_bytes(b"OggS" + seed * 64)
    return str(p)


async def test_zh_transcript_normalized_to_simplified(tmp_path):
    pytest.importorskip("opencc")
    t = _ImplFake("平時在家學做AI短劇啊自己煮飯下廚房")
    out = await t.transcribe_voice_message(_voice_file(tmp_path), "auto")
    assert out == "平时在家学做AI短剧啊自己煮饭下厨房"


async def test_zh_normalize_keeps_cantonese_and_non_zh(tmp_path):
    # 粤文豁免（粤语用字不属繁简映射，且有专线路由）
    yue = "我哋啲客都係香港嘅，咁講粵語好啲"
    assert await _ImplFake(yue).transcribe_voice_message(
        _voice_file(tmp_path, b"\x01"), "auto") == yue
    # 非中文主体原样
    en = "Hello, how are you today?"
    assert await _ImplFake(en).transcribe_voice_message(
        _voice_file(tmp_path, b"\x02"), "auto") == en


async def test_zh_normalize_opt_out(tmp_path):
    trad = "你平時在家都做些什麼"
    t = _ImplFake(trad, zh_simplified_output=False)
    assert await t.transcribe_voice_message(_voice_file(tmp_path), "zh") == trad
