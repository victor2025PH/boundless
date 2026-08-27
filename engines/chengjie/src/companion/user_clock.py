"""用户侧时区推断 + 主动触达调度策略的单一事实源（纯函数、零 IO、绝不 raise）。

背景：主动触达（早晚安 ritual / 节日问候 / 久未联系回访）全部锚定**服务器本地时区**
（UTC+8），而客户遍布 18 个语种、十几个时区——「早安」发到对方半夜是常态。本模块推断
「用户住哪、此刻几点」，并把「推断可不可以用来改调度」的安全决策一次性封装掉。

与 `persona_location` 的语义边界（勿混）：
- `persona_location` = **AI 人设**住哪（人设自称的城市，用于自身作息/天气/出图一致性）；
- `user_clock`      = **客户**住哪（推断值，用于择时/安静时段/节日日历）。
两者共用 `CITY_PRESETS` / `infer_place_from_text` / `daypart_label`，但绝不共享状态。

## 为什么要 trust 三档（本模块最重要的设计）

时区推断必然会错。错误的代价**不对称**：把「本可发送的时刻」判成安静时段只是少发一条，
把「对方的凌晨 3 点」判成可发时段则是骚扰 + 拉黑。所以按证据强度分三档，各配不同的
使用权限：

- ``TRUST_REPLACE``（显式信号：客户自己说了城市 / 单时区国家的电话国码）
  可**完全取代**服务器钟。理由：服务器钟本来就只是对用户时钟的拙劣代理，一旦拿到真信号，
  代理就该退场；此时再叠加服务器钟只会把两个时区的安静时段并起来，白白削掉可发窗口。
- ``TRUST_NARROW``（行为推断：从入站消息的 UTC 小时分布反推）
  **只能收窄发送窗口，绝不新开**——服务器钟安静 **或** 用户钟安静都算安静。理由：行为推断
  是统计量，可能整体偏移几小时；若允许它「新开」窗口（服务器钟安静但用户钟不安静就发），
  一次错误推断就会造出**新的凌晨骚扰**；只收窄则最坏结果是少发几条，风险单调不增。
  这条不变量是行为推断敢于默认启用的全部前提。
- ``TRUST_ADVISORY``（弱信号：语种默认国家 / 多时区国家的国码）
  **不参与调度**，只供节日日历与展示。理由：语种猜国家的错误率高（英语/西语/葡语/法语跨洲
  通用），而多时区国家（US/CA/AU/ID/RU/BR/MX/KZ）的国码根本定位不到时区。

## 其他设计要点

- **naive 本地时间铁律**：`user_now` 一律返回 naive datetime（与 `persona_location.persona_now`
  一致）——下游 strftime / crc / 比较全按 naive 消费，混入 aware 会在比较处炸。
- **可注入 now、确定性**：所有消费函数吃 `now`（unix 秒或 datetime）；不读配置、不读 DB、
  不发 HTTP，同输入同输出。
- **热路径绝不 raise**：任何异常吞掉并回落到「等价旧行为」（服务器钟 / None / 空串），
  即最坏情况退化成本模块上线前的行为。
- **零新依赖**：只用 stdlib（zoneinfo/datetime/math/re/threading/time/dataclasses）。
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from src.companion.persona_location import (
    CITY_PRESETS,
    daypart_label,
    infer_place_from_text,
)

__all__ = [
    "TRUST_REPLACE",
    "TRUST_NARROW",
    "TRUST_ADVISORY",
    "UserClock",
    "ACTIVITY_PRIOR",
    "PHONE_CC_COUNTRY",
    "infer_from_stated_place",
    "infer_from_phone",
    "infer_from_activity",
    "corroborate",
    "infer_from_language",
    "resolve_user_clock",
    "user_now",
    "user_local_hour",
    "user_day_key",
    "user_month_day",
    "user_time_line",
    "build_tz_bridge_line",
    "in_quiet_hours",
    "schedule_clock",
    "shift_hours_to_clock",
    "city_ask_eligible",
    "dump_stats",
    "reset_stats_for_tests",
]

# ── trust 三档（语义见模块 docstring）────────────────────────────────────
TRUST_REPLACE = "replace"      # 显式信号：可完全取代服务器钟
TRUST_NARROW = "narrow"        # 行为推断：只能收窄发送窗口，绝不新开
TRUST_ADVISORY = "advisory"    # 弱信号：只供节日/展示，不参与调度

# 各推断源的置信度常量（0..1，衡量的是「时钟」维度而非「国家」维度的把握）。
_CONF_STATED = 0.92        # 客户亲口说的城市
_CONF_CORROBORATED = 0.88  # 行为 × 区域 两个独立弱信号互证
_CONF_PHONE_TZ = 0.85      # 单时区国家的电话国码
_CONF_PHONE_COUNTRY = 0.5  # 多时区国家：国家可信、时钟无法定位 → 降档
_CONF_LANG = 0.35          # 语种默认国家
_CONF_BEHAVIOR_MIN = 0.5   # 行为推断下限
_CONF_BEHAVIOR_MAX = 0.78  # 行为推断上限：**必须 < 0.8**，永远不到显式信号档


@dataclass(frozen=True)
class UserClock:
    """客户所在时区的推断结果（不可变）。

    - ``tz_name``：IANA 名（``"Asia/Bangkok"``）或固定偏移伪名（``"UTC+07:00"``）；
      未知（只知国家的弱信号）为 ``""``。
    - ``offset_hours``：**创建时刻**的 UTC 偏移快照。DST 会漂，故所有消费函数一律优先
      按 ``tz_name`` 实时换算，本字段只用于固定偏移伪时区与观测展示。
    - ``source``：``stated_city`` | ``phone_cc`` | ``behavior`` | ``behavior_corroborated``
      | ``lang_default``。
    - ``trust``：调度使用权限，见模块 docstring 的三档语义。
    """

    tz_name: str
    offset_hours: float
    source: str
    confidence: float
    country: str
    city_slug: str
    trust: str

    @property
    def is_fixed_offset(self) -> bool:
        """是否固定偏移伪时区（行为推断产物，无 DST 信息）。"""
        try:
            return str(self.tz_name or "").upper().startswith("UTC")
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 观测（进程级计数，风格对齐 weather_state）
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_STATS: Dict[str, int] = {
    "resolved_stated": 0,
    "resolved_phone": 0,
    "resolved_behavior": 0,
    "resolved_corroborated": 0,
    "resolved_lang": 0,
    "unresolved": 0,
    "quiet_narrowed": 0,
    "quiet_replaced": 0,
}


def _record_stat(key: str, n: int = 1) -> None:
    """best-effort 计数；未知键忽略，绝不影响热路径。"""
    try:
        with _LOCK:
            if key in _STATS:
                _STATS[key] += int(n)
    except Exception:
        pass


def dump_stats() -> Dict[str, int]:
    """导出计数快照（ops 卡/metrics 消费）。"""
    with _LOCK:
        return dict(_STATS)


def reset_stats_for_tests() -> None:
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0


# ---------------------------------------------------------------------------
# 国码 / 国家 / 语种 静态表
# ---------------------------------------------------------------------------

# 已知多时区国家：**永不**进 _COUNTRY_TZ，国码只能定位到国家（供节日），定位不到时钟。
# CN 例外：全境行政统一 Asia/Shanghai，按单时区国处理。
_MULTI_TZ_COUNTRIES: FrozenSet[str] = frozenset({
    "US", "CA", "AU", "ID", "RU", "BR", "MX", "KZ",
    # 同理但不在规格枚举里的真·多时区国（同一条规则的自然延伸）
    "MN", "CD", "GL", "KI", "PF", "PG", "FM",
})

# 单时区国家 → IANA 名。口径：以**本土主时区**代表；海外离岛少数派（西班牙加那利 / 葡萄牙
# 亚速尔 / 智利复活节岛 / 厄瓜多尔加拉帕戈斯 / 新西兰查塔姆）不单列——本土占比 >95%，
# 少数派最多差 1-2 小时，且 trust=replace 下只影响安静时段边界，代价可接受。
_COUNTRY_TZ: Dict[str, str] = {
    # 东亚 / 大中华
    "CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "MO": "Asia/Macau",
    "TW": "Asia/Taipei", "JP": "Asia/Tokyo", "KR": "Asia/Seoul",
    # 东南亚
    "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh", "PH": "Asia/Manila",
    "SG": "Asia/Singapore", "MY": "Asia/Kuala_Lumpur", "KH": "Asia/Phnom_Penh",
    "LA": "Asia/Vientiane", "MM": "Asia/Yangon", "BN": "Asia/Brunei",
    "TL": "Asia/Dili",
    # 南亚
    "IN": "Asia/Kolkata", "PK": "Asia/Karachi", "BD": "Asia/Dhaka",
    "LK": "Asia/Colombo", "NP": "Asia/Kathmandu", "AF": "Asia/Kabul",
    "MV": "Indian/Maldives",
    # 中东
    "AE": "Asia/Dubai", "SA": "Asia/Riyadh", "QA": "Asia/Qatar",
    "KW": "Asia/Kuwait", "BH": "Asia/Bahrain", "OM": "Asia/Muscat",
    "IL": "Asia/Jerusalem", "JO": "Asia/Amman", "LB": "Asia/Beirut",
    "IQ": "Asia/Baghdad", "IR": "Asia/Tehran", "SY": "Asia/Damascus",
    "YE": "Asia/Aden", "TR": "Europe/Istanbul",
    # 中亚 / 高加索
    "UZ": "Asia/Tashkent", "TJ": "Asia/Dushanbe", "TM": "Asia/Ashgabat",
    "KG": "Asia/Bishkek", "AZ": "Asia/Baku", "GE": "Asia/Tbilisi",
    "AM": "Asia/Yerevan",
    # 非洲
    "EG": "Africa/Cairo", "ZA": "Africa/Johannesburg", "NG": "Africa/Lagos",
    "KE": "Africa/Nairobi", "GH": "Africa/Accra", "MA": "Africa/Casablanca",
    "DZ": "Africa/Algiers", "TN": "Africa/Tunis", "LY": "Africa/Tripoli",
    "ET": "Africa/Addis_Ababa", "TZ": "Africa/Dar_es_Salaam",
    "UG": "Africa/Kampala", "ZM": "Africa/Lusaka", "ZW": "Africa/Harare",
    # 欧洲
    "GB": "Europe/London", "IE": "Europe/Dublin", "FR": "Europe/Paris",
    "DE": "Europe/Berlin", "IT": "Europe/Rome", "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon", "NL": "Europe/Amsterdam", "BE": "Europe/Brussels",
    "CH": "Europe/Zurich", "AT": "Europe/Vienna", "PL": "Europe/Warsaw",
    "CZ": "Europe/Prague", "SK": "Europe/Bratislava", "HU": "Europe/Budapest",
    "RO": "Europe/Bucharest", "BG": "Europe/Sofia", "GR": "Europe/Athens",
    "SE": "Europe/Stockholm", "NO": "Europe/Oslo", "DK": "Europe/Copenhagen",
    "FI": "Europe/Helsinki", "EE": "Europe/Tallinn", "LV": "Europe/Riga",
    "LT": "Europe/Vilnius", "UA": "Europe/Kyiv", "BY": "Europe/Minsk",
    "RS": "Europe/Belgrade", "HR": "Europe/Zagreb", "SI": "Europe/Ljubljana",
    # 大洋洲
    "NZ": "Pacific/Auckland", "FJ": "Pacific/Fiji",
    # 美洲（单一偏移国）
    "AR": "America/Argentina/Buenos_Aires", "CL": "America/Santiago",
    "PE": "America/Lima", "CO": "America/Bogota", "VE": "America/Caracas",
    "EC": "America/Guayaquil", "BO": "America/La_Paz", "PY": "America/Asuncion",
    "UY": "America/Montevideo", "CU": "America/Havana",
    "GT": "America/Guatemala", "SV": "America/El_Salvador",
    "HN": "America/Tegucigalpa", "NI": "America/Managua",
    "CR": "America/Costa_Rica", "PA": "America/Panama",
    "DO": "America/Santo_Domingo", "JM": "America/Jamaica",
}

# 国际电话区号 → ISO-3166 alpha-2 国家码（最长前缀匹配 3→2→1）。
# 与 `src/ai/lang_prior.PHONE_CC_LANG`（国码→语言）正交，刻意不改那张表。
# NANP 坑：国码 "1" 是美加及加勒比共用，"7" 是俄哈共用——两组都是多时区国，本就只出
# advisory 不参与调度，故按人口主体记 US / RU，节日维度的误差可接受。
PHONE_CC_COUNTRY: Dict[str, str] = {
    # 北美（NANP，多时区）
    "1": "US",
    # 俄罗斯 / 哈萨克（多时区）
    "7": "RU",
    # 非洲
    "20": "EG", "27": "ZA", "212": "MA", "213": "DZ", "216": "TN",
    "218": "LY", "233": "GH", "234": "NG", "251": "ET", "254": "KE",
    "255": "TZ", "256": "UG", "260": "ZM", "263": "ZW",
    # 欧洲
    "30": "GR", "31": "NL", "32": "BE", "33": "FR", "34": "ES",
    "36": "HU", "39": "IT", "40": "RO", "41": "CH", "43": "AT",
    "44": "GB", "45": "DK", "46": "SE", "47": "NO", "48": "PL",
    "49": "DE", "351": "PT", "353": "IE", "358": "FI", "359": "BG",
    "370": "LT", "371": "LV", "372": "EE", "375": "BY", "380": "UA",
    "381": "RS", "385": "HR", "386": "SI", "420": "CZ", "421": "SK",
    # 拉美
    "51": "PE", "52": "MX", "53": "CU", "54": "AR", "55": "BR",
    "56": "CL", "57": "CO", "58": "VE", "502": "GT", "503": "SV",
    "504": "HN", "505": "NI", "506": "CR", "507": "PA", "591": "BO",
    "593": "EC", "595": "PY", "598": "UY", "809": "DO", "876": "JM",
    # 东南亚 / 大洋洲
    "60": "MY", "61": "AU", "62": "ID", "63": "PH", "64": "NZ",
    "65": "SG", "66": "TH", "84": "VN", "670": "TL", "673": "BN",
    "679": "FJ", "855": "KH", "856": "LA", "95": "MM",
    # 东亚
    "81": "JP", "82": "KR", "86": "CN", "852": "HK", "853": "MO",
    "886": "TW", "976": "MN",
    # 南亚
    "91": "IN", "92": "PK", "93": "AF", "94": "LK", "880": "BD",
    "960": "MV", "977": "NP",
    # 中东 / 中亚 / 高加索
    "90": "TR", "961": "LB", "962": "JO", "963": "SY", "964": "IQ",
    "965": "KW", "966": "SA", "967": "YE", "968": "OM", "971": "AE",
    "972": "IL", "973": "BH", "974": "QA", "98": "IR",
    "992": "TJ", "993": "TM", "994": "AZ", "995": "GE", "996": "KG",
    "998": "UZ", "374": "AM",
}

# 语种 → 默认国家。en/es/pt/fr **刻意留空**：跨洲通用语，猜国家的错误率远高于收益
# （英语客户可能在 US/GB/PH/SG/AU/NG…，猜错就是把节日与时钟一起带偏）。
_LANG_COUNTRY: Dict[str, str] = {
    "th": "TH", "vi": "VN", "ko": "KR", "ja": "JP", "id": "ID",
    "ms": "MY", "tl": "PH", "km": "KH", "hi": "IN", "ar": "AE",
    "tr": "TR", "ru": "RU", "he": "IL", "zh": "CN", "de": "DE",
    "it": "IT",
    "en": "", "es": "", "pt": "", "fr": "",
}

_WEEKDAYS_ZH: Tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WEEKDAYS_EN: Tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------------------
# 活跃度先验（行为推断的似然模板）
# ---------------------------------------------------------------------------

# 人类聊天活跃度先验（**局部时钟**下的小时分布）：凌晨 3-5 点谷底、晚 19-22 点峰值。
# 数值为规格给定原样值，导入时归一化保证和为 1。
ACTIVITY_PRIOR: Tuple[float, ...] = (
    0.030, 0.018, 0.010, 0.006, 0.005, 0.007, 0.014, 0.026,
    0.038, 0.046, 0.050, 0.052, 0.054, 0.052, 0.052, 0.053,
    0.055, 0.058, 0.060, 0.064, 0.068, 0.070, 0.060, 0.045,
)


def _normalized_log_prior(prior: Tuple[float, ...]) -> Tuple[float, ...]:
    total = float(sum(prior))
    return tuple(math.log(float(p) / total) for p in prior)


_LOG_PRIOR: Tuple[float, ...] = _normalized_log_prior(ACTIVITY_PRIOR)


def _circ_hour_dist(a: int, b: int) -> int:
    """两个整点偏移的**环形**距离（模 24）。

    必须环形：候选区间 [-11, +14] 里 -11≡+13、-10≡+14（模 24 完全等价），线性距离会
    把「同一个解的另一个写法」当成次优解 → margin 恒为 0 → 那两档偏移的用户永远推不出来。
    """
    d = abs(int(a) - int(b)) % 24
    return min(d, 24 - d)


def _max_separation(log_prior: Tuple[float, ...]) -> float:
    """先验能提供的**每样本最大可分辨度**（margin 的归一化分母）。

    含义：把全部样本压在最有利的一个局部小时上时，最优偏移与「至少差 2 小时」的次优偏移
    之间的每样本对数似然差上界。本先验下 ≈0.125 —— 也就是说未归一化的 margin 在数学上
    **永远** ≤0.125，直接拿它跟 0.35 比会让行为推断恒返回 None（见模块 README/汇报）。
    除以本上界后 margin 落在 0..1，0.35 = 「达到理论可分辨度的 35%」，与 confidence 公式
    ``0.5 + 0.5*margin`` 的取值域也自洽。
    """
    best = 0.0
    for h in range(24):
        alt = max(
            log_prior[(h + d) % 24] for d in range(24) if _circ_hour_dist(0, d) >= 2
        )
        best = max(best, log_prior[h] - alt)
    return best if best > 0 else 1.0


_MAX_SEPARATION: float = _max_separation(_LOG_PRIOR)

# 模 24 等价偏移的规范化：候选区间 [-11,+14] 内 -11 与 +13、+14 与 -10 是同一个解的两种
# 写法（行为数据在数学上无从区分）。取该残差下人口占优的真实偏移，保证输出确定且不荒谬：
# +13 = 新西兰夏令时/萨摩亚/汤加（数百万人）> -11 = 美属萨摩亚（数万人）；
# -10 = 夏威夷/大溪地（百万级）> +14 = 基里巴斯线岛（数千人）。
_OFFSET_ALIASES: Dict[int, int] = {-11: 13, 14: -10}


# ---------------------------------------------------------------------------
# 时区工具
# ---------------------------------------------------------------------------

def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _as_utc(now: Any) -> datetime:
    """把 None / unix 秒 / datetime（aware 或 naive）统一成 aware UTC 时刻。

    naive datetime 按**服务器本地**解释（与 persona_location.persona_now 同口径）。
    """
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        base = now if now.tzinfo is not None else now.astimezone()
        return base.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(now), timezone.utc)


def _zone(tz_name: str) -> Optional[ZoneInfo]:
    try:
        name = str(tz_name or "").strip()
        return ZoneInfo(name) if name else None
    except Exception:
        return None


def _tz_offset_hours(tz_name: str, now: Any = None) -> Optional[float]:
    """IANA 时区在指定时刻的实际 UTC 偏移（小时，DST 正确）；无效 → None。"""
    try:
        zone = _zone(tz_name)
        if zone is None:
            return None
        off = _as_utc(now).astimezone(zone).utcoffset()
        return None if off is None else off.total_seconds() / 3600.0
    except Exception:
        return None


def _clock_tzinfo(clock: Optional[UserClock]):
    """UserClock → tzinfo；固定偏移伪名走 timezone(timedelta)，无 tz_name → None。"""
    try:
        if clock is None or not clock.tz_name:
            return None
        if clock.is_fixed_offset:
            return timezone(timedelta(hours=float(clock.offset_hours)))
        return _zone(clock.tz_name)
    except Exception:
        return None


def _fixed_offset_name(offset: int) -> str:
    """整点偏移 → 固定偏移伪时区名（"UTC+07:00" / "UTC-08:00"）。"""
    return f"UTC{int(offset):+03d}:00"


def _server_offset_hours(now: Any = None) -> float:
    """服务器本地时区在指定时刻的 UTC 偏移（小时）；异常 → 0.0。"""
    try:
        return _as_utc(now).astimezone().utcoffset().total_seconds() / 3600.0  # type: ignore[union-attr]
    except Exception:
        return 0.0


def _clock_from_slug(slug: str, now: Any = None) -> Optional[UserClock]:
    """CITY_PRESETS slug → 显式信号档 UserClock。"""
    meta = CITY_PRESETS.get(str(slug or ""))
    if not meta:
        return None
    tz_name = str(meta.get("tz") or "")
    off = _tz_offset_hours(tz_name, now)
    if off is None:
        return None
    return UserClock(
        tz_name=tz_name,
        offset_hours=off,
        source="stated_city",
        confidence=_CONF_STATED,
        country=str(meta.get("country") or ""),
        city_slug=str(slug),
        trust=TRUST_REPLACE,
    )


def _country_tz(country: str) -> str:
    """单时区国家 → IANA 名；多时区国/未知 → ""（结构性保证多时区国推不出时钟）。"""
    code = str(country or "").strip().upper()
    if not code or code in _MULTI_TZ_COUNTRIES:
        return ""
    return _COUNTRY_TZ.get(code, "")


def _country_city_zones(country: str) -> List[str]:
    """该国在 CITY_PRESETS 里的全部时区（去重排序）；corroborate 的多时区国候选集。"""
    code = str(country or "").strip().upper()
    if not code:
        return []
    return sorted({
        str(meta.get("tz") or "")
        for meta in CITY_PRESETS.values()
        if str(meta.get("country") or "").upper() == code and meta.get("tz")
    })


# ---------------------------------------------------------------------------
# 推断器 1：客户自述地点
# ---------------------------------------------------------------------------

def _match_preset_name(text: str) -> Optional[str]:
    """精确名匹配预置城市：slug（空白/连字符归一）→ city_zh / city_en（大小写不敏感）。"""
    try:
        s = str(text).strip().lower()
        if not s:
            return None
        slug_norm = re.sub(r"[\s\-]+", "_", s)
        if slug_norm in CITY_PRESETS:
            return slug_norm
        for slug, meta in CITY_PRESETS.items():
            if s == str(meta.get("city_zh", "")).lower() or s == str(meta.get("city_en", "")).lower():
                return slug
    except Exception:
        return None
    return None


def _stated_clock(text: Any, now: Any = None) -> Optional[UserClock]:
    try:
        if not isinstance(text, str) or not text.strip():
            return None
        slug = _match_preset_name(text) or infer_place_from_text(text)
        return _clock_from_slug(slug, now) if slug else None
    except Exception:
        return None


def infer_from_stated_place(text: Any) -> Optional[UserClock]:
    """客户亲口说的地点 → 最强档时钟（``stated_city`` / 0.92 / replace）。

    「我在曼谷」「Based in Manila」这类自述是**用户自己给的事实**，比任何统计推断都硬，
    故直接 replace 服务器钟。识别复用 persona_location 的中英城市词表（人设侧与用户侧
    共用同一份地名知识，但结果绝不互串）。无法识别/垃圾输入 → None。
    """
    return _stated_clock(text, None)


# ---------------------------------------------------------------------------
# 推断器 2：电话国码
# ---------------------------------------------------------------------------

def _phone_digits(raw: Any) -> str:
    """从任意字符串抠出 E.164 数字段（7-15 位）；抠不出 → ""。

    兼容 baileys JID（``639171234567@s.whatsapp.net``、设备位 ``…:12@…``）与人手输入的
    ``+66 81 234 5678``。⚠️ 调用方必须只喂**真电话号码**：Telegram 的数字 user_id 形似
    号码（lang_prior 为此专门加了平台白名单），误喂会推出完全错误的国家。
    """
    try:
        s = str(raw or "").strip()
        if not s:
            return ""
        s = s.split("@", 1)[0].split(":", 1)[0]
        digits = re.sub(r"\D", "", s)
        return digits if 7 <= len(digits) <= 15 else ""
    except Exception:
        return ""


def _phone_clock(raw: Any, now: Any = None) -> Optional[UserClock]:
    try:
        digits = _phone_digits(raw)
        if not digits:
            return None
        country = ""
        for ln in (3, 2, 1):
            code = digits[:ln]
            if code in PHONE_CC_COUNTRY:
                country = PHONE_CC_COUNTRY[code]
                break
        if not country:
            return None
        tz_name = _country_tz(country)
        off = _tz_offset_hours(tz_name, now) if tz_name else None
        if tz_name and off is not None:
            return UserClock(
                tz_name=tz_name, offset_hours=off, source="phone_cc",
                confidence=_CONF_PHONE_TZ, country=country, city_slug="",
                trust=TRUST_REPLACE,
            )
        # 多时区国家（US/CA/AU/ID/RU/BR/MX/KZ…）：国码定位得到国家，定位不到时钟。
        return UserClock(
            tz_name="", offset_hours=0.0, source="phone_cc",
            confidence=_CONF_PHONE_COUNTRY, country=country, city_slug="",
            trust=TRUST_ADVISORY,
        )
    except Exception:
        return None


def infer_from_phone(raw: Any) -> Optional[UserClock]:
    """电话国码 → 时钟（单时区国）或仅国家（多时区国）。

    **关键护栏**：多时区国家绝不由国码推时区——一个 "+1" 号码可能在 UTC-5 也可能在
    UTC-10，猜一个就是 5 小时误差的凌晨骚扰。这类只返回 ``country`` + ``tz_name=""`` +
    ``trust=advisory``（够节日日历用，不参与调度）；单时区国家给明确 IANA 名 + replace。
    """
    return _phone_clock(raw, None)


# ---------------------------------------------------------------------------
# 推断器 3：入站活跃度（最大似然模板匹配）
# ---------------------------------------------------------------------------

def _histogram(utc_hour_counts: Any) -> Dict[int, int]:
    """归一成 {UTC 小时: 计数}；支持原始小时序列与直方图两种输入，垃圾条目静默丢弃。"""
    out: Dict[int, int] = {}
    try:
        if isinstance(utc_hour_counts, Mapping):
            items: Iterable[Tuple[Any, Any]] = utc_hour_counts.items()
        elif isinstance(utc_hour_counts, (str, bytes)) or utc_hour_counts is None:
            return {}
        else:
            items = ((h, 1) for h in utc_hour_counts)
        for hour, count in items:
            try:
                h = int(hour)
                c = int(count)
            except Exception:
                continue
            if not (0 <= h <= 23) or c <= 0:
                continue
            out[h] = out.get(h, 0) + c
    except Exception:
        return {}
    return out


def infer_from_activity(
    utc_hour_counts: Any,
    *,
    min_samples: int = 24,
    min_margin: float = 0.35,
) -> Optional[UserClock]:
    """从入站消息的 UTC 小时分布反推时区偏移（最大似然模板匹配）。

    算法：对候选整点偏移 ``o ∈ [-11, +14]`` 计算
    ``score(o) = Σ_h count[h] · ln(PRIOR[(h+o) mod 24])``（PRIOR = 人类聊天活跃度先验，
    局部时钟），取 argmax。用似然模板而非圆均值，是因为活跃分布不对称（凌晨谷 vs 晚间峰），
    圆均值会被跨午夜的长尾拽偏。

    三道采纳门槛（任一不过 → None，宁可不推也不乱推）：
    a. 总样本 ≥ ``min_samples``；
    b. 出现过的不同小时数 ≥ 4（全挤在一两个小时的直方图对时区毫无信息，
       却能凑出很高的 margin —— 必须单独挡掉）；
    c. ``margin ≥ min_margin``，margin = 最优与次优（**排除环形距离 ≤1 的邻居**）的
       每样本对数似然差，再除以先验的理论最大可分辨度归一到 0..1。排除邻居是因为
       ±1 小时的偏移本来就无从分辨，把它当次优会让所有输入都「边际不足」。

    产出 ``trust=narrow`` 且 ``confidence`` 硬上限 0.78（< 0.8）：统计推断永远不进显式
    信号档，下游据此只收窄发送窗口、绝不新开（见模块 docstring）。
    """
    try:
        counts = _histogram(utc_hour_counts)
        total = sum(counts.values())
        if total < int(min_samples) or len(counts) < 4:
            return None
        scores: Dict[int, float] = {
            o: sum(c * _LOG_PRIOR[(h + o) % 24] for h, c in counts.items())
            for o in range(-11, 15)
        }
        # 同分时取 |offset| 更小者（更靠近 UTC = 更常见），再取更小者：确定性 tie-break。
        best_off = max(scores, key=lambda o: (scores[o], -abs(o), -o))
        runner = max(
            (v for o, v in scores.items() if _circ_hour_dist(o, best_off) > 1),
            default=None,
        )
        if runner is None:
            return None
        margin = (scores[best_off] - runner) / (float(total) * _MAX_SEPARATION)
        if margin < float(min_margin):
            return None
        offset = _OFFSET_ALIASES.get(best_off, best_off)
        confidence = _clamp(
            0.5 + 0.5 * min(margin, 1.0), _CONF_BEHAVIOR_MIN, _CONF_BEHAVIOR_MAX)
        return UserClock(
            tz_name=_fixed_offset_name(offset),
            offset_hours=float(offset),
            source="behavior",
            confidence=confidence,
            country="",
            city_slug="",
            trust=TRUST_NARROW,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 推断器 4：交叉验证升格
# ---------------------------------------------------------------------------

def corroborate(
    behavior: Optional[UserClock],
    region: Optional[UserClock],
    *,
    now: Optional[datetime] = None,
) -> Optional[UserClock]:
    """行为推断 × 区域信号 互证 → 升格为显式档。

    两个**互相独立**的弱信号（活跃时段统计 / 号码归属地）指向同一个偏移时，误判概率是两者
    之积，已强于任一单独信号 → 升 ``trust=replace`` / 0.88。更实际的收益是顺带拿到真 IANA
    名：固定偏移伪时区不懂夏令时，换成 ``Asia/Bangkok`` 这类真名后 DST 白拿。

    region 只有国家没时区时：单时区国直接用其 IANA 名；多时区国用 CITY_PRESETS 里该国的
    城市逐个试，**只有全部候选城市当前偏移都等于行为偏移**才升格（否则 "+1" 里 -7 与 -4
    并存，升格等于瞎猜）。不满足一律原样返回 behavior —— 只升不降，绝不因互证失败而丢掉
    已有的行为推断。
    """
    try:
        if behavior is None:
            return None
        if region is None:
            return behavior
        candidates: List[str] = []
        if region.tz_name and not region.is_fixed_offset:
            candidates = [region.tz_name]
        elif region.country:
            single = _country_tz(region.country)
            candidates = [single] if single else _country_city_zones(region.country)
        if not candidates:
            return behavior
        offsets = [_tz_offset_hours(tz, now) for tz in candidates]
        if any(off is None for off in offsets):
            return behavior
        if any(abs(float(off) - float(behavior.offset_hours)) > 1e-6 for off in offsets):
            return behavior
        return UserClock(
            tz_name=candidates[0],
            offset_hours=float(behavior.offset_hours),
            source="behavior_corroborated",
            confidence=_CONF_CORROBORATED,
            country=str(region.country or ""),
            city_slug=str(region.city_slug or ""),
            trust=TRUST_REPLACE,
        )
    except Exception:
        return behavior


# ---------------------------------------------------------------------------
# 推断器 5：语种默认国家
# ---------------------------------------------------------------------------

def _norm_lang(lang: Any) -> str:
    """语言码归一：``"zh-CN"`` / ``"ZH_Hans"`` → ``"zh"``；非法 → ""。"""
    try:
        s = str(lang or "").strip().lower()
        if not s:
            return ""
        m = re.match(r"^([a-z]{2,3})", s)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _language_clock(lang: Any, now: Any = None) -> Optional[UserClock]:
    try:
        code = _norm_lang(lang)
        if not code or code not in _LANG_COUNTRY:
            return None
        country = _LANG_COUNTRY[code]
        if not country:
            return None  # en/es/pt/fr：刻意不猜
        tz_name = _country_tz(country)
        off = _tz_offset_hours(tz_name, now) if tz_name else None
        return UserClock(
            tz_name=tz_name if off is not None else "",
            offset_hours=float(off) if off is not None else 0.0,
            source="lang_default",
            confidence=_CONF_LANG,
            country=country,
            city_slug="",
            trust=TRUST_ADVISORY,
        )
    except Exception:
        return None


def infer_from_language(lang: Any) -> Optional[UserClock]:
    """会话语种 → 默认国家（最弱档，``advisory``，只供节日/展示）。

    泰语用户几乎一定在泰国，这条信息对**节日日历**很值钱；但语种猜国家的错误率不足以支撑
    改调度，故 trust 固定 advisory。en/es/pt/fr 是跨洲通用语，**刻意返回 None**：猜错代价
    （节日全错 + 误导 corroborate）大于收益。
    """
    return _language_clock(lang, None)


# ---------------------------------------------------------------------------
# 汇总解析
# ---------------------------------------------------------------------------

def resolve_user_clock(
    *,
    stated_place: Any = None,
    phone: Any = None,
    activity_hours: Any = None,
    language: Any = None,
    min_samples: int = 24,
    min_margin: float = 0.35,
    now: Optional[datetime] = None,
) -> Optional[UserClock]:
    """多源汇总出唯一时钟（调用方的单一入口）。

    优先级：自述地点 → 有时区的电话国码 → 行为×区域互证 → 纯行为 → 仅国家的电话国码 →
    语种默认。**信息合并**：高优先级项拿到时区却没国家（行为推断天生没国家）时，从低优先级
    项补 ``country``（不改 trust/confidence）—— 让「时钟」与「节日」各取所长，而不是二选一。
    """
    try:
        stated = _stated_clock(stated_place, now)
        phone_clock = _phone_clock(phone, now)
        lang_clock = _language_clock(language, now)
        behavior = infer_from_activity(
            activity_hours, min_samples=min_samples, min_margin=min_margin)

        chosen: Optional[UserClock] = None
        stat = "unresolved"
        if stated is not None:
            chosen, stat = stated, "resolved_stated"
        elif phone_clock is not None and phone_clock.tz_name:
            chosen, stat = phone_clock, "resolved_phone"
        elif behavior is not None:
            merged = corroborate(behavior, phone_clock or lang_clock, now=now) or behavior
            chosen = merged
            stat = ("resolved_corroborated"
                    if merged.source == "behavior_corroborated" else "resolved_behavior")
        elif phone_clock is not None:
            chosen, stat = phone_clock, "resolved_phone"
        elif lang_clock is not None:
            chosen, stat = lang_clock, "resolved_lang"

        if chosen is not None and not chosen.country:
            for cand in (stated, phone_clock, lang_clock):
                if cand is not None and cand.country:
                    chosen = replace(chosen, country=cand.country)
                    break
        _record_stat(stat)
        return chosen
    except Exception:
        _record_stat("unresolved")
        return None


# ---------------------------------------------------------------------------
# 时钟消费（naive 本地时间，绝不返回 aware）
# ---------------------------------------------------------------------------

def user_now(clock: Optional[UserClock], now: Optional[float | datetime] = None) -> datetime:
    """客户所在时区此刻的 **naive** 本地时间（铁律：绝不返回 aware）。

    ``now`` 支持 unix 秒 float 或 datetime（naive 视为服务器本地时刻）；``clock`` 为 None、
    只有国家没时区、或时区无效时逐位回落服务器本地时间 = 与本模块上线前完全一致的旧行为。
    """
    try:
        tz = _clock_tzinfo(clock)
        if tz is None:
            if now is None:
                return datetime.now()
            return _as_utc(now).astimezone().replace(tzinfo=None)
        return _as_utc(now).astimezone(tz).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def user_local_hour(clock: Optional[UserClock], now: Optional[float | datetime] = None) -> int:
    """客户当地小时（0-23）；异常回落服务器当前小时。"""
    try:
        return int(user_now(clock, now).hour)
    except Exception:
        return int(datetime.now().hour)


def user_day_key(clock: Optional[UserClock], now: Optional[float | datetime] = None) -> str:
    """客户钟下的日历日键 ``"%Y%m%d"``（每日只发一次早安之类的去重键）。"""
    try:
        return user_now(clock, now).strftime("%Y%m%d")
    except Exception:
        return datetime.now().strftime("%Y%m%d")


def user_month_day(clock: Optional[UserClock], now: Optional[float | datetime] = None) -> str:
    """客户钟下的 ``"%m-%d"``（节日判定：对方的 12-25 不是服务器的 12-25）。"""
    try:
        return user_now(clock, now).strftime("%m-%d")
    except Exception:
        return datetime.now().strftime("%m-%d")


def user_time_line(
    clock: Optional[UserClock],
    lang: str = "zh",
    now: Optional[float | datetime] = None,
) -> str:
    """一行「对方那边现在几点」内部事实块（注入 prompt）。

    只有 ``trust == replace``（显式信号）才注入；narrow/advisory → ""。
    2026-08-19 事故同根收口：裸行为推断把国内客户猜成 UTC-5（0.78 置信、
    country=CN 自相矛盾）——narrow 猜出的时间进 prompt，LLM 就会在对方上午
    说「这么晚了早点睡」，与仪式档位错位是同一类内容错误。宁可不注入。
    """
    try:
        if clock is None or clock.trust != TRUST_REPLACE:
            return ""
        if _clock_tzinfo(clock) is None:
            return ""
        dt = user_now(clock, now)
        part = daypart_label(dt.hour, lang)
        if str(lang or "zh").lower().startswith("zh"):
            return (
                f"【对方当地时间（内部事实）】对方那边现在约 {dt:%Y-%m-%d} "
                f"{_WEEKDAYS_ZH[dt.weekday()]} {dt:%H:%M}（{part}）。"
                "别报时间戳，只在自然相关时体现作息；这是推断值，不要当确定信息复述。"
            )
        return (
            f"[Their local time — internal] It's about {dt:%Y-%m-%d} "
            f"{_WEEKDAYS_EN[dt.weekday()]} {dt:%H:%M} ({part}) where they are. "
            "Never quote the timestamp; reflect their daily rhythm only when it fits "
            "naturally — this is an inference, don't repeat it as fact."
        )
    except Exception:
        return ""


def build_tz_bridge_line(
    persona_now: Optional[datetime],
    peer_now: Optional[datetime],
    lang: str = "zh",
) -> str:
    """「时差桥」prompt 行（纯函数）：两侧当地时间都已知且明显错位时，把时差
    从穿帮源变成真实感资产。

    背景（2026-08-02 WA 实录）：温哥华人设周六傍晚 × 客户周日凌晨——两侧的
    星期/时段各自都对，但 LLM 在没有显式桥接指令时会随机混用两个框架
    （上一句 "Saturday evening"、下一句借客户的 "Sunday" 说自己），读者视角
    就是自相矛盾。桥接指令钉死「自己的状态用自己的钟表述」，并鼓励自然点破
    时差（真人异地聊天本来就会说「你那边该是周日早上了吧」）。

    触发条件：两个 naive datetime 都在 && （星期不同 或 小时差 ≥ 3）；
    否则返回 ""（同城/近时区注入桥接反而是噪音）。
    """
    try:
        if not isinstance(persona_now, datetime) or not isinstance(peer_now, datetime):
            return ""
        wd_diff = persona_now.weekday() != peer_now.weekday()
        # 小时差取环上最短距离（23 点 vs 1 点 = 2 小时，不是 22）
        raw = abs(persona_now.hour - peer_now.hour)
        hr_diff = min(raw, 24 - raw)
        if not wd_diff and hr_diff < 3:
            return ""
        p_part = daypart_label(persona_now.hour, lang)
        u_part = daypart_label(peer_now.hour, lang)
        if str(lang or "zh").lower().startswith("zh"):
            return (
                f"【时差桥（内部事实）】你和对方不在同一时区：你那边是"
                f"{_WEEKDAYS_ZH[persona_now.weekday()]}{persona_now:%H:%M}（{p_part}），"
                f"对方那边约 {_WEEKDAYS_ZH[peer_now.weekday()]}{peer_now:%H:%M}（{u_part}）。"
                "谈「今天/明天/周末/几点」时：**你自己的状态只按你的当地钟说**，"
                "对方的作息按对方的钟聊；可以自然点破时差（如「你那边该是"
                "周日早上了吧，我这边还是周六傍晚」）——这更像真的异地聊天；"
                "但绝不要用对方的星期/时段来描述你自己正在做的事。"
            )
        return (
            f"[Timezone bridge — internal] You two are in different timezones: "
            f"for you it's {_WEEKDAYS_EN[persona_now.weekday()]} "
            f"{persona_now:%H:%M} ({p_part}); for them it's about "
            f"{_WEEKDAYS_EN[peer_now.weekday()]} {peer_now:%H:%M} ({u_part}). "
            "When talking about today/tomorrow/the weekend, describe YOUR state "
            "strictly on your own clock and theirs on their clock. Feel free to "
            "acknowledge the gap naturally (e.g. 'it must be Sunday morning over "
            "there — still Saturday evening here'); never borrow their weekday or "
            "daypart to describe what you yourself are doing."
        )
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 调度策略（安全语义全部收口在这里）
# ---------------------------------------------------------------------------

def _window_contains(hour: int, start: int, end: int) -> bool:
    """小时是否落在 [start, end) 窗口内；``start == end`` → 恒 False（不设安静时段）。"""
    s = int(start) % 24
    e = int(end) % 24
    h = int(hour) % 24
    if s == e:
        return False
    if s < e:
        return s <= h < e
    return h >= s or h < e  # 跨午夜（如 23..8）


def in_quiet_hours(
    clock: Optional[UserClock],
    now_ts: float,
    *,
    quiet_start: int,
    quiet_end: int,
    server_hour: Optional[int] = None,
) -> bool:
    """当前是否处于安静时段（主动触达的唯一闸门）。

    逐档语义（**本模块的安全核心**）：
    - ``clock is None`` / ``advisory``：只看服务器钟 = 完全的旧行为，弱信号一票不投；
    - ``narrow``（行为推断）：服务器钟安静 **或** 用户钟安静都算安静。**只收窄，绝不新开**
      —— 推错了最多少发几条，不可能造出「新的凌晨骚扰」；
    - ``replace``（显式信号）：**只看用户钟**。服务器钟只是对用户钟的拙劣代理，有真信号
      就该退场；此时再叠加服务器钟只会把两地安静时段并起来，白削可发窗口。

    计数口径：``quiet_narrowed`` = narrow 档**真的**因用户钟而多判出安静的次数（收窄生效）；
    ``quiet_replaced`` = replace 档接管判定的次数。异常一律回落服务器钟。
    """
    try:
        if int(quiet_start) % 24 == int(quiet_end) % 24:
            return False
        if server_hour is None:
            sh = int(time.localtime(float(now_ts)).tm_hour)
        else:
            sh = int(server_hour)
        server_quiet = _window_contains(sh, quiet_start, quiet_end)
        if clock is None or clock.trust == TRUST_ADVISORY:
            return server_quiet
        if _clock_tzinfo(clock) is None:
            return server_quiet
        user_quiet = _window_contains(
            user_local_hour(clock, float(now_ts)), quiet_start, quiet_end)
        if clock.trust == TRUST_NARROW:
            if user_quiet and not server_quiet:
                _record_stat("quiet_narrowed")
            return server_quiet or user_quiet
        if clock.trust == TRUST_REPLACE:
            _record_stat("quiet_replaced")
            return user_quiet
        return server_quiet
    except Exception:
        try:
            return _window_contains(
                int(time.localtime().tm_hour), quiet_start, quiet_end)
        except Exception:
            return False


# 与服务器默认钟（本机 UTC+8）差这么多才算「行为时钟像海外」——
# 2026-08-19 晚安事故那条是 UTC-5 vs +8 = 13h；3h 正好盖住东亚 vs 西亚/欧洲。
_CITY_ASK_MIN_SHIFT = 3.0
_CITY_ASK_SERVER_OFFSET = 8.0
_ZH_LANG_RE = re.compile(r"^(zh|cmn|yue|wuu)\b", re.IGNORECASE)


def city_ask_eligible(
    clock: Optional[UserClock],
    *,
    language: str = "",
    server_offset: float = _CITY_ASK_SERVER_OFFSET,
    min_shift_hours: float = _CITY_ASK_MIN_SHIFT,
) -> Tuple[bool, str]:
    """是否该借 gentle_checkin 问居住城市。返回 ``(ok, reason)``。

    只问「没有 replace 时钟」且「像海外」的会话——不是人人都问：

    - 已有自述城市 / 单时区国码 → ``already_replace``（时钟已可信）
    - 会话语种非中文 → ``foreign_lang``
    - 行为时钟相对服务器钟偏移 ≥ ``min_shift_hours``（含 8/19 那条
      ``behavior UTC-5 + country=CN`` 自相矛盾）→ ``behavior_shift``
    - 其余（国内中文、无信号）→ ``not_overseas``

    纯函数、绝不 raise。上层再叠亲密度 / 冷却 / 槽位已知。
    """
    try:
        if clock is not None and str(getattr(clock, "trust", "") or "") == TRUST_REPLACE:
            return False, "already_replace"
        lang = str(language or "").strip().lower()
        if lang in ("unknown", "und", "auto", "null", "none"):
            lang = ""
        if lang and not _ZH_LANG_RE.match(lang):
            return True, "foreign_lang"
        source = str(getattr(clock, "source", "") or "") if clock is not None else ""
        if source in ("behavior", "behavior_corroborated"):
            off = getattr(clock, "offset_hours", None)
            if off is not None and abs(float(off) - float(server_offset)) >= float(min_shift_hours):
                return True, "behavior_shift"
        return False, "not_overseas"
    except Exception:
        return False, "error"


def schedule_clock(
    clock: Optional[UserClock],
    now_ts: float,
) -> Tuple[int, str, float]:
    """择时用的 ``(小时, day_key, offset_hours)``。

    **只有 ``trust == replace``（显式信号：自述城市/号码国码/行为得到佐证）才把
    择时基准换成用户钟**；narrow（裸行为推断）与 advisory 一律服务器钟、offset 0.0。

    2026-08-19 10:12 生产事故把旧赌注证伪：旧实现让 narrow 也驱动择时（理由
    「挪到对方白天=收窄，越界防线在 in_quiet_hours」）——但 5 条稀疏活跃样本把
    国内客户推断成 UTC-5（连 country=CN 都自相矛盾），晚安仪式在服务器上午
    10:12 发出「外面雨声哗哗的，被子裹紧点哈，晚安🌙」。错钟驱动档位选择产生的
    是**内容错误**（上午说晚安），安静时段防线管不到这一层；timing 优化绝不值
    这个险。offset 按 ``now_ts`` 实时算（DST 正确），而非用创建时的快照。
    """
    try:
        tz = _clock_tzinfo(clock)
        if clock is not None and tz is not None and clock.trust == TRUST_REPLACE:
            dt = _as_utc(now_ts).astimezone(tz)
            off = dt.utcoffset()
            offset = (off.total_seconds() / 3600.0) if off is not None else float(clock.offset_hours)
            return (int(dt.hour), dt.strftime("%Y%m%d"), float(offset))
        local = _as_utc(now_ts).astimezone().replace(tzinfo=None)
        return (int(local.hour), local.strftime("%Y%m%d"), 0.0)
    except Exception:
        fallback = datetime.now()
        return (int(fallback.hour), fallback.strftime("%Y%m%d"), 0.0)


def shift_hours_to_clock(utc_hours: Any, clock: Optional[UserClock]) -> List[int]:
    """UTC 小时序列 → 该时钟下的本地小时序列（个性化择时的直方图用）。

    ``clock`` 为 None / 无可用时区 → 换算到**服务器本地**，即等价旧行为。半小时时区
    （印度 +5:30）向下取整到所在整点。非序列输入（Mapping/字符串）与垃圾条目静默丢弃。
    """
    out: List[int] = []
    try:
        if utc_hours is None or isinstance(utc_hours, (str, bytes, Mapping)):
            return out
        tz = _clock_tzinfo(clock)
        if tz is not None:
            off = _tz_offset_hours(clock.tz_name) if not clock.is_fixed_offset else None  # type: ignore[union-attr]
            if off is None:
                off = float(clock.offset_hours)  # type: ignore[union-attr]
        else:
            off = _server_offset_hours()
        for raw in utc_hours:
            try:
                h = int(raw)
            except Exception:
                continue
            if not (0 <= h <= 23):
                continue
            out.append(int(math.floor(h + float(off))) % 24)
    except Exception:
        return out
    return out
