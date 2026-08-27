"""B88 线程缺口探测+补拉门禁（实施68 P1-13，QTSE67 诊断包定向）。

事故形态：重启/停机窗口漏进镜像的消息永不回来 → 线程视图冻在「重启前最后
一条」，列表 ts 照动（live dialogs）、AI 照回（handler 链正常），坐席以为群死了。

覆盖：
- ``probe_fill_tg_gap``：缺口判定/遇镜像 id 即停/触顶 capped 语义/冷会话小页/守卫；
- 探测登记表：冷却窗+running 单飞+force 豁免+容量清理；
- ``InboxStore.max_numeric_platform_msg_id``：纯数字 id 口径；
- ``maybe_probe_tg_thread_gap`` 不该动的路径（store 缺席/严格 client 路由——
  非 default 账号绝不回落主 client 串号拉数）。
"""

from __future__ import annotations

import time
import types

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.integrations.protocol_bridge import probe_fill_tg_gap
from src.web.routes import unified_inbox_account_routes as R


# ── duck-typed pyrogram 假对象（与 test_tg_deep_backfill 同款契约）────────────

class _Date:
    def __init__(self, ts: float):
        self._ts = ts

    def timestamp(self) -> float:
        return self._ts


class _Chat:
    def __init__(self, cid: int = -1002000):
        self.id = cid
        self.title = "bug群"
        self.first_name = ""
        self.last_name = ""
        self.username = ""
        self.phone_number = ""
        self.type = None


class _Msg:
    def __init__(self, mid: int, ts: float, text: str = "hi",
                 chat: "_Chat" = None):
        self.id = mid
        self.chat = chat
        self.text = text
        self.caption = None
        self.sticker = None
        self.date = _Date(ts)
        self.outgoing = False


class _FakeClient:
    """get_chat_history：newest→oldest 生成器，尊重 limit。"""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.calls = []

    def get_chat_history(self, peer, **kwargs):
        self.calls.append((peer, dict(kwargs)))
        limit = int(kwargs.get("limit") or 0)
        selected = self._msgs[:limit] if limit else list(self._msgs)

        async def _gen():
            for m in selected:
                yield m

        return _gen()


def _mk_msgs(chat, top_id: int, n: int):
    # newest → oldest：ids top_id..top_id-n+1
    return [_Msg(top_id - i, 200000.0 - i, text=f"m{top_id - i}", chat=chat)
            for i in range(n)]


# ── probe_fill_tg_gap 核心 ───────────────────────────────────────────────────

async def test_gap_fill_stops_at_mirror_and_ingests_gap_span():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(chat, top_id=120, n=60))
    got = []

    def _ingest(c, batch):
        got.append((c, list(batch)))
        return len(batch)

    stats = await probe_fill_tg_gap(
        client, "acct", "-1002000", mirror_max_id=100, cap=300, ingest=_ingest)
    assert stats["top_id"] == 120
    assert stats["gap"] is True
    assert stats["capped"] is False
    assert stats["fetched"] == 20          # 只拉 (100, 120] 缺口段
    assert stats["inserted"] == 20
    ids = sorted(int(m["message_id"]) for _, b in got for m in b)
    assert ids == list(range(101, 121))    # 缺口段全集，一条不多一条不少
    c0 = got[0][0]
    assert c0["platform"] == "telegram" and c0["account_id"] == "acct"


async def test_gap_fill_no_gap_zero_fetch():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(chat, top_id=100, n=50))
    calls = []
    stats = await probe_fill_tg_gap(
        client, "acct", "-1002000", mirror_max_id=100, cap=300,
        ingest=lambda c, b: calls.append(1) or len(b))
    assert stats["top_id"] == 100
    assert stats["gap"] is False
    assert stats["fetched"] == 0           # 首条即接上镜像 → 零拉取
    assert calls == []


async def test_gap_fill_capped_when_gap_deeper_than_cap():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(chat, top_id=1000, n=1000))
    stats = await probe_fill_tg_gap(
        client, "acct", "-1002000", mirror_max_id=10, cap=50,
        ingest=lambda c, b: len(b))
    assert stats["gap"] is True
    assert stats["capped"] is True         # 拉满 cap 没接上 → 必须显式提示
    assert stats["fetched"] == 50


async def test_gap_fill_cold_conversation_small_page():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(chat, top_id=500, n=400))
    stats = await probe_fill_tg_gap(
        client, "acct", "-1002000", mirror_max_id=0, cap=300,
        ingest=lambda c, b: len(b))
    # 镜像无数字 id 的冷会话：只补最近一小页（min(cap,50)），不放大成深同步
    assert client.calls[0][1].get("limit") == 50
    assert stats["fetched"] == 50
    assert stats["gap"] is True
    assert stats["capped"] is False        # 冷会话没有「接上」语义，不谎报 capped


async def test_gap_fill_guards():
    stats = await probe_fill_tg_gap(
        None, "acct", "777", mirror_max_id=1, ingest=lambda c, b: 1)
    assert stats["fetched"] == 0 and stats["gap"] is False
    stats = await probe_fill_tg_gap(
        _FakeClient([]), "acct", "", mirror_max_id=1, ingest=lambda c, b: 1)
    assert stats["fetched"] == 0


async def test_gap_fill_ingest_error_swallowed():
    chat = _Chat()
    client = _FakeClient(_mk_msgs(chat, top_id=120, n=40))

    def _boom(c, b):
        raise RuntimeError("db locked")

    stats = await probe_fill_tg_gap(
        client, "acct", "-1002000", mirror_max_id=100, cap=300, ingest=_boom)
    assert stats["fetched"] == 20
    assert stats["inserted"] == 0          # 失败被吞（best-effort），不炸调用方


# ── 探测登记表：冷却 + 单飞 + force + 容量 ───────────────────────────────────

def test_gap_try_start_cooldown_and_force():
    R._TG_GAP_PROBE.clear()
    now = time.time()
    assert R._tg_gap_try_start("cid1", now) is True
    # running 单飞：force 也不穿
    assert R._tg_gap_try_start("cid1", now + 1, force=True) is False
    R._tg_gap_update("cid1", state="done", finished_at=now + 1)
    # 冷却窗内（checked_at 起算）→ 拒
    assert R._tg_gap_try_start("cid1", now + 10) is False
    # force 豁免冷却
    assert R._tg_gap_try_start("cid1", now + 10, force=True) is True
    R._tg_gap_update("cid1", state="done", finished_at=now + 11)
    # 冷却窗过 → 放行
    assert R._tg_gap_try_start(
        "cid1", now + 10 + R._TG_GAP_COOLDOWN_SEC + 1) is True
    R._TG_GAP_PROBE.clear()


def test_gap_probe_capacity_prunes_oldest_done():
    R._TG_GAP_PROBE.clear()
    for i in range(R._TG_GAP_MAX):
        R._TG_GAP_PROBE[f"cid{i}"] = {"state": "done", "checked_at": float(i)}
    assert R._tg_gap_try_start("fresh", time.time()) is True
    assert len(R._TG_GAP_PROBE) <= R._TG_GAP_MAX
    assert "cid0" not in R._TG_GAP_PROBE   # 最旧完成态被清
    assert R.tg_gap_probe_snapshot("fresh")["state"] == "running"
    assert R.tg_gap_probe_snapshot("nope") == {"state": "idle"}
    R._TG_GAP_PROBE.clear()


# ── store：纯数字平台 id 口径 ────────────────────────────────────────────────

def test_store_max_numeric_platform_msg_id(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:-1002000"
    conv = InboxConversation(
        conversation_id=cid, platform="telegram", account_id="acct",
        chat_key="-1002000", display_name="bug群", chat_type="group")
    msgs = [
        InboxMessage(conversation_id=cid, platform_msg_id="7",
                     text="a", ts=1.0),
        InboxMessage(conversation_id=cid, platform_msg_id="102",
                     text="b", ts=2.0),
        InboxMessage(conversation_id=cid, platform_msg_id="",
                     text="c", ts=3.0),               # 无 id → 不参与
        InboxMessage(conversation_id=cid, platform_msg_id="wamid.abc99",
                     text="d", ts=4.0),               # 非数字 → 不参与
    ]
    store.ingest_batch(conv, msgs)
    assert store.max_numeric_platform_msg_id(cid) == 102
    assert store.max_numeric_platform_msg_id("telegram:acct:none") == 0
    assert store.max_numeric_platform_msg_id("") == 0


# ── maybe_probe_tg_thread_gap 不该动的路径 ───────────────────────────────────

def test_maybe_probe_guards(tmp_path, monkeypatch):
    R._TG_GAP_PROBE.clear()
    store = InboxStore(tmp_path / "inbox.db")
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        telegram_client=None, inbox_store=store))
    # store 缺席 → unavailable
    res = R.maybe_probe_tg_thread_gap(app, None, "acct", "123")
    assert res == {"ok": False, "reason": "unavailable"}
    # 非 default 账号 + 无编排器 worker → 严格路由拿不到 client → skipped
    # （绝不回落主 client：按别的账号身份拉历史落库=串号污染）
    res = R.maybe_probe_tg_thread_gap(app, store, "acct", "123")
    assert res == {"ok": False, "reason": "client_unavailable"}
    assert R.tg_gap_probe_snapshot("telegram:acct:123")["state"] == "skipped"
    # skipped 也吃冷却：紧接着的下一拍不再重试（/thread 秒级轮询不放大）
    res = R.maybe_probe_tg_thread_gap(app, store, "acct", "123")
    assert res.get("skipped") == "cooldown_or_running"
    R._TG_GAP_PROBE.clear()


def test_maybe_probe_respects_client_cooling(tmp_path, monkeypatch):
    R._TG_GAP_PROBE.clear()
    store = InboxStore(tmp_path / "inbox.db")
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        telegram_client=None, inbox_store=store))
    monkeypatch.setattr(R, "_tg_client_cooling", lambda a: True)
    res = R.maybe_probe_tg_thread_gap(app, store, "acct", "123")
    assert res == {"ok": False, "reason": "client_cooling"}
    assert R.tg_gap_probe_snapshot("telegram:acct:123") == {"state": "idle"}
    R._TG_GAP_PROBE.clear()
