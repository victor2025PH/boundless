# -*- coding: utf-8 -*-
"""数据分析路由。"""

import csv
import io
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.utils.subprocess_text import run as _sp_run_text
from src.host.device_registry import DEFAULT_DEVICES_YAML, data_file

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", tags=["analytics"])


def _get_task_store():
    from ..api import task_store
    return task_store


@router.get("/summary")
def analytics_summary(days: int = 30, _local_only: int = 0):
    """Aggregated task statistics for the analytics dashboard — includes cluster Worker tasks."""
    import urllib.request as _ur, json as _json, concurrent.futures as _cf
    ts = _get_task_store()
    all_tasks = ts.list_tasks()
    cutoff = datetime.now() - timedelta(days=days)
    recent = []
    for t in all_tasks:
        created = t.get("created_at", "")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if dt.replace(tzinfo=None) >= cutoff:
                recent.append(t)
        except Exception:
            recent.append(t)

    total = len(recent)
    success = sum(1 for t in recent if t.get("status") == "completed")
    failed = sum(1 for t in recent if t.get("status") == "failed")
    running = sum(1 for t in recent if t.get("status") == "running")

    daily: dict = {}
    for t in recent:
        day = t.get("created_at", "")[:10]
        if day:
            daily.setdefault(day, {"total": 0, "success": 0, "failed": 0})
            daily[day]["total"] += 1
            if t.get("status") == "completed":
                daily[day]["success"] += 1
            elif t.get("status") == "failed":
                daily[day]["failed"] += 1

    type_counts: dict = {}
    for t in recent:
        tt = t.get("task_type", "unknown")
        type_counts[tt] = type_counts.get(tt, 0) + 1

    device_stats: dict = {}
    for t in recent:
        did = t.get("device_id", "unknown")
        device_stats.setdefault(did, {"total": 0, "success": 0})
        device_stats[did]["total"] += 1
        if t.get("status") == "completed":
            device_stats[did]["success"] += 1

    device_rank = sorted(
        [{"device_id": k, "total": v["total"],
          "success": v["success"],
          "rate": round(v["success"] / v["total"] * 100, 1)
          if v["total"] else 0}
         for k, v in device_stats.items()],
        key=lambda x: x["rate"], reverse=True
    )[:10]

    # Aggregate Worker-03 summary (non-recursive: skip if _local_only=1)
    if _local_only:
        success_rate = round(success / total * 100, 1) if total else 0
        return {
            "total": total, "success": success, "failed": failed,
            "running": running, "success_rate": success_rate,
            "daily": {k: v for k, v in sorted(daily.items())},
            "type_counts": type_counts,
            "device_rank": device_rank,
        }
    try:
        from ..multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]
        if online_hosts:
            def _fetch_summary(h):
                url = f"http://{h.host_ip}:{h.port}/analytics/summary?days={days}&_local_only=1"
                req = _ur.Request(url)
                if coord._secret:
                    req.add_header("X-Cluster-Secret", coord._secret)
                resp = _ur.urlopen(req, timeout=4)
                return _json.loads(resp.read().decode())

            with _cf.ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(_fetch_summary, h) for h in online_hosts]
                for f in _cf.as_completed(futures, timeout=5):
                    try:
                        w = f.result()
                        total += w.get("total", 0)
                        success += w.get("success", 0)
                        failed += w.get("failed", 0)
                        running += w.get("running", 0)
                        for d, entry in w.get("daily", {}).items():
                            if d:
                                daily.setdefault(d, {"total": 0, "success": 0, "failed": 0})
                                daily[d]["total"] += entry.get("total", 0)
                                daily[d]["success"] += entry.get("success", 0)
                                daily[d]["failed"] += entry.get("failed", 0)
                        for tt, cnt in w.get("type_counts", {}).items():
                            type_counts[tt] = type_counts.get(tt, 0) + cnt
                        for dr in w.get("device_rank", []):
                            did = dr.get("device_id", "")
                            if did:
                                device_stats.setdefault(did, {"total": 0, "success": 0})
                                device_stats[did]["total"] += dr.get("total", 0)
                                device_stats[did]["success"] += dr.get("success", 0)
                    except Exception:
                        pass
            # Recompute device_rank with merged data
            device_rank = sorted(
                [{"device_id": k, "total": v["total"],
                  "success": v["success"],
                  "rate": round(v["success"] / v["total"] * 100, 1) if v["total"] else 0}
                 for k, v in device_stats.items()],
                key=lambda x: x["rate"], reverse=True
            )[:10]
    except Exception:
        pass

    success_rate = round(success / total * 100, 1) if total else 0
    return {
        "total": total, "success": success, "failed": failed,
        "running": running, "success_rate": success_rate,
        "daily": {k: v for k, v in sorted(daily.items())},
        "type_counts": type_counts,
        "device_rank": device_rank,
    }


@router.get("/today")
def analytics_today():
    """P10: 今日关键指标 — 聚合 device_state 日粒度数据 + CRM + 跨节点。"""
    from datetime import date
    today_str = date.today().isoformat()   # YYYY-MM-DD

    result = {
        "date": today_str,
        "watched": 0,        # 今日刷视频
        "followed": 0,       # 今日关注
        "dms_sent": 0,       # 今日私信（发出）
        "auto_replied": 0,   # AI自动回复
        "new_leads": 0,      # 今日新线索（CRM）
        "conversions": 0,    # 已转化
        # 兼容旧字段
        "follows": 0,
        "likes": 0,
        "replies_received": 0,
    }

    # ── 1. device_state 日粒度数据（本节点） ──
    try:
        from src.host.device_state import get_device_state_store
        from src.device_control.device_manager import get_device_manager
        ds = get_device_state_store("tiktok")
        manager = get_device_manager(DEFAULT_DEVICES_YAML)
        devices = [d.device_id for d in manager.get_all_devices()]
        for did in devices:
            result["watched"]   += ds.get_int(did, f"daily:{today_str}:watched", 0)
            result["followed"]  += ds.get_int(did, f"daily:{today_str}:followed", 0)
            result["dms_sent"]  += ds.get_int(did, f"daily:{today_str}:dms", 0)
    except Exception:
        pass

    # ── 2. 从 Worker 节点拉取并合并（cluster） ──
    try:
        import urllib.request as _ur, json as _json
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]
        for h in online_hosts:
            url = f"http://{h.host_ip}:{h.port}/analytics/today"
            try:
                resp = _ur.urlopen(_ur.Request(url), timeout=4)
                w = _json.loads(resp.read().decode())
                if w.get("date") == today_str:
                    result["watched"]   += w.get("watched", 0)
                    result["followed"]  += w.get("followed", 0)
                    result["dms_sent"]  += w.get("dms_sent", 0)
                    result["auto_replied"] += w.get("auto_replied", 0)
            except Exception:
                pass
    except Exception:
        pass

    # ── 3. 任务级今日数据（followed/auto_replied）来自 tiktok_daily_report，它已聚合 Worker-03 ──
    try:
        from src.host.routers.tiktok import tiktok_daily_report
        dr = tiktok_daily_report()
        today_task = dr.get("today", {})
        # followed/dms_sent 优先用 device_state（精确）；如果为0则用任务统计兜底
        if result["followed"] == 0:
            result["followed"] = today_task.get("followed", 0)
        if result["dms_sent"] == 0:
            result["dms_sent"] = today_task.get("follow_backs", 0)  # DMs sent to followbacks
        result["auto_replied"] = today_task.get("auto_replied", 0)
    except Exception:
        pass

    # ── 4. CRM 今日新线索 + 转化（本地 + Worker-03 缓存） ──
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        for lead in store.list_leads(limit=1000):
            created = (lead.get("created_at") or "")[:10]
            if created == today_str:
                result["new_leads"] += 1
            if lead.get("status") == "converted":
                conv_at = (lead.get("converted_at") or "")[:10]
                if conv_at == today_str:
                    result["conversions"] += 1
    except Exception:
        pass

    # Worker-03 CRM 缓存补充
    try:
        from src.host.leads_cache import get_w03_cache, _IS_WORKER03
        if not _IS_WORKER03:
            w03_leads = get_w03_cache().get_leads() or []
            for lead in w03_leads:
                created = (lead.get("created_at") or "")[:10]
                if created == today_str:
                    result["new_leads"] += 1
                if lead.get("status") == "converted":
                    conv_at = (lead.get("converted_at") or "")[:10]
                    if conv_at == today_str:
                        result["conversions"] += 1
    except Exception:
        pass

    result["follows"] = result["followed"]
    return result


@router.get("/export")
def analytics_export(days: int = 30):
    """Export task data as CSV."""
    ts = _get_task_store()
    all_tasks = ts.list_tasks()
    cutoff = datetime.now() - timedelta(days=days)
    recent = []
    for t in all_tasks:
        created = t.get("created_at", "")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if dt.replace(tzinfo=None) >= cutoff:
                recent.append(t)
        except Exception:
            recent.append(t)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["task_id", "task_type", "device_id", "status",
                     "created_at", "completed_at", "duration_s"])
    for t in recent:
        writer.writerow([
            t.get("task_id", ""), t.get("task_type", ""),
            t.get("device_id", ""), t.get("status", ""),
            t.get("created_at", ""), t.get("completed_at", ""),
            t.get("duration_seconds", ""),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=openclaw_tasks_{days}d.csv"})


# ---------------------------------------------------------------------------
# Trend endpoints (moved from api.py)
# ---------------------------------------------------------------------------

@router.get("/device-trend")
def device_trend(range: str = "24h"):
    import time
    from ..analytics_store import load_analytics_history, range_to_count, get_analytics_cache
    load_analytics_history()
    count = range_to_count(range)
    cache = get_analytics_cache()
    snaps = cache.get("device_snapshots", [])[-count:]
    if not snaps:
        try:
            from src.device_control.device_manager import get_device_manager

            manager = get_device_manager(DEFAULT_DEVICES_YAML)
            devices = manager.get_all_devices()
            online = sum(1 for d in devices if d.is_online)
            total = len(devices)
        except Exception:
            online, total = 0, 0
        snaps = [{"ts": time.strftime("%H:%M"), "online": online, "total": total}]
    return {
        "labels": [s["ts"].split(" ")[-1] if " " in s["ts"] else s["ts"] for s in snaps],
        "online": [s.get("online", 0) for s in snaps],
        "total": [s.get("total", 0) for s in snaps],
    }


@router.get("/task-trend")
def task_trend(range: str = "24h"):
    import time
    from ..analytics_store import load_analytics_history, range_to_count, get_analytics_cache
    load_analytics_history()
    count = range_to_count(range)
    cache = get_analytics_cache()
    snaps = cache.get("task_snapshots", [])[-count:]
    if not snaps:
        try:
            ts = _get_task_store()
            tasks = ts.list_tasks(limit=9999)
            success = sum(1 for t in tasks if getattr(t, "status", "") in ("completed", "success"))
            failed = sum(1 for t in tasks if getattr(t, "status", "") == "failed")
        except Exception:
            tasks, success, failed = [], 0, 0
        snaps = [{"ts": time.strftime("%H:%M"), "success": success, "failed": failed, "total": len(tasks)}]
    return {
        "labels": [s["ts"].split(" ")[-1] if " " in s["ts"] else s["ts"] for s in snaps],
        "success": [s.get("success", 0) for s in snaps],
        "failed": [s.get("failed", 0) for s in snaps],
        "total": [s.get("total", 0) for s in snaps],
    }


@router.get("/roi")
def analytics_roi():
    """ROI (投入产出比) dashboard data for the last 7 days — cluster aggregated."""
    import urllib.request as _ur, json as _json, concurrent.futures as _cf
    # Use the already-aggregated summary endpoint (includes Worker-03 tasks)
    summary = analytics_summary(days=7)
    type_counts = summary.get("type_counts", {})
    tasks_executed = summary.get("total", 0)
    cutoff = datetime.now() - timedelta(days=7)

    # Devices active: from summary device_rank
    devices_active = len(summary.get("device_rank", []))

    # Estimate hours: fetch from /tasks to get duration_seconds (best effort)
    total_hours = 0.0
    try:
        import urllib.request as _ur2, json as _json2
        from ..multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]
        all_recent = []
        for h in online_hosts:
            try:
                url = f"http://{h.host_ip}:{h.port}/tasks?limit=200"
                req = _ur2.Request(url)
                if coord._secret:
                    req.add_header("X-Cluster-Secret", coord._secret)
                resp = _ur2.urlopen(req, timeout=4)
                all_recent.extend(_json2.loads(resp.read().decode()))
            except Exception:
                pass
        _cutoff_str = cutoff.isoformat()
        for t in all_recent:
            if (t.get("created_at", "") or "") >= _cutoff_str[:10]:
                try:
                    total_hours += float(t.get("duration_seconds") or 0) / 3600
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    total_hours = round(total_hours, 1)

    # --- Output ---
    follows_sent = type_counts.get("follow", 0) + type_counts.get("tiktok_follow", 0)
    dms_sent = type_counts.get("dm", 0) + type_counts.get("send_dm", 0) + type_counts.get("tiktok_dm", 0)

    # ── 从 device_state 读取真实的关注/私信数据 ──────────────────────────
    # task_type 计数无法反映 warmup/auto 内部的实际行为，device_state 才是真实数据源
    try:
        from ..device_state import DeviceStateStore
        _ds = DeviceStateStore()
        _dev_ids = _ds.list_devices()
        _today = datetime.now()
        _ds_follows = 0
        _ds_dms = 0
        _ds_active = set()
        for _i in range(7):
            _date = (_today - timedelta(days=_i)).strftime("%Y-%m-%d")
            for _did in _dev_ids:
                _f = _ds.get_int(_did, f"daily:{_date}:followed")
                _d = _ds.get_int(_did, f"daily:{_date}:dms")
                _ds_follows += _f
                _ds_dms += _d
                if _f > 0 or _d > 0:
                    _ds_active.add(_did)
        # 若 device_state 有数据则优先使用
        if _ds_follows > 0:
            follows_sent = _ds_follows
        if _ds_dms > 0:
            dms_sent = _ds_dms
        if _ds_active:
            devices_active = max(devices_active, len(_ds_active))
    except Exception:
        pass

    # ── 如果本地 device_state 无数据（Coordinator无本地设备），尝试从集群Worker获取 ──
    if follows_sent == 0:
        try:
            import urllib.request as _ur_tt
            import json as _json_tt
            from src.openclaw_env import local_api_base

            _req = _ur_tt.Request(f"{local_api_base()}/tiktok/daily-report")
            _resp = _ur_tt.urlopen(_req, timeout=4)
            _dr = _json_tt.loads(_resp.read().decode())
            _devs = _dr.get("devices", [])
            _cluster_follows = sum(d.get("total_followed", 0) for d in _devs)
            _cluster_dms = sum(d.get("total_dms", 0) for d in _devs)
            _cluster_active = sum(1 for d in _devs if d.get("total_followed", 0) > 0 or d.get("total_dms", 0) > 0)
            if _cluster_follows > follows_sent:
                follows_sent = _cluster_follows
            if _cluster_dms > dms_sent:
                dms_sent = _cluster_dms
            if _cluster_active > devices_active:
                devices_active = _cluster_active
        except Exception:
            pass

    # Try to get leads data
    leads_generated = 0
    followbacks = 0
    try:
        from ..api import lead_store
        if lead_store:
            all_leads = lead_store.list_leads() if hasattr(lead_store, 'list_leads') else []
            for ld in all_leads:
                created = ld.get("created_at", "")
                if not created:
                    continue
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if dt.replace(tzinfo=None) >= cutoff:
                        leads_generated += 1
                except Exception:
                    leads_generated += 1
    except Exception:
        pass

    # Followbacks from task results or type counts
    followbacks = type_counts.get("follow_back", 0) + type_counts.get("followback", 0)

    followback_rate = round(followbacks / follows_sent * 100, 1) if follows_sent > 0 else 0

    # --- Efficiency ---
    days_period = 7
    follows_per_device_per_day = round(
        follows_sent / (devices_active * days_period), 1
    ) if devices_active > 0 else 0
    dms_per_device_per_day = round(
        dms_sent / (devices_active * days_period), 1
    ) if devices_active > 0 else 0
    cost_per_lead_minutes = round(
        (total_hours * 60) / leads_generated, 1
    ) if leads_generated > 0 else 0

    return {
        "period": "7d",
        "investment": {
            "devices_active": devices_active,
            "total_hours": total_hours,
            "tasks_executed": tasks_executed,
        },
        "output": {
            "follows_sent": follows_sent,
            "followbacks": followbacks,
            "dms_sent": dms_sent,
            "leads_generated": leads_generated,
            "followback_rate": followback_rate,
        },
        "efficiency": {
            "follows_per_device_per_day": follows_per_device_per_day,
            "dms_per_device_per_day": dms_per_device_per_day,
            "cost_per_lead_minutes": cost_per_lead_minutes,
        },
    }


@router.get("/activity-summary")
def activity_summary():
    """Combined activity summary from device_state (real data) + leads_store (lead-level data).
    Bridges the gap between aggregate device stats and per-lead funnel."""
    from src.host.device_state import get_device_state_store
    from src.device_control.device_manager import get_device_manager

    ds = get_device_state_store("tiktok")
    try:
        manager = get_device_manager(DEFAULT_DEVICES_YAML)
        devices = [d.device_id for d in manager.get_all_devices()]
    except Exception:
        devices = []

    total_watched = 0
    total_followed = 0
    total_dms = 0
    total_sessions = 0
    today_followed = 0
    today_dms = 0
    active_devices = 0

    for did in devices:
        try:
            s = ds.get_device_summary(did)
            total_watched += s.get("total_watched", 0)
            total_followed += s.get("total_followed", 0)
            total_dms += s.get("total_dms_sent", 0)
            total_sessions += s.get("sessions_today", 0)
            if s.get("phase") in ("active", "interest_building"):
                active_devices += 1
        except Exception:
            pass

    # Rates
    dm_rate = round(total_dms / max(total_followed, 1) * 100, 1)
    est_replies = round(total_dms * 0.3)  # Estimate: ~30% reply rate if not tracked

    # Try to get real lead data too
    lead_stats = {}
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
        lead_stats = store.get_stats() if hasattr(store, "get_stats") else {}
    except Exception:
        pass

    return {
        "source": "device_state_aggregate",
        "devices": len(devices),
        "active_devices": active_devices,
        "funnel": {
            "watched": total_watched,
            "followed": total_followed,
            "dms_sent": total_dms,
            "dm_rate_pct": dm_rate,
            "sessions_today": total_sessions,
        },
        "leads": lead_stats,
        "rates": {
            "follow_to_dm": dm_rate,
            "estimated_reply_rate": 30.0,
        }
    }


@router.get("/export/data")
def export_analytics_data(type: str = "devices", format: str = "csv"):
    """Export analytics data as CSV or JSON.
    type: devices | tasks | leads
    """
    from fastapi.responses import JSONResponse

    if type == "devices":
        from src.host.device_state import get_device_state_store
        from src.device_control.device_manager import get_device_manager
        ds = get_device_state_store("tiktok")
        try:
            manager = get_device_manager(DEFAULT_DEVICES_YAML)
            devices = [d.device_id for d in manager.get_all_devices()]
        except Exception:
            devices = []
        rows = []
        for did in devices:
            try:
                s = ds.get_device_summary(did)
                rows.append({
                    "device_id": did,
                    "phase": s.get("phase", ""),
                    "total_watched": s.get("total_watched", 0),
                    "total_followed": s.get("total_followed", 0),
                    "total_dms": s.get("total_dms_sent", 0),
                    "dm_rate_pct": round(s.get("total_dms_sent", 0) / max(s.get("total_followed", 0), 1) * 100, 1),
                    "sessions_today": s.get("sessions_today", 0),
                    "days_active": s.get("days_active", 0),
                })
            except Exception:
                pass

    elif type == "tasks":
        from src.host.task_store import list_tasks
        try:
            tasks = list_tasks(limit=500)
        except Exception:
            tasks = _get_task_store().list_tasks()
        rows = [{"task_id": t.get("task_id", "")[:8], "type": t.get("type", t.get("task_type", "")), "device_id": t.get("device_id", ""), "status": t.get("status", ""), "created_at": t.get("created_at", ""), "updated_at": t.get("updated_at", t.get("completed_at", ""))} for t in tasks]

    elif type == "leads":
        try:
            from src.leads.store import get_leads_store
            leads = get_leads_store().list_leads(limit=500)
            rows = [{"id": str(l.get("id", ""))[:8], "name": l.get("name", ""), "status": l.get("status", ""), "score": l.get("score", 0), "platform": l.get("source_platform", ""), "created_at": l.get("created_at", ""), "tags": ",".join(l.get("tags", []))} for l in leads]
        except Exception:
            rows = []
    else:
        return {"error": f"Unknown type: {type}"}

    if format == "json":
        return rows

    # CSV output
    if not rows:
        return StreamingResponse(io.StringIO("no data\n"), media_type="text/csv",
                                  headers={"Content-Disposition": f"attachment; filename={type}.csv"})
    output = io.StringIO()
    import time as _time
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=openclaw_{type}_{_time.strftime('%Y%m%d')}.csv"}
    )


@router.get("/cluster-summary")
def cluster_activity_summary():
    """Aggregate activity summary across all cluster nodes."""
    import urllib.request as _ur, json as _json
    import concurrent.futures

    # Get local data first
    local = activity_summary()

    worker_summaries = []
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]

        def _fetch(h):
            url = f"http://{h.host_ip}:{h.port}/analytics/activity-summary"
            resp = _ur.urlopen(_ur.Request(url), timeout=3)
            return _json.loads(resp.read().decode())

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(_fetch, h) for h in online_hosts]
            for f in concurrent.futures.as_completed(futures, timeout=4):
                try:
                    worker_summaries.append(f.result())
                except Exception:
                    pass
    except Exception:
        pass

    if not worker_summaries:
        return {**local, "cluster": False}

    # Aggregate
    total_followed = local["funnel"]["followed"]
    total_dms = local["funnel"]["dms_sent"]
    total_watched = local["funnel"]["watched"]
    total_devices = local["devices"]

    for w in worker_summaries:
        total_followed += w.get("funnel", {}).get("followed", 0)
        total_dms += w.get("funnel", {}).get("dms_sent", 0)
        total_watched += w.get("funnel", {}).get("watched", 0)
        total_devices += w.get("devices", 0)

    dm_rate = round(total_dms / max(total_followed, 1) * 100, 1)
    return {
        "source": "cluster_aggregate",
        "cluster": True,
        "worker_nodes": len(worker_summaries),
        "devices": total_devices,
        "funnel": {
            "watched": total_watched,
            "followed": total_followed,
            "dms_sent": total_dms,
            "dm_rate_pct": dm_rate,
        },
        "rates": {"follow_to_dm": dm_rate},
    }


@router.get("/daily-trend")
def daily_trend(days: int = 7):
    """P10-C: 最近 N 天的每日关注/私信趋势（本节点 + Worker-03 聚合）。"""
    from datetime import date, timedelta as _td
    from src.host.device_state import get_device_state_store
    from src.device_control.device_manager import get_device_manager

    ds = get_device_state_store("tiktok")
    try:
        manager = get_device_manager(DEFAULT_DEVICES_YAML)
        devices = [d.device_id for d in manager.get_all_devices()]
    except Exception:
        devices = []

    today = date.today()
    trend = []
    for i in range(days - 1, -1, -1):
        d = today - _td(days=i)
        date_str = d.strftime("%Y-%m-%d")   # DeviceStateStore._today() 使用 YYYY-MM-DD 格式
        day_followed = 0
        day_dms = 0
        for did in devices:
            day_followed += ds.get_int(did, f"daily:{date_str}:followed", 0)
            day_dms += ds.get_int(did, f"daily:{date_str}:dms", 0)
        trend.append({
            "date": date_str,
            "label": d.strftime("%m/%d"),
            "followed": day_followed,
            "dms": day_dms,
        })

    # 从 Worker 节点拉取并合并
    try:
        import urllib.request as _ur, json as _json
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]
        for h in online_hosts:
            url = f"http://{h.host_ip}:{h.port}/analytics/daily-trend?days={days}"
            try:
                resp = _ur.urlopen(_ur.Request(url), timeout=4)
                w_data = _json.loads(resp.read().decode())
                for i, entry in enumerate(w_data.get("trend", [])):
                    if i < len(trend) and trend[i]["date"] == entry.get("date"):
                        trend[i]["followed"] += entry.get("followed", 0)
                        trend[i]["dms"] += entry.get("dms", 0)
            except Exception:
                pass
    except Exception:
        pass

    return {"days": days, "trend": trend}


@router.get("/unified-funnel")
def unified_funnel(days: int = 30):
    """P10-E: 统一漏斗 = device_state 顶层漏斗 + CRM 底层漏斗，7 阶段全链路。"""
    import urllib.request as _ur, json as _json
    from src.host.device_state import get_device_state_store
    from src.device_control.device_manager import get_device_manager

    ds = get_device_state_store("tiktok")
    try:
        manager = get_device_manager(DEFAULT_DEVICES_YAML)
        devices = [d.device_id for d in manager.get_all_devices()]
    except Exception:
        devices = []

    # 顶层漏斗来自 device_state（全量累计）
    total_watched = sum(ds.get_int(did, "total_watched", 0) for did in devices)
    total_followed = sum(ds.get_int(did, "total_followed", 0) for did in devices)
    total_dms = sum(ds.get_int(did, "total_dms_sent", 0) for did in devices)

    # 从 Worker 节点拉取 device_state 顶层数据
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]
        for h in online_hosts:
            url = f"http://{h.host_ip}:{h.port}/analytics/activity-summary"
            try:
                resp = _ur.urlopen(_ur.Request(url), timeout=4)
                w = _json.loads(resp.read().decode())
                total_watched += w.get("funnel", {}).get("watched", 0)
                total_followed += w.get("funnel", {}).get("followed", 0)
                total_dms += w.get("funnel", {}).get("dms_sent", 0)
            except Exception:
                pass
    except Exception:
        pass

    # 底层漏斗来自 CRM（本地 + Worker-03 代理）
    crm_funnel: dict = {}
    try:
        from src.leads.store import get_leads_store
        crm_funnel = get_leads_store().get_conversion_funnel("tiktok", days) or {}
    except Exception:
        pass

    # 如果本地是 coordinator（无 device），还需从缓存取 Worker-03 CRM 漏斗
    try:
        from src.host.leads_cache import get_w03_cache, _IS_WORKER03
        if not _IS_WORKER03:
            w03_funnel = get_w03_cache().get_funnel() or {}
            if w03_funnel and isinstance(w03_funnel.get("funnel"), dict):
                cf = w03_funnel["funnel"]
                bf = crm_funnel.get("funnel", {})
                crm_funnel = {
                    "funnel": {k: bf.get(k, 0) + cf.get(k, 0) for k in set(bf) | set(cf)}
                }
    except Exception:
        pass

    crm = crm_funnel.get("funnel", {})
    follow_back = crm.get("follow_back", 0)
    chatted = crm.get("chatted", 0)
    replied = crm.get("replied", 0)
    qualified = crm.get("qualified", 0)
    converted = crm.get("converted", 0)

    return {
        "period_days": days,
        "stages": [
            {"stage": "watched",     "label": "刷到视频",  "count": total_watched,  "source": "device_state"},
            {"stage": "followed",    "label": "已关注",    "count": total_followed, "source": "device_state"},
            {"stage": "follow_back", "label": "回关",      "count": follow_back,    "source": "crm"},
            {"stage": "chatted",     "label": "已私信",    "count": max(chatted, total_dms), "source": "hybrid"},
            {"stage": "replied",     "label": "已回复",    "count": replied,        "source": "crm"},
            {"stage": "qualified",   "label": "已认定",    "count": qualified,      "source": "crm"},
            {"stage": "converted",   "label": "已转化",    "count": converted,      "source": "crm"},
        ],
        "rates": {
            "follow_rate":    round(total_followed / max(total_watched, 1) * 100, 1),
            "followback_rate": round(follow_back / max(total_followed, 1) * 100, 1),
            "dm_rate":        round(total_dms / max(total_followed, 1) * 100, 1),
            "reply_rate":     round(replied / max(total_dms, 1) * 100, 1),
            "qualify_rate":   round(qualified / max(replied, 1) * 100, 1),
            "convert_rate":   round(converted / max(qualified, 1) * 100, 1),
        },
    }


@router.get("/device-perf")
def device_perf():
    """Fetch battery/memory performance for all cluster devices.
    Coordinator has no local ADB devices, so we proxy to each Worker's monitoring endpoint.
    Returns: {"devices": {"device_id": {"battery_level": N, "mem_usage": N, ...}}}
    """
    import urllib.request as _ur
    import json as _json
    import concurrent.futures

    result: dict = {}

    # Fast path: read from WS hub's cached perf data (updated every ~30s by push.performance)
    try:
        from ..websocket_hub import get_ws_hub
        hub = get_ws_hub()
        if hub and hub._last_perf_data:
            result.update(hub._last_perf_data)
    except Exception:
        pass

    # Slow path: run ADB if cache is empty (first request before WS has pushed any data)
    if not result:
        try:
            import re
            from src.device_control.device_manager import get_device_manager
            manager = get_device_manager(DEFAULT_DEVICES_YAML)
            for d in manager.get_all_devices():
                if d.status.value not in ("connected", "online"):
                    continue
                try:
                    r = _sp_run_text(
                        ["adb", "-s", d.device_id, "shell",
                         "dumpsys battery | grep level && cat /proc/meminfo | head -3"],
                        capture_output=True, timeout=5,
                    )
                    if r.returncode == 0:
                        entry = {}
                        bat = re.search(r'level:\s*(\d+)', r.stdout)
                        mt_m = re.search(r'MemTotal:\s*(\d+)', r.stdout)
                        ma_m = re.search(r'MemAvailable:\s*(\d+)', r.stdout)
                        if bat:
                            entry["battery_level"] = int(bat.group(1))
                        if mt_m and ma_m:
                            mt = int(mt_m.group(1))
                            ma = int(ma_m.group(1))
                            entry["mem_usage"] = round((mt - ma) / mt * 100, 1) if mt else 0
                        if entry:
                            result[d.device_id] = entry
                except Exception:
                    pass
        except Exception:
            pass

    # Cluster Worker nodes — proxy to their /analytics/device-perf
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]

        def _fetch_worker(h):
            url = f"http://{h.host_ip}:{h.port}/analytics/device-perf"
            req = _ur.Request(url)
            if coord._secret:
                req.add_header("X-Cluster-Secret", coord._secret)
            resp = _ur.urlopen(req, timeout=4)
            return _json.loads(resp.read().decode())

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(_fetch_worker, h) for h in online_hosts]
            for f in concurrent.futures.as_completed(futures, timeout=5):
                try:
                    w = f.result()
                    result.update(w.get("devices", {}))
                except Exception:
                    pass
    except Exception:
        pass

    return {"devices": result}


# ---------------------------------------------------------------------------
# P3: 运营日报 — 按时间区间生成摘要报告
# ---------------------------------------------------------------------------

@router.post("/report")
def generate_report(body: dict):
    """生成指定时间段的运营日报。

    Body:
        start_dt: "2026-04-01 00:00"  (起始日期时间)
        end_dt:   "2026-04-05 23:59"  (截止日期时间)
        use_ai:   true                (是否用AI生成文字摘要, 默认true)
    """
    import sqlite3 as _sql
    from datetime import datetime as _dt

    start_str = body.get("start_dt", "")
    end_str = body.get("end_dt", "")
    use_ai = body.get("use_ai", True)

    if not start_str or not end_str:
        return {"ok": False, "error": "start_dt 和 end_dt 不能为空"}

    # 解析时间
    try:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                start_dt = _dt.strptime(start_str, fmt)
                break
            except ValueError:
                continue
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                end_dt = _dt.strptime(end_str, fmt)
                break
            except ValueError:
                continue
    except Exception:
        return {"ok": False, "error": f"时间格式无效: {start_str} / {end_str}"}

    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")
    period_days = max(1, (end_dt - start_dt).days + 1)

    stats = {
        "period": {"start": start_str, "end": end_str, "days": period_days},
        "devices": [],
        "totals": {
            "sessions": 0,
            "videos_watched": 0,
            "follows": 0,
            "dms_sent": 0,
            "dms_responded": 0,
            "leads_qualified": 0,
            "online_minutes": 0,
            "algo_score_avg": 0,
        },
        "leads": {
            "new_leads": 0,
            "pitched": 0,
            "responded": 0,
            "qualified": 0,
            "converted": 0,
        },
        "top_devices": [],
        "ai_summary": "",
    }

    # ── 从 device_daily_stats 聚合 ──
    db_path = data_file("openclaw.db")
    if db_path.exists():
        try:
            conn = _sql.connect(str(db_path), timeout=5)
            rows = conn.execute("""
                SELECT device_id,
                       SUM(sessions_count)    as sessions,
                       SUM(videos_watched)    as watched,
                       SUM(follows_count)     as follows,
                       SUM(dms_sent)          as dms_sent,
                       SUM(dms_responded)     as dms_responded,
                       SUM(leads_qualified)   as leads_qualified,
                       AVG(algo_score)        as algo_avg,
                       SUM(online_minutes)    as online_minutes
                FROM device_daily_stats
                WHERE date >= ? AND date <= ?
                GROUP BY device_id
                ORDER BY dms_sent DESC
            """, (start_date, end_date)).fetchall()
            conn.close()

            for r in rows:
                dev = {
                    "device_id": r[0],
                    "sessions": r[1] or 0,
                    "watched": r[2] or 0,
                    "follows": r[3] or 0,
                    "dms_sent": r[4] or 0,
                    "dms_responded": r[5] or 0,
                    "leads_qualified": r[6] or 0,
                    "algo_score_avg": round(r[7] or 0, 1),
                    "online_minutes": r[8] or 0,
                }
                stats["devices"].append(dev)
                for k in ("sessions", "follows", "dms_sent", "dms_responded", "leads_qualified", "online_minutes"):
                    stats["totals"][k] += dev[k]
                stats["totals"]["videos_watched"] += dev["watched"]

            if stats["devices"]:
                stats["totals"]["algo_score_avg"] = round(
                    sum(d["algo_score_avg"] for d in stats["devices"]) / len(stats["devices"]), 1
                )

            # Top 5 by DMs sent
            stats["top_devices"] = sorted(stats["devices"], key=lambda d: d["dms_sent"], reverse=True)[:5]

        except Exception as _e:
            logger.error("读取 device_daily_stats 失败: %s", _e)

    # ── 用 W03 实时数据补充本地为0的统计项 ──
    try:
        import urllib.request as _ur2, json as _json2
        _w3r = _ur2.urlopen(_ur2.Request("http://192.168.0.103:8000/tiktok/devices"), timeout=4)
        _w3devs = _json2.loads(_w3r.read())
        if isinstance(_w3devs, list):
            # 如果本地 watched/follows/dms 均为0，使用 W03 累计总量（累计≠区间，但比0更有参考价值）
            if stats["totals"]["videos_watched"] == 0:
                _w3watched = sum(d.get("total_watched", 0) for d in _w3devs)
                if _w3watched > 0:
                    stats["totals"]["videos_watched"] = _w3watched
                    stats["_w3_enriched"] = True
            if stats["totals"]["follows"] == 0:
                _w3follows = sum(d.get("total_followed", 0) for d in _w3devs)
                if _w3follows > 0:
                    stats["totals"]["follows"] = _w3follows
            if stats["totals"]["dms_sent"] == 0:
                _w3dms = sum(d.get("total_dms_sent", 0) for d in _w3devs)
                if _w3dms > 0:
                    stats["totals"]["dms_sent"] = _w3dms
            # 补充 top_devices：如果本地 top_devices 所有 dms=0，用 W03 数据填充
            if all(d.get("dms_sent", 0) == 0 for d in stats["top_devices"]):
                _aliases_map = {}
                try:
                    _ar = _ur2.urlopen(_ur2.Request("http://192.168.0.103:8000/devices/aliases"), timeout=3)
                    _aliases_map = _json2.loads(_ar.read())
                except Exception:
                    pass
                _w3_top = sorted(_w3devs, key=lambda d: d.get("total_dms_sent", 0), reverse=True)[:5]
                stats["top_devices"] = [
                    {
                        "device_id": d.get("device_id", ""),
                        "alias": (_aliases_map.get(d.get("device_id", ""), {}) or {}).get("alias", d.get("device_id","")[:8]),
                        "sessions": d.get("sessions_today", 0),
                        "watched": d.get("total_watched", 0),
                        "follows": d.get("total_followed", 0),
                        "dms_sent": d.get("total_dms_sent", 0),
                        "dms_responded": 0,
                        "leads_qualified": 0,
                        "algo_score_avg": round(float(d.get("algorithm_score") or 0), 1),
                        "online_minutes": 0,
                    }
                    for d in _w3_top if d.get("total_dms_sent", 0) > 0
                ]
    except Exception:
        pass

    # ── 从 leads.db 读取线索统计 ──
    leads_db = data_file("leads.db")
    if leads_db.exists():
        try:
            conn = _sql.connect(str(leads_db), timeout=5)
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status IN ('pitched','responded','qualified','converted') THEN 1 ELSE 0 END) as pitched,
                    SUM(CASE WHEN status IN ('responded','qualified','converted') THEN 1 ELSE 0 END) as responded,
                    SUM(CASE WHEN status IN ('qualified','converted') THEN 1 ELSE 0 END) as qualified,
                    SUM(CASE WHEN status = 'converted' THEN 1 ELSE 0 END) as converted
                FROM leads
                WHERE created_at >= ? AND created_at <= ?
            """, (start_str, end_str + ":59")).fetchone()
            conn.close()
            if row:
                stats["leads"]["new_leads"] = row[0] or 0
                stats["leads"]["pitched"] = row[1] or 0
                stats["leads"]["responded"] = row[2] or 0
                stats["leads"]["qualified"] = row[3] or 0
                stats["leads"]["converted"] = row[4] or 0
        except Exception as _e:
            logger.error("读取 leads.db 失败: %s", _e)

    # ── A/B 实验分析 + 模板排行 ──
    ab_section: dict = {}
    try:
        from src.host.ab_testing import get_ab_store as _get_ab_r
        _ab_r = _get_ab_r()
        _ab_variants = _ab_r.analyze("dm_template_style") or {}
        _ab_best = _ab_r.best_variant("dm_template_style", metric="reply_received", min_samples=3)

        # 从 ab_winner.json 读取胜者索引和置信度
        _winner_conf = 0.0
        _winner_idx = None
        try:
            import json as _jab
            _wp = data_file("ab_winner.json")
            if _wp.exists():
                _wd = _jab.loads(_wp.read_text())
                _winner_idx = _wd.get("winner_idx")
        except Exception:
            pass

        # Z-test 置信度
        try:
            from src.ai.template_optimizer import _ztest_winner_confidence
            _conf_map = _ztest_winner_confidence(_ab_variants)
            _winner_conf = _conf_map.get(_ab_best, 0.0)
            for _vn, _conf in _conf_map.items():
                if _vn in _ab_variants:
                    _ab_variants[_vn]["winner_confidence"] = _conf
        except Exception:
            pass

        # 模板排行：按 reply_rate 降序
        _template_rank = sorted(
            [{"variant": v, **d} for v, d in _ab_variants.items()],
            key=lambda x: x.get("reply_rate", 0),
            reverse=True,
        )

        ab_section = {
            "best_variant": _ab_best or "control",
            "winner_confidence": round(_winner_conf, 4),
            "winner_idx": _winner_idx,
            "variants": _ab_variants,
            "template_rank": _template_rank,
        }
    except Exception as _abe:
        logger.debug("A/B 数据读取失败: %s", _abe)
    stats["ab_analysis"] = ab_section

    # ── per-device 转化率（回复率 + 私信数综合评分） ──
    for _dev in stats.get("devices", []):
        _sent = _dev.get("dms_sent", 0)
        _replied = _dev.get("dms_responded", 0)
        _dev["reply_rate"] = round(_replied / _sent, 4) if _sent > 0 else 0.0
        _dev["conversion_score"] = round(
            (_replied * 0.6 + _dev.get("leads_qualified", 0) * 1.5) / max(_sent, 1), 4
        )
    # 同步更新 top_devices（按 conversion_score 重排）
    if stats.get("devices"):
        stats["top_devices"] = sorted(
            stats["devices"],
            key=lambda d: d.get("conversion_score", 0),
            reverse=True,
        )[:5]

    # M2: 生命周期 + 跨设备去重指标 (always collect, not just for AI)
    _lc_section = ""
    _dd_section = ""
    try:
        from src.host.lead_mesh.canonical import (
            get_lifecycle_summary, get_cross_device_dedup_stats)
        _lc = get_lifecycle_summary()
        _lc_s = _lc.get("stages", {})
        _lc_total = _lc.get("total", 0)
        _lc_rates = _lc.get("stage_rates", {})
        _lc_section = f"""
Lead生命周期（全量 {_lc_total} 条）：
- 新建: {_lc_s.get('new',0)} | 已触达: {_lc_s.get('contacted',0)} | 互动中: {_lc_s.get('engaged',0)}
- 合格: {_lc_s.get('qualified',0)} | 已转化: {_lc_s.get('converted',0)} | 流失: {_lc_s.get('lost',0)}
- 触达率: {_lc_rates.get('contacted',0)*100:.1f}% | 互动率: {_lc_rates.get('engaged',0)*100:.1f}% | 转化率: {_lc_rates.get('converted',0)*100:.1f}%"""
        stats["lifecycle"] = _lc

        _dd = get_cross_device_dedup_stats(since_days=period_days)
        _dd_blocks = _dd.get("total_blocks", 0)
        _dd_leads = _dd.get("unique_leads_saved", 0)
        if _dd_blocks > 0:
            _dd_section = f"""
跨设备去重效果（近{period_days}天）：
- 拦截次数: {_dd_blocks} | 保护 Lead 数: {_dd_leads}"""
        stats["cross_device_dedup"] = _dd
    except Exception:
        pass

    # ── AI 生成文字摘要 ──
    if use_ai:
        try:
            from src.ai.llm_client import get_llm_client
            t = stats["totals"]
            l = stats["leads"]
            top_dev_lines = "\n".join(
                f"  - {d['device_id'][:8]}: 私信{d['dms_sent']}条 回复率{d.get('reply_rate',0)*100:.0f}% 转化分{d.get('conversion_score',0):.3f}"
                for d in stats["top_devices"][:3]
            ) or "  无数据"

            _ab_best_v = ab_section.get("best_variant", "未知")
            _ab_conf = ab_section.get("winner_confidence", 0)
            _ab_rank_lines = "\n".join(
                f"  - {r['variant']}: reply_rate={r.get('reply_rate',0)*100:.0f}% (sent={r.get('sent',0)})"
                for r in ab_section.get("template_rank", [])[:3]
            ) or "  暂无数据"

            prompt = f"""你是TikTok运营分析师，根据以下数据撰写运营日报摘要（300字以内，中文，分段落）：

周期：{start_str} 至 {end_str}（共{period_days}天）
设备数量：{len(stats["devices"])}台

行为数据：
- 观看视频：{t['videos_watched']}个
- 关注账号：{t['follows']}个
- 发送私信：{t['dms_sent']}条
- 收到回复：{t['dms_responded']}条（回复率{round(t['dms_responded']/max(t['dms_sent'],1)*100,1)}%）
- 平均算法分：{t['algo_score_avg']}分

线索数据：
- 新增线索：{l['new_leads']}个
- 已发话术：{l['pitched']}个
- 已回复：{l['responded']}个
- 已认定：{l['qualified']}个
- 已转化：{l['converted']}个
{_lc_section}{_dd_section}

A/B话术实验：
- 当前最优变体：{_ab_best_v}（统计置信度 {_ab_conf*100:.0f}%）
- 话术排行：
{_ab_rank_lines}

表现最佳设备（按综合转化分）：
{top_dev_lines}

请从运营效果、私信转化、话术优化、生命周期漏斗四个维度分析，给出下一步建议。"""

            client = get_llm_client()
            ai_text = client.chat(prompt, temperature=0.7, max_tokens=500, use_cache=False)
            stats["ai_summary"] = ai_text.strip()
        except Exception as _e:
            logger.error("AI摘要生成失败: %s", _e)
            stats["ai_summary"] = "AI摘要生成失败，请检查AI配置。"

    stats["ok"] = True
    return stats


@router.get("/cluster-daily-report")
def cluster_daily_report(hours: int = 24):
    """聚合协调器+所有Worker节点的今日运营数据，返回合并报告。"""
    import time as _t, json as _j, concurrent.futures as _cf
    import urllib.request as _ur
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    start_dt = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    end_dt = now.strftime("%Y-%m-%d %H:%M")

    def _fetch_report(base_url: str) -> dict:
        payload = _j.dumps({"start_dt": start_dt, "end_dt": end_dt, "use_ai": False}).encode()
        req = _ur.Request(
            f"{base_url}/analytics/report",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = _ur.urlopen(req, timeout=10)
        return _j.loads(resp.read().decode())

    # 本地报告
    reports = []
    try:
        from src.openclaw_env import local_api_base

        local = _fetch_report(local_api_base())
        if local.get("ok"):
            local["_source"] = "coordinator"
            reports.append(local)
    except Exception:
        pass

    # Worker 报告
    try:
        from ..multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]

        def _fetch_worker_report(h):
            r = _fetch_report(f"http://{h.host_ip}:{h.port}")
            r["_source"] = h.host_id or h.host_ip
            return r

        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_fetch_worker_report, h): h for h in online_hosts}
            for fut in _cf.as_completed(futs, timeout=12):
                try:
                    r = fut.result()
                    if r.get("ok"):
                        reports.append(r)
                except Exception:
                    pass
    except Exception:
        pass

    if not reports:
        return {"ok": False, "error": "无法获取任何节点的报告", "period_hours": hours}

    # 合并统计
    totals = {
        "sessions": 0, "videos_watched": 0, "follows": 0,
        "dms_sent": 0, "dms_responded": 0, "leads_qualified": 0,
    }
    leads = {"new_leads": 0, "pitched": 0, "responded": 0, "qualified": 0, "converted": 0}
    all_devices = []
    sources = []
    ai_summaries = []

    for rpt in reports:
        t = rpt.get("totals") or {}
        for k in totals:
            totals[k] += t.get(k, 0)
        l = rpt.get("leads") or {}
        for k in leads:
            leads[k] += l.get(k, 0)
        all_devices.extend(rpt.get("devices") or [])
        sources.append(rpt.get("_source", "?"))
        s = (rpt.get("ai_summary") or "").strip()
        if s:
            ai_summaries.append(s)

    # 设备效能排行（按 follows+dms_sent 降序）
    all_devices.sort(key=lambda d: (d.get("follows", 0) + d.get("dms_sent", 0)), reverse=True)
    top_devices = all_devices[:10]

    # M2: 附加生命周期 + 跨设备去重指标
    _m2_lifecycle = {}
    _m2_dedup = {}
    try:
        from src.host.lead_mesh.canonical import (
            get_lifecycle_summary, get_cross_device_dedup_stats)
        _m2_lifecycle = get_lifecycle_summary()
        _m2_dedup = get_cross_device_dedup_stats(since_days=max(1, hours // 24))
    except Exception:
        pass

    return {
        "ok": True,
        "period_hours": hours,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "sources": sources,
        "totals": totals,
        "leads": leads,
        "top_devices": top_devices,
        "ai_summary": "\n".join(ai_summaries) if ai_summaries else "",
        "device_count": len(all_devices),
        "lifecycle": _m2_lifecycle,
        "cross_device_dedup": _m2_dedup,
    }


# ---------------------------------------------------------------------------
# P2.1 种子账号效果报告
# ---------------------------------------------------------------------------

@router.get("/seed-quality-report")
def seed_quality_report(country: str = "italy", limit: int = 20):
    """
    P2.1 种子账号效果报告：
    - 从 seed_quality 表读取排名数据
    - 计算整体种子池健康度
    - 给出推荐操作（升权/降权/替换）
    """
    try:
        from ..seed_tracker import get_seed_quality_ranking, get_best_seeds
    except Exception:
        from src.host.seed_tracker import get_seed_quality_ranking, get_best_seeds

    ranking = get_seed_quality_ranking(country, limit=limit)
    best = get_best_seeds(country, limit=5)

    # 计算健康指标
    if ranking:
        avg_hit_rate = sum(r["hit_rate"] for r in ranking) / len(ranking)
        high_quality = [r for r in ranking if r["weight"] >= 2.0]
        mid_quality = [r for r in ranking if r["weight"] == 1.0]
        low_quality = [r for r in ranking if r["weight"] <= 0.3]
    else:
        avg_hit_rate = 0.0
        high_quality = mid_quality = low_quality = []

    # 健康度评分（0-100）
    health_score = min(100, int(
        len(high_quality) * 20 +
        len(mid_quality) * 5 +
        max(0, avg_hit_rate * 200)
    ))

    # 生成操作建议
    suggestions = []
    if not ranking:
        suggestions.append({"type": "warning", "msg": f"暂无{country}种子数据，请先运行smart_follow任务"})
    if len(low_quality) > len(high_quality):
        suggestions.append({"type": "action", "msg": f"建议替换低效种子：{[r['seed'] for r in low_quality[:3]]}"})
    if avg_hit_rate < 0.03:
        suggestions.append({"type": "critical", "msg": f"整体命中率偏低({avg_hit_rate:.1%})，建议扩充种子池"})
    if len(high_quality) >= 3:
        suggestions.append({"type": "good", "msg": f"高质量种子充足：{[r['seed'] for r in high_quality[:3]]}"})

    return {
        "ok": True,
        "country": country,
        "health_score": health_score,
        "avg_hit_rate": round(avg_hit_rate, 4),
        "total_seeds": len(ranking),
        "high_quality": len(high_quality),
        "mid_quality": len(mid_quality),
        "low_quality": len(low_quality),
        "best_seeds": best,
        "ranking": ranking,
        "suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
# P2.2 市场饱和度检测
# ---------------------------------------------------------------------------

@router.get("/market-saturation")
def market_saturation(country: str = "italy", hours: int = 72):
    """
    P2.2 市场饱和度检测：
    - 分析近N小时的follow/reply/referral数据
    - 判断是否达到饱和阈值
    - 给出是否应该扩展新市场的建议
    """
    from datetime import datetime, timedelta
    try:
        from ..database import get_conn
    except Exception:
        from src.host.database import get_conn

    cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 读取近期任务数据（从tasks表，字段为type；排除回收站）
    try:
        from src.host.task_store import _alive_sql

        _aq = _alive_sql()
        with get_conn() as conn:
            # 完成的tiktok_*任务数量
            follow_tasks = conn.execute(
                f"""SELECT COUNT(*) FROM tasks
                   WHERE type LIKE 'tiktok_%' AND status='completed'
                   AND created_at >= ? AND {_aq}""",
                (cutoff,),
            ).fetchone()[0]

            # 失败任务数量
            failed_tasks = conn.execute(
                f"""SELECT COUNT(*) FROM tasks
                   WHERE type LIKE 'tiktok_%' AND status='failed'
                   AND created_at >= ? AND {_aq}""",
                (cutoff,),
            ).fetchone()[0]
    except Exception:
        follow_tasks = failed_tasks = 0

    # 读取种子命中率
    try:
        from ..seed_tracker import get_seed_quality_ranking
    except Exception:
        from src.host.seed_tracker import get_seed_quality_ranking
    ranking = get_seed_quality_ranking(country, limit=10)
    avg_seed_hit = sum(r["hit_rate"] for r in ranking) / len(ranking) if ranking else 0.05

    # 饱和度阈值判断
    SATURATION_THRESHOLDS = {
        "seed_hit_rate": 0.02,      # 种子命中率 < 2% → 饱和信号
        "task_failure_rate": 0.30,  # 任务失败率 > 30% → 竞争激烈
    }

    total_tasks = follow_tasks + failed_tasks
    failure_rate = (failed_tasks / total_tasks) if total_tasks > 0 else 0.0

    saturation_signals = []
    if avg_seed_hit < SATURATION_THRESHOLDS["seed_hit_rate"]:
        saturation_signals.append(f"种子命中率过低({avg_seed_hit:.1%} < {SATURATION_THRESHOLDS['seed_hit_rate']:.0%})")
    if failure_rate > SATURATION_THRESHOLDS["task_failure_rate"]:
        saturation_signals.append(f"任务失败率过高({failure_rate:.1%} > {SATURATION_THRESHOLDS['task_failure_rate']:.0%})")

    is_saturated = len(saturation_signals) >= 2
    saturation_score = min(100, int(
        (1 - avg_seed_hit / 0.1) * 50 +
        failure_rate * 50
    ))

    # 候选市场建议
    ADJACENT_MARKETS = {
        "italy": ["germany", "france", "spain", "portugal"],
        "germany": ["austria", "switzerland", "netherlands"],
        "france": ["belgium", "spain", "luxembourg"],
    }
    candidate_markets = ADJACENT_MARKETS.get(country, [])

    return {
        "ok": True,
        "country": country,
        "period_hours": hours,
        "is_saturated": is_saturated,
        "saturation_score": saturation_score,
        "saturation_signals": saturation_signals,
        "metrics": {
            "avg_seed_hit_rate": round(avg_seed_hit, 4),
            "follow_tasks": follow_tasks,
            "failed_tasks": failed_tasks,
            "failure_rate": round(failure_rate, 4),
        },
        "recommendation": (
            f"建议评估扩展至: {candidate_markets[:2]}" if is_saturated
            else f"{country}市场尚未饱和，继续深耕"
        ),
        "candidate_markets": candidate_markets if is_saturated else [],
    }


# ═══════════════════════════════════════════════════════════
# P3.1 A/B测试实验平台
# ═══════════════════════════════════════════════════════════

# 内存存储：{exp_name: {variant: {impressions, replies, referrals, conversions}}}
_ab_experiments: dict = {}


def _ensure_ab_loaded():
    """从DB重建内存中的_ab_experiments（启动时懒加载）。"""
    if _ab_experiments:
        return  # 已有数据无需重建
    try:
        from ..database import get_conn as _gc
        with _gc() as conn:
            rows = conn.execute(
                "SELECT experiment, variant, event_type FROM ab_events"
            ).fetchall()
        # event_type → 内存key映射（与ab_record_event保持一致：event+"s"，但reply→replies）
        _key_map = {"impression": "impressions", "reply": "replies",
                    "referral": "referrals", "conversion": "conversions"}
        for exp, variant, event_type in rows:
            if exp not in _ab_experiments:
                _ab_experiments[exp] = {}
            if variant not in _ab_experiments[exp]:
                _ab_experiments[exp][variant] = {"impressions": 0, "replies": 0,
                                                   "referrals": 0, "conversions": 0}
            key = _key_map.get(event_type, event_type + "s")
            _ab_experiments[exp][variant][key] = _ab_experiments[exp][variant].get(key, 0) + 1
    except Exception:
        pass


def _get_ab_variant(contact_id: str, experiment: str, variants: list) -> str:
    """基于contact_id哈希固定分配实验变体，保证同一用户始终在同一组。"""
    idx = hash(contact_id) % len(variants)
    return variants[idx]


@router.post("/ab-experiment/record")
def ab_record_event(body: dict):
    """
    P3.1 记录A/B实验事件。
    body: {experiment, contact_id, variant, event_type}
    event_type: impression | reply | referral | conversion
    """
    _ensure_ab_loaded()
    exp = body.get("experiment", "")
    contact = body.get("contact_id", "")
    variant = body.get("variant", "")
    event = body.get("event_type", "impression")
    if not exp or not variant:
        raise HTTPException(400, "experiment and variant required")
    if exp not in _ab_experiments:
        _ab_experiments[exp] = {}
    if variant not in _ab_experiments[exp]:
        _ab_experiments[exp][variant] = {"impressions": 0, "replies": 0,
                                          "referrals": 0, "conversions": 0}
    _ab_experiments[exp][variant][event + "s"] = (
        _ab_experiments[exp][variant].get(event + "s", 0) + 1
    )
    # Fix-3: 持久化到DB
    try:
        from ..database import get_conn as _gc_ab
        contact_id = body.get("contact_id", "")
        _ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with _gc_ab() as _conn_ab:
            _conn_ab.execute(
                "INSERT INTO ab_events (experiment, variant, event_type, contact_id, ts) VALUES (?,?,?,?,?)",
                (exp, variant, event, contact_id, _ts)
            )
    except Exception:
        pass
    return {"ok": True, "experiment": exp, "variant": variant, "event": event}


@router.get("/ab-experiment/{experiment}/report")
def ab_experiment_report(experiment: str):
    """
    P3.1 获取A/B实验报告：各变体的回复率/引流率/转化率，自动判断winner。
    """
    _ensure_ab_loaded()
    data = _ab_experiments.get(experiment, {})
    if not data:
        return {"ok": False, "error": "实验不存在或暂无数据", "experiment": experiment}

    results = []
    for variant, counts in data.items():
        impressions = counts.get("impressions", 0)
        replies = counts.get("replies", 0)
        referrals = counts.get("referrals", 0)
        conversions = counts.get("conversions", 0)
        reply_rate = replies / impressions if impressions > 0 else 0.0
        referral_rate = referrals / max(replies, 1)
        conversion_rate = conversions / max(impressions, 1)
        score = reply_rate * 0.3 + referral_rate * 0.5 + conversion_rate * 0.2
        results.append({
            "variant": variant, "impressions": impressions,
            "replies": replies, "referrals": referrals, "conversions": conversions,
            "reply_rate": round(reply_rate, 4),
            "referral_rate": round(referral_rate, 4),
            "conversion_rate": round(conversion_rate, 4),
            "composite_score": round(score, 4),
        })

    results.sort(key=lambda x: x["composite_score"], reverse=True)
    winner = results[0]["variant"] if results else None

    return {
        "ok": True, "experiment": experiment,
        "winner": winner,
        "winner_score": results[0]["composite_score"] if results else 0,
        "variants": results,
        "recommendation": f"建议将 '{winner}' 设为默认策略" if winner else "数据不足",
    }


@router.get("/ab-experiment/list")
def ab_experiment_list():
    """列出所有A/B实验。"""
    return {
        "experiments": list(_ab_experiments.keys()),
        "count": len(_ab_experiments),
    }


@router.post("/ab-experiment/assign")
def ab_assign_variant(body: dict):
    """
    P3.1 为联系人分配实验变体（幂等，同一contact始终同一variant）。
    body: {contact_id, experiment, variants}
    """
    contact = body.get("contact_id", "")
    experiment = body.get("experiment", "")
    variants = body.get("variants", ["A", "B"])
    if not contact or not experiment:
        raise HTTPException(400, "contact_id and experiment required")
    variant = _get_ab_variant(contact, experiment, variants)
    return {"ok": True, "contact_id": contact, "experiment": experiment,
            "variant": variant, "variants": variants}


# ═══════════════════════════════════════════════════════════
# P3.2 全链路AI质量评估
# ═══════════════════════════════════════════════════════════

@router.get("/funnel-analysis")
def funnel_analysis(country: str = "italy", hours: int = 24):
    """
    P3.2 全链路引流漏斗分析：follow→reply→referral→TG跟进。
    从tasks表+seed_quality表聚合，生成可操作建议。
    """
    from datetime import datetime, timedelta
    try:
        from ..database import get_conn
    except Exception:
        from src.host.database import get_conn

    cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        from src.host.task_store import _alive_sql

        _aq = _alive_sql()
        with get_conn() as conn:
            # 关注任务完成数
            follow_ok = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE type LIKE 'tiktok_follow%' AND status='completed' "
                f"AND created_at>=? AND {_aq}",
                (cutoff,),
            ).fetchone()[0]
            # 收件箱任务（代表对话处理量）
            inbox_ok = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE type='tiktok_check_inbox' AND status='completed' "
                f"AND created_at>=? AND {_aq}",
                (cutoff,),
            ).fetchone()[0]
            # 失败任务
            failed = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE type LIKE 'tiktok_%' AND status='failed' "
                f"AND created_at>=? AND {_aq}",
                (cutoff,),
            ).fetchone()[0]
    except Exception:
        follow_ok = inbox_ok = failed = 0

    # 从seed_quality读取回复/引流数
    try:
        from ..seed_tracker import get_seed_quality_ranking
    except Exception:
        from src.host.seed_tracker import get_seed_quality_ranking
    seeds = get_seed_quality_ranking(country, limit=20)
    total_follows = sum(s["follows"] for s in seeds)
    total_replies = sum(s["replies"] for s in seeds)
    total_referrals = sum(s["referrals"] for s in seeds)
    total_conversions = sum(s["conversions"] for s in seeds)

    # 漏斗比率
    follow_to_reply = total_replies / total_follows if total_follows > 0 else 0
    reply_to_referral = total_referrals / total_replies if total_replies > 0 else 0
    referral_to_conv = total_conversions / total_referrals if total_referrals > 0 else 0

    # 生成建议
    recommendations = []
    if follow_to_reply < 0.05:
        recommendations.append({"priority": "high", "issue": "关注→回复率过低",
                                  "value": f"{follow_to_reply:.1%}",
                                  "action": "检查种子账号质量，调用/analytics/seed-quality-report"})
    if reply_to_referral < 0.15:
        recommendations.append({"priority": "high", "issue": "回复→引流率过低",
                                  "value": f"{reply_to_referral:.1%}",
                                  "action": "检查引流触发时机，考虑降低inbound_count触发阈值"})
    if reply_to_referral > 0.40:
        recommendations.append({"priority": "good", "issue": "引流率良好",
                                  "value": f"{reply_to_referral:.1%}",
                                  "action": "保持当前策略"})
    if failed > follow_ok:
        recommendations.append({"priority": "critical", "issue": "任务失败率高于成功率",
                                  "value": f"失败{failed}/成功{follow_ok}",
                                  "action": "检查设备状态和VPN连接"})

    return {
        "ok": True,
        "country": country,
        "period_hours": hours,
        "funnel": {
            "follows": total_follows,
            "replies": total_replies,
            "referrals": total_referrals,
            "conversions": total_conversions,
        },
        "rates": {
            "follow_to_reply": round(follow_to_reply, 4),
            "reply_to_referral": round(reply_to_referral, 4),
            "referral_to_conversion": round(referral_to_conv, 4),
        },
        "task_stats": {
            "follow_tasks_ok": follow_ok,
            "inbox_tasks_ok": inbox_ok,
            "failed_tasks": failed,
        },
        "recommendations": recommendations,
        "ab_experiments": list(_ab_experiments.keys()),
    }
