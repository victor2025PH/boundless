# -*- coding: utf-8 -*-
"""L2 中央客户画像 — Worker 侧 push client SDK.

动机
----
Worker (W03 / W175 / 主控自己) 业务发生事件时, 通过 HTTP 调主控
``/cluster/customers/...`` 端点 push 到中央 PG. 本模块封装:

* HTTP 调用 (复用 ``cluster_lock_client.get_coordinator_url`` 配置)
* 失败重试 (指数 backoff)
* 可选 ``fire_and_forget`` 异步模式 (不阻塞 worker 业务)
* 本地 SQLite 失败队列 (网络断时缓存, 恢复后回补) — 见 ``EnqueueRetryStore``

使用
----
::

    from src.host.central_push_client import (
        upsert_customer, record_event, record_chat,
        initiate_handoff, accept_handoff,
    )

    # 同步调用 (业务流程必须知道结果)
    cust_id = upsert_customer(
        canonical_id="fb_uid_123",
        canonical_source="facebook",
        primary_name="さとう",
        ai_profile={"topics": ["food"]},
        worker_id="worker-175",
    )

    # 异步 fire-and-forget (高频 chat / event push)
    record_chat(
        customer_id=cust_id, channel="messenger",
        direction="outgoing", content="...",
        fire_and_forget=True,
    )

线程安全
--------
- 同步函数无共享状态, 安全
- 异步队列用 module-level ThreadPoolExecutor (单例, lazy init)

设计取舍
--------
- 不直接连 PG (需 worker 装 psycopg2 + 知道 PG 凭证). HTTP 解耦, 走主控
  API key + secret. 后续如有性能瓶颈再考虑直连
- 失败队列用本地 SQLite: 业务死时数据不丢; 恢复后回补
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error as _uerr, request as _ureq

logger = logging.getLogger(__name__)

DEFAULT_HTTP_TIMEOUT = 10.0
DEFAULT_RETRY_TIMES = 3
DEFAULT_RETRY_BACKOFF = 1.5  # 1.5x 每次

# 失败队列指数 backoff: attempts=1 → 30s, =2 → 60s, =3 → 120s ... 上限 1h
DRAIN_BACKOFF_BASE_SEC = 30.0
DRAIN_BACKOFF_MULTIPLIER = 2.0
DRAIN_BACKOFF_MAX_SEC = 3600.0
# 超过此次数移到死信表
DEAD_LETTER_THRESHOLD = 100

# ── module-level metrics counters ─────────────────────────────────────
_metrics_lock = threading.Lock()
_metrics: Dict[str, int] = {
    "push_total": 0,            # 所有 push 调用 (sync + async)
    "push_success": 0,          # 成功 (HTTP 2xx)
    "push_failure": 0,          # 失败 (5xx / network / 4xx)
    "push_4xx": 0,              # 4xx 业务错误 (单独计, 不重试)
    "push_async_enqueue": 0,    # fire_and_forget 提交到 executor
    "drain_attempts": 0,        # drain 调用次数
    "drain_success": 0,         # drain 内成功 push 的条数
    "drain_failure": 0,         # drain 内仍失败的条数
    "dead_letter_total": 0,     # 累计移到死信表的条数
}


def _metric_inc(name: str, n: int = 1) -> None:
    with _metrics_lock:
        _metrics[name] = _metrics.get(name, 0) + n


def get_push_metrics() -> Dict[str, Any]:
    """快照 push 失败队列状态 + counters. 暴露到 /cluster/customers/push/metrics."""
    with _metrics_lock:
        snapshot = dict(_metrics)
    try:
        store = get_retry_store()
        snapshot["queue_pending"] = store.pending_count()
        snapshot["queue_due_now"] = store.pending_count_due()
        snapshot["dead_letter_pending"] = store.dead_letter_count()
    except Exception:  # noqa: BLE001
        snapshot["queue_pending"] = -1
    return snapshot


def reset_push_metrics_for_tests() -> None:
    """仅测试用. 清零 counters."""
    with _metrics_lock:
        for k in _metrics:
            _metrics[k] = 0


def reset_async_executor_for_tests() -> None:
    """仅测试用. shutdown 现有 _async_executor 强制 cancel pending tasks.

    2026-05-04 修: 旧 reset_state fixture 仅 monkeypatch.setattr 设
    _async_executor=None, monkeypatch undo 后旧 executor (E1) 还 alive 持
    4 worker thread + 残留 fire_and_forget task. E1 worker 在下一 test setup
    之后跑 → 调真 _http_post_json (mock 已 undo) → enqueue 本地 → inc
    push_async_enqueue counter → 污染下一 test (test_metrics_async_enqueue_counter
    在 alphabetical 全 suite 跑序下 actual=N+1 vs expected=1).

    fix 同 central_push_drain.reset_for_tests: 进 reset 就 shutdown +
    cancel_futures, 防孤儿 executor leak.
    """
    global _async_executor
    with _async_lock:
        ex = _async_executor
        _async_executor = None
    if ex is not None:
        # 2026-05-04 Stage E.5 修 D.4 副作用: 旧版 wait=True 让 t.join()
        # 在 worker thread 跑真 _http_post_json 卡 30s+ urlopen 时永挂
        # (E.2 全 suite trace: conftest.py:85 → reset_async_executor_for_tests
        # → ex.shutdown wait=True → t.join → 永挂 timeout).
        #
        # 改 wait=False: cancel_futures=True 丢 pending submission, 已 started
        # worker 在 daemon thread (ThreadPoolExecutor worker default daemon=True
        # since Python 3.9) 自然跑完后 process exit 清理.
        #
        # trade-off: 已 started task 跑到结束前可能 inc 1-2 次 metric, 但
        # 因为 fixture 的 _http_post_json monkeypatch 还在 (P2-⑨ teardown
        # LIFO 顺序: 此处 stop 在 monkeypatch undo 之前), worker 跑的还是
        # mock 状态, inc 也在 reset_push_metrics_for_tests 的 scope 里清掉.
        ex.shutdown(wait=False, cancel_futures=True)

# UUIDv5 namespace: worker 离线时也能算出确定性 customer_id, 主控收到 push
# 时若已存在 (canonical_source, canonical_id) 则 ON CONFLICT 走 update 路径,
# customer_id 保持为首次写入时的值 (PK 不变).
_CUSTOMER_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "openclaw.l2.customer")


def compute_customer_id(canonical_source: str, canonical_id: str) -> str:
    """对 (source, id) 算确定性 UUIDv5, worker 离线也能不阻塞算 customer_id."""
    name = f"{canonical_source}:{canonical_id}"
    return str(uuid.uuid5(_CUSTOMER_NS, name))

# ── retry queue 路径 (本地 SQLite, 失败缓存) ────────────────────────
_DEFAULT_QUEUE_DB = os.environ.get(
    "CENTRAL_PUSH_QUEUE_DB",
    str(Path(__file__).resolve().parents[2] / "config" / "central_push_queue.db"),
)


def _coord_url() -> str:
    from src.host.cluster_lock_client import get_coordinator_url
    return get_coordinator_url()


def _api_key_header() -> Dict[str, str]:
    key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
    return {"X-API-Key": key} if key else {}


def _http_post_json(
    path: str,
    body: Dict[str, Any],
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    retries: int = DEFAULT_RETRY_TIMES,
) -> Dict[str, Any]:
    """同步 POST + 重试. 失败则 raise."""
    url = _coord_url().rstrip("/") + path
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(_api_key_header())

    _metric_inc("push_total")
    last_exc: Optional[Exception] = None
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            req = _ureq.Request(url, data=data, method="POST", headers=headers)
            with _ureq.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            _metric_inc("push_success")
            return json.loads(raw)
        except _uerr.HTTPError as e:
            # 4xx 不重试 (业务错误)
            if 400 <= e.code < 500:
                _metric_inc("push_4xx")
                _metric_inc("push_failure")
                try:
                    detail = e.read().decode("utf-8", errors="replace")
                except Exception:
                    detail = str(e)
                raise RuntimeError(f"central push HTTP {e.code}: {detail}") from e
            last_exc = e
        except Exception as e:  # noqa: BLE001
            last_exc = e
        if attempt < retries:
            time.sleep(delay)
            delay *= DEFAULT_RETRY_BACKOFF

    _metric_inc("push_failure")
    raise RuntimeError(f"central push failed after {retries + 1}: {last_exc}")


# ── Phase-9: HTTP GET (用于读 LLM 洞察 readiness) ─────────────────────
def _http_get_json(path: str, timeout: float = 8.0) -> Dict[str, Any]:
    """同步 GET. 失败抛."""
    url = _coord_url().rstrip("/") + path
    headers = _api_key_header()
    req = _ureq.Request(url, method="GET", headers=headers)
    with _ureq.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# ── Phase-9/10: 客户端 readiness 缓存 + 连续 2 次共识 ─────────────────
# Phase-9: 单值 5min 缓存
# Phase-10: 历史 2 个不同 chat_id 的 readiness 都 ≥ early 才返高值, 否则 cap 0.6
#         (防 LLM 单次误判触发早引流)
_READINESS_CACHE: Dict[str, Dict[str, Any]] = {}
_READINESS_HISTORY: Dict[str, list] = {}  # customer_id → [(chat_id, readiness, ts), ...]
_READINESS_LOCK = __import__("threading").Lock()
_READINESS_TTL_SEC = 300.0
_READINESS_CONSENSUS_CAP = 0.6  # 单次误判时返的中性值
_READINESS_CONSENSUS_HIGH = 0.8  # 共识门槛 (与 early_refer_readiness 一致)


def fetch_llm_readiness(customer_id: str) -> Optional[Dict[str, float]]:
    """Phase-9/10: worker 端拉 LLM 洞察的 readiness/frustration. 失败返 None.

    返 {"conversion_readiness": 0-1, "frustration": 0-1} 或 None.
    Phase-9 缓存 5 min.
    Phase-10 共识防呆: 历史最近 2 个不同 chat_id 的 readiness 都 ≥ 0.8 才返实测;
    任一次不够即使本次实测高, 也 cap 到 0.6 (避免单次误判触发早引流).
    """
    if not customer_id:
        return None
    now = time.time()
    with _READINESS_LOCK:
        rec = _READINESS_CACHE.get(customer_id)
        if rec and (now - rec["fetched_at"]) < _READINESS_TTL_SEC:
            return rec["data"]
    try:
        data = _http_get_json(
            f"/cluster/customers/{customer_id}/llm-insight",
            timeout=8.0,
        )
        if not data.get("ok"):
            return None
        raw_readiness = float(data.get("conversion_readiness") or 0.5)
        last_chat_id = "no_chat"  # llm-insight endpoint 缓存 key 已含 chat_id, 但响应没回出
        # 用 cached 字段当变化指示: cached=False 意味着 chat_id 变了 (LLM 重新算了)
        is_new_chat = not bool(data.get("cached"))

        # Phase-10: 共识检查
        consensus_readiness = raw_readiness
        with _READINESS_LOCK:
            hist = _READINESS_HISTORY.setdefault(customer_id, [])
            if is_new_chat:
                hist.append((now, raw_readiness))
                # 只保留最近 5 个
                if len(hist) > 5:
                    del hist[:-5]
            # 共识规则: 历史中最近 2 条 (含本次) 都 ≥ HIGH 才允许高 readiness
            recent = hist[-2:] if len(hist) >= 2 else hist
            if raw_readiness >= _READINESS_CONSENSUS_HIGH:
                if len(recent) < 2 or any(r < _READINESS_CONSENSUS_HIGH for _, r in recent):
                    consensus_readiness = min(raw_readiness, _READINESS_CONSENSUS_CAP)

        out = {
            "conversion_readiness": consensus_readiness,
            "raw_readiness": raw_readiness,  # 留底, 调试可看
            "frustration": 0.0,  # frustration 来自 emotion_scorer, 这里不出
        }
        with _READINESS_LOCK:
            _READINESS_CACHE[customer_id] = {"data": out, "fetched_at": now}
            if len(_READINESS_CACHE) > 1000:
                old = sorted(_READINESS_CACHE.items(), key=lambda kv: kv[1]["fetched_at"])[:200]
                for k, _ in old:
                    _READINESS_CACHE.pop(k, None)
        return out
    except Exception:
        return None


# ── 本地 SQLite retry queue ──────────────────────────────────────────
class EnqueueRetryStore:
    """worker 离线时把 push 写本地 SQLite, 后台 drain 线程扫表回补.

    特性:
    - 指数 backoff: 失败时计算 next_retry_at = now + base × multiplier^attempts
    - 死信表 push_dead_letter: attempts > DEAD_LETTER_THRESHOLD 移过去
    - drain 锁优化: 取出 N 条 → 释放锁 → push → 再拿锁更新, 不阻塞 enqueue
    - schema 平滑升级: __init__ 检测 next_retry_at 列缺失就 ALTER ADD
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = Path(db_path or _DEFAULT_QUEUE_DB)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS push_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    body TEXT NOT NULL,
                    enqueued_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            # v1 → v2: 加 next_retry_at 列 (旧 worker 升级用)
            cols = {r[1] for r in c.execute("PRAGMA table_info(push_queue)")}
            if "next_retry_at" not in cols:
                c.execute(
                    "ALTER TABLE push_queue ADD COLUMN next_retry_at REAL NOT NULL DEFAULT 0"
                )
            # 死信表
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS push_dead_letter (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    body TEXT NOT NULL,
                    enqueued_at REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error TEXT,
                    moved_at REAL NOT NULL
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_queue_due "
                "ON push_queue(next_retry_at)"
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db_path), timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    @staticmethod
    def _backoff(attempts: int) -> float:
        """指数 backoff. attempts=0 → 30s, 1 → 60s, 2 → 120s, ... 上限 1h."""
        delay = DRAIN_BACKOFF_BASE_SEC * (DRAIN_BACKOFF_MULTIPLIER ** attempts)
        return min(delay, DRAIN_BACKOFF_MAX_SEC)

    def enqueue(self, path: str, body: Dict[str, Any]) -> int:
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO push_queue (path, body, enqueued_at, next_retry_at) "
                "VALUES (?, ?, ?, ?)",
                (path, json.dumps(body), now, now),  # 立即可重试
            )
            return cur.lastrowid or 0

    def drain(self, limit: int = 100, now: Optional[float] = None) -> int:
        """扫 next_retry_at <= now 的条目尝试 push. 返回成功数.

        锁优化: SELECT 后释放锁让 enqueue 可进, push 完再加锁更新/删除.
        失败的设 next_retry_at = now + backoff(attempts), 不重复占用资源.
        attempts > DEAD_LETTER_THRESHOLD 移到死信表.
        """
        _metric_inc("drain_attempts")
        now = now if now is not None else time.time()
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, path, body, attempts FROM push_queue "
                "WHERE next_retry_at <= ? "
                "ORDER BY next_retry_at, id LIMIT ?",
                (now, limit),
            ).fetchall()

        ok = 0
        for row in rows:
            qid, path, body_str, attempts = row
            try:
                _http_post_json(path, json.loads(body_str), retries=1)
                # 成功: 删除条目
                with self._lock, self._conn() as c:
                    c.execute("DELETE FROM push_queue WHERE id = ?", (qid,))
                _metric_inc("drain_success")
                ok += 1
            except Exception as e:  # noqa: BLE001
                new_attempts = attempts + 1
                err = str(e)[:300]
                if new_attempts > DEAD_LETTER_THRESHOLD:
                    # 移到死信表
                    with self._lock, self._conn() as c:
                        c.execute(
                            "INSERT INTO push_dead_letter "
                            "(path, body, enqueued_at, attempts, last_error, moved_at) "
                            "SELECT path, body, enqueued_at, ?, ?, ? "
                            "FROM push_queue WHERE id = ?",
                            (new_attempts, err, time.time(), qid),
                        )
                        c.execute("DELETE FROM push_queue WHERE id = ?", (qid,))
                    _metric_inc("dead_letter_total")
                    logger.warning(
                        "[central_push] item id=%d 累计失败 %d 次, 移至死信表. "
                        "last_error: %s", qid, new_attempts, err,
                    )
                else:
                    next_retry = time.time() + self._backoff(new_attempts)
                    with self._lock, self._conn() as c:
                        c.execute(
                            "UPDATE push_queue SET attempts = ?, last_error = ?, "
                            "next_retry_at = ? WHERE id = ?",
                            (new_attempts, err, next_retry, qid),
                        )
                    _metric_inc("drain_failure")
        return ok

    def pending_count(self) -> int:
        """等待中的所有条目 (含未到 next_retry_at 的)."""
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) FROM push_queue").fetchone()
            return int(row[0]) if row else 0

    def pending_count_due(self, now: Optional[float] = None) -> int:
        """到达重试时间, 当前 drain 会扫到的条目数."""
        now = now if now is not None else time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM push_queue WHERE next_retry_at <= ?",
                (now,),
            ).fetchone()
            return int(row[0]) if row else 0

    def dead_letter_count(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) FROM push_dead_letter").fetchone()
            return int(row[0]) if row else 0


_retry_store_singleton: Optional[EnqueueRetryStore] = None
_retry_lock = threading.Lock()


def get_retry_store() -> EnqueueRetryStore:
    global _retry_store_singleton
    if _retry_store_singleton is None:
        with _retry_lock:
            if _retry_store_singleton is None:
                _retry_store_singleton = EnqueueRetryStore()
    return _retry_store_singleton


# ── 异步 fire-and-forget executor ────────────────────────────────────
_async_executor: Optional[ThreadPoolExecutor] = None
_async_lock = threading.Lock()


def _get_async_executor() -> ThreadPoolExecutor:
    global _async_executor
    if _async_executor is None:
        with _async_lock:
            if _async_executor is None:
                _async_executor = ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="central-push-async",
                )
    return _async_executor


def _push_with_retry_queue(path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """sync push + 失败时 enqueue 本地. 返回 server response 或 None.

    2026-05-04 Stage F.1: _async_executor 被 reset (test 间 fixture cleanup
    通过 reset_async_executor_for_tests 设 None) 时, daemon 残留 task
    no-op return. 防 E.5 wait=False 后 daemon 在 fixture cleanup +
    monkeypatch undo 后还在跑, 调真 _http_post_json → fail → enqueue
    SQLite, 让下一 test fixture setup 时 SQLite 被持有 conn.execute 卡死.

    用 _async_executor is None 而非 PYTEST_CURRENT_TEST: 不需要 hardcoded
    test 文件名 exception list (test_metrics_async_enqueue_counter 这种
    显式测 fire_and_forget 的 test 在 fixture 内, _async_executor 仍 set,
    走 normal path; daemon 残留 task 永远在 fixture cleanup 之后, 看到的
    _async_executor 已是 None, no-op). production 0 影响 (function name
    带 _for_tests 不被 prod 调).
    """
    if _async_executor is None:
        # daemon worker 残留 task: executor 已 reset, no-op 静默退出
        return None
    try:
        return _http_post_json(path, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[central_push] %s failed: %s, queue locally", path, exc)
        try:
            get_retry_store().enqueue(path, body)
            _metric_inc("push_async_enqueue")
        except Exception as q_exc:  # noqa: BLE001
            logger.error("[central_push] enqueue local 也失败: %s", q_exc)
        return None


# ── public API ───────────────────────────────────────────────────────
def upsert_customer(
    canonical_id: str,
    canonical_source: str,
    primary_name: Optional[str] = None,
    age_band: Optional[str] = None,
    gender: Optional[str] = None,
    country: Optional[str] = None,
    interests: Optional[List[str]] = None,
    ai_profile: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    worker_id: Optional[str] = None,
    device_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    fire_and_forget: bool = True,
) -> str:
    """upsert. 默认 fire_and_forget — worker 端用 UUIDv5 自算 customer_id,
    push 走异步队列, 主控离线时 worker 不阻塞.

    customer_id 不传时按 (canonical_source, canonical_id) 算 UUIDv5;
    传了就用传的 (用于跨次调用复用).

    返回 customer_id (worker 端自算或调用方自传, sync 模式回退到主控返回值).
    sync 模式失败 raise; fire_and_forget 模式失败 enqueue 本地 retry queue.
    """
    cid = customer_id or compute_customer_id(canonical_source, canonical_id)
    body = {k: v for k, v in dict(
        customer_id=cid,
        canonical_id=canonical_id, canonical_source=canonical_source,
        primary_name=primary_name, age_band=age_band, gender=gender,
        country=country, interests=interests, ai_profile=ai_profile,
        status=status, worker_id=worker_id, device_id=device_id,
    ).items() if v is not None}
    if fire_and_forget:
        _get_async_executor().submit(
            _push_with_retry_queue, "/cluster/customers/upsert", body,
        )
        return cid
    res = _http_post_json("/cluster/customers/upsert", body)
    return res.get("customer_id") or cid


def record_event(
    customer_id: str,
    event_type: str,
    worker_id: str,
    device_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    fire_and_forget: bool = True,  # 高频, 默认异步
) -> Optional[str]:
    body = {k: v for k, v in dict(
        event_type=event_type, worker_id=worker_id,
        device_id=device_id, meta=meta or {},
    ).items() if v is not None}
    path = f"/cluster/customers/{customer_id}/events/push"
    if fire_and_forget:
        _get_async_executor().submit(_push_with_retry_queue, path, body)
        return None
    return _http_post_json(path, body).get("event_id")


def record_chat(
    customer_id: str,
    channel: str,
    direction: str,
    content: str,
    content_lang: Optional[str] = None,
    ai_generated: bool = False,
    template_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    device_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    fire_and_forget: bool = True,  # 高频, 默认异步
) -> Optional[str]:
    body = {k: v for k, v in dict(
        channel=channel, direction=direction, content=content,
        content_lang=content_lang, ai_generated=ai_generated,
        template_id=template_id, worker_id=worker_id, device_id=device_id,
        meta=meta or {},
    ).items() if v is not None}
    path = f"/cluster/customers/{customer_id}/chats/push"
    if fire_and_forget:
        _get_async_executor().submit(_push_with_retry_queue, path, body)
        return None
    return _http_post_json(path, body).get("chat_id")


def initiate_handoff(
    customer_id: str,
    from_stage: str,
    to_stage: str,
    initiating_worker_id: str,
    initiating_device_id: Optional[str] = None,
    ai_summary: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """handoff 启动: sync (业务必须知道 handoff_id 作记录).

    AI summary 强烈建议传 — 人工接管时第一眼看到这一段, 决定接不接.
    """
    body = {k: v for k, v in dict(
        from_stage=from_stage, to_stage=to_stage,
        initiating_worker_id=initiating_worker_id,
        initiating_device_id=initiating_device_id,
        ai_summary=ai_summary, meta=meta or {},
    ).items() if v is not None}
    res = _http_post_json(
        f"/cluster/customers/{customer_id}/handoff/initiate", body,
    )
    return res.get("handoff_id")


def accept_handoff(handoff_id: str, accepted_by_human: str) -> bool:
    res = _http_post_json(
        f"/cluster/customers/handoff/{handoff_id}/accept",
        {"accepted_by_human": accepted_by_human},
    )
    return bool(res.get("accepted"))


def complete_handoff(handoff_id: str, outcome: str) -> bool:
    res = _http_post_json(
        f"/cluster/customers/handoff/{handoff_id}/complete",
        {"outcome": outcome},
    )
    return bool(res.get("completed"))


# ── retry queue drain (background, 可由 worker 定时调) ──────────────
def drain_retry_queue(limit: int = 100) -> int:
    """主动扫本地失败队列回补. 建议 worker 每分钟调一次."""
    return get_retry_store().drain(limit=limit)


def retry_queue_pending() -> int:
    return get_retry_store().pending_count()
