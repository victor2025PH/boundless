# -*- coding: utf-8 -*-
"""`VisionFallback` — invalidate API + cache 基础逻辑。

`find_element` 的 LLM call 不测 (真 LLM 或 mock 复杂), 只测:
  * `invalidate(target, context)` 清 cache
  * `_cache_key` 稳定 (same inputs → same key)
  * cache TTL 逻辑 (set 后命中, 超时后 miss)
  * budget counting
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


def _make_vf(hourly_budget=20, cache_ttl_sec=300.0):
    from src.ai.vision_fallback import VisionFallback, VisionConfig
    client = MagicMock()
    return VisionFallback(
        client=client,
        config=VisionConfig(hourly_budget=hourly_budget,
                            cache_ttl_sec=cache_ttl_sec),
    )


class TestInvalidate:
    """`invalidate(target, context)` API — 2026-04-24 新增 for Level 4 VLM
    click-verify fail 场景。"""

    def test_invalidate_removes_cached_entry(self):
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf()
        # 直接写 cache (绕开 find_element)
        key = vf._cache_key("search bar", "ctx")
        vf._set_cache(key, VisionResult(coordinates=(100, 200)))
        assert vf._get_cache(key) is not None
        assert vf.invalidate("search bar", "ctx") is True
        assert vf._get_cache(key) is None

    def test_invalidate_returns_false_when_not_cached(self):
        vf = _make_vf()
        assert vf.invalidate("never-cached", "") is False

    def test_invalidate_precise_match_only(self):
        """不同 target 的 cache 不互相干扰。"""
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf()
        vf._set_cache(
            vf._cache_key("A", "ctx1"), VisionResult(coordinates=(1, 1)))
        vf._set_cache(
            vf._cache_key("B", "ctx2"), VisionResult(coordinates=(2, 2)))
        assert vf.invalidate("A", "ctx1") is True
        # B 保留
        assert vf._get_cache(vf._cache_key("B", "ctx2")) is not None
        # A 已空
        assert vf._get_cache(vf._cache_key("A", "ctx1")) is None

    def test_invalidate_default_context(self):
        """context 省略默认为 ''"""
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf()
        vf._set_cache(
            vf._cache_key("target", ""), VisionResult(coordinates=(5, 5)))
        assert vf.invalidate("target") is True


class TestCacheKey:
    def test_same_inputs_same_key(self):
        vf = _make_vf()
        k1 = vf._cache_key("search", "context1")
        k2 = vf._cache_key("search", "context1")
        assert k1 == k2

    def test_different_target_different_key(self):
        vf = _make_vf()
        assert vf._cache_key("A", "ctx") != vf._cache_key("B", "ctx")

    def test_different_context_different_key(self):
        vf = _make_vf()
        assert vf._cache_key("A", "ctx1") != vf._cache_key("A", "ctx2")


class TestCacheTTL:
    def test_cache_hit_within_ttl(self):
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf(cache_ttl_sec=300.0)
        key = vf._cache_key("t", "c")
        vf._set_cache(key, VisionResult(coordinates=(1, 2)))
        assert vf._get_cache(key) is not None

    def test_cache_miss_after_ttl_expired(self):
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf(cache_ttl_sec=0.01)
        key = vf._cache_key("t", "c")
        vf._set_cache(key, VisionResult(coordinates=(1, 2)))
        time.sleep(0.05)
        assert vf._get_cache(key) is None


class TestBudget:
    def test_record_call_counts(self):
        vf = _make_vf(hourly_budget=20)
        assert vf.budget_remaining == 20
        vf._record_call()
        vf._record_call()
        assert vf.budget_remaining == 18
        assert vf._check_budget() is True

    def test_budget_exhausted(self):
        vf = _make_vf(hourly_budget=2)
        vf._record_call()
        vf._record_call()
        assert vf.budget_remaining == 0
        assert vf._check_budget() is False

    def test_stats_shape(self):
        vf = _make_vf(hourly_budget=10)
        vf._record_call()
        s = vf.stats()
        assert s["hourly_used"] == 1
        assert s["hourly_budget"] == 10
        assert s["budget_remaining"] == 9
        assert "cache_size" in s


# ─── P5a: _png_dimensions + bounds check (2026-04-24) ───────────────


def _make_png_header(width: int, height: int) -> bytes:
    """合成 PNG 前 24 字节 (signature + IHDR length + type + w + h), 够
    `_png_dimensions` 读取。不生成完整有效 PNG。"""
    import struct
    return (
        b"\x89PNG\r\n\x1a\n"           # 8-byte signature
        + struct.pack(">I", 13)        # IHDR length (BE uint32)
        + b"IHDR"                      # chunk type
        + struct.pack(">II", width, height)  # width + height (BE uint32 each)
    )


class TestPngDimensions:
    """`VisionFallback._png_dimensions` — 零依赖解析 PNG 尺寸。"""

    def test_valid_720x1600(self):
        from src.ai.vision_fallback import VisionFallback
        png = _make_png_header(720, 1600)
        assert VisionFallback._png_dimensions(png) == (720, 1600)

    def test_valid_1080x2400(self):
        from src.ai.vision_fallback import VisionFallback
        png = _make_png_header(1080, 2400)
        assert VisionFallback._png_dimensions(png) == (1080, 2400)

    def test_truncated_returns_none(self):
        from src.ai.vision_fallback import VisionFallback
        assert VisionFallback._png_dimensions(b"\x89PNG") == (None, None)

    def test_non_png_returns_none(self):
        from src.ai.vision_fallback import VisionFallback
        # JPEG header 开头
        assert VisionFallback._png_dimensions(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 20
        ) == (None, None)

    def test_empty_returns_none(self):
        from src.ai.vision_fallback import VisionFallback
        assert VisionFallback._png_dimensions(b"") == (None, None)
        assert VisionFallback._png_dimensions(None) == (None, None)

    def test_zero_size_rejected(self):
        """宽高 = 0 不合法 (sanity check)。"""
        from src.ai.vision_fallback import VisionFallback
        png = _make_png_header(0, 0)
        assert VisionFallback._png_dimensions(png) == (None, None)


class TestBoundsCheck:
    """`find_element` 对 VLM 返超屏坐标的处理 — eval 发现 Gemini 常返 upscaled
    coords, 需 reject 避免 d.click() 打屏外无效。"""

    def _vf(self, response_coords_list):
        """造 VisionFallback, 可控 chat_vision 每次返 'COORDINATES: x, y'."""
        from src.ai.vision_fallback import VisionFallback, VisionConfig
        client = MagicMock()
        responses = [f"COORDINATES: {x}, {y}"
                     for (x, y) in response_coords_list]
        client.chat_vision = MagicMock(side_effect=responses)
        return VisionFallback(
            client=client,
            config=VisionConfig(hourly_budget=20, max_retries=3)), client

    def test_in_bounds_accepted(self):
        """VLM 返屏内坐标 → 正常 cache + return。"""
        vf, client = self._vf([(500, 300)])
        png = _make_png_header(720, 1600)
        r = vf.find_element(
            device=None, target="x", context="c", screenshot_bytes=png)
        assert r and r.coordinates == (500, 300)
        assert client.chat_vision.call_count == 1

    def test_out_of_bounds_x_rejected_retries(self):
        """VLM 返 x 超屏 → reject, retry 下一次 (返 valid 则 HIT)。"""
        vf, client = self._vf([(9999, 300), (500, 300)])
        png = _make_png_header(720, 1600)
        r = vf.find_element(
            device=None, target="x", context="c", screenshot_bytes=png)
        assert r and r.coordinates == (500, 300)
        assert client.chat_vision.call_count == 2

    def test_out_of_bounds_y_rejected_retries(self):
        vf, client = self._vf([(500, 9999), (500, 300)])
        png = _make_png_header(720, 1600)
        r = vf.find_element(
            device=None, target="x", context="c", screenshot_bytes=png)
        assert r and r.coordinates == (500, 300)

    def test_all_retries_out_of_bounds_returns_none(self):
        """所有 retry 都超屏 → 最终返 None (treated as miss)。"""
        vf, client = self._vf([(9999, 9999), (8888, 8888), (7777, 7777)])
        png = _make_png_header(720, 1600)
        r = vf.find_element(
            device=None, target="x", context="c", screenshot_bytes=png)
        assert r is None
        assert client.chat_vision.call_count == 3

    def test_no_png_header_skips_bounds_check(self):
        """无法解析 image 维度 → bounds check 跳过, VLM 返值原样 accept
        (向后兼容, 不因为 screenshot 格式未知就把所有结果拒掉)。"""
        vf, client = self._vf([(9999, 9999)])
        not_png = b"not-a-png-at-all" + b"\x00" * 20
        r = vf.find_element(
            device=None, target="x", context="c", screenshot_bytes=not_png)
        assert r and r.coordinates == (9999, 9999)  # no rejection w/o img size

    def test_out_of_bounds_not_cached(self):
        """超屏坐标不应入 cache (下次 call 不复发坏结果)。"""
        vf, client = self._vf([(9999, 9999), (9999, 9999), (9999, 9999)])
        png = _make_png_header(720, 1600)
        vf.find_element(
            device=None, target="x", context="c", screenshot_bytes=png)
        assert vf._get_cache(vf._cache_key("x", "c")) is None


# ─── P13 (2026-04-24): _parse_response 负坐标正则 bug fix ────────────

class TestParseResponseNegativeCoords:
    """原 regex `(\\d+)` 吞掉负号 — `COORDINATES: -10, 20` 被解为 `(10, 20)`
    绕过 find_element 的 img bounds check (`x < 0` 永远不 trigger)。现改
    `(-?\\d+)` 保负号 + 下游 bounds check 正常 reject。"""

    def test_primary_parses_negative_x(self):
        from src.ai.vision_fallback import VisionFallback
        r = VisionFallback._parse_response("COORDINATES: -10, 20")
        assert r.coordinates == (-10, 20)
        assert r.confidence == "high"

    def test_primary_parses_negative_y(self):
        from src.ai.vision_fallback import VisionFallback
        r = VisionFallback._parse_response("COORDINATES: 100, -5")
        assert r.coordinates == (100, -5)

    def test_primary_parses_both_negative(self):
        from src.ai.vision_fallback import VisionFallback
        r = VisionFallback._parse_response("COORDINATES: -10, -20")
        assert r.coordinates == (-10, -20)

    def test_primary_parses_positive_unchanged(self):
        """正常正数解析不变 (向后兼容 regression guard)。"""
        from src.ai.vision_fallback import VisionFallback
        r = VisionFallback._parse_response("COORDINATES: 500, 300")
        assert r.coordinates == (500, 300)

    def test_secondary_negative_rejected_by_range_check(self):
        """secondary fallback (no 'COORDINATES:' prefix) 捕到负数 → 被
        `0 < x < 2000` range check 拒, 不返 coords (降级 description)。"""
        from src.ai.vision_fallback import VisionFallback
        r = VisionFallback._parse_response("看点 -10, 200 附近")
        # range check rejects negative → no coordinates set
        assert r.coordinates is None

    def test_secondary_positive_in_text_still_works(self):
        """secondary 正常 `宽数字, 宽数字` 匹配不变 (regression)。"""
        from src.ai.vision_fallback import VisionFallback
        r = VisionFallback._parse_response("元素位于 540, 438 处")
        assert r.coordinates == (540, 438)
        assert r.confidence == "medium"

    def test_bounds_check_catches_negative_from_primary(self):
        """端到端: primary 解析 `-10, 20`, find_element 的 img bounds check
        reject (x < 0), 不入 cache, retry 下次。"""
        from src.ai.vision_fallback import VisionFallback, VisionConfig
        client = MagicMock()
        # 3 次 retry: 第 1 次返 neg, 第 2 次返 valid
        client.chat_vision = MagicMock(side_effect=[
            "COORDINATES: -10, 20",
            "COORDINATES: 300, 400",
        ])
        vf = VisionFallback(
            client=client,
            config=VisionConfig(hourly_budget=20, max_retries=3))
        png_bytes = (b"\x89PNG\r\n\x1a\n"
                      + (13).to_bytes(4, "big") + b"IHDR"
                      + (720).to_bytes(4, "big") + (1600).to_bytes(4, "big"))
        r = vf.find_element(
            device=None, target="t", context="c",
            screenshot_bytes=png_bytes)
        # 第 1 次 (-10, 20) 被 bounds check reject, 第 2 次 (300, 400) HIT
        assert r and r.coordinates == (300, 400)
        assert client.chat_vision.call_count == 2
