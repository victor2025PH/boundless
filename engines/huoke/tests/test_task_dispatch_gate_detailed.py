# -*- coding: utf-8 -*-
"""evaluate_task_gate_detailed 集成测：mock 预检/GEO，无真机 ADB。"""

import sys

from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.behavior.geo_check import GeoCheckResult
from src.host.preflight import PreflightResult


@pytest.fixture(autouse=True)
def _task_policy_enforce_preflight_for_gate_tests(monkeypatch):
    """仓库内 task_execution_policy.yaml 可能为运维将 enforce_preflight 置为 false；本文件用 mock 验门禁，须强制为 true。"""
    from src.host import task_policy

    _orig = task_policy.load_task_execution_policy

    def _merged(force_reload: bool = False):
        p = dict(_orig(force_reload=force_reload))
        mg = dict(p.get("manual_gate") or {})
        mg["enforce_preflight"] = True
        # 与上同理：生产 YAML 可能为 false，须覆盖而非 setdefault
        mg["enforce_geo_for_risky"] = True
        p["manual_gate"] = mg
        return p

    monkeypatch.setattr(task_policy, "load_task_execution_policy", _merged)


@pytest.fixture
def devices_yaml() -> str:
    return str(Path(__file__).resolve().parent.parent / "config" / "devices.yaml")


@patch("src.host.preflight.run_preflight")
def test_preflight_fail_sets_hint_code_and_message(mock_pf, devices_yaml):
    from src.host.task_dispatch_gate import evaluate_task_gate_detailed, resolve_gate_hint_message

    mock_pf.return_value = PreflightResult(
        device_id="dev1",
        passed=False,
        blocked_step="VPN",
        blocked_reason="tunnel down",
        preflight_mode="full",
    )
    ev = evaluate_task_gate_detailed(
        {"type": "tiktok_follow", "params": {}},
        "SERIAL1",
        devices_yaml,
    )
    assert ev.allowed is False
    assert ev.hint_code == "preflight_vpn"
    d = ev.to_dict()
    assert d["hint_message"] == resolve_gate_hint_message("preflight_vpn")
    assert "[gate]" in ev.reason


@patch("src.behavior.geo_check.check_device_geo")
@patch("src.host.preflight.run_preflight")
def test_geo_mismatch_sets_hint(mock_pf, mock_geo, devices_yaml):
    from src.host.task_dispatch_gate import evaluate_task_gate_detailed, resolve_gate_hint_message

    mock_pf.return_value = PreflightResult(
        device_id="dev1",
        passed=True,
        network_ok=True,
        vpn_ok=True,
        account_ok=True,
        preflight_mode="full",
    )
    mock_geo.return_value = GeoCheckResult(
        device_id="SERIAL1",
        public_ip="203.0.113.1",
        detected_country="Germany",
        detected_country_code="DE",
        expected_country="italy",
        matches=False,
        error="",
    )
    ev = evaluate_task_gate_detailed(
        {"type": "tiktok_follow", "params": {"target_country": "italy"}},
        "SERIAL1",
        devices_yaml,
    )
    assert ev.allowed is False
    assert ev.hint_code == "geo_country_mismatch"
    assert ev.to_dict()["hint_message"] == resolve_gate_hint_message("geo_country_mismatch")


@patch("src.behavior.geo_check.check_device_geo")
@patch("src.host.preflight.run_preflight")
def test_geo_error_sets_public_ip_hint(mock_pf, mock_geo, devices_yaml):
    from src.host.task_dispatch_gate import evaluate_task_gate_detailed, resolve_gate_hint_message

    mock_pf.return_value = PreflightResult(
        device_id="dev1",
        passed=True,
        network_ok=True,
        vpn_ok=True,
        account_ok=True,
        preflight_mode="full",
    )
    mock_geo.return_value = GeoCheckResult(
        device_id="SERIAL1",
        public_ip="",
        detected_country="",
        detected_country_code="",
        expected_country="italy",
        matches=False,
        error="Could not determine public IP",
    )
    ev = evaluate_task_gate_detailed(
        {"type": "tiktok_follow", "params": {}},
        "SERIAL1",
        devices_yaml,
    )
    assert ev.allowed is False
    assert ev.hint_code == "geo_public_ip_failed"
    assert ev.to_dict()["hint_message"] == resolve_gate_hint_message("geo_public_ip_failed")


def test_result_dict_with_gate_hints_backfills():
    from src.host.task_dispatch_gate import (
        resolve_gate_hint_message,
        result_dict_with_gate_hints,
    )

    raw = {"success": False, "gate_evaluation": {"hint_code": "preflight_network"}}
    out = result_dict_with_gate_hints(raw)
    assert out is not raw
    assert out["gate_evaluation"]["hint_message"] == resolve_gate_hint_message(
        "preflight_network"
    )
    assert result_dict_with_gate_hints(out) is out

    with_msg = {
        "gate_evaluation": {"hint_code": "x", "hint_message": "自定义"},
    }
    assert result_dict_with_gate_hints(with_msg) is with_msg


def test_to_response_injects_hint_message():
    from src.host.api import _to_response

    t = {
        "task_id": "tid-gate-hint",
        "type": "tiktok_follow",
        "device_id": "d1",
        "status": "failed",
        "params": {},
        "result": {
            "error": "[gate] x",
            "gate_evaluation": {"hint_code": "geo_country_mismatch"},
        },
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    resp = _to_response(t)
    assert resp.result["gate_evaluation"].get("hint_message")
    assert resp.result is not t["result"]


@patch("src.behavior.geo_check.check_device_geo")
@patch("src.host.preflight.run_preflight")
def test_geo_reuses_preflight_snapshot_skips_second_lookup(mock_pf, mock_geo, devices_yaml):
    """full 预检已带 geo_snapshot 且期望国一致时，门禁不再调用 check_device_geo。"""
    from src.host.task_dispatch_gate import evaluate_task_gate_detailed

    mock_pf.return_value = PreflightResult(
        device_id="dev1",
        passed=True,
        network_ok=True,
        vpn_ok=True,
        account_ok=True,
        preflight_mode="full",
        geo_snapshot={
            "expected_country": "italy",
            "detected_country": "Italy",
            "detected_country_code": "IT",
            "public_ip": "203.0.113.2",
            "matches": True,
            "error": "",
        },
    )
    ev = evaluate_task_gate_detailed(
        {"type": "tiktok_follow", "params": {"target_country": "italy"}},
        "SERIAL1",
        devices_yaml,
    )
    assert ev.allowed is True
    mock_geo.assert_not_called()
    geo = ev.connectivity.get("geo") or {}
    assert geo.get("reused_from_preflight") is True
    assert geo.get("detected_country") == "Italy"


@patch("src.behavior.geo_check.check_device_geo")
@patch("src.host.preflight.run_preflight")
def test_geo_mismatch_from_snapshot_skips_second_lookup(mock_pf, mock_geo, devices_yaml):
    from src.host.task_dispatch_gate import evaluate_task_gate_detailed, resolve_gate_hint_message

    mock_pf.return_value = PreflightResult(
        device_id="dev1",
        passed=True,
        network_ok=True,
        vpn_ok=True,
        account_ok=True,
        preflight_mode="full",
        geo_snapshot={
            "expected_country": "italy",
            "detected_country": "Germany",
            "detected_country_code": "DE",
            "public_ip": "198.51.100.1",
            "matches": False,
            "error": "",
        },
    )
    ev = evaluate_task_gate_detailed(
        {"type": "tiktok_follow", "params": {"target_country": "italy"}},
        "SERIAL1",
        devices_yaml,
    )
    assert ev.allowed is False
    assert ev.hint_code == "geo_country_mismatch"
    mock_geo.assert_not_called()
    assert ev.to_dict()["hint_message"] == resolve_gate_hint_message("geo_country_mismatch")
