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


# ─────────────────────────────────────────────────────────────────────────────
# 近况陈述编造守卫（#208 L-1 C，2026-09-06）—— 天气 / 地点·行程 / 正在做的事
#
# FTK6S7 全链：客户 15:03 一句问候 → 15:04「Just got back from a walk, the rain's
# light here」——档案无雨、无天气源、KB 跳过、记忆 0 条。编造进 _conversation_history
# 后被 FACT_LOCK（事实一致）当成事实反复提。这是与「编造往事」同族的另一半：
# 往事守卫管「我们过去…」，本守卫管「我此刻…」。
#
# 处置口径与往事守卫一致（应答不能失语；漏拦优于误伤）：
#   - 只看**自指**的近况陈述句（我/这边/here/I'm/Just got back…）；问对方的
#     （"Is it raining there?"）不算；
#   - 来源池 = 人设档案（bio 块）/ 天气源 / 场景状态 / 长期记忆 / 摘要 / 客户入站
#     （本条 + 上一条 + 历史里 **user 侧**）。**AI 自己的历史句不算来源**——那正是
#     编造自我强化的通道；
#   - 天气源在场 → 天气类整体视为有据（AI 拿到的是真数据，措辞偏差不算编造）；
#     场景状态在场 → 地点/行程/活动类整体有据；否则按类别词 + 内容 token 宽松重叠；
#   - 命中且无据 → 句级剥离；剥空 → 如实回落原文并标 all_stripped（调用方可换
#     无叙事问候）。
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_CATEGORY_RES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("weather", re.compile(
        r"(下雨|下着雨|小雨|大雨|暴雨|雨天|雨停|下雪|飘雪|好热|太热|热死|好冷|冷死|降温|"
        r"天气|放晴|晴天|阴天|刮风|大风|台风|起雾|闷热|凉快|暖和|"
        r"\brain(?:ing|y|ed|s)?\b|\bdrizzl\w*|\bpouring\b|\bsnow(?:ing|y)?\b|\bsunny\b|"
        r"\bcloudy\b|\bwindy\b|\bstorm\w*|\bweather\b|\bhumid\b|\bfreezing\b|"
        r"\bscorching\b|\bchilly\b|\bmuggy\b|\bdegrees\b|\d{1,2}\s?°)", re.I)),
    ("trip", re.compile(
        r"(刚回来|刚到家|刚到|在路上|出门了|刚出门|去了一趟|去了|飞到|落地|在机场|出差|"
        r"旅行|旅游|回老家|搬家|在车上|在地铁|在高铁|"
        r"just got back|just got home|just came back|got back from|heading (?:to|out|home|over)|"
        r"on my way|at the airport|on a trip|travel(?:ing|ling)|just landed|just arrived|"
        r"out of town|on the road|in the car|on the train|road trip)", re.I)),
    ("activity", re.compile(
        r"(刚跑完|刚健身|健身房|散步|遛狗|刚吃|在吃|做饭|煮饭|刚洗完|洗澡|在忙|加班|开会|"
        r"上班中|刚下班|刚醒|刚起|躺着|在做|逛街|买菜|刷剧|跑步|瑜伽|"
        r"just finished|just had|just ate|having (?:lunch|dinner|breakfast|coffee|a coffee)|"
        r"cooking|grabbing|at the gym|(?:a|my|from a|for a) walk|jog(?:ging)?|workout|"
        r"working late|in a meeting|just woke|shopping|binge|at work|off work)", re.I)),
)

# 自指（我在陈述自己的近况）；含「Just got back…」这类省略主语的英文口语
_STATUS_SELF_RE = re.compile(
    r"(我|咱|这边|这里|\bhere\b|\bi\b|\bi'm\b|\bi’m\b|\bim\b|\bi've\b|\bi’ve\b|\bme\b|\bmy\b|"
    r"^\s*(?:just|got|heading|on my|been|came|finished|had)\b)", re.I)
# 第二人称（问对方近况）：无自指 + 有第二人称 → 不是自述
_STATUS_PEER_RE = re.compile(r"(你|您|\byou\b|\byour\b|\bu\b|\bthere\?)", re.I)
# 以疑问词/助动词开头的英文句＝在问对方（"Is it raining where you are?"），即便句中有 I
_STATUS_QUESTION_LEAD_RE = re.compile(
    r"^\s*(?:is|are|was|were|do|does|did|have|has|had|will|would|can|could|should|"
    r"how|what|where|when|why|which|who)\b", re.I)
# 近况守卫的句切分：CJK 句读处必切；拉丁 .!?; 仅后随空白才切（"3.5" / "U.S." 不误切）。
# 刻意不用往事守卫的 _SENT_SPLIT_RE（它不认英文句号，"…rain's light here. How are
# you?" 会整段当一句问句放行——FTK6S7 原句正是这个形态）。
_STATUS_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；\n])|(?<=[.!?;])(?=\s)")

# 来源池里「类别级」有据的键：天气源在场 → weather 有据；场景状态在场 → trip/activity 有据
_WEATHER_SOURCE_KEYS: Tuple[str, ...] = (
    "_persona_weather_note", "_persona_weather_hook", "_persona_weather_snap",
)
_SCENE_SOURCE_KEYS: Tuple[str, ...] = ("_current_scene_note",)
# 人设档案 / 记忆类文本源（token 级 + 类别词级宽松匹配）
_STATUS_TEXT_SOURCE_KEYS: Tuple[str, ...] = (
    "_persona_bio_block", "_persona_place_label",
    "_episodic_memory_text", "_conversation_summary", "last_message",
)


def _status_hits(sentence: str) -> List[str]:
    """句子命中的近况类别（weather/trip/activity），空=不是近况陈述。"""
    s = str(sentence or "")
    if not s.strip():
        return []
    return [name for name, rx in _STATUS_CATEGORY_RES if rx.search(s)]


def is_self_status_claim(sentence: str) -> List[str]:
    """是否为**自指**的近况陈述；返回命中的类别列表（空=否）。

    问对方近况（"Is it raining there?" / "你那边下雨了吗"）不算：无自指 + 有第二人称
    或以问号收尾 → 排除。宁可漏拦不误伤。
    """
    s = str(sentence or "").strip()
    cats = _status_hits(s)
    if not cats:
        return []
    has_self = bool(_STATUS_SELF_RE.search(s))
    is_question = s.endswith(("?", "？"))
    if not has_self:
        # 无自指：问句 / 带第二人称 → 在问对方；否则（"Weather's nice today."）保守视为自述
        return [] if (is_question or _STATUS_PEER_RE.search(s)) else cats
    if is_question and _STATUS_QUESTION_LEAD_RE.match(s):
        # 疑问词开头的问句（"Is it raining where you are?"）→ 问对方，即便句中有 I
        return []
    # 自指陈述（含「我这边下雨了，你那边呢？」这类先陈述再反问）→ 近况陈述
    return cats


def build_status_evidence(user_context: Optional[dict], user_text: str = "") -> dict:
    """近况陈述的来源池：``{"weather": bool, "scene": bool, "texts": [..]}``。

    texts 只收**非 AI 侧**：人设档案 / 记忆 / 摘要 / 客户本条 + 上一条 + 历史里
    role==user 的条目。assistant 侧与 last_reply 刻意不收——AI 自己编过一次不能
    成为下一次的依据（FTK6S7「编造进历史后反复提」的闭环就断在这）。
    """
    ctx = user_context if isinstance(user_context, dict) else {}
    out: dict = {"weather": False, "scene": False, "texts": []}
    for k in _WEATHER_SOURCE_KEYS:
        v = ctx.get(k)
        if (isinstance(v, str) and v.strip()) or (isinstance(v, dict) and v):
            out["weather"] = True
            if isinstance(v, dict):
                out["texts"].append(" ".join(str(x) for x in v.values() if x))
            else:
                out["texts"].append(v.strip())
    for k in _SCENE_SOURCE_KEYS:
        v = ctx.get(k)
        if isinstance(v, str) and v.strip():
            out["scene"] = True
            out["texts"].append(v.strip())
    ut = str(user_text or "").strip()
    if ut:
        out["texts"].append(ut)
    for k in _STATUS_TEXT_SOURCE_KEYS:
        v = ctx.get(k)
        if isinstance(v, str) and v.strip():
            out["texts"].append(v.strip())
    hist = ctx.get("_conversation_history")
    if isinstance(hist, (list, tuple)):
        for item in list(hist)[-12:]:
            if isinstance(item, str) and item.strip():
                out["texts"].append(item.strip())      # 无角色信息 → 宽收
            elif isinstance(item, dict):
                role = str(item.get("role") or "user").lower()
                if role in ("assistant", "model", "ai", "system"):
                    continue
                for kk in _HISTORY_TEXT_KEYS:
                    vv = item.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        out["texts"].append(vv.strip())
    return out


def persona_status_facts(persona: Optional[dict]) -> str:
    """人设档案里能给「近况」作依据的字段拼成一段文本（纯函数）。

    只收 prompt 也在消费的人生素材（persona_manager.PROMPT_CONSUMED_FIELDS 同源）：
    background / role / context.hobbies / context.schedule / context.specific_memories /
    tastes.likes。档案写了「每天傍晚散步」「在咖啡店上班」，AI 说 walk / at work 就有据。
    """
    p = persona if isinstance(persona, dict) else {}
    if not p:
        return ""
    parts: List[str] = []

    def _add(v) -> None:
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
        elif isinstance(v, (list, tuple)):
            for x in v:
                _add(x)
        elif isinstance(v, dict):
            for x in v.values():
                _add(x)

    _add(p.get("background"))
    _add(p.get("role"))
    ctx = p.get("context") or {}
    if isinstance(ctx, dict):
        _add(ctx.get("hobbies"))
        _add(ctx.get("schedule"))
        _add(ctx.get("specific_memories"))
    tastes = p.get("tastes") or {}
    if isinstance(tastes, dict):
        _add(tastes.get("likes"))
    return "\n".join(parts)


def _lenient_overlap(a: str, b: str) -> bool:
    """内容 token 宽松重叠：CJK bigram 交集，或拉丁词按 4 字前缀互配（rain/raining）。"""
    a_bi, a_lat = _content_tokens(str(a or ""))
    b_bi, b_lat = _content_tokens(str(b or ""))
    if a_bi & b_bi:
        return True
    for x in a_lat:
        xs = x.lower()
        for y in b_lat:
            ys = y.lower()
            if xs == ys:
                return True
            if len(xs) >= 4 and len(ys) >= 4 and (xs.startswith(ys[:4]) or ys.startswith(xs[:4])):
                return True
    return False


def _status_claim_grounded(sentence: str, cats: List[str], evidence: dict) -> bool:
    """这句近况陈述有没有来源。类别级（天气源/场景）优先，其次类别词在档案/记忆/入站
    里出现过（跨语言也算：档案写「喜欢散步」→ 英文说 walk 有据），最后内容 token 宽松重叠。"""
    ev = evidence if isinstance(evidence, dict) else {}
    if "weather" in cats and ev.get("weather"):
        return True
    if ({"trip", "activity"} & set(cats)) and ev.get("scene"):
        return True
    texts = [str(t) for t in (ev.get("texts") or []) if str(t).strip()]
    if not texts:
        return False
    blob = "\n".join(texts)
    for name, rx in _STATUS_CATEGORY_RES:
        if name in cats and rx.search(blob):
            return True
    return _lenient_overlap(sentence, blob)


def strip_status_claims(text: str, evidence: Optional[dict] = None) -> Tuple[str, dict]:
    """应答出站守卫：剥掉**无来源**的自指近况陈述句（天气/地点·行程/正在做的事）。

    Returns:
        ``(处理后文本, info)``；info＝``{status_stripped: [被剥句], status_cats: [...],
        all_stripped: bool}``。剥空 → 如实回落原文 + all_stripped=True（调用方决定换
        无叙事问候还是照发被观测）。
    """
    src = str(text or "")
    info: dict = {"status_stripped": [], "status_cats": [], "all_stripped": False}
    if not src.strip():
        return (src, info)
    ev = evidence if isinstance(evidence, dict) else {"weather": False, "scene": False, "texts": []}
    kept: List[str] = []
    dropped: List[str] = []
    cats_all: List[str] = []
    for part in _STATUS_SENT_SPLIT_RE.split(src):
        cats = is_self_status_claim(part) if part.strip() else []
        if cats and not _status_claim_grounded(part, cats, ev):
            dropped.append(part.strip())
            cats_all.extend(c for c in cats if c not in cats_all)
        else:
            kept.append(part)
    if not dropped:
        return (src, info)
    info["status_stripped"] = dropped
    info["status_cats"] = cats_all
    new_text = "".join(kept).strip()
    if not new_text:
        info["all_stripped"] = True
        return (src, info)
    return (new_text, info)


_HISTORY_STATUS_NOTE_ZH = (
    "【近况说明】你之前在对话里提过的天气 / 地点行程 / 正在做的事，没有人设档案或对方"
    "原话作依据，只当随口一说：本条不要延续、不要展开、不要追加新细节；对方若追问就"
    "简短带过。没有依据时只问候、不叙述近况。"
)
_HISTORY_STATUS_NOTE_EN = (
    "Any weather / whereabouts / what-you-were-doing you mentioned earlier had no basis in "
    "the persona profile or the other person's words — treat it as an offhand remark: do not "
    "continue it, elaborate on it or add new details; if asked, brush past it briefly."
)


def history_status_claim_note(user_context: Optional[dict]) -> str:
    """AI 自己历史句里含**无来源**近况陈述 → 返回注入 prompt 的说明行（否则空串）。

    与 FACT_LOCK（事实一致）不冲突：不要求改口，只禁止把随口一说当事实**延续/展开**。
    """
    ctx = user_context if isinstance(user_context, dict) else {}
    ai_texts: List[str] = []
    hist = ctx.get("_conversation_history")
    if isinstance(hist, (list, tuple)):
        for item in list(hist)[-12:]:
            if isinstance(item, dict) and str(item.get("role") or "").lower() in (
                    "assistant", "model", "ai"):
                for kk in _HISTORY_TEXT_KEYS:
                    vv = item.get(kk)
                    if isinstance(vv, str) and vv.strip():
                        ai_texts.append(vv.strip())
    lr = ctx.get("last_reply")
    if isinstance(lr, str) and lr.strip():
        ai_texts.append(lr.strip())
    if not ai_texts:
        return ""
    ev = build_status_evidence(ctx, "")
    for t in ai_texts:
        for part in _STATUS_SENT_SPLIT_RE.split(t):
            cats = is_self_status_claim(part) if part.strip() else []
            if cats and not _status_claim_grounded(part, cats, ev):
                return _HISTORY_STATUS_NOTE_ZH + "\n" + _HISTORY_STATUS_NOTE_EN
    return ""


# 进程级轻量计数（零依赖零 IO；将来接看板只需读 fabrication_counters()）
_COUNTERS: dict = {}


def record_fabrication_guard(info: Optional[dict], *, source: str = "unknown") -> None:
    """把一次守卫判定计入进程计数（best-effort，绝不抛）。"""
    if not isinstance(info, dict):
        return
    bucket = _COUNTERS.setdefault(
        str(source or "unknown"),
        {"strip": 0, "suspect": 0, "all_stripped": 0, "status_strip": 0},
    )
    bucket.setdefault("status_strip", 0)
    if info.get("stripped"):
        bucket["strip"] += 1
    if info.get("suspect"):
        bucket["suspect"] += 1
    if info.get("all_stripped"):
        bucket["all_stripped"] += 1
    if info.get("status_stripped"):
        bucket["status_strip"] += 1


def fabrication_counters() -> dict:
    """计数快照（按 source 分桶：a_line / b_line / …）。"""
    return {k: dict(v) for k, v in _COUNTERS.items()}


# ─────────────────────────────────────────────────────────────────────────────
# M-1 A（#214 #218，2026-09-06）：「与客户档案矛盾」守卫 —— 出站内容正确性第三条腿
#
# 实锤：运营给菲律宾本地客户（相识一年）排了一条关怀，LLM 改写成「How are you
# finding life in the Philippines?」真发出去——把本地人当成刚到菲律宾的外国人、把老
# 客当成新客。前两条守卫（编造共同回忆 / 无来源近况）都不盖这类错：句子里没有
# 「你以前」也没有 AI 自己的近况，它是**与已知档案矛盾**。
#
# 判据只吃两类硬事实（宁可漏拦不误伤）：
#   ① 客户的国家/居住地 C（档案 country / residence）：文案对 C 用「外来者框架」
#      （life in C / moved to C / 在 C 的生活习惯吗 / 去 C 好玩吗 …）→ 矛盾；
#   ② 相识时长 ≥30 天：文案用「初识框架」（nice to meet you / 很高兴认识你 /
#      初次见面 …）→ 矛盾。
# 档案缺字段 → 对应判据不参与；档案全空 → 恒不拦。
# ─────────────────────────────────────────────────────────────────────────────

# 极简 gazetteer：ISO-2 → (中文名/城市, 英文名/城市, 民族称谓)。只收对客业务高频地区
# （东南亚 + 主要英语国 + 东亚），够用即可；未收录的地名只是不参与判定。
PLACE_GAZETTEER: dict = {
    "PH": (("菲律宾", "马尼拉", "宿务", "宿雾", "达沃"),
           ("philippines", "manila", "cebu", "davao", "quezon"),
           ("filipino", "filipina", "pinoy", "pinay")),
    "TH": (("泰国", "曼谷", "清迈", "普吉", "芭提雅"),
           ("thailand", "bangkok", "chiang mai", "phuket", "pattaya"), ("thai",)),
    "VN": (("越南", "河内", "胡志明", "岘港"),
           ("vietnam", "hanoi", "ho chi minh", "saigon", "da nang"), ("vietnamese",)),
    "ID": (("印尼", "印度尼西亚", "雅加达", "巴厘"),
           ("indonesia", "jakarta", "bali", "surabaya"), ("indonesian",)),
    "MY": (("马来西亚", "吉隆坡", "槟城"),
           ("malaysia", "kuala lumpur", "penang", "johor"), ("malaysian",)),
    "SG": (("新加坡",), ("singapore",), ("singaporean",)),
    "KH": (("柬埔寨", "金边"), ("cambodia", "phnom penh", "siem reap"), ("cambodian", "khmer")),
    "MM": (("缅甸", "仰光"), ("myanmar", "burma", "yangon"), ("burmese",)),
    "LA": (("老挝", "万象"), ("laos", "vientiane"), ("laotian", "lao")),
    "JP": (("日本", "东京", "大阪", "京都"),
           ("japan", "tokyo", "osaka", "kyoto"), ("japanese",)),
    "KR": (("韩国", "首尔", "釜山"), ("korea", "seoul", "busan"), ("korean",)),
    "CN": (("中国", "北京", "上海", "深圳", "广州", "成都"),
           ("china", "beijing", "shanghai", "shenzhen", "guangzhou", "chengdu"), ("chinese",)),
    "HK": (("香港",), ("hong kong", "hongkong"), ()),
    "TW": (("台湾", "台北", "高雄"), ("taiwan", "taipei", "kaohsiung"), ("taiwanese",)),
    "IN": (("印度", "孟买", "新德里", "班加罗尔"),
           ("india", "mumbai", "delhi", "bangalore", "bengaluru"), ("indian",)),
    "PK": (("巴基斯坦", "卡拉奇"), ("pakistan", "karachi", "lahore"), ("pakistani",)),
    "BD": (("孟加拉", "达卡"), ("bangladesh", "dhaka"), ("bangladeshi",)),
    "NP": (("尼泊尔", "加德满都"), ("nepal", "kathmandu"), ("nepali", "nepalese")),
    "AE": (("阿联酋", "迪拜", "阿布扎比"), ("dubai", "abu dhabi", "uae", "emirates"), ("emirati",)),
    "SA": (("沙特", "利雅得"), ("saudi", "riyadh", "jeddah"), ("saudi",)),
    "TR": (("土耳其", "伊斯坦布尔"), ("turkey", "türkiye", "istanbul", "ankara"), ("turkish",)),
    "EG": (("埃及", "开罗"), ("egypt", "cairo"), ("egyptian",)),
    "NG": (("尼日利亚", "拉各斯"), ("nigeria", "lagos", "abuja"), ("nigerian",)),
    "ZA": (("南非", "约翰内斯堡", "开普敦"),
           ("south africa", "johannesburg", "cape town"), ("south african",)),
    "US": (("美国", "纽约", "洛杉矶", "旧金山", "加州"),
           ("america", "the states", "the us", "usa", "new york", "los angeles",
            "san francisco", "california", "texas", "florida", "chicago"), ("american",)),
    "CA": (("加拿大", "多伦多", "温哥华"), ("canada", "toronto", "vancouver", "montreal"), ("canadian",)),
    "GB": (("英国", "伦敦", "曼彻斯特"),
           ("england", "britain", "the uk", "london", "manchester"), ("british", "english")),
    "AU": (("澳洲", "澳大利亚", "悉尼", "墨尔本"),
           ("australia", "sydney", "melbourne", "brisbane", "perth"), ("australian", "aussie")),
    "NZ": (("新西兰", "奥克兰"), ("new zealand", "auckland"), ("kiwi",)),
    "DE": (("德国", "柏林", "慕尼黑"), ("germany", "berlin", "munich"), ("german",)),
    "FR": (("法国", "巴黎"), ("france", "paris"), ("french",)),
    "ES": (("西班牙", "马德里", "巴塞罗那"), ("spain", "madrid", "barcelona"), ("spanish",)),
    "IT": (("意大利", "罗马", "米兰"), ("italy", "rome", "milan"), ("italian",)),
    "RU": (("俄罗斯", "莫斯科"), ("russia", "moscow"), ("russian",)),
    "BR": (("巴西", "圣保罗", "里约"), ("brazil", "sao paulo", "rio"), ("brazilian",)),
    "MX": (("墨西哥",), ("mexico", "mexico city", "cancun"), ("mexican",)),
}

_COUNTRY_NAME_TO_CODE: dict = {}
for _code, (_zh, _en, _dem) in PLACE_GAZETTEER.items():
    for _n in (*_zh, *_en, *_dem, _code.lower()):
        _COUNTRY_NAME_TO_CODE.setdefault(str(_n).lower(), _code)


def normalize_country_code(value: str) -> str:
    """任意国家/城市/民族写法（『菲律宾』/『Philippines』/『Cebu』/『PH』/『Filipino』）→ ISO-2；
    认不出 → ""。"""
    v = str(value or "").strip().lower()
    if not v:
        return ""
    if v in _COUNTRY_NAME_TO_CODE:
        return _COUNTRY_NAME_TO_CODE[v]
    # 自由文本（「菲律宾宿务」「Cebu, Philippines」）：按最长名称子串命中
    best = ""
    best_len = 0
    for name, code in _COUNTRY_NAME_TO_CODE.items():
        if len(name) <= best_len:
            continue
        if _name_in_text(name, v):
            best, best_len = code, len(name)
    return best


def _name_in_text(name: str, low_text: str) -> bool:
    """地名是否出现在（已小写的）文本里：拉丁名按词边界，CJK 名按子串。"""
    n = str(name or "").lower()
    if not n:
        return False
    if re.search(r"[a-z]", n):
        return re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", low_text) is not None
    return n in low_text


def place_mentions(text: str) -> List[str]:
    """文案里提到的国家/地区（ISO-2 列表，去重保序）。只认 gazetteer 内地名；
    民族称谓（Filipino）也算提到该国。"""
    low = str(text or "").lower()
    if not low:
        return []
    out: List[str] = []
    for code, (zh, en, dem) in PLACE_GAZETTEER.items():
        for n in (*zh, *en, *dem):
            if _name_in_text(n, low):
                if code not in out:
                    out.append(code)
                break
    return out


# 「外来者框架」：把对方当成**刚到 / 旅居 / 造访** C 的人才会说的话。
_OUTSIDER_FRAME_EN = re.compile(
    r"\b(life in|living in|live in|moved? to|moving to|settl(?:e|ed|ing) in|"
    r"getting used to|adjust(?:ed|ing)? to|adapt(?:ed|ing)? to|"
    r"how (?:do|are|did) you (?:like|find|finding|enjoy|enjoying)|finding life|"
    r"how(?:'s| is) (?:life|it) (?:in|over there)|welcome to|enjoy(?:ing)? your (?:time|stay)|"
    r"your (?:stay|trip|visit|time) (?:in|to)|(?:been|going|went|travel(?:l)?ing|flying|fly) to|"
    r"visit(?:ing)?|vacation in|holiday in|arrive[ds]? in|landed in|there yet)\b",
    re.IGNORECASE,
)
_OUTSIDER_FRAME_ZH = (
    "的生活怎么样", "生活怎么样", "生活习惯", "习惯那边", "适应那边", "适应了吗", "习惯了吗",
    "刚到", "刚去", "来到", "去了", "到了吗", "旅行", "旅游", "度假", "出差", "好玩吗", "玩得",
    "那边天气", "那边怎么样", "在那边", "过去了", "飞过去", "落地",
)

# 「初识框架」：只对刚认识的人说的话。
_NEW_ACQ_EN = re.compile(
    r"\b(nice|glad|pleased|great|lovely|good) to (?:finally )?meet you\b|"
    r"\bpleasure to meet you\b|\bfirst time (?:we|to) (?:talk|chat|speak)|"
    r"\bwe just met\b|\bnew friend\b|\bgetting to know you\b|\bjust started talking\b",
    re.IGNORECASE,
)
_NEW_ACQ_ZH = ("很高兴认识你", "认识你很高兴", "初次见面", "刚认识", "新朋友", "第一次聊",
               "初次聊", "很高兴认识您", "第一次跟你聊", "刚加你")

_KNOWN_SINCE_MIN_DAYS = {"recent": 0, "months": 60, "halfyear": 180, "years": 365}


def known_days_from_profile(profile: Optional[dict]) -> int:
    """档案 → 相识天数下限（known_days 显式值优先，其次 known_since 档位；未知 → 0）。"""
    prof = profile or {}
    try:
        d = int(float(prof.get("known_days") or 0))
    except (TypeError, ValueError):
        d = 0
    ks = str(prof.get("known_since") or "").strip().lower()
    return max(d, _KNOWN_SINCE_MIN_DAYS.get(ks, 0))


def detect_profile_contradiction(text: str, profile: Optional[dict]) -> Tuple[bool, str]:
    """出站文案是否与客户档案矛盾。返回 ``(是否矛盾, 原因码)``。

    原因码：``local_as_foreigner``（把 C 的本地人当外来者）/ ``old_as_new``（把老客当
    新客）/ ``""``。``profile`` 键：``country`` / ``residence``（任意写法，内部归一
    ISO-2）、``known_since``（recent/months/halfyear/years）、``known_days``。
    纯函数、绝不抛；档案缺字段即该判据不参与。
    """
    t = str(text or "")
    if not t.strip():
        return (False, "")
    prof = profile or {}
    home = (normalize_country_code(str(prof.get("residence") or ""))
            or normalize_country_code(str(prof.get("country") or "")))
    if home and home in place_mentions(t):
        if _OUTSIDER_FRAME_EN.search(t) or any(k in t for k in _OUTSIDER_FRAME_ZH):
            return (True, "local_as_foreigner")
    if known_days_from_profile(prof) >= 30:
        if _NEW_ACQ_EN.search(t) or any(k in t for k in _NEW_ACQ_ZH):
            return (True, "old_as_new")
    return (False, "")


# ─────────────────────────────────────────────────────────────────────────────
# 第三方实体守卫（#341，2026-09-20）——「你妈妈装修怎么样了」而记忆里没有妈妈
#
# 往事守卫只认「你以前/我们上次」这类断言标记；#341 原句 *how your client's mom
# is doing with the renovation* 一个标记都没有，直接放行。真正该问的是：文案里
# 提到的 **对方的某个人**（妈妈/女儿/老公/朋友/老板/狗…）在这位客户的事实集里
# 有没有对应实体。实体级支撑比整句词汇重叠硬得多，且几乎不可能误伤——开场
# 里点名对方的家人本来就必须有据。
#
# 另外「your client / 你的客户」是视角泄漏（运营称谓漏进对客文案）：无论事实
# 集里有没有，这句都不能发。
# ─────────────────────────────────────────────────────────────────────────────

# (实体组名, 文案里指向对方第三方的正则, 事实集里可作支撑的别名正则)
_THIRD_PARTY_GROUPS: Tuple[Tuple[str, "re.Pattern[str]", "re.Pattern[str]"], ...] = (
    ("mother", re.compile(
        r"(你|您|TA|ta)(的)?(妈|媽|母亲|母親)|\byour\s+(?:mom|mum|mother|mama|mommy)\b", re.I),
     re.compile(r"妈|媽|母亲|母親|\b(?:mom|mum|mother|mama|mommy)\b", re.I)),
    ("father", re.compile(
        r"(你|您|TA|ta)(的)?(爸|父亲|父親)|\byour\s+(?:dad|father|papa|daddy)\b", re.I),
     re.compile(r"爸|父亲|父親|\b(?:dad|father|papa|daddy)\b", re.I)),
    ("parents", re.compile(
        r"(你|您|TA|ta)(的)?(父母|爸妈|爸媽)|\byour\s+(?:parents|folks)\b", re.I),
     re.compile(r"父母|爸妈|爸媽|妈|媽|爸|\b(?:parents|folks|mom|dad|mother|father)\b", re.I)),
    ("partner", re.compile(
        r"(你|您|TA|ta)(的)?(老公|老婆|丈夫|妻子|太太|男朋友|女朋友|男友|女友|对象|對象)"
        r"|\byour\s+(?:husband|wife|boyfriend|girlfriend|partner|fiance|fiancé|fiancee|fiancée|bf|gf)\b", re.I),
     re.compile(r"老公|老婆|丈夫|妻子|太太|男朋友|女朋友|男友|女友|对象|對象|已婚|结婚|結婚"
                r"|\b(?:husband|wife|boyfriend|girlfriend|partner|fiance|fiancé|fiancee|fiancée|married|bf|gf)\b", re.I)),
    ("children", re.compile(
        r"(你|您|TA|ta)(的)?(女儿|女兒|儿子|兒子|孩子|小孩|娃|宝宝|寶寶)"
        r"|\byour\s+(?:daughter|son|kid|kids|child|children|baby|little\s+one)\b", re.I),
     re.compile(r"女儿|女兒|儿子|兒子|孩子|小孩|娃|宝宝|寶寶"
                r"|\b(?:daughter|son|kid|kids|child|children|baby)\b", re.I)),
    ("siblings", re.compile(
        r"(你|您|TA|ta)(的)?(哥|姐|弟|妹|兄弟|姐妹)|\byour\s+(?:brother|sister|bro|sis|siblings?)\b", re.I),
     re.compile(r"哥|姐|弟|妹|兄弟|姐妹|\b(?:brother|sister|bro|sis|siblings?)\b", re.I)),
    ("grandparents", re.compile(
        r"(你|您|TA|ta)(的)?(奶奶|外婆|姥姥|爷爷|爺爺|外公|姥爷)"
        r"|\byour\s+(?:grandma|grandmother|grandpa|grandfather|granny|nana)\b", re.I),
     re.compile(r"奶奶|外婆|姥姥|爷爷|爺爺|外公|姥爷"
                r"|\b(?:grandma|grandmother|grandpa|grandfather|granny|nana)\b", re.I)),
    ("boss", re.compile(
        r"(你|您|TA|ta)(的)?(老板|老闆|上司|领导|領導)|\byour\s+(?:boss|manager|supervisor)\b", re.I),
     re.compile(r"老板|老闆|上司|领导|領導|\b(?:boss|manager|supervisor)\b", re.I)),
    ("pet", re.compile(
        r"(你|您|TA|ta)(的|家)?(狗|猫|貓|宠物|寵物)|\byour\s+(?:dog|cat|puppy|kitten|pet|pup)\b", re.I),
     re.compile(r"狗|猫|貓|宠物|寵物|柯基|哈士奇|柴犬|金毛|拉布拉多|泰迪|布偶|英短|美短|橘猫"
                r"|\b(?:dog|cat|puppy|kitten|pet|pup|corgi|husky|shiba|golden|labrador|poodle|ragdoll)\b", re.I)),
    ("ex", re.compile(
        r"(你|您|TA|ta)(的)?(前任|前男友|前女友|前夫|前妻)|\byour\s+ex(?:-?(?:husband|wife|boyfriend|girlfriend))?\b", re.I),
     re.compile(r"前任|前男友|前女友|前夫|前妻|离婚|離婚|分手|\byour\s+ex\b|\bex(?:-?(?:husband|wife|boyfriend|girlfriend))?\b|\bdivorce", re.I)),
)

# 运营称谓泄漏到对客文案：无论有无事实一律拦（#341 *your client's mom*）
_PERSPECTIVE_LEAK_RE = re.compile(
    r"\byour\s+(?:client|customer)s?\b|(你|您)(的)?(客户|客戶|顾客)", re.I)


def detect_ungrounded_third_party(
    text: str, context_facts: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """文案点名了「对方的某个人」但该客户的事实集里没有这个实体 → 编造。

    返回 ``(是否编造, 证据摘要)``。零 IO、绝不抛。只看指向**对方**的第三方
    （你妈/your mom）；人设自己的家人（我妈/my mom）由人设档案管，这里不判。
    ``context_facts`` 应是**该会话**的全部可用事实（选中事实 + 背景事实）。
    """
    t = str(text or "")
    if not t.strip():
        return (False, "")
    if _PERSPECTIVE_LEAK_RE.search(t):
        return (True, "对客文案出现运营称谓「客户/your client」（视角泄漏）")
    facts_blob = " ".join(str(f or "") for f in (context_facts or []))
    for name, mention_re, support_re in _THIRD_PARTY_GROUPS:
        m = mention_re.search(t)
        if not m:
            continue
        if facts_blob and support_re.search(facts_blob):
            continue
        return (True, f"提到对方的「{m.group(0)}」({name}) 但记忆事实里无此人")
    return (False, "")


def detect_proactive_fabrication(
    text: str, context_facts: Optional[List[str]] = None,
    *, entity_facts: Optional[List[str]] = None,
    strict_when_facts: bool = False,
) -> Tuple[bool, str]:
    """主动开场出站前的合并判定：往事断言守卫 + 第三方实体守卫。

    ``context_facts`` 维持往事守卫的既有口径（背景事实）；``entity_facts`` 是实体守卫
    可用的全部事实（选中事实 + 背景事实），缺省等于 ``context_facts``。
    """
    fab, why = detect_fabricated_memory(
        text, context_facts, strict_when_facts=strict_when_facts)
    if fab:
        return (fab, why)
    return detect_ungrounded_third_party(
        text, context_facts if entity_facts is None else entity_facts)


__all__ = [
    "detect_ungrounded_third_party",
    "detect_proactive_fabrication",
    "PLACE_GAZETTEER",
    "normalize_country_code",
    "place_mentions",
    "known_days_from_profile",
    "detect_profile_contradiction",
    "past_claim_markers_hit",
    "detect_fabricated_memory",
    "build_reply_evidence",
    "build_precise_evidence",
    "strip_fabricated_sentences",
    "is_self_status_claim",
    "build_status_evidence",
    "persona_status_facts",
    "strip_status_claims",
    "history_status_claim_note",
    "record_fabrication_guard",
    "fabrication_counters",
]
