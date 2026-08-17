# -*- coding: utf-8 -*-
"""``/api/unified-inbox/conv-probe``（深链探针，2026-08-09）门禁。

案例页「打开会话」落空事故的失败出路收口：深链落空的四类真实原因
（账号段拼错 / 沉在列表窗口外 / 账号已登出 / 真归档）此前被前端一律误报
「可能已归档」。本端点按 id 直查持久库 → 尾段 chat_key 反查自动纠错 →
归档/账号状态如实回报；前端 ``_rescueConvFromStore`` 据此分支出话。
"""

from __future__ import annotations

import pytest

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore


@pytest.fixture()
def probe_env(auth_client, tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:8244899900:12345",
        platform="telegram", account_id="8244899900", chat_key="12345",
        display_name="王先生", last_text="hi", last_ts=1000.0))
    store.upsert_conversation(InboxConversation(
        conversation_id="line:acct1:U99",
        platform="line", account_id="acct1", chat_key="U99",
        display_name="Ms. Lin", last_text="hello", last_ts=2000.0))
    old = getattr(auth_client.app.state, "inbox_store", None)
    auth_client.app.state.inbox_store = store
    try:
        yield auth_client, store
    finally:
        auth_client.app.state.inbox_store = old
        try:
            store.close()
        except Exception:
            pass


def test_probe_exact_hit(probe_env):
    client, _ = probe_env
    d = client.get(
        "/api/unified-inbox/conv-probe?cid=telegram:8244899900:12345").json()
    assert d["ok"] is True and d["found"] is True and d["exact"] is True
    assert d["resolved_cid"] == "telegram:8244899900:12345"
    assert d["archived"] is False
    assert d["chat"]["conversation_id"] == "telegram:8244899900:12345"
    assert d["chat"]["platform"] == "telegram"


def test_probe_fuzzy_rescues_wrong_account_segment(probe_env):
    """账号段拼错（conv_ref 缺省猜测 ``default``）→ 尾段 chat_key 反查唯一
    命中即自动纠正——2026-08-09 深链落空事故里最常见的引用形态。"""
    client, _ = probe_env
    d = client.get(
        "/api/unified-inbox/conv-probe?cid=telegram:default:12345").json()
    assert d["found"] is True and d["exact"] is False
    assert d["resolved_cid"] == "telegram:8244899900:12345"
    assert d["chat"]["conversation_id"] == "telegram:8244899900:12345"


def test_probe_archived_still_openable(probe_env):
    client, store = probe_env
    store.set_conv_archived("line:acct1:U99", True, source="test", actor="t")
    d = client.get("/api/unified-inbox/conv-probe?cid=line:acct1:U99").json()
    assert d["found"] is True and d["archived"] is True
    assert d["chat"] is not None    # 归档仍可开——前端照常打开并如实提示


def test_probe_offline_account_reports_status(probe_env):
    from src.integrations.account_registry import get_account_registry
    client, _ = probe_env
    get_account_registry().upsert(
        "telegram", "8244899900", mode="protocol", status="offline")
    d = client.get(
        "/api/unified-inbox/conv-probe?cid=telegram:8244899900:12345").json()
    assert d["found"] is True
    assert d["account_status"] == "offline"
    assert d["chat"] is None        # thread 对 offline 拒 409，注入了也打不开


def test_probe_not_found_and_param_guard(probe_env):
    client, _ = probe_env
    d = client.get(
        "/api/unified-inbox/conv-probe?cid=telegram:x:999999999").json()
    assert d["found"] is False
    r = client.get("/api/unified-inbox/conv-probe")
    assert r.status_code == 400


def test_probe_without_store_degrades(auth_client):
    old = getattr(auth_client.app.state, "inbox_store", None)
    auth_client.app.state.inbox_store = None
    try:
        d = auth_client.get(
            "/api/unified-inbox/conv-probe?cid=telegram:a:1").json()
        assert d["ok"] is True and d["found"] is False
    finally:
        auth_client.app.state.inbox_store = old
