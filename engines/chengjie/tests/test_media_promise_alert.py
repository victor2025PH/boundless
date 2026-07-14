"""Phase21a：出站媒体承诺未兑现升级告警（HealthWatchdog._check_media_promise）。

delta 口径累加「净撤回」（本窗口撤回 − 兑现），达阈值首提、周期重提，兑现追平恢复。
"""
from __future__ import annotations

import pytest

from src.inbox import health_watchdog as hw
from src.inbox import image_autosend as ia


class _FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, etype, data):
        self.events.append((etype, data))


def _watchdog(cfg: dict):
    from src.inbox.health_watchdog import HealthWatchdog

    class _CM:
        config = cfg

    class _App:
        class state:
            pass

    return HealthWatchdog(app=_App(), config_manager=_CM())


def _set_promise_counts(monkeypatch, retracted: int, fulfilled: int):
    """替换 image_autosend.metrics_snapshot 只暴露承诺计数（隔离进程级累计污染）。"""
    monkeypatch.setattr(
        ia, "metrics_snapshot",
        lambda: {"promise_retracted": retracted, "promise_fulfilled": fulfilled,
                 "fallback_reasons": {}})


@pytest.fixture
def _bus(monkeypatch):
    bus = _FakeBus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    # 语音同期回落上下文置空，避免读进程级真值
    from src.inbox import voice_autosend as va
    monkeypatch.setattr(va, "metrics_snapshot", lambda: {"fallback_reasons": {}})
    return bus


def test_first_tick_only_baseline(monkeypatch, _bus):
    cfg = {"health_watchdog": {"media_promise_remind": {"enabled": True}}}
    wd = _watchdog(cfg)
    _set_promise_counts(monkeypatch, retracted=5, fulfilled=1)
    wd._check_media_promise(now=1000.0)   # 首个周期只建基线（历史累计不误报）
    assert _bus.events == []
    assert wd._promise_last_ret == 5 and wd._promise_last_ful == 1


def test_escalation_and_reminder(monkeypatch, _bus):
    cfg = {"health_watchdog": {"media_promise_remind": {
        "enabled": True, "min_retracted": 3, "interval_min": 240}}}
    wd = _watchdog(cfg)
    t0 = 1_000_000.0
    _set_promise_counts(monkeypatch, 0, 0)
    wd._check_media_promise(now=t0)                    # baseline

    _set_promise_counts(monkeypatch, 2, 0)            # +2 撤回
    wd._check_media_promise(now=t0 + 300)
    assert _bus.events == []                          # 累计 2 < 3
    _set_promise_counts(monkeypatch, 3, 0)            # +1 撤回 → 累计 3 ≥ 阈值
    wd._check_media_promise(now=t0 + 600)
    assert len(_bus.events) == 1
    etype, data = _bus.events[0]
    assert etype == "media_promise_alert"
    assert data["net_retracted"] == 3 and data["reminder"] is False
    assert data["rate_key"] == "media_promise:remind"
    assert wd.total_media_promise_alerts == 1

    # 无新增（delta=0）不重提，也不衰减
    _set_promise_counts(monkeypatch, 3, 0)
    wd._check_media_promise(now=t0 + 900)
    assert len(_bus.events) == 1

    # 继续撤回但未到重提间隔 → 不重提
    _set_promise_counts(monkeypatch, 5, 0)
    wd._check_media_promise(now=t0 + 1200)
    assert len(_bus.events) == 1
    # 超过 interval → 重提
    _set_promise_counts(monkeypatch, 6, 0)
    wd._check_media_promise(now=t0 + 600 + 241 * 60)
    assert len(_bus.events) == 2
    assert _bus.events[1][1]["reminder"] is True


def test_recovery_when_fulfilled_catches_up(monkeypatch, _bus):
    cfg = {"health_watchdog": {"media_promise_remind": {
        "enabled": True, "min_retracted": 3}}}
    wd = _watchdog(cfg)
    t0 = 2_000_000.0
    _set_promise_counts(monkeypatch, 0, 0)
    wd._check_media_promise(now=t0)                    # baseline
    _set_promise_counts(monkeypatch, 4, 0)            # 净撤回 4 → 首提
    wd._check_media_promise(now=t0 + 300)
    assert len(_bus.events) == 1 and wd._promise_alerted

    # 两个 tick 兑现占优（撤回不增、兑现增）→ 判恢复
    _set_promise_counts(monkeypatch, 4, 2)            # d_ret=0 d_ful=2 → idle 1
    wd._check_media_promise(now=t0 + 600)
    _set_promise_counts(monkeypatch, 4, 4)            # d_ret=0 d_ful=2 → idle 2 → 恢复
    wd._check_media_promise(now=t0 + 900)
    assert _bus.events[-1][1].get("recovered") is True
    assert wd._promise_alerted is False and wd._promise_bad == 0


def test_disabled_no_op(monkeypatch, _bus):
    cfg = {"health_watchdog": {"media_promise_remind": {"enabled": False}}}
    wd = _watchdog(cfg)
    _set_promise_counts(monkeypatch, 10, 0)
    wd._check_media_promise(now=1.0)
    wd._check_media_promise(now=2.0)
    assert _bus.events == []


def test_async_fulfillment_counts_as_good(monkeypatch, _bus):
    """A 线异步兑现成功(fulfilled_async)算「好」、异步失败(fulfill_failed)算「坏」——
    只有 B 线同步 fulfilled 会漏判 A 线在正常工作的场景。"""
    cfg = {"health_watchdog": {"media_promise_remind": {
        "enabled": True, "min_retracted": 3}}}
    wd = _watchdog(cfg)

    def _counts(ret=0, ful=0, ful_async=0, ff=0):
        monkeypatch.setattr(
            ia, "metrics_snapshot",
            lambda: {"promise_retracted": ret, "promise_fulfilled": ful,
                     "promise_fulfilled_async": ful_async,
                     "promise_fulfill_failed": ff, "fallback_reasons": {}})

    t0 = 3_000_000.0
    _counts(); wd._check_media_promise(now=t0)                       # baseline
    # 4 次撤回 + 但 4 次异步兑现 → 净坏 0 → 不告警（旧口径会误报）
    _counts(ret=4, ful_async=4); wd._check_media_promise(now=t0 + 300)
    assert _bus.events == []
    # 异步失败也算坏：+3 fulfill_failed，无兑现 → 净坏 3 → 告警
    _counts(ret=4, ful_async=4, ff=3); wd._check_media_promise(now=t0 + 600)
    assert len(_bus.events) == 1
    assert _bus.events[0][1]["net_retracted"] == 3


def test_recovery_when_activity_stops(monkeypatch, _bus):
    """问题停止（不再有净坏，即便无兑现事件）→ 2 tick 后判恢复，不永久刷屏。"""
    cfg = {"health_watchdog": {"media_promise_remind": {
        "enabled": True, "min_retracted": 2}}}
    wd = _watchdog(cfg)
    t0 = 4_000_000.0
    _set_promise_counts(monkeypatch, 0, 0); wd._check_media_promise(now=t0)
    _set_promise_counts(monkeypatch, 3, 0); wd._check_media_promise(now=t0 + 300)
    assert wd._promise_alerted
    # 撤回停止、也没有兑现活动（计数不动）→ 连续 2 tick idle → 恢复
    _set_promise_counts(monkeypatch, 3, 0); wd._check_media_promise(now=t0 + 600)
    _set_promise_counts(monkeypatch, 3, 0); wd._check_media_promise(now=t0 + 900)
    assert _bus.events[-1][1].get("recovered") is True
    assert not wd._promise_alerted


def test_voice_fallback_context_attached(monkeypatch, _bus):
    from src.inbox import voice_autosend as va
    monkeypatch.setattr(
        va, "metrics_snapshot",
        lambda: {"fallback_reasons": {"7852_unready": 3, "edge_rejected": 1, "x": 9}})
    cfg = {"health_watchdog": {"media_promise_remind": {
        "enabled": True, "min_retracted": 2}}}
    wd = _watchdog(cfg)
    _set_promise_counts(monkeypatch, 0, 0)
    wd._check_media_promise(now=10.0)
    _set_promise_counts(monkeypatch, 3, 0)
    wd._check_media_promise(now=20.0)
    assert len(_bus.events) == 1
    vfb = _bus.events[0][1]["voice_fallback"]
    assert vfb == {"7852_unready": 3, "edge_rejected": 1}  # 只带相关键
