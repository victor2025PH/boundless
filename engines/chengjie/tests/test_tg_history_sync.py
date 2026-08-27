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


def test_history_message_obj_carries_group_speaker():
    """群历史行必须带发言人（``ingest._msg_from_obj`` 读 source.sender_*）。

    只修实时链＝只有「修好之后新收到的」有发言人；群历史（深度回填/账号级同步）
    是库里群消息的绝大多数，漏了它坐席翻群历史看着像一个人自言自语。
    """
    grp = _FakeChat(-1001234, title="工作群", type_value="supergroup")
    m = _FakeMsg(grp, text="早上好", mid=11, ts=140)
    m.from_user = types.SimpleNamespace(id=555, first_name="张三")
    obj = pb.history_message_obj(pb.tg_message_payload(m, "acc1"), m)
    assert obj["source"]["sender_name"] == "张三"
    assert obj["source"]["sender_id"] == "555"
    assert obj["source"]["id"] == "11"        # 去重键不受影响


def test_history_message_obj_private_has_no_speaker():
    """私聊不写发言人（发言人就是会话本人＝纯冗余，且空串是「非群」语义位）。"""
    chat = _FakeChat(101, first_name="Alice")
    m = _FakeMsg(chat, text="hi", mid=12, ts=150)
    m.from_user = types.SimpleNamespace(id=101, first_name="Alice")
    obj = pb.history_message_obj(pb.tg_message_payload(m, "acc1"), m)
    assert "sender_name" not in obj["source"]
    assert "sender_id" not in obj["source"]


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


def test_tg_chat_dict_channel_is_not_group():
    """频道不得被负数 chat_id 启发式算成群。

    ``-100…`` 前缀同样是负数 → infer_chat_type 的 TG 启发式会判 group；只有把
    ``chat.type`` 真的归一出来才分得开。频道当群 → 群闸/群护栏对着一个只能读的
    广播会话空转。
    """
    chat = pb.tg_chat_dict(
        _FakeChat(-1009876, title="公告频道", type_value="channel"), "acc1")
    assert chat["chat_type"] == "channel"


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


# ── 「会话已死」错误分类（2026-08-13：198 坐席机主号被「终止所有会话」实锤） ──
# 吊销后同步只报笼统的「同步聊天记录失败」，坐席连点 4 次无从知道该去重新登录。
# tg_error_kind 把这类 401 归 session_revoked，三条同步链（账号级/深度回填/全量）
# 落 state=error 时统一携带，前端据此给「请重新登录」的可行动提示。


def test_tg_error_kind_session_dead_variants():
    revoked = ('Telegram says: [401 SESSION_REVOKED] - The authorization has '
               'been invalidated, because of the user terminating all sessions '
               '(caused by "messages.GetDialogs")')
    assert pb.tg_error_kind(revoked) == "session_revoked"
    assert pb.tg_error_kind("[401 AUTH_KEY_UNREGISTERED]") == "session_revoked"
    assert pb.tg_error_kind("[401 SESSION_EXPIRED]") == "session_revoked"
    assert pb.tg_error_kind("[406 AUTH_KEY_DUPLICATED]") == "session_revoked"
    assert pb.tg_error_kind(
        "The key is not registered in the system") == "session_revoked"
    # 生产调用形态是直接传异常对象（tg_error_kind(exc)）
    assert pb.tg_error_kind(RuntimeError("SESSION_REVOKED")) == "session_revoked"


def test_tg_error_kind_not_misfired():
    assert pb.tg_error_kind("") == ""
    assert pb.tg_error_kind(None) == ""
    assert pb.tg_error_kind("FloodWait of 30 seconds") == ""
    assert pb.tg_error_kind("Connection reset by peer") == ""
    # 对方账号注销（INPUT_USER_DEACTIVATED）≠ 本账号会话死——重新登录救不了，
    # 误提示比不提示更糟（分类器刻意不收 USER_DEACTIVATED 族）
    assert pb.tg_error_kind("[400 INPUT_USER_DEACTIVATED]") == ""


def test_tg_history_sync_error_kind_reset_on_restart():
    """再次启动必须清掉上一轮的 error/error_kind（防陈旧 kind 污染新一轮快照）。"""
    from src.web.routes import unified_inbox_account_routes as r
    aid = "state_test_error_kind"
    r._TG_HIST_SYNC.pop(aid, None)
    assert r._tg_hist_try_start(aid) is True
    r._tg_hist_update(aid, state="error", error="[401 SESSION_REVOKED] x",
                      error_kind="session_revoked", finished_at=1.0)
    assert r.tg_history_sync_snapshot(aid)["error_kind"] == "session_revoked"
    assert r._tg_hist_try_start(aid) is True
    snap = r.tg_history_sync_snapshot(aid)
    assert snap["error"] == "" and snap["error_kind"] == ""
    r._TG_HIST_SYNC.pop(aid, None)


def test_start_tg_history_sync_marks_session_revoked():
    """吊销全链路：get_dialogs 抛 401 SESSION_REVOKED → 后台 _run 落
    state=error + error_kind=session_revoked（前端轮询据此提示重新登录）。"""
    import asyncio
    import threading
    import time as _time
    from src.web.routes import unified_inbox_account_routes as r

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    class _RevokedPyro:
        """鸭子类型对齐 _extract_pyro 判据（.loop + .get_chat）。"""

        def __init__(self, lp):
            self.loop = lp

        async def get_chat(self, *a, **k):
            raise AssertionError("not used")

        async def get_dialogs(self, limit=100):
            raise RuntimeError(
                "Telegram says: [401 SESSION_REVOKED] - The authorization has "
                "been invalidated, because of the user terminating all sessions")
            yield  # noqa: E501  # 不可达——只为让本方法成为 async generator（对齐 pyrogram 形态）

    aid = "revoked_e2e_acct"
    r._TG_HIST_SYNC.pop(aid, None)
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        telegram_client=_RevokedPyro(loop)))
    try:
        res = r.start_tg_history_sync(
            app, object(), aid, dialogs_limit=5, per_chat=5)
        assert res.get("started") is True
        snap: dict = {}
        deadline = _time.time() + 5
        while _time.time() < deadline:
            snap = r.tg_history_sync_snapshot(aid)
            if snap.get("state") == "error":
                break
            _time.sleep(0.05)
        assert snap.get("state") == "error"
        assert snap.get("error_kind") == "session_revoked"
        assert "SESSION_REVOKED" in str(snap.get("error") or "")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)
        loop.close()
        r._TG_HIST_SYNC.pop(aid, None)
