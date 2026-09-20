# -*- coding: utf-8 -*-
"""对练台（Duel Bench）状态聚合——给 API / CLI / 看板共用（与 ``gpu_watermark`` 同族：
纯读、绝不抛、缺数据即空态，专供 ops 卡消费）。

产物由两个计划任务写，各答一个不同的问题（分开看才有意义）：
- ``DuelNightly`` → ``logs/duel/latest_summary.json``（覆盖式当晚汇总）+
  ``LAST_RUN.json``（覆盖式「上一晚是否超标」）+ ``duel_trend.jsonl``（历史，
  归一到 ``defects_per_100_turns`` 才能跨夜比较——夜跑轮数会调）：**产品**变好还是变差。
- ``DuelSemanticWeekly`` → ``logs/eval/duel_semantic_trend.jsonl``：金标固定，
  掉了就是模型/提示词漂移，即**裁判**自己还准不准。

读盘根目录＝引擎代码根（夜跑脚本已钉死同一棵树），不要用实例 data 根去推。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "read_trend",
    "trend_direction",
    "load_json_file",
    "build_status",
    "DEFAULT_ROOT",
]

DEFAULT_ROOT = Path(__file__).resolve().parents[2]  # engines/chengjie


def load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """读单个 JSON 对象；缺/坏 → None。"""
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def read_trend(path: Path, *, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """读趋势 JSONL（缺文件 → []；坏行跳过）。"""
    rows: List[Dict[str, Any]] = []
    try:
        if not path.is_file():
            return []
        cutoff = (datetime.now() - timedelta(days=days)) if days else None
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if cutoff:
                try:
                    if datetime.fromisoformat(str(row.get("ts"))) < cutoff:
                        continue
                except (TypeError, ValueError):
                    pass
            rows.append(row)
    except Exception:  # noqa: BLE001
        return rows
    return rows


def trend_direction(rows: Sequence[Dict[str, Any]],
                    key: str = "defects_per_100_turns") -> str:
    """两条及以上点 → ``better`` / ``worse`` / ``flat``；否则 ``unknown``。"""
    if len(rows) < 2:
        return "unknown"
    try:
        d0 = float(rows[0].get(key) or 0)
        d1 = float(rows[-1].get(key) or 0)
    except (TypeError, ValueError):
        return "unknown"
    if abs(d1 - d0) < 0.01:
        return "flat"
    return "better" if d1 < d0 else "worse"


def build_status(
    root: Optional[Path] = None,
    *,
    days: int = 14,
) -> Dict[str, Any]:
    """聚合夜跑最新摘要 + 两条趋势线，供 ``GET /api/admin/duel-bench``。"""
    base = Path(root) if root else DEFAULT_ROOT
    summary_path = base / "logs" / "duel" / "latest_summary.json"
    last_run_path = base / "logs" / "duel" / "LAST_RUN.json"
    nightly_path = base / "logs" / "duel" / "duel_trend.jsonl"
    semantic_path = base / "logs" / "eval" / "duel_semantic_trend.jsonl"

    summary = load_json_file(summary_path)
    last_run = load_json_file(last_run_path)
    nightly = read_trend(nightly_path, days=days or None)
    semantic_all = read_trend(semantic_path, days=days or None)
    semantic_llm = [r for r in semantic_all if r.get("mode") == "llm"]

    over = (summary or {}).get("over_budget") or {}
    has_data = bool(summary or last_run or nightly or semantic_all)
    return {
        "ok": True,
        "active": has_data,
        "latest": summary,
        "last_run": last_run,
        "over_budget": bool(over) or bool(
            (last_run or {}).get("exit_code") == 1),
        "nightly": {
            "rows": nightly,
            "direction": trend_direction(nightly),
            "points": len(nightly),
        },
        "semantic": {
            "rows": semantic_llm,
            "points": len(semantic_llm),
            "last_passed": (semantic_llm[-1].get("passed")
                            if semantic_llm else None),
        },
        "paths": {
            "summary": str(summary_path),
            "last_run": str(last_run_path),
            "nightly_trend": str(nightly_path),
            "semantic_trend": str(semantic_path),
        },
    }
