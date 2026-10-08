"""节点执行远程手机操作（agent 0.3.7）：phone_screenshot / phone_tap / phone_swipe / phone_text / phone_key。

安全栏：
* adb 只跑白名单参数（``check_adb_args`` → ``adb_allowlist.py``）：原有 ``devices -l``、
  ``version``、截图、``input``，加上只读诊断。界面层次优先写到固定文件
  ``/sdcard/chatx_like_hierarchy.xml`` 再 pull 回来；``--compressed`` 和
  ``dump /dev/tty`` 是回退。别的路径（含 ``window_dump.xml``）仍拒绝。
  找赞前可以发一次媒体暂停（keyevent 127 / ``media_session dispatch pause``），
  不再点画面。dump 之后如果前台已经离开信息流（帖子详情或全屏），再按一次返回。
  仅限 Facebook 的启动照旧。``phone_app_restart`` 才能 force-stop
  ``com.facebook.katana`` / ``com.facebook.lite``，而且要单独的
  ``allow_app_restart`` 标志；其它包的 force-stop、改设置、开关流量、重启、卸载
  仍默认拒绝。参数列表、不经本机 shell、每条都有超时。
* 默认不拉起 adb server，也不停、不改端口、不改连接模式：先按清点同一套办法用 ``host:version``
  问现有 server；没有 server、或本机 adb 客户端版本和 server 不一致（会触发 server 重启）→ 拒绝。
  agent.json ``adb_manage_server: true``（默认关）时，仅当没有 server 在应答，才允许安装目录里
  自带的 adb 把 server 拉起来；直播机永远不拉。PATH 上的 adb 和 ``C:\\platform-tools`` 不会被拉起。
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
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from array import array
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .phone_flow_robust import MAX_DWELL_SEC
from .phone_rules import (
    KEYCODES, MAX_PNG_B64, PhoneOpError, check_target, escape_input_text, validate_payload,
)
from .phones import (
    is_excluded, normalize_excludes, parse_adb_devices, parse_client_version, prepare_adb,
)
from .protocol import (
    CAP_PHONE_OPS_V1, PHONE_TASK_KINDS, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, TASK_PHONE_APP_RESTART,
    TASK_PHONE_KEY, TASK_PHONE_SCREENSHOT, TASK_PHONE_SWIPE, TASK_PHONE_TAP, TASK_PHONE_TEXT,
)

SCREENCAP_TIMEOUT_SEC = 15
INPUT_TIMEOUT_SEC = 10
LIST_TIMEOUT_SEC = 5
HIERARCHY_DUMP_TIMEOUT_SEC = 12.0
HIERARCHY_PULL_TIMEOUT_SEC = 8.0
HIERARCHY_PAUSE_TIMEOUT_SEC = 4.0
HIERARCHY_SETTLE_SEC = 0.45
HIERARCHY_MAX_BYTES = 2_000_000
MIN_INTERVAL_SEC = 0.5
MAX_CONCURRENT = 2
LOCK_WAIT_SEC = 10
SCREENSHOT_MAX_SIDE = 1024
MAX_SCALE = 16
MAX_RAW_BYTES = 80 * 1024 * 1024
_RAW_FORMATS = {1: "rgba", 2: "rgba", 5: "bgra"}     # PixelFormat RGBA_8888 / RGBX_8888 / BGRA_8888
_PX = "I" if array("I").itemsize == 4 else "L"

RunFn = Callable[..., Any]


def check_adb_args(args: Sequence[str], *, allow_guarded_writes: bool = False,
                   allow_experimental_ussd: bool = False,
                   allow_app_restart: bool = False) -> None:
    """Admit ``args`` or raise PhoneOpError.

    The catalog lives in ``adb_allowlist``. The extra flags default off.
    ``allow_app_restart`` admits only a Facebook force-stop.
    """
    from .adb_allowlist import admit_adb_args

    admit_adb_args(args, allow_guarded_writes=allow_guarded_writes,
                   allow_experimental_ussd=allow_experimental_ussd,
                   allow_app_restart=allow_app_restart)


_FB_PACKAGES = ("com.facebook.katana", "com.facebook.lite")
_OFF_FEED = (
    "permalink", "immersive", "fullscreen", "fbchrome",
    "storyviewer", "videoplayer", "fbshorts", "watchandbrowse",
)
_RESUMED_HINT = re.compile(r"mResumedActivity|topResumedActivity|mCurrentFocus|mFocusedApp")
_COMPONENT = re.compile(r"([A-Za-z][\w.]*)/(\.?[A-Za-z][\w.$]*)")
_PKG_CHARS = re.compile(r"^[A-Za-z][\w.]*\Z")


def parse_resumed_component(text: str) -> Tuple[str, str]:
    """Package and activity class from a dumpsys activity or window blob."""
    lines = [line for line in (text or "").splitlines() if _RESUMED_HINT.search(line)]
    chosen = lines or (text or "").splitlines()
    found = ("", "")
    for line in chosen:
        match = _COMPONENT.search(line)
        if match is None:
            continue
        package, activity = match.group(1), match.group(2)
        if not _PKG_CHARS.fullmatch(package) or len(package) > 80 or len(activity) > 120:
            continue
        found = (package, activity)
        if package in _FB_PACKAGES:
            return found
    return found


def off_feed_page(package: str, activity: str) -> bool:
    """True only when Facebook is positively off the feed (detail or fullscreen)."""
    if package not in _FB_PACKAGES:
        return False
    low = (activity or "").casefold()
    if not low or "fbmaintab" in low:
        return False
    return any(token in low for token in _OFF_FEED)


_DUMP_NOISE = (
    "ui hierchary dumped to:",
    "ui hierarchy dumped to:",
)


def _hierarchy_text(data: Any) -> str:
    if isinstance(data, str):
        raw = data.encode("utf-8", "replace")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        return ""
    if len(raw) > HIERARCHY_MAX_BYTES:
        return ""
    return raw.decode("utf-8", "replace")


def _hierarchy_ok(text: str) -> bool:
    if not text or "<hierarchy" not in text:
        return False
    from .like_locate import hierarchy_text_ok

    return hierarchy_text_ok(text)


def _dump_error_line(*chunks: str) -> str:
    """First printable dump-error line. The 'dumped to' notice is not an error."""
    for chunk in chunks:
        if not isinstance(chunk, str):
            continue
        for line in chunk.splitlines():
            text = " ".join(line.split())
            if not text:
                continue
            low = text.lower()
            if low.startswith(_DUMP_NOISE) or "hierchary dumped to:" in low or "hierarchy dumped to:" in low:
                continue
            clean = "".join(ch for ch in text if ch.isprintable() and ch not in "<>")[:160]
            if clean:
                return clean
    return ""


def _hierarchy_local_path(state_dir: Optional[Path]) -> Optional[str]:
    """A pull destination adb can create. The empty file is removed first.

    ``adb pull`` refuses to overwrite. The name has no device serial. A path
    with a space or ``..`` is skipped so the allowlist can reject it.
    """
    from .adb_allowlist import hierarchy_pull_local_ok

    dirs: List[str] = []
    if state_dir is not None:
        dirs.append(str(state_dir))
    dirs.append(tempfile.gettempdir())
    for directory in dirs:
        try:
            os.makedirs(directory, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="chatx_like_", suffix=".xml", dir=directory)
            os.close(fd)
            os.remove(path)
        except OSError:
            continue
        if hierarchy_pull_local_ok(path):
            return path
    return None


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
                 locate: Optional[Callable[[str], str]] = None,
                 manage_server: bool = False, live_stream: Optional[bool] = None,
                 state_dir: Optional[Path] = None) -> None:
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._server_version = server_version
        self._locate = locate
        self._live_stream = live_stream
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._last_op: Dict[str, float] = {}
        self._last_raw: Dict[str, bytes] = {}
        self._screen: Dict[str, Tuple[int, int]] = {}
        self._client_versions: Dict[str, Optional[int]] = {}
        self.configure(adb_path=adb_path, exclude=exclude, enabled=enabled, allow_tcp=allow_tcp,
                       manage_server=manage_server, state_dir=state_dir)

    def configure(self, *, adb_path: str = "", exclude: Any = None, enabled: bool = True,
                  allow_tcp: bool = False, manage_server: bool = False,
                  state_dir: Optional[Path] = None) -> None:
        """agent.json 热加载（phones_exclude / adb_path / phone_ops_* / adb_manage_server）。"""
        self.adb_path = str(adb_path or "")
        self.excludes = normalize_excludes(exclude)
        self.enabled = bool(enabled)
        self.allow_tcp = bool(allow_tcp)
        self.manage_server = bool(manage_server)
        self._state_dir = state_dir

    def caps(self) -> List[str]:
        return [CAP_PHONE_OPS_V1] if self.enabled else []

    # ── adb ──
    def _adb(self, adb: str, args: Sequence[str], timeout: float, *,
             allow_app_restart: bool = False) -> bytes:
        check_adb_args(args, allow_app_restart=allow_app_restart)
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

    def _adb_capture(self, adb: str, args: Sequence[str], timeout: float) -> Tuple[int, bytes, bytes]:
        """Run an allowlisted command and keep stdout even when the exit code is not zero.

        ``uiautomator dump`` often exits non-zero with ``could not get idle state``
        after it has already written a usable file. Callers decide whether the
        bytes parse. A timeout is exit 124 with empty output.
        """
        check_adb_args(args)
        try:
            proc = (self._run or subprocess.run)([adb, *args], **_run_kwargs(timeout))
        except subprocess.TimeoutExpired:
            return 124, b"", b"timeout"
        rc = int(getattr(proc, "returncode", 1) or 0)
        out = getattr(proc, "stdout", b"") or b""
        err = getattr(proc, "stderr", b"") or b""
        if not isinstance(out, (bytes, bytearray)):
            out = str(out).encode("utf-8", "replace")
        if not isinstance(err, (bytes, bytearray)):
            err = str(err).encode("utf-8", "replace")
        return rc, bytes(out), bytes(err)

    def _ready_adb(self) -> str:
        try:
            adb, server = prepare_adb(
                self.adb_path, manage_server=self.manage_server, server_version=self._server_version,
                locate=self._locate, run=self._run, live_stream=self._live_stream, state_dir=self._state_dir,
            )
        except RuntimeError as e:
            raise PhoneOpError(str(e)[:120] or "adb_error") from None
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

    def add_human_gap(self, serial: str, seconds: float) -> None:
        """步骤之间额外停一下。不超过 2 秒；停完把「上次操作」拨到现在，后面的最短间隔照旧。

        调用方已经占着这台手机的锁。非正数什么都不做。
        """
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            return
        extra = float(seconds)
        if extra > 2.0:
            extra = 2.0
        self._sleep(extra)
        self._last_op[serial] = self._clock()

    def add_dwell(self, serial: str, seconds: float) -> None:
        """看一条、停一下。不超过 12 秒；停完把「上次操作」拨到现在，0.5 秒最短间隔照旧。

        调用方已经占着这台手机的锁。不发 adb。非正数什么都不做。
        """
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            return
        extra = float(seconds)
        if extra > MAX_DWELL_SEC:
            extra = MAX_DWELL_SEC
        self._sleep(extra)
        self._last_op[serial] = self._clock()

    def last_raw(self, serial: str) -> bytes:
        return self._last_raw.get(serial, b"")

    def read_ui_hierarchy(self, serial: str) -> Dict[str, Any]:
        """Read a window hierarchy, settling autoplay first.

        The caller is already inside ``session`` and holds this phone's lock.
        A failure is not an error: the Like search then uses the screenshot.
        The dict is ``xml`` (parseable document, or the last raw text when
        nothing parsed), ``error`` (first dump-error line, not yet scrubbed),
        ``attempts``, ``via`` (``file`` or ``stdout``), and ``compressed``.
        """
        blank = {"xml": "", "error": "", "attempts": 0, "via": "", "compressed": False}
        try:
            adb = self._ready_adb()
        except PhoneOpError:
            return dict(blank)
        self._settle_autoplay(adb, serial)
        try:
            return self._dump_plan(adb, serial)
        finally:
            self._recover_off_feed(adb, serial)

    def _dump_plan(self, adb: str, serial: str) -> Dict[str, Any]:
        plan = (
            ("file", False, 0.0),
            ("file", False, 0.35),
            ("file", True, 0.55),
            ("stdout", False, 0.35),
            ("stdout", True, 0.55),
        )
        last_xml = ""
        last_error = ""
        last_via = ""
        last_compressed = False
        attempts = 0
        for index, (via, compressed, backoff) in enumerate(plan):
            if index and backoff > 0:
                self._sleep(backoff)
                self._last_op[serial] = self._clock()
            attempts += 1
            last_via = via
            last_compressed = compressed
            xml, error = self._dump_once(adb, serial, via, compressed)
            if error:
                last_error = error
            if xml:
                last_xml = xml
            if _hierarchy_ok(xml):
                return {
                    "xml": xml, "error": "", "attempts": attempts,
                    "via": via, "compressed": compressed,
                }
        if len(last_xml) > 4000:
            last_xml = last_xml[:4000]
        return {
            "xml": last_xml, "error": last_error, "attempts": attempts,
            "via": last_via, "compressed": last_compressed,
        }

    def _settle_autoplay(self, adb: str, serial: str) -> None:
        """Pause an autoplaying feed with media keys only. Failures do not abort the dump.

        A tap on the upper half of the last screenshot used to open a post, so
        the next probe could not see the feed. That tap is gone.
        """
        commands = (
            ("-s", serial, "shell", "cmd", "media_session", "dispatch", "pause"),
            ("-s", serial, "shell", "input", "keyevent", "127"),
        )
        for args in commands:
            self._paced_capture(adb, serial, args, HIERARCHY_PAUSE_TIMEOUT_SEC)
        self._sleep(HIERARCHY_SETTLE_SEC)
        self._last_op[serial] = self._clock()

    def _recover_off_feed(self, adb: str, serial: str) -> None:
        """Press Back once when the dump left Facebook on a post or a fullscreen page.

        The main tab, an unknown page, and a non-Facebook window are left alone.
        One press only, after the dump retries, not between them.
        """
        try:
            found = self.read_foreground(serial)
        except Exception:
            return
        if off_feed_page(found.get("package") or "", found.get("activity") or ""):
            self._paced_capture(
                adb, serial, ("-s", serial, "shell", "input", "keyevent", "4"),
                HIERARCHY_PAUSE_TIMEOUT_SEC,
            )

    def read_foreground(self, serial: str) -> Dict[str, str]:
        """Resumed package and activity. Empty strings on failure.

        Does not take the phone lock. A caller that already holds the lock
        (a like flow that just failed its login check) may call this directly.
        """
        blank = {"package": "", "activity": ""}
        try:
            adb = self._ready_adb()
        except PhoneOpError:
            return dict(blank)
        _rc, out, err = self._paced_capture(
            adb, serial, ("-s", serial, "shell", "dumpsys", "activity", "activities"), 6.0,
        )
        text = _hierarchy_text(out) or _hierarchy_text(err)
        package, activity = parse_resumed_component(text)
        return {"package": package, "activity": activity}

    def _paced_capture(self, adb: str, serial: str, args: Sequence[str], timeout: float) -> Tuple[int, bytes, bytes]:
        self._pace(serial)
        try:
            return self._adb_capture(adb, args, timeout)
        except PhoneOpError:
            return 1, b"", b""
        finally:
            self._last_op[serial] = self._clock()

    def _dump_once(self, adb: str, serial: str, via: str, compressed: bool) -> Tuple[str, str]:
        from .adb_allowlist import HIERARCHY_REMOTE

        target = HIERARCHY_REMOTE if via == "file" else "/dev/tty"
        argv = ["-s", serial, "shell", "uiautomator", "dump"]
        if compressed:
            argv.append("--compressed")
        argv.append(target)
        _rc, out, err = self._paced_capture(adb, serial, tuple(argv), HIERARCHY_DUMP_TIMEOUT_SEC)
        stdout = _hierarchy_text(out)
        stderr = _hierarchy_text(err)
        pulled = self._pull_hierarchy(adb, serial) if via == "file" else ""
        if _hierarchy_ok(pulled):
            return pulled, ""
        if _hierarchy_ok(stdout):
            return stdout, ""
        raw = pulled or stdout or stderr
        return raw, _dump_error_line(stderr, stdout, pulled)

    def _pull_hierarchy(self, adb: str, serial: str) -> str:
        from .adb_allowlist import HIERARCHY_REMOTE, hierarchy_pull_local_ok

        path = _hierarchy_local_path(self._state_dir)
        if path is None or not hierarchy_pull_local_ok(path):
            return ""
        self._paced_capture(
            adb, serial, ("-s", serial, "pull", HIERARCHY_REMOTE, path), HIERARCHY_PULL_TIMEOUT_SEC,
        )
        try:
            if not os.path.isfile(path):
                return ""
            data = Path(path).read_bytes()
        except OSError:
            return ""
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return _hierarchy_text(data)

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
            self._last_raw[serial] = bytes(raw)
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

    @contextmanager
    def session(self, serial: str) -> Iterator[Callable[[str, Any], Dict[str, Any]]]:
        """一把锁跑完一整段复合动作：占一个并发名额、占住这台手机，步骤之间照旧留间隔。

        产出 ``run(kind, payload) -> dict``。校验失败抛 PhoneOpError；
        ``node_busy`` / ``phone_busy`` 的 ``failed=True``（和单步 execute 一样记失败）。
        """
        serial = check_target(serial)
        if is_excluded(serial, self.excludes):
            raise PhoneOpError("excluded_phone")
        if not self.enabled:
            raise PhoneOpError("phone_ops_disabled")
        if not self._slots.acquire(timeout=LOCK_WAIT_SEC):
            raise PhoneOpError("node_busy", failed=True)
        lock = self._lock_for(serial)
        locked = False
        try:
            if not lock.acquire(timeout=LOCK_WAIT_SEC):
                raise PhoneOpError("phone_busy", failed=True)
            locked = True
            adb = self._ready_adb()
            self._device(adb, serial)

            def run(kind: str, payload: Any) -> Dict[str, Any]:
                p = validate_payload(kind, payload)
                self._pace(serial)
                try:
                    return self._do(adb, kind, serial, p)
                finally:
                    self._last_op[serial] = self._clock()

            yield run
        finally:
            if locked:
                lock.release()
            self._slots.release()

    def execute_app_restart(self, payload: Any, target: Any) -> Tuple[str, Dict[str, Any], str]:
        """Force-stop Facebook, then open its launcher. Never raises.

        The caller (the agent) has already refused a live-stream host and the
        seat machine. This still refuses a protected phone, an excluded phone,
        and a phone that is not ``state=device``. Only katana and lite.
        """
        if not self.enabled:
            return STATUS_REJECTED, {}, "phone_ops_disabled"
        try:
            serial = check_target((target or {}).get("serial") if isinstance(target, dict) else "")
            if is_excluded(serial, self.excludes):
                raise PhoneOpError("excluded_phone")
            p = validate_payload(TASK_PHONE_APP_RESTART, payload)
        except PhoneOpError as e:
            return STATUS_REJECTED, {}, e.code
        if not self._slots.acquire(timeout=LOCK_WAIT_SEC):
            return STATUS_FAILED, {"serial": serial}, "node_busy"
        lock = self._lock_for(serial)
        try:
            if not lock.acquire(timeout=LOCK_WAIT_SEC):
                return STATUS_FAILED, {"serial": serial}, "phone_busy"
            try:
                from .adb_allowlist import facebook_force_stop_args, facebook_launch_args

                adb = self._ready_adb()
                self._device(adb, serial)
                package = p["package"]
                self._pace(serial)
                t0 = self._clock()
                try:
                    self._adb(
                        adb, ("-s", serial, "shell") + facebook_force_stop_args(package),
                        INPUT_TIMEOUT_SEC, allow_app_restart=True,
                    )
                    self._last_op[serial] = self._clock()
                    self._pace(serial)
                    self._adb(
                        adb, ("-s", serial, "shell") + facebook_launch_args(package),
                        INPUT_TIMEOUT_SEC,
                    )
                finally:
                    self._last_op[serial] = self._clock()
                elapsed = int(max(0.0, self._clock() - t0) * 1000)
                return STATUS_DONE, {
                    "serial": serial, "package": package, "stopped": True, "launched": True,
                    "elapsed_ms": elapsed,
                }, "ok"
            finally:
                lock.release()
        except PhoneOpError as e:
            res: Dict[str, Any] = {"serial": serial, "package": p["package"]}
            if getattr(e, "stderr", ""):
                res["stderr"] = e.stderr  # type: ignore[attr-defined]
            return (STATUS_FAILED if e.failed else STATUS_REJECTED), res, e.code
        except subprocess.TimeoutExpired:
            return STATUS_FAILED, {"serial": serial, "package": p["package"]}, "adb_timeout"
        except Exception as e:  # noqa: BLE001 - never raise into the agent loop
            return STATUS_FAILED, {"serial": serial, "package": p["package"]}, f"error:{type(e).__name__}"
        finally:
            self._slots.release()

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
