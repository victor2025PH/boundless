"""客户端错误回传（beacon）——公网安装版的远程可观测（2026-07-29）。

桌面版装在天南海北的用户机器上，出错时厂商完全失明（此前只能靠用户截图）。
本模块把 **ERROR 级日志摘要**回传官网 ``POST {site}/api/client-log``，让
「谁的机器、哪个版本、哪个组件、什么错、错几次」在服务端可见。

隐私红线（宁可少送不多送）：
- 只送 logger 名 / 级别 / **消毒后**的消息前 300 字符 / 版本号 / 机器指纹；
- 消毒：``C:\\Users\\<名>`` → ``~``；``sk-…`` / ``cx.…`` 形态的密钥令牌打码；
- 绝不送聊天内容字段、配置全文、堆栈本地变量。

防噪防刷：
- 仅桌面模式激活（``AITR_DESKTOP_MODE``；或 ``telemetry.client_errors.enabled``
  显式 true）；server 部署天然不激活；显式 false 一键关；
- 进程级：每小时 ≤ ``MAX_PER_HOUR`` 条；同 (logger, 消息前 80 字) 在窗口内去重
  计数（重复错误只加 n 不加行）；
- 后台 daemon 线程 30s 批量刷（每请求 ≤10 条），网络失败**丢弃不重试**——
  回传是旁路，绝不能变成第二个故障源。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

DEFAULT_SITE = "https://bd2026.cc"
HTTP_TIMEOUT = 8
FLUSH_INTERVAL_SEC = 30
MAX_PER_HOUR = 30
MAX_BATCH = 10
MSG_MAX = 300
DEDUP_WINDOW_SEC = 300.0

_USER_PATH_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE)
_SECRET_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{6,}|cx\.[A-Za-z0-9_\-\.]{10,})")

_installed_lock = threading.Lock()
_installed: Optional["_Beacon"] = None

logger = logging.getLogger(__name__)


def sanitize_message(msg: str) -> str:
    s = str(msg or "")[:MSG_MAX * 2]
    s = _USER_PATH_RE.sub(r"~", s)
    s = _SECRET_RE.sub("***", s)
    return s[:MSG_MAX]


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def beacon_enabled(config: Optional[dict]) -> bool:
    """默认：桌面模式开、server 部署关；``telemetry.client_errors.enabled`` 显式值最优先。"""
    tel = ((config or {}).get("telemetry") or {}).get("client_errors") or {}
    if "enabled" in tel:
        return bool(tel.get("enabled"))
    return _env_truthy("AITR_DESKTOP_MODE")


def _site_url(config: Optional[dict]) -> str:
    tel = ((config or {}).get("telemetry") or {}).get("client_errors") or {}
    trial = ((config or {}).get("licensing") or {}).get("trial") or {}
    return str(tel.get("site_url") or trial.get("site_url") or DEFAULT_SITE).rstrip("/")


class _Beacon(logging.Handler):
    """logging.Handler：捕 ERROR+ → 去重节流 → 后台批量回传。"""

    def __init__(self, config: Optional[dict],
                 post: Optional[Callable[[str, dict], bool]] = None):
        super().__init__(level=logging.ERROR)
        self._config = config or {}
        self._post = post or self._http_post
        self._lock = threading.Lock()
        # key=(logger, msg[:80]) → {"first": ts, "n": int, "ev": event-dict}
        self._pending: Dict[tuple, Dict[str, Any]] = {}
        self._hour_start = time.time()
        self._hour_sent = 0
        self._fp = ""
        self._ver = str(os.environ.get("AITR_APP_VERSION") or "")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── logging.Handler ──
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            if record.levelno < logging.ERROR:
                return  # 双保险：logging 框架级过滤之外，直接 handle() 也拦
            if record.name.startswith("telemetry_beacon") or record.name == __name__:
                return  # 自己的日志绝不回传（防递归）
            msg = sanitize_message(record.getMessage())
            if record.exc_info and record.exc_info[0] is not None:
                msg = f"[{record.exc_info[0].__name__}] {msg}"[:MSG_MAX]
            key = (record.name, msg[:80])
            now = time.time()
            with self._lock:
                hit = self._pending.get(key)
                if hit and now - hit["first"] < DEDUP_WINDOW_SEC:
                    hit["n"] += 1
                    return
                self._pending[key] = {
                    "first": now,
                    "n": 1,
                    "ev": {
                        "ts": int(now),
                        "logger": record.name[:80],
                        "level": record.levelname,
                        "msg": msg,
                    },
                }
                if len(self._pending) > 100:  # 极端刷错兜底
                    self._pending.pop(next(iter(self._pending)))
        except Exception:
            pass  # 回传是旁路，任何异常吞掉

    # ── 生命周期 ──
    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            from src.licensing.machine_bridge import machine_fingerprint
            self._fp = machine_fingerprint() or ""
        except Exception:
            self._fp = ""
        self.note_event("beacon", "INFO", f"boot version={self._ver or 'dev'}")
        self._thread = threading.Thread(
            target=self._loop, name="telemetry-beacon", daemon=True)
        self._thread.start()

    def note_event(self, logger_name: str, level: str, msg: str) -> None:
        """非日志路径的手工事件（boot 等）。"""
        with self._lock:
            key = (logger_name, msg[:80])
            self._pending[key] = {
                "first": time.time(), "n": 1,
                "ev": {"ts": int(time.time()), "logger": logger_name,
                       "level": level, "msg": sanitize_message(msg)},
            }

    def _loop(self) -> None:
        while not self._stop.wait(FLUSH_INTERVAL_SEC):
            try:
                self.flush_pending()
            except Exception:
                pass

    # ── 发送 ──
    def flush_pending(self) -> int:
        """把待发事件批量送出（同键带 n）。返回本轮真正送出的事件数。"""
        now = time.time()
        with self._lock:
            if now - self._hour_start > 3600:
                self._hour_start = now
                self._hour_sent = 0
            budget = max(0, MAX_PER_HOUR - self._hour_sent)
            if budget <= 0 or not self._pending:
                # 超预算：清掉积压（丢弃优于积爆内存）
                if budget <= 0:
                    self._pending.clear()
                return 0
            keys = list(self._pending.keys())[:min(MAX_BATCH, budget)]
            events: List[Dict[str, Any]] = []
            for k in keys:
                item = self._pending.pop(k)
                ev = dict(item["ev"])
                if item["n"] > 1:
                    ev["n"] = item["n"]
                events.append(ev)
            self._hour_sent += len(events)
        payload = {"fingerprint": self._fp, "version": self._ver, "events": events}
        ok = False
        try:
            ok = bool(self._post(f"{_site_url(self._config)}/api/client-log", payload))
        except Exception:
            ok = False
        return len(events) if ok else 0

    @staticmethod
    def _http_post(url: str, body: dict) -> bool:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False


def install_beacon(config: Optional[dict],
                   post: Optional[Callable[[str, dict], bool]] = None) -> Optional[_Beacon]:
    """挂到 root logger（幂等）。非桌面/显式关闭返回 None。"""
    global _installed
    if not beacon_enabled(config):
        return None
    with _installed_lock:
        if _installed is not None:
            return _installed
        b = _Beacon(config, post=post)
        logging.getLogger().addHandler(b)
        b.start()
        _installed = b
        logger.info("客户端错误回传已启用（ERROR 级摘要 → 官网；telemetry.client_errors.enabled: false 可关）")
        return b


def reset_for_tests() -> None:
    global _installed
    with _installed_lock:
        if _installed is not None:
            try:
                logging.getLogger().removeHandler(_installed)
            except Exception:
                pass
        _installed = None
