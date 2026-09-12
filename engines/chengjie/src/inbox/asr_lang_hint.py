"""会话语种先验 → ASR 复核提示（ASR P0，2026-09-12）。

Whisper 对短促/带口音/方言音频在 99 语开放空间里自由猜语种，是「中文客户语音被转成
韩语乱码」（0830 停电期实锤）这类事故的源头。WhatsApp RPA 线早有
``_asr_language_hint``（会话稳定语言先验 + 语种可疑重转），主链（协议入站 / AutoDraft）
一直是裸 ``auto``。本模块把先验算法抽成纯函数给主链用：

- **先验不直接钉死 language**（Whisper 对不匹配语言会翻译而非转写，见
  ``voice_transcriber.should_retry_with_lang_hint``）——只在检出语种与先验冲突且置信
  不足时按先验重转一次；
- 先验来源（优先级）：会话级「发→X」显式设置（``store.get_outbound_lang_if_set``，
  客户语言的运营事实源）→ 最近入站**文字**消息的强证据多数语种（转写/识图/占位等
  系统生成文本不算证据——它们正是要被复核的对象）；
- 只产 Whisper 认得的语种码（``ASR_HINT_LANGS``），算不出 → None（保持 auto）。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

#: Whisper 系语种码白名单（与 whatsapp_rpa.runner._ASR_HINT_LANGS 同集合 + yue）
ASR_HINT_LANGS = frozenset({
    "zh", "yue", "en", "ja", "ko", "ar", "ru", "th", "vi", "de", "fr",
    "es", "pt", "tr", "id", "hi", "it", "nl", "pl", "ms", "tl",
})

#: 至少几条文字消息构成强证据才给先验（单条太薄）
MIN_EVIDENCE_ROWS = 2
#: 多数语种占比门槛
MIN_MAJORITY = 0.6
#: 最多回看多少条入站消息
LOOKBACK_ROWS = 12


def _norm_hint(code: str) -> str:
    c = str(code or "").strip().lower().replace("_", "-")
    if not c or c in ("auto", "none", "null", ""):
        return ""
    if c in ("zh-tw", "zh-hk", "zh-hant", "zh-cn", "zh-hans", "cn"):
        return "zh"
    if c in ("fil",):
        return "tl"
    c = c.split("-")[0]
    return c if c in ASR_HINT_LANGS else ""


def _is_system_text(text: str) -> bool:
    """占位 / 识别结果 / 转写标记 → 不构成客户语言证据。"""
    t = str(text or "").strip()
    if not t:
        return True
    if t.startswith("["):
        return True
    try:
        from src.inbox.media_enrich import is_placeholder_only
        if is_placeholder_only(t):
            return True
    except Exception:
        pass
    return False


def hint_from_history(rows: Optional[Iterable[Dict[str, Any]]]) -> Optional[str]:
    """最近入站**文字**消息的强证据多数语种 → 先验码；证据不足 → None。

    ``rows``：消息行（dict，含 direction/text/media_type），顺序任意（内部按 ts 取最近）。
    只看 ``direction == "in"`` 且 ``media_type`` 为空（纯文字）的行——语音行的文本是
    转写（正是要复核的对象），识图/占位是系统文本。
    """
    if not rows:
        return None
    try:
        from src.ai.lang_policy import EvidenceStrength, classify_evidence
    except Exception:
        return None
    items: List[Dict[str, Any]] = [r for r in rows if isinstance(r, dict)]
    try:
        items.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    except Exception:
        pass
    fams: Counter = Counter()
    seen = 0
    for r in items:
        if str(r.get("direction") or "in") != "in":
            continue
        if str(r.get("media_type") or "").strip():
            continue
        t = str(r.get("text") or "")
        if _is_system_text(t):
            continue
        seen += 1
        try:
            code, strength = classify_evidence(t)
        except Exception:
            continue
        if strength == EvidenceStrength.STRONG:
            fam = _norm_hint(code)
            if fam:
                fams[fam] += 1
        if seen >= LOOKBACK_ROWS:
            break
    if not fams:
        return None
    total = sum(fams.values())
    top, n = fams.most_common(1)[0]
    if n < MIN_EVIDENCE_ROWS or (n / total) < MIN_MAJORITY:
        return None
    return top


def conversation_asr_lang_hint(store: Any, conversation_id: str) -> Optional[str]:
    """会话级先验：显式「发→X」→ 历史文字多数语种；算不出 → None。任何异常 → None。"""
    cid = str(conversation_id or "")
    if not cid or store is None:
        return None
    try:
        explicit = str(getattr(store, "get_outbound_lang_if_set")(cid) or "").strip().lower()
        h = _norm_hint(explicit)
        if h:
            return h
    except Exception:
        pass
    try:
        rows = store.list_recent_messages(cid, limit=LOOKBACK_ROWS * 2)
    except TypeError:
        try:
            rows = store.list_recent_messages(cid)
        except Exception:
            return None
    except Exception:
        return None
    return hint_from_history(rows)


__all__ = ["ASR_HINT_LANGS", "conversation_asr_lang_hint", "hint_from_history"]
