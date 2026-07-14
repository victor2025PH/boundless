# -*- coding: utf-8 -*-
"""
任务风险等级 + 门禁矩阵 — 与 config/task_execution_policy.yaml 中 gate_mode / tier_by_prefix / gate_matrix 联动。

- tier: L0（尽量不挡） / L1（仅外网） / L2（业务默认） / L3（最严）
- preflight: none | network_only | full
- geo: 是否对出口国做校验
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_VALID_TIERS = frozenset({"L0", "L1", "L2", "L3"})
_VALID_MODES = frozenset({"strict", "balanced", "dev"})
_VALID_PREFLIGHT = frozenset({"none", "network_only", "full"})


def _default_matrix() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """内置默认矩阵（YAML 未写 gate_matrix 时使用）。"""
    return {
        "strict": {
            "L0": {"preflight": "none", "geo": False},
            "L1": {"preflight": "full", "geo": False},
            "L2": {"preflight": "full", "geo": True},
            "L3": {"preflight": "full", "geo": True},
        },
        "balanced": {
            "L0": {"preflight": "none", "geo": False},
            "L1": {"preflight": "network_only", "geo": False},
            "L2": {"preflight": "full", "geo": True},
            "L3": {"preflight": "full", "geo": True},
        },
        "dev": {
            "L0": {"preflight": "none", "geo": False},
            "L1": {"preflight": "network_only", "geo": False},
            "L2": {"preflight": "full", "geo": False},
            "L3": {"preflight": "full", "geo": True},
        },
    }


def get_effective_gate_matrix(policy: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """合并内置矩阵与 YAML 覆盖（供策略 API 展示）。"""
    return _merge_matrix_from_policy(policy)


def _merge_matrix_from_policy(policy: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    base = _default_matrix()
    raw = policy.get("gate_matrix")
    if not isinstance(raw, dict):
        return base
    for mode, tiers in raw.items():
        if mode not in base or not isinstance(tiers, dict):
            continue
        for tier, row in tiers.items():
            if tier not in base[mode] or not isinstance(row, dict):
                continue
            if row.get("preflight") in _VALID_PREFLIGHT:
                base[mode][tier]["preflight"] = row["preflight"]
            if "geo" in row:
                base[mode][tier]["geo"] = bool(row["geo"])
    return base


def resolve_gate_mode(policy: Dict[str, Any]) -> str:
    gm = (policy.get("gate_mode") or "strict").strip().lower()
    if gm not in _VALID_MODES:
        logger.warning("[gate_matrix] 未知 gate_mode=%s，回退 strict", gm)
        return "strict"
    return gm


def resolve_task_tier(task_type: str, policy: Dict[str, Any]) -> str:
    """最长前缀匹配 tier_by_prefix，否则 default_tier（默认 L2）。"""
    if not task_type:
        return "L2"
    m = policy.get("tier_by_prefix")
    if not isinstance(m, dict) or not m:
        return str(policy.get("default_tier") or "L2").upper()
    best_len = -1
    best: Optional[str] = None
    for prefix, tier in m.items():
        p = str(prefix)
        t = str(tier).upper().strip()
        if t not in _VALID_TIERS:
            continue
        if task_type.startswith(p) and len(p) > best_len:
            best_len = len(p)
            best = t
    if best:
        return best
    dt = str(policy.get("default_tier") or "L2").upper()
    return dt if dt in _VALID_TIERS else "L2"


def resolve_requirements(
    policy: Dict[str, Any], task_type: str
) -> Tuple[str, str, str, bool, Dict[str, Any]]:
    """
    返回 (gate_mode, tier, preflight_mode, geo_enforce, row_dict)。
    """
    gm = resolve_gate_mode(policy)
    tier = resolve_task_tier(task_type, policy)
    matrix = _merge_matrix_from_policy(policy)
    row = dict(matrix.get(gm, {}).get(tier, matrix["strict"]["L2"]))
    pf = str(row.get("preflight") or "full")
    if pf not in _VALID_PREFLIGHT:
        pf = "full"
    geo = bool(row.get("geo", True))
    return gm, tier, pf, geo, row
