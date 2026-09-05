"""J-8 #182：手动排关怀的「语义护栏」纯函数（零 IO、零依赖）。

事故：运营在「什么事」填「主动问候对方早上好」，AI 把它当成**客户的事**——
「客户承诺要向某人问早安」，凭空生出第三人、还把无关记忆（人鱼潘）塞进话术。
根因两处：① 表单只有一种语义（客户生活事件），运营写的却是**给 AI 的指令**；
② care prompt 把该联系人的近期记忆整块喂给 LLM，不按事件相关性筛。

本模块三件纯函数，派发器与预览端点、路由三处共用同一口径（预判＝行为）：

- ``detect_instruction_intent(text)``：这段话像不像「给 AI 的指令 / 要发出去的话」
  （而不是客户的事）。保守词表：命中＝提示切「原文直发」，不命中不打扰。
- ``filter_relevant_memory(memory_block, *ref_texts)``：记忆要点逐条与事件文本做
  内容级词汇重叠（CJK bigram / 拉丁词），无重叠的条目剔掉；一条不剩 → ""（整块
  不注入）。「宁可不引用记忆，也不引用不相关的记忆」。
- ``care_understanding(item, now)``：预览前先回显「我理解为：客户 <何时> 会 <做什么>」
  的结构化字段，理解错了运营当场看得见。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ── ① 指令意图识别 ─────────────────────────────────────────────────────────
# 句首指令动词（给 AI 的动作）。刻意不收「复查/提车/搬家/体检」这类客户事件名词，
# 也不收孤立单字「发/说/问」（「发烧」「说明会」会误伤），全部要求动词+宾语/助词形态。
_LEAD_VERBS = (
    "主动", "提醒", "告诉", "通知", "催", "帮我", "请你", "请", "记得", "麻烦",
    "发一条", "发条", "发个", "发消息", "发信息", "发句", "发一句", "发点",
    "说一句", "说句", "说声", "说一声",
    "问一下", "问问", "问一问", "问候", "问一句", "问声", "问一声",
    "打个招呼", "打招呼", "招呼",
    "祝他", "祝她", "祝TA", "祝ta", "祝对方", "祝客户", "祝福",
    "让他", "让她", "让TA", "让对方", "让客户",
    "给他", "给她", "给TA", "给对方", "给客户",
    "跟他", "跟她", "跟TA", "跟对方", "跟客户",
    "和他", "和她", "和对方", "和客户",
    "向他", "向她", "向对方", "向客户",
    "回访", "跟进一下", "关心一下", "问候一下", "慰问",
)
_LEAD_VERBS_EN = (
    "tell ", "remind ", "send ", "say ", "wish ", "ask ", "greet ", "message ",
    "text ", "ping ", "notify ", "follow up", "check in", "please ", "let them",
)
# 「客户/对方/他/她 + 直接要发的话」——「对方早上好」「客户晚安」这种把对象和
# 问候语拼在一起的写法，几乎必是「要发给 TA 的话」而非 TA 的事。
_TARGET_PLUS_GREETING = re.compile(
    r"(对方|客户|他|她|TA|ta)\s*[，,:：]?\s*"
    r"(早上好|早安|晚安|晚上好|中午好|下午好|你好|您好|加油|节日快乐|生日快乐|新年快乐|辛苦了|注意休息)"
)
# 直接引号包住的整句 =「要发这句话」
_QUOTED = re.compile(r"^[「“\"']\s*.{2,}\s*[」”\"']\s*$")
# 客户事件的强特征——命中则**不**判指令（给否定优先权，防误伤）
_EVENT_NOUN_HINT = re.compile(
    r"(面试|复查|体检|手术|考试|答辩|提车|搬家|旅行|出差|生日|开学|毕业|结婚|"
    r"入职|离职|出院|住院|签约|比赛|演出|汇报|开庭|路考|见家长|相亲|约会|"
    r"interview|exam|surgery|check-?up|birthday|trip|moving|wedding)"
)


def detect_instruction_intent(text: str) -> Dict[str, Any]:
    """「什么事」这一栏填的是不是给 AI 的指令 / 要发出去的话。

    返回 ``{"looks_like_instruction": bool, "reason": str}``。reason ∈
    ``lead_verb`` / ``target_greeting`` / ``quoted`` / ``""``。保守：客户事件名词
    在场且没有句首指令动词 → 不判（「提醒我复查」仍算指令，因为句首是「提醒」）。
    """
    t = str(text or "").strip()
    if not t:
        return {"looks_like_instruction": False, "reason": ""}
    low = t.lower()
    if _QUOTED.match(t):
        return {"looks_like_instruction": True, "reason": "quoted"}
    for v in _LEAD_VERBS:
        if t.startswith(v):
            return {"looks_like_instruction": True, "reason": "lead_verb"}
    for v in _LEAD_VERBS_EN:
        if low.startswith(v):
            return {"looks_like_instruction": True, "reason": "lead_verb"}
    # 「对方早上好」式：对象+问候语拼在一起；但若同句带客户事件名词
    # （「她生日快乐那天要回老家」）则按客户的事放行——事件名词有否定优先权。
    if _TARGET_PLUS_GREETING.search(t) and not _EVENT_NOUN_HINT.search(t):
        return {"looks_like_instruction": True, "reason": "target_greeting"}
    return {"looks_like_instruction": False, "reason": ""}


# ── ② 记忆相关性过滤 ────────────────────────────────────────────────────────
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_NUM_RE = re.compile(r"[A-Za-z][A-Za-z\-']{1,}|\d{2,}")
# 虚词/代词/高频功能字：含这些字的 bigram 不算内容重叠（「对方的」「了一」这类
# 到处都有，拿它们算相关等于没过滤）。
_FUNC_CHARS = set(
    "的了是在我你他她它们这那个把被和与就都也很会要去来说吗呢啊吧对着过给"
    "让从到又还有没不为以及或者但如果因为所以什么怎么时候一二三两几多少些"
    "上下前后里外中间已经正在可以可能应该觉得知道想要喜欢"
)
# 整词级停用（这些词本身是完整 bigram，但对「相关性」零信息量）
_STOP_BIGRAMS = {
    "对方", "客户", "用户", "今天", "明天", "昨天", "后天", "最近", "现在",
    "提到", "说过", "表示", "准备", "打算", "计划", "希望", "感觉", "自己",
}


def _content_tokens(text: str) -> Tuple[set, set]:
    bigrams: set = set()
    for run in _CJK_RUN_RE.findall(str(text or "")):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg[0] in _FUNC_CHARS or bg[1] in _FUNC_CHARS:
                continue
            if bg in _STOP_BIGRAMS:
                continue
            bigrams.add(bg)
    latin = {t.lower() for t in _LATIN_NUM_RE.findall(str(text or ""))}
    return bigrams, latin


def memory_line_relevant(line: str, ref_bigrams: set, ref_latin: set) -> bool:
    """单条记忆要点与事件文本是否有内容级重叠（≥1 个内容 bigram 或拉丁词）。"""
    b, l = _content_tokens(line)
    return bool(b & ref_bigrams) or bool(l & ref_latin)


def filter_relevant_memory(memory_block: str, *ref_texts: str) -> str:
    """把记忆块按事件文本相关性筛：保留有词汇重叠的行；一条不剩 → ""。

    ``memory_block`` 为 ``get_bullets_for_prompt`` 输出的多行要点（每行一条，
    可带 ``- `` / ``• `` 前缀）。``ref_texts``＝事件 topic + source_text 等。
    事件文本本身没有内容词（如全是虚词）→ 无从判相关，**整块不注入**（保守）。
    """
    block = str(memory_block or "").strip()
    if not block:
        return ""
    ref_b: set = set()
    ref_l: set = set()
    for t in ref_texts:
        b, l = _content_tokens(t)
        ref_b |= b
        ref_l |= l
    if not ref_b and not ref_l:
        return ""
    kept: List[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if memory_line_relevant(line, ref_b, ref_l):
            kept.append(raw.rstrip())
    return "\n".join(kept).strip()


# ── ③ 理解回显 ──────────────────────────────────────────────────────────────
def _when_words(event_at: float, now: float) -> str:
    try:
        ev = datetime.fromtimestamp(float(event_at)).date()
        nd = datetime.fromtimestamp(float(now)).date()
    except Exception:
        return "最近"
    delta = (ev - nd).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    if delta == -1:
        return "昨天"
    if delta < -1:
        return "前几天"
    if 2 < delta <= 7:
        return "这周"
    return "之后"


def care_understanding(item: dict, now: Optional[float] = None) -> Dict[str, Any]:
    """预览前的「我理解为」结构化字段（前端按 i18n 拼句）。

    返回 ``{"mode": "event"|"verbatim", "topic", "when", "event_at", "source_text",
    "looks_like_instruction"}``。verbatim 行 topic 即摘要、source_text 即原文。
    """
    import time as _t
    from src.contacts.care_schedule import care_verbatim_text, is_verbatim_care

    n = float(now if now is not None else _t.time())
    it = dict(item or {})
    if is_verbatim_care(it):
        return {
            "mode": "verbatim",
            "topic": str(it.get("topic") or ""),
            "when": _when_words(float(it.get("due_at") or n), n),
            "event_at": float(it.get("due_at") or 0),
            "source_text": care_verbatim_text(it),
            "looks_like_instruction": False,
        }
    topic = str(it.get("topic") or "").strip()
    return {
        "mode": "event",
        "topic": topic,
        "when": _when_words(float(it.get("event_at") or it.get("due_at") or n), n),
        "event_at": float(it.get("event_at") or it.get("due_at") or 0),
        "source_text": str(it.get("source_text") or "")[:200],
        "looks_like_instruction": bool(
            detect_instruction_intent(topic)["looks_like_instruction"]),
    }


__all__ = [
    "detect_instruction_intent", "filter_relevant_memory", "memory_line_relevant",
    "care_understanding",
]
