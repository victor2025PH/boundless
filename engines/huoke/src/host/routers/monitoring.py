# -*- coding: utf-8 -*-
"""监控、健康检查、告警、可观测性、事件流路由。"""

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from src.utils.subprocess_text import run as _sp_run_text
from src.host.device_registry import DEFAULT_DEVICES_YAML, config_file, data_file

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["monitoring"])


# ── auth dependency (lazy to avoid circular deps) ──


async def _verify_api_key(request: Request,
                          key: Optional[str] = Security(
                              APIKeyHeader(name="X-API-Key", auto_error=False))):
    from ..api import verify_api_key
    await verify_api_key(request, key)


_auth = [Depends(_verify_api_key)]


# ── helpers (lazy imports to avoid circular deps) ──


def _get_metrics():
    from ..health_monitor import metrics
    return metrics


def _get_config_path():
    return DEFAULT_DEVICES_YAML


def _get_device_manager():
    from src.device_control.device_manager import get_device_manager
    return get_device_manager(_get_config_path())


def _get_worker_pool():
    from ..worker_pool import get_worker_pool
    return get_worker_pool()


def _audit(action: str, target: str = "", detail: str = "",
           source: str = "api"):
    from ..api import _audit as _api_audit
    _api_audit(action, target, detail, source)


def _load_aliases():
    alias_path = config_file("device_aliases.json")
    if alias_path.exists():
        with open(alias_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ═══════════════════════════════════════════════════════════════════════════
# Metrics / Logs / Health
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/metrics", dependencies=_auth)
def get_metrics():
    """运行时 metrics"""
    return _get_metrics().snapshot()


@router.get("/devices/dispatch-heatmap", dependencies=_auth)
def dispatch_heatmap(hours: int = 24):
    """2026-05-13: 返回最近 N 小时（最多 72h）的任务派发热图。

    数据来源: SmartScheduler._dispatch_buf（内存 ring-buffer，进程重启清零）。
    每小时一个桶，按 device:task_type 分组，包含 total/success/fail 计数。
    用于可视化设备工作负载分布和识别高故障率设备/任务。
    """
    try:
        from src.host.smart_scheduler import get_smart_scheduler
        scheduler = get_smart_scheduler()
        return scheduler.get_dispatch_heatmap(hours=hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config/audit", dependencies=_auth)
def config_audit_log(limit: int = 100):
    """2026-05-13: 返回最近 N 条配置热重载变更记录（来自 data/config_audit.jsonl）。

    记录包含: ts（UTC）、file、section、new_value。
    当运维参数调整出现问题时，可追溯"谁在何时改了什么"。
    """
    try:
        from src.host.config_audit import read_recent
        entries = read_recent(limit=limit)
        return {"entries": entries, "count": len(entries)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tasks/rate-status", dependencies=_auth)
def task_rate_status():
    """2026-05-13: 返回各 device:task_type 的滑动窗口速率限制当前使用情况。

    每条记录包含: used (窗口内派发次数), limit, window_sec, reset_in_sec (最早时间戳到期)。
    便于运维判断哪些设备/任务类型当前被限流。
    """
    try:
        from src.host.task_dispatcher import (
            _rate_windows, _rate_windows_lock, _load_rate_limits, _rate_cfg_lock
        )
        import time as _time
        with _rate_cfg_lock:
            cfg = _load_rate_limits()

        now = _time.time()
        result = {}
        with _rate_windows_lock:
            for key, win in _rate_windows.items():
                if not win:
                    continue
                parts = key.split(":", 1)
                if len(parts) != 2:
                    continue
                device_id, task_type = parts
                type_cfg = cfg.get(task_type) or {}
                limit = int(type_cfg.get("per_device_per_hour", 0))
                window_sec = float(type_cfg.get("window_hours", 1.0)) * 3600
                cutoff = now - window_sec
                # 统计窗口内有效条目
                valid = [ts for ts in win if ts >= cutoff]
                if not valid:
                    continue
                result[key] = {
                    "device_id": device_id,
                    "task_type": task_type,
                    "used": len(valid),
                    "limit": limit,
                    "window_sec": int(window_sec),
                    "reset_in_sec": max(0, int(valid[0] + window_sec - now)),
                    "rate_limited": len(valid) >= limit,
                }
        ranked = sorted(result.values(),
                        key=lambda x: (-int(x["rate_limited"]), -x["used"]))
        return {"rate_windows": ranked, "count": len(ranked)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tasks/rate-bypass", dependencies=_auth)
def set_task_rate_bypass(task_type: str, minutes: int = 60, device_id: str = ""):
    """2026-05-13: 为指定任务类型设置临时速率限制豁免。

    Args:
        task_type:  任务类型（如 facebook_add_friend）
        minutes:    豁免时长（分钟，最大 1440）
        device_id:  不填则豁免所有设备，填写则只豁免指定设备

    豁免期间 dispatch_after_create 跳过速率限制检查。豁免到期后自动恢复。
    """
    try:
        from src.host.task_dispatcher import set_rate_bypass
        return set_rate_bypass(task_type=task_type, minutes=minutes, device_id=device_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system/circuit-status", dependencies=_auth)
def circuit_status():
    """2026-05-13: 汇总所有熔断相关状态到单一端点（运维仪表盘一次拉取）。

    包含：LLM CB 状态（主/fallback）、活跃任务暂停、设备级熔断、
    调度器连续失败计数、系统整体 LLM 可用性标志。
    """
    result: dict = {}

    # 1. LLM 断路器状态
    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        primary_cb = client.cb_status()
        primary_cb["provider"] = client.config.provider
        result["primary_cb"] = primary_cb
        fb = client._get_fallback_client()
        if fb is not None:
            fb_cb = fb.cb_status()
            fb_cb["provider"] = fb.config.provider
            result["fallback_cb"] = fb_cb
        primary_open = primary_cb.get("state") == "open"
        fallback_open = fb is None or result.get("fallback_cb", {}).get("state") == "open"
        result["llm_available"] = not (primary_open and fallback_open)
    except Exception as e:
        result["primary_cb"] = {"error": str(e)}

    # 2. 活跃任务/设备暂停
    try:
        from src.host.task_dispatcher import get_suspensions
        suspensions = get_suspensions()
        result["suspensions"] = suspensions
        result["suspension_count"] = len(suspensions)
    except Exception as e:
        result["suspensions"] = {"error": str(e)}

    # 3. 调度器连续失败计数
    try:
        from src.host.smart_scheduler import get_smart_scheduler
        sched = get_smart_scheduler()
        with sched._lock:
            fail_streaks = {k: v for k, v in sched._consecutive_fails.items() if v > 0}
            device_fails = {k: v for k, v in sched._device_fail_count.items() if v > 0}
        result["task_fail_streaks"] = fail_streaks
        result["device_fail_counts"] = device_fails
    except Exception as e:
        result["task_fail_streaks"] = {"error": str(e)}

    return result


@router.get("/tasks/suspensions", dependencies=_auth)
def list_task_suspensions():
    """2026-05-13: 查询当前所有自动暂停条目（连续失败触发的保护性熔断）。"""
    try:
        from src.host.task_dispatcher import get_suspensions
        items = get_suspensions()
        return {"suspensions": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/devices/suspend", dependencies=_auth)
def suspend_device_endpoint(device_id: str, minutes: int = 60):
    """2026-05-13: 暂停某设备的所有任务派发（设备级熔断，跨任务类型）。

    适用于设备整体异常时的保护性操作。minutes=0 立即解除。
    """
    try:
        from src.host.task_dispatcher import suspend_device
        return suspend_device(device_id=device_id, minutes=minutes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tasks/suspensions/clear", dependencies=_auth)
def clear_task_suspension(task_type: str, device_id: str = ""):
    """2026-05-13: 手动解除某个自动暂停（运维确认恢复后调用）。"""
    try:
        from src.host.task_dispatcher import suspend_task_type
        result = suspend_task_type(task_type, device_id=device_id, minutes=0)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tasks/rate-bypasses", dependencies=_auth)
def list_task_rate_bypasses():
    """2026-05-13: 返回当前所有有效的速率限制豁免条目。"""
    try:
        from src.host.task_dispatcher import get_rate_bypasses
        bypasses = get_rate_bypasses()
        return {"bypasses": bypasses, "count": len(bypasses)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/metrics/reset", dependencies=_auth)
def reset_metrics(confirm: str = ""):
    """2026-05-13: 重置内存 metrics 计数器 + 持久化快照。

    必须传 ?confirm=yes 才执行，防止误操作。
    仅重置任务计数和 per-type 统计，不影响设备历史和健康评分。
    同时清空 task_dispatcher 的告警状态，避免遗留过时告警。
    """
    if confirm != "yes":
        raise HTTPException(status_code=400,
                            detail="请传 ?confirm=yes 确认重置操作")
    m = _get_metrics()
    with m._lock:
        m.tasks_total = 0
        m.tasks_success = 0
        m.tasks_failed = 0
        m.tasks_timeout = 0
        m._task_type_stats.clear()
    # 同步清零持久化快照
    m.save_snapshot()
    # 清空告警状态，避免重置后立刻重新触发历史告警
    try:
        from src.host.task_dispatcher import _ALERT_STATE
        _ALERT_STATE.clear()
    except Exception:
        pass
    logger.info("[metrics] 指标已手动重置")
    return {"ok": True, "message": "任务计数和 per-type 统计已清零"}


@router.get("/logs")
def get_logs(limit: int = 100, level: str = ""):
    """Return recent log entries from in-memory ring buffer."""
    from src.utils.log_config import RingBufferHandler
    handler = RingBufferHandler.get_instance()
    if not handler:
        return {"logs": [], "count": 0}
    raw = handler.get_entries(limit=min(limit, 500), level=level)
    entries = []
    for item in raw:
        try:
            entries.append(json.loads(item))
        except Exception:
            entries.append({"msg": item})
    return {"logs": entries, "count": len(entries)}


@router.get("/health")
def health():
    import os

    metrics = _get_metrics()
    pool = _get_worker_pool()
    snap = metrics.snapshot()
    devices_online = sum(
        1 for d in snap.get("devices", {}).values()
        if d.get("status") == "connected"
    )
    devices_total = len(snap.get("devices", {}))
    recent_alerts = snap.get("recent_alerts", [])
    critical_alerts = [a for a in recent_alerts if a.get("level") == "critical"]

    status = "ok"
    if critical_alerts:
        status = "degraded"
    if devices_online == 0 and devices_total > 0:
        status = "unhealthy"

    build_id = (os.environ.get("OPENCLAW_BUILD_ID") or "").strip() or None

    return {
        "status": status,
        "version": "1.2.0",
        "build_id": build_id,
        "capabilities": {
            "post_batch_install_apk": True,
            "post_batch_install_apk_cluster": True,
            "post_cluster_batch_install_apk": True,
            # Facebook：POST /tasks 在入队前执行 add_friend / campaign gate（与 executor 同源）
            "facebook_task_precreate_gate": True,
        },
        "pool_running": pool._running,
        "uptime_seconds": snap["uptime_seconds"],
        "devices_online": devices_online,
        "devices_total": devices_total,
        "device_reconnects": snap.get("device_reconnects", 0),
        "last_heartbeat": snap.get("last_heartbeat"),
        "tasks": snap.get("tasks", {}),
        # Facebook launch 累计计数（routers/facebook.fb_device_launch → health_monitor.metrics）
        "facebook_launch": snap.get("facebook_launch", {}),
        "tiktok_launch": snap.get("tiktok_launch", {}),
        "tiktok_campaign_launch": snap.get("tiktok_campaign_launch", {}),
        "recent_alerts": recent_alerts[-5:],
    }


@router.get("/health/alerts", dependencies=_auth)
def health_alerts(level: str = "", limit: int = 50):
    """Get device health alerts. Filter by level: info, warning, error, critical."""
    snap = _get_metrics().snapshot()
    alerts = snap.get("recent_alerts", [])
    if level:
        alerts = [a for a in alerts if a.get("level") == level]
    return {"alerts": alerts[-limit:], "total": len(alerts)}


# ═══════════════════════════════════════════════════════════════════════════
# Health Report
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/health-report", dependencies=_auth)
def generate_health_report():
    """Generate a comprehensive device health report."""
    manager = _get_device_manager()
    devices = manager.get_all_devices()
    aliases = _load_aliases()
    online = [d for d in devices if d.is_online]
    offline = [d for d in devices if not d.is_online]
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total_devices": len(devices),
            "online": len(online),
            "offline": len(offline),
            "offline_devices": [aliases.get(d.device_id, {}).get("alias", d.device_id[:8]) for d in offline],
        },
        "performance": {},
        "alerts": [],
    }
    import re
    from concurrent.futures import ThreadPoolExecutor

    def _check(did):
        data = {}
        try:
            bat = _sp_run_text(["adb", "-s", did, "shell", "dumpsys", "battery"],
                               capture_output=True, timeout=5)
            level_m = re.search(r'level:\s*(\d+)', bat.stdout)
            temp_m = re.search(r'temperature:\s*(\d+)', bat.stdout)
            if level_m:
                data["battery"] = int(level_m.group(1))
            if temp_m:
                data["temp"] = round(int(temp_m.group(1)) / 10, 1)
        except Exception as e:
            logger.debug("health report: 获取电池信息失败 %s: %s", did, e)
        try:
            mem = _sp_run_text(["adb", "-s", did, "shell", "cat", "/proc/meminfo"],
                               capture_output=True, timeout=5)
            total = re.search(r'MemTotal:\s+(\d+)', mem.stdout)
            free = re.search(r'MemAvailable:\s+(\d+)', mem.stdout)
            if total and free:
                t, f = int(total.group(1)), int(free.group(1))
                data["mem_pct"] = round((t - f) / t * 100, 1)
        except Exception as e:
            logger.debug("health report: 获取内存信息失败 %s: %s", did, e)
        try:
            df = _sp_run_text(["adb", "-s", did, "shell", "df", "/data"],
                              capture_output=True, timeout=5)
            lines = df.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit():
                    data["storage_pct"] = round(int(parts[2]) / int(parts[1]) * 100, 1)
        except Exception as e:
            logger.debug("health report: 获取存储信息失败 %s: %s", did, e)
        return data

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_check, d.device_id): d for d in online}
        for fut in futs:
            dev = futs[fut]
            alias = aliases.get(dev.device_id, {}).get("alias", dev.device_id[:8])
            data = fut.result()
            report["performance"][alias] = data
            if data.get("battery", 100) < 20:
                report["alerts"].append(f"{alias}: 低电量 {data['battery']}%")
            if data.get("temp", 0) > 42:
                report["alerts"].append(f"{alias}: 高温 {data['temp']}°C")
            if data.get("mem_pct", 0) > 85:
                report["alerts"].append(f"{alias}: 高内存 {data['mem_pct']}%")
            if data.get("storage_pct", 0) > 90:
                report["alerts"].append(f"{alias}: 存储即将满 {data['storage_pct']}%")
    return report


# ═══════════════════════════════════════════════════════════════════════════
# SSE Event Stream
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/events/stream")
async def sse_stream():
    """Server-Sent Events endpoint for real-time dashboard updates."""
    import asyncio
    from fastapi.responses import StreamingResponse
    from ..event_stream import EventStreamHub

    hub = EventStreamHub.get()
    queue = hub.subscribe()

    async def _generate():
        try:
            yield f"data: {json.dumps({'type':'connected','ts':time.strftime('%H:%M:%S')})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"id: {event.get('id','')}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(_generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/events/hub-snapshot")
def hub_snapshot(since_id: int = 0, limit: int = 50):
    """返回 EventStreamHub 中 id > since_id 的最近事件（供集群内轮询，无需鉴权）。"""
    from ..event_stream import EventStreamHub
    hub = EventStreamHub.get()
    all_recent = hub.get_recent(200)
    filtered = [e for e in all_recent if (e.get("id") or 0) > since_id]
    trimmed = filtered[-limit:]
    max_id = max((e.get("id", 0) for e in trimmed), default=since_id)
    return {"events": trimmed, "max_id": max_id, "total": len(filtered)}


@router.get("/events/recent", dependencies=_auth)
def recent_events(limit: int = 50, pattern: str = ""):
    """Get recent events from the event bus."""
    from src.workflow.event_bus import get_event_bus
    bus = get_event_bus()
    return bus.recent_events(limit, pattern)


@router.get("/events/subscriptions", dependencies=_auth)
def event_subscriptions():
    """List active event subscriptions."""
    from src.workflow.event_bus import get_event_bus
    bus = get_event_bus()
    return bus.active_subscriptions()


@router.post("/events/emit", dependencies=_auth)
def events_emit(body: dict):
    """Emit a custom event."""
    from src.workflow.event_bus import get_event_bus, Event
    event_type = body.get("type", "")
    if not event_type:
        raise HTTPException(status_code=400, detail="event type is required")
    get_event_bus().emit(Event(
        type=event_type,
        data=body.get("data", {}),
        source=body.get("source", "api"),
    ))
    return {"ok": True, "type": event_type}


# ═══════════════════════════════════════════════════════════════════════════
# Observability
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/observability/metrics", dependencies=_auth)
def obs_metrics():
    """Metrics snapshot (counters, gauges, histograms)."""
    from src.observability.metrics import get_metrics_collector
    return get_metrics_collector().snapshot()


@router.get("/observability/prometheus", dependencies=_auth)
def obs_prometheus():
    """Prometheus text exposition format."""
    from src.observability.metrics import get_metrics_collector
    from src.host.health_monitor import health_monitor_launch_prometheus_text
    from fastapi.responses import PlainTextResponse

    body = (get_metrics_collector().prometheus()
            + health_monitor_launch_prometheus_text())
    # P16 (2026-04-24): 追加 Level 4 VLM fallback 状态 (P5b swap 状态 + budget
    # + last_error_code + swap_events counter)。fail-safe: 读 app_automation
    # 失败不应让整个 /observability/prometheus 挂。
    try:
        from src.app_automation.facebook import vlm_level4_prometheus_text
        body += vlm_level4_prometheus_text()
    except Exception:  # pragma: no cover — fail-safe defensive
        pass
    return PlainTextResponse(
        content=body,
        media_type="text/plain; version=0.0.4",
    )


@router.get("/observability/logs", dependencies=_auth)
def obs_logs(date: Optional[str] = None, level: Optional[str] = None,
             limit: int = 100, contains: str = ""):
    """Query structured logs."""
    from src.observability.structured_log import get_structured_logger
    slog = get_structured_logger()
    return slog.query_logs(date, level, limit, contains)


@router.get("/observability/logs/files", dependencies=_auth)
def obs_log_files():
    """List available log files."""
    from src.observability.structured_log import get_structured_logger
    return get_structured_logger().log_files()


@router.get("/observability/executions", dependencies=_auth)
def obs_executions(workflow: str = "", limit: int = 50, offset: int = 0):
    """List workflow execution history."""
    from src.observability.execution_store import get_execution_store
    return get_execution_store().list_runs(workflow, limit, offset)


@router.get("/observability/executions/{run_id}", dependencies=_auth)
def obs_execution_detail(run_id: str):
    """Get detailed execution with per-step results."""
    from src.observability.execution_store import get_execution_store
    detail = get_execution_store().get_run(run_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Run not found")
    return detail


@router.get("/observability/executions/stats/summary", dependencies=_auth)
def obs_execution_stats():
    """Aggregate execution statistics."""
    from src.observability.execution_store import get_execution_store
    return get_execution_store().get_stats()


@router.get("/observability/alerts", dependencies=_auth)
def obs_alerts():
    """Active alerts + alert rules."""
    from src.observability.alerting import get_alert_manager
    am = get_alert_manager()
    return {
        "active": am.get_active_alerts(),
        "rules": am.get_rules(),
    }


@router.get("/observability/alerts/history", dependencies=_auth)
def obs_alert_history(limit: int = 50):
    """Alert firing history."""
    from src.observability.alerting import get_alert_manager
    return get_alert_manager().get_alert_history(limit)


@router.post("/observability/alerts/evaluate", dependencies=_auth)
def obs_alert_evaluate():
    """Manually trigger alert evaluation."""
    from src.observability.alerting import get_alert_manager
    from src.observability.metrics import get_metrics_collector
    am = get_alert_manager()
    mc = get_metrics_collector()
    events = am.evaluate(mc)
    # Push critical/error events to Telegram
    for ev in events:
        try:
            ev_dict = ev.to_dict()
            sev = ev_dict.get("severity", "warning")
            if sev in ("critical", "error"):
                from ..alert_notifier import AlertNotifier
                AlertNotifier.get().notify_event(
                    "obs.alert",
                    f"告警触发: {ev_dict.get('rule', '')}",
                    ev_dict.get("description", ""),
                    level=sev,
                )
        except Exception:
            pass
    return {"evaluated": len(am.get_rules()), "fired": len(events),
            "events": [e.to_dict() for e in events]}


@router.post("/observability/alerts/rules", dependencies=_auth)
def obs_create_rule(body: dict):
    """Create a declarative alert rule from JSON config.

    Body: {name, description, metric, operator, threshold, severity, cooldown_sec}
    metric choices: devices_offline, error_rate, tasks_failed, tasks_pending,
                    health_score_min, vpn_down_count
    operator choices: >, <, >=, <=, ==
    """
    from src.observability.alerting import get_alert_manager, AlertRule, AlertSeverity
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name required")
    metric = body.get("metric", "")
    op = body.get("operator", ">")
    threshold = float(body.get("threshold", 0))
    sev_str = body.get("severity", "warning")
    cooldown = float(body.get("cooldown_sec", 300))
    desc = body.get("description", f"{metric} {op} {threshold}")

    def _make_check(m, o, t):
        def check(mc):
            val = _resolve_alert_metric(m)
            if o == ">": return val > t
            if o == "<": return val < t
            if o == ">=": return val >= t
            if o == "<=": return val <= t
            if o == "==": return val == t
            return False
        return check

    sev_map = {"info": AlertSeverity.INFO, "warning": AlertSeverity.WARNING,
               "critical": AlertSeverity.CRITICAL}
    rule = AlertRule(
        name=name, description=desc,
        check=_make_check(metric, op, threshold),
        severity=sev_map.get(sev_str, AlertSeverity.WARNING),
        cooldown_sec=cooldown,
    )
    am = get_alert_manager()
    am.add_rule(rule)

    _custom_alert_rules[name] = {
        "name": name, "description": desc, "metric": metric,
        "operator": op, "threshold": threshold, "severity": sev_str,
        "cooldown_sec": cooldown,
    }
    _save_custom_alert_rules()
    _audit("create_alert_rule", name, f"{metric} {op} {threshold}")
    return {"ok": True, "rule": _custom_alert_rules[name]}


@router.delete("/observability/alerts/rules/{name}", dependencies=_auth)
def obs_delete_rule(name: str):
    """Delete an alert rule by name."""
    from src.observability.alerting import get_alert_manager
    am = get_alert_manager()
    removed = am.remove_rule(name)
    _custom_alert_rules.pop(name, None)
    _save_custom_alert_rules()
    _audit("delete_alert_rule", name)
    return {"ok": removed, "name": name}


@router.get("/observability/alerts/rules/custom", dependencies=_auth)
def obs_list_custom_rules():
    """List user-defined alert rules (serializable config)."""
    return {"rules": list(_custom_alert_rules.values())}


# ── Observability alert helpers ──


def _resolve_alert_metric(metric: str) -> float:
    """Resolve a named metric to its current value."""
    metrics = _get_metrics()
    snap = metrics.snapshot()
    devs = snap.get("devices", {})
    if metric == "devices_offline":
        return sum(1 for d in devs.values()
                   if d.get("status") != "connected")
    if metric == "error_rate":
        tasks = snap.get("tasks", {})
        total = tasks.get("total", 0)
        failed = tasks.get("failed", 0)
        return (failed / max(1, total)) * 100
    if metric == "tasks_failed":
        return snap.get("tasks", {}).get("failed", 0)
    if metric == "tasks_pending":
        return snap.get("tasks", {}).get("pending", 0)
    if metric == "health_score_min":
        scores = metrics.all_health_scores()
        return min(scores.values()) if scores else 100
    if metric == "vpn_down_count":
        return sum(1 for d in devs.values()
                   if d.get("vpn_status") == "down")
    return 0


_custom_alert_rules: dict = {}


def _save_custom_alert_rules():
    """Persist custom rules to JSON."""
    path = config_file("alert_rules.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(_custom_alert_rules.values()), f,
                      ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug("保存告警规则失败: %s", e)


def _load_custom_alert_rules():
    """Load custom alert rules on startup."""
    path = config_file("alert_rules.json")
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            rules = json.load(f)
        from src.observability.alerting import (
            get_alert_manager, AlertRule, AlertSeverity)
        am = get_alert_manager()
        sev_map = {"info": AlertSeverity.INFO,
                   "warning": AlertSeverity.WARNING,
                   "critical": AlertSeverity.CRITICAL}
        for r in rules:
            name = r.get("name", "")
            if not name:
                continue
            m, o, t = r["metric"], r["operator"], float(r["threshold"])

            def _mk(m_, o_, t_):
                def check(mc):
                    val = _resolve_alert_metric(m_)
                    if o_ == ">": return val > t_
                    if o_ == "<": return val < t_
                    if o_ == ">=": return val >= t_
                    if o_ == "<=": return val <= t_
                    if o_ == "==": return val == t_
                    return False
                return check

            rule = AlertRule(
                name=name, description=r.get("description", ""),
                check=_mk(m, o, t),
                severity=sev_map.get(r.get("severity"), AlertSeverity.WARNING),
                cooldown_sec=float(r.get("cooldown_sec", 300)),
            )
            am.add_rule(rule)
            _custom_alert_rules[name] = r
        logger.info("已加载 %d 条自定义告警规则", len(rules))
    except Exception as e:
        logger.debug("加载告警规则失败: %s", e)


_load_custom_alert_rules()


# ═══════════════════════════════════════════════════════════════════════════
# Device Timeline
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/devices/{device_id}/timeline", dependencies=_auth)
def device_timeline(device_id: str, limit: int = 50):
    """Get operation timeline for a device from audit logs."""
    import sqlite3
    db_path = data_file("openclaw.db")
    if not db_path.exists():
        return {"device_id": device_id, "events": []}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_logs WHERE extra LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f'%{device_id}%', limit)
        ).fetchall()
        conn.close()
        events = [dict(r) for r in rows]
        return {"device_id": device_id, "events": events}
    except Exception:
        return {"device_id": device_id, "events": []}


@router.get("/timeline/all", dependencies=_auth)
def all_timeline(limit: int = 100):
    """Get global operation timeline."""
    import sqlite3
    db_path = data_file("openclaw.db")
    if not db_path.exists():
        return {"events": []}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return {"events": [dict(r) for r in rows]}
    except Exception:
        return {"events": []}


# ═══════════════════════════════════════════════════════════════════════════
# Performance Alert Rules (device-level)
# ═══════════════════════════════════════════════════════════════════════════

_alert_rules_path = config_file("alert_rules.json")


def _load_alert_rules() -> list:
    if _alert_rules_path.exists():
        with open(_alert_rules_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return [
        {"id": "low_battery", "name": "低电量告警", "metric": "battery_level",
         "operator": "<", "threshold": 20, "enabled": True,
         "action": "notify", "action_cmd": ""},
        {"id": "high_memory", "name": "高内存告警", "metric": "mem_usage",
         "operator": ">", "threshold": 80, "enabled": True,
         "action": "notify", "action_cmd": ""},
        {"id": "high_temp", "name": "高温告警", "metric": "battery_temp",
         "operator": ">", "threshold": 42.0, "enabled": True,
         "action": "notify_and_cmd", "action_cmd": "am force-stop com.zhiliaoapp.musically"},
        {"id": "critical_battery", "name": "电量严重不足", "metric": "battery_level",
         "operator": "<", "threshold": 5, "enabled": True,
         "action": "notify_and_cmd", "action_cmd": "input keyevent 26"},
    ]


def _save_alert_rules(rules: list):
    _alert_rules_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_alert_rules_path, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


_alert_cooldown: dict = {}
_ALERT_COOLDOWN_SEC = 300


def check_perf_alerts(device_id: str, perf: dict):
    """Check device performance against alert rules, fire if triggered."""
    rules = _load_alert_rules()
    now = time.time()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        metric = rule.get("metric", "")
        val = perf.get(metric)
        if val is None:
            continue
        threshold = rule.get("threshold", 0)
        op = rule.get("operator", ">")
        triggered = False
        if op == ">" and val > threshold:
            triggered = True
        elif op == "<" and val < threshold:
            triggered = True
        elif op == ">=" and val >= threshold:
            triggered = True
        elif op == "<=" and val <= threshold:
            triggered = True

        if not triggered:
            continue

        cooldown_key = f"{device_id}:{rule['id']}"
        if now - _alert_cooldown.get(cooldown_key, 0) < _ALERT_COOLDOWN_SEC:
            continue
        _alert_cooldown[cooldown_key] = now

        aliases = _load_aliases()
        alias = aliases.get(device_id, {}).get("alias", device_id[:8])
        title = f"{rule['name']}: {alias}"
        msg = f"{metric}={val} {op} {threshold}"

        action = rule.get("action", "notify")
        if action in ("notify", "notify_and_cmd"):
            try:
                from ..api import send_notification
                send_notification("perf.alert", title, msg, "warning")
            except Exception:
                pass
            try:
                from ..websocket_hub import get_ws_hub
                get_ws_hub().broadcast("device.alert", {
                    "device_id": device_id, "rule": rule["name"],
                    "metric": metric, "value": val, "threshold": threshold,
                    "level": "warning", "message": title,
                })
            except Exception:
                pass
            try:
                from ..alert_notifier import AlertNotifier
                AlertNotifier.get().notify("warning", device_id, f"{title}: {msg}")
            except Exception:
                pass

        if action in ("notify_and_cmd", "cmd") and rule.get("action_cmd"):
            import threading
            cmd = rule["action_cmd"]

            def _exec():
                try:
                    _sp_run_text(["adb", "-s", device_id, "shell", cmd],
                                 capture_output=True, timeout=15)
                except Exception:
                    pass
            threading.Thread(target=_exec, daemon=True).start()


@router.get("/health/recovery-stats")
def health_recovery_stats():
    """恢复统计和掉线排行榜。"""
    try:
        from ..health_monitor import metrics
        device_stats = getattr(metrics, 'disconnect_counts', {})
        reconnect_counts = getattr(metrics, 'reconnect_counts', {})

        total_disc = sum(device_stats.values())
        total_rec = sum(reconnect_counts.values())

        devices = []
        for did in set(list(device_stats.keys()) + list(reconnect_counts.keys())):
            disc = device_stats.get(did, 0)
            rec = reconnect_counts.get(did, 0)
            if disc > 0 or rec > 0:
                devices.append({
                    "device_id": did,
                    "display_name": did[:8],
                    "disconnects": disc,
                    "recoveries": rec,
                    "recovery_rate": round(rec / max(disc, 1) * 100, 1),
                })

        devices.sort(key=lambda x: x["disconnects"], reverse=True)

        return {
            "total_disconnects": total_disc,
            "total_recoveries": total_rec,
            "overall_rate": round(total_rec / max(total_disc, 1) * 100, 1),
            "devices": devices[:20],
        }
    except Exception as e:
        return {"total_disconnects": 0, "total_recoveries": 0, "overall_rate": 0, "devices": []}


@router.get("/alert-rules", dependencies=_auth)
def get_alert_rules():
    return {"rules": _load_alert_rules()}


@router.post("/alert-rules", dependencies=_auth)
def save_alert_rules_endpoint(body: dict):
    rules = body.get("rules", [])
    _save_alert_rules(rules)
    return {"ok": True}


@router.post("/alert-rules/add", dependencies=_auth)
def add_alert_rule(body: dict):
    import uuid
    rules = _load_alert_rules()
    rule = {
        "id": body.get("id", uuid.uuid4().hex[:8]),
        "name": body.get("name", "自定义规则"),
        "metric": body.get("metric", "battery_level"),
        "operator": body.get("operator", "<"),
        "threshold": body.get("threshold", 20),
        "enabled": body.get("enabled", True),
        "action": body.get("action", "notify"),
        "action_cmd": body.get("action_cmd", ""),
    }
    rules.append(rule)
    _save_alert_rules(rules)
    return {"ok": True, "rule": rule}


@router.delete("/alert-rules/{rule_id}", dependencies=_auth)
def delete_alert_rule(rule_id: str):
    rules = _load_alert_rules()
    rules = [r for r in rules if r.get("id") != rule_id]
    _save_alert_rules(rules)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
# System Metrics Summary
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/metrics/summary", dependencies=_auth)
def metrics_summary():
    """系统综合指标摘要 -- 用于监控面板和告警。"""
    import psutil

    # 系统资源
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    # 进程信息
    proc = psutil.Process()
    proc_mem = proc.memory_info()

    return {
        "timestamp": time.time(),
        "system": {
            "cpu_percent": cpu,
            "memory_used_mb": round(mem.used / 1024 / 1024),
            "memory_total_mb": round(mem.total / 1024 / 1024),
            "memory_percent": mem.percent,
            "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
            "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
            "disk_percent": disk.percent,
        },
        "process": {
            "pid": proc.pid,
            "memory_rss_mb": round(proc_mem.rss / 1024 / 1024, 1),
            "threads": proc.num_threads(),
            "uptime_seconds": round(time.time() - proc.create_time()),
        },
    }


@router.get("/screen-stats")
def screen_stats():
    """屏幕监控页统计栏 — 单请求合并 VPN+任务+健康+TikTok 统计。"""
    result = {"ts": time.time()}

    # 任务统计
    try:
        from src.host import task_store
        tasks = task_store.list_tasks(limit=200)
        running = [t for t in tasks if getattr(t, "status", "") == "running"]
        running_dids = set(getattr(t, "device_id", "") for t in running)
        result["tasks_running"] = len(running_dids)
    except Exception:
        result["tasks_running"] = 0

    # VPN 统计（须与 _get_config_path 一致：routers 下四层才到项目根，勿用三层 parent）
    try:
        from src.behavior.vpn_manager import get_vpn_manager
        mgr = _get_device_manager()
        devices = mgr.get_all_devices()
        vpn_mgr = get_vpn_manager()
        vpn_total = 0
        vpn_ok = 0
        for d in devices:
            did = getattr(d, "device_id", "")
            if did and getattr(d, "is_online", False):
                vpn_total += 1
                try:
                    s = vpn_mgr.status(did)
                    if s.connected:
                        vpn_ok += 1
                except Exception:
                    pass
        result["vpn_ok"] = vpn_ok
        result["vpn_total"] = vpn_total
    except Exception:
        result["vpn_ok"] = 0
        result["vpn_total"] = 0

    # 健康平均分
    try:
        from src.host.health_monitor import metrics
        scores = []
        for did, st in (metrics.device_status or {}).items():
            hs = st.get("health_score")
            if hs is not None:
                scores.append(hs)
        result["health_avg"] = round(sum(scores) / len(scores)) if scores else 0
    except Exception:
        result["health_avg"] = 0

    # TikTok 今日统计
    try:
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        devs = ds.list_devices()
        totals = {"watched": 0, "followed": 0, "dms_sent": 0}
        for did in devs:
            s = ds.get_device_summary(did)
            totals["watched"] += s.get("total_watched", 0)
            totals["followed"] += s.get("total_followed", 0)
            totals["dms_sent"] += s.get("total_dms_sent", 0)
        result["tiktok"] = totals
    except Exception:
        result["tiktok"] = {"watched": 0, "followed": 0, "dms_sent": 0}

    return result


@router.get("/system/task-analysis", dependencies=_auth)
def task_analysis(hours: int = 24):
    """按任务类型/设备/错误类别分析最近 N 小时的执行数据（默认 24h，最大 168h）。

    Returns:
        totals           — 全局合计（total/completed/failed/success_rate）
        by_type          — 按任务类型分组，按失败数降序排列
        by_device        — 按设备分组，按失败数降序排列
        error_categories — 失败原因分类计数（sla_timeout/gate_blocked/account_risk/...）
        top_errors       — Top-20 具体错误文本（附分类标签）
        fail_history     — 各 device:task_type 最近 5 条带 error 片段的失败记录
    """
    from src.host.task_store import get_task_analysis
    hours = max(1, min(int(hours), 168))
    return get_task_analysis(hours)
