# -*- coding: utf-8 -*-
"""坐席字符额度强制闸 + 能力权限守卫接线 + watchdog 告警链门禁（2026-08-16）。

三段钉住的不变量：
1) ``check_request_quota`` 六态矩阵——enforce 默认关=软提醒先行；未登录/master/
   不限额/开关关一律放行但回填读数；**任何异常 fail-open**（额度闸绝不能把
   发送/翻译主链打挂）。
2) 静态接线钉——四个路由文件的能力守卫（resolve_user_perm 懒 import）与
   额度闸（check_request_quota）不被后续重构悄悄解线；send-voice 的额度闸
   必须在幂等占位 ``reserve`` **之前**（拦在占位前=零清理收尾）。
3) ``HealthWatchdog._check_agent_quota``——无人超限不发 / 超限首发（warn/over
   两桶+quota_alert_pct 按人生效）/ interval 内不重发 / 全部回落补恢复通知 /
   user_store 缺席、计量未开一律静默（误报比漏报更快摧毁信任）。
"""

from __future__ import annotations

import pathlib
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.inbox import health_watchdog as hw
from src.utils.agent_char_usage import (
    AgentCharUsageStore,
    check_request_quota,
    configure_agent_char_usage,
    reset_agent_char_usage_store,
)

_REPO = pathlib.Path(__file__).resolve().parents[1]
_ROUTES = _REPO / "src" / "web" / "routes"


@pytest.fixture(autouse=True)
def _isolated_singleton():
    reset_agent_char_usage_store()
    yield
    reset_agent_char_usage_store()


# ── 假体（对齐 test_agent_char_usage._FakeReq，多挂 role / user_store）────────


class _FakeUserStore:
    def __init__(self, rows: Dict[str, Dict[str, Any]]):
        self._rows = dict(rows)

    def get_user(self, username):
        return self._rows.get(username)

    def list_users(self):
        return list(self._rows.values())


class _BoomUserStore:
    def get_user(self, username):
        raise RuntimeError("db locked")

    def list_users(self):
        raise RuntimeError("db locked")


class _FakeReq:
    """最小 request 假体：session + app.state.{config_manager, user_store?}。"""

    def __init__(self, username="", role="agent", *, enabled=True,
                 enforce=True, user_store=None):
        self.session = ({"username": username, "role": role}
                        if username else {})
        state = SimpleNamespace()
        state.config_manager = SimpleNamespace(config={
            "usage": {"agent_chars": {"enabled": enabled, "enforce": enforce}}})
        if user_store is not None:
            state.user_store = user_store
        self.app = SimpleNamespace(state=state)


def _user_row(username: str, *, role="agent", quota=100, alert_pct=80) -> Dict[str, Any]:
    return {"username": username, "role": role,
            "monthly_char_quota": quota, "quota_alert_pct": alert_pct}


def _seed(username: str, chars: int) -> AgentCharUsageStore:
    """注入 :memory: 账本单例并预置本月用量。"""
    s = AgentCharUsageStore(":memory:")
    configure_agent_char_usage(store=s)
    if chars > 0:
        s.record(username, "translation", chars)
    return s


# ── 1) check_request_quota 六态矩阵 ──────────────────────────────────────────


def test_enforce_off_allows_but_fills_readings():
    """enforce 关（本批生产档）：超额也放行，但 level/used/quota 回填供软提醒。"""
    us = _FakeUserStore({"a": _user_row("a", quota=100)})
    _seed("a", 150)
    q = check_request_quota(_FakeReq("a", user_store=us, enforce=False))
    assert q["allowed"] is True
    assert q["enforce"] is False and q["enabled"] is True
    assert q["level"] == "over" and q["used"] == 150 and q["quota"] == 100
    # enabled 关同样不拦（enabled 或 enforce 任一关 → 放行）
    q2 = check_request_quota(_FakeReq("a", user_store=us, enabled=False))
    assert q2["allowed"] is True and q2["enabled"] is False


def test_master_role_immune_even_over_quota():
    """master 恒放行（额度管坐席，不闸管理者）；读数仍回填。"""
    us = _FakeUserStore({"boss": _user_row("boss", role="master", quota=100)})
    _seed("boss", 500)
    q = check_request_quota(_FakeReq("boss", role="master", user_store=us))
    assert q["allowed"] is True
    assert q["level"] == "over" and q["used"] == 500


def test_quota_zero_means_unlimited():
    us = _FakeUserStore({"a": _user_row("a", quota=0)})
    _seed("a", 999999)
    q = check_request_quota(_FakeReq("a", user_store=us))
    assert q["allowed"] is True
    assert q["level"] == "unlimited" and q["quota"] == 0


def test_exhausted_with_enforce_blocks():
    """用满（>=quota）且 enabled+enforce 双开 → 拦；文案占位数据齐备。"""
    us = _FakeUserStore({"a": _user_row("a", quota=100)})
    _seed("a", 100)   # 恰好用满：>= 语义即拦（再放行就是超额）
    q = check_request_quota(_FakeReq("a", user_store=us))
    assert q["allowed"] is False
    assert q["used"] == 100 and q["quota"] == 100 and q["pct"] == 100
    # 超额同拦
    _seed("b", 130)
    us2 = _FakeUserStore({"b": _user_row("b", quota=100)})
    q2 = check_request_quota(_FakeReq("b", user_store=us2))
    assert q2["allowed"] is False and q2["level"] == "over"


def test_missing_user_store_or_anonymous_allows():
    """user_store 未暴露（旧装配）/ 未登录（token 链）→ 放行（fail-open）。"""
    _seed("a", 10 ** 9)
    q = check_request_quota(_FakeReq("a"))          # app.state 无 user_store
    assert q["allowed"] is True
    q2 = check_request_quota(_FakeReq(""))          # 未登录
    assert q2["allowed"] is True


def test_any_exception_allows():
    """store 抛异常 / request 形状非法 → 一律放行（额度闸绝不打挂主链）。"""
    _seed("a", 10 ** 9)
    q = check_request_quota(_FakeReq("a", user_store=_BoomUserStore()))
    assert q["allowed"] is True
    q2 = check_request_quota(object())              # 非法 request 不抛
    assert q2["allowed"] is True


# ── 2) 静态接线钉（防后续重构悄悄解线）──────────────────────────────────────


def _route_src(name: str) -> str:
    return (_ROUTES / name).read_text(encoding="utf-8")


def test_capability_guard_wired_in_route_files():
    """四个路由文件都必须带能力守卫（_perm_ok 懒 import resolve_user_perm）。"""
    for fn in ("unified_inbox_translate_routes.py", "voice_routes.py",
               "unified_inbox_send_routes.py", "drafts_routes.py"):
        src = _route_src(fn)
        assert "_perm_ok" in src, f"{fn} 缺 _perm_ok 能力守卫"
        assert "resolve_user_perm" in src, f"{fn} 缺 resolve_user_perm 懒 import"


def test_quota_gate_wired_where_specified():
    """额度闸接线面：翻译主入口 / tts-test / send-voice 三处（drafts 只守卫不闸额度）。"""
    for fn in ("unified_inbox_translate_routes.py", "voice_routes.py",
               "unified_inbox_send_routes.py"):
        assert "check_request_quota" in _route_src(fn), f"{fn} 缺额度闸"


def test_send_voice_quota_gate_before_dedup_reserve():
    """send-voice 的额度闸必须在幂等占位之前——拦在 reserve 后就得补
    release + record_failed 两处收尾，选点错误会让被拦请求泄漏占位。"""
    src = _route_src("unified_inbox_send_routes.py")
    seg = src[src.index('"/api/unified-inbox/send-voice"'):]
    assert "check_request_quota" in seg, "send-voice 缺额度闸"
    assert seg.index("check_request_quota") < seg.index(".reserve("), \
        "send-voice 额度闸必须在 _dedup.reserve 之前"


def test_accounting_wired():
    """归因记账接线面：compare/image/document（translate 域）、send 出站预翻译、
    drafts 草稿翻译——都必须还在调 record_request_chars。"""
    assert _route_src("unified_inbox_translate_routes.py").count(
        "record_request_chars") >= 4   # 主入口 + compare + image + document
    assert "record_request_chars" in _route_src("unified_inbox_send_routes.py")
    assert "record_request_chars" in _route_src("drafts_routes.py")


# ── 3) watchdog._check_agent_quota ───────────────────────────────────────────


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


def _quota_wd(monkeypatch, *, users, store=None, remind=None,
              metering_enabled=True, user_store_present=True):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    if store is not None:
        configure_agent_char_usage(store=store)
    conf: Dict[str, Any] = {
        "usage": {"agent_chars": {"enabled": metering_enabled}},
        "health_watchdog": {"agent_quota_remind": dict(
            {"enabled": True}, **(remind or {}))},
    }
    state = SimpleNamespace()
    if user_store_present:
        state.user_store = _FakeUserStore({u["username"]: u for u in users})
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(state=state)
    w._config_manager = SimpleNamespace(config=conf)
    w._aq_alerted = False
    w._aq_last_remind = 0.0
    w.total_agent_quota_alerts = 0
    return w, bus


def test_watchdog_quiet_paths(monkeypatch):
    """不超限不发 / 计量关静默 / user_store 缺席静默 / 巡检开关关静默。"""
    ts = time.time()
    s = AgentCharUsageStore(":memory:")
    s.record("a", "tts", 50)
    w, bus = _quota_wd(monkeypatch, users=[_user_row("a", quota=100)], store=s)
    w._check_agent_quota(now=ts)
    assert bus.events == []
    # 计量未开：超额也静默（父开关默认开无噪音的原因）
    s2 = AgentCharUsageStore(":memory:")
    s2.record("a", "tts", 500)
    w2, bus2 = _quota_wd(monkeypatch, users=[_user_row("a", quota=100)],
                         store=s2, metering_enabled=False)
    w2._check_agent_quota(now=ts)
    assert bus2.events == []
    # user_store 未暴露：静默不猜
    w3, bus3 = _quota_wd(monkeypatch, users=[], store=s2,
                         user_store_present=False)
    w3._check_agent_quota(now=ts)
    assert bus3.events == []
    # 巡检开关显式关
    w4, bus4 = _quota_wd(monkeypatch, users=[_user_row("a", quota=100)],
                         store=s2, remind={"enabled": False})
    w4._check_agent_quota(now=ts)
    assert bus4.events == []


def test_watchdog_alert_buckets_and_payload(monkeypatch):
    """超限首发：over/warn 两桶按 quota_alert_pct 分箱，payload 明细齐备。"""
    s = AgentCharUsageStore(":memory:")
    s.record("over1", "translation", 12100)   # 12100/10000 → over(121%)
    s.record("warn1", "tts", 8400)            # 84% ≥ 80 → warn
    s.record("calm1", "tts", 8400)            # 84% < alert_pct(90) → 不报
    s.record("free1", "tts", 99999)           # quota=0 → 不限额跳过
    users = [
        _user_row("over1", quota=10000),
        _user_row("warn1", quota=10000, alert_pct=80),
        _user_row("calm1", quota=10000, alert_pct=90),
        _user_row("free1", quota=0),
    ]
    w, bus = _quota_wd(monkeypatch, users=users, store=s)
    ts = time.time()
    w._check_agent_quota(now=ts)

    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "agent_quota_alert"
    assert p["over_count"] == 1 and p["warn_count"] == 1
    assert p["over"][0]["username"] == "over1" and p["over"][0]["pct"] == 121
    assert p["warn"][0]["username"] == "warn1" and p["warn"][0]["pct"] == 84
    assert p["month"] and p["reminder"] is False
    assert p["rate_key"] == "agent_quota:remind"
    assert w.total_agent_quota_alerts == 1


def test_watchdog_reminder_throttled_then_reminds(monkeypatch):
    s = AgentCharUsageStore(":memory:")
    s.record("a", "tts", 200)
    w, bus = _quota_wd(monkeypatch, users=[_user_row("a", quota=100)],
                       store=s, remind={"interval_min": 360})
    ts = time.time()
    w._check_agent_quota(now=ts)
    w._check_agent_quota(now=ts + 60)             # interval 内：静默
    assert len(bus.events) == 1
    w._check_agent_quota(now=ts + 361 * 60)       # 过窗：重提且标 reminder
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True


def test_watchdog_recovery_after_all_clear(monkeypatch):
    """全部回落（调额/月初账本清零）→ 恢复通知一次并复位状态。"""
    s = AgentCharUsageStore(":memory:")
    s.record("a", "tts", 200)
    w, bus = _quota_wd(monkeypatch, users=[_user_row("a", quota=100)], store=s)
    ts = time.time()
    w._check_agent_quota(now=ts)
    assert len(bus.events) == 1 and w._aq_alerted
    # 账本清零（等价于月初自然月切换）→ 恢复通知
    configure_agent_char_usage(store=AgentCharUsageStore(":memory:"))
    w._check_agent_quota(now=ts + 60)
    assert len(bus.events) == 2
    assert bus.events[1][1].get("recovered") is True
    assert bus.events[1][1]["rate_key"] == "agent_quota:recovered"
    assert w._aq_alerted is False
    # 再跑一轮：没告过警就不再发恢复（防重复恢复噪音）
    w._check_agent_quota(now=ts + 120)
    assert len(bus.events) == 2
