"""Telegram 直连可达性预检（2026-08-10 事故链 P1-⑥）。

作用：接入弹窗打开时先探「本机能不能直连 Telegram DC」，探不通提前亮黄条给
「配代理」指引——把大陆用户的失败体验从「扫码失败后猜原因」变成「进门就有路标」。

设计：
- 只做 TCP connect 快败（2s），**不发任何 MTProto 数据**——判断的是网络通不通，
  不是凭据对不对（那是 tg_cred_probe / 登录本身的事）；
- 三个 DC 锚点并行探，任一通即算可达（DC IP 是 Telegram 公开常量，多年稳定；
  个别 IP 变更也有其余锚点兜底）；
- 60s TTL 进程缓存：弹窗反复开合不重探；``force=True`` 强刷；
- **探测结论只用于提示，绝不阻断登录**——用户配了代理时直连不通是预期态，
  banner 文案自己说明这一点。

纯函数核心 + 可注入 connector，零网络单测。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

#: Telegram 生产 DC 锚点（DC1/DC2/DC4 各取一）——只探 TCP 443 连通性
DC_PROBES: Tuple[Tuple[str, int], ...] = (
    ("149.154.175.53", 443),   # DC1
    ("149.154.167.51", 443),   # DC2
    ("149.154.167.91", 443),   # DC4
)
PROBE_TIMEOUT_SEC = 2.0
CACHE_TTL_SEC = 60.0

Connector = Callable[[str, int, float], Awaitable[bool]]

_cache: Dict[str, Any] = {"ts": 0.0, "result": None}


async def _tcp_connect_ok(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


async def probe_telegram_reachable(
    *,
    force: bool = False,
    timeout: float = PROBE_TIMEOUT_SEC,
    probes: Optional[Iterable[Tuple[str, int]]] = None,
    connector: Optional[Connector] = None,
    ttl: float = CACHE_TTL_SEC,
) -> Dict[str, Any]:
    """探测直连可达性。返回 ``{reachable, latency_ms, checked_at, cached}``。"""
    now = time.time()
    if (not force and _cache["result"] is not None
            and now - float(_cache["ts"] or 0) < ttl):
        return {**_cache["result"], "cached": True}

    conn = connector or _tcp_connect_ok
    targets = list(probes or DC_PROBES)
    t0 = time.time()

    async def _one(host: str, port: int) -> bool:
        try:
            return await conn(host, port, timeout)
        except Exception:  # noqa: BLE001
            return False

    reachable = False
    latency_ms: Optional[int] = None
    if targets:
        tasks = [asyncio.ensure_future(_one(h, p)) for h, p in targets]
        try:
            # 任一成功即可收工（剩余任务取消）——可达路径的延迟≈最快锚点 RTT
            for fut in asyncio.as_completed(tasks, timeout=timeout + 0.5):
                try:
                    if await fut:
                        reachable = True
                        latency_ms = int((time.time() - t0) * 1000)
                        break
                except Exception:  # noqa: BLE001
                    continue
        except (asyncio.TimeoutError, TimeoutError):
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    result = {
        "reachable": reachable,
        "latency_ms": latency_ms,
        "checked_at": int(now),
    }
    _cache["ts"] = now
    _cache["result"] = dict(result)
    return {**result, "cached": False}


def reset_cache_for_tests() -> None:
    _cache["ts"] = 0.0
    _cache["result"] = None
