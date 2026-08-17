# -*- coding: utf-8 -*-
"""回复节奏 Stage2 门禁（2026-08-05）：延迟窗防线补齐 + 条间节奏观测。

背景：拟人延迟可配 30-60s 后，B 线出现两个新暴露面——
  ① worker 的 fresh_guard 过期检查跑在延迟**之前**，整个延迟窗口不设防：
     客户此间插话，旧稿照发＝答非所问 + 新稿接踵而至＝两连发；
  ② 分条条间隔可达 20s，客户条间插话时剩余 bubble 照发（A 线有
     interject_absorb 条间中止，B 线没有）。
本文件锁定：
  - draft_fresh_guard.interrupted_by_inbound 纯函数语义（宽口径：任何新入站
    都算打断——与 find_superseding_inbound 的严筛选**刻意不同**，见 docstring）；
  - worker 延迟后二次过期复查（post_humanize gate）：拦下不发送、计
    total_superseded 不计 error 不喂熔断；关闭/同文本/重试项不误拦；
  - humanize_metrics.record_bubble_gap 观测口径；
  - B 线 helpers / A 线 telegram_client 的静态接线不被回退。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.draft_fresh_guard import interrupted_by_inbound

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── interrupted_by_inbound 纯函数 ─────────────────────────────────────────


def _row(direction: str, ts: float, text: str = "", media: str = "") -> dict:
    return {"direction": direction, "ts": ts, "text": text,
            "media_type": media}


def test_interrupt_hit_on_later_inbound():
    rows = [_row("in", 100.0, "原话"), _row("out", 105.0, "回复"),
            _row("in", 130.0, "插话")]
    hit = interrupted_by_inbound(rows, started_ts=120.0)
    assert hit is not None and hit["ts"] == 130.0


def test_interrupt_ignores_older_and_outbound():
    rows = [_row("in", 100.0, "原话"), _row("out", 130.0, "自己发的")]
    assert interrupted_by_inbound(rows, started_ts=120.0) is None


def test_interrupt_grace_pads_clock_jitter():
    rows = [_row("in", 120.3, "边界")]
    # 默认 grace 0.5：120.3 <= 120 + 0.5 → 不算打断
    assert interrupted_by_inbound(rows, started_ts=120.0) is None
    # 超出垫层 → 算
    rows2 = [_row("in", 120.6, "边界外")]
    assert interrupted_by_inbound(rows2, started_ts=120.0) is not None


def test_interrupt_counts_media_and_same_text():
    """宽口径不变量：纯媒体、同文本都算打断（与 find_superseding 刻意不同）。

    首条 bubble 已发出＝客户已有回复，不存在「取消后零回复」断链；真人被
    打断就是停手，不管对方发来的是什么。
    """
    media_only = [_row("in", 130.0, "", media="voice")]
    assert interrupted_by_inbound(media_only, started_ts=120.0) is not None
    same_text = [_row("in", 130.0, "好的")]
    assert interrupted_by_inbound(
        same_text, started_ts=120.0) is not None


def test_interrupt_picks_latest_hit():
    rows = [_row("in", 125.0, "一"), _row("in", 140.0, "二"),
            _row("in", 133.0, "三")]
    hit = interrupted_by_inbound(rows, started_ts=120.0)
    assert hit is not None and hit["text"] == "二"


def test_interrupt_bad_inputs_never_block():
    assert interrupted_by_inbound(None, started_ts=120.0) is None
    assert interrupted_by_inbound([], started_ts=120.0) is None
    assert interrupted_by_inbound("rows", started_ts=120.0) is None
    assert interrupted_by_inbound([_row("in", 130.0)], started_ts=0) is None
    assert interrupted_by_inbound(
        [_row("in", 130.0)], started_ts="bad") is None
    assert interrupted_by_inbound(
        [{"direction": "in", "ts": "NaN?"}], started_ts=120.0) is None
    # 负 grace 按 0（任何更晚入站都算）
    assert interrupted_by_inbound(
        [_row("in", 120.1)], started_ts=120.0, grace_sec=-5) is not None


# ── record_bubble_gap 观测 ────────────────────────────────────────────────


def test_record_bubble_gap_snapshot_shape():
    from src.integrations.humanize_metrics import (
        pacing_snapshot,
        record_bubble_gap,
    )
    src = f"t{uuid.uuid4().hex[:8]}"           # 唯一 source 防并行测试串味
    record_bubble_gap(src, "Telegram", 4.0)
    record_bubble_gap(src, "telegram", 8.0)
    row = pacing_snapshot()[f"bubble_gap/{src}/telegram"]
    assert row["count"] == 2
    assert row["avg_delay"] == 6.0
    assert row["max_delay"] == 8.0
    assert row["last_delay"] == 8.0


def test_record_bubble_gap_bad_input_noop():
    from src.integrations.humanize_metrics import (
        pacing_snapshot,
        record_bubble_gap,
    )
    src = f"t{uuid.uuid4().hex[:8]}"
    record_bubble_gap(src, "telegram", "not-a-number")
    assert f"bubble_gap/{src}/telegram" not in pacing_snapshot()
    # 空 source/platform 归一化，不炸
    record_bubble_gap("", "", 1.5)
    assert pacing_snapshot()["bubble_gap/other/-"]["count"] >= 1


# ── worker 延迟后二次过期复查（post_humanize gate） ───────────────────────


class _FakeSvc:
    """最小草稿服务（形状对齐 test_autosend_dup_guard_wiring._FakeSvc）。"""

    def __init__(self):
        self.queue = []
        self.resolved = []
        self._store = None

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append((draft_id, action))
        return {"ok": True}


class _StatefulStore:
    """第 1 次查询（_process_batch 预检）返回空，之后返回给定行。

    专门用来把「插话发生在拟人延迟窗内」这个时序摆到测试里：预检时插话
    还没到（放行 resolve），延迟后复查时已到（应拦投递）。
    """

    def __init__(self, rows_after_first):
        self.calls = 0
        self.rows_after_first = rows_after_first

    def list_recent_messages(self, cid, limit=8):
        self.calls += 1
        return [] if self.calls <= 1 else list(self.rows_after_first)


def _draft(conv, text, *, peer_text="今天有空吗", created_ago=60.0):
    return {
        "draft_id": f"d-{uuid.uuid4().hex[:8]}", "autopilot_level": "L2",
        "final_text": text, "platform": "telegram",
        "account_id": "a1", "chat_key": "c1", "conversation_id": conv,
        "created_ts": time.time() - created_ago,
        "peer_text": peer_text,
    }


def _worker(svc, sent, *, fg_enabled=True):
    async def _send_cb(platform, account_id, chat_key, text):
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    return AutosendWorker(
        draft_service=svc, send_callback=_send_cb, sleep=_sleep,
        fresh_guard_cfg={"enabled": fg_enabled, "grace_sec": 3.0,
                         "min_text_len": 0},
    )


@pytest.mark.asyncio
async def test_post_humanize_recheck_blocks_stale():
    """延迟窗内客户插话（不同文本）→ 本条不投递，计 superseded 不计 error。"""
    conv = f"t-ph-{uuid.uuid4().hex[:8]}"
    svc = _FakeSvc()
    svc._store = _StatefulStore(
        [{"direction": "in", "ts": time.time() + 5, "text": "换个话题问你"}])
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, "旧稿回复")]
    await w._tick()
    assert sent == []                          # 没发出去
    assert w.total_superseded == 1
    assert w.total_deliver_errors == 0         # 不算投递错误
    assert w._consecutive_errors == 0          # 不喂熔断
    assert len(svc.resolved) == 1              # 预检时插话未到 → 正常 resolve
    assert svc._store.calls >= 2               # 预检 + 延迟后复查各一次


@pytest.mark.asyncio
async def test_post_humanize_recheck_same_text_passes():
    """同文本插话不拦：幂等跳过不会催生新稿，拦了=客户零回复。"""
    conv = f"t-ph-{uuid.uuid4().hex[:8]}"
    svc = _FakeSvc()
    svc._store = _StatefulStore(
        [{"direction": "in", "ts": time.time() + 5, "text": "今天有空吗"}])
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, "旧稿回复", peer_text="今天有空吗")]
    await w._tick()
    assert sent == ["旧稿回复"]
    assert w.total_superseded == 0


@pytest.mark.asyncio
async def test_post_humanize_recheck_disabled_is_noop():
    conv = f"t-ph-{uuid.uuid4().hex[:8]}"
    svc = _FakeSvc()
    svc._store = _StatefulStore(
        [{"direction": "in", "ts": time.time() + 5, "text": "换个话题"}])
    sent = []
    w = _worker(svc, sent, fg_enabled=False)
    svc.queue = [_draft(conv, "旧稿回复")]
    await w._tick()
    assert sent == ["旧稿回复"]               # 守卫关 = 零行为变更
    assert w.total_superseded == 0


@pytest.mark.asyncio
async def test_post_humanize_recheck_skips_retry_items():
    """重试项 _attempt>0 免检：重发同文本是 recoverable 的既定语义。"""
    conv = f"t-ph-{uuid.uuid4().hex[:8]}"
    svc = _FakeSvc()

    class _AlwaysHitStore:
        def list_recent_messages(self, cid, limit=8):
            return [{"direction": "in", "ts": time.time() + 5,
                     "text": "换个话题"}]

    svc._store = _AlwaysHitStore()
    sent = []
    w = _worker(svc, sent)
    w._recoverable = True
    item = _draft(conv, "重试文本")
    item["text"] = "重试文本"
    item["_attempt"] = 1
    w._retry_queue.append({"item": item, "next_ts": 0})
    await w._tick()
    assert sent == ["重试文本"]
    assert w.total_superseded == 0


@pytest.mark.asyncio
async def test_post_humanize_recheck_store_error_fails_open():
    """守卫自身异常 → 放行投递（宁发旧稿不可断链）。"""
    conv = f"t-ph-{uuid.uuid4().hex[:8]}"
    svc = _FakeSvc()

    class _BoomStore:
        def __init__(self):
            self.calls = 0

        def list_recent_messages(self, cid, limit=8):
            self.calls += 1
            if self.calls <= 1:
                return []
            raise RuntimeError("db locked")

    svc._store = _BoomStore()
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, "旧稿回复")]
    await w._tick()
    assert sent == ["旧稿回复"]
    assert w.total_superseded == 0
    assert w.total_deliver_errors == 0


# ── 静态接线（防重构悄悄拆线） ────────────────────────────────────────────


def test_static_wiring_not_reverted():
    worker_src = (_ENGINE_ROOT / "src" / "inbox" / "autosend_worker.py"
                  ).read_text(encoding="utf-8")
    # 预检 + 延迟后复查各一次调用（共 ≥2 处引用 + post_humanize 标记）
    assert worker_src.count("find_superseding_inbound") >= 3  # import×2+调用
    assert "post_humanize" in worker_src
    assert '"peer_text"' in worker_src         # to_deliver 载荷带 peer_text

    helpers_src = (_ENGINE_ROOT / "src" / "inbox" / "autosend_helpers.py"
                   ).read_text(encoding="utf-8")
    assert "interrupted_by_inbound" in helpers_src
    assert "record_bubble_gap" in helpers_src

    tg_src = (_ENGINE_ROOT / "src" / "client" / "telegram_client.py"
              ).read_text(encoding="utf-8")
    assert "record_bubble_gap" in tg_src
