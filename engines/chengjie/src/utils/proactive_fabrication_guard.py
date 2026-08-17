"""主动开场「编造共同回忆」守卫 — 出站内容正确性红线（P1 2026-08-03）。

实锤事故（2026-08-03 13:36，account 8041810715 → 神马搜索会话）：主动触达
``news_share`` 模式给一个**没有任何记忆事实**的会话发了
「刷到粤BA决赛新闻，第一反应就是你以前拽我去打球的画面😂 最近还有在打吗？」——
「刷到新闻」是当下（合法），「你以前拽我去打球」是**凭空编造的共同经历**
（该联系人 episodic_memory 零条目，两人从没聊过打球）。真人客户一眼识破
「我们根本没一起打过球」→ 当场穿帮，还会反噬之前所有真实感。

与 ``memory_grounding`` 对称：那条护栏管**入库**（抽取的事实必须锚定用户原话），
这条管**出站**（要发出去的开场如果断言了具体共同过去，必须有记忆事实支撑）。
复用 ``memory_grounding._content_tokens`` 做词汇重叠，口径一致、单一事实源。

设计哲学（与变体守卫同族——宁可不发也不做错）：
  - **只在文案主动断言「共同过去」时才可能拦**——日常开场（想你了/最近怎么样/
    早安/纯新闻分享）不含断言标记，永不误伤（零误判是第一优先级）；
  - 命中断言 + 无记忆事实 → 高置信编造（神马搜索形态），拦；
  - 命中断言 + 有事实但事实不支撑该断言 → 疑似编造，仅在 ``strict_when_facts``
    档拦（默认关：有真事实的会话信任 grounding，绝不误伤）。

纯函数、零 IO、可单测。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from src.ai.memory_grounding import _content_tokens

# ── 共同过去断言标记词（触发检查的信号；组合词而非裸字，防误判）──────────────
# 判据＝「明确指向两人共同经历 / 对方具体过去行为」的断言。刻意**不**收裸「记得」
# （"记得多穿点"是当下关心）、"想起你"（想念，非事件断言）——只有下面这些组合形
# 才是"我在陈述一件发生过的具体往事"。按 2026-08-03 事故语料 + 保守边界校准。
_PAST_CLAIM_MARKERS: Tuple[str, ...] = (
    # 对方过去（时间指示 + 断言）
    "你以前", "你之前", "你曾经", "你原来", "你当时", "你那时", "你上回", "你上次",
    "你说过要", "你说过想", "你之前说", "你以前说", "你还说",
    # 共同「上次 / 那次」
    "上次我们", "上次咱", "上次一起", "我们上次", "咱们上次", "咱俩上次",
    "那次我们", "那次你", "那次咱", "那回我们",
    # 回忆召唤（组合，避开裸「记得」）
    "还记得我们", "还记得你", "还记得那", "还记得咱", "还记得上",
    "记得那次", "记得我们", "记得你说", "记得你当", "记得上次", "记得咱们",
    # 共同经历
    "我们一起", "咱们一起", "咱俩一起", "我们曾", "我们那会", "我们那次",
    # 英文
    "you used to", "we used to", "last time we", "last time you",
    "remember when we", "remember when you", "remember you told",
    "remember you said", "remember that time", "back when we", "back when you",
    "we once", "that time we", "that time you", "we always used",
)

# 断言标记词本身的字符（求内容 token 支撑时先剔除，防标记词参与重叠判定）
_MARKER_STRIP_RE = re.compile(
    "|".join(re.escape(m) for m in _PAST_CLAIM_MARKERS), re.IGNORECASE)


def past_claim_markers_hit(text: str) -> List[str]:
    """文案命中的共同过去断言标记词（可能多个；空=无断言）。"""
    t = str(text or "").lower()
    return [m for m in _PAST_CLAIM_MARKERS if m.lower() in t]


def _claim_supported_by_facts(text: str, context_facts: List[str]) -> bool:
    """断言的内容词是否被记忆事实支撑（复用 memory_grounding 词汇重叠口径）。

    取文案剔除断言标记词后的内容 token（CJK bigram / 拉丁词·数字），与所有
    记忆事实的内容 token 求交——有交集＝这件"往事"在记忆里有据。偏向放行
    （整句取词，宁可漏拦不误伤真事实会话）。
    """
    facts_blob = " ".join(str(f or "") for f in (context_facts or [])).strip()
    if not facts_blob:
        return False
    stripped = _MARKER_STRIP_RE.sub(" ", str(text or ""))
    t_bi, t_latin = _content_tokens(stripped)
    f_bi, f_latin = _content_tokens(facts_blob)
    return bool((t_bi & f_bi) or (t_latin & f_latin))


def detect_fabricated_memory(
    text: str,
    context_facts: Optional[List[str]] = None,
    *,
    strict_when_facts: bool = False,
) -> Tuple[bool, str]:
    """检测主动开场是否编造了无据的共同回忆。返回 ``(是否编造, 证据摘要)``。

    Args:
        text: 待发出的开场文案。
        context_facts: 本条计划携带的记忆事实（``plan['context_facts']``）；
            空 = 这条会话没有可依据的共同经历。
        strict_when_facts: True 时，即便有 facts 也要求断言被事实支撑（否则判编造）；
            默认 False——有 facts 即信任 grounding，只抓「零事实凭空编造」。

    绝不抛（判定器异常应由调用方 try 兜底，但本函数内部已尽量防御）。
    """
    hits = past_claim_markers_hit(text)
    if not hits:
        return (False, "")   # 无共同过去断言 → 安全，绝大多数开场走这里
    facts = [str(f).strip() for f in (context_facts or []) if str(f).strip()]
    if not facts:
        return (True, f"断言共同过去「{hits[0]}」但无任何记忆事实支撑")
    if strict_when_facts and not _claim_supported_by_facts(text, facts):
        return (True, f"断言共同过去「{hits[0]}」但记忆事实不支撑该往事")
    return (False, "")


# ─────────────────────────────────────────────────────────────────────────────
# 回复链（A 线 process_message / B 线 generate_inbox_draft）扩展 —— 2026-08-03
#
# ⚠ 模块名保留 ``proactive_`` 历史前缀（改名要碰热区 proactive_topic.py），但
# 适用范围**已不止主动触达**：下面这组用于日常应答的出站守卫。勿据名判断范围。
#
# 应答与主动开场有两处本质差异，处置因此不同：
#   ① **不能放弃发送**——主动开场可以「今天不说」，应答不回复＝失联。故这里是
#      **句级剥离**，且剥空时如实回落原文（宁可发出去被观测到，不可静默失语）。
#   ② **依据面广得多**——AI 说「你上次说想去大阪」可能依据的是五分钟前的对话，
#      甚至是**用户自己刚问起**「还记得我们聊的旅行吗」（顺着复述完全合法）。
#      只看长期记忆就会把合法的上下文引用剥掉——那比偶发编造更伤对话。
#      故依据池尽可能全收（见 build_reply_evidence），**漏拦优于误伤**。
#
# 于是分两档，避免拍脑袋定阈值：
#   - 依据池**完全为空**（真正零上下文的首轮 / bot 会话）＝高置信编造 → 剥离；
#   - 依据池非空但不支撑该断言＝疑似 → **只计数不动手**，攒样本让数据说话。
# ─────────────────────────────────────────────────────────────────────────────

# 依据池字符串字段（user_context 内；缺失即跳过，全部 best-effort）
_EVIDENCE_STR_KEYS: Tuple[str, ...] = (
    "_episodic_memory_text",   # 长期记忆注入块
    "_conversation_summary",   # 历史摘要
    "last_message",            # 上一轮用户消息
    "last_reply",              # 上一轮 AI 回复
)

# 对话历史条目里可能承载文本的键（不同链路结构不一，全试一遍）
_HISTORY_TEXT_KEYS: Tuple[str, ...] = (
    "content", "text", "message", "user", "assistant", "reply",
)

# 句切分：保留分隔符（剥离后其余句子的标点不受影响）
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")


def build_reply_evidence(
    user_context: Optional[dict],
    user_text: str = "",
    *,
    history_limit: int = 12,
) -> List[str]:
    """收集应答链可用的「往事依据池」。

    刻意宽口径：**用户本条消息也算依据**——客户自己提起「还记得我们上次…」时，
    AI 顺着复述是合法对话行为，不是编造。任何一处有内容即视为「这会话有据可依」，
    守卫随即退到只观测（见 strip_fabricated_sentences）。

    纯函数（只读入参 dict），任何异常形态的字段一律跳过。
    """
    out: List[str] = []
    ctx = user_context if isinstance(user_context, dict) else {}
    ut = str(user_text or "").strip()
    if ut:
        out.append(ut)
    for k in _EVIDENCE_STR_KEYS:
        v = ctx.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    hist = ctx.get("_conversation_history")
    if isinstance(hist, (list, tuple)):
        for item in list(hist)[-max(1, int(history_limit)):]:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for kk in _HISTORY_TEXT_KEYS:
                    vv = item.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        out.append(vv.strip())
    prof = ctx.get("_user_profile")
    if isinstance(prof, str) and prof.strip():
        out.append(prof.strip())
    elif isinstance(prof, dict):
        for vv in prof.values():
            if isinstance(vv, str) and vv.strip():
                out.append(vv.strip())
    return out


def build_precise_evidence(user_context: Optional[dict]) -> List[str]:
    """只收「长期事实源」的窄依据池（记忆 / 摘要 / 画像），**不含**对话历史。

    存在理由（2026-08-03 首轮真实语料验证的直接产物）：宽依据池里塞了整段对话
    历史，词汇交集几乎必然命中 → 用它判「疑似」等于装了个永远指零的哑仪表
    （1229 条实测 suspect=0）。故分工：
      - **剥离**（会改文本，必须零误伤）→ 宽池 build_reply_evidence；
      - **疑似**（只计数，要有信息量）→ 本函数的窄池，问的是「AI 说的这件往事，
        长期记忆里到底有没有对应」。
    窄池为空时刻意**不判疑似**（无从判定 ≠ 可疑，不冤枉）。
    """
    out: List[str] = []
    ctx = user_context if isinstance(user_context, dict) else {}
    for k in ("_episodic_memory_text", "_conversation_summary"):
        v = ctx.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    prof = ctx.get("_user_profile")
    if isinstance(prof, str) and prof.strip():
        out.append(prof.strip())
    elif isinstance(prof, dict):
        for vv in prof.values():
            if isinstance(vv, str) and vv.strip():
                out.append(vv.strip())
    return out


def strip_fabricated_sentences(
    text: str,
    evidence: Optional[List[str]] = None,
    *,
    precise_evidence: Optional[List[str]] = None,
) -> Tuple[str, dict]:
    """应答出站守卫：零依据时剥掉「断言共同往事」的句子。

    Returns:
        ``(处理后文本, info)``；info＝``{stripped: [被剥句], suspect: bool,
        evidence_empty: bool, all_stripped: bool}``。

    行为：
      - 文案无共同过去断言（绝大多数回复）→ 原样返回，零开销；
      - 有断言 + 依据池非空 → **不动手**；此时若给了 ``precise_evidence``（窄的
        长期事实源）且该往事在其中无对应 → 置 ``suspect`` 供观测（窄池为空则不判，
        无从判定不等于可疑）；
      - 有断言 + 依据池为空 → 逐句剥离命中句；
      - 剥空（整条都是编造）→ **如实回落原文** + ``all_stripped``：应答链不能
        发空、也不该突然答非所问，此时价值在「被计数、被看见」而非硬改文本。
    """
    src = str(text or "")
    info: dict = {
        "stripped": [], "suspect": False,
        "evidence_empty": False, "all_stripped": False,
    }
    if not src.strip() or not past_claim_markers_hit(src):
        return (src, info)
    ev = [str(e).strip() for e in (evidence or []) if str(e).strip()]
    if ev:
        pev = [str(e).strip()
               for e in (precise_evidence or []) if str(e).strip()]
        info["suspect"] = bool(pev) and not _claim_supported_by_facts(src, pev)
        return (src, info)
    info["evidence_empty"] = True
    kept: List[str] = []
    dropped: List[str] = []
    for part in _SENT_SPLIT_RE.split(src):
        if part.strip() and past_claim_markers_hit(part):
            dropped.append(part.strip())
        else:
            kept.append(part)
    if not dropped:
        return (src, info)
    info["stripped"] = dropped
    new_text = "".join(kept).strip()
    if not new_text:
        info["all_stripped"] = True
        return (src, info)
    return (new_text, info)


# 进程级轻量计数（零依赖零 IO；将来接看板只需读 fabrication_counters()）
_COUNTERS: dict = {}


def record_fabrication_guard(info: Optional[dict], *, source: str = "unknown") -> None:
    """把一次守卫判定计入进程计数（best-effort，绝不抛）。"""
    if not isinstance(info, dict):
        return
    bucket = _COUNTERS.setdefault(
        str(source or "unknown"),
        {"strip": 0, "suspect": 0, "all_stripped": 0},
    )
    if info.get("stripped"):
        bucket["strip"] += 1
    if info.get("suspect"):
        bucket["suspect"] += 1
    if info.get("all_stripped"):
        bucket["all_stripped"] += 1


def fabrication_counters() -> dict:
    """计数快照（按 source 分桶：a_line / b_line / …）。"""
    return {k: dict(v) for k, v in _COUNTERS.items()}


__all__ = [
    "past_claim_markers_hit",
    "detect_fabricated_memory",
    "build_reply_evidence",
    "build_precise_evidence",
    "strip_fabricated_sentences",
    "record_fabrication_guard",
    "fabrication_counters",
]
