"""ASR P0（2026-09-12）：转写缓存 / 动态超时 / 热词 prompt + verbose_json / 语种先验复核。

背景（zhiliao 09-12 日志实锤）：同一条语音被「协议入站落库前」与「AutoDraft」各转一次
（38 次主 ASR 调用 ↔ ~19 条语音）；``timeout: 3`` 按 176 定、迁 198 后没重校；
``hotwords`` 只在进程内 FasterWhisper 生效、生产 OpenAI 兼容主路从没收到过；
主路 ``response_format=text`` 拿不到任何置信度。本文件钉住修法的不变量，全程无网无模型。
"""
from __future__ import annotations

import struct
from unittest.mock import patch

import httpx
import openai
import pytest

from src.ai.asr_stats import get_asr_stats
from src.voice_transcriber import (
    FallbackTranscriber,
    OpenAITranscriber,
    VoiceTranscriber,
    effective_timeout,
    extract_transcription_meta,
    get_transcript_cache,
    hotwords_prompt,
    looks_like_prompt_echo,
    should_retry_with_lang_hint,
)


@pytest.fixture(autouse=True)
def _reset():
    get_transcript_cache().reset()
    get_asr_stats().reset()
    yield
    get_transcript_cache().reset()
    get_asr_stats().reset()


def _ogg(tmp_path, seed: bytes = b"\x01", name: str = "v.ogg") -> str:
    p = tmp_path / name
    p.write_bytes(b"OggS" + seed * 64)
    return str(p)


def _wav(tmp_path, seconds: float, name: str = "v.wav") -> str:
    """16k / mono / 16bit 的合法 RIFF 头 + 静音数据（时长可被 probe_audio_file 读出）。"""
    rate, ch, bits = 16000, 1, 16
    byte_rate = rate * ch * bits // 8
    data_len = int(seconds * byte_rate)
    hdr = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch, rate, byte_rate, ch * bits // 8, bits)
    hdr += b"data" + struct.pack("<I", data_len)
    p = tmp_path / name
    p.write_bytes(hdr + b"\x00" * data_len)
    return str(p)


class _Impl(VoiceTranscriber):
    """可编排的假实现：``results`` 按调用序返回（str / None / Exception），记录每次 language。"""

    def __init__(self, results, *, metas=None, error: str = "", **cfg):
        super().__init__({"temp_dir": "./temp/test_voice_p0", **cfg})
        self._results = list(results)
        self._metas = list(metas or [])
        self._error = error
        self.calls = []

    async def _transcribe_impl(self, voice_file_path, language):
        self.calls.append(language)
        i = min(len(self.calls) - 1, len(self._results) - 1)
        r = self._results[i]
        if self._metas:
            self.last_meta = dict(self._metas[min(i, len(self._metas) - 1)])
        if isinstance(r, Exception):
            raise r
        if r is None and self._error:
            self.last_error = self._error
        return r


# ── 转写缓存 ─────────────────────────────────────────────────────────────────
async def test_cache_hit_skips_second_transcription(tmp_path):
    t = _Impl(["你好呀"])
    f = _ogg(tmp_path)
    assert await t.transcribe_voice_message(f, "auto") == "你好呀"
    assert await t.transcribe_voice_message(f, "auto") == "你好呀"
    assert len(t.calls) == 1, "同一音频第二次必须命中缓存，不再打 ASR"
    assert t.last_meta.get("cache_hit") is True
    assert get_asr_stats().dump()["events"]["cache_hit"] == 1
    assert get_transcript_cache().stats()["hits"] == 1


async def test_cache_is_content_addressed_not_path(tmp_path):
    t = _Impl(["同一段话"])
    a = _ogg(tmp_path, b"\x07", "a.ogg")
    b = _ogg(tmp_path, b"\x07", "b.ogg")  # 同字节、不同路径（协议线与 AutoDraft 拿到的是同一文件，
    assert await t.transcribe_voice_message(a, "auto") == "同一段话"  # 这里更严：连拷贝也命中）
    assert await t.transcribe_voice_message(b, "auto") == "同一段话"
    assert len(t.calls) == 1


async def test_cache_key_includes_language(tmp_path):
    t = _Impl(["a", "b"])
    f = _ogg(tmp_path)
    assert await t.transcribe_voice_message(f, "auto") == "a"
    assert await t.transcribe_voice_message(f, "zh") == "b"
    assert len(t.calls) == 2


async def test_cache_negative_only_for_content_decided_empty(tmp_path):
    # empty_result → 负缓存：第二次不再打 ASR
    t = _Impl([None, "不该被调到"])
    f = _ogg(tmp_path, b"\x02")
    assert await t.transcribe_voice_message(f, "auto") is None
    assert await t.transcribe_voice_message(f, "auto") is None
    assert len(t.calls) == 1
    assert t.last_error.startswith("cache:")
    # 异常（超时/不可达）→ 不缓存：下一次照常重试
    t2 = _Impl([RuntimeError("timeout"), "恢复了"])
    f2 = _ogg(tmp_path, b"\x03")
    assert await t2.transcribe_voice_message(f2, "auto") is None
    assert await t2.transcribe_voice_message(f2, "auto") == "恢复了"
    assert len(t2.calls) == 2


async def test_cache_can_be_disabled(tmp_path):
    t = _Impl(["x", "y"], transcript_cache={"enabled": False})
    f = _ogg(tmp_path)
    assert await t.transcribe_voice_message(f, "auto") == "x"
    assert await t.transcribe_voice_message(f, "auto") == "y"


async def test_fallback_chain_caches_result_and_all_level_empty(tmp_path):
    a, b = _Impl([None, None], error="empty_result"), _Impl(["回落转的", "x"])
    chain = FallbackTranscriber({"temp_dir": str(tmp_path / "f")}, [a, b])
    f = _ogg(tmp_path, b"\x04")
    assert await chain.transcribe_voice_message(f, "auto") == "回落转的"
    assert chain.last_meta.get("provider") == "_Impl" and chain.last_meta.get("level") == 1
    assert await chain.transcribe_voice_message(f, "auto") == "回落转的"
    assert len(a.calls) == 1 and len(b.calls) == 1  # 第二次全链零调用
    # 全链都是「内容决定」的空 → 负缓存（AutoDraft 十几秒后再来不再让 GPU 白跑两遍）
    c, d = _Impl([None], error="empty_result"), _Impl([None], error="no_speech: hallucination_guard")
    chain2 = FallbackTranscriber({"temp_dir": str(tmp_path / "g")}, [c, d])
    f2 = _ogg(tmp_path, b"\x05")
    assert await chain2.transcribe_voice_message(f2, "auto") is None
    assert await chain2.transcribe_voice_message(f2, "auto") is None
    assert len(c.calls) == 1 and len(d.calls) == 1
    # 任一级异常 → 不负缓存
    e, g = _Impl([RuntimeError("down")]), _Impl([None], error="empty_result")
    chain3 = FallbackTranscriber({"temp_dir": str(tmp_path / "h")}, [e, g])
    f3 = _ogg(tmp_path, b"\x06")
    assert await chain3.transcribe_voice_message(f3, "auto") is None
    assert await chain3.transcribe_voice_message(f3, "auto") is None
    assert len(e.calls) == 2


# ── 动态超时 ─────────────────────────────────────────────────────────────────
def test_effective_timeout_scales_with_duration_and_caps():
    assert effective_timeout(3, None) == 3.0          # 时长未知 → 基础超时（行为不变）
    assert effective_timeout(3, 4.25) == 3.0          # 短音频：4.25×0.25+1.5=2.56 < 3 → 3
    assert effective_timeout(3, 20) == 6.5            # 20s 语音：不再 3s 就超时切备路
    assert effective_timeout(3, 60) == 10.0           # 封顶（与入站 25s 墙钟 + 备路 18s 联动）
    assert effective_timeout(30, 60) == 30.0          # cap 不低于基础超时（云端 30s 配置不变）
    assert effective_timeout(3, 20, per_audio_sec=0.5, overhead_sec=1.0, cap_sec=20) == 11.0
    assert effective_timeout("bad", 10) == 30.0       # 坏配置回默认 30


# ── 热词 prompt / 回声 ───────────────────────────────────────────────────────
def test_hotwords_prompt_resolution():
    assert hotwords_prompt({"hotwords": ["智聊ChatX", "", "无界科技"]}) == "智聊ChatX、无界科技"
    assert hotwords_prompt({"hotwords": "智聊ChatX、通译LingoX"}) == "智聊ChatX、通译LingoX"
    assert hotwords_prompt({"whisper": {"hotwords": ["a"]}}) == "a"
    assert hotwords_prompt({}) == "" and hotwords_prompt(None) == ""


def test_prompt_echo_detection():
    p = "智聊ChatX、通译LingoX、无界科技"
    assert looks_like_prompt_echo("智聊ChatX、通译LingoX、无界科技", p)            # 整串吐回
    assert looks_like_prompt_echo("智聊ChatX 无界科技。", p)                        # ≥2 个热词拼成
    assert not looks_like_prompt_echo("我想了解一下智聊ChatX的价格", p)             # 真句子
    assert not looks_like_prompt_echo("无界科技", p)                                # 单热词、无旁证放行
    assert looks_like_prompt_echo("无界科技", p, no_speech_prob=0.7)                # 单热词 + 高 no_speech
    assert looks_like_prompt_echo("智聊ChatX", p, duration=1.47)                    # 09-12 实锤：1.47s 直吐热词
    assert looks_like_prompt_echo("智聊ChatX", p, language_probability=0.42)        # 语种都判不清
    assert not looks_like_prompt_echo("无界科技", p, duration=4.0, language_probability=0.95)
    assert not looks_like_prompt_echo("", p) and not looks_like_prompt_echo("x", "")


# ── 响应元数据 ───────────────────────────────────────────────────────────────
def test_extract_meta_from_dict_object_and_segments():
    d = extract_transcription_meta({"text": "hi", "language": "zh", "duration": 2.5,
                                    "avg_logprob": -0.4, "no_speech_prob": 0.1})
    assert d == {"text": "hi", "language": "zh", "duration": 2.5,
                 "avg_logprob": -0.4, "no_speech_prob": 0.1}
    from openai.types.audio import TranscriptionVerbose
    obj = TranscriptionVerbose.model_validate({
        "text": "hello", "language": "english", "duration": 3.0,
        "segments": [{"id": 0, "seek": 0, "start": 0, "end": 1, "text": "hello", "tokens": [],
                      "temperature": 0, "avg_logprob": -0.2, "compression_ratio": 1.1,
                      "no_speech_prob": 0.05},
                     {"id": 1, "seek": 0, "start": 1, "end": 3, "text": "there", "tokens": [],
                      "temperature": 0, "avg_logprob": -0.6, "compression_ratio": 1.3,
                      "no_speech_prob": 0.4}],
    })
    m = extract_transcription_meta(obj)
    assert m["text"] == "hello" and m["language"] == "en" and m["duration"] == 3.0
    assert abs(m["avg_logprob"] - (-0.4)) < 1e-9 and m["no_speech_prob"] == 0.4
    assert m["compression_ratio"] == 1.3
    assert extract_transcription_meta("plain") == {"text": "plain"}
    assert extract_transcription_meta(None) == {}


# ── 语种先验复核判定 ─────────────────────────────────────────────────────────
def test_should_retry_with_lang_hint_rules():
    # 检出韩语、概率低、先验中文 → 重转（0830 事故形态）
    assert should_retry_with_lang_hint({"language": "ko", "language_probability": 0.41}, "섬멸에서", "zh")
    # 检出英文但很自信 → 客户真说了英文，不重转
    assert not should_retry_with_lang_hint({"language": "en", "language_probability": 0.95}, "hello there", "zh")
    # 同家族（zh-tw / yue vs zh）→ 不重转
    assert not should_retry_with_lang_hint({"language": "zh", "language_probability": 0.3}, "你好", "zh-tw")
    assert not should_retry_with_lang_hint({"language": "yue"}, "你好", "zh")
    # 服务端没给语种 → 按文字系统判：韩文 vs 中文先验 → 重转；中文文本 → 不重转
    assert should_retry_with_lang_hint({}, "안녕하세요 반갑습니다", "zh")
    assert not should_retry_with_lang_hint({}, "你好呀今天怎么样", "zh")
    # 无先验 / 空文本 → 不重转
    assert not should_retry_with_lang_hint({"language": "ko"}, "x", "")
    assert not should_retry_with_lang_hint({"language": "ko"}, "", "zh")
    # 强制语言时 probability=1.0 → 不重转
    assert not should_retry_with_lang_hint({"language": "en", "language_probability": 1.0}, "hi", "zh")


async def test_lang_hint_retry_in_chain_adopts_matching_script(tmp_path):
    # 主级第一次（auto）转出韩语乱码 + 低概率；按先验 zh 重转得中文 → 采用中文
    a = _Impl(["섬멸에서 섬미꼬야", "我吃过了呀你吃了吗"],
              metas=[{"language": "ko", "language_probability": 0.42},
                     {"language": "zh", "language_probability": 1.0}])
    chain = FallbackTranscriber({"temp_dir": str(tmp_path / "f")}, [a])
    out = await chain.transcribe_voice_message(_ogg(tmp_path, b"\x11"), "auto", lang_hint="zh")
    assert out == "我吃过了呀你吃了吗"
    assert a.calls == ["auto", "zh"]
    assert chain.last_meta.get("lang_retry") is True and chain.last_meta.get("lang_retry_from") == "ko"
    assert chain.last_meta.get("language") == "zh" and not chain.last_meta.get("lang_suspect")
    ev = get_asr_stats().dump()["events"]
    assert ev["lang_retry"] == 1 and ev["lang_retry_changed"] == 1
    # 顶层成败只记一次
    assert get_asr_stats().dump()["attempts"] == 1


async def test_lang_hint_retry_keeps_original_when_script_still_mismatch(tmp_path):
    a = _Impl(["섬멸에서 섬미꼬야", "다시 한국어"],
              metas=[{"language": "ko", "language_probability": 0.42}, {"language": "zh"}])
    chain = FallbackTranscriber({"temp_dir": str(tmp_path / "f")}, [a])
    out = await chain.transcribe_voice_message(_ogg(tmp_path, b"\x12"), "auto", lang_hint="zh")
    assert out == "섬멸에서 섬미꼬야"          # 重转仍非中文 → 保留原转写
    assert chain.last_meta.get("lang_suspect") is True
    assert get_asr_stats().dump()["events"]["lang_retry_changed"] == 0


async def test_lang_hint_no_retry_when_confident_or_same_family(tmp_path):
    a = _Impl(["hello how are you"], metas=[{"language": "en", "language_probability": 0.97}])
    chain = FallbackTranscriber({"temp_dir": str(tmp_path / "f")}, [a])
    assert await chain.transcribe_voice_message(_ogg(tmp_path, b"\x13"), "auto", lang_hint="zh") == "hello how are you"
    assert a.calls == ["auto"]
    b = _Impl(["你好"], metas=[{"language": "zh", "language_probability": 0.5}])
    assert await b.transcribe_voice_message(_ogg(tmp_path, b"\x14"), "auto", lang_hint="zh-tw") == "你好"
    assert b.calls == ["auto"]


async def test_standalone_lang_hint_retry_records_once(tmp_path):
    t = _Impl(["안녕", "你好"], metas=[{"language": "ko", "language_probability": 0.3}, {"language": "zh"}])
    assert await t.transcribe_voice_message(_ogg(tmp_path, b"\x15"), "auto", lang_hint="zh") == "你好"
    assert t.calls == ["auto", "zh"]
    d = get_asr_stats().dump()
    assert d["primary_ok"] == 1 and d["attempts"] == 1


# ── OpenAI 兼容转录器：prompt / verbose_json / 动态超时 / 闸 ────────────────
class _FakeTranscriptions:
    def __init__(self, responder):
        self.calls = []
        self._responder = responder

    def create(self, **kw):
        kw = dict(kw)
        kw.pop("file", None)
        self.calls.append(kw)
        return self._responder(kw)


class _FakeClient:
    def __init__(self, responder):
        self.audio = type("A", (), {})()
        self.audio.transcriptions = _FakeTranscriptions(responder)


def _openai_t(**extra):
    return OpenAITranscriber({
        "temp_dir": "./temp/test_voice_p0", "provider": "openai_compatible",
        "api_key": "local", "base_url": "http://127.0.0.1:1/v1", "model": "large-v3-turbo",
        "timeout": 3, "max_retries": 0,
        "hotwords": ["智聊ChatX", "通译LingoX", "无界科技"], **extra,
    })


def _bad_request(msg="unsupported response_format"):
    req = httpx.Request("POST", "http://127.0.0.1:1/v1/audio/transcriptions")
    return openai.BadRequestError(msg, response=httpx.Response(400, request=req), body=None)


async def test_openai_sends_prompt_verbose_json_and_dynamic_timeout(tmp_path):
    t = _openai_t(send_hotwords=True)   # 热词默认关（A/B 实锤边缘音频受害），显式开
    fake = _FakeClient(lambda kw: {"text": "帮我介绍一下智聊ChatX", "language": "zh",
                                   "language_probability": 0.98, "avg_logprob": -0.3,
                                   "no_speech_prob": 0.02, "duration": 20.0})
    f = _wav(tmp_path, 20.0)  # 20s：旧逻辑 3s 必超时
    with patch.object(t, "_get_client", return_value=fake):
        out = await t.transcribe_voice_message(f, "auto")
    assert out == "帮我介绍一下智聊ChatX"
    kw = fake.audio.transcriptions.calls[0]
    assert kw["prompt"] == "智聊ChatX、通译LingoX、无界科技"
    assert kw["response_format"] == "verbose_json"
    assert kw["language"] is None                       # auto 仍传 None（勿映射成 zh）
    assert kw["timeout"] == 6.5                         # 20×0.25+1.5
    assert t.last_meta["language"] == "zh" and t.last_meta["timeout_sec"] == 6.5
    assert t.last_meta["no_speech_prob"] == 0.02 and "elapsed_ms" in t.last_meta


async def test_openai_default_no_prompt_but_verbose_json(tmp_path):
    """缺省：不送热词（09-12 A/B：prompt 让边缘音频乱码）、仍要 verbose_json 拿置信度。"""
    t = _openai_t()
    fake = _FakeClient(lambda kw: {"text": "你好", "language": "zh", "language_probability": 0.9})
    with patch.object(t, "_get_client", return_value=fake):
        assert await t.transcribe_voice_message(_wav(tmp_path, 6.0), "auto") == "你好"
    kw = fake.audio.transcriptions.calls[0]
    assert "prompt" not in kw and kw["response_format"] == "verbose_json"
    assert t.send_hotwords is False


async def test_openai_prompt_gated_by_duration_when_enabled(tmp_path):
    """开了热词也只对 ≥3s 音频送：1.47s 片段带 prompt 直吐「智聊ChatX」（09-12 实锤）。"""
    t = _openai_t(send_hotwords=True)
    fake = _FakeClient(lambda kw: {"text": "嗯"})
    with patch.object(t, "_get_client", return_value=fake):
        assert await t.transcribe_voice_message(_wav(tmp_path, 1.5), "auto") == "嗯"
        assert "prompt" not in fake.audio.transcriptions.calls[0]
        assert await t.transcribe_voice_message(_wav(tmp_path, 3.5, "b.wav"), "auto") == "嗯"
        assert fake.audio.transcriptions.calls[1]["prompt"]


async def test_openai_short_audio_keeps_base_timeout_and_foreign_lang_no_prompt(tmp_path):
    t = _openai_t(send_hotwords=True)
    fake = _FakeClient(lambda kw: {"text": "hello"})
    f = _wav(tmp_path, 4.0)
    with patch.object(t, "_get_client", return_value=fake):
        assert await t.transcribe_voice_message(f, "en") == "hello"
    kw = fake.audio.transcriptions.calls[0]
    assert kw["timeout"] == 3.0 and kw["language"] == "en"
    assert "prompt" not in kw                          # 强制外语时不送中文热词


async def test_openai_verbose_json_downgrades_sticky_on_400(tmp_path):
    t = _openai_t()

    def _resp(kw):
        if kw["response_format"] == "verbose_json":
            raise _bad_request()
        return {"text": "只认 json 的端点"}
    fake = _FakeClient(_resp)
    f = _ogg(tmp_path, b"\x21")
    with patch.object(t, "_get_client", return_value=fake):
        assert await t.transcribe_voice_message(f, "auto") == "只认 json 的端点"
        assert [c["response_format"] for c in fake.audio.transcriptions.calls] == ["verbose_json", "json"]
        assert t._verbose_json_ok is False
        # 第二条：直接 json，不再撞 400
        assert await t.transcribe_voice_message(_ogg(tmp_path, b"\x22"), "auto") == "只认 json 的端点"
    assert fake.audio.transcriptions.calls[-1]["response_format"] == "json"


async def test_openai_no_speech_gate_and_prompt_echo_dropped(tmp_path):
    t = _openai_t()
    fake = _FakeClient(lambda kw: {"text": "谢谢大家", "no_speech_prob": 0.93})
    with patch.object(t, "_get_client", return_value=fake):
        assert await t.transcribe_voice_message(_ogg(tmp_path, b"\x31"), "auto") is None
    assert t.last_error.startswith("no_speech: no_speech_prob")
    t2 = _openai_t(send_hotwords=True)
    fake2 = _FakeClient(lambda kw: {"text": "智聊ChatX、通译LingoX、无界科技", "no_speech_prob": 0.2})
    with patch.object(t2, "_get_client", return_value=fake2):
        # ogg 假字节估不出时长 → 不受 3s 闸限制，prompt 照送 → 整串吐回 → 回声丢弃
        assert await t2.transcribe_voice_message(_ogg(tmp_path, b"\x32"), "auto") is None
    assert t2.last_error == "no_speech: prompt_echo"
    ev = get_asr_stats().dump()["events"]
    assert ev["no_speech_gate"] == 1 and ev["prompt_echo_dropped"] == 1


async def test_openai_low_confidence_flag(tmp_path):
    t = _openai_t()
    fake = _FakeClient(lambda kw: {"text": "大造成呢", "language": "zh",
                                   "language_probability": 0.9, "avg_logprob": -1.4})
    with patch.object(t, "_get_client", return_value=fake):
        assert await t.transcribe_voice_message(_ogg(tmp_path, b"\x41"), "auto") == "大造成呢"
    assert t.last_meta.get("low_confidence") is True
    assert get_asr_stats().dump()["events"]["low_confidence"] == 1


async def test_openai_send_hotwords_opt_out(tmp_path):
    t = _openai_t(send_hotwords=False, response_format="json")
    fake = _FakeClient(lambda kw: {"text": "ok"})
    with patch.object(t, "_get_client", return_value=fake):
        assert await t.transcribe_voice_message(_ogg(tmp_path, b"\x51"), "auto") == "ok"
    kw = fake.audio.transcriptions.calls[0]
    assert "prompt" not in kw and kw["response_format"] == "json"
