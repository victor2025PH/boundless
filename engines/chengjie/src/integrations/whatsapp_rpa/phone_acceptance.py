"""多台手机真发验收。只使用号码闭环，不启动 Telegram、关怀或旧 RPA。

adb 一律经 ssh 到远端执行。禁止 kill-server、start-server、connect。
读屏由调用方注入（验收脚本注入 PP-OCR）。发送前用输入框里的实际文字
和要发的中文逐字核对，只有占位提示不算通过。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.integrations.whatsapp_rpa.phone_identity import (
    normalize_e164,
    wa_me_url,
)
from src.integrations.whatsapp_rpa.phone_notify import parse_wa_notifications
from src.integrations.whatsapp_rpa.phone_send import (
    PhoneScreen,
    composer_matches,
    screen_is_rate_limited,
)

logger = logging.getLogger("wa_phone_acceptance")

_FORBIDDEN_ADB = frozenset({"kill-server", "start-server", "connect"})
TOKEN_ENV_NAMES = ("WA_DECIDE_TOKEN", "BOUNDLESS_BUS_TOKEN")


def ssh_adb_argv(host: str, serial: str, adb_args: Sequence[str]) -> List[str]:
    """拼出 ``ssh <host> adb -s <serial> ...``。本地不调用 adb。"""
    remote_host = str(host or "").strip()
    device = str(serial or "").strip()
    if not remote_host or not device:
        raise ValueError("adb ssh requires host and serial")
    tokens: List[str] = []
    for arg in adb_args:
        tokens.extend(str(arg).split())
    for token in tokens:
        if token in _FORBIDDEN_ADB:
            raise ValueError(f"forbidden adb command: {token}")
    remote = " ".join(
        ["adb", "-s", shlex.quote(device), *[shlex.quote(str(arg)) for arg in adb_args]]
    )
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", remote_host, remote]


def redact_text(text: str, secrets: Sequence[str]) -> str:
    out = str(text or "")
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), "***")
    return out


def safe_log_fields(fields: Dict[str, Any], secrets: Sequence[str]) -> Dict[str, Any]:
    """日志和报告里去掉 token、Authorization。"""
    hidden = {"authorization", "token", "bearer", "api_key", "access_token"}
    clean: Dict[str, Any] = {}
    for key, value in fields.items():
        if str(key).lower() in hidden:
            continue
        if isinstance(value, str):
            clean[key] = redact_text(value, secrets)
        else:
            clean[key] = value
    return clean


def compare_prefill(expected: str, composer: str) -> Dict[str, Any]:
    """核对预填正文。只有「输入消息」这类占位不算通过。"""
    observed = str(composer or "")
    match = bool(composer_matches(observed, expected))
    placeholder_only = _looks_placeholder(observed)
    return {
        "expected": expected,
        "observed": observed,
        "match": match and not placeholder_only,
        "placeholder_only": placeholder_only,
        "expected_chars": len(expected or ""),
        "observed_chars": len(observed),
    }


def _looks_placeholder(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return False
    from src.integrations.whatsapp_rpa.phone_send import _is_hint_line
    return all(_is_hint_line(line) for line in lines)


def png_size(png: bytes) -> tuple:
    if not png or len(png) < 24 or png[12:16] != b"IHDR":
        return (0, 0)
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    return (width, height)


# 输入栏只占屏幕最底部。再往上是聊天气泡，不能算进输入框。
_COMPOSER_TOP = 0.90


def lines_to_screen(lines: Sequence[Dict[str, Any]], width: int, height: int) -> PhoneScreen:
    """按 PP-OCR 的像素框把文字分成顶栏、输入框、对方气泡、己方气泡。

    对方在左侧，己方在右侧，看文字框靠哪一边，不看中心点是否过半。
    输入框只取底部输入栏。时间、日期、勾号和加密提示不进气泡。
    """
    from src.integrations.whatsapp_rpa.phone_send import is_screen_chrome

    header: List[str] = []
    composer: List[str] = []
    incoming: List[str] = []
    outgoing: List[str] = []
    notes: List[str] = []
    if width <= 0 or height <= 0:
        return PhoneScreen()
    for line in lines or []:
        text = str(line.get("text") or "").strip()
        box = line.get("box") or []
        if not text or len(box) < 4:
            continue
        cy = (float(box[1]) + float(box[3])) / 2.0
        if cy < height * 0.16:
            header.append(text)
            continue
        if cy > height * _COMPOSER_TOP:
            if is_screen_chrome(text):
                notes.append(text)
            else:
                composer.append(text)
            continue
        if is_screen_chrome(text):
            notes.append(text)
            continue
        left = float(box[0])
        right_gap = float(width) - float(box[2])
        if right_gap < left:
            outgoing.append(text)
        elif left < right_gap:
            incoming.append(text)
        else:
            notes.append(text)
    return PhoneScreen(
        header=" ".join(header),
        composer="\n".join(composer),
        outgoing=outgoing,
        incoming=incoming,
        last_incoming=incoming[-1] if incoming else "",
        notes=notes,
    )


def new_left_bubbles(incoming: Sequence[str], baseline: Sequence[str]) -> List[str]:
    """只留这次发送之后新出现的左侧气泡。基线里已有的旧消息不算。"""
    from src.integrations.whatsapp_rpa.phone_send import is_screen_chrome

    pending = [str(item or "").strip() for item in baseline or [] if str(item or "").strip()]
    fresh: List[str] = []
    for raw in incoming or []:
        text = str(raw or "").strip()
        if not text or is_screen_chrome(text):
            continue
        if text in pending:
            pending.remove(text)
            continue
        fresh.append(text)
    return fresh


def make_ppocr_reader(base_url: str, post_json: Optional[Callable[..., Dict[str, Any]]] = None,
                      timeout: float = 20.0) -> Callable[[bytes], PhoneScreen]:
    """``POST {ocr-url}/v1/ocr``，body ``{"image_b64": ...}``。"""
    url = str(base_url or "").rstrip("/") + "/v1/ocr"

    def read(png: bytes) -> PhoneScreen:
        if not png:
            return PhoneScreen()
        import base64
        payload = {"image_b64": base64.b64encode(png).decode("ascii")}
        if post_json is not None:
            data = post_json(url, payload)
        else:
            data = _post_json(url, payload, timeout=timeout, headers=None)
        lines = list((data or {}).get("lines") or [])
        width, height = png_size(png)
        return lines_to_screen(lines, width, height)

    return read


def decide_request_body(*, account_id: str, phone: str, last_inbound: str,
                        persona_id: str = "") -> Dict[str, Any]:
    from src.integrations.whatsapp_rpa.phone_identity import conversation_id
    hint: Dict[str, Any] = {}
    if persona_id:
        hint["persona"] = persona_id
    return {
        "message": {
            "platform": "whatsapp",
            "account": account_id,
            "external_id": normalize_e164(phone),
            "text": last_inbound,
            "session_id": conversation_id(account_id, phone),
            "context_hint": hint,
        }
    }


def make_http_decide(url: str, token: str = "",
                     post_json: Optional[Callable[..., Dict[str, Any]]] = None):
    """向已经在跑的 decide 接口要草稿。token 只放请求头，不进日志。"""

    async def decide(*, account_id: str, phone: str, last_inbound: str, persona_id: str = ""):
        body = decide_request_body(
            account_id=account_id, phone=phone, last_inbound=last_inbound,
            persona_id=persona_id,
        )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        sender = post_json or _post_json
        data = sender(url, body, timeout=60.0, headers=headers)
        logger.info("%s", json.dumps(safe_log_fields(
            {"event": "decide", "url": url, "action": (data or {}).get("action")},
            [token],
        ), ensure_ascii=False))
        reply = str((data or {}).get("text") or (data or {}).get("reply") or "").strip()
        action = str((data or {}).get("action") or "")
        return {"ok": action == "draft" and bool(reply), "reply": reply, "action": action}

    return decide


def _post_json(url: str, body: Dict[str, Any], timeout: float = 20.0,
               headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    import urllib.request
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class DeviceBusy(RuntimeError):
    def __init__(self, serial: str) -> None:
        self.serial = serial
        super().__init__(f"设备 {serial} 正在被另一个验收占用")


class DeviceLock:
    """同一台手机的独占锁。锁文件按序列号放在 data-dir，进程退出或 release 后放开。"""

    def __init__(self, directory: Path, serial: str) -> None:
        self.serial = str(serial or "").strip()
        safe = "".join(ch if ch.isalnum() else "_" for ch in self.serial) or "device"
        self.path = Path(directory) / f".adb-{safe}.lock"
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        if not self.serial:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os_open_exclusive(self.path)
            except FileExistsError:
                if not lock_is_stale(self.path):
                    raise DeviceBusy(self.serial)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            os.write(fd, f"{os.getpid()}\n".encode())
            self._fd = fd
            return
        raise DeviceBusy(self.serial)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            if self.path.exists():
                self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "DeviceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def os_open_exclusive(path: Path) -> int:
    return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)


def lock_is_stale(path: Path, *, max_age_sec: float = 7200.0) -> bool:
    try:
        text = path.read_text(encoding="utf-8").strip().splitlines()
        pid = int(text[0]) if text else 0
    except (OSError, ValueError):
        return True
    if not _pid_alive(pid):
        return True
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age > max_age_sec


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def lock_serials(directory: Path, serials: Sequence[str]):
    """按序列号排序加锁，避免两台手机交叉抢锁。中途失败时放开已经拿到的锁。"""
    locks = [DeviceLock(directory, serial) for serial in sorted({s for s in serials if str(s).strip()})]
    held: List[DeviceLock] = []
    try:
        for lock in locks:
            lock.acquire()
            held.append(lock)
    except DeviceBusy:
        for lock in held:
            lock.release()
        raise
    return held


class EvidenceLog:
    def __init__(self, directory: Path, secrets: Optional[Sequence[str]] = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.secrets = [s for s in (secrets or []) if s]
        self.steps: List[Dict[str, Any]] = []

    def save(self, name: str, png: bytes, screen: PhoneScreen, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        index = len(self.steps) + 1
        stem = f"{index:03d}-{name}"
        png_path = self.dir / f"{stem}.png"
        ocr_path = self.dir / f"{stem}.ocr.json"
        png_path.write_bytes(png or b"")
        ocr = {
            "header": screen.header,
            "composer": screen.composer,
            "outgoing": list(screen.outgoing or []),
            "last_incoming": screen.last_incoming,
            "notes": list(getattr(screen, "notes", []) or []),
        }
        if extra:
            ocr.update(safe_log_fields(extra, self.secrets))
        ocr_path.write_text(json.dumps(ocr, ensure_ascii=False, indent=2), encoding="utf-8")
        step = {"name": name, "png": str(png_path), "ocr": str(ocr_path), **ocr}
        self.steps.append(step)
        logger.info("%s", json.dumps(safe_log_fields(
            {"event": "evidence", "name": name, "header": screen.header, "composer": screen.composer},
            self.secrets,
        ), ensure_ascii=False))
        return step

    def finish(self, report: Dict[str, Any]) -> Dict[str, Any]:
        report = dict(report)
        report["steps"] = self.steps
        clean = json.loads(redact_text(json.dumps(report, ensure_ascii=False), self.secrets))
        path = self.dir / "report.json"
        path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        clean["report_path"] = str(path)
        return clean


class RecordingDevice:
    """把每一次读屏的截图和 OCR 落盘，再交给原来的设备。"""

    def __init__(self, inner: Any, evidence: EvidenceLog, label: str) -> None:
        self.inner = inner
        self.evidence = evidence
        self.label = label
        self._reads = 0

    async def open_url(self, url: str) -> None:
        await self.inner.open_url(url)

    async def screencap(self) -> bytes:
        data = await self.inner.screencap()
        return data or b""

    async def tap_send(self) -> None:
        await self.inner.tap_send()

    async def press_back(self) -> None:
        await self.inner.press_back()

    async def poll_notifications(self):
        poll = getattr(self.inner, "poll_notifications", None)
        if poll is None:
            return []
        return await poll()

    async def read_screen(self, png: bytes) -> PhoneScreen:
        self._reads += 1
        screen = await self.inner.read_screen(png)
        self.evidence.save(f"{self.label}-read{self._reads}", png, screen)
        return screen

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def subprocess_runner(argv: Sequence[str]) -> tuple:
    import subprocess
    proc = subprocess.run(list(argv), capture_output=True, timeout=40)
    return proc.returncode, proc.stdout or b"", (proc.stderr or b"").decode("utf-8", "replace")


class SshAdbPhoneDevice:
    """通过 ssh 在远端执行 adb。测试注入 runner，不会真的连上主机。"""

    def __init__(self, host: str, serial: str, read_screen_fn: Callable[[bytes], Any],
                 runner: Optional[Callable[[Sequence[str]], tuple]] = None) -> None:
        self.host = host
        self.serial = serial
        self.read_screen_fn = read_screen_fn
        self.runner = runner or subprocess_runner

    def _argv(self, adb_args: Sequence[str]) -> List[str]:
        return ssh_adb_argv(self.host, self.serial, adb_args)

    async def _run(self, adb_args: Sequence[str]) -> tuple:
        import asyncio
        return await asyncio.to_thread(self.runner, self._argv(adb_args))

    async def open_url(self, url: str) -> None:
        from src.integrations.whatsapp_rpa.phone_identity import view_intent_args
        code, _out, err = await self._run(view_intent_args(url))
        if code != 0:
            raise RuntimeError(redact_text(err or "open_url failed", []))

    async def screencap(self) -> bytes:
        code, out, err = await self._run(["exec-out", "screencap", "-p"])
        if code != 0 or not out:
            raise RuntimeError(redact_text(err or "screencap failed", []))
        return out

    async def tap_send(self) -> None:
        code, out, err = await self._run(["shell", "wm", "size"])
        text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        width, height = _parse_wm_size(text)
        x, y = int(width * 0.94), int(height * 0.956)
        code, _out, err = await self._run(["shell", "input", "tap", str(x), str(y)])
        if code != 0:
            raise RuntimeError(redact_text(err or "tap failed", []))

    async def tap_header(self) -> None:
        code, out, err = await self._run(["shell", "wm", "size"])
        text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        width, height = _parse_wm_size(text)
        x, y = int(width * 0.32), int(height * 0.09)
        code, _out, err = await self._run(["shell", "input", "tap", str(x), str(y)])
        if code != 0:
            raise RuntimeError(redact_text(err or "header tap failed", []))

    async def press_back(self) -> None:
        await self._run(["shell", "input", "keyevent", "KEYCODE_BACK"])

    async def poll_notifications(self) -> List[Dict[str, str]]:
        code, out, _err = await self._run(["shell", "dumpsys", "notification", "--noredact"])
        if code != 0:
            return []
        text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        return parse_wa_notifications(text)

    async def read_screen(self, png: bytes) -> PhoneScreen:
        screen = self.read_screen_fn(png)
        if hasattr(screen, "__await__"):
            screen = await screen
        return screen if isinstance(screen, PhoneScreen) else PhoneScreen()


def _parse_wm_size(text: str) -> tuple:
    import re
    match = re.search(r"(\d+)x(\d+)", text or "")
    if not match:
        return (720, 1600)
    return (int(match.group(1)), int(match.group(2)))


async def open_peer_chat(device: Any, phone: str) -> str:
    """用 wa.me/{号码} 打开对端，保证不是点到列表第一行。"""
    url = wa_me_url(phone)
    if not url:
        raise ValueError("bad phone")
    await device.open_url(url)
    return url


async def preview_prefill(device: Any, phone: str, text: str, evidence: EvidenceLog) -> Dict[str, Any]:
    """打开预填链接，等到聊天页再核对中文，不点发送。"""
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat

    url = wa_me_url(phone, text)
    await device.open_url(url)
    ready = await wait_for_chat(device, phone, expected=text)
    compared = compare_prefill(text, str(ready.get("composer") or ""))
    confirmed = bool(ready.get("profile_confirmed"))
    if ready.get("ready") and compared["match"]:
        return {
            "status": "dry_run", "reason": "", "tapped": False, "url": url,
            "polls": ready.get("polls"), "profile_confirmed": confirmed, **compared,
        }
    reason = str(ready.get("reason") or "chat_not_ready")
    status = "throttled" if reason == "wa_rate_limit" else "failed"
    return {
        "status": status, "reason": reason, "tapped": False, "url": url,
        "polls": ready.get("polls"), "profile_confirmed": confirmed, **compared,
    }


async def snapshot_incoming(device: Any, phone: str) -> List[str]:
    """打开会话，记下已经在左侧的旧气泡。打不开就返回空，调用方不得把旧消息当新消息。"""
    try:
        await open_peer_chat(device, phone)
    except Exception:
        return []
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat
    ready = await wait_for_chat(device, phone, expected="")
    if not ready.get("ready"):
        return []
    screen = await device.read_screen(await device.screencap())
    return new_left_bubbles(getattr(screen, "incoming", None) or [], [])


async def read_peer_text(device: Any, peer_phone: str, evidence: EvidenceLog,
                         baseline: Sequence[str] = ()) -> Dict[str, Any]:
    """先看通知。没有就打开 wa.me，只认发送之后新出现的左侧气泡。"""
    notes = []
    poll = getattr(device, "poll_notifications", None)
    if poll is not None:
        notes = await poll() or []
    wanted = normalize_e164(peer_phone)
    noted = ""
    for note in notes:
        candidate = normalize_e164(str(note.get("phone") or "")) or normalize_e164(str(note.get("sender") or ""))
        if candidate == wanted:
            noted = str(note.get("text") or "").strip()
            break
    await open_peer_chat(device, wanted)
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat
    ready = await wait_for_chat(device, wanted, expected="")
    if not ready.get("ready"):
        return {
            "ok": False, "reason": ready.get("reason") or "header_mismatch",
            "text": "", "header": ready.get("header") or "",
        }
    text = noted or ""
    if not text:
        png = await device.screencap()
        screen = await device.read_screen(png)
        fresh = new_left_bubbles(getattr(screen, "incoming", None) or [], baseline)
        text = fresh[-1] if fresh else ""
    if not text:
        return {"ok": False, "reason": "empty_text", "text": "", "header": ready.get("header") or ""}
    return {
        "ok": True, "text": text, "header": ready.get("header") or "",
        "source": "notification" if noted else "screenshot",
    }


def cold_texts(texts: Sequence[str], cap: int) -> List[str]:
    out = [str(item).strip() for item in texts if str(item).strip()]
    if not out:
        out = ["验收冷启动一"]
    while len(out) <= int(cap):
        out.append(f"验收冷启动{len(out) + 1}")
    return out


def report_ok(steps: Sequence[Dict[str, Any]]) -> bool:
    if not steps:
        return False
    bad = {"failed", "header_mismatch", "empty_text"}
    for step in steps:
        status = str(step.get("status") or "")
        if step.get("ok") is False or status in bad:
            return False
    return True


async def run_dry(loop: Any, device: Any, phone: str, text: str, evidence: EvidenceLog) -> Dict[str, Any]:
    wrapped = RecordingDevice(device, evidence, "dry")
    result = await preview_prefill(wrapped, phone, text, evidence)
    result["command"] = "dry-run"
    result["ok"] = result.get("status") == "dry_run" and result.get("tapped") is False and result.get("match") is True
    return evidence.finish(result)


def variant_phone(base: str, index: int) -> str:
    """同一号段上的不同号码。用来验收条数上限，而不是「一个号码只能发一次」。"""
    e164 = normalize_e164(base)
    digits = e164[1:]
    suffix = f"{index % 100:02d}"
    return normalize_e164("+" + digits[:-2] + suffix)


async def run_first_touch_cap(loop: Any, phone: str, texts: Sequence[str], cap: int,
                              evidence: EvidenceLog) -> Dict[str, Any]:
    from src.integrations.whatsapp_rpa.phone_loop import client_msg_id
    loop.cfg["hourly_cap"] = int(cap)
    loop.cfg["daily_cap"] = max(int(cap), int(loop.cfg.get("daily_cap") or cap))
    loop.cfg["first_touch_hourly_cap"] = int(cap)
    loop.cfg["first_touch_daily_cap"] = max(int(cap), int(loop.cfg.get("daily_cap") or cap))
    sent = []
    for index, text in enumerate(cold_texts(texts, cap)):
        target = variant_phone(phone, index)
        item = await loop.send_prepared(
            phone=target, text=text, source="first_touch",
            client_msg_id=client_msg_id("first_touch", target, text, f"cap-{index}"),
        )
        item["phone"] = target
        sent.append(item)
        if item.get("status") == "capped":
            break
    ok = (
        len(sent) == int(cap) + 1
        and all(row.get("status") == "sent" for row in sent[:-1])
        and sent[-1].get("status") == "capped"
        and sent[-1].get("tapped") is not True
    )
    return evidence.finish({
        "command": "first-touch-cap", "ok": ok, "cap": int(cap), "results": sent,
    })


async def run_stop_test(loop: Any, phone: str, text: str, evidence: EvidenceLog) -> Dict[str, Any]:
    from src.integrations.whatsapp_rpa.phone_loop import client_msg_id
    await open_peer_chat(loop.device, phone)
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat
    ready = await wait_for_chat(loop.device, phone, expected="")
    if not ready.get("ready"):
        return evidence.finish({
            "command": "stop-test", "ok": False, "status": ready.get("reason") or "header_mismatch",
            "header": ready.get("header") or "",
        })
    inbound = await loop.handle_inbound(phone=phone, text=text, display_name=phone)
    follow = await loop.send_prepared(
        phone=phone, text="还在吗", source="reply",
        client_msg_id=client_msg_id("reply", normalize_e164(phone), "还在吗", "stop-follow"),
    )
    ok = bool(inbound.get("stopped")) and follow.get("status") == "stopped" and follow.get("tapped") is not True
    return evidence.finish({
        "command": "stop-test", "ok": ok, "inbound": inbound, "follow": follow,
    })


async def run_convert_pending(loop: Any, phone: str, text: str, evidence: EvidenceLog,
                              confirm: bool = False) -> Dict[str, Any]:
    loop.cfg["reply_mode"] = "off"
    await open_peer_chat(loop.device, phone)
    from src.integrations.whatsapp_rpa.phone_send import wait_for_chat
    ready = await wait_for_chat(loop.device, phone, expected="")
    if not ready.get("ready"):
        return evidence.finish({
            "command": "convert-pending", "ok": False,
            "status": ready.get("reason") or "header_mismatch",
            "header": ready.get("header") or "",
        })
    inbound = await loop.handle_inbound(phone=phone, text=text, display_name=phone)
    deal = None
    if confirm:
        deal = loop.mark_deal(phone, source="manual", note="验收确认", by="acceptance")
    deals = 0
    cid = inbound.get("conversation_id") or ""
    if cid and hasattr(loop.inbox, "count_active_deals"):
        deals = int(loop.inbox.count_active_deals(cid))
    pending = (inbound.get("conversion") or {}).get("status") == "pending_confirm"
    ok = pending and deals == (1 if confirm else 0)
    return evidence.finish({
        "command": "convert-pending", "ok": ok, "inbound": inbound,
        "confirmed": deal, "active_deals": deals,
    })


async def run_rate_limit_check(device: Any, evidence: EvidenceLog) -> Dict[str, Any]:
    wrapped = RecordingDevice(device, evidence, "rate")
    png = await wrapped.screencap()
    screen = await wrapped.read_screen(png)
    limited = screen_is_rate_limited(screen)
    return evidence.finish({
        "command": "rate-limit-check",
        "ok": True,
        "status": "throttled" if limited else "clear",
        "tapped": False,
        "header": screen.header,
        "composer": screen.composer,
    })


async def run_pair_chat(loop_a: Any, loop_b: Any, phone_a: str, phone_b: str,
                        turns: int, seed: str, evidence: EvidenceLog) -> Dict[str, Any]:
    """A、B 交替。第 1 句由 A 发给 B，之后每人先收对方的信再回复。

    第一句没发出去就停，不再往下跑。收信只认发送前画面里没有的新左侧气泡。
    """
    seats = ((loop_a, phone_a, phone_b, "A"), (loop_b, phone_b, phone_a, "B"))
    loops = {"A": loop_a, "B": loop_b}
    results = []
    baselines: Dict[str, List[str]] = {"A": [], "B": []}
    for index in range(int(turns)):
        loop, own, peer, label = seats[index % 2]
        other = "B" if label == "A" else "A"
        if index == 0:
            from src.integrations.whatsapp_rpa.phone_loop import client_msg_id
            baselines[other] = await snapshot_incoming(loops[other].device, own)
            item = await loop.send_prepared(
                phone=peer, text=seed, source="acceptance",
                client_msg_id=client_msg_id("acceptance", normalize_e164(peer), seed, "seed"),
                display_name=peer,
            )
            item["turn"] = index
            item["seat"] = label
            results.append(item)
            if item.get("status") != "sent":
                break
            continue
        heard = await read_peer_text(
            loop.device, peer, evidence, baseline=baselines.get(label, []),
        )
        if not heard.get("ok"):
            heard["turn"] = index
            heard["seat"] = label
            heard["status"] = heard.get("reason") or "failed"
            results.append(heard)
            break
        baselines[label] = list(baselines.get(label, [])) + [str(heard.get("text") or "")]
        baselines[other] = await snapshot_incoming(loops[other].device, own)
        item = await loop.handle_inbound(
            phone=peer, text=heard["text"], display_name=peer,
        )
        send = item.get("send") or {}
        results.append({
            "turn": index, "seat": label, "heard": heard["text"],
            "source": heard.get("source"), "status": send.get("status") or item.get("reply"),
            "send": send, "ok": send.get("status") == "sent",
        })
    ok = len(results) == int(turns) and all(row.get("status") == "sent" for row in results)
    return evidence.finish({"command": "pair-chat", "ok": ok, "turns": results})
