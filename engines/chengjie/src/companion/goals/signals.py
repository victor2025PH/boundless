"""目标结算/规划的「信号采集」——惰性读现有事实源，绝不新建状态。

设计：**settle-on-read（读时结算）**。不订阅事件总线、不加轮询任务——目标被读到
（右栏卡打开 / 注入 / 主动桥）那一刻，从既有单一事实源现取信号：

- intimacy / funnel_stage：``companion_context`` 进程级 provider（contacts 子系统）
- entitlement：``resolve_entitlement(chat_key)``（monetization；contact_key == 端用户 id）
- 最近入站时刻：inbox store（``protocol_bridge.get_inbox_store`` 进程级 getter）

任何一路缺失 → 对应字段维持「未知」哨兵值，消费方（ledger/planner）按保守行为
降级；本模块**绝不抛**。纯函数消费方只吃 ``GoalSignals``，可零 IO 单测。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("src.companion.goals.signals")


@dataclass
class GoalSignals:
    """结算/规划输入的统一信号快照（哨兵：intimacy=-1 未知；ts=0 未知）。"""

    now: float = 0.0
    intimacy: float = -1.0
    funnel_stage: str = ""
    entitlement: Optional[Dict[str, Any]] = None
    last_ts: float = 0.0            # 会话最后一条消息（任意方向）
    last_inbound_ts: float = 0.0    # 对方最后开口时刻
    negative_emotion: bool = False
    emotion_intensity: float = -1.0
    extras: Dict[str, Any] = field(default_factory=dict)


def entitlement_unlocked(ent: Optional[Dict[str, Any]], item_id: str) -> bool:
    """端用户是否已解锁某付费项（unlocked/grants 任一命中）。形状对齐
    ``companion_context.resolve_entitlement`` 的 ``{tier, grants, unlocked}``。"""
    if not isinstance(ent, dict) or not item_id:
        return False
    item = str(item_id)
    for key in ("unlocked", "grants"):
        val = ent.get(key)
        try:
            if isinstance(val, dict) and item in val:
                return True
            if isinstance(val, (list, tuple, set)) and item in val:
                return True
        except Exception:
            continue
    return False


def entitlement_tier(ent: Optional[Dict[str, Any]]) -> str:
    if not isinstance(ent, dict):
        return ""
    return str(ent.get("tier") or "").strip().lower()


def _last_inbound_ts(inbox_store: Any, conversation_id: str) -> float:
    """对方最后开口时刻：扫最近消息找 direction=='in' 的最大 ts。best-effort。"""
    if inbox_store is None or not conversation_id:
        return 0.0
    try:
        msgs = inbox_store.list_recent_messages(conversation_id, limit=30) or []
    except Exception:
        return 0.0
    best = 0.0
    for m in msgs:
        try:
            if str(m.get("direction") or "") != "in":
                continue
            ts = float(m.get("ts") or 0)
            if ts > best:
                best = ts
        except Exception:
            continue
    return best


def inbound_count_since(inbox_store: Any, conversation_id: str,
                        since_ts: float) -> int:
    """``since_ts`` 之后对方开口的条数（O-3 B 客户活跃判据：30min ≥5 条）。best-effort。"""
    if inbox_store is None or not conversation_id:
        return 0
    try:
        msgs = inbox_store.list_recent_messages(conversation_id, limit=40) or []
    except Exception:
        return 0
    n = 0
    for m in msgs:
        try:
            if str(m.get("direction") or "") != "in":
                continue
            if float(m.get("ts") or 0) >= float(since_ts):
                n += 1
        except Exception:
            continue
    return n


def collect_signals(
    *,
    platform: str,
    account_id: str,
    chat_key: str,
    conversation_id: str = "",
    inbox_store: Any = None,
    negative_emotion: bool = False,
    emotion_intensity: float = -1.0,
    now: Optional[float] = None,
) -> GoalSignals:
    """从现有事实源惰性采集信号快照。任何一路失败按未知降级，绝不抛。

    ``inbox_store`` 缺省时经 ``protocol_bridge.get_inbox_store()`` 进程级 getter
    兜底取（未注册 → None → 入站时刻未知）。
    """
    n = float(now if now is not None else time.time())
    sig = GoalSignals(now=n, negative_emotion=bool(negative_emotion),
                      emotion_intensity=float(emotion_intensity))
    try:
        from src.utils.companion_context import (
            resolve_entitlement,
            resolve_funnel_stage,
            resolve_intimacy_score,
        )
        if chat_key:
            v = resolve_intimacy_score(
                account_id or "default", chat_key, channel=platform or "telegram")
            if v is not None:
                sig.intimacy = float(v)
            st = resolve_funnel_stage(
                account_id or "default", chat_key, channel=platform or "telegram")
            if st:
                sig.funnel_stage = str(st).strip().lower()
            ent = resolve_entitlement(str(chat_key))
            if isinstance(ent, dict):
                sig.entitlement = ent
    except Exception:
        logger.debug("collect_signals: relationship providers failed", exc_info=True)

    store = inbox_store
    if store is None:
        try:
            from src.integrations.protocol_bridge import get_inbox_store
            store = get_inbox_store()
        except Exception:
            store = None
    if store is not None and conversation_id:
        try:
            conv = store.get_conversation(conversation_id) or {}
            sig.last_ts = float(conv.get("last_ts") or 0)
        except Exception:
            pass
        sig.last_inbound_ts = _last_inbound_ts(store, conversation_id)
        # O-3 B：近 30 分钟入站条数（planner 客户活跃自适应）
        sig.extras["inbound_30m"] = inbound_count_since(
            store, conversation_id, n - 1800.0)
    return sig


# ── Q-8 C/E（#264 #263）：「客户本条有没有新信息」纯判定 ──────────────────────────
# B9D8NW：四轮纯夸赞（you're beautiful / wow / love it）零新信息，AI 顺着夸回去、第五轮自己退场。
# 无新信息＝纯夸赞 / 应答词 / 表情 / 问候，且不含问句、不含数字、不含自述——保守判：
# 判不准算「有新信息」（宁漏不错，错判会把正常聊天推成 must）。
import re as _re

_NNI_WORDS = frozenset((
    # en praise / ack / filler
    "you", "u", "are", "re", "so", "very", "really", "such", "a", "an", "the", "look", "looks",
    "looking", "beautiful", "pretty", "cute", "gorgeous", "hot", "sexy", "lovely", "sweet",
    "amazing", "awesome", "perfect", "wonderful", "stunning", "nice", "good", "great", "cool",
    "wow", "omg", "lol", "haha", "hahaha", "hehe", "ok", "okay", "yes", "yeah", "yep", "no",
    "nope", "sure", "thanks", "thank", "thx", "ty", "love", "like", "it", "that", "this",
    "morning", "night", "evening", "afternoon", "hi", "hello", "hey", "babe", "baby", "dear",
    "honey", "my", "mine", "me", "too", "same", "true", "right", "indeed", "agree", "smile",
    "eyes", "face", "voice", "pic", "pics", "photo", "picture", "gm", "gn", "xoxo", "kiss",
    "kisses", "hug", "hugs", "miss", "and", "is", "am", "of", "to", "your", "ur", "wanna",
    "see", "more", "again", "always", "forever", "angel", "princess", "queen", "goddess",
    # zh praise / ack / filler（按字切，见 _cjk_only_praise）
))
_NNI_CJK = "你真好美漂亮可爱迷人性感甜漂亮好看棒赞哈嗯呢啊呀哦喔哇是的对好的行嗯嗯早安晚安午安晚上好早上好谢谢爱喜欢想亲抱笨蛋宝贝宝亲爱的女神仙女天使漂亮啦啊哈哈嘿嘻嘻嘿嘿么么哒吻抱抱一直永远都也真的很超太特别非常"
_EMOJI_RX = _re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2764\uFE0F\U0001F900-\U0001F9FF]+")
_WORD_RX = _re.compile(r"[A-Za-z']+")


def no_new_info(text: Any) -> bool:
    """本条是否**无新信息**（纯夸赞 / 应答 / 表情 / 问候）。含问号 / 数字 / 长句 → False。"""
    t = str(text or "").strip()
    if not t:
        return True
    if "?" in t or "？" in t or _re.search(r"\d", t):
        return False
    body = _EMOJI_RX.sub("", t)
    body = _re.sub(r"[\s\.,!！。，~～…'\"“”:;()（）\-]+", " ", body).strip()
    if not body:
        return True  # 纯表情 / 纯标点
    words = _WORD_RX.findall(body)
    cjk = [c for c in body if "\u4e00" <= c <= "\u9fff"]
    other = _re.sub(r"[A-Za-z'\s\u4e00-\u9fff]", "", body)
    if other.strip():
        return False  # 含其它文字（日 / 韩 / 数字符号）→ 判不准算有信息
    if len(words) > 8 or len(cjk) > 14:
        return False
    if words and any(w.lower().replace("'", "") not in _NNI_WORDS
                     and w.lower() not in _NNI_WORDS for w in words):
        return False
    if cjk and any(c not in _NNI_CJK for c in cjk):
        return False
    return True


def no_new_info_streak(history: Any) -> int:
    """会话最近**连续**无新信息的入站条数（从最新一条向前数；遇到我方出站不打断，遇到
    有信息的入站即停）。``history``＝``recent_history`` 形状 ``[{direction, text}]`` 时间正序。"""
    n = 0
    for m in reversed(list(history or [])):
        if not isinstance(m, dict) or str(m.get("direction") or "in") != "in":
            continue
        if no_new_info(m.get("text")):
            n += 1
        else:
            break
    return n


__all__ = [
    "GoalSignals",
    "collect_signals",
    "no_new_info",
    "no_new_info_streak",
    "entitlement_tier",
    "entitlement_unlocked",
    "inbound_count_since",
]
