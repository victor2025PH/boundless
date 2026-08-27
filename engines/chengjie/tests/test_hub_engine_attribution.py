"""hub 引擎归属门禁（2026-08-22「fish 冒充 IndexTTS-2」事故）。

事故：hub `/api/tts_only` 是 **prefer 语义**——点名的引擎在目录里不可用就静默换一个
合成，且响应信封**没有任何引擎字段**。于是「HTTP 200 + 音频有效 + 合成成功」全链
全绿，客户听到的却是另一个人的声音（44.1kHz fish_speech 冒充 22.05kHz IndexTTS-2）。

唯一可用的正面反证＝**采样率指纹**（写在 wav 头里）。这批门禁钉住三层：
① 判定纯函数「只有拿到正面反证才判 mismatch」（宁可漏判绝不误杀）；
② `_engine_gate` 拦截语义（strict 拦 / lenient 记 / 计数入观测）；
③ **strict 人设强制要 wav**——ogg 直出会被 hub 转码重采样到 48k 并改写 OpusHead
   的原采样率字段，指纹被抹平＝热路根本查不出冒名（这是本事故能潜伏的直接原因）。
"""

import asyncio
import struct

import pytest

from src.ai.avatar_voice import (
    HubEngineMismatch,
    engine_attribution,
    engine_of_sample_rate,
    normalize_engine_name,
    wav_sample_rate,
)
from src.ai.tts_pipeline import TTSPipeline, TTSResult


def _wav(sample_rate: int, *, seconds: float = 0.2, amp: int = 6000) -> bytes:
    """最小可解析 WAV（16bit 单声道方波）。

    amp 默认非零：B61 起管线带哑音闸（零能量产物拦下重试），指纹类测试的
    夹具必须模拟**真实有声**音频；amp=0 专供哑音检测测试造无声样本。
    """
    n = max(1, int(sample_rate * seconds))
    if amp <= 0:
        data = b"\x00\x00" * n
    else:
        hi = struct.pack("<h", amp)
        lo = struct.pack("<h", -amp)
        chunk = hi * 20 + lo * 20
        data = (chunk * (n // 40 + 1))[: n * 2]
    return (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(data)) + data
    )


# ── ① 判定纯函数 ────────────────────────────────────────────────────────────

def test_normalize_engine_name_covers_written_variants():
    # 配置/hub 目录/日志里同一引擎写法五花八门，漏一种＝该次合成静默不校验
    for name in ("index_tts", "IndexTTS-2", "indextts2", "Index TTS", "index-tts-v2"):
        assert normalize_engine_name(name) == "index_tts", name
    assert normalize_engine_name("fish-speech") == "fish_speech"
    assert normalize_engine_name("MOSS_TTSD") == "moss_ttsd"
    assert normalize_engine_name("") == ""
    # 认不出的引擎不得被硬塞进已知引擎（否则会拿错指纹误判）
    assert normalize_engine_name("some_new_tts") not in ("index_tts", "fish_speech")


def test_wav_sample_rate_and_engine_map():
    assert wav_sample_rate(_wav(22050)) == 22050
    assert wav_sample_rate(_wav(44100)) == 44100
    assert wav_sample_rate(b"OggS" + b"\x00" * 200) is None
    assert wav_sample_rate(b"") is None
    assert engine_of_sample_rate(24000) == "moss_ttsd"
    assert engine_of_sample_rate(48000) == ""  # hub 转码产物，不构成指认


def test_engine_attribution_mismatch_is_the_accident_case():
    verdict, detail = engine_attribution(_wav(44100), "wav", "index_tts")
    assert verdict == "mismatch"
    assert "fish_speech" in detail and "index_tts" in detail


def test_engine_attribution_ok():
    verdict, _ = engine_attribution(_wav(22050), "wav", "IndexTTS-2")
    assert verdict == "ok"


@pytest.mark.parametrize("case", [
    "no_engine_pinned",     # 没钉引擎 → 无从判
    "engine_unregistered",  # 引擎不在指纹表
    "ogg_fingerprint_lost",  # opus 恒 48k，指纹已被 hub 转码抹平
    "wav_header_broken",
    "sample_rate_unknown",  # 采样率不在指纹表 → 不构成指认
])
def test_engine_attribution_unknown_never_blocks(case):
    """判不了 ≠ 判失败。误杀正常语音比漏判更伤（会让整个人设哑掉）。"""
    audio, fmt, expected = {
        "no_engine_pinned": (_wav(44100), "wav", ""),
        "engine_unregistered": (_wav(44100), "wav", "some_new_tts"),
        "ogg_fingerprint_lost": (b"OggS" + b"\x00" * 200, "ogg", "index_tts"),
        "wav_header_broken": (b"RIFF" + b"\x00" * 8, "wav", "index_tts"),
        "sample_rate_unknown": (_wav(16000), "wav", "index_tts"),
    }[case]
    verdict, _ = engine_attribution(audio, fmt, expected)
    assert verdict == "unknown"


# ── ② _engine_gate 拦截语义 ─────────────────────────────────────────────────

def _pipeline(**hub_fish) -> TTSPipeline:
    hf = {"enabled": True, "base_url": "http://hub.invalid:9000",
          "tts_engine": "index_tts"}
    hf.update(hub_fish)
    return TTSPipeline({
        "enabled": True, "backend": "avatar_clone", "persona_id": "p1",
        "avatar_voice": {"enabled": True, "hub_fish": hf,
                         "voice_consistency": hub_fish.pop("_consistency", "strict")},
    })


def test_engine_gate_blocks_and_marks_when_verify_on():
    from src.ai.avatar_voice_stats import get_avatar_voice_stats
    st = get_avatar_voice_stats()
    before = int((st.dump().get("engine_check") or {}).get("mismatch", 0))
    rv = TTSResult()
    with pytest.raises(HubEngineMismatch):
        _pipeline()._engine_gate(
            rv, _wav(44100), "wav", expected="index_tts", verify=True,
            profile="p1")
    assert rv.extra["hub_engine_verdict"] == "mismatch"
    # 分段路据此**不再回落整段单发**（那条路多是 ogg＝查不出来）
    assert rv.extra.get("hub_engine_blocked")
    after = int((st.dump().get("engine_check") or {}).get("mismatch", 0))
    assert after == before + 1


def test_engine_gate_records_only_when_verify_off():
    """verify_engine=false＝hub 侧排障档：只记不拦，语音照发。"""
    rv = TTSResult()
    _pipeline()._engine_gate(
        rv, _wav(44100), "wav", expected="index_tts", verify=False, profile="p1")
    assert rv.extra["hub_engine_verdict"] == "mismatch"
    assert rv.extra.get("hub_engine_mismatch")
    assert not rv.extra.get("hub_engine_blocked")


def test_engine_gate_noop_on_ok_and_unknown():
    for audio, fmt in ((_wav(22050), "wav"), (b"OggS" + b"\x00" * 200, "ogg")):
        rv = TTSResult()
        _pipeline()._engine_gate(
            rv, audio, fmt, expected="index_tts", verify=True, profile="p1")
        assert rv.extra["hub_engine_verdict"] in ("ok", "unknown")
        assert "hub_engine_mismatch" not in rv.extra


# ── ③ strict 人设强制 wav（否则热路根本没有指纹可查）────────────────────────

def _run_hub(monkeypatch, tmp_path, *, sample_rate: int, **hub_fish):
    """跑一次 `_try_hub_fish`，回 (结果, 实际请求的 audio_format, rv)。"""
    import src.ai.avatar_voice as av
    seen = {}

    def fake_synth(base_url, profile, text, **kw):
        seen["audio_format"] = kw.get("audio_format")
        seen["tts_engine"] = kw.get("tts_engine")
        return _wav(sample_rate), "wav"

    monkeypatch.setattr(av, "hub_fish_synthesize", fake_synth)
    pipe = _pipeline(**hub_fish)
    rv = TTSResult(text="你好呀，今天过得怎么样")
    res = asyncio.run(pipe._try_hub_fish(rv, tmp_path / "o", 0.0))
    return res, seen, rv


def test_strict_persona_forces_wav_to_keep_the_fingerprint(monkeypatch, tmp_path):
    """ogg 直出省一次本机转码，但 hub 转 opus 会重采样 48k + 改写 OpusHead 原采样率
    字段 → 冒名在热路彻底查不出来。strict 人设一律换成 wav 换「可验证」。"""
    res, seen, rv = _run_hub(
        monkeypatch, tmp_path, sample_rate=22050, response_format="ogg")
    assert seen["audio_format"] == "wav"
    assert rv.extra.get("hub_format_forced_wav") is True
    assert res is not None and res.ok


def test_lenient_persona_keeps_ogg_passthrough(monkeypatch, tmp_path):
    """非 strict 不付这个代价（换声风险由 lenient 的本地克隆兜底承担）。"""
    _, seen, rv = _run_hub(
        monkeypatch, tmp_path, sample_rate=22050, response_format="ogg",
        _consistency="lenient")
    assert seen["audio_format"] == "ogg"
    assert "hub_format_forced_wav" not in rv.extra


def test_verify_off_keeps_ogg_passthrough(monkeypatch, tmp_path):
    _, seen, _ = _run_hub(
        monkeypatch, tmp_path, sample_rate=22050, response_format="ogg",
        verify_engine=False)
    assert seen["audio_format"] == "ogg"


def test_unfingerprintable_engine_keeps_ogg_passthrough(monkeypatch, tmp_path):
    """引擎没登记指纹时要 wav 只是白付转码——保持旧行为。"""
    _, seen, _ = _run_hub(
        monkeypatch, tmp_path, sample_rate=22050, response_format="ogg",
        tts_engine="some_new_tts")
    assert seen["audio_format"] == "ogg"


def test_hub_engine_mismatch_fails_hub_so_strict_falls_back_to_text(
        monkeypatch, tmp_path):
    """事故重演：hub 拿 fish 顶包 → 判 hub 失败；strict 的 hub_fish_required 会挡住
    本地 7852 顶班（另一份参考音=又一次换声），调用方据此回落文字。"""
    res, _, rv = _run_hub(
        monkeypatch, tmp_path, sample_rate=44100, response_format="ogg")
    assert res is None
    assert rv.extra.get("hub_fish_required") is True
    assert rv.extra.get("hub_engine_verdict") == "mismatch"


# ── ④ 错误码分得开（告警别把运维引向绿着的 /health）─────────────────────────

@pytest.mark.asyncio
async def test_synthesize_reports_engine_mismatch_as_its_own_error_code(
        monkeypatch, tmp_path):
    """端到端：strict + hub 换引擎 → 硬失败，且错误码**不是**泛用的
    `hub_voice_source_unavailable`——那个码会让运维去看 /health（此时正绿着），
    正是本次事故潜伏两小时的原因。码经 voice_outage 台账进断档告警的原因 Top。"""
    import src.ai.avatar_voice as av
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav(22050))
    pipe = _pipeline()
    pipe.voice_profile = {
        "enabled": True, "owner_consent": True, "backend": "avatar_clone",
        "reference_audio_path": str(ref)}
    pipe.fallback_on_error = False
    monkeypatch.setattr(
        av, "hub_fish_synthesize", lambda *a, **k: (_wav(44100), "wav"))
    rv = await pipe.synthesize("你好呀，今天过得怎么样", emotion=None)
    assert rv.ok is False
    assert rv.error.startswith("hub_engine_mismatch:")
    assert "fish_speech" in rv.error          # 冒名者写进码里，一眼定位


def test_outage_alert_text_points_at_the_engine_not_at_a_green_health():
    """告警正文必须把运维引向**引擎目录**：本次事故里 hub `/health` 全程绿着，
    通用排查语「7852 掉线/音色档 404」把人引偏了两小时。"""
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("voice_outage_alert", {
        "attempts": 5, "window_hours": 24, "consecutive_fails": 5,
        "top_reasons": {
            "hub_engine_mismatch:期望 index_tts@22050Hz，实得 fish_speech@44100Hz": 5},
        "last_ok_hours": 3.2,
    })
    assert "引擎冒名" in text
    assert "fish_speech" in text
    assert "available" in text            # 指向 hub 引擎目录
    assert "7852 掉线" not in text         # 误导性通用排查语必须让位


def test_outage_alert_keeps_generic_hint_when_not_a_mismatch():
    from src.inbox.webhook_notifier import _build_message
    _, text = _build_message("voice_outage_alert", {
        "attempts": 5, "window_hours": 24, "consecutive_fails": 5,
        "top_reasons": {"hub_voice_source_unavailable": 5}, "last_ok_hours": -1,
    })
    assert "引擎冒名" not in text
    assert "7852 掉线" in text


def test_engine_mismatch_classifies_to_the_same_agent_facing_bucket():
    """码分两种、桶只有一个：坐席该做的事一样（改用系统通用音色/等运维）。"""
    from src.ai.tts_pipeline import (
        HUB_SOURCE_ERROR_MARKERS,
        classify_voice_error,
    )
    assert classify_voice_error(
        "hub_engine_mismatch:期望 index_tts@22050Hz，实得 fish_speech@44100Hz"
    ) == "hub_source_down"
    assert classify_voice_error("hub_voice_source_unavailable") == "hub_source_down"
    # hub 风险预告（voice-context）与坐席分类共用同一份码族，勿各自硬编
    assert "hub_engine_mismatch" in HUB_SOURCE_ERROR_MARKERS


# ── ⑤ B61 哑音闸（2026-08-23 `_309`/`_311`：全链 200、真字节、播放无声）───────

def test_detect_silent_audio_wav_verdicts():
    from src.ai.avatar_voice import detect_silent_audio
    assert detect_silent_audio(_wav(22050, amp=0), "wav") is True    # 零能量
    assert detect_silent_audio(_wav(22050), "wav") is False          # 真语音级能量
    assert detect_silent_audio(_wav(22050, amp=200), "wav") is False  # 低但非零
    assert detect_silent_audio(b"", "wav") is None                   # 判不了
    assert detect_silent_audio(b"RIFF" + b"\x00" * 8, "wav") is None  # 头坏


def test_detect_silent_audio_unparseable_compressed_is_none():
    """压缩容器解不开（ffmpeg 缺席/字节非法）→ None＝放行，绝不误拦。"""
    from src.ai.avatar_voice import detect_silent_audio
    assert detect_silent_audio(b"OggS" + b"\x00" * 64, "ogg") is None


def test_hub_silent_audio_retries_once_then_fails(monkeypatch, tmp_path):
    """哑音首见重试一次；复发→判合成失败走回落链，绝不把无声音频发出去。"""
    import src.ai.avatar_voice as av
    calls = {"n": 0}

    def fake_synth(*a, **k):
        calls["n"] += 1
        return _wav(22050, amp=0), "wav"

    monkeypatch.setattr(av, "hub_fish_synthesize", fake_synth)
    pipe = _pipeline()
    rv = TTSResult(text="你好呀，今天过得怎么样")
    res = asyncio.run(pipe._try_hub_fish(rv, tmp_path / "o", 0.0))
    assert res is None
    assert calls["n"] == 2
    assert rv.extra.get("hub_silent_audio") == 2


def test_hub_silent_audio_retry_recovers(monkeypatch, tmp_path):
    import src.ai.avatar_voice as av
    seq = [_wav(22050, amp=0), _wav(22050)]

    def fake_synth(*a, **k):
        return seq.pop(0), "wav"

    monkeypatch.setattr(av, "hub_fish_synthesize", fake_synth)
    pipe = _pipeline()
    rv = TTSResult(text="你好呀，今天过得怎么样")
    res = asyncio.run(pipe._try_hub_fish(rv, tmp_path / "o", 0.0))
    assert res is not None and res.ok
    assert rv.extra.get("hub_silent_audio") == 1  # 首见记录在案（观测留痕）
