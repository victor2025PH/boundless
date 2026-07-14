# -*- coding: utf-8 -*-
"""
任务工作池 — 多设备并行执行引擎。

核心设计:
  - ThreadPoolExecutor 管理工作线程
  - 每设备一把 Lock，确保同一设备上的任务串行执行
  - 线程数自动匹配在线设备数量（至少 4，最多 16）
  - 支持优雅关机
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Dict, Optional, Callable

logger = logging.getLogger(__name__)

_DEFAULT_MAX_WORKERS = 4
_MAX_CAP = 16


def _auto_workers() -> int:
    """Determine optimal worker count from env or connected devices."""
    env_val = os.environ.get("OPENCLAW_MAX_WORKERS")
    if env_val and env_val.isdigit():
        return max(2, min(int(env_val), _MAX_CAP))
    try:
        from src.device_control.device_manager import get_device_manager
        from src.host.device_registry import DEFAULT_DEVICES_YAML

        mgr = get_device_manager(DEFAULT_DEVICES_YAML)
        mgr.discover_devices()
        n = len([d for d in mgr.get_all_devices()
                 if d.status.value in ("connected", "online")])
        return max(_DEFAULT_MAX_WORKERS, min(n + 2, _MAX_CAP))
    except Exception:
        return _DEFAULT_MAX_WORKERS


class WorkerPool:
    """多设备并行任务执行池"""

    def __init__(self, max_workers: int = _DEFAULT_MAX_WORKERS):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="openclaw-worker",
        )
        self._max_workers = max_workers
        self._device_locks: Dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()
        self._futures: Dict[str, Future] = {}
        self._active_tasks: Dict[str, str] = {}
        self._cancel_flags: Dict[str, threading.Event] = {}
        self._task_priorities: Dict[str, int] = {}  # task_id → priority
        self._running = True
        logger.info("WorkerPool 启动: max_workers=%d", max_workers)

    def _get_device_lock(self, device_id: str) -> threading.Lock:
        if device_id not in self._device_locks:
            with self._lock_guard:
                if device_id not in self._device_locks:
                    self._device_locks[device_id] = threading.Lock()
        return self._device_locks[device_id]

    def submit(self, task_id: str, device_id: str,
               fn: Callable, *args, priority: int = 50, **kwargs) -> bool:
        """
        提交任务到执行队列。
        priority: 0=最低, 50=默认, 100=高意向紧急（会抢占低优先级运行中任务）。
        优先级 >= 90 的任务会取消同设备上正在运行的低优先级任务。

        ★ P1 健康降级: 高风险设备自动拒绝低优先级任务，保护账号安全。
        """
        if not self._running:
            logger.warning("WorkerPool 已关闭，拒绝任务 %s", task_id)
            return False

        # ★ P1: 健康风险检测 — 高风险设备只接受紧急任务
        if priority < 90:
            try:
                from src.host.health_monitor import metrics
                risk = metrics.predict_disconnect_risk(device_id)
                if risk.get("risk") == "high":
                    reasons = "; ".join(risk.get("reasons", []))
                    logger.warning(
                        "[pool] ⚠ 设备 %s 掉线风险高(%s)，拒绝低优先级任务 %s。原因: %s",
                        device_id[:8], risk.get("score", "?"),
                        task_id[:8], reasons,
                    )
                    # 记录被拒绝的任务到 task_store
                    try:
                        from src.host.task_store import set_task_result
                        set_task_result(task_id, success=False,
                                        error=f"设备掉线风险高，任务已跳过。风险分={risk.get('score')}")
                    except Exception:
                        pass
                    # ★ P2-3: 高风险设备告警
                    try:
                        from src.host.alert_notifier import get_alert_notifier
                        get_alert_notifier().notify(
                            level="warning",
                            device_id=device_id,
                            message=(
                                f"设备掉线风险高(分={risk.get('score','?')})，"
                                f"已拒绝任务 {task_id[:8]}。{reasons}"
                            ),
                        )
                    except Exception:
                        pass
                    return False
                if risk.get("risk") == "medium" and priority < 40:
                    # 中等风险：降级日志警告，但允许执行（仅阻止最低优先级任务）
                    logger.info("[pool] 设备 %s 掉线风险中等，任务 %s 继续（priority=%d）",
                                device_id[:8], task_id[:8], priority)
            except Exception:
                pass  # 健康检查失败时不阻断任务

        # ── 高优先级抢占：取消同设备上的低优先级任务 ──
        if priority >= 90:
            current_task_id = self._active_tasks.get(device_id)
            if current_task_id:
                current_priority = self._task_priorities.get(current_task_id, 50)
                if priority > current_priority:
                    logger.info(
                        "[pool] 高优先级任务 %s (p=%d) 发送取消信号给 %s (p=%d) on %s",
                        task_id[:8], priority, current_task_id[:8], current_priority,
                        device_id[:8],
                    )
                    self.cancel_task(current_task_id)

        cancel_event = threading.Event()
        self._cancel_flags[task_id] = cancel_event
        self._task_priorities[task_id] = priority

        def _wrapped():
            lock = self._get_device_lock(device_id)
            lock.acquire()
            try:
                if cancel_event.is_set():
                    logger.info("[pool] 任务 %s 在等待期间被取消", task_id[:8])
                    return
                self._active_tasks[device_id] = task_id
                logger.info("[pool] 开始执行 task=%s device=%s priority=%d",
                            task_id[:8], device_id[:8], priority)
                fn(*args, **kwargs)
            finally:
                lock.release()
                self._active_tasks.pop(device_id, None)
                self._futures.pop(task_id, None)
                self._cancel_flags.pop(task_id, None)
                self._task_priorities.pop(task_id, None)
                logger.info("[pool] 完成 task=%s device=%s", task_id[:8], device_id[:8])

        future = self._executor.submit(_wrapped)
        self._futures[task_id] = future
        return True

    def cancel_task(self, task_id: str) -> bool:
        """Request cooperative cancellation of a task.
        Returns True if the task was found and signal sent."""
        flag = self._cancel_flags.get(task_id)
        if flag:
            flag.set()
            logger.info("[pool] 取消信号已发送: task=%s", task_id[:8])
            future = self._futures.get(task_id)
            if future and not future.running():
                future.cancel()
            return True
        return False

    def is_cancelled(self, task_id: str) -> bool:
        """Check if a task has been cancelled (for cooperative checking)."""
        flag = self._cancel_flags.get(task_id)
        return flag.is_set() if flag else False

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "max_workers": self._max_workers,
            "active_tasks": dict(self._active_tasks),
            "queued_count": len(self._futures),
            "device_locks": {
                did: ("busy" if lock.locked() else "idle")
                for did, lock in self._device_locks.items()
            },
        }

    def is_device_busy(self, device_id: str) -> bool:
        lock = self._device_locks.get(device_id)
        return lock.locked() if lock else False

    def shutdown(self, wait: bool = True, timeout: float = 30):
        self._running = False
        logger.info("WorkerPool 关闭中... (wait=%s)", wait)
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
        logger.info("WorkerPool 已关闭")


_pool: Optional[WorkerPool] = None
_pool_lock = threading.Lock()


def get_worker_pool(max_workers: Optional[int] = None) -> WorkerPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                w = max_workers if max_workers else _auto_workers()
                _pool = WorkerPool(max_workers=w)
    return _pool


def shutdown_pool():
    global _pool
    if _pool:
        _pool.shutdown()
        _pool = None
