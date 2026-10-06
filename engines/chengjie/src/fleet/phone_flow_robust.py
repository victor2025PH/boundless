"""社交动作的核对与节奏。默认都关着：不截图复查、步骤间也不加随机间隔。

打开后：
* 某一步标了 expect，做完再截一张图。画面没变、颜色对不上，就停在这一步，有限次重试。
* 点开应用的那一下可以要求「已登录」的颜色在、登录页的颜色不在。
* jitter 加在每步原有的最短间隔之上，不缩短它，也不放开手机锁。
"""

from __future__ import annotations

import struct
import zlib
from typing import Any, Callable, Optional, Sequence, Tuple

MAX_RETRIES = 3
DEFAULT_RETRIES = 2
MAX_JITTER_MS = 2000
MAX_HUMAN_GAP_SEC = 2.0

_RAW_ORDERS = {1: "rgba", 2: "rgba", 5: "bgra"}


def parse_jitter_ms(value: Any) -> Optional[Tuple[int, int]]:
    """agent.json 里的一对毫秒。形状不对 → None（沿用坐标文件）。上限收进 0..MAX_JITTER_MS。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lo, hi = value
    if isinstance(lo, bool) or isinstance(hi, bool) or not isinstance(lo, int) or not isinstance(hi, int):
        return None
    if lo < 0 or hi < 0 or lo > hi:
        return None
    hi = min(hi, MAX_JITTER_MS)
    lo = min(lo, hi)
    return (lo, hi)


def jitter_seconds(span: Sequence[int], rng: Callable[[], float]) -> float:
    """[lo, hi] 毫秒 → 秒。两端都是 0 时不调用 rng。"""
    lo, hi = int(span[0]), int(span[1])
    if hi > MAX_JITTER_MS:
        hi = MAX_JITTER_MS
    if lo < 0:
        lo = 0
    if lo > hi:
        lo = hi
    if lo == 0 and hi == 0:
        return 0.0
    if lo == hi:
        return lo / 1000.0
    u = float(rng())
    if u < 0.0:
        u = 0.0
    elif u > 1.0:
        u = 1.0
    return (lo + (hi - lo) * u) / 1000.0


def resolve_policy(ui: Any, verify_override: Any, jitter_override: Any) -> Tuple[bool, Tuple[int, int], int]:
    """(是否核对, 抖动毫秒, 额外重试次数)。override 为 None 时用坐标文件。"""
    robust = ui.get("robust") if isinstance(ui, dict) else None
    if not isinstance(robust, dict):
        robust = {}
    verify = robust.get("verify") is True if verify_override is None else verify_override is True
    if jitter_override is None:
        raw = robust.get("jitter_ms") or (0, 0)
        jitter = (int(raw[0]), int(raw[1]))
    else:
        jitter = (int(jitter_override[0]), int(jitter_override[1]))
    retries = robust.get("retries", DEFAULT_RETRIES)
    if isinstance(retries, bool) or not isinstance(retries, int):
        retries = DEFAULT_RETRIES
    if retries < 0:
        retries = 0
    if retries > MAX_RETRIES:
        retries = MAX_RETRIES
    return verify, jitter, retries


def frame_token(raw: bytes) -> int:
    return zlib.crc32(raw) & 0xFFFFFFFF


def _sample(raw: bytes, x: int, y: int) -> Tuple[int, int, int]:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 12:
        raise ValueError("bad frame")
    w, h, fmt = struct.unpack_from("<III", raw, 0)
    if not (0 < w <= 8192 and 0 < h <= 8192):
        raise ValueError("bad frame")
    hdr = len(raw) - w * h * 4
    order = _RAW_ORDERS.get(fmt)
    if hdr not in (12, 16) or order is None or not (0 <= x < w and 0 <= y < h):
        raise ValueError("bad frame")
    px = raw[hdr + (y * w + x) * 4: hdr + (y * w + x) * 4 + 4]
    if len(px) < 3:
        raise ValueError("bad frame")
    if order == "bgra":
        return px[2], px[1], px[0]
    return px[0], px[1], px[2]


def probe_matches(raw: bytes, x: int, y: int, rgb: Sequence[int], tol: int) -> bool:
    """锚点上的像素是否落在容差内。帧读不出来 → 不匹配（宁可停）。"""
    try:
        got = _sample(raw, int(x), int(y))
    except (ValueError, struct.error, IndexError):
        return False
    return all(abs(int(a) - int(b)) <= int(tol) for a, b in zip(got, rgb))


__all__ = [
    "DEFAULT_RETRIES", "MAX_HUMAN_GAP_SEC", "MAX_JITTER_MS", "MAX_RETRIES",
    "frame_token", "jitter_seconds", "parse_jitter_ms", "probe_matches", "resolve_policy",
]
