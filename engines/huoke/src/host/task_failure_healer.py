# -*- coding: utf-8 -*-
"""任务失败自动自愈 — 2026-05-08 P2-3。

设计思路:
  error_classifier.py 已将错误归类到 (layer, code, fix_action)，
  前端展示一键修复按钮让人工点击。本模块把"点按钮"的动作自动化：

  task.failed → error_classifier → fix_action → 自动执行 → 重新派单

  自动自愈仅限以下 **可恢复** 错误类型:
    1. rotate_ip   — 代理/VPN 故障，自动轮换节点后 requeue
    2. reconnect_usb — ADB 离线，自动 reconnect 后 requeue
    3. smart_retry   — 瞬时错误，直接复制 task 重派

  安全约束:
    - 每台设备每小时最多 3 次自动自愈（防风暴）
    - 同一 task_id 不会被自愈两次
    - 非幂等任务（add_friend/send_message）不自动重试
    - 整体可通过 task_execution_policy.yaml 关闭
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# 每设备每小时最大自愈次数 (默认值; 可通过 P6-2 热配置覆盖)
_MAX_HEALS_PER_HOUR = 3

# P6-2: 运行时可修改的配置
_RUNTIME_CONFIG: Dict[str, Any] = {
    "max_heals_per_hour": _MAX_HEALS_PER_HOUR,
    "min_recovery_rate": 30,
    "enabled": True,
}

# 幂等任务集（与 task_dispatcher._IDEMPOTENT_TASK_TYPES 对齐）
_IDEMPOTENT_TASK_TYPES = frozenset({
    "facebook_browse_feed", "facebook_browse_feed_by_interest",
    "facebook_check_inbox", "facebook_check_friend_requests",
    "facebook_check_message_requests", "facebook_browse_groups",
    "facebook_search_leads", "facebook_join_group",
    "tiktok_browse_feed", "tiktok_warmup", "tiktok_check_inbox",
    "tiktok_status", "tiktok_scan_username",
    "instagram_browse_feed", "instagram_search_leads",
})

# 允许自动自愈的 fix_action → 对应的自动修复函数名
_AUTO_HEALABLE_ACTIONS = frozenset({"rotate_ip", "reconnect_usb", "smart_retry"})


class TaskFailureHealer:
    """监听 task.failed 事件，自动执行修复动作并 requeue。"""

    def __init__(self):
        self._lock = threading.Lock()
        # device_id → [(timestamp, task_id)]
        self._heal_history: Dict[str, list] = defaultdict(list)
        self._healed_task_ids: Set[str] = set()
        self._enabled = True
        self._stats = {"total": 0, "healed": 0, "skipped": 0, "escalated": 0,
                       "rotate_ip": 0, "reconnect_usb": 0, "smart_retry": 0,
                       "adaptive_switch": 0}
        self._escalated_devices: Dict[str, float] = {}
        self._effectiveness_cache: Optional[Dict[str, Any]] = None
        self._effectiveness_cache_ts: float = 0

    def on_task_failed(self, payload: dict):
        """EventBus task.failed 回调。"""
        if not self._enabled or not _RUNTIME_CONFIG.get('enabled', True):
            return

        data = payload.get("data") or {}
        task_id = data.get("task_id", "")
        device_id = data.get("device_id", "")
        error = data.get("error", "")

        if not task_id or not device_id or not error:
            return

        self._stats["total"] += 1

        # 读取完整 task 信息
        try:
            from .task_store import get_task
            task = get_task(task_id)
            if not task:
                return
        except Exception:
            return

        task_type = task.get("type", "")

        # 非幂等任务不自动重试（防止重复 add_friend 等副作用操作）
        if task_type not in _IDEMPOTENT_TASK_TYPES:
            self._stats["skipped"] += 1
            return

        # 已经自愈过的 task 不重复处理
        with self._lock:
            if task_id in self._healed_task_ids:
                return

        # 分类错误
        try:
            from .error_classifier import classify_task_error
            cls = classify_task_error(error)
        except Exception:
            return

        if not cls:
            return

        fix_action = cls.get("fix_action", "")
        if fix_action not in _AUTO_HEALABLE_ACTIONS:
            self._stats["skipped"] += 1
            return

        # P5-1: 自适应策略 — 恢复率低的 action 降权，尝试替代
        fix_action = self._adaptive_select_action(fix_action)
        if not fix_action:
            self._stats["skipped"] += 1
            return

        # 频率限制 — 超限后升级为隔离+通知（P3-4 多级降级）
        if not self._check_rate_limit(device_id, task_id):
            self._stats["skipped"] += 1
            self._escalate_to_isolation(device_id, cls.get("code", ""), fix_action)
            return

        # 执行修复
        logger.info("[auto_heal] task=%s type=%s error_code=%s → action=%s",
                    task_id[:8], task_type, cls.get("code"), fix_action)

        healed = False
        if fix_action == "rotate_ip":
            healed = self._do_rotate_ip(device_id)
        elif fix_action == "reconnect_usb":
            healed = self._do_reconnect_usb(device_id)
        elif fix_action == "smart_retry":
            healed = True  # 直接重派，不需前置修复

        if healed:
            self._do_requeue(task, task_id, fix_action)
            self._stats["healed"] += 1
            self._stats[fix_action] = self._stats.get(fix_action, 0) + 1
            with self._lock:
                self._healed_task_ids.add(task_id)
                # 保持 set 不无限膨胀
                if len(self._healed_task_ids) > 5000:
                    self._healed_task_ids = set(list(self._healed_task_ids)[-2000:])
        else:
            self._stats["skipped"] += 1

    # ── P5-1: 自适应策略选择 ─────────────────────────────────────────
    _EFFECTIVENESS_TTL = 300  # 5分钟缓存
    @property
    def _MIN_RECOVERY_RATE(self):
        return _RUNTIME_CONFIG.get('min_recovery_rate', 30)
    _FALLBACK_ORDER = ["smart_retry", "rotate_ip", "reconnect_usb"]

    def _adaptive_select_action(self, suggested: str) -> str:
        """P5-1: 根据历史恢复率动态选择最优 action。

        如果建议的 action 恢复率 < 30%，选择恢复率最高的替代。
        如果所有 action 恢复率都 < 30%，返回空字符串表示放弃。
        """
        eff = self._get_effectiveness_cached()
        if not eff:
            return suggested  # 无数据时使用默认

        suggested_rate = eff.get(suggested, {}).get("recovery_rate", 50)
        if suggested_rate >= self._MIN_RECOVERY_RATE:
            return suggested

        # 降权：选替代 action
        best_action = ""
        best_rate = 0
        for action in self._FALLBACK_ORDER:
            if action == suggested:
                continue
            if action not in _AUTO_HEALABLE_ACTIONS:
                continue
            rate = eff.get(action, {}).get("recovery_rate", 50)
            if rate > best_rate:
                best_rate = rate
                best_action = action

        if best_rate >= self._MIN_RECOVERY_RATE:
            logger.info("[auto_heal] 自适应: %s(%.0f%%) → %s(%.0f%%)",
                        suggested, suggested_rate, best_action, best_rate)
            self._stats["adaptive_switch"] = self._stats.get("adaptive_switch", 0) + 1
            return best_action

        # 所有 action 恢复率都低 → 放弃自愈
        logger.debug("[auto_heal] 所有 action 恢复率低，跳过自愈")
        return ""

    def _get_effectiveness_cached(self) -> Dict[str, Any]:
        """带缓存获取自愈效果数据。"""
        now = time.time()
        if (self._effectiveness_cache is not None
                and now - self._effectiveness_cache_ts < self._EFFECTIVENESS_TTL):
            return self._effectiveness_cache
        try:
            from .chain_advisor import get_heal_effectiveness
            data = get_heal_effectiveness()
            self._effectiveness_cache = data.get("actions", {})
            self._effectiveness_cache_ts = now
        except Exception:
            self._effectiveness_cache = {}
            self._effectiveness_cache_ts = now
        return self._effectiveness_cache

    def _check_rate_limit(self, device_id: str, task_id: str) -> bool:
        """检查设备级自愈频率。"""
        now = time.time()
        cutoff = now - 3600
        with self._lock:
            hist = self._heal_history[device_id]
            # 清除过期记录
            hist[:] = [(ts, tid) for ts, tid in hist if ts > cutoff]
            if len(hist) >= _RUNTIME_CONFIG.get('max_heals_per_hour', _MAX_HEALS_PER_HOUR):
                return False
            hist.append((now, task_id))
        return True

    def _do_rotate_ip(self, device_id: str) -> bool:
        """为设备所在路由器轮换代理。"""
        try:
            from src.device_control.router_manager import get_router_manager
            mgr = get_router_manager()
            # 找到该设备所在的路由器
            for router in mgr.list_routers():
                if device_id in (router.device_ids or []):
                    from src.device_control.proxy_rotator import rotate_proxy
                    result = rotate_proxy(router.router_id,
                                          reason=f"auto_heal:device={device_id[:8]}")
                    if result.get("ok"):
                        logger.info("[auto_heal] rotate_ip 成功: router=%s new=%s",
                                    router.router_id, result.get("new_proxy_ids"))
                        # 等代理生效
                        time.sleep(5)
                        return True
                    elif result.get("skipped"):
                        logger.debug("[auto_heal] rotate_ip 跳过: %s",
                                     result.get("skipped"))
                        return False
                    else:
                        logger.warning("[auto_heal] rotate_ip 失败: %s",
                                       result.get("error"))
                        return False
            logger.debug("[auto_heal] device=%s 未关联路由器，无法 rotate", device_id[:8])
            return False
        except Exception as e:
            logger.debug("[auto_heal] rotate_ip 异常: %s", e)
            return False

    def _do_reconnect_usb(self, device_id: str) -> bool:
        """ADB reconnect 设备。"""
        try:
            import subprocess
            r = subprocess.run(
                ["adb", "-s", device_id, "reconnect"],
                capture_output=True, timeout=15,
            )
            if r.returncode == 0:
                logger.info("[auto_heal] reconnect_usb 成功: %s", device_id[:8])
                time.sleep(3)
                return True
            logger.warning("[auto_heal] reconnect_usb 失败: %s", r.stderr[:100])
            return False
        except Exception as e:
            logger.debug("[auto_heal] reconnect_usb 异常: %s", e)
            return False

    def _do_requeue(self, task: dict, original_task_id: str, fix_action: str):
        """将失败任务重新入队。"""
        try:
            from .task_store import create_task

            params = task.get("params") or {}
            if isinstance(params, str):
                params = json.loads(params)
            # 标记自愈来源
            params["_auto_healed_from"] = original_task_id
            params["_auto_heal_action"] = fix_action

            new_id = create_task(
                task_type=task.get("type", ""),
                device_id=task.get("device_id"),
                params=params,
                priority=45,  # 略低于正常任务，避免抢占
            )
            logger.info("[auto_heal] requeue 成功: %s → %s (action=%s)",
                        original_task_id[:8], new_id[:8], fix_action)
        except Exception as e:
            logger.warning("[auto_heal] requeue 失败: %s", e)

    # ── P3-4: 多级降级 ─────────────────────────────────────────────

    def _escalate_to_isolation(self, device_id: str, error_code: str,
                               fix_action: str):
        """自愈频率超限 → 自动隔离设备 + 发送运维通知。"""
        now = time.time()
        # 同一设备 30 分钟内不重复升级
        last = self._escalated_devices.get(device_id, 0)
        if now - last < 1800:
            return
        self._escalated_devices[device_id] = now
        self._stats["escalated"] = self._stats.get("escalated", 0) + 1
        logger.warning(
            "[auto_heal] 设备 %s 自愈超限 → 自动隔离 (error=%s action=%s)",
            device_id[:8], error_code, fix_action)

        # 1) 隔离设备（不再分配新任务）
        try:
            from .health_monitor import DeviceHealthMetrics
            metrics = DeviceHealthMetrics.get()
            if not metrics.is_isolated(device_id):
                metrics.isolate_device(device_id)
        except Exception as e:
            logger.debug("[auto_heal] 隔离设备失败: %s", e)

        # 2) 推送事件到 EventBus（前端 WS 能捕获）
        try:
            from .event_stream import EventStreamHub
            EventStreamHub.get().push_event("auto_heal.escalated", {
                "device_id": device_id,
                "error_code": error_code,
                "fix_action": fix_action,
                "reason": "heal_rate_exceeded",
            }, device_id=device_id)
        except Exception:
            pass

        # 3) 外部通知（Telegram / webhook）
        try:
            from .alert_notifier import AlertNotifier
            notifier = AlertNotifier.get()
            if notifier:
                notifier.notify(
                    level="warning",
                    device_id=device_id,
                    alert_code="AUTO_HEAL_ESCALATED",
                    message=(f"设备 {device_id[:8]} 自愈频率超限，已自动隔离。"
                             f" 最近错误: {error_code}, 动作: {fix_action}"),
                    params={},
                )
        except Exception:
            pass

    def get_stats(self) -> dict:
        now = time.time()
        cutoff_1h = now - 3600
        with self._lock:
            hist_count = sum(len(h) for h in self._heal_history.values())
            recent_1h = sum(
                1 for h in self._heal_history.values()
                for ts, _ in h if ts > cutoff_1h
            )
            per_device = {}
            for did, h in self._heal_history.items():
                active = [(ts, tid) for ts, tid in h if ts > cutoff_1h]
                if active:
                    per_device[did[:12]] = len(active)
        heal_rate = (
            round(self._stats["healed"] / self._stats["total"] * 100, 1)
            if self._stats["total"] > 0 else 0
        )
        return {
            **self._stats,
            "active_trackers": hist_count,
            "heals_last_hour": recent_1h,
            "heal_rate_pct": heal_rate,
            "per_device_1h": per_device,
            "enabled": self._enabled,
        }

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        logger.info("[auto_heal] enabled=%s", enabled)


# ── 单例 + 启动 ───────────────────────────────────────────────────────

_HEALER: Optional[TaskFailureHealer] = None


def get_task_failure_healer() -> TaskFailureHealer:
    global _HEALER
    if _HEALER is None:
        _HEALER = TaskFailureHealer()
    return _HEALER


def setup_task_failure_healer():
    """注册 EventBus 监听。server 启动时调用。"""
    try:
        # 检查 policy 开关
        try:
            from src.host.task_policy import load_task_execution_policy
            policy = load_task_execution_policy()
            if not policy.get("auto_heal", {}).get("enabled", True):
                logger.info("[auto_heal] 已按 task_execution_policy 关闭")
                return
        except Exception:
            pass

        from src.host.risk_auto_heal import register_inproc_listener, patch_event_stream
        patch_event_stream()

        healer = get_task_failure_healer()
        register_inproc_listener("task.failed", healer.on_task_failed)
        logger.info("[auto_heal] 任务失败自愈监听已注册")
    except Exception as e:
        logger.warning("[auto_heal] 启动失败: %s", e)
