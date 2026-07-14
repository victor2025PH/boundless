# -*- coding: utf-8 -*-
"""Cluster Lock Client SDK 单测."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from src.host import cluster_lock_client as cli


@pytest.fixture(autouse=True)
def reset_caches():
    cli.reset_caches_for_tests()
    cli._LOCAL_FALLBACK_LOCKS.clear()
    yield
    cli.reset_caches_for_tests()
    cli._LOCAL_FALLBACK_LOCKS.clear()


# ── 配置 ──────────────────────────────────────────────────────────────
def test_get_worker_id_from_yaml(tmp_path, monkeypatch):
    """cluster.yaml::host_id 优先."""
    yaml_path = tmp_path / "cluster.yaml"
    yaml_path.write_text("host_id: my-worker-test\n", encoding="utf-8")

    def fake_config(name):
        return yaml_path if name == "cluster.yaml" else tmp_path / name

    monkeypatch.setattr("src.host.device_registry.config_file", fake_config)
    cli.reset_caches_for_tests()
    assert cli.get_worker_id() == "my-worker-test"


def test_get_coordinator_url_env(monkeypatch):
    """环境变量优先."""
    monkeypatch.setenv("OPENCLAW_COORDINATOR_URL", "http://test:9999/")
    cli.reset_caches_for_tests()
    assert cli.get_coordinator_url() == "http://test:9999"


# ── HTTP 调用 mock ────────────────────────────────────────────────────
class MockServer:
    """轻量内存 mock 模拟主控 HTTP API."""

    def __init__(self):
        self.locks = {}  # lock_id → entry
        self.dr_index = {}  # (device_id, resource) → lock_id
        self.heartbeat_count = 0
        self.acquire_count = 0
        self._lock = threading.Lock()

    def acquire(self, body):
        with self._lock:
            self.acquire_count += 1
            key = (body["device_id"], body.get("resource", "default"))
            if key in self.dr_index:
                # 简化: 不实现 priority 抢占, 只返回 wait_timeout
                return {"granted": False, "wait_ms": 0.0, "reason": "wait_timeout"}
            import uuid
            lid = str(uuid.uuid4())
            self.locks[lid] = body
            self.dr_index[key] = lid
            return {"granted": True, "lock_id": lid, "wait_ms": 0.0}

    def heartbeat(self, body):
        with self._lock:
            self.heartbeat_count += 1
            if body["lock_id"] in self.locks:
                return {"ok": True}
            return {"ok": False, "reason": "not_found"}

    def release(self, body):
        with self._lock:
            lid = body["lock_id"]
            entry = self.locks.pop(lid, None)
            if entry:
                key = (entry["device_id"], entry.get("resource", "default"))
                self.dr_index.pop(key, None)
                return {"ok": True}
            return {"ok": False}


@pytest.fixture
def mock_server(monkeypatch):
    s = MockServer()
    monkeypatch.setenv("OPENCLAW_COORDINATOR_URL", "http://mock-test")
    cli.reset_caches_for_tests()

    def fake_post(path, body, timeout=8.0, base_url=None):
        if path == "/cluster/lock/acquire":
            return s.acquire(body)
        if path == "/cluster/lock/heartbeat":
            return s.heartbeat(body)
        if path == "/cluster/lock/release":
            return s.release(body)
        raise ValueError(f"unknown path {path}")

    monkeypatch.setattr(cli, "_http_post", fake_post)
    return s


def test_acquire_release_basic(mock_server: MockServer):
    res = cli.acquire_lock(device_id="dev1")
    assert res["granted"]
    assert res["lock_id"]
    assert cli.release_lock(res["lock_id"]) is True


def test_device_lock_context_manager(mock_server: MockServer):
    with cli.device_lock("dev1", "send_greeting", ttl_sec=10) as info:
        assert info["lock_id"]
        assert info["backend"] == "cluster"
        assert info["device_id"] == "dev1"
    # 退出后已 release
    assert ("dev1", "send_greeting") not in mock_server.dr_index


def test_device_lock_heartbeat_running(mock_server: MockServer):
    """持锁期间 heartbeat thread 自动续 lease.

    interval = max(1.0, ttl/3), 所以用 ttl=6 → interval=2s, 等 4.5s 看到 2 次 heartbeat.
    """
    with cli.device_lock("dev1", "r1", ttl_sec=6.0):
        time.sleep(4.5)
    assert mock_server.heartbeat_count >= 2


def test_device_lock_blocked_raises(mock_server: MockServer):
    """同 (device, resource) 抢锁失败 → 抛 ClusterLockError."""
    with cli.device_lock("dev1", "r1", ttl_sec=10):
        with pytest.raises(cli.ClusterLockError):
            with cli.device_lock("dev1", "r1", wait_timeout_sec=0.1, fallback_local=False):
                pass


def test_device_lock_fallback_on_network_error(monkeypatch):
    """coordinator 不可达 + fallback_local=True → 用本地 lock."""
    monkeypatch.setenv("OPENCLAW_COORDINATOR_URL", "http://nonexistent-12345:9999")
    cli.reset_caches_for_tests()

    def raise_url_error(*args, **kwargs):
        from urllib.error import URLError
        raise URLError("simulated unreachable")

    monkeypatch.setattr(cli, "_http_post", raise_url_error)

    with cli.device_lock("dev1", "r1", wait_timeout_sec=0.5,
                          fallback_local=True) as info:
        assert info["backend"] == "local_fallback"


def test_device_lock_strict_mode_raises_on_network_error(monkeypatch):
    """fallback_local=False + 网络故障 → ClusterLockError."""
    cli.reset_caches_for_tests()

    def raise_url_error(*args, **kwargs):
        from urllib.error import URLError
        raise URLError("simulated unreachable")

    monkeypatch.setattr(cli, "_http_post", raise_url_error)

    with pytest.raises(cli.ClusterLockError):
        with cli.device_lock("dev1", "r1", wait_timeout_sec=0.1,
                             fallback_local=False):
            pass


def test_local_fallback_serializes_threads(monkeypatch):
    """fallback 模式应仍提供本进程内串行化."""
    cli.reset_caches_for_tests()

    def raise_url_error(*args, **kwargs):
        from urllib.error import URLError
        raise URLError("offline")

    monkeypatch.setattr(cli, "_http_post", raise_url_error)

    counter = {"in_section": 0, "max_concurrent": 0}
    counter_lock = threading.Lock()

    def worker():
        with cli.device_lock("dev1", "r1", wait_timeout_sec=2.0,
                             fallback_local=True):
            with counter_lock:
                counter["in_section"] += 1
                if counter["in_section"] > counter["max_concurrent"]:
                    counter["max_concurrent"] = counter["in_section"]
            time.sleep(0.05)
            with counter_lock:
                counter["in_section"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 串行化 → 任一时刻只 1 in_section
    assert counter["max_concurrent"] == 1


def test_release_idempotent_on_failure(mock_server: MockServer, monkeypatch):
    """release 失败 (网络) 不抛, 返 False."""
    res = cli.acquire_lock(device_id="dev1")
    lock_id = res["lock_id"]

    def raise_url_error(*args, **kwargs):
        from urllib.error import URLError
        raise URLError("temporarily unreachable")

    monkeypatch.setattr(cli, "_http_post", raise_url_error)
    assert cli.release_lock(lock_id) is False  # 不抛
