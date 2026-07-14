# -*- coding: utf-8 -*-
"""
智能调度引擎 — 基于健康评分 + 负载 + 任务亲和度的动态任务分配。

调度评分 = health_score * 0.40 + load_score * 0.35 + affinity_score * 0.25

健康评分: 直接使用 HealthMonitor 的综合评分 (0-100)
负载评分: 空闲=100, 1个任务=60, 2+=30, 设备锁定=0
亲和度:   该设备执行过同类任务的成功率 → 加权
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional

import json
import yaml


logger = logging.getLogger(__name__)

_WEIGHT_HEALTH = 0.40
_WEIGHT_LOAD = 0.35
_WEIGHT_AFFINITY = 0.25

# 设备冷却期默认值（秒）— 可通过 task_execution_policy.yaml 热覆盖
_COOLING_HARD_SEC = 60    # 刚完成任务: load_score - 30
_COOLING_SOFT_SEC = 180   # 轻度冷却:   load_score - 15

# 冷却参数热重载状态
_cooling_cfg_mtime: float = 0.0
_cooling_hard_sec: int = _COOLING_HARD_SEC
_cooling_soft_sec: int = _COOLING_SOFT_SEC
_cooling_cfg_lock = threading.Lock()


def _reload_cooling_config() -> tuple[int, int]:
    """从 task_execution_policy.yaml 热加载冷却参数ï¼ï¼蛟断阈値（mtime 比对，开销极低）"""
    global _cooling_cfg_mtime, _cooling_hard_sec, _cooling_soft_sec
    global _fail_pattern_threshold, _device_fail_threshold
    global _task_suspend_minutes, _device_suspend_minutes
    try:
        from src.host.device_registry import config_file
        path = config_file("task_execution_policy.yaml")
        mtime = path.stat().st_mtime
        if mtime == _cooling_cfg_mtime:
            return _cooling_hard_sec, _cooling_soft_sec
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        sched = data.get("scheduler") or {}
        _cooling_hard_sec = int(sched.get("cooling_hard_sec", _COOLING_HARD_SEC))
        _cooling_soft_sec = int(sched.get("cooling_soft_sec", _COOLING_SOFT_SEC))
        # 2026-05-13: 蛟断阈値热重载
        cb = data.get("scheduler_circuit_breaker") or {}
        _fail_pattern_threshold = int(cb.get("fail_pattern_threshold", 5))
        _device_fail_threshold = int(cb.get("device_fail_threshold", 15))
        _task_suspend_minutes = int(cb.get("task_suspend_minutes", 30))
        _device_suspend_minutes = int(cb.get("device_suspend_minutes", 60))
        _cooling_cfg_mtime = mtime
        logger.info("[scheduler] 冷却参数已热重载: hard=%ds soft=%ds "
                    "fail_thr=%d dev_thr=%d task_susp=%dm dev_susp=%dm",
                    _cooling_hard_sec, _cooling_soft_sec,
                    _fail_pattern_threshold, _device_fail_threshold,
                    _task_suspend_minutes, _device_suspend_minutes)
        try:
            from src.host.config_audit import record as _audit
            _audit("task_execution_policy.yaml", "scheduler",
                   {"cooling_hard_sec": _cooling_hard_sec,
                    "cooling_soft_sec": _cooling_soft_sec,
                    "fail_pattern_threshold": _fail_pattern_threshold,
                    "device_fail_threshold": _device_fail_threshold})
        except Exception:
            pass
    except Exception:
        pass
    return _cooling_hard_sec, _cooling_soft_sec


# 2026-05-13: 连续失败告警去重表（模块级，跨实例共享）
_fail_alert_last: Dict[str, float] = {}
_FAIL_ALERT_COOLDOWN = 1800   # 30 分钟内同一 key 至多一条告警

# 2026-05-13: 熟断阈値—支持通过 task_execution_policy.yaml 热重载
_fail_pattern_threshold: int = 5     # 任务类型级：连续失败 N 次触发告警ï¼,2N 次自动暂停
_device_fail_threshold: int = 15     # 设备级：跨类型总失败 N 次触发警告ï¼,2N 次熟断设备
_task_suspend_minutes: int = 30      # 任务类型级自动暂停时长（分钟）
_device_suspend_minutes: int = 60    # 设备级熟断时长（分钟）


class SmartScheduler:
    # 2026-05-13: 派发历史保留时长（24h）
    _DISPATCH_HISTORY_TTL = 86400

    def __init__(self):
        self._lock = threading.Lock()
        self._task_history: Dict[str, Dict[str, List[bool]]] = defaultdict(
            lambda: defaultdict(list)
        )
        # 2026-05-13: 每设备最后任务完成时间，用于冷却期惩罚
        self._last_task_end: Dict[str, float] = {}
        # 2026-05-13: 派发历史 ring-buffer：(ts, device_id, task_type, success)
        # 保留最近 24h，驱动 /devices/dispatch-heatmap API
        self._dispatch_buf: deque = deque()
        # 2026-05-13: 连续失败模式识别——记录每 (device:task_type) 的连续失败次数
        self._consecutive_fails: Dict[str, int] = {}
        # 2026-05-13: 设备级跨任务类型总连续失败计数（用于设备级熟断）
        self._device_fail_count: Dict[str, int] = {}
        # 2026-05-13: 任务失败溯源快照—每 (device:task_type) 保留最近 5 条失败操作记录
        self._fail_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=5)
        )
        # 启动时从磁盘恢复设备失败计数
        self._load_device_fails()

    def _save_device_fails(self) -> None:
        """2026-05-13: 将设备失败计数持久化到磁盘（防止重启清空导致熟断被绕过）。"""
        try:
            from src.host.device_registry import data_file
            path = data_file("scheduler_fails.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                snapshot = {k: v for k, v in self._device_fail_count.items() if v > 0}
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_device_fails(self) -> None:
        """2026-05-13: 启动时从磁盘恢复设备失败计数（渐进衰减策略需要跨重启保持计数）。"""
        try:
            from src.host.device_registry import data_file
            path = data_file("scheduler_fails.json")
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            with self._lock:
                for k, v in data.items():
                    if isinstance(v, int) and v > 0:
                        self._device_fail_count[k] = v
            logger.info("[scheduler] 设备失败计数已恢复: %d 张条目", len(self._device_fail_count))
        except Exception as e:
            logger.debug("[scheduler] 加载设备失败计数失败: %s", e)

    def record_task_result(self, device_id: str, task_type: str,
                           success: bool, error_msg: str = ""):
        """Record task result for affinity learning."""
        prefix = self._task_prefix(task_type)
        now = time.time()
        with self._lock:
            hist = self._task_history[device_id][prefix]
            hist.append(success)
            if len(hist) > 50:
                self._task_history[device_id][prefix] = hist[-25:]
            # 2026-05-13: 记录本次任务结束时间（无论成功失败）
            self._last_task_end[device_id] = now
            # 2026-05-13: 派发历史 ring-buffer—过期条目券剔
            self._dispatch_buf.append((now, device_id, task_type, success))
            cutoff = now - self._DISPATCH_HISTORY_TTL
            while self._dispatch_buf and self._dispatch_buf[0][0] < cutoff:
                self._dispatch_buf.popleft()
        # 连续失败模式检测（锁外执行，名义上是独立操作）
        self._check_failure_pattern(device_id, task_type, success, error_msg=error_msg)

    def select_device(self, task_type: str,
                      preferred: Optional[str] = None,
                      exclude: Optional[List[str]] = None) -> Optional[str]:
        """
        Select the best device for a task based on health, load, and affinity.

        Returns device_id or None if no devices available.
        """
        from .health_monitor import metrics
        from .worker_pool import get_worker_pool

        pool = get_worker_pool()
        candidates = []

        vpn_paused = set()
        try:
            from src.behavior.vpn_health import get_vpn_health_monitor
            vpn_mon = get_vpn_health_monitor()
            for did_check, st in vpn_mon.get_status().items():
                if st.get("paused"):
                    vpn_paused.add(did_check)
        except Exception:
            pass

        for did, status in metrics.device_status.items():
            if status.get("status") != "connected":
                continue
            if metrics.is_isolated(did):
                continue
            if did in vpn_paused:
                continue
            if exclude and did in exclude:
                continue
            candidates.append(did)

        if not candidates:
            return preferred

        if preferred and preferred in candidates:
            return preferred

        scores = {}
        for did in candidates:
            health = metrics.device_health_score(did).get("total", 50)
            load = self._load_score(pool, did)
            affinity = self._affinity_score(did, task_type)

            total = (health * _WEIGHT_HEALTH
                     + load * _WEIGHT_LOAD
                     + affinity * _WEIGHT_AFFINITY)
            scores[did] = {
                "total": round(total, 1),
                "health": health,
                "load": load,
                "affinity": affinity,
            }

        best = max(scores, key=lambda d: scores[d]["total"])
        logger.debug("智能调度: task=%s → 选择 %s (%.1f) 候选=%d",
                     task_type, best[:8], scores[best]["total"],
                     len(candidates))
        return best

    def get_scheduling_scores(self, task_type: str = "") -> Dict[str, dict]:
        """Return scheduling scores for all devices (for dashboard)."""
        from .health_monitor import metrics
        from .worker_pool import get_worker_pool

        pool = get_worker_pool()
        result = {}

        for did in metrics.device_status:
            health = metrics.device_health_score(did).get("total", 50)
            load = self._load_score(pool, did)
            affinity = self._affinity_score(did, task_type) if task_type else 70
            total = (health * _WEIGHT_HEALTH
                     + load * _WEIGHT_LOAD
                     + affinity * _WEIGHT_AFFINITY)
            # 2026-05-13: 冷却倒计时（前端可展示设备冷却状态）
            with self._lock:
                last_end = self._last_task_end.get(did, 0.0)
            with _cooling_cfg_lock:
                hard_sec, soft_sec = _reload_cooling_config()
            elapsed = time.time() - last_end
            cooling_remaining = max(0, int(soft_sec - elapsed)) if last_end else 0
            result[did] = {
                "total": round(total, 1),
                "health": health,
                "load": load,
                "affinity": affinity,
                "busy": pool.is_device_busy(did),
                "isolated": metrics.is_isolated(did),
                "cooling_remaining_sec": cooling_remaining,
                # 2026-05-13: 连续失败模式——key->count（仅该设备有记录时显示）
                "fail_streaks": {
                    tt.split(":", 1)[1]: cnt
                    for tt, cnt in self._consecutive_fails.items()
                    if tt.startswith(f"{did}:") and cnt > 0
                },
            }
        return result

    def _load_score(self, pool, device_id: str) -> int:
        """Score based on current task load + cooling penalty.

        2026-05-13: 加入冷却期惩罚，刚完成任务的设备 load_score 降低，
        避免同一设备在短时间内被连续派发，降低风控密度。
        """
        status = pool.get_status()
        active_map = status.get("active_tasks", {})
        running_on = sum(1 for did in active_map.values() if did == device_id)
        device_locks = status.get("device_locks", {})
        is_locked = device_locks.get(device_id) == "busy"

        if is_locked:
            return 10
        if running_on >= 2:
            return 20
        if running_on == 1:
            return 50

        # 设备空闲，但检查是否在冷却期内（参数热重载）
        base = 100
        with self._lock:
            last_end = self._last_task_end.get(device_id, 0.0)
        with _cooling_cfg_lock:
            hard_sec, soft_sec = _reload_cooling_config()
        elapsed = time.time() - last_end
        if elapsed < hard_sec:
            base -= 30   # 刚完成: 100 → 70，优先派给更空闲的设备
        elif elapsed < soft_sec:
            base -= 15   # 轻度冷却: 100 → 85
        return base

    def _affinity_score(self, device_id: str, task_type: str) -> int:
        """Score based on historical success rate for this task type."""
        prefix = self._task_prefix(task_type)
        with self._lock:
            hist = self._task_history.get(device_id, {}).get(prefix, [])

        if not hist:
            return 70

        recent = hist[-10:]
        success_rate = sum(1 for r in recent if r) / len(recent)
        return int(50 + success_rate * 50)

    def _check_failure_pattern(self, device_id: str, task_type: str,
                               success: bool, error_msg: str = "") -> None:
        """2026-05-13: 检测并告警连续失败模式。

        成功时重置计数；失败倒计，达阈値时推送告警并写入事件流。
        30 分钟告警冷却，防止持续失败时刷屏。
        """
        key = f"{device_id}:{task_type}"
        if success:
            with self._lock:
                self._consecutive_fails.pop(key, None)
                cur = self._device_fail_count.get(device_id, 0)
                if cur > 0:
                    self._device_fail_count[device_id] = max(0, cur - 2)
            return
        # 2026-05-13: 记录失败溯源快照
        with self._lock:
            self._fail_history[key].append({
                "ts": time.time(),
                "err": (error_msg or "")[:120],
            })
        with self._lock:
            count = self._consecutive_fails.get(key, 0) + 1
            self._consecutive_fails[key] = count
        if count < _fail_pattern_threshold:
            return
        now = time.time()
        last = _fail_alert_last.get(key, 0.0)
        if now - last < _FAIL_ALERT_COOLDOWN:
            return
        _fail_alert_last[key] = now
        with self._lock:
            dev_total = self._device_fail_count.get(device_id, 0) + 1
            self._device_fail_count[device_id] = dev_total
        if dev_total >= _device_fail_threshold * 2:
            try:
                from src.host.task_dispatcher import suspend_device
                suspend_device(device_id, minutes=_device_suspend_minutes)
                logger.warning("[scheduler] 设备级熟断: device=%s 跨类型总失败=%d → 暂停 %dmin",
                               device_id[:12], dev_total, _device_suspend_minutes)
            except Exception:
                pass
            with self._lock:
                self._device_fail_count.pop(device_id, None)
            self._save_device_fails()
        auto_suspended = count >= _fail_pattern_threshold * 2
        if auto_suspended:
            try:
                from src.host.task_dispatcher import suspend_task_type
                suspend_task_type(task_type, device_id=device_id,
                                  minutes=_task_suspend_minutes,
                                  reason="auto", triggered_by="scheduler")
            except Exception:
                pass
        with self._lock:
            recent = list(self._fail_history.get(key, []))[-3:]
        fail_snippets = "; ".join(
            r["err"][:60] for r in recent if r.get("err")
        ) or "无错误详情"
        action_hint = (
            f"已自动暂停 {_task_suspend_minutes} 分钟，请排查后手动恢复"
            if auto_suspended else "建议检查账号状态或 UI 变更"
        )
        msg = (
            f"[调度] 连续失败模式: device={device_id[:12]} "
            f"type={task_type} 连续={count} 次，{action_hint}"
            f" | 近期失败: {fail_snippets}"
        )
        logger.warning("[scheduler] %s", msg)
        try:
            from src.host.alert_notifier import AlertNotifier
            AlertNotifier.get().notify("warning", "", msg)
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event
            push_event("scheduler.fail_pattern", {
                "device_id": device_id,
                "task_type": task_type,
                "consecutive_fails": count,
                "auto_suspended": auto_suspended,
                "recent_errors": [r.get("err", "") for r in recent],
            })
        except Exception:
            pass

    def get_dispatch_heatmap(self, hours: int = 24) -> dict:
        """2026-05-13: 返回最近 N 小时的按小时聚合派发热图数据。

        每个桶 (hour_slot × device_id × task_type) 包含 total/success/fail 计数。
        供 GET /devices/dispatch-heatmap 端点使用，可视化设备繁忙程度和任务分布。

        Returns:
            {
              "hours": int,
              "generated_at": str,          # ISO timestamp
              "total_entries": int,
              "by_hour": {
                "YYYY-MM-DDTHH": {
                  "device_id:task_type": {"total": N, "success": N, "fail": N}
                }
              },
              "by_device": {
                "device_id": {"total": N, "success": N, "fail": N}
              }
            }
        """
        import math
        from datetime import datetime as _dt, timezone as _tz

        hours = max(1, min(hours, 72))
        cutoff = time.time() - hours * 3600
        by_hour: dict = {}
        by_device: dict = {}

        with self._lock:
            entries = [(ts, did, tt, ok)
                       for ts, did, tt, ok in self._dispatch_buf
                       if ts >= cutoff]

        for ts, device_id, task_type, success in entries:
            # 小时桶：本地时间 "YYYY-MM-DDTHH"
            hour_slot = _dt.fromtimestamp(ts).strftime("%Y-%m-%dT%H")
            key = f"{device_id}:{task_type}"

            # by_hour
            hour_data = by_hour.setdefault(hour_slot, {})
            bucket = hour_data.setdefault(key, {"total": 0, "success": 0, "fail": 0})
            bucket["total"] += 1
            if success:
                bucket["success"] += 1
            else:
                bucket["fail"] += 1

            # by_device
            dev_bucket = by_device.setdefault(device_id, {"total": 0, "success": 0, "fail": 0})
            dev_bucket["total"] += 1
            if success:
                dev_bucket["success"] += 1
            else:
                dev_bucket["fail"] += 1

        return {
            "hours": hours,
            "generated_at": _dt.now(_tz.utc).isoformat(),
            "total_entries": len(entries),
            "by_hour": dict(sorted(by_hour.items())),
            "by_device": by_device,
        }

    @staticmethod
    def _task_prefix(task_type: str) -> str:
        if "_" in task_type:
            return task_type.split("_")[0] + "_"
        return task_type


_scheduler: Optional[SmartScheduler] = None
_scheduler_lock = threading.Lock()


def get_smart_scheduler() -> SmartScheduler:
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = SmartScheduler()
    return _scheduler
