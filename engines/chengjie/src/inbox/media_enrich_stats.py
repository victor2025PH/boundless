"""入站媒体识别的「没识别成」计数（进程级单例，零依赖、绝不抛）。

存在理由（2026-07-31 实锤）：识别链的失败是**彻底静默**的——``media_enrich`` 拿不到
描述就回落 ``[图片]`` / ``[语音]`` 占位，AI 照常回复，日志里只有 debug。于是三种完全
不同的处境在运营眼里长得一模一样：

  ① 开关没开（客户在发图，你还没开识图）——这是**销售/运营信号**，不是故障；
  ② 开了但后端没接上（``no_backend``，托管识图供给缺席那种）——**该修**，且能自助修；
  ③ 后端在但识别失败/返空（``failed``，网关不通 / 模型出错 / 令牌过期）——该看日志。

自检卡此前只能回答「配置长得像不像能用」，回答不了「**最近真有多少条媒体没被看懂**」。
本模块补上这个计数：进程内累计，按 kind × reason 分桶，随 ``media_runtime_signals``
出到自检接口与看板。刻意**不记任何内容**（只记类型与原因码），无隐私面。

进程级即可：它服务的是「现在这台在跑的实例健不健康」，重启清零是正确语义（重启后
供给会重新注入，旧账没有参考价值）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict

#: 分桶上限（kind/reason 都是闭集，这里只防未来新增类型把字典撑爆）
_MAX_KEYS = 32


class MediaEnrichStats:
    """入站媒体识别成功/未成功的计数器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._understood = 0
        self._missed = 0
        self._by_reason: Dict[str, int] = {}
        self._by_kind: Dict[str, int] = {}
        self._last_miss_ts = 0.0
        self._last_miss_reason = ""

    def record_understood(self, kind: str) -> None:
        with self._lock:
            self._understood += 1

    def record_miss(self, kind: str, reason: str) -> None:
        k = str(kind or "?")[:24]
        r = str(reason or "unknown")[:24]
        with self._lock:
            self._missed += 1
            if r in self._by_reason or len(self._by_reason) < _MAX_KEYS:
                self._by_reason[r] = self._by_reason.get(r, 0) + 1
            if k in self._by_kind or len(self._by_kind) < _MAX_KEYS:
                self._by_kind[k] = self._by_kind.get(k, 0) + 1
            self._last_miss_ts = time.time()
            self._last_miss_reason = r

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            total = self._understood + self._missed
            return {
                "attempts": total,
                "understood": self._understood,
                "missed": self._missed,
                "miss_rate": round(self._missed / total, 4) if total else 0.0,
                "by_reason": dict(self._by_reason),
                "by_kind": dict(self._by_kind),
                "last_miss_ts": round(self._last_miss_ts, 3) or 0.0,
                "last_miss_reason": self._last_miss_reason,
            }

    def reset(self) -> None:
        """仅供测试：进程级单例在用例间要能清零。"""
        with self._lock:
            self._understood = 0
            self._missed = 0
            self._by_reason.clear()
            self._by_kind.clear()
            self._last_miss_ts = 0.0
            self._last_miss_reason = ""


_STATS = MediaEnrichStats()


def get_media_enrich_stats() -> MediaEnrichStats:
    return _STATS


__all__ = ["MediaEnrichStats", "get_media_enrich_stats"]
