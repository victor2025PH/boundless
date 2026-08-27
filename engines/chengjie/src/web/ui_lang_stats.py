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
                 "_negotiated_by_lang", "_unsupported_by_lang")

    _MAX_UNSUPPORTED_KEYS = 30  # 防 UA 乱报语言撑爆内存；超限归 __other__

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
            }

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self._by_source.clear()
            self._by_lang.clear()
            self._negotiated_by_lang.clear()
            self._unsupported_by_lang.clear()


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
