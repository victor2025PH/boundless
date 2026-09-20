# -*- coding: utf-8 -*-
"""出站媒体归档发布观测（进程级单例）。

背景：A 线（pyrogram 直发）的出站图/语音**发完即删**，坐席台能否回放取决于
``publish_outbound_media`` 有没有先把文件归档到 /static。该步是 best-effort——
失败只回空串，调用方静默退回纯文本占位镜像。这个「静默降级」此前完全无观测：
static 目录写坏 / 磁盘满 / 路径漂移时，坐席只会发现「自己发的语音又看不到了」，
没有任何计数指向根因（2026-08-02 P0，配套分条语音逐条镜像落地）。

本模块按 (platform, kind) 累计发布成败：
- ``dump()``      → ``/api/workspace/metrics.outbound_mirror``
- ``dump_prom()`` → Prometheus（``outbound_media_publish_*``）
- ops-overview「📤 出站媒体归档」卡在失败 > 0 时亮出（零失败不占版面）。

风格对齐 src/web/frontend_error_stats.py：无新增依赖、线程安全、进程级单例、
distinct key 封顶防撑爆。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

# platform 是小集合（telegram/whatsapp/line/messenger…）、kind 来自
# media_type_from_ext 的小枚举；上限只是防脏入参撑爆内存的保险丝。
_MAX_COMBOS = 32


def _san(s: str) -> str:
    v = str(s or "").strip().lower()
    return (v or "unknown")[:24]


class OutboundMirrorStats:
    """出站媒体归档发布计数（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_last_ok_ts", "_last_fail_ts",
                 "total", "fail", "_by_combo")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ok_ts = 0.0
        self._last_fail_ts = 0.0
        self.total = 0
        self.fail = 0
        # {(platform, kind): [total, fail]}
        self._by_combo: Dict[Tuple[str, str], list] = {}

    def record_publish(self, platform: str, kind: str, *, ok: bool) -> None:
        key = (_san(platform), _san(kind))
        with self._lock:
            self.total += 1
            if not ok:
                self.fail += 1
                self._last_fail_ts = time.time()
            else:
                self._last_ok_ts = time.time()
            if key not in self._by_combo and len(self._by_combo) >= _MAX_COMBOS:
                key = ("__other__", "__other__")
            c = self._by_combo.setdefault(key, [0, 0])
            c[0] += 1
            if not ok:
                c[1] += 1

    @staticmethod
    def _nest(items, idx: int) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for (plat, kind), (t, f) in items:
            k = (plat, kind)[idx]
            d = out.setdefault(k, {"total": 0, "fail": 0})
            d["total"] += t
            d["fail"] += f
        return dict(sorted(out.items(), key=lambda kv: (-kv[1]["fail"], -kv[1]["total"], kv[0])))

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            items = list(self._by_combo.items())
            return {
                "started_at": self._started_at,
                "last_ok_ts": self._last_ok_ts,
                "last_fail_ts": self._last_fail_ts,
                "total": self.total,
                "fail": self.fail,
                "by_platform": self._nest(items, 0),
                "by_kind": self._nest(items, 1),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP outbound_media_publish_total Outbound media archive publish attempts (A-line mirror)",
                "# TYPE outbound_media_publish_total counter",
            ]
            for (plat, kind), (t, _f) in sorted(self._by_combo.items()):
                lines.append(
                    f'outbound_media_publish_total{{platform="{plat}",kind="{kind}"}} {int(t)}')
            lines += [
                "# HELP outbound_media_publish_fail_total Outbound media archive publish failures (mirror silently degrades to text placeholder)",
                "# TYPE outbound_media_publish_fail_total counter",
            ]
            for (plat, kind), (_t, f) in sorted(self._by_combo.items()):
                lines.append(
                    f'outbound_media_publish_fail_total{{platform="{plat}",kind="{kind}"}} {int(f)}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.fail = 0
            self._last_ok_ts = 0.0
            self._last_fail_ts = 0.0
            self._by_combo.clear()


_SINGLETON: Optional[OutboundMirrorStats] = None
_LOCK = threading.Lock()


def get_outbound_mirror_stats() -> OutboundMirrorStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = OutboundMirrorStats()
    return _SINGLETON


__all__ = ["OutboundMirrorStats", "get_outbound_mirror_stats"]
