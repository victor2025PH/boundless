"""
OpenClaw Plugin Manager — lightweight plugin architecture.

Plugins live in the `plugins/` directory, each as a Python file or package
containing a `plugin_info()` function returning metadata and optional
lifecycle hooks.
"""
from __future__ import annotations
import importlib
import importlib.util
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from src.host.device_registry import plugins_dir as _default_plugins_dir

log = logging.getLogger("openclaw.plugins")


class PluginMeta:
    __slots__ = ("name", "version", "author", "description", "enabled",
                 "loaded_at", "hooks", "module", "file_path", "error")

    def __init__(self, *, name: str, version: str = "0.1.0", author: str = "",
                 description: str = "", file_path: str = ""):
        self.name = name
        self.version = version
        self.author = author
        self.description = description
        self.enabled: bool = False
        self.loaded_at: float = 0
        self.hooks: dict[str, Callable] = {}
        self.module: Any = None
        self.file_path = file_path
        self.error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "version": self.version, "author": self.author,
            "description": self.description, "enabled": self.enabled,
            "loaded_at": self.loaded_at, "file": self.file_path,
            "hooks": list(self.hooks.keys()), "error": self.error,
        }


_HOOK_NAMES = (
    "on_load", "on_unload",
    "on_device_connected", "on_device_disconnected",
    "on_task_start", "on_task_complete",
    "on_screenshot", "on_message",
    "register_routes",
)


class PluginManager:
    def __init__(self, plugins_dir: str | Path | None = None):
        self._dir = Path(plugins_dir) if plugins_dir else _default_plugins_dir()
        self._plugins: dict[str, PluginMeta] = {}

    @property
    def plugins(self) -> dict[str, PluginMeta]:
        return self._plugins

    def discover(self) -> list[str]:
        """Scan plugins/ for .py files and sub-packages."""
        found: list[str] = []
        if not self._dir.exists():
            return found
        for entry in self._dir.iterdir():
            if entry.name.startswith("_"):
                continue
            if entry.is_file() and entry.suffix == ".py":
                found.append(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").exists():
                found.append(entry.name)
        return found

    def load(self, name: str) -> PluginMeta:
        """Load a plugin by name from the plugins directory."""
        file_path = self._dir / f"{name}.py"
        pkg_path = self._dir / name / "__init__.py"

        if file_path.exists():
            spec = importlib.util.spec_from_file_location(f"plugins.{name}", str(file_path))
        elif pkg_path.exists():
            spec = importlib.util.spec_from_file_location(f"plugins.{name}", str(pkg_path))
        else:
            meta = PluginMeta(name=name, file_path="")
            meta.error = "Plugin file not found"
            return meta

        meta = PluginMeta(name=name, file_path=str(file_path if file_path.exists() else pkg_path))
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            meta.module = module

            info_fn = getattr(module, "plugin_info", None)
            if callable(info_fn):
                info = info_fn()
                meta.version = info.get("version", meta.version)
                meta.author = info.get("author", "")
                meta.description = info.get("description", "")

            for hook_name in _HOOK_NAMES:
                fn = getattr(module, hook_name, None)
                if callable(fn):
                    meta.hooks[hook_name] = fn

            meta.loaded_at = time.time()
            log.info("Plugin loaded: %s v%s", meta.name, meta.version)
        except Exception as exc:
            meta.error = str(exc)
            log.error("Failed to load plugin %s: %s", name, exc)

        self._plugins[name] = meta
        return meta

    def enable(self, name: str) -> bool:
        meta = self._plugins.get(name)
        if not meta or meta.error:
            return False
        meta.enabled = True
        if "on_load" in meta.hooks:
            try:
                meta.hooks["on_load"]()
            except Exception as exc:
                log.error("Plugin %s on_load error: %s", name, exc)
                meta.error = f"on_load: {exc}"
        return True

    def disable(self, name: str) -> bool:
        meta = self._plugins.get(name)
        if not meta:
            return False
        if "on_unload" in meta.hooks:
            try:
                meta.hooks["on_unload"]()
            except Exception as exc:
                log.error("Plugin %s on_unload error: %s", name, exc)
        meta.enabled = False
        return True

    def unload(self, name: str):
        self.disable(name)
        self._plugins.pop(name, None)

    def reload(self, name: str) -> PluginMeta:
        """Hot-reload a plugin: unload then re-load from disk."""
        was_enabled = False
        if name in self._plugins:
            was_enabled = self._plugins[name].enabled
            self.unload(name)
        import sys
        mod_key = f"plugins.{name}"
        if mod_key in sys.modules:
            del sys.modules[mod_key]
        meta = self.load(name)
        if was_enabled and not meta.error:
            self.enable(name)
        log.info("Plugin hot-reloaded: %s (enabled=%s)", name, meta.enabled)
        return meta

    def reload_all(self):
        """Hot-reload all currently loaded plugins."""
        names = list(self._plugins.keys())
        for name in names:
            self.reload(name)

    def emit(self, hook_name: str, *args, **kwargs):
        """Call a hook across all enabled plugins."""
        for meta in self._plugins.values():
            if meta.enabled and hook_name in meta.hooks:
                try:
                    meta.hooks[hook_name](*args, **kwargs)
                except Exception as exc:
                    log.error("Plugin %s hook %s error: %s", meta.name, hook_name, exc)

    def emit_async(self, hook_name: str, *args, **kwargs):
        """Fire-and-forget emit in a background thread."""
        import threading
        t = threading.Thread(
            target=self.emit, args=(hook_name, *args), kwargs=kwargs,
            daemon=True, name=f"plugin-emit-{hook_name}",
        )
        t.start()

    def load_all(self):
        """Discover and load all plugins."""
        for name in self.discover():
            self.load(name)

    def list_all(self) -> list[dict]:
        return [m.to_dict() for m in self._plugins.values()]

    def get_event_log(self) -> list[dict]:
        """Return recent plugin events."""
        return list(self._event_log)

    _event_log: list = []

    def _log_event(self, event_type: str, data: dict):
        entry = {"ts": time.strftime("%H:%M:%S"), "type": event_type, **data}
        self._event_log.append(entry)
        if len(self._event_log) > 200:
            self._event_log = self._event_log[-200:]


_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    global _manager
    if _manager is None:
        _manager = PluginManager()
        _manager.load_all()
    return _manager
