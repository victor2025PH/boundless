# -*- coding: utf-8 -*-
"""P16 (2026-04-24): `vlm_level4_prometheus_text()` Prometheus text exposition
+ swap_events_total counter in `/facebook/vlm/level4/status` JSON endpoint。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset():
    import src.app_automation.facebook as fb
    orig = {
        "inst": fb._vision_fallback_instance,
        "swap": fb._vlm_provider_swapped,
        "fail": fb._vlm_consecutive_failures,
        "swap_total": fb._vlm_swap_events_total,
        "att": fb._vision_fallback_init_attempted,
    }
    fb._vision_fallback_instance = None
    fb._vlm_provider_swapped = False
    fb._vlm_consecutive_failures = 0
    fb._vlm_swap_events_total = 0
    fb._vision_fallback_init_attempted = False
    yield
    fb._vision_fallback_instance = orig["inst"]
    fb._vlm_provider_swapped = orig["swap"]
    fb._vlm_consecutive_failures = orig["fail"]
    fb._vlm_swap_events_total = orig["swap_total"]
    fb._vision_fallback_init_attempted = orig["att"]


def _metrics() -> dict:
    """Parse output of `vlm_level4_prometheus_text` into `{name_labels: val}` dict."""
    from src.app_automation.facebook import vlm_level4_prometheus_text
    text = vlm_level4_prometheus_text()
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        # "name val" or "name{labels} val"
        if " " not in line:
            continue
        key, val = line.rsplit(" ", 1)
        out[key] = val
    return out


# ─── Prometheus text emission ────────────────────────────────────────

class TestPrometheusEmission:

    def test_uninitialized_minimum_metrics(self):
        """未 init 时仍应发 swapped=0 / failures=0 / swap_total=0 / ready=0 / init_attempted=0."""
        m = _metrics()
        assert m["openclaw_vlm_level4_swapped"] == "0"
        assert m["openclaw_vlm_level4_consecutive_failures"] == "0"
        assert m["openclaw_vlm_level4_swap_events_total"] == "0"
        assert m["openclaw_vlm_level4_ready"] == "0"
        assert m["openclaw_vlm_level4_init_attempted"] == "0"
        # budget/provider-labels 未发 (无 instance)
        assert "openclaw_vlm_level4_budget_hourly" not in m

    def test_swapped_and_counter_surface(self):
        import src.app_automation.facebook as fb
        fb._vlm_provider_swapped = True
        fb._vlm_swap_events_total = 3
        m = _metrics()
        assert m["openclaw_vlm_level4_swapped"] == "1"
        assert m["openclaw_vlm_level4_swap_events_total"] == "3"

    def test_consecutive_failures(self):
        import src.app_automation.facebook as fb
        fb._vlm_consecutive_failures = 2
        m = _metrics()
        assert m["openclaw_vlm_level4_consecutive_failures"] == "2"

    def test_init_attempted_flag(self):
        import src.app_automation.facebook as fb
        fb._vision_fallback_init_attempted = True
        m = _metrics()
        assert m["openclaw_vlm_level4_init_attempted"] == "1"

    def test_ready_with_instance(self):
        import src.app_automation.facebook as fb
        vf = MagicMock()
        vf.stats.return_value = {
            "hourly_used": 5, "hourly_budget": 20,
            "budget_remaining": 15, "cache_size": 2}
        vf._client = SimpleNamespace(
            config=SimpleNamespace(provider="gemini",
                                    vision_model="gemini-2.5-flash"),
            last_error_code=None)
        fb._vision_fallback_instance = vf
        m = _metrics()
        assert m["openclaw_vlm_level4_ready"] == "1"
        assert m["openclaw_vlm_level4_budget_used"] == "5"
        assert m["openclaw_vlm_level4_budget_hourly"] == "20"
        assert m["openclaw_vlm_level4_budget_remaining"] == "15"
        assert m["openclaw_vlm_level4_cache_size"] == "2"

    def test_last_error_code_gauge(self):
        import src.app_automation.facebook as fb
        vf = MagicMock()
        vf.stats.return_value = {}
        vf._client = SimpleNamespace(
            config=SimpleNamespace(provider="gemini",
                                    vision_model="gemini-2.5-flash"),
            last_error_code=503)
        fb._vision_fallback_instance = vf
        m = _metrics()
        assert m["openclaw_vlm_level4_last_error_code"] == "503"

    def test_last_error_none_becomes_zero(self):
        import src.app_automation.facebook as fb
        vf = MagicMock()
        vf.stats.return_value = {}
        vf._client = SimpleNamespace(
            config=SimpleNamespace(provider="ollama",
                                    vision_model="llava:7b"),
            last_error_code=None)
        fb._vision_fallback_instance = vf
        m = _metrics()
        assert m["openclaw_vlm_level4_last_error_code"] == "0"

    def test_provider_info_labels(self):
        import src.app_automation.facebook as fb
        vf = MagicMock()
        vf.stats.return_value = {}
        vf._client = SimpleNamespace(
            config=SimpleNamespace(provider="gemini",
                                    vision_model="gemini-2.5-flash"),
            last_error_code=None)
        fb._vision_fallback_instance = vf
        m = _metrics()
        key = 'openclaw_vlm_level4_provider_info{provider="gemini",vision_model="gemini-2.5-flash"}'
        assert m[key] == "1"

    def test_prometheus_format_has_help_and_type(self):
        """每 metric 必有 HELP + TYPE 行 (Prometheus exposition 格式标准)。"""
        from src.app_automation.facebook import vlm_level4_prometheus_text
        text = vlm_level4_prometheus_text()
        # 至少 5 个 metric, 每个 HELP+TYPE 各 1 行
        assert text.count("# HELP ") >= 5
        assert text.count("# TYPE ") >= 5
        # counter 类型必出现至少 1 次 (swap_events_total)
        assert "# TYPE openclaw_vlm_level4_swap_events_total counter" in text

    def test_stats_exception_does_not_crash(self):
        """vf.stats() 抛 → 应返 partial text, 不把整个 /observability/prometheus 打挂。"""
        import src.app_automation.facebook as fb
        vf = MagicMock()
        vf.stats = MagicMock(side_effect=RuntimeError("stats boom"))
        vf._client = SimpleNamespace(
            config=SimpleNamespace(provider="gemini", vision_model="x"),
            last_error_code=None)
        fb._vision_fallback_instance = vf
        m = _metrics()
        assert m["openclaw_vlm_level4_ready"] == "1"
        # budget 字段 fall back 到 0 (因为 stats={})
        assert m.get("openclaw_vlm_level4_budget_used") == "0"


# ─── swap_events_total counter increments on real swap ───────────────

class TestSwapCounterIncrement:
    """_record_vlm_result 触发 swap 时 _vlm_swap_events_total 应 +1."""

    def test_threshold_hit_increments_counter(self):
        import src.app_automation.facebook as fb
        from unittest.mock import patch

        client = SimpleNamespace(
            config=SimpleNamespace(provider="gemini",
                                    vision_model="gemini-2.5-flash"),
            last_error_code=503, last_error_body="high demand")
        vf = SimpleNamespace(_client=client)
        fake_ollama = SimpleNamespace(
            config=SimpleNamespace(provider="ollama", vision_model="llava"))
        assert fb._vlm_swap_events_total == 0
        with patch(
                "src.app_automation.facebook._try_ollama_vision_client",
                return_value=fake_ollama), \
             patch("src.ai.vision_fallback.VisionFallback"):
            for _ in range(3):
                fb._record_vlm_result(vf)
        assert fb._vlm_provider_swapped is True
        assert fb._vlm_swap_events_total == 1

    def test_failed_swap_no_counter_increment(self):
        """Ollama 不可用 → no swap, counter 不动。"""
        import src.app_automation.facebook as fb
        from unittest.mock import patch

        client = SimpleNamespace(
            config=SimpleNamespace(provider="gemini",
                                    vision_model="gemini-2.5-flash"),
            last_error_code=503, last_error_body="oops")
        vf = SimpleNamespace(_client=client)
        with patch(
                "src.app_automation.facebook._try_ollama_vision_client",
                return_value=None):
            for _ in range(5):
                fb._record_vlm_result(vf)
        assert fb._vlm_provider_swapped is False
        assert fb._vlm_swap_events_total == 0


# ─── /facebook/vlm/level4/status JSON exposes counter ─────────────────

class TestStatusEndpointCounter:
    def test_swap_events_total_in_json(self):
        import src.app_automation.facebook as fb
        from src.host.routers.facebook import fb_vlm_level4_status
        fb._vlm_swap_events_total = 7
        r = fb_vlm_level4_status()
        assert r["swap_events_total"] == 7

    def test_default_zero(self):
        from src.host.routers.facebook import fb_vlm_level4_status
        r = fb_vlm_level4_status()
        assert r["swap_events_total"] == 0
