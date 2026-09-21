"""译文在线置信度（确定性、零 LLM/零网络）—— 用于引擎智能切换 + 质量护栏。

线上无法对每条译文跑回译（翻倍成本/延迟），但常见失败模式可被**确定性信号**廉价识别：
  - **空译**：引擎降级/超时返回空；
  - **未翻译**：输出与原文几乎一致（引擎对该语对无能为力，原样回吐）；
  - **错语种**：目标语 ja/ko/en，输出却仍是中文（或目标脚本占比极低）；
  - **长度异常**：输出相对原文过短/过长（截断/复读）。

``translation_confidence`` 把这些合成 [0,1] 分。**不**判语义对错（那需回译/LLM），只挡硬错。
供 ``EngineRouter`` 在主引擎低置信时自动切换到下一引擎择优。
"""

from __future__ import annotations

import re
from typing import Any, Dict

# Unicode 脚本判定
_RE_CJK = re.compile(r"[\u4e00-\u9fff]")
_RE_KANA = re.compile(r"[\u3040-\u30ff]")           # 平/片假名（日文强信号）
_RE_HANGUL = re.compile(r"[\uac00-\ud7a3]")
_RE_LATIN = re.compile(r"[A-Za-z]")
_RE_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_RE_THAI = re.compile(r"[\u0e00-\u0e7f]")
_RE_ARABIC = re.compile(r"[\u0600-\u06ff]")
# P0-3（#343，2026-09-21）：Indic 五语 + 其它非拉丁脚本此前不在表里 → 目标语「未知」恒 1.0，
# 引擎把印地语原样吐英文 / 吐中文都判不出来。补齐后错脚本才会砸破 TIER_LOW 进重译 / HOLD。
_RE_DEVANAGARI = re.compile(r"[\u0900-\u097f]")     # hi / mr / ne
_RE_BENGALI = re.compile(r"[\u0980-\u09ff]")
_RE_GURMUKHI = re.compile(r"[\u0a00-\u0a7f]")       # pa
_RE_GUJARATI = re.compile(r"[\u0a80-\u0aff]")
_RE_TAMIL = re.compile(r"[\u0b80-\u0bff]")
_RE_TELUGU = re.compile(r"[\u0c00-\u0c7f]")
_RE_KANNADA = re.compile(r"[\u0c80-\u0cff]")
_RE_MALAYALAM = re.compile(r"[\u0d00-\u0d7f]")
_RE_SINHALA = re.compile(r"[\u0d80-\u0dff]")
_RE_GREEK = re.compile(r"[\u0370-\u03ff]")
_RE_HEBREW = re.compile(r"[\u0590-\u05ff]")
_RE_ARMENIAN = re.compile(r"[\u0530-\u058f]")
_RE_GEORGIAN = re.compile(r"[\u10a0-\u10ff]")
_RE_KHMER = re.compile(r"[\u1780-\u17ff]")
_RE_LAO = re.compile(r"[\u0e80-\u0eff]")
_RE_MYANMAR = re.compile(r"[\u1000-\u109f]")
_RE_ETHIOPIC = re.compile(r"[\u1200-\u137f]")       # am
_ALL_SCRIPTS = (
    _RE_CJK, _RE_KANA, _RE_HANGUL, _RE_LATIN, _RE_CYRILLIC, _RE_THAI, _RE_ARABIC,
    _RE_DEVANAGARI, _RE_BENGALI, _RE_GURMUKHI, _RE_GUJARATI, _RE_TAMIL, _RE_TELUGU,
    _RE_KANNADA, _RE_MALAYALAM, _RE_SINHALA, _RE_GREEK, _RE_HEBREW, _RE_ARMENIAN,
    _RE_GEORGIAN, _RE_KHMER, _RE_LAO, _RE_MYANMAR, _RE_ETHIOPIC,
)

# 目标语 → 期望脚本判定器（命中即「像目标语」）。CJK 系互相宽容（kanji 与中文同区）。
# zh-tw/yue：繁体与粤文都在 CJK 统一表意区，与 zh 同判定器（简繁/粤普之分不是
# 脚本层能判的，交给引擎层 variant_style_hint + 人工审校）。
_TARGET_SCRIPT = {
    "ja": (_RE_KANA, _RE_CJK),
    "ko": (_RE_HANGUL,),
    "zh": (_RE_CJK,),
    "zh-tw": (_RE_CJK,),
    "yue": (_RE_CJK,),
    "en": (_RE_LATIN,), "es": (_RE_LATIN,), "fr": (_RE_LATIN,),
    "de": (_RE_LATIN,), "pt": (_RE_LATIN,), "it": (_RE_LATIN,),
    "id": (_RE_LATIN,), "ms": (_RE_LATIN,), "vi": (_RE_LATIN,), "tr": (_RE_LATIN,),
    "ru": (_RE_CYRILLIC,), "th": (_RE_THAI,), "ar": (_RE_ARABIC,),
    "uk": (_RE_CYRILLIC,), "bg": (_RE_CYRILLIC,), "sr": (_RE_CYRILLIC, _RE_LATIN),
    "fa": (_RE_ARABIC,), "ur": (_RE_ARABIC,),
    "hi": (_RE_DEVANAGARI,), "mr": (_RE_DEVANAGARI,), "ne": (_RE_DEVANAGARI,),
    "bn": (_RE_BENGALI,), "pa": (_RE_GURMUKHI,), "gu": (_RE_GUJARATI,),
    "ta": (_RE_TAMIL,), "te": (_RE_TELUGU,), "kn": (_RE_KANNADA,), "ml": (_RE_MALAYALAM,),
    "si": (_RE_SINHALA,), "el": (_RE_GREEK,), "he": (_RE_HEBREW,), "hy": (_RE_ARMENIAN,),
    "ka": (_RE_GEORGIAN,), "km": (_RE_KHMER,), "lo": (_RE_LAO,), "my": (_RE_MYANMAR,),
    "am": (_RE_ETHIOPIC,),
    "nl": (_RE_LATIN,), "pl": (_RE_LATIN,), "sv": (_RE_LATIN,), "da": (_RE_LATIN,),
    "no": (_RE_LATIN,), "fi": (_RE_LATIN,), "cs": (_RE_LATIN,), "hu": (_RE_LATIN,),
    "ro": (_RE_LATIN,), "tl": (_RE_LATIN,), "sw": (_RE_LATIN,),
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _target_script_ratio(text: str, target_lang: str) -> float:
    """目标语脚本字符 / 全部「有意义脚本」字符；目标语未知 → 1.0（不判脚本）。"""
    tgt = (target_lang or "").strip().lower()
    pats = _TARGET_SCRIPT.get(tgt)
    if not pats:
        return 1.0
    meaningful = sum(len(p.findall(text)) for p in _ALL_SCRIPTS)
    if meaningful == 0:
        return 1.0  # 纯数字/符号/emoji（如 "OK 👍"）不按脚本判
    expected = sum(len(p.findall(text)) for p in pats)
    return expected / meaningful


def confidence_signals(source: str, translated: str, target_lang: str) -> Dict[str, Any]:
    """返回各信号明细（便于诊断/门禁解释）。"""
    src = source or ""
    out = translated or ""
    empty = not out.strip()
    untranslated = (not empty) and _norm(out) == _norm(src) and bool(_norm(src))
    script_ratio = round(_target_script_ratio(out, target_lang), 3)
    slen, olen = len(src.strip()), len(out.strip())
    ratio = (olen / slen) if slen else 1.0
    length_ok = (0.25 <= ratio <= 4.0) if slen else True
    return {
        "empty": empty,
        "untranslated": untranslated,
        "script_ratio": script_ratio,
        "length_ratio": round(ratio, 3),
        "length_ok": length_ok,
    }


def translation_confidence(source: str, translated: str, target_lang: str) -> float:
    """译文在线置信度 [0,1]。空=0；未翻译/错语种/长度异常显著拉低。

    刻意保守：纯数字/符号、目标语未知等不确定情形不扣分（返回偏高），只对**明确**
    的硬错（空、原样回吐、目标语脚本缺失、长度离谱）下狠手。
    """
    sig = confidence_signals(source, translated, target_lang)
    if sig["empty"]:
        return 0.0
    score = 1.0
    if sig["untranslated"]:
        score *= 0.15
    # 错语种：脚本占比越低扣越狠（ratio=1 不扣；ratio=0 仅留 0.3）
    score *= (0.3 + 0.7 * sig["script_ratio"])
    if not sig["length_ok"]:
        score *= 0.6
    return round(max(0.0, min(1.0, score)), 3)


# P0-2：分档阈值（对外单一真相源：compare 候选卡徽标 / 单条低置信提示共用同一口径）。
# low 上界 0.5 与 EngineRouter 常用 min_confidence 量级一致：确定性硬错信号才会砸破 0.5。
TIER_HIGH = 0.8
TIER_LOW = 0.5


def confidence_tier(score: float) -> str:
    """把 [0,1] 置信分离散成 high/mid/low 三档（前端徽标用，避免各端自造阈值漂移）。"""
    s = max(0.0, min(1.0, float(score or 0.0)))
    if s >= TIER_HIGH:
        return "high"
    if s >= TIER_LOW:
        return "mid"
    return "low"


# ── 引擎拒绝话术检测（P1-198，2026-08-05） ──────────────────────────────
# 生产实锤（198，6/25 历史消息）：坐席经出站翻译发「1」，LLM 引擎没有翻译而是
# 客套拒绝——「谢谢，您只提供了『1』，没有可翻译的内容。请您提供需要翻译的
# 文本」，这句**被当译文原样发给了客户**。判定刻意保守（宁漏勿误伤）：
#
# 必须同时满足两个正交信号才算拒绝——
#   ① 译文里出现「谈论翻译本身」的元词（翻译/translat/traduc…）而**源文没有**
#      （引擎输出在讨论翻译任务＝出戏；源文本来就聊翻译则不算，如
#      「帮我把这句话转成英文」→ "help me translate this" 是合格意译）；
#   ② 译文含拒绝框架词（请提供/没有/无法/抱歉/provide/nothing/cannot/sorry…）。
# 纯函数零网络；消费口在 TranslationService.translate 成功分支前置检
# （命中 → ok=False, error="engine_refusal"，出站链自动回落原文/HOLD）。

_REFUSAL_META_RE = re.compile(r"翻译|翻譯|译文|譯文|translat|traduc", re.IGNORECASE)
# #115（0831 钧原图 906）：实弹漏网句「您没有提供需要翻译的消息内容。请发送您
# 想翻译的文本。」——「没有提供 / 请发送」两个框架形态不在词表（只收了
# 未提供/请提供），meta 错误话术被当译文放进会话流。补齐简繁两形。
_REFUSAL_FRAME_RE = re.compile(
    r"请提供|请您提供|請提供|請您提供|没有可|沒有可|无内容|無內容|无法|無法"
    r"|不需要|未提供|没有提供|沒有提供|请发送|請發送|抱歉|仅提供|只提供"
    r"|please provide|provide (?:the |some )?text|please send|nothing to"
    r"|no (?:text|content)|cannot|can't|unable|sorry",
    re.IGNORECASE,
)


def looks_like_engine_refusal(source: str, translated: str) -> bool:
    """译文是不是「引擎拒绝/客套话」而非翻译（True=别把它发给客户）。

    #115：源文**为空**不再直接放行——服务层虽有空文本早退，但直连引擎/
    历史缓存等旁路仍可能把空输入送到 LLM；空源 + 译文在谈论翻译任务本身
    ＝最典型的 meta 错误响应，恰恰最该拦。
    """
    src = str(source or "").strip()
    out = str(translated or "").strip()
    if not out or out == src:
        return False
    if not _REFUSAL_META_RE.search(out):
        return False
    if src and _REFUSAL_META_RE.search(src):
        return False   # 源文本来就在聊翻译 → 元词出现在译文是正常的
    return bool(_REFUSAL_FRAME_RE.search(out))


__all__ = [
    "translation_confidence", "confidence_signals", "confidence_tier",
    "looks_like_engine_refusal",
    "TIER_HIGH", "TIER_LOW",
]
