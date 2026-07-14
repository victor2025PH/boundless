# -*- coding: utf-8 -*-
"""
Screen Recorder — record device screens using ADB screenrecord.

Uses `adb shell screenrecord` to capture MP4 directly on-device,
then pulls the file to local storage. Supports:
- Per-device recording start/stop
- Automatic pull on stop
- Configurable time limit (default 180s, max 180s per segment)
- Long recordings via automatic segment chaining
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Dict, Optional

log = logging.getLogger(__name__)

_RECORDINGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "recordings"
)


class RecordingSession:
    """Single device recording session."""

    def __init__(self, device_id: str, output_dir: str,
                 max_segment_sec: int = 180, max_size: int = 0):
        self.device_id = device_id
        self.output_dir = output_dir
        self.max_segment_sec = min(max_segment_sec, 180)
        self.max_size = max_size
        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self._lock = threading.Lock()
        self._segment_idx = 0
        self._segments: list[str] = []
        self._start_time = 0.0
        self._chain_thread: Optional[threading.Thread] = None
        self._remote_path = ""

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return True
            os.makedirs(self.output_dir, exist_ok=True)
            self._running = True
            self._start_time = time.time()
            self._segment_idx = 0
            self._segments = []
        self._start_segment()
        return True

    def stop(self) -> list[str]:
        """Stop recording, pull files from device, return local file paths."""
        with self._lock:
            self._running = False
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        if self._chain_thread:
            self._chain_thread.join(timeout=5)
            self._chain_thread = None

        pulled = self._pull_segments()
        self._cleanup_remote()
        return pulled

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def duration_sec(self) -> float:
        if not self._running:
            return 0
        return time.time() - self._start_time

    def _start_segment(self):
        self._segment_idx += 1
        remote = f"/sdcard/openclaw_rec_{self.device_id[:8]}_{self._segment_idx}.mp4"
        self._remote_path = remote

        cmd = ["adb", "-s", self.device_id, "shell",
               f"screenrecord --time-limit {self.max_segment_sec} "
               f"{'--size ' + str(self.max_size) + ' ' if self.max_size else ''}"
               f"{remote}"]

        self._process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._segments.append(remote)
        log.info("[recorder] Started segment %d for %s",
                 self._segment_idx, self.device_id[:8])

        self._chain_thread = threading.Thread(
            target=self._segment_chain_loop, daemon=True)
        self._chain_thread.start()

    def _segment_chain_loop(self):
        """Wait for current segment to end and start a new one if still recording."""
        while self._running:
            if self._process:
                self._process.wait()
            if not self._running:
                break
            time.sleep(0.5)
            if self._running:
                self._start_segment()

    def _pull_segments(self) -> list[str]:
        pulled = []
        for remote in self._segments:
            local_name = os.path.basename(remote)
            local_path = os.path.join(self.output_dir, local_name)
            try:
                r = subprocess.run(
                    ["adb", "-s", self.device_id, "pull", remote, local_path],
                    capture_output=True, timeout=30,
                )
                if r.returncode == 0 and os.path.exists(local_path):
                    pulled.append(local_path)
                    log.info("[recorder] Pulled: %s", local_path)
            except Exception as e:
                log.warning("[recorder] Pull failed %s: %s", remote, e)
        return pulled

    def _cleanup_remote(self):
        for remote in self._segments:
            try:
                subprocess.run(
                    ["adb", "-s", self.device_id, "shell", f"rm -f {remote}"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass


class ScreenRecorder:
    """Manages recording sessions for multiple devices."""

    def __init__(self, output_dir: str = _RECORDINGS_DIR):
        self.output_dir = output_dir
        self._sessions: Dict[str, RecordingSession] = {}
        self._lock = threading.Lock()

    def start_recording(self, device_id: str,
                        max_segment_sec: int = 180) -> bool:
        with self._lock:
            if device_id in self._sessions and self._sessions[device_id].is_running:
                return True
            session = RecordingSession(
                device_id, self.output_dir, max_segment_sec)
            self._sessions[device_id] = session
        return session.start()

    def stop_recording(self, device_id: str) -> list[str]:
        with self._lock:
            session = self._sessions.pop(device_id, None)
        if session:
            return session.stop()
        return []

    def is_recording(self, device_id: str) -> bool:
        session = self._sessions.get(device_id)
        return session.is_running if session else False

    def get_status(self, device_id: str) -> Optional[dict]:
        session = self._sessions.get(device_id)
        if not session:
            return None
        return {
            "device_id": device_id,
            "recording": session.is_running,
            "duration_sec": round(session.duration_sec, 1),
            "segments": len(session._segments),
        }

    def all_status(self) -> list:
        return [
            self.get_status(did) for did in list(self._sessions.keys())
            if self.get_status(did)
        ]

    def list_recordings(self) -> list[dict]:
        """List all saved recording files."""
        if not os.path.isdir(self.output_dir):
            return []
        files = []
        for f in sorted(os.listdir(self.output_dir), reverse=True):
            if f.endswith(".mp4"):
                fp = os.path.join(self.output_dir, f)
                files.append({
                    "filename": f,
                    "size_mb": round(os.path.getsize(fp) / 1048576, 2),
                    "created": time.ctime(os.path.getctime(fp)),
                })
        return files

    def stop_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.stop()


_recorder_instance: Optional[ScreenRecorder] = None
_rec_lock = threading.Lock()


def get_screen_recorder() -> ScreenRecorder:
    global _recorder_instance
    if _recorder_instance is None:
        with _rec_lock:
            if _recorder_instance is None:
                _recorder_instance = ScreenRecorder()
    return _recorder_instance
