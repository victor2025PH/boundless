"""双侧本地节日引擎——「对方那边今天过什么节 / 我人设这边今天过什么节」的单一事实源。

## 为什么要有这个模块

现有节日能力只有 ``src/utils/milestone_ritual.py::DEFAULT_HOLIDAYS``：5 个**公历固定**
中文节日，且注释明写「农历逐年漂移，不内置，留给配置按年覆盖」。系统面向 18 语种国际
用户、AI 人设常驻海外城市，两个缺口很致命：

1. **用户侧缺本地节日**：泰国用户过宋干节、越南用户过 Tết、美国用户过感恩节——拿中国
   节日表去问候等于文化失礼。
2. **人设侧缺生活纹理**：人设住温哥华，加拿大国庆日街上什么气氛，是「真人感」的免费素材。

两侧语义**刻意不同**（见 ``holiday_fact_line`` 的 ``side``）：用户侧是「对方在过节」，
人设侧是「我自己在过节」，前者用于问候/共情，后者只作生活背景注入。

## 为什么是规则引擎，不是逐年 YAML

最省事的做法是逐年手写公历日期（``2026: 春节=02-17``）。但那意味着**每年年底必须有人
记得来续表**，而运营一定会忘——表一过期，系统不是报错而是**静默地不再有任何节日**，
或更糟：拿去年的日子发今年的祝福。节日数据的腐烂是无声的，所以不能依赖人的记性。

因此这里做**规则引擎**：一次写清「宋干节＝公历 4/13 起 3 天」「春节＝农历正月初一」
「感恩节＝11 月第 4 个周四」「Good Friday＝复活节前 2 天」，此后**永久自动正确**。
仓库已有 ``lunar_python``（命理技能在用）可动态算农历与二十四节气；复活节用
Anonymous Gregorian（Meeus）算法自己实现，零新依赖。

唯一的例外是 ``table``（逐年查表）：伊斯兰历依赖新月观测、希伯来历需要另一套历法实现，
两者都无法用现有依赖算准。这类规则**到了表尽头就让节日自动消失，而不是外推猜日子**
——「今天没节日」是可接受的降级，「把开斋节发错三天」是文化事故。宁可静默失效。

## 设计约束

- 纯函数 + 进程级缓存；不发 HTTP、不读数据库、零新依赖。
- **所有公开函数绝不 raise**：异常一律吞掉回落空值。缺 ``lunar_python`` 时农历/节气规则
  返回 ``[]``，其余规则（fixed/nth_weekday/easter_offset/table）照常工作——一个可选依赖
  缺失不该让整个节日能力归零。
- ``holidays_on`` 会在主动触达 tick 里对**每个会话**调用，故对
  ``(country, year) → {date: (Holiday, ...)}`` 做线程安全的展开索引缓存；单次查询是
  一次 dict 查找，不重算规则。
"""

from __future__ import annotations

import calendar as _calendar
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("LocaleHolidays")

__all__ = [
    "Holiday",
    "DEFAULT_CALENDAR_PATH",
    "SCOPES",
    "RULE_TYPES",
    "COUNTRY_LABELS",
    "load_calendar",
    "country_for_language",
    "holidays_on",
    "upcoming_holidays",
    "holiday_fact_line",
    "resolve_rule",
    "lunar_available",
    "clear_caches_for_tests",
]

# 内置数据文件：相对**本文件**定位（绝不依赖 cwd——生产以服务方式启动，cwd 不可预期）。
DEFAULT_CALENDAR_PATH = str(
    Path(__file__).resolve().parents[2] / "config" / "locale_holidays.yaml"
)

# scope 白名单。排序权重同时决定同一天多节日的展示序：法定假 > 宗教节 > 民俗节
# （法定假最可能被对方真正过到，宗教节次之，民俗节属氛围）。
SCOPES: Tuple[str, ...] = ("public", "religious", "cultural")
_SCOPE_ORDER: Dict[str, int] = {s: i for i, s in enumerate(SCOPES)}

RULE_TYPES: Tuple[str, ...] = (
    "fixed", "lunar", "nth_weekday", "easter_offset", "table", "jieqi",
)

# 国家码 → (中文名, 英文名)。用于 ``holiday_fact_line`` 用户侧的「今天是**泰国**的…」。
# 刻意**不**复用 ``persona_location._COUNTRY_NAMES``：那是私有名（下划线前缀）且只有 20 国，
# 跨模块借私有符号会把别人的内部实现变成我们的公开契约。这里自带一份覆盖策展的 25 国。
COUNTRY_LABELS: Dict[str, Tuple[str, str]] = {
    "AE": ("阿联酋", "the UAE"),
    "AU": ("澳大利亚", "Australia"),
    "CA": ("加拿大", "Canada"),
    "CN": ("中国", "China"),
    "DE": ("德国", "Germany"),
    "FR": ("法国", "France"),
    "GB": ("英国", "the UK"),
    "HK": ("香港", "Hong Kong"),
    "ID": ("印尼", "Indonesia"),
    "IL": ("以色列", "Israel"),
    "IN": ("印度", "India"),
    "IT": ("意大利", "Italy"),
    "JP": ("日本", "Japan"),
    "KH": ("柬埔寨", "Cambodia"),
    "KR": ("韩国", "Korea"),
    "MY": ("马来西亚", "Malaysia"),
    "NZ": ("新西兰", "New Zealand"),
    "PH": ("菲律宾", "the Philippines"),
    "RU": ("俄罗斯", "Russia"),
    "SG": ("新加坡", "Singapore"),
    "TH": ("泰国", "Thailand"),
    "TR": ("土耳其", "Türkiye"),
    "TW": ("台湾", "Taiwan"),
    "US": ("美国", "the US"),
    "VN": ("越南", "Vietnam"),
}

# 单条事实块最多列几个节日（同一天撞多个节时截断，保住 prompt 预算）。
_MAX_FACT_ITEMS = 4
# 一条规则最多展开多少连续天（宋干节 3 天、俄罗斯新年假 5 天、光明节 8 天）。
_MAX_RULE_DAYS = 10
# 展开索引缓存条数上限（25 国 × 若干年，撑满即整清重建，不做 LRU——重建很便宜）。
_INDEX_MAX_ENTRIES = 512


@dataclass(frozen=True)
class Holiday:
    """一个节日的稳定事实。

    ``greet`` 是**运营语义**而非日历事实：能不能给一个陌生/半熟用户主动送祝福。
    春节/圣诞/开斋节/母亲节 → True；政治性（国庆/独立日/革命纪念）、哀悼性（阵亡将士/
    国难日）、纯行政假（银行假日/节礼日）→ False，只作事实注入不群发祝福。判不准一律
    保守设 False——「该祝贺却没祝贺」是遗憾，「在人家的国难日说节日快乐」是事故。
    """

    key: str
    name_zh: str
    name_en: str
    country: str
    scope: str
    greet: bool


# ---------------------------------------------------------------------------
# lunar_python 惰性探测
# ---------------------------------------------------------------------------
# 刻意用**惰性**导入（而非 bazi_engine 的 import-time 探测）：① 缺库时 import 本模块零
# 代价；② 让测试能真实模拟「导入失败」（重置 tried 后 patch __import__ 即可），而不是
# 只 patch 一个布尔标志——后者测不出真实的缺库路径。
_LUNAR_LOCK = threading.Lock()
_LUNAR_STATE: Dict[str, Any] = {"tried": False, "Lunar": None, "Solar": None}


def _lunar_api() -> Tuple[Any, Any]:
    """``(Lunar, Solar)``；缺库/导入异常 → ``(None, None)``（只探一次）。"""
    st = _LUNAR_STATE
    if not st["tried"]:
        with _LUNAR_LOCK:
            if not st["tried"]:
                try:
                    from lunar_python import Lunar, Solar  # type: ignore

                    st["Lunar"], st["Solar"] = Lunar, Solar
                except Exception:
                    logger.debug("lunar_python 不可用，农历/节气规则将返回空", exc_info=True)
                    st["Lunar"] = st["Solar"] = None
                st["tried"] = True
    return st["Lunar"], st["Solar"]


def lunar_available() -> bool:
    """农历/节气规则是否可用（``lunar_python`` 已安装）。"""
    return _lunar_api()[0] is not None


# ---------------------------------------------------------------------------
# YAML 加载（mtime 缓存，照 goals/site_catalog.py::load_catalog 的模式）
# ---------------------------------------------------------------------------
_CACHE_LOCK = threading.Lock()
# ``gen`` 每次真正解析 YAML 时自增：展开索引把它并进缓存键，热改日历自动失效旧索引。
_CACHE: Dict[str, Any] = {
    "path": "", "mtime": -1.0, "data": None, "checked": 0.0, "gen": 0,
}
_CHECK_INTERVAL = 5.0  # 秒；mtime stat 节流


def load_calendar(path: str = "") -> Dict[str, Any]:
    """mtime 缓存读节日日历 YAML。缺文件/坏 YAML → ``{}``。

    ``path`` 为空时用仓库内置 ``config/locale_holidays.yaml``（相对本文件定位）。
    坏态也进缓存：YAML 写坏了不该让每个会话每次 tick 都去重读一遍坏文件。
    """
    p = str(path or "").strip() or DEFAULT_CALENDAR_PATH
    now = time.time()
    with _CACHE_LOCK:
        if (_CACHE["path"] == p and _CACHE["data"] is not None
                and now - _CACHE["checked"] < _CHECK_INTERVAL):
            return _CACHE["data"]
        try:
            mt = Path(p).stat().st_mtime
        except OSError:
            if _CACHE["path"] != p or _CACHE["data"] is not None:
                _CACHE["gen"] = int(_CACHE["gen"]) + 1
            _CACHE.update(path=p, mtime=-1.0, data={}, checked=now)
            return _CACHE["data"]
        if _CACHE["path"] == p and _CACHE["mtime"] == mt and _CACHE["data"] is not None:
            _CACHE["checked"] = now
            return _CACHE["data"]
        try:
            import yaml

            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            logger.debug("load_calendar failed: %s", p, exc_info=True)
            data = {}
        _CACHE.update(path=p, mtime=mt, data=data, checked=now,
                      gen=int(_CACHE["gen"]) + 1)
        return _CACHE["data"]


def clear_caches_for_tests() -> None:
    """清空日历缓存与展开索引（测试用；生产靠 mtime/gen 自然失效）。"""
    with _CACHE_LOCK:
        _CACHE.update(path="", mtime=-1.0, data=None, checked=0.0,
                      gen=int(_CACHE["gen"]) + 1)
    with _INDEX_LOCK:
        _INDEX.clear()


# ---------------------------------------------------------------------------
# 语种 → 默认国家
# ---------------------------------------------------------------------------

def country_for_language(lang: Any) -> str:
    """语种 → 默认国家 ISO-3166 alpha-2；无法判定 → ``""``。

    ``en``/``es``/``pt``/``fr`` 在数据里**刻意留空**：这四种是跨洲通用语，
    英语可能是美/英/加/澳/菲/印，西语可能是西班牙或整个拉美——猜错的代价
    （给美国人发西班牙国庆祝福）远大于猜对的收益，宁可不给国家、退回通用节日路径。

    但**显式区域子标签是数据而非猜测**：``th-TH`` / ``zh-HK`` / ``en-US`` 里的区域
    是调用方明确给出的，故优先采用（须是日历里已策展的国家，防 ``en-XX`` 出垃圾码）。
    """
    try:
        s = str(lang or "").strip().replace("_", "-")
        if not s:
            return ""
        cal = load_calendar()
        parts = [p for p in s.split("-") if p]
        if len(parts) >= 2:
            region = parts[-1].upper()
            if len(region) == 2 and region.isalpha() and region in _known_countries(cal):
                return region
        defaults = cal.get("lang_defaults") if isinstance(cal, dict) else None
        if not isinstance(defaults, dict):
            return ""
        return str(defaults.get(parts[0].lower()) or "").strip().upper()
    except Exception:
        logger.debug("country_for_language failed: %r", lang, exc_info=True)
        return ""


def _known_countries(calendar: Any) -> frozenset:
    countries = (calendar or {}).get("countries") if isinstance(calendar, dict) else None
    if not isinstance(countries, dict):
        return frozenset()
    return frozenset(str(k).strip().upper() for k in countries)


# ---------------------------------------------------------------------------
# 规则解析（6 种规则类型）
# ---------------------------------------------------------------------------

def _gregorian_easter(year: int) -> date:
    """西方（格里高利）复活节——Anonymous Gregorian / Meeus-Jones-Butcher 算法。

    自己实现而非引 ``dateutil.easter``：整算法 12 行，不值得为它增加一个依赖。
    """
    a = year % 19
    b, c = year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _julian_easter(year: int) -> date:
    """东正教（儒略历）复活节，已换算为公历日期。

    俄罗斯的复活节/谢肉节走这一支：格里高利算法给的是**西方**复活节，两者常差
    一到五周（2026：西方 04-05、东正教 04-12），拿西历的日子发东正教复活节问候是错的。
    儒略→公历在 1900-2099 固定 +13 天。
    """
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    return date(year, month, day) + timedelta(days=13)


def _rule_fixed(rule: Dict[str, Any], year: int) -> List[date]:
    return [date(year, int(rule["month"]), int(rule["day"]))]


def _lunar_month_last_day(lunar_cls: Any, lyear: int, lmonth: int) -> int:
    """农历某月的最后一天（29 或 30，逐年不定）；探不到 → 0。"""
    for cand in (30, 29):
        try:
            lunar_cls.fromYmd(int(lyear), int(lmonth), cand)
            return cand
        except Exception:
            continue
    return 0


def _rule_lunar(rule: Dict[str, Any], year: int) -> List[date]:
    """农历月日 → 公历。缺 ``lunar_python`` → ``[]``（优雅降级）。

    农历年与公历年**不同步**：正月初一落在公历 1-2 月，腊月则落进下一个公历年
    （农历 2026 腊月初八 = 公历 2027-01-15）。故对给定**公历年**要同时试
    ``year-1 / year / year+1`` 三个农历年，只保留落在该公历年内的日期。

    ``day`` 支持负数＝该农历月**倒数第 N 天**：``-1`` 即除夕（腊月最后一天可能是
    廿九也可能是三十，写死 30 会在小月直接抛错——这是 Tết/春节前夜必须处理的现实）。
    """
    lunar_cls, _ = _lunar_api()
    if lunar_cls is None:
        return []
    lmonth, lday = int(rule["month"]), int(rule["day"])
    out: List[date] = []
    for lyear in (year - 1, year, year + 1):
        dd = lday
        if lday < 0:
            last = _lunar_month_last_day(lunar_cls, lyear, lmonth)
            if last <= 0:
                continue
            dd = last + 1 + lday
            if dd < 1:
                continue
        try:
            solar = lunar_cls.fromYmd(lyear, lmonth, dd).getSolar()
            got = date(solar.getYear(), solar.getMonth(), solar.getDay())
        except Exception:
            continue
        if got.year == year:
            out.append(got)
    return sorted(set(out))


def _rule_nth_weekday(rule: Dict[str, Any], year: int) -> List[date]:
    """某月第 N 个星期几。``weekday`` 用 Python ``date.weekday()`` 口径（0=周一）；
    ``nth`` 为负＝倒数第 N 个（``-1``＝最后一个，如美国阵亡将士纪念日＝5 月最后一个周一）。"""
    month, weekday, nth = int(rule["month"]), int(rule["weekday"]), int(rule["nth"])
    if not (1 <= month <= 12) or not (0 <= weekday <= 6) or nth == 0:
        return []
    dim = _calendar.monthrange(year, month)[1]
    if nth > 0:
        first_wd = date(year, month, 1).weekday()
        day = 1 + ((weekday - first_wd) % 7) + (nth - 1) * 7
    else:
        last_wd = date(year, month, dim).weekday()
        day = dim - ((last_wd - weekday) % 7) + (nth + 1) * 7
    if not (1 <= day <= dim):
        return []
    return [date(year, month, day)]


def _rule_easter_offset(rule: Dict[str, Any], year: int) -> List[date]:
    """复活节 ± N 天。``calendar: julian``（或 ``orthodox``）走东正教复活节。"""
    which = str(rule.get("calendar") or "gregorian").strip().lower()
    base = (_julian_easter(year) if which.startswith(("jul", "orth"))
            else _gregorian_easter(year))
    return [base + timedelta(days=int(rule.get("offset") or 0))]


def _rule_table(rule: Dict[str, Any], year: int) -> List[date]:
    """逐年查表。**表里没有该年 → ``[]``，绝不外推猜日子。**

    伊斯兰历依赖新月观测、希伯来历需另一套历法实现，两者都算不准/算不了，只能查表。
    表一到尽头节日就自动消失——「今天没节日」是无害降级，「把开斋节发错三天」是事故。
    值支持单个 ``"MM-DD"``（或 ``"YYYY-MM-DD"``）与列表（同一公历年可能撞上两次
    伊斯兰节：2033 年就有两个开斋节）。
    """
    dates = rule.get("dates")
    if not isinstance(dates, dict):
        return []
    raw = dates.get(year, dates.get(str(year)))
    if raw is None:
        return []
    items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    out: List[date] = []
    for item in items:
        try:
            parts = [int(x) for x in str(item).strip().split("-")]
            if len(parts) == 2:
                out.append(date(year, parts[0], parts[1]))
            elif len(parts) == 3:
                out.append(date(parts[0], parts[1], parts[2]))
        except Exception:
            continue  # 单条写坏只丢这一条，不连坐整个规则
    return sorted(set(out))


# lunar_python 的节气表对**重复出现**的节气用拼音键区分（同一张表跨两个冬至）。
_JIEQI_PINYIN: Dict[str, str] = {
    "DA_XUE": "大雪", "DONG_ZHI": "冬至", "XIAO_HAN": "小寒", "DA_HAN": "大寒",
    "LI_CHUN": "立春", "YU_SHUI": "雨水", "JING_ZHE": "惊蛰",
}
# 输入别名（简繁/异体）→ lunar_python 的用字。
_JIEQI_ALIAS: Dict[str, str] = {
    "驚蟄": "惊蛰", "惊蜇": "惊蛰", "驚蜇": "惊蛰",
    "清明節": "清明", "冬至節": "冬至",
}


def _rule_jieqi(rule: Dict[str, Any], year: int) -> List[date]:
    """二十四节气（清明/冬至/春分/秋分…）。缺 ``lunar_python`` → ``[]``。

    节气是**天文事件**，公历日期逐年在 ±1 天内漂（清明 4/4 或 4/5，日本春分日同理），
    写死公历日会年年错一次——正是规则引擎存在的理由。
    """
    _, solar_cls = _lunar_api()
    if solar_cls is None:
        return []
    name = str(rule.get("name") or "").strip()
    name = _JIEQI_ALIAS.get(name, name)
    if not name:
        return []
    out: set = set()
    # 表按**公历年**开窗（前一年大雪 → 次年惊蛰），探 year 与 year+1 两窗可完整覆盖
    # 目标年 1-12 月（年末的冬至在 year 窗里以拼音键出现，在 year+1 窗里以中文键出现）。
    for probe_year in (year, year + 1):
        try:
            table = solar_cls.fromYmdHms(probe_year, 7, 1, 12, 0, 0).getLunar().getJieQiTable()
        except Exception:
            continue
        if not isinstance(table, dict):
            continue
        for raw_key, solar in table.items():
            if _JIEQI_PINYIN.get(str(raw_key), str(raw_key)) != name:
                continue
            try:
                got = date(solar.getYear(), solar.getMonth(), solar.getDay())
            except Exception:
                continue
            if got.year == year:
                out.add(got)
    return sorted(out)


_RULE_DISPATCH = {
    "fixed": _rule_fixed,
    "lunar": _rule_lunar,
    "nth_weekday": _rule_nth_weekday,
    "easter_offset": _rule_easter_offset,
    "table": _rule_table,
    "jieqi": _rule_jieqi,
}


def resolve_rule(rule: Dict[str, Any], year: int) -> List[date]:
    """把一条规则解析成某年的公历日期列表（可多天，如宋干节 3 天）。无法解析 → ``[]``。

    ``days``（可选，默认 1）＝从基准日起的连续天数，对所有规则类型统一生效
    （宋干节 3 天、俄罗斯新年假 5 天、光明节 8 天、谢肉节一周）。

    年份语义：``lunar``/``jieqi``/``table`` 只返回**落在 ``year`` 内**的日期；
    ``fixed``/``nth_weekday``/``easter_offset`` 以 ``year`` 内的基准日起算，``days``
    展开可能跨到次年 1 月（如 12-31 起 3 天）——跨年溢出由查询层的三年扫描兜住。
    """
    try:
        if not isinstance(rule, dict):
            return []
        y = int(year)
        handler = _RULE_DISPATCH.get(str(rule.get("type") or "").strip().lower())
        if handler is None:
            return []
        base = handler(rule, y)
        if not base:
            return []
        span = 1
        if rule.get("days") is not None:
            span = max(1, min(int(rule["days"]), _MAX_RULE_DAYS))
        if span == 1:
            return sorted(set(base))
        out: set = set()
        for anchor in base:
            for i in range(span):
                out.add(anchor + timedelta(days=i))
        return sorted(out)
    except Exception:
        logger.debug("resolve_rule failed: rule=%r year=%r", rule, year, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# (country, year) → 展开索引（进程级缓存，线程安全）
# ---------------------------------------------------------------------------
_INDEX_LOCK = threading.Lock()
_INDEX: Dict[Tuple[str, int, int], Dict[date, Tuple[Holiday, ...]]] = {}


def _norm_country(country: Any) -> str:
    try:
        c = str(country or "").strip().upper()
        return c if len(c) == 2 and c.isalpha() else ""
    except Exception:
        return ""


def _as_date(day: Any) -> Optional[date]:
    """``date``/``datetime`` → ``date``；其他 → None（datetime 是 date 的子类，先判它）。"""
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    return None


def _holiday_from_spec(spec: Dict[str, Any], country: str) -> Optional[Holiday]:
    """一条 YAML 条目 → Holiday；缺 key/名称或 scope 非法 → None（坏数据静默跳过）。"""
    try:
        key = str(spec.get("key") or "").strip()
        name_zh = str(spec.get("name_zh") or "").strip()
        name_en = str(spec.get("name_en") or "").strip()
        if not key or not name_zh or not name_en:
            return None
        scope = str(spec.get("scope") or "").strip().lower()
        if scope not in _SCOPE_ORDER:
            return None
        return Holiday(
            key=key, name_zh=name_zh, name_en=name_en,
            country=str(spec.get("country") or country).strip().upper(),
            scope=scope, greet=bool(spec.get("greet")),
        )
    except Exception:
        return None


def _country_specs(calendar: Any, country: str) -> List[Dict[str, Any]]:
    countries = (calendar or {}).get("countries") if isinstance(calendar, dict) else None
    if not isinstance(countries, dict):
        return []
    rows = countries.get(country)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _build_index(
    calendar: Any, country: str, year: int,
) -> Dict[date, Tuple[Holiday, ...]]:
    """展开某国某年的全部规则为 ``{date: (Holiday, ...)}``（按 scope→key 稳定排序）。

    对每条规则扫 ``year-1 / year / year+1`` 三年再筛回 ``year``：农历腊月节日、
    ``days`` 跨年溢出、伊斯兰历同年两次，全靠这一手统一收口。
    """
    buckets: Dict[date, List[Holiday]] = {}
    for spec in _country_specs(calendar, country):
        holiday = _holiday_from_spec(spec, country)
        if holiday is None:
            continue
        rule = spec.get("rule")
        seen: set = set()
        for probe in (year - 1, year, year + 1):
            for got in resolve_rule(rule, probe):
                if got.year == year and got not in seen:
                    seen.add(got)
                    buckets.setdefault(got, []).append(holiday)
    return {
        got: tuple(sorted(items, key=lambda h: (_SCOPE_ORDER.get(h.scope, 99), h.key)))
        for got, items in buckets.items()
    }


def _index_for(
    country: str, year: int, calendar: Any = None,
) -> Dict[date, Tuple[Holiday, ...]]:
    """展开索引（内置日历走缓存；显式注入的 calendar 不缓存——身份无法安全做键）。"""
    if calendar is not None:
        return _build_index(calendar, country, year)
    cal = load_calendar()
    with _INDEX_LOCK:
        gen = int(_CACHE["gen"])
        hit = _INDEX.get((country, year, gen))
        if hit is not None:
            return hit
    # 锁外展开：一国一年约几十条规则，不值得让并发查询全排在锁上。重复计算无害
    # （确定性纯函数），最坏是两个线程各算一次同一份结果。
    built = _build_index(cal, country, year)
    with _INDEX_LOCK:
        if len(_INDEX) >= _INDEX_MAX_ENTRIES:
            _INDEX.clear()
        _INDEX[(country, year, gen)] = built
    return built


# ---------------------------------------------------------------------------
# 查询 API
# ---------------------------------------------------------------------------

def holidays_on(
    day: date, country: Any, *, calendar: Any = None,
) -> List[Holiday]:
    """某国某天的节日列表（``public`` → ``religious`` → ``cultural``，同档按 ``key``）。

    国家空/未知 → ``[]``；任何异常 → ``[]``（主动触达 tick 的热路径，绝不抛）。
    """
    try:
        code = _norm_country(country)
        target = _as_date(day)
        if not code or target is None:
            return []
        return list(_index_for(code, target.year, calendar).get(target, ()))
    except Exception:
        logger.debug("holidays_on failed: day=%r country=%r", day, country, exc_info=True)
        return []


def upcoming_holidays(
    day: date, country: Any, *, within_days: int = 2, calendar: Any = None,
) -> List[Tuple[Holiday, int]]:
    """未来 ``within_days`` 天内的节日 + 距今天数（``0``＝今天），用于「快到了」预热。

    按 (天数, scope, key) 升序。跨月/跨年自然成立——逐日查询各自解析所属年份的索引。
    """
    try:
        target = _as_date(day)
        code = _norm_country(country)
        if target is None or not code:
            return []
        span = int(within_days)
        if span < 0:
            return []
        out: List[Tuple[Holiday, int]] = []
        for delta in range(0, min(span, 366) + 1):
            for holiday in holidays_on(target + timedelta(days=delta), code,
                                       calendar=calendar):
                out.append((holiday, delta))
        return out
    except Exception:
        logger.debug("upcoming_holidays failed: day=%r country=%r",
                     day, country, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# prompt 事实块
# ---------------------------------------------------------------------------
# 两侧文案刻意分开：用户侧要「对方在过节」+ 别百科播报别编习俗（LLM 一被点到异国节日
# 就爱背维基条目，反而露馅）；人设侧要「这是我自己的日常背景」，不能变成对客户播报。
_FACT_HEAD = {
    ("user", "zh"): "【对方那边的节日（内部事实）】",
    ("user", "en"): "[Holiday where they are - internal fact] ",
    ("persona", "zh"): "【你所在地的节日（内部事实）】",
    ("persona", "en"): "[Holiday where you live - internal fact] ",
}
_FACT_TAIL = {
    ("user", "zh"): "只在自然相关时提起、按对方文化说人话，不要像百科播报，也不要编造习俗细节。",
    ("user", "en"): ("Bring it up only when it fits naturally and speak like a human in a way "
                     "that suits their culture; don't sound like an encyclopedia and don't "
                     "invent customs."),
    ("persona", "zh"): "当作你自己的生活背景，可自然提到街上的气氛。",
    ("persona", "en"): ("Treat it as your own everyday backdrop; you can naturally mention the "
                        "mood on the streets."),
}


def _normalize_items(items: Any) -> List[Tuple[Holiday, int]]:
    """把 ``Holiday`` / ``(Holiday, 天数)`` / 两者的可迭代对象统一成 ``[(Holiday, 天数)]``。

    这样 ``holidays_on`` 与 ``upcoming_holidays`` 的产物都能直接喂进来，调用方不必转换。
    """
    if isinstance(items, Holiday):
        return [(items, 0)]
    if isinstance(items, tuple) and len(items) == 2 and isinstance(items[0], Holiday):
        try:
            return [(items[0], max(0, int(items[1])))]
        except Exception:
            return [(items[0], 0)]
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes, dict)):
        return []
    out: List[Tuple[Holiday, int]] = []
    for item in items:
        out.extend(_normalize_items(item))
    return out


def _when_phrase(delta: int, en: bool) -> str:
    if delta <= 0:
        return "Today is " if en else "今天是"
    if delta == 1:
        return "Tomorrow is " if en else "明天是"
    return f"In {delta} days it's " if en else f"再过{delta}天是"


def holiday_fact_line(items: Any, lang: str = "zh", *, side: str = "user") -> str:
    """节日事实块（prompt 内部注入用）。空输入/异常 → ``""``。

    ``side="user"``：对方那边的节日，句里点名国家（「今天是泰国的宋干节（泼水节）。」）。
    ``side="persona"``：人设自己所在地的节日，不点国家（人就住那儿，节日名自带地名）。
    ``lang`` 按仓库惯例二元判定（``en*`` → 英文，其余 → 中文）——这是给 LLM 的**内部**
    事实块，跟随系统/坐席语言即可，不需要按 18 语种各写一份。
    """
    try:
        pairs = _normalize_items(items)[:_MAX_FACT_ITEMS]
        if not pairs:
            return ""
        en = str(lang or "").strip().lower().startswith("en")
        which = "persona" if str(side or "user").strip().lower() == "persona" else "user"
        sep = ", " if en else "、"

        # 按 (天数, 国家) 归组：同一天同一国的多个节日合并成一句
        groups: Dict[Tuple[int, str], List[Holiday]] = {}
        for holiday, delta in pairs:
            groups.setdefault((delta, holiday.country), []).append(holiday)

        clauses: List[str] = []
        for (delta, code), holidays in sorted(groups.items()):
            names = sep.join(h.name_en if en else h.name_zh for h in holidays)
            if not names:
                continue
            when = _when_phrase(delta, en)
            if which == "persona":
                clauses.append(f"{when}{names}." if en else f"{when}{names}。")
                continue
            label = COUNTRY_LABELS.get(code, (code, code))[1 if en else 0]
            if en:
                clauses.append(f"{when}{names} in {label}." if label else f"{when}{names}.")
            else:
                clauses.append(f"{when}{label}的{names}。" if label else f"{when}{names}。")
        if not clauses:
            return ""
        body = (" " if en else "").join(clauses)
        return f"{_FACT_HEAD[(which, 'en' if en else 'zh')]}{body}" \
               f"{' ' if en else ''}{_FACT_TAIL[(which, 'en' if en else 'zh')]}"
    except Exception:
        logger.debug("holiday_fact_line failed: items=%r", items, exc_info=True)
        return ""
