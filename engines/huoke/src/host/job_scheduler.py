# -*- coding: utf-8 -*-
"""定时任务调度器 — Cron 匹配 + 定期执行。"""
import json
import logging
import os
import time

from .device_registry import DEFAULT_DEVICES_YAML, config_file

logger = logging.getLogger(__name__)

from src.openclaw_env import local_api_base


def _local(path: str) -> str:
    return local_api_base() + path

_scheduled_jobs_path = config_file("scheduled_jobs.json")


def load_scheduled_jobs() -> list:
    if _scheduled_jobs_path.exists():
        with open(_scheduled_jobs_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_scheduled_jobs(data: list):
    _scheduled_jobs_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_scheduled_jobs_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_ALERT_CD_FILE = os.path.join(os.path.dirname(__file__), ".alert_cooldown.json")


def _alert_cooldown_ok(alert_key: str, days: int = 3) -> bool:
    """U4: 检查 alert_key 是否在 cooldown 内. 如果不在, 标记并返回 True."""
    import time as _t
    now = _t.time()
    cooldown_sec = days * 86400
    data = {}
    try:
        if os.path.exists(_ALERT_CD_FILE):
            with open(_ALERT_CD_FILE, "r") as f:
                data = json.load(f)
    except Exception:
        data = {}
    last = data.get(alert_key, 0)
    if now - last < cooldown_sec:
        return False  # 仍在 cooldown 内
    data[alert_key] = now
    try:
        with open(_ALERT_CD_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass
    return True


def execute_scheduled_action(job: dict) -> dict:
    """Execute the action defined in a scheduled job."""
    action = job.get("action", "")
    params = job.get("params", {})
    _config_path = DEFAULT_DEVICES_YAML
    try:
        if action == "batch_text":
            import requests
            r = requests.post(_local("/batch/text-input"),
                              json=params, timeout=30)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "batch_quick_action":
            import requests
            r = requests.post(_local("/batch/quick-action"),
                              json=params, timeout=30)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "batch_app_action":
            import requests
            r = requests.post(_local("/batch/app-action"),
                              json=params, timeout=30)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "execute_script":
            import requests
            r = requests.post(_local("/scripts/execute"),
                              json=params, timeout=60)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "deploy_wallpapers":
            import requests
            r = requests.post(_local("/devices/wallpaper/all"),
                              json={}, timeout=60)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "tiktok_daily_campaign":
            import requests
            country = params.get("country", "italy")
            max_devices = params.get("max_devices", 17)
            r = requests.post(_local("/tiktok/start-daily-campaign"),
                              json={"country": country, "max_devices": max_devices},
                              timeout=30)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "tiktok_sync_leads":
            # W03 线索同步到本地 leads.db（供 followup 任务使用）
            import requests
            r = requests.post(_local("/tiktok/sync-w03-leads"),
                              json=params, timeout=30)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "leads_merge_duplicates":
            # 跨设备线索自动去重合并（安全操作：保留主线索，迁移互动记录）
            import requests
            r = requests.post(_local("/tiktok/leads/merge-duplicates"),
                              json={"platform": params.get("platform", "tiktok")},
                              timeout=30)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action in ("tiktok_ai_rescore", "tiktok_ai_restore"):
            # 与 POST /tiktok/leads/ai-rescore 同源，仅 HTTP 路由侧一次调用；不按设备建任务
            from src.host.routers.tiktok import run_ai_rescore_leads_scheduled

            return run_ai_rescore_leads_scheduled({
                "limit": params.get("limit", 30),
                "platform": params.get("platform", "tiktok"),
                "dry_run": bool(params.get("dry_run", False)),
            })
        elif action.startswith("tiktok_") or action.startswith("telegram_") or \
             action.startswith("whatsapp_") or action.startswith("facebook_"):
            # 策略：关闭无人值守自动查收件箱（仍允许控制台手动批量）
            if action == "tiktok_check_inbox":
                try:
                    from src.host.task_policy import policy_blocks_auto_tiktok_check_inbox
                    if policy_blocks_auto_tiktok_check_inbox():
                        return {"skipped": True, "reason": "disable_auto_tiktok_check_inbox"}
                except Exception:
                    pass
            # Fix-5: 活跃时间窗口 — check_inbox仅在意大利时区 9:00-22:00 执行
            if action == "tiktok_check_inbox":
                try:
                    from datetime import datetime, timezone, timedelta
                    # CET = UTC+1, CEST = UTC+2; 保守使用 UTC+1
                    italy_offset = timedelta(hours=1)
                    now_italy = datetime.now(timezone.utc).astimezone(timezone(italy_offset))
                    current_hour = now_italy.hour
                    if not (9 <= current_hour < 22):
                        import logging as _log
                        _log.getLogger(__name__).info(
                            "[定时任务] tiktok_check_inbox 跳过: 当前意大利时间 %02d:%02d (活跃窗口 09-22)",
                            current_hour, now_italy.minute)
                        return {"skipped": True, "reason": "outside_active_hours",
                                "italy_hour": current_hour}
                except Exception:
                    pass  # 时区检查失败则正常执行

            from src.device_control.device_manager import get_device_manager
            from src.host.routers.tasks import run_batch_tasks_core

            manager = get_device_manager(_config_path)
            online = [d.device_id for d in manager.get_all_devices() if d.is_online]
            if not online:
                return {"created": 0, "devices": 0, "skipped": True, "reason": "no_online_devices"}
            try:
                r = run_batch_tasks_core({
                    "type": action,
                    "device_ids": online,
                    "params": params,
                })
            except ValueError as ve:
                return {"error": str(ve), "created": 0}
            return {
                "created": r.get("count", 0),
                "batch_id": r.get("batch_id"),
                "task_ids": r.get("task_ids"),
                "devices": len(online),
                "via": "tasks/batch",
            }
        elif action == "vpn_rotate":
            import requests
            strategy = params.get("strategy", "round-robin")
            apply_now = params.get("apply", True)
            r = requests.post(_local("/vpn/pool/rotate"),
                              json={"strategy": strategy, "apply": apply_now},
                              timeout=120)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "vpn_health_check":
            # 检查所有设备 VPN 状态，断开的自动静默重连
            from src.device_control.device_manager import get_device_manager
            from src.behavior.vpn_manager import check_vpn_status, reconnect_vpn_silent
            mgr = get_device_manager(_config_path)
            devices = [d.device_id for d in mgr.get_all_devices() if d.is_online]
            reconnected = 0
            for did in devices:
                s = check_vpn_status(did)
                if not s.connected:
                    ok = reconnect_vpn_silent(did)
                    if ok:
                        reconnected += 1
                        logger.info("[VPN健康] %s: 静默重连成功", did[:8])
                    else:
                        logger.warning("[VPN健康] %s: 重连失败", did[:8])
            return {"checked": len(devices), "reconnected": reconnected}
        elif action == "purge_old_tasks":
            from .task_store import purge_old_tasks as _purge
            days = params.get("days", 7)
            count = _purge(days=days)
            return {"purged": count, "days": days}
        elif action == "cross_interact_all":
            import requests
            r = requests.post(_local("/tiktok/cross-interact-all"),
                              json=params, timeout=30)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "cross_follow_all":
            import requests
            r = requests.post(_local("/tiktok/cross-follow-all"),
                              json=params, timeout=30)
            return r.json() if r.ok else {"error": r.text[:200]}
        elif action == "geo_check_all":
            # P5: 定期 IP 地理位置验证 — 全设备扫描，IP 不在目标国则告警+尝试重连
            from src.device_control.device_manager import get_device_manager
            from src.behavior.geo_check import check_device_geo
            from src.behavior.vpn_manager import reconnect_vpn_silent
            expected_country = params.get("country", "italy")
            mgr = get_device_manager(_config_path)
            devices = [d.device_id for d in mgr.get_all_devices() if d.is_online]
            results = []
            for did in devices:
                try:
                    geo = check_device_geo(did, expected_country, mgr)
                    if geo.error:
                        results.append({"device": did[:8], "status": "skipped", "reason": geo.error})
                        continue
                    if geo.matches:
                        results.append({"device": did[:8], "status": "ok",
                                        "country": geo.detected_country, "ip": geo.public_ip})
                    else:
                        logger.warning("[GEO巡检] %s: 国家不匹配 期望=%s 实际=%s IP=%s，尝试重连 VPN",
                                       did[:8], expected_country, geo.detected_country, geo.public_ip)
                        reconnected = reconnect_vpn_silent(did)
                        results.append({"device": did[:8], "status": "mismatch",
                                        "country": geo.detected_country, "ip": geo.public_ip,
                                        "reconnected": reconnected})
                        try:
                            from src.host.event_stream import push_event
                            push_event("vpn.geo_mismatch", {
                                "device_id": did,
                                "expected": expected_country,
                                "detected": geo.detected_country,
                                "ip": geo.public_ip,
                                "reconnected": reconnected,
                            }, "")
                        except Exception:
                            pass
                except Exception as e:
                    results.append({"device": did[:8], "status": "error", "reason": str(e)})
            ok_count = sum(1 for r in results if r["status"] == "ok")
            bad_count = sum(1 for r in results if r["status"] == "mismatch")
            return {"checked": len(devices), "ok": ok_count, "mismatch": bad_count,
                    "results": results, "expected_country": expected_country}
        elif action == "analytics_daily_report":
            # 自动生成今日运营日报（每天 23:50 触发）
            import urllib.request as _ur, json as _json
            from datetime import date, timedelta
            period = params.get("period", "today")
            use_ai = params.get("use_ai", True)
            notify_ws = params.get("notify_ws", True)
            today = date.today()
            if period == "today":
                start_dt = today.strftime("%Y-%m-%d 00:00")
                end_dt = today.strftime("%Y-%m-%d 23:59")
            elif period == "yesterday":
                y = today - timedelta(days=1)
                start_dt = y.strftime("%Y-%m-%d 00:00")
                end_dt = y.strftime("%Y-%m-%d 23:59")
            else:
                start_dt = today.strftime("%Y-%m-%d 00:00")
                end_dt = today.strftime("%Y-%m-%d 23:59")
            req_body = _json.dumps({"start_dt": start_dt, "end_dt": end_dt, "use_ai": use_ai}).encode()
            req = _ur.Request(
                _local("/analytics/report"),
                data=req_body,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            resp = _ur.urlopen(req, timeout=60)
            report = _json.loads(resp.read().decode())
            if notify_ws and report.get("ok"):
                try:
                    from src.host.event_stream import push_event
                    push_event("analytics.daily_report_ready", {
                        "date": today.strftime("%Y-%m-%d"),
                        "totals": report.get("totals", {}),
                        "leads": report.get("leads", {}),
                        "ai_summary": (report.get("ai_summary") or "")[:500],
                    }, "")
                except Exception:
                    pass
            logger.info("[日报] 已生成 %s 运营日报，私信=%d 线索=%d",
                     today.strftime("%m/%d"),
                     (report.get("totals") or {}).get("dms_sent", 0),
                     (report.get("leads") or {}).get("new_leads", 0))
            # Push daily report to Telegram if configured (W1: 增强版)
            if report.get("ok"):
                try:
                    from src.host.alert_notifier import AlertNotifier
                    extra = []
                    # W1: top-5 高分 leads
                    try:
                        from src.host.lead_mesh.canonical import get_score_leaderboard
                        lb = get_score_leaderboard(top_n=5)
                        if lb.get("top"):
                            top_lines = []
                            for i, l in enumerate(lb["top"][:5]):
                                top_lines.append(
                                    f"  {i+1}. {l['primary_name'] or l['canonical_id'][:8]}"
                                    f" ⭐{l['lead_score']} ({l['lifecycle_stage']})")
                            extra.append(
                                f"\n🏆 <b>Top leads</b> (avg {lb.get('avg_score',0)}):\n"
                                + "\n".join(top_lines))
                    except Exception:
                        pass
                    # W1: lifecycle 分布摘要
                    try:
                        from src.host.lead_mesh.canonical import get_lifecycle_summary
                        lcs = get_lifecycle_summary()
                        if lcs:
                            dist_parts = []
                            for s in ("new","contacted","engaged","qualified","converted","lost"):
                                n = lcs.get(s, 0)
                                if n > 0:
                                    dist_parts.append(f"{s}:{n}")
                            if dist_parts:
                                extra.append(f"📈 Funnel: {' · '.join(dist_parts)}")
                    except Exception:
                        pass
                    AlertNotifier.get().notify_daily_report(
                        date_str=today.strftime("%Y-%m-%d"),
                        totals=report.get("totals") or {},
                        leads=report.get("leads") or {},
                        ai_summary=(report.get("ai_summary") or "")[:200],
                        extra_sections=extra if extra else None,
                    )
                except Exception:
                    pass
            # O1: 漏斗瓶颈自动告警 (搭便车日报触发, U4: 3天 cooldown)
            try:
                from src.host.lead_mesh.canonical import check_lifecycle_alerts
                from src.host.alert_notifier import AlertNotifier
                lc_alerts = check_lifecycle_alerts()
                if lc_alerts and _alert_cooldown_ok("lifecycle_funnel_alert", days=3):
                    body_lines = [a["message"] for a in lc_alerts]
                    AlertNotifier.get().notify_event(
                        event_type="lifecycle_funnel_alert",
                        title="🔄 生命周期漏斗告警",
                        body="\n".join(body_lines),
                        level="warning",
                    )
                    logger.info("[日报] 漏斗告警 %d 条: %s",
                                len(lc_alerts), "; ".join(body_lines))
            except Exception as _la_e:
                logger.debug("[日报] lifecycle alert 检查失败: %s", _la_e)
            # Q4: 自动流失标记 (30天无活动)
            try:
                from src.host.lead_mesh.canonical import auto_mark_lost_leads
                lost_result = auto_mark_lost_leads(inactive_days=30, dry_run=False)
                if lost_result.get("marked_lost", 0) > 0:
                    logger.info("[日报] 自动标记流失 %d 条 lead",
                                lost_result["marked_lost"])
            except Exception as _al_e:
                logger.debug("[日报] auto_mark_lost 失败: %s", _al_e)
            # R4: SLA 超时告警 (U4: 3天 cooldown)
            try:
                from src.host.lead_mesh.canonical import check_lifecycle_sla
                from src.host.alert_notifier import AlertNotifier
                sla_result = check_lifecycle_sla(limit=20)
                cnt = sla_result.get("at_risk_count", 0)
                if cnt > 0 and _alert_cooldown_ok("lifecycle_sla_breach", days=3):
                    top3 = sla_result["leads"][:3]
                    lines = [f"{l['primary_name'] or l['canonical_id'][:8]}({l['stage']}) {l['dwell_days']}天" for l in top3]
                    AlertNotifier.get().notify_event(
                        event_type="lifecycle_sla_breach",
                        title=f"⏰ {cnt} 条 lead 超 SLA",
                        body="\n".join(lines) + (f"\n… 及其他 {cnt-3} 条" if cnt > 3 else ""),
                        level="warning",
                    )
                    logger.info("[日报] SLA 超时 %d 条 lead", cnt)
            except Exception as _sla_e:
                logger.debug("[日报] lifecycle SLA 检查失败: %s", _sla_e)
            # S1: 批量重算 lead_score
            try:
                from src.host.lead_mesh.canonical import batch_recompute_scores
                score_result = batch_recompute_scores(limit=2000)
                logger.info("[日报] lead_score 重算 %d 条, 平均 %s",
                            score_result.get("computed", 0),
                            score_result.get("avg_score", "?"))
            except Exception as _sc_e:
                logger.debug("[日报] score recompute 失败: %s", _sc_e)
            return {"ok": True, "date": today.strftime("%Y-%m-%d"),
                    "dms_sent": (report.get("totals") or {}).get("dms_sent", 0),
                    "new_leads": (report.get("leads") or {}).get("new_leads", 0)}
        elif action == "vpn_keepalive":
            # MIUI 保活：确保 V2RayNG 前台服务在运行并已连接
            import time as _time
            from src.device_control.device_manager import get_device_manager
            from src.behavior.vpn_manager import check_vpn_status, _toggle_vpn
            mgr = get_device_manager(_config_path)
            devices = [d.device_id for d in mgr.get_all_devices() if d.is_online]
            kept = 0
            toggled = 0
            for did in devices:
                # 1. 检查 VPN 是否已连接
                s = check_vpn_status(did)
                if s.connected:
                    continue
                # 2. V2RayNG 进程是否在运行
                ok, out = mgr.execute_adb_command("shell pidof com.v2ray.ang", did)
                if not ok or not (out or "").strip():
                    # V2RayNG 被 MIUI 杀了，重启它
                    mgr.execute_adb_command(
                        "shell am startservice -n com.v2ray.ang/.service.V2RayVpnService", did)
                    logger.info("[VPN保活] %s: 重启 V2RayNG 服务", did[:8])
                    kept += 1
                    _time.sleep(3)
                # 3. Toggle VPN on
                _toggle_vpn(did)
                toggled += 1
                logger.info("[VPN保活] %s: 已触发 VPN 连接", did[:8])
            return {"checked": len(devices), "restarted": kept, "toggled": toggled}
        else:
            return {"error": f"Unknown action: {action}"}
    except Exception as e:
        return {"error": str(e)}


_scheduler_thread_started = False
_scheduler_stop_event = None  # threading.Event 延迟初始化 (避免 import time 占用)
_scheduler_thread = None


def start_job_scheduler():
    """Background thread checking cron jobs every 30 seconds."""
    global _scheduler_thread_started, _scheduler_stop_event, _scheduler_thread
    if _scheduler_thread_started:
        return
    _scheduler_thread_started = True
    import threading
    if _scheduler_stop_event is None:
        _scheduler_stop_event = threading.Event()
    else:
        _scheduler_stop_event.clear()

    def _cron_matches(cron_expr: str) -> bool:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        now = time.localtime()
        fields = [now.tm_min, now.tm_hour, now.tm_mday, now.tm_mon, now.tm_wday]
        for expr_part, current in zip(parts, fields):
            if expr_part == "*":
                continue
            if expr_part.startswith("*/"):
                step = int(expr_part[2:])
                if current % step != 0:
                    return False
            elif "," in expr_part:
                if str(current) not in expr_part.split(","):
                    return False
            elif "-" in expr_part:
                lo, hi = expr_part.split("-", 1)
                if not (int(lo) <= current <= int(hi)):
                    return False
            elif int(expr_part) != current:
                return False
        return True

    _analytics_tick = [0]

    def _loop():
        logger.info("Scheduled job runner started")
        # Lazy import to avoid circular dependency
        from .analytics_store import load_analytics_history, record_analytics_snapshot
        load_analytics_history()
        # 2026-05-05 H.2-followup: while True → stop_event 可控
        while not _scheduler_stop_event.is_set():
            try:
                jobs = load_scheduled_jobs()
                try:
                    from src.host.task_policy import policy_blocks_json_scheduled_jobs
                    _skip_json = policy_blocks_json_scheduled_jobs()
                except Exception:
                    _skip_json = False
                if not _skip_json:
                    for job in jobs:
                        if not job.get("enabled", True):
                            continue
                        cron = job.get("cron", "")
                        if not cron:
                            continue
                        if _cron_matches(cron):
                            last = job.get("last_run", "")
                            now_str = time.strftime("%Y-%m-%d %H:%M")
                            if last and last.startswith(now_str):
                                continue
                            logger.info("Running scheduled job: %s", job.get("name", ""))
                            execute_scheduled_action(job)
                            job["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
                            save_scheduled_jobs(jobs)
            except Exception as e:
                logger.warning("Scheduler error: %s", e)
            _analytics_tick[0] += 1
            if _analytics_tick[0] % 10 == 0:
                try:
                    record_analytics_snapshot()
                except Exception:
                    pass
            # 每 2880 次 tick (=约24小时) 自动清理旧任务
            if _analytics_tick[0] % 2880 == 0:
                try:
                    from src.host.task_store import purge_old_tasks
                    deleted = purge_old_tasks(days=7)
                    if deleted:
                        logger.info("Auto-purge: deleted %d old tasks", deleted)
                except Exception as e:
                    logger.debug("Auto-purge failed: %s", e)
            # 每 2160 次 tick (=约18小时，凌晨3点左右) 自动优化话术权重
            if _analytics_tick[0] % 2160 == 0 and _analytics_tick[0] > 0:
                try:
                    from src.ai.template_optimizer import optimize_template_weights
                    result = optimize_template_weights()
                    logger.info("话术权重优化完成: %s", result)
                except Exception as e:
                    logger.debug("话术权重优化失败: %s", e)

            # Phase 5.5 新增: Lead Mesh 定时任务 ────────────────────
            # 每 tick (30s) 刷新 pending webhook, 有到期的就发
            try:
                from src.host.lead_mesh.webhook_dispatcher import flush_pending_webhooks
                stats = flush_pending_webhooks(max_batch=50)
                if stats.get("delivered", 0) > 0 or stats.get("dead_letter", 0) > 0:
                    logger.info("[lead_mesh/webhooks] flush stats=%s", stats)
            except Exception as e:
                logger.debug("[lead_mesh/webhooks] flush 失败: %s", e)

            # 每 720 次 tick (=6 小时) 过期 72h 未 ack 的 handoffs
            if _analytics_tick[0] % 720 == 0 and _analytics_tick[0] > 0:
                try:
                    from src.host.lead_mesh.handoff import expire_pending_handoffs
                    n = expire_pending_handoffs(expire_hours=72)
                    if n > 0:
                        logger.info("[lead_mesh/handoffs] 自动过期 %s 个 pending", n)
                except Exception as e:
                    logger.debug("[lead_mesh/handoffs] 过期任务失败: %s", e)

            # 每 2880 次 tick (=24 小时) 清理旧 agent_messages (已 ack/failed 超 30 天)
            if _analytics_tick[0] % 2880 == 0 and _analytics_tick[0] > 0:
                try:
                    from src.host.lead_mesh.agent_mesh import cleanup_old_messages
                    n = cleanup_old_messages(older_than_days=30)
                    if n > 0:
                        logger.info("[lead_mesh/mesh] 清理旧消息 %s 条", n)
                except Exception as e:
                    logger.debug("[lead_mesh/mesh] cleanup 失败: %s", e)

            # 2026-05-05 H.2-followup: time.sleep(30) → stop_event.wait(30)
            # 让 stop_job_scheduler 立即唤醒 thread 退出
            _scheduler_stop_event.wait(30)

    t = threading.Thread(target=_loop, daemon=True, name="job-scheduler")
    t.start()
    _scheduler_thread = t


def stop_job_scheduler(timeout_sec: float = 5.0) -> bool:
    """优雅停止 job_scheduler thread (Stage H.2-followup).

    设 _stop_event 让 _loop 下次 wait 立即唤醒退出. join thread.
    Returns True 如果在 timeout 内退出.
    """
    global _scheduler_thread_started, _scheduler_thread
    if _scheduler_stop_event is not None:
        _scheduler_stop_event.set()
    t = _scheduler_thread
    _scheduler_thread = None
    _scheduler_thread_started = False
    if t is None:
        return True
    t.join(timeout=timeout_sec)
    return not t.is_alive()


# ---------------------------------------------------------------------------
# System config files list (used by routers/system.py)
# ---------------------------------------------------------------------------

CONFIG_FILES = [
    "config/devices.yaml",
    "config/cluster.yaml",
    "config/task_execution_policy.yaml",
    "config/device_aliases.json",
    "config/phrases.json",
    "config/scheduled_jobs.json",
    "config/device_assets.json",
    "config/users.json",
    "config/visual_workflows.json",
    "config/analytics_history.json",
]
