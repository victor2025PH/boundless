"""
GenericAppPlugin — automate ANY app using self-learning selectors.

Instead of writing platform-specific code (like TelegramAutomation),
define an app in YAML with action flows, and this plugin figures out
how to execute them using AutoSelector + Vision.

Action flows are multi-step sequences:
  send_message:
    - find: "New chat or compose icon"
      action: tap
    - find: "Search/recipient field"
      action: tap
    - find: "Text input"
      action: type
      param: recipient
    - find: "First matching contact"
      action: tap
    - find: "Message input box"
      action: type
      param: message
    - find: "Send button"
      action: tap

First execution uses Vision to locate each element → learns selectors.
Subsequent runs use cached selectors (instant, zero API cost).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .base_automation import BaseAutomation
from ..vision.auto_selector import AutoSelector, get_auto_selector
from ..vision.screen_parser import ParsedElement

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action step model
# ---------------------------------------------------------------------------

@dataclass
class ActionStep:
    """One step in an action flow."""

    find: str                     # element description ("Send button")
    action: str = "tap"           # tap, type, long_press, swipe, wait
    param: str = ""               # parameter name for substitution
    value: str = ""               # static value (if param not used)
    timeout: float = 10.0
    optional: bool = False        # if True, skip on failure instead of aborting
    wait_after: float = 0.5       # post-action delay (seconds)
    context: str = ""             # extra Vision context hint


@dataclass
class ActionFlow:
    """Multi-step action definition."""

    name: str
    description: str = ""
    steps: List[ActionStep] = field(default_factory=list)
    params: List[str] = field(default_factory=list)


@dataclass
class AppDefinition:
    """Complete app definition loaded from YAML."""

    package: str
    name: str
    main_activity: str = ""
    actions: Dict[str, ActionFlow] = field(default_factory=dict)
    compliance: Dict[str, Any] = field(default_factory=dict)
    behavior_profile: str = "default"


# ---------------------------------------------------------------------------
# Flow executor
# ---------------------------------------------------------------------------

class FlowExecutor:
    """Executes action flows using AutoSelector for each step."""

    def __init__(self, auto_selector: AutoSelector):
        self._auto = auto_selector

    def execute(self, device, package: str, flow: ActionFlow,
                params: Optional[Dict[str, str]] = None,
                behavior=None) -> FlowResult:
        """
        Execute an action flow step by step.

        Args:
            device: u2 device
            package: Android package name
            flow: the action flow to execute
            params: parameter values for substitution (e.g. {"recipient": "John", "message": "Hi"})
            behavior: optional HumanBehavior instance for delays

        Returns FlowResult with success/failure details.
        """
        params = params or {}
        result = FlowResult(flow_name=flow.name)

        for i, step in enumerate(flow.steps):
            step_result = self._execute_step(device, package, step, params, behavior)
            result.step_results.append(step_result)

            if not step_result.success:
                if step.optional:
                    log.debug("Step %d/%d optional, skipping: %s",
                              i + 1, len(flow.steps), step.find)
                    continue
                result.success = False
                result.error = f"Step {i+1} failed: {step.find}"
                log.warning("Flow '%s' failed at step %d: %s",
                            flow.name, i + 1, step.find)
                return result

        result.success = True
        return result

    def _execute_step(self, device, package: str, step: ActionStep,
                      params: Dict[str, str],
                      behavior) -> StepResult:
        """Execute a single step."""
        sr = StepResult(target=step.find, action=step.action)
        start = time.time()

        try:
            if step.action == "wait":
                wait_time = float(step.value or step.timeout)
                if behavior:
                    behavior.wait_between_actions(context_weight=wait_time / 3.0)
                else:
                    time.sleep(wait_time)
                sr.success = True
                sr.elapsed = time.time() - start
                return sr

            # find element
            parsed = self._auto.find(device, package, step.find, step.context)
            if not parsed:
                sr.success = False
                sr.error = "Element not found"
                sr.elapsed = time.time() - start
                return sr

            sr.selectors_used = parsed.selectors
            sr.coordinates = parsed.center

            # execute action
            if step.action == "tap":
                self._do_tap(device, parsed, behavior)
            elif step.action == "type":
                value = params.get(step.param, step.value) if step.param else step.value
                self._do_type(device, parsed, value, behavior)
            elif step.action == "long_press":
                self._do_long_press(device, parsed, behavior)
            elif step.action == "swipe":
                self._do_swipe(device, parsed)
            else:
                log.warning("Unknown action: %s", step.action)

            sr.success = True

            # post-action delay
            if step.wait_after > 0:
                if behavior:
                    behavior.wait_between_actions(context_weight=step.wait_after)
                else:
                    time.sleep(step.wait_after)

        except Exception as e:
            sr.success = False
            sr.error = str(e)

        sr.elapsed = time.time() - start
        return sr

    @staticmethod
    def _do_tap(device, parsed: ParsedElement, behavior=None):
        cx, cy = parsed.center
        if behavior:
            behavior.tap(device, cx, cy)
        else:
            device.click(cx, cy)

    @staticmethod
    def _do_type(device, parsed: ParsedElement, value: str, behavior=None):
        cx, cy = parsed.center
        device.click(cx, cy)
        time.sleep(0.3)
        if behavior:
            behavior.type_text(device, value)
        else:
            device.send_keys(value, clear=True)

    @staticmethod
    def _do_long_press(device, parsed: ParsedElement, behavior=None):
        cx, cy = parsed.center
        device.long_click(cx, cy)

    @staticmethod
    def _do_swipe(device, parsed: ParsedElement):
        cx, cy = parsed.center
        device.swipe(cx, cy, cx, cy - 300, duration=0.5)


@dataclass
class StepResult:
    target: str = ""
    action: str = ""
    success: bool = False
    error: str = ""
    elapsed: float = 0.0
    selectors_used: List[Dict[str, str]] = field(default_factory=list)
    coordinates: tuple = (0, 0)


@dataclass
class FlowResult:
    flow_name: str = ""
    success: bool = False
    error: str = ""
    step_results: List[StepResult] = field(default_factory=list)

    @property
    def steps_completed(self) -> int:
        return sum(1 for s in self.step_results if s.success)

    @property
    def total_elapsed(self) -> float:
        return sum(s.elapsed for s in self.step_results)


# ---------------------------------------------------------------------------
# GenericAppPlugin — the main class
# ---------------------------------------------------------------------------

class GenericAppPlugin(BaseAutomation):
    """
    Automates any Android app using YAML-defined action flows.

    Unlike TelegramAutomation/LinkedInAutomation which have hardcoded selectors,
    this plugin discovers and learns selectors automatically through Vision.
    """

    def __init__(self, app_def: AppDefinition, device_manager=None,
                 auto_selector: Optional[AutoSelector] = None, **kwargs):
        self.PLATFORM = app_def.name.lower().replace(" ", "_")
        self.PACKAGE = app_def.package
        self.MAIN_ACTIVITY = app_def.main_activity

        if device_manager is None:
            from ..device_control.device_manager import get_device_manager
            device_manager = get_device_manager()

        super().__init__(device_manager, **kwargs)
        self.app_def = app_def
        self._auto = auto_selector or get_auto_selector()
        self._flow_exec = FlowExecutor(self._auto)

    def execute_action(self, action_name: str,
                       params: Optional[Dict[str, str]] = None,
                       device_id: Optional[str] = None) -> FlowResult:
        """
        Execute a named action from the app definition.

        Example:
            plugin.execute_action("send_message", {"recipient": "John", "message": "Hi!"})
        """
        flow = self.app_def.actions.get(action_name)
        if not flow:
            raise ValueError(
                f"Action '{action_name}' not defined for {self.app_def.name}. "
                f"Available: {list(self.app_def.actions.keys())}"
            )

        did = self._did(device_id)
        d = self._u2(did)

        if not self.is_foreground(did):
            self.start_app(did)

        with self.guarded(action_name, device_id=did):
            return self._flow_exec.execute(
                d, self.PACKAGE, flow, params, self.hb
            )

    def find_element(self, target: str, context: str = "",
                     device_id: Optional[str] = None) -> Optional[ParsedElement]:
        """Find any element on the current screen by description."""
        d = self._u2(device_id)
        return self._auto.find(d, self.PACKAGE, target, context)

    def parse_screen(self, use_vision: bool = False,
                     device_id: Optional[str] = None) -> List[ParsedElement]:
        """Parse all interactive elements on the current screen."""
        d = self._u2(device_id)
        return self._auto.find_all(d, self.PACKAGE, use_vision=use_vision)

    def tap_element(self, target: str, context: str = "",
                    device_id: Optional[str] = None) -> bool:
        """Find and tap an element by description."""
        parsed = self.find_element(target, context, device_id)
        if not parsed:
            return False
        d = self._u2(device_id)
        cx, cy = parsed.center
        self.hb.tap(d, cx, cy)
        return True

    def type_into(self, target: str, text: str,
                  device_id: Optional[str] = None) -> bool:
        """Find an input element and type text into it."""
        parsed = self.find_element(target, device_id=device_id)
        if not parsed:
            return False
        d = self._u2(device_id)
        cx, cy = parsed.center
        d.click(cx, cy)
        time.sleep(0.3)
        self.hb.type_text(d, text)
        return True

    def get_available_actions(self) -> List[str]:
        return list(self.app_def.actions.keys())

    def selector_stats(self) -> Dict[str, Any]:
        return self._auto.store.stats(self.PACKAGE)
