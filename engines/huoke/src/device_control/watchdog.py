"""
Device Watchdog — self-healing system for device and app failures.

Monitors:
  - Device connectivity (ADB online status)
  - App foreground state (crash detection)
  - Network connectivity on device
  - UI responsiveness (frozen screen detection)
  - CAPTCHA/verification detection

Recovery Actions:
  - Reconnect ADB for dropped connections
  - Restart crashed apps
  - Toggle airplane mode for network issues
  - Press back/home to escape stuck screens
  - Alert human for CAPTCHA requiring manual intervention
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from src.utils.subprocess_text import run as _sp_run_text

log = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"


class FailureType(str, Enum):
    DEVICE_OFFLINE = "device_offline"
    APP_CRASH = "app_crash"
    NETWORK_DOWN = "network_down"
    UI_FROZEN = "ui_frozen"
    CAPTCHA_DETECTED = "captcha_detected"
    UNKNOWN = "unknown"


@dataclass
class DeviceHealth:
    device_id: str
    status: HealthStatus = HealthStatus.HEALTHY
    adb_online: bool = True
    u2_responsive: bool = True
    network_ok: bool = True
    current_app: str = ""
    expected_app: str = ""
    last_check: float = 0.0
    consecutive_failures: int = 0
    last_failure: str = ""
    recovery_attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "status": self.status.value,
            "adb_online": self.adb_online,
            "u2_responsive": self.u2_responsive,
            "network_ok": self.network_ok,
            "current_app": self.current_app,
            "expected_app": self.expected_app,
            "consecutive_failures": self.consecutive_failures,
            "recovery_attempts": self.recovery_attempts,
        }


@dataclass
class RecoveryAction:
    failure_type: FailureType
    action: str
    success: bool = False
    timestamp: float = 0.0
    details: str = ""


CAPTCHA_INDICATORS = [
    "recaptcha", "captcha", "verify", "robot",
    "security check", "verification", "人机验证",
    "验证码", "安全验证", "I'm not a robot",
    "Select all", "Pick the",
]


class DeviceWatchdog:
    """
    Monitors device health and performs automatic recovery.

    Usage:
        watchdog = DeviceWatchdog()
        watchdog.watch("R8CIFUBIOVCIUW5H", expected_app="org.telegram.messenger")
        watchdog.start()
    """

    _DEGRADED_AFTER_CYCLES = 10
    _DEGRADED_SKIP_RATIO = 5

    def __init__(self, check_interval: float = 30.0,
                 max_recovery_attempts: int = 3):
        self._devices: Dict[str, DeviceHealth] = {}
        self._check_interval = check_interval
        self._max_recovery = max_recovery_attempts
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._on_captcha: Optional[Callable] = None
        self._recovery_log: List[RecoveryAction] = []
        self._offline_cycles: Dict[str, int] = {}
        self._cycle_counter: int = 0

    def watch(self, device_id: str, expected_app: str = ""):
        with self._lock:
            if device_id in self._devices:
                return
            self._devices[device_id] = DeviceHealth(
                device_id=device_id, expected_app=expected_app,
            )
            self._offline_cycles.pop(device_id, None)

    def unwatch(self, device_id: str):
        with self._lock:
            self._devices.pop(device_id, None)

    def set_expected_app(self, device_id: str, package: str):
        if device_id in self._devices:
            self._devices[device_id].expected_app = package

    def on_captcha(self, callback: Callable):
        """Register callback for CAPTCHA detection (human intervention needed)."""
        self._on_captcha = callback

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop,
                                         daemon=True, name="watchdog")
        self._thread.start()
        log.info("Watchdog started (interval=%.0fs)", self._check_interval)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _monitor_loop(self):
        while self._running:
            self._cycle_counter += 1
            with self._lock:
                devices = list(self._devices.values())

            for health in devices:
                did = health.device_id
                off_cycles = self._offline_cycles.get(did, 0)
                if off_cycles >= self._DEGRADED_AFTER_CYCLES:
                    if self._cycle_counter % self._DEGRADED_SKIP_RATIO != 0:
                        continue
                try:
                    self._check_device(health)
                except Exception as e:
                    log.error("Watchdog check failed for %s: %s",
                              health.device_id, e)

            time.sleep(self._check_interval)

    def _check_device(self, health: DeviceHealth):
        """Run health checks and trigger recovery if needed."""
        health.last_check = time.time()

        health.adb_online = self._check_adb(health.device_id)
        if not health.adb_online:
            health.status = HealthStatus.OFFLINE
            health.consecutive_failures += 1
            health.last_failure = FailureType.DEVICE_OFFLINE.value
            self._offline_cycles[health.device_id] = \
                self._offline_cycles.get(health.device_id, 0) + 1
            self._try_recover(health, FailureType.DEVICE_OFFLINE)
            return

        self._offline_cycles.pop(health.device_id, None)
        health.u2_responsive = self._check_u2(health.device_id)
        if not health.u2_responsive:
            health.status = HealthStatus.DEGRADED
            health.consecutive_failures += 1
            health.last_failure = FailureType.UI_FROZEN.value
            self._try_recover(health, FailureType.UI_FROZEN)
            return

        # Check 3: Current app
        health.current_app = self._get_current_app(health.device_id)
        if health.expected_app and health.current_app != health.expected_app:
            # Check for CAPTCHA first
            if self._detect_captcha(health.device_id):
                health.status = HealthStatus.DEGRADED
                health.last_failure = FailureType.CAPTCHA_DETECTED.value
                self._handle_captcha(health)
                return
            # App crash
            health.consecutive_failures += 1
            health.last_failure = FailureType.APP_CRASH.value
            self._try_recover(health, FailureType.APP_CRASH)
            return

        # Check 4: Network
        health.network_ok = self._check_network(health.device_id)
        if not health.network_ok:
            health.status = HealthStatus.DEGRADED
            health.consecutive_failures += 1
            health.last_failure = FailureType.NETWORK_DOWN.value
            self._try_recover(health, FailureType.NETWORK_DOWN)
            return

        # All good
        health.status = HealthStatus.HEALTHY
        health.consecutive_failures = 0

    # ── Health Checks ─────────────────────────────────────────────────────

    @property
    def _adb(self) -> str:
        try:
            from .device_manager import get_device_manager
            return getattr(get_device_manager(), 'adb_path', 'adb')
        except Exception as e:
            log.debug("watchdog 获取adb路径失败: %s", e)
            return "adb"

    def _check_adb(self, device_id: str) -> bool:
        try:
            result = _sp_run_text(
                [self._adb, "-s", device_id, "shell", "echo", "ok"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0 and "ok" in result.stdout
        except Exception as e:
            log.debug("watchdog ADB检查失败 %s: %s", device_id, e)
            return False

    def _check_u2(self, device_id: str) -> bool:
        try:
            from .device_manager import get_device_manager
            d = get_device_manager().get_u2(device_id)
            if d is None:
                return False
            info = d.info
            return info is not None
        except Exception as e:
            log.debug("watchdog u2检查失败 %s: %s", device_id, e)
            return False

    def _get_current_app(self, device_id: str) -> str:
        try:
            from .device_manager import get_device_manager
            d = get_device_manager().get_u2(device_id)
            if d:
                return d.app_current().get("package", "")
        except Exception as e:
            log.debug("watchdog 获取当前应用失败 %s: %s", device_id, e)
        return ""

    def _check_network(self, device_id: str) -> bool:
        # 2026-05-04: ping 8.8.8.8 在 911proxy 等 Per-App SOCKS 代理设备上
        # adb shell (uid=2000) 走默认路由不通,但 FB App 走代理通—— watchdog
        # 误报触发 _recover_network(切飞行模式 8s),严重干扰 task 跑期间的
        # dump_hierarchy 与 FB 网络. PR #157 commit 3 已为 preflight 加 VPN
        # HttpProxy + Android IS_VALIDATED 双源兜底,这里直接 short-circuit
        # 跳过 ping 误判,避免 watchdog 把 task 设备搞炸.
        return True

    def _detect_captcha(self, device_id: str) -> bool:
        """Check if a CAPTCHA/verification screen is visible."""
        try:
            from .device_manager import get_device_manager
            d = get_device_manager().get_u2(device_id)
            if not d:
                return False
            xml = d.dump_hierarchy()
            xml_lower = xml.lower()
            return any(ind in xml_lower for ind in CAPTCHA_INDICATORS)
        except Exception as e:
            log.debug("watchdog CAPTCHA检测失败 %s: %s", device_id, e)
            return False

    # ── Recovery Actions ──────────────────────────────────────────────────

    def _try_recover(self, health: DeviceHealth, failure: FailureType):
        if health.recovery_attempts >= self._max_recovery:
            health.status = HealthStatus.UNHEALTHY
            self._emit("watchdog.recovery_exhausted",
                        device_id=health.device_id, failure=failure.value)
            return

        # 获取设备级互斥锁，避免与 HealthMonitor 竞争
        try:
            from src.host.shared import get_device_recovery_lock
            lock = get_device_recovery_lock(health.device_id)
            if not lock.acquire(blocking=False):
                log.debug("设备 %s 恢复锁已被占用(HealthMonitor)，跳过", health.device_id)
                return
        except ImportError:
            lock = None

        try:
            self._try_recover_inner(health, failure)
        finally:
            if lock is not None:
                lock.release()

    def _try_recover_inner(self, health: DeviceHealth, failure: FailureType):
        health.recovery_attempts += 1
        log.info("Watchdog: recovering %s from %s (attempt %d/%d)",
                 health.device_id, failure.value,
                 health.recovery_attempts, self._max_recovery)

        success = False
        action_desc = ""

        if failure == FailureType.DEVICE_OFFLINE:
            action_desc = "reconnect_adb"
            success = self._recover_adb(health.device_id)

        elif failure == FailureType.APP_CRASH:
            action_desc = "restart_app"
            success = self._recover_app(health.device_id, health.expected_app)

        elif failure == FailureType.NETWORK_DOWN:
            action_desc = "toggle_airplane"
            success = self._recover_network(health.device_id)

        elif failure == FailureType.UI_FROZEN:
            action_desc = "force_home"
            success = self._recover_frozen(health.device_id)

        action = RecoveryAction(
            failure_type=failure, action=action_desc,
            success=success, timestamp=time.time(),
            details=f"attempt {health.recovery_attempts}",
        )
        self._recovery_log.append(action)
        if len(self._recovery_log) > 200:
            self._recovery_log = self._recovery_log[-100:]

        self._emit("watchdog.recovery_attempt",
                    device_id=health.device_id, failure=failure.value,
                    action=action_desc, success=success)

        if success:
            health.consecutive_failures = 0
            health.recovery_attempts = 0

    def _recover_adb(self, device_id: str) -> bool:
        adb = self._adb
        try:
            is_wifi = bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", device_id))
            if is_wifi:
                _sp_run_text([adb, "connect", device_id],
                             capture_output=True, timeout=10)
            else:
                _sp_run_text([adb, "reconnect", device_id],
                             capture_output=True, timeout=10)
            time.sleep(3)
            return self._check_adb(device_id)
        except Exception as e:
            log.debug("watchdog ADB恢复失败 %s: %s", device_id, e)
            return False

    def _recover_app(self, device_id: str, package: str) -> bool:
        if not package:
            return False
        adb = self._adb
        try:
            _sp_run_text([adb, "-s", device_id, "shell",
                          "am", "force-stop", package],
                         capture_output=True, timeout=5)
            time.sleep(2)
            _sp_run_text([adb, "-s", device_id, "shell",
                          "monkey", "-p", package,
                          "-c", "android.intent.category.LAUNCHER", "1"],
                         capture_output=True, timeout=5)
            time.sleep(4)
            return self._get_current_app(device_id) == package
        except Exception as e:
            log.debug("watchdog 应用恢复失败 %s: %s", device_id, e)
            return False

    def _recover_network(self, device_id: str) -> bool:
        adb = self._adb
        try:
            _sp_run_text([adb, "-s", device_id, "shell",
                          "cmd", "connectivity", "airplane-mode", "enable"],
                         capture_output=True, timeout=5)
            time.sleep(3)
            _sp_run_text([adb, "-s", device_id, "shell",
                          "cmd", "connectivity", "airplane-mode", "disable"],
                         capture_output=True, timeout=5)
            time.sleep(5)
            return self._check_network(device_id)
        except Exception as e:
            log.debug("watchdog 网络恢复失败 %s: %s", device_id, e)
            return False

    def _recover_frozen(self, device_id: str) -> bool:
        adb = self._adb
        try:
            for _ in range(3):
                _sp_run_text([adb, "-s", device_id, "shell",
                              "input", "keyevent", "KEYCODE_BACK"],
                             capture_output=True, timeout=3)
                time.sleep(0.5)
            _sp_run_text([adb, "-s", device_id, "shell",
                          "input", "keyevent", "KEYCODE_HOME"],
                         capture_output=True, timeout=3)
            time.sleep(2)
            return self._check_u2(device_id)
        except Exception as e:
            log.debug("watchdog UI冻结恢复失败 %s: %s", device_id, e)
            return False

    def _handle_captcha(self, health: DeviceHealth):
        """Handle CAPTCHA detection — alert human."""
        log.warning("CAPTCHA detected on %s! Manual intervention needed.",
                    health.device_id)
        self._emit("watchdog.captcha_detected", device_id=health.device_id,
                    app=health.current_app)
        if self._on_captcha:
            try:
                self._on_captcha(health.device_id, health.current_app)
            except Exception as e:
                log.error("CAPTCHA callback failed: %s", e)

    # ── Status ────────────────────────────────────────────────────────────

    def get_health(self, device_id: str) -> Optional[DeviceHealth]:
        return self._devices.get(device_id)

    def all_health(self) -> Dict[str, dict]:
        return {did: h.to_dict() for did, h in self._devices.items()}

    def recent_recoveries(self, limit: int = 20) -> List[dict]:
        return [
            {
                "failure": a.failure_type.value,
                "action": a.action,
                "success": a.success,
                "timestamp": a.timestamp,
                "details": a.details,
            }
            for a in self._recovery_log[-limit:]
        ]

    def _emit(self, event_type: str, **data):
        try:
            from ..workflow.event_bus import get_event_bus
            get_event_bus().emit_simple(event_type, source="watchdog", **data)
        except Exception as e:
            log.debug("watchdog 事件发送失败 %s: %s", event_type, e)


# ── Singleton ─────────────────────────────────────────────────────────────

_watchdog: Optional[DeviceWatchdog] = None
_watchdog_lock = threading.Lock()


def get_watchdog() -> DeviceWatchdog:
    global _watchdog
    if _watchdog is None:
        with _watchdog_lock:
            if _watchdog is None:
                _watchdog = DeviceWatchdog()
    return _watchdog
