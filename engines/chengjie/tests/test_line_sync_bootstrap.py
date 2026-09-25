"""LINE 登录后存量同步（好友通讯录 / 群会话占位）门禁。

背景：``LineProtocolWorker`` 原先只做实时收消息——登录成功后工作台里空无一物，得等对方
主动发消息才冒出一条会话（2026-07-25 真机实录：登录成功但 conversations/protocol_contacts
双双 0 行）。本文件锁住补上的存量同步语义。

夹具里的 API 返回形状全部照抄**真机探针**实测结果（`getAllContactIds` 直接回 mid 列表、
`getContactsV2` 回 ``{contacts: {mid: {contact: {...}}}}``），所以这些用例锁的是真实协议
契约，不是想象的形状。
"""
from __future__ import annotations

import asyncio

import pytest

from src.integrations.account_orchestrator import LineProtocolWorker

# ── 真机探针实测形状 ──────────────────────────────────────────────────────────
_MID_A = "Uuc571g2zAVNFdzYtvlH5CMklTF859vIxKMvwgEfsM7E"
_MID_B = "UukzTDocgZ8cTP1u4g10uiq2_d1Arr4K-jmITmikx7nk"
_PIC_B = "/0hjuZvapeENVpnDycFJXRKDVtKOzcQITMSHzwuPhUPOT1Lb3QPW2p6OEUGaGtNOiIPUmx9OxVdaGNI"

_CONTACTS_RES = {
    "contacts": {
        # 无头像的好友（picturePath 空串——真机确有这种）
        _MID_A: {"userStatus": 3, "contact": {"mid": _MID_A, "displayName": "阿花", "picturePath": ""}},
        _MID_B: {"userStatus": 3, "contact": {"mid": _MID_B, "displayName": "QF", "picturePath": _PIC_B}},
    }
}


class _FakeClient:
    def __init__(self, *, contact_ids=None, contacts=None, chat_mids=None, chats=None):
        self._contact_ids = [] if contact_ids is None else contact_ids
        self._contacts = contacts if contacts is not None else {"contacts": {}}
        self._chat_mids = chat_mids if chat_mids is not None else {"memberChatMids": [], "invitedChatMids": []}
        self._chats = chats if chats is not None else {"chats": []}
        self.asked_contact_mids = None
        self.asked_chat_mids = None
        self.closed = False

    def get_all_contact_ids(self):
        return self._contact_ids

    def get_contacts(self, mids):
        self.asked_contact_mids = list(mids)
        return self._contacts

    def get_all_chat_mids(self):
        return self._chat_mids

    def get_chats(self, mids):
        self.asked_chat_mids = list(mids)
        return self._chats

    def close(self):
        self.closed = True


class _FakeStore:
    def __init__(self):
        self.contact_rows = None
        self.chat_rows = None

    def upsert_protocol_contacts(self, platform, account_id, rows):
        self.contact_rows = (platform, account_id, list(rows))
        return len(rows)

    def upsert_protocol_chats(self, platform, account_id, rows):
        self.chat_rows = (platform, account_id, list(rows))
        return len(rows)


def _worker(config=None) -> LineProtocolWorker:
    return LineProtocolWorker({"account_id": "Uacct", "meta": {}}, config or {})


def _wire(monkeypatch, client, store):
    """把一次性 client 与 inbox store 换成夹具（两处都是函数内 import，patch 模块属性即可）。"""
    okline = pytest.importorskip("okline")  # CI 只装 requirements-ci.txt，不含 okline
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(okline.OkLine, "from_tokens_file",
                        classmethod(lambda cls, p, **k: client))
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)


# ── 一、纯函数：mid 抽取 / 身份解析 ──────────────────────────────────────────

def test_extract_mids_accepts_bare_list_and_wrapped_dict():
    """真机是裸 list；dict 包裹形态也认（okline 换封装不至于静默同步 0 条）。"""
    assert LineProtocolWorker._extract_mids([_MID_A, _MID_B]) == [_MID_A, _MID_B]
    assert LineProtocolWorker._extract_mids({"contactIds": [_MID_A]}) == [_MID_A]
    for junk in (None, {}, [], "x", {"other": [1]}, [1, 2, None]):
        assert LineProtocolWorker._extract_mids(junk) == []


def test_contact_identity_prefers_override_and_builds_avatar_url():
    name, avatar = LineProtocolWorker._contact_identity(_CONTACTS_RES["contacts"][_MID_B])
    assert name == "QF"
    assert avatar.startswith("http") and avatar.endswith(_PIC_B.lstrip("/"))
    # 备注名优先于公开名（贴合账号主在客户端看到的称呼）
    over = {"contact": {"displayName": "公开名", "displayNameOverridden": "备注名", "picturePath": ""}}
    assert LineProtocolWorker._contact_identity(over) == ("备注名", "")
    # 无头像 → 空串而不是垃圾 URL
    assert LineProtocolWorker._contact_identity(_CONTACTS_RES["contacts"][_MID_A]) == ("阿花", "")
    for junk in (None, {}, "x", {"contact": None}):
        assert LineProtocolWorker._contact_identity(junk) == ("", "")


# ── 二、好友名单 → 通讯录 rows ───────────────────────────────────────────────

def test_fetch_contact_rows_shapes_and_warms_peer_cache():
    """rows 用 upsert_protocol_contacts 认的 ``jid``/``name``；顺带预热 peer 缓存省往返。"""
    w = _worker()
    client = _FakeClient(contact_ids=[_MID_A, _MID_B], contacts=_CONTACTS_RES)
    rows = w._fetch_contact_rows(client, 1000)
    assert {r["jid"] for r in rows} == {_MID_A, _MID_B}
    assert {r["name"] for r in rows} == {"阿花", "QF"}
    # 预热后首条消息无需再打 getContactsV2（名字+头像当场可用）
    assert w._peer_ident_cache[_MID_B][0] == "QF"
    assert w._peer_ident_cache[_MID_B][1].startswith("http")


def test_fetch_contact_rows_respects_limit_and_empty():
    w = _worker()
    client = _FakeClient(contact_ids=[_MID_A, _MID_B], contacts=_CONTACTS_RES)
    w._fetch_contact_rows(client, 1)
    assert client.asked_contact_mids == [_MID_A], "limit 必须在请求前裁剪，别把上千 mid 全打出去"
    # 没有好友 → 不打 getContactsV2
    empty = _FakeClient(contact_ids=[])
    assert _worker()._fetch_contact_rows(empty, 1000) == []
    assert empty.asked_contact_mids is None


# ── 三、群名单 → 会话占位 rows ───────────────────────────────────────────────

def test_fetch_group_rows_marks_group():
    w = _worker()
    client = _FakeClient(
        chat_mids={"memberChatMids": ["Cgroup1"], "invitedChatMids": ["Cignored"]},
        chats={"chats": [{"chatMid": "Cgroup1", "chatName": "家族群"}]})
    rows = w._fetch_group_rows(client, 200)
    assert rows == [{"jid": "Cgroup1", "name": "家族群", "is_group": True}]
    assert client.asked_chat_mids == ["Cgroup1"], "只同步已加入的群，invited 不建占位"


def test_fetch_group_rows_empty_account_makes_no_call():
    """真机该号 memberChatMids 为空 → 不该继续打 getChats。"""
    client = _FakeClient()
    assert _worker()._fetch_group_rows(client, 200) == []
    assert client.asked_chat_mids is None


# ── 四、端到端：落库 + 会话占位闸门 ─────────────────────────────────────────

def test_bootstrap_syncs_contacts_and_seeds_chats(monkeypatch):
    store, client = _FakeStore(), _FakeClient(contact_ids=[_MID_A, _MID_B], contacts=_CONTACTS_RES)
    _wire(monkeypatch, client, store)
    _worker()._sync_bootstrap_blocking("tokens.json", {})
    platform, account_id, rows = store.contact_rows
    assert (platform, account_id) == ("line", "Uacct")
    assert len(rows) == 2
    # 小号：好友同时建成会话占位，工作台立刻可主动发起
    assert {r["jid"] for r in store.chat_rows[2]} == {_MID_A, _MID_B}
    assert client.closed, "一次性 client 必须关掉，否则每次登录泄漏一条 HTTPS 长连"


def test_bootstrap_skips_chat_seeding_for_large_accounts(monkeypatch):
    """上千好友的号不建会话占位——通讯录留着，别把工作台列表灌成噪音。"""
    store = _FakeStore()
    client = _FakeClient(contact_ids=[_MID_A, _MID_B], contacts=_CONTACTS_RES)
    _wire(monkeypatch, client, store)
    _worker()._sync_bootstrap_blocking("tokens.json", {"seed_chats_max": 1})
    assert store.contact_rows is not None and len(store.contact_rows[2]) == 2
    assert store.chat_rows is None


def test_bootstrap_noop_without_store(monkeypatch):
    """store 没就绪（启动竞态）→ 静默跳过，绝不抛进 worker.start。"""
    client = _FakeClient(contact_ids=[_MID_A])
    _wire(monkeypatch, client, None)
    _worker()._sync_bootstrap_blocking("tokens.json", {})
    assert client.asked_contact_mids is None


def test_bootstrap_disabled_by_config():
    w = _worker({"platform_login": {"line": {"sync": {"enabled": False}}}})
    # 关掉时连线程池都不该进（client 建不出来也不会炸）
    asyncio.run(w._sync_bootstrap("tokens.json"))


def test_bootstrap_swallows_api_failure(monkeypatch):
    """名单 API 挂了不能影响收发主职能——同步是加分项，不是启动前置条件。"""
    class _Boom(_FakeClient):
        def get_all_contact_ids(self):
            raise RuntimeError("LINE 抽风")

    store, client = _FakeStore(), _Boom(contact_ids=[_MID_A])
    _wire(monkeypatch, client, store)
    asyncio.run(_worker()._sync_bootstrap("tokens.json"))   # 不抛即通过
    assert store.contact_rows is None


def test_bootstrap_uses_dedicated_client_not_the_receiver_one(monkeypatch):
    """⚠ 同步必须用一次性 client，不能复用 ``self.client``。

    ``self.client`` 正被 ``Bot.run`` 的长轮询占着（另一个线程），跨线程复用同一 requests
    Session 去打 getContactsV2 是数据竞争——别把这里"优化"成共用一个实例。
    """
    store, fresh = _FakeStore(), _FakeClient(contact_ids=[_MID_A], contacts=_CONTACTS_RES)
    _wire(monkeypatch, fresh, store)
    w = _worker()
    receiver_client = _FakeClient(contact_ids=["SHOULD_NOT_BE_USED"])
    w.client = receiver_client
    w._sync_bootstrap_blocking("tokens.json", {})
    assert fresh.asked_contact_mids == [_MID_A]
    assert receiver_client.asked_contact_mids is None
    assert not receiver_client.closed, "收发用的 client 不能被同步流程关掉（等于把账号踢下线）"


# ── 五、授权后立刻催编排器（消掉「图标一分钟才变蓝」）────────────────────────

def test_kick_orchestrator_triggers_sync(monkeypatch):
    import src.integrations.account_orchestrator as ao
    import src.integrations.line_protocol_login as lpl
    calls = []

    class _Orch:
        async def sync(self):
            calls.append(1)

    monkeypatch.setattr(ao, "get_orchestrator_if_running", lambda: _Orch())

    async def _go():
        lpl._kick_orchestrator()
        await asyncio.sleep(0.05)

    asyncio.run(_go())
    assert calls == [1], "登录成功必须催一次巡检，否则要等 15s 才起 worker"


def test_kick_orchestrator_never_blocks_the_poll(monkeypatch):
    """必须 create_task 而非 await：sync() 会顺带联网起别的 worker，await 会拖住登录轮询。"""
    import time as _time

    import src.integrations.account_orchestrator as ao
    import src.integrations.line_protocol_login as lpl

    class _Slow:
        async def sync(self):
            await asyncio.sleep(0.3)

    monkeypatch.setattr(ao, "get_orchestrator_if_running", lambda: _Slow())

    async def _go():
        t0 = _time.monotonic()
        lpl._kick_orchestrator()
        assert _time.monotonic() - t0 < 0.1, "催巡检不得阻塞调用方"
        await asyncio.sleep(0.35)

    asyncio.run(_go())


def test_kick_orchestrator_noop_when_not_running(monkeypatch):
    """编排器还没建（启动早期）→ 静默跳过，绝不让登录因此失败。"""
    import src.integrations.account_orchestrator as ao
    import src.integrations.line_protocol_login as lpl
    monkeypatch.setattr(ao, "get_orchestrator_if_running", lambda: None)

    async def _go():
        lpl._kick_orchestrator()

    asyncio.run(_go())


def test_start_schedules_sync_without_blocking(monkeypatch):
    """``start()`` 不得 await 同步：大号名单几秒起步，别拖慢账号变「在线」。"""
    import src.integrations.account_orchestrator as ao
    calls = []

    async def _slow(self, tokens_file):
        calls.append(tokens_file)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(ao.LineProtocolWorker, "_sync_bootstrap", _slow)
    monkeypatch.setattr(ao.LineProtocolWorker, "_start_receiver", lambda self: None)
    import src.integrations.line_protocol_login as lpl
    monkeypatch.setattr(lpl, "is_okline_available", lambda: True)
    monkeypatch.setattr(lpl, "tokens_path", lambda cfg, acct: __file__)  # 存在即可
    okline = pytest.importorskip("okline")  # CI 只装 requirements-ci.txt，不含 okline
    monkeypatch.setattr(okline.OkLine, "from_tokens_file",
                        classmethod(lambda cls, p, **k: _FakeClient()))

    async def _go():
        w = _worker()
        await w.start()
        assert w.state == "running", "同步是后台任务，不该挡住 running"
        await asyncio.sleep(0.1)          # 让后台任务跑完
        return w

    asyncio.run(_go())
    assert calls, "start() 必须把存量同步调度出去（漏调=登录后永远空列表）"
