"""问候链「真实素材」供给（2026-08-16 聊真实世界 P1）。

背景：早晚安（daily_ritual）与温和问候（gentle_checkin）的 prompt 此前只有
「切入角方向」（从天气/窗外/早餐说起）而没有可断言的事实——反编造钉子会正确地
拦住 LLM 编数值，于是问候只能落在「早安，睡得好吗」级安全壳。本模块把**人设
所在城市的真实天气**做成一行可拼进 directive 的素材：

- 单一消费入口：``ritual_weather_line``（早晚安）/ ``checkin_weather_line``
  （温和问候）；SkillManager 侧薄封装见 ``_ritual_weather_line`` /
  ``_checkin_weather_line``（负责解析会话人设后调这里）。
- 开关：``companion.weather.enabled``（父开关，基线默认关）+
  ``companion.weather.greeting_inject``（默认开，随父）。
- 数据走 ``weather_state.fetch_weather``（进程级 TTL 缓存 + 软失败），本模块
  **绝不抛**——缺坐标/取数失败/关闭一律返回 ""，问候链行为与旧版逐字一致。
- 陈旧快照（stale）沿用 weather_state 语义：素材行显式提醒「别报精确数字」。

刻意边界（数据教训，勿随手扩）：
- **只做天气，不带新闻进问候**——news_share 主动开场 14 天回复率 16.7%
  （vs 温和问候 34% / 晚安 38%，2026-08-08 数据止损）。问候里的「真实世界」
  必须与 TA/人设的生活相关；泛新闻属于反应式聊天（冷场/被问起）的领地。
  娱乐素材等「按客户兴趣命中」机制就绪后再进问候（下一阶段）。
- **节日不在这里做**——milestone_ritual（Stage P）已按「节点优先于晨晚安」
  承载节日问候，重复注入＝同一天两条节日话。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

__all__ = [
    "weather_material",
    "ritual_weather_line",
    "checkin_weather_line",
]


def _weather_cfg(config: Any) -> Dict[str, Any]:
    """从整份 config 取 ``companion.weather``；任何形态异常返回 {}。"""
    try:
        comp = config.get("companion") if isinstance(config, dict) else None
        w = comp.get("weather") if isinstance(comp, dict) else None
        return w if isinstance(w, dict) else {}
    except Exception:
        return {}


def weather_material(
    persona: Any,
    weather_cfg: Dict[str, Any],
    *,
    fetch_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """人设城市当前天气素材；无货/关闭/异常 → ``{}``（绝不抛）。

    ``fetch_fn(place, ttl_sec=, max_stale_sec=)`` 可注入（测试用 fake，返回
    ``weather_state.WeatherSnapshot`` 形状的对象）；缺省走真实
    ``weather_state.fetch_weather``（自带 TTL 缓存与软失败）。
    """
    try:
        if not (isinstance(weather_cfg, dict) and weather_cfg.get("enabled")):
            return {}
        if not weather_cfg.get("greeting_inject", True):
            return {}
        from src.companion.persona_location import resolve_place_with_fallback
        place = resolve_place_with_fallback(
            persona if isinstance(persona, dict) else {})
        if place is None:
            return {}
        if fetch_fn is None:
            from src.companion.weather_state import fetch_weather as fetch_fn
        snap = fetch_fn(
            place,
            ttl_sec=int(weather_cfg.get("ttl_sec") or 1800),
            max_stale_sec=int(weather_cfg.get("max_stale_sec") or 10800),
        )
        if snap is None:
            return {}
        city = str(getattr(place, "city_zh", "") or getattr(place, "city_en", "") or "")
        if not city:
            return {}
        temp = getattr(snap, "temp_c", None)
        return {
            "city": city,
            "label_zh": str(getattr(snap, "summary_zh", "") or ""),
            "temp_c": float(temp) if temp is not None else None,
            "stale": bool(getattr(snap, "stale", False)),
            "bucket": str(getattr(snap, "bucket", "") or ""),
        }
    except Exception:
        return {}


def _desc_zh(mat: Dict[str, Any]) -> str:
    label = str(mat.get("label_zh") or "").strip()
    temp = mat.get("temp_c")
    if not label:
        return ""
    if temp is None:
        return label
    return f"{label}、{temp:.0f}°C"


_STALE_NOTE = "（数据稍旧，别报精确数字）"


def ritual_weather_line(
    persona: Any,
    config: Any,
    *,
    slot: str,
    fetch_fn: Optional[Callable[..., Any]] = None,
) -> str:
    """早/晚安 directive 的真实天气素材行；无货返回 ""。

    素材行自带反编造约束（「天气细节只以这条为准」）——与全链既有的
    反编造钉子同向，给方向也给事实。
    """
    s = str(slot or "").strip().lower()
    if s not in ("morning", "night"):
        return ""
    mat = weather_material(persona, _weather_cfg(config), fetch_fn=fetch_fn)
    desc = _desc_zh(mat)
    if not desc:
        return ""
    when = "今早" if s == "morning" else "今晚"
    stale = _STALE_NOTE if mat.get("stale") else ""
    return (
        f"（真实天气·内部事实）你住在{mat['city']}，{when}你那边{desc}{stale}。"
        "可以把天气自然揉进问候里（下雨、降温、天气好都行，顺嘴一提别硬塞；"
        "今天按切入角聊别的更顺就不提天气——别让每天的问候都长成天气播报）；"
        "天气细节只以这条为准，不要自己编。"
    )


def checkin_weather_line(
    persona: Any,
    config: Any,
    *,
    fetch_fn: Optional[Callable[..., Any]] = None,
) -> str:
    """温和问候（gentle_checkin）directive 的天气素材行；无货返回 ""。

    与 ``_weather_opener``（暴雨/极端气温强信号才成为**开场主题**）互补：
    这里是给「从窗外/天气说起」这类切入角配平常天气的**真实值**，不改变
    开场主题的选择。
    """
    mat = weather_material(persona, _weather_cfg(config), fetch_fn=fetch_fn)
    desc = _desc_zh(mat)
    if not desc:
        return ""
    stale = _STALE_NOTE if mat.get("stale") else ""
    return (
        f"（真实天气·内部事实）你住的{mat['city']}现在{desc}{stale}——"
        "若从天气/出门切入就按这个说，不要编造别的天气细节。"
    )
