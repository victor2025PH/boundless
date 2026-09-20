"""启动阶段计时（Phase 11：冷启动可观测）。

`initialize()` 是「进程起来 → /login 可服务」这段的全部成本所在（web 线程在
setup_web_app 末尾才起）。这段曾观测到 2-3.5 分钟，seat 端反复撞「加载超时」。
但此前**无法归因**到底慢在哪一步（ai_client / skill_manager / telegram / KB…）。

本模块是一个零副作用的纯计时器：`initialize()` 在每个重型阶段边界调用 `mark()`，
结束时 `summary()` 出一份「每阶段耗时 + 累计 + 最慢阶段」，落 app.log 一行并入
metrics_store，供下次自然重启即读真实数字、据此安全地做启动顺序重排（Phase 12）。

刻意保持纯函数 + 无 IO + 任何异常都不得冒泡（调用侧另有 try/except 兜底）：这段
代码活在生产启动关键路径上，绝不允许因观测而拖慢或拖垮启动。
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Tuple


class BootTimer:
    """单调时钟的分阶段计时器。

    用法::

        t = BootTimer()
        ... 加载配置 ...
        t.mark("config")
        ... 初始化 ai_client ...
        t.mark("ai_client")
        log.info(t.format_line())
        metrics.set_boot_timing(t.summary())

    - `mark(name)` 记录「自上次 mark（或起点）以来的增量」与「自起点的累计」，返回增量秒。
    - 时钟默认 `time.monotonic`（不受系统时间回拨影响）；测试可注入假时钟。
    - 无锁：`initialize()` 单线程顺序调用，不存在并发 mark。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._start = clock()
        self._last = self._start
        # (name, delta_sec, cumulative_sec)
        self._phases: List[Tuple[str, float, float]] = []

    def mark(self, name: str) -> float:
        """记录一个阶段边界，返回该阶段耗时（秒）。异常安全：坏时钟不抛。"""
        try:
            now = self._clock()
            delta = max(0.0, now - self._last)
            cum = max(0.0, now - self._start)
            self._phases.append((str(name), delta, cum))
            self._last = now
            return delta
        except Exception:
            return 0.0

    @property
    def total(self) -> float:
        """自起点至今的累计秒数。"""
        try:
            return max(0.0, self._clock() - self._start)
        except Exception:
            return 0.0

    def phases(self) -> List[Tuple[str, float, float]]:
        return list(self._phases)

    def slowest(self) -> Tuple[str, float]:
        """最慢阶段 (name, delta_sec)；无阶段时返回 ("", 0.0)。"""
        if not self._phases:
            return ("", 0.0)
        name, delta, _ = max(self._phases, key=lambda p: p[1])
        return (name, round(delta, 3))

    def summary(self) -> Dict:
        """结构化摘要，供 metrics_store / 状态接口消费。"""
        total = self.total
        slow_name, slow_delta = self.slowest()
        return {
            "total_sec": round(total, 3),
            "phases": [
                {
                    "name": n,
                    "delta_sec": round(d, 3),
                    "cumulative_sec": round(c, 3),
                }
                for (n, d, c) in self._phases
            ],
            "slowest_phase": slow_name,
            "slowest_sec": slow_delta,
        }

    def format_line(self) -> str:
        """单行日志：``boot phases: config=0.1s ai_client=2.3s ... total=12.4s (slowest=skill_manager 8.1s)``。"""
        parts = [f"{n}={d:.1f}s" for (n, d, _c) in self._phases]
        slow_name, slow_delta = self.slowest()
        tail = f" total={self.total:.1f}s"
        if slow_name:
            tail += f" (slowest={slow_name} {slow_delta:.1f}s)"
        return "boot phases: " + " ".join(parts) + tail
