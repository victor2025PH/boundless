# -*- coding: utf-8 -*-
"""WorkerPool 单元测试。"""

import threading
import time
import pytest

from src.host.worker_pool import WorkerPool


class TestWorkerPool:

    def test_submit_and_execute(self):
        pool = WorkerPool(max_workers=2)
        result = [None]

        def work():
            result[0] = "done"

        pool.submit("t1", "devA", work)
        time.sleep(0.5)
        assert result[0] == "done"
        pool.shutdown()

    def test_device_lock_serializes(self):
        """同一设备的任务应串行执行"""
        pool = WorkerPool(max_workers=4)
        log = []

        def task(name, duration):
            log.append(f"{name}_start")
            time.sleep(duration)
            log.append(f"{name}_end")

        pool.submit("t1", "devA", task, "A", 0.3)
        pool.submit("t2", "devA", task, "B", 0.1)
        time.sleep(1.0)

        assert log[0] == "A_start"
        assert log[1] == "A_end"
        assert log[2] == "B_start"
        assert log[3] == "B_end"
        pool.shutdown()

    def test_different_devices_parallel(self):
        """不同设备的任务应并行执行"""
        pool = WorkerPool(max_workers=4)
        log = []
        barrier = threading.Barrier(2, timeout=3)

        def task(name):
            log.append(f"{name}_start")
            barrier.wait()  # 两个任务应该同时到达这里
            log.append(f"{name}_end")

        pool.submit("t1", "devA", task, "A")
        pool.submit("t2", "devB", task, "B")
        time.sleep(1.0)

        starts = [x for x in log if x.endswith("_start")]
        ends = [x for x in log if x.endswith("_end")]
        assert len(starts) == 2
        assert len(ends) == 2
        pool.shutdown()

    def test_status(self):
        pool = WorkerPool(max_workers=2)
        status = pool.get_status()
        assert status["running"] is True
        assert status["queued_count"] == 0
        pool.shutdown()

    def test_shutdown_rejects_new(self):
        pool = WorkerPool(max_workers=2)
        pool.shutdown()
        assert pool.submit("t1", "d1", lambda: None) is False
