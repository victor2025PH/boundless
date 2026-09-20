# -*- coding: utf-8 -*-
"""UI 语言来源观测（进程级单例；系统语言自动跟随 2026-08-27 配套）。

回答两个运营问题：
1. 有多少坐席在被 Accept-Language **自动推断**服务（negotiated 占比高 = 「跟随
   系统」价值实锤；某语种 negotiated 高但词包覆盖低 = 校对优先级信号）；
2. 显式选择（cookie）与推断的语言分布——ja/ko 等**未支持语言的推断落空**
   （default 且浏览器带了我们不认识的语言）暂不细分，后续要做语种扩张决策时
   在 negotiate 里加「最想要却没有」计数即可。

风格对齐 frontend_error_stats：零依赖、线程安全、只存计数（无 PII）。
消费：/api/workspace/metrics.ui_lang（drafts_routes 合并出口）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_SOURCES = ("query", "cookie", "negotiated", "default")


class UiLangStats:
    """语言解析来源计数（record 在请求热路径上——纯字典自增，O(1) 无 IO）。"""

    __slots__ = ("_lock", "_started_at", "total", "_by_source", "_by_lang",
                 "_negotiated_by_lang", "_unsupported_by_lang",
                 "_conflicts_by_query_lang", "_switches_to", "_switch_back_from",
                 "_last_switch")

    _MAX_UNSUPPORTED_KEYS = 30  # 防 UA 乱报语言撑爆内存；超限归 __other__
    _MAX_SWITCH_SESSIONS = 512  # 「切回」判定的会话记忆上限（FIFO 淘汰）
    SWITCH_BACK_WINDOW_S = 300  # 切换后多久内切回算「放弃该语种」

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self.total = 0
        self._by_source: Dict[str, int] = {}
        self._by_lang: Dict[str, int] = {}
        self._negotiated_by_lang: Dict[str, int] = {}
        # 「想要却没有」：negotiate 落空时浏览器最想要的语言（ja/ko…）——
        # 语种扩张立项的真实需求分布，不再拍脑袋。
        self._unsupported_by_lang: Dict[str, int] = {}
        # 2026-09-12 切语言不生效事故配套的两个「最能暴露问题」的信号：
        # ① ?lang= 与 ui_lang cookie 不一致（URL 钉住 ≠ 用户选择）——桌面壳首帧/分享链接
        #    把用户选择压掉的次数，按 query 语种分桶；
        # ② 切到某语种后 5 分钟内又切回原语种——该语种「翻译质量/覆盖不够用」的放弃信号。
        self._conflicts_by_query_lang: Dict[str, int] = {}
        self._switches_to: Dict[str, int] = {}
        self._switch_back_from: Dict[str, int] = {}
        self._last_switch: Dict[str, tuple] = {}   # sid → (from, to, ts)

    def record(self, source: str, lang: str) -> None:
        s = source if source in _SOURCES else "default"
        lg = str(lang or "zh")[:16]
        with self._lock:
            self.total += 1
            self._by_source[s] = self._by_source.get(s, 0) + 1
            self._by_lang[lg] = self._by_lang.get(lg, 0) + 1
            if s == "negotiated":
                self._negotiated_by_lang[lg] = self._negotiated_by_lang.get(lg, 0) + 1

    def record_unsupported(self, primary_tag: str) -> None:
        t = str(primary_tag or "").strip()[:8]
        if not t:
            return
        with self._lock:
            d = self._unsupported_by_lang
            if t in d or len(d) < self._MAX_UNSUPPORTED_KEYS:
                d[t] = d.get(t, 0) + 1
            else:
                d["__other__"] = d.get("__other__", 0) + 1

    def record_conflict(self, query_lang: str, cookie_lang: str) -> None:
        """``?lang=`` 压过了不同值的 cookie：用户选择被 URL 钉住的一次。"""
        q = str(query_lang or "")[:16]
        c = str(cookie_lang or "")[:16]
        if not q or not c or q == c:
            return
        with self._lock:
            self._conflicts_by_query_lang[q] = self._conflicts_by_query_lang.get(q, 0) + 1

    def record_switch(self, sid: str, from_lang: str, to_lang: str,
                      now: Optional[float] = None) -> bool:
        """一次 /set_lang。返回是否判定为「窗口内切回」（同会话上一次切到 X，现在又切回原语）。

        sid 由调用方给匿名会话键（用户名/会话 cookie 的短哈希），这里不存原值。
        """
        f = str(from_lang or "")[:16]
        t = str(to_lang or "")[:16]
        s = str(sid or "")[:24]
        ts = time.time() if now is None else float(now)
        back = False
        with self._lock:
            self._switches_to[t] = self._switches_to.get(t, 0) + 1
            if s:
                prev = self._last_switch.get(s)
                if prev and prev[1] == f and prev[0] == t and ts - prev[2] <= self.SWITCH_BACK_WINDOW_S:
                    self._switch_back_from[f] = self._switch_back_from.get(f, 0) + 1
                    back = True
                if s not in self._last_switch and len(self._last_switch) >= self._MAX_SWITCH_SESSIONS:
                    oldest = min(self._last_switch, key=lambda k: self._last_switch[k][2])
                    self._last_switch.pop(oldest, None)
                self._last_switch[s] = (f, t, ts)
        return back

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "total": self.total,
                "by_source": dict(sorted(self._by_source.items())),
                "by_lang": dict(sorted(
                    self._by_lang.items(), key=lambda kv: (-kv[1], kv[0]))),
                "negotiated_by_lang": dict(sorted(
                    self._negotiated_by_lang.items(), key=lambda kv: (-kv[1], kv[0]))),
                "unsupported_by_lang": dict(sorted(
                    self._unsupported_by_lang.items(), key=lambda kv: (-kv[1], kv[0]))),
                "query_cookie_conflicts_by_query_lang": dict(sorted(
                    self._conflicts_by_query_lang.items(), key=lambda kv: (-kv[1], kv[0]))),
                "switches_to": dict(sorted(
                    self._switches_to.items(), key=lambda kv: (-kv[1], kv[0]))),
                "switch_back_within_5m_from": dict(sorted(
                    self._switch_back_from.items(), key=lambda kv: (-kv[1], kv[0]))),
            }

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self._by_source.clear()
            self._by_lang.clear()
            self._negotiated_by_lang.clear()
            self._unsupported_by_lang.clear()
            self._conflicts_by_query_lang.clear()
            self._switches_to.clear()
            self._switch_back_from.clear()
            self._last_switch.clear()


_SINGLETON: Optional[UiLangStats] = None
_LOCK = threading.Lock()


def get_ui_lang_stats() -> UiLangStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = UiLangStats()
    return _SINGLETON


__all__ = ["UiLangStats", "get_ui_lang_stats"]
