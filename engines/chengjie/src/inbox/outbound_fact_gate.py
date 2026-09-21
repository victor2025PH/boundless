# -*- coding: utf-8 -*-
"""出站事实门（P0-1，2026-09-21）：自动回复真发前的「这句话有没有据」终检。

Bug 群 #341/#342 实锤：AI 在**普通自动回复**里问客户「你妈妈装修怎么样了」，而客户从未
提过妈妈——主动开场链已有 ``proactive_fabrication_guard`` 守着，但普通回复链一路到
``AutosendWorker._deliver_one`` 都没有任何一道「提到的人 / 事在这位客户这里有没有依据」
的闸门。本模块补上这道闸，口径与 ``fact_gate``（写入侧）对称：**写进记忆要有客户原句，
说出口要有客户原句或客户口述事实**。

判定（纯函数、零 IO、绝不抛）：
1. 视角泄漏：对客文案出现「your client / 你的客户」这类运营称谓 → 无条件拦；
2. 否认禁提：客户刚否认过某个「TA 的谁」（「我没说过 / I never said / what are you
   talking about」紧跟在一条点名该人的出站之后），随后 ``deny_turns`` 条出站内再提
   同一实体 → 拦（哪怕记忆里有——客户已经说了没有，再提就是顶嘴）；
3. 第三方实体无据：文案点名「你妈 / your daughter / 你老板…」，而客户的**全部入站原文**
   + **客户口述（user_stated）记忆事实**里都找不到这个人 → 拦。AI 推断（ai_inferred）
   的事实**不算依据**——这正是 provenance 分层的意义。

拦下之后：先让重写链「删掉无据的那部分」重写一次再复检；仍不过 → 不发、打「需人工」
（reason=fact_gate_blocked）、拦截台账写人话细节。绝不静默、绝不硬发。

支撑集来源：``rows`` 用 ``store.list_recent_messages(conv, limit=history_limit)``（direction=in
的 text），facts 由装配层闭包按 memory_key 精确召回（``facts_for_key(..., source="user_stated")``）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Pattern, Set, Tuple

logger = logging.getLogger(__name__)

REASON = "fact_gate_blocked"
DEFAULT_HISTORY_LIMIT = 200
DEFAULT_DENY_TURNS = 10

RewriteFn = Callable[[str, str], Awaitable[Optional[str]]]
FactsFn = Callable[[str, str, str], List[str]]

# 运营称谓漏进对客文案（#341 *your client's mom*）——无论有没有依据都不能发
_PERSPECTIVE_LEAK_RE = re.compile(
    r"\byour\s+(?:client|customer)s?\b|(你|您)(的)?(客户|客戶|顾客|顧客)", re.I)

# 客户否认「你说的那个人 / 那件事」——只认紧跟在点名出站之后的那条入站
_DENIAL_RE = re.compile(
    r"我(从|從)?(没|沒|未)(有)?(说|說|提|讲|講)(过|過)?"
    r"|我(哪|那)有|我没有(妈|媽|爸|女儿|女兒|儿子|兒子|孩子|老公|老婆|狗|猫|貓)"
    r"|(你|您)(在)?(说|說|讲|講)(什么|什麼|啥)|(谁|誰)是|认错人|認錯人"
    r"|\bi\s+never\s+(?:said|told|mentioned|talked)\b"
    r"|\bwhat\s+are\s+you\s+talking\s+about\b|\bwho(?:'s|\s+is)\s+that\b"
    r"|\bi\s+don'?t\s+have\s+(?:a|any)\s+\w+\b|\bwrong\s+person\b"
    r"|\byou(?:'re|\s+are)\s+(?:confusing|mixing)\s+me\b|\bnever\s+happened\b",
    re.I)


def _zh_en(zh: str, en: str) -> Pattern[str]:
    """文案里「指向对方的第三方」：中文要带「你/您/TA(的)」，英文要带 your。"""
    return re.compile(rf"(你|您|TA|ta)(的|家)?(?:{zh})|\byour\s+(?:{en})\b", re.I)


def _any(zh: str, en: str) -> Pattern[str]:
    """支撑集里的别名：客户自己提过就算（「我妈」「my mom」「mom」都算）。"""
    return re.compile(rf"(?:{zh})|\b(?:{en})\b", re.I)


# (实体组, 文案里指向对方第三方的正则, 支撑集里可作依据的别名正则)——与主动链守卫同口径
_THIRD_PARTY_GROUPS: Tuple[Tuple[str, Pattern[str], Pattern[str]], ...] = (
    ("mother", _zh_en("妈妈|媽媽|妈|媽|母亲|母親", "mom|mum|mother|mama|mommy"),
     _any("妈|媽|母亲|母親|父母", "mom|mum|mother|mama|mommy|parents")),
    ("father", _zh_en("爸爸|爸|父亲|父親", "dad|father|papa|daddy"),
     _any("爸|父亲|父親|父母", "dad|father|papa|daddy|parents")),
    ("parents", _zh_en("父母|爸妈|爸媽", "parents|folks"),
     _any("父母|爸妈|爸媽|妈|媽|爸", "parents|folks|mom|dad|mother|father")),
    ("partner", _zh_en("老公|老婆|丈夫|妻子|太太|男朋友|女朋友|男友|女友|对象|對象",
                       "husband|wife|boyfriend|girlfriend|partner|fiancé?e?|bf|gf"),
     _any("老公|老婆|丈夫|妻子|太太|男朋友|女朋友|男友|女友|对象|對象|已婚|结婚|結婚",
          "husband|wife|boyfriend|girlfriend|partner|fiancé?e?|married|bf|gf")),
    ("children", _zh_en("女儿|女兒|儿子|兒子|孩子|小孩|娃|宝宝|寶寶",
                        "daughter|son|kid|kids|child|children|baby|little\\s+one"),
     _any("女儿|女兒|儿子|兒子|孩子|小孩|娃|宝宝|寶寶", "daughter|son|kid|kids|child|children|baby")),
    ("siblings", _zh_en("哥|姐|弟|妹|兄弟|姐妹", "brother|sister|bro|sis|siblings?"),
     _any("哥|姐|弟|妹|兄弟|姐妹", "brother|sister|bro|sis|siblings?")),
    ("grandparents", _zh_en("奶奶|外婆|姥姥|爷爷|爺爺|外公|姥爷",
                            "grandma|grandmother|grandpa|grandfather|granny|nana"),
     _any("奶奶|外婆|姥姥|爷爷|爺爺|外公|姥爷", "grandma|grandmother|grandpa|grandfather|granny|nana")),
    ("boss", _zh_en("老板|老闆|上司|领导|領導", "boss|manager|supervisor"),
     _any("老板|老闆|上司|领导|領導", "boss|manager|supervisor")),
    ("pet", _zh_en("狗|猫|貓|宠物|寵物", "dog|cat|puppy|kitten|pet|pup"),
     _any("狗|猫|貓|宠物|寵物|柯基|哈士奇|柴犬|金毛|拉布拉多|泰迪|布偶|英短|美短|橘猫",
          "dog|cat|puppy|kitten|pet|pup|corgi|husky|shiba|golden|labrador|poodle|ragdoll")),
    ("ex", _zh_en("前任|前男友|前女友|前夫|前妻", "ex(?:-?(?:husband|wife|boyfriend|girlfriend))?"),
     _any("前任|前男友|前女友|前夫|前妻|离婚|離婚|分手", "ex(?:-?(?:husband|wife|boyfriend|girlfriend))?|divorced?")),
)


def _third_party_groups() -> Tuple[Tuple[str, Pattern[str], Pattern[str]], ...]:
    return _THIRD_PARTY_GROUPS


def mentioned_groups(text: str) -> Dict[str, str]:
    """文案里点名的「对方的第三方」：``{group: 命中原文}``。"""
    t = str(text or "")
    out: Dict[str, str] = {}
    if not t.strip():
        return out
    for name, mention_re, _support in _third_party_groups():
        m = mention_re.search(t)
        if m:
            out[name] = m.group(0)
    return out


def _sorted_rows(rows: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in list(rows or []):
        if not isinstance(r, dict):
            continue
        d = str(r.get("direction") or "")
        if d not in ("in", "out"):
            continue
        if d == "out" and str(r.get("status") or "") in ("failed", "resent"):
            continue
        out.append(r)
    out.sort(key=_row_ts)
    return out


def _row_ts(r: Dict[str, Any]) -> float:
    try:
        return float(r.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def peer_texts(rows: Optional[Iterable[Dict[str, Any]]]) -> List[str]:
    """客户入站原文（支撑集的第一来源）。"""
    return [str(r.get("text") or "") for r in _sorted_rows(rows)
            if str(r.get("direction") or "") == "in" and str(r.get("text") or "").strip()]


def denied_groups(rows: Optional[Iterable[Dict[str, Any]]], *,
                  deny_turns: int = DEFAULT_DENY_TURNS) -> Set[str]:
    """客户已否认、且否认后出站不足 ``deny_turns`` 条的第三方实体组。

    判据：某条入站命中否认句式，且它前面最近一条出站点名了实体 G → G 进禁提期，
    此后每条出站消耗一格；耗尽解禁。纯函数绝不抛。
    """
    turns = max(1, int(deny_turns or DEFAULT_DENY_TURNS))
    active: Dict[str, int] = {}
    last_out_groups: Dict[str, str] = {}
    for r in _sorted_rows(rows):
        text = str(r.get("text") or "")
        if str(r.get("direction") or "") == "out":
            for g in list(active):
                active[g] -= 1
                if active[g] <= 0:
                    del active[g]
            last_out_groups = mentioned_groups(text)
            continue
        if last_out_groups and _DENIAL_RE.search(text):
            for g in last_out_groups:
                active[g] = turns
        last_out_groups = {}
    return set(active)


def check(text: str, rows: Optional[Iterable[Dict[str, Any]]],
          facts: Optional[Iterable[str]] = None, *,
          deny_turns: int = DEFAULT_DENY_TURNS) -> Optional[Dict[str, Any]]:
    """终检。返回 ``None``＝放行；否则 ``{kind, why, hit}``（``why`` 是给人看的一句）。

    kind ∈ perspective_leak / denied_recall / ungrounded_third_party。
    """
    t = str(text or "")
    if not t.strip():
        return None
    try:
        m = _PERSPECTIVE_LEAK_RE.search(t)
        if m:
            return {"kind": "perspective_leak", "hit": m.group(0),
                    "why": f"对客文案出现运营称谓「{m.group(0)}」"}
        groups = mentioned_groups(t)
        if not groups:
            return None
        denied = denied_groups(rows, deny_turns=deny_turns) & set(groups)
        if denied:
            g = sorted(denied)[0]
            return {"kind": "denied_recall", "hit": groups[g],
                    "why": f"客户刚否认过「{groups[g]}」这个人，{int(deny_turns)} 轮内不再提"}
        support = " ".join(peer_texts(rows) + [str(f or "") for f in (facts or [])])
        for name, _mention_re, support_re in _third_party_groups():
            if name not in groups:
                continue
            if support and support_re.search(support):
                continue
            return {"kind": "ungrounded_third_party", "hit": groups[name],
                    "why": f"提到对方的「{groups[name]}」，但客户从未说过有这个人"}
    except Exception:
        logger.debug("[outbound_fact_gate] 判定异常（放行）", exc_info=True)
    return None


def build_rewrite_prompt(hit: Dict[str, Any]) -> str:
    """重写指令：删掉无据的那部分，其余保语言 / 保语气 / 保长度。"""
    who = str((hit or {}).get("hit") or "").strip()[:60]
    kind = str((hit or {}).get("kind") or "")
    if kind == "perspective_leak":
        what = f"「{who}」是运营内部称谓，不能出现在给对方的话里；把它去掉或改成直接称呼对方"
    else:
        what = (f"这句话提到了对方的「{who}」，但对方从没说过有这么一个人，属于编造；"
                f"删掉与「{who}」有关的所有内容，不要用别的人物替代")
    return (
        "你是聊天回复改写器。" + what + "。其余部分保持原意和语气，"
        "**用与原文完全相同的语言**，长度接近原文，绝不解释、绝不加引号、只输出改写后的话。"
    )


def resolve_cfg(root_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``inbox.outbound_fact_gate``（默认**开**——它拦的是「编造对方的家人」这类
    一句就穿帮的事故；``enabled:false`` 为紧急停用开关）。"""
    try:
        blk = dict(((root_config or {}).get("inbox") or {}).get("outbound_fact_gate") or {})
    except Exception:
        blk = {}
    return {
        "enabled": bool(blk.get("enabled", True)),
        "history_limit": int(blk.get("history_limit", DEFAULT_HISTORY_LIMIT)),
        "deny_turns": int(blk.get("deny_turns", DEFAULT_DENY_TURNS)),
        "rewrite_retry": bool(blk.get("rewrite_retry", True)),
    }


def attach_sources(cfg: Optional[Dict[str, Any]], *, ai_client: Any = None,
                   skill_manager: Any = None) -> Optional[Dict[str, Any]]:
    """装配层：挂 ``rewrite_fn``（ai_client.rewrite_local）与 ``facts_fn``（按会话精确召回
    客户口述事实）。任一缺席只是少一种能力，不影响门本身。返回拷贝。"""
    if not isinstance(cfg, dict):
        return cfg
    out = dict(cfg)
    rewrite_local = getattr(ai_client, "rewrite_local", None) if ai_client is not None else None
    if rewrite_local is not None:
        async def _rewrite(text: str, prompt: str) -> Optional[str]:
            return await rewrite_local(prompt, str(text))
        out["rewrite_fn"] = _rewrite
    epi = getattr(skill_manager, "_episodic_store", None) if skill_manager is not None else None
    key_fn = getattr(skill_manager, "_episodic_storage_key", None) if skill_manager is not None else None
    if epi is not None and key_fn is not None:
        def _facts(platform: str, account_id: str, chat_key: str) -> List[str]:
            try:
                mkey = str(key_fn(chat_key, "", platform, account_id) or "").strip()
                if not mkey:
                    return []
                rows = list(epi.list_rows(prefix=mkey, limit=80, source="user_stated") or [])
                # 前缀搜是 LIKE：带 memory_key 的行必须精确等于本会话的键，防别人的事实漏进支撑集
                return [str(r.get("content") or "") for r in rows
                        if isinstance(r, dict)
                        and str(r.get("memory_key") or mkey).strip() == mkey]
            except Exception:
                return []
        out["facts_fn"] = _facts
    return out


__all__ = [
    "REASON", "DEFAULT_HISTORY_LIMIT", "DEFAULT_DENY_TURNS",
    "mentioned_groups", "peer_texts", "denied_groups", "check",
    "build_rewrite_prompt", "resolve_cfg", "attach_sources",
]
