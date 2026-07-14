"""活人感第二批门禁（2026-07-14 夜）：①情绪分库参考音 ②通用长句呼吸
③<strong>重点词 ④深夜悄悄话 ⑤环境底噪 ⑥口误自纠。

守住的不变量：
  - 情绪分库：neutral/弱情绪不切（默认 ref=保真主路）；精确键>别名组；文件缺失
    回落 None；命中后 emotion 标签必须归 neutral（韵律情绪由参考音承担）。
  - 通用呼吸：任何情绪（含 neutral/serious）的 ≥22 字长句可换气；短句不动
    （既有「neutral/serious 不注入」门禁语义只对短句成立，本批刻意放宽长句）。
  - <strong>：仅 excited/serious 且强度≥0.5；每句 ≤1；已手工标注不叠。
  - 深夜：23–6 点整体 ×0.96（neutral 也生效）；白天行为不变；限幅不破。
  - 底噪：命令确定性（固定 seed）；振幅限幅；纯函数可单测。
  - 口误自纠：确定性 crc 低频门控；prompt 注入语义；缓存键区分。
"""
from __future__ import annotations

import pytest

from src.ai.voice_emotion import (
    NEUTRAL,
    EmotionSpec,
    cosyvoice_speed,
    inject_paralinguistic,
    is_quiet_hour,
    pick_emotion_reference,
)

_LONG_NEUTRAL = "我下午先去了趟超市，然后又顺路取了快递，回来的路上还买了杯奶茶呢。"


# ── ② 通用长句呼吸 ───────────────────────────────────────────────────────────
_LONG_SET = [
    _LONG_NEUTRAL,
    "这个方案我们要分三步走，先确认预算，再定时间表，最后同步给所有人。",
    "我刚整理完这周的照片，有好几张拍得特别好，等会儿挑几张发给你看看。",
    "白天开了一整天的会，中间只吃了个面包，现在总算能坐下来歇一歇了。",
    "周末要不要一起出去走走，天气预报说是晴天，我们可以去江边转转。",
]


def test_universal_breath_on_long_sentences():
    """长句（≥22 字、≥2 逗号）呼吸=生理必然（p=0.8）：多数命中且只加标记不动字，
    neutral/serious 都适用。"""
    hits = 0
    for t in _LONG_SET:
        out = inject_paralinguistic(t, NEUTRAL)
        if "[breath]" in out:
            hits += 1
            assert out.replace("[breath]", "") == t   # 只加标记不动字
    assert hits >= 3
    out_serious = [
        inject_paralinguistic(t, EmotionSpec("serious", intensity=0.9))
        for t in _LONG_SET]
    assert any("[breath]" in o for o in out_serious)


def test_short_neutral_serious_still_untouched():
    """既有门禁语义保持：短句 neutral/serious/apologetic 不注入。"""
    assert inject_paralinguistic("好的。", NEUTRAL) == "好的。"
    assert inject_paralinguistic(
        "这件事很重要。", EmotionSpec("serious", intensity=0.9)) == "这件事很重要。"
    assert inject_paralinguistic(
        "嗯。", EmotionSpec("apologetic", intensity=0.9)) == "嗯。"


def test_universal_breath_deterministic():
    assert (inject_paralinguistic(_LONG_NEUTRAL, NEUTRAL)
            == inject_paralinguistic(_LONG_NEUTRAL, NEUTRAL))


def test_universal_breath_not_stacked_with_recipe_breath():
    """warm 配方气口命中后，通用呼吸不再叠加（marks/去重双保险）。"""
    t = "唉，今天好累呀，不过看到你就开心多了，晚上想吃点好吃的犒劳自己。"
    out = inject_paralinguistic(t, EmotionSpec("warm", intensity=0.9), max_marks=2)
    assert out.count("[breath]") <= 2


# ── ③ <strong> 重点词 ────────────────────────────────────────────────────────
def test_strong_emphasis_excited():
    t = "今天的演出真的超精彩，我到现在都特别激动，你一定要去看一次！"
    out = inject_paralinguistic(t, EmotionSpec("excited", intensity=0.9))
    if "<strong>" in out:                         # 概率位（0.7×scale≈0.95+）
        assert out.count("<strong>") == 1
        assert "</strong>" in out
    # 确定性：同文本恒同结果
    assert out == inject_paralinguistic(t, EmotionSpec("excited", intensity=0.9))


def test_strong_not_for_playful_or_low_intensity():
    t = "今天的演出真的超精彩，我特别激动！"
    assert "<strong>" not in inject_paralinguistic(
        t, EmotionSpec("playful", intensity=0.9))
    assert "<strong>" not in inject_paralinguistic(
        t, EmotionSpec("excited", intensity=0.3))


def test_strong_respects_manual_marks():
    manual = "今天<strong>真的</strong>很精彩！"
    assert inject_paralinguistic(
        manual, EmotionSpec("excited", intensity=0.9)) == manual


# ── ④ 深夜悄悄话 ─────────────────────────────────────────────────────────────
def test_quiet_hour_ranges():
    assert is_quiet_hour(23) and is_quiet_hour(0) and is_quiet_hour(6)
    assert not is_quiet_hour(7) and not is_quiet_hour(12) and not is_quiet_hour(22)
    assert not is_quiet_hour(None)


def test_night_speed_applies_to_neutral_and_emotions():
    assert cosyvoice_speed(NEUTRAL) == 1.0                       # 白天不变
    assert cosyvoice_speed(NEUTRAL, hour=14) == 1.0
    assert cosyvoice_speed(NEUTRAL, hour=2) == pytest.approx(0.96)
    day = cosyvoice_speed(EmotionSpec("excited", intensity=0.9), hour=14)
    night = cosyvoice_speed(EmotionSpec("excited", intensity=0.9), hour=1)
    assert night < day
    # 下限不破（sad 夜里也 ≥0.90）
    assert cosyvoice_speed(EmotionSpec("sad", intensity=0.9, pace="slow"),
                           hour=2) >= 0.90


# ── ① 情绪分库参考音 ─────────────────────────────────────────────────────────
def _vp_with_lib(tmp_path, keys=("happy", "sad")):
    lib = {}
    for k in keys:
        p = tmp_path / f"ref_{k}.wav"
        p.write_bytes(b"RIFFfake")
        lib[k] = str(p)
    return {"reference_audio_path": "default.wav",
            "reference_audio_by_emotion": lib}


def test_pick_emotion_reference_exact_and_alias(tmp_path):
    vp = _vp_with_lib(tmp_path)
    hit = pick_emotion_reference(vp, EmotionSpec("happy", intensity=0.8))
    assert hit and hit[1] == "happy"
    # excited 无精确键 → 吸附 happy 别名组
    hit2 = pick_emotion_reference(vp, EmotionSpec("excited", intensity=0.8))
    assert hit2 and hit2[1] == "happy"
    hit3 = pick_emotion_reference(vp, EmotionSpec("empathetic", intensity=0.8))
    assert hit3 and hit3[1] == "sad"


def test_pick_emotion_reference_gates(tmp_path):
    vp = _vp_with_lib(tmp_path)
    assert pick_emotion_reference(vp, NEUTRAL) is None            # neutral 不切
    assert pick_emotion_reference(                                # 弱情绪不切
        vp, EmotionSpec("happy", intensity=0.3), threshold=0.5) is None
    assert pick_emotion_reference(None, EmotionSpec("happy", intensity=0.9)) is None
    assert pick_emotion_reference({}, EmotionSpec("happy", intensity=0.9)) is None
    # 文件不存在 → None（回落默认 ref，绝不报错）
    vp2 = {"reference_audio_by_emotion": {"happy": str(tmp_path / "absent.wav")}}
    assert pick_emotion_reference(vp2, EmotionSpec("happy", intensity=0.9)) is None


@pytest.mark.asyncio
async def test_avatar_clone_uses_emotion_ref_and_neutral_tag(tmp_path, monkeypatch):
    """管线契约：命中情绪库 → 请求体用情绪 ref 的 b64 + emotion=neutral +
    该 ref 自己的 sidecar 逐字稿。"""
    import json

    from src.ai.avatar_voice import AvatarVoiceClient
    from src.ai.tts_pipeline import TTSPipeline

    default_ref = tmp_path / "default.wav"
    default_ref.write_bytes(b"RIFF" + b"d" * 64)
    (tmp_path / "default.txt").write_text("默认逐字稿", encoding="utf-8")
    happy_ref = tmp_path / "happy.wav"
    happy_ref.write_bytes(b"RIFF" + b"h" * 64)
    (tmp_path / "happy.txt").write_text("开心逐字稿", encoding="utf-8")

    sent = {}

    def fake_post(self, url, payload, *, timeout, headers=None):
        sent["body"] = json.loads(payload.decode("utf-8"))
        return json.dumps({"audio_base64": "UklGRg=="}).encode()

    monkeypatch.setattr(AvatarVoiceClient, "_post", fake_post)
    monkeypatch.setattr(AvatarVoiceClient, "health_ok", lambda self, **kw: True)

    tts = TTSPipeline({
        "enabled": True, "backend": "avatar_clone", "format": "ogg",
        "out_dir": str(tmp_path / "out"),
        "voice_profile": {
            "enabled": True, "owner_consent": True,
            "reference_audio_path": str(default_ref),
            "emotion_default": "neutral",
            "reference_audio_by_emotion": {"happy": str(happy_ref)},
        },
        "avatar_voice": {"enabled": True, "emotion_channel_threshold": 0.5,
                         "prerender": {"enabled": False}},
        "tts_cache": {"enabled": False},
    })
    rv = await tts.synthesize(
        "今天真的太开心啦，跟你说话特别舒服呢！",
        emotion={"emotion": "happy", "intensity": 0.9})
    assert rv.ok
    import base64 as _b64
    assert sent["body"]["emotion"] == "neutral"       # 标签归 neutral（纯保真）
    assert (_b64.b64decode(sent["body"]["reference_audio_b64"])
            .startswith(b"RIFFhh"))                    # 用的是 happy ref
    assert sent["body"]["reference_text"] == "开心逐字稿"
    assert rv.extra.get("emotion_ref") == "happy"


# ── ⑤ 环境底噪 ───────────────────────────────────────────────────────────────
def test_build_ambience_cmd_deterministic_and_clamped():
    from src.ai.tts_pipeline import build_ambience_cmd
    cmd = build_ambience_cmd("a.wav", "b.wav", amplitude=0.03)
    joined = " ".join(cmd)
    assert "anoisesrc=colour=brown:amplitude=0.03:seed=7" in joined
    assert "lowpass=f=400" in joined and "normalize=0" in joined
    # 振幅限幅（防手滑 0.5 把人声淹了）
    cmd2 = build_ambience_cmd("a.wav", "b.wav", amplitude=0.5)
    assert "amplitude=0.12" in " ".join(cmd2)
    assert build_ambience_cmd("a.wav", "b.wav") == build_ambience_cmd("a.wav", "b.wav")


def test_build_ambience_cmd_outdoor_profile():
    from src.ai.tts_pipeline import build_ambience_cmd
    joined = " ".join(build_ambience_cmd("a.wav", "b.wav", profile="outdoor"))
    assert "colour=pink" in joined                 # 户外=宽频带 pink
    assert "lowpass=f=1100" in joined              # profile 默认低通
    assert "tremolo=f=0.3:d=0.35" in joined        # 风声/远车流的慢起伏
    # 显式 lowpass 覆盖 profile 默认
    j2 = " ".join(build_ambience_cmd("a.wav", "b.wav", profile="outdoor",
                                     lowpass_hz=800))
    assert "lowpass=f=800" in j2
    # 未知 profile → room 配方兜底
    j3 = " ".join(build_ambience_cmd("a.wav", "b.wav", profile="bogus"))
    assert "colour=brown" in j3


def test_resolve_ambience_amplitude_per_profile():
    from src.ai.tts_pipeline import resolve_ambience_amplitude
    amb = {"amplitude": 0.03, "outdoor_amplitude": 0.045, "night_factor": 0.6}
    assert resolve_ambience_amplitude(amb, profile="room", hour=14) == 0.03
    assert resolve_ambience_amplitude(amb, profile="outdoor", hour=14) == 0.045
    # outdoor 未单配 → 回落 amplitude
    assert resolve_ambience_amplitude(
        {"amplitude": 0.03}, profile="outdoor", hour=14) == 0.03
    # 深夜衰减 + 限幅
    assert resolve_ambience_amplitude(amb, profile="room", hour=2) \
        == pytest.approx(0.018)
    assert resolve_ambience_amplitude(
        {"amplitude": 0.5}, profile="room", hour=14) == 0.12
    assert resolve_ambience_amplitude(None, profile="room", hour=14) == 0.03


def test_classify_scene_ambience():
    from src.ai.tts_pipeline import classify_scene_ambience
    assert classify_scene_ambience("sunny street corner cafe, afternoon") == "outdoor"
    assert classify_scene_ambience("park with soft morning light") == "outdoor"
    assert classify_scene_ambience("seaside boardwalk at sunset") == "outdoor"
    assert classify_scene_ambience("cozy room with warm lamp light") == "room"
    assert classify_scene_ambience("") == "room"
    assert classify_scene_ambience(None) == "room"  # type: ignore[arg-type]


# ── ⑥ 口误自纠 ───────────────────────────────────────────────────────────────
def test_disfluency_prompt_variants():
    from src.ai.spoken_variant import build_spoken_variant_instruction
    from src.ai.voice_colloquial_llm import build_colloquial_prompt
    assert "口误" in build_spoken_variant_instruction(disfluency=True)
    assert "口误" not in build_spoken_variant_instruction()
    assert "口误" in build_colloquial_prompt("warm", disfluency=True)
    assert "口误" not in build_colloquial_prompt("warm")


def test_want_disfluency_gate():
    from src.ai.spoken_variant import want_disfluency
    cfg_on = {"avatar_voice": {"colloquial": {"disfluency": True}}}
    cfg_off = {"avatar_voice": {"colloquial": {"disfluency": False}}}
    # 开关关 → 恒 False
    assert not any(want_disfluency(cfg_off, f"文本{i}") for i in range(30))
    # 开关开 → 约 1/7 命中（确定性：同文本恒同结果）
    hits = [want_disfluency(cfg_on, f"文本{i}") for i in range(70)]
    assert 0 < sum(hits) < 30
    assert want_disfluency(cfg_on, "文本1") == want_disfluency(cfg_on, "文本1")


def test_llm_cache_key_distinguishes_disfluency():
    from src.ai.voice_colloquial_llm import _cache_key
    assert (_cache_key("t", "warm", True, "", False)
            != _cache_key("t", "warm", True, "", True))
