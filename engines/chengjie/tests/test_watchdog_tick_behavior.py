# -*- coding: utf-8 -*-
"""补齐两个只有「提及」没有「真调用」的巡检（2026-08-27）。

## 为什么非要真调用

``_tick`` 把每个 ``_check_*`` 包在 ``try/except: logger.debug`` 里，运行时错误
（缺导入的 NameError、改名后的 AttributeError、签名漂移的 TypeError）会让那个检查
**从此不再运行**而日志只有一条 DEBUG。当天实锤：``_check_true_probes`` 漏了个函数内
``pathlib`` 导入，探针从重启起整段不跑——而当时**源码级断言全绿**。
``assert "_check_x" in src`` 证明的是「代码里写着」，不是「跑得起来」。

``tools/watchdog_check_audit.py`` 用 AST 把两者分开统计，实测 56 个巡检里 54 个已有
真调用测试；本文件补上剩下两个，把棘轮 ``_KNOWN_NO_INVOKE`` 清空。

## 手法

不构造真 ``HealthWatchdog``（它的 ``__init__`` 依赖一大票外部对象），而是用
``SimpleNamespace`` 假 self + 未绑定方法调用——只需提供该方法真正读到的那几个属性。
外部副作用在**源模块**上 monkeypatch（这些方法都是函数内 import，调用时才取模块属性，
所以打补丁生效）。每条都同时钉两件事：**开着时真跑到了**、**关着时全程静默**。
"""
from __future__ import annotations

import types
from typing import Any, Dict, List

from src.inbox.health_watchdog import HealthWatchdog


def _fake(cfg: Dict[str, Any], **attrs: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        _config_manager=types.SimpleNamespace(config=cfg), **attrs)


# ── _check_tg_history_autosync ────────────────────────────────────────────


def _autosync_cfg(enabled: bool) -> Dict[str, Any]:
    return {"inbox": {"tg_history_autosync": {"enabled": enabled}}}


def test_tg_history_autosync_runs_and_counts(monkeypatch):
    """开着时：真调到触发核心、计数按启动账号数累加、节流时间戳推进。"""
    import src.web.routes.unified_inbox_account_routes as routes

    calls: List[Any] = []
    monkeypatch.setattr(routes, "maybe_autostart_tg_history_sync",
                        lambda app, cfg: calls.append(app) or ["a1", "a2"])

    app = object()
    fake = _fake(_autosync_cfg(True), _last_tg_autosync_ts=0.0, _app=app,
                 total_tg_autosyncs=0)
    HealthWatchdog._check_tg_history_autosync(fake, now=1000.0)

    assert calls == [app], "没真调到触发核心（早返回了？）"
    assert fake.total_tg_autosyncs == 2          # 按启动账号数累加，不是 +1
    assert fake._last_tg_autosync_ts == 1000.0   # 节流基准已推进


def test_tg_history_autosync_throttles_and_respects_switch(monkeypatch):
    """15 分钟节流内不重复触发；开关关着全程静默（未启用部署零开销）。"""
    import src.web.routes.unified_inbox_account_routes as routes

    calls: List[Any] = []
    monkeypatch.setattr(routes, "maybe_autostart_tg_history_sync",
                        lambda app, cfg: calls.append(1) or [])

    fake = _fake(_autosync_cfg(True), _last_tg_autosync_ts=1000.0,
                 _app=object(), total_tg_autosyncs=0)
    HealthWatchdog._check_tg_history_autosync(fake, now=1000.0 + 899.0)
    assert calls == [], "节流窗内不该触发"
    HealthWatchdog._check_tg_history_autosync(fake, now=1000.0 + 901.0)
    assert len(calls) == 1, "过了节流窗应触发一次"

    off = _fake(_autosync_cfg(False), _last_tg_autosync_ts=0.0,
                _app=object(), total_tg_autosyncs=0)
    HealthWatchdog._check_tg_history_autosync(off, now=9e9)
    assert len(calls) == 1, "开关关着不该触发"


def test_tg_history_autosync_no_app_is_safe(monkeypatch):
    """拿不到 app 时安全返回——巡检自身绝不能成为新的故障面。"""
    import src.web.routes.unified_inbox_account_routes as routes

    monkeypatch.setattr(routes, "maybe_autostart_tg_history_sync",
                        lambda app, cfg: (_ for _ in ()).throw(
                            AssertionError("无 app 时不该调用")))
    fake = _fake(_autosync_cfg(True), _last_tg_autosync_ts=0.0, _app=None,
                 total_tg_autosyncs=0)
    HealthWatchdog._check_tg_history_autosync(fake, now=1000.0)   # 不抛即通过


# ── _check_mutual_chat ────────────────────────────────────────────────────


def _mc_env(monkeypatch, *, enabled: bool, items: List[Dict[str, Any]],
            due: List[Dict[str, Any]]):
    """把 mutual_chat 的三个纯核心与事件总线都换成可观测替身。"""
    import src.inbox.mutual_chat_monitor as mcm
    import src.integrations.shared.event_bus as bus

    seen: Dict[str, Any] = {"scan": 0, "published": []}
    monkeypatch.setattr(mcm, "mutual_chat_cfg", lambda cfg: {
        "enabled": enabled, "interval_min": 60, "window_hours": 6,
        "remind_interval_hours": 12})
    monkeypatch.setattr(mcm, "scan_mutual_chat",
                        lambda store, mc, now=None: seen.__setitem__(
                            "scan", seen["scan"] + 1) or items)
    monkeypatch.setattr(mcm, "filter_due_reminders",
                        lambda it, ledger, now=None, remind_interval_hours=0: due)
    monkeypatch.setattr(bus, "get_event_bus", lambda: types.SimpleNamespace(
        publish=lambda name, payload: seen["published"].append((name, payload))))
    return seen


def test_mutual_chat_scans_and_publishes_one_aggregated_alert(monkeypatch):
    """开着且有到期会话：真扫了、且**聚合成一条**事件（逐会话逐条＝噪音）。"""
    due = [{"conversation_id": "tg:1"}, {"conversation_id": "tg:2"}]
    seen = _mc_env(monkeypatch, enabled=True, items=due, due=due)

    store = object()
    fake = _fake({}, _mutual_chat_last_scan=0.0,
                 _app=types.SimpleNamespace(
                     state=types.SimpleNamespace(inbox_store=store)))
    HealthWatchdog._check_mutual_chat(fake, now=2000.0)

    assert seen["scan"] == 1, "没真扫（早返回了？）"
    assert len(seen["published"]) == 1, "两个会话必须聚合成一条告警"
    name, payload = seen["published"][0]
    assert name == "ai_mutual_chat_alert"
    assert payload["rate_key"] == "ai_mutual_chat"      # 限流键，防多会话挤窗
    assert len(payload["conversations"]) == 2
    assert fake._mutual_chat_last_scan == 2000.0
    assert isinstance(getattr(fake, "_mutual_chat_ledger", None), dict)


def test_mutual_chat_silent_when_disabled_or_nothing_due(monkeypatch):
    """关着 / 无会话 / 无到期 → 一个字都不发（纯观测子系统不许制造噪音）。"""
    seen = _mc_env(monkeypatch, enabled=False, items=[{"conversation_id": "x"}],
                   due=[{"conversation_id": "x"}])
    fake = _fake({}, _mutual_chat_last_scan=0.0,
                 _app=types.SimpleNamespace(
                     state=types.SimpleNamespace(inbox_store=object())))
    HealthWatchdog._check_mutual_chat(fake, now=2000.0)
    assert seen["scan"] == 0 and seen["published"] == []

    seen2 = _mc_env(monkeypatch, enabled=True, items=[], due=[])
    fake2 = _fake({}, _mutual_chat_last_scan=0.0,
                  _app=types.SimpleNamespace(
                      state=types.SimpleNamespace(inbox_store=object())))
    HealthWatchdog._check_mutual_chat(fake2, now=2000.0)
    assert seen2["scan"] == 1 and seen2["published"] == []

    # 扫到了但都没到重提间隔 → 同样不发
    seen3 = _mc_env(monkeypatch, enabled=True,
                    items=[{"conversation_id": "y"}], due=[])
    fake3 = _fake({}, _mutual_chat_last_scan=0.0,
                  _app=types.SimpleNamespace(
                      state=types.SimpleNamespace(inbox_store=object())))
    HealthWatchdog._check_mutual_chat(fake3, now=2000.0)
    assert seen3["scan"] == 1 and seen3["published"] == []


def test_mutual_chat_no_store_is_safe(monkeypatch):
    """拿不到 inbox_store（纯 web 部署等）→ 安全返回，不抛不发。"""
    seen = _mc_env(monkeypatch, enabled=True,
                   items=[{"conversation_id": "z"}], due=[{"conversation_id": "z"}])
    fake = _fake({}, _mutual_chat_last_scan=0.0,
                 _app=types.SimpleNamespace(
                     state=types.SimpleNamespace(inbox_store=None)))
    HealthWatchdog._check_mutual_chat(fake, now=2000.0)
    assert seen["published"] == []
