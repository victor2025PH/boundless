"""Telegram 聊天记录同步（对齐手机）的单元测试。

覆盖 protocol_bridge 的历史同步核心：
- history_message_obj：文本/媒体占位/服务消息跳过 + 稳定去重 id；
- tg_chat_dict：pyrogram Chat → 收件箱 chat dict（含 supergroup→group 归一）；
- collect_tg_dialog_history：单会话云端历史拉取（offset_id 锚点语义）；
- sync_telegram_history：账号级全量同步落库（经 ingest_thread 直写 store，
  不触发事件），与实时路径（ingest_incoming）产出同一去重主键——不落重复行。
"""

from __future__ import annotations

import types

from src.inbox.ingest import ingest_thread
from src.inbox.store import InboxStore
from src.integrations import protocol_bridge as pb


class _FakeChatType:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeChat:
    def __init__(self, cid, title=None, first_name=None, type_value="private",
                 username="", phone_number=""):
        self.id = cid
        self.title = title
        self.first_name = first_name
        self.username = username
        self.phone_number = phone_number
        self.type = _FakeChatType(type_value)


class _FakeDate:
    def __init__(self, ts: float) -> None:
        self._ts = ts

    def timestamp(self) -> float:
        return self._ts


class _FakeMsg:
    def __init__(self, chat, text="", mid=0, ts=0, outgoing=False, photo=None):
        self.chat = chat
        self.text = text
        self.caption = None
        self.id = mid
        self.date = _FakeDate(ts) if ts else None
        self.outgoing = outgoing
        if photo is not None:
            self.photo = photo


class _FakeClient:
    """get_dialogs / get_chat_history 皆为 async 生成器（鸭子类型对齐 pyrogram）。"""

    def __init__(self, dialogs=None, history=None):
        self._dialogs = dialogs or []
        self._history = history or {}     # chat_id -> [msgs 新→旧]
        self.history_calls = []

    async def get_dialogs(self, limit=100):
        for d in self._dialogs[:limit]:
            yield d

    async def get_chat_history(self, chat_id, limit=50, offset_id=0):
        self.history_calls.append((chat_id, limit, offset_id))
        msgs = self._history.get(chat_id, [])
        if offset_id:
            msgs = [m for m in msgs if int(m.id) < int(offset_id)]
        for m in msgs[:limit]:
            yield m


def _dialog(chat, top_message=None, unread=0):
    return types.SimpleNamespace(
        chat=chat, top_message=top_message, unread_messages_count=unread)


# ── history_message_obj ───────────────────────────────────────────────────────

def test_history_message_obj_text_and_dedup_id():
    chat = _FakeChat(101, first_name="Alice")
    m = _FakeMsg(chat, text="hello", mid=7, ts=100)
    obj = pb.history_message_obj(pb.tg_message_payload(m, "acc1"), m)
    assert obj["text"] == "hello"
    assert obj["direction"] == "in"
    assert obj["source"]["id"] == "7"     # extract_platform_msg_id 可抽到 → 稳定去重键


def test_history_message_obj_media_placeholder_and_service_skip():
    chat = _FakeChat(101, first_name="Alice")
    media = _FakeMsg(chat, text="", mid=8, ts=110, photo=object())
    obj = pb.history_message_obj(pb.tg_message_payload(media, "acc1"), media)
    assert obj["text"] == "[图片]"        # 媒体不下载，占位文本保留上下文
    service = _FakeMsg(chat, text="", mid=9, ts=120)
    assert pb.history_message_obj(pb.tg_message_payload(service, "acc1"), service) == {}


def test_history_message_obj_outgoing_direction():
    chat = _FakeChat(101, first_name="Alice")
    m = _FakeMsg(chat, text="me too", mid=10, ts=130, outgoing=True)
    obj = pb.history_message_obj(pb.tg_message_payload(m, "acc1"), m)
    assert obj["direction"] == "out"


# ── tg_chat_dict ──────────────────────────────────────────────────────────────

def test_tg_chat_dict_private_identity():
    chat = pb.tg_chat_dict(
        _FakeChat(101, first_name="Alice", username="alice_w"), "acc1",
        last_msg="hi", last_ts=100, unread=2)
    assert chat["conversation_id"] == "telegram:acc1:101"
    assert chat["name"] == "Alice"
    assert chat["username"] == "alice_w"
    assert chat["chat_type"] == "private"
    assert chat["unread"] == 2


def test_tg_chat_dict_supergroup_maps_to_group():
    chat = pb.tg_chat_dict(
        _FakeChat(-1001234, title="工作群", type_value="supergroup"), "acc1")
    assert chat["chat_type"] == "group"
    assert chat["name"] == "工作群"


def test_tg_chat_dict_no_id_returns_none():
    assert pb.tg_chat_dict(types.SimpleNamespace(), "acc1") is None


# ── collect_tg_dialog_history ────────────────────────────────────────────────

async def test_collect_dialog_history_returns_chat_and_msgs():
    chat = _FakeChat(101, first_name="Alice")
    history = {101: [_FakeMsg(chat, text=f"m{i}", mid=i, ts=100 + i)
                     for i in (5, 4, 3, 2, 1)]}
    client = _FakeClient(history=history)
    res = await pb.collect_tg_dialog_history(client, "acc1", "101", limit=10)
    assert res is not None
    chat_dict, msgs = res
    assert chat_dict["conversation_id"] == "telegram:acc1:101"
    assert len(msgs) == 5
    # 最新一条作为会话预览
    assert chat_dict["last_msg"] == "m5"


async def test_collect_dialog_history_offset_anchor_only_older():
    chat = _FakeChat(101, first_name="Alice")
    history = {101: [_FakeMsg(chat, text=f"m{i}", mid=i, ts=100 + i)
                     for i in (5, 4, 3, 2, 1)]}
    client = _FakeClient(history=history)
    res = await pb.collect_tg_dialog_history(
        client, "acc1", "101", limit=10, offset_id=3)
    assert client.history_calls == [(101, 10, 3)]
    assert res is not None
    _, msgs = res
    assert sorted(m["source"]["id"] for m in msgs) == ["1", "2"]  # 仅比锚点更早的


async def test_collect_dialog_history_empty_returns_none():
    client = _FakeClient(history={})
    assert await pb.collect_tg_dialog_history(client, "acc1", "101") is None
    assert await pb.collect_tg_dialog_history(None, "acc1", "101") is None
    assert await pb.collect_tg_dialog_history(client, "acc1", "") is None


# ── sync_telegram_history ────────────────────────────────────────────────────

async def test_sync_telegram_history_full_flow(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    alice = _FakeChat(101, first_name="Alice", username="alice_w")
    group = _FakeChat(-1002000, title="项目群", type_value="supergroup")
    quiet = _FakeChat(303, first_name="Quiet")
    dialogs = [
        _dialog(alice, unread=2),
        _dialog(group, unread=1),
        # 近期无可入库消息的会话：top_message 兜底做预览，仍在列表可见
        _dialog(quiet, top_message=_FakeMsg(quiet, text="top only", mid=1, ts=50)),
    ]
    history = {
        101: [
            _FakeMsg(alice, text="hi", mid=5, ts=105),
            _FakeMsg(alice, text="", mid=4, ts=104, photo=object()),   # 媒体→占位
            _FakeMsg(alice, text="yo", mid=3, ts=103, outgoing=True),  # 出站保留方向
        ],
        -1002000: [_FakeMsg(group, text="早", mid=9, ts=200)],
        303: [],
    }
    client = _FakeClient(dialogs=dialogs, history=history)
    progress_calls = []
    stats = await pb.sync_telegram_history(
        client, "acc1", dialogs_limit=10, per_chat=30, pace_sec=0,
        ingest=lambda chat, msgs: ingest_thread(store, chat, msgs),
        progress=lambda d, t, m: progress_calls.append((d, t, m)))
    assert stats["dialogs"] == 3
    assert stats["messages"] == 4          # 3 (alice) + 1 (group)，quiet 无消息
    assert progress_calls[0] == (0, 3, 0)  # 会话清单就绪即报 total
    assert progress_calls[-1][0] == 3
    convs = {c["conversation_id"]: c
             for c in store.list_conversations(limit=50, platform="telegram")}
    a = convs["telegram:acc1:101"]
    assert a["display_name"] == "Alice"
    assert a["unread"] == 2                # 未读数与云端（=手机）一致
    assert a["last_text"] == "hi"
    g = convs["telegram:acc1:-1002000"]
    assert g["chat_type"] == "group"
    q = convs["telegram:acc1:303"]
    assert q["last_text"] == "top only"    # 兜底预览
    rows = store.list_messages("telegram:acc1:101")
    texts = {r["text"]: r["direction"] for r in rows}
    assert texts["hi"] == "in"
    assert texts["yo"] == "out"
    assert texts["[图片]"] == "in"


async def test_sync_telegram_history_dedupes_with_realtime_path(tmp_path):
    """同一条消息：实时 push（ingest_incoming）先落库、历史同步再来 → 不落重复行。"""
    store = InboxStore(tmp_path / "inbox.db")
    pb.ingest_incoming(
        store, platform="telegram", account_id="acc1", chat_key="101",
        name="Alice", text="hi", ts=105, msg_id="5", direction="in")
    alice = _FakeChat(101, first_name="Alice")
    client = _FakeClient(
        dialogs=[_dialog(alice)],
        history={101: [_FakeMsg(alice, text="hi", mid=5, ts=105)]})
    stats = await pb.sync_telegram_history(
        client, "acc1", dialogs_limit=10, per_chat=30, pace_sec=0,
        ingest=lambda chat, msgs: ingest_thread(store, chat, msgs))
    assert stats["dialogs"] == 1
    assert stats["messages"] == 0          # 主键一致（conv:5）→ INSERT OR IGNORE
    assert len(store.list_messages("telegram:acc1:101")) == 1


async def test_sync_telegram_history_guards():
    called = []
    assert (await pb.sync_telegram_history(
        None, "a", ingest=lambda c, m: called.append(1)))["dialogs"] == 0
    assert (await pb.sync_telegram_history(
        _FakeClient(), "a", dialogs_limit=0,
        ingest=lambda c, m: called.append(1)))["dialogs"] == 0
    assert (await pb.sync_telegram_history(
        _FakeClient(), "a", ingest=None))["dialogs"] == 0
    assert called == []


async def test_sync_telegram_history_single_dialog_failure_skips(tmp_path):
    """单会话拉取抛错只跳过该会话，其余照常（best-effort）。"""
    store = InboxStore(tmp_path / "inbox.db")
    ok_chat = _FakeChat(101, first_name="Alice")
    bad_chat = _FakeChat(202, first_name="Bob")

    class _Boom(_FakeClient):
        async def get_chat_history(self, chat_id, limit=50, offset_id=0):
            if chat_id == 202:
                raise RuntimeError("boom")
            async for m in super().get_chat_history(chat_id, limit, offset_id):
                yield m

    client = _Boom(
        dialogs=[_dialog(bad_chat), _dialog(ok_chat)],
        history={101: [_FakeMsg(ok_chat, text="hi", mid=5, ts=105)]})
    stats = await pb.sync_telegram_history(
        client, "acc1", dialogs_limit=10, per_chat=30, pace_sec=0,
        ingest=lambda chat, msgs: ingest_thread(store, chat, msgs))
    assert stats["dialogs"] == 1
    assert stats["messages"] == 1
    assert store.get_conversation("telegram:acc1:101") is not None


# ── 路由层进度状态（纯函数，无需起 app） ─────────────────────────────────────

def test_tg_history_sync_state_helpers():
    from src.web.routes import unified_inbox_account_routes as r
    aid = "state_test_acct"
    r._TG_HIST_SYNC.pop(aid, None)
    assert r.tg_history_sync_snapshot(aid) == {"state": "idle"}
    assert r._tg_hist_try_start(aid) is True
    assert r._tg_hist_try_start(aid) is False       # 已在跑 → 拒绝并发
    r._tg_hist_update(aid, dialogs_done=3, dialogs_total=10, messages=42)
    snap = r.tg_history_sync_snapshot(aid)
    assert snap["state"] == "running"
    assert (snap["dialogs_done"], snap["dialogs_total"], snap["messages"]) == (3, 10, 42)
    r._tg_hist_update(aid, state="done")
    assert r._tg_hist_try_start(aid) is True        # 完成后可再次启动
    r._TG_HIST_SYNC.pop(aid, None)
