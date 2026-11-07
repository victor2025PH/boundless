"""真机冒烟脚本的护栏。不连接设备，不调用系统 adb。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wa_rpa_live_smoke.py"
_FIX = Path(__file__).resolve().parent / "fixtures" / "wa_rpa"


def _load():
    spec = importlib.util.spec_from_file_location("wa_rpa_live_smoke", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakePort:
    def __init__(self, dumps):
        self.dumps = list(dumps)
        self.calls = []
        self.taps = []
        self.texts = []

    def run(self, args, timeout=30):
        mod = _load()
        mod.assert_adb_args(args)
        self.calls.append(list(args))
        if list(args) == ["get-state"]:
            return 0, "device\n", ""
        return 0, "", ""

    def dump_hierarchy(self):
        self.calls.append(["dump_hierarchy"])
        if not self.dumps:
            return b""
        return self.dumps.pop(0)

    def tap(self, x, y):
        self.taps.append((int(x), int(y)))

    def input_text(self, text):
        self.texts.append(text)
        return True

    def back(self):
        self.calls.append(["back"])


def _ns(mod, **overrides):
    base = dict(
        serial="SERIAL-PLACEHOLDER",
        to="+63 917 555 0101",
        text="auto reply",
        max_sends=1,
        confirm=False,
        account="wa_test",
    )
    base.update(overrides)
    return argparse_ns(base)


def argparse_ns(data):
    import argparse
    return argparse.Namespace(**data)


def test_missing_confirm_is_read_only_and_refuses_server_commands():
    mod = _load()
    with pytest.raises(RuntimeError):
        mod.assert_adb_args(["kill-server"])
    with pytest.raises(RuntimeError):
        mod.assert_adb_args(["start-server"])
    device = mod.RealAdbDevice("SERIAL-PLACEHOLDER")
    with pytest.raises(RuntimeError):
        device.run(["kill-server"])
    with pytest.raises(RuntimeError):
        device.run(["start-server"])

    port = FakePort([])
    report = mod.execute(_ns(mod, confirm=False), port)
    assert report["result"] == "read_only"
    assert report["sends"] == 0
    assert report["send_status"] == "not_sent"
    assert report["conversation_id"] == "whatsapp:wa_test:639175550101"
    assert report["client_msg_id"].startswith("live-")
    assert port.taps == []
    assert port.texts == []
    assert port.calls == [["get-state"]]
    flat = " ".join(" ".join(c) if isinstance(c, list) else str(c) for c in port.calls)
    assert "kill-server" not in flat
    assert "start-server" not in flat


def test_confirm_sends_once_to_the_numbered_row():
    mod = _load()
    list_xml = (_FIX / "list_unread_alex.xml").read_bytes()
    thread = (_FIX / "thread_alex_hello.xml").read_bytes()
    after = (_FIX / "thread_alex_hello_sent.xml").read_bytes()
    port = FakePort([list_xml, thread, after])
    report = mod.execute(
        _ns(mod, confirm=True, text="auto reply", max_sends=1),
        port,
    )
    assert report["result"] == "sent"
    assert report["send_status"] == "sent"
    assert report["receipt"] == "sent"
    assert report["sends"] == 1
    assert report["conversation_id"] == "whatsapp:wa_test:639175550101"
    assert report["client_msg_id"].startswith("live-")
    assert report["evidence"]["bubble"] is True
    assert port.texts == ["auto reply"]
    assert len(port.taps) == 2
    joined = []
    for call in port.calls:
        joined.extend(call if isinstance(call, list) else [call])
    assert "kill-server" not in joined
    assert "start-server" not in joined


def test_confirm_without_budget_does_not_tap():
    mod = _load()
    port = FakePort([])
    report = mod.execute(_ns(mod, confirm=True, max_sends=0), port)
    assert report["result"] == "refused"
    assert report["sends"] == 0
    assert port.taps == []
    assert port.texts == []


def test_parse_requires_explicit_flags():
    mod = _load()
    with pytest.raises(SystemExit):
        mod.parse_args(["--serial", "S", "--to", "1", "--text", "hi"])
    ns = mod.parse_args([
        "--serial", "S", "--to", "+1 202 555 0101", "--text", "hi",
        "--max-sends", "1",
    ])
    assert ns.confirm is False
    assert ns.max_sends == 1
