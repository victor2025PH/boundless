# -*- coding: utf-8 -*-
"""
Macro Recorder & Player — record user interactions and replay on devices.

Macro format (JSON):
  {
    "name": "macro_name",
    "created_at": 1711234567.89,
    "screen_width": 1080,
    "screen_height": 2400,
    "steps": [
      {"type": "tap", "x": 540, "y": 1200, "delay_ms": 500},
      {"type": "swipe", "x1": 540, "y1": 1800, "x2": 540, "y2": 600,
       "duration": 300, "delay_ms": 1000},
      {"type": "key", "keycode": 4, "delay_ms": 200},
      {"type": "text", "text": "hello", "delay_ms": 500},
      {"type": "wait", "delay_ms": 2000}
    ]
  }

Coordinate scaling: macros recorded at one resolution are automatically
scaled to the target device's actual resolution.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_MACROS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "macros"
)


class MacroPlayer:
    """Replays a macro on one or more devices."""

    def __init__(self, manager=None):
        self._manager = manager
        self._playing: Dict[str, bool] = {}
        self._paused: Dict[str, bool] = {}
        self._progress: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def play(self, macro: dict, device_id: str,
             speed: float = 1.0, repeat: int = 1,
             callback=None) -> bool:
        """Play a macro on a single device. Runs in a new thread."""
        with self._lock:
            if self._playing.get(device_id):
                return False
            self._playing[device_id] = True

        t = threading.Thread(
            target=self._play_thread,
            args=(macro, device_id, speed, repeat, callback),
            daemon=True,
        )
        t.start()
        return True

    def play_group(self, macro: dict, device_ids: List[str],
                   speed: float = 1.0, repeat: int = 1) -> dict:
        """Play a macro on multiple devices concurrently."""
        results = {}
        for did in device_ids:
            ok = self.play(macro, did, speed, repeat)
            results[did[:12]] = "started" if ok else "already_playing"
        return results

    def stop(self, device_id: str):
        with self._lock:
            self._playing[device_id] = False
            self._paused.pop(device_id, None)

    def stop_all(self):
        with self._lock:
            for did in list(self._playing.keys()):
                self._playing[did] = False
            self._paused.clear()

    def pause(self, device_id: str):
        with self._lock:
            self._paused[device_id] = True

    def resume(self, device_id: str):
        with self._lock:
            self._paused[device_id] = False

    def is_playing(self, device_id: str) -> bool:
        return self._playing.get(device_id, False)

    def is_paused(self, device_id: str) -> bool:
        return self._paused.get(device_id, False)

    def get_progress(self, device_id: str) -> Optional[dict]:
        return self._progress.get(device_id)

    def all_progress(self) -> dict:
        return dict(self._progress)

    def _play_thread(self, macro: dict, device_id: str,
                     speed: float, repeat: int, callback):
        try:
            src_w = macro.get("screen_width", 1080)
            src_h = macro.get("screen_height", 2400)
            steps = macro.get("steps", [])
            macro_name = macro.get("name", "unnamed")

            tgt_w, tgt_h = self._get_screen_size(device_id)
            scale_x = tgt_w / src_w if src_w else 1.0
            scale_y = tgt_h / src_h if src_h else 1.0

            total_steps = len(steps)

            for rep in range(repeat):
                if not self._playing.get(device_id):
                    break
                log.info("[macro] Playing on %s (rep %d/%d, %d steps)",
                         device_id[:8], rep + 1, repeat, total_steps)

                for step_idx, step in enumerate(steps):
                    if not self._playing.get(device_id):
                        break

                    while self._paused.get(device_id) and self._playing.get(device_id):
                        time.sleep(0.1)

                    self._progress[device_id] = {
                        "macro": macro_name,
                        "step": step_idx + 1,
                        "total": total_steps,
                        "repeat": rep + 1,
                        "total_repeats": repeat,
                        "current_type": step.get("type", ""),
                        "current_detail": self._step_summary(step),
                        "paused": self._paused.get(device_id, False),
                        "percent": round((step_idx / total_steps) * 100),
                    }

                    delay = step.get("delay_ms", 0) / speed
                    if delay > 0:
                        time.sleep(delay / 1000.0)

                    stype = step.get("type", "")
                    self._execute_step(device_id, stype, step,
                                      scale_x, scale_y)

                if rep < repeat - 1:
                    time.sleep(0.5)

        except Exception as e:
            log.error("[macro] Playback error on %s: %s", device_id[:8], e)
        finally:
            with self._lock:
                self._playing[device_id] = False
                self._paused.pop(device_id, None)
                self._progress.pop(device_id, None)
            if callback:
                try:
                    callback(device_id, "completed")
                except Exception:
                    pass

    @staticmethod
    def _step_summary(step: dict) -> str:
        stype = step.get("type", "")
        if stype == "tap":
            return f"点击 ({step.get('x',0)},{step.get('y',0)})"
        elif stype == "swipe":
            return f"滑动 ({step.get('x1',0)},{step.get('y1',0)})→({step.get('x2',0)},{step.get('y2',0)})"
        elif stype == "key":
            return f"按键 {step.get('keycode',0)}"
        elif stype == "text":
            return f"输入 \"{step.get('text','')[:20]}\""
        elif stype == "wait_for":
            return f"等待 \"{step.get('target','')}\""
        elif stype == "wait_gone":
            return f"等消失 \"{step.get('target','')}\""
        elif stype == "tap_element":
            return f"点击元素 \"{step.get('target','')}\""
        elif stype == "screenshot_check":
            return "异常检测"
        elif stype == "wait":
            return f"等待 {step.get('delay_ms',0)}ms"
        return stype

    def _execute_step(self, device_id: str, stype: str, step: dict,
                      sx: float, sy: float):
        if not self._manager:
            return
        if stype == "tap":
            x = int(step["x"] * sx)
            y = int(step["y"] * sy)
            self._manager.input_tap(device_id, x, y)
        elif stype == "swipe":
            x1 = int(step["x1"] * sx)
            y1 = int(step["y1"] * sy)
            x2 = int(step["x2"] * sx)
            y2 = int(step["y2"] * sy)
            dur = int(step.get("duration", 300))
            self._manager.input_swipe(device_id, x1, y1, x2, y2, dur)
        elif stype == "key":
            self._manager.input_key(device_id, step.get("keycode", 0))
        elif stype == "text":
            self._manager.input_text(device_id, step.get("text", ""))
        elif stype == "wait":
            pass
        elif stype == "wait_for":
            self._wait_for_element(device_id, step)
        elif stype == "wait_gone":
            self._wait_for_gone(device_id, step)
        elif stype == "tap_element":
            self._tap_element(device_id, step, sx, sy)
        elif stype == "screenshot_check":
            self._screenshot_check(device_id, step)

    def _wait_for_element(self, device_id: str, step: dict):
        """Wait until a UI element matching the target text/desc appears."""
        target = step.get("target", "")
        timeout_ms = step.get("timeout_ms", 10000)
        if not target:
            return
        deadline = time.time() + timeout_ms / 1000.0
        poll_interval = 0.5
        while time.time() < deadline and self._playing.get(device_id):
            if self._find_on_screen(device_id, target):
                log.info("[macro] wait_for '%s' found on %s", target, device_id[:8])
                return
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.2, 2.0)
        log.warning("[macro] wait_for '%s' timed out on %s", target, device_id[:8])

    def _wait_for_gone(self, device_id: str, step: dict):
        """Wait until a UI element disappears."""
        target = step.get("target", "")
        timeout_ms = step.get("timeout_ms", 10000)
        if not target:
            return
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline and self._playing.get(device_id):
            if not self._find_on_screen(device_id, target):
                log.info("[macro] wait_gone '%s' gone on %s", target, device_id[:8])
                return
            time.sleep(0.5)
        log.warning("[macro] wait_gone '%s' timed out on %s", target, device_id[:8])

    def _tap_element(self, device_id: str, step: dict,
                     sx: float, sy: float):
        """Find element by text/description and tap its center."""
        target = step.get("target", "")
        timeout_ms = step.get("timeout_ms", 5000)
        if not target:
            return
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline and self._playing.get(device_id):
            coords = self._find_on_screen(device_id, target)
            if coords:
                x, y = coords
                self._manager.input_tap(device_id, x, y)
                log.info("[macro] tap_element '%s' at (%d,%d) on %s",
                         target, x, y, device_id[:8])
                return
            time.sleep(0.5)
        log.warning("[macro] tap_element '%s' not found on %s", target, device_id[:8])

    def _screenshot_check(self, device_id: str, step: dict):
        """Check screen for anomalies before continuing."""
        try:
            from src.behavior.screen_anomaly import get_anomaly_detector
            detector = get_anomaly_detector()
            result = detector.detect_and_recover(device_id, self._manager)
            if result and result.severity.value == "critical":
                log.warning("[macro] Critical anomaly on %s: %s — pausing",
                            device_id[:8], result.description)
                self._playing[device_id] = False
        except Exception:
            pass

    def _find_on_screen(self, device_id: str, target: str):
        """Search for target text in UI hierarchy. Returns center (x,y) or None."""
        try:
            d = self._manager.get_u2_device(device_id) if hasattr(
                self._manager, 'get_u2_device') else None
            if not d:
                return None
            xml = d.dump_hierarchy()
            target_lower = target.lower()
            import re
            for m in re.finditer(
                r'text="([^"]*)".*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
                if target_lower in m.group(1).lower():
                    x = (int(m.group(2)) + int(m.group(4))) // 2
                    y = (int(m.group(3)) + int(m.group(5))) // 2
                    return (x, y)
            for m in re.finditer(
                r'content-desc="([^"]*)".*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
                if target_lower in m.group(1).lower():
                    x = (int(m.group(2)) + int(m.group(4))) // 2
                    y = (int(m.group(3)) + int(m.group(5))) // 2
                    return (x, y)
        except Exception:
            pass
        return None

    def _get_screen_size(self, device_id: str) -> tuple:
        try:
            if self._manager:
                ok, output = self._manager.execute_adb_command(
                    "shell wm size", device_id)
                if ok and "x" in output:
                    parts = output.strip().split()[-1].split("x")
                    return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return 1080, 2400


class MacroStore:
    """Manages macro storage (JSON files)."""

    def __init__(self, macros_dir: str = _MACROS_DIR):
        self.macros_dir = macros_dir
        os.makedirs(macros_dir, exist_ok=True)

    def save(self, macro: dict) -> str:
        """Save a macro (with step normalization), return the filename."""
        name = macro.get("name", f"macro_{int(time.time())}")
        safe_name = "".join(c for c in name if c.isalnum() or c in "_- ").strip()
        if not safe_name:
            safe_name = f"macro_{int(time.time())}"
        filename = f"{safe_name}.json"
        filepath = os.path.join(self.macros_dir, filename)

        macro["name"] = safe_name
        if "created_at" not in macro:
            macro["created_at"] = time.time()

        macro["steps"] = self._normalize_steps(macro.get("steps", []))

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(macro, f, ensure_ascii=False, indent=2)

        log.info("[macro] Saved: %s (%d steps)", filename,
                 len(macro.get("steps", [])))
        return filename

    @staticmethod
    def _normalize_steps(steps: list) -> list:
        """Merge consecutive text inputs, cap delays, remove zero-delay waits."""
        if not steps:
            return steps
        normalized = []
        i = 0
        while i < len(steps):
            step = dict(steps[i])
            if step.get("delay_ms", 0) > 10000:
                step["delay_ms"] = 10000
            if step["type"] == "text":
                merged_text = step.get("text", "")
                total_delay = step.get("delay_ms", 0)
                while (i + 1 < len(steps) and
                       steps[i + 1].get("type") == "text" and
                       steps[i + 1].get("delay_ms", 0) < 200):
                    i += 1
                    merged_text += steps[i].get("text", "")
                if merged_text:
                    step["text"] = merged_text
                    step["delay_ms"] = total_delay
            if step["type"] == "wait" and step.get("delay_ms", 0) < 50:
                i += 1
                continue
            normalized.append(step)
            i += 1
        return normalized

    def load(self, filename: str) -> Optional[dict]:
        filepath = os.path.join(self.macros_dir, filename)
        if not os.path.exists(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def delete(self, filename: str) -> bool:
        filepath = os.path.join(self.macros_dir, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            return True
        return False

    def list_macros(self) -> List[dict]:
        if not os.path.isdir(self.macros_dir):
            return []
        result = []
        for f in sorted(os.listdir(self.macros_dir)):
            if f.endswith(".json"):
                try:
                    macro = self.load(f)
                    if macro:
                        result.append({
                            "filename": f,
                            "name": macro.get("name", f),
                            "steps": len(macro.get("steps", [])),
                            "screen_width": macro.get("screen_width", 0),
                            "screen_height": macro.get("screen_height", 0),
                            "created_at": macro.get("created_at", 0),
                        })
                except Exception:
                    pass
        return result


_store_instance: Optional[MacroStore] = None
_player_instance: Optional[MacroPlayer] = None
_macro_lock = threading.Lock()


def get_macro_store() -> MacroStore:
    global _store_instance
    if _store_instance is None:
        with _macro_lock:
            if _store_instance is None:
                _store_instance = MacroStore()
    return _store_instance


def get_macro_player(manager=None) -> MacroPlayer:
    global _player_instance
    if _player_instance is None:
        with _macro_lock:
            if _player_instance is None:
                _player_instance = MacroPlayer(manager)
    if manager and _player_instance._manager is None:
        _player_instance._manager = manager
    return _player_instance
