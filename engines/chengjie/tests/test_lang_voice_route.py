"""出站语音语言路由（粤语路由 + follow_text 通用语种路由）单测。"""

from __future__ import annotations

from src.ai import lang_voice_route as lvr
from src.ai.lang_voice_route import (
    EDGE_VOICE_BY_LANG,
    count_cantonese_markers,
    default_edge_voice_for_lang,
    detect_text_lang,
    edge_voice_lang_prefix,
    is_cantonese_text,
    is_reject_tag,
    route_voice_cfg_for_text,
)

_ROUTE_ON = {
    "voice_lang_route": {
        "enabled": True,
        "cantonese": {"voice": "zh-HK-HiuMaanNeural"},
    },
}


def test_cantonese_detection_positive():
    # 今天生产实测的真实粤语回复/转写
    assert is_cantonese_text("识听少少啦，但讲得麻麻地，你要同我讲粤语都OK")
    assert is_cantonese_text("我哋啲客咧，都系大部分都系香港嘅，咁，我哋讲粤语咧好啲嘅。")
    assert is_cantonese_text("你识唔识讲粤语噶？")


def test_cantonese_detection_negative():
    # 普通话/书面中文绝不误伤
    assert not is_cantonese_text("你好，我们的产品支持多平台自动回复，欢迎咨询。")
    assert not is_cantonese_text("帮我介绍一下智聊的价格")
    assert not is_cantonese_text("Hello, how are you today?")
    assert not is_cantonese_text("")


def test_marker_scoring():
    assert count_cantonese_markers("咁样点解唔得嘅") >= 4
    assert count_cantonese_markers("普通话文本") == 0


def test_route_disabled_by_default():
    vc = {"backend": "avatar_clone", "voice": "x"}
    out, tag = route_voice_cfg_for_text(vc, "我哋讲粤语嘅", {})
    assert tag == ""
    assert out is vc, "未启用路由必须原样返回（零行为变更）"


def test_route_switches_to_cantonese_voice():
    vc = {"backend": "avatar_clone", "voice": "clone-x",
          "rvc": {"enabled": True},
          "voice_profile": {"enabled": True, "backend": "avatar_clone",
                            "reference_audio_path": "x.wav"}}
    out, tag = route_voice_cfg_for_text(
        vc, "识听少少啦，但讲得麻麻地，咁你同我讲粤语都OK嘅", _ROUTE_ON)
    assert tag == "yue"
    assert out["backend"] == "edge_tts"
    assert out["voice"] == "zh-HK-HiuMaanNeural"
    assert out["fallback_voice"] == "zh-HK-HiuMaanNeural"
    assert "rvc" not in out, "粤语路由应剥掉普通话克隆链的 RVC 附件"
    # 回归守卫（2026-07-21 生产事故）：voice_profile.enabled=true 时
    # TTSPipeline 用 voice_profile.backend 覆盖顶层 backend——路由必须停用它，
    # 否则日志「切 edge_tts」但实际仍走克隆链（provider=avatar_clone）。
    assert out["voice_profile"] == {"enabled": False}
    assert vc["backend"] == "avatar_clone", "原配置不可被就地修改"
    assert vc["voice_profile"]["enabled"] is True, "原 voice_profile 不可被改"


def test_route_mandarin_clone_backend_and_voice_untouched():
    """普通话文本走克隆链：主后端/音色/人设声不动；follow_text 只把 edge 兜底
    音色对齐中文（原先 fallback 空 → 克隆失败时会回落 pipeline 默认音色，
    默认若是 ja 声就是事故重演——对齐后兜底路径也钉死中文音色）。"""
    vc = {"backend": "avatar_clone", "voice": "clone-x"}
    out, tag = route_voice_cfg_for_text(
        vc, "好的，我把产品资料发给您，请查收。", _ROUTE_ON)
    assert tag == ""
    assert out["backend"] == "avatar_clone"
    assert out["voice"] == "clone-x"
    assert out["fallback_voice"] == "zh-CN-XiaoxiaoNeural"
    assert vc == {"backend": "avatar_clone", "voice": "clone-x"}, "原配置不可变"


def test_route_mandarin_untouched_when_follow_text_off():
    """follow_text 关闭时恢复旧行为：普通话文本原样返回（含对象同一性）。"""
    cfg = {"voice_lang_route": {
        "enabled": True,
        "cantonese": {"voice": "zh-HK-HiuMaanNeural"},
        "follow_text": {"enabled": False}}}
    vc = {"backend": "avatar_clone", "voice": "clone-x"}
    out, tag = route_voice_cfg_for_text(
        vc, "好的，我把产品资料发给您，请查收。", cfg)
    assert tag == ""
    assert out is vc


# ══ follow_text 通用「音色跟随文本语种」路由（2026-07-23 生产事故回归网）══════
# 事故：telegram.voice_reply.voice=ja-JP-NanamiNeural（历史默认）→ WhatsApp 新
# 好友无人设绑定，回落该音色，中文回复被日语女声念成「日本腔中文」，客户投诉
# "第一句总是日语"。follow_text=每条合成前把音色语言对齐文本语种。


def test_helper_edge_voice_lang_prefix():
    assert edge_voice_lang_prefix("ja-JP-NanamiNeural") == "ja"
    assert edge_voice_lang_prefix("zh-CN-XiaoxiaoNeural") == "zh"
    assert edge_voice_lang_prefix("fil-PH-BlessicaNeural") == "fil"
    assert edge_voice_lang_prefix("clone_speaker_01") == ""
    assert edge_voice_lang_prefix("") == ""


def test_helper_default_edge_voice_for_lang():
    assert default_edge_voice_for_lang("zh") == "zh-CN-XiaoxiaoNeural"
    assert default_edge_voice_for_lang("EN") == "en-US-JennyNeural"
    assert default_edge_voice_for_lang("pt-BR") == "pt-BR-FranciscaNeural"
    assert default_edge_voice_for_lang("xx") == ""


def test_detect_text_lang_short_text_guard():
    # 内容量不足 → unknown（防中文会话里一句 "OK" 被路由成英文音色）
    assert detect_text_lang("OK") == "unknown"
    assert detect_text_lang("？") == "unknown"
    assert detect_text_lang("") == "unknown"
    assert detect_text_lang("你好呀，今天过得怎么样？") == "zh"
    assert detect_text_lang("Hey! How was your day today?") == "en"


def test_follow_text_ja_voice_reads_zh_switches_to_zh():
    """事故主场景：ja 音色 + 中文文本 → 必须切中文音色。"""
    vc = {"backend": "edge_tts", "voice": "ja-JP-NanamiNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "你好呀，很高兴认识你，今天过得怎么样？", _ROUTE_ON)
    assert tag == "zh"
    assert out["backend"] == "edge_tts"
    assert out["voice"] == "zh-CN-XiaoxiaoNeural"
    assert out["fallback_voice"] == "zh-CN-XiaoxiaoNeural"
    assert out["voice_profile"] == {"enabled": False}
    assert vc["voice"] == "ja-JP-NanamiNeural", "原配置不可被就地修改"


def test_follow_text_zh_voice_reads_en_switches_to_en():
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "Sure! Let me send you the product brochure right away.", _ROUTE_ON)
    assert tag == "en"
    assert out["voice"] == "en-US-JennyNeural"


def test_follow_text_matching_lang_untouched():
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "好的，我把产品资料发给您，请查收。", _ROUTE_ON)
    assert tag == ""
    assert out is vc


def test_follow_text_multilingual_voice_exempt():
    """Multilingual 系音色自适应语种，任何语言都不换声。"""
    vc = {"backend": "edge_tts", "voice": "en-US-AvaMultilingualNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "你好呀，很高兴认识你，今天过得怎么样？", _ROUTE_ON)
    assert tag == ""
    assert out is vc


def test_follow_text_voice_profile_effective_voice_considered():
    """voice_profile 生效时按其 speaker_id 判定当前音色（与 TTSPipeline 同口径）。"""
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural",
          "voice_profile": {"enabled": True, "backend": "edge_tts",
                            "speaker_id": "ja-JP-NanamiNeural"}}
    out, tag = route_voice_cfg_for_text(
        vc, "你好呀，很高兴认识你，今天过得怎么样？", _ROUTE_ON)
    assert tag == "zh"
    assert out["voice"] == "zh-CN-XiaoxiaoNeural"
    assert out["voice_profile"] == {"enabled": False}


def test_follow_text_clone_backend_keeps_backend_aligns_fallback():
    """克隆链原生跟随文本语种：主后端不动，只对齐 edge 兜底音色。"""
    vc = {"backend": "avatar_clone", "voice": "clone-x",
          "fallback_voice": "ja-JP-NanamiNeural",
          "voice_profile": {"enabled": True, "backend": "avatar_clone",
                            "reference_audio_path": "x.wav"}}
    out, tag = route_voice_cfg_for_text(
        vc, "你好呀，很高兴认识你，今天过得怎么样？", _ROUTE_ON)
    assert tag == "", "主后端未变，tag 必须为空（不影响 no_edge 观测口径）"
    assert out["backend"] == "avatar_clone"
    assert out["fallback_voice"] == "zh-CN-XiaoxiaoNeural"
    assert out["voice_profile"]["enabled"] is True, "克隆链人设声不可被停用"


def test_follow_text_clone_backend_fallback_already_aligned_untouched():
    vc = {"backend": "avatar_clone", "voice": "clone-x",
          "fallback_voice": "zh-CN-XiaoxiaoNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "你好呀，很高兴认识你，今天过得怎么样？", _ROUTE_ON)
    assert tag == ""
    assert out is vc


def test_follow_text_config_voices_override():
    cfg = {"voice_lang_route": {
        "enabled": True,
        "follow_text": {"voices": {"en": "en-US-AriaNeural"}}}}
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "Sure! Let me send you the product brochure right away.", cfg)
    assert tag == "en"
    assert out["voice"] == "en-US-AriaNeural"


def test_follow_text_reject_unmapped(monkeypatch):
    """语种明确但无音色可映射 → reject tag，调用方回落文字（宁缺毋滥）。"""
    monkeypatch.setattr(lvr, "detect_text_lang", lambda t: "bn")  # 无映射语种
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}
    out, tag = route_voice_cfg_for_text(vc, "placeholder text", _ROUTE_ON)
    assert tag == "reject:bn"
    assert is_reject_tag(tag)
    assert not is_reject_tag("zh") and not is_reject_tag("")


def test_follow_text_reject_unmapped_off_keeps_voice(monkeypatch):
    monkeypatch.setattr(lvr, "detect_text_lang", lambda t: "bn")
    cfg = {"voice_lang_route": {
        "enabled": True, "follow_text": {"reject_unmapped": False}}}
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"}
    out, tag = route_voice_cfg_for_text(vc, "placeholder text", cfg)
    assert tag == ""
    assert out is vc


def test_follow_text_can_be_disabled_independently():
    cfg = {"voice_lang_route": {
        "enabled": True, "follow_text": {"enabled": False}}}
    vc = {"backend": "edge_tts", "voice": "ja-JP-NanamiNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "你好呀，很高兴认识你，今天过得怎么样？", cfg)
    assert tag == ""
    assert out is vc


def test_follow_text_cantonese_takes_priority():
    """粤语路由（更专门的判定）优先于 follow_text 通用路由。"""
    vc = {"backend": "edge_tts", "voice": "ja-JP-NanamiNeural"}
    out, tag = route_voice_cfg_for_text(
        vc, "识听少少啦，但讲得麻麻地，咁你同我讲粤语都OK嘅", _ROUTE_ON)
    assert tag == "yue"
    assert out["voice"] == "zh-HK-HiuMaanNeural"


def test_builtin_map_voices_language_consistent():
    """内置映射表自洽：键=音色 ID 的语言前缀（fil 别名 tl 除外）。"""
    alias = {"tl": "fil"}
    for lang, voice in EDGE_VOICE_BY_LANG.items():
        want = alias.get(lang, lang)
        assert edge_voice_lang_prefix(voice) == want, (lang, voice)


# ══ 路由观测（lang_route_stats）——routed/rejected/fallback_aligned 计数 ══════


def test_route_stats_counted_at_single_choke_point(monkeypatch):
    """route_voice_cfg_for_text 单一出口计数：三条出站链零漂移全覆盖。"""
    from src.ai.lang_route_stats import get_lang_route_stats
    st = get_lang_route_stats()
    st.reset()

    # 未启用 → 不计数（分母只算启用后的决策）
    route_voice_cfg_for_text({"backend": "edge_tts", "voice": "x"}, "你好呀朋友", {})
    assert st.dump()["checks"] == 0

    # 命中 follow_text（ja 音色念中文 → 切 zh）
    route_voice_cfg_for_text(
        {"backend": "edge_tts", "voice": "ja-JP-NanamiNeural"},
        "你好呀，很高兴认识你，今天过得怎么样？", _ROUTE_ON)
    # 粤语路由
    route_voice_cfg_for_text(
        {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"},
        "识听少少啦，但讲得麻麻地，咁你同我讲粤语都OK嘅", _ROUTE_ON)
    # 克隆链兜底对齐（tag 空但 cfg 变了）
    route_voice_cfg_for_text(
        {"backend": "avatar_clone", "voice": "c", "fallback_voice": "ja-JP-NanamiNeural"},
        "你好呀，很高兴认识你，今天过得怎么样？", _ROUTE_ON)
    # 拒发守卫
    monkeypatch.setattr(lvr, "detect_text_lang", lambda t: "bn")
    route_voice_cfg_for_text(
        {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"},
        "placeholder text", _ROUTE_ON)
    # 未命中（同语种，直通）
    monkeypatch.undo()
    route_voice_cfg_for_text(
        {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"},
        "好的，我把产品资料发给您，请查收。", _ROUTE_ON)

    d = get_lang_route_stats().dump()
    assert d["checks"] == 5
    assert d["routed"] == {"zh": 1, "yue": 1}
    assert d["rejected"] == {"bn": 1}
    assert d["fallback_aligned"] == 1
    assert d["routed_total"] == 2 and d["rejected_total"] == 1

    prom = get_lang_route_stats().dump_prom()
    assert "lang_voice_route_checks_total 5" in prom
    assert 'lang_voice_route_routed_total{lang="yue"} 1' in prom
    assert 'lang_voice_route_rejected_total{lang="bn"} 1' in prom
    st.reset()


def test_route_stats_sanitizes_dirty_tags():
    from src.ai.lang_route_stats import LangRouteStats
    st = LangRouteStats()
    st.record_routed("  EN ")
    st.record_rejected("weird tag!\n")
    d = st.dump()
    assert d["routed"] == {"en": 1}
    assert d["rejected"] == {"unknown": 1}
