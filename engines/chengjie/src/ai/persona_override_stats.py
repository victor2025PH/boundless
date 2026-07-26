"""会话级人设覆写观测（进程级单例）。

背景：2026-07-26 方案 A 上线「会话级人设覆写」（``inbox.persona_conv_override``），
出站链统一走 ``persona_voice.resolve_effective_persona``。上线后两个运营问题此前无数据：
① 覆写真的有人用吗（决定该开关将来要不要默认开）；② 7/24 修复后被账号层压制的
legacy peer-global 绑定还剩多少「活债」（决定清理工具的优先级与收官时点）。

本模块把出站解析与治理动作变成计数：
- ``record_resolve(tier, platform, legacy_present)`` —— 每次 resolve_effective_persona
  出结果时打点（tier 分布 + 覆写命中按平台 + legacy 被压制次数）；
- ``record_action(kind)`` —— bind_conv / unbind_conv / account_set /
  legacy_upgrade / legacy_remove 五类治理动作（persona_routes 写侧埋点）。

读出：dump() → ``/api/workspace/metrics.persona_override``；dump_prom() → Prometheus
``persona_override_*``；ops-overview「🎭 人设覆写」卡（零活动整卡隐藏）。
风格对齐 src/web/frontend_error_stats.py：无新依赖、线程安全、进程级单例；
埋点全部 best-effort——观测绝不允许影响出站解析本体。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_MAX_PLATFORMS = 20  # by_platform distinct key 上限（平台是小集合，防脏数据撑爆）

_TIERS = ("conv_override", "account_profile", "none")
_ACTIONS = ("bind_conv", "unbind_conv", "account_set",
            "legacy_upgrade", "legacy_remove")


def _san_platform(platform: str) -> str:
    p = str(platform or "").strip().lower()
    return p[:24] if p else "unknown"


class PersonaOverrideStats:
    """人设覆写解析/治理动作计数（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_started_at", "_last_ts",
        "resolves", "legacy_suppressed", "_by_tier", "_conv_by_platform",
        "_actions",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.resolves = 0
        self.legacy_suppressed = 0
        self._by_tier: Dict[str, int] = {}
        self._conv_by_platform: Dict[str, int] = {}
        self._actions: Dict[str, int] = {}

    def record_resolve(self, tier: str, platform: str = "",
                       legacy_present: bool = False) -> None:
        """一次出站解析：tier=解析结果档位（空串按 none 归档）。

        ``legacy_present``：这条会话存在 legacy peer-global 绑定。tier 非空时该绑定
        没赢（被会话/账号层压制）→ 计入 legacy_suppressed；tier 为空时 legacy 会在
        调用方回落链里生效，不算被压制。
        """
        t = str(tier or "").strip() or "none"
        if t not in _TIERS:
            t = "none"
        with self._lock:
            self.resolves += 1
            self._last_ts = time.time()
            self._by_tier[t] = self._by_tier.get(t, 0) + 1
            if t == "conv_override":
                p = _san_platform(platform)
                if p in self._conv_by_platform or \
                        len(self._conv_by_platform) < _MAX_PLATFORMS:
                    self._conv_by_platform[p] = self._conv_by_platform.get(p, 0) + 1
                else:
                    self._conv_by_platform["__other__"] = \
                        self._conv_by_platform.get("__other__", 0) + 1
            if legacy_present and t != "none":
                self.legacy_suppressed += 1

    def record_action(self, kind: str) -> None:
        """一次治理动作（换绑/解除/整号切换/legacy 升级/legacy 清除）。"""
        k = str(kind or "").strip()
        if k not in _ACTIONS:
            return
        with self._lock:
            self._last_ts = time.time()
            self._actions[k] = self._actions.get(k, 0) + 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "resolves": self.resolves,
                "conv_hits": self._by_tier.get("conv_override", 0),
                "account_hits": self._by_tier.get("account_profile", 0),
                "fallback_hits": self._by_tier.get("none", 0),
                "legacy_suppressed": self.legacy_suppressed,
                "conv_by_platform": dict(sorted(
                    self._conv_by_platform.items(),
                    key=lambda kv: (-kv[1], kv[0]))),
                "actions": {k: self._actions.get(k, 0) for k in _ACTIONS},
                "actions_total": sum(self._actions.values()),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP persona_override_resolves_total Outbound persona resolutions (denominator)",
                "# TYPE persona_override_resolves_total counter",
                f"persona_override_resolves_total {self.resolves}",
                "# HELP persona_override_by_tier_total Outbound persona resolutions by winning tier",
                "# TYPE persona_override_by_tier_total counter",
            ]
            for t in _TIERS:
                n = self._by_tier.get(t, 0)
                lines.append(
                    f'persona_override_by_tier_total{{tier="{t}"}} {int(n)}')
            lines += [
                "# HELP persona_override_legacy_suppressed_total Resolutions where a legacy peer-global binding existed but was overridden",
                "# TYPE persona_override_legacy_suppressed_total counter",
                f"persona_override_legacy_suppressed_total {self.legacy_suppressed}",
                "# HELP persona_override_conv_hits_by_platform_total Conversation-override wins by platform",
                "# TYPE persona_override_conv_hits_by_platform_total counter",
            ]
            for p, n in sorted(self._conv_by_platform.items()):
                lines.append(
                    f'persona_override_conv_hits_by_platform_total{{platform="{_esc(p)}"}} {int(n)}')
            lines += [
                "# HELP persona_override_actions_total Persona governance actions",
                "# TYPE persona_override_actions_total counter",
            ]
            for k in _ACTIONS:
                lines.append(
                    f'persona_override_actions_total{{action="{k}"}} {int(self._actions.get(k, 0))}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.resolves = 0
            self.legacy_suppressed = 0
            self._by_tier.clear()
            self._conv_by_platform.clear()
            self._actions.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[PersonaOverrideStats] = None
_LOCK = threading.Lock()


def get_persona_override_stats() -> PersonaOverrideStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = PersonaOverrideStats()
    return _SINGLETON


__all__ = ["PersonaOverrideStats", "get_persona_override_stats"]
