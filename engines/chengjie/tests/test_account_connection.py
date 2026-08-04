"""账号接入时刻登记（AccountConnectionLog）门禁：自举 / observe-once / 持久化 / 容错。"""
from __future__ import annotations

from src.inbox.account_connection import AccountConnectionLog

NOW = 1_000_000.0
HOUR = 3600.0


def test_observe_seeds_from_earliest_activity():
    log = AccountConnectionLog(None)  # 纯内存
    earliest = NOW - 10 * HOUR
    conn = log.observe("telegram", "123", earliest, now=NOW)
    assert conn == earliest
    assert log.get_connected_at("telegram", "123") == earliest


def test_observe_once_freezes_first_value():
    log = AccountConnectionLog(None)
    first = log.observe("telegram", "123", NOW - 100 * HOUR, now=NOW)
    # 后续扫描只看到更晚的会话（>N 窗口漂移）→ 不得覆盖首见值。
    second = log.observe("telegram", "123", NOW - 1 * HOUR, now=NOW)
    assert first == second == NOW - 100 * HOUR


def test_unknown_earliest_uses_now():
    log = AccountConnectionLog(None)
    assert log.observe("telegram", "a", 0.0, now=NOW) == NOW
    assert log.observe("telegram", "b", -5.0, now=NOW) == NOW


def test_future_earliest_clamped_to_now():
    # 时钟漂移到未来 → 用 now（保守：按 now 起算预热窗，不会「一接入就过期」）。
    log = AccountConnectionLog(None)
    assert log.observe("telegram", "c", NOW + 999 * HOUR, now=NOW) == NOW


def test_unseen_account_returns_none():
    log = AccountConnectionLog(None)
    assert log.get_connected_at("telegram", "nope") is None


def test_accounts_are_isolated_by_platform_and_id():
    log = AccountConnectionLog(None)
    log.observe("telegram", "1", NOW - 50 * HOUR, now=NOW)
    log.observe("whatsapp", "1", NOW - 5 * HOUR, now=NOW)
    assert log.get_connected_at("telegram", "1") == NOW - 50 * HOUR
    assert log.get_connected_at("whatsapp", "1") == NOW - 5 * HOUR


def test_persistence_roundtrip(tmp_path):
    p = tmp_path / "account_connection.json"
    log = AccountConnectionLog(p)
    log.observe("telegram", "777", NOW - 20 * HOUR, now=NOW)
    assert p.exists()
    # 新实例从盘加载，读到同一冻结值。
    log2 = AccountConnectionLog(p)
    assert log2.get_connected_at("telegram", "777") == NOW - 20 * HOUR


def test_corrupt_file_degrades_gracefully(tmp_path):
    p = tmp_path / "account_connection.json"
    p.write_text("{ not valid json ", "utf-8")
    log = AccountConnectionLog(p)  # 不得抛
    assert log.get_connected_at("telegram", "x") is None
    # 坏文件之后仍能正常记录（覆盖写回合法 JSON）。
    conn = log.observe("telegram", "x", NOW - HOUR, now=NOW)
    assert conn == NOW - HOUR
    assert AccountConnectionLog(p).get_connected_at("telegram", "x") == NOW - HOUR
