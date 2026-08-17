# -*- coding: utf-8 -*-
"""本地 MT 模型的「模型契约」档案（纯函数，零 IO）。

`OllamaMTEngine` 名字是通用的（「本地 Ollama 上的专用机器翻译模型引擎」），
但一个 MT 模型的契约远不止 base_url+model 两个字段——它还包括：

1. **prompt 格式**：Hunyuan-MT 是指令式（「把下面的文本翻译成X，不要额外解释」），
   MiLMMT-46 是补全式（`Translate this from A to B:\\nA: 原文\\nB:` 让模型续写）；
2. **调用模式**：指令式走 chat.completions（套模型的 chat template），补全式官方
   用法是裸 completions（不套 template）——喂错模式等于喂错训练分布；
3. **语种集**：HY-MT 33 语 vs MiLMMT 46 语，且**两者不是包含关系**
   （MiLMMT 多 az/bg/ca/da/el/fi/hr/hu/kk/lo/nb/ro/sk/sl/sv/uz/zht，
   少 te/mr/gu/uk）——白名单跟着模型走，不跟着类走；
4. **语种命名**：同一个 `zh` 在 HY prompt 里叫「中文」，在 MiLMMT 里必须叫
   `Chinese (Simplified)`（它把简繁当两个语种训练）。

把这四项焊死在引擎类里的后果不是报错，而是**静默的假结论**：换上覆盖更广的模型，
17 个新语种在模型被调用前就被旧白名单拒掉（零覆盖收益、零错误日志），而质量横比
则因为喂了错格式的 prompt 让新模型无端输掉——「测过了，不如现在的」。

默认 profile ``hunyuan_mt`` 与改造前的行为**逐字节一致**，换档才需要显式配置。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Tuple

logger = logging.getLogger(__name__)

DEFAULT_PROFILE = "hunyuan_mt"


# ── Hunyuan-MT（现行生产模型）────────────────────────────────────────────
# 官方模型卡覆盖的语种（映射到本项目语种码）。命中集内 supports_target=True，
# 集外交给下游引擎（ai/deepl/google）——即便置信度切换关着，冷门语种也不会被硬吃。
HYMT_LANGS: FrozenSet[str] = frozenset({
    "zh", "yue", "en", "ja", "ko", "fr", "es", "it", "pt", "de", "tr", "ru",
    "ar", "th", "id", "ms", "vi", "tl", "hi", "pl", "cs", "nl", "km", "my",
    "fa", "he", "bn", "ta", "te", "mr", "gu", "ur", "uk",
})

# zh 相关语种对的中文指令名（Hunyuan-MT 官方 zh<=>xx prompt 用中文语种名）。
HYMT_ZH_NAME: Dict[str, str] = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语",
    "es": "西班牙语", "it": "意大利语", "pt": "葡萄牙语", "de": "德语",
    "tr": "土耳其语", "ru": "俄语", "ar": "阿拉伯语", "th": "泰语",
    "id": "印尼语", "ms": "马来语", "vi": "越南语", "tl": "菲律宾语",
    "hi": "印地语", "pl": "波兰语", "nl": "荷兰语", "km": "高棉语",
    "yue": "粤语", "he": "希伯来语", "uk": "乌克兰语",
}


def _hymt_prompt(text: str, src: str, tgt: str) -> str:
    if src in ("zh", "yue") or tgt in ("zh", "yue"):
        name = HYMT_ZH_NAME.get(tgt, tgt)
        return f"把下面的文本翻译成{name}，不要额外解释。\n\n{text}"
    from src.ai.translation_service import LANG_NAMES

    name = LANG_NAMES.get(tgt, tgt)
    return (
        f"Translate the following segment into {name}, "
        f"without additional explanation.\n\n{text}"
    )


# ── MiLMMT-46（小米 Gemma3 系多语 MT）────────────────────────────────────
# 语种名严格照抄官方模型卡（prompt 里的语种名是训练时的字面量，不能自己意译）。
# 注意 zh/zht 被当两个语种训练——简繁分开是 MiLMMT 相对 HY-MT 的能力，不是笔误。
MILMMT_NAME: Dict[str, str] = {
    "ar": "Arabic", "az": "Azerbaijani", "bg": "Bulgarian", "bn": "Bengali",
    "ca": "Catalan", "cs": "Czech", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "fa": "Persian",
    "fi": "Finnish", "fr": "French", "he": "Hebrew", "hi": "Hindi",
    "hr": "Croatian", "hu": "Hungarian", "id": "Indonesian", "it": "Italian",
    "ja": "Japanese", "kk": "Kazakh", "km": "Khmer", "ko": "Korean",
    "lo": "Lao", "ms": "Malay", "my": "Burmese", "nb": "Norwegian",
    "nl": "Dutch", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
    "ru": "Russian", "sk": "Slovak", "sl": "Slovenian", "sv": "Swedish",
    "ta": "Tamil", "th": "Thai", "tl": "Tagalog", "tr": "Turkish",
    "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese", "yue": "Cantonese",
    "zh": "Chinese (Simplified)", "zht": "Chinese (Traditional)",
}
MILMMT_LANGS: FrozenSet[str] = frozenset(MILMMT_NAME)

# 本项目常见的等价码 → MiLMMT 码（no/nn 都归挪威语；zh-tw/zh-hant 归繁体）。
_MILMMT_ALIAS: Dict[str, str] = {
    "no": "nb", "nn": "nb",
    "zh-tw": "zht", "zh_tw": "zht", "zh-hant": "zht", "zh_hant": "zht",
    "zh-cn": "zh", "zh_cn": "zh", "zh-hans": "zh", "zh_hans": "zh",
    "fil": "tl", "iw": "he", "in": "id",
}


def _milmmt_canon(code: str) -> str:
    c = str(code or "").strip().lower().replace("_", "-")
    return _MILMMT_ALIAS.get(c, c)


def _milmmt_prompt(text: str, src: str, tgt: str) -> str:
    """官方补全式模板：末行以 `<目标语名>:` 结尾，由模型续写译文。"""
    s = MILMMT_NAME.get(_milmmt_canon(src), "")
    t = MILMMT_NAME.get(_milmmt_canon(tgt), _milmmt_canon(tgt))
    if not s:
        # 源语未知时不敢编一个语种名（会把模型引到错误的语言先验）：
        # 退化为「只声明目标语」的最小变体，仍保留官方的续写结构。
        return f"Translate this to {t}:\n{text}\n{t}:"
    return f"Translate this from {s} to {t}:\n{s}: {text}\n{t}:"


def _milmmt_post(out: str, tgt: str) -> str:
    """补全式模型偶尔会把 `English:` 这类锚点连同译文一起吐出来，剥掉。"""
    s = str(out or "").strip()
    name = MILMMT_NAME.get(_milmmt_canon(tgt), "")
    for prefix in ([f"{name}:"] if name else []):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].lstrip()
    return s


def _identity_post(out: str, tgt: str) -> str:  # noqa: ARG001
    return str(out or "")


@dataclass(frozen=True)
class MTProfile:
    """一个本地 MT 模型的完整调用契约。"""

    name: str
    langs: FrozenSet[str]
    build_prompt: Callable[[str, str, str], str]
    #: "chat" = chat.completions（套模型 chat template）；
    #: "completion" = completions（裸续写，补全式模型的官方用法）
    mode: str = "chat"
    postprocess: Callable[[str, str], str] = _identity_post
    stop: Tuple[str, ...] = field(default_factory=tuple)

    def supports(self, target_lang: str) -> bool:
        code = str(target_lang or "").strip().lower()
        if self.name == "milmmt46":
            code = _milmmt_canon(code)
        return code in self.langs


PROFILES: Dict[str, MTProfile] = {
    "hunyuan_mt": MTProfile(
        name="hunyuan_mt",
        langs=HYMT_LANGS,
        build_prompt=_hymt_prompt,
        mode="chat",
    ),
    "milmmt46": MTProfile(
        name="milmmt46",
        langs=MILMMT_LANGS,
        build_prompt=_milmmt_prompt,
        mode="completion",
        postprocess=_milmmt_post,
    ),
}
# 便于配置里写型号名而不是内部档名。
_ALIASES = {
    "hymt": "hunyuan_mt", "hunyuan": "hunyuan_mt", "hy-mt": "hunyuan_mt",
    "milmmt": "milmmt46", "milmmt-46": "milmmt46", "milmmt46-v0.1": "milmmt46",
}


def get_profile(name: object) -> MTProfile:
    """按名取档；未知名回落默认档并告警（配置笔误不该让出站翻译整条崩）。"""
    if isinstance(name, MTProfile):
        return name
    key = str(name or "").strip().lower().replace(" ", "")
    if not key:
        return PROFILES[DEFAULT_PROFILE]
    key = _ALIASES.get(key, key)
    prof = PROFILES.get(key)
    if prof is None:
        logger.warning(
            "[mt_profiles] 未知 MT 档案 %r，回落 %s（可选：%s）",
            name, DEFAULT_PROFILE, ", ".join(sorted(PROFILES)),
        )
        return PROFILES[DEFAULT_PROFILE]
    return prof


def guess_profile(model: str) -> str:
    """按 Ollama 模型名猜档（仅供 CLI 便利，生产一律显式配置）。"""
    m = str(model or "").lower()
    if "milmmt" in m or "mimt" in m:
        return "milmmt46"
    return DEFAULT_PROFILE
