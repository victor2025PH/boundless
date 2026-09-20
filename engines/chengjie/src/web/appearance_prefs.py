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

# 4096（2026-08-17 Dock P3 由 2048 上调）：appearance JSON 列现在还承载 dock_order
# 子键（账号坞自定义排序，见 sanitize_dock_order）——多平台多账号 id 会把 blob 撑过旧限。
APPEARANCE_MAX_BYTES = 4096

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_THEMES = {"brand", "aurora", "starry", "sunset", "graphite", "sakura", "custom"}
_WALLS = {"flat", "mist", "night", "dusk", "dots"}
_NIGHT_MODES = {"auto", "light", "dark", "schedule"}
_RADII = {8, 12, 16, 20}
_FS_MIN, _FS_MAX = 13, 16

# ---- Dock P3：账号坞自定义排序（dock_order 子键）----------------------------
# 形状 {"ts": epoch_ms, "plats": {"telegram": ["8244899900", ...], ...}}。
# 落在 appearance 同一 JSON 列（零迁移零新列）；appearance.js 的整状态推送不带
# 此键 → 路由层负责从库里已存值 carry-over（见 workspace_prefs 路由），这里只管
# 「进来的 dock_order 是否值域封闭」。id 集合刻意宽字符集：TG 数字 / LINE U 开头
# base64ish / 自定义别名都要装得下，但绝不许引号尖括号（该 JSON 会回放进前端）。
_DOCK_PLAT_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_DOCK_AID_RE = re.compile(r"^[A-Za-z0-9_.@:+\-]{1,80}$")
_DOCK_MAX_PLATS = 8
_DOCK_MAX_AIDS = 30


def sanitize_dock_order(raw: Any) -> Optional[Dict[str, Any]]:
    """净化账号坞排序偏好；非 dict → None（调用方按非法拒绝）。

    空 plats 是合法值（显式「已清空自定义排序」，与「从未设置」区分开——
    pull 端见键即采信，跨设备的恢复默认才能传播）。
    """
    if not isinstance(raw, dict):
        return None
    plats_raw = raw.get("plats")
    if not isinstance(plats_raw, dict):
        return None
    try:
        ts = int(raw.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    plats: Dict[str, Any] = {}
    for plat, aids in list(plats_raw.items())[:_DOCK_MAX_PLATS]:
        if not (isinstance(plat, str) and _DOCK_PLAT_RE.match(plat)):
            continue
        if not isinstance(aids, list):
            continue
        seen: set = set()
        clean = []
        for aid in aids:
            s = str(aid)
            if not _DOCK_AID_RE.match(s) or s in seen:
                continue
            seen.add(s)
            clean.append(s)
            if len(clean) >= _DOCK_MAX_AIDS:
                break
        if clean:
            plats[plat] = clean
    return {"ts": max(0, ts), "plats": plats}


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
    # Dock P3：raw 带 dock_order 且值域合法则透传（appearance 整写不丢排序）；
    # 非法/缺席都静默不落——appearance 写链的 dock_order 保全由路由层 carry-over 兜底。
    dk = sanitize_dock_order(raw.get("dock_order")) if "dock_order" in raw else None
    if dk is not None:
        out["dock_order"] = dk
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
