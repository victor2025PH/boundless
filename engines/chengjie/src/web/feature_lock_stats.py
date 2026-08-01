"""功能锁触达观测（进程级单例）——「哪个功能的锁被撞得最多」。

背景（E6，2026-08-01）：档位闸门把未解锁功能拦在 API（403）与页面（302 →
/membership 升级引导）两个面，但**拦截本身此前不可观测**：运营不知道客户
在撞哪个锁、撞多频——而这正是「下一个该进低档位的功能是谁」「哪个功能该
重点卖」的定价/打包信号。

本模块把两类拦截变成计数：``record(family, kind)``（kind=api|page）按功能
族累计，经 dump() → ``/api/workspace/metrics.feature_lock``、dump_prom() →
Prometheus。**只记我方注册表内的族名与计数，零用户内容零 PII**。

刻意不计的：运营 kill-switch（inbox.workflows.enabled=false 等 config 关闭）
——那是部署选择不是购买意向，混进来会污染定价信号；/membership ?from= 到达
数（302 自动跟随，与 page 计数近似恒等，重复维度）。

风格对齐 frontend_error_stats.py：无新增依赖，线程安全，进程级单例。
族名来自我方 FEATURE_MIN_PLAN 注册表（调用点全在自家守卫），仍做形状消毒
（截断 + 白名单字符）防未来调用点手滑。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_KINDS = ("api", "page")


def _san_family(family: str) -> str:
    f = str(family or "").strip().lower()
    return f if _FAMILY_RE.match(f) else "unknown"


class FeatureLockStats:
    """功能锁拦截计数（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_last_ts", "total", "_by")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.total = 0
        # family → {"api": n, "page": n}
        self._by: Dict[str, Dict[str, int]] = {}

    def record(self, family: str, kind: str) -> None:
        if kind not in _KINDS:
            return
        fam = _san_family(family)
        with self._lock:
            self.total += 1
            self._last_ts = time.time()
            b = self._by.setdefault(fam, {"api": 0, "page": 0})
            b[kind] += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            by = {
                fam: {"api": v["api"], "page": v["page"],
                      "total": v["api"] + v["page"]}
                for fam, v in self._by.items()
            }
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total": self.total,
                "by_family": dict(sorted(
                    by.items(), key=lambda kv: (-kv[1]["total"], kv[0]))),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP feature_lock_hits_total Feature-gate lock hits (api 403 + page redirect)",
                "# TYPE feature_lock_hits_total counter",
                f"feature_lock_hits_total {self.total}",
                "# HELP feature_lock_hits_by_family_total Lock hits by feature family and surface",
                "# TYPE feature_lock_hits_by_family_total counter",
            ]
            for fam in sorted(self._by):
                v = self._by[fam]
                for kind in _KINDS:
                    lines.append(
                        f'feature_lock_hits_by_family_total{{family="{_esc(fam)}",kind="{kind}"}} '
                        f"{int(v[kind])}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self._by.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[FeatureLockStats] = None
_LOCK = threading.Lock()


def get_feature_lock_stats() -> FeatureLockStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = FeatureLockStats()
    return _SINGLETON


__all__ = ["FeatureLockStats", "get_feature_lock_stats"]
