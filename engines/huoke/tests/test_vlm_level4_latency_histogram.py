# -*- coding: utf-8 -*-
"""P18 (2026-04-24): `_observe_vlm_latency` + Prometheus histogram export +
`/facebook/vlm/level4/status` JSON `latency` field。

Histogram cumulative bucket 语义: bucket[k] = count of samples ≤ BUCKETS[k],
且包含所有更小 bucket 的 count (Prometheus spec 要求单调递增到 +Inf)。
"""
from __future__ import annotations

import re

import pytest


@pytest.fixture(autouse=True)
def _reset():
    import src.app_automation.facebook as fb
    orig = {
        "bc": list(fb._vlm_latency_bucket_counts),
        "sum": fb._vlm_latency_sum,
        "cnt": fb._vlm_latency_count,
    }
    fb._vlm_latency_bucket_counts = [0] * len(fb._vlm_latency_bucket_counts)
    fb._vlm_latency_sum = 0.0
    fb._vlm_latency_count = 0
    yield
    fb._vlm_latency_bucket_counts = orig["bc"]
    fb._vlm_latency_sum = orig["sum"]
    fb._vlm_latency_count = orig["cnt"]


# ─── _observe_vlm_latency bucket 语义 ────────────────────────────────

class TestObserveBucketing:

    def test_single_small_observation_hits_all_buckets_above(self):
        """0.2s sample — ≤ 每个 bucket (0.5/1/2/5/10/20/30/60/+Inf), 所有
        bucket 都 +1 (cumulative)."""
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(0.2)
        # 每个 bucket count == 1
        for i, cnt in enumerate(fb._vlm_latency_bucket_counts):
            assert cnt == 1, f"bucket[{i}] expected 1, got {cnt}"
        assert fb._vlm_latency_count == 1
        assert abs(fb._vlm_latency_sum - 0.2) < 1e-6

    def test_mid_observation_hits_only_higher_buckets(self):
        """8s sample — 超过 0.5/1/2/5 (前 4 bucket), ≤ 10/20/30/60/+Inf
        (后 5 bucket)。"""
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(8.0)
        # BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60); 8 ≤ 10 → idx 4+ 命中
        # 前 4 bucket (≤ 0.5, 1, 2, 5) 不命中, 后 4 bucket + +Inf 命中
        expected = [0, 0, 0, 0, 1, 1, 1, 1, 1]
        assert fb._vlm_latency_bucket_counts == expected

    def test_large_observation_only_inf_bucket(self):
        """70s sample (> 60) 只命中 +Inf bucket。"""
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(70.0)
        expected = [0] * 8 + [1]  # 前 8 都 0, +Inf = 1
        assert fb._vlm_latency_bucket_counts == expected

    def test_boundary_exact_bucket_edge_inclusive(self):
        """边界: sample == bucket upper bound 应 ≤ 命中 (Prometheus spec 要求
        `le="0.5"` 的 bucket 包含 0.5)."""
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(0.5)
        # 0.5 ≤ 0.5 ✓ 所有 bucket 命中
        assert all(c == 1 for c in fb._vlm_latency_bucket_counts)

    def test_multiple_samples_aggregate(self):
        """多次 sample: count + sum 累加, bucket count 累加。"""
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(0.3)   # 命中所有 bucket
        fb._observe_vlm_latency(8.0)   # 命中 idx 4+ + Inf
        fb._observe_vlm_latency(40.0)  # 命中 idx 7 + Inf
        # bucket[0..3]: 只 0.3s 命中, 各 1
        # bucket[4..6]: 0.3 + 8 命中, 各 2
        # bucket[7] (≤60): 0.3 + 8 + 40 命中, 3
        # +Inf: 3 (每次必加)
        assert fb._vlm_latency_bucket_counts == [1, 1, 1, 1, 2, 2, 2, 3, 3]
        assert fb._vlm_latency_count == 3
        assert abs(fb._vlm_latency_sum - (0.3 + 8.0 + 40.0)) < 1e-6

    def test_zero_duration_still_records(self):
        """0s sample — 边界但 Prometheus 接受。"""
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(0.0)
        assert all(c == 1 for c in fb._vlm_latency_bucket_counts)
        assert fb._vlm_latency_count == 1
        assert fb._vlm_latency_sum == 0.0


# ─── Prometheus text emission includes histogram ─────────────────────

class TestPrometheusHistogramFormat:

    def test_histogram_type_declared(self):
        from src.app_automation.facebook import vlm_level4_prometheus_text
        text = vlm_level4_prometheus_text()
        assert ("# TYPE openclaw_vlm_level4_call_duration_seconds histogram"
                in text)

    def test_all_buckets_emitted(self):
        """每个 bucket 都有对应 _bucket{le=...} 行 + +Inf。"""
        import src.app_automation.facebook as fb
        text = fb.vlm_level4_prometheus_text()
        for upper in fb._VLM_LATENCY_BUCKETS:
            assert f'_bucket{{le="{upper}"}}' in text
        assert '_bucket{le="+Inf"}' in text

    def test_sum_and_count_emitted(self):
        from src.app_automation.facebook import vlm_level4_prometheus_text
        text = vlm_level4_prometheus_text()
        assert "openclaw_vlm_level4_call_duration_seconds_sum " in text
        assert "openclaw_vlm_level4_call_duration_seconds_count " in text

    def test_bucket_counts_reflect_observations(self):
        """_observe 后 Prometheus text 里 +Inf bucket count 对应 samples 数。"""
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(1.0)
        fb._observe_vlm_latency(5.0)
        fb._observe_vlm_latency(15.0)
        text = fb.vlm_level4_prometheus_text()
        # +Inf bucket 每次必加
        m = re.search(
            r'openclaw_vlm_level4_call_duration_seconds_bucket\{le="\+Inf"\} (\d+)',
            text)
        assert m is not None
        assert int(m.group(1)) == 3
        # count 行
        m = re.search(
            r'openclaw_vlm_level4_call_duration_seconds_count (\d+)', text)
        assert m and int(m.group(1)) == 3

    def test_sum_value_in_text(self):
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(2.5)
        fb._observe_vlm_latency(7.5)
        text = fb.vlm_level4_prometheus_text()
        m = re.search(
            r'openclaw_vlm_level4_call_duration_seconds_sum (\d+\.?\d*)',
            text)
        assert m is not None
        assert abs(float(m.group(1)) - 10.0) < 1e-3

    def test_cumulative_monotone(self):
        """Prometheus 要求 cumulative bucket 单调递增 (每个 bucket ≤ 下一个)。"""
        import src.app_automation.facebook as fb
        # 几个随机 sample
        for s in (0.3, 4.0, 12.0, 0.1, 25.0, 8.0):
            fb._observe_vlm_latency(s)
        counts = fb._vlm_latency_bucket_counts
        for i in range(len(counts) - 1):
            assert counts[i] <= counts[i + 1], (
                f"bucket monotone broken at idx {i}: {counts[i]} > {counts[i+1]}")


# ─── JSON /facebook/vlm/level4/status exposes latency ─────────────────

class TestStatusJsonLatency:

    def test_default_zero(self):
        from src.host.routers.facebook import fb_vlm_level4_status
        r = fb_vlm_level4_status()
        assert r["latency"]["count"] == 0
        assert r["latency"]["sum_sec"] == 0.0
        assert r["latency"]["avg_sec"] == 0.0

    def test_populated_avg(self):
        import src.app_automation.facebook as fb
        fb._observe_vlm_latency(2.0)
        fb._observe_vlm_latency(8.0)
        from src.host.routers.facebook import fb_vlm_level4_status
        r = fb_vlm_level4_status()
        assert r["latency"]["count"] == 2
        assert r["latency"]["sum_sec"] == 10.0
        assert r["latency"]["avg_sec"] == 5.0

    def test_avg_zero_when_count_zero(self):
        """无 sample 时 avg=0 不除 0。"""
        from src.host.routers.facebook import fb_vlm_level4_status
        r = fb_vlm_level4_status()
        assert r["latency"]["avg_sec"] == 0.0
