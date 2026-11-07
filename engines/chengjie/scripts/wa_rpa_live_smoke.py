"""WhatsApp 真机单账号冒烟。

只给机房电脑用。云端没有手机，不要在这里执行。

不带 ``--confirm`` 只做只读检查（adb get-state），不点、不输入、不发送。
带上 ``--confirm`` 也只给 ``--to`` 这一个号码发 ``--text``，次数不超过 ``--max-sends``。

本脚本不会执行 ``adb kill-server`` 或 ``adb start-server``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from typing import Optional, Sequence, Tuple

from src.integrations.whatsapp_rpa.min_loop import (
    classify_send_evidence,
    resolve_identity,
    scan_list_rows,
    select_chat_row,
    thread_confirms_target,
)
from src.integrations.whatsapp_rpa.ui_hierarchy import find_send_button

_FORBIDDEN = frozenset({"kill-server", "start-server"})
_DUMP_REMOTE = "/sdcard/wa_rpa_live_smoke.xml"
_WA_ACTIVITY = "com.whatsapp/.Main"


def assert_adb_args(args: Sequence[str]) -> None:
    for arg in args:
        if arg in _FORBIDDEN:
            raise RuntimeError(f"refusing adb {arg}")


def build_plan(account: str, to: str, text: str) -> dict:
    ident = resolve_identity(account, display_name=to, phone=to)
    digest = hashlib.sha256(f"{ident.conversation_id}|{text}".encode("utf-8")).hexdigest()[:16]
    return {
        "conversation_id": ident.conversation_id,
        "client_msg_id": f"live-{digest}",
        "phone": ident.phone,
        "key_source": ident.key_source,
        "phone_unresolved": ident.phone_unresolved,
    }


class RealAdbDevice:
    """真机 adb。任何 kill-server / start-server 在拉起子进程之前拒绝。"""

    def __init__(self, serial: str) -> None:
        self.serial = serial

    def run(self, args: Sequence[str], timeout: float = 30.0) -> Tuple[int, str, str]:
        assert_adb_args(args)
        cmd = ["adb", "-s", self.serial, *list(args)]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def dump_hierarchy(self) -> bytes:
        self.run(["shell", "rm", "-f", _DUMP_REMOTE], timeout=8)
        self.run(["shell", "uiautomator", "dump", _DUMP_REMOTE], timeout=20)
        code, out, _err = self.run(["shell", "cat", _DUMP_REMOTE], timeout=15)
        if code != 0 or not out:
            return b""
        text = out
        start = text.find("<?xml")
        if start < 0:
            start = text.find("<hierarchy")
        if start > 0:
            text = text[start:]
        return text.encode("utf-8")

    def tap(self, x: int, y: int) -> None:
        self.run(["shell", "input", "tap", str(int(x)), str(int(y))], timeout=8)

    def input_text(self, text: str) -> bool:
        payload = text.replace(" ", "%s")
        if payload.isascii():
            code, _out, _err = self.run(["shell", "input", "text", payload], timeout=15)
            return code == 0
        import base64
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        code, _out, _err = self.run(
            ["shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", encoded],
            timeout=15,
        )
        return code == 0

    def back(self) -> None:
        self.run(["shell", "input", "keyevent", "KEYCODE_BACK"], timeout=8)


def execute(ns: argparse.Namespace, device) -> dict:
    plan = build_plan(ns.account, ns.to, ns.text)
    report = {
        **plan,
        "serial": ns.serial,
        "to": ns.to,
        "text": ns.text,
        "max_sends": int(ns.max_sends),
        "confirm": bool(ns.confirm),
        "sends": 0,
        "result": "",
        "send_status": "not_sent",
        "receipt": "",
        "evidence": {},
    }
    if not ns.confirm:
        code, out, err = device.run(["get-state"])
        report["result"] = "read_only"
        report["evidence"] = {
            "adb_state": (out or err or "").strip(),
            "adb_rc": code,
            "sent": False,
        }
        return report
    if int(ns.max_sends) < 1:
        report["result"] = "refused"
        report["evidence"] = {"reason": "max_sends", "sent": False}
        return report

    code, out, err = device.run(["get-state"])
    state = (out or "").strip()
    if code != 0 or state != "device":
        report["result"] = "device_not_ready"
        report["evidence"] = {"adb_state": state or (err or "").strip(), "adb_rc": code, "sent": False}
        return report

    device.run(
        ["shell", "am", "start", "-n", _WA_ACTIVITY],
        timeout=15,
    )
    list_xml = device.dump_hierarchy()
    ident = resolve_identity(ns.account, display_name=ns.to, phone=ns.to)
    row, why = select_chat_row(scan_list_rows(list_xml), ident)
    if row is None:
        report["result"] = why or "not_found"
        report["evidence"] = {"reason": why, "sent": False}
        return report
    if report["sends"] >= int(ns.max_sends):
        report["result"] = "refused"
        report["evidence"] = {"reason": "max_sends", "sent": False}
        return report

    device.tap(row.cx, row.cy)
    thread = device.dump_hierarchy()
    confirmed = thread_confirms_target(thread, display_name=ns.to, phone=ident.phone)
    if not confirmed.get("ok"):
        device.back()
        report["result"] = confirmed.get("reason") or "unverified_thread"
        report["evidence"] = {**confirmed, "sent": False}
        return report
    send_xy = find_send_button(thread)
    if send_xy is None:
        device.back()
        report["result"] = "send_button_missing"
        report["evidence"] = {"sent": False}
        return report
    typed = device.input_text(ns.text)
    if not typed:
        device.back()
        report["result"] = "input_failed"
        report["evidence"] = {"sent": False}
        return report
    device.tap(*send_xy)
    report["sends"] = 1
    after = device.dump_hierarchy()
    evidence = classify_send_evidence(after, ns.text)
    report["evidence"] = {
        "bubble": evidence["bubble"],
        "screen_width": evidence["screen_width"],
        "title": confirmed.get("title") or "",
        "phone": confirmed.get("phone") or "",
        "sent": evidence["status"] == "sent",
    }
    report["receipt"] = evidence["receipt"]
    if evidence["status"] == "sent":
        report["result"] = "sent"
        report["send_status"] = "sent"
    else:
        report["result"] = "send_unconfirmed"
        report["send_status"] = "send_unconfirmed"
    device.back()
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WhatsApp 真机单号冒烟（默认只读）")
    parser.add_argument("--serial", required=True, help="adb 序列号")
    parser.add_argument("--to", required=True, help="单个对方号码")
    parser.add_argument("--text", required=True, help="要发送的文本")
    parser.add_argument("--max-sends", required=True, type=int, help="本次最多发送条数")
    parser.add_argument("--confirm", action="store_true", help="没有它就只做只读检查")
    parser.add_argument("--account", default="default", help="收件箱账号 id，默认 default")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None, device=None) -> int:
    ns = parse_args(argv)
    if device is None:
        device = RealAdbDevice(ns.serial)
    report = execute(ns, device)
    print(json.dumps(report, ensure_ascii=False))
    if report["result"] in {"read_only", "sent"}:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
