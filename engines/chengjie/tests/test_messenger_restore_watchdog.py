# -*- coding: utf-8 -*-
"""Messenger 注册表×sidecar 对账巡检门禁（_check_messenger_not_restored）。

重点覆盖「不该告警」的路径（本仓告警纪律：宁可漏报不误报）：
sidecar 不可达不对账、offline 号不催、未告警过的抖动恢复不发恢复通知、
web_enabled 关清零状态。该盲区的背景：零事件零心跳的账号在
_check_platform_sessions / _check_inbox_read_stall 里都隐形。
"""
from src.inbox.health_watchdog import HealthWatchdog


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, etype, payload):
        self.events.append((etype, dict(payload)))


def _snap(accounts, *, reachable=True, web=True):
    return {"sources": {"sidecar_reachable": reachable},
            "gates": {"web_effective": web},
            "accounts": accounts}


def _missing(aid="A1", status="online"):
    return {"account_id": aid, "in_registry": True, "in_sidecar": False,
            "registry_status": status}


def _present(aid="A1"):
    return {"account_id": aid, "in_registry": True, "in_sidecar": True,
            "registry_status": "online"}


def _wd(monkeypatch, holder, cfg=None):
    bus = _Bus()
    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: bus)
    import src.integrations.messenger_readiness_collect as mrc
    monkeypatch.setattr(mrc, "collect_messenger_readiness",
                        lambda c, now=None: holder["snap"])

    class _CM:
        config = cfg if cfg is not None else {}

    wd = HealthWatchdog.__new__(HealthWatchdog)   # 绕开重依赖 __init__
    wd._config_manager = _CM()
    return wd, bus


T0 = 100_000.0
MIN = 60.0


def _alerts(bus, status="not_restored"):
    return [p for e, p in bus.events
            if e == "platform_session_alert" and p.get("status") == status]


def test_first_alert_only_after_after_min():
    holder = {"snap": _snap([_missing()])}
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        wd, bus = _wd(mp, holder)
        wd._check_messenger_not_restored(now=T0)
        wd._check_messenger_not_restored(now=T0 + 10 * MIN)
        assert not bus.events, "30min 首提阈值之前不得告警"
        wd._check_messenger_not_restored(now=T0 + 31 * MIN)
        al = _alerts(bus)
        assert len(al) == 1
        p = al[0]
        assert p["platform"] == "messenger" and p["account_id"] == "A1"
        assert p["reminder"] is True and p["down_minutes"] >= 30
        assert p["rate_key"] == "messenger:A1:not_restored"
        assert wd.total_messenger_restore_reminders == 1
    finally:
        mp.undo()


def test_reremind_respects_interval(monkeypatch):
    holder = {"snap": _snap([_missing()])}
    wd, bus = _wd(monkeypatch, holder)
    wd._check_messenger_not_restored(now=T0)
    wd._check_messenger_not_restored(now=T0 + 31 * MIN)      # 首提
    wd._check_messenger_not_restored(now=T0 + 90 * MIN)      # <4h 不重提
    assert len(_alerts(bus)) == 1
    wd._check_messenger_not_restored(now=T0 + 31 * MIN + 241 * MIN)
    assert len(_alerts(bus)) == 2


def test_offline_account_never_nagged(monkeypatch):
    holder = {"snap": _snap([_missing(status="offline")])}
    wd, bus = _wd(monkeypatch, holder)
    wd._check_messenger_not_restored(now=T0)
    wd._check_messenger_not_restored(now=T0 + 600 * MIN)
    assert not bus.events, "offline（运营登出/未登录）不催——UI/CLI 负责提示"


def test_sidecar_only_account_ignored(monkeypatch):
    row = {"account_id": "X", "in_registry": False, "in_sidecar": False,
           "registry_status": ""}
    holder = {"snap": _snap([row])}
    wd, bus = _wd(monkeypatch, holder)
    wd._check_messenger_not_restored(now=T0 + 600 * MIN)
    assert not bus.events


def test_sidecar_unreachable_skips_and_does_not_start_timer(monkeypatch):
    holder = {"snap": _snap([_missing()], reachable=False)}
    wd, bus = _wd(monkeypatch, holder)
    wd._check_messenger_not_restored(now=T0)
    assert not bus.events
    # sidecar 恢复可达后计时才开始：T0+35min 不告警（差集从 T0+5min 起算）
    holder["snap"] = _snap([_missing()])
    wd._check_messenger_not_restored(now=T0 + 5 * MIN)
    wd._check_messenger_not_restored(now=T0 + 34 * MIN)
    assert not bus.events, "不可达期间不得偷偷计时"
    wd._check_messenger_not_restored(now=T0 + 36 * MIN)
    assert len(_alerts(bus)) == 1


def test_recovery_notice_only_after_alert(monkeypatch):
    holder = {"snap": _snap([_missing()])}
    wd, bus = _wd(monkeypatch, holder)
    # 抖动：缺席 5min 即恢复（从未告警）→ 不发恢复通知
    wd._check_messenger_not_restored(now=T0)
    holder["snap"] = _snap([_present()])
    wd._check_messenger_not_restored(now=T0 + 5 * MIN)
    assert not bus.events
    # 真告警后恢复 → restored 通知一次 + 状态清零（再缺席重新计时）
    holder["snap"] = _snap([_missing()])
    wd._check_messenger_not_restored(now=T0 + 10 * MIN)
    wd._check_messenger_not_restored(now=T0 + 41 * MIN)
    assert len(_alerts(bus)) == 1
    holder["snap"] = _snap([_present()])
    wd._check_messenger_not_restored(now=T0 + 50 * MIN)
    rec = _alerts(bus, status="restored")
    assert len(rec) == 1 and rec[0]["recovered"] is True
    holder["snap"] = _snap([_missing()])
    wd._check_messenger_not_restored(now=T0 + 60 * MIN)
    wd._check_messenger_not_restored(now=T0 + 80 * MIN)
    assert len(_alerts(bus)) == 1, "恢复后重新缺席须重新计满 after_min"


def test_disabled_via_config(monkeypatch):
    holder = {"snap": _snap([_missing()])}
    cfg = {"health_watchdog": {"messenger_restore_remind": {"enabled": False}}}
    wd, bus = _wd(monkeypatch, holder, cfg=cfg)
    wd._check_messenger_not_restored(now=T0 + 600 * MIN)
    assert not bus.events


def test_web_disabled_clears_state(monkeypatch):
    holder = {"snap": _snap([_missing()])}
    wd, bus = _wd(monkeypatch, holder)
    wd._check_messenger_not_restored(now=T0)              # 开始计时
    holder["snap"] = _snap([_missing()], web=False)
    wd._check_messenger_not_restored(now=T0 + 10 * MIN)   # 关闸 → 清零
    holder["snap"] = _snap([_missing()])
    wd._check_messenger_not_restored(now=T0 + 31 * MIN)   # 重开：从头计
    assert not bus.events
    wd._check_messenger_not_restored(now=T0 + 10 * MIN + 31 * MIN + 31 * MIN)
    assert len(_alerts(bus)) == 1


def test_collector_failure_is_silent(monkeypatch):
    bus = _Bus()
    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: bus)
    import src.integrations.messenger_readiness_collect as mrc

    def _boom(c, now=None):
        raise RuntimeError("collect down")

    monkeypatch.setattr(mrc, "collect_messenger_readiness", _boom)

    class _CM:
        config = {}

    wd = HealthWatchdog.__new__(HealthWatchdog)
    wd._config_manager = _CM()
    wd._check_messenger_not_restored(now=T0)
    assert not bus.events
