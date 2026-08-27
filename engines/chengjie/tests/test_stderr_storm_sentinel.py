"""stderr 风暴哨兵门禁（2026-08-27 06:11 宕机事故沉淀）。

boot err 日志不经 logging 体系——当日 45 分钟写满 1.5GB、实例被拖死，既有健康
面全程零告警。本哨兵在 watchdog tick 里对最新 ``boot_*.err.log`` 做绝对增量
判定（默认 15MB/间隔），超阈经 ``notify_host`` 点名（自带 30min 冷却）。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import src.inbox.health_watchdog as hw
from src.inbox.health_watchdog import stderr_storm_verdict

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_MB = 1024 * 1024


# ── 纯函数判定 ───────────────────────────────────────────────────────────────

def test_first_sight_builds_baseline_no_alert():
    v = stderr_storm_verdict(None, {"path": "a", "size": 900 * _MB},
                             growth_bytes=15 * _MB)
    assert v == {"alert": False, "grew": 0}, "首见只建基线——存量大文件不算风暴"


def test_growth_below_threshold_quiet():
    v = stderr_storm_verdict({"path": "a", "size": 0},
                             {"path": "a", "size": 14 * _MB},
                             growth_bytes=15 * _MB)
    assert v["alert"] is False and v["grew"] == 14 * _MB


def test_growth_at_threshold_alerts():
    v = stderr_storm_verdict({"path": "a", "size": 5 * _MB},
                             {"path": "a", "size": 21 * _MB},
                             growth_bytes=15 * _MB)
    assert v["alert"] is True and v["grew"] == 16 * _MB


def test_new_boot_file_resets_baseline():
    v = stderr_storm_verdict({"path": "boot_a.err.log", "size": 0},
                             {"path": "boot_b.err.log", "size": 500 * _MB},
                             growth_bytes=15 * _MB)
    assert v["alert"] is False, "换文件（新 boot）＝重建基线，不拿旧基线比新文件"


# ── watchdog 方法（__new__ 构造，与本仓 watchdog 测试同款）─────────────────────

def _wd(cfg_extra=None):
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    conf = {"health_watchdog": {"stderr_storm": {"enabled": True}}}
    if cfg_extra:
        conf["health_watchdog"]["stderr_storm"].update(cfg_extra)
    w._config_manager = SimpleNamespace(config=conf)
    return w


def test_watchdog_alerts_on_growth(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("src.utils.host_alert.notify_host",
                        lambda title, msg, **kw: calls.append((title, msg, kw)))
    logs = tmp_path / "logs"
    logs.mkdir()
    err = logs / "boot_20260827_052822.err.log"
    err.write_bytes(b"x" * 1024)
    w = _wd({"growth_mb": 1})
    w._check_stderr_storm(logs_dir=str(logs))          # 首见建基线
    assert calls == []
    err.write_bytes(b"x" * (3 * _MB))                  # 一个间隔涨 3MB（阈 1MB）
    w._check_stderr_storm(logs_dir=str(logs))
    assert len(calls) == 1
    assert calls[0][2].get("key") == "stderr_storm"
    assert w.total_stderr_storm_alerts == 1


def test_watchdog_quiet_without_growth_or_when_disabled(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("src.utils.host_alert.notify_host",
                        lambda *a, **kw: calls.append(1))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "boot_x.err.log").write_bytes(b"")
    w = _wd({"growth_mb": 1})
    w._check_stderr_storm(logs_dir=str(logs))
    w._check_stderr_storm(logs_dir=str(logs))          # 零增长
    assert calls == []
    w2 = _wd({"enabled": False})
    w2._check_stderr_storm(logs_dir=str(logs))
    assert calls == []


def test_watchdog_survives_missing_dir():
    w = _wd()
    w._check_stderr_storm(logs_dir=r"Z:\no\such\dir")  # 不抛即过


def test_tick_wires_the_check():
    src = (_ENGINE_ROOT / "src" / "inbox"
           / "health_watchdog.py").read_text(encoding="utf-8")
    assert "self._check_stderr_storm()" in src, "巡检 tick 未接线＝写了等于没写"
