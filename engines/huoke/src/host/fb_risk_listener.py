# -*- coding: utf-8 -*-
"""Facebook 风控自愈 — 向后兼容 shim(Sprint 3 P0 重构)。

Sprint 2 实现的 Facebook 专属 listener 已迁移到 `risk_auto_heal.py`
的跨平台总线;本文件保留是为了:
  - routers/facebook.py 旧调用 `from .fb_risk_listener import get_healer`
  - 暴露 facebook 专属字段 _cfg / _cooldown_until / _lock,
    保证 Sprint 2 的 4 个风控查询接口 0 改动继续工作。

新代码应直接用 `risk_auto_heal.get_cross_platform_healer()`。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_started = False


class _FacebookHealerAdapter:
    """适配 Sprint 2 facebook-only API → Sprint 3 跨平台 core。"""

    PLATFORM = "facebook"

    def __init__(self):
        from .risk_auto_heal import get_cross_platform_healer
        self._core = get_cross_platform_healer()

    @property
    def _lock(self):
        return self._core._lock

    @property
    def _cooldown_until(self) -> Dict[str, float]:
        """旧 API: device_id -> ts(只看 facebook 域)。"""
        with self._core._lock:
            return {k.split(":", 1)[1]: v
                    for k, v in self._core._cooldown.items()
                    if k.startswith("facebook:")}

    @property
    def _cfg(self) -> Dict[str, Any]:
        cfg = self._core._configs.get("facebook")
        if not cfg:
            return {}
        return {
            "enabled": cfg.enabled,
            "strategy": cfg.strategy,
            "cooldown_seconds": cfg.cooldown_seconds,
            "downgrade_to_preset": cfg.downgrade_preset,
            "cancel_other_platforms": cfg.cancel_other_platforms,
            "max_history_per_device": cfg.max_history_per_device,
        }

    def reload_config(self):
        self._core.reload_all_configs()

    def get_history(self, device_id: str) -> list:
        return self._core.get_history(device_id, platform="facebook")

    def get_all_histories(self) -> Dict[str, list]:
        return self._core.get_all_histories(platform="facebook")

    def get_cooldown_status(self, device_id: str) -> float:
        return self._core.get_cooldown_remaining("facebook", device_id)

    def handle_risk_event(self, payload: Dict):
        if "event_type" not in payload:
            payload = {**payload, "event_type": "facebook.risk_detected"}
        self._core.handle(payload)


_HEALER: Optional[_FacebookHealerAdapter] = None


def get_healer() -> _FacebookHealerAdapter:
    global _HEALER
    if _HEALER is None:
        _HEALER = _FacebookHealerAdapter()
    return _HEALER


def start_fb_risk_listener():
    """向后兼容入口 — 内部转发到跨平台 listener。"""
    global _started
    if _started:
        return
    from .risk_auto_heal import start_cross_platform_risk_listener
    start_cross_platform_risk_listener()
    _started = True
    logger.info("[fb_risk] 已转发到跨平台风控总线 (risk_auto_heal)")
