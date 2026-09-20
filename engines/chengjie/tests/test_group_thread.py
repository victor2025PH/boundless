# -*- coding: utf-8 -*-
"""群/频道 P0 纯函数 + Zalo chat_type 透传 + 引用回执镜像门禁。"""
from __future__ import annotations

from src.inbox.group_thread import (
    annotate_quote_applied,
    filter_send_kwargs,
    is_one_to_one,
    lookup_chat_type,
    normalize_chat_type,
    quote_is_native,
    should_mirror_quote,
    speakers_as_members,
)
from src.inbox.store import InboxStore
from src.integrations.protocol_bridge import ingest_incoming


def test_normalize_chat_type_aliases():
    assert normalize_chat_type("GROUP") == "group"
    assert normalize_chat_type("supergroup") == "group"
    assert normalize_chat_type("room") == "group"
    assert normalize_chat_type("gigagroup") == "group"
    assert normalize_chat_type("group_thread") == "group"
    assert normalize_chat_type("community") == "group"
    assert normalize_chat_type("channel") == "channel"
    assert normalize_chat_type("user") == "private"
    assert normalize_chat_type("") == "private"
    assert normalize_chat_type("mystery") == "private"


def test_is_one_to_one_is_whitelist_not_blacklist():
    """自动回复闸门判据：只有真 1:1 放行，channel/room/supergroup 一律拦。

    旧口径 `chat_type != "group"` 把这三类全放进自动回复（生产实测 zhiliao
    有 10 个 TG 频道会话），本条钉死白名单语义。
    """
    assert is_one_to_one("") is True
    assert is_one_to_one("private") is True
    assert is_one_to_one("user") is True
    assert is_one_to_one("channel") is False
    assert is_one_to_one("supergroup") is False
    assert is_one_to_one("room") is False
    assert is_one_to_one("group_thread") is False
    assert is_one_to_one("GROUP") is False


def test_ingest_route_auto_reply_gate_uses_whitelist():
    """静态接线钉：ingest 路由不得退回 `chat_type != "group"` 黑名单。"""
    from pathlib import Path
    src = Path("src/web/routes/unified_inbox_account_routes.py").read_text(encoding="utf-8")
    assert 'is_one_to_one as _is_1to1' in src
    assert 'direction == "in" and _one_to_one and not _is_spam_request' in src
    assert '_chat_type != "group"' not in src


def test_quote_is_native_whitelist():
    assert quote_is_native("telegram") is True
    assert quote_is_native("WhatsApp") is True
    assert quote_is_native("messenger") is False
    assert quote_is_native("line") is False
    assert quote_is_native("zalo") is False


def test_filter_send_kwargs_ignores_var_keyword():
    def send(chat_key, text, *, reply_to=None, **kwargs):
        return None

    out = filter_send_kwargs(send, {"reply_to": {"id": "1"}, "chat_type": "group", "mentions": ["a"]})
    assert out == {"reply_to": {"id": "1"}}
    assert "chat_type" not in out
    assert "mentions" not in out


def test_filter_send_kwargs_named_only_and_drops_none():
    def send(chat_key, text, *, reply_to=None, chat_type=None):
        return None

    out = filter_send_kwargs(
        send, {"reply_to": {"id": "1"}, "chat_type": None, "mentions": ["x"]})
    assert out == {"reply_to": {"id": "1"}}


def test_annotate_and_mirror_quote_requires_receipt():
    reply = {"id": "m1", "text": "hi"}
    bare = annotate_quote_applied({"delivered": True})
    assert bare["quote_applied"] is False
    assert should_mirror_quote(reply, bare) is False
    flagged = annotate_quote_applied({"delivered": True, "quote_applied": True})
    assert should_mirror_quote(reply, flagged) is True
    assert should_mirror_quote({}, flagged) is False


def test_speakers_as_members_wa_and_nameless():
    rows = [
        {"sender_id": "639111@s.whatsapp.net", "sender_name": "Ann"},
        {"sender_id": "639111@s.whatsapp.net", "sender_name": "Ann"},
        {"sender_id": "639777", "sender_name": "Bob"},
        {"sender_id": "", "sender_name": "陈三"},
    ]
    members = speakers_as_members(rows)
    assert [m["name"] for m in members] == ["Ann", "Bob", "陈三"]
    assert members[0]["number"] == "639111"
    assert members[1]["number"] == "639777"
    assert members[2]["number"] == ""
    assert members[2]["jid"] == ""


def test_lookup_chat_type_from_store(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    ingest_incoming(
        store, platform="zalo", account_id="acc", chat_key="gid-1",
        name="工群", text="hi", direction="in", chat_type="group",
        sender_id="u1", sender_name="Alice",
    )
    assert lookup_chat_type("zalo", "acc", "gid-1", store=store) == "group"
    assert lookup_chat_type("zalo", "acc", "missing", store=store) == ""


def test_list_group_speakers_distinct_inbound(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = ingest_incoming(
        store, platform="telegram", account_id="a", chat_key="-1001",
        name="群", text="one", ts=10, msg_id="1", direction="in",
        chat_type="group", sender_id="11", sender_name="甲",
    )
    ingest_incoming(
        store, platform="telegram", account_id="a", chat_key="-1001",
        name="群", text="two", ts=20, msg_id="2", direction="in",
        chat_type="group", sender_id="22", sender_name="乙",
    )
    ingest_incoming(
        store, platform="telegram", account_id="a", chat_key="-1001",
        name="群", text="out", ts=30, msg_id="3", direction="out",
        chat_type="group",
    )
    ingest_incoming(
        store, platform="telegram", account_id="a", chat_key="-1001",
        name="群", text="one-again", ts=40, msg_id="4", direction="in",
        chat_type="group", sender_id="11", sender_name="甲",
    )
    rows = store.list_group_speakers(cid)
    names = [r["sender_name"] for r in rows]
    assert names == ["甲", "乙"]  # 最近发言优先，同人去重
    members = speakers_as_members(rows)
    assert {m["jid"] for m in members} == {"11", "22"}


async def test_zalo_send_and_media_pass_chat_type(monkeypatch):
    from src.integrations.account_orchestrator import ZaloPersonalWorker

    captured = []

    async def fake_post(url, payload, timeout=20.0):
        captured.append((url, dict(payload)))
        return {"ok": True, "message_id": "z1"}

    monkeypatch.setattr("src.integrations.zalo_personal_login._post_json", fake_post)
    w = ZaloPersonalWorker({"account_id": "acc1"}, {})
    monkeypatch.setattr(w, "_session_unhealthy", lambda: False)
    res = await w.send("gid", "hello", chat_type="group")
    assert res["delivered"] is True
    assert captured[0][1]["thread_id"] == "gid"
    assert captured[0][1]["chat_type"] == "group"
    await w.send_media("gid", media_path="C:\\x.jpg", media_type="image",
                       caption="", chat_type="group")
    assert captured[1][1]["chat_type"] == "group"
    # 未知类型不传键，让 Node 注册表自己判
    await w.send("uid", "hi")
    assert "chat_type" not in captured[2][1]
