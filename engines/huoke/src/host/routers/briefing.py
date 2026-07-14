# -*- coding: utf-8 -*-
"""每日简报 API — 汇总昨日数据和今日待办。"""

import logging
import time
from datetime import datetime, timedelta
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["briefing"])


@router.get("/briefing/daily")
def daily_briefing():
    """获取每日简报：昨日成果 + 今日待办 + 设备状态概览。"""
    now = time.time()
    today_start = now - (now % 86400)  # UTC 今日 0 点
    yesterday_start = today_start - 86400

    result = {
        "generated_at": datetime.utcnow().isoformat(),
        "yesterday": _yesterday_summary(yesterday_start, today_start),
        "today": _today_plan(today_start, now),
        "devices": _device_overview(),
        "alerts": _recent_alerts(),
    }
    return result


def _yesterday_summary(start, end):
    """昨日任务汇总。"""
    try:
        from ..api import task_store
        all_tasks = task_store.list_tasks(limit=500)
        yesterday = [t for t in all_tasks
                     if t.get("created_at") and start <= t["created_at"] < end]

        success = sum(1 for t in yesterday if t.get("status") == "completed")
        failed = sum(1 for t in yesterday if t.get("status") == "failed")

        # 按类型统计
        type_stats = {}
        for t in yesterday:
            tt = t.get("type", "unknown")
            type_stats[tt] = type_stats.get(tt, 0) + 1

        return {
            "total_tasks": len(yesterday),
            "success": success,
            "failed": failed,
            "success_rate": round(success / max(len(yesterday), 1) * 100, 1),
            "by_type": type_stats,
        }
    except Exception as e:
        logger.debug("昨日汇总失败: %s", e)
        return {"total_tasks": 0, "success": 0, "failed": 0, "success_rate": 0, "by_type": {}}


def _today_plan(today_start, now):
    """今日待办任务。"""
    try:
        from ..api import task_store
        all_tasks = task_store.list_tasks(limit=200)
        running = [t for t in all_tasks if t.get("status") == "running"]
        pending = [t for t in all_tasks if t.get("status") == "pending"]

        # 检查定时任务
        scheduled = []
        try:
            from ..api import _load_scheduled_jobs
            jobs = _load_scheduled_jobs()
            for j in jobs:
                if j.get("enabled"):
                    scheduled.append({
                        "name": j.get("name", ""),
                        "cron": j.get("cron", ""),
                        "next_run": j.get("next_run", ""),
                    })
        except Exception:
            pass

        return {
            "running_tasks": len(running),
            "pending_tasks": len(pending),
            "scheduled_jobs": scheduled[:10],
        }
    except Exception as e:
        logger.debug("今日计划失败: %s", e)
        return {"running_tasks": 0, "pending_tasks": 0, "scheduled_jobs": []}


def _device_overview():
    """设备状态概览。"""
    try:
        from ..health_monitor import metrics
        online = sum(1 for s in metrics.device_status.values()
                     if s.get("status") in ("connected", "online"))
        total = len(metrics.device_status)

        low_battery = []
        for did, s in metrics.device_status.items():
            bat = s.get("battery", 100)
            if isinstance(bat, (int, float)) and bat < 20:
                low_battery.append({"device_id": did, "battery": bat})

        return {
            "online": online,
            "total": total,
            "offline": total - online,
            "low_battery": low_battery,
        }
    except Exception as e:
        logger.debug("设备概览失败: %s", e)
        return {"online": 0, "total": 0, "offline": 0, "low_battery": []}


def _recent_alerts():
    """最近告警。"""
    try:
        from ..health_monitor import metrics
        alerts = getattr(metrics, "recent_alerts", [])
        return alerts[-10:] if alerts else []
    except Exception:
        return []
