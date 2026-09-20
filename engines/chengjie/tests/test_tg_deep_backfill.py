"""Telegram 单会话「深度回填」+ 历史自动补缺口（2026-08-02）门禁。

覆盖：
- ``deep_backfill_tg_history`` 流式核心：分批落库/进度回调/到头判定/offset 锚点
  透传/服务消息计 fetched 不计 inserted/ingest 异常不炸整轮；
- 深度回填进度登记表：按账号串行单飞/快照/完成态上限清理；
- ``tg_autosync_pick`` 纯函数：到期挑选、最久未同步优先、interval/max_pick 关闸；
- ``maybe_autostart_tg_history_sync`` 不该动的路径（关闸/缺 store 零副作用）。
"""

from __future__ import annotations

import types

import pytest

from src.integrations.protocol_bridge import deep_backfill_tg_history
from src.web.routes import unified_inbox_account_routes as R


# ── duck-typed pyrogram 假对象（协议层全 getattr，无需真依赖）────────────────

class _Date:
    def __init__(self, ts: float):
        self._ts = ts

    def timestamp(self) -> float:
        return self._ts


class _Chat:
    def __init__(self, cid: int = 777):
        self.id = cid
        self.title = ""
        self.first_name = "阿忆"
        self.last_name = ""
        self.username = "ayi"
        self.phone_number = ""
        self.type = None


class _Msg:
    def __init__(self, mid: int, ts: float, text: str = "hi",
                 chat: _Chat = None, outgoing: bool = False):
        self.id = mid
        self.chat = chat
        self.text = text
        self.caption = None
        self.sticker = None
        self.date = _Date(ts)
        self.outgoing = outgoing


class _FakeClient:
    """get_chat_history 契约复刻：返回 async 生成器（newest→oldest），
    尊重 limit 与 offset_id（只给比锚点 id 更早的）。"""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.calls = []

    def get_chat_history(self, peer, **kwargs):
        self.calls.append((peer, dict(kwargs)))
        limit = int(kwargs.get("limit") or 0)
        offset_id = int(kwargs.get("offset_id") or 0)
        pool = [m for m in self._msgs if not offset_id or m.id < offset_id]
        selected = pool[:limit] if limit else pool

        async def _gen():
            for m in selected:
                yield m

        return _gen()


def _mk_msgs(n: int, chat: _Chat, start_id: int = 1000) -> list:
    # newest → oldest（id/ts 均递减），模拟 get_chat_history 的产出顺序
    return [_Msg(start_id - i, 100000.0 - i, text=f"m{i}", chat=chat)
            for i in range(n)]


# ── 流式核心 ────────────────────────────────────────────────────────────────

async def test_deep_backfill_batches_progress_and_exhausted():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(25, chat))
    ingested, progress = [], []

    def _ingest(c, batch):
        ingested.append((c, list(batch)))
        return len(batch)

    stats = await deep_backfill_tg_history(
        client, "acct", "777", max_messages=100, batch_size=10, pace_sec=0,
        ingest=_ingest, progress=lambda f, i: progress.append((f, i)))

    assert stats == {"fetched": 25, "inserted": 25, "exhausted": True}
    assert [len(b) for _, b in ingested] == [10, 10, 5]
    c0 = ingested[0][0]
    assert c0["platform"] == "telegram"
    assert c0["account_id"] == "acct"
    assert str(c0["chat_key"]) == "777"
    assert progress[-1] == (25, 25)


async def test_deep_backfill_cap_reached_not_exhausted():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(25, chat))
    stats = await deep_backfill_tg_history(
        client, "acct", "777", max_messages=10, pace_sec=0,
        ingest=lambda c, b: len(b))
    assert stats["fetched"] == 10
    assert stats["inserted"] == 10
    assert stats["exhausted"] is False   # 拉满上限 = 云端大概率还有更早


async def test_deep_backfill_offset_anchor_forwarded():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(30, chat, start_id=1000))  # ids 1000..971
    stats = await deep_backfill_tg_history(
        client, "acct", "777", max_messages=100, offset_id=980, pace_sec=0,
        ingest=lambda c, b: len(b))
    assert client.calls[0][1].get("offset_id") == 980
    # 只拉比锚点更早的（id < 980 → 979..971 共 9 条）
    assert stats["fetched"] == 9
    assert stats["exhausted"] is True


async def test_deep_backfill_service_messages_counted_not_ingested():
    # chat=None → tg_message_payload 解析不出 → 不入库；fetched 仍计数（到头判定不受影响）
    client = _FakeClient([_Msg(10 - i, 1000.0 - i, chat=None) for i in range(5)])
    calls = []
    stats = await deep_backfill_tg_history(
        client, "acct", "777", max_messages=50, pace_sec=0,
        ingest=lambda c, b: calls.append(1) or len(b))
    assert stats == {"fetched": 5, "inserted": 0, "exhausted": True}
    assert calls == []   # 没有可入库消息 → ingest 从未被调


async def test_deep_backfill_ingest_error_not_fatal():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(25, chat))

    def _boom(c, b):
        raise RuntimeError("db locked")

    stats = await deep_backfill_tg_history(
        client, "acct", "777", max_messages=100, batch_size=10, pace_sec=0,
        ingest=_boom)
    assert stats["fetched"] == 25
    assert stats["inserted"] == 0      # 批次失败被吞（best-effort），不炸整轮
    assert stats["exhausted"] is True


async def test_deep_backfill_guards_return_empty_stats():
    stats = await deep_backfill_tg_history(
        None, "acct", "777", ingest=lambda c, b: 1)
    assert stats == {"fetched": 0, "inserted": 0, "exhausted": False}
    stats = await deep_backfill_tg_history(
        _FakeClient([]), "acct", "", ingest=lambda c, b: 1)
    assert stats["fetched"] == 0
    stats = await deep_backfill_tg_history(
        _FakeClient([]), "acct", "777", ingest=None)
    assert stats["fetched"] == 0


# ── 进度登记表（按账号串行单飞 + 上限清理）──────────────────────────────────

def test_deep_bf_single_flight_per_account():
    R._TG_DEEP_BF.clear()
    assert R._tg_deep_try_start("a1", "c1") is True
    # 同账号另一会话也拒（按账号串行，防 RPC 压力翻倍）
    assert R._tg_deep_try_start("a1", "c2") is False
    # 其他账号不受影响
    assert R._tg_deep_try_start("a2", "c9") is True
    # 完成后放行
    R._tg_deep_update("a1", "c1", state="done", finished_at=1.0)
    assert R._tg_deep_try_start("a1", "c2") is True
    assert R.tg_deep_backfill_snapshot("a1", "c2")["state"] == "running"
    assert R.tg_deep_backfill_snapshot("nope", "x") == {"state": "idle"}
    R._TG_DEEP_BF.clear()


def test_deep_bf_prunes_completed_entries_at_cap():
    R._TG_DEEP_BF.clear()
    for i in range(R._TG_DEEP_BF_MAX):
        R._TG_DEEP_BF[f"acct{i}:chat"] = {
            "state": "done", "finished_at": float(i)}
    assert R._tg_deep_try_start("fresh", "c") is True
    assert len(R._TG_DEEP_BF) <= R._TG_DEEP_BF_MAX
    # 被清的是最旧完成态（finished_at=0 的 acct0）
    assert "acct0:chat" not in R._TG_DEEP_BF
    assert R.tg_deep_backfill_snapshot("fresh", "c")["state"] == "running"
    R._TG_DEEP_BF.clear()


# ── 自动补缺口：到期挑选纯函数 + 不该动的路径 ────────────────────────────────

def test_tg_autosync_pick_due_order_and_gates():
    now = 1_000_000.0
    rows = [
        {"account_id": "fresh", "last_sync_ts": now - 3600},       # 1h 前 → 未到期
        {"account_id": "never", "last_sync_ts": 0},                # 从未 → 最优先
        {"account_id": "old", "last_sync_ts": now - 24 * 3600},    # 24h 前 → 到期
    ]
    assert R.tg_autosync_pick(rows, now, 12, max_pick=5) == ["never", "old"]
    assert R.tg_autosync_pick(rows, now, 12, max_pick=1) == ["never"]
    assert R.tg_autosync_pick(rows, now, 0, max_pick=5) == []      # interval 关闸
    assert R.tg_autosync_pick(rows, now, 12, max_pick=0) == []     # 配额关闸
    assert R.tg_autosync_pick([], now, 12) == []
    assert R.tg_autosync_pick(
        [{"account_id": "", "last_sync_ts": 0}], now, 12) == []    # 空 id 剔除
    assert R.tg_autosync_pick(
        [{"account_id": "x", "last_sync_ts": "bad"}], now, 12) == ["x"]  # 脏值按 0


def test_autosync_noop_when_disabled_or_store_missing():
    app = types.SimpleNamespace(state=types.SimpleNamespace(inbox_store=None))
    assert R.maybe_autostart_tg_history_sync(app, {}) == []
    assert R.maybe_autostart_tg_history_sync(
        app, {"inbox": {"tg_history_autosync": {"enabled": False}}}) == []
    # 开了闸但 store 未就绪 → 同样零副作用（不碰注册表/不触发同步）
    assert R.maybe_autostart_tg_history_sync(
        app, {"inbox": {"tg_history_autosync": {"enabled": True}}}) == []
