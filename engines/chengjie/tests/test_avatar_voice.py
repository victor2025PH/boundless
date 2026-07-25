"""avatar_voice（AvatarHub 7852/7858/7854 客户端）单测。

覆盖：纯函数（payload 构建/响应解析/情绪映射/令牌读取/逐字稿发现）、
GPU 串行锁语义、重试语义、TTSPipeline avatar_clone 后端接线（mock HTTP，
成功/不可达回落/配置类硬失败）、AvatarWhisperTranscriber（mock HTTP）。
全部离线可跑，零真实网络。
"""
from __future__ import annotations

import base64
import io
import json
import threading
import time
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from src.ai.avatar_voice import (
    AVATAR_EMOTIONS,
    AvatarVoiceClient,
    build_batch_payload,
    build_clone_payload,
    build_instruct_payload,
    build_stt_payload,
    build_tts_only_payload,
    find_reference_text,
    hub_fish_synthesize,
    load_reference_b64,
    normalize_avatar_emotion,
    parse_audio_response,
    parse_batch_response,
    parse_stt_response,
    parse_tts_only_response,
    read_service_token,
    reset_caches,
    sniff_audio_format,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    reset_caches()
    yield
    reset_caches()


def _wav_bytes(ms: int = 200, rate: int = 24000) -> bytes:
    """生成一段有效 WAV（静音）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * ms / 1000))
    return buf.getvalue()


# ── 纯函数：payload 构建 ──────────────────────────────────────────────────────
def test_build_clone_payload_shape():
    body = json.loads(build_clone_payload(
        text="你好呀", reference_audio_b64="QUJD", reference_text="参考稿",
        emotion="gentle", speed=1.0).decode("utf-8"))
    assert body == {
        "text": "你好呀", "reference_audio_b64": "QUJD",
        "reference_text": "参考稿", "emotion": "gentle",
        "speed": 1.0, "return_base64": True, "prosody_variation": True,
    }


def test_build_clone_payload_prosody_fields():
    body = json.loads(build_clone_payload(
        text="你好呀", reference_audio_b64="QUJD", flow_temperature=1.12,
        llm_top_k=40, prosody_variation=False).decode("utf-8"))
    assert body["flow_temperature"] == 1.12
    assert body["llm_top_k"] == 40
    assert body["prosody_variation"] is False
    assert body["flow_temperature"] <= 1.18


def test_build_clone_payload_omits_empty_ref_text_and_normalizes_emotion():
    body = json.loads(build_clone_payload(
        text="t", reference_audio_b64="QQ==", emotion="不存在").decode("utf-8"))
    assert "reference_text" not in body
    assert body["emotion"] == "neutral"  # 未知情绪 → 默认 neutral（音色保真路径）


def test_build_instruct_payload_shape():
    body = json.loads(build_instruct_payload(
        text="悄悄话", instruct="用小声耳语的语气说",
        reference_audio_b64="QQ==").decode("utf-8"))
    assert body["instruct"] == "用小声耳语的语气说"
    assert body["return_base64"] is True
    assert "emotion" not in body


def test_build_batch_payload_shape():
    body = json.loads(build_batch_payload(
        texts=["早安呀", "晚安"], reference_audio_b64="QQ==",
        reference_text="稿", language="zh").decode("utf-8"))
    assert body["texts"] == ["早安呀", "晚安"]
    assert body["language"] == "zh"


def test_build_stt_payload_roundtrip():
    body = json.loads(build_stt_payload(b"\x01\x02", language="zh").decode())
    assert base64.b64decode(body["audio_base64"]) == b"\x01\x02"
    assert body["language"] == "zh"


# ── 纯函数：情绪规整 ─────────────────────────────────────────────────────────
def test_normalize_avatar_emotion():
    for e in AVATAR_EMOTIONS:
        assert normalize_avatar_emotion(e) == e
    assert normalize_avatar_emotion("GENTLE") == "gentle"
    assert normalize_avatar_emotion("") == "neutral"    # 保真路径默认
    assert normalize_avatar_emotion(None) == "neutral"
    assert normalize_avatar_emotion("bogus", default="calm") == "calm"
    assert normalize_avatar_emotion("bogus", default="也不存在") == "neutral"


def test_to_cosyvoice_emotion_mapping():
    """音色保真语义（2026-07-13）：弱情绪(<0.7)归 neutral=zero_shot 保真路径；
    强情绪(≥0.7)才出情感标签切 instruct2（音色换表现力）。"""
    from src.ai.voice_emotion import EmotionSpec, NEUTRAL, to_cosyvoice_emotion
    # 强情绪 → 情感标签
    assert to_cosyvoice_emotion(EmotionSpec("warm", intensity=0.8)) == "gentle"
    assert to_cosyvoice_emotion(EmotionSpec("playful", intensity=0.75)) == "happy"
    assert to_cosyvoice_emotion(EmotionSpec("excited", intensity=0.9)) == "excited"
    assert to_cosyvoice_emotion(EmotionSpec("sad", intensity=0.7)) == "sad"
    # 弱情绪（默认 intensity=0.6）→ 保真路径
    assert to_cosyvoice_emotion(EmotionSpec("warm")) == "neutral"
    assert to_cosyvoice_emotion(EmotionSpec("sad", intensity=0.5)) == "neutral"
    assert to_cosyvoice_emotion(EmotionSpec("empathetic")) == "neutral"
    # neutral / None → 保真
    assert to_cosyvoice_emotion(NEUTRAL) == "neutral"
    assert to_cosyvoice_emotion(NEUTRAL, default="calm") == "calm"
    assert to_cosyvoice_emotion(None) == "neutral"
    # 阈值可调
    assert to_cosyvoice_emotion(
        EmotionSpec("sad", intensity=0.6), strong_threshold=0.5) == "sad"


def test_cosyvoice_speed_pace():
    # Phase7 起情绪携带默认速度曲线（sad 0.92/excited 1.08），pace 相乘微调；
    # 详见 test_avatar_voice_phase7.py::test_cosyvoice_speed_emotion_curve。
    from src.ai.voice_emotion import EmotionSpec, cosyvoice_speed
    assert cosyvoice_speed(EmotionSpec("warm", pace="slow")) == 0.95
    assert cosyvoice_speed(EmotionSpec("warm", pace="fast")) == 1.05
    assert cosyvoice_speed(EmotionSpec("warm")) == 1.0
    assert cosyvoice_speed(None) == 1.0


# ── 纯函数：响应解析 ─────────────────────────────────────────────────────────
def test_parse_audio_response_json_b64():
    wav = _wav_bytes()
    body = json.dumps({
        "audio_base64": base64.b64encode(wav).decode(),
        "sample_rate": 24000, "elapsed_ms": 2100}).encode()
    assert parse_audio_response(body) == wav


def test_parse_audio_response_raw_bytes_passthrough():
    raw = b"\x00\x01\x02notjson"
    assert parse_audio_response(raw) == raw


def test_parse_audio_response_errors():
    with pytest.raises(RuntimeError, match="empty"):
        parse_audio_response(b"")
    with pytest.raises(RuntimeError, match="boom"):
        parse_audio_response(json.dumps({"ok": False, "error": "boom"}).encode())
    with pytest.raises(RuntimeError, match="no audio"):
        parse_audio_response(json.dumps({"ok": True}).encode())


def test_parse_batch_response_order_and_errors():
    w1, w2 = _wav_bytes(100), _wav_bytes(150)
    body = json.dumps({
        "ok": True, "sample_rate": 24000,
        "results": [
            {"audio_base64": base64.b64encode(w1).decode(), "seconds": 0.1},
            {"audio_base64": base64.b64encode(w2).decode(), "seconds": 0.15},
        ]}).encode()
    out = parse_batch_response(body)
    assert out == [w1, w2]  # 与 texts 等长同序
    with pytest.raises(RuntimeError):
        parse_batch_response(json.dumps({"ok": False, "error": "x"}).encode())
    with pytest.raises(RuntimeError, match="item 0"):
        parse_batch_response(json.dumps({"ok": True, "results": [{}]}).encode())


def test_parse_stt_response_variants():
    ok = json.dumps({"ok": True, "text": " 你好 ", "no_speech_prob": 0.01}).encode()
    assert parse_stt_response(ok) == "你好"
    # 静音置信过高 → None（防幻觉）
    silent = json.dumps({"ok": True, "text": "谢谢观看", "no_speech_prob": 0.97}).encode()
    assert parse_stt_response(silent) is None
    assert parse_stt_response(json.dumps({"ok": False}).encode()) is None
    assert parse_stt_response(json.dumps({"ok": True, "text": ""}).encode()) is None
    assert parse_stt_response(b"") is None
    assert parse_stt_response(b"not json") is None


# ── 令牌 / 参考音 / 逐字稿 ───────────────────────────────────────────────────
def test_read_service_token_runtime_and_cache(tmp_path):
    f = tmp_path / "service_token.txt"
    f.write_text("sekret-123\n", encoding="utf-8")
    assert read_service_token(str(f)) == "sekret-123"
    assert read_service_token(str(tmp_path / "nope.txt")) == ""
    assert read_service_token("") == ""


def test_load_reference_b64_cache_invalidates_on_change(tmp_path):
    f = tmp_path / "ref.wav"
    f.write_bytes(b"AAA")
    b1 = load_reference_b64(str(f))
    assert base64.b64decode(b1) == b"AAA"
    time.sleep(0.02)
    f.write_bytes(b"BBBB")  # size 变 → 缓存必失效
    assert base64.b64decode(load_reference_b64(str(f))) == b"BBBB"
    with pytest.raises(RuntimeError, match="reference_audio_missing"):
        load_reference_b64(str(tmp_path / "missing.wav"))


def test_find_reference_text_sidecar(tmp_path):
    wav = tmp_path / "ref.wav"
    wav.write_bytes(b"x")
    assert find_reference_text(str(wav)) == ""  # 无 sidecar → 空
    (tmp_path / "ref.txt").write_text(" 参考音的逐字稿 \n", encoding="utf-8")
    assert find_reference_text(str(wav)) == "参考音的逐字稿"
    assert find_reference_text("") == ""


# ── 客户端：串行锁 + 重试 ────────────────────────────────────────────────────
def _client(**over) -> AvatarVoiceClient:
    cfg = {"enabled": True, "retries": 1, "chunk_max_chars": 0}
    cfg.update(over)
    return AvatarVoiceClient(cfg)


def test_tts_serializes_via_gpu_lock():
    """3 个并发 tts() 必须串行执行（并发峰值 =1）。"""
    c = _client()
    wav = _wav_bytes()
    active = {"n": 0, "peak": 0}
    lk = threading.Lock()

    def fake_post(url, payload, *, timeout, headers=None):
        with lk:
            active["n"] += 1
            active["peak"] = max(active["peak"], active["n"])
        time.sleep(0.05)  # 模拟 GPU 占用
        with lk:
            active["n"] -= 1
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    with patch.object(AvatarVoiceClient, "_post", side_effect=fake_post):
        threads = [
            threading.Thread(target=lambda: c.tts(
                f"第{i}条", reference_audio_b64="QQ=="))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    assert active["peak"] == 1  # 串行：任意时刻至多一个请求在途


def test_post_with_retry_retries_once_then_succeeds():
    c = _client(retries=1)
    calls = {"n": 0}

    def flaky(url, payload, *, timeout, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection reset")
        return json.dumps(
            {"audio_base64": base64.b64encode(b"OK").decode()}).encode()

    with patch.object(AvatarVoiceClient, "_post", side_effect=flaky):
        out = c.tts("hi", reference_audio_b64="QQ==")
    assert out == b"OK"
    assert calls["n"] == 2  # 失败 1 次 + 重试成功


def test_post_with_retry_gives_up_after_retries():
    c = _client(retries=1)
    with patch.object(
        AvatarVoiceClient, "_post", side_effect=OSError("dead")
    ) as mock_post:
        with pytest.raises(OSError):
            c.tts("hi", reference_audio_b64="QQ==")
    assert mock_post.call_count == 2  # 1 原始 + 1 重试，不无限重试


# ── B1+ 忙碌感知多端点路由（2026-07-21 三端点扩容）─────────────────────────
def _fake_audio_post(seen):
    wav = _wav_bytes()

    def fake_post(url, payload, *, timeout, headers=None):
        seen.append(url)
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    return fake_post


def test_post_any_priority_order_when_all_free():
    """全部端点空闲 → 仍按配置优先级走主端点（主力机优先不变）。"""
    c = _client(retries=0, base_urls=[
        "http://10.9.0.1:7852", "http://10.9.0.2:7852"])
    seen: list = []
    with patch.object(AvatarVoiceClient, "_health_ok_base", return_value=True), \
            patch.object(AvatarVoiceClient, "_post",
                         side_effect=_fake_audio_post(seen)):
        c.tts("hi", reference_audio_b64="QQ==")
    assert seen and seen[0].startswith("http://10.9.0.1")


def test_post_any_busy_aware_routes_to_free_endpoint():
    """主端点 GPU 锁被占 → 请求派给空闲备用端点（并行合成而非排队）。"""
    from src.ai.avatar_voice import _gpu_lock_for
    c = _client(retries=0, base_urls=[
        "http://10.9.1.1:7852", "http://10.9.1.2:7852"])
    seen: list = []
    lock_main = _gpu_lock_for("http://10.9.1.1:7852")
    done = {"ok": False}

    def _run():
        c.tts("hi", reference_audio_b64="QQ==")
        done["ok"] = True

    with patch.object(AvatarVoiceClient, "_health_ok_base", return_value=True), \
            patch.object(AvatarVoiceClient, "_post",
                         side_effect=_fake_audio_post(seen)):
        lock_main.acquire()  # 模拟主端点正在合成中
        try:
            t = threading.Thread(target=_run)
            t.start()
            t.join(timeout=5)
        finally:
            lock_main.release()
    assert done["ok"], "主端点忙时请求不应排队等待（应切空闲端点）"
    assert seen and seen[0].startswith("http://10.9.1.2")


def test_tts_long_text_chunked_and_merged():
    """长文本按句切块逐块合成，产物为合法 WAV 且时长≈各块之和。"""
    c = _client(chunk_max_chars=20, chunk_gap_ms=0)
    wav = _wav_bytes(100)
    seen: list = []

    def fake_post(url, payload, *, timeout, headers=None):
        seen.append(json.loads(payload.decode())["text"])
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    long_text = "第一句话在这里。第二句话也在这里。第三句话让它超过二十个字符限制。"
    with patch.object(AvatarVoiceClient, "_post", side_effect=fake_post):
        out = c.tts(long_text, reference_audio_b64="QQ==")
    assert len(seen) >= 2  # 确实切块了
    with wave.open(io.BytesIO(out), "rb") as w:
        assert w.getnframes() == len(seen) * int(24000 * 100 / 1000)


def test_stt_missing_token_returns_none(tmp_path):
    c = _client(stt={"token_file": str(tmp_path / "absent.txt")})
    assert c.stt(b"audio") is None  # 令牌缺失 → 降级不抛


def test_stt_sends_token_header(tmp_path):
    tok = tmp_path / "t.txt"
    tok.write_text("tok-abc", encoding="utf-8")
    c = _client(stt={"token_file": str(tok)})
    captured = {}

    def fake_post(url, payload, *, timeout, headers=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return json.dumps({"ok": True, "text": "你好", "no_speech_prob": 0.0}).encode()

    with patch.object(AvatarVoiceClient, "_post", side_effect=fake_post):
        assert c.stt(b"wavbytes") == "你好"
    assert captured["headers"].get("X-AH-Svc") == "tok-abc"
    assert captured["url"].endswith("/transcribe_b64")


def test_register_spk_idempotent():
    c = _client()
    with patch.object(
        AvatarVoiceClient, "_post",
        return_value=json.dumps({"ok": True}).encode(),
    ) as mock_post:
        assert c.register_spk("QUJDREVG") is True
        assert c.register_spk("QUJDREVG") is True  # 第二次直接命中缓存
    assert mock_post.call_count == 1


def test_health_parsers():
    c = _client()
    with patch("urllib.request.urlopen") as mock_open:
        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"ok": True, "models_loaded": True, "device": "cuda"}).encode()

        mock_open.return_value = _R()
        d = c.health()
    assert d == {"reachable": True, "models_loaded": True}


def test_qwen_health_parser():
    c = _client()
    with patch("urllib.request.urlopen") as mock_open:
        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"status": "ok", "model_loaded": True}).encode()

        mock_open.return_value = _R()
        d = c.qwen_health()
    assert d == {"reachable": True, "models_loaded": True}


def test_health_unreachable():
    c = _client(base_url="http://127.0.0.1:1", health_timeout_sec=0.2)
    d = c.health()
    assert d["reachable"] is False
    assert c.health_ok(use_cache=False) is False


# ── TTSPipeline avatar_clone 后端接线 ────────────────────────────────────────
def _pipeline_cfg(tmp_path, ref: Path) -> dict:
    return {
        "enabled": True,
        "backend": "avatar_clone",
        "format": "wav",
        "out_dir": str(tmp_path / "out"),
        "fallback_on_error": False,   # 单测里关兜底，暴露真实结果
        "tts_cache": {"enabled": False},
        "voice_profile": {
            "enabled": True,
            "owner_consent": True,
            "backend": "avatar_clone",
            "reference_audio_path": str(ref),
        },
        "avatar_voice": {"enabled": True, "cloud_fallback": False,
                         "chunk_max_chars": 0, "retries": 0},
    }


@pytest.mark.asyncio
async def test_pipeline_avatar_clone_success(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    (tmp_path / "ref.txt").write_text("参考稿", encoding="utf-8")
    wav = _wav_bytes(500)
    sent = {}

    def fake_post(self, url, payload, *, timeout, headers=None):
        sent["url"] = url
        sent["body"] = json.loads(payload.decode())
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(_pipeline_cfg(tmp_path, ref))
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await tts.synthesize("你好呀，今天想我了吗？", emotion="warm")

    assert rv.ok, rv.error
    assert rv.provider == "avatar_clone"
    assert rv.format == "wav"
    assert Path(rv.audio_path).is_file()
    assert rv.duration_sec > 0.4
    assert sent["url"].endswith("/v1/tts/clone")
    # warm(intensity=0.6) 弱情绪 → neutral 保真路径（音色最像；情绪由标记+speed 表达）
    assert sent["body"]["emotion"] == "neutral"
    assert sent["body"]["reference_text"] == "参考稿"  # sidecar 自动带上=zero_shot 保真


@pytest.mark.asyncio
async def test_pipeline_avatar_clone_instruct_channel(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg(tmp_path, ref)
    cfg["voice_profile"]["instruct"] = "用小声耳语的语气说"
    wav = _wav_bytes(400)
    sent = {}

    def fake_post(self, url, payload, *, timeout, headers=None):
        sent["url"] = url
        sent["body"] = json.loads(payload.decode())
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await tts.synthesize("悄悄告诉你一件事")
    assert rv.ok
    assert sent["url"].endswith("/v1/tts/instruct")
    assert sent["body"]["instruct"] == "用小声耳语的语气说"


@pytest.mark.asyncio
async def test_pipeline_avatar_clone_unreachable_falls_back_to_edge(tmp_path):
    """7852 不可达 → 回落 edge_tts（优雅降级不崩溃）。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg(tmp_path, ref)
    cfg["fallback_on_error"] = True
    cfg["avatar_voice"]["cloud_fallback"] = True

    async def fake_edge(self, text, out, voice, spec=None):
        Path(out).write_bytes(b"MP3FAKE")

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch.object(AvatarVoiceClient, "health_ok", return_value=False), \
         patch.object(TTSPipeline, "_edge_tts", fake_edge):
        rv = await tts.synthesize("测试降级")
    assert rv.ok
    assert rv.provider == "edge_tts"
    assert rv.extra.get("fallback_from") == "avatar_clone"


@pytest.mark.asyncio
async def test_pipeline_avatar_clone_config_errors_not_masked(tmp_path):
    """缺同意/缺参考音是配置类硬失败：暴露错误，绝不用通用音色掩盖。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(200))
    cfg = _pipeline_cfg(tmp_path, ref)
    cfg["fallback_on_error"] = True  # 即使开了兜底也不得掩盖
    cfg["voice_profile"]["owner_consent"] = False

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    rv = await tts.synthesize("hi")
    assert not rv.ok
    assert "owner_consent" in rv.error


@pytest.mark.asyncio
async def test_pipeline_avatar_clone_synth_error_falls_back(tmp_path):
    """健康但合成失败（重试后仍败）→ 回落 edge。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(200))
    cfg = _pipeline_cfg(tmp_path, ref)
    cfg["fallback_on_error"] = True
    cfg["avatar_voice"]["cloud_fallback"] = True

    async def fake_edge(self, text, out, voice, spec=None):
        Path(out).write_bytes(b"MP3FAKE")

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", side_effect=OSError("boom")), \
         patch.object(TTSPipeline, "_edge_tts", fake_edge):
        rv = await tts.synthesize("测试合成失败降级")
    assert rv.ok
    assert rv.provider == "edge_tts"


# ── 幻声 hub Fish-Speech 高保真（Phase B, /api/tts_only）────────────────────────
def test_build_tts_only_payload_omits_neutral_and_short():
    """neutral 情绪不下发（走保真默认）；best_of<=1 不下发；language 空不下发。"""
    body = json.loads(build_tts_only_payload("lin_jiaxin", "你好"))
    assert body == {"profile": "lin_jiaxin", "text": "你好"}
    body2 = json.loads(build_tts_only_payload(
        "lin_jiaxin", "你好", language="zh", emotion="neutral", best_of=1))
    assert body2 == {"profile": "lin_jiaxin", "text": "你好", "language": "zh"}


def test_build_tts_only_payload_includes_emotion_and_bestof():
    body = json.loads(build_tts_only_payload(
        "zhao_laoshi", "晚上好", language="zh", emotion="warm", best_of=3))
    assert body["emotion"] == "warm"
    assert body["best_of"] == 3


def test_build_tts_only_payload_format_two_states():
    """audio_format 空/wav 不下发 format 键（旧行为）；ogg 才下发。"""
    body = json.loads(build_tts_only_payload("lin_jiaxin", "你好"))
    assert "format" not in body
    body_wav = json.loads(build_tts_only_payload(
        "lin_jiaxin", "你好", audio_format="wav"))
    assert "format" not in body_wav
    body_ogg = json.loads(build_tts_only_payload(
        "lin_jiaxin", "你好", audio_format="ogg"))
    assert body_ogg["format"] == "ogg"


def test_sniff_audio_format_magic():
    assert sniff_audio_format(b"OggS" + b"\x00" * 32) == "ogg"
    assert sniff_audio_format(_wav_bytes(50)) == "wav"          # RIFF
    assert sniff_audio_format(b"\x01\x02\x03\x04") == "wav"     # 未知 → default
    assert sniff_audio_format(b"\x01\x02", default="ogg") == "ogg"
    assert sniff_audio_format(b"") == "wav"


def test_parse_tts_only_response_ok_and_failures():
    wav = _wav_bytes(200)
    good = json.dumps(
        {"ok": True, "audio_base64": base64.b64encode(wav).decode()}).encode()
    assert parse_tts_only_response(good) == (wav, "wav")
    with pytest.raises(RuntimeError):
        parse_tts_only_response(json.dumps({"ok": False, "detail": "x"}).encode())
    with pytest.raises(RuntimeError):
        parse_tts_only_response(json.dumps({"ok": True}).encode())  # 无音频
    with pytest.raises(RuntimeError):
        parse_tts_only_response(b"")


def test_parse_tts_only_response_format_field_and_magic():
    """格式判定：字段与魔数不符以魔数为准；缺字段默认 wav；OggS → ogg。"""
    wav = _wav_bytes(100)
    ogg = b"OggS" + b"\x00" * 64
    # ① 字段说 ogg 但魔数是 RIFF（hub ffmpeg 异常回退 wav 场景）→ 判 wav
    body = json.dumps({"ok": True, "format": "ogg",
                       "audio_base64": base64.b64encode(wav).decode()}).encode()
    assert parse_tts_only_response(body) == (wav, "wav")
    # ② 缺 format 字段 → 判 wav
    body = json.dumps({"ok": True,
                       "audio_base64": base64.b64encode(wav).decode()}).encode()
    assert parse_tts_only_response(body) == (wav, "wav")
    # ③ OggS 魔数（字段一致）→ 判 ogg
    body = json.dumps({"ok": True, "format": "ogg",
                       "audio_base64": base64.b64encode(ogg).decode()}).encode()
    assert parse_tts_only_response(body) == (ogg, "ogg")
    # ④ 字段说 wav 但魔数是 OggS → 魔数为准判 ogg
    body = json.dumps({"ok": True, "format": "wav",
                       "audio_base64": base64.b64encode(ogg).decode()}).encode()
    assert parse_tts_only_response(body) == (ogg, "ogg")


def test_hub_fish_synthesize_posts_and_decodes():
    wav = _wav_bytes(250)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"ok": True,
                     "audio_base64": base64.b64encode(wav).decode()}).encode()

        return _R()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = hub_fish_synthesize(
            "http://192.168.0.176:9000", "lin_jiaxin", "你好呀", language="zh")
    assert out == (wav, "wav")
    assert seen["url"] == "http://192.168.0.176:9000/api/tts_only"
    assert seen["body"]["profile"] == "lin_jiaxin"
    assert "format" not in seen["body"]  # 缺省不下发（Hub 缺省回 wav=旧行为）


def test_hub_fish_synthesize_requests_ogg_and_detects_format():
    """audio_format=ogg 透传进请求体；返回格式按响应字节魔数判定。"""
    ogg = b"OggS" + b"\x00" * 64
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"ok": True, "format": "ogg",
                     "audio_base64": base64.b64encode(ogg).decode()}).encode()

        return _R()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = hub_fish_synthesize(
            "http://192.168.0.176:9000", "lin_jiaxin", "你好呀",
            audio_format="ogg")
    assert seen["body"]["format"] == "ogg"
    assert out == (ogg, "ogg")


def _pipeline_cfg_hub(tmp_path, ref: Path, *, hub_enabled=True,
                      allowlist=None, response_format=None) -> dict:
    cfg = _pipeline_cfg(tmp_path, ref)
    cfg["persona_id"] = "lin_jiaxin"
    hf = {"enabled": hub_enabled,
          "base_url": "http://192.168.0.176:9000",
          "timeout_sec": 5.0, "best_of": 1}
    if allowlist is not None:
        hf["persona_allowlist"] = allowlist
    if response_format is not None:
        hf["response_format"] = response_format
    cfg["avatar_voice"]["hub_fish"] = hf
    return cfg


@pytest.mark.asyncio
async def test_pipeline_hub_fish_hit_takes_priority(tmp_path):
    """hub_fish 开 + 命中 → provider=hub_fish（最高保真优先于本地 CosyVoice3）。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg_hub(tmp_path, ref)
    wav = _wav_bytes(600)

    def fake_hub(base_url, profile, text, **kw):
        assert profile == "lin_jiaxin"
        return wav, "wav"

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch("src.ai.avatar_voice.hub_fish_synthesize", side_effect=fake_hub):
        rv = await tts.synthesize("你好呀，今天怎么样")
    assert rv.ok
    assert rv.provider == "hub_fish"
    assert rv.format == "wav"
    assert rv.audio_path.endswith(".wav")
    assert rv.extra.get("hub_fish_profile") == "lin_jiaxin"


@pytest.mark.asyncio
async def test_pipeline_hub_fish_ogg_direct_out(tmp_path):
    """response_format=ogg → 请求透传 + 按实际 ogg 落 .ogg 后缀 + ffprobe 测时长。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg_hub(tmp_path, ref, response_format="ogg")
    ogg = b"OggS" + b"\x00" * 128
    seen = {}

    def fake_hub(base_url, profile, text, **kw):
        seen["audio_format"] = kw.get("audio_format")
        return ogg, "ogg"

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch("src.ai.avatar_voice.hub_fish_synthesize", side_effect=fake_hub), \
         patch("src.client.voice_sender.probe_audio_duration_ms",
               return_value=1234):
        rv = await tts.synthesize("你好呀，直出测试")
    assert rv.ok and rv.provider == "hub_fish"
    assert seen["audio_format"] == "ogg"
    assert rv.format == "ogg"
    assert rv.audio_path.endswith(".ogg")
    assert Path(rv.audio_path).read_bytes() == ogg
    assert rv.duration_sec == pytest.approx(1.234)
    assert rv.duration_source == "ffprobe"


@pytest.mark.asyncio
async def test_pipeline_hub_fish_ogg_config_but_hub_falls_back_wav(tmp_path):
    """配置要 ogg 但 hub 回退 wav（ffmpeg 异常）→ 按实际格式落 .wav，绝不装 ogg。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg_hub(tmp_path, ref, response_format="ogg")
    wav = _wav_bytes(400)

    def fake_hub(base_url, profile, text, **kw):
        assert kw.get("audio_format") == "ogg"
        return wav, "wav"   # hub 侧回退：字节实为 RIFF

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch("src.ai.avatar_voice.hub_fish_synthesize", side_effect=fake_hub):
        rv = await tts.synthesize("你好呀，回退测试")
    assert rv.ok and rv.provider == "hub_fish"
    assert rv.format == "wav"
    assert rv.audio_path.endswith(".wav")


def test_pipeline_hub_fish_cache_key_includes_response_format(tmp_path):
    """缓存键并入 response_format：翻 ogg 开关自动失效旧 wav 缓存。"""
    from src.ai.tts_pipeline import TTSPipeline
    from src.ai.voice_emotion import NEUTRAL
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    p1 = TTSPipeline(_pipeline_cfg_hub(tmp_path, ref))                          # 缺省=wav
    p2 = TTSPipeline(_pipeline_cfg_hub(tmp_path, ref, response_format="ogg"))
    k1 = p1._cache_key("你好", "v", "avatar_clone", NEUTRAL, hour=12)
    k2 = p2._cache_key("你好", "v", "avatar_clone", NEUTRAL, hour=12)
    assert k1 != k2


@pytest.mark.asyncio
async def test_pipeline_hub_fish_failure_falls_through_to_local(tmp_path):
    """hub_fish 失败 → 贯穿回落本机 CosyVoice3(7852)，provider=avatar_clone。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg_hub(tmp_path, ref)
    local_wav = _wav_bytes(400)

    def fake_post(self, url, payload, *, timeout, headers=None):
        return json.dumps(
            {"audio_base64": base64.b64encode(local_wav).decode()}).encode()

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch("src.ai.avatar_voice.hub_fish_synthesize",
               side_effect=OSError("hub down")), \
         patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await tts.synthesize("你好呀，回落测试")
    assert rv.ok
    assert rv.provider == "avatar_clone"


@pytest.mark.asyncio
async def test_pipeline_hub_fish_not_in_allowlist_uses_local(tmp_path):
    """人设不在 allowlist → 不走 hub，直接本地 CosyVoice3。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg_hub(tmp_path, ref, allowlist=["zhao_laoshi"])
    local_wav = _wav_bytes(400)
    called = {"hub": False}

    def fake_hub(*a, **k):
        called["hub"] = True
        return _wav_bytes(100), "wav"

    def fake_post(self, url, payload, *, timeout, headers=None):
        return json.dumps(
            {"audio_base64": base64.b64encode(local_wav).decode()}).encode()

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch("src.ai.avatar_voice.hub_fish_synthesize", side_effect=fake_hub), \
         patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await tts.synthesize("你好呀，名单外")
    assert rv.ok
    assert rv.provider == "avatar_clone"
    assert called["hub"] is False


@pytest.mark.asyncio
async def test_pipeline_hub_fish_disabled_uses_local(tmp_path):
    """hub_fish 关（默认）→ 行为不变，走本地 CosyVoice3。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg_hub(tmp_path, ref, hub_enabled=False)
    local_wav = _wav_bytes(400)

    def fake_post(self, url, payload, *, timeout, headers=None):
        return json.dumps(
            {"audio_base64": base64.b64encode(local_wav).decode()}).encode()

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await tts.synthesize("你好呀，关闭测试")
    assert rv.ok
    assert rv.provider == "avatar_clone"


@pytest.mark.asyncio
async def test_pipeline_hub_fish_gets_colloquial_spoken_text(tmp_path):
    """hub 命中前必须先口语化——送稿≠书面原文（读稿音根因回归网）。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    cfg = _pipeline_cfg_hub(tmp_path, ref)
    cfg["avatar_voice"]["colloquial"] = {
        "enabled": True, "mode": "rule", "fillers": True, "lexical": True,
        "min_chars": 8, "lead_prob": 1.0,
    }
    seen = {"text": ""}

    def fake_hub(base_url, profile, text, **kw):
        seen["text"] = text
        return _wav_bytes(500), "wav"

    async def fake_llm(*a, **k):
        return "说真的，这件事你不用急，慢慢来就好"

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline(cfg)
    # 强制走 LLM 口语档返回固定口语稿，验证 hub 收到的是口语版而非原文
    cfg["avatar_voice"]["colloquial"]["mode"] = "llm"
    with patch("src.ai.avatar_voice.hub_fish_synthesize", side_effect=fake_hub), \
         patch("src.ai.voice_colloquial_llm.llm_colloquialize", side_effect=fake_llm):
        rv = await tts.synthesize(
            "因此您无需着急，可以慢慢处理这件事。", emotion="warm")
    assert rv.ok and rv.provider == "hub_fish"
    assert seen["text"]
    assert "因此您无需着急" not in seen["text"]
    assert "说真的" in seen["text"] or "慢慢" in seen["text"]
    assert rv.extra.get("colloquial") or rv.extra.get("colloquial_llm")
    assert "[sigh]" not in seen["text"]


# ── AvatarWhisperTranscriber ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_avatar_whisper_transcriber_ok(tmp_path):
    from src.voice_transcriber import AvatarWhisperTranscriber

    tok = tmp_path / "tok.txt"
    tok.write_text("tk", encoding="utf-8")
    voice = tmp_path / "v.wav"
    voice.write_bytes(_wav_bytes(300, rate=16000))

    t = AvatarWhisperTranscriber({
        "temp_dir": str(tmp_path / "tmp"),
        "base_url": "http://198.51.100.1:7854",
        "token_file": str(tok),
    })

    def fake_urlopen(req, timeout=None):
        assert req.headers.get("X-ah-svc") == "tk"  # urllib 规范化头名大小写

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"ok": True, "text": "转写成功", "no_speech_prob": 0.02}).encode()

        return _R()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = await t.transcribe_voice_message(str(voice), "zh")
    assert out == "转写成功"


@pytest.mark.asyncio
async def test_avatar_whisper_transcriber_no_token_returns_none(tmp_path):
    from src.voice_transcriber import AvatarWhisperTranscriber

    voice = tmp_path / "v.wav"
    voice.write_bytes(_wav_bytes(200, rate=16000))
    t = AvatarWhisperTranscriber({
        "temp_dir": str(tmp_path / "tmp"),
        "token_file": str(tmp_path / "absent.txt"),
    })
    out = await t.transcribe_voice_message(str(voice), "zh")
    assert out is None  # 令牌缺失 → None（级联回落下一级）


def test_factory_creates_avatar_whisper(tmp_path):
    from src.voice_transcriber import (
        AvatarWhisperTranscriber,
        FallbackTranscriber,
        VoiceTranscriberFactory,
    )

    t = VoiceTranscriberFactory.create_transcriber({
        "provider": "avatar_whisper", "temp_dir": str(tmp_path)})
    assert isinstance(t, AvatarWhisperTranscriber)

    chain = VoiceTranscriberFactory.create_transcriber({
        "provider": "openai_compatible", "temp_dir": str(tmp_path),
        "fallback": [
            {"provider": "avatar_whisper"},
        ]})
    assert isinstance(chain, FallbackTranscriber)


# ── 预热收集 ─────────────────────────────────────────────────────────────────
def test_warmup_personas_disabled_returns_zero():
    from src.ai.avatar_voice import warmup_personas
    assert warmup_personas({"avatar_voice": {"enabled": False}}) == 0


def test_warmup_personas_collects_refs_and_registers(tmp_path):
    from src.ai.avatar_voice import warmup_personas

    ref = tmp_path / "p1.wav"
    ref.write_bytes(_wav_bytes(200))
    cfg = {
        "avatar_voice": {"enabled": True},
        "telegram": {"voice_reply": {"voice_profile": {
            "reference_audio_path": str(ref)}}},
    }
    with patch.object(AvatarVoiceClient, "ensure_ready", return_value=True), \
         patch.object(AvatarVoiceClient, "register_spk", return_value=True) as reg:
        n = warmup_personas(cfg)
    assert n == 1
    assert reg.call_count == 1
