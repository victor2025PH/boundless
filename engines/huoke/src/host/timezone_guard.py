# -*- coding: utf-8 -*-
"""
时区窗口守卫 — P3-3。

每个目标国家有其最佳发送时段（UTC）。在用户睡觉时发消息不仅无效，
还会触发 TikTok 反垃圾机制（集中在特定UTC时段发消息 = 机器人特征）。

功能:
  1. 判断指定国家当前是否处于活跃时段
  2. 计算距下一个活跃窗口的等待时间
  3. 根据 lead 的 source 国家智能过滤任务

活跃时段定义（本地时间 9:00 - 22:00，周末 10:00-21:00）。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

# 国家 → 标准 UTC 偏移小时（简化，不处理夏令时）
# 夏令时影响通常 ±1h，在 ±1h 的活跃窗口余量内可接受
_COUNTRY_UTC_OFFSET: Dict[str, int] = {
    # 欧洲
    "italy": 1, "france": 1, "spain": 1, "germany": 1,
    "netherlands": 1, "belgium": 1, "austria": 1, "switzerland": 1,
    "portugal": 0, "uk": 0, "ireland": 0,
    "greece": 2, "turkey": 3, "romania": 2, "poland": 1,
    "sweden": 1, "norway": 1, "denmark": 1, "finland": 2,
    "czech": 1, "hungary": 1, "croatia": 1, "serbia": 1,
    # 北美
    "usa": -5, "canada": -5, "mexico": -6,
    # 南美
    "brazil": -3, "argentina": -3, "colombia": -5, "chile": -4, "peru": -5,
    # 中东/北非
    "uae": 4, "saudi_arabia": 3, "israel": 2, "egypt": 2,
    "morocco": 0, "tunisia": 1, "algeria": 1,
    # 亚洲
    "india": 5, "pakistan": 5, "indonesia": 7, "malaysia": 8,
    "philippines": 8, "vietnam": 7, "thailand": 7,
    "japan": 9, "south_korea": 9, "taiwan": 8,
    # 默认
    "default": 1,  # 意大利（主要目标市场）
}

# 工作日活跃窗口（本地时间小时范围，闭区间）
_ACTIVE_HOURS_WEEKDAY: Tuple[int, int] = (9, 22)
# 周末活跃窗口
_ACTIVE_HOURS_WEEKEND: Tuple[int, int] = (10, 21)

# 别名映射（常见写法 → 标准 key）
_ALIASES: Dict[str, str] = {
    "italian": "italy", "french": "france", "spanish": "spain",
    "german": "germany", "english": "uk", "portuguese": "portugal",
    "brazilian": "brazil", "american": "usa", "american english": "usa",
    "indian": "india",
}


def _normalize_country(country: str) -> str:
    c = country.lower().strip()
    return _ALIASES.get(c, c)


def _get_utc_offset(country: str) -> int:
    c = _normalize_country(country)
    return _COUNTRY_UTC_OFFSET.get(c, _COUNTRY_UTC_OFFSET["default"])


def is_country_active(country: str, utc_now: Optional[datetime] = None) -> bool:
    """
    判断目标国家当前是否处于用户活跃时段。

    活跃时段（本地时间）:
    - 工作日: 09:00 ~ 22:00
    - 周末: 10:00 ~ 21:00

    Args:
        country: 国家名（支持中英文和语言名，如 "italy", "italian", "意大利"）
        utc_now: 当前 UTC 时间（默认使用系统时间）

    Returns:
        True = 当前在活跃窗口内，可以发送消息
    """
    if utc_now is None:
        utc_now = datetime.now(timezone.utc)

    offset = _get_utc_offset(country)
    local_now = utc_now + timedelta(hours=offset)
    local_hour = local_now.hour
    is_weekend = local_now.weekday() >= 5  # Saturday=5, Sunday=6

    if is_weekend:
        low, high = _ACTIVE_HOURS_WEEKEND
    else:
        low, high = _ACTIVE_HOURS_WEEKDAY

    return low <= local_hour <= high


def minutes_until_active(country: str, utc_now: Optional[datetime] = None) -> int:
    """
    返回距目标国家下一个活跃窗口开始的分钟数。
    如果当前已在活跃窗口内，返回 0。
    """
    if utc_now is None:
        utc_now = datetime.now(timezone.utc)

    if is_country_active(country, utc_now):
        return 0

    offset = _get_utc_offset(country)
    local_now = utc_now + timedelta(hours=offset)
    is_weekend = local_now.weekday() >= 5

    low, _ = _ACTIVE_HOURS_WEEKEND if is_weekend else _ACTIVE_HOURS_WEEKDAY
    # 计算到下一个活跃开始时刻
    next_active = local_now.replace(hour=low, minute=0, second=0, microsecond=0)
    if next_active <= local_now:
        next_active += timedelta(days=1)
        # 如果次日是周末/工作日边界，重新计算窗口
        is_weekend_next = next_active.weekday() >= 5
        low_next, _ = _ACTIVE_HOURS_WEEKEND if is_weekend_next else _ACTIVE_HOURS_WEEKDAY
        next_active = next_active.replace(hour=low_next)

    diff = (next_active - local_now).total_seconds() / 60
    return max(0, int(diff))


def get_supported_countries() -> List[str]:
    return list(_COUNTRY_UTC_OFFSET.keys())


def best_send_utc_hour(country: str) -> int:
    """返回目标国家的最佳发送 UTC 小时（本地下午2点 = 峰值互动时段）。"""
    offset = _get_utc_offset(country)
    best_local = 14  # 下午2点
    utc_hour = (best_local - offset) % 24
    return utc_hour
