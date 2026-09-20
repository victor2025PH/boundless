"""Telegram 目录同步（好友名单 + 全量会话占位）门禁。

用假 pyrogram client（duck-typed）+ 内存 InboxStore 覆盖：rows 形状/名字拼接、
群频道 is_group、datetime→float、seed_chats_max 护栏、feature flag 关闭、
store 未就绪优雅跳过、端到端读回，以及**红线**——同步全程零消息 sink 调用。
"""

from datetime import datetime, timezone

import pytest

from src.inbox.store import InboxStore
from src.integrations import protocol_bridge
from src.integrations.telegram_directory_sync import (
    directory_sync_cfg,
    fetch_contact_rows,
    fetch_dialog_rows,
    sync_directory_once,
    sync_enabled,
)

CFG_ON = {"enabled": True, "max_contacts": 100, "max_dialogs": 100,
          "seed_chats_max": 200, "pace_seconds": 0}
# 本仓 Telegram 的 account_id 即该账号自己的 TG user id（= Saved Messages 的 chat_id）
TG_SELF = "8244899900"


class FakeUser:
    """pyrogram types.User 的最小替身（只需 tg_peer_identity 用到的属性）。"""

    def __init__(self, uid, first_name="", last_name="", username=""):
        self.id = uid
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class FakeChatType:
    """pyrogram ChatType 枚举替身：判定只读 .name。"""

    def __init__(self, name):
        self.name = name


class FakeChat:
    def __init__(self, cid, type_name="PRIVATE", title="", first_name="",
                 last_name="", username=""):
        self.id = cid
        self.type = FakeChatType(type_name)
        self.title = title
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class FakeTopMessage:
    def __init__(self, date):
        self.date = date


class FakeDialog:
    def __init__(self, chat, date=None, unread=0):
        self.chat = chat
        self.top_message = FakeTopMessage(date) if date is not None else None
        self.unread_messages_count = unread


class FakeClient:
    def __init__(self, contacts=None, dialogs=None):
        self._contacts = list(contacts or [])
        self._dialogs = list(dialogs or [])
        self.dialog_limit = None

    async def get_contacts(self):
        return list(self._contacts)

    def get_dialogs(self, limit=0):
        dialogs = self._dialogs
        self.dialog_limit = limit

        async def _gen():
            for d in dialogs[:limit] if limit else dialogs:
                yield d

        return _gen()


@pytest.fixture()
def store_bound(tmp_path):
    """把内存 store 注册进 protocol_bridge，用完恢复（避免污染同进程其他用例）。"""
    store = InboxStore(tmp_path / "inbox.db")
    protocol_bridge.register_inbox_store_getter(lambda: store)
    try:
        yield store
    finally:
        protocol_bridge.register_inbox_store_getter(None)
        store.close()


# ── 拉取形状 ────────────────────────────────────────────────────────────────

async def test_contact_rows_name_assembly():
    """名字拼接：全名 / 无 last_name / 只有 username / 全空回落裸 id。"""
    client = FakeClient(contacts=[
        FakeUser(111, "张", "三", "zhangsan"),
        FakeUser(222, "Alice", "", ""),
        FakeUser(333, "", "", "bob_only"),
        FakeUser(444),
    ])
    rows = await fetch_contact_rows(client, 100)
    assert rows == [
        {"jid": "111", "name": "张 三", "notify": "zhangsan"},
        {"jid": "222", "name": "Alice", "notify": ""},
        {"jid": "333", "name": "@bob_only", "notify": "bob_only"},
        {"jid": "444", "name": "444", "notify": ""},
    ]


async def test_contact_rows_limit_and_empty_client():
    """pyrogram get_contacts 无 limit 形参 → 客户端截断；无 client/limit=0 → 空。"""
    client = FakeClient(contacts=[FakeUser(i) for i in range(1, 6)])
    assert len(await fetch_contact_rows(client, 2)) == 2
    assert await fetch_contact_rows(client, 0) == []
    assert await fetch_contact_rows(None, 10) == []


async def test_dialog_rows_shape_group_flag_and_timestamp():
    """会话 rows：群/频道 is_group=True、私聊 False，datetime → float epoch。"""
    when = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    client = FakeClient(dialogs=[
        FakeDialog(FakeChat(111, "PRIVATE", first_name="Alice"), date=when, unread=3),
        FakeDialog(FakeChat(-100, "GROUP", title="家族群"), date=when),
        FakeDialog(FakeChat(-200, "SUPERGROUP", title="大群")),
        FakeDialog(FakeChat(-300, "CHANNEL", title="公告频道")),
        FakeDialog(FakeChat(None)),          # 无 id → 跳过
    ])
    rows = await fetch_dialog_rows(client, 100, pace_sec=0)
    assert [r["jid"] for r in rows] == ["111", "-100", "-200", "-300"]
    assert [r["is_group"] for r in rows] == [False, True, True, True]
    assert rows[0] == {"jid": "111", "name": "Alice", "ts": when.timestamp(),
                       "unread": 3, "is_group": False}
    assert rows[1]["name"] == "家族群"
    assert rows[2]["ts"] == 0.0          # 无 top_message → 时间戳 0（会话仍可见）
    assert rows[2]["unread"] == 0


async def test_dialog_rows_skip_system_peers():
    """源头止血：Saved Messages（chat_id 等于账号自身 TG user id）与官方服务号
    777000 都是 private，不在落库前挡住，每轮同步都给这俩非人条目建一次会话
    占位，库里噪音只增不减。"""
    client = FakeClient(dialogs=[
        FakeDialog(FakeChat(int(TG_SELF), "PRIVATE", first_name="我自己")),
        FakeDialog(FakeChat(777000, "PRIVATE", first_name="Telegram")),
        FakeDialog(FakeChat(111, "PRIVATE", first_name="Alice")),
        FakeDialog(FakeChat(-100, "GROUP", title="家族群")),
    ])
    rows = await fetch_dialog_rows(client, 100, pace_sec=0, account_id=TG_SELF)
    assert [r["jid"] for r in rows] == ["111", "-100"]


async def test_dialog_rows_without_account_id_stay_backward_compatible():
    """account_id 缺省（既有调用方）→ 不过滤，行为逐字节不变。"""
    client = FakeClient(dialogs=[
        FakeDialog(FakeChat(int(TG_SELF), "PRIVATE", first_name="我自己")),
        FakeDialog(FakeChat(777000, "PRIVATE", first_name="Telegram")),
        FakeDialog(FakeChat(111, "PRIVATE", first_name="Alice")),
    ])
    rows = await fetch_dialog_rows(client, 100, pace_sec=0)
    assert [r["jid"] for r in rows] == [TG_SELF, "777000", "111"]
    # 别的账号在跑同步时，本号的 id 只是个普通 peer，不得被误滤
    rows = await fetch_dialog_rows(client, 100, pace_sec=0, account_id="999")
    assert [r["jid"] for r in rows] == [TG_SELF, "111"]


# ── 一轮同步 ────────────────────────────────────────────────────────────────

async def test_sync_writes_contacts_and_chats_end_to_end(store_bound):
    client = FakeClient(
        contacts=[FakeUser(111, "Alice"), FakeUser(222, "Bob")],
        dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="家族群"), unread=1)],
    )
    stats = await sync_directory_once(client, "acct1", CFG_ON)
    assert stats == {"contacts": 2, "chats": 3}   # 1 个群 + 2 个好友占位

    names = {r["chat_key"]: r["name"]
             for r in store_bound.list_protocol_contacts("telegram", "acct1")}
    assert names == {"111": "Alice", "222": "Bob"}

    convs = {c["conversation_id"]: c
             for c in store_bound.list_conversations(platform="telegram")}
    assert convs["telegram:acct1:-100"]["chat_type"] == "group"
    assert convs["telegram:acct1:111"]["chat_type"] == "private"
    assert convs["telegram:acct1:111"]["display_name"] == "Alice"


async def test_seed_chats_max_guard_skips_friend_placeholders(store_bound):
    """好友数 > seed_chats_max：只写通讯录，好友不建会话占位；群占位仍建。"""
    client = FakeClient(
        contacts=[FakeUser(i, f"U{i}") for i in range(1, 6)],
        dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="家族群"))],
    )
    stats = await sync_directory_once(client, "acct1", {**CFG_ON, "seed_chats_max": 3})
    assert stats == {"contacts": 5, "chats": 1}
    convs = [c["conversation_id"]
             for c in store_bound.list_conversations(platform="telegram")]
    assert convs == ["telegram:acct1:-100"]
    # 通讯录本身照写不误（新消息到了自然冒出会话）
    assert len(store_bound.list_protocol_contacts("telegram", "acct1")) == 5


async def test_friend_already_in_dialogs_is_not_duplicated(store_bound):
    """好友同时也在会话列表里 → 只按云端会话行落一次，不用空 ts 覆盖。"""
    when = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    client = FakeClient(
        contacts=[FakeUser(111, "Alice")],
        dialogs=[FakeDialog(FakeChat(111, "PRIVATE", first_name="Alice"),
                            date=when, unread=2)],
    )
    stats = await sync_directory_once(client, "acct1", CFG_ON)
    assert stats["chats"] == 1
    conv = [c for c in store_bound.list_conversations(platform="telegram")
            if c["conversation_id"] == "telegram:acct1:111"][0]
    assert conv["last_ts"] == when.timestamp()


async def test_sync_does_not_seed_system_peer_chats(store_bound):
    """端到端：``sync_directory_once`` 必须把 account_id 透传给 fetch_dialog_rows，
    否则源头过滤形同虚设（调用点漏传是最容易发生的回归）。"""
    client = FakeClient(
        contacts=[FakeUser(111, "Alice")],
        dialogs=[FakeDialog(FakeChat(int(TG_SELF), "PRIVATE", first_name="我自己")),
                 FakeDialog(FakeChat(777000, "PRIVATE", first_name="Telegram")),
                 FakeDialog(FakeChat(111, "PRIVATE", first_name="Alice"))],
    )
    stats = await sync_directory_once(client, TG_SELF, CFG_ON)
    assert stats == {"contacts": 1, "chats": 1}
    convs = [c["conversation_id"]
             for c in store_bound.list_conversations(platform="telegram")]
    assert convs == [f"telegram:{TG_SELF}:111"]


async def test_disabled_flag_writes_nothing(store_bound):
    client = FakeClient(contacts=[FakeUser(111, "Alice")],
                        dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="群"))])
    assert await sync_directory_once(client, "acct1", {**CFG_ON, "enabled": False}) == \
        {"contacts": 0, "chats": 0}
    assert store_bound.list_protocol_contacts("telegram", "acct1") == []
    assert store_bound.list_conversations(platform="telegram") == []


async def test_missing_store_is_graceful():
    """store 未就绪（getter 未注册）→ 返回零统计，不抛。"""
    protocol_bridge.register_inbox_store_getter(None)
    client = FakeClient(contacts=[FakeUser(111, "Alice")])
    assert await sync_directory_once(client, "acct1", CFG_ON) == {"contacts": 0, "chats": 0}


async def test_bad_inputs_are_graceful(store_bound):
    assert await sync_directory_once(None, "acct1", CFG_ON) == {"contacts": 0, "chats": 0}
    assert await sync_directory_once(FakeClient(), "", CFG_ON) == {"contacts": 0, "chats": 0}
    assert await sync_directory_once(FakeClient(), "acct1", None) == {"contacts": 0, "chats": 0}


async def test_rpc_failure_of_one_stage_does_not_kill_the_other(store_bound):
    """好友名单 RPC 挂掉不该让会话同步陪葬（否则要等满一个 6h 周期才补齐）。"""

    class BoomContacts(FakeClient):
        async def get_contacts(self):
            raise RuntimeError("FLOOD_WAIT")

    client = BoomContacts(dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="群"))])
    stats = await sync_directory_once(client, "acct1", CFG_ON)
    assert stats == {"contacts": 0, "chats": 1}


# ── 红线：目录同步绝不喂消息管道 ─────────────────────────────────────────────

async def test_sync_never_touches_message_sink(store_bound, monkeypatch):
    """硬红线回归：不得调用 emit_incoming / make_message（否则会触发自动回复等下游）。"""
    calls = []
    monkeypatch.setattr(protocol_bridge, "emit_incoming",
                        lambda *a, **kw: calls.append("emit"))
    monkeypatch.setattr(protocol_bridge, "make_message",
                        lambda *a, **kw: calls.append("make"))
    # sink 本体也装探针：即便绕过 emit_incoming 直接调 sink 也会被抓到
    protocol_bridge.register_inbox_sink(lambda msg: calls.append("sink"))
    try:
        client = FakeClient(
            contacts=[FakeUser(111, "Alice")],
            dialogs=[FakeDialog(FakeChat(-100, "GROUP", title="群"))],
        )
        await sync_directory_once(client, "acct1", CFG_ON)
    finally:
        protocol_bridge.register_inbox_sink(None)
    assert calls == []
    # 只写了通讯录/会话占位，一条消息都没落
    assert store_bound.count_messages("telegram:acct1:-100") == 0


# ── 配置读取 ────────────────────────────────────────────────────────────────

def test_directory_sync_cfg_and_default_off():
    cfg = directory_sync_cfg({"platform_login": {"telegram": {"sync": {"enabled": True}}}})
    assert cfg == {"enabled": True}
    assert sync_enabled(cfg) is True
    # 缺段/形态异常 → 空 dict，且默认关（AGENTS.md 新子系统约定）
    assert directory_sync_cfg({}) == {}
    assert directory_sync_cfg({"platform_login": {"telegram": {"sync": "nope"}}}) == {}
    assert directory_sync_cfg(object()) == {}
    assert sync_enabled({}) is False


def test_repo_config_ships_sync_block_default_off():
    """仓库基线配置须带该段且默认关（本机 overlay 才开）。"""
    from pathlib import Path

    import yaml
    root = Path(__file__).resolve().parent.parent
    with open(root / "config" / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sync = directory_sync_cfg(cfg)
    assert sync.get("enabled") is False
    assert int(sync["interval_seconds"]) > 0
    assert int(sync["seed_chats_max"]) > 0
