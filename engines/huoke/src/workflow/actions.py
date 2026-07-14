"""
ActionRegistry — maps action names to callable functions.

Each automation module registers its methods here. The workflow engine
looks up actions by dotted name (e.g. "telegram.send_text_message").

Design: singleton registry, thread-safe, supports lazy module loading.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


class ActionRegistry:
    """
    Central registry for workflow-callable actions.

    Usage:
        registry = get_action_registry()
        registry.register("telegram.send_message", tg.send_text_message, {
            "params": ["message", "device_id"],
            "returns": "bool",
        })

        fn, meta = registry.get("telegram.send_message")
        result = fn(message="hello", device_id="abc")
    """

    def __init__(self):
        self._actions: Dict[str, Tuple[Callable, dict]] = {}
        self._lock = threading.Lock()

    def register(self, name: str, fn: Callable, meta: Optional[dict] = None):
        with self._lock:
            self._actions[name] = (fn, meta or {})
            log.debug("Action registered: %s", name)

    def register_module(self, prefix: str, obj: Any, methods: List[str],
                        meta_override: Optional[Dict[str, dict]] = None):
        """
        Bulk-register methods from an automation module.

        Example:
            registry.register_module("telegram", tg_instance, [
                "send_text_message", "search_and_open_user", "smart_send",
            ])
        """
        meta_override = meta_override or {}
        for method_name in methods:
            fn = getattr(obj, method_name, None)
            if fn and callable(fn):
                action_name = f"{prefix}.{method_name}"
                meta = meta_override.get(method_name, {})
                self.register(action_name, fn, meta)
            else:
                log.warning("Method '%s' not found on %s", method_name, type(obj).__name__)

    def get(self, name: str) -> Optional[Tuple[Callable, dict]]:
        return self._actions.get(name)

    def has(self, name: str) -> bool:
        return name in self._actions

    def list_actions(self, prefix: str = "") -> List[str]:
        with self._lock:
            if prefix:
                return [k for k in sorted(self._actions) if k.startswith(prefix)]
            return sorted(self._actions)

    def list_by_platform(self) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        for name in sorted(self._actions):
            parts = name.split(".", 1)
            platform = parts[0] if len(parts) > 1 else "_global"
            result.setdefault(platform, []).append(name)
        return result

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._actions.pop(name, None) is not None

    def clear(self):
        with self._lock:
            self._actions.clear()

    @property
    def count(self) -> int:
        return len(self._actions)


# -- Built-in utility actions -----------------------------------------------

def _action_log(message: str = "", level: str = "info", **kwargs) -> bool:
    getattr(log, level, log.info)("WORKFLOW LOG: %s", message)
    return True


def _action_wait(seconds: float = 1.0, **kwargs) -> bool:
    import time
    time.sleep(seconds)
    return True


def _action_set_variable(key: str = "", value: Any = None, **kwargs) -> Any:
    return value


def _action_check_compliance(platform: str = "", action: str = "",
                              account: str = "", **kwargs) -> dict:
    from ..behavior.compliance_guard import get_compliance_guard
    guard = get_compliance_guard()
    remaining = guard.get_remaining(platform, action, account)
    return remaining


# -- Singleton ---------------------------------------------------------------

_registry: Optional[ActionRegistry] = None
_reg_lock = threading.Lock()


def get_action_registry() -> ActionRegistry:
    global _registry
    if _registry is None:
        with _reg_lock:
            if _registry is None:
                _registry = ActionRegistry()
                _registry.register("util.log", _action_log, {"params": ["message", "level"]})
                _registry.register("util.wait", _action_wait, {"params": ["seconds"]})
                _registry.register("util.set_variable", _action_set_variable, {"params": ["key", "value"]})
                _registry.register("compliance.check_remaining", _action_check_compliance,
                                   {"params": ["platform", "action", "account"]})
    return _registry
