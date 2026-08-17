# -*- coding: utf-8 -*-
"""回复时延 SLO 聚合门禁（P1-8，2026-08-09）。

钉三层：
1. ``build_episodes`` 纯语义——burst 折叠 / 我方主动开口不产生段 / 尾部未回复；
2. ``build_reply_latency`` 端到端（真 InboxStore）——适用会话筛选（私聊、剔
   bot/群/系统会话）、时延数学、零回复宽限、窗口归因、分桶、平台切片；
3. metrics 消费面接线（静态契约）+ TTL 缓存语义。
"""
from __future__ import annotations

import time
from pathlib import Path

from src.inbox.store import InboxStore
from src.ops.reply_latency import (
    GRACE_UNANSWERED_S,
    build_episodes,
    build_reply_latency,
    reply_latency_snapshot,
    reset_snapshot_cache,
    _percentile,
)

ENGINE_ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0  # 固定时钟：断言全部确定性
HOUR = 3600.0


# ── 纯函数层 ───────────────────────────────────────────────────────────────


def test_build_episodes_folds_burst_to_first_inbound():
    rows = [("in", 100.0), ("in", 105.0), ("in", 110.0), ("out", 160.0)]
    assert build_episodes(rows) == [(100.0, 160.0)]  # 时延按首条入站算


def test_build_episodes_outbound_without_pending_inbound_is_ignored():
    # 我方主动开口（proactive/坐席先发话）不构成「客户在等」
    rows = [("out", 50.0), ("in", 100.0), ("out", 130.0), ("out", 140.0)]
    assert build_episodes(rows) == [(100.0, 130.0)]


def test_build_episodes_trailing_unanswered():
    rows = [("in", 100.0), ("out", 120.0), ("in", 200.0), ("in", 210.0)]
    assert build_episodes(rows) == [(100.0, 120.0), (200.0, None)]


def test_percentile_edges():
    assert _percentile([], 0.95) == 0.0
    assert _percentile([7.0], 0.5) == 7.0
    assert _percentile([0.0, 100.0], 0.5) == 50.0  # 线性插值


# ── 端到端（真 store） ─────────────────────────────────────────────────────


def _seed_conv(store, cid, *, platform="telegram", account="acct1",
               chat_key="peer", chat_type="private", peer_is_bot=0,
               username=""):
    with store._lock:
        store._conn.execute(
            "INSERT INTO conversations (conversation_id, platform, account_id,"
            " chat_key, chat_type, peer_is_bot, username, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, platform, account, chat_key, chat_type, peer_is_bot,
             username, NOW - 30 * 86400, NOW - 30 * 86400))
        store._conn.commit()


def _seed_msgs(store, cid, rows):
    import uuid
    with store._lock:
        for direction, ts in rows:
            store._conn.execute(
                "INSERT INTO messages (message_id, conversation_id, direction,"
                " ts, ingested_at) VALUES (?,?,?,?,?)",
                (f"{cid}:{uuid.uuid4().hex[:10]}", cid, direction, ts, ts))
        store._conn.commit()


def test_end_to_end_latency_windows_and_filters(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 适用会话：私聊真人
    _seed_conv(store, "telegram:acct1:1001", chat_key="1001")
    _seed_msgs(store, "telegram:acct1:1001", [
        ("in", NOW - 2 * HOUR), ("out", NOW - 2 * HOUR + 30),       # 30s
        ("in", NOW - 1 * HOUR), ("in", NOW - 1 * HOUR + 5),
        ("out", NOW - 1 * HOUR + 90),                               # 90s（burst）
        # 同一未回 burst 的两条入站：等待从首条(NOW-2000)起算 → 一段零回复
        # （宽限内的第二条不另起段——这正是「客户视角最坏等待」的语义）
        ("in", NOW - 2000),
        ("in", NOW - 100),
    ])
    # 昨天窗（d1_prev）：一段 600s 的慢回复
    _seed_conv(store, "line:acct1:2001", platform="line", chat_key="2001")
    _seed_msgs(store, "line:acct1:2001", [
        ("in", NOW - 30 * HOUR), ("out", NOW - 30 * HOUR + 600),
    ])
    # 不适用会话们：bot / 群聊 / TG 服务号——全部必须被剔除
    _seed_conv(store, "telegram:acct1:bot1", chat_key="bot1",
               username="somebot")
    _seed_msgs(store, "telegram:acct1:bot1", [
        ("in", NOW - HOUR), ("out", NOW - HOUR + 1)])
    _seed_conv(store, "telegram:acct1:g1", chat_key="g1", chat_type="group")
    _seed_msgs(store, "telegram:acct1:g1", [
        ("in", NOW - HOUR), ("out", NOW - HOUR + 1)])
    _seed_conv(store, "telegram:acct1:777000", chat_key="777000")
    _seed_msgs(store, "telegram:acct1:777000", [
        ("in", NOW - HOUR), ("out", NOW - HOUR + 1)])

    data = build_reply_latency(store, now=NOW)
    assert data, "聚合不应软失败"
    d1 = data["d1"]
    # 24h 窗：2 replied（30s/90s）+ 1 unanswered —— bot/群/服务号零掺入
    assert d1["episodes"] == 3
    assert d1["replied"] == 2
    assert d1["unanswered"] == 1
    assert d1["pending_grace"] == 0
    assert d1["p50_s"] == 60.0          # (30+90)/2 线性插值
    assert d1["max_s"] == 90.0
    # 分桶：30s 不小于边界 30 → 落 <60 桶；90s 落 <5m 桶
    assert d1["buckets"][1] == 0
    assert d1["buckets"][2] == 1 and d1["buckets"][3] == 1
    # 平台切片只有 telegram（line 的段在昨天窗）
    assert set(d1["by_platform"].keys()) == {"telegram"}
    # 昨天窗：line 的 600s 慢回复
    prev = data["d1_prev"]
    assert prev["replied"] == 1 and prev["p95_s"] == 600.0
    # 7 天窗聚合两天全部段
    assert data["d7"]["episodes"] == 4


def test_unanswered_respects_grace(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "telegram:a:1", chat_key="1")
    _seed_msgs(store, "telegram:a:1",
               [("in", NOW - GRACE_UNANSWERED_S + 60)])  # 还差 60s 到宽限
    d1 = build_reply_latency(store, now=NOW)["d1"]
    assert d1["unanswered"] == 0 and d1["pending_grace"] == 1


def test_snapshot_ttl_cache(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "telegram:a:1", chat_key="1")
    _seed_msgs(store, "telegram:a:1", [("in", NOW - 500), ("out", NOW - 480)])
    reset_snapshot_cache()
    s1 = reply_latency_snapshot(store, now=NOW)
    assert s1["d1"]["replied"] == 1
    # 缓存窗内：新写入不可见（同一对象直接返回）
    _seed_msgs(store, "telegram:a:1", [("in", NOW - 60), ("out", NOW - 30)])
    s2 = reply_latency_snapshot(store, now=NOW + 10)
    assert s2 is s1
    # 缓存过期后重算
    s3 = reply_latency_snapshot(store, now=NOW + 400)
    assert s3["d1"]["replied"] == 2
    reset_snapshot_cache()


def test_bbroken_store_soft_fails():
    class _Boom:
        @property
        def _lock(self):
            raise RuntimeError("no store")

    assert build_reply_latency(_Boom(), now=NOW) == {}


# ── 消费面接线（静态契约） ──────────────────────────────────────────────────


def test_metrics_endpoint_wired():
    src = (ENGINE_ROOT / "src" / "web" / "routes" / "drafts_routes.py"
           ).read_text(encoding="utf-8", errors="replace")
    assert "reply_latency_snapshot" in src, "metrics 端点必须挂 reply_latency 段"
    assert 'metrics["reply_latency"]' in src
    assert "ws_reply_latency_p95_seconds" in src, "Prometheus gauge 必须同步暴露"
