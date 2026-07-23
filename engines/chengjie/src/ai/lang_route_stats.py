"""出站语音「语言路由」观测（进程级单例）。

lang_voice_route（粤语路由 + follow_text 音色跟随文本语种 + 拒发守卫）此前只有
日志——「路由了多少条、哪些语种在被路由、拒发了多少（= 哪些语种该配音色）」全靠
翻 log。本模块把路由决策变成可观测计数：

  - checks         路由启用下的决策总次数（分母）
  - routed[tag]    音色被改写的次数，按 tag 分桶（yue / en / ja …）
  - rejected[lang] 拒发守卫命中（语种明确但无音色映射 → 回落文字）——持续增长
                   = 该语种客户在收「本该是语音」的文字，照单补 follow_text.voices
  - fallback_aligned 克隆链只对齐 edge 兜底音色的次数（主后端未变）

记录点＝route_voice_cfg_for_text 单一出口（三条出站语音链全覆盖，零漂移）。
读出：/api/workspace/metrics.lang_voice_route + Prometheus lang_voice_route_* +
rpa-overview「会话语言质量」卡。风格对齐 frontend_error_stats：线程安全、
distinct key 上限防撑爆、绝不抛异常。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_MAX_KEYS = 40  # 语种桶上限（语种是小集合，40 足够；防脏 tag 刷爆）


def _san_lang(tag: str) -> str:
    t = str(tag or "").strip().lower()[:16]
    return t if t and t.replace("-", "").isalnum() else "unknown"


class LangRouteStats:
    """语音语言路由计数（线程安全，进程级）。"""

    __slots__ = ("_lock", "_started_at", "_last_ts",
                 "checks", "fallback_aligned", "_routed", "_rejected")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._last_ts = 0.0
        self.checks = 0
        self.fallback_aligned = 0
        self._routed: Dict[str, int] = {}
        self._rejected: Dict[str, int] = {}

    @staticmethod
    def _bump(d: Dict[str, int], key: str) -> None:
        if key in d or len(d) < _MAX_KEYS:
            d[key] = d.get(key, 0) + 1
        else:
            d["__other__"] = d.get("__other__", 0) + 1

    def record_check(self) -> None:
        with self._lock:
            self.checks += 1
            self._last_ts = time.time()

    def record_routed(self, tag: str) -> None:
        with self._lock:
            self._bump(self._routed, _san_lang(tag))
            self._last_ts = time.time()

    def record_rejected(self, lang: str) -> None:
        with self._lock:
            self._bump(self._rejected, _san_lang(lang))
            self._last_ts = time.time()

    def record_fallback_aligned(self) -> None:
        with self._lock:
            self.fallback_aligned += 1
            self._last_ts = time.time()

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "checks": self.checks,
                "routed_total": sum(self._routed.values()),
                "rejected_total": sum(self._rejected.values()),
                "fallback_aligned": self.fallback_aligned,
                "routed": dict(sorted(
                    self._routed.items(), key=lambda kv: (-kv[1], kv[0]))),
                "rejected": dict(sorted(
                    self._rejected.items(), key=lambda kv: (-kv[1], kv[0]))),
            }

    def dump_prom(self) -> str:
        with self._lock:
            lines = [
                "# HELP lang_voice_route_checks_total Voice language-route decisions (denominator)",
                "# TYPE lang_voice_route_checks_total counter",
                f"lang_voice_route_checks_total {self.checks}",
                "# HELP lang_voice_route_routed_total Voice switched by text language, by route tag",
                "# TYPE lang_voice_route_routed_total counter",
            ]
            for k, n in sorted(self._routed.items()):
                lines.append(f'lang_voice_route_routed_total{{lang="{_esc(k)}"}} {int(n)}')
            lines += [
                "# HELP lang_voice_route_rejected_total Voice sends rejected (no voice for language)",
                "# TYPE lang_voice_route_rejected_total counter",
            ]
            for k, n in sorted(self._rejected.items()):
                lines.append(f'lang_voice_route_rejected_total{{lang="{_esc(k)}"}} {int(n)}')
            lines += [
                "# HELP lang_voice_route_fallback_aligned_total Clone-chain edge fallback voice aligned",
                "# TYPE lang_voice_route_fallback_aligned_total counter",
                f"lang_voice_route_fallback_aligned_total {self.fallback_aligned}",
            ]
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self.checks = 0
            self.fallback_aligned = 0
            self._routed.clear()
            self._rejected.clear()
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[LangRouteStats] = None
_LOCK = threading.Lock()


def get_lang_route_stats() -> LangRouteStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = LangRouteStats()
    return _SINGLETON


__all__ = ["LangRouteStats", "get_lang_route_stats"]
