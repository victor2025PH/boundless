# -*- coding: utf-8 -*-
"""失败队列 drain 后台线程 + push 失败队列 backoff/死信表 测试.

覆盖:
1. 指数 backoff: 失败后 next_retry_at 设对
2. drain 只取到期条目 (next_retry_at <= now)
3. 死信表: attempts > THRESHOLD 移过去
4. drain 锁优化: enqueue 跟 drain 不互相阻塞 (并发场景)
5. _DrainThread 启动 / 停止 / 异常恢复
6. metrics counters 正确累加
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.host import central_push_client as cli
from src.host import central_push_drain as drain_mod


# ── fixtures ─────────────────────────────────────────────────────────
@pytest.fixture
def reset_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_DEFAULT_QUEUE_DB", str(tmp_path / "drain_q.db"))
    monkeypatch.setattr(cli, "_retry_store_singleton", None)
    monkeypatch.setattr(cli, "_async_executor", None)
    cli.reset_push_metrics_for_tests()
    drain_mod.reset_for_tests()
    yield
    # cleanup: 停 drain 线程
    # critical 顺序约定 (2026-05-04 Stage D.2): 此处 stop 跑在 monkeypatch
    # undo 之前 (pytest fixture LIFO teardown). 删了之后 daemon 在 monkeypatch
    # undo 后才被 conftest P2-⑨ 兜底 stop, 这期间 thread tick 调真
    # _http_post_json (mock 已 undo) → 真 push_total inc → 污染下一 test.
    # C.2 的 reset_for_tests stop+join 是 conftest 兜底, 不替代此处 primary stop.
    drain_mod.stop_drain_thread(timeout_sec=2.0)


# ── backoff schedule ─────────────────────────────────────────────────
def test_backoff_exponential_capped(reset_state):
    store = cli.get_retry_store()
    # 30 × 2^0 = 30
    assert store._backoff(0) == 30.0
    # 30 × 2^1 = 60
    assert store._backoff(1) == 60.0
    # 30 × 2^2 = 120
    assert store._backoff(2) == 120.0
    # 30 × 2^7 = 3840 → 上限 3600
    assert store._backoff(7) == 3600.0
    # 30 × 2^20 → 远超上限, 仍 = 3600
    assert store._backoff(20) == 3600.0


def test_failed_drain_sets_next_retry_at(reset_state, monkeypatch):
    """drain 失败后, 该条目 next_retry_at 应推后, 下轮立即 drain 不再扫到."""
    monkeypatch.setattr(
        cli, "_http_post_json",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    store = cli.get_retry_store()
    store.enqueue("/cluster/customers/upsert", {"x": 1})
    assert store.pending_count_due() == 1

    # 第一次 drain 失败, 设 next_retry_at = now + 60s (attempts=0→1, backoff(1)=60)
    store.drain(limit=10)
    # 立即再 drain, 不应扫到 (next_retry_at 在未来)
    assert store.pending_count_due() == 0
    # 但总数还在
    assert store.pending_count() == 1


def test_drain_picks_due_items_only(reset_state, monkeypatch):
    """drain 只扫 next_retry_at <= now 的, 未到期的留下来."""
    posted = []
    monkeypatch.setattr(
        cli, "_http_post_json",
        lambda path, body, **kw: posted.append(body) or {},
    )
    store = cli.get_retry_store()
    store.enqueue("/p", {"a": 1})
    # 手动改一条到未来
    with store._lock, store._conn() as c:
        c.execute(
            "UPDATE push_queue SET next_retry_at = ? WHERE id = 1",
            (time.time() + 3600,),
        )
    store.enqueue("/p", {"b": 2})  # 这条立即可重试

    drained = store.drain(limit=10)
    assert drained == 1  # 只 push 了 b
    assert posted == [{"b": 2}]


# ── 死信表 ───────────────────────────────────────────────────────────
def test_dead_letter_threshold_moves_item(reset_state, monkeypatch):
    monkeypatch.setattr(
        cli, "_http_post_json",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("permafail")),
    )
    # 临时把 threshold 调小, 不用真跑 100 次
    monkeypatch.setattr(cli, "DEAD_LETTER_THRESHOLD", 2)
    store = cli.get_retry_store()
    store.enqueue("/p", {"x": 1})

    # 跑 3 次 drain, 强制 next_retry_at 立即可用
    for _ in range(3):
        with store._lock, store._conn() as c:
            c.execute("UPDATE push_queue SET next_retry_at = 0")
        store.drain(limit=10)

    # 主队列空, 死信表 1 条
    assert store.pending_count() == 0
    assert store.dead_letter_count() == 1


# ── drain 锁优化 (enqueue 不被阻塞) ───────────────────────────────────
def test_drain_does_not_block_enqueue(reset_state, monkeypatch):
    """drain 长任务时, enqueue 仍能进 (锁优化的核心 invariant)."""
    drain_started = threading.Event()
    drain_can_finish = threading.Event()
    enqueue_done = threading.Event()

    def slow_post(*a, **kw):
        drain_started.set()
        drain_can_finish.wait(2.0)
        return {}

    monkeypatch.setattr(cli, "_http_post_json", slow_post)
    store = cli.get_retry_store()
    store.enqueue("/p", {"id": 1})

    drain_thread = threading.Thread(target=lambda: store.drain(limit=10))
    drain_thread.start()

    # drain 锁住 SELECT 后释放, 进入 push (slow_post). 此时 enqueue 应可进.
    drain_started.wait(2.0)
    assert drain_started.is_set()

    # enqueue 必须能在 1s 内完成 (没被 drain 阻塞)
    def do_enqueue():
        store.enqueue("/p", {"id": 2})
        enqueue_done.set()

    enq_thread = threading.Thread(target=do_enqueue)
    enq_thread.start()
    enqueue_done.wait(1.0)
    assert enqueue_done.is_set(), "enqueue 被 drain 阻塞超过 1s"

    drain_can_finish.set()
    drain_thread.join(2.0)
    enq_thread.join(2.0)


# ── _DrainThread ─────────────────────────────────────────────────────
def test_drain_thread_starts_and_stops(reset_state):
    t = drain_mod.start_drain_thread(
        interval_sec=5.0, limit=50, startup_delay_sec=0,
    )
    assert t.is_alive()
    status = drain_mod.get_drain_status()
    assert status["running"] is True
    assert status["interval_sec"] == 5.0
    assert status["limit"] == 50

    ok = drain_mod.stop_drain_thread(timeout_sec=2.0)
    assert ok is True


def test_drain_thread_idempotent_start(reset_state):
    """重复 start 返回同一线程, 不创建多个."""
    t1 = drain_mod.start_drain_thread(interval_sec=10.0, startup_delay_sec=0)
    t2 = drain_mod.start_drain_thread(interval_sec=99.0, startup_delay_sec=0)
    assert t1 is t2
    drain_mod.stop_drain_thread()


def test_drain_thread_runs_tick_periodically(reset_state, monkeypatch):
    """启动后 wait 一段, 看 iterations 是否累加."""
    drained_calls = []

    class FakeStore:
        def drain(self, limit):
            drained_calls.append(limit)
            return 0

    monkeypatch.setattr(cli, "get_retry_store", lambda: FakeStore())
    t = drain_mod.start_drain_thread(
        interval_sec=0.1, limit=42, startup_delay_sec=0,
    )
    time.sleep(0.4)  # 应跑 3-4 次
    drain_mod.stop_drain_thread(timeout_sec=1.0)

    assert len(drained_calls) >= 2
    assert all(c == 42 for c in drained_calls)


def test_drain_thread_survives_exception(reset_state, monkeypatch):
    """drain.tick 抛异常时线程不死, 下轮还能跑."""
    calls = [0]

    class BrokenStore:
        def drain(self, limit):
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("boom")
            return 0

    monkeypatch.setattr(cli, "get_retry_store", lambda: BrokenStore())
    drain_mod.start_drain_thread(
        interval_sec=0.1, startup_delay_sec=0,
    )
    time.sleep(0.4)
    drain_mod.stop_drain_thread(timeout_sec=1.0)

    # 第一次抛异常, 后续仍应继续跑
    assert calls[0] >= 2


def test_drain_thread_stop_unblocks_immediately(reset_state):
    """stop() 应让 wait() 立即返回, 不等满 interval."""
    drain_mod.start_drain_thread(interval_sec=300.0, startup_delay_sec=0)
    t0 = time.time()
    drain_mod.stop_drain_thread(timeout_sec=2.0)
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"stop 花了 {elapsed:.1f}s, 应 < 2s"


# ── metrics ──────────────────────────────────────────────────────────
class _FakeUrlOpen:
    """mock urlopen, 让真 _http_post_json 跑 (含 metric inc)."""

    def __init__(self, payload=b'{"customer_id":"c1"}'):
        self._payload = payload

    def __call__(self, req, timeout=10.0):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def test_metrics_increments_on_success(reset_state, monkeypatch):
    """走真 _http_post_json (mock urlopen), 验证 success counters."""
    monkeypatch.setattr(cli._ureq, "urlopen", _FakeUrlOpen())
    cli._http_post_json("/p", {"x": 1})

    m = cli.get_push_metrics()
    assert m["push_total"] == 1
    assert m["push_success"] == 1
    assert m["push_failure"] == 0
    assert m["push_4xx"] == 0


def test_metrics_increments_on_4xx(reset_state, monkeypatch):
    """4xx HTTPError 应 inc push_4xx + push_failure, 不重试."""
    from urllib.error import HTTPError
    import io

    def raise_400(req, timeout=10.0):
        raise HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b"invalid"))

    monkeypatch.setattr(cli._ureq, "urlopen", raise_400)

    with pytest.raises(RuntimeError, match="HTTP 400"):
        cli._http_post_json("/p", {"x": 1})

    m = cli.get_push_metrics()
    assert m["push_total"] == 1
    assert m["push_4xx"] == 1
    assert m["push_failure"] == 1
    assert m["push_success"] == 0


def test_metrics_increments_on_5xx_after_retries(reset_state, monkeypatch):
    """5xx 重试 N 次都失败应 inc push_failure (不算 4xx)."""
    from urllib.error import HTTPError
    import io

    def raise_503(req, timeout=10.0):
        raise HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b""))

    monkeypatch.setattr(cli._ureq, "urlopen", raise_503)
    monkeypatch.setattr(cli, "DEFAULT_RETRY_TIMES", 0)  # 不等

    with pytest.raises(RuntimeError):
        cli._http_post_json("/p", {"x": 1})

    m = cli.get_push_metrics()
    assert m["push_total"] == 1
    assert m["push_failure"] == 1
    assert m["push_4xx"] == 0
    assert m["push_success"] == 0


def test_metrics_async_enqueue_counter(reset_state, monkeypatch):
    """fire_and_forget 失败时 enqueue, 应 inc push_async_enqueue counter."""
    def always_fail(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(cli, "_http_post_json", always_fail)

    cli.upsert_customer(
        canonical_id="x", canonical_source="facebook",
        worker_id="w1", fire_and_forget=True,
    )
    time.sleep(0.3)  # 等异步线程跑完
    m = cli.get_push_metrics()
    assert m["push_async_enqueue"] == 1
    assert m["queue_pending"] == 1


def test_get_push_metrics_includes_queue_state(reset_state, monkeypatch):
    """get_push_metrics() 应包含 queue_pending / queue_due_now / dead_letter_pending."""
    monkeypatch.setattr(
        cli, "_http_post_json",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    store = cli.get_retry_store()
    store.enqueue("/p", {"a": 1})

    m = cli.get_push_metrics()
    assert m["queue_pending"] == 1
    assert m["queue_due_now"] >= 1
    assert m["dead_letter_pending"] == 0


# ── schema migration v1 → v2 ─────────────────────────────────────────
def test_existing_v1_db_gets_next_retry_at_added(reset_state, tmp_path):
    """旧 worker 的 push_queue 没有 next_retry_at, init 时应自动 ALTER 加列."""
    import sqlite3
    db = tmp_path / "v1.db"
    # 模拟 v1 schema (没 next_retry_at)
    c = sqlite3.connect(str(db))
    c.execute("""
        CREATE TABLE push_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL, body TEXT NOT NULL,
            enqueued_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )
    """)
    c.execute(
        "INSERT INTO push_queue (path, body, enqueued_at) VALUES (?,?,?)",
        ("/p", '{"x":1}', time.time()),
    )
    c.commit()
    c.close()

    # init store 应 ALTER 加 next_retry_at, 旧数据保留
    store = cli.EnqueueRetryStore(db_path=str(db))
    cols = set()
    with store._conn() as c:
        for r in c.execute("PRAGMA table_info(push_queue)"):
            cols.add(r[1])
    assert "next_retry_at" in cols
    assert store.pending_count() == 1
