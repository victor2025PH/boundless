# -*- coding: utf-8 -*-
"""人设发图能力盘点（P1，2026-07-31）——默认关后的开回建议。

纯函数核心：给定人设摘要 + 相册库存统计 → 排出「建议开启」清单。
CLI 见 ``scripts/photo_capability_audit.py``；门禁
``tests/test_photo_capability_audit.py``。

建议开回的判据（只减误开、不替运营做商业决策）：
- 相册有启用中的库存（enabled > 0），或
- 历史上真发过相册媒体（hits / send ledger > 0），
且当前 ``capabilities.photos`` 未开。

已开 / 无库存且无投放记录 → 不进建议表。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def recommend_enable(
    personas: List[Dict[str, Any]],
    stock_by_pid: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """返回建议开启发图能力的人设行（按库存+投放权重降序）。

    ``personas`` 每项至少 ``{id, name?, capabilities?}``。
    ``stock_by_pid[pid]`` 形如 ``{total, enabled, hits, sends?}``。
    """
    rows: List[Dict[str, Any]] = []
    for p in personas or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        if not pid:
            continue
        caps = p.get("capabilities") if isinstance(p.get("capabilities"), dict) else {}
        photos_on = bool(caps.get("photos"))
        st = stock_by_pid.get(pid) or {}
        enabled = int(st.get("enabled") or 0)
        total = int(st.get("total") or 0)
        hits = int(st.get("hits") or st.get("total_hits") or 0)
        sends = int(st.get("sends") or 0)
        has_signal = enabled > 0 or hits > 0 or sends > 0
        if photos_on or not has_signal:
            continue
        score = enabled * 10 + hits + sends
        reason = []
        if enabled > 0:
            reason.append(f"stock_enabled={enabled}")
        if hits > 0:
            reason.append(f"hits={hits}")
        if sends > 0:
            reason.append(f"sends={sends}")
        rows.append({
            "id": pid,
            "name": str(p.get("name") or pid),
            "photos": False,
            "stock_total": total,
            "stock_enabled": enabled,
            "hits": hits,
            "sends": sends,
            "score": score,
            "reason": ",".join(reason) or "signal",
            "action": "enable",
        })
    rows.sort(key=lambda r: (-int(r["score"]), str(r["id"])))
    return rows


def summarize_audit(
    personas: List[Dict[str, Any]],
    stock_by_pid: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """盘点总览：开/关计数 + 建议开回列表。"""
    on = off = orphan_on = 0  # orphan_on = 已开但零库存零投放（可能误开）
    for p in personas or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        if not pid:
            continue
        caps = p.get("capabilities") if isinstance(p.get("capabilities"), dict) else {}
        photos_on = bool(caps.get("photos"))
        st = stock_by_pid.get(pid) or {}
        signal = (
            int(st.get("enabled") or 0)
            + int(st.get("hits") or st.get("total_hits") or 0)
            + int(st.get("sends") or 0)
        ) > 0
        if photos_on:
            on += 1
            if not signal:
                orphan_on += 1
        else:
            off += 1
    rec = recommend_enable(personas, stock_by_pid)
    return {
        "persona_total": on + off,
        "photos_on": on,
        "photos_off": off,
        "orphan_on": orphan_on,
        "recommend_enable": rec,
        "recommend_count": len(rec),
    }


def stock_map_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把 SQL/store 聚合行收成 ``{pid: {total, enabled, hits}}``。"""
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        pid = str(r.get("persona_id") or r.get("id") or "").strip()
        if not pid:
            continue
        out[pid] = {
            "total": int(r.get("total") or 0),
            "enabled": int(r.get("enabled") or 0),
            "hits": int(r.get("hits") or r.get("total_hits") or 0),
            "sends": int(r.get("sends") or 0),
        }
    return out


__all__ = [
    "recommend_enable",
    "summarize_audit",
    "stock_map_from_rows",
]
