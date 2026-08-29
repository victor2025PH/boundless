# -*- coding: utf-8 -*-
"""AI 自述一致性锚点（P1-198，问题 6：自己说过的话前后矛盾/记不准）。

实锤背景（2026-07-31，198 坐席机，Steven 人设 × 英文客户）：AI 在会话里亲口
说过「Divorced, actually」「Just me and my daughter, Maya. She's eight」，隔了
几十条消息再被问到同类话题时表述漂移（情景记忆**刻意**只记用户侧事实——
Phase8 反幻觉设计，AI 自述无处落地；人设 bio 检索按当前消息关键词走，漏检
时自述事实就缺位）。

设计（与 Phase8 记忆接地同哲学：宁可漏记不可错记，且**只引用不解释**）：
- 纯函数扫描**assistant 侧**历史消息，按槽位词表（年龄/婚姻/家庭/职业/居住）
  挑出第一人称事实句，**原句引用**注入提示——不解析、不改写，否定句
  （"我不是单身"）天然保真，误命中的代价只是多引一句原话；
- 每槽位保留**最新**一句（客户最近听到的版本；若已发生漂移，锚定最新版
  至少止住继续翻烙饼）；
- 零存储零迁移：证据窗口=调用方传入的会话历史（工作台/自动草稿链天然携带）。
  跨会话长期一致性仍是人设 bio 的职责，这里治的是「同一会话内说变卦」。

消费口：``apply_inbound_enrichments`` 汇入 ``_topic_switch_hint``（与语言锚点
同通道），零 ai_client 改动。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# 句级切分（中英标点 + 换行）；保留问号句以便剔除（问句不是自述）
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?\n])|(?<=[.](?=\s))")

# 槽位 → 匹配模式（保守：第一人称 + 高置信事实词；开放式措辞刻意不收，
# 宁可漏——漏了只是没锚，错了会把闲聊句当事实反复注入）
_SLOT_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("age", re.compile(
        r"我今年\s*\d{1,2}\s*岁|我\s*\d{1,2}\s*岁|"
        r"\bI'?m\s+\d{1,2}\s*(?:years?\s+old|myself)?\b"
        r"(?!\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|km|miles?|kg|"
        r"blocks?|floors?|am|pm|%|:))|"
        r"\bI am\s+\d{1,2}\s*(?:years?\s+old)?\b"
        r"(?!\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|km|miles?|kg|"
        r"blocks?|floors?|am|pm|%|:))", re.IGNORECASE)),
    ("marital", re.compile(
        r"我?(离过婚|离婚了|已经离婚|现在(是)?单身|还(是)?单身|结过婚|结婚了|"
        r"没(有)?结过?婚|未婚)|"
        r"\bI'?m\s+(divorced|single|married|separated)\b|"
        r"\bdivorced,?\s+actually\b|\bI got divorced\b|\bnever (been )?married\b",
        re.IGNORECASE)),
    ("family", re.compile(
        r"我(有|带着)(一个|个|两个)?(女儿|儿子|孩子|娃)|我(女儿|儿子)叫|"
        r"我(前妻|前夫|妹妹|哥哥|弟弟|姐姐)|"
        r"\bmy\s+(daughter|son|kid|kids|ex[- ]?wife|ex[- ]?husband|sister|brother)\b",
        re.IGNORECASE)),
    ("job", re.compile(
        r"我(是|做|经营|开|管理)着?[^，。！？\n]{0,14}"
        r"(公司|工作室|基金|投资|生意|餐厅|店|设计师|工程师|律师|医生|老师|顾问)|"
        r"\bI\s+(run|own|manage|founded)\s+(a|my|an)\b|"
        r"\bI work\s+(as|at|in)\b|\bmy (investment )?(firm|company|studio|business)\b",
        re.IGNORECASE)),
    ("home", re.compile(
        r"我(住在|定居在?|长期在|老家在?|现在在)[^，。！？\n]{2,12}"
        r"(生活|定居|工作)?|"
        r"\bI (live|stay|am based)\s+in\b|\bI moved to\b", re.IGNORECASE)),
    # 名字槽**刻意不带 IGNORECASE**：英文名依赖「首字母大写」启发区分
    # "I'm Steven"（自述名字）与 "I'm sure/glad"（口语短语）。
    ("name", re.compile(
        r"我(叫|是)[\u4e00-\u9fffA-Za-z·\s]{2,12}(?:，|。|！|$)|"
        r"\bI'?m\s+[A-Z][a-z]{2,12}\b(?!\s+(?:sure|sorry|glad|fine|good|okay|ok|"
        r"here|just|not|so|really|still|always|a\b|an\b|the\b))|"
        r"\b[Ii]t'?s\s+[A-Z][a-z]{2,12}\b(?!\s+(?:been|time|okay|ok|fine|all|"
        r"just|not|so|really|a\b|an\b|the\b))|"
        r"\b[Tt]his is\s+[A-Z][a-z]{2,12}\b")),
]

_MAX_SCAN_MSGS = 30       # 只扫最近 N 条 assistant 消息（成本与相关性平衡）
_MAX_CLAIMS = 6           # 注入上限（防 prompt 膨胀）
_MAX_SENT_CHARS = 80      # 单句截断
_QUESTION_TAIL = ("？", "?")


def _sentences(text: str) -> List[str]:
    out: List[str] = []
    for seg in _SENT_SPLIT_RE.split(str(text or "")):
        s = (seg or "").strip()
        if s:
            out.append(s)
    return out


def extract_self_claims(history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """从会话历史抽 AI 自述事实句（assistant 侧、槽位命中、非问句）。

    返回 ``[{slot, text}]``，每槽位取**最新**命中句、按首次出现槽位序排列，
    整体截到 ``_MAX_CLAIMS``。纯函数、绝不抛。
    """
    latest: Dict[str, str] = {}
    order: List[str] = []
    try:
        msgs = [m for m in list(history or [])
                if isinstance(m, dict) and m.get("role") == "assistant"]
        for m in msgs[-_MAX_SCAN_MSGS:]:
            for sent in _sentences(str(m.get("content") or "")):
                if sent.endswith(_QUESTION_TAIL):
                    continue  # 问句不是自述（"你结婚了吗？"）
                for slot, rx in _SLOT_PATTERNS:
                    if rx.search(sent):
                        clipped = sent[:_MAX_SENT_CHARS]
                        if slot not in latest:
                            order.append(slot)
                        latest[slot] = clipped  # 后出现覆盖 → 最新版本
                        break  # 一句只归一个槽（首个命中），防重复引用
    except Exception:
        return []
    return [{"slot": s, "text": latest[s]} for s in order if s in latest][:_MAX_CLAIMS]


def build_self_claims_hint(history: List[Dict[str, Any]]) -> str:
    """自述锚点提示（无命中返回 ""）。原句引用，不解释、不改写。"""
    claims = extract_self_claims(history)
    if not claims:
        return ""
    lines = "\n".join(f"- {c['text']}" for c in claims)
    return (
        "【自述一致性——锚定】你此前在本会话里亲口说过（原话摘录）：\n"
        f"{lines}\n"
        "这些是对方已经听到的你的个人情况。再谈到相关话题时保持一致，"
        "不要说出与之矛盾的版本；记不清细节时宁可说得模糊，也不要临时编一个新版本。"
    )


# ── #62（2026-08-30）：近时自述——即时行为 / 时间承诺 ─────────────────────────
# 实锤两例（钧 0830，截图在单）：01:32「正泡了杯茶发呆」→ 02:03「大半夜的哪能
# 喝茶，刚泡了杯白水」31 分钟内当客户面自我否认，客户正是冲着那杯茶发问；
# 03:29「下个月应该会过去一趟，到时候提前跟你约」→ 03:39「你看这周哪天方便？」
# 10 分钟内推翻自己的时间安排。上面的槽位锚点只覆盖**长期**个人事实（年龄/婚姻
# /职业…），「在喝什么/说好什么时候见」这类**会过期的**自述不在其内——但过期
# 恰恰是双刃：拿三天前的「正泡着茶」当锚点会制造反向事故（AI 三天后还坚称在喝
# 那杯茶）。所以本段带**新鲜度窗口**：
#   · activity（即时行为）：有 ts 限 6h；无 ts（A 线 _conversation_history 不带
#     时间戳）退化为「最近 3 条 assistant 消息」的位置近因；
#   · plan（时间承诺）：有 ts 限 72h；无 ts 限最近 12 条 assistant 消息。
# 与主锚点同哲学：只引用不解释、每类取最新一句（说法变过就锚最新版，至少止住
# 继续翻烙饼）、宁可漏勿错锚。

_ACTIVITY_WINDOW_SEC = 6 * 3600.0
_PLAN_WINDOW_SEC = 72 * 3600.0
_ACTIVITY_POS_WINDOW = 3   # 无 ts 时 activity 只认最近 N 条 assistant 消息
_PLAN_POS_WINDOW = 12

# 即时行为：第一人称 + 进行/刚完成态动词（刻意不收「做/开/经营」这类会与 job
# 槽双重引用的动词；做饭/做菜按字面单列）。饮品「泡/沏/煮了杯X」结构在聊天语境
# 强指第一人称，允许省略「我」。
_TRANSIENT_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("activity", re.compile(
        r"我[^，。！？!?\n]{0,6}(?:刚刚?|正在?|在)"
        r"(?:泡|沏|煮|喝|吃|热|炖|忙|看|听|收工|加班|散步|遛|洗|做饭|做菜|发呆)|"
        r"(?:泡|沏|煮)了杯[^，。！？!?\n]{0,8}|"
        r"\bI'?m\s+(?:just\s+)?(?:having|drinking|sipping|brewing|making|"
        r"eating|cooking|watching|listening to)\b", re.IGNORECASE)),
    ("plan", re.compile(
        r"我[^，。！？!?\n]{0,10}(?:下个?月|下下?周|这?周末|明后天|明天|后天|"
        r"过几天|过阵子|月底|月初|年底)[^，。！？!?\n]{0,16}"
        r"(?:过去|过来|去|来|约|见)|"
        r"(?:下个?月|下下?周|这?周末|明后天|明天|后天|过几天|过阵子|月底|年底)"
        r"[^，。！？!?\n]{0,10}(?:过去|过来|去找你|来看你|去看你|约你?|见面?)|"
        r"(?:到时候|回头|等我?忙完)[^，。！？!?\n]{0,10}约|"
        r"\bI(?:'ll| will|'m going to| plan to| might| should)\b"
        r"[^,.!?\n]{0,40}\b(?:next month|next week|this weekend|tomorrow|"
        r"in a few days)\b", re.IGNORECASE)),
]


def _ago_phrase(sec: float) -> str:
    """秒差 → 人话时距（<90s=刚刚；分钟；小时；天）。"""
    if sec < 90:
        return "刚刚"
    if sec < 3600:
        return f"约 {int(sec // 60)} 分钟前"
    if sec < 86400:
        return f"约 {int(sec // 3600)} 小时前"
    return f"约 {int(sec // 86400)} 天前"


def extract_recent_self_statements(
    history: List[Dict[str, Any]], *, now: float = 0.0,
) -> List[Dict[str, Any]]:
    """近时自述（即时行为/时间承诺），过新鲜度窗口后每类取最新一句。

    返回 ``[{kind, text, ago_sec}]``（``ago_sec`` 无 ts 时为 None）。
    纯函数、绝不抛；行内 ``ts``（epoch 秒）可选——B 线 normalize_history 透传，
    A 线 _conversation_history 无 ts 走位置近因。
    """
    import time as _t
    now = float(now) if now else _t.time()
    latest: Dict[str, Dict[str, Any]] = {}
    try:
        msgs = [m for m in list(history or [])
                if isinstance(m, dict) and m.get("role") == "assistant"]
        msgs = msgs[-_MAX_SCAN_MSGS:]
        total = len(msgs)
        for idx, m in enumerate(msgs):
            pos_from_end = total - idx  # 1 = 最新一条
            try:
                mts = float(m.get("ts") or 0)
            except (TypeError, ValueError):
                mts = 0.0
            ago = (now - mts) if mts > 0 else None
            for sent in _sentences(str(m.get("content") or "")):
                if sent.endswith(_QUESTION_TAIL):
                    continue
                for kind, rx in _TRANSIENT_PATTERNS:
                    if not rx.search(sent):
                        continue
                    if kind == "activity":
                        if ago is not None:
                            if ago > _ACTIVITY_WINDOW_SEC:
                                continue
                        elif pos_from_end > _ACTIVITY_POS_WINDOW:
                            continue
                    else:
                        if ago is not None:
                            if ago > _PLAN_WINDOW_SEC:
                                continue
                        elif pos_from_end > _PLAN_POS_WINDOW:
                            continue
                    latest[kind] = {"kind": kind,
                                    "text": sent[:_MAX_SENT_CHARS],
                                    "ago_sec": ago}
                    break  # 一句只归一类，防同句双引
    except Exception:
        return []
    return [latest[k] for k in ("activity", "plan") if k in latest]


def build_recent_self_statement_hint(
    history: List[Dict[str, Any]], *, now: float = 0.0,
) -> str:
    """「你刚说过」锚点（无命中返回 ""）。带时距的原句引用 + 自洽指令。"""
    stmts = extract_recent_self_statements(history, now=now)
    if not stmts:
        return ""
    lines = []
    for s in stmts:
        when = _ago_phrase(float(s["ago_sec"])) if s["ago_sec"] is not None else "刚才"
        lines.append(f"-（{when}）「{s['text']}」")
    body = "\n".join(lines)
    return (
        "【你刚说过——保持自洽】你最近在本会话里亲口说过（原话摘录）：\n"
        f"{body}\n"
        "对方听到了这些话，很可能正顺着它们追问。不要否认或凭空推翻：说过在做"
        "的事别转头说没做（如说过在泡茶就不能答「哪能喝茶」）；给过的时间安排"
        "别立刻改口（如说好下个月见就别再问这周哪天）。情况真的变了就自然地"
        "衔接说明，别装作从没说过。"
    )


__all__ = [
    "extract_self_claims", "build_self_claims_hint",
    "extract_recent_self_statements", "build_recent_self_statement_hint",
]
