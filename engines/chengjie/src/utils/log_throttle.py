"""第三方噪声 logger 折叠过滤器（2026-07-23 降噪）。

背景：root 钉 WARNING 只挡住了 INFO/DEBUG 级三方刷屏，但 pyrogram 的
``pyrogram.connection.connection`` 在**掉线窗口**里以 WARNING 级狂刷
「Unable to connect due to network issues」/「Connection failed! Trying again...」——
一次断网就往 app.log 灌 400+ 行同类记录，把真信号淹没（本仓 07-22 实录 454+146 行）。

``ThrottleFilter`` 对**指定前缀**的 logger 做「每窗口每模板至多放行 1 条」的折叠：
窗口内后续同类记录直接丢弃并计数，下次放行时在消息尾部追加「[+N 条同类已折叠]」，
既保留「故障仍在持续」的可见性，又把刷屏压成每分钟 1 行。**只作用于配置的三方前缀，
本仓 ``src.*`` / ``ai_chat_assistant`` 一律不折叠**（业务日志一条不少）。

纯逻辑（time 可注入）便于单测；挂载见 ``bootstrap/logging_setup.py``。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, Iterable, Optional, Tuple

# 默认折叠的三方噪声 logger 前缀（掉线/重试类刷屏）
DEFAULT_THROTTLE_PREFIXES = (
    "pyrogram.connection",
    "pyrogram.session",
)

_NUM_RE = re.compile(r"\d+")


def _template(msg: str) -> str:
    """把消息里的数字抹成占位，让 cycle=125/999 之类折叠到同一模板。"""
    return _NUM_RE.sub("#", msg)


class ThrottleFilter(logging.Filter):
    """按 (logger, 消息模板) 在时间窗内折叠重复记录。

    - ``prefixes``：命中任一前缀的 logger 才折叠，其余全部放行；
    - ``window_sec``：同一模板的放行间隔（窗口）；
    - 放行时若期间有折叠，尾部追加「[+N 条同类已折叠]」并清零计数。
    """

    def __init__(
        self,
        prefixes: Iterable[str] = DEFAULT_THROTTLE_PREFIXES,
        window_sec: float = 60.0,
        time_fn: Optional[Callable[[], float]] = None,
        note_fmt: str = " [+{n} 条同类已折叠/{w:.0f}s]",
    ) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)
        self._window = float(window_sec)
        self._time = time_fn or time.monotonic
        self._note_fmt = note_fmt
        # key -> (last_emit_ts, suppressed_since_emit)
        self._state: Dict[Tuple[str, str], Tuple[float, int]] = {}

    def _matches(self, name: str) -> bool:
        return any(name == p or name.startswith(p + ".") or name.startswith(p)
                   for p in self._prefixes)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not self._matches(record.name):
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True  # 格式化异常不拦截，交给下游
        key = (record.name, _template(msg))
        now = self._time()
        last_emit, suppressed = self._state.get(key, (None, 0))

        if last_emit is not None and (now - last_emit) < self._window:
            # 窗口内：折叠丢弃
            self._state[key] = (last_emit, suppressed + 1)
            return False

        # 放行：若期间有折叠，把折叠数追加进本条消息
        if suppressed > 0:
            note = self._note_fmt.format(n=suppressed, w=self._window)
            record.msg = msg + note
            record.args = ()
        self._state[key] = (now, 0)
        return True


def build_throttle_filter(log_config: Optional[dict]) -> Optional[ThrottleFilter]:
    """从 config.logging.throttle 造过滤器；未配置时用默认前缀+60s 窗口。

    throttle: {enabled: bool=True, prefixes: [..], window_sec: 60}
    返回 None 表示显式关闭（enabled=false）。
    """
    cfg = (log_config or {}).get("throttle") or {}
    if cfg.get("enabled") is False:
        return None
    prefixes = cfg.get("prefixes") or list(DEFAULT_THROTTLE_PREFIXES)
    window = cfg.get("window_sec", 60)
    try:
        window = float(window)
    except (TypeError, ValueError):
        window = 60.0
    return ThrottleFilter(prefixes=prefixes, window_sec=window)
