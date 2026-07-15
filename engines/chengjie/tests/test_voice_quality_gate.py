"""出站语音质量闸门单测（2026-07-15「乱码语音」事故修复）。

覆盖：
- speakable_units / looks_truncated / resolve_quality_gate 纯函数
- pack_voice_parts 末条过短并入前条
- _send_voice_reply_parts：截断条被拦 → 整批回落、文件清理、计数器 +1
- TTSPipeline：截断嫌疑音不入缓存（防坏音复用）
"""
from __future__ import annotations

import base64
import io
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.ai.tts_quality import (
    looks_truncated,
    resolve_quality_gate,
    speakable_units,
)


# ── 纯函数：发声单位 ─────────────────────────────────────────────────────────
def test_speakable_units_cjk_words_digits():
    assert speakable_units("吃饭了吗") == 4
    assert speakable_units("ハルコです") == 5           # 假名按字
    assert speakable_units("hello world") == 2          # 拉丁按词
    assert speakable_units("等我5分钟") == 5            # 4 CJK + 1 数字串
    assert speakable_units("😆😆～！…") == 0            # emoji/标点不计
    assert speakable_units("") == 0
    assert speakable_units(None) == 0


# ── 纯函数：截断判定 ─────────────────────────────────────────────────────────
def test_incident_case_20_chars_1s_is_truncated():
    """事故样本：约 20 字文本只合成出 ~1 秒音频 → 必须判坏。"""
    text = "不过你要是请我吃好的，我还能再吃一顿哦～"
    bad, why = looks_truncated(text, 1.3)
    assert bad is True and "floor" in why


def test_normal_speech_not_flagged():
    text = "不过你要是请我吃好的，我还能再吃一顿哦～"      # 18 单位
    assert looks_truncated(text, 4.2)[0] is False        # 正常语速
    assert looks_truncated(text, 1.9)[0] is False        # 1.9s ≥ 1.8s floor：极快但物理可能


def test_unknown_duration_and_short_text_skipped():
    assert looks_truncated("很长的一句话啊啊啊啊", -1.0)[0] is False   # 未测得
    assert looks_truncated("很长的一句话啊啊啊啊", 0)[0] is False
    assert looks_truncated("嗯嗯。", 0.4)[0] is False               # 短语短音正常
    assert looks_truncated("", 0.1)[0] is False
    assert looks_truncated("abc", "bogus")[0] is False              # 脏输入不炸


def test_thresholds_configurable():
    text = "一二三四五六七八九十"
    assert looks_truncated(text, 1.5)[0] is False        # 默认 0.10：floor 1.0
    assert looks_truncated(
        text, 1.5, min_sec_per_unit=0.2)[0] is True      # 收紧后 floor 2.0
    assert looks_truncated(
        text, 0.5, min_units=99)[0] is False             # min_units 抬高 → 不判


def test_resolve_quality_gate_defaults_and_overrides():
    qg = resolve_quality_gate({})
    assert qg == {"enabled": True, "min_sec_per_unit": 0.10, "min_units": 6}
    qg2 = resolve_quality_gate({"quality_gate": {
        "enabled": False, "min_sec_per_unit": 0.15, "min_units": 10}})
    assert qg2 == {"enabled": False, "min_sec_per_unit": 0.15, "min_units": 10}
    # 脏配置回默认
    qg3 = resolve_quality_gate({"quality_gate": {
        "min_sec_per_unit": "x", "min_units": None}})
    assert qg3["min_sec_per_unit"] == 0.10 and qg3["min_units"] == 6


# ── pack_voice_parts 末条并入 ────────────────────────────────────────────────
def test_pack_voice_parts_merges_short_tail():
    from src.ai.voice_clone_client import pack_voice_parts
    # 末条只有"哦～"这种孤短尾 → 并入前条（修复放大器：孤条短音合成易出怪音）
    text = "哈哈你怎么又来一次啦。刚吃了面包垫了肚子。哦～"
    parts = pack_voice_parts(text, part_max_chars=20, max_parts=3)
    assert all(len(p) >= 8 or len(parts) == 1 for p in parts[-1:])
    assert "".join(parts) == text                        # 无丢字
    # min_tail_chars=0 → 关闭并入（保持旧行为可选）
    parts_off = pack_voice_parts(
        text, part_max_chars=20, max_parts=3, min_tail_chars=0)
    assert "".join(parts_off) == text
    # 两条打包、尾条过短 → 并成单条（调用方自动走整段单条路径）
    parts2 = pack_voice_parts("这句正好二十个字左右哦。嗯～",
                              part_max_chars=14, max_parts=3)
    assert "".join(parts2) == "这句正好二十个字左右哦。嗯～"
    assert len(parts2[-1]) >= 8 or len(parts2) == 1


def test_pack_voice_parts_existing_behavior_unchanged():
    from src.ai.voice_clone_client import pack_voice_parts
    assert pack_voice_parts("好呀。", part_max_chars=40, max_parts=3) == ["好呀。"]
    long = "第一句话说完了。第二句话也说完了。第三句话有点长但也说完了。第四句话继续说。第五句话收尾啦。"
    parts = pack_voice_parts(long, part_max_chars=20, max_parts=3)
    assert 2 <= len(parts) <= 3 and "".join(parts) == long


def test_pack_voice_parts_incident_20260715_emoji_tail():
    """2026-07-15 11:24 事故样本回归：按生产参数打包后尾条恰好只剩「💭 ✨」
    （emoji 被硬送克隆 TTS → 1 秒杂音）。修复后 emoji 尾条必须并入前条。"""
    from src.ai.voice_clone_client import pack_voice_parts
    text = ("林小雨现在不太方便拍照呢，不过我一直在这儿陪你～"
            "想我了的话，多跟我说说话好不好？💭 ✨")
    parts = pack_voice_parts(text, part_max_chars=40, max_parts=3,
                             min_tail_chars=8)
    assert "".join(parts) == text
    # 不允许出现「无可发声内容」的孤条
    from src.ai.tts_quality import speakable_units
    assert all(speakable_units(p) > 0 for p in parts)


@pytest.mark.asyncio
async def test_pipeline_rejects_emoji_only_text():
    """全 emoji/符号文本 → 直接判失败（no_speakable_text），绝不硬合成杂音。"""
    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline({"enabled": True, "backend": "disabled"})
    rv = await tts.synthesize("💭 ✨")
    assert rv.ok is False and rv.error == "no_speakable_text"
    # 空文本仍走原「empty text」路径（行为不变）
    rv2 = await tts.synthesize("  ")
    assert rv2.ok is False and rv2.error != "no_speakable_text"


# ── 分条发送闸门（sender mixin）──────────────────────────────────────────────
class _FakeSender:
    def __init__(self):
        self.client = SimpleNamespace(send_chat_action=AsyncMock())
        self.logger = SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, debug=lambda *a, **k: None)
        self.recorded = 0
        self.mirrored = []
        # no_edge 解析读 self.config.config
        self.config = SimpleNamespace(config={})

    async def _presend_pace(self):
        pass

    def _postsend_record_count(self):
        self.recorded += 1

    def _postsend_mirror_and_record(self, chat_id, text):
        self.mirrored.append(text)

    def _reply_to_message_id_for_send(self, msg):
        return 42

    async def _voice_recording_action(self, chat_id):
        pass

    async def _voice_recording_gap(self, chat_id, gap_sec):
        pass

    async def send_parts(self, parts, tts, vr_cfg, split_cfg):
        from src.client.sender import TelegramSenderMixin
        msg = SimpleNamespace(chat=SimpleNamespace(id=777))
        return await TelegramSenderMixin._send_voice_reply_parts(
            self, msg, parts, tts, {"emotion": None, "persona_id": "p1"},
            vr_cfg, split_cfg, timeout_sec=5.0)


def _mk_tts(tmp_path, durations):
    calls = {"n": 0}

    async def synthesize(text, *, timeout_sec=30.0, emotion=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        p = tmp_path / f"part{i}.ogg"
        p.write_bytes(b"OggS-part")
        return SimpleNamespace(
            ok=True, audio_path=str(p),
            duration_sec=durations[min(i, len(durations) - 1)],
            error="", provider="avatar_clone", extra={})

    return SimpleNamespace(synthesize=synthesize), calls


_LONG_A = "哈哈你怎么又来一次啦刚吃了面包垫了肚子"   # 18 单位
_LONG_B = "不过你要是请我吃好的我还能再吃一顿哦"     # 17 单位


@pytest.mark.asyncio
async def test_split_send_rejects_truncated_part(tmp_path, monkeypatch):
    """第 2 条 18 字只有 1.2s（事故样本）→ 整批拦下回落，一条都不发。"""
    async def fake_send_voice(*a, **k):
        raise AssertionError("truncated batch must not be sent")

    monkeypatch.setattr(
        "src.client.voice_sender.send_telegram_voice", fake_send_voice)
    from src.ai.avatar_voice_stats import get_avatar_voice_stats
    base = get_avatar_voice_stats().dump()["truncation_rejects"]

    s = _FakeSender()
    tts, calls = _mk_tts(tmp_path, [5.0, 1.2])
    ok = await s.send_parts(
        [_LONG_A, _LONG_B], tts, {"max_seconds": 60},
        {"gap_factor": 1.0, "max_gap_sec": 20})
    assert ok is False
    assert calls["n"] == 2
    assert not (tmp_path / "part0.ogg").exists()   # 已合成的清理干净
    assert not (tmp_path / "part1.ogg").exists()
    assert s.mirrored == []
    assert get_avatar_voice_stats().dump()["truncation_rejects"] == base + 1


@pytest.mark.asyncio
async def test_split_send_normal_durations_pass(tmp_path, monkeypatch):
    import src.client.sender as sender_mod
    sends = []

    async def fake_send_voice(client, chat_id, path, **kw):
        sends.append(path)
        return True

    monkeypatch.setattr(
        "src.client.voice_sender.send_telegram_voice", fake_send_voice)

    async def no_sleep(sec):
        return None
    monkeypatch.setattr(sender_mod.asyncio, "sleep", no_sleep)

    s = _FakeSender()
    tts, _ = _mk_tts(tmp_path, [5.0, 4.5])
    ok = await s.send_parts(
        [_LONG_A, _LONG_B], tts, {"max_seconds": 60},
        {"gap_factor": 1.0, "gap_jitter_sec": [0, 0], "max_gap_sec": 20})
    assert ok is True and len(sends) == 2


@pytest.mark.asyncio
async def test_split_send_gate_can_be_disabled(tmp_path, monkeypatch):
    """quality_gate.enabled=false → 旧行为（截断音照发，运营自担）。"""
    import src.client.sender as sender_mod
    sends = []

    async def fake_send_voice(client, chat_id, path, **kw):
        sends.append(path)
        return True

    monkeypatch.setattr(
        "src.client.voice_sender.send_telegram_voice", fake_send_voice)

    async def no_sleep(sec):
        return None
    monkeypatch.setattr(sender_mod.asyncio, "sleep", no_sleep)

    s = _FakeSender()
    tts, _ = _mk_tts(tmp_path, [5.0, 1.2])
    ok = await s.send_parts(
        [_LONG_A, _LONG_B], tts,
        {"max_seconds": 60, "quality_gate": {"enabled": False}},
        {"gap_factor": 1.0, "gap_jitter_sec": [0, 0], "max_gap_sec": 20})
    assert ok is True and len(sends) == 2


# ── TTSPipeline 缓存卫生：截断嫌疑音不入缓存 ─────────────────────────────────
def _wav_bytes(ms: int, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * ms / 1000))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_pipeline_marks_suspect_and_skips_cache(tmp_path):
    """长文本合出 0.2s 音 → extra 标截断嫌疑 + 不入缓存（第二次必须重合成）。"""
    from src.ai.avatar_voice import AvatarVoiceClient
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache

    reset_tts_cache()
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    calls = {"n": 0}

    def fake_post(self, url, payload, *, timeout, headers=None):
        calls["n"] += 1
        return json.dumps(
            {"audio_base64": base64.b64encode(_wav_bytes(200)).decode()}
        ).encode()

    cfg = {
        "enabled": True, "backend": "avatar_clone", "format": "wav",
        "out_dir": str(tmp_path / "out"), "fallback_on_error": False,
        "tts_cache": {"enabled": True},
        "voice_profile": {"enabled": True, "owner_consent": True,
                          "backend": "avatar_clone",
                          "reference_audio_path": str(ref)},
        "avatar_voice": {"enabled": True, "cloud_fallback": False,
                         "prerender": {"enabled": False},
                         "chunk_max_chars": 0, "retries": 0},
    }
    text = "唉今天没等到你的消息有点失落呢真的好想你呀"   # 19 单位 → floor 1.9s ≫ 0.2s
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv1 = await TTSPipeline(cfg).synthesize(text)
        assert rv1.ok and rv1.extra.get("suspect_truncated")
        rv2 = await TTSPipeline(cfg).synthesize(text)
    assert calls["n"] == 2                    # 未走缓存 → 真的重合成了
    assert not rv2.extra.get("cache_hit")


@pytest.mark.asyncio
async def test_pipeline_normal_audio_still_cached(tmp_path):
    """正常时长音频缓存行为不变（回归保护）。"""
    from src.ai.avatar_voice import AvatarVoiceClient
    from src.ai.tts_pipeline import TTSPipeline, reset_tts_cache

    reset_tts_cache()
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    calls = {"n": 0}

    def fake_post(self, url, payload, *, timeout, headers=None):
        calls["n"] += 1
        return json.dumps(
            {"audio_base64": base64.b64encode(_wav_bytes(4000)).decode()}
        ).encode()

    cfg = {
        "enabled": True, "backend": "avatar_clone", "format": "wav",
        "out_dir": str(tmp_path / "out"), "fallback_on_error": False,
        "tts_cache": {"enabled": True},
        "voice_profile": {"enabled": True, "owner_consent": True,
                          "backend": "avatar_clone",
                          "reference_audio_path": str(ref)},
        "avatar_voice": {"enabled": True, "cloud_fallback": False,
                         "prerender": {"enabled": False},
                         "chunk_max_chars": 0, "retries": 0},
    }
    text = "唉今天没等到你的消息有点失落呢真的好想你呀"
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv1 = await TTSPipeline(cfg).synthesize(text)
        assert rv1.ok and not rv1.extra.get("suspect_truncated")
        rv2 = await TTSPipeline(cfg).synthesize(text)
    assert calls["n"] == 1                    # 第二次命中缓存
    assert rv2.extra.get("cache_hit") is True
