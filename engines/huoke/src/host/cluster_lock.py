# -*- coding: utf-8 -*-
"""Cluster Device Lock Service — 跨 worker 设备锁中央协调.

动机
----
现有 ``fb_concurrency.device_section_lock`` 用 ``threading.Lock`` 解决**单进程
多线程**竞态. ``worker_pool._device_locks`` 同理. 但 200 设备生产拓扑下 10 worker
PC 各自一份 worker_pool, 它们之间没有协调 → 主控 ``/cluster/dispatch`` 把同一
device 派给两个 worker 时, 各 worker 内 lock 解决不了跨机冲突.

本模块提供中央锁服务, 由主控持有 SQLite WAL 后端, 任何 worker (含主控自己) 通过
HTTP API 申请/释放. 所有 device 操作 critical section 都走这层.

设计原则
--------
* **(device_id, resource)** 唯一键: 同 device 不同 resource (如 "send_greeting"
  vs "add_friend") 可并行; 同 resource 不能并行
* **TTL leasing**: 锁有过期时间, worker 死亡 TTL 自动释放. heartbeat 续期
* **Priority 抢占**: 高优 (>=90) 任务可踢低优持锁者
* **审计**: 每次 acquire/release/eviction 写 audit log (现有 audit_helpers)
* **fail-safe degradation**: caller 网络异常 fallback 本地 worker_pool lock,
  不静默死等
* **rate limit**: per-worker 100 acquire/sec (防 worker bug 风暴)

容量
----
* 200 设备 × 5 ops/min ≈ 17 ops/sec, SQLite WAL 单进程 10k+ writes/sec 充足
* 内存缓存所有 active locks (典型 < 200), O(1) 查询
* 后台 cleanup 线程每 10s 扫过期锁

线程安全
--------
* 一把 module 级 ``_state_lock`` 保护内存 dict + DB writer
* DB 用 WAL mode, 读不阻塞写

使用 (server side, 通常通过 HTTP API 调)
::

    from src.host.cluster_lock import lock_service

    # acquire
    res = lock_service.acquire(
        worker_id="worker-175",
        device_id="4HUSIB...",
        resource="send_greeting",
        priority=50,
        ttl_sec=300,
        wait_timeout_sec=180,
    )
    if res.granted:
        ...
        lock_service.release(res.lock_id)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────
DEFAULT_TTL_SEC = 300.0
MAX_TTL_SEC = 3600.0
DEFAULT_WAIT_TIMEOUT_SEC = 180.0
TTL_CLEANUP_INTERVAL_SEC = 10.0
RATE_LIMIT_ACQUIRE_PER_SEC = 100  # per-worker
PREEMPTION_PRIORITY_THRESHOLD = 90  # priority >= 此阈值 才允许抢占低优


# ── 类型 ──────────────────────────────────────────────────────────────
@dataclass
class LockEntry:
    lock_id: str
    worker_id: str
    device_id: str
    resource: str
    priority: int
    acquired_at: float
    expires_at: float
    last_heartbeat: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lock_id": self.lock_id,
            "worker_id": self.worker_id,
            "device_id": self.device_id,
            "resource": self.resource,
            "priority": self.priority,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "last_heartbeat": self.last_heartbeat,
        }


@dataclass
class AcquireResult:
    granted: bool
    lock_id: Optional[str] = None
    wait_ms: float = 0.0
    evicted_lock: Optional[Dict[str, Any]] = None  # 抢占时被踢的旧锁信息
    reason: str = ""  # 失败原因 (timeout / rate_limited / etc.)


# ── 服务实现 ──────────────────────────────────────────────────────────
class ClusterLockService:
    """单 process 内的 Cluster Lock 服务实例 (单例 via ``lock_service``)."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_lock = threading.RLock()
        self._locks: Dict[str, LockEntry] = {}  # lock_id → entry
        # (device_id, resource) → lock_id (索引)
        self._dr_index: Dict[Tuple[str, str], str] = {}
        # per-worker 1s 滑动窗口请求计数, 用 deque
        self._rate_buckets: Dict[str, deque[float]] = defaultdict(deque)

        self._metrics = {
            "acquired_total": 0,
            "released_total": 0,
            "evicted_total": 0,
            "ttl_expired_total": 0,
            "wait_timeout_total": 0,
            "rate_limited_total": 0,
            "heartbeat_total": 0,
        }

        self._cleanup_stop = threading.Event()
        self._cleanup_thread: Optional[threading.Thread] = None

        self._init_db()
        self._restore_from_db()

    # ── DB ────────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db_path), timeout=10.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_locks (
                    lock_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    UNIQUE(device_id, resource)
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_locks_expires ON cluster_locks(expires_at)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_locks_worker ON cluster_locks(worker_id)"
            )
            # 审计日志 (历史 acquire/release/evict, 最多保 7 天)
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_lock_audit (
                    ts REAL NOT NULL,
                    event TEXT NOT NULL,
                    lock_id TEXT,
                    worker_id TEXT,
                    device_id TEXT,
                    resource TEXT,
                    priority INTEGER,
                    detail TEXT
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_ts ON cluster_lock_audit(ts)"
            )

    def _restore_from_db(self) -> None:
        """启动时从 DB 加载未过期的 active locks 到内存."""
        now = time.time()
        with self._state_lock:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT * FROM cluster_locks WHERE expires_at > ?", (now,)
                ).fetchall()
            for r in rows:
                e = LockEntry(
                    lock_id=r["lock_id"],
                    worker_id=r["worker_id"],
                    device_id=r["device_id"],
                    resource=r["resource"],
                    priority=int(r["priority"]),
                    acquired_at=float(r["acquired_at"]),
                    expires_at=float(r["expires_at"]),
                    last_heartbeat=float(r["last_heartbeat"]),
                )
                self._locks[e.lock_id] = e
                self._dr_index[(e.device_id, e.resource)] = e.lock_id
            # 启动时一次性清理过期
            with self._conn() as c:
                c.execute(
                    "DELETE FROM cluster_locks WHERE expires_at <= ?", (now,)
                )
            logger.info(
                "[cluster_lock] restored %d active locks from %s",
                len(self._locks), self._db_path,
            )

    def _audit(
        self,
        event: str,
        lock: Optional[LockEntry] = None,
        detail: str = "",
    ) -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO cluster_lock_audit "
                    "(ts, event, lock_id, worker_id, device_id, resource, priority, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        time.time(),
                        event,
                        lock.lock_id if lock else None,
                        lock.worker_id if lock else None,
                        lock.device_id if lock else None,
                        lock.resource if lock else None,
                        lock.priority if lock else None,
                        detail,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[cluster_lock] audit write failed: %s", exc)

    # ── Rate limit ────────────────────────────────────────────────
    def _check_rate_limit(self, worker_id: str) -> bool:
        """1 秒滑动窗口, 超过 RATE_LIMIT_ACQUIRE_PER_SEC 则拒."""
        now = time.time()
        bucket = self._rate_buckets[worker_id]
        # drop 1s 之前的 timestamp
        while bucket and bucket[0] < now - 1.0:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_ACQUIRE_PER_SEC:
            self._metrics["rate_limited_total"] += 1
            return False
        bucket.append(now)
        return True

    # ── 核心 acquire/release/heartbeat ────────────────────────────
    def acquire(
        self,
        worker_id: str,
        device_id: str,
        resource: str = "default",
        priority: int = 50,
        ttl_sec: float = DEFAULT_TTL_SEC,
        wait_timeout_sec: float = DEFAULT_WAIT_TIMEOUT_SEC,
    ) -> AcquireResult:
        """请求一把锁. 阻塞最多 wait_timeout_sec, 超时返回 granted=False.

        priority >= PREEMPTION_PRIORITY_THRESHOLD 且持锁者 priority 更低时
        允许立即抢占 (踢现持锁者).
        """
        if ttl_sec > MAX_TTL_SEC:
            ttl_sec = MAX_TTL_SEC

        if not self._check_rate_limit(worker_id):
            return AcquireResult(
                granted=False,
                reason="rate_limited",
            )

        t0 = time.time()
        deadline = t0 + max(0.0, wait_timeout_sec)

        while True:
            now = time.time()
            with self._state_lock:
                key = (device_id, resource)
                existing_id = self._dr_index.get(key)

                # 清理已过期 (lazy)
                if existing_id:
                    e = self._locks.get(existing_id)
                    if e and e.is_expired(now):
                        self._evict_locked(e, "ttl_expired")
                        existing_id = None

                if existing_id is None:
                    # 直接获取
                    return self._grant_locked(
                        worker_id, device_id, resource, priority,
                        ttl_sec, wait_ms=(now - t0) * 1000,
                    )

                # 有持锁者, 检查是否能抢占
                e = self._locks.get(existing_id)
                if e and (
                    priority >= PREEMPTION_PRIORITY_THRESHOLD
                    and priority > e.priority
                ):
                    evicted = e.to_dict()
                    self._evict_locked(e, "preempted")
                    res = self._grant_locked(
                        worker_id, device_id, resource, priority,
                        ttl_sec, wait_ms=(now - t0) * 1000,
                    )
                    res.evicted_lock = evicted
                    return res

            # 等待 — poll
            if now >= deadline:
                self._metrics["wait_timeout_total"] += 1
                return AcquireResult(
                    granted=False,
                    wait_ms=(now - t0) * 1000,
                    reason="wait_timeout",
                )
            # short poll, 让出 CPU. 200/sec 量级 0.2s 间隔够
            time.sleep(0.2)

    def _grant_locked(
        self,
        worker_id: str,
        device_id: str,
        resource: str,
        priority: int,
        ttl_sec: float,
        wait_ms: float,
    ) -> AcquireResult:
        """state_lock 已持有, 真创建 lock 记录."""
        now = time.time()
        e = LockEntry(
            lock_id=str(uuid.uuid4()),
            worker_id=worker_id,
            device_id=device_id,
            resource=resource,
            priority=priority,
            acquired_at=now,
            expires_at=now + ttl_sec,
            last_heartbeat=now,
        )
        # 持久化
        with self._conn() as c:
            c.execute(
                "INSERT INTO cluster_locks "
                "(lock_id, worker_id, device_id, resource, priority, "
                " acquired_at, expires_at, last_heartbeat) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (e.lock_id, e.worker_id, e.device_id, e.resource, e.priority,
                 e.acquired_at, e.expires_at, e.last_heartbeat),
            )
        self._locks[e.lock_id] = e
        self._dr_index[(e.device_id, e.resource)] = e.lock_id
        self._metrics["acquired_total"] += 1
        self._audit("acquired", e, f"wait_ms={wait_ms:.1f}")
        return AcquireResult(
            granted=True,
            lock_id=e.lock_id,
            wait_ms=wait_ms,
        )

    def release(self, lock_id: str) -> bool:
        """主动释放. 返回 True if 真释放; False if lock 不存在 (已释放/过期)."""
        with self._state_lock:
            e = self._locks.pop(lock_id, None)
            if not e:
                return False
            self._dr_index.pop((e.device_id, e.resource), None)
            with self._conn() as c:
                c.execute("DELETE FROM cluster_locks WHERE lock_id = ?", (lock_id,))
            self._metrics["released_total"] += 1
            self._audit("released", e)
            return True

    def heartbeat(self, lock_id: str, extend_ttl_sec: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """续 lease. 返回 lock dict if 续期成功, None if lock 不存在或已过期."""
        with self._state_lock:
            e = self._locks.get(lock_id)
            now = time.time()
            if not e or e.is_expired(now):
                return None
            ttl = extend_ttl_sec if extend_ttl_sec is not None else (e.expires_at - e.acquired_at)
            ttl = min(ttl, MAX_TTL_SEC)
            e.expires_at = now + ttl
            e.last_heartbeat = now
            with self._conn() as c:
                c.execute(
                    "UPDATE cluster_locks SET expires_at=?, last_heartbeat=? WHERE lock_id=?",
                    (e.expires_at, e.last_heartbeat, lock_id),
                )
            self._metrics["heartbeat_total"] += 1
            return e.to_dict()

    def _evict_locked(self, e: LockEntry, reason: str) -> None:
        """state_lock 已持有, 踢一个 lock."""
        self._locks.pop(e.lock_id, None)
        self._dr_index.pop((e.device_id, e.resource), None)
        with self._conn() as c:
            c.execute("DELETE FROM cluster_locks WHERE lock_id = ?", (e.lock_id,))
        if reason == "ttl_expired":
            self._metrics["ttl_expired_total"] += 1
        elif reason == "preempted":
            self._metrics["evicted_total"] += 1
        self._audit(reason, e)
        logger.info(
            "[cluster_lock] evicted lock=%s worker=%s device=%s resource=%s reason=%s",
            e.lock_id[:8], e.worker_id, e.device_id[:8], e.resource, reason,
        )

    # ── 查询 ──────────────────────────────────────────────────────
    def list_locks(
        self,
        worker_id: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._state_lock:
            out = []
            for e in self._locks.values():
                if worker_id and e.worker_id != worker_id:
                    continue
                if device_id and e.device_id != device_id:
                    continue
                out.append(e.to_dict())
            return out

    def get_lock(self, lock_id: str) -> Optional[Dict[str, Any]]:
        with self._state_lock:
            e = self._locks.get(lock_id)
            return e.to_dict() if e else None

    def metrics(self) -> Dict[str, Any]:
        with self._state_lock:
            snap = dict(self._metrics)
            snap["active_count"] = len(self._locks)
            snap["unique_devices_locked"] = len({
                e.device_id for e in self._locks.values()
            })
            return snap

    # ── 后台 cleanup 线程 ─────────────────────────────────────────
    def start_cleanup_thread(self) -> None:
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return
        self._cleanup_stop.clear()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="cluster-lock-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()
        logger.info("[cluster_lock] cleanup thread started")

    def stop_cleanup_thread(self) -> None:
        self._cleanup_stop.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5.0)

    def _cleanup_loop(self) -> None:
        last_audit_gc = time.time()
        while not self._cleanup_stop.is_set():
            try:
                self._cleanup_once()
                # 每 60s GC 一次 audit log (留 7 天)
                if time.time() - last_audit_gc > 60.0:
                    self._cleanup_audit_old()
                    last_audit_gc = time.time()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[cluster_lock] cleanup error: %s", exc)
            self._cleanup_stop.wait(TTL_CLEANUP_INTERVAL_SEC)

    def _cleanup_once(self) -> int:
        """扫过期锁, 返回清理数量."""
        now = time.time()
        with self._state_lock:
            expired = [e for e in self._locks.values() if e.is_expired(now)]
            for e in expired:
                self._evict_locked(e, "ttl_expired")
            return len(expired)

    def _cleanup_audit_old(self, retain_days: float = 7.0) -> int:
        """删除 retain_days 天前的 audit log, 防止表无限膨胀.

        200 设备 × 17 ops/sec × 86400s = 1.5M rows/day, 7 天 = 10M rows.
        SQLite 这个量级 OK, 但定期 GC 让备份/查询更轻.
        """
        try:
            cutoff = time.time() - retain_days * 86400
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM cluster_lock_audit WHERE ts < ?", (cutoff,)
                )
                deleted = cur.rowcount or 0
            if deleted > 0:
                logger.info("[cluster_lock] GC'd %d old audit rows (>%.0fd)",
                            deleted, retain_days)
            return deleted
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cluster_lock] audit GC failed: %s", exc)
            return 0


# ── 单例 ──────────────────────────────────────────────────────────────
_DEFAULT_DB_PATH = Path(
    os.environ.get(
        "CLUSTER_LOCK_DB",
        str(Path(__file__).resolve().parents[2] / "config" / "cluster_locks.db"),
    )
)

_lock_service_singleton: Optional[ClusterLockService] = None
_singleton_lock = threading.Lock()


def get_lock_service() -> ClusterLockService:
    """获取/初始化单例."""
    global _lock_service_singleton
    if _lock_service_singleton is not None:
        return _lock_service_singleton
    with _singleton_lock:
        if _lock_service_singleton is None:
            _lock_service_singleton = ClusterLockService(_DEFAULT_DB_PATH)
            _lock_service_singleton.start_cleanup_thread()
        return _lock_service_singleton


def reset_for_tests(db_path: Optional[Path] = None) -> ClusterLockService:
    """仅测试用: 重置单例."""
    global _lock_service_singleton
    with _singleton_lock:
        if _lock_service_singleton is not None:
            _lock_service_singleton.stop_cleanup_thread()
        _lock_service_singleton = ClusterLockService(db_path or _DEFAULT_DB_PATH)
        _lock_service_singleton.start_cleanup_thread()
        return _lock_service_singleton
