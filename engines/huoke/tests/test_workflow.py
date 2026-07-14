"""
Tests for Workflow Engine, ActionRegistry, EventBus, and Smart Scheduling.

All tests run without real devices or LLM API keys.
"""

import os
import sys
import time
import threading
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.workflow.actions import ActionRegistry, get_action_registry
from src.workflow.engine import (
    WorkflowDef, WorkflowExecutor, WorkflowResult,
    StepDef, StepStatus, ExecutionContext, ErrorPolicy,
    evaluate_condition,
)
from src.workflow.event_bus import EventBus, Event, Subscription
from src.workflow.smart_schedule import (
    SmartScheduleConfig, ActivityWindow, WeekendConfig,
    check_smart_constraints, next_available_time, get_rate_multiplier,
)


# ---------------------------------------------------------------------------
# ActionRegistry
# ---------------------------------------------------------------------------

class TestActionRegistry:
    def test_register_and_get(self):
        reg = ActionRegistry()
        fn = lambda x=0: x + 1
        reg.register("test.add_one", fn, {"desc": "adds one"})
        result = reg.get("test.add_one")
        assert result is not None
        assert result[0](5) == 6

    def test_has(self):
        reg = ActionRegistry()
        reg.register("test.exists", lambda: True)
        assert reg.has("test.exists")
        assert not reg.has("test.missing")

    def test_list_actions(self):
        reg = ActionRegistry()
        reg.register("telegram.send", lambda: None)
        reg.register("telegram.search", lambda: None)
        reg.register("linkedin.connect", lambda: None)
        assert reg.list_actions("telegram") == ["telegram.search", "telegram.send"]
        assert len(reg.list_actions()) == 3

    def test_list_by_platform(self):
        reg = ActionRegistry()
        reg.register("telegram.a", lambda: None)
        reg.register("telegram.b", lambda: None)
        reg.register("linkedin.c", lambda: None)
        by_plat = reg.list_by_platform()
        assert "telegram" in by_plat
        assert len(by_plat["telegram"]) == 2

    def test_register_module(self):
        reg = ActionRegistry()

        class FakeModule:
            def method_a(self): return "a"
            def method_b(self): return "b"

        obj = FakeModule()
        reg.register_module("fake", obj, ["method_a", "method_b"])
        assert reg.has("fake.method_a")
        assert reg.has("fake.method_b")
        fn, _ = reg.get("fake.method_a")
        assert fn() == "a"

    def test_unregister(self):
        reg = ActionRegistry()
        reg.register("test.x", lambda: None)
        assert reg.unregister("test.x")
        assert not reg.has("test.x")
        assert not reg.unregister("test.missing")

    def test_clear(self):
        reg = ActionRegistry()
        reg.register("a", lambda: None)
        reg.register("b", lambda: None)
        reg.clear()
        assert reg.count == 0

    def test_singleton_has_builtins(self):
        reg = get_action_registry()
        assert reg.has("util.log")
        assert reg.has("util.wait")
        assert reg.has("util.set_variable")
        assert reg.has("compliance.check_remaining")


# ---------------------------------------------------------------------------
# ExecutionContext
# ---------------------------------------------------------------------------

class TestExecutionContext:
    def test_variable_resolution(self):
        ctx = ExecutionContext({"name": "Alice", "count": 5})
        assert ctx.resolve("{variables.name}") == "Alice"
        assert ctx.resolve("{variables.count}") == 5

    def test_nested_dict_resolution(self):
        ctx = ExecutionContext({"user": {"name": "Bob", "age": 30}})
        assert ctx.resolve("{variables.user.name}") == "Bob"
        assert ctx.resolve("{variables.user.age}") == 30

    def test_step_result_resolution(self):
        ctx = ExecutionContext()
        from src.workflow.engine import StepResult
        ctx.set_step_result("s1", StepResult(
            step_id="s1", status=StepStatus.SUCCESS, result={"value": 42},
        ))
        assert ctx.resolve("{steps.s1.result.value}") == 42
        assert ctx.resolve("{steps.s1.status}") == "success"

    def test_item_resolution(self):
        ctx = ExecutionContext()
        ctx.current_item = {"name": "Charlie", "company": "Acme"}
        ctx.current_index = 2
        assert ctx.resolve("{item.name}") == "Charlie"
        assert ctx.resolve("{index}") == 2

    def test_mixed_string_interpolation(self):
        ctx = ExecutionContext({"platform": "telegram", "count": 3})
        result = ctx.resolve("Sent {variables.count} messages on {variables.platform}")
        assert result == "Sent 3 messages on telegram"

    def test_unresolved_reference(self):
        ctx = ExecutionContext()
        result = ctx.resolve("{variables.missing}")
        assert result == "{variables.missing}"

    def test_dict_resolution(self):
        ctx = ExecutionContext({"x": "hello"})
        result = ctx.resolve({"key": "{variables.x}", "static": 42})
        assert result == {"key": "hello", "static": 42}

    def test_list_resolution(self):
        ctx = ExecutionContext({"a": "A", "b": "B"})
        result = ctx.resolve(["{variables.a}", "{variables.b}", "C"])
        assert result == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Condition Evaluation
# ---------------------------------------------------------------------------

class TestConditionEvaluation:
    def test_empty_condition(self):
        ctx = ExecutionContext()
        assert evaluate_condition("", ctx) is True

    def test_true_string(self):
        ctx = ExecutionContext()
        assert evaluate_condition("true", ctx) is True
        assert evaluate_condition("True", ctx) is True

    def test_false_string(self):
        ctx = ExecutionContext()
        assert evaluate_condition("false", ctx) is False

    def test_numeric_comparison(self):
        ctx = ExecutionContext()
        assert evaluate_condition("5 > 3", ctx) is True
        assert evaluate_condition("2 > 10", ctx) is False

    def test_resolved_variable(self):
        ctx = ExecutionContext({"ok": "true"})
        assert evaluate_condition("{variables.ok}", ctx) is True


# ---------------------------------------------------------------------------
# StepDef
# ---------------------------------------------------------------------------

class TestStepDef:
    def test_from_dict_minimal(self):
        s = StepDef.from_dict({"id": "s1", "action": "util.log"})
        assert s.id == "s1"
        assert s.action == "util.log"
        assert s.retry == 0
        assert s.on_error == "abort"

    def test_from_dict_full(self):
        s = StepDef.from_dict({
            "id": "s2",
            "action": "telegram.send",
            "params": {"msg": "hello"},
            "condition": "true",
            "on_error": "continue",
            "retry": 2,
            "for_each": "targets",
            "depends_on": ["s1"],
            "delay_after": 3.0,
        })
        assert s.retry == 2
        assert s.for_each == "targets"
        assert s.depends_on == ["s1"]
        assert s.delay_after == 3.0

    def test_depends_on_string_to_list(self):
        s = StepDef.from_dict({"id": "s1", "action": "x", "depends_on": "other"})
        assert s.depends_on == ["other"]


# ---------------------------------------------------------------------------
# WorkflowDef
# ---------------------------------------------------------------------------

class TestWorkflowDef:
    def test_from_dict(self):
        wf = WorkflowDef.from_dict({
            "name": "test_wf",
            "description": "A test workflow",
            "variables": {"x": 1},
            "steps": [
                {"id": "s1", "action": "util.log", "params": {"message": "hello"}},
                {"id": "s2", "action": "util.log", "depends_on": ["s1"]},
            ],
        })
        assert wf.name == "test_wf"
        assert len(wf.steps) == 2
        assert wf.steps[1].depends_on == ["s1"]

    def test_from_yaml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write("""
name: yaml_test
variables:
  greeting: hello
steps:
  - id: log_it
    action: util.log
    params:
      message: "{variables.greeting} world"
""")
            f.flush()
            path = f.name

        try:
            wf = WorkflowDef.from_yaml(path)
            assert wf.name == "yaml_test"
            assert wf.variables["greeting"] == "hello"
            assert len(wf.steps) == 1
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# WorkflowExecutor
# ---------------------------------------------------------------------------

class TestWorkflowExecutor:
    def _make_registry(self):
        reg = ActionRegistry()
        reg.register("util.log", lambda message="", **kw: True)
        reg.register("util.wait", lambda seconds=0, **kw: True)
        reg.register("util.set_variable", lambda key="", value=None, **kw: value)
        reg.register("test.echo", lambda text="", **kw: text)
        reg.register("test.add", lambda a=0, b=0, **kw: a + b)
        reg.register("test.fail", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        return reg

    def test_simple_workflow(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "simple",
            "steps": [
                {"id": "s1", "action": "util.log", "params": {"message": "step1"}},
                {"id": "s2", "action": "test.echo", "params": {"text": "hello"}},
            ],
        })
        result = executor.run(wf)
        assert result.success
        assert result.steps["s1"]["status"] == "success"
        assert result.steps["s2"]["result"] == "hello"

    def test_variable_interpolation(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "vars",
            "variables": {"greeting": "hi"},
            "steps": [
                {"id": "s1", "action": "test.echo", "params": {"text": "{variables.greeting}"}},
            ],
        })
        result = executor.run(wf)
        assert result.steps["s1"]["result"] == "hi"

    def test_initial_vars_override(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "override",
            "variables": {"x": "default"},
            "steps": [
                {"id": "s1", "action": "test.echo", "params": {"text": "{variables.x}"}},
            ],
        })
        result = executor.run(wf, initial_vars={"x": "overridden"})
        assert result.steps["s1"]["result"] == "overridden"

    def test_dependency_ordering(self):
        reg = self._make_registry()
        order = []
        reg.register("test.track", lambda step_id="", **kw: order.append(step_id) or step_id)
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "deps",
            "steps": [
                {"id": "c", "action": "test.track", "params": {"step_id": "c"}, "depends_on": ["a", "b"]},
                {"id": "a", "action": "test.track", "params": {"step_id": "a"}},
                {"id": "b", "action": "test.track", "params": {"step_id": "b"}, "depends_on": ["a"]},
            ],
        })
        result = executor.run(wf)
        assert result.success
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    def test_condition_skip(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "conditional",
            "steps": [
                {"id": "s1", "action": "test.echo", "params": {"text": "ok"}, "condition": "false"},
                {"id": "s2", "action": "test.echo", "params": {"text": "runs"}},
            ],
        })
        result = executor.run(wf)
        assert result.success
        assert result.steps["s1"]["status"] == "skipped"
        assert result.steps["s2"]["status"] == "success"

    def test_for_each(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "loop",
            "variables": {"items": ["a", "b", "c"]},
            "steps": [
                {"id": "process", "action": "test.echo",
                 "params": {"text": "{item}"},
                 "for_each": "items", "on_error": "continue"},
            ],
        })
        result = executor.run(wf)
        assert result.success
        items_result = result.steps["process"]["result"]
        assert items_result["total"] == 3
        assert items_result["success_count"] == 3

    def test_error_abort(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "abort_test",
            "steps": [
                {"id": "s1", "action": "test.fail", "on_error": "abort"},
                {"id": "s2", "action": "test.echo", "params": {"text": "never"}},
            ],
        })
        result = executor.run(wf)
        assert not result.success
        assert result.aborted
        assert result.steps["s1"]["status"] == "failed"
        assert result.steps["s2"]["status"] == "cancelled"

    def test_error_continue(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "continue_test",
            "steps": [
                {"id": "s1", "action": "test.fail", "on_error": "continue"},
                {"id": "s2", "action": "test.echo", "params": {"text": "after_error"}},
            ],
        })
        result = executor.run(wf)
        assert not result.success  # s1 failed
        assert not result.aborted
        assert result.steps["s2"]["status"] == "success"

    def test_retry(self):
        reg = self._make_registry()
        call_count = [0]

        def flaky(**kw):
            call_count[0] += 1
            if call_count[0] < 3:
                raise RuntimeError("not yet")
            return "finally"

        reg.register("test.flaky", flaky)
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "retry_test",
            "steps": [
                {"id": "s1", "action": "test.flaky", "retry": 3},
            ],
        })
        result = executor.run(wf)
        assert result.success
        assert result.steps["s1"]["retries_used"] == 2

    def test_step_result_chaining(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "chain",
            "steps": [
                {"id": "s1", "action": "test.echo", "params": {"text": "world"}},
                {"id": "s2", "action": "test.echo",
                 "params": {"text": "hello {steps.s1.result}"},
                 "depends_on": ["s1"]},
            ],
        })
        result = executor.run(wf)
        assert result.steps["s2"]["result"] == "hello world"

    def test_on_step_complete_callback(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        completed = []

        def on_complete(step_id, step_result):
            completed.append(step_id)

        wf = WorkflowDef.from_dict({
            "name": "cb_test",
            "steps": [
                {"id": "a", "action": "util.log"},
                {"id": "b", "action": "util.log"},
            ],
        })
        executor.run(wf, on_step_complete=on_complete)
        assert completed == ["a", "b"]

    def test_workflow_result_summary(self):
        reg = self._make_registry()
        executor = WorkflowExecutor(reg)
        wf = WorkflowDef.from_dict({
            "name": "summary_test",
            "steps": [
                {"id": "s1", "action": "util.log"},
            ],
        })
        result = executor.run(wf)
        assert "SUCCESS" in result.summary
        assert "summary_test" in result.summary


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class TestEventBus:
    def test_publish_subscribe(self):
        bus = EventBus()
        received = []
        bus.on("test.event", lambda e: received.append(e.data))
        bus.emit(Event(type="test.event", data={"msg": "hello"}), synchronous=True)
        assert len(received) == 1
        assert received[0]["msg"] == "hello"

    def test_pattern_matching(self):
        bus = EventBus()
        received = []
        bus.on("telegram.*", lambda e: received.append(e.type))
        bus.emit(Event(type="telegram.message_sent"), synchronous=True)
        bus.emit(Event(type="telegram.message_received"), synchronous=True)
        bus.emit(Event(type="linkedin.connection"), synchronous=True)
        assert len(received) == 2

    def test_once_subscription(self):
        bus = EventBus()
        received = []
        bus.once("test.once", lambda e: received.append(1))
        bus.emit(Event(type="test.once"), synchronous=True)
        bus.emit(Event(type="test.once"), synchronous=True)
        assert len(received) == 1

    def test_unsubscribe(self):
        bus = EventBus()
        received = []
        sub_id = bus.on("test.unsub", lambda e: received.append(1))
        bus.emit(Event(type="test.unsub"), synchronous=True)
        bus.off(sub_id)
        bus.emit(Event(type="test.unsub"), synchronous=True)
        assert len(received) == 1

    def test_emit_simple(self):
        bus = EventBus()
        received = []
        bus.on("simple.test", lambda e: received.append(e.data), )
        bus.emit_simple("simple.test", source="unit_test", key="value")
        time.sleep(0.1)  # async handler
        assert len(received) == 1
        assert received[0]["key"] == "value"

    def test_recent_events(self):
        bus = EventBus()
        for i in range(5):
            bus.emit(Event(type=f"test.event_{i}"), synchronous=True)
        events = bus.recent_events()
        assert len(events) == 5

    def test_recent_events_pattern_filter(self):
        bus = EventBus()
        bus.emit(Event(type="telegram.msg"), synchronous=True)
        bus.emit(Event(type="linkedin.msg"), synchronous=True)
        bus.emit(Event(type="telegram.sent"), synchronous=True)
        events = bus.recent_events(pattern="telegram.*")
        assert len(events) == 2

    def test_active_subscriptions(self):
        bus = EventBus()
        bus.on("a.*", lambda e: None)
        bus.on("b.*", lambda e: None)
        subs = bus.active_subscriptions()
        assert len(subs) == 2

    def test_error_in_handler_doesnt_crash(self):
        bus = EventBus()
        bus.on("error.test", lambda e: 1/0)
        bus.emit(Event(type="error.test"), synchronous=True)
        # Should not raise

    def test_thread_safety(self):
        bus = EventBus()
        count = [0]
        lock = threading.Lock()

        def handler(e):
            with lock:
                count[0] += 1

        bus.on("stress.*", handler)
        threads = []
        for i in range(50):
            t = threading.Thread(target=lambda i=i: bus.emit(
                Event(type=f"stress.event_{i}"), synchronous=True))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        assert count[0] == 50


# ---------------------------------------------------------------------------
# Smart Scheduling
# ---------------------------------------------------------------------------

class TestActivityWindow:
    def test_within_window(self):
        from datetime import datetime
        w = ActivityWindow(start_hour=9, end_hour=17)
        dt = datetime(2026, 3, 12, 12, 0)
        assert w.contains(dt)

    def test_outside_window(self):
        from datetime import datetime
        w = ActivityWindow(start_hour=9, end_hour=17)
        dt = datetime(2026, 3, 12, 23, 0)
        assert not w.contains(dt)

    def test_edge_start(self):
        from datetime import datetime
        w = ActivityWindow(start_hour=9, end_hour=17)
        dt = datetime(2026, 3, 12, 9, 0)
        assert w.contains(dt)

    def test_overnight_window(self):
        from datetime import datetime
        w = ActivityWindow(start_hour=22, end_hour=6)
        assert w.contains(datetime(2026, 3, 12, 23, 0))
        assert w.contains(datetime(2026, 3, 12, 3, 0))
        assert not w.contains(datetime(2026, 3, 12, 12, 0))


class TestSmartScheduleConfig:
    def test_from_dict(self):
        cfg = SmartScheduleConfig.from_dict({
            "timezone": "Asia/Manila",
            "activity_window": {"start_hour": 8, "end_hour": 22},
            "jitter_minutes": 10,
            "blackout_dates": ["2026-01-01"],
        })
        assert cfg.timezone == "Asia/Manila"
        assert cfg.activity_window.start_hour == 8
        assert cfg.jitter_minutes == 10
        assert "2026-01-01" in cfg.blackout_dates

    def test_default_config(self):
        cfg = SmartScheduleConfig()
        assert cfg.timezone == "UTC"
        assert cfg.activity_window.start_hour == 8


class TestSmartConstraints:
    def test_blackout_date(self):
        from datetime import datetime, timezone as tz
        cfg = SmartScheduleConfig(
            timezone="UTC",
            blackout_dates=[datetime.now(tz.utc).strftime("%Y-%m-%d")],
        )
        allowed, reason, _ = check_smart_constraints(cfg)
        assert not allowed
        assert "blackout" in reason

    def test_daily_limit(self):
        cfg = SmartScheduleConfig(timezone="UTC", max_daily_runs=5)
        allowed, reason, _ = check_smart_constraints(cfg, daily_run_count=5)
        assert not allowed
        assert "daily limit" in reason

    def test_jitter_range(self):
        cfg = SmartScheduleConfig(timezone="UTC", jitter_minutes=10)
        for _ in range(20):
            allowed, _, jitter = check_smart_constraints(cfg)
            if allowed:
                assert 0 <= jitter <= 600

    def test_rate_multiplier(self):
        cfg = SmartScheduleConfig(timezone="UTC")
        mult = get_rate_multiplier(cfg)
        assert 0 < mult <= 1.0

    def test_next_available(self):
        cfg = SmartScheduleConfig(timezone="UTC")
        nxt = next_available_time(cfg)
        assert nxt is not None


class TestWeekendConfig:
    def test_defaults(self):
        w = WeekendConfig()
        assert w.enabled is True
        assert w.rate_multiplier == 0.5
        assert w.window.start_hour == 10
