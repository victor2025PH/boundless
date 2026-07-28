"""纪念日·节日仪式：认识 N 天 / 节日问候（事件驱动）——确定性纯函数。

在每日晨/晚安（``daily_ritual``）之上，再加**高情感节点**的仪式问候——对标 Replika /
星野的「我们在一起 N 天了」「节日快乐」这类**记得重要日子**的体验。与每日仪式互补：
- 每日仪式：**时段驱动**（每天到点道早/晚安）。
- 本模块：**事件/日期驱动**（只在认识满 N 天那天 / 节日当天，到点问候一次）。

三类事件：
- **生日**（最高优先）：经注入式 ``birthday_provider`` 从该用户记忆扫出 (月,日)，当天问候一次。
- **认识 N 天纪念日**：用「会话首次建立时间」(``first_seen_ts``，≈ 首次接触）算认识天数，
  命中配置里的里程碑（7/30/100/365/520/1314…）那天问候一次。
- **节日**：当天 (月-日) 命中配置日历则问候（内置公历常见节日；农历逐年变，留给配置覆盖）。

去重：复用 ``ritual_key`` 机制（与 daily_ritual 同一冷却表）——
- 生日 ``{cid}:ms:birthday:{year}``（每年只一次）；
- 纪念日 ``{cid}:ms:anniversary:{N}``（每个 N 永久只一次）；
- 节日 ``{cid}:ms:holiday:{year}:{月-日}``（每年只一次）。
故**无需改 CompanionProactiveLoop**：上层把本计划并进 ritual 计划即可。

设计与 ``plan_daily_rituals`` 同范式：纯函数、零 IO、注入式 opener/时钟、默认关。

**用户时钟 / 地区节日（注入式，默认关）**：不传 ``user_clock_provider`` 与
``locale_holiday_provider`` 时全按服务器本地钟 + 配置日历判定（＝逐位等价旧行为）；
传了则每会话各按对方的钟算「到没到问候整点」「今天是几月几号」，节日改问「**该会话所在
地区**今天过什么节」——泰国用户过宋干节而不是元旦，且对方的 12-25 不是服务器的 12-25。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.companion.user_clock import schedule_clock, user_day_key, user_month_day

logger = logging.getLogger(__name__)

MODE_BIRTHDAY = "milestone_birthday"
MODE_ANNIVERSARY = "milestone_anniversary"
MODE_HOLIDAY = "milestone_holiday"

# 「认识 N 天」默认里程碑：一周 / 一月 / 百日 / 半年 / 一年 / 520 / 千日 / 一生一世。
DEFAULT_ANNIVERSARY_DAYS: Tuple[int, ...] = (7, 30, 100, 180, 365, 520, 1000, 1314)

# 默认节日日历（"月-日" → 名称）。只放**公历固定日期**；农历（春节/中秋等）逐年漂移，
# 不内置（会发错日子），留给上层配置按年覆盖/补充。
DEFAULT_HOLIDAYS: Dict[str, str] = {
    "01-01": "元旦",
    "02-14": "情人节",
    "12-24": "平安夜",
    "12-25": "圣诞节",
    "12-31": "跨年夜",
}


def days_known(first_seen_ts: Any, now: float) -> int:
    """认识天数 = ⌊(now - first_seen)/86400⌋。非法/未来/缺省 → -1。"""
    try:
        f = float(first_seen_ts or 0)
    except (TypeError, ValueError):
        return -1
    if f <= 0 or now < f:
        return -1
    return int((now - f) // 86400)


def due_anniversary(
    first_seen_ts: Any, now: float, milestones: Any = DEFAULT_ANNIVERSARY_DAYS,
) -> Optional[int]:
    """今天是否正好认识满某个里程碑天数；是则返回该 N，否则 None。"""
    d = days_known(first_seen_ts, now)
    if d < 0:
        return None
    try:
        ms = {int(m) for m in (milestones or ()) if int(m) > 0}
    except (TypeError, ValueError):
        return None
    return d if d in ms else None


def _holiday_from_calendar(
    month_day: str, calendar: Any = None,
) -> Optional[Tuple[str, str]]:
    """给定 ``"MM-DD"`` 查配置节日日历；命中返回 (月-日, 名称)，否则 None。

    ``holiday_for_date`` 的「日期已算好」版本——用户时钟下的今天不等于服务器的今天，
    但查表口径必须与旧路径完全一致，故两条路共用本函数（单一事实源）。
    """
    cal = calendar if isinstance(calendar, dict) else DEFAULT_HOLIDAYS
    key = str(month_day or "")
    name = cal.get(key)
    if name:
        return key, str(name)
    return None


def holiday_for_date(
    now: float, calendar: Any = None,
) -> Optional[Tuple[str, str]]:
    """今天 (月-日) 是否命中节日日历；命中返回 ("MM-DD", 名称)，否则 None。"""
    return _holiday_from_calendar(
        time.strftime("%m-%d", time.localtime(now)), calendar)


def _birthday_on_date(
    birthday: Any, *, year: int, month: int, day: int,
) -> bool:
    """给定「今天」的 (年,月,日) 判定是否该庆生。

    与 ``src.utils.birthday.is_birthday_today`` 同语义（含 2/29 生日在平年顺延到 2/28），
    只是把「今天」从 ``time.localtime(now)``（服务器钟）换成调用方给的日期——这样同一份
    判定既能用服务器钟也能用用户钟，而无需改动 birthday 模块的既有签名。
    """
    if not isinstance(birthday, (tuple, list)) or len(birthday) < 2:
        return False
    try:
        bmo, bda = int(birthday[0]), int(birthday[1])
    except (TypeError, ValueError):
        return False
    if not (1 <= bmo <= 12 and 1 <= bda <= 31):
        return False
    if (int(month), int(day)) == (bmo, bda):
        return True
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    if bmo == 2 and bda == 29 and not leap:
        return (int(month), int(day)) == (2, 28)
    return False


def _resolve_user_clock(
    provider: Optional[Callable[[str], Optional[Any]]], cid: str,
) -> Optional[Any]:
    """注入式取该会话的用户时钟；无 provider / 解析失败 → None（＝服务器钟旧行为）。"""
    if provider is None:
        return None
    try:
        return provider(cid)
    except Exception:
        logger.debug("[milestone] user_clock_provider 失败 cid=%s", cid, exc_info=True)
        return None


def _resolve_locale_holiday(
    provider: Optional[Callable[[str], Optional[Tuple[str, str]]]], cid: str,
) -> Optional[Tuple[str, str]]:
    """注入式取该会话所在地区今天适合问候的节日 ``(key, 名称)``；无/异常 → None。

    返回 None 表示「该地区今天没有可问候的节日」→ 调用方回落配置日历（绝不因地区
    引擎缺数据就丢掉原有的公历节日能力）。
    """
    if provider is None:
        return None
    try:
        got = provider(cid)
    except Exception:
        logger.debug("[milestone] locale_holiday_provider 失败 cid=%s",
                     cid, exc_info=True)
        return None
    # 必须是 (key, 名称) 二元组：字符串同样可索引（``"x"[0]`` 不报错），宽松取值会把
    # 一个坏返回值变成 key="x" 的假节日 + 一条永久占位的冷却键，故按类型硬校验。
    if not isinstance(got, (tuple, list)) or len(got) < 2:
        return None
    try:
        key = str(got[0] or "").strip()
        name = str(got[1] or "").strip()
        return (key, name) if key and name else None
    except Exception:
        logger.debug("[milestone] locale_holiday_provider 返回值非法 cid=%s",
                     cid, exc_info=True)
        return None


def _detect_event(
    conv: Dict[str, Any],
    now: float,
    *,
    anniversary_milestones: Any,
    holiday: Optional[Tuple[str, str]],
    year: int,
    birthday: Optional[Tuple[int, int]] = None,
    today_md: Optional[Tuple[int, int]] = None,
) -> Optional[Dict[str, Any]]:
    """该会话今天应触发的事件（优先级：生日 > 纪念日 > 节日）；无 → None。

    ``today_md``＝用户钟下今天的 (月, 日)；给了就按它判生日（配合 ``year`` 做闰日顺延），
    没给仍走 ``is_birthday_today``（服务器钟）＝旧行为。
    """
    if birthday is not None:
        if today_md is not None:
            hit = _birthday_on_date(
                birthday, year=year, month=today_md[0], day=today_md[1])
        else:
            from src.utils.birthday import is_birthday_today
            hit = is_birthday_today(birthday, now)
        if hit:
            return {
                "type": "birthday", "mode": MODE_BIRTHDAY,
                "tag": str(year), "days": 0, "label": "生日",
            }
    anniv = due_anniversary(conv.get("first_seen_ts"), now, anniversary_milestones)
    if anniv is not None:
        return {
            "type": "anniversary", "mode": MODE_ANNIVERSARY,
            "tag": str(anniv), "days": anniv, "label": f"认识{anniv}天",
        }
    if holiday is not None:
        hid, hname = holiday
        return {
            "type": "holiday", "mode": MODE_HOLIDAY,
            "tag": f"{year}:{hid}", "days": 0, "label": hname,
        }
    return None


def plan_milestone_rituals(
    conversations: List[Dict[str, Any]],
    *,
    ritual_sent: Dict[str, float],
    opener_fn: Callable[..., Dict[str, Any]],
    now: Optional[float] = None,
    greet_hour: int = 10,
    min_intimacy: float = 20.0,
    max_per_tick: int = 5,
    anniversary_milestones: Any = DEFAULT_ANNIVERSARY_DAYS,
    holiday_calendar: Any = None,
    has_pending_care: Optional[Callable[[str], bool]] = None,
    birthday_provider: Optional[Callable[[str], Optional[Tuple[int, int]]]] = None,
    user_clock_provider: Optional[Callable[[str], Optional[Any]]] = None,
    locale_holiday_provider: Optional[
        Callable[[str], Optional[Tuple[str, str]]]] = None,
) -> List[Dict[str, Any]]:
    """决定本 tick 该给谁发纪念日/节日问候（确定性纯函数）。非问候时点 → 空。

    Args:
        conversations: 会话快照（含 ``first_seen_ts``——会话首次建立时间，认识天数据此算）。
        ritual_sent: ``{ritual_key: ts}`` 去重表（与 daily_ritual 共用一张冷却表，key 不冲突）。
        opener_fn: ``opener_fn(event_type=, event_label=, days=, memory_key=, stage=,
            intimacy=, last_emotion=, contact_key=) -> {mode, directive, fact, ...}``
            （即 build_milestone_opener；含情绪护栏：危机→blocked、低落→克制）。
        greet_hour: 节点问候只在这个整点触发一次（默认 10 点；与晨/晚安窗口错开，避免扎堆）。
        min_intimacy: 低于此亲密度不发（不对刚认识的人庆"认识 100 天"）。
        max_per_tick: 单 tick 上限（按亲密度降序截断）。
        user_clock_provider: 可选 ``(cid) -> UserClock|None``。给了则 ``greet_hour``
            整点判定、节日日期、生日日期全按该会话的用户钟算（替代「服务器不到整点就整
            tick 空」的全局早退）。解析排在 intimacy 等便宜过滤之后；异常按无时钟处理。
        locale_holiday_provider: 可选 ``(cid) -> (key, 名称)|None``——「该会话所在地区、
            今天、适合问候的节日」。命中则**优先于**配置日历（返回 None 才回落配置
            日历）；``ritual_key`` 用节日 key 而非 MM-DD，因为同一天不同地区可能是不同
            节日（同一张冷却表里两地节日不会互相顶掉）。
            纪念日的 ``days_known`` 与时区无关（纯时长差），故不随用户钟改动。

    Returns:
        计划列表（同 plan_daily_rituals 形状 + ``slot/ritual_key/event_type/event_label``
        + 观测字段 ``clock_source/clock_offset/local_hour``）。
        ``slot`` 借用为事件类型，便于上层统一按 ritual_key 记冷却。
    """
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    # 任一 provider 给了 → 逐会话判定（各地的整点/节日各自成立）；都没给 → 逐位旧行为。
    per_conv = (user_clock_provider is not None
                or locale_holiday_provider is not None)
    year = lt.tm_year
    holiday: Optional[Tuple[str, str]] = None
    if not per_conv:
        if lt.tm_hour != int(greet_hour):
            return []  # 非节点问候整点
        holiday = holiday_for_date(now, holiday_calendar)

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
            continue
        # 用户时钟接管（解析故意排在亲密度过滤之后：被筛掉的会话不付 IO 代价）
        clock: Optional[Any] = None
        hour = lt.tm_hour
        today_md: Optional[Tuple[int, int]] = None
        if per_conv:
            clock = _resolve_user_clock(user_clock_provider, cid)
            hour, _day_key, _offset = schedule_clock(clock, now)
            if hour != int(greet_hour):
                continue  # 对方那边还没到（或已过）节点问候整点
            # 节日/生日/年份一律按**用户钟下的今天**算
            ukey = user_day_key(clock, now)
            month_day = user_month_day(clock, now)
            try:
                year = int(ukey[:4])
            except (TypeError, ValueError):
                year = lt.tm_year
            if clock is not None:
                # 无时钟时刻意不走新路径：仍调 is_birthday_today（服务器钟）＝旧行为
                try:
                    today_md = (int(month_day[:2]), int(month_day[3:5]))
                except (TypeError, ValueError):
                    today_md = None
            # 地区节日优先；该地区今天无可问候节日 → 回落配置日历（同一份查表口径）
            holiday = _resolve_locale_holiday(locale_holiday_provider, cid)
            if holiday is None:
                holiday = _holiday_from_calendar(month_day, holiday_calendar)
        # 生日取数（IO，注入式）：仅对通过亲密度门槛的候选查一次，控成本（同 active_hours 范式）。
        bday = None
        if birthday_provider is not None:
            try:
                bday = birthday_provider(str(c.get("memory_key") or ""))
            except Exception:
                bday = None
        event = _detect_event(
            c, now, anniversary_milestones=anniversary_milestones,
            holiday=holiday, year=year, birthday=bday, today_md=today_md)
        if event is None:
            continue
        ritual_key = f"{cid}:ms:{event['type']}:{event['tag']}"
        if ritual_key in (ritual_sent or {}):
            continue  # 这个节点已问候过（纪念日永久 / 节日当年）
        # 与 proactive_care 去重：已排关怀的会话让路（care 更具体、优先）
        if has_pending_care is not None:
            try:
                if has_pending_care(cid):
                    continue
            except Exception:
                logger.debug("[milestone] has_pending_care 失败 cid=%s", cid, exc_info=True)
        try:
            opener = opener_fn(
                event_type=event["type"],
                event_label=event["label"],
                last_emotion_intensity=float(c.get("last_emotion_intensity") or -1.0),
                days=event["days"],
                memory_key=str(c.get("memory_key") or ""),
                stage=str(c.get("stage") or ""),
                intimacy=intimacy,
                last_emotion=str(c.get("last_emotion") or ""),
                contact_key=cid,
            ) or {}
        except Exception:
            logger.debug("[milestone] opener_fn 失败 cid=%s", cid, exc_info=True)
            continue
        mode = str(opener.get("mode") or "")
        directive = str(opener.get("directive") or "")
        if not mode or not directive:
            continue  # 危机护栏拦下或无文案
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
            "slot": event["type"],
            "ritual_key": ritual_key,
            "event_type": event["type"],
            "event_label": event["label"],
            "intimacy": round(intimacy, 1),
            # 观测/排障：按谁的钟判的整点/日期（server=服务器钟）、时钟偏移、本地小时
            "clock_source": str(getattr(clock, "source", "") or "server"),
            "clock_offset": round(float(getattr(clock, "offset_hours", 0.0) or 0.0), 1),
            "local_hour": hour,
        })

    plans.sort(key=lambda p: p["intimacy"], reverse=True)
    return plans[: max(0, int(max_per_tick))]


__all__ = [
    "MODE_BIRTHDAY",
    "MODE_ANNIVERSARY",
    "MODE_HOLIDAY",
    "DEFAULT_ANNIVERSARY_DAYS",
    "DEFAULT_HOLIDAYS",
    "days_known",
    "due_anniversary",
    "holiday_for_date",
    "plan_milestone_rituals",
]
