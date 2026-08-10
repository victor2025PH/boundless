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

# 目标语 → 期望脚本判定器（命中即「像目标语」）。CJK 系互相宽容（kanji 与中文同区）。
_TARGET_SCRIPT = {
    "ja": (_RE_KANA, _RE_CJK),
    "ko": (_RE_HANGUL,),
    "zh": (_RE_CJK,),
    "en": (_RE_LATIN,), "es": (_RE_LATIN,), "fr": (_RE_LATIN,),
    "de": (_RE_LATIN,), "pt": (_RE_LATIN,), "it": (_RE_LATIN,),
    "id": (_RE_LATIN,), "ms": (_RE_LATIN,), "vi": (_RE_LATIN,), "tr": (_RE_LATIN,),
    "ru": (_RE_CYRILLIC,), "th": (_RE_THAI,), "ar": (_RE_ARABIC,),
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _target_script_ratio(text: str, target_lang: str) -> float:
    """目标语脚本字符 / 全部「有意义脚本」字符；目标语未知 → 1.0（不判脚本）。"""
    tgt = (target_lang or "").strip().lower()
    pats = _TARGET_SCRIPT.get(tgt)
    if not pats:
        return 1.0
    meaningful = (
        len(_RE_CJK.findall(text)) + len(_RE_KANA.findall(text))
        + len(_RE_HANGUL.findall(text)) + len(_RE_LATIN.findall(text))
        + len(_RE_CYRILLIC.findall(text)) + len(_RE_THAI.findall(text))
        + len(_RE_ARABIC.findall(text))
    )
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

_REFUSAL_META_RE = re.compile(r"翻译|译文|translat|traduc", re.IGNORECASE)
_REFUSAL_FRAME_RE = re.compile(
    r"请提供|请您提供|没有可|无内容|无法|不需要|未提供|抱歉|仅提供|只提供"
    r"|please provide|provide (?:the |some )?text|nothing to|no (?:text|content)"
    r"|cannot|can't|unable|sorry",
    re.IGNORECASE,
)


def looks_like_engine_refusal(source: str, translated: str) -> bool:
    """译文是不是「引擎拒绝/客套话」而非翻译（True=别把它发给客户）。"""
    src = str(source or "").strip()
    out = str(translated or "").strip()
    if not src or not out or out == src:
        return False
    if not _REFUSAL_META_RE.search(out):
        return False
    if _REFUSAL_META_RE.search(src):
        return False   # 源文本来就在聊翻译 → 元词出现在译文是正常的
    return bool(_REFUSAL_FRAME_RE.search(out))


__all__ = [
    "translation_confidence", "confidence_signals", "confidence_tier",
    "looks_like_engine_refusal",
    "TIER_HIGH", "TIER_LOW",
]
