"""语言能力矩阵单点（Q-21 A，#290，2026-09-12）。

ZDPNMT / R33MZW / N8G3JZ 三单 1.0.82 复验仍报「下拉没有粤语」：回复工坊 10 语、
cp-xlate-tools 15 语、顶栏译文 34 语、人设语言 4 语——四处各写一份短表，粤语 / 繁体只在
其中一处出现。本模块是**唯一**语言目录：34 码（= Hunyuan-MT 模型卡，含 ``yue`` / ``zh-tw``）
+ 中 / 英 / 繁显示名 + 本族自称 + 能力位（可起草 / 可翻译 / 可 TTS 克隆）。

五处消费方只读它：
- 回复工坊（``cp-draft.js``）与翻译工具（``cp-xlate-tools.js``）经 ``GET /api/lang-catalog``；
- 人设语言（``personas.html`` ``#p-language``）经模板注入 ``lang_catalog``；
- 顶栏译文 / 对话翻译（``unified_inbox.html`` ``_XL_CATALOG`` 字面量）经门禁
  ``tests/test_lang_catalog_gate.py`` 钉死同一份码表（模板热更、字面量不能带 CJK，故不注入）；
- 后端 ``translation_engines.HYMT_TARGET_LANGS`` / 路由 ``_AGENT_LANG_ALLOWED`` 直接引用 ``CODES``。

纯数据 + 纯函数、零外部依赖（``src.ai`` 导入它，它不得反向导入任何业务模块）。
能力位语义：
- ``draft``     人设产线能用该语种起草（LLM 全覆盖 → 全 True；留位给将来按模型收窄）
- ``translate`` 出站 / 入站翻译目标（= Hunyuan-MT 34 码；集外由下游引擎兜底）
- ``tts_clone`` 声音克隆 TTS 已验证语种（IndexTTS-2 / fish-speech 官方支持集；**只是展示位**，
  Q-22 才动 TTS 链——本批只把 ``tts_lang`` 透传）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

__all__ = [
    "LangEntry", "LANG_CATALOG", "CODES", "ordered_codes", "get", "has",
    "normalize", "display_name", "catalog_payload", "codes_with", "CATALOG_VERSION",
]

#: 目录版本（前端缓存键 / 门禁对账用；码表变更时递增）
CATALOG_VERSION = "2026-09-12.q21"


@dataclass(frozen=True)
class LangEntry:
    code: str
    zh: str          # 简体显示名
    en: str          # 英文显示名
    hant: str        # 繁体显示名
    endonym: str     # 本族自称（选语页副行；与 unified_inbox _XL_CATALOG 的 e: 同源）
    draft: bool = True
    translate: bool = True
    tts_clone: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "code": self.code, "zh": self.zh, "en": self.en, "hant": self.hant,
            "endonym": self.endonym,
            "caps": {"draft": self.draft, "translate": self.translate, "tts_clone": self.tts_clone},
        }


# 声音克隆 TTS 已验证语种（IndexTTS-2：zh/en；fish-speech：zh/en/ja/ko/fr/de/es/ar/ru/nl/it/pl/pt）。
# 繁体与简体同一套 TTS 前端；粤语 **不在**已验证集（Q-22 决定）。
_TTS_CLONE_OK = frozenset({
    "zh", "zh-tw", "en", "ja", "ko", "fr", "de", "es", "ar", "ru", "nl", "it", "pl", "pt",
})


def _e(code: str, zh: str, en: str, hant: str, endonym: str) -> LangEntry:
    return LangEntry(code=code, zh=zh, en=en, hant=hant, endonym=endonym,
                     draft=True, translate=True, tts_clone=(code in _TTS_CLONE_OK))


# 顺序即 UI 顺序（与 unified_inbox.html _XL_CATALOG 一致：中文族 → 东亚 → 东南亚 → 欧洲 → 中东/南亚）。
LANG_CATALOG: Tuple[LangEntry, ...] = (
    _e("zh", "中文", "Chinese", "簡體中文", "中文"),
    _e("zh-tw", "繁体中文", "Traditional Chinese", "繁體中文", "繁體中文"),
    _e("yue", "粤语", "Cantonese", "粵語", "粵語"),
    _e("en", "英语", "English", "英語", "English"),
    _e("ja", "日语", "Japanese", "日語", "日本語"),
    _e("ko", "韩语", "Korean", "韓語", "한국어"),
    _e("th", "泰语", "Thai", "泰語", "ไทย"),
    _e("vi", "越南语", "Vietnamese", "越南語", "Tiếng Việt"),
    _e("id", "印尼语", "Indonesian", "印尼語", "Bahasa Indonesia"),
    _e("ms", "马来语", "Malay", "馬來語", "Melayu"),
    _e("tl", "他加禄语", "Tagalog", "他加祿語", "Tagalog"),
    _e("ru", "俄语", "Russian", "俄語", "Русский"),
    _e("es", "西班牙语", "Spanish", "西班牙語", "Español"),
    _e("pt", "葡萄牙语", "Portuguese", "葡萄牙語", "Português"),
    _e("fr", "法语", "French", "法語", "Français"),
    _e("de", "德语", "German", "德語", "Deutsch"),
    _e("it", "意大利语", "Italian", "意大利語", "Italiano"),
    _e("tr", "土耳其语", "Turkish", "土耳其語", "Türkçe"),
    _e("ar", "阿拉伯语", "Arabic", "阿拉伯語", "العربية"),
    _e("hi", "印地语", "Hindi", "印地語", "हिन्दी"),
    _e("pl", "波兰语", "Polish", "波蘭語", "Polski"),
    _e("cs", "捷克语", "Czech", "捷克語", "Čeština"),
    _e("nl", "荷兰语", "Dutch", "荷蘭語", "Nederlands"),
    _e("uk", "乌克兰语", "Ukrainian", "烏克蘭語", "Українська"),
    _e("fa", "波斯语", "Persian", "波斯語", "فارسی"),
    _e("he", "希伯来语", "Hebrew", "希伯來語", "עברית"),
    _e("km", "高棉语", "Khmer", "高棉語", "ខ្មែរ"),
    _e("my", "缅甸语", "Burmese", "緬甸語", "မြန်မာ"),
    _e("bn", "孟加拉语", "Bengali", "孟加拉語", "বাংলা"),
    _e("ta", "泰米尔语", "Tamil", "泰米爾語", "தமிழ்"),
    _e("te", "泰卢固语", "Telugu", "泰盧固語", "తెలుగు"),
    _e("mr", "马拉地语", "Marathi", "馬拉地語", "मराठी"),
    _e("gu", "古吉拉特语", "Gujarati", "古吉拉特語", "ગુજરાતી"),
    _e("ur", "乌尔都语", "Urdu", "烏爾都語", "اردو"),
)

_BY_CODE: Dict[str, LangEntry] = {e.code: e for e in LANG_CATALOG}
CODES: FrozenSet[str] = frozenset(_BY_CODE)
assert len(LANG_CATALOG) == 34 and len(CODES) == 34, "lang_catalog 必须恰为 34 码且无重复"

# 常见别名 → 目录码（与 unified_inbox._canonXlLang / translation_service.normalize_lang 同精神，
# 只收目录相关的一小撮；不做全量 BCP-47 解析）。
_ALIASES: Dict[str, str] = {
    "zh-cn": "zh", "zh-hans": "zh", "zh_cn": "zh", "zh-sg": "zh", "cn": "zh", "chinese": "zh",
    "zh-hant": "zh-tw", "zh_tw": "zh-tw", "zh-hk": "zh-tw", "zh-mo": "zh-tw", "zh_hant": "zh-tw",
    "zh-yue": "yue", "yue-hk": "yue", "cantonese": "yue",
    "fil": "tl", "filipino": "tl",
    "jp": "ja", "kr": "ko", "ua": "uk", "iw": "he", "in": "id",
}


def ordered_codes() -> List[str]:
    return [e.code for e in LANG_CATALOG]


def get(code: str) -> Optional[LangEntry]:
    return _BY_CODE.get(normalize(code))


def has(code: str) -> bool:
    return normalize(code) in _BY_CODE


def normalize(code: str) -> str:
    """归一到目录码：小写、下划线→连字符、别名表、``en-US``→``en``。目录外原样（小写）返回。"""
    c = str(code or "").strip().lower().replace("_", "-")
    if not c:
        return ""
    if c in _BY_CODE:
        return c
    if c in _ALIASES:
        return _ALIASES[c]
    base = c.split("-", 1)[0]
    if base in _BY_CODE:
        return base
    return c


def display_name(code: str, ui_lang: str = "zh") -> str:
    """按 UI 语言取显示名（zh / en / zh-hant|hant|zh-tw）；目录外回落原码。"""
    e = get(code)
    if e is None:
        return str(code or "")
    ul = str(ui_lang or "zh").lower()
    if ul in ("hant", "zh-hant", "zh-tw", "zh_hant", "zh-hk"):
        return e.hant
    if ul.startswith("en"):
        return e.en
    return e.zh


def codes_with(cap: str) -> List[str]:
    """具备某能力位（draft / translate / tts_clone）的目录码，按目录顺序。"""
    return [e.code for e in LANG_CATALOG if bool(getattr(e, cap, False))]


def catalog_payload(ui_lang: str = "zh", only: Optional[Iterable[str]] = None) -> Dict[str, object]:
    """``/api/lang-catalog`` 响应体 / 模板注入用：``{ok, version, ui_lang, langs:[{code,name,zh,en,hant,endonym,caps}]}``。

    ``name`` 已按 ``ui_lang`` 解析，前端零判断直接渲染；``only`` 可按能力位过滤
    （如 ``codes_with("tts_clone")``）。
    """
    allow = set(only) if only is not None else None
    langs: List[Dict[str, object]] = []
    for e in LANG_CATALOG:
        if allow is not None and e.code not in allow:
            continue
        d = e.as_dict()
        d["name"] = display_name(e.code, ui_lang)
        langs.append(d)
    return {"ok": True, "version": CATALOG_VERSION, "ui_lang": str(ui_lang or "zh"), "langs": langs}
