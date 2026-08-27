"""B52 人设自述状态短期记忆（2026-08-23 实施64 P1-2，`_287` 实录）。

事故：AI 上一轮亲口说「我先去睡了」，几小时后 proactive 开场却像没说过一样
（甚至自相矛盾「刚健身回来」）。人设的**自述近况**是对话事实，后续消息必须
衔接或自然过渡（睡醒了/回来了），不能装失忆。

设计（与 ``_media_sent_log`` 同哲学）：
- 捕获＝出站单点 ``_update_after_reply``（A/B 两线共经）跑保守正则，认得出的
  高置信状态才记（宁漏勿错）；
- 存储＝``user_context["_self_state_log"]``（bounded 3，随 ContextStore 持久化）；
- 消费＝每轮注入 ``_self_state_block``（在 TTL 窗内才注入；睡觉窗更长），
  proactive 开场链同源消费。

纯函数、零 LLM、零新存储。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

#: 状态 → (识别正则, 生效窗秒)。窗过了自然不再注入（睡一觉 8h、其余 2-3h）。
#: 正则刻意窄：只认第一人称、明确宣告型自述；否定/疑问/转述不碰。
_STATES = (
    ("sleep", re.compile(
        r"(?:我?(?:先|要|去|准备|得)去?睡(?:了|觉|啦)|我?困了.{0,6}睡|晚安)"), 9 * 3600),
    ("gym", re.compile(
        r"我?(?:先|要|去|准备)去?(?:健身|撸铁|跑步|锻炼)"), 3 * 3600),
    ("shower", re.compile(r"我?(?:先|要|去|准备)去?(?:洗澡|冲个澡|洗漱)"), 2 * 3600),
    ("work", re.compile(
        r"我?(?:先|要|去|准备)去?(?:上班|开会|忙工作|加班)(?:了|啦)?"), 4 * 3600),
    ("cook", re.compile(r"我?(?:先|要|去|准备)去?(?:做饭|煮饭|烧菜)"), 2 * 3600),
    ("out", re.compile(r"我?(?:先|要|准备)出(?:门|去)(?:了|啦|一趟)"), 3 * 3600),
)

#: 状态的人话名（注入块用；后续衔接由 LLM 自然发挥，不硬编话术）。
_STATE_LABEL = {
    "sleep": "去睡觉", "gym": "去健身", "shower": "去洗澡",
    "work": "去忙工作", "cook": "去做饭", "out": "出门",
}

#: 排除面：否定 / 疑问 / 说的是对方（「你去睡吧」「你先睡」）。
_NEG_RE = re.compile(r"不(?:去|想|用|准备)|别睡|还不睡|睡不着|[吗么呢?？]\s*$")
_PEER_RE = re.compile(r"^(?:你|妳|寶|宝|亲)|你(?:先|去|快去|也早点)?(?:睡|洗|忙)")

LOG_KEY = "_self_state_log"
_LOG_CAP = 3


def extract_self_state(reply: str) -> Optional[Dict[str, Any]]:
    """出站回复 → 自述状态条目 ``{state, phrase}``；认不出/被排除 → None。"""
    t = str(reply or "").strip()
    if not t or len(t) > 400:
        return None
    for state, pat, _ttl in _STATES:
        m = pat.search(t)
        if not m:
            continue
        # 命中片段所在句：句级排除否定/说对方（整条排除会误杀
        # 「你先忙，我去睡了」这类合法复合句）。
        seg_start = max(t.rfind(p, 0, m.start()) for p in ("。", "！", "？", "!", "?", "\n"))
        seg_end_candidates = [t.find(p, m.end()) for p in ("。", "！", "？", "!", "?", "\n")]
        seg_end_candidates = [x for x in seg_end_candidates if x >= 0]
        seg = t[seg_start + 1: min(seg_end_candidates) if seg_end_candidates else len(t)]
        if _NEG_RE.search(seg) or _PEER_RE.search(seg.strip()):
            continue
        return {"state": state, "phrase": seg.strip()[:60] or m.group(0)[:60]}
    return None


def record_self_state(user_context: Dict[str, Any], reply: str,
                      *, now: Optional[float] = None) -> bool:
    """出站后调用：认得出自述状态则记进 bounded log。绝不抛。"""
    try:
        hit = extract_self_state(reply)
        if not hit:
            return False
        ts = float(now if now is not None else time.time())
        log = user_context.get(LOG_KEY)
        if not isinstance(log, list):
            log = []
        log.append({"ts": ts, "state": hit["state"], "phrase": hit["phrase"]})
        user_context[LOG_KEY] = log[-_LOG_CAP:]
        return True
    except Exception:
        return False


def _ttl_of(state: str) -> float:
    for s, _pat, ttl in _STATES:
        if s == state:
            return float(ttl)
    return 2 * 3600.0


def active_self_state(user_context: Dict[str, Any],
                      *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """TTL 窗内最新一条自述状态；无/过期 → None。"""
    try:
        log = user_context.get(LOG_KEY)
        if not isinstance(log, list) or not log:
            return None
        ts_now = float(now if now is not None else time.time())
        last = log[-1]
        ts = float((last or {}).get("ts") or 0)
        state = str((last or {}).get("state") or "")
        if not state or ts <= 0:
            return None
        age = ts_now - ts
        if age < 0 or age > _ttl_of(state):
            return None
        return {"state": state, "phrase": str(last.get("phrase") or ""),
                "age_min": age / 60.0}
    except Exception:
        return None


def self_state_note(user_context: Dict[str, Any],
                    *, now: Optional[float] = None) -> str:
    """供 prompt 注入的自述状态块；无活跃状态 → 空串。"""
    cur = active_self_state(user_context, now=now)
    if not cur:
        return ""
    label = _STATE_LABEL.get(cur["state"], cur["state"])
    mins = int(cur["age_min"])
    ago = f"{mins} 分钟前" if mins < 90 else f"{mins // 60} 小时前"
    phrase = str(cur.get("phrase") or "").strip()
    quoted = f"（原话「{phrase}」）" if phrase else ""
    return (
        f"【你自己最近说过的状态】你 {ago} 亲口说过要{label}{quoted}。"
        "本条消息必须与该状态衔接或自然过渡（如刚回来/睡醒了/忙完了），"
        "绝不能装作没说过，更不能说出与之矛盾的近况。"
    )


__all__ = [
    "LOG_KEY",
    "active_self_state",
    "extract_self_state",
    "record_self_state",
    "self_state_note",
]
