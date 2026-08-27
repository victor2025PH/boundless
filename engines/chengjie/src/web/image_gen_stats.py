"""坐席手动出图观测（进程级单例，2026-08-22 P1）。

背景：手动出图（cp-image → /api/image/*）此前零观测——「用了多少、成功率、
失败在哪、多慢、相册秒发替代了多少次 GPU 渲染」全部无读数，而这些正是
「要不要迁 173 / 要不要限额 / 值不值得推广」的决策输入（2026-08-22 模型被
清空事故里，故障发生到被发现隔了数小时，也是因为无读数无告警）。

计数面：
- 生成漏斗：尝试 / 成功 / 失败（按错误码分桶——model_missing 涨=运维事故；
  vram_insufficient 涨=算力互挤；gen_timeout 涨=冷启动窗）
- 时延：成功样本环形窗（近 50 发）出 p50/p95 + 峰值——「数十秒」的承诺是否还成立
- 出口：发送（生成图/相册图分开——album 占比高=相册优先层在省 GPU）/ 存册
- 相册优先层：查询数 / 有货数（命中率低=备货缺口，接 media_gap 补）
- 任务取消数（取消率高=坐席等不起=该催异步分级/迁 173）

风格对齐 frontend_error_stats：线程安全、零依赖、dump()/dump_prom() 双出口。
消费面：/api/workspace/metrics.image_gen + Prometheus image_gen_*。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

_LAT_WINDOW = 50  # 成功时延环形窗样本数


class ImageGenStats:
    """手动出图计数（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_started_at", "_last_ts",
        "gen_total", "gen_ok", "gen_fail", "cancelled",
        "_fail_by_code", "_by_engine", "_lat_ms",
        "sent_generated", "sent_album", "saved_album",
        "stock_queries", "stock_hits",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.gen_total = 0
        self.gen_ok = 0
        self.gen_fail = 0
        self.cancelled = 0
        self._fail_by_code: Dict[str, int] = {}
        self._by_engine: Dict[str, int] = {}
        self._lat_ms: List[int] = []
        self.sent_generated = 0
        self.sent_album = 0
        self.saved_album = 0
        self.stock_queries = 0
        self.stock_hits = 0

    def record_attempt(self, engine: str) -> None:
        with self._lock:
            self.gen_total += 1
            self._last_ts = time.time()
            e = str(engine or "unknown")[:32]
            self._by_engine[e] = self._by_engine.get(e, 0) + 1

    def record_ok(self, latency_ms: int) -> None:
        with self._lock:
            self.gen_ok += 1
            self._lat_ms.append(int(latency_ms))
            if len(self._lat_ms) > _LAT_WINDOW:
                self._lat_ms.pop(0)

    def record_fail(self, code: str) -> None:
        with self._lock:
            self.gen_fail += 1
            c = str(code or "unknown")[:32]
            self._fail_by_code[c] = self._fail_by_code.get(c, 0) + 1

    def record_cancel(self) -> None:
        with self._lock:
            self.cancelled += 1

    def record_sent(self, *, album: bool) -> None:
        with self._lock:
            if album:
                self.sent_album += 1
            else:
                self.sent_generated += 1

    def record_saved(self) -> None:
        with self._lock:
            self.saved_album += 1

    def record_stock_query(self, hit: bool) -> None:
        with self._lock:
            self.stock_queries += 1
            if hit:
                self.stock_hits += 1

    @staticmethod
    def _pct(sorted_ms: List[int], q: float) -> int:
        if not sorted_ms:
            return 0
        idx = min(len(sorted_ms) - 1, max(0, int(round(q * (len(sorted_ms) - 1)))))
        return int(sorted_ms[idx])

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            lat = sorted(self._lat_ms)
            return {
                "started_at": self._started_at,
                "last_ts": self._last_ts,
                "gen_total": self.gen_total,
                "gen_ok": self.gen_ok,
                "gen_fail": self.gen_fail,
                "cancelled": self.cancelled,
                "fail_by_code": dict(sorted(
                    self._fail_by_code.items(), key=lambda kv: (-kv[1], kv[0]))),
                "by_engine": dict(sorted(
                    self._by_engine.items(), key=lambda kv: (-kv[1], kv[0]))),
                "latency_p50_ms": self._pct(lat, 0.50),
                "latency_p95_ms": self._pct(lat, 0.95),
                "latency_max_ms": lat[-1] if lat else 0,
                "sent_generated": self.sent_generated,
                "sent_album": self.sent_album,
                "saved_album": self.saved_album,
                "stock_queries": self.stock_queries,
                "stock_hits": self.stock_hits,
            }

    def dump_prom(self) -> str:
        d = self.dump()
        lines = [
            "# HELP image_gen_total Manual image generation attempts",
            "# TYPE image_gen_total counter",
            f"image_gen_total {d['gen_total']}",
            "# HELP image_gen_ok_total Manual image generation successes",
            "# TYPE image_gen_ok_total counter",
            f"image_gen_ok_total {d['gen_ok']}",
            "# HELP image_gen_fail_total Manual image generation failures by code",
            "# TYPE image_gen_fail_total counter",
        ]
        for c, n in d["fail_by_code"].items():
            lines.append(f'image_gen_fail_total{{code="{_esc(c)}"}} {int(n)}')
        lines += [
            "# HELP image_gen_cancelled_total Manual image jobs cancelled by agent",
            "# TYPE image_gen_cancelled_total counter",
            f"image_gen_cancelled_total {d['cancelled']}",
            "# HELP image_gen_sent_total Images sent to customers (by source)",
            "# TYPE image_gen_sent_total counter",
            f'image_gen_sent_total{{source="generated"}} {d["sent_generated"]}',
            f'image_gen_sent_total{{source="album"}} {d["sent_album"]}',
            "# HELP image_gen_latency_ms Manual generation latency (rolling window)",
            "# TYPE image_gen_latency_ms gauge",
            f'image_gen_latency_ms{{q="p50"}} {d["latency_p50_ms"]}',
            f'image_gen_latency_ms{{q="p95"}} {d["latency_p95_ms"]}',
            "# HELP image_gen_stock_queries_total Album-first stock lookups (hit=had stock)",
            "# TYPE image_gen_stock_queries_total counter",
            f"image_gen_stock_queries_total {d['stock_queries']}",
            "# HELP image_gen_stock_hits_total Album-first stock lookups that found stock",
            "# TYPE image_gen_stock_hits_total counter",
            f"image_gen_stock_hits_total {d['stock_hits']}",
        ]
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.gen_total = self.gen_ok = self.gen_fail = self.cancelled = 0
            self._fail_by_code.clear()
            self._by_engine.clear()
            self._lat_ms.clear()
            self.sent_generated = self.sent_album = self.saved_album = 0
            self.stock_queries = self.stock_hits = 0
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[ImageGenStats] = None
_LOCK = threading.Lock()


def get_image_gen_stats() -> ImageGenStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = ImageGenStats()
    return _SINGLETON


__all__ = ["ImageGenStats", "get_image_gen_stats"]
