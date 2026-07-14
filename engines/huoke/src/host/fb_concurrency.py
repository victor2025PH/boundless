# -*- coding: utf-8 -*-
"""Facebook 业务并发控制（2026-04-23 Phase 3 P3-1）。

动机
----
``add_friend`` / ``send_greeting`` 的 daily_cap 检查是典型的 **read-then-write**
竞态: executor WorkerPool 多线程同时跑同一 device 上的两个任务时, 两边都读到
``n24=7 < cap=8`` 就都放行, 结果实际 9 条超 cap。

本模块提供按 ``(device_id, section)`` 粒度的线程锁, 把 "check cap →
perform action → insert record" 的整段关键区串行化。

设计原则
--------
* **细粒度**: 锁 key = ``(device_id, section)``, 不同 device 完全独立, 同 device
  不同 section(add_friend / send_greeting) 也独立, 不会假互斥。
* **timeout**: 每个锁获取有 180s 超时, 避免业务崩在临界区导致永久死锁。
* **进程内**: 本模块只解决**单进程多线程**的竞态。多进程 / 分布式场景需另外
  用 sqlite advisory lock 或 Redis, 目前架构是单进程 FastAPI, 够用。
* **不要嵌套同 key**: ``add_friend_and_greet`` 会先后拿 ``(D, "add_friend")`` 和
  ``(D, "send_greeting")``, 两个 key 不同, 不会自锁。禁止同 section 重入。

使用
----
::

    from src.host.fb_concurrency import device_section_lock

    with device_section_lock(device_id, "send_greeting"):
        n24 = count_outgoing_messages_since(device_id, 24, "greeting")
        if n24 >= cap:
            return False
        # ... 实际发送 + 入库 ...

监控
----
``device_lock_metrics()`` 返回最近一次锁获取耗时 / 活跃锁数 / 超时计数,
供 /health 或 /admin/locks API 暴露。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# ── 锁池 ──────────────────────────────────────────────────────────────
# 一个 device/section 键对应一把 Lock。defaultdict 天然懒创建,
# _LOCKS_MASTER 是元锁保护 defaultdict 本身在并发下不被两个 key 同时创建。
_LOCKS: Dict[tuple, threading.Lock] = defaultdict(threading.Lock)
_LOCKS_MASTER = threading.Lock()

# ── 指标 ──────────────────────────────────────────────────────────────
_METRICS: Dict[str, Any] = {
    "acquired_count": 0,
    "waited_total_ms": 0.0,
    "timeouts": 0,
    "active_count": 0,
    "last_contention_ms": 0.0,
}
_METRICS_LOCK = threading.Lock()

DEFAULT_LOCK_TIMEOUT_SEC = 180.0


class LockTimeoutError(RuntimeError):
    """device_section_lock 等待超时。

    子类 RuntimeError 保向后兼容:
      * 老 caller 用 ``except RuntimeError`` 仍能捕获
      * 老 caller 用 ``"device_section_lock timeout" in str(e)`` 字符串 match 仍 work
        (B 在 round 3 时的 ``_messenger_active_lock`` 上层 catch)

    新代码应改 ``except LockTimeoutError`` 拿到清晰类型契约。
    """
    pass


def _get_lock(device_id: str, section: str) -> threading.Lock:
    """返回 (device_id, section) 对应的锁(懒创建)。"""
    key = (device_id, section)
    with _LOCKS_MASTER:
        return _LOCKS[key]


@contextmanager
def device_section_lock(device_id: Optional[str],
                        section: str,
                        timeout: float = DEFAULT_LOCK_TIMEOUT_SEC
                        ) -> Iterator[None]:
    """按 (device_id, section) 粒度加锁, 串行化关键区。

    ``device_id`` 为空 → 直接 yield 不锁(用于命令行/测试场景)。
    ``timeout`` 秒内未拿到锁 → ``RuntimeError``, 不会永久阻塞。
    """
    if not device_id:
        yield
        return

    lock = _get_lock(str(device_id), section)
    t0 = time.monotonic()
    acquired = lock.acquire(timeout=timeout)
    wait_ms = (time.monotonic() - t0) * 1000.0

    with _METRICS_LOCK:
        if acquired:
            _METRICS["acquired_count"] += 1
            _METRICS["waited_total_ms"] += wait_ms
            _METRICS["active_count"] += 1
            if wait_ms > _METRICS["last_contention_ms"]:
                _METRICS["last_contention_ms"] = wait_ms
        else:
            _METRICS["timeouts"] += 1

    if not acquired:
        logger.warning(
            "[fb_lock] timeout device=%s section=%s 等锁 %.1fs,放弃",
            device_id[:8], section, timeout)
        raise LockTimeoutError(
            f"device_section_lock timeout: device={device_id[:8]} "
            f"section={section} after {timeout:.0f}s")

    if wait_ms > 500:
        # 等 >500ms 说明真的发生了并发争用,值得记一下
        logger.info("[fb_lock] device=%s section=%s 等锁 %.0fms 获得",
                    device_id[:8], section, wait_ms)

    try:
        yield
    finally:
        lock.release()
        with _METRICS_LOCK:
            _METRICS["active_count"] -= 1


def device_lock_metrics() -> Dict[str, Any]:
    """返回锁使用指标快照(只读,非阻塞)。"""
    with _METRICS_LOCK:
        snap = dict(_METRICS)
    snap["lock_pool_size"] = len(_LOCKS)
    if snap["acquired_count"] > 0:
        snap["avg_wait_ms"] = round(
            snap["waited_total_ms"] / snap["acquired_count"], 2)
    else:
        snap["avg_wait_ms"] = 0.0
    return snap


def reset_metrics_for_tests() -> None:
    """仅测试用:重置所有指标和锁池(生产不该调)。"""
    global _METRICS
    with _METRICS_LOCK:
        _METRICS = {
            "acquired_count": 0,
            "waited_total_ms": 0.0,
            "timeouts": 0,
            "active_count": 0,
            "last_contention_ms": 0.0,
        }
    with _LOCKS_MASTER:
        _LOCKS.clear()
