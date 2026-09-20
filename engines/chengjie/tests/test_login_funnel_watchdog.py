"""账号接入链路停摆告警（HealthWatchdog._check_login_funnel）。

2026-07-25 事故的第二半：LINE 协议扫码 100% 失败烂了多日。当时补了
``login_funnel_stats`` 计数 + ops 卡 stalled 标记，但那是**被动**的——看板得有人打开，
而登录链路坏掉时通常没人正盯着运营总览。本检查把同一判据变成推送。

重点覆盖那些**不该吵**的路径：零流量、样本不足、没有新尝试的重提、有成功即闭嘴。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

from src.inbox import health_watchdog as hw


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _row(key: str, started: int, authorized: int, *, since: int = -1,
         reasons: Dict[str, int] | None = None,
         legacy: bool = False) -> Dict[str, Any]:
    """``since``＝距上次成功以来的 started（缺省＝从未成功，等于累计）。

    ``legacy=True`` 模拟升级窗口内的旧快照（无 since 字段）。
    """
    d_started = started if since < 0 else since
    row: Dict[str, Any] = {
        "key": key, "started": started, "authorized": authorized,
        "qr_shown": started, "pin_issued": 0,
        "failed": max(0, started - authorized),
        "reasons": dict(reasons or {}),
        "avg_authorized_ms": 0,
    }
    if not legacy:
        row.update({"started_since_success": d_started,
                    "qr_shown_since_success": d_started,
                    "failed_since_success": d_started,
                    "stalled": d_started >= 3})
    return row


def _wd(monkeypatch, rows, cfg_extra=None):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    monkeypatch.setattr(
        "src.integrations.login_funnel_stats.get_login_funnel_stats",
        lambda: SimpleNamespace(dump=lambda: {"rows": list(rows)}))
    conf: Dict[str, Any] = {"health_watchdog": {"login_funnel_remind": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["login_funnel_remind"].update(cfg_extra)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace()
    w._config_manager = SimpleNamespace(config=conf)
    w._lf_base = {}
    w._lf_alerted = {}
    w._lf_last_remind = {}
    w.total_login_funnel_alerts = 0
    return w, bus


def test_alerts_when_attempts_pile_up_without_a_single_success(monkeypatch):
    rows = [_row("line:protocol", 9, 0, reasons={"pin_timeout": 7, "network": 2})]
    w, bus = _wd(monkeypatch, rows)
    w._check_login_funnel(now=1000.0)

    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "login_funnel_alert"
    assert (p["platform"], p["mode"]) == ("line", "protocol")
    assert p["started"] == 9 and p["reminder"] is False
    # 归因分布必须原样带上：全是 checkpoint 就别去查代码了
    assert p["reasons"] == {"pin_timeout": 7, "network": 2}
    assert p["rate_key"] == "login_funnel:line:protocol"
    assert w.total_login_funnel_alerts == 1


def test_regression_after_past_success_still_alerts(monkeypatch):
    """本函数存在的理由：昨天能登、今天全挂，绝不能因历史成功而永久静默。

    累计口径（``authorized == 0``）在这里会闭嘴——而回归比「从没通过的新链路」
    更隐蔽、更该报。
    """
    rows = [_row("messenger:web", 12, 9, since=0)]
    w, bus = _wd(monkeypatch, rows)
    w._check_login_funnel(now=1000.0)
    assert bus.events == []

    rows[0] = _row("messenger:web", 20, 9, since=8, reasons={"checkpoint": 8})
    w._check_login_funnel(now=2000.0)
    assert len(bus.events) == 1
    assert bus.events[0][1]["started"] == 8       # 只报「上次成功以来」的新流量


def test_legacy_snapshot_without_since_field_degrades_safely(monkeypatch):
    """升级窗口内的旧快照没有 since 字段：退回累计口径，少报不误报。"""
    w, bus = _wd(monkeypatch, [_row("messenger:web", 20, 9, legacy=True)])
    w._check_login_funnel(now=1000.0)
    assert bus.events == []                        # 有过成功 → 旧口径闭嘴（可接受）

    w2, bus2 = _wd(monkeypatch, [_row("line:protocol", 9, 0, legacy=True)])
    w2._check_login_funnel(now=1000.0)
    assert len(bus2.events) == 1                   # 从没成功过仍照报


def test_silent_on_zero_traffic_and_small_samples(monkeypatch):
    """零流量天然静默（这是本检查敢默认开的前提）；样本不足也不报。"""
    w, bus = _wd(monkeypatch, [_row("telegram:protocol", 0, 0),
                               _row("whatsapp:protocol", 3, 0)])
    w._check_login_funnel(now=1000.0)
    assert bus.events == []


def test_counter_reset_is_not_mistaken_for_success(monkeypatch):
    """进程重启 / stats.reset 让计数回退——不能被当成「又成功了一次」而发恢复。"""
    rows = [_row("line:protocol", 14, 4, since=10)]
    w, bus = _wd(monkeypatch, rows)
    w._check_login_funnel(now=1000.0)
    assert len(bus.events) == 1

    rows[0] = _row("line:protocol", 2, 0)          # 计数归零后又攒了 2 次
    w._check_login_funnel(now=2000.0)
    assert len(bus.events) == 1                    # 回退不是成功，不发恢复
    assert w._lf_base["line:protocol"] == 0


def test_no_new_attempts_means_no_reminder(monkeypatch):
    """夜里没人再试就别刷屏——重提要求 started 真的又涨了，不是单纯等够 4 小时。"""
    rows = [_row("line:protocol", 9, 0)]
    w, bus = _wd(monkeypatch, rows)
    w._check_login_funnel(now=1000.0)
    assert len(bus.events) == 1

    w._check_login_funnel(now=1000.0 + 10 * 3600)   # 时间够了，但没有新尝试
    assert len(bus.events) == 1

    rows[0] = _row("line:protocol", 12, 0)          # 又撞了三次墙
    w._check_login_funnel(now=1000.0 + 11 * 3600)
    assert len(bus.events) == 2 and bus.events[1][1]["reminder"] is True


def test_new_attempts_still_respect_interval(monkeypatch):
    """有新尝试但还在间隔内也不重提（否则每 tick 都叫）。"""
    rows = [_row("line:protocol", 9, 0)]
    w, bus = _wd(monkeypatch, rows)
    w._check_login_funnel(now=1000.0)
    rows[0] = _row("line:protocol", 30, 0)
    w._check_login_funnel(now=1060.0)
    assert len(bus.events) == 1


def test_recovery_notice_and_state_reset(monkeypatch):
    rows = [_row("line:protocol", 9, 0)]
    w, bus = _wd(monkeypatch, rows)
    w._check_login_funnel(now=1000.0)

    rows[0] = _row("line:protocol", 10, 1, since=0)
    w._check_login_funnel(now=3000.0)
    assert bus.events[-1][1]["recovered"] is True
    assert w._lf_alerted == {} and w._lf_last_remind == {}

    # 恢复后不再重复发恢复通知
    w._check_login_funnel(now=4000.0)
    assert len(bus.events) == 2


def test_disabled_and_other_bucket_skipped(monkeypatch):
    w, bus = _wd(monkeypatch, [_row("line:protocol", 9, 0)],
                 cfg_extra={"enabled": False})
    w._check_login_funnel(now=1000.0)
    assert bus.events == []

    # __other__ 是溢出桶（多个脏 key 混在一起），报出来无法处置
    w2, bus2 = _wd(monkeypatch, [_row("__other__", 50, 0)])
    w2._check_login_funnel(now=1000.0)
    assert bus2.events == []


def test_stats_failure_is_silent(monkeypatch):
    """取数异常绝不能把巡检打挂（观测失败 < 巡检停摆）。"""
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    def _boom():
        raise RuntimeError("stats down")

    monkeypatch.setattr(
        "src.integrations.login_funnel_stats.get_login_funnel_stats", _boom)
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace()
    w._config_manager = SimpleNamespace(
        config={"health_watchdog": {"login_funnel_remind": {"enabled": True}}})
    w._lf_base = {}
    w._lf_alerted = {}
    w._lf_last_remind = {}
    w.total_login_funnel_alerts = 0
    w._check_login_funnel(now=1000.0)
    assert bus.events == []
