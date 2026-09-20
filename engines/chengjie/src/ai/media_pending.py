"""悬置媒体请求状态（实施69，2026-08-27）——「客户在等一张还没到的图」的跨轮记忆。

背景（2026-08-24 夜市摊实录）：claim 守卫（「这不就来了嘛/发给你了呀」）的
media_context 门控只看**客户本条**是否在索要媒体；而真实催促句（「拍吧」「我不信」
「在哪呢 没看到」）大多不带媒体名词 → 要图发生在 1-3 轮前、谎言在后续轮出站，
门控每轮从零重判、全程关闭。三条谎言的词表**全部能命中**，差的只是这扇门。

本模块把「要图→兑现」升格为跨轮状态（挂 ``user_context["_media_pending"]``，
随 ContextStore 持久化）：

- 点亮：客户显式要图/要语音（``wants_media``/``detect_selfie_request``）、
  或 AI 出站承诺/offer（``detect_media_promise``/``detect_media_offer``——AI 自己
  开的空头支票同样让客户进入等待态）；主体（「烤串/大腰子」）一并记下，
  后续「照片呢」这类无主体催促轮不再丢失「想看的是什么」。
- 续期：悬置中客户催促（「拍吧/快点/照片呢/我不信」）或短肯定。
- 熄灭：**图片真发出**（``_media_sent_log`` 出现晚于点亮时刻的条目——所有 A 线
  发图路径都写该日志，零新增接线）或 TTL（默认 15 分钟）到期。
- 消费：① claim 门控 ``_mctx``（仅 image 轨——语音发出不落媒体日志，无可靠熄灭
  信号，误开门会剥掉真话，语音轨维持旧的当轮判定）；② 预生成 hint（客户在等图
  的每一轮都提醒 LLM「别承诺别称已发」）；③ Stage A/0 的主体记忆兜底。

刻意不做：不用 conversation_meta/DB（A 线 user_context 本就持久化且随会话隔离）；
不做语音轨门控（见上）；不做跨实例共享（悬置是单会话语义）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional, Sequence

STATE_KEY = "_media_pending"
PENDING_TTL_SEC = 15 * 60.0

# 催促/追问（只用于**续期**已点亮的悬置，永不点亮新悬置——「我不信」单独出现
# 可能在聊别的，只有客户已在等图时才解释为催图）。
_URGE_RE = re.compile(
    r"拍吧|快拍|快[点點發发]|发[呀啊吧]|發[呀啊吧]|照片呢|图呢|圖呢|"
    r"磨[叽蹭]|墨迹|咋还没|怎么还没|还没(?:好|拍|发|發)|"
    r"好了[吗嗎没沒]|拍了[吗嗎]|发了[吗嗎]|發了[嗎吗]|"
    r"我不信|骗我|騙我|骗人|騙人|hurry|where.{0,6}(?:photo|pic)",
    re.IGNORECASE,
)


def _now(now: Any) -> float:
    try:
        return float(now) if now is not None else time.time()
    except (TypeError, ValueError):
        return time.time()


def _last_media_sent_ts(user_context: Dict[str, Any]) -> float:
    """已发媒体日志（Phase18 ``_media_sent_log``）里最近一条的时间戳；无则 0。"""
    best = 0.0
    try:
        for it in (user_context.get("_media_sent_log") or []):
            ts = float((it or {}).get("ts") or 0)
            if ts > best:
                best = ts
    except Exception:
        return 0.0
    return best


def last_media_sent_ts(user_context: Dict[str, Any]) -> float:
    """公开口径：该会话（A 线 user_context）最近一次媒体真发的时间戳；无则 0。"""
    return _last_media_sent_ts(user_context or {})


def media_sent_within(
    user_context: Dict[str, Any], window_sec: float, *, now: Any = None,
) -> bool:
    """近 ``window_sec`` 秒内该会话是否真发过媒体（#171「已发假声明」的真伪判据）。

    A 线所有发图路径都写 ``_media_sent_log``，这里只读它：窄窗内真发过＝
    「我刚发了/你该收到了」是真话（绝不剥）；没发＝谎（走兑现→撤回）。
    异常/无日志一律 False（宁可多拦一次「刚发了」，也不放过一句空头断言——
    调用方紧跟着会先尝试真发一张，拦错的代价只是多送一张图）。
    """
    try:
        ts = _last_media_sent_ts(user_context or {})
        if ts <= 0:
            return False
        return (_now(now) - ts) <= max(0.0, float(window_sec))
    except Exception:
        return False


def _state(user_context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    st = user_context.get(STATE_KEY)
    return st if isinstance(st, dict) and st.get("kind") else None


def _valid(st: Optional[Dict[str, Any]], user_context: Dict[str, Any],
           now: float) -> bool:
    """悬置是否仍然成立：未过期 且（image 轨）点亮后没有图片真发出。"""
    if not st:
        return False
    try:
        ts = float(st.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    if ts <= 0 or (now - ts) > PENDING_TTL_SEC:
        return False
    if str(st.get("kind")) == "image" and _last_media_sent_ts(user_context) >= ts:
        return False
    return True


def is_media_urging(text: str) -> bool:
    """短催促句判定（≤12 字符；仅用于续期，见模块注释）。

    长度收紧是刻意的：真实催促是短爆发（「拍吧」「照片呢」「我不信」），
    长句里出现「我不信」多半在聊别的，续期窗被它撑着会误伤后续闲聊。
    """
    s = str(text or "").strip()
    if not s or len(s) > 12:
        return False
    return bool(_URGE_RE.search(s))


def note_peer_turn(
    user_context: Dict[str, Any], peer_text: str, *,
    history: Optional[Sequence[Dict[str, Any]]] = None, now: Any = None,
) -> str:
    """入站轮更新（每轮进 LLM 前调一次）。返回更新后的悬置 kind（''=无）。

    过期/已兑现的旧态先清；客户显式要媒体 → 点亮/重置（主体尽力捕获）；
    悬置中催促/短肯定 → 续期（主体保留）。任何异常不抛（守卫纪律）。
    """
    t = _now(now)
    try:
        st = _state(user_context)
        if st and not _valid(st, user_context, t):
            user_context.pop(STATE_KEY, None)
            st = None
        text = str(peer_text or "")
        kind = ""
        try:
            from src.ai.outbound_promise_guard import wants_media
            kind = wants_media(text)
        except Exception:
            kind = ""
        if not kind:
            try:
                from src.ai.companion_selfie import detect_selfie_request
                if detect_selfie_request(text):
                    kind = "image"
            except Exception:
                pass
        if kind:
            subject = ""
            try:
                from src.ai.outbound_promise_guard import wanted_media_subject
                subject = wanted_media_subject(
                    text, history, generic_request=True)
            except Exception:
                subject = ""
            # 无新主体的重复要图（「照片呢」）沿用旧主体——主体记忆正是本状态
            # 机对 wanted_media_subject 单轮抽取的增量价值。
            if not subject and st and str(st.get("kind")) == kind:
                subject = str(st.get("subject") or "")
            # 重复要图＝事实上的催促：连续第二次显式要同类媒体，催促计数照升
            # （「照片呢」既是新请求也是催——升级 hint 的判据要吃到这类轮次）。
            _urges = 0
            if st and str(st.get("kind")) == kind:
                _urges = int(st.get("urges") or 0) + 1
            user_context[STATE_KEY] = {
                "kind": kind, "ts": t, "subject": subject, "src": "peer",
                "urges": _urges}
            return kind
        if st:
            refresh = is_media_urging(text)
            if not refresh:
                try:
                    from src.ai.outbound_promise_guard import (
                        is_short_affirmative,
                    )
                    refresh = is_short_affirmative(text)
                except Exception:
                    refresh = False
            if refresh:
                st = dict(st)
                st["ts"] = t
                st["urges"] = int(st.get("urges") or 0) + 1
                user_context[STATE_KEY] = st
            return str(st.get("kind") or "")
    except Exception:
        pass
    return ""


def note_ai_turn(
    user_context: Dict[str, Any], final_reply: str, *, now: Any = None,
) -> None:
    """出站轮更新（守卫处理**后**的最终文本）：AI 承诺/offer 也让客户进入等待态。

    承诺被守卫撤回后最终文本已无承诺 → 不点亮（正确）；异步兑现保留原文 →
    点亮，兑现成功由媒体日志自动熄灭、失败则悬置继续压住后续谎言。
    """
    t = _now(now)
    try:
        text = str(final_reply or "")
        if not text.strip():
            return
        kind = ""
        src = ""
        try:
            from src.ai.outbound_promise_guard import (
                detect_media_offer,
                detect_media_promise,
            )
            kind = detect_media_promise(text)
            src = "promise" if kind else ""
            if not kind:
                kind = detect_media_offer(text)
                src = "offer" if kind else ""
        except Exception:
            return
        if not kind:
            return
        st = _state(user_context)
        subject = ""
        try:
            from src.ai.outbound_promise_guard import detect_show_offer_subject
            subject = detect_show_offer_subject(text)
        except Exception:
            subject = ""
        if not subject and st and str(st.get("kind")) == kind:
            subject = str(st.get("subject") or "")
        user_context[STATE_KEY] = {
            "kind": kind, "ts": t, "subject": subject, "src": src}
    except Exception:
        pass


def pending_kind(user_context: Dict[str, Any], *, now: Any = None) -> str:
    """当前有效悬置的 kind（''=无）。只读不清态（清态在 note_peer_turn）。"""
    try:
        st = _state(user_context)
        return str(st.get("kind") or "") if _valid(
            st, user_context, _now(now)) else ""
    except Exception:
        return ""


def pending_subject(user_context: Dict[str, Any], *, now: Any = None) -> str:
    """当前有效悬置记住的主体（「烤串/大腰子」；''=无/纯自拍请求）。"""
    try:
        st = _state(user_context)
        if _valid(st, user_context, _now(now)):
            return str(st.get("subject") or "")
    except Exception:
        pass
    return ""


def pending_urges(user_context: Dict[str, Any], *, now: Any = None) -> int:
    """当前有效悬置期间客户催促/重复要图的次数（0=首轮等待）。

    消费方（实施69「IOU 即 hint 升级」）：≥2 时把常驻提示升级为「你已经拖了
    对方好几轮，主动给个可信交代」——替代「10 分钟无图自动补发台阶文本」的
    原方案：那是一条要过静默时段/预算/防骚扰整套闸的新主动出站路径，而催促
    升级只改下一轮回复的措辞，零新增发送面，覆盖同一场景的绝大多数价值
    （客户催了才最需要交代；催都不催的沉默流失留给 P2 带预算接线再做）。
    """
    try:
        st = _state(user_context)
        if _valid(st, user_context, _now(now)):
            return max(0, int(st.get("urges") or 0))
    except Exception:
        pass
    return 0


__all__ = [
    "STATE_KEY", "PENDING_TTL_SEC", "is_media_urging",
    "note_peer_turn", "note_ai_turn", "pending_kind", "pending_subject",
    "pending_urges", "last_media_sent_ts", "media_sent_within",
]
