# -*- coding: utf-8 -*-
"""跨平台风控自愈核心 — Sprint 3 P0。

设计:
  - 一个 EventStreamHub monkey-patch + N 个平台 listener 共用
  - 任何平台触发风控 → 取消该设备**所有平台**的 pending 任务(株连保护)
  - 然后按 platform 配置降级到对应 platform 的 warmup
  - 全局配置 + per-platform 覆盖

兼容:fb_risk_listener.py 退化为薄壳调用本核心,API 保持向后兼容。

运行示例:
  push_event("facebook.risk_detected", {"message": "blocked"}, device_id="X")
  → 触发: 取消 X 上所有 pending 任务(任何平台)
  → 降级: 用 facebook warmup 排入 X
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

import yaml

from src.host.device_registry import config_dir

logger = logging.getLogger(__name__)

_CFG_DIR = config_dir()


# ─────────────────────────────────────────────────────────────────────
# in-process listener registry (复用 fb_risk_listener 已打的补丁,避免重复 patch)
# ─────────────────────────────────────────────────────────────────────

_inproc_listeners: Dict[str, list] = {}
_listeners_lock = threading.Lock()


def register_inproc_listener(event_type: str, fn: Callable[[Dict], None]):
    with _listeners_lock:
        _inproc_listeners.setdefault(event_type, []).append(fn)
    logger.info("[risk_core] inproc listener: %s -> %s",
                event_type, getattr(fn, "__name__", str(fn)))


def _dispatch_inproc(event_type: str, payload: Dict):
    with _listeners_lock:
        listeners = list(_inproc_listeners.get(event_type, []))
    for fn in listeners:
        try:
            fn(payload)
        except Exception:
            logger.exception("[risk_core] listener 异常: %s",
                             getattr(fn, "__name__", "unknown"))


def patch_event_stream():
    """复用 fb_risk_listener 的同款 patch(幂等)。"""
    from . import event_stream as es
    if getattr(es.EventStreamHub, "_risk_patched", False):
        return
    _orig = es.EventStreamHub.push_event

    def _wrapped(self, event_type: str, data: Dict = None,
                 device_id: str = ""):
        _orig(self, event_type, data, device_id)
        if event_type in _inproc_listeners:
            _dispatch_inproc(event_type, {
                "event_type": event_type,
                "data": data or {},
                "device_id": device_id,
            })

    es.EventStreamHub.push_event = _wrapped
    es.EventStreamHub._risk_patched = True
    logger.info("[risk_core] EventStreamHub.push_event 已打补丁(跨平台)")


# ─────────────────────────────────────────────────────────────────────
# 平台 adapter 抽象
# ─────────────────────────────────────────────────────────────────────

class PlatformRiskConfig:
    """单平台的风控自愈配置。"""

    def __init__(self, platform: str, *,
                 enabled: bool = True,
                 strategy: str = "B",
                 cooldown_seconds: int = 600,
                 downgrade_preset: str = "warmup",
                 cancel_other_platforms: bool = True,
                 max_history_per_device: int = 50):
        self.platform = platform
        self.enabled = enabled
        self.strategy = strategy
        self.cooldown_seconds = cooldown_seconds
        self.downgrade_preset = downgrade_preset
        # P0 升级: 任何平台风控会株连其它平台 pending 任务(防止 tt 把 fb 推到风控)
        self.cancel_other_platforms = cancel_other_platforms
        self.max_history_per_device = max_history_per_device


def _auto_scrcpy_on_risk_enabled() -> bool:
    """读取 `config/risk_scrcpy.yaml` 里 `auto_start: true` 决定是否在风控
    触发降级时自动拉起投屏。默认 false。"""
    try:
        yaml_path = _CFG_DIR / "risk_scrcpy.yaml"
        if not yaml_path.exists():
            return False
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return bool(data.get("auto_start", False))
    except Exception:
        return False


def _try_start_scrcpy_for_risk(device_id: str) -> bool:
    """异步启动 scrcpy 投屏,不阻塞 risk 主流程。成功提交返回 True。"""
    try:
        def _do():
            try:
                from .scrcpy_manager import get_scrcpy_manager
                mgr = get_scrcpy_manager()
                existing = mgr._sessions.get(device_id) if hasattr(mgr, "_sessions") else None
                if existing and getattr(existing, "is_running", False):
                    logger.info("[risk_core/scrcpy] 设备 %s 已有活跃投屏,跳过",
                                device_id[:12])
                    return
                session = mgr.start_session(device_id)
                if session:
                    logger.info("[risk_core/scrcpy] 已为风控设备 %s 启动投屏",
                                device_id[:12])
                else:
                    logger.warning("[risk_core/scrcpy] 设备 %s 投屏启动失败",
                                   device_id[:12])
            except Exception as e:
                logger.warning("[risk_core/scrcpy] 启动异常: %s", e)
        t = threading.Thread(target=_do, daemon=True,
                             name=f"risk-scrcpy-{device_id[:8]}")
        t.start()
        return True
    except Exception as e:
        logger.warning("[risk_core/scrcpy] 派发线程失败: %s", e)
        return False


def _load_platform_config(platform: str) -> PlatformRiskConfig:
    """从 config/{platform}_risk.yaml 加载,缺失用默认。"""
    yaml_path = _CFG_DIR / f"{platform}_risk.yaml"
    data: Dict[str, Any] = {}
    if yaml_path.exists():
        try:
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("[risk_core] %s 配置读取失败: %s", platform, e)
    return PlatformRiskConfig(
        platform=platform,
        enabled=bool(data.get("enabled", True)),
        strategy=str(data.get("strategy", "B")),
        cooldown_seconds=int(data.get("cooldown_seconds", 600)),
        downgrade_preset=str(data.get("downgrade_to_preset", "warmup")),
        cancel_other_platforms=bool(data.get("cancel_other_platforms", True)),
        max_history_per_device=int(data.get("max_history_per_device", 50)),
    )


# ─────────────────────────────────────────────────────────────────────
# 通用核心
# ─────────────────────────────────────────────────────────────────────

class CrossPlatformRiskHealer:
    """一个实例,处理所有平台的风控事件。"""

    def __init__(self):
        self._configs: Dict[str, PlatformRiskConfig] = {}
        self._cooldown: Dict[str, float] = {}  # f"{platform}:{device_id}" -> ts
        self._history: Dict[str, deque] = {}   # device_id -> deque[record]
        self._lock = threading.Lock()
        self._preset_resolvers: Dict[str, Callable[[str], Optional[Dict]]] = {}

    def register_platform(self, platform: str,
                          preset_resolver: Callable[[str], Optional[Dict]]):
        """注册一个平台:
           - platform: 'facebook' / 'tiktok' / ...
           - preset_resolver(preset_key) → preset dict({steps: [...], ...}) 或 None
        """
        self._configs[platform] = _load_platform_config(platform)
        self._preset_resolvers[platform] = preset_resolver
        logger.info("[risk_core] 已注册平台: %s (enabled=%s strategy=%s)",
                    platform, self._configs[platform].enabled,
                    self._configs[platform].strategy)

    def reload_all_configs(self):
        with self._lock:
            for plat in list(self._configs.keys()):
                self._configs[plat] = _load_platform_config(plat)
        logger.info("[risk_core] 所有平台配置已重载")

    def handle(self, payload: Dict):
        """通用入口:由 in-process listener 触发。

        event_type 必须形如 '{platform}.risk_detected'。
        """
        ev_type = payload.get("event_type") or ""
        if not ev_type.endswith(".risk_detected"):
            return
        platform = ev_type.split(".", 1)[0]
        device_id = payload.get("device_id") or ""
        message = (payload.get("data") or {}).get("message", "")
        if not device_id:
            return

        cfg = self._configs.get(platform)
        if not cfg or not cfg.enabled:
            logger.debug("[risk_core] 平台 %s 未注册或已禁用,跳过", platform)
            return

        cd_key = f"{platform}:{device_id}"
        now = time.time()
        with self._lock:
            cd_until = self._cooldown.get(cd_key, 0)
            if now < cd_until:
                logger.debug("[risk_core] %s/%s 在 cooldown(%ds 剩余),跳过",
                             platform, device_id[:12], int(cd_until - now))
                return
            self._cooldown[cd_key] = now + cfg.cooldown_seconds

        logger.warning("[risk_core] %s/%s 触发风控自愈: %s",
                       platform, device_id[:12], message[:100])

        # 1. 取消该设备所有 pending 任务(默认株连其它平台)
        cancelled_per_platform = self._cancel_pending(
            device_id, only_platform=None if cfg.cancel_other_platforms else platform
        )

        # 1b. Sprint 5 P0: 对 running 任务发 cooperative cancel 信号。
        # 风控事件意味着"继续跑会加剧风险",但 runner 内部已有 checkpoint
        # 可在 ~10s 内响应取消。这里不等待,只发信号。
        running_cancelled = self._cancel_running_on_device(
            device_id, only_platform=None if cfg.cancel_other_platforms else platform
        )
        for plat, n in running_cancelled.items():
            cancelled_per_platform[plat] = cancelled_per_platform.get(plat, 0) + n

        # 2. 降级到当前平台的 warmup
        downgrade_task_id = ""
        if cfg.strategy == "B":
            downgrade_task_id = self._enqueue_warmup(platform, device_id, cfg)

        # 3. Sprint 5 P3-3: 风控降级任务入队时,自动拉起 scrcpy 投屏,
        # 让操作员能第一时间看到风控现场(验证码/拼图/账号异常页)。
        # gate:需 global config `auto_scrcpy_on_risk: true`(默认 false),
        # 且环境有显示(GUI 头);失败静默。
        scrcpy_launched = False
        if downgrade_task_id and _auto_scrcpy_on_risk_enabled():
            scrcpy_launched = _try_start_scrcpy_for_risk(device_id)

        # 4. 记录 + 通知
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "platform": platform,
            "message": message,
            "cancelled_per_platform": cancelled_per_platform,
            "cancelled_total": sum(cancelled_per_platform.values()),
            "downgraded": bool(downgrade_task_id),
            "downgrade_task_id": downgrade_task_id,
            "strategy": cfg.strategy,
            "cancel_other_platforms": cfg.cancel_other_platforms,
            "scrcpy_launched": scrcpy_launched,
        }
        self._append_history(device_id, record, cfg)
        self._mark_device_state(platform, device_id, message, record)
        self._push_event(platform, device_id, record)

    # ─────────────────────────────────────────────────────────────

    def _cancel_pending(self, device_id: str,
                        only_platform: Optional[str] = None) -> Dict[str, int]:
        """取消该设备所有 pending 任务,返回 {platform: count}。"""
        result: Dict[str, int] = {}
        try:
            from .task_store import list_tasks, set_task_cancelled
            tasks = list_tasks(device_id=device_id, status="pending", limit=1000)
            for t in tasks:
                ttype = t.get("type", "")
                # 推断 platform:facebook_xxx / tiktok_xxx / linkedin_xxx
                if "_" not in ttype:
                    continue
                plat = ttype.split("_", 1)[0]
                if only_platform and plat != only_platform:
                    continue
                try:
                    set_task_cancelled(t["task_id"])
                    result[plat] = result.get(plat, 0) + 1
                except Exception:
                    pass
        except Exception:
            logger.exception("[risk_core] 取消任务异常")
        if result:
            logger.info("[risk_core] 已取消 pending: %s (设备 %s)",
                        result, device_id[:12])
        return result

    def _cancel_running_on_device(self, device_id: str,
                                  only_platform: Optional[str] = None
                                  ) -> Dict[str, int]:
        """Sprint 5 P0: 风控触发时,对该设备上 running 的任务发送
        cooperative cancel 信号。runner 内部 is_cancelled() checkpoint
        会在约 10s 内响应。返回 {platform: count}。
        """
        result: Dict[str, int] = {}
        try:
            from .worker_pool import get_worker_pool
            from .task_store import list_tasks
            pool = get_worker_pool()
            active_tid = pool._active_tasks.get(device_id)
            if not active_tid:
                return result
            tasks = list_tasks(device_id=device_id, status="running", limit=20)
            for t in tasks:
                tid = t.get("task_id") or ""
                if tid != active_tid:
                    continue
                ttype = t.get("type", "")
                if "_" not in ttype:
                    continue
                plat = ttype.split("_", 1)[0]
                if only_platform and plat != only_platform:
                    continue
                ok = pool.cancel_task(tid)
                if ok:
                    result[plat] = result.get(plat, 0) + 1
                    logger.info("[risk_core] 已向 running task=%s "
                                "(%s, device=%s) 发送 cooperative cancel",
                                tid[:8], plat, device_id[:12])
        except Exception:
            logger.exception("[risk_core] cancel running 异常")
        return result

    def _enqueue_warmup(self, platform: str, device_id: str,
                        cfg: PlatformRiskConfig) -> str:
        try:
            resolver = self._preset_resolvers.get(platform)
            if not resolver:
                return ""
            preset = resolver(cfg.downgrade_preset)
            if not preset or not preset.get("steps"):
                logger.warning("[risk_core] 平台 %s 无 %s 预设",
                               platform, cfg.downgrade_preset)
                return ""
            from .task_store import create_task
            from .task_dispatcher import dispatch_after_create
            step = preset["steps"][0]
            _params = {
                **(step.get("params") or {}),
                "_origin": f"{platform}_risk_auto_downgrade",
                "_downgrade_reason": "risk_detected",
            }
            tid = create_task(
                task_type=step["type"],
                device_id=device_id,
                params=_params,
                priority=20,
            )
            dispatch_after_create(
                task_id=tid,
                device_id=device_id,
                task_type=step["type"],
                params=_params,
                priority=20,
            )
            logger.info("[risk_core] 已为 %s/%s 排入降级任务 %s",
                        platform, device_id[:12], tid[:8])
            return tid
        except Exception:
            logger.exception("[risk_core] 降级排队异常")
            return ""

    def _append_history(self, device_id: str, record: Dict,
                        cfg: PlatformRiskConfig):
        with self._lock:
            dq = self._history.setdefault(
                device_id, deque(maxlen=cfg.max_history_per_device)
            )
            dq.append(record)

    def _mark_device_state(self, platform: str, device_id: str,
                           message: str, record: Dict):
        try:
            from .device_state import DeviceStateStore
            ds = DeviceStateStore(platform=platform)
            ds.set(device_id, "risk_status", "red")
            ds.set(device_id, "last_risk_message", message[:200])
            ds.set(device_id, "last_risk_action_ts", record["ts"])
            ds.set(device_id, "last_risk_cancelled_total", record["cancelled_total"])
            ds.set(device_id, "last_risk_downgraded", record["downgraded"])
            # 跨平台标记:让其他平台也知道这设备风险升高
            for other in self._configs:
                if other == platform:
                    continue
                try:
                    other_ds = DeviceStateStore(platform=other)
                    other_ds.set(device_id, "cross_platform_risk_alert",
                                 f"{platform}@{record['ts']}")
                except Exception:
                    pass
        except Exception:
            logger.debug("[risk_core] DeviceStateStore 写入失败", exc_info=True)

    def _push_event(self, platform: str, device_id: str, record: Dict):
        try:
            from . import event_stream as es
            es.EventStreamHub.get().push_event(
                f"{platform}.risk_auto_heal", record, device_id=device_id
            )
            es.EventStreamHub.get().push_event(
                "risk.cross_platform_action", record, device_id=device_id
            )
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # 查询接口
    # ─────────────────────────────────────────────────────────────

    def get_history(self, device_id: str,
                    platform: Optional[str] = None) -> List[Dict]:
        with self._lock:
            full = list(self._history.get(device_id, deque()))
        if platform:
            full = [r for r in full if r.get("platform") == platform]
        return full

    def get_all_histories(self,
                          platform: Optional[str] = None) -> Dict[str, List[Dict]]:
        with self._lock:
            full = {k: list(v) for k, v in self._history.items()}
        if platform:
            return {k: [r for r in v if r.get("platform") == platform]
                    for k, v in full.items()}
        return full

    def get_cooldown_remaining(self, platform: str,
                               device_id: str) -> float:
        with self._lock:
            cd = self._cooldown.get(f"{platform}:{device_id}", 0)
        return max(cd - time.time(), 0.0)

    def clear_cooldown(self, platform: str, device_id: str):
        with self._lock:
            self._cooldown.pop(f"{platform}:{device_id}", None)

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            histories = {k: list(v) for k, v in self._history.items()}
            cooldowns = dict(self._cooldown)
        per_platform: Dict[str, Dict[str, int]] = {}
        for did, recs in histories.items():
            for r in recs:
                p = r.get("platform", "?")
                pp = per_platform.setdefault(p, {"events": 0, "downgrades": 0})
                pp["events"] += 1
                if r.get("downgraded"):
                    pp["downgrades"] += 1
        return {
            "platforms_registered": list(self._configs.keys()),
            "per_platform_stats": per_platform,
            "active_cooldowns": len([k for k, v in cooldowns.items()
                                     if v > time.time()]),
            "configs": {
                p: {"enabled": c.enabled, "strategy": c.strategy,
                    "cooldown_seconds": c.cooldown_seconds,
                    "downgrade_preset": c.downgrade_preset,
                    "cancel_other_platforms": c.cancel_other_platforms}
                for p, c in self._configs.items()
            },
        }


# ─────────────────────────────────────────────────────────────────────
# 单例
# ─────────────────────────────────────────────────────────────────────

_HEALER: Optional[CrossPlatformRiskHealer] = None
_started = False


def get_cross_platform_healer() -> CrossPlatformRiskHealer:
    global _HEALER
    if _HEALER is None:
        _HEALER = CrossPlatformRiskHealer()
    return _HEALER


def _resolve_facebook_preset(preset_key: str) -> Optional[Dict]:
    try:
        from .routers.facebook import FB_FLOW_PRESETS
        return next((p for p in FB_FLOW_PRESETS
                     if p.get("key") == preset_key), None)
    except Exception:
        return None


def _resolve_tiktok_preset(preset_key: str) -> Optional[Dict]:
    """占位 — 等 TikTok 那边有 TT_FLOW_PRESETS 时拓展。"""
    try:
        from .routers.tiktok import TT_FLOW_PRESETS  # type: ignore
        return next((p for p in TT_FLOW_PRESETS
                     if p.get("key") == preset_key), None)
    except Exception:
        # 保底:返回一个最小 warmup(浏览主页 5 屏)
        if preset_key == "warmup":
            return {
                "key": "warmup",
                "steps": [{
                    "type": "tiktok_browse_home",
                    "params": {"scroll_count": 5},
                }],
            }
        return None


def start_cross_platform_risk_listener():
    """系统启动时调用:打补丁 + 注册所有平台 listener。"""
    global _started
    if _started:
        return
    patch_event_stream()
    healer = get_cross_platform_healer()

    # Facebook
    healer.register_platform("facebook", _resolve_facebook_preset)
    register_inproc_listener("facebook.risk_detected", healer.handle)

    # TikTok(占位 — 一旦 TikTok 模块 push 'tiktok.risk_detected' 事件即生效)
    healer.register_platform("tiktok", _resolve_tiktok_preset)
    register_inproc_listener("tiktok.risk_detected", healer.handle)

    _started = True
    logger.info("[risk_core] 跨平台风控总线已启动: %s",
                list(healer._configs.keys()))
