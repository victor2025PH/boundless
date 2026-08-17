"""回复生成「时间推理」单一事实源（P0，2026-08-12）。

实录事故（telegram 8244899900↔8921664288）：客户 8/8 21:47 后彻底沉默，AI 每天
早晚 ritual 单向发了 9 条；8/12 21:15 坐席在桌面壳点「智能回复」，链路把 4 天前
**已经回答过**的「闽江知道吗」当成刚收到的消息重新回答，且措辞几乎逐字复读
自己 8/8 的旧回复（DOM 抓取无时间戳 + mode=reply 恒锚定最后一条入站 + 无复读
守卫三层叠加）。

本模块提供纯函数（零 IO、可单测），供 ``persona_reply.generate_persona_reply``
（B 线唯一入口：auto-draft / 工坊 / replybus / 桌面浮钮全经它）消费：

- ``classify_reply_anchor``：「待回复锚点」新鲜度四态判定。**结构优先**——
  最后入站之后我方连续出站 ≥ ``min_monologue`` 条（默认 6，必须大于
  reply_split ``max_parts``＝5，否则一条逐句分条回复就会误判成连发独白）
  即可在**零时间戳**时判定「已答且在单向连发」；有 ts 时按时间精化。
- ``build_followup_note``：stale_answered → 切开场产线时的跟进语境说明
  （勿重答旧问题 / 勿复读连发内容 / 语气克制）。
- ``build_late_reply_hint``：stale_unanswered → 迟回复要带时间感。
- ``build_now_anchor_hint``：当前时刻锚点（日期/星期/时段），治「晚上生成
  早安体」——所有 B 线拟稿注入。
- ``resolve_time_context_cfg``：``inbox.time_context`` 配置解析。**默认开**
  （这是出站正确性守卫，与 media_promise_guard / consistency 同族），
  ``enabled: false`` 一键回旧行为。

「对方隔了很久刚回来」（fresh 锚点 + 上一条用户消息距今很久）的提示措辞
沿用 ``inbound_enrich.build_time_gap_hint``（同一象限，勿重复造词）——由
persona_reply 按 ``prev_user_gap_sec`` 组合，本模块不重复实现。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# min_monologue 必须 > reply_split.max_parts（坐席机 per_sentence 分条上限 5）：
# 一条逻辑回复会被拆成 ≤5 个出站气泡，结构判定的门槛低于它就会把「刚答完一条
# 分条回复」误判成「单向连发独白」→ 智能回复被错切成开场模式。
DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "stale_after_hours": 6.0,     # 与 build_time_gap_hint 的 6h 档同刻度
    "min_monologue": 6,           # 零 ts 时的结构判定门槛（> reply_split max_parts=5）
    "return_gap_hours": 6.0,      # fresh 锚点下「对方刚回来」提示的间隔门槛
    "store_lookup": True,         # 客户端无 ts 时按 conversation_id 反查 inbox store
    "store_limit": 40,
    "repeat_guard": True,         # 生成后与最近出站做相似度守卫
    "repeat_threshold": 0.60,     # 与 proactive_variety 生产校准阈值同值
}


def resolve_time_context_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``inbox.time_context`` → 完整配置（缺省深合并 DEFAULTS，容错任意脏值）。"""
    out = dict(DEFAULTS)
    try:
        raw = ((config or {}).get("inbox") or {}).get("time_context")
        if isinstance(raw, bool):
            out["enabled"] = raw
            return out
        if isinstance(raw, dict):
            for k in out:
                if k in raw and raw[k] is not None:
                    out[k] = raw[k]
        out["enabled"] = bool(out["enabled"])
        out["stale_after_hours"] = max(0.5, float(out["stale_after_hours"]))
        out["min_monologue"] = max(2, int(out["min_monologue"]))
        out["return_gap_hours"] = max(0.5, float(out["return_gap_hours"]))
        out["store_lookup"] = bool(out["store_lookup"])
        out["store_limit"] = max(5, min(200, int(out["store_limit"])))
        out["repeat_guard"] = bool(out["repeat_guard"])
        out["repeat_threshold"] = min(0.95, max(0.3, float(out["repeat_threshold"])))
    except (TypeError, ValueError):
        return dict(DEFAULTS)
    return out


def _is_user_row(row: Dict[str, Any]) -> bool:
    """行方向归一：兼容 {role} (normalize_history) 与 {direction} (store 行)。"""
    role = str(row.get("role") or "")
    if role:
        return role == "user"
    return str(row.get("direction") or "") in ("in", "inbound")


def _row_ts(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def classify_reply_anchor(
    rows: Optional[List[Dict[str, Any]]],
    *,
    now: Optional[float] = None,
    stale_after_sec: float = DEFAULTS["stale_after_hours"] * 3600.0,
    min_monologue: int = DEFAULTS["min_monologue"],
) -> Dict[str, Any]:
    """判定「回复模式要锚定的最后一条入站」还新不新鲜。

    输入行形如 ``{"role": "user"|"assistant", "content": str, "ts"?: float}``
    （normalize_history 产物）或 ``{"direction": "in"|"out", "text": ..., "ts": ...}``
    （inbox store 行），时间升序。

    返回::

        {kind, ts_known, age_sec, outbound_after, prev_user_gap_sec,
         last_inbound_ts, last_inbound_text}

    kind 四态：
    - ``no_inbound``：窗口内没有任何入站——调用方走开场产线（既有语义）。
    - ``fresh``：锚点新鲜（或无任何证据），维持现行 reply 语义。
    - ``stale_unanswered``：锚点陈旧且我方从未回过 → 仍回复，但要带时间感。
    - ``stale_answered``：锚点陈旧且我方已回过（甚至在单向连发）→ 再按
      reply 语义生成就是「重答旧问题」，应切换为跟进/开场语义。

    判定次序（结构证据不依赖时间戳，DOM 抓不到 ts 时仍能兜住实录事故）：
    1. ts 可知且 age ≥ stale_after_sec → 按「其后有无我方出站」分 stale 两态；
    2. ts 不可知（或新鲜）但我方在锚点之后已连发 ≥ min_monologue 条 →
       stale_answered（结构判定；门槛须 > 分条回复上限，防误伤）；
    3. 其余 → fresh。
    """
    now_ts = float(now or time.time())
    items = [r for r in (rows or []) if isinstance(r, dict)]
    last_user_idx = -1
    for i in range(len(items) - 1, -1, -1):
        if _is_user_row(items[i]):
            last_user_idx = i
            break
    if last_user_idx < 0:
        return {
            "kind": "no_inbound", "ts_known": False, "age_sec": 0.0,
            "outbound_after": sum(1 for r in items if not _is_user_row(r)),
            "prev_user_gap_sec": 0.0, "last_inbound_ts": 0.0,
            "last_inbound_text": "",
        }
    outbound_after = sum(
        1 for r in items[last_user_idx + 1:] if not _is_user_row(r))
    ts = _row_ts(items[last_user_idx])
    ts_known = ts > 0
    age = max(0.0, now_ts - ts) if ts_known else 0.0
    prev_gap = 0.0
    if ts_known:
        for i in range(last_user_idx - 1, -1, -1):
            if _is_user_row(items[i]):
                pts = _row_ts(items[i])
                if pts > 0:
                    prev_gap = max(0.0, ts - pts)
                break
    if ts_known and age >= max(1.0, float(stale_after_sec)):
        kind = "stale_answered" if outbound_after >= 1 else "stale_unanswered"
    elif outbound_after >= max(2, int(min_monologue)):
        kind = "stale_answered"
    else:
        kind = "fresh"
    text = str(items[last_user_idx].get("content")
               or items[last_user_idx].get("text") or "").strip()
    return {
        "kind": kind, "ts_known": ts_known, "age_sec": age,
        "outbound_after": outbound_after, "prev_user_gap_sec": prev_gap,
        "last_inbound_ts": ts if ts_known else 0.0,
        "last_inbound_text": text,
    }


def describe_age(age_sec: float) -> str:
    """粗粒度时距（提示语用）：N 天 / N 小时 / 不到 1 小时。"""
    try:
        s = max(0.0, float(age_sec or 0))
    except (TypeError, ValueError):
        s = 0.0
    if s >= 48 * 3600:
        return f"{int(s // 86400)} 天"
    if s >= 24 * 3600:
        return "1 天多"
    if s >= 3600:
        return f"{int(s // 3600)} 小时"
    return "不到 1 小时"


def build_followup_note(anchor: Dict[str, Any]) -> str:
    """stale_answered → 开场产线的跟进语境（进 build_opener_directive 头部）。"""
    a = anchor or {}
    text = str(a.get("last_inbound_text") or "").strip().replace("\n", " ")
    if len(text) > 40:
        text = text[:39] + "…"
    when = (
        f"{describe_age(a.get('age_sec') or 0)}前"
        if a.get("ts_known") else "较早之前"
    )
    n_out = int(a.get("outbound_after") or 0)
    sent_line = (
        f"在那之后你已经又主动发过 {n_out} 条消息，对方一直没有回应。"
        if n_out > 0 else ""
    )
    quoted = f"（{when}的「{text}」）" if text else f"（{when}）"
    return (
        f"【跟进语境——重要】对方最后一条消息是{when}发的{quoted}，"
        "你当时**已经回复过**，不要再回答那条旧消息，更不要复述你当时的回答。"
        + sent_line +
        "本条要像真人隔了一段时间自然跟进：可以轻轻提起当时聊过的具体话题"
        "（带时间感，如「那天你问的…」），或按切入点开启新话题；"
        "语气克制自然，绝不逼问对方为什么不回、也不要连环追问。"
    )


def build_late_reply_hint(age_sec: float) -> str:
    """stale_unanswered → 迟回复提示（汇入 _topic_switch_hint 消费口）。"""
    span = describe_age(age_sec)
    return (
        f"【迟回复提示——重要】对方这条消息是约 {span}之前发的，**不是刚刚**，"
        "你现在才回复。要像真人一样自然带上时间感（如「刚看到」「这两天忙晕了」），"
        "绝不要装作消息刚到就顺口接话；也不要过度道歉，一句带过即可。"
    )


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def daypart_label(hour: int) -> str:
    """小时 → 中文时段词（与常识口径一致，供提示语与测试共用）。"""
    h = int(hour) % 24
    if 5 <= h < 8:
        return "清晨"
    if 8 <= h < 11:
        return "上午"
    if 11 <= h < 13:
        return "中午"
    if 13 <= h < 17:
        return "下午"
    if 17 <= h < 19:
        return "傍晚"
    if 19 <= h < 23:
        return "晚上"
    return "深夜"


def build_now_anchor_hint(now: Optional[float] = None) -> str:
    """当前时刻锚点：日期/星期/时刻/时段 + 一致性约束（进 _topic_switch_hint）。

    刻意只给「事实 + 约束」，不指示复述时间——真人不会每句话报时；
    这行治的是「晚上 9 点生成『早安』体」这类时段错位。
    """
    t = time.localtime(now if now is not None else time.time())
    part = daypart_label(t.tm_hour)
    return (
        f"【当前时间】现在是 {t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        f"（{_WEEKDAYS[t.tm_wday]}）{t.tm_hour:02d}:{t.tm_min:02d}，{part}。"
        "回复中涉及时段/日期/问候（早安、晚安、吃了吗等）必须与当前时间一致，"
        "不要沿用对话历史里旧消息的时段。"
    )


def build_repeat_rewrite_hint(dup_text: str) -> str:
    """复读守卫触发 → 重写指令（追加进 extra_hint 再生成一次）。"""
    d = str(dup_text or "").strip().replace("\n", " ")
    if len(d) > 60:
        d = d[:59] + "…"
    return (
        "【禁止复读——必须执行】你刚拟的回复与你最近**已经发过**的消息高度雷同"
        f"（已发过：「{d}」）。对方已经看过那些话，原样再说一遍会立刻穿帮。"
        "必须换全新的内容和角度重写：不重复相同的信息点、比喻和句式；"
        "如果实在没有新内容可说，就简短自然地把话题推进到当下。"
    )


__all__ = [
    "DEFAULTS",
    "resolve_time_context_cfg",
    "classify_reply_anchor",
    "describe_age",
    "daypart_label",
    "build_followup_note",
    "build_late_reply_hint",
    "build_now_anchor_hint",
    "build_repeat_rewrite_hint",
]
