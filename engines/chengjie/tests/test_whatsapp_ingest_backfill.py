"""WhatsApp ingest 空 account_id 回填护栏（2026-07 离线回放事故回归网）。

事故：Baileys Node 断线假死 2 小时，人工重启后离线队列回放的消息因 Node 侧
accountId 竞态带了 ``account_id=""``，ingest 原样收下 → 建出孤儿会话
``whatsapp::<peer>``（与正常 ``whatsapp:<acct>:<peer>`` 同一客户裂成两条线程），
且 protocol_autoreply 因 payload 缺 account_id 直接 return，不自动回复。

契约（只对 whatsapp 生效，其它平台 account_id 语义不同、零变化）：
- 注册表在册（未删除）whatsapp 账号恰好 1 个 → 无歧义回填：落正常会话线程、
  不建孤儿；autoreply hook 拿到的 payload 也必须带回填后的 account_id
  （防「落库归对了、自动回复链还是空」的半修状态）；
- 0 个或多个在册账号 → 归属有歧义，保持原行为照收进孤儿线程
  （Node 推送无重试，拒收 4xx = 丢消息——消息绝不能丢），仅记 error 日志；
- telegram 等其它平台空 account_id → 行为零变化，注册表根本不被查询。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.routes.unified_inbox_account_routes as uar
from src.inbox.store import InboxStore
from src.integrations.account_registry import AccountRegistry


def _client(tmp_path, monkeypatch, wa_accounts=()):
    """挂好 account routes + 独立 InboxStore/AccountRegistry 的 TestClient。

    注册表用真 ``AccountRegistry``（tmp 库）而非裸桩——回填读的是
    ``list(platform="whatsapp")`` 真签名（含"排除 removed"语义），桩会盖住签名漂移。
    """
    app = FastAPI()
    uar.register_account_routes(app, api_auth=lambda request: None,
                                config_manager=None)
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    reg = AccountRegistry(tmp_path / "reg.db")
    for acct in wa_accounts:
        reg.upsert("whatsapp", acct, mode="protocol", status="online")
    # 路由模块顶部 `from ... import get_account_registry` → 打模块属性即可注入
    monkeypatch.setattr(uar, "get_account_registry", lambda: reg)
    return TestClient(app), store, reg


def _ingest(client, **overrides):
    payload = {
        "platform": "whatsapp", "account_id": "",
        "chat_key": "639273815533", "name": "", "text": "回放的离线消息",
        "ts": 1750000000.0, "msg_id": "WAMID_REPLAY_1", "direction": "in",
    }
    payload.update(overrides)
    return client.post("/api/internal/protocol/ingest", json=payload)


# ── ① 恰好一个在册账号 → 回填，不建孤儿线程 ─────────────────────────────────

def test_backfill_when_single_registered_account(tmp_path, monkeypatch, caplog):
    import src.integrations.protocol_bridge as pb

    c, store, _reg = _client(tmp_path, monkeypatch,
                             wa_accounts=("639270135480",))
    # 捕获 autoreply hook 实收 payload（monkeypatch 自动还原全局 hook）
    captured = []
    monkeypatch.setattr(pb, "_reply_hook", lambda p: captured.append(dict(p)))

    with caplog.at_level(logging.WARNING):
        r = _ingest(c)
    assert r.status_code == 200, r.text
    cid = r.json()["conversation_id"]
    assert cid == "whatsapp:639270135480:639273815533"
    # 落库归到正常线程，且**没有**孤儿会话
    assert store.get_conversation("whatsapp:639270135480:639273815533")
    assert store.get_conversation("whatsapp::639273815533") is None
    msgs = store.list_messages(cid)
    assert len(msgs) == 1 and msgs[0]["text"] == "回放的离线消息"
    # 自动回复链贯穿：hook 拿到的是回填后的 account_id（修「缺 account_id 直接 return」）
    assert captured and captured[0]["account_id"] == "639270135480"
    assert captured[0]["chat_key"] == "639273815533"
    # 发生了回填要大声说（指向 Node accountId 竞态，便于日志检索定位）
    assert any("639270135480" in rec.getMessage() and "回填" in rec.getMessage()
               for rec in caplog.records if rec.levelno == logging.WARNING)


def test_backfill_ignores_removed_accounts(tmp_path, monkeypatch):
    """在册 1 个 online + 1 个 removed → removed 不算数，仍无歧义回填。"""
    c, store, reg = _client(tmp_path, monkeypatch,
                            wa_accounts=("639270135480",))
    reg.upsert("whatsapp", "639999999999", mode="protocol", status="online")
    reg.remove("whatsapp", "639999999999")
    r = _ingest(c)
    assert r.json()["conversation_id"] == "whatsapp:639270135480:639273815533"
    assert store.get_conversation("whatsapp::639273815533") is None


# ── ② 0 个 / 多个在册账号 → 歧义，照收进孤儿线程（消息绝不丢） ───────────────

def test_zero_accounts_falls_back_to_orphan_thread(tmp_path, monkeypatch, caplog):
    c, store, _reg = _client(tmp_path, monkeypatch, wa_accounts=())
    with caplog.at_level(logging.ERROR):
        r = _ingest(c)
    assert r.status_code == 200
    cid = r.json()["conversation_id"]
    assert cid == "whatsapp::639273815533"  # 原行为：孤儿线程
    msgs = store.list_messages(cid)
    assert len(msgs) == 1 and msgs[0]["text"] == "回放的离线消息"  # 不丢消息
    assert any("歧义" in rec.getMessage() for rec in caplog.records
               if rec.levelno == logging.ERROR)


def test_two_accounts_ambiguous_falls_back_to_orphan_thread(
        tmp_path, monkeypatch, caplog):
    c, store, _reg = _client(
        tmp_path, monkeypatch,
        wa_accounts=("639270135480", "639888888888"))
    with caplog.at_level(logging.ERROR):
        r = _ingest(c)
    assert r.json()["conversation_id"] == "whatsapp::639273815533"
    assert len(store.list_messages("whatsapp::639273815533")) == 1
    # 两个候选都不该被瞎猜归属
    assert store.get_conversation("whatsapp:639270135480:639273815533") is None
    assert store.get_conversation("whatsapp:639888888888:639273815533") is None
    assert any("歧义" in rec.getMessage() for rec in caplog.records
               if rec.levelno == logging.ERROR)


def test_registry_read_failure_degrades_to_orphan(tmp_path, monkeypatch):
    """注册表读挂了（DB 锁/损坏）→ 按 0 候选处理照收，绝不 5xx 丢消息。"""
    c, store, _reg = _client(tmp_path, monkeypatch, wa_accounts=())

    def _boom():
        raise RuntimeError("registry db locked")

    monkeypatch.setattr(uar, "get_account_registry", _boom)
    r = _ingest(c)
    assert r.status_code == 200
    assert r.json()["conversation_id"] == "whatsapp::639273815533"
    assert len(store.list_messages("whatsapp::639273815533")) == 1


# ── ③ 行为不变面：telegram 空 account_id / whatsapp 带 account_id ────────────

def test_telegram_empty_account_id_unchanged(tmp_path, monkeypatch):
    """telegram 空 account_id 行为零变化：不查注册表、照旧落 telegram::<peer>。"""
    c, store, reg = _client(tmp_path, monkeypatch,
                            wa_accounts=("639270135480",))
    list_calls = []
    real_list = reg.list
    monkeypatch.setattr(
        reg, "list",
        lambda platform=None, **kw: (list_calls.append(platform),
                                     real_list(platform=platform, **kw))[1])
    r = _ingest(c, platform="telegram", chat_key="777000111")
    assert r.json()["conversation_id"] == "telegram::777000111"
    assert list_calls == []  # 注册表根本没被碰
    assert len(store.list_messages("telegram::777000111")) == 1


def test_whatsapp_with_account_id_skips_backfill(tmp_path, monkeypatch):
    """带 account_id 的正常 push 不进回填分支（注册表零查询，热路径零开销）。"""
    c, store, reg = _client(tmp_path, monkeypatch,
                            wa_accounts=("639270135480",))
    list_calls = []
    real_list = reg.list
    monkeypatch.setattr(
        reg, "list",
        lambda platform=None, **kw: (list_calls.append(platform),
                                     real_list(platform=platform, **kw))[1])
    r = _ingest(c, account_id="639270135480")
    assert (r.json()["conversation_id"]
            == "whatsapp:639270135480:639273815533")
    assert list_calls == []
