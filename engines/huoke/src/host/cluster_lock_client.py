# -*- coding: utf-8 -*-
"""Cluster Lock Client SDK — worker 侧调用主控 Lock Service.

动机
----
中央 Lock Service (``cluster_lock.py``) 由主控持有, 各 worker (含主控自己)
通过 HTTP API 申请锁. 本模块提供 ``device_lock`` context manager + 自动
heartbeat 续 lease, 让业务代码尽量不变:

::

    from src.host.cluster_lock_client import device_lock

    with device_lock(device_id, "send_greeting", priority=50, ttl_sec=300):
        # 持锁的业务逻辑 (heartbeat 后台线程自动续 lease)
        ...

设计
----
* **Background heartbeat**: 持锁期间每 ttl/3 秒发 heartbeat HTTP 请求
* **Auto release on exit**: __exit__ 释放锁; 异常也释放
* **Fail-safe degradation**: coordinator 不可达 / 5xx 时, 默认 fallback
  到本地 ``threading.Lock`` (单进程 worker_pool 旧行为), 避免阻塞业务.
  通过 ``fallback_local=False`` 显式禁用 fallback (强一致, 出错抛异常)
* **Worker ID 自动**: 从 ``config/cluster.yaml::host_id`` 读, 未配则
  使用 ``socket.gethostname()`` + uuid 后缀
* **Coordinator URL 自动**: ``cluster.yaml::coordinator_url`` 优先,
  否则环境变量 ``OPENCLAW_COORDINATOR_URL``, 最后默认主控 ``http://192.168.0.118:8000``

异常场景
--------
* 网络超时 → fallback (默认) 或 raise ClusterLockError (强一致)
* HTTP 5xx → 同上
* HTTP 4xx (ttl 过大 / device_id 必填等) → 直接 raise ClusterLockError, 不 fallback
* 持锁期间 coordinator 死亡 → heartbeat 失败 log warning, 业务继续
  (TTL 过期后 server 自动释放, 风险窗口 = ttl)
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional
from urllib import request as _ureq, error as _uerr

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────
DEFAULT_COORDINATOR_URL = "http://192.168.0.118:8000"
DEFAULT_TTL_SEC = 300.0
DEFAULT_WAIT_TIMEOUT_SEC = 180.0
DEFAULT_HTTP_TIMEOUT_SEC = 8.0

# 防止业务死循环: heartbeat 总续期不超过此时间, 之后让 TTL 自然过期
# 大多数业务 < 5 分钟; 真有长任务可显式传 max_lease_sec
DEFAULT_MAX_LEASE_SEC = 1800.0  # 30 min

_HOST_ID_CACHE: Optional[str] = None
_COORD_URL_CACHE: Optional[str] = None
_CONFIG_CACHE_LOCK = threading.Lock()


class ClusterLockError(RuntimeError):
    """Cluster Lock 通信失败 (强一致模式) 或参数错误."""


# ── 配置加载 ──────────────────────────────────────────────────────────
def _load_cluster_yaml() -> Dict[str, Any]:
    """从项目 config/cluster.yaml 加载, 失败返 {}."""
    try:
        from src.host.device_registry import config_file
        cfg_path = config_file("cluster.yaml")
        if not cfg_path.exists():
            return {}
        import yaml
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[cluster_lock_client] load cluster.yaml failed: %s", exc)
        return {}


def get_worker_id() -> str:
    """获取本 worker 的 host_id (cached)."""
    global _HOST_ID_CACHE
    if _HOST_ID_CACHE:
        return _HOST_ID_CACHE
    with _CONFIG_CACHE_LOCK:
        if _HOST_ID_CACHE:
            return _HOST_ID_CACHE
        cfg = _load_cluster_yaml()
        hid = (cfg.get("host_id") or "").strip()
        if not hid:
            hid = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        _HOST_ID_CACHE = hid
        return hid


def get_coordinator_url() -> str:
    """获取 coordinator URL (cached). priority: env > cluster.yaml > default."""
    global _COORD_URL_CACHE
    if _COORD_URL_CACHE:
        return _COORD_URL_CACHE
    with _CONFIG_CACHE_LOCK:
        if _COORD_URL_CACHE:
            return _COORD_URL_CACHE
        env_url = (os.environ.get("OPENCLAW_COORDINATOR_URL") or "").strip()
        if env_url:
            _COORD_URL_CACHE = env_url.rstrip("/")
            return _COORD_URL_CACHE
        cfg = _load_cluster_yaml()
        cu = (cfg.get("coordinator_url") or "").strip()
        if cu:
            _COORD_URL_CACHE = cu.rstrip("/")
            return _COORD_URL_CACHE
        # role=coordinator 时连自己
        if str(cfg.get("role", "")).lower() == "coordinator":
            port = int(cfg.get("local_port") or 8000)
            _COORD_URL_CACHE = f"http://127.0.0.1:{port}"
            return _COORD_URL_CACHE
        _COORD_URL_CACHE = DEFAULT_COORDINATOR_URL
        return _COORD_URL_CACHE


def _api_key_header() -> Dict[str, str]:
    key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
    return {"X-API-Key": key} if key else {}


def reset_caches_for_tests() -> None:
    global _HOST_ID_CACHE, _COORD_URL_CACHE
    _HOST_ID_CACHE = None
    _COORD_URL_CACHE = None


# ── HTTP 调用 ─────────────────────────────────────────────────────────
def _http_post(
    path: str,
    body: Dict[str, Any],
    timeout: float = DEFAULT_HTTP_TIMEOUT_SEC,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    url = (base_url or get_coordinator_url()).rstrip("/") + path
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(_api_key_header())
    req = _ureq.Request(url, data=data, method="POST", headers=headers)
    with _ureq.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _http_get(
    path: str,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SEC,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    url = (base_url or get_coordinator_url()).rstrip("/") + path
    req = _ureq.Request(url, headers=_api_key_header())
    with _ureq.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# ── 远程 acquire/release/heartbeat ────────────────────────────────────
def acquire_lock(
    device_id: str,
    resource: str = "default",
    priority: int = 50,
    ttl_sec: float = DEFAULT_TTL_SEC,
    wait_timeout_sec: float = DEFAULT_WAIT_TIMEOUT_SEC,
    worker_id: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """单次 acquire 调用 (no context). 返回 server 完整响应."""
    body = {
        "worker_id": worker_id or get_worker_id(),
        "device_id": device_id,
        "resource": resource,
        "priority": priority,
        "ttl_sec": ttl_sec,
        "wait_timeout_sec": wait_timeout_sec,
    }
    # 等待时间是 server-side block. client HTTP timeout 必须 > server wait_timeout
    http_timeout = max(DEFAULT_HTTP_TIMEOUT_SEC, wait_timeout_sec + 5.0)
    return _http_post("/cluster/lock/acquire", body, timeout=http_timeout, base_url=base_url)


def release_lock(lock_id: str, base_url: Optional[str] = None) -> bool:
    try:
        res = _http_post(
            "/cluster/lock/release", {"lock_id": lock_id}, base_url=base_url,
        )
        return bool(res.get("ok"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cluster_lock_client] release %s failed: %s", lock_id[:8], exc)
        return False


def heartbeat_lock(
    lock_id: str,
    extend_ttl_sec: Optional[float] = None,
    base_url: Optional[str] = None,
) -> bool:
    body: Dict[str, Any] = {"lock_id": lock_id}
    if extend_ttl_sec is not None:
        body["extend_ttl_sec"] = extend_ttl_sec
    try:
        res = _http_post("/cluster/lock/heartbeat", body, base_url=base_url)
        return bool(res.get("ok"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cluster_lock_client] heartbeat %s failed: %s", lock_id[:8], exc)
        return False


# ── Heartbeat 后台线程 ────────────────────────────────────────────────
class _HeartbeatThread(threading.Thread):
    """持锁期间每 interval 秒发 heartbeat. stop() 退出.

    安全网: 总续期不超过 max_lease_sec, 防止业务死循环导致锁永不释放.
    超过 max_lease 后 heartbeat 停止发, TTL 自然过期, server 自动 evict.
    """

    def __init__(
        self,
        lock_id: str,
        interval_sec: float,
        ttl_sec: float,
        max_lease_sec: float = DEFAULT_MAX_LEASE_SEC,
        base_url: Optional[str] = None,
    ):
        super().__init__(daemon=True, name=f"hb-{lock_id[:8]}")
        self._lock_id = lock_id
        self._interval = max(1.0, interval_sec)
        self._ttl = ttl_sec
        self._max_lease = max_lease_sec
        self._base_url = base_url
        self._stop_event = threading.Event()
        self._failures = 0
        self._started_at = time.time()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(self._interval)
            if self._stop_event.is_set():
                break
            # 安全网: 超过 max_lease 不再续期
            elapsed = time.time() - self._started_at
            if elapsed > self._max_lease:
                logger.warning(
                    "[cluster_lock_client] lock %s elapsed %.0fs > max_lease "
                    "%.0fs, 停止 heartbeat. TTL %.0fs 后 server 自动 evict.",
                    self._lock_id[:8], elapsed, self._max_lease, self._ttl,
                )
                break
            ok = heartbeat_lock(
                self._lock_id, extend_ttl_sec=self._ttl, base_url=self._base_url,
            )
            if not ok:
                self._failures += 1
                if self._failures >= 3:
                    logger.warning(
                        "[cluster_lock_client] heartbeat %s 连续 3 次失败, "
                        "持锁有效期 = TTL (%.0fs), 业务继续但风险窗口 ↑",
                        self._lock_id[:8], self._ttl,
                    )
            else:
                self._failures = 0


# ── Context manager: device_lock ──────────────────────────────────────
@contextmanager
def device_lock(
    device_id: str,
    resource: str = "default",
    priority: int = 50,
    ttl_sec: float = DEFAULT_TTL_SEC,
    wait_timeout_sec: float = DEFAULT_WAIT_TIMEOUT_SEC,
    max_lease_sec: float = DEFAULT_MAX_LEASE_SEC,
    worker_id: Optional[str] = None,
    base_url: Optional[str] = None,
    fallback_local: bool = True,
) -> Iterator[Dict[str, Any]]:
    """跨 worker 设备锁 context manager.

    Yields lock info dict (lock_id 等) 或 fallback 标记.

    fallback_local=True (默认): coordinator 不可达 → 用本地 threading.Lock
    fallback (单进程串行化, 跨进程无保护). 用于 30 台测试 / 主控临时挂.
    fallback_local=False: 强一致, coordinator 故障直接抛 ClusterLockError.

    使用::

        with device_lock(device_id, "send_greeting", priority=50, ttl_sec=300):
            # 持锁的业务. heartbeat 自动续 lease (interval = ttl/3).
            ...
    """
    lock_info: Optional[Dict[str, Any]] = None
    hb_thread: Optional[_HeartbeatThread] = None
    using_fallback = False
    fallback_lock: Optional[threading.Lock] = None
    fallback_acquired = False

    t0 = time.time()
    try:
        # 1. acquire 远程
        try:
            res = acquire_lock(
                device_id=device_id,
                resource=resource,
                priority=priority,
                ttl_sec=ttl_sec,
                wait_timeout_sec=wait_timeout_sec,
                worker_id=worker_id,
                base_url=base_url,
            )
        except (_uerr.URLError, _uerr.HTTPError, OSError, json.JSONDecodeError) as exc:
            if not fallback_local:
                raise ClusterLockError(
                    f"acquire failed (no fallback): {exc}"
                ) from exc
            logger.warning(
                "[cluster_lock_client] acquire %s failed (%s), fallback to local lock",
                device_id[:8], exc,
            )
            using_fallback = True

        if not using_fallback:
            if not res.get("granted"):
                raise ClusterLockError(
                    f"lock not granted: {res.get('reason') or 'unknown'}, "
                    f"wait_ms={res.get('wait_ms')}"
                )
            lock_info = {
                "lock_id": res["lock_id"],
                "wait_ms": res.get("wait_ms"),
                "evicted_lock": res.get("evicted_lock"),
                "device_id": device_id,
                "resource": resource,
                "backend": "cluster",
            }
            # 2. 启动 heartbeat (interval = ttl/3, 至少 1s)
            hb_thread = _HeartbeatThread(
                lock_id=res["lock_id"],
                interval_sec=max(1.0, ttl_sec / 3.0),
                ttl_sec=ttl_sec,
                max_lease_sec=max_lease_sec,
                base_url=base_url,
            )
            hb_thread.start()
        else:
            # fallback 本地 lock
            fallback_lock = _get_local_fallback_lock(device_id, resource)
            fallback_acquired = fallback_lock.acquire(timeout=wait_timeout_sec)
            if not fallback_acquired:
                raise ClusterLockError(
                    f"local fallback lock acquire timeout after {wait_timeout_sec}s"
                )
            lock_info = {
                "lock_id": None,
                "device_id": device_id,
                "resource": resource,
                "backend": "local_fallback",
            }

        yield lock_info

    finally:
        # 3. cleanup
        if hb_thread:
            hb_thread.stop()
            hb_thread.join(timeout=2.0)
        if lock_info and lock_info.get("lock_id"):
            release_lock(lock_info["lock_id"], base_url=base_url)
        if fallback_acquired and fallback_lock:
            fallback_lock.release()


# ── 本地 fallback lock 池 ─────────────────────────────────────────────
_LOCAL_FALLBACK_LOCKS: Dict[tuple, threading.Lock] = {}
_LOCAL_FALLBACK_GUARD = threading.Lock()


def _get_local_fallback_lock(device_id: str, resource: str) -> threading.Lock:
    key = (device_id, resource)
    with _LOCAL_FALLBACK_GUARD:
        if key not in _LOCAL_FALLBACK_LOCKS:
            _LOCAL_FALLBACK_LOCKS[key] = threading.Lock()
        return _LOCAL_FALLBACK_LOCKS[key]
