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
import zlib
from datetime import datetime as _dt
from typing import Any, Callable, Dict, List, Optional, Tuple

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


# ── 仪式未回退避（2026-08-18）───────────────────────────────────────────────
# 实锤根因：no_reply_backoff（P0 2026-07-29）只装在沉默回访链上，而仪式问候占
# 主动发送 96.7% 且完全没有退避——从不回复的用户每天照收 2 条早晚安（14 天
# ~290 条未回仪式消息）。语义：连续 N 个「仪式日」（发过仪式问候的自然日）
# 对方零回复 → 按阶梯降频（隔天 → 每 4 天 → 每周封顶），且降频期每天至多一档
# （不再早晚双发）；对方任何一条入站立即恢复每日节奏。
# 与 topic 链退避同哲学（只减发送不增、默认开、确定性可单测），但独立阶梯——
# 仪式是长期陪伴钩子，曲线比 3^n 指数更温和，且永不彻底停（月频语义交给
# 可见性墙/opt-out，那是另一层）。

# (连续未回仪式日 ≥ N 天, 每 stride 天允许一次)；从高档往低档匹配。
DEFAULT_BACKOFF_TIERS: Tuple[Tuple[int, int], ...] = ((14, 7), (7, 4), (3, 2))


def parse_ritual_backoff_cfg(
    ritual_cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 ``companion.proactive_topic.daily_ritual.no_reply_backoff`` 解析。

    缺省 enabled=true（防骚扰护栏惯例：只减发送）；``tiers`` 可覆写
    ``[[天数, 间隔], ...]``（非法条目忽略，全非法回默认阶梯）。
    """
    rc = ritual_cfg or {}
    blk = rc.get("no_reply_backoff") if isinstance(
        rc.get("no_reply_backoff"), dict) else {}
    tiers: List[Tuple[int, int]] = []
    raw = blk.get("tiers")
    if isinstance(raw, (list, tuple)):
        for item in raw:
            try:
                d, s = int(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if d > 0 and s > 1:
                tiers.append((d, s))
    tiers.sort(reverse=True)
    return {
        "enabled": bool(blk.get("enabled", True)),
        "tiers": tuple(tiers) if tiers else DEFAULT_BACKOFF_TIERS,
    }


def _build_sent_index(
    ritual_sent: Dict[str, float],
) -> Dict[str, List[Tuple[float, str]]]:
    """把冷却表 ``{f"{cid}:{daykey}:{slot}": ts}`` 按会话分组为 ``{cid: [(ts, daykey)]}``。

    cid 本身含冒号（如 ``telegram:acct:chat``）→ 从右侧拆两段，剩余即完整 cid。
    整表只扫一遍（规划每 tick 调用，冷却表会随月份增长）。
    """
    index: Dict[str, List[Tuple[float, str]]] = {}
    for k, ts in (ritual_sent or {}).items():
        parts = str(k).rsplit(":", 2)
        if len(parts) != 3:
            continue
        try:
            tsf = float(ts)
        except (TypeError, ValueError):
            continue
        index.setdefault(parts[0], []).append((tsf, parts[1]))
    return index


def ritual_reply_streak_days(
    sent_entries: List[Tuple[float, str]], last_in_ts: float,
) -> int:
    """最后一次入站之后已发出仪式问候的**天数**（distinct daykey）。

    对方回过话（入站晚于某天的仪式）→ 该天不计 → 一开口 streak 自然归零。
    ``last_in_ts=0``（从未开口/未知）→ 全部仪式日计入（对零互动用户最严）。
    """
    try:
        li = float(last_in_ts or 0.0)
    except (TypeError, ValueError):
        li = 0.0
    days = {dk for ts, dk in (sent_entries or []) if ts > li}
    return len(days)


def ritual_backoff_stride(
    streak_days: int,
    tiers: Tuple[Tuple[int, int], ...] = DEFAULT_BACKOFF_TIERS,
) -> int:
    """连续未回仪式日 → 发送间隔（天）；未达最低档返回 1（每日，旧行为）。"""
    try:
        sd = int(streak_days or 0)
    except (TypeError, ValueError):
        return 1
    for min_days, stride in tiers or DEFAULT_BACKOFF_TIERS:
        if sd >= int(min_days):
            return max(1, int(stride))
    return 1


def ritual_backoff_allows(
    conversation_id: str, day_key: str, stride: int,
) -> bool:
    """降频期今天是否轮到该会话：``(日序数 + crc32(cid)) % stride == 0``。

    确定性按**日**掷签（15min tick 重掷会把「跳过」磨成「延迟 15 分钟」）；
    crc 盐把不同会话错开到不同天，避免全员同日恢复。day_key 异常按放行
    （拿不准宁可保持旧行为，不做新的静默故障源）。
    """
    s = max(1, int(stride or 1))
    if s <= 1:
        return True
    try:
        ordinal = _dt.strptime(str(day_key), "%Y%m%d").date().toordinal()
    except (TypeError, ValueError):
        return True
    salt = zlib.crc32(str(conversation_id or "").encode("utf-8", "ignore"))
    return (ordinal + salt) % s == 0


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
    backoff_cfg: Optional[Dict[str, Any]] = None,
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
        backoff_cfg: ``parse_ritual_backoff_cfg`` 产物（None=不退避，旧行为）。
            连续 ≥3 个仪式日零回复 → 阶梯降频 + 每天至多一档；判据用快照
            ``last_in_ts``（与 topic 链退避同源，未知=0 按最严算，保守方向）。

    Returns:
        计划列表（同 plan_proactive_sends 形状 + ``slot/ritual_key`` + 观测字段
        ``clock_source/clock_offset/local_hour``；退避启用时带 ``reply_streak_days``），
        按亲密度降序截断。
    """
    now = now if now is not None else time.time()
    _bk_on = bool(backoff_cfg and backoff_cfg.get("enabled"))
    _bk_tiers = tuple(
        (backoff_cfg or {}).get("tiers") or DEFAULT_BACKOFF_TIERS)
    _bk_min_days = min(
        (int(d) for d, _s in _bk_tiers), default=3) if _bk_on else 0
    sent_index = _build_sent_index(ritual_sent or {}) if _bk_on else {}
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
        # 未回退避：连续 ≥N 个仪式日零回复 → 降频期每天至多一档 + 阶梯掷签
        streak_days = 0
        if _bk_on:
            try:
                _li = float(c.get("last_in_ts") or 0.0)
            except (TypeError, ValueError):
                _li = 0.0
            streak_days = ritual_reply_streak_days(
                sent_index.get(cid) or [], _li)
            if streak_days >= _bk_min_days:
                other = MORNING if slot == NIGHT else NIGHT
                if f"{cid}:{day_key}:{other}" in (ritual_sent or {}):
                    continue  # 降频期不再早晚双发（另一档今天已发）
                stride = ritual_backoff_stride(streak_days, _bk_tiers)
                if not ritual_backoff_allows(cid, day_key, stride):
                    continue  # 阶梯降频：今天不轮到该会话
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
            # 未回退避观测：本条计划时该会话的连续未回仪式日（退避关=恒 0）
            "reply_streak_days": streak_days,
        })

    plans.sort(key=lambda p: p["intimacy"], reverse=True)
    # 「0=不限」语义（与 plan_proactive_sends 同口径）：max_per_tick<=0 不截断。
    # 旧实现 ``plans[:0]`` 会把「不限」变成「全不发」。
    cap = int(max_per_tick or 0)
    return plans if cap <= 0 else plans[:cap]


__all__ = [
    "MORNING",
    "NIGHT",
    "DEFAULT_BACKOFF_TIERS",
    "window_hours",
    "current_slot",
    "infer_active_hour",
    "parse_ritual_backoff_cfg",
    "ritual_reply_streak_days",
    "ritual_backoff_stride",
    "ritual_backoff_allows",
    "plan_daily_rituals",
]
