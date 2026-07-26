"""好友名单「资产盘点」增强：联系人 × 会话状态联表 + 手动重同步端点。

市场侧的核心诉求是识别「花了获客成本加了好友、但从来没开口的线索」——原
``GET /api/platforms/{platform}/{account_id}/contacts`` 只回 名字/号码/更新时间，
看不出「聊没聊过、最后一次什么时候、有没有未读」。本文件钉死两件事的契约：

1. ``InboxStore.list_protocol_contacts_enriched`` / ``protocol_contacts_summary``
   的联表语义（有会话 / 无会话 / 有会话但从无消息 三态 + 档位筛选 + 模糊搜索）；
2. 路由 ``enriched=1`` 的新形状、``enriched`` 缺省时**逐字段与改动前一致**
   （现有前端仍在消费旧形状，向后兼容是硬约束），以及
   ``POST .../contacts/refresh`` 的软依赖降级 / 限频 / 离线三种 reason；
3. ``include_chats``「人的并集」口径（通讯录 ∪ 会话里的私聊 peer）——线上实测
   Telegram 的 ``get_contacts()`` 只含「我主动保存过的联系人」，三个生产 TG 账号
   目录同步全成功而通讯录=0、同账号却有 62 条真实往来会话，只读通讯录的面板对
   Telegram 恒空。这里钉住：默认关时行为逐字节不变、并集去重与命名优先、群/频道
   必须排除、``chat_only`` 档位、以及**参数绑定顺序**（并集版 SQL 里 ``?`` 的顺序
   与通讯录版不同，错位在 SQLite 里不报错、只静默返回错数据）；
4. **系统 peer 过滤**（``is_system_peer``）——Telegram 的 Saved Messages（chat_key
   等于账号自身 TG user id）与官方服务号 777000 都是 private 类型，会实打实混进
   联系人面板并污染「好友总数 / 未开口数」。这里钉住真值表、两种 ``include_chats``
   口径、汇总数字，以及叠加 query/only 后的绑定顺序；其他平台一律不受影响。
"""

from __future__ import annotations

import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.routes.unified_inbox_account_routes as uar
from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import (
    TELEGRAM_SERVICE_CHAT_KEYS, InboxStore, is_system_peer,
)

DAY = 86400.0
# 本仓 Telegram 的 account_id 就是该账号自己的 TG user id —— Saved Messages
# 的 chat_key 与它逐字符相等，这正是「收藏夹混进联系人面板」的成因。
TG_SELF = "8244899900"
# 以真实当下为基准：路由层用的是 time.time()（不暴露 now 注入口），造未来时间戳会让
# 「60 天前」仍落在真实 now 之后 → 沉默判定失真。store 层用例仍显式传 now=NOW 保持确定性。
NOW = time.time()


@pytest.fixture(autouse=True)
def _reset_refresh_cooldown():
    """限频状态是模块级 dict → 每例前后清空，防跨用例串味。"""
    uar._CONTACTS_REFRESH_LAST.clear()
    yield
    uar._CONTACTS_REFRESH_LAST.clear()


def _seeded_store(tmp_path) -> InboxStore:
    """三态齐全的样本库：聊过 / 有会话但无消息（占位）/ 完全没会话。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_protocol_contacts("whatsapp", "acct1", [
        {"jid": "63911", "name": "Alice", "notify": "ally"},      # 聊过（近期）
        {"jid": "63922", "name": "Bob", "notify": "bobby"},       # 聊过（很久前）
        {"jid": "63933", "name": "Carol", "notify": ""},          # 占位会话，无消息
        {"jid": "63944", "name": "", "notify": "dave-notify"},    # 完全没会话
    ])
    # Alice：3 天前聊过，2 条未读
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:63911", platform="whatsapp",
        account_id="acct1", chat_key="63911", display_name="Alice",
        last_text="hi", last_ts=NOW - 3 * DAY, unread=2))
    # Bob：60 天前聊过，无未读
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:63922", platform="whatsapp",
        account_id="acct1", chat_key="63922", display_name="Bob",
        last_text="bye", last_ts=NOW - 60 * DAY))
    # Carol：只有会话占位（upsert_protocol_chats 建的），从无消息 → last_ts=0
    store.upsert_protocol_chats("whatsapp", "acct1", [{"jid": "63933", "ts": 0}])
    return store


def _client(tmp_path, monkeypatch, store=None):
    app = FastAPI()
    uar.register_account_routes(app, api_auth=lambda request: None,
                                config_manager=None)
    store = store if store is not None else _seeded_store(tmp_path)
    app.state.inbox_store = store
    return TestClient(app), store


# ── store 层：联表三态 ───────────────────────────────────────────────────────

def test_enriched_join_three_states(tmp_path):
    store = _seeded_store(tmp_path)
    rows = {r["chat_key"]: r for r in
            store.list_protocol_contacts_enriched("whatsapp", "acct1", now=NOW)}
    assert set(rows) == {"63911", "63922", "63933", "63944"}

    alice = rows["63911"]
    assert alice["has_conversation"] is True
    assert alice["never_spoke"] is False
    assert alice["last_ts"] == pytest.approx(NOW - 3 * DAY)
    assert alice["unread"] == 2
    # 原有四字段一并保留（enriched 是纯加法）
    assert alice["name"] == "Alice" and alice["notify_name"] == "ally"
    assert alice["updated_at"] > 0

    bob = rows["63922"]
    assert bob["has_conversation"] is True and bob["never_spoke"] is False
    assert bob["unread"] == 0

    # 有会话占位但从无消息 → has_conversation=True 但仍算「未开口」
    carol = rows["63933"]
    assert carol["has_conversation"] is True
    assert carol["last_ts"] == 0 and carol["never_spoke"] is True
    assert carol["unread"] == 0

    # 压根没有会话行
    dave = rows["63944"]
    assert dave["has_conversation"] is False
    assert dave["never_spoke"] is True
    assert dave["last_ts"] == 0 and dave["unread"] == 0
    store.close()


def test_enriched_unread_follows_read_watermark(tmp_path):
    """未读走 effective_unread 同口径：坐席读过（水位覆盖末条）→ 归零，
    不与工作台会话列表打架。"""
    store = _seeded_store(tmp_path)
    store.mark_conversation_read("whatsapp:acct1:63911")
    rows = {r["chat_key"]: r for r in
            store.list_protocol_contacts_enriched("whatsapp", "acct1", now=NOW)}
    assert rows["63911"]["unread"] == 0
    assert rows["63911"]["never_spoke"] is False   # 读过不等于没聊过
    store.close()


def test_enriched_ordering_named_first_then_recent(tmp_path):
    store = _seeded_store(tmp_path)
    keys = [r["chat_key"] for r in
            store.list_protocol_contacts_enriched("whatsapp", "acct1", now=NOW)]
    # 有名字的排前（dave 无 name 只有 notify_name → 沉底）
    assert keys[-1] == "63944"
    # 有名字的内部按 last_ts 降序：Alice(3天前) > Bob(60天前) > Carol(0)
    assert keys[:3] == ["63911", "63922", "63933"]
    store.close()


def test_enriched_isolated_by_account_and_platform(tmp_path):
    """联表前缀是 platform:account_id:chat_key —— 别号/别平台的同号会话不得串味。"""
    store = _seeded_store(tmp_path)
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct2:63944", platform="whatsapp",
        account_id="acct2", chat_key="63944", last_ts=NOW))
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:acct1:63944", platform="telegram",
        account_id="acct1", chat_key="63944", last_ts=NOW))
    rows = {r["chat_key"]: r for r in
            store.list_protocol_contacts_enriched("whatsapp", "acct1", now=NOW)}
    assert rows["63944"]["has_conversation"] is False
    assert store.list_protocol_contacts_enriched("whatsapp", "acct9") == []
    store.close()


# ── store 层：汇总 ───────────────────────────────────────────────────────────

def test_summary_counts(tmp_path):
    store = _seeded_store(tmp_path)
    s = store.protocol_contacts_summary("whatsapp", "acct1", now=NOW)
    # 默认（纯通讯录）口径下人人在册 → in_book == total、chat_only == 0
    assert s == {"total": 4, "never_spoke": 2, "silent": 1,
                 "with_conversation": 3, "in_book": 4, "chat_only": 0}
    store.close()


def test_summary_follows_query_not_limit(tmp_path):
    """汇总跟随 q（数字与搜索结果同集合），且不被 limit 截断。"""
    store = _seeded_store(tmp_path)
    assert store.protocol_contacts_summary(
        "whatsapp", "acct1", query="alice", now=NOW)["total"] == 1
    # limit 只截列表，不截汇总
    assert len(store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", limit=1, now=NOW)) == 1
    assert store.protocol_contacts_summary(
        "whatsapp", "acct1", now=NOW)["total"] == 4
    store.close()


# ── store 层：档位筛选 ───────────────────────────────────────────────────────

def test_only_never_spoke(tmp_path):
    store = _seeded_store(tmp_path)
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="never_spoke", now=NOW)}
    assert keys == {"63933", "63944"}     # 占位无消息 + 完全没会话
    store.close()


def test_only_silent_excludes_never_spoke(tmp_path):
    """silent = 聊过但很久没动静；「从没聊过」不算 silent（那是另一档）。"""
    store = _seeded_store(tmp_path)
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="silent", now=NOW)}
    assert keys == {"63922"}
    store.close()


@pytest.mark.parametrize("age_days, expect_silent", [
    (30.0, True),     # 恰好 30 天（last_ts == cutoff）→ 计入（<= 边界闭）
    (30.001, True),   # 更久 → 计入
    (29.999, False),  # 差一点 → 不计入
])
def test_only_silent_boundary(tmp_path, age_days, expect_silent):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_protocol_contacts("whatsapp", "acct1", [{"jid": "1", "name": "X"}])
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:1", platform="whatsapp",
        account_id="acct1", chat_key="1", last_ts=NOW - age_days * DAY))
    hit = store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="silent", now=NOW)
    assert bool(hit) is expect_silent
    assert (store.protocol_contacts_summary(
        "whatsapp", "acct1", now=NOW)["silent"] == 1) is expect_silent
    store.close()


def test_silent_days_is_tunable(tmp_path):
    """阈值是参数不是 SQL 魔数：调成 90 天后 60 天前的 Bob 就不再算沉默。"""
    store = _seeded_store(tmp_path)
    assert store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="silent", silent_days=90, now=NOW) == []
    assert store.protocol_contacts_summary(
        "whatsapp", "acct1", silent_days=90, now=NOW)["silent"] == 0
    store.close()


def test_unknown_only_falls_back_to_all(tmp_path):
    """未知档位不报错、退化为全部（前端传脏值不该 500）。"""
    store = _seeded_store(tmp_path)
    assert len(store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="bogus", now=NOW)) == 4
    store.close()


# ── store 层：模糊搜索 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("q, expect", [
    ("ali", {"63911"}),           # name 命中（大小写不敏感）
    ("ALICE", {"63911"}),
    ("bobby", {"63922"}),         # notify_name 命中
    ("dave", {"63944"}),          # 只有 notify_name 的联系人
    ("6393", {"63933"}),          # chat_key 命中
    ("639", {"63911", "63922", "63933", "63944"}),
    ("zzz", set()),
])
def test_query_matches_name_notify_and_chat_key(tmp_path, q, expect):
    store = _seeded_store(tmp_path)
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", query=q, now=NOW)}
    assert keys == expect
    store.close()


def test_query_wildcards_are_escaped(tmp_path):
    """名字里的 % / _ 不该被当 LIKE 通配符（否则搜 '%' 会命中全部）。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_protocol_contacts("whatsapp", "acct1", [
        {"jid": "1", "name": "100% cotton"},
        {"jid": "2", "name": "plain"},
    ])
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", query="%", now=NOW)}
    assert keys == {"1"}
    store.close()


def test_query_and_only_compose(tmp_path):
    store = _seeded_store(tmp_path)
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="never_spoke", query="639", now=NOW)}
    assert keys == {"63933", "63944"}
    store.close()


def test_enriched_limit_capped_at_5000(tmp_path):
    """新方法沿用 list_protocol_contacts 的 5000 上限保护（不被超大 limit 击穿）。"""
    store = _seeded_store(tmp_path)
    rows = store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", limit=10 ** 9, now=NOW)
    assert len(rows) == 4      # 语义上仍返回全部，limit 只是被夹到 5000
    store.close()


# ── 路由：enriched=1 新形状 ──────────────────────────────────────────────────

def test_route_enriched_shape_and_summary(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    r = client.get("/api/platforms/whatsapp/acct1/contacts?enriched=1")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["platform"] == "whatsapp" and data["account_id"] == "acct1"
    assert data["count"] == 4
    # 样本库里会话 peer 全都已在通讯录 → 并集不多不少还是这 4 人，四个老数字不变
    assert data["summary"] == {"total": 4, "never_spoke": 2, "silent_30d": 1,
                               "with_conversation": 3, "in_book": 4, "chat_only": 0}
    assert data["include_chats"] is True     # 默认开（见路由 docstring 的理由）
    row = {c["chat_key"]: c for c in data["contacts"]}["63944"]
    assert set(row) == {"chat_key", "name", "notify_name", "updated_at",
                        "has_conversation", "last_ts", "unread", "never_spoke",
                        "in_book"}
    assert row["never_spoke"] is True and row["has_conversation"] is False
    store.close()


def test_route_enriched_only_and_q(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    r = client.get("/api/platforms/whatsapp/acct1/contacts"
                   "?enriched=1&only=never_spoke")
    keys = {c["chat_key"] for c in r.json()["contacts"]}
    assert keys == {"63933", "63944"}
    # 汇总不跟随 only（要给全景基数），只跟随 q
    assert r.json()["summary"]["total"] == 4

    r = client.get("/api/platforms/whatsapp/acct1/contacts?enriched=1&q=bobby")
    assert [c["chat_key"] for c in r.json()["contacts"]] == ["63922"]
    assert r.json()["summary"]["total"] == 1
    store.close()


def test_route_default_shape_is_byte_for_byte_backward_compatible(tmp_path,
                                                                  monkeypatch):
    """enriched 缺省（含 enriched=0）→ 响应与改动前逐字段一致：既有前端零改动。"""
    client, store = _client(tmp_path, monkeypatch)
    legacy = store.list_protocol_contacts("whatsapp", "acct1", limit=1000)
    for url in ("/api/platforms/whatsapp/acct1/contacts",
                "/api/platforms/whatsapp/acct1/contacts?enriched=0"):
        data = client.get(url).json()
        assert set(data) == {"ok", "platform", "account_id", "count", "contacts"}
        assert data == {"ok": True, "platform": "whatsapp", "account_id": "acct1",
                        "count": len(legacy), "contacts": legacy}
        for c in data["contacts"]:
            assert set(c) == {"chat_key", "name", "notify_name", "updated_at"}
    store.close()


def test_route_contacts_requires_store(tmp_path, monkeypatch):
    app = FastAPI()
    uar.register_account_routes(app, api_auth=lambda request: None,
                                config_manager=None)
    client = TestClient(app)
    assert client.get(
        "/api/platforms/whatsapp/acct1/contacts?enriched=1").status_code == 503


# ── 路由：手动重同步 ─────────────────────────────────────────────────────────

def _wa_ok(monkeypatch, *, enabled=True, result=None, raiser=None):
    """把 Baileys 薄封装打成桩（路由是函数内 import → 打模块属性即可）。"""
    import src.integrations.whatsapp_baileys_login as wa

    async def _post(url, payload, timeout=20.0):
        if raiser is not None:
            raise raiser
        return result if result is not None else {"ok": True}

    monkeypatch.setattr(wa, "protocol_enabled", lambda cfg: enabled)
    monkeypatch.setattr(wa, "service_base_url", lambda cfg: "http://wa.test")
    monkeypatch.setattr(wa, "_post_json", _post)


def test_refresh_unsupported_platform(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    r = client.post("/api/platforms/line/acct1/contacts/refresh")
    assert r.json() == {"ok": False, "reason": "unsupported_platform"}
    store.close()


def test_refresh_telegram_soft_dependency_missing(tmp_path, monkeypatch):
    """目录同步模块由并行工作线交付——不在就优雅降级，绝不 500 / 绝不带崩 app。"""
    # sys.modules 里放 None → `from X import Y` 抛 ImportError（不依赖真实文件是否存在）
    monkeypatch.setitem(sys.modules, "src.integrations.telegram_directory_sync", None)
    client, store = _client(tmp_path, monkeypatch)
    r = client.post("/api/platforms/telegram/tg1/contacts/refresh")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "reason": "unsupported_platform"}
    store.close()


def test_refresh_telegram_account_offline(tmp_path, monkeypatch):
    """模块在、但该号没有活跃 client → account_offline（不占冷却窗）。"""
    mod = type(sys)("src.integrations.telegram_directory_sync")

    async def _sync(client, account_id, cfg):
        return {"contacts": 0}

    mod.sync_directory_once = _sync
    monkeypatch.setitem(sys.modules, "src.integrations.telegram_directory_sync", mod)
    monkeypatch.setattr(uar, "_get_tg_pyro_for_account", lambda app, acct: None)
    client, store = _client(tmp_path, monkeypatch)
    r = client.post("/api/platforms/telegram/tg1/contacts/refresh")
    assert r.json() == {"ok": False, "reason": "account_offline"}
    assert not uar._CONTACTS_REFRESH_LAST      # 失败不该吃掉 10 分钟窗口
    store.close()


def test_refresh_whatsapp_ok_then_cooldown(tmp_path, monkeypatch):
    """第一次真触发；第二次落进 10 分钟限频（防坐席狂点打爆平台 RPC）。"""
    _wa_ok(monkeypatch)
    client, store = _client(tmp_path, monkeypatch)
    r1 = client.post("/api/platforms/whatsapp/acct1/contacts/refresh").json()
    assert r1 == {"ok": True, "started": True, "platform": "whatsapp",
                  "account_id": "acct1"}
    r2 = client.post("/api/platforms/whatsapp/acct1/contacts/refresh").json()
    assert r2["ok"] is False and r2["reason"] == "cooldown"
    assert 0 < r2["retry_after_sec"] <= int(uar._CONTACTS_REFRESH_COOLDOWN_SEC) + 1
    # 限频是 platform+account 维度：换个号不受影响
    r3 = client.post("/api/platforms/whatsapp/acct2/contacts/refresh").json()
    assert r3["ok"] is True
    store.close()


def test_refresh_cooldown_expires(tmp_path, monkeypatch):
    _wa_ok(monkeypatch)
    client, store = _client(tmp_path, monkeypatch)
    assert client.post(
        "/api/platforms/whatsapp/acct1/contacts/refresh").json()["ok"] is True
    monkeypatch.setattr(uar, "_CONTACTS_REFRESH_COOLDOWN_SEC", 0.0)
    assert client.post(
        "/api/platforms/whatsapp/acct1/contacts/refresh").json()["ok"] is True
    store.close()


def test_refresh_whatsapp_protocol_disabled(tmp_path, monkeypatch):
    _wa_ok(monkeypatch, enabled=False)
    client, store = _client(tmp_path, monkeypatch)
    assert client.post("/api/platforms/whatsapp/acct1/contacts/refresh").json() == {
        "ok": False, "reason": "protocol_disabled"}
    store.close()


def test_refresh_whatsapp_node_404_is_offline(tmp_path, monkeypatch):
    """Node 对「账号未连接」回 404 —— 语义是号离线，别混成 service_error。"""
    class _Resp:
        status_code = 404

    class _Err(Exception):
        response = _Resp()

    _wa_ok(monkeypatch, raiser=_Err("not connected"))
    client, store = _client(tmp_path, monkeypatch)
    assert client.post("/api/platforms/whatsapp/acct1/contacts/refresh").json() == {
        "ok": False, "reason": "account_offline"}
    assert not uar._CONTACTS_REFRESH_LAST
    store.close()


def test_refresh_whatsapp_service_error(tmp_path, monkeypatch):
    _wa_ok(monkeypatch, raiser=RuntimeError("boom"))
    client, store = _client(tmp_path, monkeypatch)
    assert client.post("/api/platforms/whatsapp/acct1/contacts/refresh").json() == {
        "ok": False, "reason": "service_error"}
    # Node 明确回 ok:false 同样算失败，且不占冷却窗
    _wa_ok(monkeypatch, result={"ok": False})
    assert client.post("/api/platforms/whatsapp/acct1/contacts/refresh").json() == {
        "ok": False, "reason": "service_error"}
    assert not uar._CONTACTS_REFRESH_LAST
    store.close()


def test_refresh_telegram_schedules_sync(tmp_path, monkeypatch):
    """有活跃 client → 经 client 自身 loop 异步调度 sync_directory_once 并立即返回。"""
    import asyncio

    calls = []
    mod = type(sys)("src.integrations.telegram_directory_sync")

    async def _sync(client, account_id, cfg):
        calls.append((client, account_id))
        return {"contacts": 3}

    mod.sync_directory_once = _sync
    monkeypatch.setitem(sys.modules, "src.integrations.telegram_directory_sync", mod)

    loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    class _Pyro:
        pass

    pyro = _Pyro()
    pyro.loop = loop
    monkeypatch.setattr(uar, "_get_tg_pyro_for_account", lambda app, acct: pyro)
    client, store = _client(tmp_path, monkeypatch)
    try:
        r = client.post("/api/platforms/telegram/tg1/contacts/refresh").json()
        assert r == {"ok": True, "started": True, "platform": "telegram",
                     "account_id": "tg1"}
        for _ in range(100):
            if calls:
                break
            import time as _t
            _t.sleep(0.02)
        assert calls and calls[0][1] == "tg1"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
        loop.close()
        store.close()


def test_enriched_ignores_messages_table_for_spoke_judgement(tmp_path):
    """「聊过」判据是 conversations.last_ts，不为此 COUNT(messages)——
    真实入站消息经 ingest 推高 last_ts，未开口徽章随即摘掉。"""
    store = _seeded_store(tmp_path)
    conv = InboxConversation(
        conversation_id="whatsapp:acct1:63944", platform="whatsapp",
        account_id="acct1", chat_key="63944", last_text="hello", last_ts=NOW - DAY)
    store.ingest_batch(conv, [InboxMessage(
        conversation_id="whatsapp:acct1:63944", platform_msg_id="W1",
        text="hello", ts=NOW - DAY)])
    rows = {r["chat_key"]: r for r in
            store.list_protocol_contacts_enriched("whatsapp", "acct1", now=NOW)}
    assert rows["63944"]["never_spoke"] is False
    assert rows["63944"]["has_conversation"] is True
    assert store.protocol_contacts_summary(
        "whatsapp", "acct1", now=NOW)["never_spoke"] == 1
    store.close()


# ── store 层：include_chats「人的并集」口径 ──────────────────────────────────

def _tg_store(tmp_path) -> InboxStore:
    """Telegram 实景复刻：通讯录 **0 条**（``get_contacts()`` 只回「我主动保存过的
    人」，群里认识的 / 主动找上来的客户全不算），但目录同步把云端会话落成了
    3 条私聊 + 1 个群 + 1 个频道 + 1 条无 chat_key 的兜底行。"""
    store = InboxStore(tmp_path / "inbox.db")
    for ck, name, ts in (("111", "客户甲", NOW - DAY),
                         ("222", "客户乙", NOW - 2 * DAY),
                         ("333", "客户丙", NOW - 70 * DAY)):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"telegram:tg1:{ck}", platform="telegram",
            account_id="tg1", chat_key=ck, display_name=name,
            chat_type="private", last_ts=ts))
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:tg1:-1001", platform="telegram",
        account_id="tg1", chat_key="-1001", display_name="出海交流群",
        chat_type="group", last_ts=NOW))
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:tg1:-1002", platform="telegram",
        account_id="tg1", chat_key="-1002", display_name="公告频道",
        chat_type="channel", last_ts=NOW))
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:tg1:", platform="telegram",
        account_id="tg1", chat_key="", display_name="", last_ts=NOW))
    return store


def test_include_chats_default_off_is_todays_behaviour(tmp_path):
    """回归钉：默认口径下多出来的「只有会话」的人一个都不能冒出来，且每行
    ``in_book`` 恒 True（基表就是通讯录，口径自洽）——向后兼容是硬约束。"""
    store = _seeded_store(tmp_path)
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:63955", platform="whatsapp",
        account_id="acct1", chat_key="63955", display_name="Walk-in",
        last_ts=NOW - DAY))
    rows = store.list_protocol_contacts_enriched("whatsapp", "acct1", now=NOW)
    assert {r["chat_key"] for r in rows} == {"63911", "63922", "63933", "63944"}
    assert all(r["in_book"] is True for r in rows)
    assert store.protocol_contacts_summary("whatsapp", "acct1", now=NOW) == {
        "total": 4, "never_spoke": 2, "silent": 1, "with_conversation": 3,
        "in_book": 4, "chat_only": 0}
    store.close()


def test_include_chats_surfaces_telegram_chat_peers(tmp_path):
    """线上事故复现 + 修复验收：TG 通讯录 0 条，但 3 个真实往来私聊必须现身。"""
    store = _tg_store(tmp_path)
    assert store.list_protocol_contacts_enriched("telegram", "tg1", now=NOW) == []
    rows = {r["chat_key"]: r for r in store.list_protocol_contacts_enriched(
        "telegram", "tg1", include_chats=True, now=NOW)}
    assert set(rows) == {"111", "222", "333"}
    assert all(r["in_book"] is False for r in rows.values())
    assert rows["111"]["name"] == "客户甲"          # 会话 display_name 兜底命名
    assert rows["111"]["notify_name"] == ""
    assert rows["111"]["has_conversation"] is True
    assert rows["111"]["never_spoke"] is False
    assert rows["111"]["last_ts"] == pytest.approx(NOW - DAY)
    assert store.protocol_contacts_summary(
        "telegram", "tg1", include_chats=True, now=NOW) == {
            "total": 3, "never_spoke": 0, "silent": 1, "with_conversation": 3,
            "in_book": 0, "chat_only": 3}
    # 老口径下这个账号依旧空空如也——正是线上「联系人面板对 TG 恒空」的病灶
    assert store.protocol_contacts_summary("telegram", "tg1", now=NOW)["total"] == 0
    store.close()


def test_include_chats_excludes_group_channel_and_blank_chat_key(tmp_path):
    """群/频道灌进「联系人」会毁掉这个面板；chat_key 空行拼出的 conversation_id
    是无意义前缀，同样不能变成「一个人」。"""
    store = _tg_store(tmp_path)
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "telegram", "tg1", include_chats=True, now=NOW)}
    assert keys.isdisjoint({"-1001", "-1002", ""})
    store.close()


def test_include_chats_excludes_legacy_groups_typed_private(tmp_path):
    """chat_type 是后加的迁移列、默认 'private'：再没人说话的存量群永远不会被
    ingest 回填成 'group'。第二道闸用 infer_chat_type 同款 chat_key 形态启发式
    （TG 群/频道 id 为负；LINE 键含 :group:/:room:）兜住它们。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:tg1:-1009", platform="telegram",
        account_id="tg1", chat_key="-1009", display_name="存量老群",
        chat_type="private", last_ts=NOW))          # 迁移默认值，未回填
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:tg1:555", platform="telegram",
        account_id="tg1", chat_key="555", display_name="真人",
        chat_type="private", last_ts=NOW))
    assert [r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "telegram", "tg1", include_chats=True, now=NOW)] == ["555"]

    store.upsert_conversation(InboxConversation(
        conversation_id="line:ln1:line:group:G1", platform="line",
        account_id="ln1", chat_key="line:group:G1", display_name="LINE 老群",
        chat_type="private", last_ts=NOW))
    store.upsert_conversation(InboxConversation(
        conversation_id="line:ln1:line:user:U1", platform="line",
        account_id="ln1", chat_key="line:user:U1", display_name="LINE 真人",
        chat_type="private", last_ts=NOW))
    assert [r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "line", "ln1", include_chats=True, now=NOW)] == ["line:user:U1"]
    store.close()


def test_include_chats_dedupes_and_prefers_book_name(tmp_path):
    """两边都有的人只出现一次，且名字取通讯录备注名（平台昵称客户随时会改）。"""
    store = _seeded_store(tmp_path)
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:63911", platform="whatsapp",
        account_id="acct1", chat_key="63911", display_name="Alice-平台昵称",
        last_ts=NOW - 3 * DAY, unread=2))
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:63955", platform="whatsapp",
        account_id="acct1", chat_key="63955", display_name="Walk-in",
        last_ts=NOW - DAY))
    rows = store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", include_chats=True, now=NOW)
    keys = [r["chat_key"] for r in rows]
    assert sorted(keys) == ["63911", "63922", "63933", "63944", "63955"]
    assert keys.count("63911") == 1
    by = {r["chat_key"]: r for r in rows}
    assert by["63911"]["in_book"] is True
    assert by["63911"]["name"] == "Alice"            # 通讯录名压过平台昵称
    assert by["63911"]["notify_name"] == "ally"      # 会话侧空串不得把它冲掉
    assert by["63911"]["unread"] == 2
    assert by["63955"]["in_book"] is False and by["63955"]["name"] == "Walk-in"
    # 只有会话名、通讯录名为空时必须回落会话名（否则整行显示成空白）
    store.upsert_protocol_contacts("whatsapp", "acct1", [{"jid": "63955"}])
    by2 = {r["chat_key"]: r for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", include_chats=True, now=NOW)}
    assert by2["63955"]["in_book"] is True and by2["63955"]["name"] == "Walk-in"
    store.close()


def test_only_chat_only_filters_and_is_empty_without_union(tmp_path):
    store = _seeded_store(tmp_path)
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:63955", platform="whatsapp",
        account_id="acct1", chat_key="63955", display_name="Walk-in",
        last_ts=NOW - DAY))
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="chat_only", include_chats=True, now=NOW)}
    assert keys == {"63955"}
    # 纯通讯录口径下「没存进通讯录的人」按定义不存在 → 空集，且绝不报错
    assert store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="chat_only", now=NOW) == []
    store.close()


def test_query_only_and_include_chats_compose(tmp_path):
    """**参数绑定顺序钉**：并集版 SQL 文本里 ``?`` 的出现顺序与通讯录版不同
    （基表参数排在 JOIN 前缀之前），错位在 SQLite 里不报错、只静默返回错数据。
    三段参数（基表 / only 的 cutoff / query）同时在场才钉得住。"""
    store = _seeded_store(tmp_path)
    for ck, nm, ts in (("63955", "Walk-in Wendy", NOW - DAY),
                       ("63966", "Walk-in Willy", NOW - 90 * DAY),
                       ("70011", "Other Olivia", NOW - DAY)):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"whatsapp:acct1:{ck}", platform="whatsapp",
            account_id="acct1", chat_key=ck, display_name=nm, last_ts=ts))
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="chat_only", query="walk-in",
        include_chats=True, now=NOW)}
    assert keys == {"63955", "63966"}
    # only=silent 比 chat_only 多一个 cutoff 绑定 → 单独再钉一次
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", only="silent", query="walk-in",
        include_chats=True, now=NOW)}
    assert keys == {"63966"}
    # 并集**两侧**都必须带 platform+account_id 过滤：别号的同名会话不得混入
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct2:88888", platform="whatsapp",
        account_id="acct2", chat_key="88888", display_name="Walk-in Wrong",
        last_ts=NOW))
    keys = {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "acct1", query="walk-in", include_chats=True, now=NOW)}
    assert keys == {"63955", "63966"}
    # 汇总走同一条绑定顺序，数字必须与列表同集合
    s = store.protocol_contacts_summary(
        "whatsapp", "acct1", query="walk-in", include_chats=True, now=NOW)
    assert s["total"] == 2 and s["chat_only"] == 2 and s["silent"] == 1
    store.close()


def test_summary_in_book_plus_chat_only_equals_total(tmp_path):
    store = _seeded_store(tmp_path)
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:acct1:63955", platform="whatsapp",
        account_id="acct1", chat_key="63955", display_name="Walk-in",
        last_ts=NOW - DAY))
    s = store.protocol_contacts_summary(
        "whatsapp", "acct1", include_chats=True, now=NOW)
    assert s == {"total": 5, "never_spoke": 2, "silent": 1,
                 "with_conversation": 4, "in_book": 4, "chat_only": 1}
    assert s["in_book"] + s["chat_only"] == s["total"]
    store.close()


# ── store 层：系统 peer 过滤（Saved Messages / 官方服务号）───────────────────

@pytest.mark.parametrize("platform, account_id, chat_key, expect", [
    ("telegram", TG_SELF, TG_SELF, True),        # Saved Messages（自己发给自己）
    ("telegram", TG_SELF, "777000", True),       # Telegram 官方服务号
    ("telegram", "", "777000", True),            # 官方号是全网固定 id，与账号无关
    ("telegram", TG_SELF, "111", False),         # 普通客户
    ("telegram", TG_SELF, "-1001", False),       # 群（另有专门的闸，不归这里管）
    ("telegram", TG_SELF, "", False),            # 空 chat_key 不是系统条目
    ("telegram", "", "", False),                 # 两边都空不得判成「相等」
    ("TELEGRAM", TG_SELF, TG_SELF, True),        # 平台名大小写不敏感
    # 其他平台没有这套语义：同样的 chat_key 是真实客户
    ("whatsapp", TG_SELF, TG_SELF, False),
    ("whatsapp", TG_SELF, "777000", False),
    ("line", "777000", "777000", False),
    ("", TG_SELF, TG_SELF, False),
])
def test_is_system_peer_truth_table(platform, account_id, chat_key, expect):
    assert is_system_peer(platform, account_id, chat_key) is expect


def test_is_system_peer_keeps_bots():
    """刻意不过滤 bot：bot 只有真发过消息才会出现，「要不要联系它」是产品决策。
    漏一条噪音可恢复，误删真实往来对象在面板上就是「人凭空消失」。"""
    assert is_system_peer("telegram", TG_SELF, "6014928354") is False


@pytest.mark.parametrize("chat_key", sorted(TELEGRAM_SERVICE_CHAT_KEYS))
def test_every_service_key_is_a_system_peer(chat_key):
    """名单里的每条都必须真的被判成系统 peer（防「加进常量却漏接线」）。"""
    assert is_system_peer("telegram", TG_SELF, chat_key) is True


def test_pipeline_guard_shares_the_one_service_list():
    """管道安全侧（``TelegramClient._is_system_chat``）与展示侧读**同一份**名单。

    此前全仓有三份各自维护的硬编码且互相有缺口：管道侧与 store 漏 ``42777``，
    主动触达侧漏 Saved Messages——即「给账号自己的收藏夹发想你了」只差一条占位
    会话。三份都没人知道自己漏了什么，所以这里钉的不是某个具体 id，而是
    **「不许再有第二份名单」**这个不变量：任何人往 store 常量里加/删一条，
    管道侧必须自动跟随，否则本例红。
    """
    from src.client.telegram_client import TELEGRAM_SERVICE_CHAT_IDS

    assert TELEGRAM_SERVICE_CHAT_IDS == {
        int(k) for k in TELEGRAM_SERVICE_CHAT_KEYS
    }


def test_proactive_outreach_skips_system_peers():
    """主动触达的候选过滤走同一判定——收藏夹与官方号都不该收到「想你了」。

    只钉判定本身（``plan_proactive_sends`` 的候选流水要真库真编排器，成本远高于
    它能多抓到的东西）；接线由 ``proactive_topic`` 直接 import 该函数保证，
    改名/删除会在导入期就崩。
    """
    import src.companion.proactive_topic as pt

    assert "_SERVICE_CHAT_KEYS" not in pt.__dict__, "旧的独立硬编码名单应已移除"
    src = __import__("inspect").getsource(pt)
    assert "is_system_peer" in src, "主动触达候选过滤必须走 store 单一事实源"


def _tg_system_store(tmp_path) -> InboxStore:
    """线上噪音复刻：会话列表里混着 Saved Messages 与官方服务号，两者都是
    private → 不挡就实打实进联系人面板。通讯录侧也各放一条，保证 include_chats
    开与关**两条取数路径**都挡得住。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_protocol_contacts("telegram", TG_SELF, [
        {"jid": TG_SELF, "name": "Saved Messages"},
        {"jid": "777000", "name": "Telegram"},
        {"jid": "111", "name": "客户甲"},
    ])
    for ck, name, ts in ((TG_SELF, "Saved Messages", NOW - DAY),
                         ("777000", "Telegram", NOW - 2 * DAY),
                         ("111", "客户甲", NOW - DAY),
                         ("222", "客户乙", NOW - 90 * DAY)):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"telegram:{TG_SELF}:{ck}", platform="telegram",
            account_id=TG_SELF, chat_key=ck, display_name=name,
            chat_type="private", last_ts=ts))
    return store


def test_system_peers_excluded_from_contacts_book_only(tmp_path):
    """include_chats **关**（纯通讯录口径）也要挡：历史回填可能已把它们写进
    protocol_contacts。"""
    store = _tg_system_store(tmp_path)
    rows = store.list_protocol_contacts_enriched("telegram", TG_SELF, now=NOW)
    assert {r["chat_key"] for r in rows} == {"111"}
    assert store.protocol_contacts_summary("telegram", TG_SELF, now=NOW) == {
        "total": 1, "never_spoke": 0, "silent": 0, "with_conversation": 1,
        "in_book": 1, "chat_only": 0}
    store.close()


def test_system_peers_excluded_from_contacts_union(tmp_path):
    """include_chats **开**（并集口径，线上默认）——收藏夹/官方号只有会话没有
    通讯录记录时同样不得冒头。"""
    store = _tg_system_store(tmp_path)
    rows = store.list_protocol_contacts_enriched(
        "telegram", TG_SELF, include_chats=True, now=NOW)
    assert {r["chat_key"] for r in rows} == {"111", "222"}
    # total 是运营盘点数字（好友总数 / 未开口数），必须干净
    assert store.protocol_contacts_summary(
        "telegram", TG_SELF, include_chats=True, now=NOW) == {
            "total": 2, "never_spoke": 0, "silent": 1, "with_conversation": 2,
            "in_book": 1, "chat_only": 1}
    store.close()


def test_system_filter_composes_with_query_only_and_include_chats(tmp_path):
    """**参数绑定顺序钉**：SQL 文本里 ``?`` 的顺序＝基表/JOIN → 系统 peer →
    only 的 cutoff → query。错位在 SQLite 里不报错、只静默返回错数据，故必须用
    「四者同时在场」的实例钉住——这里刻意让两个系统条目也能命中 query，绑定
    错位时它们要么漏出来、要么把 query 参数吃掉使结果集塌空，两种都会红。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_protocol_contacts("telegram", TG_SELF, [
        {"jid": "111", "name": "VIP 甲"}])
    for ck, name, ts in ((TG_SELF, "VIP 收藏夹", NOW - 90 * DAY),
                         ("777000", "VIP Telegram", NOW - 90 * DAY),
                         ("111", "VIP 甲", NOW - DAY),
                         ("222", "VIP 乙", NOW - 90 * DAY),
                         ("333", "路人丙", NOW - 90 * DAY)):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"telegram:{TG_SELF}:{ck}", platform="telegram",
            account_id=TG_SELF, chat_key=ck, display_name=name,
            chat_type="private", last_ts=ts))

    # only=silent（多一个 cutoff 绑定）+ query + 并集 + 系统过滤 四者叠加
    assert {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "telegram", TG_SELF, only="silent", query="vip",
        include_chats=True, now=NOW)} == {"222"}
    # only=chat_only 走另一条分支（不带 cutoff）——绑定偏移量不同，单独再钉一次
    assert {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "telegram", TG_SELF, only="chat_only", query="vip",
        include_chats=True, now=NOW)} == {"222"}
    # 汇总与列表同一集合（汇总的 cutoff 排在 SELECT 里、位置又不同）
    s = store.protocol_contacts_summary(
        "telegram", TG_SELF, query="vip", include_chats=True, now=NOW)
    assert s == {"total": 2, "never_spoke": 0, "silent": 1,
                 "with_conversation": 2, "in_book": 1, "chat_only": 1}
    # 纯通讯录口径同样叠加（base_where 里 platform/account_id 之后才是系统子句）
    assert {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "telegram", TG_SELF, query="vip", now=NOW)} == {"111"}
    store.close()


def test_non_telegram_lookalike_chat_keys_are_not_filtered(tmp_path):
    """其他平台没有这套语义：WhatsApp 号码恰好等于 account_id 或 '777000' 也是
    真实客户，一个都不能滤掉。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_protocol_contacts("whatsapp", "777000", [
        {"jid": "777000", "name": "同号客户"},
        {"jid": "63911", "name": "Alice"},
    ])
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:777000:888", platform="whatsapp",
        account_id="777000", chat_key="888", display_name="Walk-in",
        chat_type="private", last_ts=NOW))
    assert {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "777000", now=NOW)} == {"777000", "63911"}
    assert {r["chat_key"] for r in store.list_protocol_contacts_enriched(
        "whatsapp", "777000", include_chats=True, now=NOW)} == {
            "777000", "63911", "888"}
    assert store.protocol_contacts_summary(
        "whatsapp", "777000", include_chats=True, now=NOW)["total"] == 3
    store.close()


# ── 路由：include_chats 默认开 + 可显式关 ───────────────────────────────────

def test_route_include_chats_defaults_on_and_can_opt_out(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, store=_tg_store(tmp_path))
    d = client.get("/api/platforms/telegram/tg1/contacts?enriched=1").json()
    assert d["include_chats"] is True
    assert {c["chat_key"] for c in d["contacts"]} == {"111", "222", "333"}
    assert d["summary"]["chat_only"] == 3 and d["summary"]["in_book"] == 0
    # 要纯通讯录口径的消费方显式关掉 → 回到「TG 面板恒空」的老数字
    d0 = client.get("/api/platforms/telegram/tg1/contacts"
                    "?enriched=1&include_chats=0").json()
    assert d0["include_chats"] is False
    assert d0["contacts"] == [] and d0["summary"]["total"] == 0
    store.close()


def test_route_only_chat_only(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, store=_tg_store(tmp_path))
    d = client.get("/api/platforms/telegram/tg1/contacts"
                   "?enriched=1&only=chat_only").json()
    assert {c["chat_key"] for c in d["contacts"]} == {"111", "222", "333"}
    assert d["summary"]["total"] == 3     # 汇总仍给全景基数，不跟随 only
    store.close()


def test_route_include_chats_ignored_without_enriched(tmp_path, monkeypatch):
    """``include_chats`` 仅 enriched 有效：旧形状分支逐字段不变。"""
    client, store = _client(tmp_path, monkeypatch)
    data = client.get(
        "/api/platforms/whatsapp/acct1/contacts?include_chats=1").json()
    assert set(data) == {"ok", "platform", "account_id", "count", "contacts"}
    assert data["count"] == 4
    store.close()
