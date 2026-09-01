"""相册媒体「季节 / 地点 / 敏感度」维度的单源词表与纯函数判定（实施90 相册智能化）。

背景：相册条目此前只有 ``scene:``/``tod:``/``series:`` 三族标签，「冬天发夏装照」
「人设在温哥华却发东京街景」两类穿帮没有任何数据维度可判。本模块是新增两族
标签的**唯一语义出口**（词表/归一化/冲突判定都在这里，别在消费方再造）：

- ``season:<spring|summer|autumn|winter>``——照片画面里的**显式季节证据**
  （雪、光秃树、盛夏海滩装、圣诞装饰…），由上传自动打标（VLM）或运营手标写入；
  无证据＝不打标＝不参与过滤（与 tod 回填的保守哲学一致）。
- ``place:<ISO-3166 两位国别码>``——照片拍摄地国别；只有高置信来源
  （明确地标 / EXIF GPS）才写。

判定语义（宁可放过不误拦）：
- 季节冲突只认**对立季**（summer↔winter）；春/秋与任何季节都不算硬冲突
  （过渡季画面歧义大，误拦代价高于放过）。
- 季节门/地点门都只约束**通用池/轮播**，运营显式触发词命中最高优先不受限
  （与 ``required_scene_class`` 的「运营意图优先」同一先例）——由
  ``persona_media.select_media`` 落实，本模块只提供判定。

场景词表本身的 canonical 单源仍是 ``persona_media.scene_class_of``（14 类），
本模块的 ``normalize_scene`` 只是它的转发口，供上传打标解析统一取用。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

SEASONS = ("spring", "summer", "autumn", "winter")
VALID_TOD = ("day", "night")

# 敏感度 0-3 → 建议关系门槛（min_bond_level）。只是**建议值**（UI 采纳才生效），
# 映射刻度对齐既有 bond 分层惯例：0=公开、20=熟人、45=亲密、70=私密。
_SENSITIVITY_BOND = {0: 0, 1: 20, 2: 45, 3: 70}

_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")


def normalize_season(v: Any) -> str:
    """季节值归一：词表内返回小写 canonical，否则 ""（=无标注，不参与过滤）。"""
    s = str(v or "").strip().lower()
    return s if s in SEASONS else ""


def normalize_tod(v: Any) -> str:
    """时段值归一（day/night；unknown/其它 → ""）。与 tod 回填口径一致。"""
    s = str(v or "").strip().lower()
    return s if s in VALID_TOD else ""


def normalize_country(v: Any) -> str:
    """国别码归一：两位字母 → 大写 ISO 码；其它（含 none/unknown 全称）→ ""。"""
    s = str(v or "").strip()
    if not _COUNTRY_RE.match(s):
        return ""
    return s.upper()


def normalize_scene(v: Any) -> str:
    """场景短语 → canonical 场景类。

    先认**类名本身**（VLM 按枚举答 ``home``/``night_city`` 这类词——个别类名
    不在自己的触发词表里，scene_class_of 反而认不出），再回落
    ``persona_media.scene_class_of`` 短语归一。单源仍在 persona_media。
    """
    try:
        from src.companion.persona_media import SCENE_CLASSES, scene_class_of
        s = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
        if s in SCENE_CLASSES:
            return s
        return scene_class_of(v)
    except Exception:
        return ""


def _tag_value(row: Optional[Dict[str, Any]], prefix: str) -> str:
    for t in (row or {}).get("tags") or []:
        ts = str(t or "")
        if ts.startswith(prefix):
            return ts[len(prefix):].strip()
    return ""


def row_season(row: Optional[Dict[str, Any]]) -> str:
    """条目的季节标注（tags 里 ``season:<v>``）；无/非法则 ""=不限。"""
    return normalize_season(_tag_value(row, "season:"))


def row_place(row: Optional[Dict[str, Any]]) -> str:
    """条目的拍摄地国别（tags 里 ``place:<CC>``）；无/非法则 ""=不限。"""
    return normalize_country(_tag_value(row, "place:"))


def season_for(now: Any = None, hemisphere: str = "north") -> str:
    """此刻季节（转发 ``outfit_state.season_of``——月份→季节的单源就在那里）。"""
    try:
        from src.companion.outfit_state import season_of
        return season_of(now, hemisphere)
    except Exception:
        return ""


def season_tag_conflicts(season_tag: Any, now_season: Any) -> bool:
    """照片季节标注与当前季节是否**硬冲突**（纯函数）。

    只认对立季：``summer`` 照片在 ``winter``、``winter`` 照片在 ``summer``。
    春/秋 vs 任何季节、无标注、判不出当前季节 → 一律不冲突（保守）。
    """
    t = normalize_season(season_tag)
    n = normalize_season(now_season)
    if not t or not n:
        return False
    return {t, n} == {"summer", "winter"}


def place_mismatch(place_tag: Any, home_country: Any) -> bool:
    """照片国别标注与人设所在国是否明确不一致（两边都有值才判）。"""
    p = normalize_country(place_tag)
    h = normalize_country(home_country)
    if not p or not h:
        return False
    return p != h


# 场景等价组（分歧提示防误报用，与 image_gate 的互认哲学同构但独立实现）：
# 镜前自拍标 home、VLM 看是 bedroom——语义上都对，不该被点名「分歧」；
# cafe/restaurant 同理。只用于**建议层**（分歧提示），不改硬匹配语义。
_SCENE_EQUIV_GROUPS = (
    frozenset({"home", "bedroom", "kitchen"}),
    frozenset({"cafe", "restaurant"}),
)


def scenes_equivalent(a: Any, b: Any) -> bool:
    """两个 canonical 场景类是否互认（相同或同等价组）。空值不互认。"""
    sa, sb = str(a or "").strip().lower(), str(b or "").strip().lower()
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    return any(sa in g and sb in g for g in _SCENE_EQUIV_GROUPS)


def suggest_min_bond(sensitivity: Any) -> int:
    """敏感度 0-3 → 建议关系门槛。非法/越界值向内收敛（<0 按 0、>3 按 3）。"""
    try:
        s = int(sensitivity)
    except (TypeError, ValueError):
        return 0
    s = max(0, min(3, s))
    return _SENSITIVITY_BOND[s]


def persona_geo_context(persona_id: Any) -> Dict[str, str]:
    """人设的「此刻季节 + 所在国」上下文（挑图门禁用；全程软失败）。

    返回 ``{"season": spring|…|"", "country": ISO|"", "city": slug|""}``——
    任一环节解析不了对应值给 ""（=该维度不设门）。绝不抛。
    """
    out = {"season": "", "country": "", "city": ""}
    pid = str(persona_id or "").strip()
    if not pid:
        return out
    try:
        from src.utils.persona_manager import PersonaManager
        persona = PersonaManager.get_instance().get_persona_by_id(pid)
        if persona is None:
            return out
        from src.companion.persona_location import (
            persona_now,
            resolve_place_with_fallback,
        )
        place = resolve_place_with_fallback(persona)
        if place is None:
            return out
        out["country"] = normalize_country(place.country)
        out["city"] = str(place.slug or "")
        out["season"] = season_for(
            persona_now(place), hemisphere=place.hemisphere)
    except Exception:
        return out
    return out


__all__ = [
    "SEASONS", "VALID_TOD",
    "normalize_season", "normalize_tod", "normalize_country", "normalize_scene",
    "row_season", "row_place", "season_for", "season_tag_conflicts",
    "place_mismatch", "scenes_equivalent", "suggest_min_bond",
    "persona_geo_context",
]
