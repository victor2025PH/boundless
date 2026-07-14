"""
VisionFallback — Screenshot-based UI understanding when XML dump fails.

When uiautomator2 XML dump fails to find an element (SDUI, dynamic content,
overlays), this module takes a screenshot → sends to multimodal LLM →
gets coordinates or action guidance.

Key features:
- Cost budget: max N vision calls per hour (vision APIs are expensive)
- Result caching: same screenshot context → reuse coordinates
- Graceful degradation: budget exhausted → return None, let caller retry
- Provider agnostic: uses LLMClient's vision endpoint
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .llm_client import LLMClient, get_llm_client

log = logging.getLogger(__name__)


@dataclass
class VisionConfig:
    hourly_budget: int = 20
    cache_ttl_sec: float = 300.0
    max_retries: int = 2


@dataclass
class VisionResult:
    coordinates: Optional[Tuple[int, int]] = None
    description: str = ""
    confidence: str = "low"
    raw_response: str = ""


class VisionFallback:
    """
    Screenshot → LLM → coordinates.

    Usage:
        vf = VisionFallback()
        result = vf.find_element(
            device=d,
            target="the Send button",
            context="in a Telegram chat with message input visible",
        )
        if result and result.coordinates:
            d.click(*result.coordinates)
    """

    def __init__(self, client: Optional[LLMClient] = None,
                 config: Optional[VisionConfig] = None):
        self._client = client or get_llm_client()
        self.config = config or VisionConfig()
        self._call_timestamps: List[float] = []
        self._cache: Dict[str, Tuple[VisionResult, float]] = {}
        self._lock = threading.Lock()

    def find_element(self, device, target: str, context: str = "",
                     screenshot_bytes: Optional[bytes] = None) -> Optional[VisionResult]:
        """
        Find a UI element by description using screenshot + LLM vision.

        Args:
            device: u2 device (for screenshot if not provided)
            target: what to find ("the Send button", "search input field")
            context: additional context ("in LinkedIn messaging screen")
            screenshot_bytes: pre-captured PNG bytes (optional)

        Returns VisionResult with coordinates, or None if budget exhausted.
        """
        if not self._check_budget():
            log.warning("VisionFallback budget exhausted (%d/%d per hour)",
                        self._hourly_count(), self.config.hourly_budget)
            return None

        cache_key = self._cache_key(target, context)
        cached = self._get_cache(cache_key)
        if cached:
            log.debug("VisionFallback cache hit for '%s'", target)
            return cached

        if screenshot_bytes is None:
            screenshot_bytes = self._take_screenshot(device)
            if not screenshot_bytes:
                return None

        img_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        # 2026-04-24 P5a: 解析 image 维度供 bounds validation
        # (eval 发现 Gemini 2.5 Flash 常返超屏 coord e.g. (980, 2020) on
        # 720x1600 image, d.click() 打屏外无效)。
        img_w, img_h = self._png_dimensions(screenshot_bytes)

        prompt = self._build_prompt(target, context)

        for attempt in range(self.config.max_retries):
            response = self._client.chat_vision(prompt, img_b64, max_tokens=200)
            if not response:
                continue
            result = self._parse_response(response)
            self._record_call()
            if not result.coordinates:
                log.debug(
                    "VisionFallback no coordinates (attempt %d)", attempt + 1)
                continue
            # P5a bounds check: 超屏 VLM 返值 treated as miss, retry
            x, y = result.coordinates
            if img_w and img_h and (
                    x < 0 or x >= img_w or y < 0 or y >= img_h):
                log.warning(
                    "VisionFallback out-of-bounds coords (%d, %d) for image "
                    "%dx%d, rejecting (attempt %d)",
                    x, y, img_w, img_h, attempt + 1)
                continue
            self._set_cache(cache_key, result)
            log.info(
                "VisionFallback found '%s' at %s", target, result.coordinates)
            return result

        log.warning(
            "VisionFallback failed to find '%s' after %d attempts",
            target, self.config.max_retries)
        return None

    @staticmethod
    def _png_dimensions(png_bytes):
        """解析 PNG bytes 前 24 字节拿 (width, height)。

        PNG spec: 8-byte signature + 4-byte chunk length + 4-byte 'IHDR' +
        4-byte width + 4-byte height。malformed/non-PNG/太短 返 (None, None)。
        零依赖 (无 PIL), 最小开销。

        Returns:
            Tuple[Optional[int], Optional[int]] — (width, height) 或 (None, None)
        """
        if not png_bytes or len(png_bytes) < 24:
            return None, None
        if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
            return None, None
        try:
            w = int.from_bytes(png_bytes[16:20], "big")
            h = int.from_bytes(png_bytes[20:24], "big")
            if 0 < w <= 100000 and 0 < h <= 100000:  # sanity
                return w, h
        except Exception:
            pass
        return None, None

    def identify_screen(self, device, context: str = "") -> str:
        """Identify what screen/state the app is currently showing."""
        if not self._check_budget():
            return "unknown (budget exhausted)"

        screenshot_bytes = self._take_screenshot(device)
        if not screenshot_bytes:
            return "unknown (screenshot failed)"

        img_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        prompt = (
            "Describe what screen or state this mobile app is showing. "
            "Be specific: app name, current page, visible UI elements, any dialogs or popups. "
            f"Context: {context}" if context else
            "Describe what screen or state this mobile app is showing. "
            "Be specific: app name, current page, visible UI elements, any dialogs or popups."
        )

        response = self._client.chat_vision(prompt, img_b64, max_tokens=300)
        self._record_call()
        return response or "unknown"

    @property
    def budget_remaining(self) -> int:
        return max(0, self.config.hourly_budget - self._hourly_count())

    def stats(self) -> dict:
        return {
            "hourly_used": self._hourly_count(),
            "hourly_budget": self.config.hourly_budget,
            "budget_remaining": self.budget_remaining,
            "cache_size": len(self._cache),
        }

    # -- Internal -----------------------------------------------------------

    @staticmethod
    def _take_screenshot(device) -> Optional[bytes]:
        try:
            img = device.screenshot(format="raw")
            if isinstance(img, bytes):
                return img
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            log.error("Screenshot failed: %s", e)
            return None

    @staticmethod
    def _build_prompt(target: str, context: str) -> str:
        parts = [
            f"Find the UI element: \"{target}\".",
            "Return the pixel coordinates (x, y) of the CENTER of this element.",
            "Format: COORDINATES: x, y",
            "If the element is not visible, say NOT_FOUND.",
        ]
        if context:
            parts.insert(1, f"Context: {context}")
        return " ".join(parts)

    @staticmethod
    def _parse_response(response: str) -> VisionResult:
        """Parse VLM text response → (x, y) coords。

        2026-04-24 bugfix: 原 regex `(\\d+)` 吞掉前导 `-` — 比如 VLM 返
        `COORDINATES: -10, 20` 会被解成 `(10, 20)`, 绕过 ``find_element`` 的
        img-size bounds check (`x < 0` 条件永远不触发)。改 `(-?\\d+)` 保留
        负号; 后续 bounds check (caller) 自然 reject 负坐标。

        Secondary fallback (`\\d{2,4}, \\d{2,4}`) 原用 `\\b` word boundary
        也有同 bug (`-` 是非 word char → `\\b` 命中 `-|10` 中间, 数字部分被
        单独捕获)。现同样改 `(-?\\d{1,4})` + 去 `\\b` 依靠数字长度 + 上下文
        避免误匹配。
        """
        result = VisionResult(raw_response=response)

        coord_match = re.search(
            r'COORDINATES:\s*(-?\d+)\s*,\s*(-?\d+)', response)
        if coord_match:
            result.coordinates = (
                int(coord_match.group(1)), int(coord_match.group(2)))
            result.confidence = "high"
            return result

        num_match = re.findall(r'(-?\d{2,4})\s*,\s*(-?\d{2,4})', response)
        if num_match:
            x, y = int(num_match[0][0]), int(num_match[0][1])
            if 0 < x < 2000 and 0 < y < 3000:
                result.coordinates = (x, y)
                result.confidence = "medium"
                return result

        result.description = response[:200]
        return result

    def _check_budget(self) -> bool:
        return self._hourly_count() < self.config.hourly_budget

    def _hourly_count(self) -> int:
        cutoff = time.time() - 3600
        with self._lock:
            self._call_timestamps = [t for t in self._call_timestamps if t > cutoff]
            return len(self._call_timestamps)

    def _record_call(self):
        with self._lock:
            self._call_timestamps.append(time.time())

    def _cache_key(self, target: str, context: str) -> str:
        raw = f"{target}:{context}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def _get_cache(self, key: str) -> Optional[VisionResult]:
        with self._lock:
            if key in self._cache:
                result, ts = self._cache[key]
                if time.time() - ts < self.config.cache_ttl_sec:
                    return result
                del self._cache[key]
        return None

    def _set_cache(self, key: str, result: VisionResult):
        with self._lock:
            self._cache[key] = (result, time.time())
            if len(self._cache) > 100:
                oldest = min(self._cache.items(), key=lambda x: x[1][1])
                del self._cache[oldest[0]]

    def invalidate(self, target: str, context: str = "") -> bool:
        """Evict cached VLM result for ``(target, context)``.

        Use when the caller verifies the VLM-returned coordinates didn't
        actually work (e.g. click at returned (x,y) but expected UI state
        change didn't happen). Without invalidation the wrong coordinates
        would be re-used for up to ``cache_ttl_sec`` (default 300s).

        Args:
            target: same ``target`` string as in ``find_element``
            context: same ``context`` string as in ``find_element``

        Returns:
            True if a cached entry was removed, False if none matched.
        """
        key = self._cache_key(target, context)
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
        return False
