# -*- coding: utf-8 -*-
"""
VPN 健康监控 — 定期检查 VPN 连接状态，自动重连，IP 泄露检测。

集成到 HealthMonitor 体系:
  - 每次设备健康检查时同步检查 VPN 状态
  - VPN 断开 → 自动重连 → 验证 IP → 成功则继续，失败则暂停任务
  - VPN 重连失败 3 次 → 报警 + 暂停该设备所有任务
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.utils.subprocess_text import run as _sp_run_text

log = logging.getLogger(__name__)

_MAX_RECONNECT_ATTEMPTS = 3
_RECONNECT_COOLDOWN = 30
_IP_CHECK_TIMEOUT = 10


@dataclass
class VPNHealthState:
    """Per-device VPN health tracking."""
    device_id: str = ""
    last_check: float = 0.0
    connected: bool = False
    verified_ip: str = ""
    expected_country: str = ""
    consecutive_failures: int = 0
    last_reconnect_attempt: float = 0.0
    paused: bool = False
    history: List[dict] = field(default_factory=list)


class VPNHealthMonitor:
    """Monitors VPN health across all devices with VPN configured.

    Two operation modes controlled by ``auto_reconnect``:
      - **report-only** (default): query VPN status, record metrics, no side-effects.
      - **auto-reconnect**: actively reconnect VPN when down (only for devices
        with an active task or when explicitly requested).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._states: Dict[str, VPNHealthState] = {}
        self._expected_countries: Dict[str, str] = {}
        self._global_auto_reconnect: bool = False

    @property
    def auto_reconnect_enabled(self) -> bool:
        return self._global_auto_reconnect

    @auto_reconnect_enabled.setter
    def auto_reconnect_enabled(self, value: bool):
        self._global_auto_reconnect = bool(value)
        log.info("[VPN健康] 全局自动重连已%s", "启用" if value else "禁用")

    def set_expected_country(self, device_id: str, country: str):
        """Set the expected VPN country for a device."""
        self._expected_countries[device_id] = country.lower()

    def check_device(self, device_id: str,
                     manager=None,
                     allow_reconnect: bool = False) -> dict:
        """Check VPN health for a device. Returns status dict.

        Args:
            allow_reconnect: When True, will attempt VPN reconnection if down.
                             When False (default), only reports status — no
                             side-effects on the device.
        """
        state = self._get_state(device_id)
        now = time.time()

        result = {
            "device_id": device_id,
            "vpn_configured": False,
            "connected": False,
            "ip_verified": False,
            "action_taken": None,
        }

        try:
            from .vpn_manager import get_vpn_manager
            vm = get_vpn_manager()
            if not vm.current_config:
                return result
            result["vpn_configured"] = True
        except Exception:
            return result

        vpn_status = vm.status(device_id)
        state.last_check = now
        state.connected = vpn_status.connected

        may_reconnect = allow_reconnect or self._global_auto_reconnect

        if vpn_status.connected:
            state.consecutive_failures = 0

            ip_ok = self._verify_ip(device_id, manager)
            result["connected"] = True
            result["ip_verified"] = ip_ok

            if not ip_ok:
                self._record_event(state, "ip_leak",
                                   "VPN connected but IP mismatch")
                result["action_taken"] = "ip_leak_detected"
                if may_reconnect:
                    self._try_reconnect(device_id, vm, state)

            if state.paused:
                state.paused = False
                self._resume_tasks(device_id)
                result["action_taken"] = "tasks_resumed"

        else:
            state.consecutive_failures += 1
            self._record_event(state, "disconnected",
                               f"failure #{state.consecutive_failures}")

            if not may_reconnect:
                result["action_taken"] = "report_only"
            elif state.consecutive_failures >= _MAX_RECONNECT_ATTEMPTS:
                if not state.paused:
                    state.paused = True
                    self._pause_tasks(device_id)
                    self._alert(device_id, "critical",
                                f"VPN 连续失败 {state.consecutive_failures} 次，"
                                "任务已暂停")
                    result["action_taken"] = "tasks_paused"
            else:
                if now - state.last_reconnect_attempt >= _RECONNECT_COOLDOWN:
                    success = self._try_reconnect(device_id, vm, state)
                    result["action_taken"] = ("reconnected" if success
                                              else "reconnect_failed")

        with self._lock:
            self._states[device_id] = state

        return result

    def _try_reconnect(self, device_id: str, vm, state: VPNHealthState) -> bool:
        """
        Escalated VPN reconnection:
          Level 1: Soft reconnect via VPN manager
          Level 2: Force-stop and restart V2RayNG
          Level 3: Full VPN app reinstall / config reset
        """
        level = min(state.consecutive_failures + 1, 3)
        state.last_reconnect_attempt = time.time()
        log.info("[VPN健康] %s: 尝试重连 (L%d, 第%d次)...",
                 device_id[:8], level, state.consecutive_failures + 1)

        try:
            if level == 1:
                success = vm.ensure_connected(device_id)
            elif level == 2:
                self._force_restart_vpn_app(device_id)
                time.sleep(5)
                success = vm.ensure_connected(device_id)
            else:
                self._force_restart_vpn_app(device_id)
                time.sleep(3)
                if vm.current_config:
                    from .vpn_manager import setup_vpn
                    status = setup_vpn(device_id, vm.current_config)
                    success = status.connected
                else:
                    success = vm.ensure_connected(device_id)

            if success:
                state.connected = True
                state.consecutive_failures = 0
                self._record_event(state, "reconnected",
                                   f"L{level} 重连成功")
                log.info("[VPN健康] %s: L%d 重连成功", device_id[:8], level)
                return True
            else:
                self._record_event(state, "reconnect_failed",
                                   f"L{level} 重连失败")
                log.warning("[VPN健康] %s: L%d 重连失败",
                            device_id[:8], level)
                return False
        except Exception as e:
            self._record_event(state, "reconnect_error", str(e))
            log.error("[VPN健康] %s: L%d 重连异常: %s",
                      device_id[:8], level, e)
            return False

    @staticmethod
    def _force_restart_vpn_app(device_id: str):
        """Force-stop and restart V2RayNG using safe am-start (no monkey)."""
        try:
            _sp_run_text(
                ["adb", "-s", device_id, "shell",
                 "am", "force-stop", "com.v2ray.ang"],
                capture_output=True, timeout=10)
            time.sleep(2)
            _sp_run_text(
                ["adb", "-s", device_id, "shell",
                 "settings", "put", "system", "accelerometer_rotation", "0"],
                capture_output=True, timeout=5)
            _sp_run_text(
                ["adb", "-s", device_id, "shell",
                 "settings", "put", "system", "user_rotation", "0"],
                capture_output=True, timeout=5)
            _sp_run_text(
                ["adb", "-s", device_id, "shell",
                 "am", "start", "-n",
                 "com.v2ray.ang/com.v2ray.ang.ui.MainActivity"],
                capture_output=True, timeout=10)
        except Exception:
            pass

    def _verify_ip(self, device_id: str, manager=None) -> bool:
        """Verify the device's IP matches expected VPN country."""
        expected = self._expected_countries.get(device_id)
        if not expected:
            return True

        try:
            from .geo_check import check_device_geo
            result = check_device_geo(device_id, expected, manager)
            if result.matches:
                return True
            log.warning("[VPN健康] %s: IP 泄露! 期望=%s, 实际=%s (IP=%s)",
                        device_id[:8], expected,
                        result.detected_country, result.public_ip)
            return False
        except Exception as e:
            log.debug("[VPN健康] IP 验证跳过: %s", e)
            return True

    def _pause_tasks(self, device_id: str):
        """Pause all pending/running tasks on this device."""
        try:
            from src.host.worker_pool import get_worker_pool
            pool = get_worker_pool()
            status = pool.get_status()
            for task_id, did in status.get("active_tasks", {}).items():
                if did == device_id:
                    pool.cancel_task(task_id)
                    log.info("[VPN健康] 取消任务 %s (VPN故障)", task_id[:8])
        except Exception as e:
            log.debug("[VPN健康] 暂停任务失败: %s", e)

    def _resume_tasks(self, device_id: str):
        """Log that tasks can resume (actual re-queue happens via HealthMonitor)."""
        log.info("[VPN健康] 设备 %s VPN 恢复，任务可继续", device_id[:8])
        self._alert(device_id, "info", "VPN 已恢复，任务恢复正常")

    def _alert(self, device_id: str, level: str, message: str):
        try:
            from src.host.alert_message_context import approximate_english_message
            from src.host.health_monitor import metrics

            t_en = approximate_english_message(message)
            metrics.add_alert(
                level,
                device_id,
                "",
                alert_code="VPN_HEALTH",
                params={"text": message, "text_en": t_en},
            )
        except Exception:
            pass

    def _record_event(self, state: VPNHealthState, event_type: str,
                      details: str):
        state.history.append({
            "type": event_type,
            "details": details,
            "ts": time.time(),
        })
        if len(state.history) > 50:
            state.history = state.history[-25:]

    def _get_state(self, device_id: str) -> VPNHealthState:
        with self._lock:
            if device_id not in self._states:
                self._states[device_id] = VPNHealthState(device_id=device_id)
            return self._states[device_id]

    def get_status(self) -> Dict[str, dict]:
        """Return VPN health status for all monitored devices."""
        with self._lock:
            result = {}
            for did, state in self._states.items():
                result[did] = {
                    "connected": state.connected,
                    "verified_ip": state.verified_ip,
                    "consecutive_failures": state.consecutive_failures,
                    "paused": state.paused,
                    "last_check": state.last_check,
                    "history_count": len(state.history),
                    "recent_events": state.history[-5:],
                }
            return result

    def is_vpn_healthy(self, device_id: str) -> bool:
        """Quick check if VPN is healthy for a device."""
        with self._lock:
            state = self._states.get(device_id)
            if not state:
                return True
            return state.connected and not state.paused


_vpn_monitor: Optional[VPNHealthMonitor] = None
_vpn_lock = threading.Lock()


def get_vpn_health_monitor() -> VPNHealthMonitor:
    global _vpn_monitor
    if _vpn_monitor is None:
        with _vpn_lock:
            if _vpn_monitor is None:
                _vpn_monitor = VPNHealthMonitor()
    return _vpn_monitor
