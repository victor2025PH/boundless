"""人设一致性守卫（陪聊沉浸感保护）。

LLM 不总是遵守 prompt 里"禁止使用 X"的指令；一旦回复漏出客服腔
（"有什么可以帮您的"）或自曝 AI 身份（"作为一个人工智能"），情感陪聊的"真人感"
就瞬间崩塌——这是本产品最致命的体验事故。本模块在回复生成后做一次**确定性**后置体检：

- 命中人设 ``speaking.forbidden_phrases``；
- 若 ``identity.deny_ai`` 为真，命中"自曝 AI 身份"的模式（保守匹配，避免误伤否定句）。

命中则**按句剥离**违规句子（保留其余内容），绝不返回空串
（极端情况整段都违规则回退原文 + 由调用方记日志/指标）。

纯函数、平台无关、可单测。真正的"违规重写（重新生成）"留作上层可选优化。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# 自曝 AI 身份的模式（仅 deny_ai 人设启用）。保守匹配："我是…AI" 命中，
# 但 "我不是 AI" 不命中（否定句不算露馅）。
_AI_SELF_ID_PATTERNS = [
    re.compile(r"作为(一个|一名)?\s*(AI|A\.?I\.?|人工智能|语言模型|大模型|聊天机器人|机器人|智能助手|虚拟助手)", re.I),
    re.compile(r"我(是|就是)(一个|一名|个|你的)?\s*(AI|A\.?I\.?|人工智能|语言模型|大模型|聊天机器人|机器人|智能助手|虚拟助手)", re.I),
    re.compile(r"(身为|作为)[^。！？!?\n]{0,8}(语言模型|人工智能|大模型)", re.I),
    re.compile(r"\bas an? (ai|artificial intelligence|language model)\b", re.I),
    re.compile(r"\bi[' ]?a?m an? (ai|artificial intelligence|language model)\b", re.I),
    re.compile(r"\blanguage model\b", re.I),
]

# 感知语境豁免（2026-07-20 阿龙实测误报）：「我怕你觉得我是AI客服机器人」是
# 「怕被当成机器人」的拟人打趣（强化真人感），不是身份自曝——与既有「否定句不算
# 露馅」同一类。命中片段若紧跟在感知动词后（觉得/以为/当成…，允许 ≤4 个非标点
# 字符间隔，如「觉得其实我是AI」）→ 不算违规。刻意不收「说」（"老实说我是AI"
# 是真自曝），宁可漏豁免不可漏拦截。
_PERCEPTION_PREFIX_RE = re.compile(
    r"(觉得|以为|当成|当作|误会|怀疑)[^。！？!?\n]{0,4}$"
)

# 按中英文句末标点切句（保留标点，便于无缝重组剩余句子）
_SENTENCE_SPLIT_RE = re.compile(r"[^。！？!?\n]*[。！？!?\n]|[^。！？!?\n]+")

# 无句末标点的中文口语流用空格当子句边界（拟人人设常用风格：「行 那再给你发一条
# 你听听」）。子句 = 非空白串 + 其尾随空白（保留空白，剔除违规子句后无缝重组）。
_WS_CLAUSE_RE = re.compile(r"\S+\s*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SENT_PUNCT_RE = re.compile(r"[。！？!?\n]")


def collect_forbidden(persona: Dict[str, Any]) -> Dict[str, Any]:
    """从人设 dict 抽取守卫所需的禁用项。"""
    speaking = (persona or {}).get("speaking") or {}
    identity = (persona or {}).get("identity") or {}
    phrases = [
        str(p).strip()
        for p in (speaking.get("forbidden_phrases") or [])
        if str(p).strip()
    ]
    return {"phrases": phrases, "deny_ai": bool(identity.get("deny_ai"))}


def _norm(s: str) -> str:
    """归一化用于子串比对：去所有空白 + 小写（中文不受影响，英文大小写/空格鲁棒）。"""
    return re.sub(r"\s+", "", s or "").lower()


def _matches_phrase(haystack_norm: str, phrases: List[str]) -> List[str]:
    out: List[str] = []
    for p in phrases:
        np = _norm(p)
        if np and np in haystack_norm:
            out.append(p)
    return out


def _matches_ai_self_id(text: str) -> List[str]:
    out: List[str] = []
    for pat in _AI_SELF_ID_PATTERNS:
        for m in pat.finditer(text):
            # 感知语境豁免：「(你)觉得/以为/当成…我是AI」不是自曝（见常量注释）
            if _PERCEPTION_PREFIX_RE.search(text[:m.start()]):
                continue
            out.append(m.group(0))
            break  # 每个模式至多记一个片段（与旧行为一致）
    return out


def matches_ai_self_identity(text: str) -> List[str]:
    """公共入口：文本中「自曝 AI 身份」的命中片段（含否定句/感知语境豁免）。

    供 quality_tracker 等监控组件复用同一判定口径，避免两套正则漂移
    （此前 quality_tracker 自带简版正则，把「怕你觉得我是AI机器人」误报成 identity_leak）。
    """
    return _matches_ai_self_id(str(text or ""))


def find_violations(text: str, persona: Dict[str, Any]) -> List[str]:
    """返回 ``text`` 中命中的违规片段清单（空 = 合规）。"""
    if not text:
        return []
    fb = collect_forbidden(persona)
    hits = _matches_phrase(_norm(text), fb["phrases"])
    if fb["deny_ai"]:
        hits.extend(_matches_ai_self_id(text))
    return hits


def _split_sentences(text: str) -> List[str]:
    parts = [m.group(0) for m in _SENTENCE_SPLIT_RE.finditer(text) if m.group(0)]
    # 整段无句末标点的中文口语流（空格代逗号句号的人设风格）→ 按空格切子句。
    # 否则整段=一个"句子"：一处违规 → 全删 → 触发「删光回退原文」= 守卫形同虚设
    # （2026-07-20 阿龙实测：日志喊「已剥离」实际原样发出的根因）。
    if (len(parts) <= 1 and text and not _SENT_PUNCT_RE.search(text)
            and _CJK_RE.search(text) and re.search(r"\s", text.strip())):
        return [m.group(0) for m in _WS_CLAUSE_RE.finditer(text)]
    return parts


def _sentence_violates(sentence: str, fb: Dict[str, Any]) -> bool:
    if _matches_phrase(_norm(sentence), fb["phrases"]):
        return True
    if fb["deny_ai"] and _matches_ai_self_id(sentence):
        return True
    return False


def sanitize(text: str, persona: Dict[str, Any]) -> Tuple[str, List[str]]:
    """剥离违规句，返回 ``(清洁文本, 命中清单)``。

    - 无禁用项或无命中 → 原样返回（命中清单为空）。
    - 有命中 → 删掉含违规片段的整句，保留其余；
    - 若删光（整段都违规）→ 先尝试 inline 抹掉禁用短语；仍空则回退原文（绝不返回空）。
    """
    if not text:
        return text, []
    fb = collect_forbidden(persona)
    if not fb["phrases"] and not fb["deny_ai"]:
        return text, []
    violations = find_violations(text, persona)
    if not violations:
        return text, []
    kept = [s for s in _split_sentences(text) if not _sentence_violates(s, fb)]
    cleaned = "".join(kept).strip()
    if not cleaned:
        cleaned = text
        for p in fb["phrases"]:
            if p:
                cleaned = re.sub(re.escape(p), "", cleaned, flags=re.I)
        cleaned = cleaned.strip()
        if not cleaned:
            return text, violations
    return cleaned, violations


__all__ = [
    "collect_forbidden", "find_violations", "matches_ai_self_identity", "sanitize",
]
