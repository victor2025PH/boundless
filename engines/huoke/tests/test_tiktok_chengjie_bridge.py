"""TK-3 ①-A：huoke × 智聊脑手分离。假 HTTP，零网络。reply_engine=local 零 diff。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from src.app_automation import tiktok_chengjie_bridge as cj

CFG_LOCAL = {"reply_engine": "local"}
CFG_CJ = {
    "reply_engine": "chengjie",
    "chengjie": {
        "endpoint": "http://chengjie.test",
        "token": "tok",
        "account_id": "shop-ph",
        "username": "myshop_ph",
        "timezone": "Asia/Manila",
        "device_account_map": {"DEV-A": {"account_id": "acc-1", "username": "u_a", "timezone": "Asia/Manila"}},
    },
}


def test_local_engine_is_default_and_zero_side_effect():
    assert cj.reply_cfg({}).get("reply_engine") == "local"
    assert cj.reply_cfg({"reply_engine": "bogus"})["reply_engine"] == "local"
    assert cj.is_chengjie(CFG_LOCAL) is False
    assert cj.validate_auto_reply(True, CFG_LOCAL) == ""
    assert cj.drain_handback(device_id="DEV-A", send_dm=lambda a, b: True, cfg=CFG_LOCAL)["skipped"] == "not_chengjie"
    calls: List[Any] = []
    r = cj.dispatch_inbox_reply(
        auto_reply=True, classifier=object(), conv_data={"contact": "x", "messages": []},
        device_id="DEV-A", local_handler=lambda: calls.append("local") or {"action": "auto_replied"},
        cfg=CFG_LOCAL, http=lambda *a: calls.append(a) or (200, {}))
    assert r == {"action": "auto_replied"} and calls == ["local"]


def test_chengjie_mutex_skips_local_brain():
    assert "互斥" in cj.validate_auto_reply(True, CFG_CJ)
    assert cj.validate_auto_reply(False, CFG_CJ) == ""
    calls: List[Any] = []

    def http(method, url, headers, body):
        calls.append((method, url, body))
        return 200, {"ok": True, "accepted": 1, "echo": 0, "drafted": 1}

    r = cj.dispatch_inbox_reply(
        auto_reply=True, classifier=object(),
        conv_data={"contact": "buyer_1", "messages": [{"text": "hi", "direction": "inbound"}]},
        device_id="DEV-A", local_handler=lambda: calls.append("LOCAL") or {"action": "auto_replied"},
        cfg=CFG_CJ, http=http)
    assert r["action"] == "forwarded" and r["accepted"] == 1
    assert "LOCAL" not in calls
    assert calls[0][0] == "POST" and calls[0][1].endswith("/api/tiktok/huoke/dm")


def test_messages_from_conversation_include_outbound_echo():
    conv = {"contact": "@buyer_1", "messages": [
        {"text": "Hi there", "direction": "outbound"},
        {"text": "how much?", "direction": "inbound"},
        {"text": "", "media_type": "image", "direction": "inbound"},
        {"text": "", "direction": "inbound"},
    ]}
    msgs = cj.messages_from_conversation(conv)
    assert [m["direction"] for m in msgs] == ["out", "in", "in"]
    assert msgs[0]["peer_username"] == "buyer_1" and msgs[2]["media_type"] == "image"


def test_account_map_and_bind_and_post_dm():
    acc = cj.account_for_device("DEV-A", CFG_CJ)
    assert acc == {"account_id": "acc-1", "username": "u_a", "timezone": "Asia/Manila"}
    acc2 = cj.account_for_device("UNKNOWN", CFG_CJ)
    assert acc2["account_id"] == "shop-ph"
    seen: List[Tuple] = []

    def http(method, url, headers, body):
        seen.append((method, url, body, headers.get("Authorization")))
        return 200, {"ok": True, "health": "authorized", "accepted": 1, "echo": 1}

    st, res = cj.bind_device("DEV-A", cfg=CFG_CJ, http=http)
    assert st == 200 and res["health"] == "authorized"
    assert seen[0][2]["account_id"] == "acc-1" and seen[0][3] == "Bearer tok"
    st, res = cj.post_dm(
        {"contact": "buyer_1", "messages": [
            {"text": "hello", "direction": "outbound"}, {"text": "price?", "direction": "inbound"}]},
        device_id="DEV-A", cfg=CFG_CJ, http=http)
    assert st == 200 and res["accepted"] == 1
    body = seen[-1][2]
    assert body["account_id"] == "acc-1" and [m["direction"] for m in body["messages"]] == ["out", "in"]


def test_drain_handback_send_ack_success_and_failure():
    inbox: List[Dict[str, Any]] = [{
        "id": 7, "username": "buyer_1", "text": "350 pesos, free shipping!", "kind": "dm"}]
    acks: List[Dict[str, Any]] = []
    sent: List[Tuple[str, str]] = []

    def http(method, url, headers, body):
        if method == "GET":
            return 200, {"ok": True, "items": list(inbox)}
        acks.append(body)
        return 200, {"ok": True, "status": "sent" if body.get("ok") else "failed"}

    r = cj.drain_handback(device_id="DEV-A", send_dm=lambda a, b: sent.append((a, b)) or True,
                          cfg=CFG_CJ, http=http)
    assert r == {"ok": True, "claimed": 1, "sent": 1, "failed": 0,
                 "items": [{"item_id": 7, "ok": True, "error": "", "recipient": "buyer_1"}]}
    assert sent == [("buyer_1", "350 pesos, free shipping!")]
    assert acks[0]["ok"] is True and acks[0]["item_id"] == 7

    def boom(a, b):
        raise RuntimeError("ui gone")

    r = cj.drain_handback(device_id="DEV-A", send_dm=boom, cfg=CFG_CJ, http=http)
    assert r["failed"] == 1 and r["sent"] == 0
    assert acks[-1]["ok"] is False and acks[-1]["error"] == "exception:RuntimeError"

    r = cj.drain_handback(device_id="DEV-A", send_dm=lambda a, b: False, cfg=CFG_CJ, http=http)
    assert r["failed"] == 1 and acks[-1]["error"] == "send_failed"
