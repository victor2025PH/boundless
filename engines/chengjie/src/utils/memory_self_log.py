"""「AI 自身经历」入口（J-10 A4，#177 · 2026-09-05）——只读汇总 + 删一条，不碰任何注入逻辑。

#177 / #171 / #182 的共同根：客户档案里只有「关于对方的事实」，找不到「人设自己
说过什么、承诺过什么、发过什么」。这些已经分散存在 ``user_context`` 的三份 bounded
日志里（都随 ContextStore 持久化，都各有自己的注入消费口——本模块**不改写**它们的
语义，只把它们拼成一张时间线给档案抽屉看，并允许删掉某一条）：

- ``_human_said_log``（J-1 #177 ``human_outbound_memory``）：坐席以人设身份亲口说过的
  自述事实（「我有个女儿」），``author=human``；
- ``_self_state_log``（B52 ``self_state``）：AI 出站里的自述近况（「我先去睡了」）；
- ``_media_sent_log``（Phase18）：真发出过的照片/语音（note / scene / series）；
- ``_media_pending``（实施69 ``media_pending``）：AI 承诺/客户在等、尚未兑现的媒体
  （15 分钟 TTL 内才算「承诺待兑现」）。

**承诺持久化归二期**：``outbound_promise_guard`` 是纯函数守卫、没有持久面，这里只能
展示 TTL 内的悬置承诺；「答应过什么 / 兑现没兑现」的长期账本等一期读数后再定。

纯函数（字典进字典出），零依赖。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

KIND_HUMAN_SAID = "human_said"
KIND_AI_STATE = "ai_state"
KIND_MEDIA_SENT = "media_sent"
KIND_PROMISE_PENDING = "promise_pending"
KINDS = (KIND_HUMAN_SAID, KIND_AI_STATE, KIND_MEDIA_SENT, KIND_PROMISE_PENDING)

# 各类对应的 user_context 键（删除时按 kind 找回那份日志）
_LOG_KEYS = {
    KIND_HUMAN_SAID: "_human_said_log",
    KIND_AI_STATE: "_self_state_log",
    KIND_MEDIA_SENT: "_media_sent_log",
}
_PENDING_KEY = "_media_pending"
_TS_TOL = 1e-3


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _entry_text(kind: str, e: Dict[str, Any]) -> str:
    if kind == KIND_HUMAN_SAID:
        return str(e.get("fact") or "")
    if kind == KIND_AI_STATE:
        return str(e.get("phrase") or e.get("state") or "")
    if kind == KIND_MEDIA_SENT:
        return str(e.get("note") or "")
    return ""


def collect_self_experience(
    user_context: Optional[Dict[str, Any]], *, now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """→ 按时间倒序的条目列表，每条 ``{kind, ts, text, ...kind 专有字段}``。绝不抛。"""
    ctx = user_context if isinstance(user_context, dict) else {}
    out: List[Dict[str, Any]] = []
    try:
        for e in (ctx.get("_human_said_log") or []):
            if not isinstance(e, dict):
                continue
            txt = _entry_text(KIND_HUMAN_SAID, e)
            if not txt:
                continue
            out.append({"kind": KIND_HUMAN_SAID, "ts": _f(e.get("ts")), "text": txt,
                        "author": str(e.get("author") or "human"),
                        "quote": str(e.get("quote") or "")})
        for e in (ctx.get("_self_state_log") or []):
            if not isinstance(e, dict):
                continue
            txt = _entry_text(KIND_AI_STATE, e)
            if not txt:
                continue
            out.append({"kind": KIND_AI_STATE, "ts": _f(e.get("ts")), "text": txt,
                        "state": str(e.get("state") or "")})
        for e in (ctx.get("_media_sent_log") or []):
            if not isinstance(e, dict):
                continue
            txt = _entry_text(KIND_MEDIA_SENT, e)
            if not txt:
                continue
            out.append({"kind": KIND_MEDIA_SENT, "ts": _f(e.get("ts")), "text": txt,
                        "scene": str(e.get("scene") or ""),
                        "series": str(e.get("series") or "")})
        st = ctx.get(_PENDING_KEY)
        if isinstance(st, dict) and st.get("kind"):
            pk, subj = "", ""
            try:
                from src.ai.media_pending import pending_kind, pending_subject
                pk = str(pending_kind(ctx, now=now) or "")   # TTL 内且未兑现才非空
                if pk:
                    subj = str(pending_subject(ctx, now=now) or "")
            except Exception:
                pk = ""
            if pk:
                out.append({"kind": KIND_PROMISE_PENDING, "ts": _f(st.get("ts")),
                            "text": subj or pk, "media_kind": pk,
                            "source": str(st.get("source") or "")})
    except Exception:
        return out
    out.sort(key=lambda x: -float(x.get("ts") or 0))
    return out


def delete_self_experience_entry(
    user_context: Optional[Dict[str, Any]], kind: str, ts: Any, text: str,
) -> bool:
    """删掉一条（按 kind + ts + text 精确匹配）；``promise_pending`` 删＝清悬置。
    改的是 user_context 里的那份日志本身，调用方负责 ``mark_dirty/flush``。返回是否删到。"""
    if not isinstance(user_context, dict):
        return False
    k = str(kind or "")
    if k == KIND_PROMISE_PENDING:
        if user_context.get(_PENDING_KEY):
            user_context.pop(_PENDING_KEY, None)
            return True
        return False
    key = _LOG_KEYS.get(k)
    if not key:
        return False
    log = user_context.get(key)
    if not isinstance(log, list) or not log:
        return False
    want_ts = _f(ts)
    want_txt = str(text or "")
    for i, e in enumerate(log):
        if not isinstance(e, dict):
            continue
        if abs(_f(e.get("ts")) - want_ts) <= _TS_TOL and _entry_text(k, e) == want_txt:
            del log[i]
            user_context[key] = log
            return True
    return False


__all__ = [
    "KIND_HUMAN_SAID", "KIND_AI_STATE", "KIND_MEDIA_SENT", "KIND_PROMISE_PENDING", "KINDS",
    "collect_self_experience", "delete_self_experience_entry",
]
