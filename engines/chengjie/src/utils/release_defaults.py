"""「这版改变了什么」——升级后首开工作台弹一次的默认值变更清单（Q-4 #267 F）。

数据：``config/release_defaults_changed.json``（每版发版线维护；随包进 ``<_internal>/config/``）。
状态：``<config_dir>/release_notice_state.json`` ``{"seen": {"1.0.79": ts}, "last_version": "…"}``。

判「升级后首开」而不是「clean 安装」（clean 装出厂默认就是默认，没什么可弹）：
- 状态文件里已 seen 本版 → 不弹；
- 状态文件 ``last_version`` 在场且 ≠ 本版 → 升级机（1.0.79 起每版都会留下 last_version）；
- 或 overlay 里有任一行的 ``marker``（``baseline_rollback`` 由 1.0.78→1.0.79 撤回写入）→ 升级机；
- 否则 → 视为 clean 装（或从「没被这些默认影响过」的老版本升上来）→ 静默记 seen 不弹。
  ⚠ overlay 非空**不能**当升级信号：``_ensure_baseline`` / 首启向导 / secret_key 在 clean 装
  首启就会往 overlay 写叶子。

行过滤 ``show_if``：
- ``marker``：只在 overlay 里有该行 marker（本机确实被撤回过）时弹——精确命中「基线补进去
  又被撤回」的那批机器；用户自己配过（无 marker）或从没有过（老版本）都不弹；
- ``missing``：overlay 里已有该键（用户显式写过）→ 不弹；
- ``always``：升级机一律弹（A 类基线会被 ``_ensure_baseline`` 写进 overlay，无法与用户显式值
  区分，只能如实告知）。

选择落地（红线①同源 ``ConfigManager.set_overlay_flag``）：``keep`` → 写**显式**新默认
（用户点过就是显式值，下版基线再怎么变都不会碰它）；``flip`` → 写旧值（=「开启」）；
班表类键 flip 时必须带合法 IANA 时区（红线③：不许留空落服务器本机）。永不抛。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
DATA_FILE = _ENGINE_ROOT / "config" / "release_defaults_changed.json"
STATE_FILENAME = "release_notice_state.json"
SHOW_IF_DEFAULT = "missing"
SHOW_IF_VALUES = ("marker", "missing", "always")
CHOICES = ("keep", "flip")
_SERVER_TZ_SENTINELS = {"", "local", "server", "system"}


# ── 数据 ──────────────────────────────────────────────────────────────────
def load_release_defaults(path: Optional[Path] = None) -> Dict[str, Any]:
    """读数据文件；缺失 / 损坏 → ``{"schema": 0, "releases": {}}``（永不抛）。"""
    p = Path(path) if path else DATA_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("releases"), dict):
            return {"schema": 0, "releases": {}}
        return data
    except Exception:
        logger.debug("release_defaults_changed.json 读取失败: %s", p, exc_info=True)
        return {"schema": 0, "releases": {}}


def release_changes(version: str, data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """某版的 change 列表（浅拷贝，字段归一化）；未登记 → []。"""
    d = data if data is not None else load_release_defaults()
    rel = (d.get("releases") or {}).get(str(version or "").strip())
    if not isinstance(rel, dict):
        return []
    out: List[Dict[str, Any]] = []
    for ch in rel.get("changes") or []:
        if not isinstance(ch, dict) or not str(ch.get("key") or "").strip():
            continue
        c = dict(ch)
        c["key"] = str(c["key"]).strip()
        c["kind"] = str(c.get("kind") or ("switch" if isinstance(c.get("new"), bool) else "value"))
        c["i18n"] = str(c.get("i18n") or "rn_" + c["key"].replace(".", "_"))
        c["needs_timezone"] = bool(c.get("needs_timezone"))
        si = str(c.get("show_if") or ("marker" if c.get("marker") else SHOW_IF_DEFAULT))
        c["show_if"] = si if si in SHOW_IF_VALUES else SHOW_IF_DEFAULT
        out.append(c)
    return out


def _version_tuple(v: str) -> tuple:
    parts = []
    for seg in str(v or "").split("."):
        num = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def latest_registered_version(data: Optional[Dict[str, Any]] = None) -> str:
    d = data if data is not None else load_release_defaults()
    vs = [str(k) for k in (d.get("releases") or {}).keys()]
    return max(vs, key=_version_tuple) if vs else ""


# ── 状态 ──────────────────────────────────────────────────────────────────
def state_path(config_manager: Any) -> Optional[Path]:
    try:
        cp = getattr(config_manager, "config_path", None)
        if not cp:
            return None
        return Path(cp).parent / STATE_FILENAME
    except Exception:
        return None


def read_state(config_manager: Any) -> Dict[str, Any]:
    p = state_path(config_manager)
    if p is None or not p.exists():
        return {"seen": {}, "last_version": ""}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return {"seen": {}, "last_version": ""}
        data.setdefault("seen", {})
        data.setdefault("last_version", "")
        if not isinstance(data["seen"], dict):
            data["seen"] = {}
        return data
    except Exception:
        return {"seen": {}, "last_version": ""}


def mark_seen(config_manager: Any, version: str, *, reason: str = "") -> bool:
    p = state_path(config_manager)
    if p is None:
        return False
    st = read_state(config_manager)
    st["seen"][str(version)] = {"ts": round(time.time(), 3), "reason": reason}
    st["last_version"] = str(version)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        logger.debug("release_notice_state 写入失败", exc_info=True)
        return False


# ── 判定 ──────────────────────────────────────────────────────────────────
def _overlay_leaves(config_manager: Any) -> Dict[str, Any]:
    fn = getattr(config_manager, "_overlay_leaves", None)
    if callable(fn):
        try:
            return dict(fn() or {})
        except Exception:
            return {}
    return {}


def _dig(cfg: Any, dotted: str) -> Any:
    node = cfg
    for k in str(dotted or "").split("."):
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def is_upgrade_install(config_manager: Any, version: str,
                       data: Optional[Dict[str, Any]] = None) -> bool:
    """升级机判定：状态文件 ``last_version`` ≠ 本版，或 overlay 里有任一行的回滚 marker。"""
    st = read_state(config_manager)
    lv = str(st.get("last_version") or "").strip()
    if lv and lv != str(version):
        return True
    leaves = _overlay_leaves(config_manager)
    for ch in release_changes(version, data):
        mk = str(ch.get("marker") or "")
        if mk and mk in leaves:
            return True
    return False


def pending_rows(config_manager: Any, version: str,
                 data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """本版要弹的行（已按 show_if 过滤），每行附 ``current``（合并视图当前值）与
    ``current_timezone``（班表类）。"""
    leaves = _overlay_leaves(config_manager)
    cfg = getattr(config_manager, "config", None) or {}
    rows: List[Dict[str, Any]] = []
    for ch in release_changes(version, data):
        key = ch["key"]
        si = ch["show_if"]
        if si == "marker":
            mk = str(ch.get("marker") or "")
            if not mk or mk not in leaves:
                continue
        elif si == "missing":
            if key in leaves:
                continue
        row = dict(ch)
        row["current"] = _dig(cfg, key)
        if ch["needs_timezone"]:
            tzk = str(ch.get("timezone_key") or "")
            row["current_timezone"] = str(_dig(cfg, tzk) or "") if tzk else ""
        rows.append(row)
    return rows


def pending_notice(config_manager: Any, current_version: str,
                   data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``{"pending": bool, "version": str, "rows": [...], "why": str}``。

    ``current_version`` 为 ``dev`` / 空 / 未登记版本 → 不弹（源码态不骚扰）。
    clean 装 → 静默 mark_seen 并不弹。
    """
    v = str(current_version or "").strip()
    d = data if data is not None else load_release_defaults()
    out: Dict[str, Any] = {"pending": False, "version": v, "rows": [], "why": ""}
    if not v or v == "dev" or v not in (d.get("releases") or {}):
        out["why"] = "version_not_registered"
        return out
    st = read_state(config_manager)
    if v in st.get("seen", {}):
        out["why"] = "seen"
        return out
    if not is_upgrade_install(config_manager, v, d):
        mark_seen(config_manager, v, reason="clean_install")
        out["why"] = "clean_install"
        return out
    rows = pending_rows(config_manager, v, d)
    if not rows:
        mark_seen(config_manager, v, reason="no_rows")
        out["why"] = "no_rows"
        return out
    out["pending"] = True
    out["rows"] = rows
    out["why"] = "upgrade"
    return out


# ── 落地 ──────────────────────────────────────────────────────────────────
def _valid_tz(name: str) -> bool:
    n = str(name or "").strip()
    if n.lower() in _SERVER_TZ_SENTINELS:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(n)
        return True
    except Exception:
        return False


def apply_choices(config_manager: Any, version: str, choices: Dict[str, str],
                  *, timezone: str = "", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按用户选择写 overlay 显式值并 mark_seen。

    返回 ``{"ok", "applied": [dotted…], "failed": [{key, reason}], "seen": bool}``。
    - 未出现在 ``choices`` 里的行按 ``keep``（关闭弹窗＝接受新默认，写显式值）。
    - ``flip`` 且 ``needs_timezone`` 而时区非法 → 该行 ``failed: tz_required``，其余照写，
      **不 mark_seen**（让用户补时区再来）。
    """
    res: Dict[str, Any] = {"ok": True, "applied": [], "failed": [], "seen": False}
    setter = getattr(config_manager, "set_overlay_flag", None)
    if not callable(setter):
        res["ok"] = False
        res["failed"].append({"key": "*", "reason": "config_unavailable"})
        return res
    rows = pending_rows(config_manager, version, data)
    tz = str(timezone or "").strip()
    for row in rows:
        key = row["key"]
        choice = str((choices or {}).get(key) or "keep").strip().lower()
        if choice not in CHOICES:
            res["failed"].append({"key": key, "reason": "bad_choice"})
            continue
        if row["kind"] != "switch" and choice == "flip":
            res["failed"].append({"key": key, "reason": "not_a_switch"})
            continue
        value = row.get("old") if choice == "flip" else row.get("new")
        if choice == "flip" and row["needs_timezone"]:
            if not _valid_tz(tz):
                res["failed"].append({"key": key, "reason": "tz_required"})
                continue
            ok_tz, msg_tz = setter(str(row.get("timezone_key") or ""), tz)
            if not ok_tz:
                res["failed"].append({"key": row.get("timezone_key"), "reason": str(msg_tz)})
                continue
            res["applied"].append(str(row.get("timezone_key")))
        ok, msg = setter(key, value)
        if ok:
            res["applied"].append(key)
            logger.info("[release_notice] %s %s=%r choice=%s", version, key, value, choice)
        else:
            res["failed"].append({"key": key, "reason": str(msg)})
    res["ok"] = not res["failed"]
    if res["ok"]:
        res["seen"] = mark_seen(config_manager, version, reason="applied")
    return res


__all__ = [
    "DATA_FILE", "STATE_FILENAME", "CHOICES", "load_release_defaults", "release_changes",
    "latest_registered_version", "read_state", "mark_seen", "is_upgrade_install",
    "pending_rows", "pending_notice", "apply_choices", "state_path",
]
