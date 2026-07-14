# -*- coding: utf-8 -*-
"""任务派发工具 - 统一的 "create_task 之后如何让它真正跑起来" 的入口。

背景：
    历史上 routers/tasks.py 的 POST /tasks 里内联写了一段"先问集群 Worker，
    失败再本机 WorkerPool.submit"的逻辑。其他入口（AI 快捷指令、风控降级等）
    经常漏掉这一段，只 create_task 不 submit，任务就卡在 pending 了。
    本模块把派发逻辑抽出来，供所有入口复用。

    同时提供 pending_rescue_loop：在 host 进程启动时注册的后台扫描器，
    定期把"写进 DB 却没被线程池登记"的 pending 任务重新补一遍 submit，
    兜住 get_retry_ready_tasks 无调用方、进程重启后内存队列丢失等情况。
"""

from __future__ import annotations
import json
import logging
import os
import queue
import random
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import yaml

from src.host.device_registry import config_dir, config_file, data_file

logger = logging.getLogger(__name__)

# pending_rescue_loop 默认轮询间隔（秒）
_RESCUE_INTERVAL_SEC = 15
# 多久未更新的 pending 才判定为"僵尸"（避免抢走刚 create 还没 submit 的任务）
_ORPHAN_AGE_SEC = 120
# running 任务多久未 update 才判定为孤儿 (跨 server 重启场景):
# - 一般业务方法每完成一步会 set_task_running / progress_cb 刷 updated_at
# - 阈值仍未 update + 不在 pool._futures 里 → 进程已死, 标 fail
# - 阈值 = executor 最长 timeout (tiktok_auto=7200s) + 10min buffer
#   宁可晚回收 1h, 不可误杀正常 2h 的 tiktok_auto / tiktok_warmup 任务.
#   用户报的 "5h running" 场景远超本阈值, 仍能被准确回收.
_ORPHAN_RUNNING_AGE_SEC = 7800
# 每轮最多补派的任务数，避免洪水
_RESCUE_BATCH_LIMIT = 100

# 2026-05-13: task_type 成功率扫描节流（秒）— 默认与配置文件一致
_TASK_TYPE_ALERT_LAST_CHECK: float = 0.0
_TASK_TYPE_ALERT_LOCK = threading.Lock()
# 2026-05-13 v2: 状态化去重——记录每个 task_type 当前是否处于告警状态
# key: task_type, value: 首次触发时间戳。只在状态变化时（首次触发/恢复）通知，
# 30 分钟后允许重复告警（防止长期沉默掩盖持续问题）。
_ALERT_STATE: Dict[str, float] = {}   # task_type → 首次触发 ts（0 = 未告警）
_ALERT_REPING_SEC = 1800              # 30 分钟后允许重复推送


def _check_task_type_success_rates() -> None:
    """扫描各 task_type 的成功率，低于阈值时状态化告警（防止告警风暴）。

    去重机制:
      - 新进入告警状态: 立即推送（仅一次）
      - 持续告警: 30 分钟后再次推送（re-ping），而非每 5 分钟刷屏
      - 恢复正常: 推送"已恢复"通知并清除告警状态
    配置来源: config/task_execution_policy.yaml -> task_type_alert 节。
    """
    global _TASK_TYPE_ALERT_LAST_CHECK
    with _TASK_TYPE_ALERT_LOCK:
        now = time.time()
        try:
            path = config_file("task_execution_policy.yaml")
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            return
        alert_cfg = cfg.get("task_type_alert") or {}
        if not alert_cfg.get("enabled", True):
            return
        interval = float(alert_cfg.get("check_interval_sec", 300))
        if now - _TASK_TYPE_ALERT_LAST_CHECK < interval:
            return
        _TASK_TYPE_ALERT_LAST_CHECK = now

        min_samples = int(alert_cfg.get("min_samples", 10))
        default_threshold = float(alert_cfg.get("default_min_success_rate", 0.55))
        overrides: dict = alert_cfg.get("overrides") or {}
        # 恢复判定使用 5% 的滞后区间，避免在阈值边界反复抖动
        hysteresis = float(alert_cfg.get("recovery_hysteresis", 0.05))

    # 读取指标（锁外执行，避免持锁太久）
    try:
        from .health_monitor import metrics as _metrics
        stats = _metrics.get_task_type_stats()
    except Exception:
        return

    newly_fired: list[str] = []   # 新触发（首次 / re-ping）
    recovered: list[str] = []     # 从告警状态恢复到正常

    for task_type, s in stats.items():
        total = s.get("total", 0)
        if total < min_samples:
            continue
        rate = s.get("success_rate") or 0.0
        threshold = float(overrides.get(task_type, default_threshold))
        in_alert = task_type in _ALERT_STATE

        if rate < threshold:
            if not in_alert:
                # 新进入告警状态
                _ALERT_STATE[task_type] = now
                newly_fired.append(
                    f"{task_type}: {rate*100:.1f}% (阈值 {threshold*100:.0f}%,"
                    f" {s['success']}成/{s['fail']}败) ⚠️ 新告警"
                )
            elif now - _ALERT_STATE[task_type] >= _ALERT_REPING_SEC:
                # 持续告警超过 re-ping 周期，更新时间戳并重复推送
                _ALERT_STATE[task_type] = now
                newly_fired.append(
                    f"{task_type}: {rate*100:.1f}% (阈值 {threshold*100:.0f}%,"
                    f" {s['success']}成/{s['fail']}败) 🔄 持续告警"
                )
        else:
            if in_alert and rate >= threshold + hysteresis:
                # 成功率越过 (阈值 + 滞后) → 恢复通知
                del _ALERT_STATE[task_type]
                recovered.append(
                    f"{task_type}: {rate*100:.1f}% ✅ 已恢复"
                )

    if newly_fired:
        msg = (f"[监控] {len(newly_fired)} 个任务类型成功率异常:\n"
               + "\n".join(f"  • {x}" for x in newly_fired))
        logger.warning("[task_alert] %s", msg)
        try:
            from .event_stream import push_event
            push_event("task.type_low_success_rate",
                       {"count": len(newly_fired),
                        "types": [x.split(":")[0] for x in newly_fired]})
        except Exception:
            pass
        try:
            from .alert_notifier import AlertNotifier
            AlertNotifier.get().notify("warning", "", msg)
        except Exception:
            pass

    if recovered:
        rec_msg = ("[监控] 任务成功率已恢复:\n"
                   + "\n".join(f"  • {x}" for x in recovered))
        logger.info("[task_alert] %s", rec_msg)
        try:
            from .alert_notifier import AlertNotifier
            AlertNotifier.get().notify("info", "", rec_msg)
        except Exception:
            pass

# ── 2026-05-13: 任务类型滑动窗口速率限制 ────────────────────────────────
# key: "device_id:task_type" → deque[timestamp]（仅记录实际派发成功的时间戳）
# 滑动窗口精确统计"过去 window_hours 小时内真实派发次数"，避免令牌桶的边界爆发。
# 超限时 dispatch_after_create 返回 mode=rate_limited，任务留 pending 等下轮重试。
_rate_windows: Dict[str, deque] = {}
_rate_windows_lock = threading.Lock()
# 速率限制配置缓存（mtime 热重载）
_rate_cfg_mtime: float = 0.0
_rate_cfg_cache: Dict[str, dict] = {}
_rate_cfg_lock = threading.Lock()
# 每个 key 的最后一次告警时间（防止同一限流每15s打一条日志）
_rate_warn_last: Dict[str, float] = {}
# 持久化节流：不超过 60s 写一次磁盘
_rate_save_last: float = 0.0
_RATE_SAVE_INTERVAL = 60
# 2026-05-13: 动态豁免表 — key → 豁免到期时间戳
# key 格式: "*:task_type"（全设备豁免）或 "device_id:task_type"（单设备豁免）
# 通过 set_rate_bypass() / POST /tasks/rate-bypass 设置
_rate_bypasses: Dict[str, float] = {}
_rate_bypasses_lock = threading.Lock()

# 2026-05-13: 自动暂停表 — key → 恢复时间戳（连续失败 2× 阈值时由调度器写入）
# 优先级高于豁免：暂停期间任何豁免都无法绕过（防止自动恢复期间再次触发风控）
_rate_suspensions: Dict[str, float] = {}
_rate_suspensions_lock = threading.Lock()

# 2026-05-13: 暂停到期 Timer 表 — 到期时主动推送恢复通知
_suspension_timers: Dict[str, threading.Timer] = {}
_suspension_timers_lock = threading.Lock()

# 2026-05-13: 暂停来源元数据 — {key: {reason, triggered_by, created_at}}
_suspension_meta: Dict[str, dict] = {}
_suspension_meta_lock = threading.Lock()

_SUSPENSIONS_FILE = "suspensions.json"


def _save_suspensions_to_disk() -> None:
    """2026-05-13: 将当前活跃暂停持久化到磁盘（防止重启绕过熔断）。"""
    try:
        path = data_file(_SUSPENSIONS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with _rate_suspensions_lock:
            active = {k: v for k, v in _rate_suspensions.items() if v > now}
        path.write_text(json.dumps(active, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_suspensions_from_disk() -> None:
    """2026-05-13: 启动时恢复持久化暂停，重新调度 Timer。

    防止通过快速重启绕过熔断保护。恢复时只保留尚未到期的条目。
    """
    try:
        path = data_file(_SUSPENSIONS_FILE)
        if not path.exists():
            return
        data: dict = json.loads(path.read_text(encoding="utf-8"))
        now = time.time()
        restored = 0
        for key, expiry in data.items():
            remaining = expiry - now
            if remaining <= 0:
                continue
            with _rate_suspensions_lock:
                _rate_suspensions[key] = expiry
            with _suspension_timers_lock:
                t = threading.Timer(remaining, _on_suspension_expired, args=(key,))
                t.daemon = True
                t.start()
                _suspension_timers[key] = t
            restored += 1
        if restored:
            logger.info("[auto_suspend] 从磁盘恢复 %d 条活跃暂停", restored)
    except Exception as e:
        logger.debug("[auto_suspend] 加载持久化暂停失败: %s", e)


def _save_rate_windows() -> None:
    """2026-05-13: 将满足配置限制的 key 的时间戳序列原子写入 data/rate_windows.json。

    跨重启恢复：防止通过快速重启绕过每小时限额。
    只写入当前配置中有限制的类型（过小数据）。
    """
    global _rate_save_last
    try:
        with _rate_cfg_lock:
            cfg = _load_rate_limits()
        now = time.time()
        # 最大窗口 = 配置中所有 window_hours 的最大值
        max_window = max(
            (float(v.get("window_hours", 1.0)) * 3600 for v in cfg.values()),
            default=3600
        )
        data: Dict[str, list] = {}
        with _rate_windows_lock:
            for key, win in _rate_windows.items():
                task_type = key.split(":", 1)[-1]
                if task_type not in cfg:
                    continue  # 无限制的类型不写入
                valid = [ts for ts in win if ts >= now - max_window]
                if valid:
                    data[key] = valid
        if not data:
            return
        path = data_file("rate_windows.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        import json as _json
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"saved_at": now, "windows": data}, f)
        tmp.replace(path)
        _rate_save_last = now
    except Exception as e:
        logger.debug("[rate_limit] 快照写入失败: %s", e)


def _load_rate_windows() -> None:
    """2026-05-13: 启动时恢复上次运行的满窗口时间戳序列。自动清除已过期条目。"""
    try:
        path = data_file("rate_windows.json")
        if not path.exists():
            return
        import json as _json
        with open(path, encoding="utf-8") as f:
            snap = _json.load(f)
        with _rate_cfg_lock:
            cfg = _load_rate_limits()
        now = time.time()
        recovered = 0
        with _rate_windows_lock:
            for key, timestamps in (snap.get("windows") or {}).items():
                task_type = key.split(":", 1)[-1]
                type_cfg = cfg.get(task_type) or {}
                window_sec = float(type_cfg.get("window_hours", 1.0)) * 3600
                valid = [ts for ts in timestamps if ts >= now - window_sec]
                if valid:
                    _rate_windows[key] = deque(valid)
                    recovered += len(valid)
        if recovered:
            logger.info("[rate_limit] 已恢复 %d 条跨重启限流记录", recovered)
    except Exception as e:
        logger.debug("[rate_limit] 快照加载跳过: %s", e)


def _load_rate_limits() -> Dict[str, dict]:
    """从 task_execution_policy.yaml 热加载速率限制配置（mtime 比对）。"""
    global _rate_cfg_mtime, _rate_cfg_cache
    try:
        path = config_file("task_execution_policy.yaml")
        mtime = path.stat().st_mtime
        if mtime == _rate_cfg_mtime:
            return _rate_cfg_cache
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        new_cfg = data.get("rate_limits") or {}
        if new_cfg != _rate_cfg_cache:  # 内容真正变化时才写入审计
            try:
                from src.host.config_audit import record as _audit
                _audit("task_execution_policy.yaml", "rate_limits", new_cfg)
            except Exception:
                pass
        _rate_cfg_cache = new_cfg
        _rate_cfg_mtime = mtime
    except Exception:
        pass
    return _rate_cfg_cache


# 模块加载时即恢复（_load_rate_limits 已在上方定义，顺序安全）
_load_rate_windows()
# 2026-05-13: 恢复持久化暂停（_on_suspension_expired / _suspension_timers 已在上方定义）
_load_suspensions_from_disk()


def set_rate_bypass(task_type: str, minutes: int = 60,
                    device_id: str = "") -> dict:
    """2026-05-13: 设置速率限制动态豁免。

    Args:
        task_type: 要豁免的任务类型
        minutes:   豁免时长（分钟），最大 1440（24h）
        device_id: 为空时豁免所有设备该类型，否则仅豁免指定设备

    Returns:
        {"key": str, "expires_in_sec": int, "expires_at": str}
    """
    minutes = max(1, min(minutes, 1440))
    key = f"{device_id or '*'}:{task_type}"
    expiry = time.time() + minutes * 60
    with _rate_bypasses_lock:
        _rate_bypasses[key] = expiry
    logger.info("[rate_limit] 豁免已设置: key=%s expires_in=%dm", key, minutes)
    return {
        "key": key,
        "expires_in_sec": minutes * 60,
        "expires_at": datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(),
    }


def get_rate_bypasses() -> list[dict]:
    """2026-05-13: 返回当前所有有效的豁免条目（已过期的自动排除）。"""
    now = time.time()
    result = []
    with _rate_bypasses_lock:
        expired = [k for k, v in _rate_bypasses.items() if v <= now]
        for k in expired:
            del _rate_bypasses[k]
        for key, expiry in _rate_bypasses.items():
            parts = key.split(":", 1)
            result.append({
                "key": key,
                "device_id": parts[0] if len(parts) == 2 else "",
                "task_type": parts[1] if len(parts) == 2 else key,
                "remaining_sec": max(0, int(expiry - now)),
            })
    return result


def _on_suspension_expired(key: str) -> None:
    """2026-05-13: Timer 回调 — 暂停到期时推送恢复通知并清理自身。"""
    with _rate_suspensions_lock:
        _rate_suspensions.pop(key, None)
    with _suspension_timers_lock:
        _suspension_timers.pop(key, None)
    _save_suspensions_to_disk()  # 到期后更新磁盘（删除该条目）
    logger.info("[auto_suspend] 暂停已自动到期恢复: key=%s", key)
    try:
        from src.host.event_stream import push_event
        push_event("scheduler.task_resumed", {"key": key, "reason": "expired"})
    except Exception:
        pass
    try:
        from src.host.alert_notifier import AlertNotifier
        parts = key.split(":", 1)
        AlertNotifier.get().notify(
            "info", "",
            f"[调度] 暂停已到期恢复: {parts[1] if len(parts)==2 else key} "
            f"(device={parts[0] if len(parts)==2 else '*'})"
        )
    except Exception:
        pass


def suspend_task_type(task_type: str, device_id: str = "",
                      minutes: int = 30, reason: str = "manual",
                      triggered_by: str = "api") -> dict:
    """2026-05-13: 自动/手动暂停某设备的某类型任务（连续失败后由调度器调用）。

    暂停优先级高于豁免 —— 暂停期间即使有 bypass 也无法执行该类型任务。
    调用方（如 SmartScheduler）通过该函数触发保护性熔断。

    Args:
        task_type:    要暂停的任务类型
        device_id:    空=全设备；否则仅暂停指定设备
        minutes:      暂停时长（分钟），最大 480（8h），0 = 立即恢复
        reason:       暂停原因（"auto"连续失败自动 / "manual"手动 / "restored"重启恢复）
        triggered_by: 触发来源（"scheduler" / "api" / "startup"）
    """
    minutes = max(0, min(minutes, 480))
    key = f"{device_id or '*'}:{task_type}"
    if minutes == 0:
        with _rate_suspensions_lock:
            _rate_suspensions.pop(key, None)
        # 取消已有 Timer（若运维提前手动解除）
        with _suspension_timers_lock:
            t = _suspension_timers.pop(key, None)
            if t:
                t.cancel()
        logger.info("[auto_suspend] 暂停已解除: key=%s", key)
        try:
            from src.host.event_stream import push_event
            push_event("scheduler.task_resumed", {"key": key, "reason": "manual"})
        except Exception:
            pass
        return {"key": key, "action": "cleared"}
    expiry = time.time() + minutes * 60
    with _rate_suspensions_lock:
        _rate_suspensions[key] = expiry
    # 2026-05-13: 记录来源元数据
    with _suspension_meta_lock:
        _suspension_meta[key] = {
            "reason": reason,
            "triggered_by": triggered_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    # 取消旧 Timer（重复设置同一 key 时），调度新 Timer
    with _suspension_timers_lock:
        old = _suspension_timers.pop(key, None)
        if old:
            old.cancel()
        t = threading.Timer(minutes * 60, _on_suspension_expired, args=(key,))
        t.daemon = True
        t.start()
        _suspension_timers[key] = t
    logger.warning("[auto_suspend] 任务已暂停: key=%s expires_in=%dm", key, minutes)
    _save_suspensions_to_disk()
    try:
        from src.host.event_stream import push_event
        push_event("scheduler.task_suspended", {
            "key": key, "device_id": device_id or "*",
            "task_type": task_type, "minutes": minutes,
        })
    except Exception:
        pass
    return {
        "key": key,
        "expires_in_sec": minutes * 60,
        "expires_at": datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat(),
    }


def suspend_device(device_id: str, minutes: int = 60) -> dict:
    """2026-05-13: 暂停某设备的所有任务派发（跨任务类型熔断）。

    通过设置 key="{device_id}:*" 实现；_check_and_record_rate 检查此 key。
    适用场景：设备跨多任务类型持续失败，说明设备整体异常而非单个任务问题。

    Args:
        device_id: 要暂停的设备 ID
        minutes:   暂停时长（分钟），最大 480（8h），0 = 立即恢复
    """
    return suspend_task_type("*", device_id=device_id, minutes=minutes,
                             reason="auto", triggered_by="scheduler")


def get_suspensions() -> list[dict]:
    """2026-05-13: 返回当前所有有效的自动暂停条目（已过期的自动清除）。"""
    now = time.time()
    result = []
    with _rate_suspensions_lock:
        expired = [k for k, v in _rate_suspensions.items() if v <= now]
        for k in expired:
            del _rate_suspensions[k]
        for key, expiry in _rate_suspensions.items():
            parts = key.split(":", 1)
            with _suspension_meta_lock:
                meta = _suspension_meta.get(key, {})
            result.append({
                "key": key,
                "device_id": parts[0] if len(parts) == 2 else "",
                "task_type": parts[1] if len(parts) == 2 else key,
                "remaining_sec": max(0, int(expiry - now)),
                "reason": meta.get("reason", "unknown"),
                "triggered_by": meta.get("triggered_by", "unknown"),
                "created_at": meta.get("created_at", ""),
            })
    return result


def _check_and_record_rate(device_id: str, task_type: str) -> tuple[bool, str]:
    """检查并（通过时）记录此次派发到滑动窗口。

    Returns:
        (allowed, reason) — allowed=True 表示通过，False 表示超限，reason 说明原因。
    """
    now = time.time()
    # 2026-05-13: 暂停检查（最高优先级，高于豁免）
    # 包括：任务类型级暂停 + 设备级全暂停（key="{device_id}:*"）
    with _rate_suspensions_lock:
        if (now < _rate_suspensions.get(f"*:{task_type}", 0)
                or now < _rate_suspensions.get(f"{device_id}:{task_type}", 0)
                or now < _rate_suspensions.get(f"{device_id}:*", 0)):
            return False, f"auto_suspended: {task_type}@{device_id[:8]}"

    # 2026-05-13: 豁免检查（优先于限流，允许运维临时绕过）
    with _rate_bypasses_lock:
        if (now < _rate_bypasses.get(f"*:{task_type}", 0)
                or now < _rate_bypasses.get(f"{device_id}:{task_type}", 0)):
            return True, ""  # 豁免有效，直接通过

    with _rate_cfg_lock:
        cfg = _load_rate_limits()

    type_cfg: dict = cfg.get(task_type) or {}
    limit = int(type_cfg.get("per_device_per_hour", 0))
    if limit <= 0:
        return True, ""  # 该类型不限流

    window_sec = float(type_cfg.get("window_hours", 1.0)) * 3600
    key = f"{device_id}:{task_type}"
    now = time.time()
    cutoff = now - window_sec

    with _rate_windows_lock:
        if key not in _rate_windows:
            _rate_windows[key] = deque()
        win = _rate_windows[key]
        # 清除窗口外的旧时间戳
        while win and win[0] < cutoff:
            win.popleft()
        count = len(win)
        if count >= limit:
            # 超限，节流日志（同一 key 每 120s 最多一条）
            last_warn = _rate_warn_last.get(key, 0.0)
            if now - last_warn >= 120:
                logger.warning("[rate_limit] 派发受限: device=%s type=%s 窗口内=%d/%d",
                               device_id[:12], task_type, count, limit)
                _rate_warn_last[key] = now
            return False, (f"rate_limited: {task_type} on {device_id[:12]} "
                           f"({count}/{limit} in last {window_sec/3600:.1f}h)")
        # 通过，记录本次派发时间戳
        win.append(now)
    # 节流写入磁盘（锁外执行，避免持锁太久）
    if now - _rate_save_last >= _RATE_SAVE_INTERVAL:
        _save_rate_windows()
    return True, ""


# 业务进展 SLA 阈值 (秒) — 按 task_type 区分.
# 触发条件: status=running + type LIKE 'facebook_%' + 在 pool 里 +
# elapsed >= 阈值 + 该 device 上从 task started 至今**0** 业务事件入库.
# 业务事件信号源: fb_contact_events.at / facebook_groups.last_visited_at
# (同一 device 同时只跑一个 task by device_section_lock, 关联可靠).
#
# 2026-04-27 加 (Phase 2 P0 #1): 兜底 R0/R3 漏掉的死循环场景:
# 即使 decorator 正确 + 入口硬编码, 仍可能在 FB 内 dump_hierarchy 卡顿
# / VPN 中途掉线断网无 UI / 群已被删 / 风控弹窗未识别等情况下死循环.
# 阈值参考 _TASK_TYPE_TIMEOUTS (executor.py:159) + 业务最低产出周期.
# 必然写业务事件的 task type — SLA 兜底.
# 必须满足: 正常完成会写至少 1 条 fb_contact_events / facebook_groups /
# fb_risk_events. 否则会被 SLA 误杀.
#
# 2026-05-13 优化: 从 config/task_execution_policy.yaml 读取 SLA 阈值，支持动态调整
_TASK_PROGRESS_TIMEOUT_SEC: dict[str, int] = {
    "facebook_extract_members":          1800,  # 30 min — writes mark_group_visit
    "facebook_group_member_greet":       3600,  # 60 min — writes group visit / friend request
    "facebook_add_friend":               1200,  # 20 min — writes add_friend_sent/risk
    "facebook_send_greeting":             900,  # 15 min — writes greeting_sent
    "facebook_send_message":              600,  # 10 min — writes contact_event
    "facebook_check_inbox":              1200,  # 20 min — writes message_received if any
    "facebook_check_message_requests":    600,  # 10 min — 同 check_inbox
    "facebook_join_group":                600,  # 10 min — writes mark_group_visit
    "facebook_profile_hunt":             1800,  # 30 min — writes contact_event on match
}
_DEFAULT_PROGRESS_TIMEOUT_SEC = 1800


def _load_sla_timeouts() -> dict[str, int]:
    """从 config/task_execution_policy.yaml 加载 SLA 超时阈值。

    优先使用配置文件中的值，未配置则使用默认值。
    支持运行时动态调整（修改配置文件后重启服务生效）。
    """
    try:
        path = config_file("task_execution_policy.yaml")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        sla_config = data.get("sla_progress_timeout_sec") or {}
        if sla_config:
            # 合并配置：配置文件优先
            merged = _TASK_PROGRESS_TIMEOUT_SEC.copy()
            merged.update(sla_config)
            # 设置默认值
            default = sla_config.get("default", _DEFAULT_PROGRESS_TIMEOUT_SEC)
            logger.info("[sla] 从配置文件加载 SLA 阈值: %d 个任务类型, 默认=%ds",
                       len(sla_config), default)
            return merged, default
    except Exception as e:
        logger.warning("[sla] 加载 SLA 配置失败，使用硬编码默认值: %s", e)
    return _TASK_PROGRESS_TIMEOUT_SEC, _DEFAULT_PROGRESS_TIMEOUT_SEC


# 初始化时加载配置
_TASK_PROGRESS_TIMEOUT_SEC, _DEFAULT_PROGRESS_TIMEOUT_SEC = _load_sla_timeouts()
# 记录配置文件修改时间，用于热重载检测
_sla_policy_mtime: float = 0.0


def _reload_sla_if_changed() -> bool:
    """热重载 SLA 阈值配置 (mtime 文件变更检测).

    在 _rescue_once() 每轮开始时调用，若 task_execution_policy.yaml
    内容有变化则原地更新 _TASK_PROGRESS_TIMEOUT_SEC，无需重启服务。
    线程安全：CPython GIL 保证 dict.update / int 赋值是原子操作。
    返回: True=已重载, False=无变化或失败。
    """
    global _DEFAULT_PROGRESS_TIMEOUT_SEC, _sla_policy_mtime
    try:
        path = config_file("task_execution_policy.yaml")
        mtime = path.stat().st_mtime
        if mtime <= _sla_policy_mtime:
            return False
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sla_config = data.get("sla_progress_timeout_sec") or {}
        # 用配置文件值原地更新（保留代码里有但配置文件未写的键）
        if sla_config:
            _TASK_PROGRESS_TIMEOUT_SEC.update(
                {k: int(v) for k, v in sla_config.items() if k != "default"}
            )
            new_default = int(sla_config.get("default", _DEFAULT_PROGRESS_TIMEOUT_SEC))
            if new_default != _DEFAULT_PROGRESS_TIMEOUT_SEC:
                _DEFAULT_PROGRESS_TIMEOUT_SEC = new_default
                logger.info("[sla] 热重载: 默认超时更新为 %ds", new_default)
        _sla_policy_mtime = mtime
        logger.info("[sla] 热重载 SLA 配置: %d 个任务类型阈值已更新 (mtime=%.0f)",
                    len(sla_config), mtime)
        try:
            from src.host.config_audit import record as _audit
            _audit("task_execution_policy.yaml", "sla_progress_timeout_sec",
                   {"default": _DEFAULT_PROGRESS_TIMEOUT_SEC, **sla_config})
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.debug("[sla] 热重载检测失败: %s", exc)
        return False

# SLA 跳过的 task type — 养号性任务, 正常跑也不产出 contact_event/group_visit/
# risk_event, 由内层 _TASK_TYPE_TIMEOUTS (executor.py:159) 兜底, 不做业务进展 SLA.
_SKIP_SLA_TASK_TYPES: frozenset = frozenset({
    "facebook_group_engage",            # 浏览 + 点赞为主, 仅评论才写 event
    "facebook_browse_feed",             # 纯浏览
    "facebook_browse_feed_by_interest",
    "facebook_browse_groups",           # 浏览 joined groups list
    "facebook_warmup",                  # 养号开屏
})

_rescue_thread: Optional[threading.Thread] = None
_rescue_stop = threading.Event()


def dispatch_after_create(
    task_id: str,
    device_id: Optional[str],
    task_type: str,
    params: dict | None = None,
    priority: int = 50,
) -> dict:
    """把刚 create_task 出来的任务真正送上线程池。

    流程和 POST /tasks 完全一致：
      1. 若设备在集群 Worker 上 → HTTP 转发到 Worker 的 /tasks
      2. 否则 → 本机 WorkerPool.submit(run_task, ...)

    返回 {"dispatched": bool, "mode": "worker"/"local"/"skip",
          "worker_ip": Optional[str], "reason": Optional[str]}，
    出错不抛异常，只记录日志，调用方可以据此决定是否重试。
    """
    result: dict[str, Any] = {
        "dispatched": False,
        "mode": "skip",
        "worker_ip": None,
        "reason": None,
    }

    if not task_id:
        result["reason"] = "empty_task_id"
        return result

    params = params or {}

    # 2026-05-13: 速率限制检查（仅限已知 device_id 的派发）
    if device_id:
        allowed, rate_reason = _check_and_record_rate(device_id, task_type)
        if not allowed:
            result["mode"] = "rate_limited"
            result["reason"] = rate_reason
            return result

    # 1) 集群路由
    if device_id:
        try:
            from .routers.cluster import _get_best_worker_url
            worker = _get_best_worker_url(device_id)
        except Exception as err:
            worker = None
            logger.debug("[dispatch] 查 worker 失败 task=%s: %s", task_id[:8], err)
        if worker:
            try:
                import urllib.request as _ur
                import json as _json
                url = f"http://{worker['ip']}:{worker['port']}/tasks"
                # 2026-05-10: 主控已做过 gate 校验（或 chain 入口免 gate），
                # Worker 端收到 _coordinator_forwarded 跳过重复 gate（含
                # GEO/preflight/add_friend quota），否则 Worker 本地
                # gate 校验失败 → HTTP 400 → 整条链 abort。
                _fwd_params = {**params, "_coordinator_forwarded": True}
                payload = _json.dumps({
                    "type": task_type,
                    "device_id": device_id,
                    "params": _fwd_params,
                    "created_via": params.get("_created_via"),
                }).encode()
                req = _ur.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = _ur.urlopen(req, timeout=10)
                remote = _json.loads(resp.read().decode())
                from . import task_store
                # Phase-13 fix: 用 set_task_running (task_store 没 update_task)
                task_store.set_task_running(task_id)
                # 真实运维 fix: 存 checkpoint 让 sync 线程能 poll worker 状态回报
                _remote_id = remote.get("task_id", "")
                if _remote_id:
                    try:
                        task_store.save_checkpoint(task_id, {
                            "_cluster_dispatch": {
                                "worker_ip": worker["ip"],
                                "worker_port": worker.get("port", 8000),
                                "remote_task_id": _remote_id,
                                "dispatched_at": datetime.now(
                                    timezone.utc).isoformat(),
                            },
                        })
                    except Exception as exc:
                        logger.debug(
                            "[dispatch] save_checkpoint 失败 task=%s: %s",
                            task_id[:8], exc)
                # 启 sync 线程 (一次性, 模块单例)
                _ensure_cluster_sync_thread()
                logger.info(
                    "[dispatch] task=%s → worker %s (remote_id=%s)",
                    task_id[:8], worker["ip"],
                    str(_remote_id)[:8],
                )
                result.update(dispatched=True, mode="worker",
                              worker_ip=worker["ip"],
                              remote_task_id=_remote_id)
                return result
            except Exception as err:
                logger.info("[dispatch] 集群派发失败 task=%s，回落本机: %s",
                            task_id[:8], err)

    # 2) 本机 WorkerPool.submit
    try:
        from src.device_control.device_manager import get_device_manager
        from .executor import _get_device_id, run_task
        from .worker_pool import get_worker_pool

        from .device_registry import DEFAULT_DEVICES_YAML

        config_path = DEFAULT_DEVICES_YAML
        manager = get_device_manager(config_path)
        try:
            manager.discover_devices()
        except Exception:
            pass
        resolved = _get_device_id(manager, device_id, config_path) if device_id else None
        device_for_lock = resolved or device_id or "default"
        pool = get_worker_pool()
        ok = pool.submit(task_id, device_for_lock, run_task, task_id, config_path,
                         priority=priority)
        if ok:
            result.update(dispatched=True, mode="local")
            logger.info("[dispatch] task=%s → local pool device=%s",
                        task_id[:8], device_for_lock[:12] if device_for_lock else "?")
        else:
            result["reason"] = "worker_pool_rejected"
            logger.warning("[dispatch] WorkerPool 拒收 task=%s（可能高风险或池已关闭）",
                           task_id[:8])
    except Exception as err:
        result["reason"] = f"local_submit_error: {err}"
        logger.exception("[dispatch] 本机派发异常 task=%s: %s", task_id[:8], err)
    return result


# ---------------------------------------------------------------------------
# pending 救援循环
# ---------------------------------------------------------------------------


def _iso_to_dt(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _load_orphan_pending(limit: int) -> list[dict]:
    """扫"pending 但既不是刚创建也不是线程池在跑"的任务。

    判定条件：
      - status='pending' 且未删除
      - updated_at 距今 >= _ORPHAN_AGE_SEC（避开刚创建未 submit 的窗口）
      - 不在 WorkerPool._futures 里
    """
    try:
        from .database import get_conn
        from .task_store import _alive_sql
        from .worker_pool import get_worker_pool
    except Exception as err:
        logger.debug("[rescue] 依赖加载失败: %s", err)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_ORPHAN_AGE_SEC)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT task_id, type, device_id, params, priority, retry_count, "
            f"next_retry_at, created_at, updated_at "
            f"FROM tasks WHERE status='pending' AND {_alive_sql()} "
            f"AND (updated_at IS NULL OR updated_at <= ?) "
            f"ORDER BY priority DESC, created_at ASC LIMIT ?",
            (cutoff_iso, limit),
        ).fetchall()

    pool = get_worker_pool()
    inflight = set(getattr(pool, "_futures", {}).keys()) | set(
        getattr(pool, "_cancel_flags", {}).keys()
    )
    now = datetime.now(timezone.utc)

    out: list[dict] = []
    for row in rows:
        task_id = row["task_id"]
        if task_id in inflight:
            continue
        next_retry = _iso_to_dt(row["next_retry_at"])
        if next_retry and next_retry > now:
            continue
        try:
            params = json.loads(row["params"] or "{}")
        except Exception:
            params = {}
        if params.get("run_on_host") is False:
            continue
        out.append({
            "task_id": task_id,
            "type": row["type"],
            "device_id": row["device_id"],
            "params": params,
            "priority": row["priority"] or 50,
        })
    return out


def _notify_orphan_recovery(*, reaped: int, requeued: int, source: str):
    """P4: 孤儿恢复/清理后推送 SSE 事件 + Telegram/Webhook 告警。"""
    total = reaped + requeued
    if total <= 0:
        return
    # SSE event → 前端仪表盘实时感知
    try:
        from .event_stream import push_event
        push_event("task.orphan_recovered", {
            "reaped": reaped,
            "requeued": requeued,
            "source": source,
        })
    except Exception:
        pass
    # AlertNotifier → Telegram / Webhook 外推
    try:
        from .alert_notifier import AlertNotifier
        parts = []
        if requeued:
            parts.append(f"{requeued} 个幂等任务已自动重排")
        if reaped:
            parts.append(f"{reaped} 个非幂等任务标记失败")
        msg = f"[{source}] 孤儿任务恢复: " + ", ".join(parts)
        AlertNotifier.get().notify("warning", "", msg)
    except Exception:
        pass


def _load_orphan_running(limit):
    """[#119] 扫 status=running 但 pool 里没在跑的孤儿 task."""
    try:
        from .database import get_conn
        from .task_store import _alive_sql
        from .worker_pool import get_worker_pool
    except Exception as err:
        logger.debug("[rescue] running orphan dep failed: %s", err)
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_ORPHAN_RUNNING_AGE_SEC)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT task_id, type, device_id, updated_at "
            f"FROM tasks WHERE status='running' AND {_alive_sql()} "
            f"AND (updated_at IS NULL OR updated_at <= ?) "
            f"ORDER BY updated_at ASC LIMIT ?",
            (cutoff_iso, limit),
        ).fetchall()
    pool = get_worker_pool()
    inflight = set(getattr(pool, "_futures", {}).keys()) | set(
        getattr(pool, "_cancel_flags", {}).keys()
    )
    out = []
    for row in rows:
        tid = row["task_id"]
        if tid in inflight:
            continue
        out.append({
            "task_id": tid,
            "type": row["type"],
            "device_id": row["device_id"],
            "updated_at": row["updated_at"],
        })
    return out


def _reap_orphan_running(orphans):
    """[#119] mark orphan running tasks as failed (no resubmit)."""
    if not orphans:
        return 0
    try:
        from .task_store import set_task_result
    except Exception as err:
        logger.debug("[rescue] set_task_result load failed: %s", err)
        return 0
    reaped = 0
    skipped_cluster = 0
    for t in orphans:
        tid = t["task_id"]
        if _is_dispatched_to_cluster_worker(tid):
            skipped_cluster += 1
            continue
        try:
            set_task_result(
                tid, False,
                error=("orphan task: server restart or thread died "
                       f"(last_update={t.get('updated_at')})"),
            )
            logger.warning("[rescue] reap orphan running task=%s type=%s device=%s",
                           tid, t.get("type"), t.get("device_id"))
            reaped += 1
        except Exception as err:
            logger.exception("[rescue] reap %s failed: %s", tid, err)
    if skipped_cluster:
        logger.info("[rescue] reap skipped %d cluster-dispatched tasks", skipped_cluster)
    # P4: 运行时孤儿清理告警
    if reaped:
        _notify_orphan_recovery(reaped=reaped, requeued=0, source="runtime")
    return reaped


def _load_no_progress_running(limit):
    """[#121] scan facebook_* tasks with 0 business events in SLA window."""
    try:
        from .database import get_conn
        from .task_store import _alive_sql
        from .worker_pool import get_worker_pool
    except Exception as err:
        logger.debug("[sla] dep load failed: %s", err)
        return []
    pool = get_worker_pool()
    inflight = set(getattr(pool, "_futures", {}).keys()) | set(
        getattr(pool, "_cancel_flags", {}).keys()
    )
    out = []
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT task_id, type, device_id, created_at "
            f"FROM tasks WHERE status='running' AND {_alive_sql()} "
            f"AND type LIKE 'facebook_%' "
            f"ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    for row in rows:
        tid = row["task_id"]
        if tid not in inflight:
            continue
        if _is_dispatched_to_cluster_worker(tid):
            continue
        ttype = row["type"]
        device = row["device_id"]
        if not device:
            continue
        if ttype in _SKIP_SLA_TASK_TYPES:
            continue
        timeout = _TASK_PROGRESS_TIMEOUT_SEC.get(ttype, _DEFAULT_PROGRESS_TIMEOUT_SEC)
        started_at = _iso_to_dt(row["created_at"])
        if not started_at:
            continue
        elapsed = (now - started_at).total_seconds()
        if elapsed < timeout:
            continue
        cutoff_sqlite = started_at.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with get_conn() as conn:
                ev = conn.execute(
                    "SELECT COUNT(*) AS n FROM fb_contact_events "
                    "WHERE device_id = ? AND at >= ?",
                    (device, cutoff_sqlite),
                ).fetchone()
                n_events = (ev["n"] if ev else 0) or 0
                gv = conn.execute(
                    "SELECT COUNT(*) AS n FROM facebook_groups "
                    "WHERE device_id = ? AND last_visited_at >= ?",
                    (device, cutoff_sqlite),
                ).fetchone()
                n_visits = (gv["n"] if gv else 0) or 0
                rk = conn.execute(
                    "SELECT COUNT(*) AS n FROM fb_risk_events "
                    "WHERE device_id = ? AND detected_at >= ?",
                    (device, cutoff_sqlite),
                ).fetchone()
                n_risks = (rk["n"] if rk else 0) or 0
        except Exception as err:
            logger.debug("[sla] query progress tables failed task=%s: %s", tid, err)
            continue
        if n_events == 0 and n_visits == 0 and n_risks == 0:
            out.append({
                "task_id": tid,
                "type": ttype,
                "device_id": device,
                "elapsed_sec": int(elapsed),
                "timeout_sec": timeout,
            })
    return out


def _abort_no_progress_tasks(stuck):
    """[#121] abort SLA-timed-out tasks via cooperative cancel + force fail."""
    if not stuck:
        return 0
    try:
        from .task_store import set_task_result
        from .worker_pool import get_worker_pool
    except Exception as err:
        logger.debug("[sla] dep load failed: %s", err)
        return 0
    aborted = 0
    pool = get_worker_pool()
    for t in stuck:
        tid = t["task_id"]
        try:
            pool.cancel_task(tid)
        except Exception:
            pass
        try:
            set_task_result(
                tid, False,
                error=(f"SLA timeout: {t['type']} on device "
                       f"{t['device_id'][:12]} {t['elapsed_sec']}s elapsed "
                       f"(SLA {t['timeout_sec']}s) with 0 events"),
            )
            logger.warning("[sla] abort no-progress task=%s type=%s device=%s",
                           tid, t["type"], t["device_id"][:12])
            # 2026-05-13: SLA abort 也计入 per-type 失败，影响成功率告警
            try:
                from .health_monitor import metrics as _hm
                _hm.record_task_type_result(t["type"], False)
            except Exception:
                pass
            aborted += 1
        except Exception as err:
            logger.exception("[sla] abort %s failed: %s", tid, err)
    return aborted


def _rescue_once() -> tuple[int, int]:
    """跑一轮救援。返回 (scanned, resubmit).

    职责 (按时间顺序):
      1. 热重载 SLA 配置（检测 task_execution_policy.yaml 是否变更）
      2. retry_ready: 历史失败的 task 到了重试时间 → 重派
      3. orphan pending: 写进 DB 但没 submit 的 → 重派
      4. (Phase 2 P0 #1) no-progress facebook task: SLA 超时无业务事件 → abort
    """
    scanned = 0
    resubmit = 0

    # 热重载：每轮检测一次配置文件变化（mtime 比对，开销极低）
    _reload_sla_if_changed()

    try:
        from . import task_store
        retry_ready = task_store.get_retry_ready_tasks(limit=_RESCUE_BATCH_LIMIT)
    except Exception as err:
        logger.debug("[rescue] get_retry_ready_tasks 失败: %s", err)
        retry_ready = []

    orphans = _load_orphan_pending(limit=_RESCUE_BATCH_LIMIT)

    # [#119] reap running orphans (status=running but thread died)
    running_orphans = _load_orphan_running(limit=_RESCUE_BATCH_LIMIT)
    reaped = _reap_orphan_running(running_orphans)
    if reaped:
        logger.info("[rescue] reaped %d orphan running tasks", reaped)

    # [#121] SLA check: facebook_* task no business event -> abort (no resubmit)
    stuck = _load_no_progress_running(limit=_RESCUE_BATCH_LIMIT)
    aborted = _abort_no_progress_tasks(stuck)
    # 2026-05-13: 每轮均扫描 task_type 成功率（函数内部节流，开销极低）
    _check_task_type_success_rates()
    if aborted:
        logger.info("[sla] aborted %d no-progress facebook tasks", aborted)
        # P4: SLA 超时告警推送
        try:
            from .event_stream import push_event
            push_event("task.sla_timeout", {"aborted": aborted})
        except Exception:
            pass
        try:
            from .alert_notifier import AlertNotifier
            AlertNotifier.get().notify(
                "warning", "",
                f"[runtime] SLA 超时: {aborted} 个任务因长时间无业务事件被终止")
        except Exception:
            pass

    all_candidates: dict[str, dict] = {}
    for t in retry_ready:
        all_candidates[t["task_id"]] = t
    for t in orphans:
        all_candidates.setdefault(t["task_id"], t)

    for task_id, t in all_candidates.items():
        scanned += 1
        r = dispatch_after_create(
            task_id=task_id,
            device_id=t.get("device_id"),
            task_type=t.get("type") or "",
            params=t.get("params") or {},
            priority=int(t.get("priority") or 50),
        )
        if r.get("dispatched"):
            resubmit += 1

    return scanned, resubmit


def _rescue_loop():
    logger.info("[rescue] pending_rescue_loop 启动，间隔 %ds，批量 %d，孤儿阈值 %ds",
                _RESCUE_INTERVAL_SEC, _RESCUE_BATCH_LIMIT, _ORPHAN_AGE_SEC)
    while not _rescue_stop.is_set():
        start_ts = time.time()
        try:
            scanned, resubmit = _rescue_once()
            if scanned or resubmit:
                logger.info("[rescue] scanned=%d resubmit=%d cost=%.2fs",
                            scanned, resubmit, time.time() - start_ts)
            else:
                logger.debug("[rescue] idle (scanned=0)")
        except Exception as err:
            logger.exception("[rescue] 轮次异常: %s", err)
        if _rescue_stop.wait(_RESCUE_INTERVAL_SEC):
            break
    logger.info("[rescue] pending_rescue_loop 已退出")


def _is_dispatched_to_cluster_worker(task_id: str) -> bool:
    """task 是否派发到 cluster worker 上跑 (而非本机 pool).

    避免 reaper 误杀派发到 worker 还在跑的 task — 那种 task 主控本地
    pool._futures 必然没有它, 但 worker 上仍在跑, 由 cluster_sync_thread
    定期 poll worker 状态回写主控. reaper 应跳过, 让 cluster_sync 处理.
    """
    try:
        from .task_store import get_checkpoint
        cp = get_checkpoint(task_id) or {}
        return bool(cp.get("_cluster_dispatch"))
    except Exception:
        return False


# 2026-05-08: 幂等任务类型 — 孤儿时可安全 auto-requeue 而非直接标 failed
# 2026-05-10: 扩展 — group_member_greet/group_engage/search_leads 每次从头开始,
#   不会产生重复好友请求 (quota gate 独立计算), 安全重试
_IDEMPOTENT_TASK_TYPES = frozenset({
    "facebook_browse_feed", "facebook_browse_feed_by_interest",
    "facebook_check_inbox", "facebook_check_friend_requests",
    "facebook_check_message_requests", "facebook_browse_groups",
    "facebook_group_member_greet", "facebook_group_engage",
    "facebook_search_leads", "facebook_extract_members",
    "tiktok_browse_feed", "tiktok_warmup", "tiktok_check_inbox",
    "tiktok_status", "tiktok_scan_username",
})
# 孤儿自动重试上限（防无限循环）— 提高到 2 应对多次重启
_ORPHAN_REQUEUE_MAX = 2


def _startup_reap_running_orphans() -> int:
    """server 启动时立即回收一次 running 孤儿.

    新进程的 pool._futures 必然为空, 所以**所有本机 status=running 的 task
    都是上一进程的孤儿** (无论 updated_at 多久前). 用一个临时小阈值
    (60s, 避开新进程刚启动后 set_task_running 写入的微小窗口) 兜底回收,
    不必等 15s 轮询周期或 _ORPHAN_RUNNING_AGE_SEC=7800s 长阈值.

    2026-05-08 优化: 幂等任务（browse_feed/check_inbox 等）自动 requeue 为
    pending 而非标记 failed，减少不必要的失败。非幂等任务保持原有行为。

    跳过派发到 cluster worker 的 task (走 cluster_sync_thread 路径).
    """
    try:
        from .database import get_conn
        from .task_store import _alive_sql, set_task_result
        from .worker_pool import get_worker_pool
    except Exception as err:
        logger.debug("[rescue] startup reap 依赖加载失败: %s", err)
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    pool = get_worker_pool()
    inflight = set(getattr(pool, "_futures", {}).keys()) | set(
        getattr(pool, "_cancel_flags", {}).keys()
    )
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT task_id, type, device_id, updated_at, params "
            f"FROM tasks WHERE status='running' AND {_alive_sql()} "
            f"AND (updated_at IS NULL OR updated_at <= ?)",
            (cutoff_iso,),
        ).fetchall()

    reaped = 0
    requeued = 0
    skipped_cluster = 0
    for row in rows:
        tid = row["task_id"]
        if tid in inflight:
            continue
        if _is_dispatched_to_cluster_worker(tid):
            skipped_cluster += 1
            continue

        task_type = row["type"] or ""
        # 幂等任务: auto-requeue（重置为 pending 让 rescue loop 重新派发）
        if task_type in _IDEMPOTENT_TASK_TYPES:
            # 检查是否已经被 requeue 过（防循环）
            try:
                import json as _json
                _params = _json.loads(row["params"]) if row["params"] else {}
            except Exception:
                _params = {}
            _requeue_count = int(_params.get("_orphan_requeue", 0))
            if _requeue_count < _ORPHAN_REQUEUE_MAX:
                try:
                    _params["_orphan_requeue"] = _requeue_count + 1
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE tasks SET status='pending', "
                            "updated_at=datetime('now'), "
                            "params=? "
                            "WHERE task_id=?",
                            (_json.dumps(_params, ensure_ascii=False), tid),
                        )
                        conn.commit()
                    logger.info("[rescue] startup-requeue idempotent task=%s "
                                "type=%s device=%s (requeue #%d)",
                                tid, task_type, row["device_id"],
                                _requeue_count + 1)
                    requeued += 1
                    continue
                except Exception as err:
                    logger.warning("[rescue] requeue %s 失败, fallback to reap: %s",
                                   tid, err)

        try:
            set_task_result(
                tid, False,
                error=("任务孤儿: server 启动时检测到上一进程未完成的 "
                       f"running 任务 (last_update={row['updated_at']})"),
            )
            logger.warning("[rescue] startup-reap task=%s type=%s device=%s",
                           tid, row["type"], row["device_id"])
            reaped += 1
        except Exception as err:
            logger.exception("[rescue] startup-reap %s 失败: %s", tid, err)
    if skipped_cluster:
        logger.info("[rescue] startup-reap skipped %d cluster-dispatched tasks "
                    "(let cluster_sync_thread handle)", skipped_cluster)
    if requeued:
        logger.info("[rescue] startup auto-requeued %d idempotent orphan tasks", requeued)
    # P4: 孤儿恢复告警推送 — 让运营第一时间知道有任务被自动恢复/清理
    _notify_orphan_recovery(reaped=reaped, requeued=requeued, source="startup")
    return reaped + requeued


def start_pending_rescue_loop() -> bool:
    """幂等启动后台救援线程。已启动返回 False。"""
    global _rescue_thread
    if _rescue_thread and _rescue_thread.is_alive():
        return False

    # 2026-04-27 加: server 启动时立即回收 running 孤儿, 不等 15s 轮询.
    # 新进程的 pool 必为空, 所有 status=running 都必然是上进程孤儿.
    try:
        n = _startup_reap_running_orphans()
        if n:
            logger.info("[rescue] startup reaped %d running orphans", n)
    except Exception as err:
        logger.exception("[rescue] startup reap 异常: %s", err)

    _rescue_stop.clear()
    _rescue_thread = threading.Thread(
        target=_rescue_loop, daemon=True, name="pending-rescue-loop"
    )
    _rescue_thread.start()
    return True


def stop_pending_rescue_loop() -> None:
    _rescue_stop.set()


# ---------------------------------------------------------------------------
# 真实运维 fix: cluster dispatch 状态同步线程
# ---------------------------------------------------------------------------
# 派发到 worker 的任务, worker 完成后状态不会自动回报主控.
# 主控的 task_store 永远停在 status=running. 此线程定期扫并 poll worker.

_CLUSTER_SYNC_INTERVAL_SEC = 5.0
_cluster_sync_thread: Optional[threading.Thread] = None
_cluster_sync_started = threading.Event()


def _ensure_cluster_sync_thread() -> None:
    """模块单例: 第一次有 cluster dispatch 时启."""
    global _cluster_sync_thread
    if _cluster_sync_started.is_set():
        return
    if _cluster_sync_started.is_set():  # double-check pattern
        return
    _cluster_sync_started.set()
    _cluster_sync_thread = threading.Thread(
        target=_cluster_sync_loop, daemon=True,
        name="cluster-dispatch-sync")
    _cluster_sync_thread.start()
    logger.info("[cluster_sync] thread started, interval=%.0fs",
                _CLUSTER_SYNC_INTERVAL_SEC)


def _cluster_sync_loop() -> None:
    while not _rescue_stop.is_set():
        try:
            _cluster_sync_tick()
        except Exception:
            logger.exception("[cluster_sync] tick failed")
        _rescue_stop.wait(_CLUSTER_SYNC_INTERVAL_SEC)


def _cluster_sync_tick() -> None:
    """扫主控 status=running 且有 cluster checkpoint 的任务,
    poll worker 状态, 完成的同步回主控 task_store."""
    from . import task_store
    from .database import get_conn
    from .task_store import _alive_sql

    # 拿所有 status=running 任务 (限 50)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT task_id, device_id, created_at FROM tasks "
            f"WHERE status='running' AND {_alive_sql()} "
            f"ORDER BY updated_at ASC LIMIT 50",
        ).fetchall()

    if not rows:
        return

    import urllib.request as _ur
    import json as _json
    synced = 0
    for row in rows:
        task_id = row["task_id"]
        # 看 checkpoint 找 cluster 派发信息
        try:
            cp = task_store.get_checkpoint(task_id) or {}
        except Exception:
            cp = {}
        cluster_info = cp.get("_cluster_dispatch")
        if not cluster_info:
            continue
        worker_ip = cluster_info.get("worker_ip")
        worker_port = cluster_info.get("worker_port", 8000)
        remote_id = cluster_info.get("remote_task_id")
        if not (worker_ip and remote_id):
            continue
        # 拉 worker 那边状态
        url = f"http://{worker_ip}:{worker_port}/tasks/{remote_id}"
        try:
            req = _ur.Request(url, method="GET")
            with _ur.urlopen(req, timeout=5) as resp:
                remote = _json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.debug("[cluster_sync] poll %s 失败: %s", remote_id[:8], exc)
            continue
        rstatus = remote.get("status")
        if rstatus not in ("completed", "failed", "cancelled", "timeout"):
            continue
        # remote 完成 → 同步到 coord task_store
        rresult = remote.get("result") or {}
        success = bool(rresult.get("success"))
        error = str(rresult.get("error") or "")
        screenshot = str(rresult.get("screenshot_path") or "")
        try:
            task_store.set_task_result(
                task_id, success=success, error=error,
                screenshot_path=screenshot,
                extra={
                    "device_id": row["device_id"] or "",
                    "_dispatched_to": worker_ip,
                    "_remote_task_id": remote_id,
                    **{k: v for k, v in rresult.items()
                        if k not in ("success", "error", "screenshot_path")},
                },
            )
            synced += 1
        except Exception as exc:
            logger.warning("[cluster_sync] set_task_result 失败 task=%s: %s",
                           task_id[:8], exc)
    if synced > 0:
        logger.info("[cluster_sync] synced %d remote completions", synced)
