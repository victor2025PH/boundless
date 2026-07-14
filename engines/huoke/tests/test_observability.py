"""
Tests for Observability modules: StructuredLogger, ExecutionStore,
MetricsCollector, AlertManager, Security.
"""

import os
import sys
import json
import time
import tempfile
import threading
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability.structured_log import StructuredLogger
from src.observability.execution_store import ExecutionStore
from src.observability.metrics import MetricsCollector, _HistogramBucket
from src.observability.alerting import (
    AlertManager, AlertRule, AlertEvent, AlertSeverity, AlertState,
    create_default_rules,
)
from src.observability.security import (
    SecureStore, ConfigValidator, sanitize, sanitize_dict,
)


# ---------------------------------------------------------------------------
# StructuredLogger
# ---------------------------------------------------------------------------

class TestStructuredLogger:
    def test_write_and_query(self):
        with tempfile.TemporaryDirectory() as td:
            slog = StructuredLogger(log_dir=td, console=False)
            slog.info("test message", key="value")
            slog.error("bad thing", code=500)
            slog.close()

            logs = slog.query_logs(limit=10)
            assert len(logs) == 2
            assert logs[0]["level"] == "info"
            assert logs[0]["ctx"]["key"] == "value"
            assert logs[1]["level"] == "error"

    def test_action_log(self):
        with tempfile.TemporaryDirectory() as td:
            slog = StructuredLogger(log_dir=td, console=False)
            slog.action("send_message", platform="telegram", success=True, duration_sec=1.5)
            slog.close()

            logs = slog.query_logs()
            assert len(logs) == 1
            assert logs[0]["ctx"]["platform"] == "telegram"
            assert logs[0]["ctx"]["success"] is True

    def test_workflow_log(self):
        with tempfile.TemporaryDirectory() as td:
            slog = StructuredLogger(log_dir=td, console=False)
            slog.workflow("test_wf", "run123", "success", steps_total=3, steps_ok=3)
            slog.close()

            logs = slog.query_logs()
            assert len(logs) == 1
            assert "WORKFLOW" in logs[0]["message"]

    def test_query_filter_level(self):
        with tempfile.TemporaryDirectory() as td:
            slog = StructuredLogger(log_dir=td, console=False)
            slog.info("info1")
            slog.error("error1")
            slog.info("info2")
            slog.close()

            errors = slog.query_logs(level="error")
            assert len(errors) == 1
            assert errors[0]["message"] == "bad thing" or errors[0]["message"] == "error1"

    def test_query_filter_contains(self):
        with tempfile.TemporaryDirectory() as td:
            slog = StructuredLogger(log_dir=td, console=False)
            slog.info("hello world")
            slog.info("goodbye world")
            slog.close()

            results = slog.query_logs(contains="hello")
            assert len(results) == 1

    def test_log_files(self):
        with tempfile.TemporaryDirectory() as td:
            slog = StructuredLogger(log_dir=td, console=False)
            slog.info("test")
            slog.close()
            files = slog.log_files()
            assert len(files) >= 1
            assert files[0].endswith(".jsonl")

    def test_thread_safety(self):
        with tempfile.TemporaryDirectory() as td:
            slog = StructuredLogger(log_dir=td, console=False)
            threads = []
            for i in range(20):
                t = threading.Thread(target=lambda i=i: slog.info(f"msg {i}"))
                threads.append(t)
                t.start()
            for t in threads:
                t.join()
            slog.close()
            logs = slog.query_logs(limit=100)
            assert len(logs) == 20


# ---------------------------------------------------------------------------
# ExecutionStore
# ---------------------------------------------------------------------------

class TestExecutionStore:
    def _make_result(self, run_id="r1", name="test_wf", success=True):
        return {
            "run_id": run_id,
            "workflow_name": name,
            "success": success,
            "aborted": False,
            "elapsed_sec": 1.5,
            "variables": {"x": 1},
            "steps": {
                "s1": {"status": "success", "result": "ok", "error": "", "duration_sec": 0.5, "retries_used": 0},
                "s2": {"status": "success", "result": 42, "error": "", "duration_sec": 1.0, "retries_used": 0},
            },
        }

    def test_save_and_list(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(db_path=os.path.join(td, "test.db"))
            store.save_run(self._make_result("r1", "wf_a"))
            store.save_run(self._make_result("r2", "wf_b"))
            runs = store.list_runs()
            assert len(runs) == 2

    def test_get_run_detail(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(db_path=os.path.join(td, "test.db"))
            store.save_run(self._make_result("r1"))
            detail = store.get_run("r1")
            assert detail is not None
            assert detail["run_id"] == "r1"
            assert len(detail["steps"]) == 2
            assert detail["steps"][0]["step_id"] in ("s1", "s2")

    def test_get_nonexistent(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(db_path=os.path.join(td, "test.db"))
            assert store.get_run("missing") is None

    def test_filter_by_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(db_path=os.path.join(td, "test.db"))
            store.save_run(self._make_result("r1", "wf_a"))
            store.save_run(self._make_result("r2", "wf_b"))
            store.save_run(self._make_result("r3", "wf_a"))
            runs = store.list_runs(workflow="wf_a")
            assert len(runs) == 2

    def test_stats(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(db_path=os.path.join(td, "test.db"))
            store.save_run(self._make_result("r1", success=True))
            store.save_run(self._make_result("r2", success=False))
            store.save_run(self._make_result("r3", success=True))
            stats = store.get_stats()
            assert stats["total_runs"] == 3
            assert stats["successful"] == 2
            assert stats["failed"] == 1

    def test_cleanup(self):
        """2026-04-24: Windows 的 `datetime.now(tz).isoformat()` 分辨率 ~15ms
        (低于 Linux 的 us 级), 全套 pytest 跑到这时 Python/SQLite 都热了,
        `save_run` 和 `cleanup(older_than_days=0)` 可能落在同一 isoformat()
        字符串 → `started_at < cutoff` (严格 <) 为 False → 行不删除 → len>0 fail。
        单独跑时 cold start 让 save/cleanup 跨毫秒所以 pass。

        用 `older_than_days=-1` 让 cutoff = now + 1 day, started_at 永远 < cutoff,
        无时钟竞争 (也避免 time.sleep 引入非确定性)。test 意图 "cleanup 能删全
        部历史" 保持。
        """
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(db_path=os.path.join(td, "test.db"))
            store.save_run(self._make_result("r1"))
            store.cleanup(older_than_days=-1)
            runs = store.list_runs()
            assert len(runs) == 0


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------

class TestMetricsCollector:
    def test_counter(self):
        mc = MetricsCollector()
        mc.inc("requests_total")
        mc.inc("requests_total")
        mc.inc("requests_total", 3)
        assert mc.get_counter("requests_total") == 5

    def test_counter_with_labels(self):
        mc = MetricsCollector()
        mc.inc("actions_total", platform="telegram", status="ok")
        mc.inc("actions_total", platform="telegram", status="ok")
        mc.inc("actions_total", platform="linkedin", status="ok")
        assert mc.get_counter("actions_total", platform="telegram", status="ok") == 2
        assert mc.get_counter("actions_total", platform="linkedin", status="ok") == 1

    def test_gauge(self):
        mc = MetricsCollector()
        mc.gauge("devices_online", 3)
        assert mc.get_gauge("devices_online") == 3
        mc.gauge("devices_online", 5)
        assert mc.get_gauge("devices_online") == 5

    def test_histogram(self):
        mc = MetricsCollector()
        for val in [0.1, 0.5, 1.0, 2.5, 5.0]:
            mc.observe("latency_sec", val)
        snap = mc.snapshot()
        h = snap["histograms"]["latency_sec"]
        assert h["count"] == 5
        assert h["avg"] > 0

    def test_snapshot(self):
        mc = MetricsCollector()
        mc.inc("x")
        mc.gauge("y", 42)
        mc.observe("z", 1.0)
        snap = mc.snapshot()
        assert "uptime_sec" in snap
        assert "counters" in snap
        assert "gauges" in snap
        assert "histograms" in snap

    def test_prometheus_format(self):
        mc = MetricsCollector()
        mc.inc("http_requests_total", method="GET", path="/api")
        mc.gauge("temperature", 23.5)
        text = mc.prometheus()
        assert "http_requests_total" in text
        assert "temperature" in text
        assert "# TYPE" in text

    def test_rate_per_minute(self):
        mc = MetricsCollector()
        for _ in range(10):
            mc.inc("actions")
        rate = mc.rate_per_minute("actions", window_sec=60)
        assert rate > 0

    def test_reset(self):
        mc = MetricsCollector()
        mc.inc("x")
        mc.gauge("y", 1)
        mc.reset()
        assert mc.get_counter("x") == 0
        assert mc.get_gauge("y") == 0

    def test_thread_safety(self):
        mc = MetricsCollector()
        threads = []
        for _ in range(10):
            t = threading.Thread(target=lambda: [mc.inc("c") for _ in range(100)])
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        assert mc.get_counter("c") == 1000


class TestHistogramBucket:
    def test_observe(self):
        h = _HistogramBucket()
        h.observe(0.05)
        h.observe(0.3)
        h.observe(100)
        assert h.count == 3
        assert h.total > 0

    def test_bucket_distribution(self):
        h = _HistogramBucket()
        h.observe(0.05)  # <= 0.1
        h.observe(0.2)   # <= 0.25
        h.observe(100)   # > 60.0 → +Inf
        assert h.counts[0] == 1  # <= 0.1
        assert h.counts[1] == 1  # <= 0.25
        assert h.counts[-1] == 1  # +Inf


# ---------------------------------------------------------------------------
# AlertManager
# ---------------------------------------------------------------------------

class TestAlertManager:
    def test_add_and_evaluate_rule(self):
        am = AlertManager()
        mc = MetricsCollector()
        mc.gauge("error_rate", 0.5)

        am.add_rule(AlertRule(
            name="high_errors",
            description="Error rate > 40%",
            check=lambda m: m.get_gauge("error_rate") > 0.4,
            severity=AlertSeverity.CRITICAL,
        ))

        events = am.evaluate(mc)
        assert len(events) == 1
        assert events[0].state == "firing"
        assert events[0].severity == "critical"

    def test_alert_resolves(self):
        am = AlertManager()
        mc = MetricsCollector()

        am.add_rule(AlertRule(
            name="test_alert",
            check=lambda m: m.get_gauge("x") > 10,
            cooldown_sec=0,
        ))

        mc.gauge("x", 20)
        events = am.evaluate(mc)
        assert len(events) == 1
        assert events[0].state == "firing"

        mc.gauge("x", 5)
        events = am.evaluate(mc)
        assert len(events) == 1
        assert events[0].state == "resolved"

    def test_cooldown(self):
        am = AlertManager()
        mc = MetricsCollector()
        mc.gauge("x", 100)

        am.add_rule(AlertRule(
            name="cooldown_test",
            check=lambda m: m.get_gauge("x") > 10,
            cooldown_sec=999,
        ))

        events1 = am.evaluate(mc)
        assert len(events1) == 1

        # Resolve and try to re-fire — should be cooled down
        mc.gauge("x", 0)
        am.evaluate(mc)  # resolve
        mc.gauge("x", 100)
        events2 = am.evaluate(mc)
        assert len(events2) == 0

    def test_handler_called(self):
        am = AlertManager()
        mc = MetricsCollector()
        mc.gauge("x", 100)
        received = []

        am.add_rule(AlertRule(
            name="handler_test",
            check=lambda m: m.get_gauge("x") > 10,
        ))
        am.add_handler(lambda e: received.append(e.rule_name))
        am.evaluate(mc)
        assert "handler_test" in received

    def test_active_alerts(self):
        am = AlertManager()
        mc = MetricsCollector()
        mc.gauge("x", 100)

        am.add_rule(AlertRule(name="a1", check=lambda m: m.get_gauge("x") > 10))
        am.add_rule(AlertRule(name="a2", check=lambda m: m.get_gauge("x") > 200))
        am.evaluate(mc)
        active = am.get_active_alerts()
        assert len(active) == 1
        assert active[0]["name"] == "a1"

    def test_alert_history(self):
        am = AlertManager()
        mc = MetricsCollector()
        mc.gauge("x", 100)
        am.add_rule(AlertRule(name="hist", check=lambda m: m.get_gauge("x") > 10))
        am.evaluate(mc)
        history = am.get_alert_history()
        assert len(history) == 1

    def test_remove_rule(self):
        am = AlertManager()
        am.add_rule(AlertRule(name="r1", check=lambda m: False))
        assert am.remove_rule("r1")
        assert not am.remove_rule("r1")

    def test_default_rules(self):
        rules = create_default_rules()
        assert len(rules) >= 3
        names = {r.name for r in rules}
        assert "high_error_rate" in names


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

class TestSecureStore:
    def test_set_and_get(self):
        with tempfile.TemporaryDirectory() as td:
            store = SecureStore(
                store_path=os.path.join(td, "secrets.enc"),
                key_path=os.path.join(td, "key.key"),
            )
            store.set("api_key", "sk-test-123456")
            assert store.get("api_key") == "sk-test-123456"

    def test_get_default(self):
        with tempfile.TemporaryDirectory() as td:
            store = SecureStore(
                store_path=os.path.join(td, "secrets.enc"),
                key_path=os.path.join(td, "key.key"),
            )
            assert store.get("missing", "default") == "default"

    def test_delete(self):
        with tempfile.TemporaryDirectory() as td:
            store = SecureStore(
                store_path=os.path.join(td, "secrets.enc"),
                key_path=os.path.join(td, "key.key"),
            )
            store.set("x", "y")
            assert store.delete("x")
            assert not store.has("x")

    def test_list_keys(self):
        with tempfile.TemporaryDirectory() as td:
            store = SecureStore(
                store_path=os.path.join(td, "secrets.enc"),
                key_path=os.path.join(td, "key.key"),
            )
            store.set("a", "1")
            store.set("b", "2")
            keys = store.list_keys()
            assert "a" in keys
            assert "b" in keys

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            sp = os.path.join(td, "secrets.enc")
            kp = os.path.join(td, "key.key")
            store1 = SecureStore(store_path=sp, key_path=kp)
            store1.set("persistent", "value123")
            store2 = SecureStore(store_path=sp, key_path=kp)
            assert store2.get("persistent") == "value123"


class TestConfigValidator:
    def test_valid_compliance(self):
        config = {
            "telegram": {"actions": {"send_message": {"hourly": 10, "daily": 50, "cooldown_sec": 5}}},
            "linkedin": {"actions": {"send_message": {"hourly": 5, "daily": 20, "cooldown_sec": 30}}},
            "whatsapp": {"actions": {"send_message": {"hourly": 15, "daily": 60, "cooldown_sec": 5}}},
        }
        errors = ConfigValidator.validate_compliance(config)
        assert len(errors) == 0

    def test_missing_platform(self):
        config = {"telegram": {"actions": {}}}
        errors = ConfigValidator.validate_compliance(config)
        assert any("linkedin" in e for e in errors)

    def test_valid_ai(self):
        config = {"llm": {"provider": "deepseek"}}
        errors = ConfigValidator.validate_ai(config)
        assert len(errors) == 0

    def test_invalid_ai_provider(self):
        config = {"llm": {"provider": "unknown_provider"}}
        errors = ConfigValidator.validate_ai(config)
        assert len(errors) >= 1


class TestSanitization:
    def test_api_key_masked(self):
        text = "Using key ${OPENAI_API_KEY}"
        result = sanitize(text)
        assert "sk-abc" not in result
        assert "REDACTED" in result

    def test_phone_masked(self):
        text = "Contact: +63-912-345-6789"
        result = sanitize(text)
        assert "912" not in result
        assert "PHONE" in result

    def test_bearer_masked(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6"
        result = sanitize(text)
        assert "eyJh" not in result
        assert "REDACTED" in result

    def test_sanitize_dict(self):
        d = {
            "api_key": "sk-secret123",
            "username": "alice",
            "password": "hunter2",
            "data": "normal text",
        }
        result = sanitize_dict(d)
        assert result["api_key"] == "***REDACTED***"
        assert result["password"] == "***REDACTED***"
        assert result["username"] == "alice"
        assert result["data"] == "normal text"

    def test_nested_sanitize(self):
        d = {"config": {"token": "abc123secret"}}
        result = sanitize_dict(d)
        assert result["config"]["token"] == "***REDACTED***"
