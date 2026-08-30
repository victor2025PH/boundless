"""目标节奏档（pace）——纯函数，零 IO。

natural = 现有日历一天一拍；today = 小时槽、当日封顶；session = 按入站回合拍、
窗内封顶。期限仍落 ``deadline_ts``；pace 写入 ``goal.params['pace']``（模板
params schema 不渲染该键，坐席表单不会把它当字段露出）。

子日期限**不是**把天数输入的 min 改成 0.01——cadence 与 horizon 必须拆开，
否则 60 分钟目标仍按「每天一拍」规划，到期几乎必 expired。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Optional

PACES = ("natural", "today", "session")

# 限时节奏只开放转化类 + 自定义。关系/唤回/摸底仍走自然天——那些模板的
# 完成信号与「这轮聊完」不是同一件事。
SPRINT_OK = frozenset({
    "custom",
    "conversion_unlock",
    "conversion_subscribe",
    "acquire_and_convert",
})

SESSION_MIN_MINUTES = 15
SESSION_MAX_MINUTES = 120
SESSION_DEFAULT_MINUTES = 60
TODAY_MIN_HOURS = 2
TODAY_MAX_HOURS = 12
TODAY_DEFAULT_HOURS = 8
NATURAL_MIN_DAYS = 1.0
NATURAL_MAX_DAYS = 180.0

SESSION_CAP = 3   # 整段目标最多几拍
TODAY_CAP = 4     # 每个日历日最多几拍

# 力度全集（close=收口档，P1 2026-08-30 仅限时档产生；natural 曲线不出它）
PUSH_ORDER = ("none", "soft", "direct", "close")

_DAY = 86400.0


def normalize_pace(raw: Any) -> str:
    p = str(raw or "").strip().lower()
    return p if p in PACES else "natural"


def sprint_ok(template_id: str) -> bool:
    return str(template_id or "").strip() in SPRINT_OK


def is_sprint(pace: Any) -> bool:
    return normalize_pace(pace) in ("today", "session")


def pace_allowed(template_id: str, pace: Any) -> bool:
    p = normalize_pace(pace)
    if p == "natural":
        return True
    return sprint_ok(template_id)


def infer_pace_from_seconds(span_sec: float) -> str:
    """无显式 pace 时按期限跨度回推（旧行/漏写 params 的兜底）。"""
    try:
        sec = float(span_sec)
    except (TypeError, ValueError):
        return "natural"
    if sec <= 0:
        return "natural"
    if sec <= 2.5 * 3600:
        return "session"
    if sec < 20 * 3600:
        return "today"
    return "natural"


def infer_pace_from_goal(goal: Optional[Dict[str, Any]]) -> str:
    if not isinstance(goal, dict):
        return "natural"
    start = float(goal.get("start_ts") or 0)
    deadline = float(goal.get("deadline_ts") or 0)
    if start <= 0 or deadline <= start:
        return "natural"
    return infer_pace_from_seconds(deadline - start)


def resolve_pace(goal: Optional[Dict[str, Any]]) -> str:
    """生效节奏：params.pace（校验模板白名单）→ 期限跨度回推 → natural。"""
    if not isinstance(goal, dict):
        return "natural"
    tid = str(goal.get("template") or "")
    params = goal.get("params") if isinstance(goal.get("params"), dict) else {}
    stored = normalize_pace(params.get("pace") if isinstance(params, dict) else "")
    if stored != "natural":
        return stored if pace_allowed(tid, stored) else "natural"
    inferred = infer_pace_from_goal(goal)
    if inferred != "natural" and pace_allowed(tid, inferred):
        return inferred
    return "natural"


def clamp_deadline_days(
    pace: Any,
    days: Any,
    *,
    default_days: float = 14.0,
) -> float:
    """按节奏夹期限。session/today 是子日区间；natural 仍是 1–180 天。"""
    p = normalize_pace(pace)
    try:
        d = float(days or 0)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0:
        if p == "session":
            d = SESSION_DEFAULT_MINUTES / 1440.0
        elif p == "today":
            d = TODAY_DEFAULT_HOURS / 24.0
        else:
            try:
                d = float(default_days or 14.0)
            except (TypeError, ValueError):
                d = 14.0
    if p == "session":
        lo = SESSION_MIN_MINUTES / 1440.0
        hi = SESSION_MAX_MINUTES / 1440.0
        return max(lo, min(d, hi))
    if p == "today":
        lo = TODAY_MIN_HOURS / 24.0
        hi = TODAY_MAX_HOURS / 24.0
        return max(lo, min(d, hi))
    return max(NATURAL_MIN_DAYS, min(d, NATURAL_MAX_DAYS))


def slot_key(
    pace: Any,
    now: Optional[float] = None,
    last_inbound_ts: float = 0.0,
    *,
    closing: bool = False,
) -> str:
    """规划/取拍用的槽位键。仍写入 ``goal_actions.day``（TEXT，无需迁移）。

    - natural → ``YYYY-MM-DD``（与 ``planner.day_key`` 同口径）
    - today → ``YYYY-MM-DDTHH``（本地时）；``closing=True``（收口窗，P1
      2026-08-30）细化到半小时桶 ``…THHh0|h1``——3 小时目标的最后一段
      每小时一拍太粗，对方一句「我再想想」就把收口拍耗光了
    - session → ``s:{int(last_inbound_ts)}``；无入站时 ``s:0``（整段共用一槽，
      没人开口不连拍——比虚构回合更老实）
    """
    n = float(now if now is not None else time.time())
    p = normalize_pace(pace)
    if p == "session":
        try:
            ts = int(float(last_inbound_ts or 0))
        except (TypeError, ValueError):
            ts = 0
        if ts < 0:
            ts = 0
        return f"s:{ts}"
    t = time.localtime(n)
    if p == "today":
        key = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}T{t.tm_hour:02d}"
        if closing:
            key += f"h{0 if t.tm_min < 30 else 1}"
        return key
    from src.companion.goals.planner import day_key
    return day_key(n)


def beat_cap(pace: Any) -> int:
    """0 = 不另加封顶（natural 已由日历槽保证一天一拍）。"""
    p = normalize_pace(pace)
    if p == "session":
        return SESSION_CAP
    if p == "today":
        return TODAY_CAP
    return 0


def effective_beat_cap(
    pace: Any, *, overrides: Optional[Dict[str, Any]] = None, mode: str = "",
) -> int:
    """生效封顶（P1 2026-08-30）：配置覆写 > 内置默认；全力档（mode=max）
    在此之上 +2（收口窗细化出的半小时槽要有额度可用）。natural 恒 0。"""
    p = normalize_pace(pace)
    if p == "natural":
        return 0
    cap = beat_cap(p)
    key = "session_cap" if p == "session" else "today_cap"
    if isinstance(overrides, dict) and overrides.get(key) is not None:
        try:
            cap = max(1, min(int(overrides[key]), 24))
        except (TypeError, ValueError):
            pass
    if str(mode or "").strip().lower() == "max":
        cap += 2
    return cap


def count_beats_for_cap(
    actions: Iterable[Dict[str, Any]],
    pace: Any,
    now: Optional[float] = None,
) -> int:
    """cap 计数：session 算整段；today 只算当天（``day`` 前缀匹配日历日）。"""
    p = normalize_pace(pace)
    rows = [a for a in (actions or []) if isinstance(a, dict)]
    if p == "session":
        return len(rows)
    if p == "today":
        n = float(now if now is not None else time.time())
        t = time.localtime(n)
        prefix = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        return sum(1 for r in rows if str(r.get("day") or "").startswith(prefix))
    return 0


def planner_thresholds(
    pace: Any,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    mode: str = "",
) -> Dict[str, int]:
    """覆盖 planner 的 unanswered 退避/熔断。空 dict = 用配置缺省。

    session：对方开口后我们已经带目标说了 1 次 → 退避；再说 1 次仍无新开口
    → 熔断（「跟一句就停」）。today 稍宽。crisis/情绪 hold 不在这里、不动。

    P1 2026-08-30：
    - ``overrides``（``companion.goals.sprint.{backoff_after,halt_after}``）
      显式覆写基线（骚扰刹车松紧交运营）；
    - ``mode=="max"``（目标 ``params.sprint_mode``，用户逐目标显式拍板的
      全力档）→ 退避/熔断双双置 0（planner 对 0 的语义就是关闭）——
      危机/情绪 hold 是另一层（planner 第 1 步），不在此列、不受影响。
    """
    p = normalize_pace(pace)
    if p == "natural":
        return {}
    if str(mode or "").strip().lower() == "max":
        return {"backoff_after": 0, "halt_after": 0}
    if p == "session":
        out = {"backoff_after": 1, "halt_after": 2}
    else:
        out = {"backoff_after": 1, "halt_after": 3}
    if isinstance(overrides, dict):
        for k in ("backoff_after", "halt_after"):
            if overrides.get(k) is not None:
                try:
                    out[k] = max(0, min(int(overrides[k]), 20))
                except (TypeError, ValueError):
                    pass
    return out


# 收口升档默认阈：剩余占比 < 该值 → 力度升 close（可经 sprint.escalate_at 覆写）
DEFAULT_ESCALATE_AT = 0.35


def sprint_push(
    pace: Any,
    beat_index: int,
    template_push: str = "soft",
    *,
    remaining_ratio: Optional[float] = None,
    escalate_at: float = DEFAULT_ESCALATE_AT,
    mode: str = "",
) -> str:
    """限时档覆盖推进力度。``push_level == none`` 的陪伴日**不**覆盖——那是
    unanswered 退避的产物，硬改成 direct 等于拆掉让路。

    P1 2026-08-30 推进力加度：
    - ``remaining_ratio``（剩余/总时长）低于 ``escalate_at`` → **close 收口档**
      （不论拍序——哪怕是第 1 拍，窗口只剩 1/3 就该收口而不是试探）；
    - ``mode=="max"``（全力档）→ 首拍即 direct（默认档首拍 soft 试探）。
    """
    p = normalize_pace(pace)
    lvl = str(template_push or "soft")
    if lvl not in PUSH_ORDER:
        lvl = "soft"
    if p == "natural" or lvl == "none":
        return lvl
    is_max = str(mode or "").strip().lower() == "max"
    if remaining_ratio is not None:
        try:
            rr = float(remaining_ratio)
        except (TypeError, ValueError):
            rr = -1.0
        if 0 <= rr < max(0.0, min(float(escalate_at or 0), 0.9)):
            return "close"
    idx = max(0, int(beat_index or 0))
    if idx <= 0:
        return "direct" if is_max else "soft"
    return "direct"


def remaining_sec(goal: Optional[Dict[str, Any]], now: Optional[float] = None) -> float:
    if not isinstance(goal, dict):
        return 0.0
    dl = float(goal.get("deadline_ts") or 0)
    n = float(now if now is not None else time.time())
    return max(0.0, dl - n)


def total_sec(goal: Optional[Dict[str, Any]]) -> float:
    if not isinstance(goal, dict):
        return 0.0
    start = float(goal.get("start_ts") or 0)
    dl = float(goal.get("deadline_ts") or 0)
    if start > 0 and dl > start:
        return dl - start
    return 0.0


__all__ = [
    "DEFAULT_ESCALATE_AT",
    "PACES",
    "PUSH_ORDER",
    "SPRINT_OK",
    "beat_cap",
    "clamp_deadline_days",
    "count_beats_for_cap",
    "effective_beat_cap",
    "infer_pace_from_goal",
    "infer_pace_from_seconds",
    "is_sprint",
    "normalize_pace",
    "pace_allowed",
    "planner_thresholds",
    "remaining_sec",
    "resolve_pace",
    "slot_key",
    "sprint_ok",
    "sprint_push",
    "total_sec",
]
