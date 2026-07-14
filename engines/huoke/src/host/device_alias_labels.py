# -*- coding: utf-8 -*-
"""设备分域槽位与展示标签 — 每台主机内 slot 可重复（跨机 01 不冲突）。"""
from __future__ import annotations

import logging
import re
from typing import Any

from .device_registry import DEFAULT_DEVICES_YAML, config_file

log = logging.getLogger(__name__)


def load_local_cluster_identity() -> tuple[str, str]:
    """返回本机 (host_id, host_name)。"""
    try:
        import yaml as _yaml
        p = config_file("cluster.yaml")
        if not p.exists():
            return "standalone", ""
        with open(p, encoding="utf-8") as f:
            c = _yaml.safe_load(f) or {}
        hid = (c.get("host_id") or "standalone").strip() or "standalone"
        hn = (c.get("host_name") or "").strip()
        return hid, hn
    except Exception:
        return "standalone", ""


def short_label_from_scope(
    host_scope: str,
    host_name: str,
    *,
    fallback_display: str = "",
) -> str:
    """生成面板/壁纸旁注用的短前缀，如 主控、W03、W175。"""
    hn = (host_name or "").strip()
    if hn.lower() in ("unknown", "?"):
        hn = ""
    hs = (host_scope or "").strip()
    if hs.lower() == "unknown":
        hs = ""
    if hn:
        m = re.match(r"(?i)worker[-_\s]?(\d+)", hn)
        if m:
            return f"W{int(m.group(1)):02d}"
        m2 = re.match(r"(?i)^w\s*(\d+)$", hn)
        if m2:
            return f"W{int(m2.group(1)):02d}"
        if "主控" in hn or hn.lower() in ("coordinator", "master"):
            return "主控"
        if len(hn) <= 12:
            return hn
    if hs and hs != "unknown":
        if hs == "coordinator":
            return "主控"
        m3 = re.match(r"(?i)worker[-_]?(\d+)", hs)
        if m3:
            return f"W{int(m3.group(1)):02d}"
        return hs[:10]
    fd = (fallback_display or "").strip()
    if fd:
        return fd[:8]
    return "?"


def format_display_label(short: str, slot: int) -> str:
    return f"{short}-{int(slot):02d}"


def get_slot(entry: dict[str, Any] | None) -> int:
    if not entry:
        return 0
    s = entry.get("slot")
    if isinstance(s, int) and s > 0:
        return s
    n = entry.get("number")
    if isinstance(n, int) and n > 0:
        return n
    return 0


def apply_slot_and_labels(
    entry: dict[str, Any],
    slot: int,
    host_scope: str,
    host_name: str,
    *,
    sync_legacy_number: bool = True,
) -> dict[str, Any]:
    """写入 slot、host_scope、display_label、alias；可选同步 number 供旧代码。"""
    out = dict(entry)
    out["slot"] = int(slot)
    out["host_scope"] = host_scope
    _hn = (host_name or "").strip()
    if _hn and _hn.lower() not in ("unknown", "?"):
        out["host_name"] = _hn
    elif (out.get("host_name") or "").lower() in ("unknown", "?"):
        out.pop("host_name", None)
    short = short_label_from_scope(
        host_scope, host_name, fallback_display=str(entry.get("display_name") or ""),
    )
    out["host_label_short"] = short
    out["display_label"] = format_display_label(short, slot)
    out["alias"] = out["display_label"]
    if sync_legacy_number:
        out["number"] = int(slot)
    return out


def resolve_host_scope_for_device(
    device_id: str,
    entry: dict[str, Any] | None,
    *,
    local_device_ids: set[str] | None = None,
) -> tuple[str, str]:
    """
    返回 (host_scope, host_name)。
    优先 entry；本机 USB 用 cluster host_id；集群设备用 coordinator 里 Worker 映射。
    """
    entry = entry or {}
    if entry.get("host_scope"):
        return str(entry["host_scope"]), (entry.get("host_name") or entry["host_scope"]) or ""

    hid, hname = load_local_cluster_identity()

    if local_device_ids is not None and device_id in local_device_ids:
        return hid, hname or hid

    try:
        from src.device_control.device_manager import get_device_manager
        dm = get_device_manager(DEFAULT_DEVICES_YAML)
        ids = {d.device_id for d in dm.get_all_devices()}
        if device_id in ids:
            return hid, hname or hid
    except Exception:
        pass

    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        if coord:
            for wh_id, hinfo in coord._hosts.items():
                for d in getattr(hinfo, "devices", None) or []:
                    if d.get("device_id") == device_id:
                        return wh_id, (hinfo.host_name or wh_id)
    except Exception as e:
        log.debug("resolve_host_scope cluster: %s", e)

    hn = entry.get("host_name") or ""
    if hn:
        guessed = hn.replace(" ", "-")
        return guessed, hn

    return "unknown", ""


def used_slots_for_scope(
    aliases: dict[str, dict[str, Any]],
    host_scope: str,
    exclude_device_id: str | None = None,
) -> set[int]:
    """某 host_scope 下已被占用的 slot（依赖 entry.host_scope 已写入）。"""
    used: set[int] = set()
    for did, info in aliases.items():
        if exclude_device_id and did == exclude_device_id:
            continue
        if (info or {}).get("host_scope") != host_scope:
            continue
        s = get_slot(info)
        if s > 0:
            used.add(s)
    return used


def used_slots_resolved(
    aliases: dict[str, dict[str, Any]],
    target_scope: str,
    *,
    local_device_ids: set[str] | None = None,
    exclude_device_id: str | None = None,
) -> set[int]:
    """用 resolve_host_scope 动态判断归属时的已占用 slot（兼容未迁移的 aliases）。"""
    used: set[int] = set()
    for did, info in aliases.items():
        if exclude_device_id and did == exclude_device_id:
            continue
        sc, _ = resolve_host_scope_for_device(did, info or {}, local_device_ids=local_device_ids)
        if sc != target_scope:
            continue
        s = get_slot(info or {})
        if s > 0:
            used.add(s)
    return used


def next_free_slot(
    aliases: dict[str, dict[str, Any]],
    host_scope: str,
    range_start: int = 1,
    range_end: int = 999,
    exclude_device_id: str | None = None,
) -> int:
    used = used_slots_for_scope(aliases, host_scope, exclude_device_id)
    for n in range(range_start, range_end + 1):
        if n not in used:
            return n
    return max(used, default=range_start - 1) + 1


def next_free_slot_resolved(
    aliases: dict[str, dict[str, Any]],
    target_scope: str,
    range_start: int,
    range_end: int,
    *,
    local_device_ids: set[str] | None = None,
    exclude_device_id: str | None = None,
) -> int:
    used = used_slots_resolved(
        aliases, target_scope,
        local_device_ids=local_device_ids,
        exclude_device_id=exclude_device_id,
    )
    for n in range(range_start, range_end + 1):
        if n not in used:
            return n
    return max(used, default=range_start - 1) + 1


def migrate_entry_if_needed(
    device_id: str,
    entry: dict[str, Any],
    *,
    local_device_ids: set[str] | None = None,
) -> dict[str, Any]:
    """补全 host_scope / slot / display_label。"""
    e = dict(entry or {})
    scope, hname = resolve_host_scope_for_device(device_id, e, local_device_ids=local_device_ids)
    slot = get_slot(e)
    if slot <= 0:
        slot = 1
    if not e.get("host_scope"):
        e["host_scope"] = scope
    if not e.get("host_name") and hname:
        e["host_name"] = hname
    return apply_slot_and_labels(
        e, slot, e["host_scope"], (e.get("host_name") or hname or ""),
    )
