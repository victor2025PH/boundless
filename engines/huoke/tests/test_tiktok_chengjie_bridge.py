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


# ═══ 独立轮询器（不再只挂 check_inbox 尾部）═══════════════════════════════════════════════

def test_poll_planner_only_wakes_devices_with_queued_items():
    pending = {"DEV-A": {"ok": True, "queued": 2, "oldest_wait_sec": 40}, "DEV-B": {"ok": True, "queued": 0},
               "DEV-D": {"ok": False, "error": "boom"}}
    polled: List[str] = []

    def http(method, url, headers, body):
        assert method == "GET" and "/handback/pending?" in url
        did = url.split("device_id=")[1].split("&")[0]
        polled.append(did)
        return (200 if pending[did].get("ok") else 503), pending[did]

    busy = {"DEV-C": {"tiktok_check_inbox"}, "DEV-E": {"tiktok_chengjie_handback"}}
    plan = cj.plan_handback_tasks(["DEV-A", "DEV-B", "DEV-C", "DEV-D", "DEV-E"], busy, cfg=CFG_CJ, http=http)
    assert [x["device_id"] for x in plan["create"]] == ["DEV-A"] and plan["create"][0]["queued"] == 2
    assert plan["skipped"] == {"DEV-B": "empty", "DEV-C": "busy", "DEV-E": "busy"}
    assert plan["errors"] == {"DEV-D": "boom"} and plan["polled"] == 3
    assert sorted(polled) == ["DEV-A", "DEV-B", "DEV-D"]  # 忙设备不打 HTTP、不占设备锁
    # local / 关轮询 → 零 HTTP
    assert cj.plan_handback_tasks(["DEV-A"], {}, cfg=CFG_LOCAL, http=lambda *a: (_ for _ in ()).throw(AssertionError("no http")))["skipped"] == {"*": "not_chengjie"}
    off = {**CFG_CJ, "chengjie": {**CFG_CJ["chengjie"], "handback_poll_sec": 0}}
    assert cj.plan_handback_tasks(["DEV-A"], {}, cfg=off, http=None)["skipped"] == {"*": "poll_off"}
    assert cj.reply_cfg(off)["handback_poll_sec"] == 0 and cj.reply_cfg(CFG_CJ)["handback_poll_sec"] == 120
    assert cj.reply_cfg({**CFG_CJ, "chengjie": {**CFG_CJ["chengjie"], "handback_poll_sec": 5}})["handback_poll_sec"] == cj.POLL_MIN_SEC


def test_poll_once_creates_tasks_via_injected_host():
    created: List[str] = []
    r = cj.run_handback_poll_once(
        cfg=CFG_CJ, http=lambda m, u, h, b: (200, {"ok": True, "queued": 1}),
        list_online=lambda: ["DEV-A", "DEV-B"], busy_types=lambda: {"DEV-B": {"tiktok_chengjie_handback"}},
        create_task=lambda did: created.append(did) or f"task-{did}")
    assert created == ["DEV-A"] and r["created"] == ["task-DEV-A"] and r["skipped"] == {"DEV-B": "busy"}
    assert cj.run_handback_poll_once(cfg=CFG_LOCAL, list_online=lambda: ["DEV-A"])["skipped"] == {"*": "not_chengjie"}
    assert cj.run_handback_poll_once(cfg=CFG_CJ, list_online=lambda: [])["skipped"] == {"*": "no_online_devices"}


def test_executor_handback_task_local_skips_and_chengjie_drains(monkeypatch):
    from unittest.mock import MagicMock, patch
    from src.host import executor as ex

    tt = MagicMock()
    tt.send_dm.return_value = True
    with patch.object(ex, "_fresh_tiktok", return_value=tt), patch.object(ex, "_check_tiktok_version", lambda *a, **k: None):
        monkeypatch.setattr(cj, "_load_yaml", lambda: CFG_LOCAL)
        ok, msg, data = ex._execute_tiktok(MagicMock(), "DEV-A", cj.TASK_HANDBACK, {})
        assert ok is True and data["skipped"] == "not_chengjie" and not tt.send_dm.called
        monkeypatch.setattr(cj, "_load_yaml", lambda: CFG_CJ)
        calls: List[Tuple[str, str]] = []

        def fake_http(cfg, method, path, *, query="", body=None, http=None):
            calls.append((method, path))
            if path == cj.HANDBACK_PATH:
                return 200, {"ok": True, "items": [{"id": 3, "username": "buyer_1", "text": "hello"}]}
            return 200, {"ok": True}

        monkeypatch.setattr(cj, "_http", fake_http)
        ok, msg, data = ex._execute_tiktok(MagicMock(), "DEV-A", cj.TASK_HANDBACK, {})
        assert ok is True and data["chengjie_handback"]["sent"] == 1
        tt.send_dm.assert_called_once_with("buyer_1", "hello")
        assert [p for _, p in calls] == [cj.DEVICES_PATH, cj.HANDBACK_PATH, cj.HANDBACK_ACK_PATH]
    assert ex._TASK_TYPE_TIMEOUTS[cj.TASK_HANDBACK] == 300
    from src.host.schemas import TaskType
    assert TaskType(cj.TASK_HANDBACK).value == cj.TASK_HANDBACK
