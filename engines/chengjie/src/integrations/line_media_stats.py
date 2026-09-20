"""LINE 媒体收发观测（进程级单例）。

**存在的唯一理由**：LINE 出站媒体（``platform_login.line.media.outbound``）默认关，
放量与否要靠数据而不是感觉。这里只留能回答「该不该放量」的那几个计数，刻意**不做**
avatar_voice_stats 那种几十字段的全景盘——LINE 当前零流量，宽口径观测等于噪音。

最要紧的一个数是 ``outbound.orphan_recalled``：LINE 媒体是「先发空壳占位、再传字节」，
两步之间失败会给客户留一条永久点不开的破图；``line_media.send_line_media`` 会撤回那条
占位，撤回次数就是**这条链在真实网络下有多不稳**的直接读数。它若持续非零，就该去做
Letter Sealing 预检（``determineMediaMessageFlow``）把占位根本不发出去。
``recall_failed`` 非零则更严重＝真有客户看到了破图。

与 ``avatar_voice_stats`` 同风格：无新增依赖，``dump()`` 供 /api/workspace/metrics，
``dump_prom()`` 供 Prometheus，**绝不记录消息原文**（只记 kind / 原因标签）。
所有 record 吞异常，绝不阻塞收发主链路。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class LineMediaStats:
    __slots__ = (
        "_lock", "_in_total", "_in_ok", "_in_by_kind", "_in_skip",
        "_out_total", "_out_ok", "_out_by_kind", "_out_fail",
        "_orphan_recalled", "_recall_failed", "_caption_failed",
        "_started_at", "_last_ts",
        "_recv_giveups", "_recv_giveup_by_acct", "_recv_hold_total_sec",
        "_recv_last_reason",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 入站：尝试过下载的媒体条数 / 真拿到字节的条数 / 按大类 / 未取到的原因分布
        self._in_total = 0
        self._in_ok = 0
        self._in_by_kind: Dict[str, int] = {}
        self._in_skip: Dict[str, int] = {}
        # 出站
        self._out_total = 0
        self._out_ok = 0
        self._out_by_kind: Dict[str, int] = {}
        self._out_fail: Dict[str, int] = {}
        self._orphan_recalled = 0    # 上传失败 → 撤回了占位（客户没看到破图）
        self._recall_failed = 0      # 连撤回都失败 → 客户**可能真看到了**破图
        self._caption_failed = 0     # 图发出去了但配文没发出（不影响 delivered）
        # #180：LINE receiver（gw operation/receive 长轮询）连败放弃→交编排器重启的次数。
        # 它就是「媒体按钮/语音发送突然 501」的直接上游读数：放弃期 worker 不在 running。
        self._recv_giveups = 0
        self._recv_giveup_by_acct: Dict[str, int] = {}
        self._recv_hold_total_sec = 0.0
        self._recv_last_reason = ""
        self._started_at = time.time()
        self._last_ts = 0.0

    # ── 入站 ─────────────────────────────────────────────────────────────────
    def record_inbound(self, kind: str, *, ok: bool, skip_reason: str = "") -> None:
        try:
            with self._lock:
                self._in_total += 1
                self._last_ts = time.time()
                k = str(kind or "unknown")[:16]
                if ok:
                    self._in_ok += 1
                    self._in_by_kind[k] = self._in_by_kind.get(k, 0) + 1
                else:
                    r = str(skip_reason or "unknown")[:24]
                    self._in_skip[r] = self._in_skip.get(r, 0) + 1
        except Exception:
            pass

    # ── 出站 ─────────────────────────────────────────────────────────────────
    def record_outbound(self, kind: str, *, ok: bool, error: str = "") -> None:
        try:
            with self._lock:
                self._out_total += 1
                self._last_ts = time.time()
                k = str(kind or "unknown")[:16]
                if ok:
                    self._out_ok += 1
                    self._out_by_kind[k] = self._out_by_kind.get(k, 0) + 1
                else:
                    e = str(error or "unknown")[:24]
                    self._out_fail[e] = self._out_fail.get(e, 0) + 1
        except Exception:
            pass

    def record_orphan_recall(self, *, recalled: bool) -> None:
        """上传失败后的占位处置：``recalled=False`` 表示连撤回都失败（最坏情况）。"""
        try:
            with self._lock:
                if recalled:
                    self._orphan_recalled += 1
                else:
                    self._recall_failed += 1
                self._last_ts = time.time()
        except Exception:
            pass

    def record_caption_failed(self) -> None:
        try:
            with self._lock:
                self._caption_failed += 1
        except Exception:
            pass

    # ── receiver（#180）───────────────────────────────────────────────────────
    def record_receiver_giveup(self, account_id: str, *, reason: str = "",
                               hold_sec: float = 0.0) -> None:
        """receiver 连败放弃一次（→ 编排器重启）；``hold_sec``＝随之安排的跨重启退避。"""
        try:
            with self._lock:
                self._recv_giveups += 1
                a = str(account_id or "unknown")[:16]
                self._recv_giveup_by_acct[a] = self._recv_giveup_by_acct.get(a, 0) + 1
                self._recv_hold_total_sec += max(0.0, float(hold_sec or 0.0))
                if reason:
                    self._recv_last_reason = str(reason)[:200]
                self._last_ts = time.time()
        except Exception:
            pass

    # ── 导出 ─────────────────────────────────────────────────────────────────
    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active": bool(self._in_total or self._out_total or self._recv_giveups),
                "receiver": {
                    "restarts_total": self._recv_giveups,
                    "by_account": dict(self._recv_giveup_by_acct),
                    "hold_total_sec": int(self._recv_hold_total_sec),
                    "last_reason": self._recv_last_reason,
                },
                "inbound": {
                    "total": self._in_total,
                    "ok": self._in_ok,
                    "by_kind": dict(self._in_by_kind),
                    "skipped": dict(self._in_skip),
                },
                "outbound": {
                    "total": self._out_total,
                    "ok": self._out_ok,
                    "by_kind": dict(self._out_by_kind),
                    "failed": dict(self._out_fail),
                    # 两步链的健康度：撤回过几次占位、有没有撤不掉的
                    "orphan_recalled": self._orphan_recalled,
                    "recall_failed": self._recall_failed,
                    "caption_failed": self._caption_failed,
                },
                "last_ts": self._last_ts,
                "uptime_sec": int(time.time() - self._started_at),
            }

    def dump_prom(self) -> str:
        d = self.dump()
        i, o = d["inbound"], d["outbound"]
        lines = [
            "line_media_inbound_total %d" % i["total"],
            "line_media_inbound_ok_total %d" % i["ok"],
            "line_media_outbound_total %d" % o["total"],
            "line_media_outbound_ok_total %d" % o["ok"],
            "line_media_orphan_recalled_total %d" % o["orphan_recalled"],
            "line_media_recall_failed_total %d" % o["recall_failed"],
            "line_receiver_restarts_total %d" % d["receiver"]["restarts_total"],
            "line_receiver_hold_seconds_total %d" % d["receiver"]["hold_total_sec"],
        ]
        for k, v in sorted(i["skipped"].items()):
            lines.append('line_media_inbound_skipped_total{reason="%s"} %d' % (k, v))
        for k, v in sorted(o["failed"].items()):
            lines.append('line_media_outbound_failed_total{error="%s"} %d' % (k, v))
        return "\n".join(lines)

    def reset(self) -> None:
        """仅供测试：清零（生产从不调用）。"""
        with self._lock:
            self.__init__()  # type: ignore[misc]


_STATS: Optional[LineMediaStats] = None
_STATS_LOCK = threading.Lock()


def get_line_media_stats() -> LineMediaStats:
    global _STATS
    if _STATS is None:
        with _STATS_LOCK:
            if _STATS is None:
                _STATS = LineMediaStats()
    return _STATS


__all__ = ["LineMediaStats", "get_line_media_stats"]
