# -*- coding: utf-8 -*-
"""告警卡公网入口可达性（运维群降噪 P2.2，2026-09-10）。

背景：运维群卡片里的链接都拼在渠道 ``base_url``（``https://katie.bd2026.cc``）上，这条链是
DNS → VPS nginx → SSH 反向隧道 → 本机 18799。隧道一断（09-09 实录：实例重启后隧道没回来），
卡片照发、链接全是 502——值班的人点了三张才明白不是自己的问题。**告警本身**归
``deploy/instances/prod_edge_watchdog.ps1``（两击、自愈重启隧道、Telegram 通知），本模块不重复报，
只做两件事：

1. 巡检每轮探一次 ``<base_url>/ops/glance``（不带令牌，403/410 也算「通」——只看链路，不看鉴权；
   5xx = nginx 通、后面断了，按不通算），两击判断，进程内记状态；
2. ``webhook_notifier`` 铸链接前问一句 :func:`is_down`——断了就把链接换成内网地址并在卡上说明，
   点开至少不是 502。恢复后自动换回。

进程内状态，重启即空（重启后第一轮就重新探）。``lan_base()`` 由 web_admin 端口 + 本机 LAN IP 推导。
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PROBE_PATH = "/ops/glance"
DEFAULT_TIMEOUT = 6.0
DEFAULT_STRIKES = 2

_lock = threading.Lock()
_state: Dict[str, Dict[str, Any]] = {}     # base_url -> {down, since, strikes, last_probe, detail}
_lan_base_override: str = ""


def _norm(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def probe(base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> Tuple[bool, str]:
    """探公网入口：拿到任何 <500 的 HTTP 应答即「通」；5xx / 连接失败 / 超时 = 「不通」。"""
    base = _norm(base_url)
    if not base.startswith("http"):
        return False, "no base_url"
    req = urllib.request.Request(base + PROBE_PATH, method="GET",
                                 headers={"User-Agent": "chengjie-public-link-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        code = int(e.code)
    except Exception as exc:  # noqa: BLE001 — URLError / timeout / ssl
        return False, type(exc).__name__ + ": " + str(getattr(exc, "reason", exc))[:80]
    if code >= 500:
        return False, f"HTTP {code}"
    return True, f"HTTP {code}"


def record(base_url: str, ok: bool, detail: str = "", *, now: Optional[float] = None,
           strikes: int = DEFAULT_STRIKES) -> Optional[bool]:
    """登记一次探测结果。返回 ``True``=刚判为断、``False``=刚恢复、``None``=状态没变。"""
    base = _norm(base_url)
    ts = float(now if now is not None else time.time())
    with _lock:
        st = _state.setdefault(base, {"down": False, "since": 0.0, "strikes": 0,
                                      "last_probe": 0.0, "detail": ""})
        st["last_probe"] = ts
        st["detail"] = str(detail or "")
        if ok:
            st["strikes"] = 0
            if st["down"]:
                st["down"] = False
                st["since"] = 0.0
                return False
            return None
        st["strikes"] = int(st["strikes"]) + 1
        if not st["down"] and st["strikes"] >= max(1, int(strikes)):
            st["down"] = True
            st["since"] = ts
            return True
        return None


def is_down(base_url: str = "") -> bool:
    """``base_url`` 为空 → 任一已登记入口断了都算断（notifier 只配一个入口的常见情形）。"""
    base = _norm(base_url)
    with _lock:
        if base:
            return bool((_state.get(base) or {}).get("down"))
        return any(bool(st.get("down")) for st in _state.values())


def snapshot() -> Dict[str, Dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in _state.items()}


def set_lan_base(url: str) -> None:
    """显式指定内网地址（配置 ``web_admin.lan_base_url`` 或测试注入）；空串恢复自动推导。"""
    global _lan_base_override
    _lan_base_override = _norm(url)


def lan_base(port: int = 0) -> str:
    """内网地址 ``http://<本机 LAN IP>:<port>``；推不出 LAN IP 给空串（调用方据此不换链接）。"""
    if _lan_base_override:
        return _lan_base_override
    if not port:
        return ""
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))     # 不真发包，只让内核选出网卡
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        ip = ""
    if not ip or ip.startswith("127."):
        return ""
    return f"http://{ip}:{int(port)}"


def _reset_for_tests() -> None:
    global _lan_base_override
    with _lock:
        _state.clear()
    _lan_base_override = ""


__all__ = ["probe", "record", "is_down", "snapshot", "lan_base", "set_lan_base",
           "PROBE_PATH", "DEFAULT_STRIKES"]
