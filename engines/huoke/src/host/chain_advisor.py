# -*- coding: utf-8 -*-
"""链式任务智能推荐 — 2026-05-09 P4-1。

基于最近 7 天各设备各任务类型的成功率、平均耗时，为用户推荐最适合
当前设备群的链模板和最优参数。

数据源: tasks 表 (SQLite)
算法:
  1. 按 device_id × task_type 聚合 success/fail/avg_duration
  2. 对每条可用链，计算「预期成功率」= 所有步骤成功率的乘积
  3. 按预期成功率降序排序推荐
  4. 对每步推荐参数调整（如耗时偏高则增加 timeout）
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 7


def _get_task_type_stats(platform: str = "",
                         device_ids: Optional[List[str]] = None,
                         ) -> Dict[str, Dict[str, Any]]:
    """查询近 N 天各 task_type 的聚合统计。

    返回 {task_type: {total, completed, failed, avg_duration, success_rate}}
    """
    from .database import get_conn

    cutoff = time.time() - _LOOKBACK_DAYS * 86400
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))

    sql = """
        SELECT type,
               COUNT(*) as total,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
               AVG(
                 CASE WHEN status='completed' AND updated_at > created_at
                 THEN (julianday(updated_at) - julianday(created_at)) * 86400
                 END
               ) as avg_dur
        FROM tasks
        WHERE (deleted_at IS NULL OR deleted_at = '')
          AND created_at >= ?
    """
    params: list = [cutoff_iso]

    if platform:
        sql += " AND type LIKE ? || '_%'"
        params.append(platform)

    if device_ids:
        placeholders = ",".join("?" * len(device_ids))
        sql += f" AND device_id IN ({placeholders})"
        params.extend(device_ids)

    sql += " GROUP BY type HAVING total >= 2"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    result = {}
    for r in rows:
        total = r["total"]
        completed = r["completed"] or 0
        rate = round(completed / total * 100, 1) if total > 0 else 0
        result[r["type"]] = {
            "total": total,
            "completed": completed,
            "failed": r["failed"] or 0,
            "avg_duration_sec": round(r["avg_dur"] or 0, 1),
            "success_rate": rate,
        }
    return result


def _get_device_task_stats(device_id: str) -> Dict[str, Dict[str, Any]]:
    """单设备维度的 task_type 成功率。"""
    return _get_task_type_stats(device_ids=[device_id])


def _get_healthy_device_ids(device_ids: Optional[List[str]] = None,
                            min_score: int = 40) -> List[str]:
    """P5-3: 过滤掉低健康评分或已隔离的设备。"""
    try:
        from .health_monitor import DeviceHealthMetrics
        metrics = DeviceHealthMetrics.get()
        if not metrics:
            return device_ids or []
        if device_ids:
            return [d for d in device_ids
                    if not metrics.is_isolated(d)
                    and metrics.get_score(d) >= min_score]
        # 无指定时返回所有健康设备
        return [d for d, score in metrics.get_all_scores().items()
                if score >= min_score and not metrics.is_isolated(d)]
    except Exception:
        return device_ids or []


def recommend_chains(platform: str,
                     device_ids: Optional[List[str]] = None,
                     ) -> List[Dict[str, Any]]:
    """为指定平台推荐链模板，按预期成功率降序排列。

    返回: [{chain_id, name, expected_success_rate, steps_analysis, param_hints,
            healthy_devices}]
    """
    from .task_chain import list_chains

    chains = list_chains()
    platform_chains = [c for c in chains if c.get("platform") == platform]
    if not platform_chains:
        return []

    # P5-3: 健康评分过滤
    healthy = _get_healthy_device_ids(device_ids)

    stats = _get_task_type_stats(platform=platform, device_ids=device_ids)

    recommendations = []
    for chain in platform_chains:
        steps = chain.get("steps") or []
        if not steps:
            continue

        step_analysis = []
        expected_rate = 1.0

        for i, step in enumerate(steps):
            tt = step.get("task_type", "") if isinstance(step, dict) else step
            on_fail = step.get("on_fail", "skip") if isinstance(step, dict) else "skip"
            st = stats.get(tt, {})
            rate = st.get("success_rate", 50)  # 无数据默认 50%
            avg_dur = st.get("avg_duration_sec", 0)
            total = st.get("total", 0)

            # on_fail=skip 不影响链整体成功率
            if on_fail == "abort":
                expected_rate *= (rate / 100)

            hints = []
            if rate < 60 and total >= 3:
                hints.append(f"成功率偏低({rate}%), 建议检查参数或跳过")
            if avg_dur > 600 and total >= 2:
                hints.append(f"平均耗时{round(avg_dur/60,1)}分钟, 较慢")
            if total == 0:
                hints.append("无历史数据, 建议先单独测试")

            step_analysis.append({
                "step": i + 1,
                "task_type": tt,
                "on_fail": on_fail,
                "success_rate": rate,
                "avg_duration_sec": avg_dur,
                "sample_count": total,
                "hints": hints,
            })

        param_hints = _generate_param_hints(step_analysis)

        recommendations.append({
            "chain_id": chain["chain_id"],
            "name": chain.get("name", chain["chain_id"]),
            "description": chain.get("description", ""),
            "expected_success_rate": round(expected_rate * 100, 1),
            "steps_analysis": step_analysis,
            "param_hints": param_hints,
            "total_steps": len(steps),
            "healthy_devices": len(healthy),
        })

    # 按预期成功率降序
    recommendations.sort(key=lambda x: x["expected_success_rate"], reverse=True)
    return recommendations


def _generate_param_hints(steps_analysis: List[dict]) -> List[str]:
    """基于步骤分析，生成参数优化建议。"""
    hints = []

    low_rate_steps = [s for s in steps_analysis
                      if s["success_rate"] < 50 and s["sample_count"] >= 3]
    if low_rate_steps:
        names = ", ".join(s["task_type"].split("_", 1)[-1] for s in low_rate_steps)
        hints.append(f"高风险步骤: {names} — 建议设为 on_fail=skip 或减少参数范围")

    slow_steps = [s for s in steps_analysis if s["avg_duration_sec"] > 900]
    if slow_steps:
        hints.append("存在超过15分钟的步骤, 建议缩短 duration 参数")

    no_data = [s for s in steps_analysis if s["sample_count"] == 0]
    if no_data:
        hints.append(f"{len(no_data)}个步骤无历史数据, 首次执行建议单步测试")

    all_rates = [s["success_rate"] for s in steps_analysis if s["sample_count"] >= 2]
    if all_rates and min(all_rates) >= 80:
        hints.append("所有步骤历史表现良好, 推荐直接执行")

    return hints


def get_chain_trend(days: int = 7) -> List[Dict[str, Any]]:
    """P5-4: 返回最近 N 天每天的链执行统计。

    返回: [{date, total, completed, aborted, avg_steps_ok}]
    """
    from .database import get_conn

    cutoff = time.time() - days * 86400
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))

    sql = """
        SELECT DATE(created_at) as day,
               COUNT(*) as total,
               SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
               SUM(CASE WHEN status='aborted' THEN 1 ELSE 0 END) as aborted
        FROM chain_runs
        WHERE created_at >= ?
        GROUP BY DATE(created_at)
        ORDER BY day
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(sql, (cutoff_iso,)).fetchall()
        return [{"date": r["day"], "total": r["total"],
                 "completed": r["completed"] or 0,
                 "aborted": r["aborted"] or 0} for r in rows]
    except Exception:
        return []


def get_heal_effectiveness() -> Dict[str, Any]:
    """P4-2: 统计每种自愈动作的真实恢复效果。

    通过追踪含 _auto_healed_from 的任务的最终状态，
    计算每种 fix_action 的「自愈→最终成功」比例。
    """
    from .database import get_conn

    cutoff = time.time() - _LOOKBACK_DAYS * 86400
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))

    # 找到所有自愈产生的任务（params 含 _auto_healed_from）
    sql = """
        SELECT params, status FROM tasks
        WHERE (deleted_at IS NULL OR deleted_at = '')
          AND created_at >= ?
          AND params LIKE '%_auto_healed_from%'
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (cutoff_iso,)).fetchall()

    action_stats: Dict[str, Dict[str, int]] = {}

    for r in rows:
        try:
            p = r["params"]
            if isinstance(p, str):
                p = json.loads(p)
            action = p.get("_auto_heal_action", "unknown")
        except Exception:
            continue

        if action not in action_stats:
            action_stats[action] = {"total": 0, "completed": 0, "failed": 0}
        action_stats[action]["total"] += 1
        if r["status"] == "completed":
            action_stats[action]["completed"] += 1
        elif r["status"] == "failed":
            action_stats[action]["failed"] += 1

    # 计算恢复率
    result = {}
    for action, s in action_stats.items():
        done = s["completed"] + s["failed"]
        rate = round(s["completed"] / done * 100, 1) if done > 0 else 0
        result[action] = {
            **s,
            "recovery_rate": rate,
            "pending": s["total"] - done,
        }

    return {
        "lookback_days": _LOOKBACK_DAYS,
        "actions": result,
        "total_healed_tasks": sum(s["total"] for s in action_stats.values()),
    }
