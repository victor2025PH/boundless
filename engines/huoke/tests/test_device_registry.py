# -*- coding: utf-8 -*-
"""device_registry：YAML 解析入口与保守容错语义（与 TikTok / Facebook 路由一致）。"""
from __future__ import annotations

from unittest.mock import patch

from src.host import device_registry as dr


def test_get_device_row_safe_returns_none_on_exception() -> None:
    with patch.object(dr, "get_device_row_strict", side_effect=OSError("bad yaml")):
        assert dr.get_device_row_safe("d1") is None


def test_is_device_in_local_registry_true_on_exception() -> None:
    with patch.object(dr, "get_device_row_strict", side_effect=ValueError("mgr")):
        assert dr.is_device_in_local_registry("any") is True


def test_is_device_in_local_registry_false_when_not_in_registry() -> None:
    with patch.object(dr, "get_device_row_strict", return_value=None):
        assert dr.is_device_in_local_registry("d1") is False


def test_is_device_in_local_registry_true_when_row_exists() -> None:
    fake = object()
    with patch.object(dr, "get_device_row_strict", return_value=fake):
        assert dr.is_device_in_local_registry("d1") is True


def test_default_devices_yaml_points_at_project_config() -> None:
    norm = dr.DEFAULT_DEVICES_YAML.replace("\\", "/")
    assert norm.endswith("config/devices.yaml")


def test_project_root_and_config_data_helpers() -> None:
    from pathlib import Path

    assert dr.config_file("devices.yaml") == Path(dr.DEFAULT_DEVICES_YAML)
    assert dr.data_file("leads.db") == dr.PROJECT_ROOT / "data" / "leads.db"
    assert dr.config_dir() == dr.PROJECT_ROOT / "config"
    assert dr.scripts_dir() == dr.PROJECT_ROOT / "scripts"
    assert dr.templates_dir() == dr.PROJECT_ROOT / "templates"
    assert dr.logs_dir() == dr.PROJECT_ROOT / "logs"
    assert dr.plugins_dir() == dr.PROJECT_ROOT / "plugins"
    assert dr.tools_dir() == dr.PROJECT_ROOT / "tools"


def test_vpn_router_paths_match_device_registry() -> None:
    """vpn.py 曾误用三层 parent 指向 src/config；须与 device_registry 主控路径一致。"""
    from src.host.routers import vpn as vpn_mod

    assert vpn_mod._config_path == dr.DEFAULT_DEVICES_YAML
    assert vpn_mod._POOL_FILE == vpn_mod._project_root / "config" / "vpn_pool.json"


def test_routers_devices_yaml_not_under_src_config() -> None:
    """streaming / conversations / websocket 曾 parent×3 误指 src/config。"""
    from src.host.routers import streaming as st
    from src.host.routers import conversations as conv
    from src.host.routers import websocket_routes as ws

    for mod in (st, conv, ws):
        assert mod._config_path == dr.DEFAULT_DEVICES_YAML
        assert "/src/config/" not in mod._config_path.replace("\\", "/")


def test_macros_project_root_is_repo_root_not_src() -> None:
    from pathlib import Path

    from src.host.routers import macros as macros_mod

    assert macros_mod._config_path == dr.DEFAULT_DEVICES_YAML
    assert (macros_mod._project_root / "config" / "devices.yaml").resolve() == Path(
        dr.DEFAULT_DEVICES_YAML
    ).resolve()


def test_monitoring_alert_and_alias_paths_use_registry() -> None:
    from src.host.routers import monitoring as mon

    assert mon._alert_rules_path == dr.config_file("alert_rules.json")


def test_workflows_visual_json_path() -> None:
    from src.host.routers import workflows as wf

    assert wf._visual_workflows_path == dr.config_file("visual_workflows.json")


def test_auth_users_json_under_project_config() -> None:
    from src.host.routers import auth as auth_mod

    assert auth_mod._users_path == dr.config_file("users.json")
    assert "/src/config/" not in str(auth_mod._users_path).replace("\\", "/")


def test_database_openclaw_db_under_project_data() -> None:
    from src.host import database as db_mod

    assert db_mod.DB_PATH == dr.data_file("openclaw.db")
