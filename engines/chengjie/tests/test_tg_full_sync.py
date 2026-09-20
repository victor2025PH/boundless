# -*- coding: utf-8 -*-
"""全量深同步引擎（P2，2026-08-05）门禁。

钉住四类不变量：

1. **配置闸**：默认关；bool/dict 宽容解析；预算夹紧（上限防「一轮 20 万条 RPC」）。
2. **排队纯函数**：已完成跳过、群组默认整类跳过、私聊在前 + 最近活跃降序（热优先）。
3. **战役预算与断点**：per_chat×total 双预算按 fetched 计、耗尽如实 ``budget_exhausted``、
   每会话落盘一次、单会话失败记 error 不卡战役、第二轮自动跳过已完成。
4. **启动编排**：disabled/client_unavailable/单飞语义 + 后台真 loop 冒烟到 done。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.inbox.store import InboxStore
from src.integrations import tg_full_sync as fs


# ── 1) 配置闸 ────────────────────────────────────────────────────────────────

def test_cfg_absent_or_false_disabled():
    assert fs.parse_full_sync_cfg({}) is None
    assert fs.parse_full_sync_cfg({"full_sync": False}) is None
    assert fs.parse_full_sync_cfg({"full_sync": "yes"}) is None
    assert fs.parse_full_sync_cfg(None) is None
    assert fs.parse_full_sync_cfg({"full_sync": {"enabled": False}}) is None


def test_cfg_true_defaults_and_overrides():
    cfg = fs.parse_full_sync_cfg({"full_sync": True})
    assert cfg == {"per_chat": 300, "total_messages": 20000, "pace_sec": 1.0,
                   "max_chats": 0, "include_groups": False}
    cfg = fs.parse_full_sync_cfg({"full_sync": {
        "per_chat": 500, "total_messages": 50000, "pace_sec": 0,
        "include_groups": True, "max_chats": 10}})
    assert cfg["per_chat"] == 500 and cfg["total_messages"] == 50000
    assert cfg["pace_sec"] == 0 and cfg["include_groups"] is True
    assert cfg["max_chats"] == 10


def test_cfg_clamps_and_bad_values():
    cfg = fs.parse_full_sync_cfg({"full_sync": {
        "per_chat": 999999, "total_messages": 1, "pace_sec": -5,
        "max_chats": "abc"}})
    assert cfg["per_chat"] == 2000          # 上限夹紧
    assert cfg["total_messages"] == 100     # 下限夹紧
    assert cfg["pace_sec"] == 0.0
    assert cfg["max_chats"] == 0            # 非法 → 默认


# ── 2) 排队纯函数 ────────────────────────────────────────────────────────────

def _meta(key, ctype="private", ts=0.0):
    return {"chat_key": str(key), "chat_type": ctype, "last_ts": ts}


def test_order_private_first_then_hot():
    metas = [_meta(1, "group", 900), _meta(2, "private", 100),
             _meta(3, "private", 500), _meta(4, "group", 950)]
    out = fs.order_dialog_candidates(metas, set(), include_groups=True)
    assert [m["chat_key"] for m in out] == ["3", "2", "4", "1"]


def test_order_skips_done_and_groups_by_default():
    metas = [_meta(1, "private", 100), _meta(2, "private", 200),
             _meta(3, "group", 999)]
    out = fs.order_dialog_candidates(metas, {"2"})
    assert [m["chat_key"] for m in out] == ["1"]    # 2=已完成、3=群组默认跳过


# ── 3) 断点状态 ──────────────────────────────────────────────────────────────

def test_state_roundtrip_and_corrupt(tmp_path):
    p = tmp_path / "st.json"
    assert fs.load_state(p) == {"chats": {}, "campaign": {}}
    st = {"chats": {"1": {"fetched": 5}}, "campaign": {"total_ingested": 5}}
    fs.save_state(p, st)
    assert fs.load_state(p)["chats"]["1"]["fetched"] == 5
    p.write_text("{not json", encoding="utf-8")
    assert fs.load_state(p) == {"chats": {}, "campaign": {}}


def test_default_state_path_sanitized():
    p = fs.default_state_path("../evil/../acct:7")
    assert p.parent == Path("config") / "tg_full_sync"
    # 消毒后是纯文件名：无路径分隔符（穿越面为零）、不以点开头（不成隐藏文件）
    assert "/" not in p.name and "\\" not in p.name
    assert not p.name.startswith(".")
    assert fs.default_state_path("").name == "default.json"


# ── 4) 战役运行器（假 client + 假 backfill）─────────────────────────────────

def _mk_dialog(cid, ctype="private", ts=100.0):
    return SimpleNamespace(
        chat=SimpleNamespace(id=cid, type=SimpleNamespace(value=ctype)),
        top_message=SimpleNamespace(
            date=SimpleNamespace(timestamp=lambda _t=ts: _t)),
    )


class FakeClient:
    def __init__(self, dialogs):
        self._dialogs = list(dialogs)

    async def get_dialogs(self):
        for d in self._dialogs:
            yield d

    async def get_chat_history(self, peer, **kw):
        return
        yield  # pragma: no cover - 空异步生成器（深回填冒烟用）


def _mk_backfill(per_chat_cloud):
    """假深回填：每会话云端可拉 per_chat_cloud[chat_key] 条；记录 (chat_key, budget)。"""
    calls: list = []

    async def backfill(client, account_id, chat_key, *, max_messages, ingest, **kw):
        calls.append((str(chat_key), int(max_messages)))
        avail = int(per_chat_cloud.get(str(chat_key), 0))
        fetched = min(avail, int(max_messages))
        return {"fetched": fetched, "inserted": fetched,
                "exhausted": fetched < int(max_messages)}

    return backfill, calls


def _cfg(**over):
    base = {"per_chat": 100, "total_messages": 250, "pace_sec": 0,
            "max_chats": 0, "include_groups": False}
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_run_budgets_and_state(tmp_path):
    """总预算按 fetched 计：100+100+50 → 第三会话只拿到剩余 50 的预算后收工。"""
    client = FakeClient([_mk_dialog(1, ts=300), _mk_dialog(2, ts=200),
                         _mk_dialog(3, ts=100), _mk_dialog(4, ts=50)])
    backfill, calls = _mk_backfill({"1": 999, "2": 999, "3": 999, "4": 999})
    state = {"chats": {}, "campaign": {}}
    saves: list = []
    stats = await fs.run_full_sync(
        client, "acc", ingest=lambda c, m: 0, cfg=_cfg(),
        state=state, save_state_fn=lambda st: saves.append(1),
        backfill=backfill)
    assert calls == [("1", 100), ("2", 100), ("3", 50)]   # 热优先 + 末会话吃剩余预算
    assert stats["fetched"] == 250 and stats["budget_exhausted"] is True
    assert stats["chats_done"] == 3 and stats["dialogs_total"] == 4
    assert len(saves) == 3                                # 每会话落盘一次
    assert set(state["chats"]) == {"1", "2", "3"}


@pytest.mark.asyncio
async def test_run_resume_skips_done(tmp_path):
    """第二轮：已完成会话跳过，从上轮断点接着吸。"""
    client = FakeClient([_mk_dialog(1, ts=300), _mk_dialog(2, ts=200),
                         _mk_dialog(3, ts=100)])
    backfill, calls = _mk_backfill({"1": 10, "2": 10, "3": 10})
    state = {"chats": {"1": {"fetched": 10}, "2": {"fetched": 10}},
             "campaign": {}}
    stats = await fs.run_full_sync(
        client, "acc", ingest=lambda c, m: 0, cfg=_cfg(),
        state=state, save_state_fn=lambda st: None, backfill=backfill)
    assert calls == [("3", 100)]
    assert stats["chats_done"] == 1 and stats["budget_exhausted"] is False


@pytest.mark.asyncio
async def test_run_chat_error_recorded_not_fatal():
    client = FakeClient([_mk_dialog(1, ts=300), _mk_dialog(2, ts=200)])

    async def backfill(client_, acct, chat_key, *, max_messages, ingest, **kw):
        if str(chat_key) == "1":
            raise RuntimeError("PEER_ID_INVALID")
        return {"fetched": 5, "inserted": 5, "exhausted": True}

    state = {"chats": {}, "campaign": {}}
    stats = await fs.run_full_sync(
        client, "acc", ingest=lambda c, m: 0, cfg=_cfg(),
        state=state, save_state_fn=lambda st: None, backfill=backfill)
    assert stats["chats_done"] == 2                      # 战役没被死会话卡住
    assert "error" in state["chats"]["1"]                # 失败入账可追溯
    assert state["chats"]["2"]["fetched"] == 5


@pytest.mark.asyncio
async def test_run_max_chats_cap():
    client = FakeClient([_mk_dialog(i, ts=100 - i) for i in range(1, 6)])
    backfill, calls = _mk_backfill({str(i): 1 for i in range(1, 6)})
    stats = await fs.run_full_sync(
        client, "acc", ingest=lambda c, m: 0, cfg=_cfg(max_chats=2),
        state={"chats": {}, "campaign": {}},
        save_state_fn=lambda st: None, backfill=backfill)
    assert len(calls) == 2 and stats["budget_exhausted"] is True


@pytest.mark.asyncio
async def test_run_groups_excluded_by_default():
    client = FakeClient([_mk_dialog(1, "group", 900), _mk_dialog(2, ts=10)])
    backfill, calls = _mk_backfill({"1": 5, "2": 5})
    await fs.run_full_sync(
        client, "acc", ingest=lambda c, m: 0, cfg=_cfg(),
        state={"chats": {}, "campaign": {}},
        save_state_fn=lambda st: None, backfill=backfill)
    assert calls == [("2", 100)]


# ── 5) 启动编排 ──────────────────────────────────────────────────────────────

@pytest.fixture()
def _clean_runs():
    with fs._RUNS_LOCK:
        fs._RUNS.clear()
    yield
    with fs._RUNS_LOCK:
        fs._RUNS.clear()


def test_start_disabled_and_unavailable(tmp_path, _clean_runs):
    store = InboxStore(tmp_path / "inbox.db")
    res = fs.start_full_sync(SimpleNamespace(loop=None), store, "a",
                             tg_cfg={}, state_path=tmp_path / "s.json")
    assert res == {"ok": False, "reason": "disabled"}
    res = fs.start_full_sync(SimpleNamespace(loop=None), store, "a",
                             tg_cfg={"full_sync": True},
                             state_path=tmp_path / "s.json")
    assert res == {"ok": False, "reason": "client_unavailable"}


def test_start_smoke_to_done_and_singleflight(tmp_path, _clean_runs):
    """后台真 loop 冒烟：两会话跑到 done、断点落盘、running 期间二次触发拿 already。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        client = FakeClient([_mk_dialog(11, ts=2), _mk_dialog(22, ts=1)])
        client.loop = loop
        store = InboxStore(tmp_path / "inbox.db")
        sp = tmp_path / "st.json"
        res = fs.start_full_sync(
            client, store, "acc",
            tg_cfg={"full_sync": {"enabled": True, "pace_sec": 0}},
            state_path=sp)
        assert res["ok"] is True and res.get("started") is True
        for _ in range(200):
            if fs.full_sync_snapshot("acc").get("state") == "done":
                break
            time.sleep(0.05)
        snap = fs.full_sync_snapshot("acc")
        assert snap["state"] == "done" and snap["chats_done"] == 2
        assert len(fs.load_state(sp)["chats"]) == 2      # 断点账本已落盘
        # restart=True 清账本重来（单飞已释放）
        res2 = fs.start_full_sync(
            client, store, "acc",
            tg_cfg={"full_sync": {"enabled": True, "pace_sec": 0}},
            state_path=sp, restart=True)
        assert res2.get("started") is True
        for _ in range(200):
            if fs.full_sync_snapshot("acc").get("state") == "done":
                break
            time.sleep(0.05)
        assert fs.full_sync_snapshot("acc")["chats_done"] == 2
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()


def test_try_start_singleflight(_clean_runs):
    assert fs._try_start("x") is True
    assert fs._try_start("x") is False                   # running 中拒绝
    fs._update("x", state="done")
    assert fs._try_start("x") is True                    # 结束后可再来
