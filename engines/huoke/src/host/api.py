# -*- coding: utf-8 -*-
"""OpenClaw Host API — core framework.

All endpoint logic lives in ``routers/*`` sub-modules.  This file keeps only:
  - app creation & lifespan
  - middleware (CORS, GZip, rate-limit, security-headers, audit)
  - router registration
  - optional ``GET /dashboard/core-aggregate`` (legacy JSON bundle; SPA 见 ``dashboard`` 路由)
"""
import os
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Request

from .device_registry import DEFAULT_DEVICES_YAML, PROJECT_ROOT, config_file

from src.device_control.device_manager import get_device_manager
from src.behavior.compliance_guard import get_compliance_guard

from . import task_store
from .database import init_db
from .schemas import TaskCreate, TaskResponse, TaskResultReport, TaskStatus, TaskType, DeviceListItem
from .executor import run_task, _resolve_serial_from_config, _get_device_id
from .worker_pool import get_worker_pool, shutdown_pool
from . import scheduler
from .health_monitor import start_monitor, stop_monitor, metrics

# Extracted modules
from .job_scheduler import start_job_scheduler, load_scheduled_jobs, save_scheduled_jobs, execute_scheduled_action, CONFIG_FILES
from .analytics_store import load_analytics_history, record_analytics_snapshot, get_analytics_cache, range_to_count
from .notification_center import send_notification  # backward compat — other modules may import from here
from .audit_helpers import record_audit_log, audit, save_config_snapshot, _audit_log, _audit_lock, _config_history, _config_history_lock

logger = logging.getLogger(__name__)


def _device_health_monitor_enabled() -> bool:
    """单测/冒烟可设 ``OPENCLAW_DISABLE_DEVICE_HEALTH_MONITOR=1`` 跳过 HealthMonitor 线程，避免 pytest 收尾时日志流已关仍写后台线程。"""
    return os.environ.get(
        "OPENCLAW_DISABLE_DEVICE_HEALTH_MONITOR", ""
    ).strip().lower() not in ("1", "true", "yes")


def _auto_wallpaper_background_thread_enabled() -> bool:
    """单测可设 ``OPENCLAW_DISABLE_AUTO_WALLPAPER_THREAD=1`` 跳过启动时壁纸自动编号守护线程（与 task_policy 关闭并行）。"""
    return os.environ.get(
        "OPENCLAW_DISABLE_AUTO_WALLPAPER_THREAD", ""
    ).strip().lower() not in ("1", "true", "yes")
_project_root = PROJECT_ROOT
_config_path = DEFAULT_DEVICES_YAML

# ── Backward-compatible aliases (used by routers via ``from ..api import ...``) ──
_audit = audit
_record_audit_log = record_audit_log
_save_config_snapshot = save_config_snapshot
_load_scheduled_jobs = load_scheduled_jobs
_save_scheduled_jobs = save_scheduled_jobs
_execute_scheduled_action = execute_scheduled_action
_CONFIG_FILES = CONFIG_FILES
_load_analytics_history = load_analytics_history
_record_analytics_snapshot = record_analytics_snapshot
_analytics_cache = get_analytics_cache()
_range_to_count = range_to_count

# ── 被多个 router 通过 from ..api import 引用的辅助函数 ──

def _resolve_device_with_manager(device_id: str):
    """Resolve device_id (alias or serial) to actual serial and return (serial, manager)."""
    manager = get_device_manager(_config_path)
    info = manager.get_device_info(device_id)
    if info:
        return device_id, manager
    serial = _resolve_serial_from_config(_config_path, device_id)
    info = manager.get_device_info(serial)
    if not info:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "device_not_on_host",
                "message": "设备不存在",
                "hint": "当前节点未通过 ADB 识别该序列号；若在 Worker 上请确认转发与集群别名。",
            },
        )
    return serial, manager


def _load_config(path: str) -> dict:
    """Load a YAML config file and return as dict."""
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_device(device_id: str) -> str:
    manager = get_device_manager(_config_path)
    manager.discover_devices()
    resolved = _get_device_id(manager, device_id, _config_path)
    if not resolved:
        raise HTTPException(status_code=404, detail="无可用设备")
    return resolved


# ── API 鉴权 (定义已移至 routers/auth.py) ──
from .routers.auth import verify_api_key, _active_sessions  # noqa: E402


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    from src.utils.log_config import setup_logging
    setup_logging()
    init_db()
    # Phase 11 (2026-04-25): LINE pool seed — 空池时注入 config/line_pool_seed.yaml 默认账号
    try:
        from src.host.line_pool import seed_from_config
        res = seed_from_config()
        if not res.get("skipped"):
            logger.info("[line_pool.seed] inserted=%s duplicate=%s invalid=%s",
                         res.get("inserted"), res.get("duplicate"),
                         res.get("invalid"))
    except Exception as e:
        logger.debug("[line_pool.seed] 失败 (忽略): %s", e)
    get_worker_pool()
    try:
        from src.host.task_policy import load_task_execution_policy, policy_blocks_db_scheduler
        load_task_execution_policy()
        if not policy_blocks_db_scheduler():
            scheduler.start_scheduler(_config_path)
        else:
            logger.info("数据库定时调度已按 task_execution_policy 跳过启动")
    except Exception as e:
        logger.debug("任务策略加载异常，仍启动调度器: %s", e)
        scheduler.start_scheduler(_config_path)
    if _device_health_monitor_enabled():
        start_monitor(_config_path)
    else:
        logger.info("HealthMonitor 未启动（OPENCLAW_DISABLE_DEVICE_HEALTH_MONITOR）")
    try:
        from src.device_control.watchdog import get_watchdog
        from src.device_control.device_manager import get_device_manager as _get_dm
        wd = get_watchdog()
        _dm = _get_dm(_config_path)
        _connected = _dm.get_connected_devices()
        for dev in _connected:
            wd.watch(dev.device_id)
        wd.start()
        logger.info("Watchdog 自动启动，监控 %d 台已连接设备 (共 %d 台配置)",
                     len(_connected), len(_dm.get_all_devices()))
    except Exception as e:
        logger.debug("Watchdog auto-start deferred: %s", e)
    try:
        from src.workflow.tiktok_escalation import register_tiktok_escalation
        register_tiktok_escalation()
    except Exception as e:
        logger.debug("TikTok escalation registration deferred: %s", e)

    # ★ P0: 注册所有平台 Action 到 WorkflowEngine
    try:
        from src.workflow.platform_actions_bridge import register_all_platform_actions
        n = register_all_platform_actions()
        logger.info("WorkflowEngine platform actions 注册完成: %d 个 action", n)
    except Exception as e:
        logger.warning("平台 Action 注册失败 (WorkflowEngine 将使用内置 util actions): %s", e)

    # L2 (PR-3): 启动失败队列 drain 后台线程
    # 60 秒周期扫本地 SQLite push_queue, 失败 push 入指数 backoff,
    # 超过 100 次移死信表. 防数据丢.
    try:
        from src.host.central_push_drain import start_drain_thread
        _drain = start_drain_thread()
        logger.info("L2 push drain thread 启动成功 (interval=60s, limit=100)")
    except Exception as e:
        logger.warning("L2 push drain thread 启动失败 (push 失败队列将堆积): %s", e)

    # PR-6.6: worker 角色起 agent_mesh listener
    # 30 秒周期 poll 主控 agent_messages, 收 cmd=manual_reply / pause_ai / resume_ai
    # 用对应物理手机发消息或调 ai_takeover_state. coordinator 不需要起.
    try:
        from src.host.multi_host import _load_role  # type: ignore
        _role = _load_role() if callable(_load_role) else ""
    except Exception:
        _role = ""
    if not _role:
        try:
            import yaml as _yaml
            from src.host.device_registry import config_file as _cf
            _cluster_yaml = _cf("cluster.yaml")
            if _cluster_yaml.exists():
                with _cluster_yaml.open(encoding="utf-8") as _f:
                    _cluster_cfg = _yaml.safe_load(_f) or {}
                _role = (_cluster_cfg.get("role") or "").strip()
        except Exception as e:
            logger.debug("[mesh_listener] cluster.yaml 读 role 失败: %s", e)

    if _role == "worker":
        try:
            from src.host.agent_mesh_worker_listener import start_worker_listener
            start_worker_listener(interval_sec=30.0, limit=50)
            logger.info("PR-6.6 worker mesh listener 启动成功 (role=worker)")
        except Exception as e:
            logger.warning("PR-6.6 worker mesh listener 启动失败: %s", e)
    else:
        logger.info("PR-6.6 worker mesh listener 跳过 (role=%s, 仅 worker 角色启动)", _role or "unknown")

    # ★ P1: 启动策略优化器（A/B 自动应用 + 每日参数调节）
    try:
        from src.host.task_policy import policy_blocks_strategy_optimizer
        if policy_blocks_strategy_optimizer():
            logger.info("策略优化器已按 task_execution_policy 关闭")
        else:
            from src.host.strategy_optimizer import start_strategy_optimizer
            start_strategy_optimizer()
            logger.info("策略优化器已启动")
    except Exception as e:
        logger.debug("策略优化器启动跳过: %s", e)
    try:
        import yaml
        notif_path = config_file("notifications.yaml")
        if notif_path.exists():
            with open(notif_path, encoding="utf-8") as f:
                notif_cfg = yaml.safe_load(f) or {}
            from .alert_notifier import AlertNotifier
            AlertNotifier.get().configure(notif_cfg.get("notifications", notif_cfg))
    except Exception as e:
        logger.debug("通知配置加载跳过: %s", e)

    try:
        start_job_scheduler()
        from src.host.task_policy import policy_blocks_json_scheduled_jobs
        if policy_blocks_json_scheduled_jobs():
            logger.info("scheduled_jobs.json 内联任务已按 task_execution_policy 禁用（线程仍运行：分析快照等）")
        else:
            logger.info("scheduled_jobs.json 定时任务线程已启动")
    except Exception as e:
        logger.debug("定时任务调度器启动跳过: %s", e)

    # Auto-number devices and deploy wallpapers in background
    try:
        from src.host.task_policy import policy_blocks_auto_wallpaper_thread
        _skip_wp = policy_blocks_auto_wallpaper_thread()
    except Exception:
        _skip_wp = False
    if not _skip_wp and _auto_wallpaper_background_thread_enabled():
        try:
            from src.utils.wallpaper_generator import get_wallpaper_auto_manager
            _wp_mgr = get_wallpaper_auto_manager(_project_root)
            from src.device_control.device_manager import get_device_manager as _get_dm2
            _dm2 = _get_dm2(_config_path)
            import threading as _th
            def _auto_wallpaper():
                import time as _t
                _t.sleep(5)
                try:
                    _wp_mgr.ensure_all_numbered(_dm2)
                except Exception as _e:
                    logger.error("自动壁纸编号失败: %s", _e)
            _th.Thread(target=_auto_wallpaper, daemon=True, name="wallpaper-auto").start()
            logger.info("壁纸自动编号线程已启动")
        except Exception as e:
            logger.debug("壁纸自动编号启动跳过: %s", e)
    elif not _skip_wp:
        logger.info("壁纸自动编号线程未启动（OPENCLAW_DISABLE_AUTO_WALLPAPER_THREAD）")
    else:
        logger.info("启动时自动壁纸编号已按 task_execution_policy 关闭")

    # ★ Sprint E/F: 统一走 ollama_vlm.warmup_async（幂等 TTL + 与 classify 同状态机）
    try:
        import threading as _th

        def _vlm_warmup():
            try:
                import time as _t
                _t.sleep(3)
                from src.host import ollama_vlm
                hc = ollama_vlm.check_health(timeout=3.0)
                if not hc.get("online") or not hc.get("model_available"):
                    logger.info(
                        "VLM warmup_async 跳过: online=%s model=%s available=%s",
                        hc.get("online"), hc.get("model"), hc.get("model_available"),
                    )
                    return
                if ollama_vlm.warmup_async(force=False):
                    logger.info("VLM warmup_async 已排队（服务启动后约 3s 触发）")
                else:
                    logger.debug("VLM warmup_async 未排队（已 fresh 或进行中）")
            except Exception as _e:
                logger.debug("VLM 预热跳过: %s", _e)

        _th.Thread(target=_vlm_warmup, daemon=True, name="vlm-warmup").start()
    except Exception as e:
        logger.debug("VLM 预热线程启动失败: %s", e)

    try:
        from .multi_host import auto_start_cluster
        auto_start_cluster()
    except Exception as e:
        logger.debug("集群自动启动跳过: %s", e)

    # 2026-05-05 Stage I: coordinator 启动 reverse heartbeat prober.
    # worker HeartbeatSender 失效时, 主控反向 GET worker /devices 把它注册
    # 成 online (Stage B 真机验证发现 W03/W175 server 活但 push 心跳停 →
    # /cluster/devices 返 0). 30s 间隔 + 10s 启动延时.
    try:
        from .multi_host import load_cluster_config, start_reverse_prober
        _cluster_cfg = load_cluster_config()
        if (_cluster_cfg.get("role") or "").lower() == "coordinator":
            start_reverse_prober()
            logger.info("Reverse heartbeat prober 已启动 (主控反向探测 stale worker)")
        else:
            logger.debug("Reverse prober 跳过 (非 coordinator role)")
    except Exception as e:
        logger.debug("Reverse heartbeat prober 启动跳过: %s", e)

    # P9-A: 启动 Worker-03 CRM 数据的 SWR 缓存（异步预热，不阻塞启动）
    try:
        from .leads_cache import get_w03_cache
        get_w03_cache().start(warm=True)
        logger.info("W03 CRM 缓存已启动")
    except Exception as e:
        logger.debug("W03 CRM 缓存启动跳过: %s", e)

    # P3: 启动设备每日统计聚合器（每5分钟轮询 W03）
    try:
        from .device_stats_aggregator import start as _start_agg
        _start_agg()
        logger.info("设备统计聚合器已启动")
    except Exception as e:
        logger.debug("设备统计聚合器启动跳过: %s", e)

    # W03 事件桥接：轮询 W03 的 EventStreamHub 并转发到本地，前端实时可见
    try:
        from .w03_event_bridge import start_w03_bridge
        start_w03_bridge()
        logger.info("W03 事件桥接已启动")
    except Exception as e:
        logger.debug("W03 事件桥接启动跳过: %s", e)

    # pending 救援循环：兜底重试就绪任务 + 孤儿 pending（create 后未 submit）
    try:
        from .task_dispatcher import start_pending_rescue_loop
        if start_pending_rescue_loop():
            logger.info("pending_rescue_loop 已启动（每 15s 扫一次 pending 补派）")
    except Exception as e:
        logger.warning("pending_rescue_loop 启动失败: %s", e)

    # 2026-05-08 P2-2: 任务链编排 — 注册 EventBus 监听
    try:
        from .task_chain import setup_chain_listener
        setup_chain_listener()
    except Exception as e:
        logger.debug("任务链监听注册跳过: %s", e)

    # 2026-05-08 P2-3: 任务失败自动自愈 — rotate_ip/reconnect_usb/smart_retry
    try:
        from .task_failure_healer import setup_task_failure_healer
        setup_task_failure_healer()
    except Exception as e:
        logger.debug("任务失败自愈注册跳过: %s", e)

    _api_key = os.environ.get("OPENCLAW_API_KEY", "")
    if _api_key:
        logger.info("API 鉴权已启用 (X-API-Key)")
    else:
        logger.warning("API 鉴权未启用 — 设置 OPENCLAW_API_KEY 环境变量以启用")

    # OpenClaw Federation: 注册到 Core（仅当 OPENCLAW_CORE_URL 配置时）
    try:
        from .openclaw_agent import start_openclaw_agent
        start_openclaw_agent()
    except Exception as e:
        logger.debug("OpenClaw Agent 启动跳过: %s", e)

    # ★ 代理健康监控 — 出口IP验证、熔断保护（每5分钟检测一次）
    try:
        from src.behavior.proxy_health import get_proxy_health_monitor
        _proxy_monitor = get_proxy_health_monitor()
        _proxy_monitor.register_all_from_routers()
        _proxy_monitor.start_monitor()
        logger.info("代理健康监控已启动（每5分钟验证出口IP）")
    except Exception as e:
        logger.debug("代理健康监控启动跳过: %s", e)

    # ★ GL.iNet 路由器后台监控（每5分钟检测在线状态）
    try:
        from src.device_control.router_manager import get_router_manager
        _rt_mgr = get_router_manager()
        _rt_mgr.start_monitor()
        logger.info("路由器健康监控已启动")
    except Exception as e:
        logger.debug("路由器监控启动跳过: %s", e)

    # ★ Phase 7 P0: 代理池定时同步任务（每天06:00 自动同步922S5代理）
    try:
        from src.device_control.proxy_pool_manager import ensure_sync_schedule
        _pool_sid = ensure_sync_schedule(cron_expr="0 6 * * *")
        if _pool_sid:
            logger.info("代理池定时同步任务已注册: schedule_id=%s", _pool_sid[:8])
    except Exception as e:
        logger.debug("代理池定时任务注册跳过: %s", e)

    yield
    try:
        from .task_dispatcher import stop_pending_rescue_loop
        stop_pending_rescue_loop()
    except Exception:
        pass
    stop_monitor()
    scheduler.stop_scheduler()
    shutdown_pool()

    # 停止代理健康监控
    try:
        from src.behavior.proxy_health import get_proxy_health_monitor
        get_proxy_health_monitor().stop_monitor()
    except Exception:
        pass

    # 停止路由器监控
    try:
        from src.device_control.router_manager import get_router_manager
        get_router_manager().stop_monitor()
    except Exception:
        pass

    # OpenClaw Federation: 停止心跳
    try:
        from .openclaw_agent import stop_openclaw_agent
        stop_openclaw_agent()
    except Exception:
        pass

    # 2026-05-05 Stage I: 停止 reverse heartbeat prober
    try:
        from .multi_host import stop_reverse_prober
        stop_reverse_prober(timeout_sec=5.0)
    except Exception:
        pass

    # 2026-05-05 Stage H.2: 让 lifespan SIGTERM 路径干净退出.
    # 补 4 个 startup 启动的 daemon 但 shutdown 没停的 (Stage E.2 全 suite 验
    # 时 starlette TestClient teardown 卡死的根因之一).
    # 剩余 3 个未补 (job_scheduler / w03_event_bridge / w03_cache) — 这些
    # module 没 stop_* 函数, 改它们是 prod 行为大改, 留 H.2-followup PR.
    try:
        from .central_push_drain import stop_drain_thread
        stop_drain_thread(timeout_sec=5.0)
    except Exception:
        pass
    try:
        from .agent_mesh_worker_listener import stop_worker_listener
        stop_worker_listener(timeout_sec=5.0)
    except Exception:
        pass
    try:
        from .strategy_optimizer import stop_strategy_optimizer
        stop_strategy_optimizer()
    except Exception:
        pass
    try:
        from .device_stats_aggregator import stop as _stop_aggregator
        _stop_aggregator()
    except Exception:
        pass

    # 2026-05-05 H.2-followup: 补 3 个新加 stop 的 daemon
    # (job_scheduler / w03_event_bridge / w03_cache 之前没 stop_*, 现已加)
    try:
        from .job_scheduler import stop_job_scheduler
        stop_job_scheduler(timeout_sec=5.0)
    except Exception:
        pass
    try:
        from .w03_event_bridge import stop_w03_bridge
        stop_w03_bridge(timeout_sec=5.0)
    except Exception:
        pass
    try:
        from .leads_cache import stop_w03_cache
        stop_w03_cache(timeout_sec=5.0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App & Middleware
# ---------------------------------------------------------------------------

app = FastAPI(title="OpenClaw Host Task API", version="1.1.0", lifespan=lifespan)

# ── CORS (remote access) ──
_CORS_ORIGINS = os.environ.get("OPENCLAW_CORS_ORIGINS", "")
try:
    from fastapi.middleware.cors import CORSMiddleware
    _origins = [o.strip() for o in _CORS_ORIGINS.split(",") if o.strip()] if _CORS_ORIGINS else []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:
    pass

try:
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
except Exception:
    pass

# ── Rate limiting ──
_RATE_LIMIT_RPM = int(os.environ.get("OPENCLAW_RATE_LIMIT", "6000"))
_rate_tracker: dict = {}  # ip -> (count, window_start)
_RATE_EXEMPT = ("/dashboard", "/static", "/health", "/cluster/", "/ws", "/favicon", "/manifest", "/sw.js")


@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    if _RATE_LIMIT_RPM <= 0:
        return await call_next(request)
    path = request.url.path
    if any(path.startswith(p) for p in _RATE_EXEMPT):
        return await call_next(request)
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    entry = _rate_tracker.get(client_ip, (0, now))
    count, window = entry
    if now - window > 60:
        count, window = 0, now
    count += 1
    _rate_tracker[client_ip] = (count, window)
    if count > _RATE_LIMIT_RPM:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=429,
                            content={"detail": "请求过于频繁，请稍后再试"})
    response = await call_next(request)
    return response


# ── Security headers + Audit logging ──
_AUDIT_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
_AUDIT_SKIP_PATHS = {"/auth/login", "/auth/me", "/ws", "/health", "/manifest.json", "/sw.js"}

@app.middleware("http")
async def security_and_audit_middleware(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    if request.method in _AUDIT_METHODS and request.url.path not in _AUDIT_SKIP_PATHS:
        user = "anonymous"
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            token = request.cookies.get("oc_token", "")
        session = _active_sessions.get(token)
        if session and session.get("expires", 0) > time.time():
            user = session.get("user", "unknown")
        try:
            record_audit_log(
                user=user,
                action=request.method,
                path=request.url.path,
                status=response.status_code,
                ip=request.client.host if request.client else "unknown",
            )
        except Exception:
            pass
    return response


# ── 主控 → Worker 设备 API 透明转发（USB 在 Worker 上时本机 DeviceManager 无此机） ──
try:
    from .worker_device_proxy import install_coordinator_device_proxy
    install_coordinator_device_proxy(app)
    logger.info("已启用主控 Worker 设备 API 转发中间件")
except Exception as _e_wdp:
    logger.warning("Worker 设备转发中间件未加载: %s", _e_wdp)


# ---------------------------------------------------------------------------
# Router registration — existing 17 routers
# ---------------------------------------------------------------------------

from .routers.notifications import router as _notif_router
from .routers.notifications import notify_router as _notify_center_router
from .routers.analytics import router as _analytics_router
from .routers.audit import router as _audit_router
from .routers.device_groups import router as _groups_router
from .routers.platforms import router as _platforms_router
from .routers.cluster import router as _cluster_router
from .routers.tasks import router as _tasks_router
from .routers.task_params import router as _task_params_router
from .routers.workflows import router as _workflows_router
from .routers.monitoring import router as _monitoring_router
from .routers.system import router as _system_router
from .routers.batch import router as _batch_router
from .routers.briefing import router as _briefing_router
from .routers.devices_core import router as _devices_core_router
from .routers.devices_control import router as _devices_control_router
from .routers.devices_health import router as _devices_health_router
from .routers.ai import router as _ai_router
from .routers.leads import router as _leads_router
from .routers.campaigns import router as campaigns_router
from .routers.crm_sync import router as _crm_sync_router
from .routers.lead_mesh import router as _lead_mesh_router  # 2026-04-23 Phase 5
from .routers.line_pool import router as _line_pool_router  # 2026-04-25 Phase 11

app.include_router(_notif_router, dependencies=[Depends(verify_api_key)])
app.include_router(_notify_center_router, dependencies=[Depends(verify_api_key)])
app.include_router(_analytics_router, dependencies=[Depends(verify_api_key)])
app.include_router(_audit_router, dependencies=[Depends(verify_api_key)])
app.include_router(_groups_router, dependencies=[Depends(verify_api_key)])
app.include_router(_platforms_router, dependencies=[Depends(verify_api_key)])
app.include_router(_cluster_router)
app.include_router(_tasks_router)
app.include_router(_task_params_router, dependencies=[Depends(verify_api_key)])
app.include_router(_workflows_router)
app.include_router(_monitoring_router)
app.include_router(_system_router, dependencies=[Depends(verify_api_key)])
app.include_router(_batch_router, dependencies=[Depends(verify_api_key)])
app.include_router(_briefing_router)
app.include_router(_devices_core_router, dependencies=[Depends(verify_api_key)])
app.include_router(_devices_control_router, dependencies=[Depends(verify_api_key)])
app.include_router(_devices_health_router, dependencies=[Depends(verify_api_key)])
app.include_router(_ai_router, dependencies=[Depends(verify_api_key)])
app.include_router(_leads_router, dependencies=[Depends(verify_api_key)])
app.include_router(campaigns_router)
app.include_router(_crm_sync_router)
app.include_router(_lead_mesh_router, dependencies=[Depends(verify_api_key)])
app.include_router(_line_pool_router, dependencies=[Depends(verify_api_key)])

# ---------------------------------------------------------------------------
# Router registration — new routers (extracted from api.py)
# ---------------------------------------------------------------------------

from .routers.auth import router as _auth_router
from .routers.streaming import router as _streaming_router
from .routers.tiktok import router as _tiktok_router
from .routers.facebook import router as _facebook_router
from .routers.vpn import router as _vpn_router
from .routers.matrix import router as _matrix_router
from .routers.macros import router as _macros_router
from .routers.experiments import router as _experiments_router
from .routers.conversations import router as _conversations_router
from .routers.risk import router as _risk_router
from .routers.websocket_routes import router as _ws_routes_router
from .routers.security import router as _security_router
from .routers.pwa import router as _pwa_router

app.include_router(_auth_router)
app.include_router(_streaming_router)  # WebSocket不兼容全局auth依赖，REST端点内部已验证
app.include_router(_tiktok_router)  # 已在 router 上设置 dependencies
app.include_router(_facebook_router)  # 已在 router 上设置 dependencies

# Sprint 2 P0 + Sprint 3 P0: 跨平台风控自愈总线 (策略 B,facebook+tiktok)
try:
    from .risk_auto_heal import start_cross_platform_risk_listener
    start_cross_platform_risk_listener()
except Exception as _e:
    import logging as _lg
    _lg.getLogger(__name__).warning("[fb_risk] 启动失败: %s", _e)

app.include_router(_vpn_router, dependencies=[Depends(verify_api_key)])
app.include_router(_matrix_router)  # 已在 router 上设置 dependencies
app.include_router(_macros_router, dependencies=[Depends(verify_api_key)])
app.include_router(_experiments_router)  # 已在 router 上设置 dependencies
app.include_router(_conversations_router)  # 已在 router 上设置 dependencies
app.include_router(_risk_router)  # 已在 router 上设置 dependencies
app.include_router(_ws_routes_router)
app.include_router(_security_router)  # 已在 router 上设置 dependencies
app.include_router(_pwa_router)

# Preflight — 设备就绪预检 API
from .routers.preflight import router as _preflight_router
app.include_router(_preflight_router)

# Task dispatch gate — 策略只读、预检探测
from .routers.task_dispatch import router as _task_dispatch_router
app.include_router(_task_dispatch_router, dependencies=[Depends(verify_api_key)])

# Router Manager — GL.iNet 软路由管理
from .routers.router_mgmt import router as _router_mgmt_router
app.include_router(_router_mgmt_router, dependencies=[Depends(verify_api_key)])

# Proxy Health Monitor — 出口IP验证、熔断保护、GPS地理配置
from .routers.proxy_health_api import router as _proxy_health_router
app.include_router(_proxy_health_router, dependencies=[Depends(verify_api_key)])

# ---------------------------------------------------------------------------
# Dashboard router
# ---------------------------------------------------------------------------

from .dashboard import router as dashboard_router
app.include_router(dashboard_router)

# ★ P2-4: 统一运营大屏路由
try:
    from .routers.unified_dashboard import router as _unified_dashboard_router
    app.include_router(_unified_dashboard_router)
except Exception as _ud_err:
    logger.debug("统一运营大屏路由注册跳过: %s", _ud_err)

# ★ Content Studio — AI内容生成与自动发布菜单
try:
    from .routers.studio import router as _studio_router
    app.include_router(_studio_router)
except Exception as _stu_err:
    logger.debug("Content Studio 路由注册跳过: %s", _stu_err)

# ★ 跨系统数据分析（Content Studio + TikTok 引流）
try:
    from .routers.analytics_studio import router as _analytics_studio_router
    app.include_router(_analytics_studio_router)
except Exception as _as_err:
    logger.warning("跨系统分析路由注册跳过: %s", _as_err)

# Studio Manager 后台初始化
try:
    from src.studio.studio_manager import get_studio_manager
    get_studio_manager()
    logger.info("Content Studio 已初始化")
except Exception as _studio_err:
    logger.warning("Content Studio 初始化跳过: %s", _studio_err)

from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.responses import Response

_static_dir = str(Path(__file__).parent / "static")

class NoCacheStaticFiles(StaticFiles):
    """静态文件不缓存，确保更新后立即生效。"""
    async def __call__(self, scope, receive, send):
        async def _send(message):
            if message.get("type") == "http.response.start":
                headers = dict(message.get("headers", []))
                new_headers = list(message.get("headers", []))
                new_headers.append((b"cache-control", b"no-cache, no-store, must-revalidate"))
                new_headers.append((b"pragma", b"no-cache"))
                message["headers"] = new_headers
            await send(message)
        await super().__call__(scope, receive, _send)

app.mount("/static", NoCacheStaticFiles(directory=_static_dir), name="static")


# ---------------------------------------------------------------------------
# Backward-compatible re-export
# ---------------------------------------------------------------------------

from .routers.devices_control import device_screenshot  # noqa: F401


# ---------------------------------------------------------------------------
# Legacy dashboard JSON bundle (SPA HTML 由 ``dashboard`` 路由单独提供，避免与 /dashboard 重复注册)
# ---------------------------------------------------------------------------

@app.get(
    "/dashboard/core-aggregate",
    dependencies=[Depends(verify_api_key)],
    operation_id="get_dashboard_core_aggregate",
)
def get_dashboard_core_aggregate():
    """聚合设备/任务/合规/AI 等 JSON（历史兼容）；新大屏优先用 ``/dashboard/*`` 运营接口。"""
    data = {"timestamp": time.time()}

    # Devices
    try:
        manager = get_device_manager(_config_path)
        manager.discover_devices()
        pool = get_worker_pool()
        data["devices"] = [
            {
                "device_id": d.device_id,
                "name": d.display_name,
                "status": d.status.value,
                "model": d.model or "",
                "busy": pool.is_device_busy(d.device_id),
            }
            for d in manager.get_all_devices()
        ]
    except Exception:
        data["devices"] = []

    # Tasks summary
    try:
        data["tasks"] = task_store.get_stats()
    except Exception:
        data["tasks"] = {}

    # Compliance
    try:
        guard = get_compliance_guard()
        data["compliance"] = {}
        for platform in ("telegram", "linkedin", "whatsapp"):
            data["compliance"][platform] = guard.get_platform_status(platform)
    except Exception:
        data["compliance"] = {}

    # AI stats
    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        data["ai"] = client.stats.snapshot()
    except Exception:
        data["ai"] = {"status": "not_initialized"}

    # Events
    try:
        from src.workflow.event_bus import get_event_bus
        bus = get_event_bus()
        data["events"] = {
            "recent_count": len(bus.recent_events(100)),
            "subscriptions": bus.subscription_count,
        }
    except Exception:
        data["events"] = {}

    # Smart schedule
    try:
        from src.workflow.smart_schedule import get_default_config, check_smart_constraints, get_rate_multiplier
        cfg = get_default_config()
        allowed, reason, jitter = check_smart_constraints(cfg)
        data["schedule"] = {
            "timezone": cfg.timezone,
            "allowed_now": allowed,
            "reason": reason,
            "rate_multiplier": get_rate_multiplier(cfg),
        }
    except Exception:
        data["schedule"] = {}

    # TikTok stats
    try:
        from src.host.device_state import get_device_state_store
        from src.leads.follow_tracker import LeadsFollowTracker
        ds = get_device_state_store("tiktok")
        tracker = LeadsFollowTracker()
        tiktok_devices = ds.list_devices()
        data["tiktok"] = {
            "devices": [ds.get_device_summary(did) for did in tiktok_devices],
            "follow_stats": tracker.get_stats(),
            "device_count": len(tiktok_devices),
        }
    except Exception:
        data["tiktok"] = {}

    # Compliance (add tiktok)
    try:
        if "compliance" in data and isinstance(data["compliance"], dict):
            guard = get_compliance_guard()
            data["compliance"]["tiktok"] = guard.get_platform_status("tiktok")
    except Exception:
        pass

    # Pool
    try:
        data["pool"] = get_worker_pool().get_status()
    except Exception:
        data["pool"] = {}

    return data


# ---------------------------------------------------------------------------
# Helper: _to_response
# ---------------------------------------------------------------------------

def _to_response(t: dict) -> TaskResponse:
    from src.host.task_dispatch_gate import result_dict_with_gate_hints
    from src.host.task_ui_enrich import build_task_ui_enrichment
    from src.host.task_labels_zh import task_label_zh
    from src.host.error_classifier import classify_task_error

    raw = t.get("result")
    result = result_dict_with_gate_hints(raw) if raw is not None else None
    ui = build_task_ui_enrichment(t)
    task_type = t.get("type", "")

    # P1 — 后端归类 last_error → {layer, code, msg, tone, emoji} 让前端不脆弱
    err_text = (result or {}).get("error") if isinstance(result, dict) else None
    error_classification = classify_task_error(err_text)

    # 2026-04-27 Phase 2 P0 #2: 从 checkpoint 提取 current_step (业务方法
    # 调 task_store.set_task_step 写入). 容错: checkpoint 可能是 dict / JSON
    # str / None / 不含 current_step 字段, 任一情况都返 None.
    current_step = current_sub_step = current_step_at = None
    cp_raw = t.get("checkpoint")
    if cp_raw:
        cp_dict = cp_raw
        if isinstance(cp_raw, str):
            try:
                import json as _j
                cp_dict = _j.loads(cp_raw)
            except Exception:
                cp_dict = None
        if isinstance(cp_dict, dict):
            current_step = cp_dict.get("current_step") or None
            current_sub_step = cp_dict.get("current_sub_step") or None
            current_step_at = cp_dict.get("current_step_at") or None

    return TaskResponse(
        task_id=t["task_id"],
        type=task_type,
        type_label_zh=task_label_zh(task_type),
        device_id=t.get("device_id"),
        status=t.get("status", ""),
        params=t.get("params") or {},
        result=result,
        created_at=t.get("created_at", ""),
        updated_at=t.get("updated_at", ""),
        device_label=ui.get("device_label"),
        worker_host=ui.get("worker_host"),
        task_origin=ui.get("task_origin"),
        task_origin_label_zh=ui.get("task_origin_label_zh"),
        phase_caption=ui.get("phase_caption"),
        execution_policy_hint=ui.get("execution_policy_hint"),
        stuck_reason_zh=ui.get("stuck_reason_zh"),
        current_step=current_step,
        current_sub_step=current_sub_step,
        current_step_at=current_step_at,
        error_classification=error_classification,
        deleted_at=t.get("deleted_at") or None,
    )
