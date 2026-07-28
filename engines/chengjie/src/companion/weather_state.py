"""当地天气事实源：Open-Meteo（免 key）+ TTL 缓存 + 软失败。

设计：
- 只读 PersonaPlace.lat/lon；缺坐标 → None（不猜城市天气）；
- 进程级缓存按 lat/lon 两位小数；新鲜窗 ttl、陈旧可用窗 max_stale；
- 热路径绝不 raise；transport 可注入（测试）；默认 urllib 3s 超时；
- 注入哲学同 scene：事实块，LLM 决定是否提起；缺数据禁编数值。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = [
    "WeatherSnapshot",
    "fetch_weather",
    "weather_chat_note",
    "weather_proactive_hook",
    "scene_conflicts_with_weather",
    "wmo_label",
    "dump_stats",
    "reset_stats_for_tests",
    "clear_cache_for_tests",
]

Transport = Callable[[str], dict]

_CACHE: Dict[Tuple[float, float], Tuple[float, "WeatherSnapshot"]] = {}
_LOCK = threading.Lock()
_STATS: Dict[str, int] = {
    "hits": 0, "stale_hits": 0, "misses": 0, "fetches": 0, "errors": 0,
}

# WMO Weather interpretation codes → (zh, en, bucket)
# bucket ∈ clear|cloudy|fog|drizzle|rain|snow|storm|unknown
_WMO: Dict[int, Tuple[str, str, str]] = {
    0: ("晴朗", "clear", "clear"),
    1: ("大致晴朗", "mainly clear", "clear"),
    2: ("多云", "partly cloudy", "cloudy"),
    3: ("阴天", "overcast", "cloudy"),
    45: ("有雾", "foggy", "fog"),
    48: ("雾凇", "depositing rime fog", "fog"),
    51: ("小毛毛雨", "light drizzle", "drizzle"),
    53: ("毛毛雨", "drizzle", "drizzle"),
    55: ("大毛毛雨", "dense drizzle", "drizzle"),
    61: ("小雨", "light rain", "rain"),
    63: ("中雨", "rain", "rain"),
    65: ("大雨", "heavy rain", "rain"),
    66: ("冻雨", "freezing rain", "rain"),
    67: ("强冻雨", "heavy freezing rain", "rain"),
    71: ("小雪", "light snow", "snow"),
    73: ("中雪", "snow", "snow"),
    75: ("大雪", "heavy snow", "snow"),
    77: ("雪粒", "snow grains", "snow"),
    80: ("阵雨", "rain showers", "rain"),
    81: ("强阵雨", "heavy rain showers", "rain"),
    82: ("暴雨", "violent rain showers", "storm"),
    85: ("阵雪", "snow showers", "snow"),
    86: ("强阵雪", "heavy snow showers", "snow"),
    95: ("雷暴", "thunderstorm", "storm"),
    96: ("雷暴伴冰雹", "thunderstorm with hail", "storm"),
    99: ("强雷暴伴冰雹", "severe thunderstorm with hail", "storm"),
}


@dataclass(frozen=True)
class WeatherSnapshot:
    temp_c: Optional[float]
    weather_code: int
    humidity: Optional[float]
    wind_kmh: Optional[float]
    precip_mm: Optional[float]
    fetched_at: float
    stale: bool
    place_slug: str
    summary_zh: str
    summary_en: str
    bucket: str


def wmo_label(code: int, lang: str = "zh") -> Tuple[str, str]:
    """返回 (展示文案, bucket)。未知码 → 含糊描述 + unknown。"""
    try:
        c = int(code)
    except Exception:
        return ("天气不明", "unknown")
    zh, en, bucket = _WMO.get(c, ("天气一般", "unremarkable weather", "unknown"))
    return (zh if str(lang or "zh").lower().startswith("zh") else en, bucket)


def _cache_key(lat: float, lon: float) -> Tuple[float, float]:
    return (round(float(lat), 2), round(float(lon), 2))


def _default_transport(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "chengjie-weather/1.0"})
    with urllib.request.urlopen(req, timeout=3.0) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _parse_current(data: dict, place_slug: str, fetched_at: float, stale: bool) -> Optional[WeatherSnapshot]:
    try:
        cur = data.get("current") or {}
        code = int(cur.get("weather_code") if cur.get("weather_code") is not None else -1)
        zh, bucket = wmo_label(code, "zh")
        en, _ = wmo_label(code, "en")
        temp = cur.get("temperature_2m")
        hum = cur.get("relative_humidity_2m")
        wind = cur.get("wind_speed_10m")
        precip = cur.get("precipitation")
        return WeatherSnapshot(
            temp_c=float(temp) if temp is not None else None,
            weather_code=code,
            humidity=float(hum) if hum is not None else None,
            wind_kmh=float(wind) if wind is not None else None,
            precip_mm=float(precip) if precip is not None else None,
            fetched_at=float(fetched_at),
            stale=bool(stale),
            place_slug=str(place_slug or ""),
            summary_zh=zh,
            summary_en=en,
            bucket=bucket,
        )
    except Exception:
        return None


def fetch_weather(
    place: Any,
    *,
    ttl_sec: int = 1800,
    max_stale_sec: int = 10800,
    now: Optional[float] = None,
    transport: Optional[Transport] = None,
) -> Optional[WeatherSnapshot]:
    """取人设当地天气；缺坐标/失败 → None；缓存命中可返回 stale=True 快照。"""
    try:
        lat = getattr(place, "lat", None)
        lon = getattr(place, "lon", None)
        slug = str(getattr(place, "slug", "") or "")
        if lat is None or lon is None:
            with _LOCK:
                _STATS["misses"] += 1
            return None
        key = _cache_key(lat, lon)
        ts = float(now if now is not None else time.time())
        with _LOCK:
            hit = _CACHE.get(key)
        if hit is not None:
            fetched_at, snap = hit
            age = ts - float(fetched_at)
            if age <= float(ttl_sec):
                with _LOCK:
                    _STATS["hits"] += 1
                return snap if not snap.stale else WeatherSnapshot(
                    **{**snap.__dict__, "stale": False})
            if age <= float(max_stale_sec):
                with _LOCK:
                    _STATS["stale_hits"] += 1
                return WeatherSnapshot(**{**snap.__dict__, "stale": True})
        # 需要刷新
        params = urllib.parse.urlencode({
            "latitude": f"{float(lat):.4f}",
            "longitude": f"{float(lon):.4f}",
            "current": "temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m,precipitation",
            "timezone": "auto",
        })
        url = "https://api.open-meteo.com/v1/forecast?" + params
        try:
            with _LOCK:
                _STATS["fetches"] += 1
            data = (transport or _default_transport)(url)
            snap = _parse_current(data, slug, ts, stale=False)
            if snap is None:
                raise ValueError("parse_failed")
            with _LOCK:
                _CACHE[key] = (ts, snap)
            return snap
        except Exception:
            with _LOCK:
                _STATS["errors"] += 1
                hit2 = _CACHE.get(key)
            if hit2 is not None:
                fetched_at, old = hit2
                if ts - float(fetched_at) <= float(max_stale_sec):
                    with _LOCK:
                        _STATS["stale_hits"] += 1
                    return WeatherSnapshot(**{**old.__dict__, "stale": True})
            with _LOCK:
                _STATS["misses"] += 1
            return None
    except Exception:
        with _LOCK:
            _STATS["errors"] += 1
        return None


def weather_chat_note(snap: Optional[WeatherSnapshot], lang: str = "zh") -> str:
    """聊天 prompt 内部事实块；空/异常 → ""。"""
    try:
        if snap is None:
            return ""
        zh_mode = str(lang or "zh").lower().startswith("zh")
        label = snap.summary_zh if zh_mode else snap.summary_en
        bits = [label]
        if snap.temp_c is not None:
            bits.append(f"{snap.temp_c:.0f}°C")
        if snap.precip_mm is not None and snap.precip_mm >= 0.5:
            bits.append(
                (f"降水 {snap.precip_mm:.1f}mm") if zh_mode
                else (f"precip {snap.precip_mm:.1f}mm"))
        fact = "，".join(bits) if zh_mode else ", ".join(bits)
        stale = "（数据稍旧，别报精确数字）" if snap.stale and zh_mode else (
            " (slightly stale — avoid exact numbers)" if snap.stale else "")
        if zh_mode:
            return (
                f"【当地天气（内部事实）】{fact}{stale}。"
                "仅当对方问起天气/出门/穿衣，或话题自然相关时口语化顺带提一句；"
                "不要每条汇报，也不要编造未给出的数值。"
            )
        return (
            f"[Local weather — internal] {fact}{stale}. "
            "Mention only when asked or naturally relevant; never invent numbers."
        )
    except Exception:
        return ""


def weather_proactive_hook(snap: Optional[WeatherSnapshot], lang: str = "zh") -> Optional[str]:
    """强信号才给主动开场种子：暴雨/雷暴/极端气温/大雪；否则 None。"""
    try:
        if snap is None:
            return None
        zh_mode = str(lang or "zh").lower().startswith("zh")
        strong = False
        reason = ""
        if snap.bucket == "storm":
            strong, reason = True, (snap.summary_zh if zh_mode else snap.summary_en)
        elif snap.bucket == "snow" and snap.weather_code in (75, 86):
            strong, reason = True, (snap.summary_zh if zh_mode else snap.summary_en)
        elif snap.bucket == "rain" and snap.weather_code in (65, 82):
            strong, reason = True, (snap.summary_zh if zh_mode else snap.summary_en)
        elif snap.temp_c is not None and (snap.temp_c <= 5.0 or snap.temp_c >= 35.0):
            strong = True
            reason = (
                f"气温 {snap.temp_c:.0f}°C" if zh_mode
                else f"{snap.temp_c:.0f}°C outside")
        if not strong:
            return None
        if zh_mode:
            return f"今天这边{reason}，想自然跟对方提一句天气/出门的事（别像播报）。"
        return f"It's {reason} here — naturally mention weather/going out if it fits."
    except Exception:
        return None


def scene_conflicts_with_weather(scene: str, snap: Optional[WeatherSnapshot]) -> bool:
    """高置信场景×天气冲突；存疑一律 False（不误杀）。"""
    try:
        if snap is None:
            return False
        s = str(scene or "").strip().lower()
        if not s:
            return False
        outdoor_sunny = any(k in s for k in (
            "beach", "seawall", "sunny", "park", "coastal", "patio", "tennis",
            "outdoor cafe", "rooftop",
        ))
        snow_scene = "snow" in s or "snowy" in s
        if snap.bucket in ("storm", "rain") and outdoor_sunny:
            # 小毛毛雨不拦；中雨及以上才冲突
            if snap.weather_code >= 61 or snap.bucket == "storm":
                return True
        if snow_scene and snap.temp_c is not None and snap.temp_c > 12.0:
            return True
        if snow_scene and snap.bucket in ("clear", "cloudy") and snap.weather_code < 70:
            # 无雪却拍雪景：仅在明确非冷季温度时拦
            if snap.temp_c is not None and snap.temp_c > 8.0:
                return True
        return False
    except Exception:
        return False


def dump_stats() -> Dict[str, int]:
    with _LOCK:
        return dict(_STATS)


def reset_stats_for_tests() -> None:
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0


def clear_cache_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()
