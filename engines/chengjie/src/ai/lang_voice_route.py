"""出站语音按语言路由音色（粤语→粤语 TTS + 通用「音色跟随文本语种」）。

背景：克隆声后端（avatar_clone/CosyVoice）用普通话音色读粤语文本，发音"唔咸唔淡"；
edge 链则更糟——音色是**按语言训练的**（ja-JP-NanamiNeural 念中文=「日本腔中文」，
2026-07-23 生产实锤：客户投诉"老是讲日语"，其实文本是中文、音色是日语女声）。
本模块把「本条合成文本的语种」与「本次要用的音色」对齐：

1. **粤语路由**（既有）：文本含粤语特征字 → 切地道粤语 Edge 音色。
2. **follow_text 通用路由**（2026-07-23 新增）：
   - 生效面=**edge_tts 链**（顶层或 voice_profile 生效后端为 edge_tts）；
     openai/elevenlabs 天然多语、克隆链原生跟随文本语种（Phase6 实证），不动主后端。
   - 检测文本语种（``translation_service.detect_language``，确定性零 LLM）；
     与当前音色的 BCP47 语言前缀不一致 → 换成该语种的映射音色
     （``follow_text.voices`` 覆写 > 内置 ``EDGE_VOICE_BY_LANG``）。
   - **Multilingual 音色豁免**（如 en-US-AvaMultilingualNeural）：自适应语种，不换。
   - **拒发守卫**：语种明确但无音色可映射（含配置残缺）→ 返回 ``reject:<lang>``，
     调用方放弃语音回落文字——「宁可没语音，不发错语言的语音」。
   - 克隆链虽不动主后端，但把 ``fallback_voice`` 对齐文本语种，克隆失败回落 edge
     时不再用错语言音色兜底。

设计：
- 纯函数 + 配置门控 ``voice_lang_route.enabled``（默认关，基线零行为变更）；
  ``follow_text.enabled`` 随父开关默认开（开了语言路由=要正确的语言路由）。
- 路由是**主动选择**而非兜底降级 → 不受 ``no_edge_fallback`` 拒发约束；
- 本模块的语种→Edge 音色映射是**单一事实源**（proactive_voice_foreign 复用）。

接线点（全部出站语音路径同口径）：
- ``inbox/voice_autosend._synth_ogg`` —— System Z autosend / protocol 自动回复
  （WA Baileys、messenger-web…）/ 主动触达中文语音（stage_voice_file 共用）；
- ``client/sender._maybe_send_voice_reply`` —— 原生 Telegram 自动语音回复；
- ``web/routes/unified_inbox_send_routes`` 手动坐席发语音 —— 坐席显式覆写
  voice/backend 时跳过路由（尊重人工选择），否则同自动路径；
- 试听/测试端点（voice_routes tts-test、voice_live preview）**刻意不路由**：
  那是「听这套配置本身」的工具，路由会掩盖操作员想验证的音色。

局限（诚实边界）：Edge 音色不是人设克隆音（音色会变）。要"同一把声讲外语"
需克隆后端原生多语（CosyVoice3 支持 zh/en/ja/ko 等；下阶段按语种能力分流）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 粤语专用/高频特征字（书面普通话几乎不用）。一个字算一个 marker。
_CANTO_CHARS = "嘅咁咗唔哋喺嚟氹嗰乜嘢佢睇畀冇諗攞嗌埋啱噉嚿餸靚嗮曬啵嘞喇喎咩囉噶嗌咯嘞"
# 粤语双字组合（比单字更强的信号）
_CANTO_BIGRAMS = (
    "点解", "而家", "咁样", "系咪", "唔系", "唔好", "唔使", "唔通", "几好",
    "好耐", "琴日", "听日", "宜家", "得闲", "冇问题", "麻麻地", "咪住",
    "識聽", "识听", "講嘢", "讲嘢", "俾我", "畀我", "睇下", "谂住",
)

# ── 语种前缀 → Edge 神经声（女声系，与陪伴人设性别一致；可经配置覆写）─────────
# 单一事实源：proactive_voice_foreign（主动外语开场）与 follow_text 路由共用。
# 覆盖 translation_service.detect_language 能返回的全部明确语种。
EDGE_VOICE_BY_LANG: Dict[str, str] = {
    "zh": "zh-CN-XiaoxiaoNeural",
    "en": "en-US-JennyNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "th": "th-TH-PremwadeeNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "id": "id-ID-GadisNeural",
    "ms": "ms-MY-YasminNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "ar": "ar-EG-SalmaNeural",
    "hi": "hi-IN-SwaraNeural",
    "tr": "tr-TR-EmelNeural",
    "tl": "fil-PH-BlessicaNeural",
    "fil": "fil-PH-BlessicaNeural",
    "km": "km-KH-SreymomNeural",
    "he": "he-IL-HilaNeural",
    "el": "el-GR-AthinaNeural",
}

# follow_text 拒发守卫的 tag 前缀（调用方判 tag.startswith 即可识别）
REJECT_TAG_PREFIX = "reject:"

# 与 TTSPipeline 的克隆后端集合同口径（克隆链原生跟随文本语种，不动主后端）
_CLONE_BACKENDS = frozenset({
    "avatar_clone", "minicpm_clone", "voice_clone_lan", "voice_clone_command",
    "coqui_http",
})

_LATIN_LETTER_RE = re.compile(r"[A-Za-z\u00C0-\u024F]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def count_cantonese_markers(text: str) -> int:
    """统计粤语特征信号数（单字 1 分、双字组合 2 分）。"""
    t = str(text or "")
    if not t:
        return 0
    score = sum(1 for ch in t if ch in _CANTO_CHARS)
    for bg in _CANTO_BIGRAMS:
        if bg in t:
            score += 2
    return score


def is_cantonese_text(text: str, *, min_markers: int = 2) -> bool:
    """文本是否为粤语书写（特征分 >= 阈值）。短文本按比例放宽由调用方决定。"""
    return count_cantonese_markers(text) >= max(1, int(min_markers))


def default_edge_voice_for_lang(lang: str) -> str:
    """语种前缀 → 内置 Edge 音色（无映射返回空串）。"""
    return EDGE_VOICE_BY_LANG.get(
        str(lang or "").strip().lower().split("-")[0], "")


def edge_voice_lang_prefix(voice_id: str) -> str:
    """Edge 音色 ID → BCP47 语言前缀（``ja-JP-NanamiNeural`` → ``ja``）。

    非 BCP47 形态（克隆 speaker/自定义名）返回空串=无法判断。
    """
    v = str(voice_id or "").strip()
    if not v or "-" not in v:
        return ""
    head = v.split("-", 1)[0].lower()
    # BCP47 primary subtag 为 2-3 位字母（fil 等三位也合法）
    if 2 <= len(head) <= 3 and head.isalpha():
        return head
    return ""


def _is_multilingual_voice(voice_id: str) -> bool:
    """Multilingual 系 Edge 音色自适应文本语种，任何语言都无需换声。"""
    return "multilingual" in str(voice_id or "").lower()


def _effective_backend_of(voice_cfg: Dict[str, Any]) -> str:
    """与 TTSPipeline._effective_backend 同口径：voice_profile 生效则其 backend 优先。"""
    cfg = voice_cfg or {}
    vp = cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
    base = str(cfg.get("backend") or "edge_tts").strip().lower()
    if vp and bool(vp.get("enabled", False)):
        return str(vp.get("backend") or base).strip().lower()
    return base


def _effective_voice_of(voice_cfg: Dict[str, Any]) -> str:
    """与 TTSPipeline._effective_voice 同口径：voice_profile 生效则 speaker_id 优先。"""
    cfg = voice_cfg or {}
    vp = cfg.get("voice_profile") if isinstance(cfg.get("voice_profile"), dict) else {}
    if vp and bool(vp.get("enabled", False)):
        return str(vp.get("speaker_id") or cfg.get("voice") or "").strip()
    return str(cfg.get("voice") or "").strip()


def detect_text_lang(text: str) -> str:
    """确定性检测文本语种（复用全局 detect_language；异常/空 → ``unknown``）。

    短文本护栏：内容量不足（CJK < 2 且拉丁字母 < 4，如 "OK"/"？"）→ ``unknown``，
    防止把中文会话里的一句 "OK" 误路由成英文音色。
    """
    t = str(text or "").strip()
    if not t:
        return "unknown"
    cjk = len(_CJK_RE.findall(t))
    letters = len(_LATIN_LETTER_RE.findall(t))
    if cjk < 2 and letters < 4:
        return "unknown"
    try:
        from src.ai.translation_service import detect_language
        lang = (detect_language(t) or "").strip().lower()
    except Exception:
        return "unknown"
    return lang or "unknown"


def _route_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (config or {}).get("voice_lang_route") or {}


def _follow_cfg(rc: Dict[str, Any]) -> Dict[str, Any]:
    ft = rc.get("follow_text")
    return dict(ft) if isinstance(ft, dict) else {}


def _mapped_voice(follow: Dict[str, Any], lang: str) -> str:
    """语种 → 音色：配置覆写（follow_text.voices）优先，内置映射兜底。"""
    prefix = str(lang or "").strip().lower().split("-")[0]
    overrides = follow.get("voices")
    if isinstance(overrides, dict):
        v = overrides.get(prefix) or overrides.get(lang)
        if v:
            return str(v).strip()
    return default_edge_voice_for_lang(prefix)


def _route_cantonese(
    voice_cfg: Dict[str, Any], text: str, rc: Dict[str, Any],
) -> Optional[Tuple[Dict[str, Any], str]]:
    """既有粤语路由（最高优先）。未命中返回 None。"""
    yue = rc.get("cantonese") or {}
    if not yue.get("enabled", True):
        return None
    min_markers = int(yue.get("min_markers", 2) or 2)
    if not is_cantonese_text(text, min_markers=min_markers):
        return None
    new_cfg = dict(voice_cfg or {})
    new_cfg["backend"] = str(yue.get("backend") or "edge_tts")
    new_cfg["voice"] = str(yue.get("voice") or "zh-HK-HiuMaanNeural")
    # 粤语音色链自身的兜底也用同音色（避免回落回普通话音色）
    new_cfg["fallback_voice"] = new_cfg["voice"]
    # RVC 变声是普通话克隆链的附件，粤语路由下关掉防串味
    new_cfg.pop("rvc", None)
    # 人设克隆声必须一并停用：TTSPipeline._effective_backend 里
    # voice_profile.enabled=true 时 voice_profile.backend 优先于顶层 backend，
    # 不清掉会把本次路由静默盖回克隆链（普通话腔读粤语，路由形同虚设）。
    new_cfg["voice_profile"] = {"enabled": False}
    logger.info(
        "[lang_voice_route] 粤语文本 → 切 %s (%s) markers>=%d",
        new_cfg["backend"], new_cfg["voice"], min_markers)
    return new_cfg, "yue"


def _route_follow_text(
    voice_cfg: Dict[str, Any], text: str, rc: Dict[str, Any],
) -> Optional[Tuple[Dict[str, Any], str]]:
    """通用「音色跟随文本语种」路由。未命中/不适用返回 None。"""
    follow = _follow_cfg(rc)
    if not follow.get("enabled", True):
        return None
    lang = detect_text_lang(text)
    if not lang or lang == "unknown":
        return None
    prefix = lang.split("-")[0]
    backend = _effective_backend_of(voice_cfg)

    # 克隆链：原生跟随文本语种，不动主后端；只把 edge 兜底音色对齐语种，
    # 防克隆失败时用错语言音色兜底（默认兜底 zh 声念英文同样是错的）。
    if backend in _CLONE_BACKENDS:
        mapped = _mapped_voice(follow, prefix)
        if mapped:
            cur_fb = str(voice_cfg.get("fallback_voice") or "").strip()
            if edge_voice_lang_prefix(cur_fb) != prefix and not _is_multilingual_voice(cur_fb):
                new_cfg = dict(voice_cfg)
                new_cfg["fallback_voice"] = mapped
                logger.debug(
                    "[lang_voice_route] 克隆链兜底音色对齐语种 %s → %s",
                    prefix, mapped)
                # tag 留空：主后端未变，不影响 no_edge 语义与观测口径
                return new_cfg, ""
        return None

    # 非 edge 的公共后端（openai/elevenlabs/pyttsx3…）：多语自适应或非 BCP47
    # 音色体系，不路由。
    if backend != "edge_tts":
        return None

    cur_voice = _effective_voice_of(voice_cfg)
    if _is_multilingual_voice(cur_voice):
        return None
    cur_prefix = edge_voice_lang_prefix(cur_voice)
    if cur_prefix == prefix:
        return None

    mapped = _mapped_voice(follow, prefix)
    if not mapped:
        # 拒发守卫：语种明确但无音色可映射 → 宁缺毋滥，让调用方回落文字。
        if bool(follow.get("reject_unmapped", True)):
            logger.info(
                "[lang_voice_route] 文本语种 %s 无音色映射（当前 %s）→ 拒发语音回落文字",
                prefix, cur_voice or "-")
            return dict(voice_cfg or {}), f"{REJECT_TAG_PREFIX}{prefix}"
        return None

    new_cfg = dict(voice_cfg or {})
    new_cfg["backend"] = "edge_tts"
    new_cfg["voice"] = mapped
    new_cfg["fallback_voice"] = mapped
    new_cfg.pop("rvc", None)
    new_cfg["voice_profile"] = {"enabled": False}
    logger.info(
        "[lang_voice_route] 文本语种 %s ≠ 音色 %s → 切 edge_tts (%s)",
        prefix, cur_voice or "-", mapped)
    return new_cfg, prefix


def route_voice_cfg_for_text(
    voice_cfg: Dict[str, Any],
    text: str,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    """按回复文本语言改写本次合成的 voice_cfg。

    返回 ``(voice_cfg, route_tag)``：
    - 未命中路由 → 原样返回、tag 空串；
    - 粤语路由命中 → 改写副本（backend/voice 切粤语 TTS）、tag="yue"；
    - follow_text 命中 → 改写副本（edge 音色对齐文本语种）、tag=语种前缀（如 "en"）；
    - 拒发守卫命中 → tag="reject:<lang>"，调用方应放弃语音回落文字；
    - 克隆链只对齐 fallback_voice 时 → 返回改写副本、tag 空串（主后端未变）。
    绝不抛异常；任何异常按未命中处理。
    """
    try:
        rc = _route_cfg(config)
        if not rc.get("enabled", False):
            return voice_cfg, ""
        _record_stats("check")
        hit = _route_cantonese(voice_cfg, text, rc)
        if hit is None:
            hit = _route_follow_text(voice_cfg, text, rc)
        if hit is not None:
            _record_stats("hit", hit[1])
            return hit
        return voice_cfg, ""
    except Exception:
        logger.debug("[lang_voice_route] 路由异常，按未命中处理", exc_info=True)
        return voice_cfg, ""


def _record_stats(kind: str, tag: str = "") -> None:
    """路由观测埋点（lang_route_stats 单例；best-effort，绝不影响路由本身）。"""
    try:
        from src.ai.lang_route_stats import get_lang_route_stats
        st = get_lang_route_stats()
        if kind == "check":
            st.record_check()
        elif is_reject_tag(tag):
            st.record_rejected(tag[len(REJECT_TAG_PREFIX):])
        elif tag:
            st.record_routed(tag)
        else:
            st.record_fallback_aligned()
    except Exception:
        pass


def is_reject_tag(tag: str) -> bool:
    """route tag 是否为「语言不匹配拒发」守卫命中。"""
    return str(tag or "").startswith(REJECT_TAG_PREFIX)


__all__ = [
    "EDGE_VOICE_BY_LANG",
    "REJECT_TAG_PREFIX",
    "count_cantonese_markers",
    "default_edge_voice_for_lang",
    "detect_text_lang",
    "edge_voice_lang_prefix",
    "is_cantonese_text",
    "is_reject_tag",
    "route_voice_cfg_for_text",
]
