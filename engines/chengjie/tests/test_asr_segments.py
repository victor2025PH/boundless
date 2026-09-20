"""P3（2026-08-18）：SRT 地基——ASR 分段时间戳 opt-in 链契约。

不变量：
- ``transcribe_file(want_segments=)`` 参数存在（默认 False＝语音主链零变化）；
- translate_voice 把鲜 ASR 的 ``extra["segment_list"]`` 透传为响应 ``segments``；
- 缓存命中路径**不带** segments 键（缓存只存文本，P4 消费方按缺失=不产字幕处理）；
- ``build_audio_transcribe_fn(want_segments=)`` 透传。
"""
import inspect

from src.ai.audio_pipeline import AudioPipeline
from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import TranslationService
from src.ai.voice_translate import VoiceTranslateService, build_audio_transcribe_fn


class _StubEngine:
    name = "ai"

    @property
    def available(self):
        return True

    def supports_target(self, t):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        return EngineResult(f"{text}#tr", self.name, True)


def _xlate():
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([_StubEngine()])
    return s


class _RV:
    def __init__(self, segs):
        self.ok = True
        self.text = "hello world"
        self.language = "en"
        self.model = "stub-asr"
        self.latency_ms = 5
        self.extra = {"segment_list": segs} if segs else {}


def test_transcribe_file_signature_has_want_segments():
    sig = inspect.signature(AudioPipeline.transcribe_file)
    p = sig.parameters.get("want_segments")
    assert p is not None and p.default is False


def test_build_transcribe_fn_signature_has_want_segments():
    sig = inspect.signature(build_audio_transcribe_fn)
    p = sig.parameters.get("want_segments")
    assert p is not None and p.default is False


async def test_translate_voice_surfaces_segments(tmp_path):
    segs = [{"start": 0.0, "end": 1.2, "text": "hello"},
            {"start": 1.2, "end": 2.0, "text": "world"}]

    async def _tr(_path):
        return _RV(segs)

    p = tmp_path / "a.ogg"
    p.write_bytes(b"OggS" + b"\x00" * 64)
    svc = VoiceTranslateService(_xlate(), _tr)
    out = await svc.translate_voice(str(p), target_lang="zh")
    assert out["ok"] is True
    assert out["segments"] == segs
    assert out["translation"]["translated_text"] == "hello world#tr"


async def test_cached_path_preserves_segments(tmp_path):
    """P6：信封缓存——同一音频重复请求，分段随缓存命中原样回来（零第二次 ASR）。

    修「第一次上传有字幕、第二次上传字幕消失」的语义不一致（P4 首版缓存只存文本）。
    """
    calls = {"n": 0}
    segs = [{"start": 0, "end": 1, "text": "hello"}]

    async def _tr(_path):
        calls["n"] += 1
        return _RV(segs)

    p = tmp_path / "b.ogg"
    p.write_bytes(b"OggS" + b"\x01" * 77)
    svc = VoiceTranslateService(_xlate(), _tr)
    first = await svc.translate_voice(str(p), target_lang="zh", want_segments=True)
    assert first["ok"] and first["segments"] == segs and calls["n"] == 1
    second = await svc.translate_voice(str(p), target_lang="zh", want_segments=True)
    assert second["ok"] and second["asr_cached"] is True
    assert second["segments"] == segs, "信封命中必须带回分段"
    assert calls["n"] == 1, "缓存命中不得二跑 ASR"


async def test_cache_upgrade_repull_when_segments_wanted(tmp_path):
    """P6：文本级旧缓存 + 调用方要分段 → 按 miss 重跑一次并升级信封；此后命中信封。"""
    calls = {"n": 0}

    async def _tr(_path):
        calls["n"] += 1
        # 第一次调用方没要分段（fn 无 segment_list），第二次起带
        return _RV([{"start": 0, "end": 1, "text": "hello"}] if calls["n"] >= 2 else None)

    p = tmp_path / "up.ogg"
    p.write_bytes(b"OggS" + b"\x03" * 55)
    svc = VoiceTranslateService(_xlate(), _tr)
    r1 = await svc.translate_voice(str(p), target_lang="zh")           # 存纯文本
    assert r1["ok"] and "segments" not in r1 and calls["n"] == 1
    r2 = await svc.translate_voice(str(p), target_lang="zh", want_segments=True)
    assert r2["ok"] and r2.get("segments") and calls["n"] == 2         # 升级重跑
    r3 = await svc.translate_voice(str(p), target_lang="zh", want_segments=True)
    assert r3["ok"] and r3["asr_cached"] is True and r3.get("segments")
    assert calls["n"] == 2                                             # 信封已命中


def test_cache_pack_unpack_roundtrip_and_legacy():
    from src.ai.voice_translate import _cache_pack, _cache_unpack

    segs = [{"start": 0.0, "end": 1.5, "text": "hi"}]
    packed = _cache_pack("hi there", segs)
    assert packed.startswith("ASRJ1:")
    t, s = _cache_unpack(packed)
    assert t == "hi there" and s == segs
    # 无分段 → 纯文本（历史格式，别的消费者零感知）
    assert _cache_pack("plain", []) == "plain"
    assert _cache_unpack("plain") == ("plain", [])
    # 坏信封 → ("", []) ＝调用方按 miss 处理，绝不把信封串当转写
    assert _cache_unpack("ASRJ1:{not json") == ("", [])


async def test_no_segments_when_extra_empty(tmp_path):
    async def _tr(_path):
        return _RV(None)

    p = tmp_path / "c.ogg"
    p.write_bytes(b"OggS" + b"\x02" * 33)
    svc = VoiceTranslateService(_xlate(), _tr)
    out = await svc.translate_voice(str(p), target_lang="zh")
    assert out["ok"] is True and "segments" not in out


def test_transcribe_fn_immune_to_poisoned_singleton():
    """P4-fix 不变量：全局单例被禁用态配置抢注，不得影响 translate 链的管线。

    实例配置里 RPA 嵌套 audio_pipeline 全是 enabled:false 死默认——单例「首调定型」
    语义下谁先调谁定生死。build_audio_transcribe_fn 必须走配置指纹独立缓存。
    """
    import src.ai.audio_pipeline as ap_mod
    from src.ai.voice_translate import _AP_CACHE, _pipeline_for

    _AP_CACHE.clear()
    old = ap_mod._pipeline_singleton
    try:
        # 模拟 RPA 先到：全局单例被 enabled:false 抢注
        ap_mod._pipeline_singleton = ap_mod.AudioPipeline({"enabled": False})
        assert ap_mod.get_audio_pipeline({"enabled": True}).enabled is False  # 单例语义如此
        # translate 链拿到的必须是自己的 enabled 管线
        mine = _pipeline_for({"enabled": True, "backend": "openai",
                              "base_url": "http://x/v1", "api_key": "k"})
        assert mine.enabled is True
        # 同配置命中缓存（不重建）；异配置各自独立
        again = _pipeline_for({"enabled": True, "backend": "openai",
                               "base_url": "http://x/v1", "api_key": "k"})
        assert again is mine
        other = _pipeline_for({"enabled": False})
        assert other is not mine and other.enabled is False
    finally:
        ap_mod._pipeline_singleton = old
        _AP_CACHE.clear()
