"""扫码登录漏斗观测（进程级单例，平台通用）。

背景（2026-07-25 事故）：LINE 协议扫码 100% 登录失败，持续多日无人知晓。后端把 PIN 送到了
浏览器、前端把它丢了，两端各自的单测都全绿——**没有任何一层在看「发起了多少次、成功了几次」**。
一个「pin_issued=N, authorized=0」的计数会在第一天就把它喊出来。

本模块把登录流程拆成漏斗段计数，按 (platform, mode) 归并：

    started → qr_shown → pin_issued → authorized
                                    ↘ failed[reason_code]

判读方式：
- ``qr_shown`` 远小于 ``started`` → 二维码生成链路坏了（网关不可达 / 渲染失败）。
- ``pin_issued`` 有量而 ``authorized`` 近零 → **PIN 没能抵达用户**，即本次事故的形态。
- ``failed`` 按 reason_code 分布 → 直接指出是超时、限流还是网络。

刻意做成平台通用而非 LINE 专用：同一个漏斗形状适用于 Telegram/WhatsApp/Messenger，
下次任一平台的登录链路悄悄坏掉，同一张表就能看见。

隐私：**只存计数**。绝不记录 PIN、token、account_id、二维码内容或任何用户可识别信息。
distinct key 有上限（防脏 platform/mode 值撑爆内存），超限归入 ``__other__``。

风格对齐 src/web/frontend_error_stats.py：无新增依赖，线程安全，进程级单例。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional

_MAX_KEYS = 40
_IDENT_RE = re.compile(r"^[a-z0-9_]{1,24}$")

# 与 line_protocol_login.classify_login_error 的枚举一致；越界值归 login_failed，
# 防脏码把 by_reason 撑成高基数维度。
_REASON_CODES = frozenset({
    "qr_expired", "pin_timeout", "network", "rate_limited",
    "login_failed", "okline_missing", "client_init", "cancelled",
})

_STAGES = ("started", "qr_shown", "pin_issued", "authorized", "failed")


def _san_key(platform: str, mode: str) -> str:
    p = str(platform or "").strip().lower()
    m = str(mode or "").strip().lower()
    p = p if _IDENT_RE.match(p) else "unknown"
    m = m if _IDENT_RE.match(m) else "unknown"
    return f"{p}:{m}"


def _san_reason(code: str) -> str:
    c = str(code or "").strip().lower()
    return c if c in _REASON_CODES else "login_failed"


class LoginFunnelStats:
    """扫码登录漏斗计数（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_last_ts", "_funnel", "_reasons",
                 "_auth_ms_sum", "_auth_ms_n", "overflow")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.overflow = 0
        self._funnel: Dict[str, Dict[str, int]] = {}
        self._reasons: Dict[str, Dict[str, int]] = {}
        # 只累计成功登录耗时：失败样本的耗时由超时预算主导，混进去会把均值钉死在超时值上
        self._auth_ms_sum: Dict[str, float] = {}
        self._auth_ms_n: Dict[str, int] = {}

    def _slot(self, key: str) -> Optional[Dict[str, int]]:
        row = self._funnel.get(key)
        if row is not None:
            return row
        if len(self._funnel) >= _MAX_KEYS:
            self.overflow += 1
            key = "__other__"
            row = self._funnel.get(key)
            if row is not None:
                return row
        row = {s: 0 for s in _STAGES}
        self._funnel[key] = row
        return row

    def record(self, platform: str, mode: str, stage: str, *,
               reason_code: str = "", elapsed_ms: float = 0.0) -> None:
        """记一次漏斗事件。调用方全部 best-effort——观测绝不能影响登录本身。"""
        if stage not in _STAGES:
            return
        key = _san_key(platform, mode)
        with self._lock:
            row = self._slot(key)
            if row is None:
                return
            row[stage] += 1
            self._last_ts = time.time()
            if stage == "failed":
                bucket = self._reasons.setdefault(key, {})
                r = _san_reason(reason_code)
                bucket[r] = bucket.get(r, 0) + 1
            elif stage == "authorized" and elapsed_ms > 0:
                self._auth_ms_sum[key] = self._auth_ms_sum.get(key, 0.0) + float(elapsed_ms)
                self._auth_ms_n[key] = self._auth_ms_n.get(key, 0) + 1

    def dump(self) -> Dict[str, Any]:
        """供 /api/workspace/metrics 消费。``stalled`` 是给看板的一眼判读。"""
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for key, f in sorted(self._funnel.items()):
                n = self._auth_ms_n.get(key, 0)
                rows.append({
                    "key": key,
                    "started": f["started"],
                    "qr_shown": f["qr_shown"],
                    "pin_issued": f["pin_issued"],
                    "authorized": f["authorized"],
                    "failed": f["failed"],
                    "reasons": dict(self._reasons.get(key, {})),
                    "avg_authorized_ms": round(self._auth_ms_sum.get(key, 0.0) / n) if n else 0,
                    # 发起过但一次都没成 = 该平台该方式的登录链路事实上不可用
                    "stalled": bool(f["started"] >= 3 and f["authorized"] == 0),
                })
            return {"rows": rows, "overflow": self.overflow,
                    "last_ts": self._last_ts, "since": self._started_at}

    def dump_prom(self) -> str:
        lines = [
            "# HELP login_funnel_total 扫码登录漏斗分段计数",
            "# TYPE login_funnel_total counter",
        ]
        with self._lock:
            for key, f in sorted(self._funnel.items()):
                plat, _, mode = key.partition(":")
                for stage in _STAGES:
                    lines.append(
                        f'login_funnel_total{{platform="{plat}",mode="{mode}",'
                        f'stage="{stage}"}} {f[stage]}')
            lines.append("# HELP login_funnel_failed_total 登录失败按原因码")
            lines.append("# TYPE login_funnel_failed_total counter")
            for key, bucket in sorted(self._reasons.items()):
                plat, _, mode = key.partition(":")
                for reason, n in sorted(bucket.items()):
                    lines.append(
                        f'login_funnel_failed_total{{platform="{plat}",mode="{mode}",'
                        f'reason="{reason}"}} {n}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._funnel.clear()
            self._reasons.clear()
            self._auth_ms_sum.clear()
            self._auth_ms_n.clear()
            self.overflow = 0


_stats: Optional[LoginFunnelStats] = None
_stats_lock = threading.Lock()


def get_login_funnel_stats() -> LoginFunnelStats:
    global _stats
    if _stats is None:
        with _stats_lock:
            if _stats is None:
                _stats = LoginFunnelStats()
    return _stats


def record_login_stage(platform: str, mode: str, stage: str, *,
                       reason_code: str = "", elapsed_ms: float = 0.0) -> None:
    """模块级便捷入口（调用方一律 try/except 包裹，观测失败绝不影响登录）。"""
    try:
        get_login_funnel_stats().record(
            platform, mode, stage, reason_code=reason_code, elapsed_ms=elapsed_ms)
    except Exception:  # noqa: BLE001
        pass
