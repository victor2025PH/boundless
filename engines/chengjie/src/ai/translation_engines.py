"""P56：多翻译引擎抽象 + 故障转移路由 + 品牌词保护。

设计目标（对标云译「6 引擎可切」并超越）：
- 引擎统一接口 `TranslationEngine`，新增引擎=加一个类，不改路由/服务。
- `EngineRouter` 按配置顺序尝试，第一个「可用且返回非空」的引擎获胜，
  并把获胜引擎名带回（落库到翻译记忆的 engine 列 + 前端徽标）。
- 任一引擎不可用/失败自动降级到下一个（默认 AI 引擎兜底）。
- 品牌词/产品名「不译保护」：mask→翻译→restore，所有引擎统一生效
  （云译只翻不保护，这里是差异化）。

所有第三方引擎（DeepL/Google）都是**可选**：缺 api_key 或缺 aiohttp 时
`available=False`，路由自动跳过——本地/测试零外部依赖。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.i18n.lang_catalog import CODES as _LANG_CATALOG_CODES

logger = logging.getLogger(__name__)

# 占位符：CJK 全角方括号 + 序号，普通翻译引擎一般原样保留；restore 时容错匹配。
_PH_RE = re.compile(r"\u3014\s*(\d+)\s*\u3015")  # 〔N〕
# 容错：部分引擎（尤其 MT 模型译向拉丁语时，实测 Hunyuan-MT zh→en）会把全角〔N〕
# 规范化为 ASCII [N] 或 【N】。仅当序号在 mapping 里才还原，绝不误伤正文里的 [2] 等。
_PH_ASCII_RE = re.compile(r"[\[\u3010]\s*(\d+)\s*[\]\u3011]")  # [N] / 【N】
_WORD_CH = re.compile(r"[A-Za-z0-9]")


def _smart_term(before: str, term: str, after: str) -> str:
    """还原术语时的智能空格：引擎常把占位符两侧空格吞掉（"using〔0〕"），

    直接替换会粘连成 "usingLINE Pay"。仅当占位符紧邻字符与术语端字符**都是拉丁
    词字符**时补一个空格；CJK 语境（"用LINE Pay付款"）不匹配词字符，保持无空格。
    """
    if not term:
        return term
    pre = " " if (before and _WORD_CH.match(before) and _WORD_CH.match(term[0])) else ""
    post = " " if (after and _WORD_CH.match(after) and _WORD_CH.match(term[-1])) else ""
    return f"{pre}{term}{post}"


def _replace_ph_spaced(text: str, ph: str, term: str) -> str:
    """逐处替换占位符，每处按邻字决定是否补空格。"""
    out: List[str] = []
    i = 0
    while True:
        j = text.find(ph, i)
        if j < 0:
            out.append(text[i:])
            break
        k = j + len(ph)
        before = text[j - 1] if j > 0 else ""
        after = text[k] if k < len(text) else ""
        out.append(text[i:j])
        out.append(_smart_term(before, term, after))
        i = k
    return "".join(out)


@dataclass
class EngineResult:
    text: str
    engine: str
    ok: bool = True
    error: str = ""


# ── P1-XT（2026-08-16）语气控制 ─────────────────────────────────────────────
# style 词汇表扩展：chat（默认口语）之外新增三档语气，仅 LLM 线（ai/custom）与
# DeepL（formality 参数）消费；NMT 线（ollama_mt/google/microsoft/youdao/baidu）
# 天然忽略——supports_tone 能力位经 describe() 下发，前端据此提示。
# TM 缓存键本就含 style 维度（translation_service._cache_key），不同语气分桶不串味。
_TONE_INSTRUCTIONS = {
    "formal": "Use a polite, professional, business-appropriate tone.",
    "friendly": "Use a warm, casual, friendly tone with natural conversational wording.",
    "sales": "Use a persuasive, upbeat sales tone that stays natural and never pushy.",
}

# DeepL formality：prefer_* 变体在不支持 formality 的目标语上优雅降级（不报错），
# 这是刻意选择——错语种硬传 more/less 会 400。
_DEEPL_FORMALITY = {"formal": "prefer_more", "friendly": "prefer_less", "sales": "prefer_less"}


def style_tone_instruction(style: str) -> str:
    """语气档 → 附加指令（纯函数）；chat/未知档返回空串=旧行为。"""
    return _TONE_INSTRUCTIONS.get(str(style or "").strip().lower(), "")


def deepl_formality(style: str) -> str:
    """语气档 → DeepL formality 参数值；无映射返回空串=不传该参数。"""
    return _DEEPL_FORMALITY.get(str(style or "").strip().lower(), "")


# ── 中文变体目标语的风格钉子（2026-08-29）────────────────────────────────────
# LLM 线（ai/custom）对 zh-tw/yue 的补充指令：光说 "Traditional Chinese" 模型偶尔
# 仍蹦简体字；粤语若不点名「口语粤文」会输出普通话式书面中文换皮。NMT 线不消费
# （HY-MT 中文 prompt 模板自带语种名，Qwen/Hunyuan 对「繁体中文/粤语」指令本就稳）。
_VARIANT_STYLE_HINTS = {
    "zh-tw": ("Output MUST use Traditional Chinese characters (繁體中文) only - "
              "never Simplified characters."),
    # 粤文惯用繁体字形（2026-08-30 实弹实锤：不点名会输出「你今晚有冇空？想请你
    # 食饭倾偈」这类简体粤文混排——口语词对了、字形穿帮）。
    "yue": ("Write in colloquial spoken Cantonese (地道粵語口語，用「嘅/唔/係/喺/"
            "咁/哋」等粵文字), in Traditional Chinese characters (粵文慣用繁體，"
            "如「請/飯/傾」不作「请/饭/倾」), NOT Mandarin-style written Chinese."),
}


def variant_style_hint(target_lang: str) -> str:
    """中文变体目标语 → 附加风格指令（纯函数）；其他语种返回空串=行为不变。"""
    return _VARIANT_STYLE_HINTS.get(str(target_lang or "").strip().lower(), "")


def build_chat_tone(style: str) -> str:
    """LLM 线共用的 tone 段（AIEngine 与 OpenAICompatEngine 单一口径）。

    chat 与三档语气同属「聊天家族」（保留意义/名字/数字/链接/emoji 的核心线 +
    可选语气附加）；词汇表外的 style 维持旧「Translate faithfully」语义。
    """
    s = str(style or "").strip().lower()
    extra = style_tone_instruction(s)
    if s == "chat" or extra:
        base = ("Keep the meaning, names, numbers, links, emojis and chat tone. "
                "Do not add explanations.")
        return f"{base} {extra}" if extra else base
    return "Translate faithfully. Do not add explanations."


# ── 译文语言完整性护栏（P0-198）────────────────────────────────────────────────
# LLM 引擎答非所译 / identity 回显的兜底防线：目标是 CJK 语种时译文必须含 CJK；
# 目标是拉丁语种时译文 CJK 占比不得过高（放行少量专名/emoji 场景）。
# 刻意不做「答问 vs 翻译」语义判定——去人设化的干净 system 已消除主要诱因，
# 这里只兜「输出语言明显不是目标语言」这个可确定性判据。
_CJK_ANY_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_CJK_TARGETS = {"zh", "ja", "ko", "yue", "zh-tw", "zh-hk", "zh-cn"}
_LATIN_CJK_MAX_RATIO = 0.2


def _output_lang_sane(text: str, target_lang: str) -> bool:
    """译文与目标语言的粗粒度一致性检查（纯函数，宁可放过不误杀）。"""
    t = str(text or "")
    if not t:
        return False
    base = str(target_lang or "").strip().lower()
    if not base:
        return True
    cjk_n = len(_CJK_ANY_RE.findall(t))
    if base in _CJK_TARGETS or base.split("-")[0] in ("zh", "ja", "ko"):
        return cjk_n > 0
    # 拉丁/其它语种目标：CJK 字符占比超阈值 = 没翻（identity）或答错了语言
    return (cjk_n / max(1, len(t))) <= _LATIN_CJK_MAX_RATIO


def mask_protected(text: str, protect: Optional[List[str]]) -> Tuple[str, Dict[str, str]]:
    """把受保护词替换为占位符〔N〕，返回(masked_text, {占位符: 原词})。

    最长词优先，避免「LINE Pay」被「LINE」截断。protect 为空则原样返回。
    """
    if not protect or not text:
        return text, {}
    mapping: Dict[str, str] = {}
    masked = text
    for i, term in enumerate(sorted({t for t in protect if t}, key=len, reverse=True)):
        if term not in masked:
            continue
        ph = f"\u3014{i}\u3015"
        masked = masked.replace(term, ph)
        mapping[ph] = term
    return masked, mapping


def apply_glossary_mask(
    text: str,
    terms: Optional[Dict[str, str]] = None,
    protect: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, str]]:
    """统一术语遮罩（P57）：让术语强制对**所有引擎**生效（含 DeepL/Google）。

    - protect 词 → 占位符 → 还原为**原词**（不译保护）。
    - terms（源词->偏好译法）→ 占位符 → 还原为**目标译法**（强制译法）。

    源词长度优先，避免「LINE Pay」被「LINE」截断。返回 (masked, {占位符: 还原值})。
    无术语/保护词则原样返回（默认行为不变）。
    """
    pairs: List[Tuple[str, str]] = []
    for t in (protect or []):
        if t:
            pairs.append((str(t), str(t)))          # 还原原词
    for src, tgt in (terms or {}).items():
        if src:
            pairs.append((str(src), str(tgt)))       # 还原目标译法
    if not text or not pairs:
        return text, {}
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    mapping: Dict[str, str] = {}
    masked = text
    idx = 0
    for src, repl in pairs:
        if src not in masked:
            continue
        ph = f"\u3014{idx}\u3015"
        masked = masked.replace(src, ph)
        mapping[ph] = repl
        idx += 1
    return masked, mapping


def restore_protected(text: str, mapping: Dict[str, str]) -> str:
    """还原占位符为原词；引擎若改动了占位符格式（如丢空格/转 ASCII 括号/吞边空格）
    也尽量容错还原。"""
    if not mapping or not text:
        return text
    out = text
    for ph, term in mapping.items():
        out = _replace_ph_spaced(out, ph, term)
    # 容错：〔 N 〕/〔N〕 残留 → 按序号映射回去
    def _sub(m: "re.Match") -> str:
        ph = f"\u3014{m.group(1)}\u3015"
        return mapping.get(ph, "")
    out = _PH_RE.sub(_sub, out)
    # 容错 2：引擎把全角括号规范化成 [N]/【N】→ 仅还原 mapping 中存在的序号，
    # 未知序号原样保留（可能是正文本身的方括号，如 markdown 脚注）；同样做智能补空格。
    def _sub_ascii(m: "re.Match") -> str:
        ph = f"\u3014{m.group(1)}\u3015"
        if ph not in mapping:
            return m.group(0)
        s = m.string
        before = s[m.start() - 1] if m.start() > 0 else ""
        after = s[m.end()] if m.end() < len(s) else ""
        return _smart_term(before, mapping[ph], after)
    out = _PH_ASCII_RE.sub(_sub_ascii, out)
    return out


class AIEngine:
    """默认引擎：走项目现有 ai_client.chat（LLM 翻译，可用 glossary 提示 + 语气）。"""

    name = "ai"
    label = "AI"
    supports_tone = True   # P1-XT：语气档（formal/friendly/sales）在本线生效

    def __init__(self, ai_client: Optional[Any]) -> None:
        self._ai = ai_client

    def supports_target(self, target_lang: str) -> bool:
        return True  # LLM 可处理任意目标语

    @property
    def available(self) -> bool:
        ai = self._ai
        if ai is None or not hasattr(ai, "chat"):
            return False
        # 真实 AIClient：底层 provider 客户端未就绪时 chat() 会回退到「兜底客服话术」，
        # 绝不能被当成译文。仅当能明确判断「未就绪」才标记不可用；无法判断
        # （如测试桩只有 chat 方法）则保持可用，行为与改造前一致。
        has_markers = (
            hasattr(ai, "client")
            or hasattr(ai, "_oa_client")
            or hasattr(ai, "_use_openai_compat")
        )
        if not has_markers:
            return True
        if getattr(ai, "_use_openai_compat", False):
            return getattr(ai, "_oa_client", None) is not None
        return getattr(ai, "client", None) is not None

    # 翻译专用 system（P0-198）：绝不带全局 system_prompt / 人设块。
    # 实锤事故（198，2026-07-31）：走完整聊天管线时，conversion 域人设把
    # 「So you married ?」按 bio 答成「离过婚，现在是单身」写进了译文槽。
    # B56（2026-08-23 `_297`）：追加专名/数字保真铁律——「see you in Cebu」被
    # 译成「在马尼拉见到你」＝地名被上下文合理化偷换，监督链失真。
    _BARE_TRANSLATION_SYSTEM = (
        "You are a machine translation engine. Translate the text the user "
        "provides. Output ONLY the translation - never answer questions, "
        "never role-play, never add explanations or comments. "
        "Proper nouns (place/person/brand names) and numbers must be "
        "preserved exactly - never substitute a different one; if unsure, "
        "keep the original word untranslated."
    )

    async def bare_chat(self, prompt: str) -> str:
        """干净翻译通道的裸 LLM 调用（无人设/无历史/跳语言守卫）。

        translate 与 P1-XF 融合器共用本入口——单一调用约定，别再复制这段。
        老签名桩（仅有 chat()）回落旧路径，行为与改造前一致。异常原样上抛，
        由调用方决定失败语义。
        """
        out: Any = None
        _gen = getattr(self._ai, "generate_reply", None)
        if callable(_gen):
            try:
                out = await _gen(
                    prompt,
                    context={
                        "_skip_lang_guard": True,
                        "_bare_system": self._BARE_TRANSLATION_SYSTEM,
                        # 成本归因（2026-09-08）：这条走 generate_reply 的翻译此前被记成
                        # customer_reply，「客户回复花了多少」因此虚高。
                        "_llm_purpose": "translate",
                    },
                    conversation_history=None,
                    _skip_quality_check=True,
                )
            except TypeError:
                out = None
        if out is None:
            try:
                out = await self._ai.chat(prompt, {"_skip_lang_guard": True})
            except TypeError:
                out = await self._ai.chat(prompt)
        return str(out or "")

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        # 延迟导入避免与 translation_service 的循环依赖
        from src.ai.translation_service import LANG_NAMES, _clean_translation

        source_name = LANG_NAMES.get(source_lang, source_lang)
        target_name = LANG_NAMES.get(target_lang, target_lang)
        tone = build_chat_tone(style)   # P1-XT：chat 家族 + 语气附加，单一口径
        variant = variant_style_hint(target_lang)   # zh-tw/yue 风格钉子（他语种空串）
        if variant:
            variant = f" {variant}"
        from src.ai.translation_fidelity import FIDELITY_PROMPT_RULE
        prompt = (
            f"Translate the following chat message from {source_name} to {target_name}. "
            f"{FIDELITY_PROMPT_RULE} "
            f"{tone}{variant}{glossary_hint}\n\n{text}"
        )
        try:
            out = await self.bare_chat(prompt)
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")
        cleaned = _clean_translation(str(out or ""))
        if not cleaned:
            return EngineResult("", self.name, False, "empty")
        if not _output_lang_sane(cleaned, target_lang):
            # 语言完整性护栏：目标 CJK 语种译文必须含 CJK；目标拉丁语种译文
            # CJK 占比不得过高（identity 回显 / 模型答非所译的兜底防线）。
            return EngineResult("", self.name, False, "target_lang_mismatch")
        return EngineResult(cleaned, self.name, True)


# DeepL 语种码（大写），仅列常用；缺失则不带 source_lang 让其自动检测。
# zh-tw → ZH-HANT（DeepL 2024 起支持繁体目标；source 侧无此码，仅目标用——
# translate() 里 source 查表 miss 即不带 source_lang，让 DeepL 自动检测，安全）。
_DEEPL_LANG = {
    "zh": "ZH", "zh-tw": "ZH-HANT", "en": "EN", "ja": "JA", "ko": "KO",
    "ru": "RU", "fr": "FR",
    "de": "DE", "es": "ES", "pt": "PT", "it": "IT", "id": "ID", "tr": "TR",
}


class DeepLEngine:
    """DeepL REST 引擎（可选）。缺 api_key 或缺 aiohttp → 不可用，路由自动跳过。"""

    name = "deepl"
    label = "DeepL"
    supports_tone = True   # P1-XT：经 formality 参数（prefer_* 变体，错语种优雅降级）

    def __init__(self, api_key: str = "", *, pro: bool = False, timeout: float = 8.0) -> None:
        self._key = str(api_key or "")
        self._url = (
            "https://api.deepl.com/v2/translate" if pro
            else "https://api-free.deepl.com/v2/translate"
        )
        self._timeout = float(timeout or 8.0)

    def supports_target(self, target_lang: str) -> bool:
        return str(target_lang or "") in _DEEPL_LANG

    @property
    def available(self) -> bool:
        if not self._key:
            return False
        try:
            import aiohttp  # noqa: F401
            return True
        except Exception:
            return False

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import aiohttp

        tgt = _DEEPL_LANG.get(target_lang)
        if not tgt:
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        data = {"text": text, "target_lang": tgt}
        src = _DEEPL_LANG.get(source_lang)
        if src:
            data["source_lang"] = src
        f = deepl_formality(style)   # P1-XT：语气档映射 formality（无映射不传参）
        if f:
            data["formality"] = f
        headers = {"Authorization": f"DeepL-Auth-Key {self._key}"}
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self._url, data=data, headers=headers) as resp:
                    if resp.status != 200:
                        return EngineResult("", self.name, False, f"http_{resp.status}")
                    j = await resp.json()
            out = ((j.get("translations") or [{}])[0] or {}).get("text", "")
            return EngineResult(out, self.name, bool(out), "" if out else "empty")
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")


class GoogleEngine:
    """Google Cloud Translation v2 REST 引擎（API key 方式，可选）。"""

    name = "google"
    label = "Google"
    URL = "https://translation.googleapis.com/language/translate/v2"

    def __init__(self, api_key: str = "", *, timeout: float = 8.0) -> None:
        self._key = str(api_key or "")
        self._timeout = float(timeout or 8.0)

    def supports_target(self, target_lang: str) -> bool:
        return True  # Google Translate 覆盖本项目全部目标语

    @property
    def available(self) -> bool:
        if not self._key:
            return False
        try:
            import aiohttp  # noqa: F401
            return True
        except Exception:
            return False

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import aiohttp

        params = {"key": self._key}
        payload = {"q": text, "target": target_lang, "format": "text"}
        if source_lang and source_lang != "unknown":
            payload["source"] = source_lang
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self.URL, params=params, data=payload) as resp:
                    if resp.status != 200:
                        return EngineResult("", self.name, False, f"http_{resp.status}")
                    j = await resp.json()
            out = (
                ((j.get("data") or {}).get("translations") or [{}])[0] or {}
            ).get("translatedText", "")
            return EngineResult(out, self.name, bool(out), "" if out else "empty")
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")


# Hunyuan-MT 官方模型卡覆盖的语种（映射到本项目语种码）。命中集内 supports_target=True，
# 集外交给下游引擎（ai/deepl/google）——即便置信度切换关着，冷门语种也不会被硬吃。
# Q-21 A（#290，2026-09-12）：码表单点迁到 src/i18n/lang_catalog（34 码含 yue/zh-tw 带能力位），
# 本处只引用——前端五处 / 路由白名单 / 本引擎再无第二份手写短表。
_HYMT_LANGS = set(_LANG_CATALOG_CODES)
# 公开别名：前端语种目录 / agent-lang 白名单必须与模型卡锁死同一份。
HYMT_TARGET_LANGS = frozenset(_HYMT_LANGS)

# zh 相关语种对的中文指令名（Hunyuan-MT 官方 zh<=>xx prompt 用中文语种名）。
_HYMT_ZH_NAME = {
    "zh": "中文", "zh-tw": "繁体中文", "en": "英语", "ja": "日语",
    "ko": "韩语", "fr": "法语",
    "es": "西班牙语", "it": "意大利语", "pt": "葡萄牙语", "de": "德语",
    "tr": "土耳其语", "ru": "俄语", "ar": "阿拉伯语", "th": "泰语",
    "id": "印尼语", "ms": "马来语", "vi": "越南语", "tl": "菲律宾语",
    "hi": "印地语", "pl": "波兰语", "nl": "荷兰语", "km": "高棉语",
    "yue": "粤语", "he": "希伯来语", "uk": "乌克兰语",
}


class OllamaMTEngine:
    """本地 Ollama 上的专用机器翻译模型引擎（默认适配腾讯 Hunyuan-MT）。

    与 AIEngine（走主对话 LLM 的完整回复管线）不同，本引擎直连 Ollama 的
    **原生 /api/chat 端点**、用**专用翻译模型 + 官方翻译 prompt 模板**，零 API
    成本、亚秒延迟、且天然保留 glossary 占位符〔N〕与 emoji。缺 base_url/model
    或 httpx 库不可用时 available=False，路由自动跳过。

    为什么原生 /api/chat 而非 /v1 兼容层（2026-08-09 实锤，勿改回）：
    /v1/chat/completions 会**忽略请求级 keep_alive**（生产两台 Ollama 0.31/0.32
    双双实测：请求带 30m，实际按服务端默认 5m/env 走）→ 模型闲置 5 分钟即被
    卸载，下一条翻译付 3~19s 冷加载；且 8s 引擎超时 < 冷载时长时直接超时 →
    端点进 60s 冷却 → 整窗流量错发云端 LLM。原生 /api/chat 尊重请求级
    keep_alive，每次翻译都会把驻留窗重置为 keep_alive——与 ai_client 断云兜底
    「Ollama 端点自动走原生 /api/chat」是同一个教训（见 _ollama_native_chat）。

    多端点双活：``base_url`` 可为单地址或 ``base_urls`` 列表（两台 LAN GPU 各跑
    一份同名模型）。每次调用按序尝试，首个成功获胜；某端点异常后进入短冷却
    （默认 60s，期间排到队尾但不剔除——全端点异常时仍会被兜底尝试），避免
    每条消息都为宕机主机付满额超时。

    Hunyuan-MT 官方 prompt：
    - zh<=>xx：``把下面的文本翻译成{中文语种名}，不要额外解释。\\n\\n{text}``
    - xx<=>xx：``Translate the following segment into {English name}, without additional explanation.\\n\\n{text}``
    """

    name = "ollama_mt"
    label = "HY-MT"

    _URL_COOLDOWN_SEC = 60.0  # 端点异常后的冷却窗（排序降权，不剔除）

    def __init__(
        self,
        base_url: Any = "",
        model: str = "",
        *,
        api_key: str = "ollama",
        timeout: float = 20.0,
        temperature: Optional[float] = None,
        max_tokens: int = 1024,
        keep_alive: str = "30m",
        api: str = "native",
        payload_extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        # base_url 兼容三种形态：单字符串 / 逗号分隔字符串 / 列表（build_engines 的 base_urls）
        if isinstance(base_url, (list, tuple)):
            urls = [str(u or "").strip() for u in base_url]
        else:
            urls = [u.strip() for u in str(base_url or "").split(",")]
        self._base_urls: List[str] = [u for u in urls if u]
        self._model = str(model or "").strip()
        self._key = str(api_key or "ollama").strip() or "ollama"
        self._timeout = float(timeout or 20.0)
        # None = 不传采样参数，沿用模型 Modelfile 内置的官方推荐值（HY-MT: t=0.7/top_p=0.6/rp=1.05）
        self._temperature = None if temperature is None else float(temperature)
        self._max_tokens = int(max_tokens or 1024)
        self._keep_alive = str(keep_alive or "").strip()
        # P2-XL（2026-08-16）：api="openai"（别名 openai_compat/v1）→ 同一套 Hunyuan
        # prompt/语种表/冷却双活，改打 OpenAI 兼容端点（vLLM 官方精度部署用；
        # keep_alive 是 Ollama 专有语义，openai 模式忽略）。缺省 native=旧行为零变化。
        self._api = ("openai" if str(api or "").strip().lower()
                     in ("openai", "openai_compat", "v1") else "native")
        # openai 模式的**逐后端**请求体补丁（与 GenericEngine.payload_extra 同名同义）。
        # 存在理由（2026-08-28 实锤）：LAN MT 落点换成 vLLM 上的 Qwen3.6-27B 后，该模型
        # **默认开 thinking** → 响应 `message.content` 为 null、正文全在 `reasoning` 里，
        # 且 60 token 预算全被思考吃光（finish_reason=length）⇒ 本引擎读 content 恒空，
        # 翻译兜底**静默失效**（云端一挂就没有第二层了）。
        # 解法是后端专有的，不该硬编进引擎：配 `chat_template_kwargs.enable_thinking:false`
        # 即恢复（实测同句 9 token / 1.1s 出正确译文）。native 模式不合并——Ollama 的
        # 采样参数走它自己的 `options` 结构，混进顶层会被忽略或报错。
        self._payload_extra: Dict[str, Any] = (
            dict(payload_extra) if isinstance(payload_extra, dict) else {})
        self._clients: Dict[str, Any] = {}
        self._url_bad_until: Dict[str, float] = {}

    def supports_target(self, target_lang: str) -> bool:
        return str(target_lang or "").strip().lower() in _HYMT_LANGS

    @property
    def _base_url(self) -> str:
        """首端点（兼容旧单端点语义，供日志/测试观察）。"""
        return self._base_urls[0] if self._base_urls else ""

    @property
    def available(self) -> bool:
        if not self._base_urls or not self._model:
            return False
        try:
            import httpx  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _native_root(url: str) -> str:
        """配置里误带 /v1 后缀也归一到 Ollama 根地址（原生 API 挂在根下）。"""
        base = str(url or "").rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")].rstrip("/")
        return base

    @staticmethod
    def _openai_chat_url(url: str) -> str:
        """OpenAI 兼容端点的 chat/completions 地址（base 带不带 /v1 都归一）。"""
        base = str(url or "").rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        return base + "/chat/completions"

    async def _post_chat(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST 原生 ``/api/chat``（或 openai 模式的 ``/v1/chat/completions``），返回 JSON。

        每端点缓存一个 httpx.AsyncClient（进程生命周期，连接复用）；连接 5s
        快败（死端点代价 ≤5s 即转下一端点），读超时全额保留给推理/冷加载。
        测试注入点：直接替换本方法（按 url 分流成败即可验双活/冷却）。
        """
        import httpx

        cli = self._clients.get(url)
        if cli is None:
            headers = {"Authorization": f"Bearer {self._key}"} if self._api == "openai" else None
            cli = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0), headers=headers)
            self._clients[url] = cli
        target = (self._openai_chat_url(url) if self._api == "openai"
                  else self._native_root(url) + "/api/chat")
        resp = await cli.post(target, json=payload)
        resp.raise_for_status()
        return resp.json()

    def _ordered_urls(self) -> List[str]:
        """健康端点保持配置序在前，冷却中的端点降到队尾（仍保底可试）。"""
        import time as _t

        now = _t.monotonic()
        healthy = [u for u in self._base_urls
                   if self._url_bad_until.get(u, 0.0) <= now]
        cooling = [u for u in self._base_urls if u not in healthy]
        return healthy + cooling

    def _mark_bad(self, url: str) -> None:
        import time as _t

        self._url_bad_until[url] = _t.monotonic() + self._URL_COOLDOWN_SEC

    def _build_prompt(self, text: str, source_lang: str, target_lang: str) -> str:
        src = str(source_lang or "").strip().lower()
        tgt = str(target_lang or "").strip().lower()
        # zh 系（含 zh-tw 繁体等变体）与粤语走官方中文指令模板
        _zh_side = (src.startswith("zh") or tgt.startswith("zh")
                    or src == "yue" or tgt == "yue")
        if _zh_side:
            name = _HYMT_ZH_NAME.get(tgt, tgt)
            return f"把下面的文本翻译成{name}，不要额外解释。\n\n{text}"
        from src.ai.translation_service import LANG_NAMES

        name = LANG_NAMES.get(tgt, tgt)
        return (
            f"Translate the following segment into {name}, "
            f"without additional explanation.\n\n{text}"
        )

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        # glossary 由 TranslationService 的 mask/restore 统一处理（占位符〔N〕被 MT 原样保留），
        # 故此处**不追加** glossary_hint —— 保持纯净翻译 prompt，MT 质量更稳。
        from src.ai.translation_service import _clean_translation

        if not (text or "").strip():
            return EngineResult("", self.name, False, "empty_input")
        # 模型卡覆盖外的语种直接让位（router 顺移下一引擎），防 7B MT 硬吃冷门语种产出乱码
        if not self.supports_target(target_lang):
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        prompt = self._build_prompt(text, source_lang, target_lang)
        if self._api == "openai":
            # vLLM/OpenAI 兼容口：同一 Hunyuan prompt，无 keep_alive/options 语义
            payload: Dict[str, Any] = {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self._max_tokens,
            }
            # 后端专有补丁（如 Qwen3 系的 chat_template_kwargs.enable_thinking:false）。
            # 放在固定字段**之后**合并＝允许覆写 max_tokens 这类需要按后端调的键。
            if self._payload_extra:
                payload.update(self._payload_extra)
            if self._temperature is not None:
                payload["temperature"] = self._temperature
        else:
            payload = {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,  # 原生端点默认流式，必须显式关掉才拿整块 JSON
                "options": {"num_predict": self._max_tokens},
            }
            if self._temperature is not None:
                payload["options"]["temperature"] = self._temperature
            if self._keep_alive:
                # 原生端点尊重请求级 keep_alive：每次翻译都把模型驻留窗重置为该值
                # （Ollama /v1 兼容层会忽略它——native 模式弃用 /v1 的直接原因，见类 docstring）。
                payload["keep_alive"] = self._keep_alive
        last_err = "no_endpoint"
        for url in self._ordered_urls():
            try:
                data = await self._post_chat(url, payload)
            except Exception as exc:  # noqa: BLE001
                self._mark_bad(url)
                last_err = f"{type(exc).__name__}: {exc}"
                continue
            if self._api == "openai":
                out = str((((data.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or "")
            else:
                out = str(((data.get("message") or {}).get("content")) or "")
            cleaned = _clean_translation(out)
            if not cleaned:
                # 空产出不冷却端点（是模型行为而非主机故障），直接试下一端点
                last_err = "empty"
                continue
            return EngineResult(cleaned, self.name, True)
        return EngineResult("", self.name, False, last_err)


class OpenAICompatEngine:
    """通用 OpenAI 兼容 LLM 翻译线（P0-XL1，2026-08-16）：一个类经配置实例化出任意多条线路。

    设计目标＝「新增一条云 LLM 线路 = 加一段 config，零代码」：Gemini（v1beta/openai
    兼容层）、DashScope Qwen-MT（compatible-mode/v1）、任意中转/自建 vLLM 端点均可。
    与 AIEngine 的分工：AIEngine 绑定主对话 ai_client（含熔断/兜底语义），本类是
    **独立的纯翻译通道**——不碰主链熔断器，坏了只影响本线路（router 自动跳过）。

    - ``raw_mode=True``：原文直送 user message（Qwen-MT 专用形态，不加指令 prompt）；
      默认 False 走与 AIEngine 同款的「干净翻译指令 + 机器翻译 system」。
    - ``payload_extra``：随请求体透传的额外字段（如 Qwen-MT 的 translation_options），
      字符串值支持占位符 {target_lang}/{source_lang}/{target_name}/{source_name}。
    - ``supports``：可选目标语白名单（列表）；缺省 None = LLM 任意目标语。
    - 鉴权：``api_key`` 为空 → available=False（缺 key 自动隐藏；LAN 免鉴权端点
      随便填一个非空值即可，如 "none"）。
    """

    def __init__(
        self,
        name: str,
        *,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        label: str = "",
        timeout: float = 12.0,
        temperature: Optional[float] = None,
        max_tokens: int = 1024,
        raw_mode: bool = False,
        payload_extra: Optional[Dict[str, Any]] = None,
        supports: Optional[List[str]] = None,
    ) -> None:
        self.name = str(name or "").strip().lower()
        self.label = str(label or name or "").strip() or self.name
        self._base = str(base_url or "").strip().rstrip("/")
        self._model = str(model or "").strip()
        self._key = str(api_key or "").strip()
        self._timeout = float(timeout or 12.0)
        self._temperature = None if temperature is None else float(temperature)
        self._max_tokens = int(max_tokens or 1024)
        self._raw_mode = bool(raw_mode)
        self._extra = payload_extra if isinstance(payload_extra, dict) else None
        subs = [str(s or "").strip().lower() for s in (supports or [])]
        self._supports = {s for s in subs if s} or None
        self._client: Any = None
        # P1-XT：raw_mode（原文直送，如 Qwen-MT）没有指令通道 → 语气不生效
        self.supports_tone = not self._raw_mode

    def supports_target(self, target_lang: str) -> bool:
        if self._supports is None:
            return True
        return str(target_lang or "").strip().lower().split("-")[0] in self._supports

    @property
    def available(self) -> bool:
        if not (self._base and self._model and self._key):
            return False
        try:
            import httpx  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _fill_placeholders(value: Any, mapping: Dict[str, str]) -> Any:
        """递归替换 payload_extra 里字符串值的 {占位符}（显式 replace，不用 format
        ——外部值可能含花括号，format 会炸）。"""
        if isinstance(value, str):
            out = value
            for k, v in mapping.items():
                out = out.replace("{" + k + "}", v)
            return out
        if isinstance(value, dict):
            return {k: OpenAICompatEngine._fill_placeholders(v, mapping) for k, v in value.items()}
        if isinstance(value, list):
            return [OpenAICompatEngine._fill_placeholders(v, mapping) for v in value]
        return value

    def build_payload(self, text: str, source_lang: str, target_lang: str,
                      style: str = "chat", glossary_hint: str = "") -> Dict[str, Any]:
        """构造请求体（纯函数便于门禁）。raw_mode=原文直送；否则干净翻译指令。"""
        from src.ai.translation_service import LANG_NAMES

        src = str(source_lang or "").strip().lower()
        tgt = str(target_lang or "").strip().lower()
        if self._raw_mode:
            messages = [{"role": "user", "content": text}]
        else:
            source_name = LANG_NAMES.get(src, src or "the source language")
            target_name = LANG_NAMES.get(tgt, tgt)
            tone = build_chat_tone(style)   # P1-XT：与 AIEngine 同一 tone 口径
            variant = variant_style_hint(tgt)   # zh-tw/yue 风格钉子（与 AIEngine 同源）
            if variant:
                variant = f" {variant}"
            messages = [
                {"role": "system", "content": AIEngine._BARE_TRANSLATION_SYSTEM},
                {"role": "user", "content": (
                    f"Translate the following chat message from {source_name} "
                    f"to {target_name}. {tone}{variant}{glossary_hint}\n\n{text}"
                )},
            ]
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if self._extra:
            from src.ai.translation_service import LANG_NAMES as _LN
            mapping = {
                "target_lang": tgt, "source_lang": src,
                "target_name": _LN.get(tgt, tgt), "source_name": _LN.get(src, src),
            }
            payload.update(self._fill_placeholders(self._extra, mapping))
        return payload

    async def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST {base}/chat/completions（测试注入点：替换本方法）。"""
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                headers={"Authorization": f"Bearer {self._key}"},
            )
        resp = await self._client.post(self._base + "/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        from src.ai.translation_service import _clean_translation

        if not (text or "").strip():
            return EngineResult("", self.name, False, "empty_input")
        if not self.supports_target(target_lang):
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        payload = self.build_payload(text, source_lang, target_lang, style, glossary_hint)
        try:
            data = await self._post(payload)
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")
        try:
            out = str((((data.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or "")
        except Exception:
            out = ""
        cleaned = _clean_translation(out)
        if not cleaned:
            return EngineResult("", self.name, False, "empty")
        # LLM 线同享语言完整性护栏（identity 回显 / 答非所译的兜底防线，与 AIEngine 同款）
        if not _output_lang_sane(cleaned, target_lang):
            return EngineResult("", self.name, False, "target_lang_mismatch")
        return EngineResult(cleaned, self.name, True)


# Microsoft Translator（BCP-47）语种映射：仅列本项目常用码，集外让位下一引擎。
_MS_LANG = {
    "zh": "zh-Hans", "zh-tw": "zh-Hant", "yue": "yue", "en": "en", "ja": "ja",
    "ko": "ko", "ru": "ru", "fr": "fr", "de": "de", "es": "es", "pt": "pt",
    "it": "it", "id": "id", "th": "th", "vi": "vi", "ms": "ms", "tr": "tr",
    "ar": "ar", "hi": "hi", "tl": "fil", "km": "km", "my": "my", "fa": "fa",
    "he": "he", "bn": "bn", "ta": "ta", "te": "te", "mr": "mr", "gu": "gu",
    "ur": "ur", "uk": "uk", "pl": "pl", "cs": "cs", "nl": "nl",
}


class MicrosoftEngine:
    """Azure Translator v3 REST 引擎（可选）。NMT 里字符单价最低、语种广的备份线。"""

    name = "microsoft"
    label = "Microsoft"
    URL = "https://api.cognitive.microsofttranslator.com/translate"

    def __init__(self, api_key: str = "", *, region: str = "", timeout: float = 8.0) -> None:
        self._key = str(api_key or "")
        self._region = str(region or "")
        self._timeout = float(timeout or 8.0)

    def supports_target(self, target_lang: str) -> bool:
        return str(target_lang or "").strip().lower() in _MS_LANG

    @property
    def available(self) -> bool:
        if not self._key:
            return False
        try:
            import aiohttp  # noqa: F401
            return True
        except Exception:
            return False

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import aiohttp

        tgt = _MS_LANG.get(str(target_lang or "").strip().lower())
        if not tgt:
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        params = {"api-version": "3.0", "to": tgt}
        src = _MS_LANG.get(str(source_lang or "").strip().lower())
        if src:
            params["from"] = src
        headers = {"Ocp-Apim-Subscription-Key": self._key,
                   "Content-Type": "application/json"}
        if self._region:
            headers["Ocp-Apim-Subscription-Region"] = self._region
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self.URL, params=params, headers=headers,
                                     json=[{"Text": text}]) as resp:
                    if resp.status != 200:
                        return EngineResult("", self.name, False, f"http_{resp.status}")
                    j = await resp.json()
            out = ((((j or [{}])[0] or {}).get("translations") or [{}])[0] or {}).get("text", "")
            return EngineResult(out, self.name, bool(out), "" if out else "empty")
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")


# 有道智云语种码（zh 用 zh-CHS）；集外让位下一引擎。
_YOUDAO_LANG = {
    "zh": "zh-CHS", "en": "en", "ja": "ja", "ko": "ko", "fr": "fr", "es": "es",
    "pt": "pt", "it": "it", "ru": "ru", "vi": "vi", "de": "de", "ar": "ar",
    "id": "id", "th": "th", "tr": "tr", "hi": "hi", "nl": "nl", "pl": "pl",
}


def youdao_sign_input(q: str) -> str:
    """有道 v3 签名的 input 截断规则（官方契约）：len<=20 原文；否则 前10+len+后10。"""
    s = str(q or "")
    return s if len(s) <= 20 else f"{s[:10]}{len(s)}{s[-10:]}"


class YoudaoEngine:
    """有道智云文本翻译引擎（可选）。中英电商语料好、国内客户信任度高的低价线。"""

    name = "youdao"
    label = "Youdao"
    URL = "https://openapi.youdao.com/api"

    def __init__(self, app_key: str = "", app_secret: str = "", *, timeout: float = 8.0) -> None:
        self._app_key = str(app_key or "")
        self._secret = str(app_secret or "")
        self._timeout = float(timeout or 8.0)

    def supports_target(self, target_lang: str) -> bool:
        return str(target_lang or "").strip().lower() in _YOUDAO_LANG

    @property
    def available(self) -> bool:
        if not (self._app_key and self._secret):
            return False
        try:
            import aiohttp  # noqa: F401
            return True
        except Exception:
            return False

    def build_form(self, text: str, source_lang: str, target_lang: str) -> Dict[str, str]:
        """v3 签名表单（纯函数便于门禁）：sign=sha256(appKey+input+salt+curtime+secret)。"""
        import hashlib
        import time as _t
        import uuid

        salt = str(uuid.uuid4())
        curtime = str(int(_t.time()))
        raw = self._app_key + youdao_sign_input(text) + salt + curtime + self._secret
        return {
            "q": text,
            "from": _YOUDAO_LANG.get(str(source_lang or "").strip().lower(), "auto"),
            "to": _YOUDAO_LANG.get(str(target_lang or "").strip().lower(), ""),
            "appKey": self._app_key,
            "salt": salt,
            "sign": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "signType": "v3",
            "curtime": curtime,
        }

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import aiohttp

        if not self.supports_target(target_lang):
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        form = self.build_form(text, source_lang, target_lang)
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self.URL, data=form) as resp:
                    if resp.status != 200:
                        return EngineResult("", self.name, False, f"http_{resp.status}")
                    j = await resp.json(content_type=None)
            if str((j or {}).get("errorCode", "")) != "0":
                return EngineResult("", self.name, False, f"api_{(j or {}).get('errorCode')}")
            out = str(((j.get("translation") or [""])[0]) or "")
            return EngineResult(out, self.name, bool(out), "" if out else "empty")
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")


# 百度翻译开放平台语种码（ja→jp / ko→kor / fr→fra / es→spa / vi→vie / ar→ara；
# zh-tw→cht=中文繁体）。
_BAIDU_LANG = {
    "zh": "zh", "zh-tw": "cht", "en": "en", "ja": "jp", "ko": "kor",
    "fr": "fra", "es": "spa",
    "th": "th", "ar": "ara", "ru": "ru", "pt": "pt", "de": "de", "it": "it",
    "nl": "nl", "pl": "pl", "cs": "cs", "vi": "vie", "yue": "yue", "id": "id",
}


class BaiduEngine:
    """百度翻译开放平台引擎（可选）。低价中英备份线（MD5 签名契约）。"""

    name = "baidu"
    label = "Baidu"
    URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

    def __init__(self, app_id: str = "", secret: str = "", *, timeout: float = 8.0) -> None:
        self._app_id = str(app_id or "")
        self._secret = str(secret or "")
        self._timeout = float(timeout or 8.0)

    def supports_target(self, target_lang: str) -> bool:
        return str(target_lang or "").strip().lower() in _BAIDU_LANG

    @property
    def available(self) -> bool:
        if not (self._app_id and self._secret):
            return False
        try:
            import aiohttp  # noqa: F401
            return True
        except Exception:
            return False

    def build_form(self, text: str, source_lang: str, target_lang: str) -> Dict[str, str]:
        """签名表单（纯函数便于门禁）：sign=md5(appid+q+salt+密钥)。"""
        import hashlib
        import random

        salt = str(random.randint(10000, 99999999))
        raw = self._app_id + text + salt + self._secret
        return {
            "q": text,
            "from": _BAIDU_LANG.get(str(source_lang or "").strip().lower(), "auto"),
            "to": _BAIDU_LANG.get(str(target_lang or "").strip().lower(), ""),
            "appid": self._app_id,
            "salt": salt,
            "sign": hashlib.md5(raw.encode("utf-8")).hexdigest(),
        }

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import aiohttp

        if not self.supports_target(target_lang):
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        form = self.build_form(text, source_lang, target_lang)
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self.URL, data=form) as resp:
                    if resp.status != 200:
                        return EngineResult("", self.name, False, f"http_{resp.status}")
                    j = await resp.json(content_type=None)
            if (j or {}).get("error_code"):
                return EngineResult("", self.name, False, f"api_{j.get('error_code')}")
            rows = (j or {}).get("trans_result") or []
            out = "\n".join(str(r.get("dst") or "") for r in rows if isinstance(r, dict)).strip()
            return EngineResult(out, self.name, bool(out), "" if out else "empty")
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")


class OpenCCEngine:
    """本地简→繁确定性转换引擎（可选，2026-08-29）。

    简体→繁体本质是字符/词汇映射而非翻译——OpenCC 零 token、微秒级、100% 保真，
    比 LLM 更适合当 zh-tw 的首选线（LLM 只兜「源语言不是中文」的场景）。默认
    profile ``s2twp``＝台湾正体 + 台湾常用词汇（软件→軟體、视频→影片）。

    让位规则（供 EngineRouter 顺移下游引擎）：
      - 目标语非 zh-tw → unsupported_target；
      - 源语言明确非中文（en/ja/...）→ unsupported_source（字形转换救不了真翻译）；
        源为空/unknown/auto 放行——translate() 的 detect 对中文文本必产出 zh，
        护栏 `_output_lang_sane` 兜底非中文误入。
    库缺失（opencc-python-reimplemented 未装）→ available=False，路由自动跳过。
    """

    name = "opencc"
    label = "OpenCC"

    _OK_SOURCES = {"", "zh", "zh-cn", "unknown", "auto"}

    def __init__(self, *, profile: str = "s2twp") -> None:
        self._profile = str(profile or "s2twp").strip() or "s2twp"
        self._cc: Any = None

    def supports_target(self, target_lang: str) -> bool:
        return str(target_lang or "").strip().lower() == "zh-tw"

    @property
    def available(self) -> bool:
        try:
            import opencc  # noqa: F401
            return True
        except Exception:
            return False

    def _converter(self) -> Any:
        if self._cc is None:
            import opencc
            self._cc = opencc.OpenCC(self._profile)
        return self._cc

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        if not (text or "").strip():
            return EngineResult("", self.name, False, "empty_input")
        if not self.supports_target(target_lang):
            return EngineResult("", self.name, False, f"unsupported_target:{target_lang}")
        src = str(source_lang or "").strip().lower()
        if src not in self._OK_SOURCES:
            return EngineResult("", self.name, False, f"unsupported_source:{src}")
        try:
            out = str(self._converter().convert(str(text)))
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", self.name, False, f"{type(exc).__name__}: {exc}")
        if not out.strip():
            return EngineResult("", self.name, False, "empty")
        return EngineResult(out, self.name, True)


class EngineRouter:
    """按顺序尝试引擎，首个「可用且非空」获胜；全失败返回 ok=False 带最后错误。

    ``min_confidence>0`` 时启用**置信度智能切换**：主引擎虽产出非空，但若译文置信度
    （空/未翻译/错语种/长度异常的确定性评分）低于阈值，则继续尝试下一引擎，最终返回
    达标的首个结果；都不达标则返回**置信度最高**的候选（degrade，不阻断）。默认 0 = 旧行为。

    ``per_lang_order``（按目标语引擎覆写）：{目标语: [引擎名...]}——评测证实某引擎在
    特定语对显著弱（如 7B MT 的 hi）时，把该语对重排到强引擎优先，其余语种不受影响。
    **只能重排 ``engines`` 里已有的引擎**（未知名忽略）；覆写列表之外的引擎按默认序
    附加在尾部兜底（保持「绝不因换序而丢兜底」）。

    ``semantic_embed_fn``（在线语义置信度，随 confidence_switch 生效）：注入异步批量
    嵌入函数后，对「确定性置信度已达标」的译文再比对 源文/译文 跨语言嵌入余弦，低于
    ``semantic_min_similarity`` 也触发切换（抓「语言对但意思漂移」——确定性信号的盲区）。
    嵌入失败/超时/返空一律**放行**（fail-open，绝不因嵌入端点抖动阻塞翻译）。
    """

    def __init__(
        self, engines: Optional[List[Any]] = None, *, min_confidence: float = 0.0,
        per_lang_order: Optional[Dict[str, Any]] = None,
        semantic_embed_fn: Optional[Any] = None,
        semantic_min_similarity: float = 0.65,
    ) -> None:
        self._engines: List[Any] = [e for e in (engines or []) if e is not None]
        self._min_confidence = max(0.0, float(min_confidence or 0.0))
        self._per_lang: Dict[str, List[str]] = {}
        for k, v in (per_lang_order or {}).items():
            names = [str(n or "").strip().lower()
                     for n in (v if isinstance(v, (list, tuple)) else [v])]
            names = [n for n in names if n]
            key = str(k or "").strip().lower()
            if key and names:
                self._per_lang[key] = names
        # 0.65 依 bge-m3 跨语言实测校准（2026-07 宽语料 44 对）：真实译文余弦
        # min=0.712/p5=0.775，错配内容 max=0.741/p95=0.683 → 0.65 好译文零误伤、
        # 意思漂移大部分被抓；阈值再高会误切 zh→fr/hi 等低分但正确的语对。
        self._sem_embed = semantic_embed_fn
        self._sem_min = float(semantic_min_similarity or 0.65)

    @property
    def primary_name(self) -> str:
        return self._engines[0].name if self._engines else "none"

    def names(self) -> List[str]:
        return [getattr(e, "name", "?") for e in self._engines]

    def any_available(self) -> bool:
        return any(getattr(e, "available", False) for e in self._engines)

    def _engines_for(self, target_lang: str) -> List[Any]:
        """目标语的引擎尝试序：per_lang_order 覆写优先，未列引擎按默认序附加兜底。"""
        tgt = str(target_lang or "").strip().lower().split("-")[0]
        names = self._per_lang.get(tgt)
        if not names:
            return self._engines
        by_name = {getattr(e, "name", ""): e for e in self._engines}
        seq = [by_name[n] for n in names if n in by_name]
        seq += [e for e in self._engines if e not in seq]
        return seq or self._engines

    # 语义闸门跳过阈：源文有效字符 < 4（"OK"/"👍"/"哈哈" 类）——超短文本嵌入噪声大、
    # 漂移风险≈0（错语种/未翻译已被确定性信号兜住），跳过省一次 embed 往返。
    _SEM_MIN_SOURCE_CHARS = 4

    async def _semantic_low(self, source: str, translated: str) -> Optional[float]:
        """跨语言语义相似度低于阈值 → 返回 sim 分（触发切换）；达标/嵌入不可用 → None。"""
        if self._sem_embed is None:
            return None
        if len(re.sub(r"\s+", "", source or "")) < self._SEM_MIN_SOURCE_CHARS:
            return None  # 超短文本：不值一次嵌入往返，直接放行
        try:
            vecs = await self._sem_embed([source or "", translated or ""])
        except Exception:
            return None
        if not vecs or len(vecs) < 2 or not vecs[0] or not vecs[1]:
            return None  # fail-open：嵌入端点抖动不当低置信处理
        va, vb = vecs[0], vecs[1]
        num = sum(x * y for x, y in zip(va, vb))
        da = sum(x * x for x in va) ** 0.5
        db = sum(x * x for x in vb) ** 0.5
        if da <= 0 or db <= 0:
            return None
        sim = num / (da * db)
        return round(sim, 3) if sim < self._sem_min else None

    def describe(self, target_lang: str) -> Dict[str, Any]:
        """对指定目标语产出引擎能力矩阵，供前端提前提示「主引擎是否兜底」。

        返回 {target_lang, primary, effective, engines:[{engine,available,supports}]}。
        primary/rows 按该目标语的**实际尝试序**（含 per_lang_order 覆写）；
        effective = 首个「可用且支持该目标语」的引擎（即实际会命中的）。
        """
        rows: List[Dict[str, Any]] = []
        effective = "none"
        seq = self._engines_for(target_lang)
        for eng in seq:
            name = getattr(eng, "name", "?")
            avail = bool(getattr(eng, "available", False))
            try:
                supports = bool(eng.supports_target(target_lang)) if hasattr(eng, "supports_target") else True
            except Exception:
                supports = True
            rows.append({"engine": name, "available": avail, "supports": supports,
                         "label": str(getattr(eng, "label", "") or name),
                         "tone": bool(getattr(eng, "supports_tone", False))})
            if effective == "none" and avail and supports:
                effective = name
        return {
            "target_lang": target_lang,
            "primary": getattr(seq[0], "name", "none") if seq else "none",
            "effective": effective,
            "engines": rows,
        }

    def engine_by_name(self, name: str) -> Optional[Any]:
        n = str(name or "").strip().lower()
        for e in self._engines:
            if getattr(e, "name", "") == n:
                return e
        return None

    async def translate_with(
        self, name: str, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        """强制走指定引擎（坐席多线路对照/手动选路），**不做故障转移**。

        引擎不存在/不可用 → ok=False，便于前端把该路显示为「不可用」。
        """
        eng = self.engine_by_name(name)
        if eng is None:
            return EngineResult("", str(name or "?"), False, "unknown_engine")
        if not getattr(eng, "available", False):
            return EngineResult("", eng.name, False, "unavailable")
        try:
            return await eng.translate(
                text, source_lang=source_lang, target_lang=target_lang,
                style=style, glossary_hint=glossary_hint,
            )
        except Exception as exc:  # noqa: BLE001
            return EngineResult("", eng.name, False, f"{type(exc).__name__}: {exc}")

    async def compare(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> List[EngineResult]:
        """并发对所有「可用且支持该目标语」的引擎各译一遍，供坐席多线路对照择优。

        不可用/不支持的引擎也返回一行（ok=False + 原因），前端可灰显。
        """
        import asyncio as _aio

        async def _one(eng: Any) -> EngineResult:
            name = getattr(eng, "name", "?")
            if not getattr(eng, "available", False):
                return EngineResult("", name, False, "unavailable")
            try:
                if hasattr(eng, "supports_target") and not eng.supports_target(target_lang):
                    return EngineResult("", name, False, f"unsupported_target:{target_lang}")
            except Exception:
                pass
            try:
                return await eng.translate(
                    text, source_lang=source_lang, target_lang=target_lang,
                    style=style, glossary_hint=glossary_hint,
                )
            except Exception as exc:  # noqa: BLE001
                return EngineResult("", name, False, f"{type(exc).__name__}: {exc}")

        if not self._engines:
            return []
        return list(await _aio.gather(*[_one(e) for e in self._engines]))

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        """选路翻译 + B56 译后对账（`_297` 宿务→马尼拉偷换实录）。

        对账层在选路层之外：无论哪个引擎胜出，源文专名/数字锚点在译文缺失
        → 括注原词追加在译文尾——监督译文的人永远能看到原词。确定性纯函数，
        任何异常放行原译文，绝不阻塞。
        """
        res = await self._translate_pick(
            text, source_lang=source_lang, target_lang=target_lang,
            style=style, glossary_hint=glossary_hint)
        try:
            if res.ok and res.text:
                from src.ai.translation_fidelity import annotate_missing_anchors
                fixed, missing = annotate_missing_anchors(
                    text, res.text, target_lang)
                if missing:
                    logger.info(
                        "[xlate] 专名对账：锚点 %s 在译文缺失 → 括注原词 "
                        "(engine=%s)", missing, res.engine)
                    res.text = fixed
        except Exception:
            logger.debug("[xlate] 专名对账异常（放行原译文）", exc_info=True)
        return res

    async def _translate_pick(
        self, text: str, *, source_lang: str, target_lang: str,
        style: str = "chat", glossary_hint: str = "",
    ) -> EngineResult:
        import time as _t

        try:
            from src.ai.translation_engine_stats import get_translation_engine_stats
            stats = get_translation_engine_stats()
        except Exception:
            stats = None

        # S：按日趋势落库（默认关 → no-op）。attempts 每次 translate 计一次，
        # low_conf/switches 在下方与 stats 观测同点记入，供看板画 7 天 sparkline。
        try:
            from src.ai.translation_trend_store import record_translation_trend as _trend
        except Exception:
            _trend = None
        if _trend is not None:
            _trend(attempts=1)

        conf_fn = None
        if self._min_confidence > 0:
            try:
                from src.ai.translation_confidence import translation_confidence as conf_fn
            except Exception:
                conf_fn = None

        last_err = "no_engine"
        attempted_fail = False  # 是否有「已尝试的可用引擎」失败（区别于「不可用被跳过」）
        best: Optional[EngineResult] = None       # 置信度切换：最高分候选（兜底）
        # 候选排序分两桶：过确定性闸门(1)恒优于没过(0)——语义低但「语言对/非空/长度正常」
        # 仍比硬错候选可用；桶内分别按 语义相似度 / 确定性分 排（确定性分差常是长度比噪声，
        # 不该压过语义证据）。
        best_key: Tuple[int, float] = (-1, -1.0)
        saw_low_conf = False                       # 本次调用是否发生过低置信（→ 切换观测）
        seq = self._engines_for(target_lang)
        call_primary = getattr(seq[0], "name", "none") if seq else "none"
        for eng in seq:
            if not getattr(eng, "available", False):
                last_err = f"{eng.name}:unavailable"
                continue
            # Q-21 C（#290）：目标语该引擎不支持（DeepL/有道无 yue，OpenCC 只 zh-tw；真支持 yue 的是
            # Hunyuan-MT / LLM(ai) / Google / Microsoft / Baidu）→ 直接让路，
            # 不再「试一下失败再切」——那会把粤语请求记成一次引擎失败 + fallback，还白付一次往返。
            # 与 compare() 同口径；引擎未声明 supports_target 视为全支持。
            try:
                if hasattr(eng, "supports_target") and not eng.supports_target(target_lang):
                    last_err = f"{getattr(eng, 'name', '?')}:unsupported_target:{target_lang}"
                    logger.debug("[xlate] skip engine=%s unsupported_target=%s",
                                 getattr(eng, "name", "?"), target_lang)
                    continue
            except Exception:
                pass
            t0 = _t.monotonic()
            try:
                res = await eng.translate(
                    text, source_lang=source_lang, target_lang=target_lang,
                    style=style, glossary_hint=glossary_hint,
                )
            except Exception as exc:  # noqa: BLE001
                if stats:
                    stats.record(getattr(eng, "name", "?"), ok=False,
                                 latency_ms=int((_t.monotonic() - t0) * 1000))
                attempted_fail = True
                last_err = f"{getattr(eng, 'name', '?')}:{type(exc).__name__}"
                continue
            lat = int((_t.monotonic() - t0) * 1000)
            won = bool(res.ok and res.text)
            if stats:
                stats.record(getattr(eng, "name", "?"), ok=won, latency_ms=lat)
            if won:
                if conf_fn is not None:
                    # 置信度智能切换：达标即返回；不达标记为候选，继续试下一引擎
                    conf = conf_fn(text, res.text, target_lang)
                    if conf >= self._min_confidence:
                        # 确定性信号达标 → 可选语义闸门（抓「语言对但意思漂移」）。
                        low_sim = await self._semantic_low(text, res.text)
                        if low_sim is None:
                            if stats and attempted_fail:
                                stats.record_fallback()
                            if stats and saw_low_conf:
                                stats.record_confidence_switch()  # 切换后采用了更优引擎
                                if _trend is not None:
                                    _trend(switches=1)
                            return res
                        # 语义低相似：与确定性低置信同待遇（候选保底 + 换下一引擎）
                        if (1, low_sim) > best_key:
                            best_key, best = (1, low_sim), res
                        saw_low_conf = True
                        if stats:
                            stats.record_low_confidence()
                            stats.record_semantic_low()
                        if _trend is not None:
                            _trend(low_conf=1, sem_low=1)
                        attempted_fail = True
                        last_err = f"{eng.name}:semantic_low({low_sim})"
                        continue
                    if (0, conf) > best_key:
                        best_key, best = (0, conf), res
                    saw_low_conf = True
                    if stats:
                        stats.record_low_confidence()
                    if _trend is not None:
                        _trend(low_conf=1)
                    attempted_fail = True
                    last_err = f"{eng.name}:low_confidence({conf})"
                    continue
                # 仅当此前有「可用引擎实际失败」才算降级（不可用引擎被跳过不计）
                if stats and attempted_fail:
                    stats.record_fallback()
                return res
            attempted_fail = True
            last_err = f"{eng.name}:{res.error or 'empty'}"
        # 置信度模式：无人达标 → 回退到最高分候选（绝不因「都不够好」而吐空）
        if best is not None:
            if stats:
                stats.record_fallback()
                if best.engine != call_primary:
                    stats.record_confidence_switch()  # 兜底也用了非主引擎
                    if _trend is not None:
                        _trend(switches=1)
            return best
        if stats and attempted_fail:
            stats.record_fallback()  # 有引擎尝试且全失败 → 记降级
        return EngineResult("", "none", False, last_err)


def build_engines(translation_cfg: Optional[Dict[str, Any]], ai_client: Optional[Any]) -> List[Any]:
    """按 config.translation.engines 构造引擎列表（顺序即故障转移优先级）。

    cfg 形如：
      engines:
        order: ["deepl", "ai"]   # 默认 ["ai"]
        deepl: {api_key: "...", pro: false}
        google: {api_key: "..."}
    未知引擎名忽略；缺 key 的引擎仍会被构造但 available=False（路由自动跳过）。
    """
    cfg = (translation_cfg or {}).get("engines") or {}
    order = cfg.get("order") or ["ai"]
    timeout = float(cfg.get("timeout_sec", 8) or 8)
    out: List[Any] = []
    for name in order:
        name = str(name or "").strip().lower()
        if name == "ai":
            out.append(AIEngine(ai_client))
        elif name == "deepl":
            dc = cfg.get("deepl") or {}
            out.append(DeepLEngine(dc.get("api_key", ""), pro=bool(dc.get("pro", False)), timeout=timeout))
        elif name == "google":
            gc = cfg.get("google") or {}
            out.append(GoogleEngine(gc.get("api_key", ""), timeout=timeout))
        elif name in ("ollama_mt", "hunyuan_mt", "hymt"):
            mc = cfg.get("ollama_mt") or cfg.get(name) or {}
            _temp = mc.get("temperature")
            # base_urls（列表，双活）优先；否则 base_url（单端点/逗号分隔）
            _urls = mc.get("base_urls") or mc.get("base_url", "")
            out.append(OllamaMTEngine(
                base_url=_urls,
                model=mc.get("model", ""),
                api_key=mc.get("api_key", "ollama"),
                timeout=float(mc.get("timeout_sec", timeout) or timeout),
                temperature=None if _temp is None else float(_temp),
                max_tokens=int(mc.get("max_tokens", 1024) or 1024),
                keep_alive=str(mc.get("keep_alive", "30m") or ""),
                api=str(mc.get("api", "native") or "native"),
                payload_extra=mc.get("payload_extra"),
            ))
        elif name == "microsoft":
            ms = cfg.get("microsoft") or {}
            out.append(MicrosoftEngine(
                ms.get("api_key", ""), region=str(ms.get("region", "") or ""),
                timeout=float(ms.get("timeout_sec", timeout) or timeout)))
        elif name == "youdao":
            yd = cfg.get("youdao") or {}
            out.append(YoudaoEngine(
                yd.get("app_key", ""), yd.get("app_secret", ""),
                timeout=float(yd.get("timeout_sec", timeout) or timeout)))
        elif name == "baidu":
            bd = cfg.get("baidu") or {}
            out.append(BaiduEngine(
                bd.get("app_id", ""), bd.get("secret", ""),
                timeout=float(bd.get("timeout_sec", timeout) or timeout)))
        elif name == "opencc":
            oc = cfg.get("opencc") or {}
            out.append(OpenCCEngine(
                profile=str(oc.get("profile", "s2twp") or "s2twp")))
        else:
            # P0-XL1：order 里的未知名先查 custom 通用线（OpenAI 兼容，一段配置=一条线路）；
            # custom 也没有 → 维持旧行为忽略（防拼写错直接炸装配）。
            cc = (cfg.get("custom") or {}).get(name)
            if isinstance(cc, dict):
                _ct = cc.get("temperature")
                out.append(OpenAICompatEngine(
                    name,
                    base_url=cc.get("base_url", ""),
                    model=cc.get("model", ""),
                    api_key=cc.get("api_key", ""),
                    label=str(cc.get("label", "") or ""),
                    timeout=float(cc.get("timeout_sec", 0) or max(timeout, 12.0)),
                    temperature=None if _ct is None else float(_ct),
                    max_tokens=int(cc.get("max_tokens", 1024) or 1024),
                    raw_mode=bool(cc.get("raw_mode", False)),
                    payload_extra=cc.get("payload_extra"),
                    supports=cc.get("supports"),
                ))
    if not out:
        out.append(AIEngine(ai_client))
    return out
