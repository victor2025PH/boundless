"""Edge 神经声目录（语种 × 性别）——预置声的单一事实源（L-2 #205，2026-09-06）。

两处消费，口径必须同源：

- **回落选声**（``tts_pipeline.safe_edge_voice`` / 兜底块）：预置态人设主后端失败
  时，兜底音色必须**同语种同性别**；目录里找不到同类 → 返回空串，调用方改发
  文字。事故实锤（ERS5QQ / V85TY9）：男人设 steven 克隆引擎离线，旧链回落
  ``zh-CN-XiaoxiaoNeural`` 女声直接发给了客户。
- **预置声选择器 / 保存校验**（人设工作室语音 tab、``persona_routes`` PUT）：
  用户按语言·性别筛选试听，不再手填 Voice ID；保存时 voice 必须在目录内，
  ``avatar_clone + 预置音色名`` 之类的非法组合直接拒绝。

每个语种第一条＝该语种的缺省声（与 ``lang_voice_route.EDGE_VOICE_BY_LANG``
逐条对齐——语种闸改道 edge 标准声时性别未知仍落同一把声，行为不变）。
纯数据 + 纯函数，零 IO，可被任何层 import。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# (voice_id, lang, gender, 展示名)
_V: Tuple[Tuple[str, str, str, str], ...] = (
    # ── 中文 ──
    ("zh-CN-XiaoxiaoNeural", "zh", "female", "晓晓 · 普通话"),
    ("zh-CN-XiaoyiNeural", "zh", "female", "晓伊 · 普通话"),
    ("zh-CN-YunxiNeural", "zh", "male", "云希 · 普通话"),
    ("zh-CN-YunjianNeural", "zh", "male", "云健 · 普通话"),
    ("zh-CN-YunyangNeural", "zh", "male", "云扬 · 普通话"),
    ("zh-CN-YunxiaNeural", "zh", "male", "云夏 · 普通话（少年）"),
    ("zh-CN-liaoning-XiaobeiNeural", "zh", "female", "晓北 · 东北话"),
    ("zh-CN-shaanxi-XiaoniNeural", "zh", "female", "晓妮 · 陕西话"),
    ("zh-TW-HsiaoChenNeural", "zh", "female", "曉臻 · 台灣國語"),
    ("zh-TW-HsiaoYuNeural", "zh", "female", "曉雨 · 台灣國語"),
    ("zh-TW-YunJheNeural", "zh", "male", "雲哲 · 台灣國語"),
    ("zh-HK-HiuGaaiNeural", "yue", "female", "曉佳 · 粵語"),
    ("zh-HK-HiuMaanNeural", "yue", "female", "曉曼 · 粵語"),
    ("zh-HK-WanLungNeural", "yue", "male", "雲龍 · 粵語"),
    # ── English ──
    ("en-US-JennyNeural", "en", "female", "Jenny · US"),
    ("en-US-AriaNeural", "en", "female", "Aria · US"),
    ("en-US-MichelleNeural", "en", "female", "Michelle · US"),
    ("en-US-AvaMultilingualNeural", "en", "female", "Ava · US (multilingual)"),
    ("en-US-EmmaMultilingualNeural", "en", "female", "Emma · US (multilingual)"),
    ("en-US-GuyNeural", "en", "male", "Guy · US"),
    ("en-US-ChristopherNeural", "en", "male", "Christopher · US"),
    ("en-US-EricNeural", "en", "male", "Eric · US"),
    ("en-US-RogerNeural", "en", "male", "Roger · US"),
    ("en-US-SteffanNeural", "en", "male", "Steffan · US"),
    ("en-US-AndrewMultilingualNeural", "en", "male", "Andrew · US (multilingual)"),
    ("en-US-BrianMultilingualNeural", "en", "male", "Brian · US (multilingual)"),
    ("en-GB-SoniaNeural", "en", "female", "Sonia · UK"),
    ("en-GB-LibbyNeural", "en", "female", "Libby · UK"),
    ("en-GB-RyanNeural", "en", "male", "Ryan · UK"),
    ("en-GB-ThomasNeural", "en", "male", "Thomas · UK"),
    ("en-AU-NatashaNeural", "en", "female", "Natasha · AU"),
    ("en-AU-WilliamNeural", "en", "male", "William · AU"),
    # ── 日本語 ──
    ("ja-JP-NanamiNeural", "ja", "female", "七海 Nanami"),
    ("ja-JP-KeitaNeural", "ja", "male", "圭太 Keita"),
    # ── 한국어 ──
    ("ko-KR-SunHiNeural", "ko", "female", "선히 SunHi"),
    ("ko-KR-InJoonNeural", "ko", "male", "인준 InJoon"),
    # ── ไทย ──
    ("th-TH-PremwadeeNeural", "th", "female", "Premwadee"),
    ("th-TH-NiwatNeural", "th", "male", "Niwat"),
    # ── Tiếng Việt ──
    ("vi-VN-HoaiMyNeural", "vi", "female", "Hoài My"),
    ("vi-VN-NamMinhNeural", "vi", "male", "Nam Minh"),
    # ── Bahasa Indonesia / Melayu ──
    ("id-ID-GadisNeural", "id", "female", "Gadis"),
    ("id-ID-ArdiNeural", "id", "male", "Ardi"),
    ("ms-MY-YasminNeural", "ms", "female", "Yasmin"),
    ("ms-MY-OsmanNeural", "ms", "male", "Osman"),
    # ── Español ──
    ("es-ES-ElviraNeural", "es", "female", "Elvira · España"),
    ("es-ES-AlvaroNeural", "es", "male", "Álvaro · España"),
    ("es-MX-DaliaNeural", "es", "female", "Dalia · México"),
    ("es-MX-JorgeNeural", "es", "male", "Jorge · México"),
    # ── Français ──
    ("fr-FR-DeniseNeural", "fr", "female", "Denise"),
    ("fr-FR-EloiseNeural", "fr", "female", "Eloise"),
    ("fr-FR-HenriNeural", "fr", "male", "Henri"),
    ("fr-FR-RemyMultilingualNeural", "fr", "male", "Rémy (multilingual)"),
    # ── Deutsch ──
    ("de-DE-KatjaNeural", "de", "female", "Katja"),
    ("de-DE-AmalaNeural", "de", "female", "Amala"),
    ("de-DE-ConradNeural", "de", "male", "Conrad"),
    ("de-DE-KillianNeural", "de", "male", "Killian"),
    # ── Italiano ──
    ("it-IT-ElsaNeural", "it", "female", "Elsa"),
    ("it-IT-IsabellaNeural", "it", "female", "Isabella"),
    ("it-IT-DiegoNeural", "it", "male", "Diego"),
    ("it-IT-GiuseppeMultilingualNeural", "it", "male", "Giuseppe (multilingual)"),
    # ── Português ──
    ("pt-BR-FranciscaNeural", "pt", "female", "Francisca · Brasil"),
    ("pt-BR-ThalitaMultilingualNeural", "pt", "female", "Thalita · Brasil (multilingual)"),
    ("pt-BR-AntonioNeural", "pt", "male", "Antônio · Brasil"),
    ("pt-PT-RaquelNeural", "pt", "female", "Raquel · Portugal"),
    ("pt-PT-DuarteNeural", "pt", "male", "Duarte · Portugal"),
    # ── Русский ──
    ("ru-RU-SvetlanaNeural", "ru", "female", "Светлана"),
    ("ru-RU-DmitryNeural", "ru", "male", "Дмитрий"),
    # ── العربية ──
    ("ar-EG-SalmaNeural", "ar", "female", "سلمى · مصر"),
    ("ar-EG-ShakirNeural", "ar", "male", "شاكر · مصر"),
    ("ar-SA-ZariyahNeural", "ar", "female", "زارية · السعودية"),
    ("ar-SA-HamedNeural", "ar", "male", "حامد · السعودية"),
    # ── हिन्दी ──
    ("hi-IN-SwaraNeural", "hi", "female", "स्वरा Swara"),
    ("hi-IN-MadhurNeural", "hi", "male", "मधुर Madhur"),
    # ── Türkçe ──
    ("tr-TR-EmelNeural", "tr", "female", "Emel"),
    ("tr-TR-AhmetNeural", "tr", "male", "Ahmet"),
    # ── Filipino ──
    ("fil-PH-BlessicaNeural", "fil", "female", "Blessica"),
    ("fil-PH-AngeloNeural", "fil", "male", "Angelo"),
    # ── ខ្មែរ ──
    ("km-KH-SreymomNeural", "km", "female", "Sreymom"),
    ("km-KH-PisethNeural", "km", "male", "Piseth"),
    # ── עברית ──
    ("he-IL-HilaNeural", "he", "female", "הילה Hila"),
    ("he-IL-AvriNeural", "he", "male", "אברי Avri"),
    # ── Ελληνικά ──
    ("el-GR-AthinaNeural", "el", "female", "Αθηνά Athina"),
    ("el-GR-NestorasNeural", "el", "male", "Νέστορας Nestoras"),
    # ── Nederlands / Polski / Svenska / Українська ──
    ("nl-NL-ColetteNeural", "nl", "female", "Colette"),
    ("nl-NL-MaartenNeural", "nl", "male", "Maarten"),
    ("pl-PL-ZofiaNeural", "pl", "female", "Zofia"),
    ("pl-PL-MarekNeural", "pl", "male", "Marek"),
    ("sv-SE-SofieNeural", "sv", "female", "Sofie"),
    ("sv-SE-MattiasNeural", "sv", "male", "Mattias"),
    ("uk-UA-PolinaNeural", "uk", "female", "Поліна"),
    ("uk-UA-OstapNeural", "uk", "male", "Остап"),
)

# 语种前缀别名：detect_language / 人设 language 字段可能给出的变体 → 目录键
_LANG_ALIASES = {
    "tl": "fil", "zh-cn": "zh", "zh-tw": "zh", "zh-hk": "yue", "cmn": "zh",
    "jp": "ja", "kr": "ko", "cn": "zh",
}

# 性别别名（人设 gender 是自由文本：female/male/女/男/F/M/girl/boy…）
_GENDER_ALIASES = {
    "female": "female", "f": "female", "woman": "female", "girl": "female",
    "女": "female", "女性": "female", "女生": "female", "女士": "female",
    "male": "male", "m": "male", "man": "male", "boy": "male",
    "男": "male", "男性": "male", "男生": "male", "先生": "male",
}

_BY_ID: Dict[str, Dict[str, str]] = {
    vid: {"id": vid, "lang": lang, "gender": gender, "name": name}
    for vid, lang, gender, name in _V
}

# OpenAI TTS 预置音色（预置态 backend=openai 时的合法表；性别按官方示例听感标注）
OPENAI_VOICES: Dict[str, str] = {
    "alloy": "female", "ash": "male", "ballad": "male", "coral": "female",
    "echo": "male", "fable": "female", "nova": "female", "onyx": "male",
    "sage": "female", "shimmer": "female", "verse": "male",
}


def normalize_gender(raw: Any) -> str:
    """人设 ``gender`` 自由文本 → ``female`` / ``male`` / ``""``（判不出不猜）。"""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    return _GENDER_ALIASES.get(s, "")


def normalize_lang(raw: Any) -> str:
    """BCP47 / 自由写法 → 目录语种键（``ja-JP`` → ``ja``；判不出返回空串）。"""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    if s in _LANG_ALIASES:
        return _LANG_ALIASES[s]
    head = s.split("-")[0].split("_")[0]
    return _LANG_ALIASES.get(head, head)


def voice_meta(voice_id: Any) -> Optional[Dict[str, str]]:
    """目录条目（``{id, lang, gender, name}``）；不在目录返回 None。"""
    return _BY_ID.get(str(voice_id or "").strip())


def is_known_edge_voice(voice_id: Any) -> bool:
    return voice_meta(voice_id) is not None


def edge_voice_lang(voice_id: Any) -> str:
    """Edge 音色 ID → 目录语种键；目录外的合法形态按 BCP47 前缀推（``xx-YY-…``）。"""
    m = voice_meta(voice_id)
    if m:
        return m["lang"]
    v = str(voice_id or "").strip()
    if "-" in v:
        head = v.split("-", 1)[0].lower()
        if 2 <= len(head) <= 3 and head.isalpha():
            return normalize_lang(head)
    return ""


def voices_for(lang: Any = "", gender: Any = "") -> List[Dict[str, str]]:
    """按语种 / 性别筛目录（空＝不筛），保持目录顺序。"""
    lg = normalize_lang(lang)
    gd = normalize_gender(gender) or str(gender or "").strip().lower()
    out: List[Dict[str, str]] = []
    for vid, vlang, vgender, name in _V:
        if lg and vlang != lg:
            continue
        if gd and vgender != gd:
            continue
        out.append(_BY_ID[vid])
    return out


def pick_edge_voice(lang: Any, gender: Any = "") -> str:
    """同语种（同性别）兜底音色；性别未知取该语种缺省声；**找不到返回空串**。

    ``lang`` 空 → 不猜语种，直接空串（调用方按自身默认处理）。
    """
    lg = normalize_lang(lang)
    if not lg:
        return ""
    gd = normalize_gender(gender)
    for vid, vlang, vgender, _name in _V:
        if vlang != lg:
            continue
        if gd and vgender != gd:
            continue
        return vid
    return ""


def languages() -> List[Tuple[str, str]]:
    """目录里出现过的语种键（去重、保序）+ 中文展示名。"""
    seen: List[str] = []
    for _vid, lang, _g, _n in _V:
        if lang not in seen:
            seen.append(lang)
    return [(lg, LANG_LABELS.get(lg, lg)) for lg in seen]


LANG_LABELS: Dict[str, str] = {
    "zh": "中文", "yue": "粤语", "en": "English", "ja": "日本語", "ko": "한국어",
    "th": "ไทย", "vi": "Tiếng Việt", "id": "Bahasa Indonesia", "ms": "Bahasa Melayu",
    "es": "Español", "fr": "Français", "de": "Deutsch", "it": "Italiano",
    "pt": "Português", "ru": "Русский", "ar": "العربية", "hi": "हिन्दी",
    "tr": "Türkçe", "fil": "Filipino", "km": "ខ្មែរ", "he": "עברית", "el": "Ελληνικά",
    "nl": "Nederlands", "pl": "Polski", "sv": "Svenska", "uk": "Українська",
}


def catalog_payload() -> Dict[str, Any]:
    """给前端选择器的整包（语种表 + 全部音色 + openai 表）。"""
    return {
        "languages": [{"code": c, "label": l} for c, l in languages()],
        "edge_voices": [dict(_BY_ID[vid]) for vid, *_ in _V],
        "openai_voices": [{"id": k, "gender": g} for k, g in OPENAI_VOICES.items()],
    }


__all__ = [
    "LANG_LABELS", "OPENAI_VOICES", "catalog_payload", "edge_voice_lang",
    "is_known_edge_voice", "languages", "normalize_gender", "normalize_lang",
    "pick_edge_voice", "voice_meta", "voices_for",
]
