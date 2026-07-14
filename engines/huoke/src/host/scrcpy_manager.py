# -*- coding: utf-8 -*-
"""
Scrcpy Manager — per-device scrcpy-server lifecycle with video + control.

Protocol (scrcpy v3.x, tunnel_forward=true, video=true, control=true):
  1. Push scrcpy-server binary to device (once, cached)
  2. ADB forward: tcp:PORT -> localabstract:scrcpy_SCID
  3. Start scrcpy-server with matching scid
  4. Connect socket #1 → video (reads: 1B dummy + 64B name + 4B codec + 8B size + frames)
  5. Connect socket #2 → control (writes: binary control messages)

Control message format (big-endian):
  Touch: type(1)=2 + action(1) + pointerId(8) + x(4) + y(4) + w(2) + h(2) + pressure(2) + actionBtn(4) + buttons(4) = 32B
  Key:   type(1)=0 + action(1) + keycode(4) + repeat(4) + meta(4) = 14B
  Text:  type(1)=1 + length(4) + utf8_bytes(N)
"""

from __future__ import annotations

import logging
import os
import random
import socket
import struct
import subprocess
import threading
import time
from typing import Dict, Optional

log = logging.getLogger(__name__)

_SCRCPY_SERVER_JAR = None
_BASE_PORT = 27200
_MAX_DEVICES = 32
_DEVICE_NAME_LEN = 64
_pushed_devices: set = set()

# Quality presets: (max_size, bitrate, max_fps, label)
QUALITY_PRESETS = {
    "ultra":  (1280, 6_000_000, 60, "超高 6Mbps/60fps"),
    "high":   (1024, 3_000_000, 30, "高 3Mbps/30fps"),
    "medium": (800,  2_000_000, 30, "中 2Mbps/30fps"),
    "low":    (600,  1_000_000, 24, "低 1Mbps/24fps"),
    "minimal":(480,  500_000,   15, "极低 500Kbps/15fps"),
    "thumb":  (320,  300_000,   10, "缩略 300Kbps/10fps"),
}
_DEFAULT_QUALITY = "medium"

# scrcpy control message types
_CTRL_INJECT_KEYCODE = 0
_CTRL_INJECT_TEXT = 1
_CTRL_INJECT_TOUCH = 2
_CTRL_INJECT_SCROLL = 3
_CTRL_BACK_OR_SCREEN_ON = 4

# Android MotionEvent actions
_ACTION_DOWN = 0
_ACTION_UP = 1
_ACTION_MOVE = 2

_POINTER_ID_MOUSE = -1  # 0xFFFFFFFFFFFFFFFF as signed i64
_PRESSURE_MAX = 0xFFFF


def _find_scrcpy_server() -> str:
    global _SCRCPY_SERVER_JAR
    if _SCRCPY_SERVER_JAR and os.path.exists(_SCRCPY_SERVER_JAR):
        return _SCRCPY_SERVER_JAR

    candidates = []

    # 项目根目录和 cwd
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    candidates.append(os.path.join(project_root, "vendor", "scrcpy-server"))
    candidates.append(os.path.join(project_root, "scrcpy-server"))
    candidates.append(os.path.join(os.getcwd(), "vendor", "scrcpy-server"))
    candidates.append(os.path.join(os.getcwd(), "scrcpy-server"))

    import shutil
    scrcpy_exe = shutil.which("scrcpy")
    if scrcpy_exe:
        d = os.path.dirname(scrcpy_exe)
        candidates.append(os.path.join(d, "scrcpy-server"))
        candidates.append(os.path.join(d, "scrcpy-server.jar"))

    winget_base = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_base):
        for folder in os.listdir(winget_base):
            if "scrcpy" in folder.lower():
                p = os.path.join(winget_base, folder)
                for root, _, files in os.walk(p):
                    for f in files:
                        if f == "scrcpy-server":
                            candidates.append(os.path.join(root, f))

    for c in candidates:
        if os.path.exists(c):
            _SCRCPY_SERVER_JAR = c
            log.info("[scrcpy] Found server: %s", c)
            return c

    raise FileNotFoundError(
        "scrcpy-server not found. Install scrcpy: winget install Genymobile.scrcpy")


class ScrcpySession:
    """Manages a single device's scrcpy streaming + control session."""

    def __init__(self, device_id: str, port: int,
                 max_size: int = 800, bitrate: int = 2_000_000,
                 max_fps: int = 30, enable_control: bool = True,
                 quality: str = _DEFAULT_QUALITY):
        self.device_id = device_id
        self.port = port
        self.max_size = max_size
        self.bitrate = bitrate
        self.max_fps = max_fps
        self.enable_control = enable_control
        self.quality = quality
        self._process: Optional[subprocess.Popen] = None
        self._video_socket: Optional[socket.socket] = None
        self._control_socket: Optional[socket.socket] = None
        self._running = False
        self._lock = threading.Lock()
        self._ctrl_lock = threading.Lock()
        self.screen_width = 0
        self.screen_height = 0
        self._device_name = ""
        self._scid = f"{random.randint(0, 0x7FFFFFFF):08x}"
        self.has_control = False
        # 缓存初始关键帧，供后续客户端重放
        self._init_frames: list = []  # [(bytes), ...] SPS+PPS帧 和 第一个IDR帧
        self._init_frames_ready = False
        # Frame rate monitoring
        self._frame_count = 0
        self._frame_bytes = 0
        self._fps_window: list = []  # timestamps of recent frames
        self._fps_lock = threading.Lock()
        self._last_fps_calc = 0.0
        self._current_fps = 0.0
        self._current_kbps = 0.0

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return True
            try:
                log.info("[scrcpy] Starting session for %s on port %d",
                         self.device_id[:8], self.port)
                server_path = _find_scrcpy_server()
                log.info("[scrcpy] Found server: %s", server_path)
                self._push_server(server_path)
                self._kill_device_server(kill_all=True)
                log.info("[scrcpy] Push done, setting up forward for %s",
                         self.device_id[:8])
                self._setup_forward()
                log.info("[scrcpy] Forward set, starting server for %s",
                         self.device_id[:8])
                self._start_server()
                log.info("[scrcpy] Server started, connecting sockets for %s",
                         self.device_id[:8])
                self._connect_video_socket()
                if self.enable_control:
                    self._connect_control()
                self._read_video_meta()
                log.info("[scrcpy] Video ready for %s, screen=%dx%d",
                         self.device_id[:8],
                         self.screen_width, self.screen_height)
                self._running = True
                ctrl_status = "with control" if self.has_control else "video-only"
                log.info("[scrcpy] Started %s: %s port=%d %dx%d",
                         ctrl_status, self.device_id[:8], self.port,
                         self.screen_width, self.screen_height)
                return True
            except Exception as e:
                log.error("[scrcpy] Failed to start for %s: %s",
                          self.device_id[:8], e)
                import traceback
                log.error("[scrcpy] Traceback:\n%s", traceback.format_exc())
                self.stop()
                return False

    def stop(self):
        with self._lock:
            self._running = False
            for sock_attr in ("_control_socket", "_video_socket"):
                sock = getattr(self, sock_attr, None)
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    setattr(self, sock_attr, None)
            self.has_control = False
            if self._process:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=3)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None
            self._kill_device_server()
            self._remove_forward()
            log.info("[scrcpy] Session stopped: %s", self.device_id[:8])

    def _kill_device_server(self, kill_all: bool = False):
        """Kill scrcpy-server on the device.

        Windows doesn't propagate SIGTERM through the ADB shell pipe,
        so we need to explicitly kill the process on the device.
        When kill_all=False, only kill this session's process (by SCID).
        When kill_all=True, kill ALL scrcpy processes (used before starting).
        """
        try:
            if kill_all:
                pattern = "com.genymobile.scrcpy.Server"
            else:
                pattern = f"scid={self._scid}"
            subprocess.run(
                ["adb", "-s", self.device_id, "shell",
                 f"pkill -f '{pattern}' 2>/dev/null || true"],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass

    @property
    def is_running(self) -> bool:
        if self._running and self._process and self._process.poll() is not None:
            log.warning("[scrcpy] 进程已退出: %s", self.device_id[:8])
            self._running = False
        return self._running

    # ── Video Stream ──

    def get_init_frames(self) -> list:
        """返回缓存的初始帧(SPS+PPS, IDR)，供新客户端重放。"""
        return list(self._init_frames)

    def read_video_frame(self) -> Optional[bytes]:
        if not self._video_socket or not self._running:
            return None
        try:
            header = self._recv_exact(self._video_socket, 12)
            if not header:
                log.debug("[scrcpy] read_video_frame: no header for %s",
                          self.device_id[:8])
                return None
            frame_len = struct.unpack(">I", header[8:12])[0]
            if frame_len == 0 or frame_len > 5_000_000:
                log.warning("[scrcpy] read_video_frame: bad frame_len=%d for %s",
                            frame_len, self.device_id[:8])
                return None
            data = self._recv_exact(self._video_socket, frame_len)
            if not data:
                log.debug("[scrcpy] read_video_frame: incomplete data for %s",
                          self.device_id[:8])
                return None
            frame = header + data
            self._track_frame(len(frame))
            # 缓存初始关键帧（SPS+PPS 和第一个 IDR）
            if not self._init_frames_ready:
                self._init_frames.append(frame)
                # 检查是否包含IDR(NAL type 5)
                for i in range(12, len(frame) - 3):
                    if frame[i]==0 and frame[i+1]==0:
                        if frame[i+2]==1 and i+3<len(frame) and (frame[i+3]&0x1f)==5:
                            self._init_frames_ready = True
                            break
                        if frame[i+2]==0 and i+4<len(frame) and frame[i+3]==1 and (frame[i+4]&0x1f)==5:
                            self._init_frames_ready = True
                            break
            return frame
        except socket.timeout:
            if self._frame_count == 0:
                log.warning("[scrcpy] read_video_frame: timeout waiting for "
                            "first frame from %s", self.device_id[:8])
            return None
        except Exception as e:
            if self._running:
                log.debug("[scrcpy] read_video_frame error for %s: %s",
                          self.device_id[:8], e)
            return None

    def _track_frame(self, byte_count: int):
        now = time.time()
        with self._fps_lock:
            self._frame_count += 1
            self._frame_bytes += byte_count
            self._fps_window.append(now)
            cutoff = now - 2.0
            self._fps_window = [t for t in self._fps_window if t > cutoff]
            if now - self._last_fps_calc >= 1.0:
                elapsed = now - self._last_fps_calc if self._last_fps_calc else 1.0
                self._current_fps = len(self._fps_window) / 2.0
                self._current_kbps = (self._frame_bytes * 8) / (elapsed * 1000)
                self._frame_bytes = 0
                self._last_fps_calc = now

    def get_stream_stats(self) -> dict:
        with self._fps_lock:
            return {
                "fps": round(self._current_fps, 1),
                "kbps": round(self._current_kbps, 0),
                "total_frames": self._frame_count,
                "quality": self.quality,
                "bitrate": self.bitrate,
                "max_size": self.max_size,
                "max_fps": self.max_fps,
            }

    # ── Control Channel ──

    def inject_touch(self, action: int, x: int, y: int,
                     pressure: int = _PRESSURE_MAX,
                     pointer_id: int = _POINTER_ID_MOUSE) -> bool:
        """Send a touch event via the scrcpy control channel.
        action: 0=DOWN, 1=UP, 2=MOVE
        """
        if not self.has_control or not self._control_socket:
            return False
        msg = struct.pack(">BbqiiHHHiI",
                          _CTRL_INJECT_TOUCH,
                          action,
                          pointer_id,
                          x, y,
                          self.screen_width, self.screen_height,
                          pressure if action != _ACTION_UP else 0,
                          0,  # actionButton
                          0)  # buttons
        return self._send_ctrl(msg)

    def inject_keycode(self, action: int, keycode: int,
                       repeat: int = 0, meta: int = 0) -> bool:
        """Send a key event. action: 0=DOWN, 1=UP"""
        if not self.has_control or not self._control_socket:
            return False
        msg = struct.pack(">BBiII",
                          _CTRL_INJECT_KEYCODE,
                          action,
                          keycode,
                          repeat,
                          meta)
        return self._send_ctrl(msg)

    def inject_text(self, text: str) -> bool:
        """Send text input."""
        if not self.has_control or not self._control_socket:
            return False
        encoded = text.encode("utf-8")
        msg = struct.pack(">BI", _CTRL_INJECT_TEXT, len(encoded)) + encoded
        return self._send_ctrl(msg)

    def inject_scroll(self, x: int, y: int, hscroll: int, vscroll: int) -> bool:
        """Send a scroll event."""
        if not self.has_control or not self._control_socket:
            return False
        msg = struct.pack(">BiiHHiiI",
                          _CTRL_INJECT_SCROLL,
                          x, y,
                          self.screen_width, self.screen_height,
                          hscroll, vscroll,
                          0)  # buttons
        return self._send_ctrl(msg)

    def tap(self, x: int, y: int) -> bool:
        """Convenience: tap at (x, y) — sends DOWN then UP."""
        ok1 = self.inject_touch(_ACTION_DOWN, x, y)
        time.sleep(0.05)
        ok2 = self.inject_touch(_ACTION_UP, x, y, pressure=0)
        return ok1 and ok2

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 300, steps: int = 15) -> bool:
        """Convenience: swipe from (x1,y1) to (x2,y2)."""
        self.inject_touch(_ACTION_DOWN, x1, y1)
        delay = duration_ms / 1000.0 / steps
        for i in range(1, steps + 1):
            frac = i / steps
            cx = int(x1 + (x2 - x1) * frac)
            cy = int(y1 + (y2 - y1) * frac)
            self.inject_touch(_ACTION_MOVE, cx, cy)
            time.sleep(delay)
        return self.inject_touch(_ACTION_UP, x2, y2, pressure=0)

    def press_key(self, keycode: int) -> bool:
        """Convenience: press and release a key."""
        ok1 = self.inject_keycode(_ACTION_DOWN, keycode)
        time.sleep(0.02)
        ok2 = self.inject_keycode(_ACTION_UP, keycode)
        return ok1 and ok2

    def _send_ctrl(self, data: bytes) -> bool:
        with self._ctrl_lock:
            try:
                self._control_socket.sendall(data)
                return True
            except Exception as e:
                log.warning("[scrcpy] control send failed: %s", e)
                return False

    # ── Internal Setup ──

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _push_server(self, local_path: str):
        remote = "/data/local/tmp/scrcpy-server.jar"

        # Calculate local MD5
        import hashlib
        with open(local_path, "rb") as f:
            local_md5 = hashlib.md5(f.read()).hexdigest()
        local_size = os.path.getsize(local_path)

        # Check remote file hash
        try:
            check = subprocess.run(
                ["adb", "-s", self.device_id, "shell",
                 f"md5sum {remote} 2>/dev/null || md5 {remote} 2>/dev/null || echo ''"],
                capture_output=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if check.returncode == 0:
                out = check.stdout.decode().strip()
                if local_md5 in out:
                    log.debug("[scrcpy] Server already up-to-date on %s (hash match)", self.device_id[:8])
                    _pushed_devices.add(self.device_id)
                    return
        except Exception:
            pass

        # Fall back to size check for speed
        try:
            check2 = subprocess.run(
                ["adb", "-s", self.device_id, "shell",
                 f"ls -l {remote} 2>/dev/null | head -1"],
                capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if check2.returncode == 0 and str(local_size) in check2.stdout.decode():
                log.debug("[scrcpy] Server size match on %s, skipping push", self.device_id[:8])
                _pushed_devices.add(self.device_id)
                return
        except Exception:
            pass

        # Push needed
        _pushed_devices.discard(self.device_id)
        log.info("[scrcpy] Pushing server to %s (%d bytes)", self.device_id[:8], local_size)
        cmd = ["adb", "-s", self.device_id, "push", local_path, remote]
        r = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            stderr = r.stderr.decode(errors="replace")
            log.error("[scrcpy] Push failed for %s: %s", self.device_id[:8], stderr)
            raise RuntimeError(f"Push failed: {stderr}")
        log.info("[scrcpy] Server pushed to %s", self.device_id[:8])
        _pushed_devices.add(self.device_id)

    def _setup_forward(self):
        socket_name = f"scrcpy_{self._scid}"
        cmd = ["adb", "-s", self.device_id, "forward",
               f"tcp:{self.port}", f"localabstract:{socket_name}"]
        r = subprocess.run(cmd, capture_output=True, timeout=5)
        if r.returncode != 0:
            raise RuntimeError(f"ADB forward failed: {r.stderr.decode()}")

    def _remove_forward(self):
        try:
            subprocess.run(
                ["adb", "-s", self.device_id, "forward", "--remove",
                 f"tcp:{self.port}"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    def _start_server(self):
        ctrl_flag = "true" if self.enable_control else "false"
        server_cmd = (
            f"CLASSPATH=/data/local/tmp/scrcpy-server.jar "
            f"app_process / com.genymobile.scrcpy.Server 3.3.4 "
            f"scid={self._scid} "
            f"log_level=info "
            f"tunnel_forward=true "
            f"video=true "
            f"audio=false "
            f"control={ctrl_flag} "
            f"max_size={self.max_size} "
            f"max_fps={self.max_fps} "
            f"video_bit_rate={self.bitrate} "
            f"video_codec_options=i-frame-interval=2 "
        )

        cmd = ["adb", "-s", self.device_id, "shell", server_cmd]
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(2.0)

        if self._process.poll() is not None:
            raise RuntimeError(
                f"scrcpy-server exited with code {self._process.returncode}")

    def _connect_video_socket(self):
        """Connect socket #1 — video stream. Read dummy byte only.

        Metadata is read later in _read_video_meta() because the server
        waits for ALL sockets (video + control) to connect before
        sending device name / codec / size.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)

        for attempt in range(15):
            try:
                sock.connect(("127.0.0.1", self.port))
                break
            except (ConnectionRefusedError, OSError):
                if attempt == 14:
                    raise
                time.sleep(0.5)

        dummy = self._recv_exact(sock, 1)
        if not dummy:
            if self._process and self._process.poll() is not None:
                err = self._process.stderr.read().decode(errors="replace")
                raise RuntimeError(f"scrcpy-server crashed: {err}")
            raise RuntimeError("No dummy byte from scrcpy")
        log.info("[scrcpy] Video socket connected, dummy=0x%02x for %s",
                 dummy[0], self.device_id[:8])
        self._video_socket = sock

    def _read_video_meta(self):
        """Read video metadata after all sockets are connected."""
        sock = self._video_socket
        sock.settimeout(15)

        device_name_bytes = self._recv_exact(sock, _DEVICE_NAME_LEN)
        if not device_name_bytes:
            raise RuntimeError("Failed to read device name")
        self._device_name = device_name_bytes.rstrip(b"\x00").decode(
            errors="replace")

        codec_bytes = self._recv_exact(sock, 4)
        if not codec_bytes:
            raise RuntimeError("Failed to read codec id")

        size_bytes = self._recv_exact(sock, 8)
        if not size_bytes:
            raise RuntimeError("Failed to read video size")
        self.screen_width = struct.unpack(">I", size_bytes[:4])[0]
        self.screen_height = struct.unpack(">I", size_bytes[4:])[0]

        sock.settimeout(8)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _connect_control(self):
        """Connect socket #2 — control channel (after video socket).

        P2-③ 二阶段重连防 race: scrcpy server 启动后 control receiver 内部 attach
        慢一点会让 TCP 三次握手成功但 server 还没读 control packet → has_control=True
        但实际丢事件. 用两阶段重试 (1.5s 短轮询 + 5s 等待 + 3s 长轮询) 提高稳定性.

        阶段一: 5 × 0.3s = 1.5s (常规快速 attach)
        阶段二: 等 5s 让 server 充分 init, 再 3 × 1s = 3s 兜底
        总耗时上限: ~10s (失败时)
        """
        if self._connect_control_once(attempts=5, delay=0.3, label="phase1"):
            return
        log.warning("[scrcpy] Control phase1 failed for %s, waiting 5s then phase2",
                    self.device_id[:8])
        time.sleep(5.0)
        if self._connect_control_once(attempts=3, delay=1.0, label="phase2"):
            return
        log.warning("[scrcpy] Control failed both phases for %s — falling back to "
                    "video-only. UI will display read-only banner; user can click "
                    "'reconnect control' button to retry without rebuilding video.",
                    self.device_id[:8])
        self.has_control = False

    def _connect_control_once(self, attempts: int, delay: float, label: str) -> bool:
        """单阶段 attempt 重连. 成功返 True, 全部失败返 False (不抛异常).

        P2-③ 在 connect 成功后立刻做 socket sanity probe (写 0 字节 noop), 检测
        '看似 connected 实际坏掉' 的极端情况 (server crash 但 forward 还在).
        """
        for attempt in range(attempts):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(("127.0.0.1", self.port))
                sock.settimeout(2)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # Sanity probe: 立刻 send 0 字节 检测 socket health
                # (broken socket 在写时立刻 EPIPE/ECONNRESET, 让我们及时降级)
                try:
                    sock.send(b"")
                except OSError as probe_err:
                    log.debug("[scrcpy] %s probe failed for %s attempt %d: %s",
                              label, self.device_id[:8], attempt + 1, probe_err)
                    sock.close()
                    raise
                self._control_socket = sock
                self.has_control = True
                log.info("[scrcpy] Control %s connected: %s (attempt %d)",
                         label, self.device_id[:8], attempt + 1)
                return True
            except Exception as e:
                if attempt < attempts - 1:
                    time.sleep(delay)
                else:
                    log.debug("[scrcpy] Control %s exhausted (%d attempts): %s",
                              label, attempts, e)
        return False

    def reconnect_control(self) -> bool:
        """P2-③ 重连 control 通道但保持 video stream.

        用户场景: 投屏视频流正常但屏幕点击没响应(has_control=False). 不必关掉视频
        流再开 (重启 scrcpy server 要 ~5-10s, 浪费), 直接重连 control socket.

        返回 True = 成功并 has_control=True, False = 仍 video-only.
        """
        with self._ctrl_lock:
            # 关闭旧 control socket (如果有)
            if self._control_socket:
                try:
                    self._control_socket.close()
                except Exception:
                    pass
                self._control_socket = None
            self.has_control = False
            # 不杀 server, 不动 video — 仅重新建 control socket
            log.info("[scrcpy] Reconnecting control for %s (keep video alive)",
                     self.device_id[:8])
            self._connect_control()
            return self.has_control


class ScrcpyManager:

    def __init__(self):
        self._sessions: Dict[str, ScrcpySession] = {}
        self._ref_counts: Dict[str, int] = {}   # WebSocket 客户端引用计数
        self._lock = threading.Lock()
        self._port_counter = _BASE_PORT

    # ── 引用计数 API（供 WebSocket handler 使用）──

    def acquire_session(self, device_id: str, **kwargs) -> Optional["ScrcpySession"]:
        """获取 session，若不存在或已停止则启动。同时增加引用计数。"""
        with self._lock:
            session = self._sessions.get(device_id)
            if session and session.is_running:
                self._ref_counts[device_id] = self._ref_counts.get(device_id, 0) + 1
                log.debug("[scrcpy] acquire %s refcnt=%d", device_id[:8],
                          self._ref_counts[device_id])
                return session
        # 不在锁内启动，避免长时间持锁
        session = self.start_session(device_id, **kwargs)
        if session:
            with self._lock:
                self._ref_counts[device_id] = self._ref_counts.get(device_id, 0) + 1
                log.info("[scrcpy] new session %s refcnt=%d", device_id[:8],
                         self._ref_counts[device_id])
        return session

    def release_session(self, device_id: str) -> None:
        """减少引用计数；当引用计数归零时才真正停止 session。"""
        session_to_stop = None
        with self._lock:
            count = max(0, self._ref_counts.get(device_id, 1) - 1)
            if count == 0:
                self._ref_counts.pop(device_id, None)
                session_to_stop = self._sessions.pop(device_id, None)
                log.info("[scrcpy] release %s → refcnt=0, stopping", device_id[:8])
            else:
                self._ref_counts[device_id] = count
                log.debug("[scrcpy] release %s refcnt=%d", device_id[:8], count)
        if session_to_stop:
            session_to_stop.stop()

    def start_session(self, device_id: str,
                      quality: str = "",
                      max_size: int = 0,
                      bitrate: int = 0,
                      max_fps: int = 0,
                      enable_control: bool = True) -> Optional[ScrcpySession]:
        with self._lock:
            if device_id in self._sessions:
                session = self._sessions[device_id]
                if session.is_running:
                    return session
                session.stop()

            if not quality:
                quality = self._detect_quality(device_id)

            active_count = sum(1 for s in self._sessions.values() if s.is_running)
            quality = self._adjust_for_concurrency(quality, active_count)

            preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS[_DEFAULT_QUALITY])
            final_size = max_size or preset[0]
            final_bitrate = bitrate or preset[1]
            final_fps = max_fps or preset[2]

            port = self._allocate_port()
            session = ScrcpySession(device_id, port, final_size, final_bitrate,
                                    final_fps, enable_control, quality)

        if session.start():
            with self._lock:
                self._sessions[device_id] = session
            return session
        return None

    @staticmethod
    def _adjust_for_concurrency(quality: str, active_count: int) -> str:
        """Auto-reduce quality when many streams are active."""
        if active_count <= 1:
            return quality
        levels = list(QUALITY_PRESETS.keys())
        idx = levels.index(quality) if quality in levels else 2
        if active_count <= 3:
            idx = min(idx, 3)  # cap at medium
        elif active_count <= 6:
            idx = min(idx, 2)  # cap at medium
        elif active_count <= 10:
            idx = min(idx, 1)  # cap at minimal
        else:
            idx = 0  # thumb
        new_q = levels[idx]
        if new_q != quality:
            log.info("[scrcpy] 并发流自动降质: %s→%s (活跃流:%d)", quality, new_q, active_count)
        return new_q

    def pre_push_server(self, device_id: str):
        """Push scrcpy-server to device in background so streaming starts instantly."""
        if device_id in _pushed_devices:
            return
        try:
            server_path = _find_scrcpy_server()
            remote = "/data/local/tmp/scrcpy-server.jar"
            local_size = os.path.getsize(server_path)
            check = subprocess.run(
                ["adb", "-s", device_id, "shell",
                 f"ls -l {remote} 2>/dev/null | head -1"],
                capture_output=True, timeout=5,
            )
            if check.returncode == 0 and str(local_size) in check.stdout.decode():
                _pushed_devices.add(device_id)
                log.info("[scrcpy] Server already on %s (pre-push check)", device_id[:8])
                return
            log.info("[scrcpy] Pre-pushing server to %s", device_id[:8])
            r = subprocess.run(
                ["adb", "-s", device_id, "push", server_path, remote],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0:
                _pushed_devices.add(device_id)
                log.info("[scrcpy] Pre-push done for %s", device_id[:8])
            else:
                log.warning("[scrcpy] Pre-push failed for %s: %s",
                            device_id[:8], r.stderr.decode(errors="replace"))
        except Exception as e:
            log.warning("[scrcpy] Pre-push error for %s: %s", device_id[:8], e)

    def stop_session(self, device_id: str):
        with self._lock:
            session = self._sessions.pop(device_id, None)
        if session:
            session.stop()

    def get_session(self, device_id: str) -> Optional[ScrcpySession]:
        return self._sessions.get(device_id)

    def stop_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()

    def change_quality(self, device_id: str, quality: str) -> Optional[ScrcpySession]:
        """Restart a session with different quality preset.

        Falls back to the previous quality (or 'medium') if the new one fails.
        Returns the session on success, None only if even the fallback fails.

        IMPORTANT: This method bypasses ref counting intentionally — it atomically
        replaces the session and resets _ref_counts so the new WS caller can
        acquire it via acquire_session() afterwards.
        """
        if quality not in QUALITY_PRESETS:
            return None

        # 原子地取出旧 session 并清空引用计数，防止旧 WS onclose 时 release_session
        # 把新 session 的引用减到 0 而误停
        with self._lock:
            old_session = self._sessions.pop(device_id, None)
            self._ref_counts.pop(device_id, None)  # 清除旧引用计数
            prev_quality = old_session.quality if old_session else _DEFAULT_QUALITY
            had_control = old_session.enable_control if old_session else True

        if old_session:
            old_session.stop()
            # 等待旧 scrcpy 进程释放 ADB forward 端口，避免新进程 bind 失败
            time.sleep(1.5)

        new_session = self.start_session(device_id, quality=quality,
                                         enable_control=had_control)
        if new_session:
            return new_session

        # 目标画质启动失败 → 回退到原画质
        log.warning("[scrcpy] quality change to '%s' failed for %s, reverting to '%s'",
                    quality, device_id[:8], prev_quality)
        time.sleep(0.5)
        fallback = self.start_session(device_id, quality=prev_quality,
                                       enable_control=had_control)
        if fallback:
            log.info("[scrcpy] fallback session restored with quality '%s' for %s",
                     prev_quality, device_id[:8])
        return fallback

    def active_sessions(self) -> list:
        return [
            {"device_id": s.device_id, "port": s.port,
             "width": s.screen_width, "height": s.screen_height,
             "has_control": s.has_control,
             "quality": s.quality,
             **s.get_stream_stats()}
            for s in self._sessions.values() if s.is_running
        ]

    def broadcast_touch(self, device_ids: list, action: int,
                        x: int, y: int) -> dict:
        """Send touch event to multiple devices simultaneously via scrcpy control."""
        results = {}
        threads = []

        def _send(did):
            session = self.get_session(did)
            if session and session.has_control:
                ok = session.inject_touch(action, x, y)
                results[did[:12]] = "ok" if ok else "fail"
            else:
                results[did[:12]] = "no_control"

        for did in device_ids:
            t = threading.Thread(target=_send, args=(did,))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)
        return results

    def broadcast_tap(self, device_ids: list, x: int, y: int) -> dict:
        """Synchronized tap on multiple devices via scrcpy control (~30ms)."""
        results_down = self.broadcast_touch(device_ids, _ACTION_DOWN, x, y)
        time.sleep(0.05)
        results_up = self.broadcast_touch(device_ids, _ACTION_UP, x, y)
        return {k: "ok" if results_down.get(k) == "ok" and results_up.get(k) == "ok"
                else results_down.get(k, "fail")
                for k in set(list(results_down.keys()) + list(results_up.keys()))}

    def broadcast_key(self, device_ids: list, keycode: int) -> dict:
        """Synchronized key press on multiple devices via scrcpy control."""
        results = {}
        threads = []

        def _send(did):
            session = self.get_session(did)
            if session and session.has_control:
                ok = session.press_key(keycode)
                results[did[:12]] = "ok" if ok else "fail"
            else:
                results[did[:12]] = "no_control"

        for did in device_ids:
            t = threading.Thread(target=_send, args=(did,))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)
        return results

    def broadcast_swipe(self, device_ids: list, x1: int, y1: int,
                        x2: int, y2: int, duration_ms: int = 300) -> dict:
        """Synchronized swipe on multiple devices via scrcpy control."""
        results = {}
        threads = []

        def _send(did):
            session = self.get_session(did)
            if session and session.has_control:
                ok = session.swipe(x1, y1, x2, y2, duration_ms)
                results[did[:12]] = "ok" if ok else "fail"
            else:
                results[did[:12]] = "no_control"

        for did in device_ids:
            t = threading.Thread(target=_send, args=(did,))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        return results

    def is_streaming(self, device_id: str) -> bool:
        """Check if a device has an active streaming session."""
        s = self._sessions.get(device_id)
        return s is not None and s.is_running

    def streaming_device_ids(self) -> set:
        """Return set of device IDs with active streams."""
        return {did for did, s in self._sessions.items() if s.is_running}

    def cleanup_dead_sessions(self):
        with self._lock:
            dead = [did for did, s in self._sessions.items()
                    if not s.is_running]
        for did in dead:
            log.info("[scrcpy] 清理死亡会话: %s", did[:8])
            self.stop_session(did)
        return len(dead)

    @staticmethod
    def _detect_quality(device_id: str) -> str:
        """Auto-detect optimal quality based on connection type."""
        try:
            r = subprocess.run(
                ["adb", "-s", device_id, "get-state"],
                capture_output=True, timeout=3,
            )
            state = r.stdout.decode().strip()
            if state == "device":
                tr = subprocess.run(
                    ["adb", "-s", device_id, "get-devpath"],
                    capture_output=True, timeout=3,
                )
                devpath = tr.stdout.decode().strip()
                if "usb" in devpath.lower():
                    log.info("[scrcpy] USB connection detected → high quality")
                    return "high"
        except Exception:
            pass
        log.info("[scrcpy] WiFi/default connection → medium quality")
        return "medium"

    def _allocate_port(self) -> int:
        port = self._port_counter
        self._port_counter += 1
        if self._port_counter > _BASE_PORT + _MAX_DEVICES:
            self._port_counter = _BASE_PORT
        while self._is_port_busy(port):
            port = self._port_counter
            self._port_counter += 1
            if self._port_counter > _BASE_PORT + _MAX_DEVICES:
                break
        return port

    @staticmethod
    def _is_port_busy(port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.bind(("127.0.0.1", port))
                return False
        except OSError:
            return True


_manager_instance: Optional[ScrcpyManager] = None
_mgr_lock = threading.Lock()


def get_scrcpy_manager() -> ScrcpyManager:
    global _manager_instance
    if _manager_instance is None:
        with _mgr_lock:
            if _manager_instance is None:
                _manager_instance = ScrcpyManager()
    return _manager_instance
