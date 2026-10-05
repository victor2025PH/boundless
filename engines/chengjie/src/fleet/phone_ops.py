"""节点执行远程手机操作（agent 0.3.7）：phone_screenshot / phone_tap / phone_swipe / phone_text / phone_key。

安全栏：
* adb 只跑白名单参数（``check_adb_args``）：``devices -l``、``version``、``-s <serial> exec-out screencap``、
  ``-s <serial> shell input tap|swipe|text|keyevent 3|4``。参数列表、不经本机 shell、每条都有超时。
* 从不启动 / 停止 / 重启 adb server，也不改端口和连接模式：先按清点同一套办法用 ``host:version``
  问现有 server；没有 server、或本机 adb 客户端版本和 server 不一致（会触发 server 重启）→ 拒绝。
* 目标必须出现在当前 ``adb devices -l`` 里且 state=device。受保护手机（3B1F… / 192.168.0.148）硬拒；
  agent.json 的 ``phones_exclude``（serial / 前缀* / model:）同样拒；无线（tcp）手机默认拒，
  ``phone_ops_allow_tcp: true`` 才放行。
* 每台手机一把锁，两次操作至少间隔 0.5 s；全节点最多 2 个操作同时进行。
* 截图取原始帧（``exec-out screencap``，不带 -p），在本机按整数倍抽样缩到长边 ≤ 1024、编成 PNG；
  base64 后超过结果上限就继续缩。不依赖 Pillow。
* agent.json ``phone_ops_enabled: false`` → 心跳不声明 phone_ops_v1，任何操作都拒。
"""

from __future__ import annotations

import base64
import math
import struct
import subprocess
import sys
import threading
import time
import zlib
from array import array
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .phone_rules import (
    KEYCODES, MAX_PNG_B64, PhoneOpError, TEXT_ALLOWED, check_target, escape_input_text, valid_serial, validate_payload,
)
from .phones import (
    adb_server_port, adb_server_version, find_adb, is_excluded, is_protected, normalize_excludes, parse_adb_devices,
    parse_client_version,
)
from .protocol import (
    CAP_PHONE_OPS_V1, PHONE_TASK_KINDS, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, TASK_PHONE_KEY,
    TASK_PHONE_SCREENSHOT, TASK_PHONE_SWIPE, TASK_PHONE_TAP, TASK_PHONE_TEXT,
)

SCREENCAP_TIMEOUT_SEC = 15
INPUT_TIMEOUT_SEC = 10
LIST_TIMEOUT_SEC = 5
MIN_INTERVAL_SEC = 0.5
MAX_CONCURRENT = 2
LOCK_WAIT_SEC = 10
SCREENSHOT_MAX_SIDE = 1024
MAX_SCALE = 16
MAX_RAW_BYTES = 80 * 1024 * 1024
_RAW_FORMATS = {1: "rgba", 2: "rgba", 5: "bgra"}     # PixelFormat RGBA_8888 / RGBX_8888 / BGRA_8888
_PX = "I" if array("I").itemsize == 4 else "L"

RunFn = Callable[..., Any]


def _digits(v: str, hi: int = 99999) -> bool:
    return v.isdigit() and len(v) <= 5 and int(v) <= hi


def check_adb_args(args: Sequence[str]) -> None:
    """adb 参数白名单。不在白名单 → PhoneOpError('adb_args_not_allowed')。"""
    a = tuple(str(x) for x in args)
    if a in (("devices", "-l"), ("version",)):
        return
    if len(a) >= 4 and a[0] == "-s" and valid_serial(a[1]) == a[1] and not is_protected(a[1]):
        rest = a[2:]
        if rest == ("exec-out", "screencap"):
            return
        if rest[:2] == ("shell", "input") and len(rest) >= 4:
            op, vals = rest[2], rest[3:]
            if op == "tap" and len(vals) == 2 and all(_digits(v) for v in vals):
                return
            if op == "swipe" and len(vals) == 5 and all(_digits(v) for v in vals):
                return
            if op == "keyevent" and len(vals) == 1 and vals[0] in KEYCODES.values():
                return
            if op == "text" and len(vals) == 1:
                plain = vals[0].replace("%s", " ")
                if "%" not in plain and TEXT_ALLOWED.match(plain) and not plain.startswith("-"):
                    return
    raise PhoneOpError("adb_args_not_allowed")


def _run_kwargs(timeout: float) -> Dict[str, Any]:
    kw: Dict[str, Any] = {"capture_output": True, "timeout": timeout, "shell": False, "stdin": subprocess.DEVNULL}
    if sys.platform.startswith("win"):
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kw


# ── 截图：原始帧 → 抽样缩小 → PNG ─────────────────────────────────────────────
def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _encode_png(raw: bytes, hdr: int, w: int, h: int, order: str, scale: int) -> Tuple[bytes, int, int]:
    ow, oh = (w + scale - 1) // scale, (h + scale - 1) // scale
    stride = w * 4
    rows = bytearray()
    for y in range(0, h, scale):
        off = hdr + y * stride
        px = array(_PX)
        px.frombytes(raw[off:off + stride])
        s = px[::scale].tobytes()
        rgb = bytearray(len(s) // 4 * 3)
        if order == "bgra":
            rgb[0::3], rgb[1::3], rgb[2::3] = s[2::4], s[1::4], s[0::4]
        else:
            rgb[0::3], rgb[1::3], rgb[2::3] = s[0::4], s[1::4], s[2::4]
        rows += b"\x00"
        rows += rgb
    png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", ow, oh, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + _chunk(b"IEND", b""))
    return png, ow, oh


def screenshot_png(raw: bytes, *, max_side: int = SCREENSHOT_MAX_SIDE,
                   max_b64: int = MAX_PNG_B64) -> Tuple[bytes, int, int, int, int, int]:
    """``screencap`` 原始帧（头 12 或 16 字节：w, h, format[, dataspace]，之后 w*h*4 像素）→
    (png, 图宽, 图高, 设备宽, 设备高, 缩小倍数)。"""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 12 or len(raw) > MAX_RAW_BYTES:
        raise PhoneOpError("screencap_bad_frame", failed=True)
    w, h, fmt = struct.unpack_from("<III", raw, 0)
    if not (0 < w <= 8192 and 0 < h <= 8192):
        raise PhoneOpError("screencap_bad_frame", failed=True)
    hdr = len(raw) - w * h * 4
    if hdr not in (12, 16):
        raise PhoneOpError("screencap_bad_frame", failed=True)
    order = _RAW_FORMATS.get(fmt)
    if order is None:
        raise PhoneOpError(f"screencap_format_{fmt}", failed=True)
    scale = max(1, math.ceil(max(w, h) / max(64, int(max_side))))
    while True:
        png, ow, oh = _encode_png(bytes(raw), hdr, w, h, order, scale)
        if 4 * math.ceil(len(png) / 3) <= max_b64:
            return png, ow, oh, w, h, scale
        if scale >= MAX_SCALE:
            raise PhoneOpError("screenshot_too_large", failed=True)
        scale += 1


class PhoneOps:
    """一个节点的远程手机执行器。``execute`` 永不抛：返回 (status, result, detail)。"""

    def __init__(self, *, adb_path: str = "", exclude: Any = None, enabled: bool = True, allow_tcp: bool = False,
                 run: Optional[RunFn] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 server_version: Optional[Callable[[int], Optional[int]]] = None,
                 locate: Optional[Callable[[str], str]] = None) -> None:
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._server_version = server_version
        self._locate = locate
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._last_op: Dict[str, float] = {}
        self._screen: Dict[str, Tuple[int, int]] = {}
        self._client_versions: Dict[str, Optional[int]] = {}
        self.configure(adb_path=adb_path, exclude=exclude, enabled=enabled, allow_tcp=allow_tcp)

    def configure(self, *, adb_path: str = "", exclude: Any = None, enabled: bool = True,
                  allow_tcp: bool = False) -> None:
        """agent.json 热加载时调用（phones_exclude / adb_path / phone_ops_enabled / phone_ops_allow_tcp）。"""
        self.adb_path = str(adb_path or "")
        self.excludes = normalize_excludes(exclude)
        self.enabled = bool(enabled)
        self.allow_tcp = bool(allow_tcp)

    def caps(self) -> List[str]:
        return [CAP_PHONE_OPS_V1] if self.enabled else []

    # ── adb ──
    def _adb(self, adb: str, args: Sequence[str], timeout: float) -> bytes:
        check_adb_args(args)
        proc = (self._run or subprocess.run)([adb, *args], **_run_kwargs(timeout))
        rc = int(getattr(proc, "returncode", 1) or 0)
        out = getattr(proc, "stdout", b"") or b""
        if rc != 0:
            err = getattr(proc, "stderr", b"") or b""
            text = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
            e = PhoneOpError(f"adb_exit_{rc}", failed=True)
            e.stderr = " ".join(text.split())[:160]  # type: ignore[attr-defined]
            raise e
        return out if isinstance(out, bytes) else str(out).encode("utf-8")

    def _ready_adb(self) -> str:
        adb = (self._locate or find_adb)(self.adb_path)
        if not adb:
            raise PhoneOpError("adb_not_found")
        server = (self._server_version or adb_server_version)(adb_server_port())
        if server is None:
            raise PhoneOpError("adb_server_not_running")
        if adb not in self._client_versions:
            ver = self._adb(adb, ("version",), LIST_TIMEOUT_SEC).decode("utf-8", "replace")
            self._client_versions[adb] = parse_client_version(ver)
        client = self._client_versions[adb]
        if client is None:
            raise PhoneOpError("adb_version_unknown")
        if client != server:
            raise PhoneOpError("adb_version_mismatch")
        return adb

    def _device(self, adb: str, serial: str) -> Dict[str, str]:
        text = self._adb(adb, ("devices", "-l"), LIST_TIMEOUT_SEC).decode("utf-8", "replace")
        dev = next((d for d in parse_adb_devices(text) if d["serial"] == serial), None)
        if dev is None:
            raise PhoneOpError("phone_not_found")
        if is_excluded(serial, self.excludes, dev.get("model", "")):
            raise PhoneOpError("excluded_phone")
        if dev["state"] != "device":
            raise PhoneOpError(f"phone_not_ready:{dev['state']}")
        if dev.get("transport") == "tcp" and not self.allow_tcp:
            raise PhoneOpError("tcp_phone_not_allowed")
        return dev

    def _lock_for(self, serial: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(serial, threading.Lock())

    def _pace(self, serial: str) -> None:
        last = self._last_op.get(serial)
        if last is not None:
            wait = MIN_INTERVAL_SEC - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)

    def _check_bounds(self, serial: str, *coords: int) -> None:
        size = self._screen.get(serial)
        if size and any(c >= max(size) for c in coords):     # either orientation: long side bounds both axes
            raise PhoneOpError("out_of_bounds")

    # ── 执行 ──
    def _do(self, adb: str, kind: str, serial: str, p: Dict[str, Any]) -> Dict[str, Any]:
        base = ("-s", serial)
        if kind == TASK_PHONE_SCREENSHOT:
            raw = self._adb(adb, base + ("exec-out", "screencap"), SCREENCAP_TIMEOUT_SEC)
            png, w, h, dw, dh, scale = screenshot_png(raw)
            self._screen[serial] = (dw, dh)
            return {"png_b64": base64.b64encode(png).decode("ascii"), "width": w, "height": h,
                    "device_width": dw, "device_height": dh, "scale": scale, "bytes": len(png)}
        if kind == TASK_PHONE_TAP:
            self._check_bounds(serial, p["x"], p["y"])
            self._adb(adb, base + ("shell", "input", "tap", str(p["x"]), str(p["y"])), INPUT_TIMEOUT_SEC)
            return {"x": p["x"], "y": p["y"]}
        if kind == TASK_PHONE_SWIPE:
            self._check_bounds(serial, p["x1"], p["y1"], p["x2"], p["y2"])
            vals = tuple(str(p[k]) for k in ("x1", "y1", "x2", "y2", "duration_ms"))
            self._adb(adb, base + ("shell", "input", "swipe") + vals, INPUT_TIMEOUT_SEC + p["duration_ms"] / 1000)
            return {k: p[k] for k in ("x1", "y1", "x2", "y2", "duration_ms")}
        if kind == TASK_PHONE_TEXT:
            self._adb(adb, base + ("shell", "input", "text", escape_input_text(p["text"])), INPUT_TIMEOUT_SEC)
            return {"chars": len(p["text"])}
        if kind == TASK_PHONE_KEY:
            self._adb(adb, base + ("shell", "input", "keyevent", KEYCODES[p["key"]]), INPUT_TIMEOUT_SEC)
            return {"key": p["key"]}
        raise PhoneOpError("bad_kind")

    def execute(self, kind: str, payload: Any, target: Any) -> Tuple[str, Dict[str, Any], str]:
        if kind not in PHONE_TASK_KINDS:
            return STATUS_REJECTED, {}, f"unknown_kind:{kind}"
        if not self.enabled:
            return STATUS_REJECTED, {}, "phone_ops_disabled"
        try:
            serial = check_target((target or {}).get("serial") if isinstance(target, dict) else "")
            if is_excluded(serial, self.excludes):
                raise PhoneOpError("excluded_phone")
            p = validate_payload(kind, payload)
        except PhoneOpError as e:
            return STATUS_REJECTED, {}, e.code
        if not self._slots.acquire(timeout=LOCK_WAIT_SEC):
            return STATUS_FAILED, {"serial": serial}, "node_busy"
        lock = self._lock_for(serial)
        try:
            if not lock.acquire(timeout=LOCK_WAIT_SEC):
                return STATUS_FAILED, {"serial": serial}, "phone_busy"
            try:
                adb = self._ready_adb()
                self._device(adb, serial)
                self._pace(serial)
                t0 = self._clock()
                try:
                    result = self._do(adb, kind, serial, p)
                finally:
                    self._last_op[serial] = self._clock()
                result.update({"serial": serial, "elapsed_ms": int(max(0.0, self._clock() - t0) * 1000)})
                return STATUS_DONE, result, "ok"
            finally:
                lock.release()
        except PhoneOpError as e:
            res: Dict[str, Any] = {"serial": serial}
            if getattr(e, "stderr", ""):
                res["stderr"] = e.stderr  # type: ignore[attr-defined]
            return (STATUS_FAILED if e.failed else STATUS_REJECTED), res, e.code
        except subprocess.TimeoutExpired:
            return STATUS_FAILED, {"serial": serial}, "adb_timeout"
        except Exception as e:  # noqa: BLE001 - never raise into the agent loop
            return STATUS_FAILED, {"serial": serial}, f"error:{type(e).__name__}"
        finally:
            self._slots.release()


__all__ = [
    "PhoneOps", "PHONE_TASK_KINDS", "check_adb_args", "screenshot_png", "SCREENSHOT_MAX_SIDE", "MIN_INTERVAL_SEC",
    "MAX_CONCURRENT",
]
