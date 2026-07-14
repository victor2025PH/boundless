# -*- coding: utf-8 -*-
"""
定时任务调度器。

后台线程每 30 秒检查一次 schedules 表，
对到期的调度创建 task 并提交到 WorkerPool。
"""

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from croniter import croniter

from .database import get_conn
from .task_store import create_task
from .worker_pool import get_worker_pool
from .executor import run_task

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 30  # 秒


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


# ── Schedule CRUD ──

def create_schedule(name: str, cron_expr: str, task_type: str,
                    device_id: Optional[str] = None,
                    params: Optional[dict] = None) -> str:
    """创建定时调度"""
    if not croniter.is_valid(cron_expr):
        raise ValueError(f"无效的 cron 表达式: {cron_expr}")

    sid = str(uuid.uuid4())
    now = _now_iso()
    nxt = croniter(cron_expr, _now_dt()).get_next(datetime)
    next_run = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO schedules (schedule_id, name, cron_expr, task_type, "
            "device_id, params, enabled, next_run, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (sid, name, cron_expr, task_type, device_id,
             json.dumps(params or {}, ensure_ascii=False), next_run, now),
        )
    logger.info("创建调度 %s: %s [%s] next=%s", sid[:8], name, cron_expr, next_run)
    return sid


def list_schedules() -> List[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_schedule(schedule_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def delete_schedule(schedule_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,)
        )
    return cur.rowcount > 0


def toggle_schedule(schedule_id: str, enabled: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE schedules SET enabled = ? WHERE schedule_id = ?",
            (1 if enabled else 0, schedule_id),
        )
    return cur.rowcount > 0


def _row_to_dict(row) -> dict:
    d = dict(row)
    val = d.get("params")
    if isinstance(val, str):
        try:
            d["params"] = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            pass
    d["enabled"] = bool(d.get("enabled", 0))
    return d


# ── Scheduler 线程 ──

class SchedulerThread(threading.Thread):
    """后台调度线程"""

    def __init__(self, config_path: str):
        super().__init__(daemon=True, name="openclaw-scheduler")
        self._config_path = config_path
        self._stop_event = threading.Event()

    def run(self):
        logger.info("Scheduler 启动，检查间隔 %ds", _CHECK_INTERVAL)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("Scheduler tick 异常: %s", e)
            self._stop_event.wait(_CHECK_INTERVAL)
        logger.info("Scheduler 已停止")

    def stop(self):
        self._stop_event.set()

    def _tick(self):
        """一次调度检查, with smart scheduling constraints."""
        try:
            from src.host.task_policy import policy_blocks_db_scheduler
            if policy_blocks_db_scheduler():
                return
        except Exception:
            pass

        now = _now_dt()
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?",
                (now_iso,),
            ).fetchall()

        for row in rows:
            s = _row_to_dict(dict(row))
            sid = s["schedule_id"]
            try:
                params = s.get("params") or {}

                # Apply smart scheduling constraints if configured
                smart_cfg = params.pop("smart_schedule", None)
                jitter_sec = 0.0
                if smart_cfg:
                    from src.workflow.smart_schedule import (
                        SmartScheduleConfig, check_smart_constraints,
                    )
                    sc = SmartScheduleConfig.from_dict(smart_cfg)
                    allowed, reason, jitter_sec = check_smart_constraints(sc)
                    if not allowed:
                        logger.debug("调度 %s [%s] 被智能约束跳过: %s",
                                     sid[:8], s["name"], reason)
                        nxt = croniter(s["cron_expr"], now).get_next(datetime)
                        next_run = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
                        with get_conn() as conn:
                            conn.execute(
                                "UPDATE schedules SET next_run = ? WHERE schedule_id = ?",
                                (next_run, sid),
                            )
                        continue

                if jitter_sec > 0:
                    logger.debug("调度 %s 应用抖动 %.0fs", sid[:8], jitter_sec)
                    time.sleep(min(jitter_sec, 15))

                # 策略：禁止数据库调度自动派发查收件箱
                if s.get("task_type") == "tiktok_check_inbox":
                    try:
                        from src.host.task_policy import policy_blocks_auto_tiktok_check_inbox
                        if policy_blocks_auto_tiktok_check_inbox():
                            logger.info(
                                "调度 %s [%s] 跳过: disable_auto_tiktok_check_inbox",
                                sid[:8], s.get("name", ""),
                            )
                            nxt = croniter(s["cron_expr"], now).get_next(datetime)
                            next_run = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
                            with get_conn() as conn2:
                                conn2.execute(
                                    "UPDATE schedules SET last_run = ?, next_run = ? WHERE schedule_id = ?",
                                    (now_iso, next_run, sid),
                                )
                            continue
                    except Exception as _pol_err:
                        logger.debug("调度 %s 策略检查异常(继续): %s", sid[:8], _pol_err)

                if s["task_type"] == "workflow":
                    self._trigger_workflow(s, params, sid)
                elif s["task_type"] == "start_chain":
                    # P4-3: 链式任务定时调度
                    self._trigger_chain(s, params, sid)
                else:
                    # 执行前检查目标设备是否就绪（有网络 + VPN），不就绪跳过本次调度
                    sched_device = s.get("device_id")
                    if sched_device:
                        try:
                            from src.host.preflight import run_preflight
                            pf = run_preflight(sched_device)
                            if not pf.passed:
                                logger.info(
                                    "调度 %s [%s] 跳过: 设备 %s 未就绪 (%s: %s)",
                                    sid[:8], s["name"], sched_device[:8],
                                    pf.blocked_step, pf.blocked_reason,
                                )
                                continue
                        except Exception as _pf_err:
                            logger.debug("调度 %s 预检异常(继续执行): %s", sid[:8], _pf_err)

                        # Phase 7 P1: 代理熔断检查（仅对内容发布类任务生效）
                        # 熔断中的设备跳过发布，避免在IP泄漏状态下留下记录
                        _publish_types = {"studio_publish", "tiktok_post", "content_publish"}
                        if s["task_type"] in _publish_types:
                            try:
                                from src.studio.publishers.base_publisher import (
                                    _check_proxy_circuit_breaker
                                )
                                cb = _check_proxy_circuit_breaker(sched_device)
                                if cb.get("blocked"):
                                    logger.warning(
                                        "调度 %s [%s] 跳过: 设备 %s 代理熔断 (%s)",
                                        sid[:8], s["name"], sched_device[:8],
                                        cb.get("reason", ""),
                                    )
                                    continue
                            except Exception as _cb_err:
                                logger.debug("调度 %s 熔断检查异常(继续执行): %s",
                                             sid[:8], _cb_err)

                    # ★ P2-5: 注入策略优化器动态参数（合并，不覆盖已有参数）
                    task_params = dict(params)
                    if "_created_via" not in task_params:
                        task_params["_created_via"] = "scheduler"
                    if s["task_type"].startswith("tiktok_"):
                        try:
                            from src.host.strategy_optimizer import get_optimized_params
                            opt = get_optimized_params()
                            injected = {}
                            for k, v in opt.items():
                                if k not in task_params:
                                    task_params[k] = v
                                    injected[k] = v
                            if injected:
                                logger.debug("调度 %s [%s] 注入优化参数: %s",
                                             sid[:8], s["name"], injected)
                        except Exception as _opt_err:
                            logger.debug("优化参数注入跳过: %s", _opt_err)

                    task_id = create_task(
                        task_type=s["task_type"],
                        device_id=s.get("device_id"),
                        params=task_params,
                        policy_id=sid,
                    )
                    logger.info("调度 %s [%s] 触发 → task %s",
                                sid[:8], s["name"], task_id[:8])

                    from .executor import _get_device_id
                    from src.device_control.device_manager import get_device_manager
                    manager = get_device_manager(self._config_path)
                    manager.discover_devices()
                    resolved = _get_device_id(manager, s.get("device_id"),
                                              self._config_path)
                    device_for_lock = resolved or "default"

                    pool = get_worker_pool()
                    pool.submit(task_id, device_for_lock, run_task,
                                task_id, self._config_path)

                # 更新 last_run 和 next_run
                nxt = croniter(s["cron_expr"], now).get_next(datetime)
                next_run = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE schedules SET last_run = ?, next_run = ? WHERE schedule_id = ?",
                        (now_iso, next_run, sid),
                    )
            except Exception as e:
                logger.error("调度 %s 执行失败: %s", sid[:8], e)


    def _trigger_workflow(self, schedule: dict, params: dict, sid: str):
        """Trigger a workflow YAML as a scheduled execution."""
        workflow_name = params.get("workflow", schedule.get("name", ""))
        variables = params.get("variables", {})

        from src.host.device_registry import config_dir

        workflow_dir = config_dir() / "workflows"
        workflow_path = None
        for ext in (".yaml", ".yml"):
            p = workflow_dir / f"{workflow_name}{ext}"
            if p.exists():
                workflow_path = p
                break

        if not workflow_path:
            logger.error("调度 %s: 工作流 '%s' 未找到", sid[:8], workflow_name)
            return

        try:
            from src.workflow.engine import WorkflowDef, WorkflowExecutor
            wf = WorkflowDef.from_yaml(str(workflow_path))
            executor = WorkflowExecutor()

            def _run_wf():
                try:
                    result = executor.run(wf, initial_vars=variables)
                    logger.info("调度工作流 %s [%s] 完成: %s",
                                sid[:8], workflow_name,
                                "成功" if result.success else "失败")
                except Exception as e:
                    logger.error("调度工作流 %s [%s] 异常: %s",
                                 sid[:8], workflow_name, e)

            t = threading.Thread(target=_run_wf, daemon=True,
                                 name=f"sched-wf-{sid[:8]}")
            t.start()
            logger.info("调度 %s [%s] 触发工作流 → %s",
                        sid[:8], schedule.get("name", ""), workflow_name)
        except Exception as e:
            logger.error("调度工作流 %s 启动失败: %s", sid[:8], e)


    def _trigger_chain(self, schedule: dict, params: dict, sid: str):
        """P4-3: 链式任务定时调度。"""
        chain_id = params.get("chain_id", "")
        if not chain_id:
            logger.error("调度 %s: start_chain 缺少 chain_id", sid[:8])
            return

        try:
            from src.host.task_chain import create_chain, create_chain_batch
            from src.device_control.device_manager import get_device_manager

            # P6-3: 支持 device_group_id 按组筛选
            device_ids = params.get("device_ids") or []
            group_id = params.get("device_group_id", "")
            if not device_ids and group_id:
                try:
                    from src.host.database import get_conn
                    with get_conn() as conn:
                        rows = conn.execute(
                            "SELECT device_id FROM device_group_members "
                            "WHERE group_id = ?", (group_id,)
                        ).fetchall()
                    device_ids = [r["device_id"] for r in rows]
                except Exception as e:
                    logger.warning("调度 %s: 读取设备组 %s 失败: %s",
                                   sid[:8], group_id, e)
            if not device_ids:
                manager = get_device_manager(self._config_path)
                device_ids = [d.device_id for d in manager.get_all_devices()
                              if d.is_online]
            elif group_id:
                # P6-3: 组设备仍需过滤在线状态
                manager = get_device_manager(self._config_path)
                online = {d.device_id for d in manager.get_all_devices()
                          if d.is_online}
                device_ids = [d for d in device_ids if d in online]

            if not device_ids:
                logger.info("调度 %s [%s] 跳过: 无在线设备",
                            sid[:8], schedule.get("name", ""))
                return

            extra_params = {k: v for k, v in params.items()
                           if k not in ("chain_id", "device_ids",
                                        "device_group_id")}

            if len(device_ids) == 1:
                r = create_chain(chain_id, device_ids[0], extra_params or None)
                logger.info("调度 %s [%s] 触发链 %s → 设备 %s",
                            sid[:8], schedule.get("name", ""),
                            chain_id, device_ids[0][:8])
            else:
                r = create_chain_batch(chain_id, device_ids, extra_params or None)
                logger.info("调度 %s [%s] 触发链 %s → %d 台设备",
                            sid[:8], schedule.get("name", ""),
                            chain_id, len(device_ids))
        except Exception as e:
            logger.error("调度链 %s 启动失败: %s", sid[:8], e)


_scheduler: Optional[SchedulerThread] = None


def start_scheduler(config_path: str):
    global _scheduler
    if _scheduler and _scheduler.is_alive():
        return
    _scheduler = SchedulerThread(config_path)
    _scheduler.start()


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None
