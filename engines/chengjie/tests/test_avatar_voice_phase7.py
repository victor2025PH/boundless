"""Phase7「活人感」单测：副语言标记注入 / 情绪变速 / 分条打包 / 分条发送流程。

全部离线可跑，零真实网络/GPU。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.ai.voice_emotion import (
    EmotionSpec,
    NEUTRAL,
    cosyvoice_speed,
    inject_paralinguistic,
)


def _wav_bytes(ms: int = 200, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * ms / 1000))
    return buf.getvalue()


# ── 副语言注入 ───────────────────────────────────────────────────────────────
def test_inject_sad_gets_sigh_or_breath():
    """sad 高强度：必然带叹气/气口类标记（概率经 intensity 放大到 ~1）。"""
    s = EmotionSpec("sad", intensity=0.9)
    out = inject_paralinguistic("唉，今天没等到你的消息，有点失落呢。", s)
    assert "[sigh]" in out or "[breath]" in out
    # 叹词「唉」存在时标记跟在叹词后（不打断词形）
    if "[sigh]" in out:
        assert out.startswith("唉[sigh]") or out.startswith("[sigh]")


def test_inject_laughter_only_with_cue():
    s = EmotionSpec("playful", intensity=0.9)
    with_cue = inject_paralinguistic("哈哈哈你也太逗了吧！", s)
    assert "[laughter]" in with_cue
    assert with_cue.index("[laughter]") >= with_cue.index("哈")  # 跟在笑点后
    # 无笑点信号 → 不硬笑（恐怖谷防线）
    no_cue = inject_paralinguistic("明天记得带伞哦。", s)
    assert "[laughter]" not in no_cue


def test_inject_skips_neutral_serious_and_low_intensity():
    assert inject_paralinguistic("好的。", NEUTRAL) == "好的。"
    assert inject_paralinguistic(
        "这件事很重要。", EmotionSpec("serious", intensity=0.9)) == "这件事很重要。"
    assert inject_paralinguistic(
        "嗯。", EmotionSpec("apologetic", intensity=0.9)) == "嗯。"
    # 低强度 sad（0.25 以下缩放为 0）→ 不注入
    out = inject_paralinguistic(
        "唉，今天没等到你的消息。", EmotionSpec("sad", intensity=0.1))
    assert "[sigh]" not in out and "[breath]" not in out


def test_inject_sigh_not_splitting_long_interjection():
    """长叹词「呜呜」不被单字截断成「呜[sigh]呜」（真机发现的注入位 bug）。"""
    s = EmotionSpec("sad", intensity=0.9)
    out = inject_paralinguistic("呜呜，真的好难过，抱抱我好不好。", s)
    assert "呜[sigh]呜" not in out
    if "[sigh]" in out:
        assert out.startswith("[sigh]")   # 哭声规整：呜呜 → [sigh]


def test_cry_onomatopoeia_normalized_to_sigh():
    """哭声拟声词治理：句首「呜呜/嘤嘤」→ [sigh]（TTS 念拟声词不稳，真机 STT
    把「呜呜」听成「喂鱼」；真叹气声既稳又更像活人哽咽）。"""
    s = EmotionSpec("sad", intensity=0.9)
    out = inject_paralinguistic("呜呜，好想你呀。", s)
    assert not out.lstrip("[sigh]").startswith("呜")   # 拟声词已移除
    assert out.startswith("[sigh]")
    assert "好想你呀。" in out
    # 多连字 + 顿号也规整；嘤嘤同理
    out2 = inject_paralinguistic("呜呜呜、别不理我嘛。", s)
    assert out2.startswith("[sigh]别不理我嘛") or out2.startswith("[sigh]")
    assert "呜" not in out2
    out3 = inject_paralinguistic("嘤嘤嘤，抱抱。", EmotionSpec("empathetic", intensity=0.9))
    assert "嘤" not in out3
    # 不叠加：规整产物句首已是标记 → 句首叹气步骤跳过
    assert out.count("[sigh]") == 1
    # 单字「呜」不是哭声拟声词（正常叹词处理）；非 sad 情绪不动拟声词
    out4 = inject_paralinguistic("呜呜，太好笑了吧。", EmotionSpec("playful", intensity=0.9))
    assert out4.startswith("呜呜")


def test_inject_deterministic_and_cache_safe():
    s = EmotionSpec("sad", intensity=0.8)
    t = "唉，今天好累呀，不过看到你就开心了。"
    assert inject_paralinguistic(t, s) == inject_paralinguistic(t, s)


def test_inject_respects_existing_marks_and_max():
    s = EmotionSpec("sad", intensity=0.9)
    manual = "[sigh]唉，我都知道啦。"
    assert inject_paralinguistic(manual, s) == manual  # 已手工标注 → 不叠加
    out = inject_paralinguistic(
        "唉，好累呀，想你了，抱抱我，快点回来呀。", s, max_marks=1)
    total = out.count("[sigh]") + out.count("[breath]") + out.count("[laughter]")
    assert total <= 1
    assert inject_paralinguistic("x", s, max_marks=0) == "x"
    assert inject_paralinguistic("", s) == ""


def test_inject_breath_at_comma_for_warm_long_text():
    """warm/calm 长句：逗号后气口（多逗号时确定性选位）。"""
    s = EmotionSpec("calm", intensity=1.0)
    t = "到家啦，先去洗个澡，然后给你打电话哦。"
    out = inject_paralinguistic(t, s)
    if "[breath]" in out:   # 概率 ×intensity 缩放后较高但非必然
        idx = out.index("[breath]")
        assert out[idx - 1] in "，,"


# ── 情绪变速 ─────────────────────────────────────────────────────────────────
def test_cosyvoice_speed_emotion_curve():
    # 2026-07-13 保真收窄：下限 0.90（真机实测 0.93 以下咬字模糊）、上限 1.12
    assert cosyvoice_speed(EmotionSpec("sad")) == 0.92
    assert cosyvoice_speed(EmotionSpec("excited")) == 1.08
    assert cosyvoice_speed(EmotionSpec("playful")) == 1.05
    assert cosyvoice_speed(NEUTRAL) == 1.0
    assert cosyvoice_speed(None) == 1.0
    # pace 相乘微调 + 限幅
    assert cosyvoice_speed(EmotionSpec("sad", pace="slow")) == 0.9    # 0.874→clamp
    assert cosyvoice_speed(EmotionSpec("excited", pace="fast")) == 1.12  # 1.134→clamp
    assert cosyvoice_speed(EmotionSpec("warm", pace="slow")) == 0.95


# ── 分条打包 ─────────────────────────────────────────────────────────────────
def test_pack_voice_parts():
    from src.ai.voice_clone_client import pack_voice_parts
    # 短文本 → 单条
    assert pack_voice_parts("好呀。", part_max_chars=40, max_parts=3) == ["好呀。"]
    # 长文本 → 多条且 ≤max_parts
    long = "第一句话说完了。第二句话也说完了。第三句话有点长但也说完了。第四句话继续说。第五句话收尾啦。"
    parts = pack_voice_parts(long, part_max_chars=20, max_parts=3)
    assert 2 <= len(parts) <= 3
    assert "".join(parts) == long          # 无丢字
    # 超出 max_parts 的余量并入末条
    parts2 = pack_voice_parts(long, part_max_chars=12, max_parts=2)
    assert len(parts2) == 2
    assert "".join(parts2) == long


# ── 管线接线：副语言注入进合成文本 ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_pipeline_injects_paralinguistic_into_synth_text(tmp_path):
    from src.ai.avatar_voice import AvatarVoiceClient
    from src.ai.tts_pipeline import TTSPipeline

    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    wav = _wav_bytes(300)
    sent = {}

    def fake_post(self, url, payload, *, timeout, headers=None):
        sent["url"] = url
        sent["body"] = json.loads(payload.decode())
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    def cfg(para: bool, vp_extra=None) -> dict:
        vp = {"enabled": True, "owner_consent": True, "backend": "avatar_clone",
              "reference_audio_path": str(ref)}
        vp.update(vp_extra or {})
        return {
            "enabled": True, "backend": "avatar_clone", "format": "wav",
            "out_dir": str(tmp_path / "out"), "fallback_on_error": False,
            "tts_cache": {"enabled": False},
            "voice_profile": vp,
            "avatar_voice": {"enabled": True, "cloud_fallback": False,
                             "paralinguistic": {"enabled": para, "max_marks": 2},
                             "chunk_max_chars": 0, "retries": 0},
        }

    text = "唉，今天没等到你的消息，有点失落呢。"
    emo = EmotionSpec("sad", intensity=0.9)
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await TTSPipeline(cfg(True)).synthesize(text, emotion=emo)
    assert rv.ok
    assert "[sigh]" in sent["body"]["text"] or "[breath]" in sent["body"]["text"]
    assert rv.extra.get("paralinguistic") is True

    # 开关关 → 原文合成
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv2 = await TTSPipeline(cfg(False)).synthesize(text, emotion=emo)
    assert rv2.ok and sent["body"]["text"] == text
    # 人设级 opt-out
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv3 = await TTSPipeline(
            cfg(True, {"paralinguistic": False})).synthesize(text, emotion=emo)
    assert rv3.ok and sent["body"]["text"] == text


# ── 分条发送流程（sender mixin）──────────────────────────────────────────────
class _FakeSender:
    """绑定真实 _send_voice_reply_parts / _voice_recording_gap 的最小宿主。"""

    def __init__(self, tmp_path):
        from src.client.sender import TelegramSenderMixin
        self._mixin = TelegramSenderMixin
        self.client = SimpleNamespace(send_chat_action=AsyncMock())
        self.logger = SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None)
        self.paced = 0
        self.recorded = 0
        self.mirrored = []
        self.tmp = tmp_path

    async def _presend_pace(self):
        self.paced += 1

    def _postsend_record_count(self):
        self.recorded += 1

    def _postsend_mirror_and_record(self, chat_id, text):
        self.mirrored.append(text)

    def _reply_to_message_id_for_send(self, msg):
        return 42

    # 直接借用真实实现
    async def _voice_recording_action(self, chat_id):
        from src.client.sender import TelegramSenderMixin
        await TelegramSenderMixin._voice_recording_action(self, chat_id)

    async def _voice_recording_gap(self, chat_id, gap_sec):
        from src.client.sender import TelegramSenderMixin
        await TelegramSenderMixin._voice_recording_gap(self, chat_id, gap_sec)

    async def send_parts(self, parts, tts, vr_cfg, split_cfg):
        from src.client.sender import TelegramSenderMixin
        msg = SimpleNamespace(chat=SimpleNamespace(id=777))
        return await TelegramSenderMixin._send_voice_reply_parts(
            self, msg, parts, tts, {"emotion": None, "persona_id": "p1"},
            vr_cfg, split_cfg, timeout_sec=5.0)


def _mk_tts(tmp_path, ok_flags):
    """按序返回 ok/fail 的假 TTSPipeline。"""
    calls = {"n": 0, "leads": []}

    async def synthesize(text, *, timeout_sec=30.0, emotion=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        calls["leads"].append(kwargs.get("colloquial_lead", True))
        ok = ok_flags[min(i, len(ok_flags) - 1)]
        p = tmp_path / f"part{i}.ogg"
        if ok:
            p.write_bytes(b"OggS-part")
        return SimpleNamespace(
            ok=ok, audio_path=str(p), duration_sec=2.0 if ok else -1.0,
            error="" if ok else "boom", provider="avatar_clone")

    return SimpleNamespace(synthesize=synthesize), calls


@pytest.mark.asyncio
async def test_split_send_happy_path(tmp_path, monkeypatch):
    import src.client.sender as sender_mod

    sends = []

    async def fake_send_voice(client, chat_id, path, *, duration=None,
                              reply_to_message_id=None, **kwargs):
        sends.append({"chat": chat_id, "reply_to": reply_to_message_id,
                      "dur": duration})
        return True

    monkeypatch.setattr(
        "src.client.voice_sender.send_telegram_voice", fake_send_voice)
    # 间隔不真睡（拟人 gap 逻辑单独测）
    async def no_sleep(sec):
        no_sleep.total += sec
    no_sleep.total = 0.0
    monkeypatch.setattr(sender_mod.asyncio, "sleep", no_sleep)

    s = _FakeSender(tmp_path)
    tts, calls = _mk_tts(tmp_path, [True, True, True])
    ok = await s.send_parts(
        ["第一条。", "第二条。", "第三条。"], tts,
        {"max_seconds": 60}, {"gap_factor": 1.1, "gap_jitter_sec": [0.5, 1.0],
                              "max_gap_sec": 20})
    assert ok is True
    assert calls["n"] == 3          # 三条全合成
    # #6：只首条允许口语化「句首迟疑词」，后续条关闭（防连发都同样开头做作）
    assert calls["leads"] == [True, False, False]
    assert len(sends) == 3          # 三条全发出
    assert sends[0]["reply_to"] == 42 and sends[1]["reply_to"] is None
    assert s.recorded == 3          # 每条各记一次外发
    assert s.mirrored == ["[语音]×3"]
    assert no_sleep.total > 0       # 条间确实等待过（拟人间隔）
    assert s.client.send_chat_action.await_count >= 2   # 录音状态挂过


@pytest.mark.asyncio
async def test_split_send_synth_fail_falls_back(tmp_path, monkeypatch):
    """任一条合成失败 → 返回 False（调用方回落整段），已合成文件被清理。"""
    async def fake_send_voice(*a, **k):
        raise AssertionError("must not send when synth failed")

    monkeypatch.setattr(
        "src.client.voice_sender.send_telegram_voice", fake_send_voice)
    s = _FakeSender(tmp_path)
    tts, calls = _mk_tts(tmp_path, [True, False])
    ok = await s.send_parts(
        ["第一条。", "第二条。"], tts, {"max_seconds": 60},
        {"gap_factor": 1.1, "max_gap_sec": 20})
    assert ok is False
    assert calls["n"] == 2
    assert not (tmp_path / "part0.ogg").exists()   # 清理干净
    assert s.mirrored == []


@pytest.mark.asyncio
async def test_split_send_mid_send_failure_keeps_sent(tmp_path, monkeypatch):
    """第 2 条投递失败 → 已发的算数（True），剩余丢弃并清理。"""
    import src.client.sender as sender_mod
    n = {"i": 0}

    async def flaky_send(client, chat_id, path, **kw):
        n["i"] += 1
        return n["i"] == 1          # 只有第一条成功

    monkeypatch.setattr(
        "src.client.voice_sender.send_telegram_voice", flaky_send)

    async def no_sleep(sec):
        return None
    monkeypatch.setattr(sender_mod.asyncio, "sleep", no_sleep)

    s = _FakeSender(tmp_path)
    tts, _ = _mk_tts(tmp_path, [True, True, True])
    ok = await s.send_parts(
        ["一。", "二。", "三。"], tts, {"max_seconds": 60},
        {"gap_factor": 1.0, "gap_jitter_sec": [0, 0], "max_gap_sec": 20})
    assert ok is True
    assert s.recorded == 1
    assert s.mirrored == ["[语音]"]
    assert not (tmp_path / "part2.ogg").exists()   # 未发的清理掉


@pytest.mark.asyncio
async def test_split_send_total_duration_gate(tmp_path, monkeypatch):
    """总时长超 max_seconds×1.5 → 整体放弃回落。"""
    async def fake_send_voice(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr(
        "src.client.voice_sender.send_telegram_voice", fake_send_voice)
    s = _FakeSender(tmp_path)

    async def synthesize(text, *, timeout_sec=30.0, emotion=None, **kwargs):
        p = tmp_path / f"long{synthesize.i}.ogg"
        synthesize.i += 1
        p.write_bytes(b"OggS")
        return SimpleNamespace(ok=True, audio_path=str(p), duration_sec=40.0,
                               error="", provider="avatar_clone")
    synthesize.i = 0
    tts = SimpleNamespace(synthesize=synthesize)
    ok = await s.send_parts(
        ["一。", "二。"], tts, {"max_seconds": 30},
        {"gap_factor": 1.0, "max_gap_sec": 20})
    assert ok is False              # 80s > 30×1.5


# ── 音色相似度抽检分级 ───────────────────────────────────────────────────────
def test_similarity_probe_classify():
    from scripts.voice_similarity_probe import classify_score
    assert classify_score(0.82) == "ok"          # 正常带
    assert classify_score(0.70) == "ok"          # 阈值边界（≥warn）
    assert classify_score(0.65) == "warn"
    assert classify_score(0.55) == "critical"    # 灾难级（音色资产坏了）
    assert classify_score("bogus") == "critical"
    assert classify_score(0.75, warn=0.8, crit=0.7) == "warn"  # 阈值可调


def test_naturalness_floor_autocalibration(tmp_path):
    """自然度告警下限自动校准：样本不足=0（只收集），够了=p10-margin。"""
    from scripts.voice_similarity_probe import (
        calibrate_naturalness_floor,
        load_history_rows,
    )
    # 样本不足 → 不告警
    few = [{"naturalness": 0.95}] * 5
    assert calibrate_naturalness_floor(few, min_n=15) == 0.0
    # 20 个样本 0.90~0.99 → p10≈0.91，floor≈0.86
    rows = [{"naturalness": 0.90 + i * 0.005} for i in range(20)]
    floor = calibrate_naturalness_floor(rows, min_n=15, margin=0.05)
    assert 0.84 <= floor <= 0.88
    # 混入 None/脏值不炸；空 → 0
    rows.append({"naturalness": None})
    rows.append({"score": 0.8})
    assert calibrate_naturalness_floor(rows, min_n=15) > 0
    assert calibrate_naturalness_floor([], min_n=15) == 0.0
    # jsonl 读取：正常行+脏行
    import json as _json
    f = tmp_path / "probe.jsonl"
    f.write_text(
        _json.dumps({"naturalness": 0.95}) + "\nnot-json\n"
        + _json.dumps({"naturalness": 0.93}) + "\n", encoding="utf-8")
    got = load_history_rows(f)
    assert len(got) == 2
    assert load_history_rows(tmp_path / "absent.jsonl") == []


@pytest.mark.asyncio
async def test_emotion_tag_requires_transcript_guard(tmp_path):
    """放量守卫：无逐字稿的人设发强情绪 → 客户端强制 neutral（防掉 instruct2 漂移）。"""
    from src.ai.avatar_voice import AvatarVoiceClient
    from src.ai.tts_pipeline import TTSPipeline

    ref = tmp_path / "ref.wav"          # 无 sidecar .txt=无逐字稿
    ref.write_bytes(_wav_bytes(300))
    wav = _wav_bytes(300)
    sent = {}

    def fake_post(self, url, payload, *, timeout, headers=None):
        sent["body"] = json.loads(payload.decode())
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    tts = TTSPipeline({
        "enabled": True, "backend": "avatar_clone", "format": "wav",
        "out_dir": str(tmp_path / "out"), "fallback_on_error": False,
        "tts_cache": {"enabled": False},
        "voice_profile": {"enabled": True, "owner_consent": True,
                          "backend": "avatar_clone",
                          "reference_audio_path": str(ref)},
        "avatar_voice": {"enabled": True, "cloud_fallback": False,
                         "chunk_max_chars": 0, "retries": 0},
    })
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await tts.synthesize(
            "真的好难过", emotion={"emotion": "sad", "intensity": 0.9})
    assert rv.ok
    assert sent["body"]["emotion"] == "neutral"     # 强情绪被守卫降级保音色
    assert "reference_text" not in sent["body"]


# ── 队列等待观测 ─────────────────────────────────────────────────────────────
def test_stats_queue_wait():
    from src.ai.avatar_voice_stats import AvatarVoiceStats
    st = AvatarVoiceStats()
    st.record_queue_wait(100)
    st.record_queue_wait(300)
    d = st.dump()
    assert d["avg_queue_wait_ms"] == 200
    st.reset()
    assert st.dump()["avg_queue_wait_ms"] == 0
