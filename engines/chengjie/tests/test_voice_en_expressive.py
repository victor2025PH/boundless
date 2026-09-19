"""English companion voice: laugh/breath cues + IndexTTS-2.5 emo_text."""
from __future__ import annotations

import json

from src.ai.avatar_voice import build_clone_payload, build_tts_only_payload
from src.ai.voice_clone_client import build_clone_payload as build_lan_payload
from src.ai.voice_colloquial import _is_english_dominant, colloquialize
from src.ai.voice_colloquial_llm import (
    build_colloquial_prompt,
    build_speech_script_prompt,
    sanitize_llm_output,
)
from src.ai.voice_emotion import (
    EmotionSpec,
    indextts_emo_alpha,
    inject_paralinguistic,
    to_indextts_emo_text,
)


def test_english_laugh_cue_injects_laughter():
    s = EmotionSpec("playful", intensity=0.9)
    hits = []
    for t in (
        "haha that's actually funny.",
        "lol you did not just say that.",
        "hehe okay cowboy.",
        "*laughs* come on.",
    ):
        out = inject_paralinguistic(t, s)
        if "[laughter]" in out:
            hits.append(out)
    assert hits, "English laugh cues should inject at high playful intensity"
    assert all("[laughter]" in h for h in hits) or hits


def test_english_no_hard_laugh_without_cue():
    s = EmotionSpec("playful", intensity=0.9)
    out = inject_paralinguistic("I have a color review in ten.", s)
    assert "[laughter]" not in out


def test_english_sigh_lead_and_chinese_unchanged():
    sad = EmotionSpec("sad", intensity=0.9)
    en = inject_paralinguistic("mm, I miss you tonight on the sofa.", sad)
    assert "[sigh]" in en or "[breath]" in en
    zh = inject_paralinguistic("唉，今天没等到你的消息，有点失落呢。", sad)
    assert "[sigh]" in zh or "[breath]" in zh
    # 中文笑点路径不被英文正则误伤
    playful = EmotionSpec("playful", intensity=0.9)
    zh_laugh = inject_paralinguistic("哈哈哈你也太逗了吧！", playful)
    assert "[laughter]" in zh_laugh


def test_indextts_emo_text_english_has_laugh_breath_spoil():
    spec = EmotionSpec("playful", intensity=0.8)
    en = to_indextts_emo_text(
        spec, language="en", style="coquettish", seed_text="wine sofa")
    assert en
    blob = en.lower()
    assert any(w in blob for w in ("laugh", "giggle", "teas"))
    assert any(w in blob for w in ("spoil", "breath", "coquettish"))
    zh = to_indextts_emo_text(spec, language="zh", seed_text="酒")
    assert any(w in zh for w in ("笑", "撒娇", "俏皮", "调皮", "黏人"))
    assert to_indextts_emo_text(EmotionSpec("neutral"), language="en") == ""
    assert 0.35 <= indextts_emo_alpha(spec) <= 0.72
    assert indextts_emo_alpha(EmotionSpec("neutral")) == 0.0


def test_clone_payload_sends_language_and_emo_text():
    body = json.loads(build_clone_payload(
        text="mm come here", reference_audio_b64="QUJD",
        language="en", emo_text="soft giggle, breathy",
        emo_alpha=0.58).decode("utf-8"))
    assert body["language"] == "en"
    assert body["emo_text"] == "soft giggle, breathy"
    assert body["use_emo_text"] is True
    assert body["emo_alpha"] == 0.58
    with_emo = json.loads(build_clone_payload(
        text="mm come here", reference_audio_b64="QUJD",
        language="en", emo_audio_b64="QUJD", emo_vector=[0.9, 0, 0, 0, 0, 0, 0, 0],
        emo_alpha=0.6).decode("utf-8"))
    assert with_emo["emo_audio_b64"] == "QUJD"
    assert with_emo["emo_vector"][0] == 0.9
    bare = json.loads(build_clone_payload(
        text="你好呀", reference_audio_b64="QUJD").decode("utf-8"))
    assert "emo_text" not in bare and "language" not in bare
    assert "emo_audio_b64" not in bare


def test_hub_and_lan_payloads_carry_emo_text():
    hub = json.loads(build_tts_only_payload(
        "claire_brennan", "mm come here", language="en",
        tts_engine="index_tts", emo_text="breathy laugh", emo_alpha=0.6))
    assert hub["tts_engine"] == "index_tts"
    assert hub["emo_text"] == "breathy laugh"
    assert hub["use_emo_text"] is True
    lan = json.loads(build_lan_payload(
        text="hi", reference_audio_b64="QQ==", language="en",
        emo_text="teasing laugh", emo_alpha=0.55).decode())
    assert lan["language"] == "en" and lan["emo_text"] == "teasing laugh"


def test_english_colloquial_rule_and_sanitize():
    assert _is_english_dominant("that's fair. maple is snoring.")
    assert not _is_english_dominant("我今天很开心啊")
    out = colloquialize(
        "That's fair. Maple is snoring like a truck tonight.",
        EmotionSpec("playful", intensity=0.8), min_chars=8)
    assert "Maple" in out
    assert sanitize_llm_output(
        "mm that's fair, Maple is snoring",
        "that's fair. Maple is snoring") == "mm that's fair, Maple is snoring"
    assert sanitize_llm_output(
        "哈哈那很公平",
        "that's fair. Maple is snoring") is None


def test_english_colloquial_prompt_forbids_bracket_tags():
    p = build_colloquial_prompt("playful", language="en", style="coquettish")
    assert "English" in p
    assert "[laughter]" in p
    assert "Do NOT write" in p or "do not write" in p.lower()
    s = build_speech_script_prompt("playful", language="en")
    assert "haha" in s.lower()
    # 中文默认提示词不被英文分支替换
    zh = build_speech_script_prompt("playful", "东北老板娘", disfluency=True)
    assert "‖短" in zh and "[laughter]" in zh


def test_vocal_bursts_lengthen_speech_when_cues_hit(tmp_path):
    import struct
    import wave

    from src.ai.voice_bursts import apply_vocal_bursts, burst_cues_from_text

    def _tone(path, sec=0.8, sr=22050, freq=220):
        n = int(sr * sec)
        frames = b"".join(
            struct.pack("<h", int(8000 * __import__("math").sin(2 * 3.1416 * freq * i / sr)))
            for i in range(n)
        )
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(frames)

    speech = tmp_path / "s.wav"
    laugh = tmp_path / "l.wav"
    breath = tmp_path / "b.wav"
    _tone(speech, 1.0, freq=180)
    _tone(laugh, 0.5, freq=420)
    _tone(breath, 0.35, freq=90)
    raw = speech.read_bytes()
    out = apply_vocal_bursts(
        raw, laugh_path=str(laugh), breath_path=str(breath),
        want_laugh=True, want_breath=True)
    assert out.startswith(b"RIFF")
    assert len(out) > len(raw)
    assert burst_cues_from_text("come here, thinking about your mouth", "playful") == (True, True)
    assert burst_cues_from_text("color review in ten", "serious") == (False, False)


def test_hush_laugh_stem_is_rejected(tmp_path):
    """The 2s glitch: a near-silent fragment gained up mid-line."""
    import struct
    import wave

    from src.ai.voice_bursts import apply_vocal_bursts, load_burst

    sr = 22050

    def _tone(path, sec, amp, freq):
        n = int(sr * sec)
        frames = b"".join(
            struct.pack("<h", int(amp * __import__("math").sin(2 * 3.1416 * freq * i / sr)))
            for i in range(n)
        )
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(frames)

    speech = tmp_path / "s.wav"
    hush = tmp_path / "hush.wav"
    real = tmp_path / "real.wav"
    _tone(speech, 1.2, 12000, 180)
    _tone(hush, 0.8, 400, 900)   # peak ~0.012 — old giggle lead-in
    _tone(real, 0.5, 10000, 320)
    raw = speech.read_bytes()
    assert load_burst(hush, sr, kind="laugh") is None
    same = apply_vocal_bursts(raw, laugh_path=str(hush), want_laugh=True)
    assert same == raw
    longer = apply_vocal_bursts(raw, laugh_path=str(real), want_laugh=True)
    assert len(longer) > len(raw)
