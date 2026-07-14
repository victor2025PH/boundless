# -*- coding: utf-8 -*-
"""告警文案上下文：手机编号、所在主机、连接方式；供 Telegram/HTML 告警使用。"""

from __future__ import annotations

import json
import logging
import platform
import re
from typing import Any, Dict, Optional

from src.host.device_alias_labels import get_slot, load_local_cluster_identity
from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_ALIASES_PATH = config_file("device_aliases.json")
_aliases_cache: Optional[Dict[str, Any]] = None
_aliases_mtime: float = 0.0


def _load_aliases_cached() -> Dict[str, Any]:
    global _aliases_cache, _aliases_mtime
    try:
        if not _ALIASES_PATH.is_file():
            return {}
        mtime = _ALIASES_PATH.stat().st_mtime
        if _aliases_cache is not None and abs(mtime - _aliases_mtime) < 1e-9:
            return _aliases_cache
        with open(_ALIASES_PATH, encoding="utf-8") as f:
            _aliases_cache = json.load(f) or {}
        _aliases_mtime = float(mtime)
        return _aliases_cache
    except Exception as e:
        logger.debug("加载 device_aliases 失败: %s", e)
        return {}


def _is_wireless_adb(device_id: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", (device_id or "").strip()))


def resolve_device_alert_context(device_id: str) -> Dict[str, Any]:
    """
    解析设备相关展示字段。
    - phone_label: 面板编号（display_label / alias / #slot）
    - host_pc: 手机登记所在主机（aliases.host_name，缺省为当前 OpenClaw 节点名）
    - service_node: 发出本告警的 OpenClaw 节点（cluster host_name / 机器名）
    """
    did = (device_id or "").strip()
    aliases = _load_aliases_cached()
    entry: Dict[str, Any] = dict(aliases.get(did) or {})
    slot = get_slot(entry)
    slot_s = f"#{slot}" if slot else "—"
    if (entry.get("display_label") or "").strip():
        phone_label = entry["display_label"].strip()
    elif (entry.get("alias") or "").strip():
        phone_label = entry["alias"].strip()
    else:
        phone_label = slot_s

    host_from_alias = (entry.get("host_name") or entry.get("host_label_short") or "").strip()
    _hid, hn_cluster = load_local_cluster_identity()
    service_node = (hn_cluster or "").strip() or platform.node()

    # 「手机在哪个电脑」：优先别名登记的主机名，否则视为当前检测节点即归属
    host_pc_cn = host_from_alias or service_node
    host_pc_en = _host_pc_to_en(host_from_alias) or _host_pc_to_en(hn_cluster) or service_node

    if _is_wireless_adb(did):
        link_cn, link_en = "无线 ADB (TCP)", "Wireless ADB (TCP)"
    else:
        link_cn, link_en = "USB", "USB"

    return {
        "device_id": did,
        "phone_number": slot_s,
        "phone_label": phone_label,
        "host_pc_cn": host_pc_cn,
        "host_pc_en": host_pc_en,
        "service_node": service_node,
        "link_cn": link_cn,
        "link_en": link_en,
    }


def _host_pc_to_en(name: str) -> str:
    if not (name or "").strip():
        return ""
    n = name.strip()
    if "主控" in n or n.lower() in ("coordinator", "master"):
        return "Coordinator"
    m = re.match(r"(?i)worker[-_\s]?(\d+)", n)
    if m:
        return f"Worker-{int(m.group(1)):02d}"
    return n


def approximate_english_message(zh: str) -> str:
    """对常见自动告警文案做英文化，其余保留简短说明。"""
    s = (zh or "").strip()
    if not s:
        return ""
    out = s
    repl = [
        (r"设备掉线\s*\(连续第(\d+)次\)", r"Device offline (streak \1)"),
        (r"设备已隔离，不再分配新任务", "Device isolated; no new tasks assigned"),
        (r"设备已解除隔离", "Device isolation cleared"),
        (r"预测性告警:\s*", "Predictive alert: "),
        (r"10分钟内掉线(\d+)次", r"\1 disconnect(s) within 10 min"),
        (r"1小时内掉线(\d+)次", r"\1 disconnect(s) within 1 hour"),
        (r"掉线频率加速", "disconnect rate accelerating"),
        (r"风险分\s*(\d+)", r"risk score \1"),
        (r"手机掉线\s*", "Phone offline "),
        (r"连续第(\d+)次", r"streak \1"),
        (r"Worker 在线设备减少:\s*", "Worker online count dropped: "),
        (r"adb在线\s*(\d+)\s*→\s*(\d+)\s*台", r"ADB online \1 → \2 devices"),
        (r"心跳登记\s*(\d+)→(\d+)", r"heartbeat registry \1→\2"),
    ]
    for pat, rep in repl:
        out = re.sub(pat, rep, out, flags=re.IGNORECASE)
    if out == s and any("\u4e00" <= c <= "\u9fff" for c in s):
        return "(See Chinese line above / 原文见中文)"
    return out
