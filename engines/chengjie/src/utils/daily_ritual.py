"""每日仪式感主动问候：晨安 / 晚安（按用户活跃时段择时）——确定性纯函数。

陪伴型 AI 对标竞品（Replika / 星野）的留存核心：除了"沉默 N 小时才主动"
（见 ``companion_proactive``），还要有**每天固定的仪式感问候**——清晨一句早安、睡前
一句晚安，像真的有人每天惦记着 TA。这是日活（DAU）留存的关键钩子，本产品此前缺失。

与 ``companion_proactive`` 互补：
- 那条是**沉默驱动**（久未联系才回访某条记忆）；本模块是**时段驱动**（每天到点问候）。
- 共用同一发送回路 / 情绪护栏 / care 去重；但**每日每档去重**（一天最多一句早安、一句晚安）。

设计（与 ``plan_proactive_sends`` 同范式）：
- ``plan_daily_rituals`` 是**确定性纯函数**：给定会话快照 + 已发表 + 时钟 + 注入式 opener，
  决定本 tick 该给谁道早/晚安。零 IO、可单测。
- **个性化择时**：注入 ``active_hours_provider`` 时，按该用户历史消息的活跃时段推断 TA 习惯的
  晨/晚点，只在那个点问候（早起的人 7 点收到、夜猫子 23 点收到）；无历史 → 退回配置窗口起点。
- **默认关**：上层 ``companion.proactive_topic.daily_ritual.enabled`` 控。

**用户时钟（注入式，默认关）**：不传 ``user_clock_provider`` 时全按服务器本地钟判定
（＝本能力上线前的逐位等价行为）；传了则**每个会话各按对方的钟**算「现在是不是晨/晚档」
与「今天是哪天」——客户遍布多时区，服务器钟的 7 点是曼谷的 6 点、伦敦的前一天深夜，
拿一个全局小时给所有人道早安必然发到别人半夜。见 ``src/companion/user_clock.py``。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.companion.user_clock import schedule_clock, shift_hours_to_clock

logger = logging.getLogger(__name__)

MORNING = "morning"
NIGHT = "night"

# 推断活跃时段时的「晨 / 晚」带（小时，闭区间集合）——比问候窗口宽，用于读懂用户作息。
_MORNING_BAND = set(range(5, 12))      # 5..11
_NIGHT_BAND = set(range(18, 24))       # 18..23


def window_hours(window: Any, *, default_start: int, default_end: int) -> List[int]:
    """把 (start, end) 问候窗口（闭开区间 [start, end)，支持跨午夜）展开成小时列表。

    例：morning (7,10)→[7,8,9]；night (21,24)→[21,22,23]；跨午夜 (23,2)→[23,0,1]。
    非法/缺省 → 用 default。
    """
    try:
        start = int(window[0]) % 24
        end = int(window[1]) % 24
    except (TypeError, ValueError, IndexError):
        start, end = int(default_start) % 24, int(default_end) % 24
    if start == end:
        return [start]
    hrs: List[int] = []
    h = start
    # 最多绕一圈，防御性封顶 24 步
    for _ in range(24):
        if h == end:
            break
        hrs.append(h)
        h = (h + 1) % 24
    return hrs or [start]


def current_slot(
    hour: int,
    *,
    morning_window: Any = (7, 10),
    night_window: Any = (21, 24),
) -> Optional[str]:
    """当前小时落在哪个仪式档（morning / night），都不在 → None。morning 优先。"""
    h = int(hour) % 24
    if h in window_hours(morning_window, default_start=7, default_end=10):
        return MORNING
    if h in window_hours(night_window, default_start=21, default_end=24):
        return NIGHT
    return None


def infer_active_hour(hour_samples: Any, slot: str) -> Optional[int]:
    """从历史消息小时直方图推断该用户在某档的习惯活跃点。无样本落在带内 → None。

    morning：取晨带 [5,12) 内出现最多的小时（并列取最早，照顾早起者先收到）；
    night：取晚带 [18,24) 内最多的小时（并列取最晚，夜猫子晚点收到）。
    """
    s = str(slot or "").strip().lower()
    band = _MORNING_BAND if s == MORNING else _NIGHT_BAND if s == NIGHT else None
    if band is None:
        return None
    counts: Dict[int, int] = {}
    for raw in hour_samples or []:
        try:
            h = int(raw) % 24
        except (TypeError, ValueError):
            continue
        if h in band:
            counts[h] = counts.get(h, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    cands = [h for h, c in counts.items() if c == best]
    return min(cands) if s == MORNING else max(cands)


def _target_hour(
    slot: str,
    window: Any,
    *,
    default_start: int,
    default_end: int,
    samples: Optional[List[int]],
) -> int:
    """该用户本档应被问候的**唯一小时**：个性化活跃点（落在窗口内才采纳），否则窗口起点。"""
    hrs = window_hours(window, default_start=default_start, default_end=default_end)
    if samples is not None:
        pref = infer_active_hour(samples, slot)
        if pref is not None and pref in hrs:
            return pref
    return hrs[0]


def _resolve_user_clock(
    provider: Optional[Callable[[str], Optional[Any]]], cid: str,
) -> Optional[Any]:
    """注入式取该会话的用户时钟；无 provider / 解析失败 → None（＝服务器钟旧行为）。

    provider 是 IO（读收件箱/记忆），一个坏会话绝不能炸掉整个 tick 的规划。
    """
    if provider is None:
        return None
    try:
        return provider(cid)
    except Exception:
        logger.debug("[ritual] user_clock_provider 失败 cid=%s", cid, exc_info=True)
        return None


def plan_daily_rituals(
    conversations: List[Dict[str, Any]],
    *,
    ritual_sent: Dict[str, float],
    opener_fn: Callable[..., Dict[str, Any]],
    now: Optional[float] = None,
    morning_window: Any = (7, 10),
    night_window: Any = (21, 24),
    min_intimacy: float = 20.0,
    min_quiet_gap_hours: float = 3.0,
    max_per_tick: int = 5,
    has_pending_care: Optional[Callable[[str], bool]] = None,
    active_hours_provider: Optional[Callable[[str], List[int]]] = None,
    user_clock_provider: Optional[Callable[[str], Optional[Any]]] = None,
    active_utc_hours_provider: Optional[Callable[[str], List[int]]] = None,
) -> List[Dict[str, Any]]:
    """决定本 tick 该给谁道早 / 晚安（确定性纯函数）。非问候时段 → 空。

    Args:
        conversations: 会话快照（同 ``plan_proactive_sends``：conversation_id/platform/
            account_id/chat_key/last_ts/last_direction/archived/memory_key/stage/intimacy/
            last_emotion）。
        ritual_sent: ``{ritual_key: ts}``，``ritual_key=f"{cid}:{daykey}:{slot}"``——每日每档去重。
        opener_fn: ``opener_fn(slot=, memory_key=, stage=, intimacy=, last_emotion=,
            contact_key=) -> {mode, directive, fact, ...}``（即 build_ritual_opener；
            含情绪护栏：危机→blocked、低落→克制问候）。
        active_hours_provider: **遗留**可选 ``(cid) -> [hour,...]``（该用户历史消息的**服务器
            本地**小时）；提供则个性化择时。跨时区不正确（服务器小时无法反映对方作息），
            新接线请用 ``active_utc_hours_provider``；两者同时给时后者优先。
        user_clock_provider: 可选 ``(cid) -> UserClock|None``。**给了就换判定基准**：
            晨/晚档、``day_key``、目标小时全按该会话的用户钟算（故不再有「服务器不在问候
            时段就整 tick 空」的全局早退——不同时区的用户各有各的窗口）。解析在
            archived/cid/intimacy 等便宜过滤**之后**才做，被筛掉的会话零成本；provider
            抛异常按无时钟处理（退化＝旧行为），绝不让一个坏会话炸掉整个 tick。
        active_utc_hours_provider: 可选 ``(cid) -> [UTC 小时,...]``；经
            ``shift_hours_to_clock`` 换算到该会话时钟的本地小时后再推断习惯活跃点
            （无时钟时换算到服务器本地＝与遗留 provider 同口径）。
        min_intimacy: 低于此亲密度不问候（不对刚认识的人道"早安亲爱的"）。
        min_quiet_gap_hours: 距上次互动不足此小时 → 不问候（人还在场，道早晚安多余）。

    Returns:
        计划列表（同 plan_proactive_sends 形状 + ``slot/ritual_key`` + 观测字段
        ``clock_source/clock_offset/local_hour``），按亲密度降序截断。
    """
    now = now if now is not None else time.time()
    # 无用户时钟 → 全局服务器钟早退（逐位等价旧行为，且非问候时段零 per-conv 成本）。
    # 有用户时钟 → 全局早退必须撤掉：服务器不在窗口内不代表对方不在，判定下沉到每会话。
    server_slot: Optional[str] = None
    server_hour = 0
    server_day_key = ""
    if user_clock_provider is None:
        lt = time.localtime(now)
        server_hour = lt.tm_hour
        server_slot = current_slot(
            server_hour, morning_window=morning_window, night_window=night_window)
        if server_slot is None:
            return []  # 非晨 / 晚问候时段
        server_day_key = time.strftime("%Y%m%d", lt)

    plans: List[Dict[str, Any]] = []
    for c in conversations or []:
        if not isinstance(c, dict) or c.get("archived"):
            continue
        cid = str(c.get("conversation_id") or "")
        if not cid:
            continue
        try:
            intimacy = float(c.get("intimacy") or 0.0)
        except (TypeError, ValueError):
            intimacy = 0.0
        if intimacy < float(min_intimacy):
            continue  # 关系太浅，不做仪式问候
        # 用户时钟接管择时（解析故意排在便宜过滤之后：被亲密度筛掉的会话不付 IO 代价）
        clock: Optional[Any] = None
        if user_clock_provider is None:
            hour, day_key, slot = server_hour, server_day_key, server_slot
        else:
            clock = _resolve_user_clock(user_clock_provider, cid)
            hour, day_key, _offset = schedule_clock(clock, now)
            slot = current_slot(
                hour, morning_window=morning_window, night_window=night_window)
            if slot is None:
                continue  # 对方那边此刻不在晨 / 晚问候时段
        # day_key 取**用户钟下的日历日**：「一天最多一句早安」按对方的一天算才是正确语义
        ritual_key = f"{cid}:{day_key}:{slot}"
        if ritual_key in (ritual_sent or {}):
            continue  # 今天这一档已问候过
        # 与 proactive_care 去重：已排关怀的会话让路（care 优先、更具体）
        if has_pending_care is not None:
            try:
                if has_pending_care(cid):
                    continue
            except Exception:
                logger.debug("[ritual] has_pending_care 失败 cid=%s", cid, exc_info=True)
        # 人还在场（刚聊过）→ 不必道早 / 晚安
        try:
            last_ts = float(c.get("last_ts") or 0)
        except (TypeError, ValueError):
            last_ts = 0.0
        if last_ts > 0 and (now - last_ts) / 3600.0 < float(min_quiet_gap_hours):
            continue
        # 个性化择时：只在该用户本档的目标小时问候（个性化活跃点或窗口起点）
        samples = None
        if active_utc_hours_provider is not None:
            # UTC 小时 → 该会话时钟的本地小时（无时钟则换算到服务器本地＝遗留口径）
            try:
                samples = shift_hours_to_clock(
                    list(active_utc_hours_provider(cid) or []), clock)
            except Exception:
                logger.debug("[ritual] active_utc_hours_provider 失败 cid=%s",
                             cid, exc_info=True)
                samples = None
        elif active_hours_provider is not None:
            try:
                samples = list(active_hours_provider(cid) or [])
            except Exception:
                samples = None
        target = _target_hour(
            slot, morning_window if slot == MORNING else night_window,
            default_start=7 if slot == MORNING else 21,
            default_end=10 if slot == MORNING else 24,
            samples=samples,
        )
        if hour != target:
            continue
        try:
            opener = opener_fn(
                slot=slot,
                memory_key=str(c.get("memory_key") or ""),
                stage=str(c.get("stage") or ""),
                intimacy=intimacy,
                last_emotion=str(c.get("last_emotion") or ""),
                last_emotion_intensity=float(c.get("last_emotion_intensity") or -1.0),
                contact_key=cid,
            ) or {}
        except Exception:
            logger.debug("[ritual] opener_fn 失败 cid=%s", cid, exc_info=True)
            continue
        mode = str(opener.get("mode") or "")
        directive = str(opener.get("directive") or "")
        if not mode or not directive:
            continue  # 被情绪护栏拦下（危机）或无文案 → 不问候
        plans.append({
            "conversation_id": cid,
            "platform": str(c.get("platform") or ""),
            "account_id": str(c.get("account_id") or ""),
            "chat_key": str(c.get("chat_key") or ""),
            "mode": mode,
            "directive": directive,
            "fact": str(opener.get("fact") or ""),
            "context_facts": [
                str(f).strip() for f in (opener.get("context_facts") or [])
                if str(f).strip()
            ],
            "scenario_id": "",
            "feature": "",
            "slot": slot,
            "ritual_key": ritual_key,
            "intimacy": round(intimacy, 1),
            # 观测/排障：这条计划是按谁的钟算出来的（server=服务器钟；其余为推断源），
            # 时钟偏移快照，以及真正用于择时的本地小时。
            "clock_source": str(getattr(clock, "source", "") or "server"),
            "clock_offset": round(float(getattr(clock, "offset_hours", 0.0) or 0.0), 1),
            "local_hour": hour,
        })

    plans.sort(key=lambda p: p["intimacy"], reverse=True)
    return plans[: max(0, int(max_per_tick))]


__all__ = [
    "MORNING",
    "NIGHT",
    "window_hours",
    "current_slot",
    "infer_active_hour",
    "plan_daily_rituals",
]
