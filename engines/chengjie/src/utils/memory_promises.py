"""人设承诺账本（J-10 二期，#177 / #171 · 2026-09-05）——「AI（或坐席替人设）答应过什么、兑现没兑现」。

一期 A4 的「承诺过」只能显示 ``media_pending`` 15 分钟 TTL 内的悬置（``outbound_promise_guard``
是纯函数守卫，没有持久面）。客户真正在意的是跨天的承诺：「明天给你打电话」「下次拍给你看」
「周末带你去吃」——AI 说完就忘，客户记得。本模块把这类第一人称、面向未来的承诺句从**出站
文本**里抓出来，存进 ``user_context["_promise_log"]``（bounded，随 ContextStore 持久化，与
``_self_state_log`` / ``_media_sent_log`` 同族），供档案抽屉「人设说过 / 承诺过」按状态展示：

- ``open``：还没兑现；``overdue``：开了 7 天还没动静（页面标灰红）；``done``：已兑现——
  媒体类由 ``_media_sent_log`` 里晚于承诺时刻的真发记录**自动结清**，其余靠坐席点「已兑现」。

抓取口径刻意窄（宁漏勿误）：疑问句、否定/条件/转述/过去指涉一律不算；只认第一人称 +
承诺动词/时间词 + 面向对方的动作。捕获点＝出站单点（``skill_manager._update_after_reply``
与 ``human_outbound_memory.on_human_outbound``），零阻断。

**不注入 prompt**（本期）：:func:`promise_note` 已备好，接线与否等一期读数后再定——
未兑现的电话/见面承诺硬塞进 prompt 可能让 AI 每轮找借口，需要单独设计口吻与频率。

纯函数、零 LLM、零新表。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

LOG_KEY = "_promise_log"
_LOG_CAP = 12
_TEXT_MAX = 120
_DEDUPE_WINDOW_SEC = 24 * 3600
OPEN_STALE_SEC = 7 * 86400

KIND_MEDIA = "media"
KIND_CALL = "call"
KIND_MEET = "meet"
KIND_SEND = "send"
KIND_GENERIC = "generic"

STATUS_OPEN = "open"
STATUS_OVERDUE = "overdue"
STATUS_DONE = "done"

_CLAUSE_SPLIT_RE = re.compile(r"([。！？!?\n;；～~]+|(?<=[a-zA-Z0-9\)])\.(?=\s|$))")
# 疑问＝终止符带 ?/？，或句末语气词（「明天去看你好吗」拆句后问号已被吃掉，靠这个兜）
_QUESTION_RE = re.compile(r"[?？]|(?:吗|呢|么|好不好|行不行|可以不|要不要|要么)\s*$")

_TIME_WORDS = (
    r"明天|后天|今晚|晚点|晚上|下周|周末|下个月|月底|下次|改天|到时候|回头|待会儿?|一会儿?|"
    r"稍后|马上|等我|之后|以后|过几天|周[一二三四五六日天]|"
    r"tomorrow|tonight|later|next\s+(?:week|time|month|weekend)|this\s+weekend|in\s+a\s+bit|soon|"
    r"after\s+work|when\s+I\s+get\s+home"
)
_TIME_RE = re.compile(_TIME_WORDS, re.IGNORECASE)

_ZH_TARGET_ACT = (
    r"(?:给你|发你|发给你|拍给你|打给你|找你|联系你|告诉你|回你|陪你|陪着你|陪伴你|见你|带你|帮你|请你|"
    r"接你|来看你|去看你|等你|教你|给你看|念给你|唱给你|做给你)"
)
_ZH_PROMISE_PATTERNS = [
    # 我 + 承诺词/时间词 + …面向对方的动作
    re.compile(
        r"我(?:们)?(?:会|一定|保证|答应|承诺|肯定会|一定会|到时候|回头|待会儿?|一会儿?|稍后|马上|"
        r"晚点|明天|后天|今晚|下周|周末|下次|以后|改天|过几天)[^，。！？!?\n]{0,16}" + _ZH_TARGET_ACT),
    # 时间词开头 + …面向对方的动作（主语省略的第一人称）
    re.compile(
        r"(?:等我|回头|待会儿?|一会儿?|稍后|马上|晚点|明天|后天|今晚|下周|周末|下次|改天|到时候|过几天)"
        r"[^，。！？!?\n]{0,12}" + _ZH_TARGET_ACT),
    # 承诺动词直接带内容：我答应你… / 我保证… / 说好了…
    re.compile(r"我(?:答应|保证|承诺|说好|约好)(?:你|了)?[^，。！？!?\n]{2,20}"),
]
_EN_PROMISE_PATTERNS = [
    re.compile(
        r"\b(?:I(?:'ll|’ll| will| promise(?: to)?| swear(?: to)?| am going to|'m going to|’m going to| gonna)"
        r"|let me|we(?:'ll|’ll| will| can))\s+(?:\w+\s+){0,3}?"
        r"(?:send|call|text|message|find|check|look|bring|visit|come|see you|tell you|show you|"
        r"take you|be there|remind|pick you|cook|make|book|buy|sing|read|teach|meet)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:tomorrow|tonight|later|next\s+(?:week|time|weekend)|this\s+weekend)[^.!?\n]{0,24}"
        r"\b(?:I(?:'ll|’ll| will)|we(?:'ll|’ll| will)|let me)\b", re.IGNORECASE),
]

# 排除面：否定 / 条件 / 转述 / 过去 / 对方为主语 / 永恒情话（「我会一直陪着你」不可兑现，不进账本）
_EXCLUDE_RE = re.compile(
    r"不会|不能|没法|不想|不用|不要|别|没有|要是|如果|假如|除非|"
    r"你说|他说|她说|听说|你答应|你保证|"
    r"昨天|上次|刚才|刚刚|已经|前几天|之前|"
    r"一直|永远|一辈子|每天都|"
    r"\b(?:won't|can't|cannot|not|never|don't|doesn't|didn't|no\s+way|if|unless|"
    r"you\s+said|he\s+said|she\s+said|yesterday|already|last\s+time|earlier|"
    r"always|forever)\b",
    re.IGNORECASE,
)
_PEER_SUBJECT_RE = re.compile(r"^\s*(?:你|妳|您)(?:会|要|可以|先|得|记得|一定)|^\s*you\b", re.IGNORECASE)

_MEDIA_RE = re.compile(r"拍|照片|自拍|相片|图|语音|视频|唱|录|photo|pic|selfie|voice|video|sing|record", re.IGNORECASE)
_CALL_RE = re.compile(r"打给你|打电话|电话|通话|\bcall\b|\bring\b|\bphone\b", re.IGNORECASE)
_MEET_RE = re.compile(r"见你|见面|来看你|去看你|接你|去找你|陪你|陪着你|陪伴你|带你|一起|meet|see\s+you|visit|be\s+there|come\s+over|pick\s+you|take\s+you", re.IGNORECASE)
_SEND_RE = re.compile(r"发你|发给你|给你看|给你|寄|send|bring|show|share|mail", re.IGNORECASE)


def _clauses(text: str) -> List[str]:
    """子句 + 其终止符（保留 ?/？ 供疑问判定）。"""
    parts = _CLAUSE_SPLIT_RE.split(str(text or ""))
    out: List[str] = []
    for i in range(0, len(parts), 2):
        body = (parts[i] or "").strip()
        term = parts[i + 1] if i + 1 < len(parts) else ""
        if body:
            out.append(body + (term.strip() if term else ""))
    return out


def _kind_of(clause: str) -> str:
    try:
        from src.ai.outbound_promise_guard import detect_media_promise
        if detect_media_promise(clause):
            return KIND_MEDIA
    except Exception:
        pass
    if _MEDIA_RE.search(clause):
        return KIND_MEDIA
    if _CALL_RE.search(clause):
        return KIND_CALL
    if _MEET_RE.search(clause):
        return KIND_MEET
    if _SEND_RE.search(clause):
        return KIND_SEND
    return KIND_GENERIC


def extract_promises(text: str) -> List[Dict[str, str]]:
    """出站文本 → 承诺条目 ``[{kind, text, due_hint}]``（子句原文，≤120 字）。认不出返回 []。"""
    t = str(text or "").strip()
    if not t or len(t) > 4000:
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for clause in _clauses(t):
        if len(clause) < 4 or _QUESTION_RE.search(clause):
            continue
        clause = clause.rstrip("。！!;；\n～~").strip()
        if _EXCLUDE_RE.search(clause) or _PEER_SUBJECT_RE.search(clause):
            continue
        hit = any(p.search(clause) for p in _ZH_PROMISE_PATTERNS) or any(
            p.search(clause) for p in _EN_PROMISE_PATTERNS)
        if not hit:
            continue
        # 即时媒体承诺（「等我拍一张给你」）也算——虽有 media_pending 管兑现，账本要留痕
        body = clause[:_TEXT_MAX]
        k = re.sub(r"\s+", "", body).lower()
        if k in seen:
            continue
        seen.add(k)
        m = _TIME_RE.search(clause)
        out.append({"kind": _kind_of(clause), "text": body,
                    "due_hint": (m.group(0) if m else "")})
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def record_promises(
    user_context: Dict[str, Any], text: str, *, author: str = "ai",
    now: Optional[float] = None,
) -> int:
    """出站后调用：认得出的承诺写进 bounded 账本（24h 内同文去重）。返回新增条数。绝不抛。"""
    try:
        if not isinstance(user_context, dict):
            return 0
        found = extract_promises(text)
        if not found:
            return 0
        ts = float(now if now is not None else time.time())
        log = user_context.get(LOG_KEY)
        if not isinstance(log, list):
            log = []
        added = 0
        for p in found:
            k = _norm(p["text"])
            dup = any(
                isinstance(e, dict) and _norm(e.get("text")) == k
                and abs(ts - float(e.get("ts") or 0)) <= _DEDUPE_WINDOW_SEC
                for e in log)
            if dup:
                continue
            log.append({
                "ts": ts, "text": p["text"], "kind": p["kind"], "due_hint": p["due_hint"],
                "author": ("human" if str(author or "").lower() == "human" else "ai"),
                "status": STATUS_OPEN, "done_ts": 0.0,
            })
            added += 1
        if added:
            user_context[LOG_KEY] = log[-_LOG_CAP:]
        return added
    except Exception:
        return 0


def _last_media_sent_ts(user_context: Dict[str, Any]) -> float:
    best = 0.0
    for it in (user_context.get("_media_sent_log") or []):
        try:
            best = max(best, float((it or {}).get("ts") or 0))
        except (TypeError, ValueError):
            continue
    return best


def promise_entries(
    user_context: Optional[Dict[str, Any]], *, now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """账本条目（含计算出的 ``status``：done / overdue / open），按时间倒序。绝不抛。

    媒体类承诺若 ``_media_sent_log`` 里有晚于承诺时刻的真发记录 → 视为已兑现（自动结清）。
    """
    ctx = user_context if isinstance(user_context, dict) else {}
    ts_now = float(now if now is not None else time.time())
    out: List[Dict[str, Any]] = []
    try:
        media_ts = _last_media_sent_ts(ctx)
        for e in (ctx.get(LOG_KEY) or []):
            if not isinstance(e, dict) or not e.get("text"):
                continue
            ts = float(e.get("ts") or 0)
            status = str(e.get("status") or STATUS_OPEN)
            done_ts = float(e.get("done_ts") or 0)
            if status != STATUS_DONE and e.get("kind") == KIND_MEDIA and media_ts > ts:
                status, done_ts = STATUS_DONE, media_ts
            if status != STATUS_DONE and (ts_now - ts) > OPEN_STALE_SEC:
                status = STATUS_OVERDUE
            out.append({
                "kind": "promise", "ts": ts, "text": str(e.get("text") or ""),
                "promise_kind": str(e.get("kind") or KIND_GENERIC),
                "due_hint": str(e.get("due_hint") or ""),
                "author": str(e.get("author") or "ai"),
                "status": status, "done_ts": done_ts,
            })
    except Exception:
        return out
    out.sort(key=lambda x: -float(x.get("ts") or 0))
    return out


def _find(user_context: Dict[str, Any], ts: Any, text: str) -> Optional[Dict[str, Any]]:
    try:
        want_ts = float(ts or 0)
    except (TypeError, ValueError):
        return None
    for e in (user_context.get(LOG_KEY) or []):
        if isinstance(e, dict) and abs(float(e.get("ts") or 0) - want_ts) <= 1e-3 \
                and str(e.get("text") or "") == str(text or ""):
            return e
    return None


def mark_promise_done(
    user_context: Optional[Dict[str, Any]], ts: Any, text: str, *, now: Optional[float] = None,
) -> bool:
    """坐席点「已兑现」。返回是否命中（已 done 的再点返回 False）。调用方负责落盘。"""
    if not isinstance(user_context, dict):
        return False
    e = _find(user_context, ts, text)
    if not e or str(e.get("status") or "") == STATUS_DONE:
        return False
    e["status"] = STATUS_DONE
    e["done_ts"] = float(now if now is not None else time.time())
    return True


def delete_promise(user_context: Optional[Dict[str, Any]], ts: Any, text: str) -> bool:
    """删一条（按 ts + text 精确匹配）。调用方负责落盘。"""
    if not isinstance(user_context, dict):
        return False
    log = user_context.get(LOG_KEY)
    if not isinstance(log, list):
        return False
    e = _find(user_context, ts, text)
    if e is None:
        return False
    log.remove(e)
    user_context[LOG_KEY] = log
    return True


def promise_note(user_context: Optional[Dict[str, Any]], *, now: Optional[float] = None,
                 max_items: int = 3, max_age_days: float = 14.0) -> str:
    """未兑现承诺的 prompt 块（三期接线：``memory.promises.inject`` 出厂关，
    ``skill_manager._inject_self_state`` 一行消费）。

    口吻与频率的三道闸：① 只列 open/overdue，超过 ``max_age_days`` 的老账不再提（别翻旧账）；
    ② 最多 ``max_items`` 条；③ 块头明说「是既定事实、别自相矛盾；只在对方提起或自然相关时
    回应，不要每轮解释或道歉」——目的是**一致性**（别装作没说过），不是逼 AI 兑现。
    媒体类未兑现的另加一句：能真发就发，不能就别再空头承诺（与 media_pending / 承诺守卫同向）。
    """
    ts_now = float(now if now is not None else time.time())
    max_age = max(0.0, float(max_age_days)) * 86400
    items = [
        p for p in promise_entries(user_context, now=ts_now)
        if p["status"] != STATUS_DONE and (max_age <= 0 or (ts_now - float(p["ts"] or 0)) <= max_age)
    ]
    if not items:
        return ""
    lines = []
    has_media = False
    for p in items[:max(1, int(max_items))]:
        stamp = time.strftime("%m-%d", time.localtime(p["ts"])) if p["ts"] > 0 else ""
        age_days = int((ts_now - float(p["ts"] or 0)) // 86400)
        tail = f"（说了 {age_days} 天还没做）" if p["status"] == STATUS_OVERDUE else ""
        lines.append(f"- {stamp + ' ' if stamp else ''}你说过「{p['text']}」{tail}")
        if p.get("promise_kind") == KIND_MEDIA:
            has_media = True
    head = ("【你之前答应过对方、还没兑现的事——这是你们之间的既定事实：别装作没说过、"
            "别自相矛盾；只在对方提起或自然相关时回应，不要每轮解释或道歉】")
    if has_media:
        head += "\n（照片/语音类：能真发就发，发不了就别再空头承诺）"
    return head + "\n" + "\n".join(lines)


__all__ = [
    "LOG_KEY", "OPEN_STALE_SEC",
    "KIND_MEDIA", "KIND_CALL", "KIND_MEET", "KIND_SEND", "KIND_GENERIC",
    "STATUS_OPEN", "STATUS_OVERDUE", "STATUS_DONE",
    "extract_promises", "record_promises", "promise_entries",
    "mark_promise_done", "delete_promise", "promise_note",
]
