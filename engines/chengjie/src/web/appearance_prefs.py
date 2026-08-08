# -*- coding: utf-8 -*-
"""坐席外观个性化（appearance）偏好的净化与序列化。

存储落点：`inbox.store.agent_prefs.appearance`（JSON 文本，经本模块白名单净化）。
读写路由：`/api/workspace/prefs`（GET 回显解析后的 dict；POST 带 ``appearance`` 键时写入）。
前端消费：`static/workspace/appearance.js`（收件箱快捷面板 + /personal-settings 个人设置页
共用同一渲染器与同一状态 schema）。

设计约束：
- **白名单 + 值域夹紧**：未知键丢弃、颜色必须 #rrggbb、圆角/字号/夜间时刻全部夹紧——
  该 JSON 会被前端直接放进 CSS 变量，净化层是防注入与防「存进去就渲染坏」的唯一闸门。
- **软失败**：格式非法返回 None（路由回 400），绝不半净化落库。
- 与前端 appearance.js 的 DEFAULTS 同构；改 schema 两边一起改并升 ``v``。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

APPEARANCE_MAX_BYTES = 2048

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_THEMES = {"brand", "aurora", "starry", "sunset", "graphite", "sakura", "custom"}
_WALLS = {"flat", "mist", "night", "dusk", "dots"}
_NIGHT_MODES = {"auto", "light", "dark", "schedule"}
_RADII = {8, 12, 16, 20}
_FS_MIN, _FS_MAX = 13, 16


def _hex_or_empty(v: Any) -> str:
    return v if isinstance(v, str) and _HEX_RE.match(v) else ""


def _minute(v: Any, default: int) -> int:
    try:
        m = int(v)
    except (TypeError, ValueError):
        return default
    return m if 0 <= m <= 1439 else default


def sanitize_appearance(raw: Any) -> Optional[Dict[str, Any]]:
    """净化外观偏好；非 dict → None。返回值保证键/值域封闭、可安全落库与回放。"""
    if not isinstance(raw, dict):
        return None
    night_raw = raw.get("night")
    if not isinstance(night_raw, dict):
        night_raw = {}
    try:
        radius = int(raw.get("radius", 16))
    except (TypeError, ValueError):
        radius = 16
    try:
        fs = int(raw.get("fs", 13))
    except (TypeError, ValueError):
        fs = 13
    try:
        ts = int(raw.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    theme = raw.get("theme")
    wall = raw.get("wall")
    mode = night_raw.get("mode")
    out = {
        "v": 1,
        "theme": theme if theme in _THEMES else "brand",
        "out": _hex_or_empty(raw.get("out")),
        "in": _hex_or_empty(raw.get("in")),
        "wall": wall if wall in _WALLS else "flat",
        "radius": radius if radius in _RADII else 16,
        "fs": min(_FS_MAX, max(_FS_MIN, fs)),
        "night": {
            "mode": mode if mode in _NIGHT_MODES else "auto",
            "start": _minute(night_raw.get("start"), 1320),
            "end": _minute(night_raw.get("end"), 480),
        },
        "anim": bool(raw.get("anim", True)),
        "ts": max(0, ts),
    }
    blob = dumps_appearance(out)
    if len(blob.encode("utf-8")) > APPEARANCE_MAX_BYTES:
        return None
    return out


def dumps_appearance(d: Dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def loads_appearance(s: Any) -> Dict[str, Any]:
    """解析落库 JSON 供 API 回显；坏数据/空串一律回 {}（前端回落默认主题）。"""
    if not s or not isinstance(s, str):
        return {}
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}
