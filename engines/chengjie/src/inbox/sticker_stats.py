"""贴纸使用观测（进程级单例；2026-08-17 表情包主线）。

只计**发送侧**读数（面板打开/点选是前端 ``_uiBeacon`` 埋点的事，这里不重复）：
- ``sends``：总发送成功数；
- ``by_platform``：platform → n；
- ``by_sent_as``：sticker|image → n（image=回退发送，占比高说明目标平台
  原生能力缺口大——WA 边车没升级 / LINE 自建包占比高）；
- ``collects``：入站贴纸收藏入包次数。

风格对齐 ``frontend_error_stats``：纯内存、重启清零（持久口径看 DB hits）、
``dump()`` 进 ``/api/workspace/metrics.stickers``、``dump_prom()`` 出 Prometheus。
"""
from __future__ import annotations

import threading
from typing import Any, Dict

_MAX_KEYS = 32  # platform 维度天花板（防异常平台名撑爆）


class StickerStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sends = 0
        self._collects = 0
        self._by_platform: Dict[str, int] = {}
        self._by_sent_as: Dict[str, int] = {}

    def record_send(self, platform: str, sent_as: str) -> None:
        p = str(platform or "unknown")[:24]
        s = str(sent_as or "unknown")[:16]
        with self._lock:
            self._sends += 1
            if p in self._by_platform or len(self._by_platform) < _MAX_KEYS:
                self._by_platform[p] = self._by_platform.get(p, 0) + 1
            if s in self._by_sent_as or len(self._by_sent_as) < _MAX_KEYS:
                self._by_sent_as[s] = self._by_sent_as.get(s, 0) + 1

    def record_collect(self) -> None:
        with self._lock:
            self._collects += 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "sends": self._sends,
                "collects": self._collects,
                "by_platform": dict(self._by_platform),
                "by_sent_as": dict(self._by_sent_as),
            }

    def dump_prom(self) -> str:
        d = self.dump()
        lines = [
            "# TYPE ws_sticker_sends_total counter",
            f"ws_sticker_sends_total {d['sends']}",
            "# TYPE ws_sticker_collects_total counter",
            f"ws_sticker_collects_total {d['collects']}",
            "# TYPE ws_sticker_sends_by_platform_total counter",
        ]
        for k, v in sorted(d["by_platform"].items()):
            lines.append(f'ws_sticker_sends_by_platform_total{{platform="{k}"}} {v}')
        lines.append("# TYPE ws_sticker_sends_by_sent_as_total counter")
        for k, v in sorted(d["by_sent_as"].items()):
            lines.append(f'ws_sticker_sends_by_sent_as_total{{sent_as="{k}"}} {v}')
        return "\n".join(lines) + "\n"


_INSTANCE: StickerStats | None = None
_LOCK = threading.Lock()


def get_sticker_stats() -> StickerStats:
    global _INSTANCE
    if _INSTANCE is None:
        with _LOCK:
            if _INSTANCE is None:
                _INSTANCE = StickerStats()
    return _INSTANCE
