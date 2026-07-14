"""
Workflow Engine — YAML-defined multi-step automation orchestrator.

Supports:
- Sequential, conditional, loop, and parallel step execution
- Variable interpolation ({variables.x}, {steps.id.result})
- for_each iteration over lists
- Error handling per step (retry, continue, abort)
- Execution state tracking for observability
- Integration with ActionRegistry for action dispatch

Workflow YAML structure:
    name: my_workflow
    description: ...
    variables:
      key: value
    steps:
      - id: step1
        action: platform.method
        params: {key: value}
        condition: "expression"
        on_error: continue|abort|retry
        retry: 2
        for_each: variable_name
        depends_on: [step_id, ...]
"""

from __future__ import annotations

import copy
import logging
import re
import time
import traceback
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .actions import ActionRegistry, get_action_registry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ErrorPolicy(str, Enum):
    ABORT = "abort"
    CONTINUE = "continue"
    RETRY = "retry"


@dataclass
class StepDef:
    id: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    condition: str = ""
    on_error: str = "abort"
    retry: int = 0
    for_each: str = ""
    depends_on: List[str] = field(default_factory=list)
    delay_after: float = 0.0
    timeout_sec: float = 300.0

    @staticmethod
    def from_dict(d: dict) -> StepDef:
        deps = d.get("depends_on", [])
        if isinstance(deps, str):
            deps = [deps]
        return StepDef(
            id=d["id"],
            action=d["action"],
            params=d.get("params", {}),
            condition=d.get("condition", ""),
            on_error=d.get("on_error", "abort"),
            retry=d.get("retry", 0),
            for_each=d.get("for_each", ""),
            depends_on=deps,
            delay_after=d.get("delay_after", 0.0),
            timeout_sec=d.get("timeout_sec", 300.0),
        )


@dataclass
class WorkflowDef:
    name: str
    description: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    steps: List[StepDef] = field(default_factory=list)
    on_complete: str = ""
    max_parallel: int = 3

    @staticmethod
    def from_dict(d: dict) -> WorkflowDef:
        return WorkflowDef(
            name=d.get("name", "unnamed"),
            description=d.get("description", ""),
            variables=d.get("variables", {}),
            steps=[StepDef.from_dict(s) for s in d.get("steps", [])],
            on_complete=d.get("on_complete", ""),
            max_parallel=d.get("max_parallel", 3),
        )

    @staticmethod
    def from_yaml(path: str) -> WorkflowDef:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return WorkflowDef.from_dict(data)


# ---------------------------------------------------------------------------
# Execution Context
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    result: Any = None
    error: str = ""
    duration_sec: float = 0.0
    retries_used: int = 0
    iteration: int = -1


class ExecutionContext:
    """
    Carries state through workflow execution.
    Provides variable interpolation and step result access.
    """

    def __init__(self, variables: Optional[Dict[str, Any]] = None):
        self.run_id: str = uuid.uuid4().hex[:12]
        self.variables: Dict[str, Any] = dict(variables or {})
        self.step_results: Dict[str, StepResult] = {}
        self.current_item: Any = None
        self.current_index: int = -1
        self._start_time: float = time.time()

    def set_step_result(self, step_id: str, result: StepResult):
        self.step_results[step_id] = result

    def get_step_result(self, step_id: str) -> Optional[StepResult]:
        return self.step_results.get(step_id)

    def resolve(self, value: Any) -> Any:
        """Recursively resolve variable references in a value."""
        if isinstance(value, str):
            return self._resolve_string(value)
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def _resolve_string(self, s: str) -> Any:
        """
        Resolve {variables.x}, {steps.id.result}, {item}, {item.field}, {index}.
        If the entire string is a single reference, return the raw value (not stringified).
        """
        single_ref = re.fullmatch(r'\{([^}]+)\}', s)
        if single_ref:
            val = self._lookup(single_ref.group(1))
            if val is not None:
                return val

        def replacer(m):
            val = self._lookup(m.group(1))
            return str(val) if val is not None else m.group(0)

        return re.sub(r'\{([^}]+)\}', replacer, s)

    def _lookup(self, path: str) -> Any:
        parts = path.split(".")
        if not parts:
            return None

        if parts[0] == "variables" and len(parts) >= 2:
            return self._deep_get(self.variables, parts[1:])

        if parts[0] == "steps" and len(parts) >= 3:
            step_id = parts[1]
            sr = self.step_results.get(step_id)
            if sr and parts[2] == "result":
                if len(parts) > 3:
                    return self._deep_get(sr.result, parts[3:]) if isinstance(sr.result, dict) else None
                return sr.result
            if sr and parts[2] == "status":
                return sr.status.value

        if parts[0] == "item":
            if len(parts) == 1:
                return self.current_item
            if isinstance(self.current_item, dict):
                return self._deep_get(self.current_item, parts[1:])

        if parts[0] == "index":
            return self.current_index

        if parts[0] == "run_id":
            return self.run_id

        return None

    @staticmethod
    def _deep_get(obj: Any, keys: List[str]) -> Any:
        for k in keys:
            if isinstance(obj, dict):
                obj = obj.get(k)
            elif isinstance(obj, (list, tuple)) and k.isdigit():
                idx = int(k)
                obj = obj[idx] if 0 <= idx < len(obj) else None
            else:
                return None
            if obj is None:
                return None
        return obj

    @property
    def elapsed_sec(self) -> float:
        return time.time() - self._start_time


# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------

def evaluate_condition(expr: str, ctx: ExecutionContext) -> bool:
    """
    Evaluate a simple condition expression.
    Supports: result.field > N, result == "value", steps.id.status == "success"
    """
    if not expr or not expr.strip():
        return True

    resolved = ctx.resolve(expr)
    if isinstance(resolved, bool):
        return resolved
    if isinstance(resolved, str):
        resolved_str = resolved.strip().lower()
        if resolved_str in ("true", "1", "yes"):
            return True
        if resolved_str in ("false", "0", "no"):
            return False

    try:
        # Safe subset: only allow comparison operators
        safe_expr = str(resolved)
        allowed_chars = set("0123456789.+-*/><= !\"'truefalsnonTFN")
        if all(c in allowed_chars or c.isalnum() or c.isspace() for c in safe_expr):
            return bool(eval(safe_expr, {"__builtins__": {}}, {}))
    except Exception:
        pass

    return bool(resolved)


# ---------------------------------------------------------------------------
# Workflow Executor
# ---------------------------------------------------------------------------

class WorkflowExecutor:
    """
    Executes a WorkflowDef using the ActionRegistry.

    Usage:
        executor = WorkflowExecutor()
        result = executor.run(workflow_def, initial_vars={"device_id": "abc"})
    """

    def __init__(self, registry: Optional[ActionRegistry] = None):
        self.registry = registry or get_action_registry()

    def run(self, workflow: WorkflowDef,
            initial_vars: Optional[Dict[str, Any]] = None,
            on_step_complete: Optional[Callable] = None) -> WorkflowResult:
        """
        Execute a workflow definition.

        Args:
            workflow: parsed WorkflowDef
            initial_vars: override/merge with workflow.variables
            on_step_complete: callback(step_id, StepResult) for monitoring

        Returns: WorkflowResult with all step outcomes
        """
        merged_vars = {**workflow.variables}
        if initial_vars:
            merged_vars.update(initial_vars)

        ctx = ExecutionContext(merged_vars)
        log.info("Workflow '%s' started (run=%s)", workflow.name, ctx.run_id)

        tracker = get_workflow_tracker()
        tracker.start_run(ctx.run_id, workflow.name,
                          len(workflow.steps), merged_vars)

        execution_order = self._topological_sort(workflow.steps)
        completed_steps = set()
        aborted = False

        for step_id in execution_order:
            if aborted:
                ctx.set_step_result(step_id, StepResult(
                    step_id=step_id, status=StepStatus.CANCELLED,
                ))
                continue

            step = next(s for s in workflow.steps if s.id == step_id)

            # Check dependencies
            deps_ok = all(
                ctx.get_step_result(dep) and
                ctx.get_step_result(dep).status == StepStatus.SUCCESS
                for dep in step.depends_on
            )
            if not deps_ok:
                log.info("Step '%s' skipped (dependency not met)", step_id)
                ctx.set_step_result(step_id, StepResult(
                    step_id=step_id, status=StepStatus.SKIPPED,
                    error="dependency_not_met",
                ))
                if step.on_error == ErrorPolicy.ABORT.value:
                    aborted = True
                continue

            # Check condition
            if step.condition and not evaluate_condition(step.condition, ctx):
                log.info("Step '%s' skipped (condition false)", step_id)
                ctx.set_step_result(step_id, StepResult(
                    step_id=step_id, status=StepStatus.SKIPPED,
                    error="condition_false",
                ))
                continue

            tracker.update_step(ctx.run_id, step_id, "running")

            if step.for_each:
                result = self._execute_for_each(step, ctx)
            else:
                result = self._execute_step(step, ctx)

            ctx.set_step_result(step_id, result)
            completed_steps.add(step_id)
            tracker.update_step(ctx.run_id, step_id, result.status.value,
                                error=result.error,
                                duration_sec=result.duration_sec)

            if on_step_complete:
                try:
                    on_step_complete(step_id, result)
                except Exception:
                    pass

            if result.status == StepStatus.FAILED and step.on_error == ErrorPolicy.ABORT.value:
                log.error("Workflow aborted at step '%s': %s", step_id, result.error)
                aborted = True

            if step.delay_after > 0 and result.status == StepStatus.SUCCESS:
                time.sleep(step.delay_after)

        wf_result = self._build_result(workflow, ctx, aborted)
        tracker.finish_run(ctx.run_id, wf_result)
        return wf_result

    def _execute_step(self, step: StepDef, ctx: ExecutionContext) -> StepResult:
        """Execute a single step with retry support."""
        action = self.registry.get(step.action)
        if not action:
            log.error("Action not found: %s", step.action)
            return StepResult(step_id=step.id, status=StepStatus.FAILED,
                              error=f"action_not_found: {step.action}")

        fn, meta = action
        resolved_params = ctx.resolve(step.params)

        max_attempts = max(1, step.retry + 1)
        last_error = ""

        for attempt in range(max_attempts):
            start = time.time()
            try:
                log.info("Step '%s' executing (action=%s, attempt=%d/%d)",
                         step.id, step.action, attempt + 1, max_attempts)
                result = fn(**resolved_params)
                duration = time.time() - start

                return StepResult(
                    step_id=step.id,
                    status=StepStatus.SUCCESS,
                    result=result,
                    duration_sec=round(duration, 2),
                    retries_used=attempt,
                )
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                duration = time.time() - start
                log.warning("Step '%s' failed (attempt %d): %s", step.id, attempt + 1, last_error)
                if attempt < max_attempts - 1:
                    backoff = min(2 ** attempt * 2, 30)
                    time.sleep(backoff)

        return StepResult(
            step_id=step.id, status=StepStatus.FAILED,
            error=last_error, retries_used=max_attempts - 1,
        )

    def _execute_for_each(self, step: StepDef, ctx: ExecutionContext) -> StepResult:
        """Execute a step iterating over a list variable."""
        items = ctx.resolve(f"{{{step.for_each}}}")
        if not isinstance(items, list):
            items_from_vars = ctx.variables.get(step.for_each, [])
            if isinstance(items_from_vars, list):
                items = items_from_vars
            else:
                return StepResult(step_id=step.id, status=StepStatus.FAILED,
                                  error=f"for_each target '{step.for_each}' is not a list")

        results = []
        success_count = 0

        for idx, item in enumerate(items):
            ctx.current_item = item
            ctx.current_index = idx
            sub_result = self._execute_step(step, ctx)
            results.append({
                "index": idx,
                "item": item,
                "status": sub_result.status.value,
                "result": sub_result.result,
                "error": sub_result.error,
            })
            if sub_result.status == StepStatus.SUCCESS:
                success_count += 1

            if sub_result.status == StepStatus.FAILED and step.on_error == ErrorPolicy.ABORT.value:
                break

        ctx.current_item = None
        ctx.current_index = -1

        all_ok = success_count == len(items)
        return StepResult(
            step_id=step.id,
            status=StepStatus.SUCCESS if success_count > 0 else StepStatus.FAILED,
            result={"items": results, "success_count": success_count,
                    "total": len(items)},
        )

    def _topological_sort(self, steps: List[StepDef]) -> List[str]:
        """Sort steps respecting depends_on edges. Falls back to definition order for ties."""
        graph = {s.id: set(s.depends_on) for s in steps}
        all_ids = {s.id for s in steps}

        # Remove dependencies on non-existent steps
        for sid, deps in graph.items():
            graph[sid] = deps & all_ids

        result = []
        visited = set()
        temp_visited = set()

        def visit(node):
            if node in temp_visited:
                raise ValueError(f"Circular dependency detected involving '{node}'")
            if node in visited:
                return
            temp_visited.add(node)
            for dep in graph.get(node, set()):
                visit(dep)
            temp_visited.discard(node)
            visited.add(node)
            result.append(node)

        order_map = {s.id: i for i, s in enumerate(steps)}
        for sid in sorted(graph, key=lambda x: order_map.get(x, 0)):
            visit(sid)

        return result

    def _build_result(self, workflow: WorkflowDef,
                      ctx: ExecutionContext, aborted: bool) -> WorkflowResult:
        steps = {}
        for sid, sr in ctx.step_results.items():
            steps[sid] = {
                "status": sr.status.value,
                "result": sr.result,
                "error": sr.error,
                "duration_sec": sr.duration_sec,
                "retries_used": sr.retries_used,
            }

        success = all(
            sr.status in (StepStatus.SUCCESS, StepStatus.SKIPPED)
            for sr in ctx.step_results.values()
        )

        return WorkflowResult(
            run_id=ctx.run_id,
            workflow_name=workflow.name,
            success=success and not aborted,
            aborted=aborted,
            steps=steps,
            variables=ctx.variables,
            elapsed_sec=round(ctx.elapsed_sec, 2),
        )


@dataclass
class WorkflowResult:
    run_id: str
    workflow_name: str
    success: bool
    aborted: bool
    steps: Dict[str, dict]
    variables: Dict[str, Any]
    elapsed_sec: float

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "workflow_name": self.workflow_name,
            "success": self.success,
            "aborted": self.aborted,
            "steps": self.steps,
            "elapsed_sec": self.elapsed_sec,
        }

    @property
    def summary(self) -> str:
        total = len(self.steps)
        ok = sum(1 for s in self.steps.values() if s["status"] == "success")
        fail = sum(1 for s in self.steps.values() if s["status"] == "failed")
        skip = sum(1 for s in self.steps.values() if s["status"] == "skipped")
        return (f"Workflow '{self.workflow_name}' [{self.run_id}]: "
                f"{'SUCCESS' if self.success else 'FAILED'} "
                f"({ok}/{total} ok, {fail} fail, {skip} skip) "
                f"in {self.elapsed_sec}s")


class WorkflowRunTracker:
    """Tracks active and recent workflow executions for dashboard visibility."""

    def __init__(self, max_history: int = 50):
        import threading
        self._lock = threading.Lock()
        self._active: Dict[str, dict] = {}
        self._history: List[dict] = []
        self._max_history = max_history

    def start_run(self, run_id: str, workflow_name: str,
                  total_steps: int, variables: dict):
        with self._lock:
            self._active[run_id] = {
                "run_id": run_id,
                "workflow_name": workflow_name,
                "status": "running",
                "total_steps": total_steps,
                "completed_steps": 0,
                "current_step": "",
                "steps": {},
                "variables": variables,
                "started_at": time.time(),
            }
        self._broadcast("workflow.started", run_id, workflow_name)

    def update_step(self, run_id: str, step_id: str,
                    status: str, result: Any = None,
                    error: str = "", duration_sec: float = 0):
        with self._lock:
            run = self._active.get(run_id)
            if not run:
                return
            run["steps"][step_id] = {
                "status": status,
                "error": error,
                "duration_sec": duration_sec,
            }
            if status in ("success", "failed", "skipped"):
                run["completed_steps"] = sum(
                    1 for s in run["steps"].values()
                    if s["status"] in ("success", "failed", "skipped")
                )
            if status == "running":
                run["current_step"] = step_id
        self._broadcast("workflow.step", run_id, step_id,
                        extra={"step_status": status})

    def finish_run(self, run_id: str, result: WorkflowResult):
        with self._lock:
            run = self._active.pop(run_id, None)
            if run:
                run["status"] = "success" if result.success else "failed"
                run["elapsed_sec"] = result.elapsed_sec
                run["steps"] = result.steps
                self._history.append(run)
                if len(self._history) > self._max_history:
                    self._history = self._history[-self._max_history:]
        self._broadcast("workflow.finished", run_id,
                        result.workflow_name,
                        extra={"success": result.success})

    def get_active_runs(self) -> List[dict]:
        with self._lock:
            return list(self._active.values())

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            if run_id in self._active:
                return dict(self._active[run_id])
            for h in reversed(self._history):
                if h["run_id"] == run_id:
                    return dict(h)
        return None

    def get_history(self, limit: int = 20) -> List[dict]:
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def _broadcast(self, event_type: str, run_id: str,
                   name: str = "", extra: Optional[dict] = None):
        try:
            from src.host.websocket_hub import get_ws_hub
            hub = get_ws_hub()
            data = {"run_id": run_id, "workflow_name": name}
            if extra:
                data.update(extra)
            hub.broadcast(event_type, data)
        except Exception:
            pass


import threading as _threading
_tracker: Optional[WorkflowRunTracker] = None
_tracker_lock = _threading.Lock()


def get_workflow_tracker() -> WorkflowRunTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = WorkflowRunTracker()
    return _tracker
