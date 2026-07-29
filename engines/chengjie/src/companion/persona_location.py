"""人设地理档案 + 本地时钟单一事实源：人设「住在哪、当地此刻几点」的唯一权威出口。

背景：人设声称长居海外城市（温哥华/加州/马尼拉等），而系统时间状态机全部锚定服务器
本地时区（UTC+8），导致「凌晨发下午茶照」类作息穿帮。设计要点：
- CITY_PRESETS 预置 35 城（slug -> city_zh/city_en/country/tz/lat/lon，tz 全为合法 IANA 名）；
- resolve_persona_place 解析 persona["location"]：字符串（"none"/"off" 显式禁用；slug ->
  中英文精确名 -> 包含匹配）或 dict 自定义（timezone 必填且必须可构造 ZoneInfo，city 命中
  预置则以预置补全缺省字段，否则 slug="custom"）；
- resolve_place_with_fallback：有 location 键完全按显式结果（含显式禁用），无键时可从
  role/background 文本保守推断（infer_place_from_text：城市/区域词优先于国家词，
  同级取文本中最先出现；英文名词边界匹配，中文直接子串）；
- persona_now 一律返回 **naive** 本地时间（下游 strftime/crc/比较全按 naive 消费，绝不
  返回 aware）；place 缺失或时区无效逐位回落 datetime.now() 旧行为；
- 热路径绝不 raise（任何异常吞掉，回落 None / datetime.now() / 空串）；零新依赖
  （stdlib zoneinfo，tzdata 已在 requirements）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

__all__ = [
    "PersonaPlace",
    "CITY_PRESETS",
    "resolve_persona_place",
    "infer_place_from_text",
    "resolve_place_with_fallback",
    "persona_now",
    "resolve_persona_now",
    "persona_local_hour",
    "daypart_label",
    "local_time_line",
    "tz_offset_hours",
    "time_gap_line",
]

# 国家码（ISO-3166 alpha-2）-> (中文名, 英文名)；display 消费，未知码回落显示原码。
_COUNTRY_NAMES: Dict[str, Tuple[str, str]] = {
    "CA": ("加拿大", "Canada"),
    "US": ("美国", "United States"),
    "GB": ("英国", "United Kingdom"),
    "FR": ("法国", "France"),
    "AE": ("阿联酋", "United Arab Emirates"),
    "JP": ("日本", "Japan"),
    "KR": ("韩国", "South Korea"),
    "TH": ("泰国", "Thailand"),
    "VN": ("越南", "Vietnam"),
    "KH": ("柬埔寨", "Cambodia"),
    "SG": ("新加坡", "Singapore"),
    "MY": ("马来西亚", "Malaysia"),
    "ID": ("印尼", "Indonesia"),
    "PH": ("菲律宾", "Philippines"),
    "TW": ("台湾", "Taiwan"),
    "HK": ("香港", "Hong Kong"),
    "MO": ("澳门", "Macau"),
    "CN": ("中国", "China"),
    "AU": ("澳大利亚", "Australia"),
    "NZ": ("新西兰", "New Zealand"),
}

# 预置城市档案：slug -> {city_zh, city_en, country, tz, lat, lon}。
# tz 必须是合法 IANA 名（含 Link，如 Asia/Phnom_Penh）；经纬度取小数点后 2 位。
CITY_PRESETS: Dict[str, dict] = {
    "vancouver": {"city_zh": "温哥华", "city_en": "Vancouver", "country": "CA", "tz": "America/Vancouver", "lat": 49.28, "lon": -123.12},
    "toronto": {"city_zh": "多伦多", "city_en": "Toronto", "country": "CA", "tz": "America/Toronto", "lat": 43.65, "lon": -79.38},
    "los_angeles": {"city_zh": "洛杉矶", "city_en": "Los Angeles", "country": "US", "tz": "America/Los_Angeles", "lat": 34.05, "lon": -118.24},
    "san_francisco": {"city_zh": "旧金山", "city_en": "San Francisco", "country": "US", "tz": "America/Los_Angeles", "lat": 37.77, "lon": -122.42},
    "seattle": {"city_zh": "西雅图", "city_en": "Seattle", "country": "US", "tz": "America/Los_Angeles", "lat": 47.61, "lon": -122.33},
    "new_york": {"city_zh": "纽约", "city_en": "New York", "country": "US", "tz": "America/New_York", "lat": 40.71, "lon": -74.01},
    "honolulu": {"city_zh": "檀香山", "city_en": "Honolulu", "country": "US", "tz": "Pacific/Honolulu", "lat": 21.31, "lon": -157.86},
    "london": {"city_zh": "伦敦", "city_en": "London", "country": "GB", "tz": "Europe/London", "lat": 51.51, "lon": -0.13},
    "paris": {"city_zh": "巴黎", "city_en": "Paris", "country": "FR", "tz": "Europe/Paris", "lat": 48.86, "lon": 2.35},
    "dubai": {"city_zh": "迪拜", "city_en": "Dubai", "country": "AE", "tz": "Asia/Dubai", "lat": 25.20, "lon": 55.27},
    "tokyo": {"city_zh": "东京", "city_en": "Tokyo", "country": "JP", "tz": "Asia/Tokyo", "lat": 35.68, "lon": 139.69},
    "osaka": {"city_zh": "大阪", "city_en": "Osaka", "country": "JP", "tz": "Asia/Tokyo", "lat": 34.69, "lon": 135.50},
    "seoul": {"city_zh": "首尔", "city_en": "Seoul", "country": "KR", "tz": "Asia/Seoul", "lat": 37.57, "lon": 126.98},
    "bangkok": {"city_zh": "曼谷", "city_en": "Bangkok", "country": "TH", "tz": "Asia/Bangkok", "lat": 13.76, "lon": 100.50},
    "chiang_mai": {"city_zh": "清迈", "city_en": "Chiang Mai", "country": "TH", "tz": "Asia/Bangkok", "lat": 18.79, "lon": 98.98},
    "phuket": {"city_zh": "普吉", "city_en": "Phuket", "country": "TH", "tz": "Asia/Bangkok", "lat": 7.88, "lon": 98.39},
    "hanoi": {"city_zh": "河内", "city_en": "Hanoi", "country": "VN", "tz": "Asia/Ho_Chi_Minh", "lat": 21.03, "lon": 105.85},
    "ho_chi_minh": {"city_zh": "胡志明市", "city_en": "Ho Chi Minh City", "country": "VN", "tz": "Asia/Ho_Chi_Minh", "lat": 10.82, "lon": 106.63},
    "phnom_penh": {"city_zh": "金边", "city_en": "Phnom Penh", "country": "KH", "tz": "Asia/Phnom_Penh", "lat": 11.56, "lon": 104.92},
    "singapore": {"city_zh": "新加坡", "city_en": "Singapore", "country": "SG", "tz": "Asia/Singapore", "lat": 1.35, "lon": 103.82},
    "kuala_lumpur": {"city_zh": "吉隆坡", "city_en": "Kuala Lumpur", "country": "MY", "tz": "Asia/Kuala_Lumpur", "lat": 3.14, "lon": 101.69},
    "jakarta": {"city_zh": "雅加达", "city_en": "Jakarta", "country": "ID", "tz": "Asia/Jakarta", "lat": -6.21, "lon": 106.85},
    "bali": {"city_zh": "巴厘岛", "city_en": "Bali", "country": "ID", "tz": "Asia/Makassar", "lat": -8.65, "lon": 115.22},
    "manila": {"city_zh": "马尼拉", "city_en": "Manila", "country": "PH", "tz": "Asia/Manila", "lat": 14.60, "lon": 120.98},
    "cebu": {"city_zh": "宿务", "city_en": "Cebu", "country": "PH", "tz": "Asia/Manila", "lat": 10.32, "lon": 123.90},
    "taipei": {"city_zh": "台北", "city_en": "Taipei", "country": "TW", "tz": "Asia/Taipei", "lat": 25.03, "lon": 121.57},
    "hong_kong": {"city_zh": "香港", "city_en": "Hong Kong", "country": "HK", "tz": "Asia/Hong_Kong", "lat": 22.32, "lon": 114.17},
    "macau": {"city_zh": "澳门", "city_en": "Macau", "country": "MO", "tz": "Asia/Macau", "lat": 22.20, "lon": 113.55},
    "shanghai": {"city_zh": "上海", "city_en": "Shanghai", "country": "CN", "tz": "Asia/Shanghai", "lat": 31.23, "lon": 121.47},
    "beijing": {"city_zh": "北京", "city_en": "Beijing", "country": "CN", "tz": "Asia/Shanghai", "lat": 39.90, "lon": 116.40},
    "shenzhen": {"city_zh": "深圳", "city_en": "Shenzhen", "country": "CN", "tz": "Asia/Shanghai", "lat": 22.54, "lon": 114.06},
    "chengdu": {"city_zh": "成都", "city_en": "Chengdu", "country": "CN", "tz": "Asia/Shanghai", "lat": 30.57, "lon": 104.07},
    "sydney": {"city_zh": "悉尼", "city_en": "Sydney", "country": "AU", "tz": "Australia/Sydney", "lat": -33.87, "lon": 151.21},
    "melbourne": {"city_zh": "墨尔本", "city_en": "Melbourne", "country": "AU", "tz": "Australia/Melbourne", "lat": -37.81, "lon": 144.96},
    "auckland": {"city_zh": "奥克兰", "city_en": "Auckland", "country": "NZ", "tz": "Pacific/Auckland", "lat": -36.85, "lon": 174.76},
}


@dataclass(frozen=True)
class PersonaPlace:
    """人设居住地档案（不可变）：预置城市或自定义纯时区场景（lat/lon 可为 None）。"""

    slug: str
    city_zh: str
    city_en: str
    country: str
    tz_name: str
    lat: Optional[float]
    lon: Optional[float]

    @property
    def hemisphere(self) -> str:
        """南北半球（季节反转用）：lat 为 None 或 >=0 按北半球。"""
        try:
            if self.lat is not None and float(self.lat) < 0:
                return "south"
        except Exception:
            pass
        return "north"

    def display(self, lang: str = "zh") -> str:
        """双语展示名：zh -> "加拿大·温哥华"，en -> "Vancouver, Canada"；城邦同名去重（新加坡/香港）。"""
        try:
            zh_mode = str(lang or "zh").lower().startswith("zh")
            country_zh, country_en = _COUNTRY_NAMES.get(self.country, (self.country, self.country))
            if zh_mode:
                city = self.city_zh or self.city_en
                if country_zh and city and country_zh != city:
                    return f"{country_zh}·{city}"
                return city or country_zh
            city = self.city_en or self.city_zh
            if country_en and city and country_en != city:
                return f"{city}, {country_en}"
            return city or country_en
        except Exception:
            return self.city_zh or self.city_en or ""


# ---------------------------------------------------------------------------
# 关键词表（infer 用）：城市/区域词一档，国家词兜底一档；同档取最先出现、同位取更长词。
# ---------------------------------------------------------------------------

_CITY_KEYWORDS_ZH: Dict[str, str] = {
    "温哥华": "vancouver", "多伦多": "toronto", "洛杉矶": "los_angeles",
    "旧金山": "san_francisco", "西雅图": "seattle", "纽约": "new_york",
    "檀香山": "honolulu", "夏威夷": "honolulu",
    "伦敦": "london", "巴黎": "paris", "迪拜": "dubai",
    "东京": "tokyo", "大阪": "osaka", "首尔": "seoul",
    "曼谷": "bangkok", "清迈": "chiang_mai", "普吉": "phuket",
    "河内": "hanoi", "胡志明": "ho_chi_minh", "西贡": "ho_chi_minh",
    "金边": "phnom_penh", "新加坡": "singapore", "吉隆坡": "kuala_lumpur",
    "雅加达": "jakarta", "巴厘岛": "bali", "巴厘": "bali",
    "马尼拉": "manila", "帕赛": "manila", "宿务": "cebu",
    "台北": "taipei", "香港": "hong_kong", "港深": "hong_kong",
    "澳门": "macau", "上海": "shanghai", "北京": "beijing",
    "深圳": "shenzhen", "成都": "chengdu",
    "悉尼": "sydney", "墨尔本": "melbourne", "奥克兰": "auckland",
    # 区域词（与城市词同档，优先于国家词）
    "加州": "los_angeles", "湾区": "san_francisco",
}

_COUNTRY_KEYWORDS_ZH: Dict[str, str] = {
    "泰国": "bangkok", "越南": "ho_chi_minh", "菲律宾": "manila",
    "印度尼西亚": "jakarta", "印尼": "jakarta", "马来西亚": "kuala_lumpur",
    "柬埔寨": "phnom_penh", "日本": "tokyo", "韩国": "seoul",
    "新西兰": "auckland", "澳大利亚": "sydney", "澳洲": "sydney",
    "英国": "london", "阿联酋": "dubai",
}


def _build_en_patterns() -> List[Tuple["re.Pattern[str]", str]]:
    """预编译英文城市名词边界正则（预置 city_en + 常见别名/区域词）。"""
    pairs: List[Tuple[str, str]] = [
        (str(meta.get("city_en", "")).lower(), slug) for slug, meta in CITY_PRESETS.items()
    ]
    pairs += [
        ("ho chi minh", "ho_chi_minh"),
        ("saigon", "ho_chi_minh"),
        ("hongkong", "hong_kong"),
        ("california", "los_angeles"),
        ("bay area", "san_francisco"),
        ("hawaii", "honolulu"),
    ]
    out: List[Tuple["re.Pattern[str]", str]] = []
    for name, slug in pairs:
        if not name:
            continue
        try:
            out.append((re.compile(r"\b" + re.escape(name) + r"\b"), slug))
        except Exception:
            continue
    return out


_EN_CITY_PATTERNS: List[Tuple["re.Pattern[str]", str]] = _build_en_patterns()


# ---------------------------------------------------------------------------
# 解析链
# ---------------------------------------------------------------------------

def _place_from_slug(slug: str) -> Optional[PersonaPlace]:
    """slug -> PersonaPlace；未知 slug 返回 None。"""
    meta = CITY_PRESETS.get(slug)
    if not meta:
        return None
    try:
        return PersonaPlace(
            slug=slug,
            city_zh=str(meta["city_zh"]),
            city_en=str(meta["city_en"]),
            country=str(meta["country"]),
            tz_name=str(meta["tz"]),
            lat=meta.get("lat"),
            lon=meta.get("lon"),
        )
    except Exception:
        return None


def _match_exact(name: str) -> Optional[str]:
    """字符串精确匹配预置：先 slug（空白/连字符归一为下划线），再 city_zh/city_en（大小写不敏感）。"""
    try:
        s = str(name).strip().lower()
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


def infer_place_from_text(text: str) -> Optional[str]:
    """从人设自由文本保守推断居住城市 slug。

    城市/区域词优先于国家词；同档多词命中取文本中最先出现（同位取更长词）；
    英文名按词边界匹配，中文直接子串；无法判定或异常 -> None。
    """
    try:
        if not isinstance(text, str) or not text.strip():
            return None
        low = text.lower()
        # (出现位置, -词长, slug)：位置小者优先，同位置长词优先
        best: Optional[Tuple[int, int, str]] = None
        for kw, slug in _CITY_KEYWORDS_ZH.items():
            pos = low.find(kw)
            if pos >= 0:
                cand = (pos, -len(kw), slug)
                if best is None or cand < best:
                    best = cand
        for pattern, slug in _EN_CITY_PATTERNS:
            m = pattern.search(low)
            if m is not None:
                cand = (m.start(), -(m.end() - m.start()), slug)
                if best is None or cand < best:
                    best = cand
        if best is not None:
            return best[2]
        for kw, slug in _COUNTRY_KEYWORDS_ZH.items():
            pos = low.find(kw)
            if pos >= 0:
                cand = (pos, -len(kw), slug)
                if best is None or cand < best:
                    best = cand
        return best[2] if best is not None else None
    except Exception:
        return None


def _coord(value: Any, bound: float) -> Optional[float]:
    """经纬度净化：None/不可转/NaN/越界 -> None。"""
    try:
        if value is None:
            return None
        f = float(value)
        if f != f or abs(f) > bound:
            return None
        return f
    except Exception:
        return None


def _resolve_location_dict(loc: dict) -> Optional[PersonaPlace]:
    """dict 形态 location：timezone/tz 必填且必须可构造 ZoneInfo；city 命中预置则补全缺省字段。"""
    tz_name = ""
    for key in ("timezone", "tz"):
        v = loc.get(key)
        if isinstance(v, str) and v.strip():
            tz_name = v.strip()
            break
    if not tz_name:
        return None
    ZoneInfo(tz_name)  # 非法时区在此抛出，由外层吞掉 -> None
    city_raw = loc.get("city")
    city_txt = str(city_raw).strip() if isinstance(city_raw, str) else ""
    preset_slug = _match_exact(city_txt) if city_txt else None
    base: dict = dict(CITY_PRESETS[preset_slug]) if preset_slug else {}
    default_city_en = str(base.get("city_en") or city_txt or tz_name.rsplit("/", 1)[-1].replace("_", " "))
    city_zh = str(loc.get("city_zh") or base.get("city_zh") or city_txt or default_city_en)
    city_en = str(loc.get("city_en") or default_city_en)
    country = str(loc.get("country") or base.get("country") or "").strip().upper()
    lat_src = loc["lat"] if "lat" in loc else base.get("lat")
    lon_src = loc["lon"] if "lon" in loc else base.get("lon")
    return PersonaPlace(
        slug=preset_slug or "custom",
        city_zh=city_zh,
        city_en=city_en,
        country=country,
        tz_name=tz_name,
        lat=_coord(lat_src, 90.0),
        lon=_coord(lon_src, 180.0),
    )


def _resolve_location_value(loc: Any) -> Optional[PersonaPlace]:
    """location 字段值 -> PersonaPlace；"none"/"off"/解析失败/异常 -> None。"""
    try:
        if isinstance(loc, str):
            s = loc.strip()
            if not s or s.lower() in ("none", "off"):
                return None
            slug = _match_exact(s) or infer_place_from_text(s)
            return _place_from_slug(slug) if slug else None
        if isinstance(loc, dict):
            return _resolve_location_dict(loc)
        return None
    except Exception:
        return None


def resolve_persona_place(persona: Any) -> Optional[PersonaPlace]:
    """从 persona（dict，容忍任意类型）的 location 字段解析居住地；任何异常 -> None。"""
    try:
        loc = persona.get("location")
    except Exception:
        return None
    return _resolve_location_value(loc)


def resolve_place_with_fallback(persona: Any, *, auto_infer: bool = True) -> Optional[PersonaPlace]:
    """解析人设居住地：有 location 键（含 "none"）完全按显式结果；无键且 auto_infer 时从
    role/background 文本推断；均无 -> None。"""
    try:
        if isinstance(persona, dict):
            if "location" in persona:
                return resolve_persona_place(persona)
            if auto_infer:
                text = str(persona.get("role", "") or "") + " " + str(persona.get("background", "") or "")
                slug = infer_place_from_text(text)
                if slug:
                    return _place_from_slug(slug)
            return None
        return resolve_persona_place(persona)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 本地时钟
# ---------------------------------------------------------------------------

def persona_now(place: Optional[PersonaPlace], now: Optional[datetime] = None) -> datetime:
    """人设城市此刻的 **naive** 本地时间（绝不返回 aware）。

    place 为 None 或时区无效 -> datetime.now()（与旧行为逐位一致）；
    now 传入 aware 时按其换算（供测试注入固定时刻）；
    now 传入 **naive** 时视为**已经是目标墙钟**（place 当地或服务器本地），
    **不再二次换算**——``persona_now`` 幂等：``persona_now(p, persona_now(p))``
    保持同一墙钟。生产接线常把当地 naive 再喂给 ``local_time_line``，若按
    「naive=服务器时刻」再转一次，温哥华深夜会被扭成近似中国/菲律宾正午
    （2026-07 实录穿帮根因）。
    """
    try:
        base = now
        # naive = 已是墙钟：有 place 即当地墙钟，无 place 即服务器墙钟；幂等直接回。
        if base is not None and base.tzinfo is None:
            return base.replace(tzinfo=None)
        if place is None:
            if base is None:
                return datetime.now()
            return base.astimezone().replace(tzinfo=None)
        tz = ZoneInfo(place.tz_name)
        if base is None:
            base = datetime.now(timezone.utc)
        return base.astimezone(tz).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def resolve_persona_now(
    persona: Any,
    now: Optional[datetime] = None,
    *,
    auto_infer: bool = True,
) -> datetime:
    """一步拿人设本地 naive 时钟：resolve_place_with_fallback → persona_now。

    热路径便捷入口；place 解析失败则与 datetime.now() 旧行为一致。
    """
    try:
        place = resolve_place_with_fallback(persona, auto_infer=auto_infer)
    except Exception:
        place = None
    return persona_now(place, now)


def persona_local_hour(place: Optional[PersonaPlace], now: Optional[datetime] = None) -> int:
    """人设城市此刻的小时（0-23）；异常回落服务器当前小时。"""
    try:
        return int(persona_now(place, now).hour)
    except Exception:
        return int(datetime.now().hour)


# 时段词表：与 deep_persona.temporal_anchor 阈值对齐（<5 深夜 <8 清晨 <11 上午 <13 中午 <17 下午 <19 傍晚 <23 晚上 else 深夜）。
_DAYPARTS: Tuple[Tuple[int, str, str], ...] = (
    (5, "深夜", "late night"),
    (8, "清晨", "early morning"),
    (11, "上午", "morning"),
    (13, "中午", "noon"),
    (17, "下午", "afternoon"),
    (19, "傍晚", "early evening"),
    (23, "晚上", "evening"),
)

_WEEKDAYS_ZH: Tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_WEEKDAYS_EN: Tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def daypart_label(hour: int, lang: str = "zh") -> str:
    """小时 -> 时段词（zh/en）；异常回落深夜档。"""
    try:
        zh_mode = str(lang or "zh").lower().startswith("zh")
        h = int(hour) % 24
        for bound, zh, en in _DAYPARTS:
            if h < bound:
                return zh if zh_mode else en
        return "深夜" if zh_mode else "late night"
    except Exception:
        return "深夜"


def local_time_line(place: PersonaPlace, lang: str = "zh", now: Optional[datetime] = None) -> str:
    """一行「你人在哪、当地时间几点（时段）」提示，注入 prompt 用；异常回落空串。

    ``now`` 语义与 ``persona_now`` 一致：aware=绝对时刻换算；naive=已是当地墙钟；
    None=取当前 UTC 再换算。
    """
    try:
        dt = persona_now(place, now)
        part = daypart_label(dt.hour, lang)
        if str(lang or "zh").lower().startswith("zh"):
            return (
                f"你人在{place.display('zh')}，当地时间 {dt:%Y-%m-%d} "
                f"{_WEEKDAYS_ZH[dt.weekday()]} {dt:%H:%M}（{part}）。"
                f"问候与作息必须按这个当地时间，禁止按中国/菲律宾（UTC+8）时间说话。"
            )
        return (
            f"You are in {place.display('en')}. Local time: {dt:%Y-%m-%d} "
            f"{_WEEKDAYS_EN[dt.weekday()]} {dt:%H:%M} ({part}). "
            f"Greetings and daily routine must follow this local clock — "
            f"do not speak as if you were on China/Philippines (UTC+8) time."
        )
    except Exception:
        return ""


def tz_offset_hours(place: Optional[PersonaPlace], now: Optional[datetime] = None) -> float:
    """人设城市相对服务器本地时区的时差（小时，城市-服务器）；无效/异常 -> 0.0。

    naive ``now`` 与 ``persona_now`` 同口径：视为**当地墙钟**，挂上 place 时区后再算
    绝对偏移（避免把当地深夜当成服务器时刻）。
    """
    try:
        if place is None:
            return 0.0
        base = now
        if base is None:
            base = datetime.now(timezone.utc)
        elif base.tzinfo is None:
            base = base.replace(tzinfo=ZoneInfo(place.tz_name))
        city_off = base.astimezone(ZoneInfo(place.tz_name)).utcoffset()
        server_off = datetime.now().astimezone().utcoffset()
        if city_off is None or server_off is None:
            return 0.0
        return (city_off - server_off).total_seconds() / 3600.0
    except Exception:
        return 0.0


def time_gap_line(place: Optional[PersonaPlace], lang: str = "zh", now: Optional[datetime] = None) -> Optional[str]:
    """|时差| >= 3 小时才输出一行作息提醒（防「对方白天你也白天」的穿帮）；否则/异常 -> None。"""
    try:
        if place is None:
            return None
        off = tz_offset_hours(place, now)
        if abs(off) < 3.0:
            return None
        num = f"{off:+g}"
        if str(lang or "zh").lower().startswith("zh"):
            return (
                f"注意：你与中国有 {num} 小时时差，"
                "对方白天可能正是你的深夜——表述作息时要自然体现这一点。"
            )
        return (
            f"Note: you have a {num}-hour time difference with China; "
            "the other person's daytime may be your late night — reflect this naturally when talking about your daily routine."
        )
    except Exception:
        return None
