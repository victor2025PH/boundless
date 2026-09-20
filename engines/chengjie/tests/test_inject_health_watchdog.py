"""内嵌网页端「选择器持续失配」告警出口契约（2026-08-10）。

桌面壳把官方网页端嵌进 webview 再注入脚本做翻译/消息回流，这套模式的长期命门是
**官方一改版选择器就失配**。此前失配只在壳层状态条 + 运营看板可见，没有任何外发
出口——即本仓反复吃过的「报进虚空」。本文件钉住最后一公里：

``InjectHealthStore.due_reminders`` → ``HealthWatchdog._check_inject_health``
→ EventBus ``inject_health_alert`` → webhook 别名 ``inject_health``。

重点覆盖**不该告警**的路径（误报两次这告警就再没人信）：陈旧上报（壳已关）、
未到首提龄、节流窗内、开关关闭、无 extract 的老客户端。
"""

from __future__ import annotations

import types

import pytest


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    import src.web.desktop_inject_health as dih
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(dih, "_STORE", None, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    yield


def _store():
    from src.web.desktop_inject_health import get_inject_health_store
    return get_inject_health_store()


def _events():
    from src.integrations.shared.event_bus import get_event_bus
    return [e for e in get_event_bus().recent_events(50)
            if e["type"] == "inject_health_alert"]


def _watchdog(config=None):
    from src.inbox.health_watchdog import HealthWatchdog
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    return HealthWatchdog(app=app,
                          config_manager=types.SimpleNamespace(config=config or {}),
                          interval_sec=60)


def _report(store, ts, **kw):
    """一条心跳上报（默认＝composer 找不到的失配态）。"""
    rec = {"platform": "telegram", "account_id": "telegram:100",
           "supported": True, "composer": False, "bubbles": 3,
           "chatOpen": True, "ts": ts}
    rec.update(kw)
    return store.record(rec)


# ── 主链路：失配 → 首提 → 节流 → 重提 ──────────────────────────────────────

def test_watchdog_alerts_after_persist_window():
    s = _store()
    _report(s, 1000)
    _report(s, 2500)            # 心跳还在（不陈旧），失配已 25min > 默认 20min
    wd = _watchdog()
    wd._check_inject_health(now=2500)
    evs = _events()
    assert len(evs) == 1
    d = evs[0]["data"]
    assert d["status"] == "mismatch_composer"
    assert d["field"] == "composer"          # 该去校准哪个选择器
    assert d["down_minutes"] == 25
    assert d["first_reminder"] is True
    assert d["rate_key"] == "inject:telegram\ttelegram:100"
    assert wd.total_inject_health_reminders == 1
    # 紧接着复查不重发（节流状态在 store 内）
    _report(s, 2530)
    wd._check_inject_health(now=2530)
    assert len(_events()) == 1


def test_watchdog_silent_before_persist_window():
    s = _store()
    _report(s, 1000)
    _report(s, 1300)            # 才 5 分钟
    _watchdog()._check_inject_health(now=1300)
    assert _events() == []


def test_watchdog_skips_stale_reports():
    """壳已关/账号已卸：库里最后那条失配记录不能永久催人修没在跑的 webview。"""
    s = _store()
    _report(s, 1000)
    _watchdog()._check_inject_health(now=1000 + 86400)
    assert _events() == []


def test_watchdog_respects_disable_flag():
    s = _store()
    _report(s, 1000)
    _report(s, 2500)
    wd = _watchdog({"health_watchdog": {"inject_health_remind": {"enabled": False}}})
    wd._check_inject_health(now=2500)
    assert _events() == []


def test_watchdog_silent_when_healthy():
    s = _store()
    _report(s, 1000, composer=True)     # ok
    _watchdog()._check_inject_health(now=1000 + 86400)
    assert _events() == []


# ── 提取器类失效：元素都在，但取不出内容 ────────────────────────────────────

def test_watchdog_reports_extract_failure_field():
    """selectors 全绿也可能坏透——文案必须指向 bubbleText 而不是某个没坏的选择器。"""
    s = _store()
    ex = {"decorated": 0, "unresolved": 9, "ingestTried": 9, "ingestKeyed": 9}
    _report(s, 1000, composer=True, extract=ex)
    _report(s, 2500, composer=True, extract=ex)
    _watchdog()._check_inject_health(now=2500)
    d = _events()[0]["data"]
    assert d["status"] == "mismatch_text"
    assert d["field"] == "bubbleText"
    assert d["missing_selectors"] == []      # 元素确实都在，别误导


def test_watchdog_reports_ingest_failure_field():
    s = _store()
    ex = {"decorated": 5, "unresolved": 0, "ingestTried": 5, "ingestKeyed": 0}
    _report(s, 1000, composer=True, extract=ex)
    _report(s, 2500, composer=True, extract=ex)
    _watchdog()._check_inject_health(now=2500)
    d = _events()[0]["data"]
    assert d["status"] == "mismatch_ingest"
    assert d["field"] == "mid"


def test_watchdog_silent_for_legacy_client_without_extract():
    """老壳不上报 extract → 不得凭空判成提取器失效（向后兼容）。"""
    s = _store()
    _report(s, 1000, composer=True)
    _report(s, 2500, composer=True)
    _watchdog()._check_inject_health(now=2500)
    assert _events() == []


# ── 附加事实：缺失选择器清单 / 通用兜底标记 ──────────────────────────────────

def test_alert_carries_missing_selectors_and_generic_flag():
    s = _store()
    kw = {"generic": True,
          "selectors": {"composer": False, "sendBtn": False,
                        "bubble": True, "peerTitle": True}}
    _report(s, 1000, **kw)
    _report(s, 2500, **kw)
    _watchdog()._check_inject_health(now=2500)
    d = _events()[0]["data"]
    assert sorted(d["missing_selectors"]) == ["composer", "sendBtn"]
    assert d["generic"] is True


def test_multiple_accounts_get_separate_rate_keys():
    """多账号同时坏不能共挤 notifier 的「每小时一条」窗口。"""
    s = _store()
    for acct in ("telegram:100", "telegram:200"):
        _report(s, 1000, account_id=acct)
        _report(s, 2500, account_id=acct)
    _watchdog()._check_inject_health(now=2500)
    keys = {e["data"]["rate_key"] for e in _events()}
    assert len(keys) == 2


# ── 告警链登记：别名/受众/文案（漏一环就是「报进虚空」）──────────────────────

def test_alias_registered_and_technical_audience():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, alert_audience
    assert "inject_health_alert" in _EVENT_ALIASES["inject_health"]["types"]
    assert alert_audience("inject_health") == "technical"


@pytest.mark.parametrize("status,field,expect", [
    ("mismatch_composer", "composer", "输入框"),
    ("mismatch_text", "bubbleText", "取不出文字"),
    ("mismatch_ingest", "mid", "消息 id"),
])
def test_formatter_renders_per_symptom_copy(status, field, expect):
    """三类症状修法不同，文案必须分开——混成一句等于把运维引去改错地方。"""
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("inject_health_alert", {
        "platform": "telegram", "account_id": "telegram:100",
        "status": status, "field": field, "down_minutes": 35,
    })
    assert "选择器失配" in title and "35 分钟" in title
    assert expect in text
    assert field in text
    assert "desktop_selector_profiles.json" in text     # 指路修法
