"""消息去重 + 会话串行锁单测（2026-07-15「三连发语音」事故修复）。

覆盖：
- MessageDedup claim/seen 语义、chat 维度复合键、TTL、容量剪枝
- PerChatLocks 同会话串行/跨会话并行、容量剪枝不清持有中的锁
- 轮询兜底 _poll_inbound_once 与实时路径共用 claim（事故回归：实时已处理的
  消息，轮询绝不再处理第二遍）
- _guarded_process 同会话串行化
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.client.message_dedup import MessageDedup, PerChatLocks


# ── MessageDedup ─────────────────────────────────────────────────────────────
def test_claim_first_wins_second_rejected():
    d = MessageDedup()
    assert d.claim(100, 638) is True     # 首见 → 处理
    assert d.claim(100, 638) is False    # 重复 → 拦下（事故核心语义）
    assert d.seen(100, 638) is True


def test_key_includes_chat_dimension():
    """supergroup 消息 id 是 per-channel 的：不同群同 mid 不得互相误判。"""
    d = MessageDedup()
    assert d.claim(-1001, 638) is True
    assert d.claim(-1002, 638) is True   # 另一个群的同号消息照常处理
    assert d.claim(-1001, 638) is False


def test_no_mid_passes_through():
    d = MessageDedup()
    assert d.claim(100, 0) is True
    assert d.claim(100, None) is True
    assert d.seen(100, 0) is False


def test_ttl_expiry_allows_reclaim():
    now = {"t": 1000.0}
    d = MessageDedup(ttl_sec=600.0, clock=lambda: now["t"])
    assert d.claim(1, 5) is True
    now["t"] += 599
    assert d.claim(1, 5) is False        # TTL 内仍拦
    assert d.seen(1, 5) is True
    now["t"] += 2                        # 超 TTL
    assert d.seen(1, 5) is False
    assert d.claim(1, 5) is True         # 过期后可重新 claim


def test_size_cap_prunes_oldest():
    d = MessageDedup(max_size=3, ttl_sec=0)
    for i in range(5):
        assert d.claim(1, i + 1) is True
    assert len(d) <= 3
    assert d.claim(1, 5) is False        # 最新的还在
    assert d.claim(1, 1) is True         # 最老的被剪掉 → 视为新


# ── PerChatLocks ─────────────────────────────────────────────────────────────
def test_same_chat_same_lock_instance():
    locks = PerChatLocks()
    assert locks.lock(42) is locks.lock(42)
    assert locks.lock(42) is not locks.lock(43)
    assert locks.lock("42") is locks.lock(42)   # str/int 归一


@pytest.mark.asyncio
async def test_same_chat_serialized_cross_chat_parallel():
    locks = PerChatLocks()
    order: list = []

    async def job(chat, tag, dur):
        async with locks.lock(chat):
            order.append(f"{tag}-in")
            await asyncio.sleep(dur)
            order.append(f"{tag}-out")

    # 同 chat 两个任务：必须完整串行（in/out 不交叉）
    await asyncio.gather(job(1, "a", 0.02), job(1, "b", 0.01))
    assert order in (["a-in", "a-out", "b-in", "b-out"],
                     ["b-in", "b-out", "a-in", "a-out"])
    # 跨 chat：允许交叉（b 在 a 结束前就能进）
    order.clear()
    await asyncio.gather(job(1, "a", 0.03), job(2, "b", 0.01))
    assert order.index("b-out") < order.index("a-out")


@pytest.mark.asyncio
async def test_prune_never_removes_held_lock():
    locks = PerChatLocks(max_size=2)
    lk_held = locks.lock("held")
    await lk_held.acquire()
    try:
        for i in range(5):
            locks.lock(f"c{i}")          # 触发多轮剪枝
        assert locks.lock("held") is lk_held   # 持有中的锁不被清
        assert len(locks) <= 3           # held + 新建的（剪枝尽力控制在 cap 附近）
    finally:
        lk_held.release()


# ── 轮询兜底与实时路径共用 claim（事故回归测试）──────────────────────────────
def _mk_tc():
    """构造最小可跑 _poll_inbound_once 的 TelegramClient（绕过重依赖 __init__）。"""
    from src.client.telegram_client import TelegramClient
    tc = TelegramClient.__new__(TelegramClient)
    tc.config = SimpleNamespace(get_telegram_config=lambda: {"process_private": True})
    tc._rate_limiter = SimpleNamespace(enabled=False)
    tc._msg_dedup = MessageDedup()
    tc._boot_timestamp = time.time() - 3600
    tc.user_info = SimpleNamespace(id=999)
    tc._process_message = AsyncMock()
    return tc


def _mk_dialog(chat_id: int, mid: int, text: str = "在吗"):
    msg = SimpleNamespace(
        id=mid, message_id=mid, text=text, caption=None,
        voice=None, audio=None, photo=None, document=None, video=None,
        video_note=None, animation=None, sticker=None,
        outgoing=False,
        from_user=SimpleNamespace(id=123, is_bot=False),
        date=SimpleNamespace(timestamp=lambda: time.time() - 5),
        chat=SimpleNamespace(id=chat_id),
    )
    chat = SimpleNamespace(id=chat_id, type=SimpleNamespace(name="PRIVATE"))
    return SimpleNamespace(chat=chat, top_message=msg)


def _wire_dialogs(tc, dialogs):
    async def get_dialogs(limit=30):
        for d in dialogs[:limit]:
            yield d
    tc.client = SimpleNamespace(get_dialogs=get_dialogs)


@pytest.mark.asyncio
async def test_poll_processes_new_message_once():
    tc = _mk_tc()
    _wire_dialogs(tc, [_mk_dialog(5433982810, 638)])
    await tc._poll_inbound_once(30, catchup=600)
    assert tc._process_message.await_count == 1
    # 第二轮（回复尚未落地、top_message 仍是进站消息）→ 去重拦下，绝不重复处理
    await tc._poll_inbound_once(30, catchup=600)
    assert tc._process_message.await_count == 1


@pytest.mark.asyncio
async def test_poll_skips_message_claimed_by_realtime_path():
    """事故场景回归：实时 handler 已 claim（处理中，回复未落地）→ 轮询必须跳过。

    修复前：实时私聊路径从不登记去重，语音链路耗时 26s > 轮询间隔 12s，
    轮询把同一条消息再跑一遍 → 双流水线并行、三连发语音、前后矛盾。
    """
    tc = _mk_tc()
    _wire_dialogs(tc, [_mk_dialog(5433982810, 638)])
    # 模拟实时路径先到：claim 成功、开始处理（TTS 还在合成中）
    assert tc._msg_dedup.claim(5433982810, 638) is True
    await tc._poll_inbound_once(30, catchup=600)
    assert tc._process_message.await_count == 0    # 轮询不再碰这条


# ── _guarded_process 同会话串行 ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_guarded_process_serializes_same_chat():
    from src.client.telegram_client import TelegramClient
    tc = TelegramClient.__new__(TelegramClient)
    tc._chat_locks = PerChatLocks()
    tc._process_semaphore = asyncio.Semaphore(10)
    tc._active_tasks = 0
    tc._max_concurrent = 10
    order: list = []

    async def fake_process(md):
        order.append(f"{md['tag']}-in")
        await asyncio.sleep(0.02 if md["tag"] == "a" else 0.001)
        order.append(f"{md['tag']}-out")

    tc._process_message_async = fake_process
    # 同 chat：a 先入队则 b 必须等 a 完成（串行，后一条能看到前一条的回复上下文）
    t1 = asyncio.create_task(
        tc._guarded_process({"chat_id": 7, "tag": "a"}))
    await asyncio.sleep(0.005)   # 确保 a 先拿到锁
    t2 = asyncio.create_task(
        tc._guarded_process({"chat_id": 7, "tag": "b"}))
    await asyncio.gather(t1, t2)
    assert order == ["a-in", "a-out", "b-in", "b-out"]
