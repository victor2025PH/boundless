# -*- coding: utf-8 -*-
"""Cluster Lock Service 单测 — 跨 worker 设备锁."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.host.cluster_lock import ClusterLockService, AcquireResult


@pytest.fixture
def svc(tmp_path: Path):
    """每个测试用独立 SQLite, 隔离."""
    s = ClusterLockService(tmp_path / "test_locks.db")
    yield s
    s.stop_cleanup_thread()


def test_acquire_basic(svc: ClusterLockService):
    res = svc.acquire(worker_id="w-A", device_id="dev1", resource="default")
    assert res.granted
    assert res.lock_id
    assert res.evicted_lock is None


def test_acquire_same_resource_blocks(svc: ClusterLockService):
    a = svc.acquire(worker_id="w-A", device_id="dev1", resource="r1",
                    wait_timeout_sec=0.1)
    assert a.granted

    # B 来抢同 (device, resource), 应 wait_timeout
    b = svc.acquire(worker_id="w-B", device_id="dev1", resource="r1",
                    wait_timeout_sec=0.5)
    assert not b.granted
    assert b.reason == "wait_timeout"


def test_acquire_different_resource_parallel(svc: ClusterLockService):
    """同 device 不同 resource 不互斥."""
    a = svc.acquire(worker_id="w-A", device_id="dev1", resource="add_friend")
    b = svc.acquire(worker_id="w-B", device_id="dev1", resource="send_greeting")
    assert a.granted and b.granted
    assert a.lock_id != b.lock_id


def test_acquire_different_device_parallel(svc: ClusterLockService):
    a = svc.acquire(worker_id="w-A", device_id="dev1")
    b = svc.acquire(worker_id="w-A", device_id="dev2")
    assert a.granted and b.granted


def test_release(svc: ClusterLockService):
    a = svc.acquire(worker_id="w-A", device_id="dev1")
    assert a.granted
    assert svc.release(a.lock_id) is True
    # 二次 release 返回 False
    assert svc.release(a.lock_id) is False
    # 释放后另一个 worker 能拿到
    b = svc.acquire(worker_id="w-B", device_id="dev1", wait_timeout_sec=0.1)
    assert b.granted


def test_heartbeat_extends_lease(svc: ClusterLockService):
    a = svc.acquire(worker_id="w-A", device_id="dev1", ttl_sec=2.0)
    assert a.granted
    time.sleep(1.0)
    info = svc.heartbeat(a.lock_id, extend_ttl_sec=2.0)
    assert info is not None
    # expires_at 应被推后
    time.sleep(1.5)
    info2 = svc.get_lock(a.lock_id)
    assert info2 is not None  # 还活着 (因为 heartbeat 续了)


def test_heartbeat_missing_returns_none(svc: ClusterLockService):
    assert svc.heartbeat("nonexistent-id") is None


def test_ttl_expiration(svc: ClusterLockService):
    a = svc.acquire(worker_id="w-A", device_id="dev1", ttl_sec=0.5)
    assert a.granted
    time.sleep(0.7)
    # 主动触发清理
    svc._cleanup_once()
    # 现在另一 worker 应能拿到
    b = svc.acquire(worker_id="w-B", device_id="dev1", wait_timeout_sec=0.1)
    assert b.granted


def test_preemption_high_priority_wins(svc: ClusterLockService):
    a = svc.acquire(worker_id="w-A", device_id="dev1", priority=50)
    assert a.granted

    # priority 95 (>= 90 阈值, 且 > 50) 应抢占
    b = svc.acquire(worker_id="w-B", device_id="dev1", priority=95,
                    wait_timeout_sec=0.1)
    assert b.granted
    assert b.evicted_lock is not None
    assert b.evicted_lock["lock_id"] == a.lock_id

    # 旧 lock_id 已失效, release 返回 False
    assert svc.release(a.lock_id) is False


def test_preemption_below_threshold_not_evict(svc: ClusterLockService):
    a = svc.acquire(worker_id="w-A", device_id="dev1", priority=50)
    # priority 80 < 90 阈值, 不抢占, 应 wait_timeout
    b = svc.acquire(worker_id="w-B", device_id="dev1", priority=80,
                    wait_timeout_sec=0.3)
    assert not b.granted


def test_preemption_same_priority_no_evict(svc: ClusterLockService):
    a = svc.acquire(worker_id="w-A", device_id="dev1", priority=95)
    # 同 priority 95, 不抢占
    b = svc.acquire(worker_id="w-B", device_id="dev1", priority=95,
                    wait_timeout_sec=0.3)
    assert not b.granted


def test_list_locks_filter(svc: ClusterLockService):
    svc.acquire(worker_id="w-A", device_id="dev1", resource="r1")
    svc.acquire(worker_id="w-A", device_id="dev2", resource="r1")
    svc.acquire(worker_id="w-B", device_id="dev3", resource="r1")
    assert len(svc.list_locks()) == 3
    assert len(svc.list_locks(worker_id="w-A")) == 2
    assert len(svc.list_locks(device_id="dev3")) == 1


def test_metrics_increment(svc: ClusterLockService):
    m0 = svc.metrics()
    a = svc.acquire(worker_id="w-A", device_id="dev1")
    svc.release(a.lock_id)
    m1 = svc.metrics()
    assert m1["acquired_total"] == m0["acquired_total"] + 1
    assert m1["released_total"] == m0["released_total"] + 1


def test_persist_across_instance(tmp_path: Path):
    db = tmp_path / "persist.db"
    svc1 = ClusterLockService(db)
    a = svc1.acquire(worker_id="w-A", device_id="dev1", ttl_sec=60)
    assert a.granted
    svc1.stop_cleanup_thread()

    # 新实例从 DB 恢复
    svc2 = ClusterLockService(db)
    locks = svc2.list_locks()
    assert len(locks) == 1
    assert locks[0]["lock_id"] == a.lock_id
    svc2.stop_cleanup_thread()


def test_concurrent_acquire(svc: ClusterLockService):
    """20 并发同抢 1 device, 应只 1 成功, 19 wait_timeout."""
    results = []
    barrier = threading.Barrier(20)

    def worker(i):
        barrier.wait()
        r = svc.acquire(
            worker_id=f"w-{i}",
            device_id="dev1",
            wait_timeout_sec=0.5,
        )
        results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [r for r in results if r.granted]
    assert len(granted) == 1


def test_rate_limit(svc: ClusterLockService, monkeypatch):
    """超过 rate limit 应返回 rate_limited."""
    from src.host import cluster_lock as mod
    monkeypatch.setattr(mod, "RATE_LIMIT_ACQUIRE_PER_SEC", 5)

    granted = 0
    rate_limited = 0
    for i in range(10):
        r = svc.acquire(
            worker_id="w-spam",
            device_id=f"dev-{i}",
            wait_timeout_sec=0.0,
        )
        if r.granted:
            granted += 1
        elif r.reason == "rate_limited":
            rate_limited += 1

    assert granted == 5
    assert rate_limited == 5


def test_max_ttl_clamp(svc: ClusterLockService):
    """ttl > MAX_TTL_SEC 应被限制."""
    from src.host.cluster_lock import MAX_TTL_SEC
    a = svc.acquire(worker_id="w-A", device_id="dev1", ttl_sec=99999.0)
    assert a.granted
    info = svc.get_lock(a.lock_id)
    assert info["expires_at"] - info["acquired_at"] <= MAX_TTL_SEC + 1
