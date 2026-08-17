"""AutosendWorker 并行投递（inbox.l2_autosend.parallel_deliver，2026-08-09）。

背景：拟人节奏上线后（deliver_delay 10-54s + 分条间隔总预算 75s），单条投递
最长可占 ~2 分钟——串行 for 循环下并发会话互相排队（§95 已知边界升级为
实际瓶颈）。并行化语义（本文件钉死）：

  - 默认关＝逐条串行（与旧行为一致；同走 _deliver_one，抽取零语义漂移）；
  - 开启后按 conversation_id 分组：**同会话保序串行**（顺序/防双发不变量），
    跨会话并发、受 max_concurrent 信号量封顶（夹 [1,8]）；
  - 单组异常不拖累其它组（_deliver_one 自吞业务异常 + gather 兜底）；
  - status_snapshot 暴露 parallel_deliver{enabled,max_concurrent,batches}。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

import pytest

from src.inbox.autosend_worker import AutosendWorker


class _FakeSvc:
    """最小草稿服务：返回给定 L2 草稿，resolve 恒成功。"""

    def __init__(self, drafts: List[Dict[str, Any]]):
        self._drafts = drafts
        self.resolved: List[str] = []

    def list_drafts(self, status="pending", limit=200):
        return list(self._drafts)

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


def _draft(i: int, conv: str) -> Dict[str, Any]:
    return {
        "draft_id": f"d{i}", "autopilot_level": "L2",
        "final_text": f"回复{i}", "platform": "telegram",
        "account_id": "a1", "chat_key": f"c-{conv}",
        "conversation_id": conv,
    }


def _item(i: int, conv: str) -> Dict[str, Any]:
    return {
        "draft_id": f"d{i}", "platform": "telegram", "account_id": "a1",
        "chat_key": f"c-{conv}", "text": f"回复{i}", "conversation_id": conv,
    }


class _Recorder:
    """send 回调：记录 (conv, start, end)，可注入时延/异常。"""

    def __init__(self, hold_sec: float = 0.12, fail_convs=()):
        self.calls: List[Dict[str, Any]] = []
        self.hold = hold_sec
        self.fail = set(fail_convs)
        self.live = 0
        self.max_live = 0

    async def __call__(self, platform, account_id, chat_key, text):
        conv = chat_key.replace("c-", "")
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        t0 = time.monotonic()
        try:
            await asyncio.sleep(self.hold)
            if conv in self.fail:
                raise RuntimeError("boom")
            return {"ok": True}
        finally:
            self.live -= 1
            self.calls.append(
                {"conv": conv, "text": text,
                 "start": t0, "end": time.monotonic()})


def _mk_worker(svc, cb, *, parallel: bool, max_concurrent: int = 3):
    return AutosendWorker(
        draft_service=svc,
        config={"parallel_deliver": {
            "enabled": parallel, "max_concurrent": max_concurrent}},
        send_callback=cb,
    )


# ── 调度语义 ────────────────────────────────────────────────────────────────

async def test_parallel_overlaps_across_conversations():
    cb = _Recorder()
    svc = _FakeSvc([_draft(1, "x1"), _draft(2, "x2")])
    w = _mk_worker(svc, cb, parallel=True)
    await w._tick()
    assert len(cb.calls) == 2 and w.total_delivered == 2
    assert cb.max_live == 2, "两个会话应并发在途（跨会话不再排队）"
    assert w.parallel_batches == 1


async def test_serial_when_disabled():
    cb = _Recorder()
    svc = _FakeSvc([_draft(1, "x1"), _draft(2, "x2")])
    w = _mk_worker(svc, cb, parallel=False)
    await w._tick()
    assert len(cb.calls) == 2 and w.total_delivered == 2
    assert cb.max_live == 1, "默认关必须逐条串行（旧行为）"
    assert w.parallel_batches == 0


async def test_same_conversation_stays_ordered():
    cb = _Recorder()
    w = _mk_worker(_FakeSvc([]), cb, parallel=True)
    await w._deliver_parallel([_item(1, "same"), _item(2, "same")])
    assert [c["text"] for c in cb.calls] == ["回复1", "回复2"], "同会话必须保序"
    assert cb.max_live == 1, "同会话两条不得并发在途"


async def test_semaphore_caps_concurrency():
    cb = _Recorder(hold_sec=0.08)
    w = _mk_worker(_FakeSvc([]), cb, parallel=True, max_concurrent=2)
    await w._deliver_parallel([_item(i, f"conv{i}") for i in range(5)])
    assert len(cb.calls) == 5
    assert cb.max_live <= 2, f"信号量未封顶：max_live={cb.max_live}"


async def test_group_failure_isolated():
    cb = _Recorder(fail_convs={"bad"})
    w = _mk_worker(_FakeSvc([]), cb, parallel=True)
    await w._deliver_parallel([_item(1, "bad"), _item(2, "good")])
    assert w.total_delivered == 1, "好组必须照常送达"
    assert w.total_deliver_errors == 1, "坏组按既有失败处置计数"


# ── 配置与暴露面 ────────────────────────────────────────────────────────────

def test_max_concurrent_clamped_and_defaulted():
    w = _mk_worker(_FakeSvc([]), None, parallel=True, max_concurrent=99)
    assert w._parallel_max == 8
    w2 = AutosendWorker(
        draft_service=_FakeSvc([]),
        config={"parallel_deliver": {"enabled": True,
                                     "max_concurrent": "abc"}})
    assert w2._parallel_max == 3
    w3 = AutosendWorker(draft_service=_FakeSvc([]))
    assert w3._parallel_enabled is False, "新子系统默认必须关"


def test_snapshot_exposes_parallel_deliver():
    w = _mk_worker(_FakeSvc([]), None, parallel=True, max_concurrent=4)
    snap = w.status_snapshot()["parallel_deliver"]
    assert snap == {"enabled": True, "max_concurrent": 4, "batches": 0}


# ── 抽取零漂移哨兵：_deliver_one 的早退语义（原 continue）不再影响批内其它条 ──

async def test_deliver_one_early_return_does_not_block_batch():
    """dup 守卫等早退路径抽取后为 return——串行分发下后续条必须照常投递。"""
    cb = _Recorder(hold_sec=0.0)
    svc = _FakeSvc([_draft(1, "x1"), _draft(2, "x2")])
    w = _mk_worker(svc, cb, parallel=False)
    # 让第一条在守卫前被会话封禁直接取消：塞进封禁表（走 _process_batch 取消路径
    # 会直接不进 to_deliver，这里改为直接验证 _deliver_one 异常早退不断批次）
    fail_cb = _Recorder(fail_convs={"x1"})
    w2 = _mk_worker(_FakeSvc([_draft(1, "x1"), _draft(2, "x2")]), fail_cb,
                    parallel=False)
    await w2._tick()
    assert any(c["conv"] == "x2" for c in fail_cb.calls), (
        "首条失败后，串行批内后续条仍须投递（原 continue 语义）")
    assert w2.total_delivered == 1 and w2.total_deliver_errors == 1
