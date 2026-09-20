"""账号发送额度「今日发送量」只读聚合（设置页 + CLI 同源）。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from src.inbox.send_gate_today import (
    collect_send_gate_today, data_root_from_config_path,
)
from src.integrations.account_registry import _DDL as _REG_DDL
from src.integrations.protocol_autoreply_limits import _SEND_DDL
from src.skills.account_health import warmup_cap

_NOW = 1_700_000_000.0
_DAY = 86400.0


def _write_registry(root: Path, rows):
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    db = cfg / "account_registry.db"
    con = sqlite3.connect(str(db))
    try:
        con.executescript(_REG_DDL)
        for plat, acct, created, status in rows:
            con.execute(
                "INSERT INTO platform_accounts "
                "(platform, account_id, status, created_at) VALUES (?,?,?,?)",
                (plat, acct, status, created),
            )
        con.commit()
    finally:
        con.close()


def _write_sends(root: Path, rows):
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    db = cfg / "account_sends.db"
    con = sqlite3.connect(str(db))
    try:
        con.executescript(_SEND_DDL)
        for key, ts in rows:
            con.execute(
                "INSERT INTO account_sends (account_key, ts) VALUES (?,?)",
                (key, ts),
            )
        con.commit()
    finally:
        con.close()


def test_data_root_from_config_path(tmp_path):
    assert data_root_from_config_path(None) is None
    assert data_root_from_config_path("") is None
    # 单测形态：config.yaml 直接落数据根
    p = tmp_path / "config.yaml"
    assert data_root_from_config_path(str(p)) == tmp_path
    # 生产形态：…/data/config/config.yaml → 数据根是 data
    nested = tmp_path / "data" / "config" / "config.yaml"
    nested.parent.mkdir(parents=True)
    assert data_root_from_config_path(str(nested)) == tmp_path / "data"


def test_missing_registry_unavailable(tmp_path):
    snap = collect_send_gate_today(tmp_path, {}, now=_NOW)
    assert snap["available"] is False
    assert snap["rows"] == []
    assert snap["gate"]["enabled"] is False


def test_empty_registry_available_no_rows(tmp_path):
    _write_registry(tmp_path, [])
    snap = collect_send_gate_today(tmp_path, {"companion_send_gate": {"enabled": True}},
                                  now=_NOW)
    assert snap["available"] is True
    assert snap["rows"] == []
    assert snap["gate"]["enabled"] is True


def test_gate_on_blocks_over_cap(tmp_path):
    created = _NOW - 30 * _DAY
    _write_registry(tmp_path, [("telegram", "acct1", created, "online")])
    sends = [("telegram:acct1", _NOW - i * 60) for i in range(20)]
    _write_sends(tmp_path, sends)
    cfg = {"companion_send_gate": {
        "enabled": True, "target_cap": 15, "warmup_start_cap": 2,
        "warmup_ramp_days": 14, "reserve_for_manual": 0,
    }}
    snap = collect_send_gate_today(tmp_path, cfg, now=_NOW)
    assert snap["available"] is True
    row = snap["rows"][0]
    assert row["account"] == "telegram:acct1"
    assert row["used_24h"] == 20
    expect_cap = warmup_cap(30.0, 15, start_cap=2, ramp_days=14)
    assert row["cap"] == expect_cap == 15
    assert row["auto_verdict"] == "BLOCK"
    # D-Q2（Q-4 #267）：额度永不限制人工——闸门开着、额度用尽，人工仍 ok
    assert row["manual_verdict"] == "ok"
    assert row["auto_pct"] >= 100 and row["warn80"] is True


def test_gate_off_lists_usage_without_block(tmp_path):
    created = _NOW - 30 * _DAY
    _write_registry(tmp_path, [("telegram", "acct1", created, "online")])
    sends = [("telegram:acct1", _NOW - i * 60) for i in range(20)]
    _write_sends(tmp_path, sends)
    snap = collect_send_gate_today(
        tmp_path, {"companion_send_gate": {"enabled": False, "target_cap": 15}},
        now=_NOW)
    row = snap["rows"][0]
    assert row["used_24h"] == 20
    assert row["cap"] == 15
    assert row["auto_verdict"] == "-"
    assert row["manual_verdict"] == "-"
    assert snap["gate"]["enabled"] is False


def test_reserve_splits_auto_vs_manual(tmp_path):
    created = _NOW - 30 * _DAY
    _write_registry(tmp_path, [("telegram", "acct1", created, "online")])
    sends = [("telegram:acct1", _NOW - i * 60) for i in range(12)]
    _write_sends(tmp_path, sends)
    cfg = {"companion_send_gate": {
        "enabled": True, "target_cap": 15, "warmup_start_cap": 2,
        "warmup_ramp_days": 14, "reserve_for_manual": 5,
    }}
    snap = collect_send_gate_today(tmp_path, cfg, now=_NOW)
    row = snap["rows"][0]
    assert row["used_24h"] == 12
    assert row["cap"] == 15
    assert row["auto_cap"] == 10
    assert row["auto_verdict"] == "BLOCK"    # 12 >= 10
    assert row["manual_verdict"] == "ok"     # 12 < 15
