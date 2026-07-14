# -*- coding: utf-8 -*-
"""L2 失败队列 drain 后台线程.

动机
----
``central_push_client`` 在 push 失败时 enqueue 到本地 SQLite (``push_queue``).
但旧实现没有调度方扫表回补 — 失败数据只进不出, 重启 worker 时丢失.

本模块起独立后台线程, 每 ``interval_sec`` 秒调一次
``EnqueueRetryStore.drain(limit=N)``. 复用 PR #87 ``cluster_lock_client``
的 ``_HeartbeatThread`` (threading.Event + wait) 模式, 退出时优雅停止.

使用
----
::

    from src.host.central_push_drain import start_drain_thread, stop_drain_thread

    # worker 启动时:
    start_drain_thread()

    # worker shutdown 时 (SIGTERM 钩子):
    stop_drain_thread()

特性
----
* idempotent: 重复 start 不创建多个线程, 单进程单实例
* fail-safe: drain 抛异常被 catch + log, 线程不死
* 优雅停止: stop_event.set() 让 wait() 立即返回, 不等满 interval
* 可配置: interval / limit / db_path 都可参数化, 默认值适合 200 设备规模
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_INTERVAL_SEC = 60.0
DEFAULT_DRAIN_LIMIT = 100
# 启动时延 (避免与 worker 主循环抢资源)
DEFAULT_STARTUP_DELAY_SEC = 5.0


class _DrainThread(threading.Thread):
    """后台扫失败队列, 周期性回补."""

    def __init__(
        self,
        interval_sec: float = DEFAULT_DRAIN_INTERVAL_SEC,
        limit: int = DEFAULT_DRAIN_LIMIT,
        startup_delay_sec: float = DEFAULT_STARTUP_DELAY_SEC,
    ):
        super().__init__(daemon=True, name="l2-push-drain")
        # interval 不在这里 clamp; caller 默认 60s, 测试可传更小.
        # 生产误配的安全网在 start_drain_thread 文档 + monitoring (drain_attempts 异常高).
        self._interval = max(0.01, interval_sec)
        self._limit = max(1, limit)
        self._startup_delay = max(0.0, startup_delay_sec)
        self._stop_event = threading.Event()
        self._iterations = 0
        self._last_drained = 0
        self._last_run_at: Optional[float] = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        # 启动延时, 避免跟 worker 启动期的其它线程抢 CPU
        if self._startup_delay > 0:
            self._stop_event.wait(self._startup_delay)
            if self._stop_event.is_set():
                return
        logger.info(
            "[push_drain] thread started, interval=%.0fs limit=%d",
            self._interval, self._limit,
        )
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(self._interval)
        logger.info(
            "[push_drain] thread stopped, total iterations=%d, last drained=%d",
            self._iterations, self._last_drained,
        )

    def _tick(self) -> None:
        """单次 drain. 任何异常 catch + log, 不让线程死."""
        self._iterations += 1
        self._last_run_at = time.time()
        try:
            from src.host.central_push_client import get_retry_store
            store = get_retry_store()
            drained = store.drain(limit=self._limit)
            self._last_drained = drained
            if drained > 0:
                logger.info("[push_drain] drained %d items (iteration #%d)",
                            drained, self._iterations)
            else:
                logger.debug("[push_drain] no items to drain (iteration #%d)",
                             self._iterations)
        except Exception:  # noqa: BLE001
            logger.exception("[push_drain] tick failed, will retry next interval")

    def status(self) -> dict:
        return {
            "running": self.is_alive() and not self._stop_event.is_set(),
            "iterations": self._iterations,
            "last_drained": self._last_drained,
            "last_run_at": self._last_run_at,
            "interval_sec": self._interval,
            "limit": self._limit,
        }


# ── 单例 ──────────────────────────────────────────────────────────────
_drain_thread: Optional[_DrainThread] = None
_thread_lock = threading.Lock()


def start_drain_thread(
    interval_sec: float = DEFAULT_DRAIN_INTERVAL_SEC,
    limit: int = DEFAULT_DRAIN_LIMIT,
    startup_delay_sec: float = DEFAULT_STARTUP_DELAY_SEC,
) -> _DrainThread:
    """启动后台 drain 线程 (idempotent). 已在跑就直接返回.

    返回当前的 thread 对象 (用于 status() / 测试).
    """
    global _drain_thread
    with _thread_lock:
        if _drain_thread is not None and _drain_thread.is_alive():
            return _drain_thread
        t = _DrainThread(
            interval_sec=interval_sec,
            limit=limit,
            startup_delay_sec=startup_delay_sec,
        )
        t.start()
        _drain_thread = t
        return t


def stop_drain_thread(timeout_sec: float = 10.0) -> bool:
    """优雅停止后台 drain 线程. 返回是否在 timeout 内退出."""
    global _drain_thread
    with _thread_lock:
        t = _drain_thread
        _drain_thread = None
    if t is None:
        return True
    t.stop()
    t.join(timeout=timeout_sec)
    return not t.is_alive()


def get_drain_status() -> dict:
    """暴露给 /cluster/customers/push/metrics 路由."""
    with _thread_lock:
        t = _drain_thread
    if t is None:
        return {"running": False, "reason": "not started"}
    return t.status()


def reset_for_tests() -> None:
    """仅测试用. 强制 stop thread + 清单例.

    2026-05-04 修: 旧行为 "不停 thread 留给上层 stop" 在 conftest.py P2-⑨
    autouse fixture 场景下导致上一 test 的 daemon 变孤儿继续 _tick →
    drain → _http_post_json → push_total 计数污染下一 test (Stage 0
    baseline 复现: alphabetical 全 suite 跑 test_metrics_increments_on_5xx_after_retries
    actual=5 vs expected=1). 进 reset 就 stop+join, 让 "reset" 名实相符.
    """
    global _drain_thread
    with _thread_lock:
        t = _drain_thread
        _drain_thread = None
    if t is not None:
        t.stop()
        t.join(timeout=2.0)
