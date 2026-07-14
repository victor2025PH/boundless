# -*- coding: utf-8 -*-
"""
设备健康监控 v3 — 掉线自动重连 + 自适应检测间隔。

后台线程定期检查设备连接状态:
- 掉线检测 → 主动 ADB 重连（USB reconnect / WiFi connect）→ EventBus 报警
- u2 连接验证 + atx-agent 深度重连
- 失败任务恢复: 设备重新上线后重新提交中断的任务（所有任务类型）
- 连续掉线计数 → 阈值告警
- 应用状态检查 + 重启
- scrcpy 流掉线清理 + 重连后自动恢复
- 运行中任务掉线立即取消
- 自适应检查间隔: 有掉线设备时加速到 10s，稳定后回退到 60s
- 掉线防抖: connection.health_monitor.disconnect_confirm_rounds（默认 2）— 仅当上一轮为已连接时，需连续 N 次未见 ADB 才确认掉线
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from src.device_control.device_manager import get_device_manager
from src.utils.subprocess_text import run as _sp_run_text

from .device_registry import DEFAULT_DEVICES_YAML, PROJECT_ROOT, config_file, data_file

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_NORMAL = 60
_HEARTBEAT_INTERVAL_FAST = 10
_DISCONNECT_ALERT_THRESHOLD = 3
_MAX_RECONNECT_ATTEMPTS = 3
_WIFI_DEVICE_PATTERN = re.compile(r"^\d{1,3}(\.\d{1,3}){3}:\d+$")

_devices_yaml = Path(DEFAULT_DEVICES_YAML)


def _load_disconnect_confirm_rounds(config_path: str = "") -> int:
    """devices.yaml → connection.health_monitor.disconnect_confirm_rounds，默认 2，范围 1–10。"""
    try:
        import yaml

        p = Path(config_path) if config_path else _devices_yaml
        if not p.is_file():
            return 2
        with open(p, encoding="utf-8") as f:
            c = yaml.safe_load(f) or {}
        hm = (c.get("connection") or {}).get("health_monitor") or {}
        v = int(hm.get("disconnect_confirm_rounds", 2))
        return max(1, min(v, 10))
    except Exception:
        return 2


# POST /preflight/settings 可覆盖；None 表示不覆盖。环境变量优先于 yaml。
_runtime_disconnect_confirm_rounds: Optional[int] = None


def effective_disconnect_confirm_rounds(config_path: str = "") -> int:
    """有效掉线确认轮次：OPENCLAW_DISCONNECT_CONFIRM_ROUNDS → 运行时覆盖 → devices.yaml。"""
    ev = os.environ.get("OPENCLAW_DISCONNECT_CONFIRM_ROUNDS", "").strip()
    if ev:
        try:
            return max(1, min(int(ev), 10))
        except ValueError:
            pass
    global _runtime_disconnect_confirm_rounds
    if _runtime_disconnect_confirm_rounds is not None:
        return max(1, min(int(_runtime_disconnect_confirm_rounds), 10))
    return _load_disconnect_confirm_rounds(config_path or str(_devices_yaml))


def set_runtime_disconnect_confirm_rounds(value: Optional[int]) -> None:
    """运行时设置掉线确认轮次；传入 None 清除覆盖（恢复 env/yaml）。"""
    global _runtime_disconnect_confirm_rounds
    _runtime_disconnect_confirm_rounds = value


def _wifi_recovery_keep_adb_connected() -> bool:
    """Wi-Fi 辅助点掉 USB 弹窗后，是否保持无线 adb connect（双通道冗余，默认 True）。"""
    try:
        import yaml

        if not _devices_yaml.is_file():
            return True
        with open(_devices_yaml, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        c = cfg.get("connection") or {}
        return bool(c.get("wifi_recovery_keep_adb_connected", True))
    except Exception:
        return True

_RECOVERY_LEVELS = [
    {"name": "reconnect",        "max_attempts": 2, "cooldown_sec": 10},
    {"name": "wifi_and_dismiss", "max_attempts": 3, "cooldown_sec": 10},
    {"name": "reset_transport",  "max_attempts": 2, "cooldown_sec": 15},
    {"name": "kill_server",      "max_attempts": 1, "cooldown_sec": 30},
    {"name": "usb_power_cycle",  "max_attempts": 1, "cooldown_sec": 60},
]

_RETRYABLE_TASK_TYPES = {
    "tiktok_", "telegram_", "whatsapp_", "linkedin_",
    "batch_send", "batch_", "auto_",
}

# 设备重连后是否自动恢复中断任务（运行时可通过 POST /preflight/settings 调整；
# 若 task_execution_policy.yaml 中 disable_reconnect_task_recovery: true 则强制关闭）。
_AUTO_RECOVER_TASKS = False


def _effective_auto_recover() -> bool:
    try:
        from src.host.task_policy import policy_blocks_reconnect_recovery
        if policy_blocks_reconnect_recovery():
            return False
    except Exception:
        pass
    return _AUTO_RECOVER_TASKS


class Metrics:
    """轻量内存计数器 + 设备健康评分。"""

    _SNAPSHOT_PATH = None  # 延迟到首次使用时确定，避免模块加载期副作用

    def __init__(self):
        self._lock = threading.Lock()
        self.tasks_total = 0
        self.tasks_success = 0
        self.tasks_failed = 0
        self.tasks_timeout = 0
        self.device_reconnects = 0
        self.uptime_start = time.time()
        self._last_snapshot_save: float = 0.0
        self.last_heartbeat: Optional[str] = None
        self.device_status: Dict[str, dict] = {}
        self.alerts: List[dict] = []
        self._consecutive_disconnects: Dict[str, int] = {}
        self._disconnect_history: Dict[str, List[float]] = {}
        self._u2_reconnect_history: Dict[str, List[float]] = {}
        self._task_results_history: Dict[str, List[bool]] = {}
        self._adb_latency: Dict[str, float] = {}
        self._recovery_events: List[dict] = []
        self._health_score_history: Dict[str, List[dict]] = {}
        self._isolated_devices: Set[str] = set()
        # 2026-05-13: per-task-type 指标（全局成功失败统计）
        self._task_type_stats: Dict[str, Dict[str, int]] = {}  # task_type -> {success, fail}
        # 刷新持久化数据（必须在 _task_type_stats 初始化后进行）
        self._load_persisted_snapshot()
        # Account health tracking for TikTok
        self._account_interaction_results: Dict[str, deque] = {}  # device_id -> deque of (ts, success:bool, rate:float)
        self._account_health_alerts: Dict[str, dict] = {}  # device_id -> {"triggered_at": ts, "reason": str}
        # Facebook POST /facebook/device/{id}/launch 可观测性（由 routers/facebook 上报）
        self.fb_launch_total = 0
        self.fb_launch_remote = 0
        self.fb_launch_remote_probe_fail = 0
        self.fb_launch_worker_cap_warning = 0
        self.fb_launch_steps_ok = 0
        self.fb_launch_steps_failed = 0
        # TikTok POST /tiktok/device/{id}/launch（flow_steps 模式，由 routers/tiktok 上报）
        self.tt_launch_total = 0
        self.tt_launch_remote = 0
        self.tt_launch_steps_ok = 0
        self.tt_launch_steps_failed = 0
        # flow_steps 模式：与 campaign 相同环境变量下因本地离线短路（未向主控/Worker 发 /tasks）
        self.tt_flow_steps_skipped_local_offline = 0
        # TikTok 无 flow_steps 时走 launch-campaign（本地或 Worker-03）
        self.tt_campaign_launch_total = 0
        self.tt_campaign_launch_remote = 0
        self.tt_campaign_launch_ok = 0
        self.tt_campaign_launch_fail = 0
        # campaign 模式：本地 devices.yaml 无该 device_id 行（仍会尝试转发 Worker；本指标仅观测「未走本地 launch-campaign」）
        self.tt_campaign_skipped_no_local_row = 0
        # campaign 模式：本地有设备行但非在线，且 OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE=1 时跳过 Worker
        self.tt_campaign_skipped_local_offline = 0

    def record_fb_device_launch(
        self,
        *,
        is_local_enqueue: bool,
        worker_capabilities_probe_ok: bool,
        had_worker_capabilities_warning: bool,
        steps_ok: int,
        steps_failed: int,
    ) -> None:
        """累计一次 FB device launch，供 /health → metrics 与外部监控抓取。"""
        with self._lock:
            self.fb_launch_total += 1
            if not is_local_enqueue:
                self.fb_launch_remote += 1
                if not worker_capabilities_probe_ok:
                    self.fb_launch_remote_probe_fail += 1
                if had_worker_capabilities_warning:
                    self.fb_launch_worker_cap_warning += 1
            self.fb_launch_steps_ok += max(0, int(steps_ok))
            self.fb_launch_steps_failed += max(0, int(steps_failed))

    def record_tt_device_launch(
        self,
        *,
        is_local_enqueue: bool,
        steps_ok: int,
        steps_failed: int,
    ) -> None:
        """累计一次 TikTok device launch（自定义 flow_steps 路径）。"""
        with self._lock:
            self.tt_launch_total += 1
            if not is_local_enqueue:
                self.tt_launch_remote += 1
            self.tt_launch_steps_ok += max(0, int(steps_ok))
            self.tt_launch_steps_failed += max(0, int(steps_failed))

    def record_tt_campaign_launch(self, *, is_local_enqueue: bool, ok: bool) -> None:
        """累计一次 TikTok launch-campaign（无 flow_steps 的兼容路径）。"""
        with self._lock:
            self.tt_campaign_launch_total += 1
            if not is_local_enqueue:
                self.tt_campaign_launch_remote += 1
            if ok:
                self.tt_campaign_launch_ok += 1
            else:
                self.tt_campaign_launch_fail += 1

    def record_tt_campaign_skip_no_local_device(self) -> None:
        """本地注册表无该设备时计数（行为上仍可能转发 Worker；与 launches 独立便于排查错误 device_id）。"""
        with self._lock:
            self.tt_campaign_skipped_no_local_row += 1

    def record_tt_campaign_skip_local_offline(self) -> None:
        """本地设备非 CONNECTED/BUSY 且环境变量禁止转发 Worker 时的短路计数。"""
        with self._lock:
            self.tt_campaign_skipped_local_offline += 1

    def record_tt_flow_steps_skip_local_offline(self) -> None:
        """flow_steps 模式下同上离线短路计数（与 campaign 分桶）。"""
        with self._lock:
            self.tt_flow_steps_skipped_local_offline += 1

    def inc_task(self, success: bool, timeout: bool = False):
        with self._lock:
            self.tasks_total += 1
            if timeout:
                self.tasks_timeout += 1
            elif success:
                self.tasks_success += 1
            else:
                self.tasks_failed += 1

    def inc_reconnect(self):
        with self._lock:
            self.device_reconnects += 1

    def record_disconnect(self, device_id: str) -> int:
        with self._lock:
            count = self._consecutive_disconnects.get(device_id, 0) + 1
            self._consecutive_disconnects[device_id] = count
            hist = self._disconnect_history.setdefault(device_id, [])
            hist.append(time.time())
            if len(hist) > 100:
                self._disconnect_history[device_id] = hist[-50:]
            return count

    def clear_disconnect(self, device_id: str):
        with self._lock:
            self._consecutive_disconnects.pop(device_id, None)

    def predict_disconnect_risk(self, device_id: str) -> dict:
        """预测设备掉线风险。返回 risk_level (low/medium/high) 和 reason。"""
        now = time.time()
        with self._lock:
            history = list(self._disconnect_history.get(device_id, []))

        if len(history) < 2:
            return {"risk": "low", "reason": "数据不足", "score": 0,
                    "reasons": [], "recent_1h": 0, "recent_10m": 0}

        # 最近1小时掉线次数
        recent_1h = sum(1 for t in history if now - t < 3600)
        # 最近10分钟掉线次数
        recent_10m = sum(1 for t in history if now - t < 600)

        # 掉线间隔趋势（间隔是否在缩短）
        intervals = [history[i + 1] - history[i] for i in range(len(history) - 1)]
        accelerating = False
        if len(intervals) >= 3:
            recent_avg = sum(intervals[-3:]) / 3
            older_avg = sum(intervals[:-3]) / max(len(intervals) - 3, 1)
            accelerating = recent_avg < older_avg * 0.7  # 最近间隔比历史短30%以上

        # 评分
        score = 0
        reasons = []

        if recent_10m >= 3:
            score += 40
            reasons.append(f"10分钟内掉线{recent_10m}次")
        elif recent_10m >= 2:
            score += 25
            reasons.append(f"10分钟内掉线{recent_10m}次")

        if recent_1h >= 5:
            score += 30
            reasons.append(f"1小时内掉线{recent_1h}次")
        elif recent_1h >= 3:
            score += 15
            reasons.append(f"1小时内掉线{recent_1h}次")

        if accelerating:
            score += 30
            reasons.append("掉线频率加速")

        if score >= 50:
            risk = "high"
        elif score >= 25:
            risk = "medium"
        else:
            risk = "low"

        return {"risk": risk, "score": score, "reasons": reasons,
                "recent_1h": recent_1h, "recent_10m": recent_10m}

    def get_disconnect_history_devices(self) -> list:
        """返回所有有掉线历史的设备ID列表。"""
        with self._lock:
            return list(self._disconnect_history.keys())

    def record_u2_reconnect(self, device_id: str):
        with self._lock:
            hist = self._u2_reconnect_history.setdefault(device_id, [])
            hist.append(time.time())
            if len(hist) > 100:
                self._u2_reconnect_history[device_id] = hist[-50:]

    def record_task_result(self, device_id: str, success: bool):
        with self._lock:
            hist = self._task_results_history.setdefault(device_id, [])
            hist.append(success)

    def _load_persisted_snapshot(self) -> None:
        """2026-05-13: 启动时加载持久化的全局计数，实现跨重启累计。"""
        try:
            path = data_file("metrics_snapshot.json")
            if not path.exists():
                return
            with open(path, encoding="utf-8") as f:
                snap = json.load(f)
            # 恢复全局计数
            self.tasks_total = int(snap.get("tasks_total", 0))
            self.tasks_success = int(snap.get("tasks_success", 0))
            self.tasks_failed = int(snap.get("tasks_failed", 0))
            self.tasks_timeout = int(snap.get("tasks_timeout", 0))
            # 恢复 per-type 统计
            for tt, s in (snap.get("task_type_stats") or {}).items():
                self._task_type_stats[tt] = {
                    "success": int(s.get("success", 0)),
                    "fail": int(s.get("fail", 0)),
                }
            logger.info("[metrics] 恢复持久化指标: total=%d, %d 种 task_type",
                        self.tasks_total, len(self._task_type_stats))
        except Exception as e:
            logger.debug("[metrics] 持乣快照加载跳过: %s", e)

    def save_snapshot(self) -> None:
        """2026-05-13: 原子写入指标快照（临时文件再重命名，避免写半损坏）。"""
        try:
            path = data_file("metrics_snapshot.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                snap = {
                    "saved_at": time.time(),
                    "tasks_total": self.tasks_total,
                    "tasks_success": self.tasks_success,
                    "tasks_failed": self.tasks_failed,
                    "tasks_timeout": self.tasks_timeout,
                    "task_type_stats": dict(self._task_type_stats),
                }
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            tmp.replace(path)
        except Exception as e:
            logger.debug("[metrics] 快照写入失败: %s", e)

    def record_task_type_result(self, task_type: str, success: bool) -> None:
        """2026-05-13: 记录每种 task_type 的全局成功失败数，供 dashboard 展示和阅值告警。"""
        with self._lock:
            s = self._task_type_stats.setdefault(task_type, {"success": 0, "fail": 0})
            if success:
                s["success"] += 1
            else:
                s["fail"] += 1

    def get_task_type_stats(self) -> Dict[str, dict]:
        """2026-05-13: 返回每种 task_type 的成功率快照。"""
        with self._lock:
            result = {}
            for tt, s in self._task_type_stats.items():
                total = s["success"] + s["fail"]
                result[tt] = {
                    "success": s["success"],
                    "fail": s["fail"],
                    "total": total,
                    "success_rate": round(s["success"] / total, 3) if total else None,
                }
            return result

    def record_adb_latency(self, device_id: str, latency_ms: float):
        with self._lock:
            self._adb_latency[device_id] = latency_ms

    def device_health_score(self, device_id: str) -> dict:
        """Compute health score (0-100) for a device across multiple dimensions."""
        now = time.time()
        cutoff_24h = now - 86400

        with self._lock:
            disconnects_24h = len([
                t for t in self._disconnect_history.get(device_id, [])
                if t > cutoff_24h
            ])
            u2_reconnects_24h = len([
                t for t in self._u2_reconnect_history.get(device_id, [])
                if t > cutoff_24h
            ])
            task_history = list(self._task_results_history.get(device_id, []))
            latency = self._adb_latency.get(device_id, 0)
            status = self.device_status.get(device_id, {})

        stability = _score_stability(disconnects_24h)
        responsiveness = _score_responsiveness(latency)
        task_success = _score_task_success(task_history[-20:])
        u2_health = _score_u2_health(u2_reconnects_24h)

        is_online = status.get("status") == "connected"
        if not is_online:
            stability = max(0, stability - 30)

        total = int(
            stability * 0.40
            + responsiveness * 0.25
            + task_success * 0.25
            + u2_health * 0.10
        )

        return {
            "total": total,
            "stability": stability,
            "responsiveness": responsiveness,
            "task_success": task_success,
            "u2_health": u2_health,
            "online": is_online,
            "disconnects_24h": disconnects_24h,
            "latency_ms": round(latency, 1),
        }

    def all_health_scores(self) -> Dict[str, dict]:
        """Compute health scores for all known devices."""
        device_ids = set(self.device_status.keys())
        return {did: self.device_health_score(did) for did in device_ids}

    def best_device(self, candidate_ids: Optional[List[str]] = None) -> Optional[str]:
        """Return the device_id with highest health score from candidates."""
        if candidate_ids is None:
            candidate_ids = [
                did for did, s in self.device_status.items()
                if s.get("status") == "connected"
            ]
        if not candidate_ids:
            return None
        scores = {did: self.device_health_score(did)["total"]
                  for did in candidate_ids}
        return max(scores, key=scores.get)

    def record_recovery_event(self, device_id: str, level_name: str,
                              success: bool, details: str = ""):
        """Record a recovery attempt for timeline visualization."""
        event = {
            "device_id": device_id,
            "level": level_name,
            "success": success,
            "details": details,
            "ts": time.time(),
            "ts_str": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with self._lock:
            self._recovery_events.append(event)
            if len(self._recovery_events) > 500:
                self._recovery_events = self._recovery_events[-250:]

    def get_recovery_timeline(self, device_id: str = "",
                              limit: int = 100) -> List[dict]:
        """Return recent recovery events, optionally filtered by device."""
        with self._lock:
            events = list(self._recovery_events)
        if device_id:
            events = [e for e in events if e["device_id"] == device_id]
        return events[-limit:]

    def record_health_snapshot(self, device_id: str, score: dict):
        """Record a periodic health score snapshot for trend graphing."""
        entry = {"ts": time.time(), **score}
        with self._lock:
            hist = self._health_score_history.setdefault(device_id, [])
            hist.append(entry)
            if len(hist) > 288:
                self._health_score_history[device_id] = hist[-144:]

    def get_health_trend(self, device_id: str, hours: int = 24) -> List[dict]:
        """Return health score history for a device over the last N hours."""
        cutoff = time.time() - hours * 3600
        with self._lock:
            hist = list(self._health_score_history.get(device_id, []))
        return [e for e in hist if e["ts"] > cutoff]

    def get_all_health_trends(self, hours: int = 24) -> Dict[str, List[dict]]:
        """Return health score trends for all devices."""
        cutoff = time.time() - hours * 3600
        with self._lock:
            result = {}
            for did, hist in self._health_score_history.items():
                result[did] = [e for e in hist if e["ts"] > cutoff]
        return result

    def isolate_device(self, device_id: str):
        """Mark a device as isolated — won't receive new tasks."""
        with self._lock:
            self._isolated_devices.add(device_id)
        self.add_alert(
            "warning", device_id, "",
            alert_code="DEVICE_ISOLATED", params={},
        )

    def unisolate_device(self, device_id: str):
        with self._lock:
            self._isolated_devices.discard(device_id)
        self.add_alert(
            "info", device_id, "",
            alert_code="DEVICE_UNISOLATED", params={},
        )

    def is_isolated(self, device_id: str) -> bool:
        return device_id in self._isolated_devices

    def get_isolated_devices(self) -> List[str]:
        with self._lock:
            return list(self._isolated_devices)

    def add_alert(
        self,
        level: str,
        device_id: str,
        message: str = "",
        *,
        alert_code: Optional[str] = None,
        params: Optional[dict] = None,
    ):
        if alert_code:
            from src.host.alert_templates import render_alert_pair

            message, _ = render_alert_pair(alert_code, params)
        with self._lock:
            alert = {
                "level": level,
                "device_id": device_id,
                "message": message,
                "alert_code": alert_code or "",
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            self.alerts.append(alert)
            if len(self.alerts) > 200:
                self.alerts = self.alerts[-100:]

        try:
            from .event_stream import push_event
            push_event("device.alert", {"level": level, "message": message},
                       device_id=device_id)
        except Exception:
            pass

        try:
            from .alert_notifier import AlertNotifier

            if alert_code:
                AlertNotifier.get().notify(
                    level, device_id, "",
                    alert_code=alert_code, params=params,
                )
            else:
                AlertNotifier.get().notify(level, device_id, message)
        except Exception:
            pass

    def snapshot(self) -> dict:
        uptime = int(time.time() - self.uptime_start)
        with self._lock:
            fb_launch = {
                "launches": self.fb_launch_total,
                "remote_launches": self.fb_launch_remote,
                "remote_probe_fail": self.fb_launch_remote_probe_fail,
                "worker_cap_warning": self.fb_launch_worker_cap_warning,
                "steps_ok": self.fb_launch_steps_ok,
                "steps_failed": self.fb_launch_steps_failed,
            }
            tt_launch = {
                "launches": self.tt_launch_total,
                "remote_launches": self.tt_launch_remote,
                "steps_ok": self.tt_launch_steps_ok,
                "steps_failed": self.tt_launch_steps_failed,
                "skipped_local_offline": self.tt_flow_steps_skipped_local_offline,
            }
            tt_campaign = {
                "launches": self.tt_campaign_launch_total,
                "remote_launches": self.tt_campaign_launch_remote,
                "ok": self.tt_campaign_launch_ok,
                "failed": self.tt_campaign_launch_fail,
                "skipped_no_local_device": self.tt_campaign_skipped_no_local_row,
                "skipped_local_offline": self.tt_campaign_skipped_local_offline,
            }
        # 2026-05-05 Stage W: reverse heartbeat prober 指标接入 /metrics
        # 让 dashboard / Prometheus exporter / ops 看 JSON snapshot 一目了然.
        # prober 没启动 (worker mode 或 env disabled) 时返 running=False
        # — 不引入新 prometheus_client 依赖.
        rev_probe = {"running": False}
        try:
            from src.host.multi_host import _reverse_prober as _rp
            if _rp is not None:
                rev_probe = _rp.status()
        except Exception:
            pass
        return {
            "uptime_seconds": uptime,
            "tasks": {
                "total": self.tasks_total,
                "success": self.tasks_success,
                "failed": self.tasks_failed,
                "timeout": self.tasks_timeout,
                "by_type": self.get_task_type_stats(),
            },
            "facebook_launch": fb_launch,
            "tiktok_launch": tt_launch,
            "tiktok_campaign_launch": tt_campaign,
            "device_reconnects": self.device_reconnects,
            "last_heartbeat": self.last_heartbeat,
            "devices": dict(self.device_status),
            "recent_alerts": self.alerts[-20:],
            "reverse_probe": rev_probe,
        }


metrics = Metrics()


def health_monitor_launch_prometheus_text() -> str:
    """供 ``GET /observability/prometheus`` 追加：FB/TT device launch 进程内累计（gauge）。"""
    snap = metrics.snapshot()
    fb = snap.get("facebook_launch") or {}
    tt = snap.get("tiktok_launch") or {}
    ttc = snap.get("tiktok_campaign_launch") or {}
    lines: List[str] = []

    def _emit(name: str, help_zh: str, val: int) -> None:
        lines.append(f"# HELP {name} {help_zh}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {int(val)}")

    _emit("openclaw_facebook_device_launch_launches", "FB POST /facebook/device/*/launch count", fb.get("launches", 0))
    _emit(
        "openclaw_facebook_device_launch_remote_launches",
        "FB launch routed to remote worker",
        fb.get("remote_launches", 0),
    )
    _emit(
        "openclaw_facebook_device_launch_remote_probe_fail_total",
        "FB remote GET /health probe failures",
        fb.get("remote_probe_fail", 0),
    )
    _emit(
        "openclaw_facebook_device_launch_worker_cap_warning_total",
        "FB launches with worker_capabilities_warning",
        fb.get("worker_cap_warning", 0),
    )
    _emit("openclaw_facebook_device_launch_steps_ok_total", "FB launch step enqueue ok", fb.get("steps_ok", 0))
    _emit(
        "openclaw_facebook_device_launch_steps_failed_total",
        "FB launch step enqueue failed",
        fb.get("steps_failed", 0),
    )
    _emit("openclaw_tiktok_device_launch_launches", "TT POST /tiktok/device/*/launch flow_steps count", tt.get("launches", 0))
    _emit(
        "openclaw_tiktok_device_launch_remote_launches",
        "TT flow_steps launch routed to remote worker",
        tt.get("remote_launches", 0),
    )
    _emit("openclaw_tiktok_device_launch_steps_ok_total", "TT flow_steps step enqueue ok", tt.get("steps_ok", 0))
    _emit(
        "openclaw_tiktok_device_launch_steps_failed_total",
        "TT flow_steps step enqueue failed",
        tt.get("steps_failed", 0),
    )
    _emit(
        "openclaw_tiktok_flow_steps_skipped_local_offline_total",
        "TT flow_steps: local offline + OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE",
        tt.get("skipped_local_offline", 0),
    )
    _emit(
        "openclaw_tiktok_campaign_launch_launches",
        "TT launch-campaign invocations (no flow_steps)",
        ttc.get("launches", 0),
    )
    _emit(
        "openclaw_tiktok_campaign_launch_remote_launches",
        "TT launch-campaign to remote worker",
        ttc.get("remote_launches", 0),
    )
    _emit("openclaw_tiktok_campaign_launch_ok_total", "TT launch-campaign returned ok", ttc.get("ok", 0))
    _emit(
        "openclaw_tiktok_campaign_launch_failed_total",
        "TT launch-campaign failed or error path",
        ttc.get("failed", 0),
    )
    _emit(
        "openclaw_tiktok_campaign_launch_skipped_no_local_device_total",
        "TT campaign mode: device_id absent from local devices.yaml (local launch not attempted)",
        ttc.get("skipped_no_local_device", 0),
    )
    _emit(
        "openclaw_tiktok_campaign_launch_skipped_local_offline_total",
        "TT campaign: local device offline + OPENCLAW_TT_CAMPAIGN_SKIP_WORKER_WHEN_OFFLINE",
        ttc.get("skipped_local_offline", 0),
    )
    return "\n".join(lines) + "\n"


def _emit_health_event(event_type: str, **data):
    """Best-effort emit to EventBus."""
    try:
        from src.workflow.event_bus import get_event_bus
        bus = get_event_bus()
        bus.emit_simple(event_type, source="health_monitor", **data)
    except Exception:
        pass


def _check_uhubctl() -> bool:
    """Check if uhubctl is installed and functional."""
    try:
        r = _sp_run_text(["uhubctl", "--version"],
                           capture_output=True, timeout=5)
        available = r.returncode == 0
        if available:
            logger.info("uhubctl 已检测到，L4 USB 电源循环可用")
        return available
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _score_stability(disconnects_24h: int) -> int:
    if disconnects_24h == 0:
        return 100
    elif disconnects_24h == 1:
        return 80
    elif disconnects_24h == 2:
        return 60
    elif disconnects_24h <= 4:
        return 40
    return 20


def _score_responsiveness(latency_ms: float) -> int:
    if latency_ms <= 0:
        return 70
    if latency_ms < 200:
        return 100
    elif latency_ms < 500:
        return 85
    elif latency_ms < 1000:
        return 65
    elif latency_ms < 2000:
        return 45
    return 25


def _score_task_success(recent_results: list) -> int:
    if not recent_results:
        return 70
    success_count = sum(1 for r in recent_results if r)
    rate = success_count / len(recent_results)
    if rate >= 0.9:
        return 100
    elif rate >= 0.7:
        return 80
    elif rate >= 0.5:
        return 60
    elif rate >= 0.3:
        return 40
    return 20


def _score_u2_health(u2_reconnects_24h: int) -> int:
    if u2_reconnects_24h == 0:
        return 100
    elif u2_reconnects_24h == 1:
        return 75
    elif u2_reconnects_24h <= 3:
        return 50
    return 30


def _is_retryable_task(task_type: str) -> bool:
    """Check if a task type should be auto-retried after device reconnect."""
    return any(task_type.startswith(prefix) for prefix in _RETRYABLE_TASK_TYPES)


_TASK_PACKAGE_MAP = {
    "tiktok_": ["com.zhiliaoapp.musically", "com.ss.android.ugc.trill"],
    "telegram_": ["org.telegram.messenger"],
    "whatsapp_": ["com.whatsapp"],
    "linkedin_": ["com.linkedin.android"],
}


def _task_type_to_package(task_type: str) -> list:
    """Return expected package names for a task type."""
    for prefix, pkgs in _TASK_PACKAGE_MAP.items():
        if task_type.startswith(prefix):
            return pkgs
    return []


_STUCK_THRESHOLDS = {
    "tiktok_": 2400,
    "telegram_": 900,
    "whatsapp_": 900,
    "linkedin_": 900,
    "batch_": 1800,
    # 2026-05-03 v27: facebook 全链路任务 (group_member_greet / campaign_run)
    # 候选池放大到 40 + 加好友间隔 60-180s + 视觉判别 + greeting 间隔, 单
    # 任务正常需要 60-90 分钟. 旧 1200s 默认值在第 30 轮把 sent>0 的任务杀
    # 在 1247s. 给 facebook_ 前缀整体 7200s (与 executor _TASK_TYPE_TIMEOUTS
    # 对齐).
    "facebook_": 7200,
}
_DEFAULT_STUCK_THRESHOLD = 1200


def _stuck_threshold_for_type(task_type: str) -> int:
    """Return stuck-detection threshold (seconds) based on task type."""
    for prefix, secs in _STUCK_THRESHOLDS.items():
        if task_type.startswith(prefix):
            return secs
    return _DEFAULT_STUCK_THRESHOLD


_ADB_KEEPALIVE_INTERVAL = 20


# ---------------------------------------------------------------------------
# 2026-05-13: LLM 质量日报辅助函数
# ---------------------------------------------------------------------------

_LLM_DAILY_SNAP_FILE = "llm_daily_snapshot.json"
_LLM_WEEKLY_SNAP_FILE = "llm_weekly_snapshot.json"


def _capture_llm_stats() -> dict:
    """2026-05-13: 捕获当前 LLM 使用统计快照（用于计算每日 delta）并持久化到磁盘。

    快照也保存到 data/llm_daily_snapshot.json，
    进程重启后可恢复当日累积数据。
    """
    snap: dict = {}
    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        snap = client.stats.snapshot()
        snap["circuit_breaker"] = client.cb_status()
    except Exception:
        pass
    try:
        path = data_file(_LLM_DAILY_SNAP_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json as _j
        snap["_saved_date"] = datetime.now().strftime("%Y-%m-%d")
        path.write_text(_j.dumps(snap, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return snap


def _load_llm_daily_snapshot() -> dict:
    """2026-05-13: 加载上次保存的当日 LLM 快照（进程重启后恢复基准）。

    若快照日期不匹配今日，返回空 dict（令 delta 从 0 开始）。
    """
    try:
        path = data_file(_LLM_DAILY_SNAP_FILE)
        if not path.exists():
            return {}
        import json as _j
        data = _j.loads(path.read_text(encoding="utf-8"))
        today = datetime.now().strftime("%Y-%m-%d")
        if data.get("_saved_date") == today:
            return data
    except Exception:
        pass
    return {}


def _send_llm_daily_report(prev_snapshot: dict, report_date: str) -> None:
    """2026-05-13: 计算前一日 LLM 使用 delta 并推送日报。

    快照已持久化，进程重启后 delta 不丢失。
    """
    try:
        from src.ai.llm_client import get_llm_client
        from src.host.alert_notifier import AlertNotifier
        client = get_llm_client()
        cur = client.stats.snapshot()
        cb = client.cb_status()

        def _delta(key: int) -> int:
            return max(0, int(cur.get(key, 0)) - int(prev_snapshot.get(key, 0)))

        total = _delta("total_calls")
        cached = _delta("cached_calls")
        errors = _delta("errors")
        low_q = _delta("low_quality_responses")
        fb_triggers = _delta("fallback_triggers")
        lat_ms = max(0.0, float(cur.get("total_latency_ms", 0))
                     - float(prev_snapshot.get("total_latency_ms", 0)))

        real_calls = total - cached
        avg_lat = round(lat_ms / real_calls, 1) if real_calls > 0 else 0.0
        err_rate = f"{errors / total:.1%}" if total > 0 else "N/A"
        lq_rate = f"{low_q / total:.1%}" if total > 0 else "N/A"
        cache_rate = f"{cached / total:.1%}" if total > 0 else "N/A"

        lines = [
            f"📊 LLM 日报 [{report_date}]",
            f"总调用: {total}  缓存命中: {cached} ({cache_rate})",
            f"平均延迟: {avg_lat}ms  错误率: {err_rate}",
            f"低质响应: {low_q} ({lq_rate})  Fallback 触发: {fb_triggers}",
            f"断路器: {cb.get('state','?')}  连续失败: {cb.get('consecutive_failures',0)}",
        ]
        msg = "\n".join(lines)
        logger.info("[llm_daily] %s", msg.replace("\n", " | "))
        AlertNotifier.get().notify("info", "", msg)
    except Exception as e:
        logger.debug("[llm_daily] 日报发送失败: %s", e)


def _load_llm_weekly_snapshot() -> dict:
    """2026-05-13: 加载上次保存的当周 LLM 快照（进程重启后恢复基准）。"""
    try:
        path = data_file(_LLM_WEEKLY_SNAP_FILE)
        if not path.exists():
            return {}
        import json as _j
        data = _j.loads(path.read_text(encoding="utf-8"))
        cur_week = datetime.now().strftime("%G-W%V")
        if data.get("_saved_week") == cur_week:
            return data
    except Exception:
        pass
    return {}


def _send_llm_weekly_report(prev_snapshot: dict, report_week: str) -> None:
    """2026-05-13: 计算上周 LLM 使用 delta + 调度热图摘要，推送周报。"""
    try:
        from src.ai.llm_client import get_llm_client
        from src.host.alert_notifier import AlertNotifier
        client = get_llm_client()
        cur = client.stats.snapshot()
        cb = client.cb_status()

        def _delta(key: str) -> int:
            return max(0, int(cur.get(key, 0)) - int(prev_snapshot.get(key, 0)))

        total = _delta("total_calls")
        cached = _delta("cached_calls")
        errors = _delta("errors")
        low_q = _delta("low_quality_responses")
        fb_triggers = _delta("fallback_triggers")
        lat_ms = max(0.0, float(cur.get("total_latency_ms", 0))
                     - float(prev_snapshot.get("total_latency_ms", 0)))
        real_calls = total - cached
        avg_lat = round(lat_ms / real_calls, 1) if real_calls > 0 else 0.0
        p95_ms = float(cur.get("p95_latency_ms", 0.0))

        # 调度热图摘要（取本周聚合 by_device top 5）
        dispatch_summary = ""
        try:
            from src.host.smart_scheduler import get_smart_scheduler
            heatmap = get_smart_scheduler().get_dispatch_heatmap(hours=168)
            top = sorted(heatmap.get("by_device", {}).items(),
                         key=lambda kv: kv[1]["total"], reverse=True)[:5]
            if top:
                rows = [f"  {d[:10]}: total={v['total']} fail={v['fail']}" for d, v in top]
                dispatch_summary = "\n派发 Top5 设备 (本周):\n" + "\n".join(rows)
        except Exception:
            pass

        lines = [
            f"📈 LLM 周报 [{report_week}]",
            f"总调用: {total}  缓存命中: {cached} ({f'{cached/total:.1%}' if total else 'N/A'})",
            f"均值延迟: {avg_lat}ms  p95: {p95_ms}ms  错误率: {f'{errors/total:.1%}' if total else 'N/A'}",
            f"低质响应: {low_q}  Fallback: {fb_triggers}  CB状态: {cb.get('state','?')}",
        ]
        if dispatch_summary:
            lines.append(dispatch_summary)

        msg = "\n".join(lines)
        logger.info("[llm_weekly] %s", msg.replace("\n", " | "))
        AlertNotifier.get().notify("info", "", msg)

        # 保存当周快照
        try:
            path = data_file(_LLM_WEEKLY_SNAP_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            import json as _j
            snap = dict(cur)
            snap["_saved_week"] = datetime.now().strftime("%G-W%V")
            path.write_text(_j.dumps(snap, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    except Exception as e:
        logger.debug("[llm_weekly] 周报发送失败: %s", e)


class HealthMonitor(threading.Thread):
    """设备心跳监控线程 — 自动重连、自适应间隔、全任务恢复、scrcpy 清理。"""

    def __init__(self, config_path: str,
                 interval: int = _HEARTBEAT_INTERVAL_NORMAL):
        super().__init__(daemon=True, name="openclaw-health")
        self._config_path = config_path
        self._interval_normal = interval
        self._interval_fast = _HEARTBEAT_INTERVAL_FAST
        self._current_interval = interval
        self._stop_event = threading.Event()
        self._interrupted_tasks: Dict[str, List[str]] = {}
        self._disconnected_devices: Set[str] = set()
        self._recovery_state: Dict[str, dict] = {}
        self._streaming_before_disconnect: Set[str] = set()
        self._uhubctl_available: Optional[bool] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._wifi_backup: Dict[str, str] = {}
        self._wifi_refresh_counter: int = 0
        self._predictive_check_counter: int = 0
        self._last_kill_server_time: float = 0.0
        self._adb_miss_streak: Dict[str, int] = {}

    def run(self):
        logger.info(
            "HealthMonitor 启动，正常间隔 %ds / 快速间隔 %ds，掉线确认轮次=%d",
            self._interval_normal,
            self._interval_fast,
            effective_disconnect_confirm_rounds(self._config_path),
        )
        self._keepalive_thread = threading.Thread(
            target=self._adb_keepalive_loop, daemon=True,
            name="openclaw-adb-keepalive")
        self._keepalive_thread.start()
        _snapshot_interval = 60  # 每 60 秒保存一次指标快照
        _last_snapshot = 0.0
        _last_daily_date: str = datetime.now().strftime("%Y-%m-%d")
        _last_weekly_isoweek: str = datetime.now().strftime("%G-W%V")
        # 2026-05-13: 优先从磁盘加载当日/当周快照，进程重启后不丢失累积数据
        _daily_llm_snapshot: dict = _load_llm_daily_snapshot()
        _weekly_llm_snapshot: dict = _load_llm_weekly_snapshot()
        while not self._stop_event.is_set():
            try:
                self._check()
            except Exception as e:
                logger.error("HealthMonitor 检查异常: %s", e)
            # 2026-05-13: 周期性持久化指标快照（60s，不阻塞主循环）
            _now = time.time()
            if _now - _last_snapshot >= _snapshot_interval:
                try:
                    metrics.save_snapshot()
                    _last_snapshot = _now
                except Exception:
                    pass
            # 2026-05-13: LLM 质量日报（每天第一次检测到日期切换时触发）
            try:
                _today = datetime.now().strftime("%Y-%m-%d")
                if _today != _last_daily_date:
                    if _last_daily_date:
                        _send_llm_daily_report(_daily_llm_snapshot, _last_daily_date)
                    _daily_llm_snapshot = _capture_llm_stats()
                    _last_daily_date = _today
            except Exception:
                pass
            # 2026-05-13: LLM 质量周报（ISO 周号切换时触发，含调度热图摘要）
            try:
                _cur_week = datetime.now().strftime("%G-W%V")
                if _cur_week != _last_weekly_isoweek:
                    if _last_weekly_isoweek:
                        _send_llm_weekly_report(_weekly_llm_snapshot, _last_weekly_isoweek)
                    _weekly_llm_snapshot = _capture_llm_stats()
                    _last_weekly_isoweek = _cur_week
            except Exception:
                pass
            self._stop_event.wait(self._current_interval)
        kt = self._keepalive_thread
        if kt is not None and kt.is_alive():
            kt.join(timeout=10)
        logger.info("HealthMonitor 已停止")

    def _adb_keepalive_loop(self):
        """Send lightweight ADB heartbeats to keep USB connections alive."""
        logger.info("ADB keepalive 启动 (间隔 %ds)", _ADB_KEEPALIVE_INTERVAL)
        while not self._stop_event.is_set():
            self._stop_event.wait(_ADB_KEEPALIVE_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                manager = get_device_manager(self._config_path)
                connected = manager.get_connected_devices()
                adb = getattr(manager, 'adb_path', 'adb')
                for dev in connected:
                    try:
                        _sp_run_text(
                            [adb, "-s", dev.device_id, "shell", "echo", "1"],
                            capture_output=True, timeout=5,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("ADB keepalive error: %s", e)

    def stop(self):
        """请求停止主循环（keepalive 由 ``run`` 收尾时 join）。"""
        self._stop_event.set()
        # 2026-05-13: 优雅停机时立即保存指标，防止进程被 kill 前 60s 窗口内的数据丢失
        try:
            metrics.save_snapshot()
            logger.info("[metrics] 停机快照已保存")
        except Exception as e:
            logger.debug("[metrics] 停机快照保存失败: %s", e)

    def _update_interval(self):
        """Switch to fast polling when any device is disconnected."""
        if self._disconnected_devices:
            if self._current_interval != self._interval_fast:
                logger.info("切换到快速检测模式 (%ds)，%d 台设备掉线",
                            self._interval_fast, len(self._disconnected_devices))
                self._current_interval = self._interval_fast
        else:
            if self._current_interval != self._interval_normal:
                logger.info("所有设备在线，回退到正常检测间隔 (%ds)",
                            self._interval_normal)
                self._current_interval = self._interval_normal

    def _check(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        metrics.last_heartbeat = now

        manager = get_device_manager(self._config_path)
        manager.discover_devices()
        connected = manager.get_connected_devices()
        all_devices = manager.get_all_devices()

        connected_ids = {d.device_id for d in connected}

        for dev in all_devices:
            prev = metrics.device_status.get(dev.device_id, {})
            prev_status = prev.get("status")
            raw_on = dev.device_id in connected_ids
            if raw_on:
                self._adb_miss_streak.pop(dev.device_id, None)
                current = "connected"
            else:
                if prev_status == "connected":
                    streak = self._adb_miss_streak.get(dev.device_id, 0) + 1
                    self._adb_miss_streak[dev.device_id] = streak
                    th = effective_disconnect_confirm_rounds(self._config_path)
                    if streak >= th:
                        current = "disconnected"
                    else:
                        current = "connected"
                        logger.debug(
                            "ADB 未见设备 %s (%d/%d 次)，暂仍视为在线（防抖）",
                            dev.device_id[:20],
                            streak,
                            th,
                        )
                else:
                    current = "disconnected"
                    self._adb_miss_streak.pop(dev.device_id, None)

            if prev_status == "connected" and current == "disconnected":
                self._handle_disconnect(manager, dev)

            elif current == "disconnected" and dev.device_id in self._disconnected_devices:
                self._handle_still_disconnected(manager, dev)

            elif (prev_status == "disconnected" or prev_status is None) and current == "connected":
                if dev.device_id in self._disconnected_devices:
                    self._handle_reconnect(manager, dev)
                elif prev_status is None:
                    self._handle_first_online(manager, dev)

            u2_ok = False
            app_running = False
            _is_streaming = False
            try:
                from .scrcpy_manager import get_scrcpy_manager
                _is_streaming = get_scrcpy_manager().is_streaming(dev.device_id)
            except Exception:
                pass

            if current == "connected" and not _is_streaming:
                self._measure_adb_latency(manager, dev.device_id)
                u2_ok = self._check_u2_deep(manager, dev.device_id)
                if u2_ok:
                    app_running = self._check_app_running(manager, dev.device_id)
                    if not app_running:
                        self._try_restart_app(manager, dev.device_id)

            if current == "connected" and not _is_streaming:
                try:
                    from src.behavior.vpn_health import get_vpn_health_monitor
                    vpn_mon = get_vpn_health_monitor()
                    _has_task = self._device_has_active_task(dev.device_id)
                    vpn_mon.check_device(dev.device_id, manager,
                                         allow_reconnect=_has_task)
                except Exception:
                    pass

            score = metrics.device_health_score(dev.device_id)
            metrics.record_health_snapshot(dev.device_id, score)

            if (score["total"] < 30 and current == "connected"
                    and not metrics.is_isolated(dev.device_id)):
                metrics.isolate_device(dev.device_id)
                logger.warning("设备 %s 健康评分极低 (%d)，自动隔离",
                               dev.device_id[:8], score["total"])
            elif (score["total"] >= 60 and metrics.is_isolated(dev.device_id)):
                metrics.unisolate_device(dev.device_id)
                logger.info("设备 %s 健康评分恢复 (%d)，解除隔离",
                            dev.device_id[:8], score["total"])

            metrics.device_status[dev.device_id] = {
                "display_name": dev.display_name,
                "status": current,
                "u2": u2_ok,
                "app_running": app_running,
                "checked_at": now,
                "health_score": score["total"],
            }

        self._update_interval()

        # 每 10 个检测周期刷新一次 WiFi 备份地址（防止 DHCP 续期导致 IP 变化）
        self._wifi_refresh_counter += 1
        if self._wifi_refresh_counter >= 10:
            self._wifi_refresh_counter = 0
            self._refresh_all_wifi_backups()

        self._check_stuck_tasks()
        self._check_screen_anomalies(manager, connected_ids)
        self._auto_tune_compliance()
        self._periodic_fingerprint_sync(manager)

        # 每 5 个检测周期执行一次预测性维护检查
        self._predictive_check_counter += 1
        if self._predictive_check_counter >= 5:
            self._predictive_check_counter = 0
            self._run_predictive_check()

    def _run_predictive_check(self):
        """预测性维护检查 — 基于掉线频率模式预测即将掉线的设备并提前告警。"""
        try:
            from src.host.alert_message_context import (
                approximate_english_message as _approx_en,
            )

            for did in metrics.get_disconnect_history_devices():
                pred = metrics.predict_disconnect_risk(did)
                if pred["risk"] == "high":
                    reasons_zh = ", ".join(pred["reasons"])
                    reasons_en = _approx_en(reasons_zh)
                    metrics.add_alert(
                        "warning",
                        did,
                        "",
                        alert_code="DEVICE_PREDICTIVE_HIGH",
                        params={
                            "reasons": reasons_zh,
                            "reasons_en": reasons_en,
                            "score": pred["score"],
                        },
                    )
                    logger.warning("预测掉线风险 HIGH: %s - %s",
                                   did[:8], pred["reasons"])
                elif pred["risk"] == "medium":
                    logger.info("预测掉线风险 MEDIUM: %s - score=%d",
                                did[:8], pred["score"])
        except Exception as e:
            logger.debug("预测性检查异常: %s", e)

    # ── Disconnect / Reconnect handlers ──────────────────────────────────

    _UNRECOVERABLE_STATES = frozenset({"unauthorized", "authorizing"})

    @staticmethod
    def _get_problem_status(manager, device_id: str) -> Optional[str]:
        """Return ADB problem status (unauthorized/offline/…) if device is in
        manager's problem list, else None."""
        for did, status in getattr(manager, "_last_problem_devices", []):
            if did == device_id:
                return status
        return None

    def _handle_disconnect(self, manager, dev):
        """First time a device is seen as disconnected."""
        disc_count = metrics.record_disconnect(dev.device_id)
        self._disconnected_devices.add(dev.device_id)
        self._recovery_state[dev.device_id] = {
            "level": 0, "attempts": 0, "last_attempt": 0,
            "started_at": time.time(),
        }

        problem_status = self._get_problem_status(manager, dev.device_id)

        logger.warning("设备 %s (%s) 掉线! (连续第 %d 次)%s",
                        dev.device_id[:8], dev.display_name, disc_count,
                        f" [ADB状态: {problem_status}]" if problem_status else "")

        try:
            from src.host.multi_host import load_cluster_config

            _hn = (load_cluster_config() or {}).get("host_name", "") or ""
            _tag = f"[{_hn}] " if _hn else ""
            _disp = dev.display_name or dev.device_id[:8]
            metrics.add_alert(
                "warning",
                dev.device_id,
                "",
                alert_code="PHONE_OFFLINE",
                params={
                    "host_tag": _tag,
                    "display": _disp,
                    "n": disc_count,
                },
            )
        except Exception:
            metrics.add_alert(
                "warning",
                dev.device_id,
                f"设备掉线 (连续第{disc_count}次)",
            )
        _emit_health_event("device.disconnected",
                           device_id=dev.device_id,
                           display_name=dev.display_name,
                           consecutive=disc_count)

        self._cancel_running_tasks(dev.device_id)
        self._snapshot_running_tasks(dev.device_id)
        self._cleanup_scrcpy_session(dev.device_id)

        if disc_count >= _DISCONNECT_ALERT_THRESHOLD:
            metrics.add_alert(
                "critical",
                dev.device_id,
                "",
                alert_code="DEVICE_OFFLINE_CRITICAL",
                params={"n": disc_count},
            )
            _emit_health_event("device.alert_critical",
                               device_id=dev.device_id,
                               display_name=dev.display_name,
                               consecutive=disc_count)

        if problem_status in self._UNRECOVERABLE_STATES:
            if dev.device_id in self._wifi_backup:
                logger.info("设备 %s 处于 %s 状态，尝试通过 Wi-Fi 备份通道恢复",
                            dev.device_id[:8], problem_status)
            else:
                logger.info("设备 %s 处于 %s 状态，无 Wi-Fi 备份，需用户在手机上授权",
                            dev.device_id[:8], problem_status)
                return

        self._escalated_recovery(manager, dev.device_id)

    def _handle_still_disconnected(self, manager, dev):
        """Device was already known disconnected — continue escalated recovery."""
        problem_status = self._get_problem_status(manager, dev.device_id)
        if problem_status in self._UNRECOVERABLE_STATES:
            if dev.device_id not in self._wifi_backup:
                return

        state = self._recovery_state.get(dev.device_id)
        if not state:
            return
        if state["level"] >= len(_RECOVERY_LEVELS):
            return

        now = time.time()
        level_cfg = _RECOVERY_LEVELS[state["level"]]
        if now - state["last_attempt"] < level_cfg["cooldown_sec"]:
            return

        self._escalated_recovery(manager, dev.device_id)

    def _handle_reconnect(self, manager, dev):
        """Device came back online after being disconnected."""
        state = self._recovery_state.pop(dev.device_id, None)
        self._disconnected_devices.discard(dev.device_id)
        metrics.clear_disconnect(dev.device_id)

        recovery_info = ""
        tail_zh, tail_en = "", ""
        if state:
            elapsed = int(time.time() - state.get("started_at", time.time()))
            level_name = _RECOVERY_LEVELS[min(state["level"], len(_RECOVERY_LEVELS) - 1)]["name"]
            recovery_info = f" (恢复方式: {level_name}, 耗时 {elapsed}s)"
            tail_zh = recovery_info
            tail_en = f" (recovery: {level_name}, {elapsed}s)"

        logger.info("设备 %s (%s) 重新上线%s",
                     dev.device_id[:8], dev.display_name, recovery_info)
        metrics.add_alert(
            "info",
            dev.device_id,
            "",
            alert_code="DEVICE_BACK_ONLINE",
            params={"tail_zh": tail_zh, "tail_en": tail_en},
        )
        _emit_health_event("device.reconnected",
                           device_id=dev.device_id,
                           display_name=dev.display_name)

        # 重新上线时自动解除隔离（人工重插USB后能立即恢复）
        if metrics.is_isolated(dev.device_id):
            metrics.unisolate_device(dev.device_id)
            logger.info("设备 %s 重新上线，自动解除隔离", dev.device_id[:8])

        try:
            from src.device_control.watchdog import get_watchdog
            get_watchdog().watch(dev.device_id)
        except Exception:
            pass

        # 2026-05-12: 重新上线后强制竖屏
        self._enforce_portrait(manager, dev.device_id)
        self._auto_deploy_wallpaper(manager, dev.device_id)
        self._recover_interrupted_tasks(dev.device_id)
        self._restore_scrcpy_session(dev.device_id)
        self._pre_push_scrcpy(dev.device_id)
        adb = getattr(manager, 'adb_path', 'adb')
        self._wifi_backup.pop(dev.device_id, None)
        self._setup_wifi_backup(adb, dev.device_id)

    def _handle_first_online(self, manager, dev):
        """Device is seen for the first time in this session."""
        logger.info("设备 %s (%s) 首次上线",
                     dev.device_id[:8], dev.display_name)
        metrics.add_alert(
            "info", dev.device_id, "",
            alert_code="DEVICE_FIRST_ONLINE", params={},
        )
        _emit_health_event("device.online",
                           device_id=dev.device_id,
                           display_name=dev.display_name,
                           first_time=True)
        try:
            from src.device_control.watchdog import get_watchdog
            get_watchdog().watch(dev.device_id)
        except Exception:
            pass
        # 2026-05-12: 首次上线时强制竖屏
        self._enforce_portrait(manager, dev.device_id)
        self._auto_deploy_wallpaper(manager, dev.device_id)
        self._pre_push_scrcpy(dev.device_id)
        adb = getattr(manager, 'adb_path', 'adb')
        self._setup_wifi_backup(adb, dev.device_id)

    @staticmethod
    def _enforce_portrait(manager, device_id: str):
        """Force portrait orientation — prevent landscape drift."""
        try:
            manager.execute_adb_command(
                "shell settings put system accelerometer_rotation 0", device_id)
            manager.execute_adb_command(
                "shell settings put system user_rotation 0", device_id)
            logger.debug("[portrait-lock] %s 已锁定竖屏", device_id[:12])
        except Exception as e:
            logger.debug("[portrait-lock] %s 失败: %s", device_id[:12], e)

    # ── ADB reconnect ────────────────────────────────────────────────────

    @staticmethod
    def _is_wifi_device(device_id: str) -> bool:
        return bool(_WIFI_DEVICE_PATTERN.match(device_id))

    def _escalated_recovery(self, manager, device_id: str):
        """Multi-level recovery: reconnect → reset_transport → kill_server → uhubctl."""
        from .shared import get_device_recovery_lock
        lock = get_device_recovery_lock(device_id)
        if not lock.acquire(blocking=False):
            logger.debug("设备 %s 恢复锁已被占用，跳过", device_id)
            return
        try:
            self._escalated_recovery_inner(manager, device_id)
        finally:
            lock.release()

    def _escalated_recovery_inner(self, manager, device_id: str):
        """_escalated_recovery 的实际逻辑（持锁调用）。"""
        state = self._recovery_state.get(device_id)
        if not state:
            return

        level = state["level"]
        if level >= len(_RECOVERY_LEVELS):
            return

        level_cfg = _RECOVERY_LEVELS[level]
        adb = getattr(manager, 'adb_path', 'adb')
        is_wifi = self._is_wifi_device(device_id)

        state["attempts"] += 1
        state["last_attempt"] = time.time()

        level_name = level_cfg["name"]
        logger.info("分级恢复 %s: L%d/%s (第%d次, 共%d次上限)",
                     device_id[:8], level, level_name,
                     state["attempts"], level_cfg["max_attempts"])

        success = False

        try:
            if level_name == "reconnect":
                success = self._recovery_l1_reconnect(adb, device_id, is_wifi)

            elif level_name == "wifi_and_dismiss":
                success = self._recovery_wifi_and_dismiss(adb, device_id)

            elif level_name == "reset_transport":
                success = self._recovery_l2_reset_transport(adb, device_id, is_wifi)

            elif level_name == "kill_server":
                success = self._recovery_l3_kill_server(
                    adb, device_id, manager)

            elif level_name == "usb_power_cycle":
                success = self._recovery_l4_usb_power_cycle(
                    device_id, is_wifi)

        except Exception as e:
            logger.warning("恢复异常 %s L%d: %s", device_id[:8], level, e)

        metrics.record_recovery_event(device_id, level_name, success,
                                      f"L{level} attempt {state['attempts']}")

        if success:
            logger.info("设备 %s 通过 L%d/%s 恢复成功",
                         device_id[:8], level, level_name)
            metrics.inc_reconnect()
            metrics.add_alert(
                "info",
                device_id,
                "",
                alert_code="ADB_RECOVERY_SUCCESS",
                params={"level": level, "method": level_name},
            )
            _emit_health_event("device.adb_reconnected",
                               device_id=device_id,
                               level=level, method=level_name)
            self._disconnected_devices.discard(device_id)
            self._recovery_state.pop(device_id, None)
            metrics.clear_disconnect(device_id)
            manager.discover_devices()
            self._auto_deploy_wallpaper(manager, device_id)
            self._recover_interrupted_tasks(device_id)
            self._restore_scrcpy_session(device_id)
        else:
            if state["attempts"] >= level_cfg["max_attempts"]:
                state["level"] = level + 1
                state["attempts"] = 0
                if state["level"] < len(_RECOVERY_LEVELS):
                    next_name = _RECOVERY_LEVELS[state["level"]]["name"]
                    logger.info("设备 %s L%d/%s 失败，升级到 L%d/%s",
                                device_id[:8], level, level_name,
                                state["level"], next_name)
                else:
                    logger.warning("设备 %s 所有恢复级别已用尽，5分钟后重新尝试",
                                   device_id[:8])
                    metrics.add_alert(
                        "error",
                        device_id,
                        "",
                        alert_code="ADB_RECOVERY_EXHAUSTED",
                        params={},
                    )
                    _emit_health_event("device.recovery_exhausted",
                                       device_id=device_id)
                    # 重置为 L0 并设置延迟，5分钟后重新开始整个恢复流程
                    state["level"] = 0
                    state["attempts"] = 0
                    state["last_attempt"] = time.time() + 300  # 5分钟后再试

    # ── Wi-Fi 备份通道 + 弹窗自动处理 ────────────────────────────────────

    def _setup_wifi_backup(self, adb: str, device_id: str):
        """后台为 USB 设备开启 tcpip 5555 并记录 Wi-Fi IP，供掉线时备用恢复。"""
        if self._is_wifi_device(device_id):
            return

        _cflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def _do():
            try:
                r = _sp_run_text(
                    [adb, "-s", device_id, "shell",
                     "ip", "route", "get", "8.8.8.8"],
                    capture_output=True, timeout=5,
                    creationflags=_cflags,
                )
                match = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', r.stdout)
                if not match:
                    return
                ip = match.group(1)
                if ip.startswith("127."):
                    return

                _sp_run_text(
                    [adb, "-s", device_id, "tcpip", "5555"],
                    capture_output=True, timeout=10, creationflags=_cflags,
                )
                time.sleep(2)

                self._wifi_backup[device_id] = f"{ip}:5555"
                logger.info("Wi-Fi 备份通道已建立: %s → %s:5555",
                            device_id[:8], ip)
            except Exception as e:
                logger.debug("Wi-Fi 备份建立失败 %s: %s", device_id[:8], e)

        threading.Thread(target=_do, daemon=True,
                         name=f"wifi-backup-{device_id[:8]}").start()

    def _refresh_all_wifi_backups(self):
        """定期刷新所有在线设备的 WiFi 备份地址，防止 DHCP 续期导致 IP 变化。"""
        try:
            manager = get_device_manager(self._config_path)
            adb = getattr(manager, 'adb_path', 'adb')
            connected = manager.get_connected_devices()
            _cflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            for dev in connected:
                did = dev.device_id
                # 只处理 USB 设备（跳过 WiFi 设备）
                if self._is_wifi_device(did):
                    continue
                try:
                    r = _sp_run_text(
                        [adb, '-s', did, 'shell', 'ip', 'route', 'get', '8.8.8.8'],
                        capture_output=True, timeout=5,
                        creationflags=_cflags,
                    )
                    match = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', r.stdout)
                    if match:
                        ip = match.group(1)
                        if ip.startswith("127."):
                            continue
                        new_addr = f"{ip}:5555"
                        old_addr = self._wifi_backup.get(did)
                        if old_addr != new_addr:
                            self._wifi_backup[did] = new_addr
                            if old_addr:
                                logger.info("WiFi 备份地址更新: %s -> %s (was %s)",
                                            did[:8], new_addr, old_addr)
                            else:
                                logger.debug("WiFi 备份建立: %s -> %s",
                                             did[:8], new_addr)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("WiFi 备份刷新失败: %s", e)

    def _recovery_wifi_and_dismiss(self, adb: str, device_id: str) -> bool:
        """通过 Wi-Fi 备份通道连接设备，自动点击 USB 调试/用途弹窗，恢复 USB。"""
        if self._is_wifi_device(device_id):
            return False

        wifi_addr = self._wifi_backup.get(device_id)
        if not wifi_addr:
            logger.debug("设备 %s 无 Wi-Fi 备份地址，跳过", device_id[:8])
            return False

        ip = wifi_addr.split(":")[0]
        _cflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            r = _sp_run_text(
                [adb, "connect", wifi_addr],
                capture_output=True, timeout=10,
                creationflags=_cflags,
            )
            wifi_ok = ("connected" in r.stdout.lower()
                       and "cannot" not in r.stdout.lower())
        except Exception:
            wifi_ok = False

        if not wifi_ok:
            logger.debug("Wi-Fi ADB 连接失败: %s", wifi_addr)
            return False

        logger.info("Wi-Fi ADB 已连接: %s → %s", device_id[:8], wifi_addr)

        dismissed = self._dismiss_usb_dialogs(adb, wifi_addr, ip)
        if dismissed:
            logger.info("已通过 Wi-Fi 通道处理弹窗: %s", device_id[:8])

        time.sleep(3 if dismissed else 1)

        try:
            v = _sp_run_text(
                [adb, "-s", device_id, "shell", "echo", "ok"],
                capture_output=True, timeout=5,
                creationflags=_cflags,
            )
            usb_back = v.returncode == 0 and "ok" in v.stdout
        except Exception:
            usb_back = False

        if not _wifi_recovery_keep_adb_connected():
            try:
                _sp_run_text([adb, "disconnect", wifi_addr],
                               capture_output=True, timeout=5,
                               creationflags=_cflags)
            except Exception:
                pass
        else:
            logger.debug("保持无线 ADB 连接（wifi_recovery_keep_adb_connected=true）: %s", wifi_addr)

        if usb_back:
            logger.info("USB 连接已通过 Wi-Fi 辅助恢复: %s", device_id[:8])
        return usb_back

    def _dismiss_usb_dialogs(self, adb: str, wifi_addr: str,
                              ip: str) -> bool:
        """通过 Wi-Fi 通道使用 u2 或 ADB 自动处理 USB 调试确认和用途弹窗。

        返回 True 表示成功点击了至少一个弹窗。
        """
        _cflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            import uiautomator2 as u2
            d = u2.connect(ip)
            if not d:
                raise ConnectionError("u2 connect returned None")
        except Exception as e:
            logger.debug("u2 via Wi-Fi 连接失败 (%s): %s，尝试 ADB 方式",
                         ip, e)
            return self._dismiss_usb_dialogs_adb(adb, wifi_addr)

        dismissed = False
        dialog_defs = [
            {
                "detect": ["允许USB调试", "Allow USB debugging", "允许 USB 调试"],
                "always": ["始终允许", "Always allow", "一律允许",
                           "始终允许使用这台计算机"],
                "buttons": ["确定", "允许", "OK", "Allow", "好", "知道了", "继续", "允许调试"],
            },
            {
                "detect": ["USB 的用途", "USB用于", "Use USB for",
                           "选择USB模式", "USB 连接方式"],
                "always": [],
                "buttons": ["传输文件", "文件传输", "File transfer",
                            "Transfer files", "MTP"],
            },
            {
                "detect": ["这台计算机的 RSA 密钥指纹", "RSA key fingerprint", "密钥指纹"],
                "always": ["始终允许", "Always allow", "一律允许"],
                "buttons": ["确定", "OK", "允许", "允许调试"],
            },
        ]

        for dlg in dialog_defs:
            for detect_text in dlg["detect"]:
                try:
                    if not d(textContains=detect_text).exists(timeout=1.5):
                        continue
                except Exception:
                    continue

                for always_text in dlg["always"]:
                    try:
                        cb = d(textContains=always_text)
                        if cb.exists(timeout=0.5):
                            info = cb.info
                            if not info.get("checked", False):
                                cb.click()
                                time.sleep(0.5)
                            break
                    except Exception:
                        continue

                for btn_text in dlg["buttons"]:
                    try:
                        btn = d(text=btn_text)
                        if btn.exists(timeout=0.5):
                            btn.click()
                            logger.info("u2 自动点击弹窗: [%s] → [%s]",
                                        detect_text, btn_text)
                            dismissed = True
                            time.sleep(1)
                            break
                    except Exception:
                        continue

                if dismissed:
                    break
            if dismissed:
                break

        return dismissed

    def _dismiss_usb_dialogs_adb(self, adb: str, wifi_addr: str) -> bool:
        """u2 不可用时通过 ADB shell uiautomator dump + input tap 处理弹窗。"""
        _cflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            r = _sp_run_text(
                [adb, "-s", wifi_addr, "shell",
                 "uiautomator", "dump", "/dev/tty"],
                capture_output=True, timeout=10,
                creationflags=_cflags,
            )
            xml = r.stdout
            if not xml:
                return False

            keywords_and_buttons = [
                ("允许USB调试", "确定"),
                ("Allow USB debugging", "OK"),
                ("允许 USB 调试", "确定"),
                ("这台计算机的 RSA 密钥指纹", "确定"),
                ("RSA key fingerprint", "OK"),
            ]

            for keyword, button_text in keywords_and_buttons:
                if keyword not in xml:
                    continue

                bounds_pattern = (
                    rf'text="{re.escape(button_text)}"[^>]*'
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
                )
                m = re.search(bounds_pattern, xml)
                if not m:
                    continue

                x = (int(m.group(1)) + int(m.group(3))) // 2
                y = (int(m.group(2)) + int(m.group(4))) // 2

                always_pattern = (
                    r'text="[^"]*(?:始终允许|Always allow)[^"]*"[^>]*'
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
                )
                ma = re.search(always_pattern, xml)
                if ma:
                    ax = (int(ma.group(1)) + int(ma.group(3))) // 2
                    ay = (int(ma.group(2)) + int(ma.group(4))) // 2
                    _sp_run_text(
                        [adb, "-s", wifi_addr, "shell",
                         "input", "tap", str(ax), str(ay)],
                        capture_output=True, timeout=5,
                        creationflags=_cflags,
                    )
                    time.sleep(0.5)

                _sp_run_text(
                    [adb, "-s", wifi_addr, "shell",
                     "input", "tap", str(x), str(y)],
                    capture_output=True, timeout=5,
                    creationflags=_cflags,
                )
                logger.info("ADB 自动点击弹窗: [%s] tap(%d,%d)",
                            button_text, x, y)
                return True

        except Exception as e:
            logger.debug("ADB 弹窗处理失败: %s", e)
        return False

    # ── ADB reconnect (原有分级恢复) ──────────────────────────────────────

    def _recovery_l1_reconnect(self, adb: str, device_id: str,
                                is_wifi: bool) -> bool:
        """Level 1: Simple adb reconnect / connect."""
        try:
            if is_wifi:
                r = _sp_run_text([adb, "connect", device_id],
                                   capture_output=True, timeout=10)
                return r.returncode == 0 and "connected" in r.stdout.lower()
            else:
                _sp_run_text([adb, "reconnect", device_id],
                               capture_output=True, timeout=10)
                time.sleep(3)
                v = _sp_run_text([adb, "-s", device_id, "shell", "echo", "ok"],
                                   capture_output=True, timeout=5)
                return v.returncode == 0 and "ok" in v.stdout
        except Exception:
            return False

    def _recovery_l2_reset_transport(self, adb: str, device_id: str,
                                      is_wifi: bool) -> bool:
        """Level 2: Full transport reset — disconnect+reconnect (WiFi) / usb reset (USB)."""
        try:
            if is_wifi:
                _sp_run_text([adb, "disconnect", device_id],
                               capture_output=True, timeout=5)
                time.sleep(2)
                r = _sp_run_text([adb, "connect", device_id],
                                   capture_output=True, timeout=10)
                time.sleep(3)
                return r.returncode == 0 and "connected" in r.stdout.lower()
            else:
                _sp_run_text([adb, "-s", device_id, "reconnect", "offline"],
                               capture_output=True, timeout=10)
                time.sleep(5)
                v = _sp_run_text([adb, "-s", device_id, "shell", "echo", "ok"],
                                   capture_output=True, timeout=5)
                return v.returncode == 0 and "ok" in v.stdout
        except Exception:
            return False

    def _recovery_l3_kill_server(self, adb: str, device_id: str,
                                  manager) -> bool:
        """Level 3: Kill and restart ADB server (affects all devices)."""
        problem_status = self._get_problem_status(manager, device_id)
        if problem_status in self._UNRECOVERABLE_STATES:
            logger.info("跳过 kill-server: 设备 %s 处于 %s 状态，"
                        "kill-server 会导致授权丢失", device_id[:8], problem_status)
            return False

        for did, st in getattr(manager, "_last_problem_devices", []):
            if st in self._UNRECOVERABLE_STATES:
                logger.info("跳过 kill-server: 设备 %s 正在等待授权，"
                            "kill-server 会中断授权流程", did[:8])
                return False

        # 冷却保护：最近 2 分钟内已执行过 kill-server 则跳过（从300s降至120s）
        if self._last_kill_server_time and time.time() - self._last_kill_server_time < 120:
            logger.info("L3 跳过: 距上次 kill-server 不到 2 分钟")
            return False

        # 阈值保护：离线设备占比不到 15% 则跳过（从50%降至15%，支持6台中1台触发）
        # 若单台设备已离线超过 3 分钟，强制绕过阈值保护直接执行
        connected_count = len(manager.get_connected_devices())
        total_offline = len(self._disconnected_devices)
        total = connected_count + total_offline

        dev_offline_secs = time.time() - self._recovery_state.get(
            device_id, {}).get("started_at", time.time())
        force_by_timeout = dev_offline_secs >= 180  # 离线超过3分钟强制执行

        if not force_by_timeout and total > 0 and total_offline / total < 0.15:
            logger.info("L3 跳过: 离线设备 %d/%d (%.0f%%) < 15%%",
                        total_offline, total, total_offline / total * 100)
            return False

        if force_by_timeout:
            logger.warning("L3 强制触发: 设备 %s 已离线 %.0fs (>3min)",
                           device_id[:8], dev_offline_secs)

        logger.warning("执行 adb kill-server (所有设备将短暂断开)...")
        self._last_kill_server_time = time.time()
        try:
            _sp_run_text([adb, "kill-server"],
                           capture_output=True, timeout=10)
            time.sleep(3)
            _sp_run_text([adb, "start-server"],
                           capture_output=True, timeout=15)
            time.sleep(5)
            manager.discover_devices()
            v = _sp_run_text([adb, "-s", device_id, "shell", "echo", "ok"],
                               capture_output=True, timeout=5)
            return v.returncode == 0 and "ok" in v.stdout
        except Exception:
            return False

    def _recovery_l4_usb_power_cycle(self, device_id: str,
                                      is_wifi: bool) -> bool:
        """Level 4: USB hub power cycle via uhubctl (hardware-level recovery)."""
        if is_wifi:
            logger.info("WiFi 设备不支持 USB 电源循环，跳过 L4")
            return False

        if self._uhubctl_available is None:
            self._uhubctl_available = _check_uhubctl()

        if not self._uhubctl_available:
            logger.info("uhubctl 不可用，跳过 L4 USB 电源循环")
            return False

        try:
            logger.info("尝试 USB 电源循环: %s", device_id[:8])
            r = _sp_run_text(
                ["uhubctl", "-a", "cycle", "-d", "5"],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0:
                logger.info("USB 电源已循环，等待设备重新枚举...")
                time.sleep(10)
                v = _sp_run_text(
                    ["adb", "-s", device_id, "shell", "echo", "ok"],
                    capture_output=True, timeout=5,
                )
                return v.returncode == 0 and "ok" in v.stdout
        except Exception as e:
            logger.debug("USB 电源循环失败: %s", e)
        return False

    # ── Task cancellation on disconnect ───────────────────────────────────

    def _cancel_running_tasks(self, device_id: str):
        """Send cancel signal to tasks running on a disconnected device."""
        try:
            from .worker_pool import get_worker_pool
            pool = get_worker_pool()
            task_id = pool._active_tasks.get(device_id)
            if task_id:
                pool.cancel_task(task_id)
                logger.info("已发送取消信号: task=%s device=%s",
                            task_id[:8], device_id[:8])
        except Exception as e:
            logger.debug("取消任务失败: %s", e)

    # ── Scrcpy stream cleanup / restore ──────────────────────────────────

    def _cleanup_scrcpy_session(self, device_id: str):
        """Stop and record scrcpy sessions when device disconnects."""
        try:
            from .scrcpy_manager import get_scrcpy_manager
            mgr = get_scrcpy_manager()
            session = mgr.get_session(device_id)
            if session and session.is_running:
                self._streaming_before_disconnect.add(device_id)
                mgr.stop_session(device_id)
                logger.info("已清理掉线设备的 scrcpy 会话: %s", device_id[:8])
        except Exception as e:
            logger.debug("清理 scrcpy 会话失败: %s", e)

    _last_fp_sync: float = 0
    _FP_SYNC_INTERVAL: int = 60  # sync every 60 seconds

    def _periodic_fingerprint_sync(self, manager):
        """Periodically re-collect fingerprints and sync registry for all online devices."""
        now = time.time()
        if now - self._last_fp_sync < self._FP_SYNC_INTERVAL:
            return
        self._last_fp_sync = now

        try:
            from src.device_control.device_registry import get_device_registry
            from src.utils.wallpaper_generator import get_wallpaper_auto_manager

            registry = get_device_registry()
            all_reg = registry.get_all()
            wp_mgr = get_wallpaper_auto_manager()

            connected = manager.get_connected_devices()
            updated = 0

            for dev in connected:
                did = dev.device_id
                if not dev.fingerprint:
                    manager._collect_fingerprint(did)
                    if dev.fingerprint:
                        manager._try_fingerprint_migration(did)
                        updated += 1

                fp = dev.fingerprint
                if not fp:
                    continue

                entry = registry.lookup(fp)
                if entry:
                    if entry.get("current_serial") != did:
                        old_s = registry.update_serial(fp, did)
                        if old_s:
                            registry.migrate_serial(old_s, did, PROJECT_ROOT)
                            updated += 1
                            logger.info("[指纹同步] 串号更新: %s → %s (编号#%02d)",
                                        old_s[:8], did[:8], entry.get("number", 0))
                else:
                    placeholder_key = f"serial:{did}"
                    if placeholder_key in all_reg:
                        old_entry = all_reg[placeholder_key]
                        num = old_entry.get("number", registry.next_available_number())
                        registry.register(
                            fp, did, num, f"{num:02d}号",
                            imei=dev.imei, hw_serial=dev.hw_serial,
                            android_id=dev.android_id, model=dev.model,
                        )
                        with registry._lock:
                            reg_data = registry._load()
                            reg_data.pop(placeholder_key, None)
                            registry._save(reg_data)
                        updated += 1
                        logger.info("[指纹同步] 升级占位符 %s → fp=%s (#%02d)",
                                    did[:8], fp[:12], num)

            # Sync aliases from registry
            if updated > 0:
                aliases_path = config_file("device_aliases.json")
                try:
                    if aliases_path.exists():
                        with open(aliases_path, "r", encoding="utf-8") as f:
                            aliases = json.load(f)
                    else:
                        aliases = {}
                except Exception:
                    aliases = {}

                changed = False
                current_reg = registry.get_all()
                for entry in current_reg.values():
                    serial = entry.get("current_serial", "")
                    num = entry.get("number", 0)
                    if serial and num > 0:
                        if serial not in aliases or aliases[serial].get("number") != num:
                            aliases[serial] = {
                                "number": num,
                                "alias": f"{num:02d}号",
                                "display_name": entry.get("model", f"Phone-{num}"),
                            }
                            changed = True

                if changed:
                    aliases_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(aliases_path, "w", encoding="utf-8") as f:
                        json.dump(aliases, f, ensure_ascii=False, indent=2)

                logger.info("[指纹同步] 同步完成: %d 条更新", updated)

        except Exception as e:
            logger.debug("[指纹同步] 异常: %s", e)

    @staticmethod
    def _auto_deploy_wallpaper(manager, device_id: str):
        """Auto-deploy numbered wallpaper when device comes online.

        先做 MIUI 安全硬化（device-online 第一时刻），防止部署壁纸过程被手机管家拦下。
        硬化是幂等 + 持久化跳过的，对非 MIUI 设备无副作用。
        """
        try:
            from src.host.routers.devices_core import _ensure_hardened, _ensure_ime_unified
            _ensure_hardened(device_id)
            _ensure_ime_unified(device_id)
        except Exception as e:
            logger.debug("MIUI 硬化/IME 统一跳过 %s: %s", device_id[:8], e)
        try:
            from src.utils.wallpaper_generator import get_wallpaper_auto_manager
            wp = get_wallpaper_auto_manager()
            wp.on_device_online(manager, device_id)
        except Exception as e:
            logger.debug("壁纸自动部署跳过 %s: %s", device_id[:8], e)
        # 兜底：wp.on_device_online 内部 _save_aliases 是整体覆盖（不 merge），
        # 会把 _ensure_hardened 写入的 miui_hardened_at 冲掉。这里在 wp 之后补写一次。
        try:
            from src.host.routers.devices_core import _HARDENED_SERIALS, _mark_hardened
            if device_id in _HARDENED_SERIALS:
                _mark_hardened(device_id)
        except Exception:
            pass

    @staticmethod
    def _pre_push_scrcpy(device_id: str):
        """Pre-push scrcpy-server to device so streaming starts instantly."""
        import threading
        def _do():
            try:
                from .scrcpy_manager import get_scrcpy_manager
                get_scrcpy_manager().pre_push_server(device_id)
            except Exception as e:
                logger.debug("scrcpy 预推送跳过 %s: %s", device_id[:8], e)
        threading.Thread(target=_do, daemon=True).start()

    def _restore_scrcpy_session(self, device_id: str):
        """Restart scrcpy stream if the device was streaming before disconnect."""
        if device_id not in self._streaming_before_disconnect:
            return
        self._streaming_before_disconnect.discard(device_id)
        try:
            from .scrcpy_manager import get_scrcpy_manager
            mgr = get_scrcpy_manager()
            session = mgr.start_session(device_id)
            if session:
                logger.info("已自动恢复 scrcpy 流: %s", device_id[:8])
                metrics.add_alert(
                    "info", device_id, "",
                    alert_code="SCRCPY_STREAM_RESTORED", params={},
                )
            else:
                logger.warning("scrcpy 流恢复失败: %s", device_id[:8])
        except Exception as e:
            logger.debug("恢复 scrcpy 会话失败: %s", e)

    def _check_stuck_tasks(self):
        """Detect stuck tasks using per-type thresholds; cancel orphan threads."""
        try:
            from .database import get_conn
            from .task_store import set_task_result
            from .worker_pool import get_worker_pool

            from src.host.task_store import _alive_sql

            _aq = _alive_sql()
            with get_conn() as conn:
                rows = conn.execute(
                    f"SELECT task_id, device_id, type, updated_at FROM tasks "
                    f"WHERE status = 'running' AND {_aq}"
                ).fetchall()

            if not rows:
                return

            pool = get_worker_pool()
            for row in rows:
                task_id, device_id, task_type, updated_at = row
                if not updated_at:
                    continue
                try:
                    dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    elapsed = time.time() - dt.timestamp()
                except Exception:
                    continue

                threshold = _stuck_threshold_for_type(task_type)
                if elapsed > threshold:
                    logger.warning("[卡死检测] 任务 %s (设备=%s, 类型=%s) "
                                    "已运行 %d 秒 (阈值=%ds)，标记为失败",
                                    task_id[:8], (device_id or "?")[:8],
                                    task_type, int(elapsed), threshold)
                    pool.cancel_task(task_id)
                    set_task_result(task_id, False,
                                   error=f"任务超时 ({int(elapsed)}秒)，已被健康监控终止")
                    metrics.add_alert(
                        "warning",
                        device_id or "system",
                        "",
                        alert_code="TASK_STUCK_TERMINATED",
                        params={
                            "task_id": task_id[:8],
                            "minutes": str(int(elapsed // 60)),
                        },
                    )
        except Exception as e:
            logger.debug("[卡死检测] 检查失败: %s", e)

    def _auto_tune_compliance(self):
        """Periodically analyze A/B experiment data to tune compliance."""
        if not hasattr(self, "_tune_counter"):
            self._tune_counter = 0
        self._tune_counter += 1
        if self._tune_counter % 10 != 0:
            return
        try:
            from ..behavior.adaptive_compliance import get_adaptive_compliance
            ac = get_adaptive_compliance()
            adjustments = ac.auto_tune_from_experiments()
            if adjustments:
                logger.info("[自适应调优] 基于 A/B 数据调整了 %d 项参数", len(adjustments))
        except Exception as e:
            logger.debug("[自适应调优] 失败: %s", e)

    def _check_screen_anomalies(self, manager, connected_ids: set):
        """Run fast screen anomaly detection on connected devices."""
        if not hasattr(self, "_anomaly_counter"):
            self._anomaly_counter = 0
        self._anomaly_counter += 1
        if self._anomaly_counter % 5 != 0:
            return
        try:
            from src.behavior.screen_anomaly import get_anomaly_detector
            from src.host.alert_message_context import approximate_english_message

            detector = get_anomaly_detector()
            for did in connected_ids:
                try:
                    result = detector.detect_and_recover(did, manager)
                    if result and result.severity.value == "critical":
                        desc = result.description or ""
                        desc_en = approximate_english_message(desc)
                        metrics.add_alert(
                            "critical",
                            did,
                            "",
                            alert_code="SCREEN_ANOMALY_CRITICAL",
                            params={
                                "atype": result.anomaly_type.value,
                                "desc": desc,
                                "atype_en": result.anomaly_type.value,
                                "desc_en": desc_en,
                            },
                        )
                except Exception:
                    pass
        except Exception as e:
            logger.debug("[屏幕异常检测] 失败: %s", e)

    @staticmethod
    def _device_has_active_task(device_id: str) -> bool:
        """Check if a device currently has a running task in the worker pool."""
        try:
            from .worker_pool import get_worker_pool
            pool = get_worker_pool()
            return device_id in pool._active_tasks.values()
        except Exception:
            return False

    @staticmethod
    def _measure_adb_latency(manager, device_id: str):
        """Measure ADB round-trip latency for health scoring."""
        adb = getattr(manager, 'adb_path', 'adb')
        try:
            t0 = time.time()
            _sp_run_text(
                [adb, "-s", device_id, "shell", "echo", "ok"],
                capture_output=True, timeout=5,
            )
            latency_ms = (time.time() - t0) * 1000
            metrics.record_adb_latency(device_id, latency_ms)
        except Exception:
            metrics.record_adb_latency(device_id, 5000)

    def _check_u2_deep(self, manager, device_id: str) -> bool:
        """u2 health check with graduated recovery: reconnect → atx-agent restart."""
        d = manager.get_u2(device_id)
        if not d:
            return False
        try:
            d.info
            return True
        except Exception:
            logger.warning("设备 %s u2 连接失效，重连中...", device_id[:8])

        for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
            d2 = manager.get_u2(device_id, force_reconnect=True)
            if d2:
                try:
                    d2.info
                    metrics.inc_reconnect()
                    metrics.record_u2_reconnect(device_id)
                    logger.info("设备 %s u2 重连成功 (第%d次尝试)",
                                device_id[:8], attempt)
                    _emit_health_event("device.u2_reconnected",
                                       device_id=device_id, attempt=attempt)
                    return True
                except Exception:
                    pass
            time.sleep(2 * attempt)

        if self._try_restart_atx_agent(manager, device_id):
            metrics.record_u2_reconnect(device_id)
            return True

        metrics.add_alert(
            "error",
            device_id,
            "",
            alert_code="U2_DEEP_RECONNECT_FAILED",
            params={},
        )
        _emit_health_event("device.u2_failed", device_id=device_id)
        return False

    @staticmethod
    def _try_restart_atx_agent(manager, device_id: str) -> bool:
        """Last-resort: kill and restart atx-agent on the device, then reconnect u2."""
        adb = getattr(manager, 'adb_path', 'adb')
        try:
            logger.info("尝试重启设备 %s 的 atx-agent...", device_id[:8])
            _sp_run_text(
                [adb, "-s", device_id, "shell",
                 "pkill", "-f", "atx-agent"],
                capture_output=True, timeout=5,
            )
            time.sleep(2)
            _sp_run_text(
                [adb, "-s", device_id, "shell",
                 "/data/local/tmp/atx-agent", "server", "-d"],
                capture_output=True, timeout=10,
            )
            time.sleep(3)
            d = manager.get_u2(device_id, force_reconnect=True)
            if d:
                d.info
                logger.info("atx-agent 重启后 u2 恢复成功: %s", device_id[:8])
                metrics.inc_reconnect()
                metrics.add_alert(
                    "info", device_id, "",
                    alert_code="ATX_AGENT_RESTART_OK", params={},
                )
                return True
        except Exception as e:
            logger.debug("atx-agent 重启失败 %s: %s", device_id[:8], e)
        return False

    @staticmethod
    def _check_app_running(manager, device_id: str) -> bool:
        """Check if the expected app is in the foreground."""
        try:
            d = manager.get_u2(device_id)
            if not d:
                return False
            current = d.app_current()
            pkg = current.get("package", "")
            return bool(pkg and pkg != "com.android.launcher3")
        except Exception:
            return False

    def _try_restart_app(self, manager, device_id: str):
        """Restart the expected app if a task is active but the app is not foreground."""
        from .worker_pool import get_worker_pool
        pool = get_worker_pool()
        active_task = pool._active_tasks.get(device_id)
        if not active_task:
            return

        from .task_store import get_task
        task = get_task(active_task)
        if not task:
            return

        task_type = task.get("type", "")
        expected_pkg = _task_type_to_package(task_type)
        if not expected_pkg:
            return

        logger.warning("[应用重启] 设备 %s 正在执行 %s 任务但应用不在前台",
                        device_id[:8], task_type)
        try:
            d = manager.get_u2(device_id)
            if not d:
                return
            for pkg in expected_pkg:
                try:
                    d.app_start(pkg)
                    time.sleep(3)
                    current = d.app_current()
                    if pkg in current.get("package", ""):
                        logger.info("[应用重启] 设备 %s 成功重启 %s",
                                    device_id[:8], pkg)
                        metrics.add_alert(
                            "info",
                            device_id,
                            "",
                            alert_code="APP_AUTO_RESTART_OK",
                            params={"pkg": pkg},
                        )
                        return
                except Exception:
                    continue
            metrics.add_alert(
                "warning",
                device_id,
                "",
                alert_code="APP_AUTO_RESTART_FAIL",
                params={},
            )
        except Exception as e:
            logger.debug("[应用重启] 设备 %s 重启失败: %s", device_id[:8], e)

    def _snapshot_running_tasks(self, device_id: str):
        """Remember tasks that were running on this device when it disconnected."""
        try:
            from .database import get_conn
            from .task_store import _alive_sql

            _aq = _alive_sql()
            with get_conn() as conn:
                rows = conn.execute(
                    f"SELECT task_id FROM tasks WHERE status = 'running' "
                    f"AND device_id = ? AND {_aq}",
                    (device_id,),
                ).fetchall()
            task_ids = [r[0] for r in rows]
            if task_ids:
                self._interrupted_tasks[device_id] = task_ids
                logger.info("设备 %s 掉线时有 %d 个运行中任务: %s",
                            device_id[:8], len(task_ids),
                            [t[:8] for t in task_ids])
        except Exception as e:
            logger.debug("快照运行中任务失败: %s", e)

    def _recover_interrupted_tasks(self, device_id: str):
        """Requeue tasks with checkpoint carry-over for resumable execution."""
        task_ids = self._interrupted_tasks.pop(device_id, [])
        if not task_ids:
            return

        import json
        from .task_store import (set_task_result, create_task,
                                 get_checkpoint, save_checkpoint)
        from .worker_pool import get_worker_pool
        from .database import get_conn

        recovered = 0
        for task_id in task_ids:
            try:
                with get_conn() as conn:
                    row = conn.execute(
                        "SELECT type, params, status, checkpoint FROM tasks "
                        "WHERE task_id = ?",
                        (task_id,),
                    ).fetchone()
                if not row:
                    continue

                status = row[2]
                if status == "completed":
                    continue

                set_task_result(task_id, False, error="设备掉线中断")
                if not _effective_auto_recover():
                    logger.info("设备 %s 重连，任务 %s 已标记失败，自动恢复已关闭", device_id[:8], task_id[:8])
                    continue

                task_type = row[0]
                params = json.loads(row[1]) if row[1] else {}

                checkpoint_data = None
                try:
                    checkpoint_data = json.loads(row[3]) if row[3] else None
                except (json.JSONDecodeError, TypeError):
                    pass

                if _effective_auto_recover() and _is_retryable_task(task_type):
                    if checkpoint_data:
                        params["_checkpoint"] = checkpoint_data
                        params["_resumed_from"] = task_id
                    params.setdefault("_created_via", "recovery")

                    new_id = create_task(
                        task_type=task_type,
                        device_id=device_id,
                        params=params,
                    )

                    if checkpoint_data:
                        save_checkpoint(new_id, checkpoint_data)
                        logger.info("恢复任务 %s → %s (类型=%s, 断点续传: %s)",
                                    task_id[:8], new_id[:8], task_type,
                                    json.dumps(checkpoint_data, ensure_ascii=False)[:100])
                    else:
                        logger.info("恢复任务 %s → 新任务 %s (类型=%s, 从头开始)",
                                    task_id[:8], new_id[:8], task_type)

                    pool = get_worker_pool()
                    from .executor import run_task
                    pool.submit(new_id, device_id, run_task, new_id)
                    recovered += 1

            except Exception as e:
                logger.debug("恢复任务 %s 失败: %s", task_id[:8], e)

        if recovered:
            metrics.add_alert(
                "info",
                device_id,
                "",
                alert_code="TASKS_RECOVERED_AFTER_RECONNECT",
                params={"count": str(recovered)},
            )
            _emit_health_event("device.tasks_recovered",
                               device_id=device_id, count=recovered)


    def record_account_interaction(self, device_id: str, success: bool, rate: float = 0.0):
        """Record a TikTok interaction attempt result for account health tracking."""
        with metrics._lock:
            hist = metrics._account_interaction_results.setdefault(device_id, deque(maxlen=10))
            hist.append((time.time(), success, rate))
            # Check for 3 consecutive failures
            recent = list(hist)[-3:]
            if len(recent) >= 3 and all(not r[1] for r in recent):
                metrics._account_health_alerts[device_id] = {
                    "triggered_at": time.time(),
                    "reason": "连续3次互动失败",
                    "last_rate": rate,
                }
            elif success and device_id in metrics._account_health_alerts:
                # Clear alert on success
                del metrics._account_health_alerts[device_id]

    def get_account_health_alert(self, device_id: str) -> Optional[dict]:
        """Return active health alert for device, or None."""
        with metrics._lock:
            return metrics._account_health_alerts.get(device_id)

    def get_all_account_alerts(self) -> Dict[str, dict]:
        """Return all active account health alerts."""
        with metrics._lock:
            return dict(metrics._account_health_alerts)


_monitor: Optional[HealthMonitor] = None


def get_health_monitor() -> Optional[HealthMonitor]:
    """Return the active HealthMonitor instance."""
    return _monitor


def start_monitor(config_path: str):
    global _monitor
    if _monitor and _monitor.is_alive():
        return
    _monitor = HealthMonitor(config_path)
    _monitor.start()


def stop_monitor():
    global _monitor
    mon = _monitor
    if mon:
        mon.stop()
        if mon.is_alive():
            mon.join(timeout=12)
        _monitor = None


def get_recovery_summary() -> dict:
    """Return recovery state summary for all devices (safe for WS push)."""
    if not _monitor:
        return {}
    result = {}
    for did, state in _monitor._recovery_state.items():
        level = state.get("level", 0)
        level_name = (_RECOVERY_LEVELS[level]["name"]
                      if level < len(_RECOVERY_LEVELS)
                      else "exhausted")
        result[did] = {
            "level": level,
            "level_name": level_name,
            "offline_sec": int(time.time() - state.get(
                "started_at", time.time())),
        }
    return result
