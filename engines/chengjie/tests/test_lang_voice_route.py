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


def test_mandarin_colloquial_particles_never_trigger():
    """GWJ2RZ（2026-09-12 钧机）：普通话回复凑两个共用字被整段切 zh-HK 声。

    啵/嘞/喇/咩/咯/噶/埋 是普通话口语字，靚/曬/囉 只是繁体；没有独占信号时
    再多也不算粤语。
    """
    assert not is_cantonese_text("好咯，那你先去忙，晚点再聊嘞。")
    assert not is_cantonese_text("你说咩呢，我哪会说广东话呀，别埋汰我啵。")
    assert not is_cantonese_text("一個人在家曬曬太陽、看看書，挺好的囉。")
    assert not is_cantonese_text("周末去宜家逛逛，得闲再约。")
    assert not is_cantonese_text("喇叭坏了，回头去修一下咯。")
    assert count_cantonese_markers("好咯嘞啵喇咩噶埋靚曬囉") == 0


def test_weak_markers_count_only_with_strong_signal():
    # 有独占字在场，弱字才计分：唔(1)+噶(1)=2 → 仍判粤语（既有正例不退化）
    assert is_cantonese_text("你识唔识讲粤语噶？")
    assert count_cantonese_markers("你识唔识讲粤语噶？") == 2
    # 同一句去掉独占字 → 0
    assert count_cantonese_markers("你识不识讲粤语噶？") == 0
    # 弱双字词也一样：有 唔 才算
    assert count_cantonese_markers("唔得闲") == 2
    assert count_cantonese_markers("得闲") == 0


def test_route_disabled_by_default():
    vc = {"backend": "avatar_clone", "voice": "x"}
    out, tag = route_voice_cfg_for_text(vc, "我哋讲粤语嘅", {})
    assert tag == ""
    assert out is vc, "未启用路由必须原样返回（零行为变更）"


def test_route_switches_to_cantonese_voice():
    """预置声人设（非克隆）粤语文本 → 切 zh-HK 粤语 edge 声（系统声换系统声，不穿帮）。"""
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural",
          "rvc": {"enabled": True}}
    out, tag = route_voice_cfg_for_text(
        vc, "识听少少啦，但讲得麻麻地，咁你同我讲粤语都OK嘅", _ROUTE_ON)
    assert tag == "yue"
    assert out["backend"] == "edge_tts"
    assert out["voice"] == "zh-HK-HiuMaanNeural"
    assert out["fallback_voice"] == "zh-HK-HiuMaanNeural"
    assert "rvc" not in out, "粤语路由应剥掉普通话克隆链的 RVC 附件"
    assert out["voice_profile"] == {"enabled": False}
    assert vc["backend"] == "edge_tts", "原配置不可被就地修改"


def test_route_cantonese_clone_persona_not_silently_switched_to_system_voice():
    """Q-22 #287/#304（GWJ2RZ 2026-09-12）：克隆人设 × 粤语文本，专线只配 edge 系统声
    → **不再**静默把人设换成 zh-HK 系统音；保持克隆主链、只标记 yue，交管线语种闸
    阻断（自动链改发文字 / 坐席链红条二选一）。"""
    vc = {"backend": "avatar_clone", "voice": "clone-x",
          "rvc": {"enabled": True},
          "voice_profile": {"enabled": True, "backend": "avatar_clone",
                            "reference_audio_path": "x.wav"}}
    out, tag = route_voice_cfg_for_text(
        vc, "识听少少啦，但讲得麻麻地，咁你同我讲粤语都OK嘅", _ROUTE_ON)
    assert tag == "yue"
    assert out["backend"] == "avatar_clone", "克隆主链不得被换成系统声"
    assert out["voice_profile"]["enabled"] is True
    assert out.get("_yue_text") is True
    assert out.get("voice") != "zh-HK-HiuMaanNeural"
    assert vc["backend"] == "avatar_clone", "原配置不可被就地修改"
    assert vc["voice_profile"]["enabled"] is True, "原 voice_profile 不可被改"


def test_route_cantonese_clone_line_declares_yue_capability():
    """粤语专线配成克隆节点＝运营声明会念粤语 → 携带 _lang_route_cleared=yue 放行语种闸。"""
    cfg = {"voice_lang_route": {
        "enabled": True,
        "cantonese": {"backend": "avatar_clone",
                      "voice_profile": {"clone_base_url": "http://127.0.0.1:7852"}}}}
    vc = {"backend": "avatar_clone", "voice": "clone-x",
          "voice_profile": {"enabled": True, "backend": "avatar_clone",
                            "reference_audio_path": "x.wav"}}
    out, tag = route_voice_cfg_for_text(
        vc, "识听少少啦，但讲得麻麻地，咁你同我讲粤语都OK嘅", cfg)
    assert tag == "yue"
    assert out["backend"] == "avatar_clone"
    assert out.get("_lang_route_cleared") == "yue"
    assert out["voice_profile"]["clone_base_url"] == "http://127.0.0.1:7852"


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


# ══ 粤语克隆分支（2026-08-30，为 CosyVoice2 等原生粤语克隆后端留口）══════════

_YUE_TEXT = "识听少少啦，但讲得麻麻地，咁你同我讲粤语都OK嘅"


def test_cantonese_clone_backend_keeps_voice_profile():
    """cantonese.backend 配克隆类＝运营声明该后端会念粤语 → 保留人设克隆声。"""
    cfg = {"voice_lang_route": {
        "enabled": True,
        "cantonese": {"backend": "avatar_clone"}}}
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural",
          "voice_profile": {"enabled": True, "backend": "avatar_clone",
                            "reference_audio_path": "x.wav"}}
    out, tag = route_voice_cfg_for_text(vc, _YUE_TEXT, cfg)
    assert tag == "yue"
    assert out["backend"] == "avatar_clone"
    assert out["voice_profile"]["enabled"] is True, "克隆分支不得停用人设克隆声"
    assert out["voice_profile"]["reference_audio_path"] == "x.wav"
    # 克隆失败回落 edge 时必须是粤语声——回落普通话音色念粤文正是要消灭的事故
    assert out["fallback_voice"] == "zh-HK-HiuMaanNeural"


def test_cantonese_clone_backend_voice_profile_merge_override():
    """cantonese.voice_profile 是 merge 覆写：只覆给出的键（典型=clone_base_url
    指到粤语克隆节点），人设自己的参考音/同意位保留＝同一把声讲粤语。"""
    cfg = {"voice_lang_route": {
        "enabled": True,
        "cantonese": {"backend": "minicpm_clone",
                      "voice_profile": {"backend": "minicpm_clone",
                                        "clone_base_url": "http://127.0.0.1:7852"},
                      "fallback_voice": "zh-HK-HiuGaaiNeural"}}}
    vc = {"backend": "avatar_clone", "voice": "clone-x",
          "voice_profile": {"enabled": True, "backend": "avatar_clone",
                            "owner_consent": True,
                            "reference_audio_path": "mandarin.wav"}}
    out, tag = route_voice_cfg_for_text(vc, _YUE_TEXT, cfg)
    assert tag == "yue"
    assert out["backend"] == "minicpm_clone"
    ovp = out["voice_profile"]
    assert ovp["clone_base_url"] == "http://127.0.0.1:7852"      # 覆写键生效
    assert ovp["backend"] == "minicpm_clone"
    assert ovp["reference_audio_path"] == "mandarin.wav"          # 人设参考音保留
    assert ovp["owner_consent"] is True                           # 同意位保留
    assert out["fallback_voice"] == "zh-HK-HiuGaaiNeural"
    assert vc["voice_profile"]["backend"] == "avatar_clone", "原配置不可被就地修改"


def test_cantonese_clone_backend_full_override_still_works():
    """覆写块也可带 reference_audio_path＝粤语专用参考音位（merge 语义下照样成立）。"""
    cfg = {"voice_lang_route": {
        "enabled": True,
        "cantonese": {"backend": "minicpm_clone",
                      "voice_profile": {
                          "backend": "minicpm_clone",
                          "reference_audio_path": "refs/lin_xiaoyu_yue.wav",
                          "reference_text": "粤语逐字稿"}}}}
    vc = {"backend": "avatar_clone", "voice": "clone-x",
          "voice_profile": {"enabled": True, "backend": "avatar_clone",
                            "reference_audio_path": "mandarin.wav"}}
    out, tag = route_voice_cfg_for_text(vc, _YUE_TEXT, cfg)
    assert tag == "yue"
    assert out["voice_profile"]["reference_audio_path"] == "refs/lin_xiaoyu_yue.wav"
    assert out["voice_profile"]["reference_text"] == "粤语逐字稿"


def test_cantonese_edge_backend_behavior_pinned():
    """edge 分支（默认档）行为钉死：剥 RVC、兜底同音色——与克隆分支互斥。

    Q-22（2026-09-12）起只对**非克隆**人设成立：克隆人设不再被静默换成系统声
    （见 test_route_cantonese_clone_persona_not_silently_switched_to_system_voice）。"""
    vc = {"backend": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural",
          "rvc": {"enabled": True},
          "voice_profile": {"enabled": False, "backend": "avatar_clone"}}
    out, tag = route_voice_cfg_for_text(vc, _YUE_TEXT, _ROUTE_ON)
    assert tag == "yue"
    assert out["backend"] == "edge_tts"
    assert out["voice_profile"] == {"enabled": False}
    assert "rvc" not in out
    assert out["fallback_voice"] == out["voice"]


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


# ══ 克隆链语种能力表（P0 2026-08-31「日文怪声」：effective-config.voice_langs
#    数据源，前端据此在「跟随翻译发声」目标语超出引擎能力时生成前警示）══════

def test_clone_voice_langs_hub_engine_pinned():
    """hub 开 + 引擎钉住 → 按引擎表（IndexTTS-2 只有中英=事故根因场景）。"""
    from src.ai.lang_voice_route import clone_voice_langs
    av = {"hub_fish": {"enabled": True, "tts_engine": "index_tts"}}
    assert clone_voice_langs(av, "avatar_clone", "lin_xiaoyu") == ("zh", "en")
    # 引擎名归一（别名/大小写）也认得
    av2 = {"hub_fish": {"enabled": True, "tts_engine": "IndexTTS-2"}}
    assert clone_voice_langs(av2, "avatar_clone", "x") == ("zh", "en")
    av3 = {"hub_fish": {"enabled": True, "tts_engine": "fish_speech"}}
    assert "es" in clone_voice_langs(av3, "avatar_clone", "x")


def test_clone_voice_langs_no_ja_anywhere_by_default(): # noqa: D401
    """#161（2026-09-03 钧机 1145）：日语不得在任何缺省能力表里出现。

    1.0.71 实录：日语三次 CER>0.35、重合成仍「ゾオパパパ」乱音。fish 的 ja
    实证属 8-02 的 fish 时代档（8-31 起 hub 档以 IndexTTS-2 形态注册，fish
    消费不到正确参考音，旧实证不可比）；avatar_clone 的 ja/ko 是照抄
    CosyVoice3 官方宣称、从无本地实测。缺省一律不放行，要开闸先过
    tools/verify_clone_lang.py 或由运营显式 voice_langs 背书。
    """
    from src.ai.lang_voice_route import (
        CLONE_ENGINE_LANGS, clone_voice_langs)
    for eng, langs in CLONE_ENGINE_LANGS.items():
        assert "ja" not in langs, f"{eng} 缺省表不得含 ja"
        assert "ko" not in langs, f"{eng} 缺省表不得含 ko"
    for backend in ("avatar_clone", "minicpm_clone", "voice_clone_lan",
                    "voice_clone_command", "coqui_http"):
        got = clone_voice_langs({}, backend, "x")
        assert "ja" not in got and "ko" not in got, (backend, got)


def test_clone_backend_table_derives_from_engine_table():
    """后端表必须是引擎表的投影——#161 前两表各写一份，avatar_clone 声称
    ja/ko 而其引擎（CosyVoice3）从未实测过，hub 未开的部署直接放行到合成。"""
    from src.ai.lang_voice_route import (
        _CLONE_BACKEND_ENGINE, CLONE_ENGINE_LANGS, clone_voice_langs)
    for backend, engine in _CLONE_BACKEND_ENGINE.items():
        assert engine in CLONE_ENGINE_LANGS, f"{backend} 指向未登记引擎 {engine}"
        assert clone_voice_langs({}, backend, "x") == CLONE_ENGINE_LANGS[engine]


def test_clone_voice_langs_hub_unknown_engine_returns_empty():
    """hub 档上引擎未钉/未登记 → ()=能力未知，前端不警示（宁漏不冤枉）。"""
    from src.ai.lang_voice_route import clone_voice_langs
    assert clone_voice_langs(
        {"hub_fish": {"enabled": True}}, "avatar_clone", "x") == ()
    assert clone_voice_langs(
        {"hub_fish": {"enabled": True, "tts_engine": "totally_new_engine"}},
        "avatar_clone", "x") == ()


def test_clone_voice_langs_allowlist_miss_falls_to_backend_table():
    """hub 开但人设不在 allowlist → 主路是本地克隆链，按后端缺省表。"""
    from src.ai.lang_voice_route import clone_voice_langs
    av = {"hub_fish": {"enabled": True, "tts_engine": "index_tts",
                       "persona_allowlist": ["a", "b"]}}
    # avatar_clone=7852 CosyVoice3；#161 起只登记实测念对的 zh/en/es/tl
    assert clone_voice_langs(av, "avatar_clone", "not_in_list") == \
        ("zh", "en", "es", "tl")
    # minicpm_clone 契约家族保守中英
    assert clone_voice_langs(av, "minicpm_clone", "not_in_list") == ("zh", "en")


def test_clone_voice_langs_operator_override_wins():
    """运营显式覆写 avatar_voice.voice_langs 最高优先（去重+前缀归一）。"""
    from src.ai.lang_voice_route import clone_voice_langs
    av = {"voice_langs": ["ZH", "en", "ja", "zh-TW", ""],
          "hub_fish": {"enabled": True, "tts_engine": "index_tts"}}
    assert clone_voice_langs(av, "avatar_clone", "x") == ("zh", "en", "ja")


def test_clone_voice_langs_unknown_backend_empty_and_never_raises():
    from src.ai.lang_voice_route import clone_voice_langs
    assert clone_voice_langs({}, "edge_tts", "") == ()
    assert clone_voice_langs(None, "", "") == ()


# ══ 合成前语种能力闸 clone_lang_gate（P0 2026-08-31：UI 警示之外的全链收口，
#    TTSPipeline 在合成前单点消费；A 线/B 线/主动触达自动链自此不再产怪声）══════

_AV_HUB_INDEX = {"hub_fish": {"enabled": True, "tts_engine": "index_tts",
                              "persona_allowlist": ["p1"]}}


def test_clone_lang_gate_blocks_ja_on_index_tts():
    """事故根因场景：日文文本 + hub 钉 IndexTTS-2（仅中英）→ 拦（返回 ja）。"""
    from src.ai.lang_voice_route import clone_lang_gate
    txt = "どこにいるの。ご飯は食べた？一緒に遊びに行こうよ。"
    assert clone_lang_gate(txt, _AV_HUB_INDEX, "avatar_clone", "p1") == "ja"
    # 泰文同理（脚本明确、表内没有）
    assert clone_lang_gate(
        "สวัสดีค่ะ วันนี้เป็นยังไงบ้าง", _AV_HUB_INDEX, "avatar_clone", "p1") == "th"


def test_clone_lang_gate_passes_supported_and_unknown():
    """中英照常放行；语种不明/短文本/能力表未知 → 放行（宁可漏拦不误拦）。"""
    from src.ai.lang_voice_route import clone_lang_gate
    av = _AV_HUB_INDEX
    assert clone_lang_gate("今天过得怎么样呀？", av, "avatar_clone", "p1") == ""
    assert clone_lang_gate("How was your day today?", av, "avatar_clone", "p1") == ""
    assert clone_lang_gate("OK", av, "avatar_clone", "p1") == ""      # 短文本→unknown
    assert clone_lang_gate("", av, "avatar_clone", "p1") == ""
    # 能力表未知（hub 引擎未钉）→ 不拦
    assert clone_lang_gate(
        "どこにいるの。ご飯は食べた？", {"hub_fish": {"enabled": True}},
        "avatar_clone", "p1") == ""


def test_clone_lang_gate_non_clone_backend_never_blocks():
    """edge/openai 等非克隆后端不归本闸管（edge 的语种由 lang_voice_route 路由）。"""
    from src.ai.lang_voice_route import clone_lang_gate, is_clone_backend
    txt = "どこにいるの。ご飯は食べた？"
    assert clone_lang_gate(txt, _AV_HUB_INDEX, "edge_tts", "p1") == ""
    assert clone_lang_gate(txt, _AV_HUB_INDEX, "", "p1") == ""
    assert is_clone_backend("avatar_clone") and is_clone_backend("minicpm_clone")
    assert not is_clone_backend("edge_tts") and not is_clone_backend("")


def test_clone_lang_gate_kaomoji_kana_not_misjudged_as_ja():
    """中文正文夹颜文字假名（ヾ(≧▽≦*)o）→ 不得拦成 ja——拦错=中文被转去
    edge 日语声念「日本腔中文」（2026-07-23 事故形态）。真日文（假名占主体）照拦。"""
    from src.ai.lang_voice_route import clone_lang_gate
    zh_kaomoji = "好呀好呀，今天超开心的啦 ヾ(≧▽≦*)o 等你消息哦"
    assert clone_lang_gate(zh_kaomoji, _AV_HUB_INDEX, "avatar_clone", "p1") == ""
    real_ja = "今日はほんとに楽しかったよ、また遊ぼうね"
    assert clone_lang_gate(real_ja, _AV_HUB_INDEX, "avatar_clone", "p1") == "ja"


def test_clone_lang_gate_respects_operator_override():
    """运营显式 voice_langs 覆写含 ja → 日文放行（部署方最了解自家节点）。"""
    from src.ai.lang_voice_route import clone_lang_gate
    av = {"voice_langs": ["zh", "en", "ja"],
          "hub_fish": {"enabled": True, "tts_engine": "index_tts"}}
    assert clone_lang_gate(
        "どこにいるの。ご飯は食べた？", av, "avatar_clone", "p1") == ""


# ══ 繁→简发音输入 to_simplified_for_tts（P0-3 2026-08-31：zh-tw 零成本进克隆
#    覆盖——繁简同音，只转喂给引擎的文本；日文/粤语/非中文内建豁免）══════════

def test_to_simplified_for_tts_converts_traditional():
    import pytest as _pytest
    _pytest.importorskip("opencc")
    from src.ai.lang_voice_route import to_simplified_for_tts
    out = to_simplified_for_tts("時間還早，我們一起去聽寫測試吧")
    assert "时间" in out and "听写" in out and "們" not in out


def test_to_simplified_for_tts_leaves_japanese_alone():
    """日文汉字与繁体同形（聞く→闻く 会把日文改成简中混排）→ 含假名文本原样返回。"""
    from src.ai.lang_voice_route import to_simplified_for_tts
    ja = "ニュースを聞くのが好きです"
    assert to_simplified_for_tts(ja) == ja


def test_to_simplified_for_tts_leaves_cantonese_alone():
    """粤语用字不属繁简映射且有专线路由 → 粤语文本原样返回。"""
    from src.ai.lang_voice_route import to_simplified_for_tts
    yue = "你而家喺邊度呀？我哋一齊去食飯啦，唔好咁晏喎"
    assert to_simplified_for_tts(yue) == yue


def test_to_simplified_for_tts_identity_on_simplified_and_soft_fail():
    from src.ai.lang_voice_route import to_simplified_for_tts
    s = "今天过得怎么样呀"
    assert to_simplified_for_tts(s) == s
    assert to_simplified_for_tts("") == ""
    assert to_simplified_for_tts(None) in ("", None)  # 防御式：绝不抛


# ══ P1 语种→引擎路由（hub_fish.lang_engines，2026-08-31 全语种克隆地基）═══════
# 能力表（clone_voice_langs）与择引擎（hub_engine_for_lang）读同一份配置，
# 「闸门说能念」与「合成真用那台引擎」口径自动一致。

def test_clone_voice_langs_lang_engines_extends_capability():
    from src.ai.lang_voice_route import clone_voice_langs
    # 改派目标引擎**确实登记了**该语种才计入（#161 后 fish 的实测语种是 es）
    av = {"hub_fish": {"enabled": True, "tts_engine": "index_tts",
                       "lang_engines": {"es": "fish_speech"}}}
    assert clone_voice_langs(av, "avatar_clone", "p") == ("zh", "en", "es")
    # #161：改派到 fish 的日语不再虚报能力——fish 自己的表里已经没有 ja 了
    av_ja = {"hub_fish": {"enabled": True, "tts_engine": "index_tts",
                          "lang_engines": {"ja": "fish_speech"}}}
    assert clone_voice_langs(av_ja, "avatar_clone", "p") == ("zh", "en")
    # 映射到未登记引擎的语种不虚报能力
    av2 = {"hub_fish": {"enabled": True, "tts_engine": "index_tts",
                        "lang_engines": {"th": "brand_new_engine"}}}
    assert clone_voice_langs(av2, "avatar_clone", "p") == ("zh", "en")
    # 主引擎未钉 → 仍是能力未知（映射不足以撑起整条主路的判定）
    av3 = {"hub_fish": {"enabled": True,
                        "lang_engines": {"es": "fish_speech"}}}
    assert clone_voice_langs(av3, "avatar_clone", "p") == ()


def test_hub_engine_for_lang_lookup():
    from src.ai.lang_voice_route import hub_engine_for_lang
    av = {"hub_fish": {"enabled": True,
                       "lang_engines": {"ja": "fish_speech",
                                        "ko-KR": "cosyvoice"}}}
    assert hub_engine_for_lang(av, "ja") == "fish_speech"
    assert hub_engine_for_lang(av, "ja-JP") == "fish_speech"   # 查询带地区码
    assert hub_engine_for_lang(av, "ko") == "cosyvoice"        # 配置键带地区码
    assert hub_engine_for_lang(av, "en") == ""
    assert hub_engine_for_lang({}, "ja") == ""
    assert hub_engine_for_lang(None, "") == ""


def test_clone_lang_gate_respects_lang_engines_routing():
    """改派到**确实会念**该语种的引擎 → 放行；表外语种仍拦。

    #161 起用 es 做正例：日语已从所有引擎表撤下，「改派 fish 念日语」正是
    1145 证据群里念出「ゾオパパパ」的那条路，闸门必须照拦。"""
    from src.ai.lang_voice_route import clone_lang_gate
    av = {"hub_fish": {"enabled": True, "tts_engine": "index_tts",
                       "persona_allowlist": ["p1"],
                       "lang_engines": {"es": "fish_speech"}}}
    assert clone_lang_gate(
        "Hola, ¿cómo estás? Quiero saber más sobre esto, señor",
        av, "avatar_clone", "p1") == ""
    assert clone_lang_gate(
        "สวัสดีค่ะ วันนี้เป็นยังไงบ้าง", av, "avatar_clone", "p1") == "th"
    av_ja = {"hub_fish": {"enabled": True, "tts_engine": "index_tts",
                          "persona_allowlist": ["p1"],
                          "lang_engines": {"ja": "fish_speech"}}}
    assert clone_lang_gate(
        "どこにいるの。ご飯は食べた？", av_ja, "avatar_clone", "p1") == "ja"


def test_clone_voice_langs_hub_branch_only_for_avatar_family():
    """hub 表只套 avatar_clone 家族——minicpm/lan/coqui 直连节点根本不经 hub，
    套 hub 引擎表会把「语种路由改写后的后端」误判（2026-08-31 修）。"""
    from src.ai.lang_voice_route import clone_voice_langs
    av = {"hub_fish": {"enabled": True, "tts_engine": "fish_speech",
                       "persona_allowlist": ["p1"]}}
    assert "es" in clone_voice_langs(av, "avatar_clone", "p1")   # hub 表（fish）
    assert clone_voice_langs(av, "", "p1") != ()                 # backend 空沿用 hub 语义
    assert clone_voice_langs(av, "minicpm_clone", "p1") == ("zh", "en")   # 后端表
    assert clone_voice_langs(av, "voice_clone_lan", "p1") == ("zh", "en")


# ══ 通用「语种 → 克隆节点」路由 voice_lang_route.clone_langs（P2 2026-08-31，
#    粤语专线机制泛化：同一把声讲外语，节点×语种先过验收工具再落配置）══════════

_ROUTE_CFG_JA = {"voice_lang_route": {
    "enabled": True,
    "follow_text": {"enabled": False},     # 隔离：只测克隆路由本身
    "clone_langs": {"ja": {"voice_profile": {
        "clone_base_url": "http://127.0.0.1:7852",
        "clone_text_prefix": "<|ja|>"}}},
}}


def test_clone_lang_route_rewrites_backend_profile_and_clears_gate():
    from src.ai.lang_voice_route import route_voice_cfg_for_text
    vc = {"backend": "avatar_clone",
          "voice_profile": {"enabled": True, "backend": "avatar_clone",
                            "reference_audio_path": "x/ref.wav"}}
    out, tag = route_voice_cfg_for_text(
        vc, "今日はどこかへ出かけましたか？", _ROUTE_CFG_JA)
    assert tag == "ja"
    assert out["backend"] == "minicpm_clone"          # 缺省直连后端
    vp = out["voice_profile"]
    assert vp["backend"] == "minicpm_clone"           # 强制对齐防人设档静默盖回
    assert vp["clone_base_url"] == "http://127.0.0.1:7852"
    assert vp["clone_text_prefix"] == "<|ja|>"
    assert vp["reference_audio_path"] == "x/ref.wav"  # 参考音跟人设走=同一把声
    assert out["fallback_voice"] == "ja-JP-NanamiNeural"   # 兜底按语种取内置表
    assert out["_lang_route_cleared"] == "ja"
    assert "clone_base_url" not in vc["voice_profile"]     # 不改入参


def test_clone_lang_route_skips_zh_unknown_unlisted():
    from src.ai.lang_voice_route import route_voice_cfg_for_text
    vc = {"backend": "avatar_clone"}
    out, tag = route_voice_cfg_for_text(vc, "今天过得怎么样呀？", _ROUTE_CFG_JA)
    assert tag == "" and out.get("_lang_route_cleared") is None    # zh 不路由
    out2, tag2 = route_voice_cfg_for_text(
        vc, "สวัสดีค่ะ วันนี้เป็นยังไงบ้าง", _ROUTE_CFG_JA)
    assert tag2 == "" and out2.get("_lang_route_cleared") is None  # 映射外不路由
    out3, tag3 = route_voice_cfg_for_text(vc, "OK", _ROUTE_CFG_JA)
    assert tag3 == "" and out3.get("_lang_route_cleared") is None  # unknown 不路由


def test_clone_lang_route_yields_to_cantonese_lane():
    """粤语专线（特征字检测）优先于通用克隆路由——yue 文本走 yue 专线。"""
    from src.ai.lang_voice_route import route_voice_cfg_for_text
    cfg = {"voice_lang_route": {
        "enabled": True,
        "cantonese": {"voice": "zh-HK-HiuMaanNeural"},
        "clone_langs": {"ja": {}},
    }}
    out, tag = route_voice_cfg_for_text(
        {"backend": "edge_tts"}, "你而家喺邊度呀？我哋一齊去食飯啦", cfg)
    assert tag == "yue"


def test_clone_route_langs_helper():
    """主动触达 planner 的能力并集数据源：路由整体关=空；zh 剔除；前缀归一。"""
    from src.ai.lang_voice_route import clone_route_langs
    cfg = {"voice_lang_route": {"enabled": True, "clone_langs": {
        "ja": {"backend": "minicpm_clone"},
        "KO-kr": {"backend": "minicpm_clone"},
        "zh": {"backend": "minicpm_clone"},     # zh 不属外语路由语义，剔除
        "th": "not-a-dict",                     # 垃圾值容忍
    }}}
    assert clone_route_langs(cfg) == ("ja", "ko")
    assert clone_route_langs(
        {"voice_lang_route": {"enabled": False,
                              "clone_langs": {"ja": {}}}}) == ()
    assert clone_route_langs({}) == ()
    assert clone_route_langs(None) == ()
