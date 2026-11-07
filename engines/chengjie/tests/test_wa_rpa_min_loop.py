"""WhatsApp RPA 最小闭环。只注入假设备和录好的 UI dump，不调用系统 adb。"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from src.inbox.store import InboxStore
from src.integrations.whatsapp_rpa.min_loop import (
    WaMinLoop,
    classify_send_evidence,
    resolve_identity,
)
from src.integrations.whatsapp_rpa.state_store import WaRpaStateStore

_FIX = Path(__file__).resolve().parent / "fixtures" / "wa_rpa"
_PHONE = "639175550101"
_ACCOUNT = "wa_test"


def _xml(name: str) -> bytes:
    return (_FIX / name).read_bytes()


def _with_outgoing(xml: bytes, text: str) -> bytes:
    snippet = (
        '<node resource-id="com.whatsapp:id/message_text" '
        f'text="{text}" content-desc="" bounds="[420,1100][700,1180]" />'
    )
    raw = xml.decode("utf-8")
    return raw.replace("</hierarchy>", snippet + "</hierarchy>").encode("utf-8")


class FakeWaDevice:
    """脚本化的假手机。dump 按当前画面返回夹具，send_text 只计数。"""

    def __init__(self, list_xml: bytes, thread_xml: bytes, *, start: str = "list"):
        self.list_xml = list_xml
        self.thread_xml = thread_xml
        self.mode = start
        self.screen_width = 720
        self.screen_height = 1600
        self.serial = "FAKE-NO-ADB"
        self.send_count = 0
        self.sent: list[str] = []
        self.taps: list[tuple[int, int]] = []

    def dump(self) -> bytes:
        if self.mode == "list":
            return self.list_xml
        if self.mode == "sent" and self.sent:
            return _with_outgoing(self.thread_xml, self.sent[-1])
        return self.thread_xml

    def tap(self, x: int, y: int) -> None:
        self.taps.append((int(x), int(y)))
        if self.mode == "list":
            self.mode = "thread"

    def send_text(self, text: str) -> None:
        self.send_count += 1
        self.sent.append(text)
        self.mode = "sent"

    def back(self) -> None:
        self.mode = "list"


class _Skill:
    def __init__(self, reply: str = "pong") -> None:
        self.reply = reply
        self.calls = 0

    async def process_message(self, text, user_id="", context=None):
        self.calls += 1
        return self.reply


def _loop(tmp_path: Path, device: FakeWaDevice, **cfg):
    state = WaRpaStateStore(tmp_path / "wa.db")
    inbox = InboxStore(tmp_path / "inbox.db")
    merged = {"reply_mode": "auto", "account_id": _ACCOUNT}
    merged.update(cfg)
    loop = WaMinLoop(
        account_id=_ACCOUNT,
        state_store=state,
        inbox_store=inbox,
        cfg=merged,
        device=device,
    )
    return loop, state, inbox


def _forbid_adb(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("测试调用了系统 adb")

    import subprocess
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


@pytest.fixture(autouse=True)
def _no_adb(monkeypatch):
    _forbid_adb(monkeypatch)


def test_inbound_queue_and_inbox_share_conversation_id(tmp_path: Path):
    device = FakeWaDevice(_xml("list_unread_alex.xml"), _xml("thread_alex_hello.xml"))
    loop, state, inbox = _loop(tmp_path, device)
    out = loop.process_unread(reply_text="auto reply")
    cid = f"whatsapp:{_ACCOUNT}:{_PHONE}"
    assert out["conversation_id"] == cid
    assert out["status"] == "sent"
    assert out["receipt"] == "sent"
    assert out["phone_unresolved"] is False
    assert device.send_count == 1

    msgs = inbox.list_messages(cid)
    assert {m["direction"] for m in msgs} == {"in", "out"}
    assert {m["conversation_id"] for m in msgs} == {cid}
    queued = state.list_send_queue(include_done=True)
    assert len(queued) == 1
    assert queued[0]["conversation_id"] == cid
    assert queued[0]["chat_key"] == cid
    assert queued[0]["status"] == "sent"
    assert queued[0]["client_msg_id"]


def test_duplicate_client_msg_id_sends_once(tmp_path: Path):
    device = FakeWaDevice(_xml("list_unread_alex.xml"), _xml("thread_alex_hello.xml"))
    loop, state, _inbox = _loop(tmp_path, device)
    ident = resolve_identity(_ACCOUNT, display_name="Alex", phone=_PHONE)
    first = loop.deliver(
        identity=ident, text="auto reply", peer_text="hello from device",
        source="unread", client_msg_id="cmsg-1",
    )
    second = loop.deliver(
        identity=ident, text="auto reply", peer_text="hello from device",
        source="unread", client_msg_id="cmsg-1",
    )
    assert first["status"] == "sent"
    assert second["duplicate"] is True
    assert device.send_count == 1
    rows = state.list_send_queue(include_done=True)
    assert len(rows) == 1
    assert rows[0]["client_msg_id"] == "cmsg-1"


def test_dry_run_is_not_sent(tmp_path: Path):
    device = FakeWaDevice(_xml("list_unread_alex.xml"), _xml("thread_alex_hello.xml"))
    loop, state, _inbox = _loop(tmp_path, device)
    out = loop.process_unread(reply_text="auto reply", dry_run=True, client_msg_id="dry-1")
    assert out["status"] == "dry_run"
    assert out["status"] != "sent"
    assert device.send_count == 0
    row = state.list_send_queue(include_done=True)[0]
    assert row["status"] == "dry_run"
    assert row["receipt"] == "dry_run"
    assert row["sent_at"] is None


def test_stop_blocks_send_and_manual_mode_does_not(tmp_path: Path):
    stop_device = FakeWaDevice(_xml("list_unread_alex.xml"), _xml("thread_alex_stop.xml"))
    loop, state, inbox = _loop(tmp_path, stop_device)
    stopped = loop.process_unread(reply_text="should not send")
    assert stopped["step"] == "stop_contact"
    assert stop_device.send_count == 0
    cid = f"whatsapp:{_ACCOUNT}:{_PHONE}"
    assert stopped["conversation_id"] == cid
    inbound = inbox.list_messages(cid)
    assert len(inbound) == 1 and inbound[0]["direction"] == "in"
    assert int(state.get_chat_state(cid).get("blacklist") or 0) == 1

    # 之后即使来的不是 STOP，黑名单仍挡住。人工接管档不参与这个判断。
    hello = FakeWaDevice(_xml("list_unread_alex.xml"), _xml("thread_alex_hello.xml"))
    loop._device = hello
    inbox.set_automation_mode(cid, "manual", source="test")
    again = loop.process_unread(reply_text="auto reply")
    assert again["step"] in {"blacklist", "quiet", "stop_contact"}
    assert hello.send_count == 0

    fresh = FakeWaDevice(_xml("list_unread_alex.xml"), _xml("thread_alex_hello.xml"))
    loop2, _state2, inbox2 = _loop(tmp_path / "manual", fresh)
    ident = resolve_identity(_ACCOUNT, display_name="Alex", phone=_PHONE)
    inbox2.set_automation_mode(ident.conversation_id, "manual", source="test")
    sent = loop2.process_unread(reply_text="auto reply")
    assert sent["status"] == "sent"
    assert fresh.send_count == 1
    assert inbox2.get_automation_mode(ident.conversation_id) == "manual"


def test_stop_gate_can_be_disabled(tmp_path: Path):
    device = FakeWaDevice(_xml("list_unread_alex.xml"), _xml("thread_alex_stop.xml"))
    loop, _state, _inbox = _loop(tmp_path, device, stop_gate_enabled=False)
    out = loop.process_unread(reply_text="still send")
    assert out["status"] == "sent"
    assert device.send_count == 1


def test_open_thread_obeys_reply_mode_off_and_approve(tmp_path: Path):
    for mode in ("off", "approve"):
        device = FakeWaDevice(
            _xml("list_unread_alex.xml"),
            _xml("thread_alex_hello.xml"),
            start="thread",
        )
        loop, _state, _inbox = _loop(tmp_path / mode, device, reply_mode=mode)
        out = loop.process_open_thread(reply_text="auto reply")
        assert device.send_count == 0, mode
        assert out["step"] == ("reply_mode_off" if mode == "off" else "reply_mode_approve")


def test_reply_routes_by_phone_not_first_display_name(tmp_path: Path):
    device = FakeWaDevice(_xml("list_two_alex.xml"), _xml("thread_alex_hello.xml"))
    loop, _state, _inbox = _loop(tmp_path, device)
    ident = resolve_identity(
        _ACCOUNT, display_name="Alex", phone="639222222222",
    )
    opened = loop.open_for_reply(ident)
    assert opened["ok"] is True
    assert opened["reason"] == "phone"
    assert device.taps[-1][1] > 500

    ambiguous = FakeWaDevice(_xml("list_two_alex_same_name.xml"), _xml("thread_alex_hello.xml"))
    loop._device = ambiguous
    named = resolve_identity(_ACCOUNT, display_name="Alex")
    refused = loop.open_for_reply(named)
    assert refused["ok"] is False
    assert refused["reason"] == "ambiguous_display_name"
    assert ambiguous.send_count == 0
    assert ambiguous.taps == []


def test_missing_phone_is_marked_fallback(tmp_path: Path):
    device = FakeWaDevice(
        _xml("list_unread_alex.xml"),
        _xml("thread_unresolved_name.xml"),
        start="thread",
    )
    loop, state, _inbox = _loop(tmp_path, device)
    out = loop.process_open_thread(reply_text="在的")
    assert out["phone_unresolved"] is True
    assert out["key_source"] == "display_name_fallback"
    assert ":name:" in out["conversation_id"]
    assert out["conversation_id"].split(":")[-1] != "店员" or out["phone_unresolved"]
    saved = state.get_chat_state(out["conversation_id"])
    assert int(saved.get("phone_unresolved") or 0) == 1
    assert saved.get("key_source") == "display_name_fallback"
    assert out["status"] == "sent"


def test_no_tick_evidence_is_not_delivered_or_read():
    evidence = classify_send_evidence(_xml("thread_alex_hello_sent.xml"), "auto reply")
    assert evidence["bubble"] is True
    assert evidence["status"] == "sent"
    assert evidence["receipt"] == "sent"
    assert evidence["receipt"] not in {"delivered", "read"}

    missing = classify_send_evidence(_xml("thread_alex_hello.xml"), "auto reply")
    assert missing["status"] == "send_unconfirmed"
    assert missing["receipt"] not in {"sent", "delivered", "read"}

    marked = _xml("thread_alex_hello_sent.xml").decode("utf-8").replace(
        'text="auto reply" content-desc=""',
        'text="auto reply" content-desc="已读"',
    ).encode("utf-8")
    read = classify_send_evidence(marked, "auto reply")
    assert read["status"] == "sent"
    assert read["receipt"] == "read"


def test_runner_open_thread_and_queue_use_fake_device(tmp_path: Path, monkeypatch):
    import asyncio

    import src.integrations.line_rpa.adb_helpers as adb
    from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner

    def boom(*_a, **_k):
        raise AssertionError("runner 测试调用了 adb")

    monkeypatch.setattr(adb, "run_adb", boom)
    monkeypatch.setattr(adb, "run_adb_binary", boom)
    monkeypatch.setattr(adb, "input_tap", boom)
    monkeypatch.setattr(adb, "input_keyevent", boom)
    monkeypatch.setattr(adb, "screen_size", boom)
    monkeypatch.setattr(adb, "pick_serial", boom)

    state = WaRpaStateStore(tmp_path / "wa.db")
    inbox = InboxStore(tmp_path / "inbox.db")
    device = FakeWaDevice(
        _xml("list_unread_alex.xml"),
        _xml("thread_alex_hello.xml"),
        start="thread",
    )
    skill = _Skill("pong")
    runner = WhatsAppRpaRunner(
        config_manager=None,
        skill_manager=skill,
        wa_cfg={"account_id": _ACCOUNT, "reply_mode": "auto"},
        state_store=state,
        device=device,
        inbox_store=inbox,
    )

    cid = f"whatsapp:{_ACCOUNT}:{_PHONE}"
    inbox.set_automation_mode(cid, "manual", source="test")
    result: dict = {"peer_name": "Alex"}
    out = asyncio.run(
        runner._process_open_thread_message(
            "FAKE", "", "hello from device", result, 0.0,
            xml=_xml("thread_alex_hello.xml"),
        )
    )
    assert out["conversation_id"] == cid
    assert out.get("send_status") == "sent"
    assert device.send_count == 1
    assert skill.calls == 1
    queued = state.list_send_queue(include_done=True)
    assert queued[0]["conversation_id"] == cid
    assert inbox.list_messages(cid)

    for mode in ("off", "approve"):
        quiet = FakeWaDevice(
            _xml("list_unread_alex.xml"),
            _xml("thread_alex_hello.xml"),
            start="thread",
        )
        held = WhatsAppRpaRunner(
            config_manager=None,
            skill_manager=_Skill(),
            wa_cfg={"account_id": _ACCOUNT, "reply_mode": mode},
            state_store=WaRpaStateStore(tmp_path / f"{mode}.db"),
            device=quiet,
            inbox_store=InboxStore(tmp_path / f"{mode}-inbox.db"),
        )
        held_out = asyncio.run(
            held._process_open_thread_message(
                "FAKE", "", "hello from device", {"peer_name": "Alex"}, 0.0,
                xml=_xml("thread_alex_hello.xml"),
            )
        )
        assert quiet.send_count == 0
        assert held_out["step"] == ("reply_mode_off" if mode == "off" else "reply_mode_approve")

    stop_device = FakeWaDevice(
        _xml("list_unread_alex.xml"),
        _xml("thread_alex_stop.xml"),
        start="thread",
    )
    stopper = WhatsAppRpaRunner(
        config_manager=None,
        skill_manager=_Skill(),
        wa_cfg={"account_id": _ACCOUNT, "reply_mode": "auto"},
        state_store=WaRpaStateStore(tmp_path / "stop.db"),
        device=stop_device,
        inbox_store=InboxStore(tmp_path / "stop-inbox.db"),
    )
    stop_out = asyncio.run(
        stopper._process_open_thread_message(
            "FAKE", "", "STOP", {"peer_name": "Alex"}, 0.0,
            xml=_xml("thread_alex_stop.xml"),
        )
    )
    assert stop_device.send_count == 0
    assert stop_out["step"] == "stop_contact"

    q_device = FakeWaDevice(_xml("list_unread_alex.xml"), _xml("thread_alex_hello.xml"))
    q_state = WaRpaStateStore(tmp_path / "q.db")
    q_runner = WhatsAppRpaRunner(
        config_manager=None,
        skill_manager=_Skill(),
        wa_cfg={"account_id": _ACCOUNT, "reply_mode": "auto"},
        state_store=q_state,
        device=q_device,
        inbox_store=InboxStore(tmp_path / "q-inbox.db"),
    )
    item_id = q_state.enqueue_send(cid, "Alex", "queued hi", client_msg_id="q-dry", conversation_id=cid)
    dry = asyncio.run(
        q_runner._handle_queued_send(
            {"id": item_id, "peer_name": "Alex", "text": "queued hi",
             "chat_key": cid, "client_msg_id": "q-dry"},
            0.0,
            dry_run=True,
        )
    )
    assert dry["send_status"] == "dry_run"
    assert q_device.send_count == 0
    assert q_state.get_send_queue_item(item_id)["status"] == "dry_run"


def test_screencap_path_calls_shared_gate():
    from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
    src = inspect.getsource(WhatsAppRpaRunner._run_screencap_fallback)
    assert "_confirm_open_thread" in src
    assert "_commit_auto_send" in src


def test_factory_enabled_stays_false_and_overlay_is_auto():
    root = Path(__file__).resolve().parents[1]
    example = yaml.safe_load((root / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    assert example["whatsapp_rpa"]["enabled"] is False
    overlay = yaml.safe_load(
        (root / "config" / "whatsapp_rpa_test_device.overlay.example.yaml").read_text(encoding="utf-8")
    )
    wa = overlay["whatsapp_rpa"]
    assert wa["enabled"] is True
    assert wa["reply_mode"] == "auto"
    assert wa["autostart"] is False
    assert wa["service"]["autostart"] is False
    assert wa["proactive"]["enabled"] is False
    assert wa["open_thread_check"] is False
    assert wa["open_thread_check_enabled"] is False
    assert wa["stop_gate_enabled"] is True
    assert wa["accounts"][0]["adb_serial"] == "REPLACE_WITH_DEVICE_SERIAL"
    assert wa["accounts"][0]["reply_mode"] == "auto"
