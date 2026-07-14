"""
App Registry — load app definitions from YAML and create GenericAppPlugin instances.

Config structure (config/apps/<name>.yaml):
  package: com.facebook.katana
  name: Facebook
  main_activity: com.facebook.katana.LoginActivity
  behavior_profile: facebook

  compliance:
    hourly_total: 40
    daily_total: 200
    actions:
      send_message: {hourly: 10, daily: 50}
      add_friend: {hourly: 5, daily: 30}
      search: {hourly: 15, daily: 80}

  actions:
    send_message:
      description: "Send a message to a user"
      params: [recipient, message]
      steps:
        - find: "Messenger or chat icon"
          action: tap
        - find: "Search contacts field"
          action: tap
        - find: "Search input"
          action: type
          param: recipient
        - find: "First matching contact"
          action: tap
        - find: "Message text box"
          action: type
          param: message
        - find: "Send button"
          action: tap
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.host.device_registry import config_dir

from .generic_plugin import (
    ActionFlow,
    ActionStep,
    AppDefinition,
    GenericAppPlugin,
)

log = logging.getLogger(__name__)

_DEFAULT_CONFIG_DIR = config_dir() / "apps"


class AppRegistry:
    """
    Central registry: loads YAML configs, creates GenericAppPlugin instances.

    Usage:
        registry = AppRegistry()
        fb = registry.get_plugin("facebook")  # or by package name
        fb.set_current_device("ABC123")
        result = fb.execute_action("send_message", {"recipient": "John", "message": "Hi!"})
    """

    def __init__(self, config_dir: Optional[Path] = None):
        self._dir = config_dir or _DEFAULT_CONFIG_DIR
        self._definitions: Dict[str, AppDefinition] = {}
        self._plugins: Dict[str, GenericAppPlugin] = {}
        self._lock = threading.Lock()
        self._load_all()

    def _load_all(self):
        if not self._dir.exists():
            log.info("App config dir not found: %s", self._dir)
            return
        for f in self._dir.glob("*.yaml"):
            try:
                self._load_file(f)
            except Exception as e:
                log.warning("Failed to load app config %s: %s", f.name, e)

    def _load_file(self, path: Path):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not raw or not isinstance(raw, dict):
            return

        package = raw.get("package", "")
        name = raw.get("name", path.stem)
        if not package:
            log.warning("App config %s missing 'package'", path.name)
            return

        actions: Dict[str, ActionFlow] = {}
        for action_name, action_data in raw.get("actions", {}).items():
            if not isinstance(action_data, dict):
                continue
            steps = []
            for step_data in action_data.get("steps", []):
                steps.append(ActionStep(
                    find=step_data.get("find", ""),
                    action=step_data.get("action", "tap"),
                    param=step_data.get("param", ""),
                    value=str(step_data.get("value", "")),
                    timeout=float(step_data.get("timeout", 10.0)),
                    optional=bool(step_data.get("optional", False)),
                    wait_after=float(step_data.get("wait_after", 0.5)),
                    context=step_data.get("context", ""),
                ))
            actions[action_name] = ActionFlow(
                name=action_name,
                description=action_data.get("description", ""),
                steps=steps,
                params=action_data.get("params", []),
            )

        app_def = AppDefinition(
            package=package,
            name=name,
            main_activity=raw.get("main_activity", ""),
            actions=actions,
            compliance=raw.get("compliance", {}),
            behavior_profile=raw.get("behavior_profile", "default"),
        )

        key = name.lower().replace(" ", "_")
        self._definitions[key] = app_def
        self._definitions[package] = app_def
        log.info("Loaded app: %s (%s) with %d actions",
                 name, package, len(actions))

    def reload(self):
        """Re-scan config directory."""
        with self._lock:
            self._definitions.clear()
            self._plugins.clear()
            self._load_all()

    def get_definition(self, name_or_package: str) -> Optional[AppDefinition]:
        key = name_or_package.lower().replace(" ", "_")
        return self._definitions.get(key)

    def get_plugin(self, name_or_package: str, **kwargs) -> Optional[GenericAppPlugin]:
        """Get or create a GenericAppPlugin for the named app."""
        key = name_or_package.lower().replace(" ", "_")
        with self._lock:
            if key in self._plugins:
                return self._plugins[key]

            app_def = self._definitions.get(key)
            if not app_def:
                log.warning("App not found: %s", name_or_package)
                return None

            plugin = GenericAppPlugin(app_def, **kwargs)
            self._plugins[key] = plugin
            self._plugins[app_def.package] = plugin
            return plugin

    def list_apps(self) -> List[Dict[str, Any]]:
        """List all registered apps."""
        seen = set()
        result = []
        for key, app_def in self._definitions.items():
            if app_def.package in seen:
                continue
            seen.add(app_def.package)
            result.append({
                "name": app_def.name,
                "package": app_def.package,
                "actions": list(app_def.actions.keys()),
                "behavior_profile": app_def.behavior_profile,
            })
        return result

    def register_app(self, app_def: AppDefinition):
        """Programmatically register an app (without YAML file)."""
        key = app_def.name.lower().replace(" ", "_")
        self._definitions[key] = app_def
        self._definitions[app_def.package] = app_def

    def save_app(self, name_or_package: str):
        """Save an app definition back to YAML."""
        app_def = self.get_definition(name_or_package)
        if not app_def:
            raise ValueError(f"App not found: {name_or_package}")

        data: Dict[str, Any] = {
            "package": app_def.package,
            "name": app_def.name,
        }
        if app_def.main_activity:
            data["main_activity"] = app_def.main_activity
        if app_def.behavior_profile != "default":
            data["behavior_profile"] = app_def.behavior_profile
        if app_def.compliance:
            data["compliance"] = app_def.compliance

        actions_data: Dict[str, Any] = {}
        for action_name, flow in app_def.actions.items():
            flow_data: Dict[str, Any] = {}
            if flow.description:
                flow_data["description"] = flow.description
            if flow.params:
                flow_data["params"] = flow.params
            steps_data = []
            for step in flow.steps:
                sd: Dict[str, Any] = {"find": step.find}
                if step.action != "tap":
                    sd["action"] = step.action
                if step.param:
                    sd["param"] = step.param
                if step.value:
                    sd["value"] = step.value
                if step.optional:
                    sd["optional"] = True
                if step.timeout != 10.0:
                    sd["timeout"] = step.timeout
                if step.wait_after != 0.5:
                    sd["wait_after"] = step.wait_after
                steps_data.append(sd)
            flow_data["steps"] = steps_data
            actions_data[action_name] = flow_data
        data["actions"] = actions_data

        self._dir.mkdir(parents=True, exist_ok=True)
        safe_name = app_def.name.lower().replace(" ", "_")
        path = self._dir / f"{safe_name}.yaml"
        path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        log.info("Saved app config: %s", path)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_registry: Optional[AppRegistry] = None
_registry_lock = threading.Lock()


def get_app_registry(config_dir: Optional[Path] = None) -> AppRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = AppRegistry(config_dir)
    return _registry
