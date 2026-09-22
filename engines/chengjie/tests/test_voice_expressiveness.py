"""表达力档位（restrained/natural/vivid/dramatic）纯函数 + 接线单测。"""
from __future__ import annotations

import json

from src.ai.voice_emotion import (
    NEUTRAL,
    EmotionSpec,
    apply_expressiveness,
    derive_emotion,
    expression_policy,
    indextts_emo_alpha,
    normalize_expressiveness,
    resolve_expressiveness,
    step_down_expressiveness,
    to_indextts_emo_text,
)


# ── 档位规整 / 解析 ──────────────────────────────────────────────────────────
def test_normalize_aliases_numbers_and_garbage():
    assert normalize_expressiveness("生动") == "vivid"
    assert normalize_expressiveness(" Restrained ") == "restrained"
    assert normalize_expressiveness("max") == "dramatic"
    assert normalize_expressiveness(2) == "natural"
    assert normalize_expressiveness(0) == "restrained"
    assert normalize_expressiveness(75) == "vivid"      # 0–100 滑杆
    assert normalize_expressiveness(100) == "dramatic"
    assert normalize_expressiveness(None) == ""
    assert normalize_expressiveness(True) == ""
    assert normalize_expressiveness("胡说") == ""


def test_resolve_precedence_session_persona_global_default():
    assert resolve_expressiveness({}) == "natural"
    assert resolve_expressiveness(None, persona=None) == "natural"
    cfg = {"emotion": {"expressiveness": "vivid"}}
    assert resolve_expressiveness(cfg) == "vivid"
    persona = {"voice_profile": {"expressiveness": "restrained"}}
    assert resolve_expressiveness(cfg, persona=persona) == "restrained"
    assert resolve_expressiveness(cfg, persona=persona, session="dramatic") == "dramatic"
    # 会话层给了脏值 → 忽略，回落人设
    assert resolve_expressiveness(cfg, persona=persona, session="???") == "restrained"
    # 预先写进 voice_cfg 的档位优先于人设
    assert resolve_expressiveness({"voice_expressiveness": "vivid"}, persona=persona) == "vivid"


def test_policy_monotonic_and_quiet_hour_downgrade():
    r, n, v, d = (expression_policy(l) for l in ("restrained", "natural", "vivid", "dramatic"))
    assert r.intensity_cap < n.intensity_cap < v.intensity_cap <= d.intensity_cap
    assert r.emo_alpha_cap < n.emo_alpha_cap < v.emo_alpha_cap < d.emo_alpha_cap
    assert r.max_marks <= n.max_marks < v.max_marks < d.max_marks
    assert r.bursts is False and n.bursts_stock is False and v.bursts_stock is True
    assert r.baseline_intensity == 0.0
    assert expression_policy("garbage").level == "natural"
    assert expression_policy("dramatic", hour=2).level == "vivid"
    assert expression_policy("vivid", hour=2).level == "natural"
    assert expression_policy("natural", hour=2).level == "natural"
    assert expression_policy("dramatic", hour=15).level == "dramatic"


def test_step_down_bottoms_out():
    assert step_down_expressiveness("dramatic") == "vivid"
    assert step_down_expressiveness("natural") == "restrained"
    assert step_down_expressiveness("restrained") == "restrained"
    assert step_down_expressiveness(None) == "restrained"


def test_cap_colloquial():
    assert expression_policy("restrained").cap_colloquial("vivid") == "light"
    assert expression_policy("natural").cap_colloquial("vivid") == "vivid"   # 尊重显式配置
    assert expression_policy("vivid").cap_colloquial("light") == "light"
    assert expression_policy("dramatic").cap_colloquial("weird") == "weird"


# ── intensity 缩放 ───────────────────────────────────────────────────────────
def test_apply_expressiveness_scales_only_intensity():
    spec = EmotionSpec("angry", intensity=0.8, pace="fast")
    out = apply_expressiveness(spec, expression_policy("restrained"))
    assert out.emotion == "angry" and out.pace == "fast"
    assert out.intensity == 0.45                       # 0.8*0.6=0.48 → cap 0.45
    out_n = apply_expressiveness(spec, expression_policy("natural"))
    assert out_n.intensity == 0.72                     # 0.8*0.9
    out_d = apply_expressiveness(spec, expression_policy("dramatic"))
    assert out_d.intensity == 0.92                     # 0.8*1.15
    assert apply_expressiveness(NEUTRAL, expression_policy("dramatic")) is NEUTRAL
    assert apply_expressiveness(None, expression_policy("vivid")).is_neutral()


# ── derive_emotion 基线 ──────────────────────────────────────────────────────
def test_baseline_intensity_only_affects_baseline_paths():
    persona = {"voice_profile": {"emotion": "playful"}}
    # 无线索：人设基线按档位收敛 / 归中性
    b = derive_emotion(text="好的", persona=persona, baseline_intensity=0.4)
    assert b.emotion == "playful" and b.intensity == 0.4
    assert derive_emotion(text="好的", persona=persona, baseline_intensity=0.0).is_neutral()
    assert derive_emotion(text="好的", default="warm", baseline_intensity=0.0).is_neutral()
    # None → 旧行为
    assert derive_emotion(text="好的", persona=persona).intensity == 0.6
    # 线索命中不受基线影响
    cue = derive_emotion(text="哈哈哈太好笑了", persona=persona, baseline_intensity=0.0)
    assert not cue.is_neutral() and cue.intensity >= 0.6
    assert derive_emotion(csat=1.0, baseline_intensity=0.0).emotion == "empathetic"
    assert derive_emotion(intent="complaint", baseline_intensity=0.0).emotion == "empathetic"
    assert derive_emotion(rel_stage="intimate", baseline_intensity=0.0).emotion == "playful"


# ── IndexTTS 轻版 / alpha 上限 ──────────────────────────────────────────────
def test_indextts_light_wording_and_alpha_cap():
    light = to_indextts_emo_text(EmotionSpec("playful", intensity=0.4), language="zh")
    full = to_indextts_emo_text(EmotionSpec("playful", intensity=0.6), language="zh")
    assert light and full and light != full
    assert "收着一点" not in light                       # 轻版不再叠「收着」
    en_light = to_indextts_emo_text(EmotionSpec("happy", intensity=0.4), language="en")
    en_full = to_indextts_emo_text(EmotionSpec("happy", intensity=0.6), language="en")
    assert en_light and en_light != en_full and "keep it small" not in en_light
    assert to_indextts_emo_text(NEUTRAL) == ""

    hot = EmotionSpec("excited", intensity=1.0)
    assert indextts_emo_alpha(hot) == 0.68
    assert indextts_emo_alpha(hot, cap=0.45) == 0.45
    assert indextts_emo_alpha(hot, cap=0.99) <= 0.72   # 绝对上限保音色
    assert indextts_emo_alpha(NEUTRAL, cap=0.45) == 0.0


# ── 罐头笑声门控 ─────────────────────────────────────────────────────────────
def test_burst_cues_need_text_cue_and_dedupe(tmp_path):
    from src.ai.voice_bursts import burst_cues_from_text, resolve_burst_paths

    assert burst_cues_from_text("come over", "playful") == (True, True)          # 旧行为
    assert burst_cues_from_text("come over", "playful", need_text_cue=True) == (False, False)
    assert burst_cues_from_text("haha you", "playful", need_text_cue=True)[0] is True
    assert burst_cues_from_text("haha [laughter] you", "playful")[0] is False   # 已有笑标记
    assert burst_cues_from_text("嘿，哈哈你来啦", "playful")[0] is False          # 已有轻笑开头
    laugh, breath = resolve_burst_paths({}, stock=False)
    assert laugh == "" and breath == ""
    f = tmp_path / "laugh.wav"
    f.write_bytes(b"RIFF")
    own, _ = resolve_burst_paths({"burst_audio": {"laugh": str(f)}}, stock=False)
    assert own == str(f)


# ── TTSPipeline 接线 ─────────────────────────────────────────────────────────
def test_pipeline_reads_policy_and_gates_bursts(monkeypatch):
    from src.ai import voice_bursts
    from src.ai.tts_pipeline import TTSPipeline

    calls = []

    def fake_decorate(wav, **kw):
        calls.append(kw)
        return wav + b"!"

    monkeypatch.setattr(voice_bursts, "decorate_clone_wav", fake_decorate)

    p = TTSPipeline({"enabled": True, "backend": "edge"})
    assert p.expressiveness == "natural"
    spec = EmotionSpec("playful", intensity=0.7)
    out, applied = p._decorate_bursts(b"wav", text="haha", emotion="playful", spec=spec)
    assert applied and out == b"wav!"
    assert calls[-1]["need_text_cue"] is True and calls[-1]["own_stems_only"] is True
    # 强度不够 → 不拼
    out, applied = p._decorate_bursts(
        b"wav", text="haha", emotion="playful", spec=EmotionSpec("playful", intensity=0.4))
    assert not applied and out == b"wav"

    pr = TTSPipeline({"enabled": True, "backend": "edge", "voice_expressiveness": "restrained"})
    assert pr.expressiveness == "restrained"
    assert pr._decorate_bursts(b"wav", text="haha", emotion="playful", spec=spec) == (b"wav", False)
    assert pr._pacing_expressive({"instruct_style": "撒娇"}) is False

    pd = TTSPipeline({"enabled": True, "backend": "edge", "emotion": {"expressiveness": "戏剧"}})
    assert pd.expressiveness == "dramatic"
    _, applied = pd._decorate_bursts(b"wav", text="ok", emotion="playful", spec=spec)
    assert applied and calls[-1]["need_text_cue"] is False and calls[-1]["own_stems_only"] is False
    assert pd._pacing_expressive({}) is True


# ── 会话级覆写持久化 ─────────────────────────────────────────────────────────
def test_session_expressiveness_roundtrip(tmp_path, monkeypatch):
    import src.ai.persona_voice as pv

    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("AITR_DATA_DIR", raising=False)
    monkeypatch.setattr(pv, "_session_expr", {})
    monkeypatch.setattr(pv, "_session_expr_loaded", False)

    assert pv.get_session_expressiveness("telegram", "acc1", "chat9") == ""
    assert pv.set_session_expressiveness("telegram", "acc1", "chat9", "克制") == "restrained"
    assert pv.get_session_expressiveness("telegram", "acc1", "chat9") == "restrained"
    data = json.loads((tmp_path / pv.SESSION_EXPRESSIVENESS_FILENAME).read_text(encoding="utf-8"))
    assert data == {"telegram:acc1:chat9": "restrained"}

    # 重启后从盘上恢复
    monkeypatch.setattr(pv, "_session_expr", {})
    monkeypatch.setattr(pv, "_session_expr_loaded", False)
    assert pv.get_session_expressiveness("telegram", "acc1", "chat9") == "restrained"

    # 脏值 / inherit → 清除
    assert pv.set_session_expressiveness("telegram", "acc1", "chat9", "???") == ""
    assert pv.get_session_expressiveness("telegram", "acc1", "chat9") == ""
    pv.set_session_expressiveness("telegram", "acc1", "chat9", "vivid")
    assert pv.set_session_expressiveness("telegram", "acc1", "chat9", "inherit") == ""
    assert pv.get_session_expressiveness("telegram", "acc1", "chat9") == ""


def test_effective_voice_context_carries_expressiveness(monkeypatch):
    import src.ai.persona_voice as pv

    monkeypatch.setattr(pv, "_session_expr", {"telegram:acc1:chat9": "restrained"})
    monkeypatch.setattr(pv, "_session_expr_loaded", True)
    cfg = {"voice": {"enabled": True, "backend": "edge", "emotion": {"enabled": True}}}
    ctx = pv.resolve_effective_voice_context(
        cfg, text="哈哈太好笑了", platform="telegram", account_id="acc1", chat_key="chat9")
    assert ctx["expressiveness"] == "restrained"
    assert ctx["voice_cfg"]["voice_expressiveness_source"] == "session"
    ctx2 = pv.resolve_effective_voice_context(
        cfg, text="哈哈太好笑了", platform="telegram", account_id="acc1", chat_key="other")
    assert ctx2["expressiveness"] == "natural"
    assert ctx2["voice_cfg"]["voice_expressiveness_source"] == "global"
    ctx3 = pv.resolve_effective_voice_context(
        cfg, text="哈哈", platform="telegram", account_id="acc1", chat_key="other",
        expressiveness="生动")
    assert ctx3["expressiveness"] == "vivid"
