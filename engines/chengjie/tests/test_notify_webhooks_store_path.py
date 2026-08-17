"""notify_webhooks 落点锚定门禁（2026-08-01）。

修复前：本模块是配置家族里唯一用裸相对路径 ``config/notify_webhooks.json`` 的叛徒，
靠「服务进程 CWD 恰好是数据根」。服务实测从引擎根起 main.py → 与经 env 锚定数据根的
``config.local.yaml`` 分裂，且多实例共享引擎根那一份（告警渠道配置串味）。

本门禁钉住：落点跟随 ``licensing.data_paths.config_dir()``（AITR_CONFIG_PATH 父 →
AITR_DATA_DIR/config → 仓内），与整个配置家族同目录；显式 ``set_store_path`` 覆盖优先；
``None`` 复位默认。范式对齐 ``test_licensing_data_paths`` / ``test_global_rules_overlay``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.integrations import notify_webhooks_store as ws


@pytest.fixture(autouse=True)
def _reset_store():
    """每例前后复位为默认落点，避免 _path/_cache 跨例串味。"""
    ws.set_store_path(None)
    yield
    ws.set_store_path(None)


def _clear_env(monkeypatch):
    for k in ("AITR_CONFIG_PATH", "AITR_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)


def test_default_follows_data_dir(monkeypatch, tmp_path):
    """设 AITR_DATA_DIR → 落点 = <data>/config/notify_webhooks.json（与配置家族同目录）。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "root"))
    ws.set_store_path(None)  # 复位 → 重新按 env 解析
    assert ws._store_path() == tmp_path / "root" / "config" / "notify_webhooks.json"


def test_default_follows_config_path_parent(monkeypatch, tmp_path):
    """AITR_CONFIG_PATH 优先级最高：落点 = 其父目录 / notify_webhooks.json。"""
    _clear_env(monkeypatch)
    cfg = tmp_path / "data" / "config" / "config.yaml"
    monkeypatch.setenv("AITR_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "ignored"))
    ws.set_store_path(None)
    assert ws._store_path() == cfg.parent / "notify_webhooks.json"


def test_no_env_falls_back_to_repo_config(monkeypatch):
    """无 env（开发/裸跑）→ 回落仓内 config/，零破坏（与配置家族同回落）。"""
    _clear_env(monkeypatch)
    ws.set_store_path(None)
    p = ws._store_path()
    assert p.name == "notify_webhooks.json"
    assert p.parent.name == "config"


def test_explicit_override_wins(monkeypatch, tmp_path):
    """set_store_path(具体路径) 覆盖优先，忽略 env（测试隔离用）。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "root"))
    explicit = tmp_path / "explicit" / "wh.json"
    ws.set_store_path(explicit)
    assert ws._store_path() == explicit


def test_none_resets_to_default(monkeypatch, tmp_path):
    """set_store_path(None) 复位为默认锚定（不再钉死在上一次显式路径）。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "root"))
    ws.set_store_path(tmp_path / "explicit" / "wh.json")
    assert ws._store_path().parent.name == "explicit"
    ws.set_store_path(None)
    assert ws._store_path() == tmp_path / "root" / "config" / "notify_webhooks.json"


def test_save_load_roundtrip_lands_in_data_dir(monkeypatch, tmp_path):
    """服务视角：设 AITR_DATA_DIR 后 save→load 落在数据根/config，不碰仓库 config/。"""
    _clear_env(monkeypatch)
    root = tmp_path / "inst" / "data"
    monkeypatch.setenv("AITR_DATA_DIR", str(root))
    ws.set_store_path(None)
    saved = ws.save_list([
        {"name": "tg", "format": "telegram", "token": "t", "target": "-100",
         "events": ["draft_backlog", "human_deliver", "host_alert"]},
    ])
    assert len(saved) == 1
    # 真的落在数据根/config（与 config.local.yaml 同目录），而非引擎根
    landed = root / "config" / "notify_webhooks.json"
    assert landed.is_file()
    got = ws.load()
    assert got and got[0]["name"] == "tg"
    assert set(got[0]["events"]) == {"draft_backlog", "human_deliver", "host_alert"}
