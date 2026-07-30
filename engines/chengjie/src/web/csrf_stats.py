"""CSRF 写请求拒绝观测（进程级单例）。

背景（2026-07-31「人设切换失败」事故）：S3 安全加固后，csrf_middleware 对缺
通行证（X-CSRF-Token / Bearer / 同源 Origin·Referer 三选一）的写请求直接 403
——但**零日志、零计数**，前端又把失败折叠成一句「请重试」。生产里坐席撞墙
两周，后台完全无感知，最终靠用户截图逆向定位。

本模块把中间件的每次拒绝变成可观测计数：按（消毒后的）path 与**拒绝形态**
累计，经 dump() → `/api/workspace/metrics.csrf_rejects`、dump_prom() →
Prometheus，进 ops 概览卡。形态分类本身就是诊断结论：

- ``cookie_no_header``  cookie 在、头没带 —— 「宿主页面缺 fetch 补丁 / 客户端
  未自带凭证」的签名（本次事故形态）；
- ``pair_mismatch``     cookie 与头都在但不相等 —— 多标签页/多域串 cookie 或攻击探测；
- ``origin_mismatch``   带了 Origin 但与 Host 不符 —— 反代/隧道改写 Host 或跨站请求；
- ``referer_mismatch``  带了 Referer 但与 Host 不符 —— 同上；
- ``bare``              什么都没带 —— 脚本/beacon/隐私浏览器（Referer 被剥）。

风格对齐 frontend_error_stats：线程安全、无新依赖、distinct key 封顶防撑爆、
只存消毒后的 path（数字段掩码 <n>，丢查询串），绝不存 token 值/完整 URL。
自带 5s/键的日志节流闸（should_log）——浏览器自动重试不至于刷爆 app.log。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional

_MAX_KEYS = 100
_PATH_SAFE = re.compile(r"[^A-Za-z0-9/_\-.:]")
_DIGITS_RE = re.compile(r"\d{2,}")
_KINDS = (
    "cookie_no_header", "pair_mismatch", "origin_mismatch",
    "referer_mismatch", "bare",
)


def _san_path(path: str) -> str:
    p = str(path or "").split("?", 1)[0].split("#", 1)[0].strip()
    p = _PATH_SAFE.sub("", p)
    if not p or not p.startswith("/"):
        return "unknown"
    return _DIGITS_RE.sub("<n>", p)[:100]


def classify_reject(*, had_cookie: bool, had_header: bool,
                    origin: str = "", referer: str = "") -> str:
    """按「带了什么、差在哪」归类一次 CSRF 拒绝（纯函数，门禁可测）。"""
    if had_cookie and had_header:
        return "pair_mismatch"
    if had_cookie:
        return "cookie_no_header"
    if origin:
        return "origin_mismatch"
    if referer:
        return "referer_mismatch"
    return "bare"


# 写请求放行「通行证」枚举（P2 收口决策的数据面）：
# - csrf_pair / bearer ＝ 正路（显式凭证）；
# - origin / referer   ＝ 同源回落——收口决策就看这两个是否长期归零
#   （若两周内 origin/referer 放行 ≈ 0，才能安全地把回落降级为纯观测）。
_TICKETS = ("csrf_pair", "bearer", "origin", "referer")


class CsrfRejectStats:
    """CSRF 拒绝 + 放行通行证计数（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_last_ts", "total",
                 "_by_kind", "_by_path", "_log_gate", "_admitted")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.total = 0
        self._by_kind: Dict[str, int] = {}
        self._by_path: Dict[str, int] = {}
        self._log_gate: Dict[str, float] = {}
        self._admitted: Dict[str, int] = {}

    def record(self, *, path: str = "", had_cookie: bool = False,
               had_header: bool = False, origin: str = "",
               referer: str = "") -> str:
        """登记一次拒绝，返回分类（调用方可直接放进日志行）。"""
        kind = classify_reject(had_cookie=had_cookie, had_header=had_header,
                               origin=origin, referer=referer)
        p = _san_path(path)
        with self._lock:
            self.total += 1
            self._last_ts = time.time()
            self._by_kind[kind] = self._by_kind.get(kind, 0) + 1
            if p in self._by_path or len(self._by_path) < _MAX_KEYS:
                self._by_path[p] = self._by_path.get(p, 0) + 1
            else:
                self._by_path["__other__"] = self._by_path.get("__other__", 0) + 1
        return kind

    def record_admit(self, ticket: str) -> None:
        """一次写请求放行，按通行证归类（白名单外丢弃；每写请求一次 dict 自增，零 IO）。"""
        t = str(ticket or "").strip()
        if t not in _TICKETS:
            return
        with self._lock:
            self._admitted[t] = self._admitted.get(t, 0) + 1

    def should_log(self, key: str, window_sec: float = 5.0) -> bool:
        """同 key（建议 ip+path）5s 内只放行一条 WARNING，防重试风暴刷日志。"""
        now = time.time()
        with self._lock:
            last = self._log_gate.get(key, 0.0)
            if now - last < window_sec:
                return False
            if len(self._log_gate) > 2048:   # 防长期运行下键集无界增长
                self._log_gate.clear()
            self._log_gate[key] = now
            return True

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "last_reject_ts": self._last_ts,
                "total": self.total,
                "by_kind": dict(sorted(self._by_kind.items())),
                "by_path": dict(sorted(
                    self._by_path.items(), key=lambda kv: (-kv[1], kv[0]))),
                "admitted_by": {t: self._admitted.get(t, 0) for t in _TICKETS},
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP csrf_rejects_total Write requests rejected by CSRF middleware",
                "# TYPE csrf_rejects_total counter",
                f"csrf_rejects_total {self.total}",
                "# HELP csrf_rejects_by_kind_total CSRF rejects by credential shape",
                "# TYPE csrf_rejects_by_kind_total counter",
            ]
            for k, n in sorted(self._by_kind.items()):
                lines.append(f'csrf_rejects_by_kind_total{{kind="{_esc(k)}"}} {int(n)}')
            lines += [
                "# HELP csrf_rejects_by_path_total CSRF rejects by sanitized path",
                "# TYPE csrf_rejects_by_path_total counter",
            ]
            for p, n in sorted(self._by_path.items()):
                lines.append(f'csrf_rejects_by_path_total{{path="{_esc(p)}"}} {int(n)}')
            lines += [
                "# HELP csrf_admits_total Write requests admitted, by credential ticket",
                "# TYPE csrf_admits_total counter",
            ]
            for t in _TICKETS:
                lines.append(
                    f'csrf_admits_total{{ticket="{t}"}} {int(self._admitted.get(t, 0))}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self._by_kind.clear()
            self._by_path.clear()
            self._log_gate.clear()
            self._admitted.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[CsrfRejectStats] = None
_LOCK = threading.Lock()


def get_csrf_reject_stats() -> CsrfRejectStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = CsrfRejectStats()
    return _SINGLETON


__all__ = ["CsrfRejectStats", "classify_reject", "get_csrf_reject_stats"]
