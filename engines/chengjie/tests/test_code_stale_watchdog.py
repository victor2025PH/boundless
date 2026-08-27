# -*- coding: utf-8 -*-
"""实施74 阶段4 门禁（实施69 P1-4）：messenger worker 代码分叉告警。

实锤：实施68 修复在盘上躺 27 小时没装载——worker 自报 `code_stale:true`
只躺在 /accounts 响应里无人消费。本检查把它接进 notify_host 升级提醒。
重点覆盖「不该告警」的路径（正常发布窗缓冲/未启用/不可达/恢复）。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import src.inbox.health_watchdog as hw


def _wd(snap: Optional[Dict[str, Any]], *, enabled=True, web_on=True,
        after_min=30, cfg_extra=None):
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    conf: Dict[str, Any] = {
        "health_watchdog": {"code_stale_remind": {
            "enabled": enabled, "after_min": after_min, "interval_min": 240}},
        "platform_login": {"messenger": {"web_enabled": web_on}},
    }
    if cfg_extra:
        conf.update(cfg_extra)
    w._config_manager = SimpleNamespace(config=conf)
    w._fetch_messenger_worker_snapshot = lambda: snap  # type: ignore
    return w


def _hooked_notify(monkeypatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def _fake(title, body, *, key="", cooldown_sec=0.0, **kw):
        calls.append({"title": title, "body": body, "key": key})
        return True

    monkeypatch.setattr("src.utils.host_alert.notify_host", _fake)
    return calls


def test_stale_below_threshold_stays_silent(monkeypatch):
    """首见 stale 只立基准；未满 after_min 不吵（给发布窗留缓冲）。"""
    calls = _hooked_notify(monkeypatch)
    w = _wd({"code_stale": True, "boot_ts": 1})
    w._check_messenger_code_stale(now=1000.0)   # 立基准
    w._check_messenger_code_stale(now=1000.0 + 10 * 60)  # 10min < 30min
    assert calls == []


def test_stale_past_threshold_alerts(monkeypatch):
    calls = _hooked_notify(monkeypatch)
    w = _wd({"code_stale": True, "boot_ts": 1})
    w._check_messenger_code_stale(now=1000.0)
    w._check_messenger_code_stale(now=1000.0 + 31 * 60)
    assert len(calls) == 1
    assert calls[0]["key"] == "msgr_code_stale"
    assert "27" in calls[0]["body"]  # 事故语境进文案（27 小时实锤）
    assert w.total_code_stale_alerts == 1


def test_recovery_notice_only_after_alert(monkeypatch):
    """恢复通知要有「曾告警」前提——抖动恢复不发（防噪）。"""
    calls = _hooked_notify(monkeypatch)
    w = _wd({"code_stale": False})
    w._check_messenger_code_stale(now=1000.0)
    assert calls == []  # 从没 stale 过 → 静默

    w2 = _wd({"code_stale": True, "boot_ts": 1})
    w2._check_messenger_code_stale(now=1000.0)
    w2._check_messenger_code_stale(now=1000.0 + 31 * 60)
    assert len(calls) == 1
    w2._fetch_messenger_worker_snapshot = lambda: {"code_stale": False}
    w2._check_messenger_code_stale(now=1000.0 + 40 * 60)
    assert len(calls) == 2 and calls[1]["key"] == "msgr_code_stale_ok"
    # 状态清零：再 stale 从头计时
    w2._fetch_messenger_worker_snapshot = lambda: {"code_stale": True}
    w2._check_messenger_code_stale(now=1000.0 + 50 * 60)
    w2._check_messenger_code_stale(now=1000.0 + 60 * 60)  # 10min < 30min
    assert len(calls) == 2


def test_unreachable_worker_keeps_state_and_silence(monkeypatch):
    """worker 不可达 → 静默且不动计时基准（可达性归 session 提醒管）。"""
    calls = _hooked_notify(monkeypatch)
    w = _wd({"code_stale": True, "boot_ts": 1})
    w._check_messenger_code_stale(now=1000.0)
    w._fetch_messenger_worker_snapshot = lambda: None
    w._check_messenger_code_stale(now=1000.0 + 31 * 60)
    assert calls == []
    # 恢复可达且仍 stale → 按原基准立即告警（不重新计时）
    w._fetch_messenger_worker_snapshot = lambda: {"code_stale": True}
    w._check_messenger_code_stale(now=1000.0 + 32 * 60)
    assert len(calls) == 1


def test_disabled_or_web_off_noop(monkeypatch):
    calls = _hooked_notify(monkeypatch)
    w = _wd({"code_stale": True}, enabled=False)
    w._check_messenger_code_stale(now=1.0)
    w2 = _wd({"code_stale": True}, web_on=False)
    w2._check_messenger_code_stale(now=1.0)
    assert calls == []


def test_wired_into_tick():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "inbox"
           / "health_watchdog.py").read_text(encoding="utf-8")
    assert "self._check_messenger_code_stale()" in src
