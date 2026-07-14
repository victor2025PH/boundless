# -*- coding: utf-8 -*-
"""scripts/real_send_meta_ai.py 的设备解析逻辑单测.

只测 _resolve_devices / _coordinator_devices, 不触发 facebook / device_manager
等真机 import.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "real_send_meta_ai.py"


def _load_module():
    """以独立模块加载脚本, 不让 sys.path 污染."""
    spec = importlib.util.spec_from_file_location(
        "_smoke_meta_ai", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


_ALIASES_SAMPLE = {
    "SERIAL_A": {
        "host_scope": "coordinator", "slot": 5, "alias": "主控-05",
        "display_label": "主控-05",
    },
    "SERIAL_B": {
        "host_scope": "coordinator", "slot": 7, "alias": "主控-07",
        "display_label": "主控-07",
    },
    "SERIAL_C": {
        "host_scope": "coordinator", "slot": 3, "alias": "主控-03",
        "display_label": "主控-03",
    },
    "SERIAL_NONCOORD": {
        "host_scope": "unknown", "slot": 9, "alias": "X-09",
    },
    "SERIAL_NOSCOPE": {
        "alias": "no-scope-default",
    },
}


# ---- _coordinator_devices ----

def test_coordinator_returns_only_coordinator_scope(mod):
    out = mod._coordinator_devices(_ALIASES_SAMPLE)
    assert set(out) == {"SERIAL_A", "SERIAL_B", "SERIAL_C"}


def test_coordinator_sorted_by_slot(mod):
    out = mod._coordinator_devices(_ALIASES_SAMPLE)
    assert out == ["SERIAL_C", "SERIAL_A", "SERIAL_B"]  # slot 3, 5, 7


def test_coordinator_empty_aliases(mod):
    assert mod._coordinator_devices({}) == []


def test_coordinator_no_coord_scope(mod):
    aliases = {"S1": {"host_scope": "unknown"}}
    assert mod._coordinator_devices(aliases) == []


# ---- _label_for ----

def test_label_prefers_display_label(mod):
    info = {"display_label": "主控-05", "alias": "fallback"}
    assert mod._label_for("SERIAL_A", {"SERIAL_A": info}) == "主控-05"


def test_label_falls_back_to_alias(mod):
    info = {"alias": "alias-only"}
    assert mod._label_for("S", {"S": info}) == "alias-only"


def test_label_unknown_serial_returns_serial(mod):
    assert mod._label_for("UNKNOWN", _ALIASES_SAMPLE) == "UNKNOWN"


# ---- _resolve_devices ----

def test_resolve_default_uses_aliases(mod, monkeypatch):
    monkeypatch.delenv("OPENCLAW_SMOKE_DEVICES", raising=False)
    out = mod._resolve_devices(None, None, aliases=_ALIASES_SAMPLE)
    serials = [s for s, _ in out]
    assert serials == ["SERIAL_C", "SERIAL_A", "SERIAL_B"]


def test_resolve_cli_overrides_aliases(mod, monkeypatch):
    monkeypatch.setenv("OPENCLAW_SMOKE_DEVICES", "ENV_S1,ENV_S2")
    out = mod._resolve_devices(
        "CLI_X,CLI_Y", None, aliases=_ALIASES_SAMPLE)
    serials = [s for s, _ in out]
    assert serials == ["CLI_X", "CLI_Y"]  # CLI wins over env


def test_resolve_env_overrides_aliases(mod, monkeypatch):
    monkeypatch.setenv("OPENCLAW_SMOKE_DEVICES", "ENV_S1,ENV_S2")
    out = mod._resolve_devices(None, None, aliases=_ALIASES_SAMPLE)
    serials = [s for s, _ in out]
    assert serials == ["ENV_S1", "ENV_S2"]


def test_resolve_limit_truncates(mod, monkeypatch):
    monkeypatch.delenv("OPENCLAW_SMOKE_DEVICES", raising=False)
    out = mod._resolve_devices(None, 2, aliases=_ALIASES_SAMPLE)
    assert len(out) == 2
    serials = [s for s, _ in out]
    assert serials == ["SERIAL_C", "SERIAL_A"]


def test_resolve_limit_zero_means_all(mod, monkeypatch):
    """limit=0 / None 都应不截断 (len > 0 才截)."""
    monkeypatch.delenv("OPENCLAW_SMOKE_DEVICES", raising=False)
    out = mod._resolve_devices(None, 0, aliases=_ALIASES_SAMPLE)
    assert len(out) == 3


def test_resolve_unknown_serial_label_falls_back(mod, monkeypatch):
    monkeypatch.delenv("OPENCLAW_SMOKE_DEVICES", raising=False)
    out = mod._resolve_devices("UNKNOWN_X", None, aliases={})
    assert out == [("UNKNOWN_X", "UNKNOWN_X")]


def test_resolve_empty_aliases_no_cli_no_env_returns_zero(mod, monkeypatch):
    monkeypatch.delenv("OPENCLAW_SMOKE_DEVICES", raising=False)
    out = mod._resolve_devices(None, None, aliases={})
    assert out == []


def test_resolve_csv_strips_whitespace(mod, monkeypatch):
    monkeypatch.delenv("OPENCLAW_SMOKE_DEVICES", raising=False)
    out = mod._resolve_devices("  S1 ,, S2  ,", None, aliases={})
    serials = [s for s, _ in out]
    assert serials == ["S1", "S2"]
