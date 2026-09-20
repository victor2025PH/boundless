# -*- coding: utf-8 -*-
"""入站「半死态」闭环契约（P0，2026-08-04 Messenger 黑洞事故沉淀）。

实测事故形态：账号 ``status=authorized``（登录探测绿——有输入框、有会话列表），但
cookie 快照只恢复了登录态、E2EE 设备密钥没恢复 → 消息区永久卡 Loading。表现为
「侧栏有未读、进线程读取持续全失败、零入站」，而轮询一路「健康」——第一个受害账号
掉线 4 天无人知，第二个账号全天零入站同样零告警。本文件钉死三段契约：

1. ``PlatformSessionHealth.record_inbox_health``：stall 判定（有未读 + 滚动窗全失败
   + 最小样本数）、边沿语义（went_stalled / recovered）、``due_inbox_stalls`` 升级式节流；
2. ``/api/internal/protocol/inbox-health``：外部 worker（messenger-web）心跳落库
   （只记录不即时告警——告警统一走看门狗，防抖）；
3. ``HealthWatchdog._check_inbox_read_stall``：stalled 持续 after_min → 经
   ``platform_session_alert``（status=inbox_stalled）外发，恢复自动停，
   运营自己登出的号（注册表非 online）不催。

重点覆盖**不该告警**的路径（本仓告警纪律：宁可漏报不误报，告警失信比没有更糟）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox import health_watchdog as hw
from src.web.routes.unified_inbox_account_routes import register_account_routes


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    """每例独立的健康单例 + EventBus（防跨测试污染）。"""
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    yield


def _store():
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    return get_platform_session_health()


def _client():
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    return TestClient(app)


class _Bus:
    def __init__(self) -> None:
        self.events: List[tuple] = []

    def publish(self, name: str, payload: Dict[str, Any]) -> None:
        self.events.append((name, payload))


# ── store 纯语义 ─────────────────────────────────────────────────────────────

def test_stall_requires_unread_and_full_window_failure():
    s = _store()
    # 空闲：无未读无样本 → 不 stall
    r = s.record_inbox_health("messenger", "A", unread=0,
                              read_attempts=0, read_fails=0)
    assert not r["stalled"] and not r["went_stalled"]

    # 有未读但读取有成功 → 不 stall（读得到内容就不是半死）
    r = s.record_inbox_health("messenger", "A", unread=5,
                              read_attempts=4, read_fails=3)
    assert not r["stalled"]

    # 全失败但无未读 → 不 stall（没有内容可读 ≠ 事故；防空账号误报）
    r = s.record_inbox_health("messenger", "A", unread=0,
                              read_attempts=4, read_fails=4)
    assert not r["stalled"]

    # 有未读 + 窗口全失败 → stalled（边沿 went_stalled 只在进入时给一次）
    r = s.record_inbox_health("messenger", "A", unread=5,
                              read_attempts=4, read_fails=4)
    assert r["stalled"] and r["went_stalled"]
    assert r["hint_code"] == "e2ee_relogin"
    assert s.dump()["stall_funnel"]["went"] == 1
    r = s.record_inbox_health("messenger", "A", unread=5,
                              read_attempts=5, read_fails=5)
    assert r["stalled"] and not r["went_stalled"]  # 持续态不重复边沿

    # 恢复：真读到了内容 → recovered + 计时清零
    r = s.record_inbox_health("messenger", "A", unread=5,
                              read_attempts=5, read_fails=2)
    assert r["recovered"] and not r["stalled"]
    assert float(s.inbox_health()["messenger:A"]["stall_since"]) == 0.0
    assert s.dump()["stall_funnel"]["recovered"] == 1


def test_stall_funnel_relogin_and_hint_code():
    from src.integrations.platform_session_health import derive_inbox_hint
    assert derive_inbox_hint(stalled=True, detail="e2ee_relogin") == "e2ee_relogin"
    assert derive_inbox_hint(stalled=True, stall_kind="read_fail") == "e2ee_relogin"
    assert derive_inbox_hint(stalled=False) == ""

    s = _store()
    s.record_inbox_health("messenger", "B", unread=2, read_attempts=3,
                          read_fails=3, detail="e2ee_relogin")
    assert s.inbox_health()["messenger:B"]["hint_code"] == "e2ee_relogin"
    s.record_relogin("messenger", "B")
    assert s.dump()["stall_funnel"]["relogin"] == 1
    assert "platform_inbox_stall_went_total" in s.dump_prom()
    assert "platform_session_relogin_total" in s.dump_prom()


def test_single_failure_below_min_attempts_not_stalled():
    """单次导航超时不算黑洞——最小样本数护栏（宁可漏报不误报）。"""
    s = _store()
    r = s.record_inbox_health("messenger", "A", unread=3,
                              read_attempts=1, read_fails=1)
    assert not r["stalled"]


def test_placeholder_blind_branch_detects_zero_sample_lockout():
    """支二（P1）：零读取样本盲区——「有未读 + 会话列表大面积 E2EE 加密占位」。

    被动读取信号只在「预览变化 → 进线程」时产生样本；存量未读、预览恒为占位时
    读取窗永远是空的（支一永无数据）。占位比例是零导航零副作用的连续信号。
    """
    s = _store()
    # 命中：有未读 + 零样本 + 14 个会话里 86% 占位
    r = s.record_inbox_health("messenger", "A", unread=5,
                              read_attempts=0, read_fails=0,
                              e2ee_ratio=12 / 14, conv_count=14)
    assert r["stalled"] and r["went_stalled"]
    assert s.inbox_health()["messenger:A"]["stall_kind"] == "e2ee_placeholder"
    # 恢复：占位比例回落（重登后预览可读）
    r = s.record_inbox_health("messenger", "A", unread=5,
                              read_attempts=0, read_fails=0,
                              e2ee_ratio=0.1, conv_count=14)
    assert r["recovered"]


def test_placeholder_blind_guards_against_false_alarms():
    s = _store()
    # 旧 worker 不带 ratio（-1=未知）→ 不判
    r = s.record_inbox_health("messenger", "A", unread=5,
                              read_attempts=0, read_fails=0)
    assert not r["stalled"]
    # 会话太少（小账号/页面未加载全）→ 不判
    r = s.record_inbox_health("messenger", "B", unread=5,
                              read_attempts=0, read_fails=0,
                              e2ee_ratio=1.0, conv_count=3)
    assert not r["stalled"]
    # 无未读 → 不判（没有内容可读 ≠ 事故）
    r = s.record_inbox_health("messenger", "C", unread=0,
                              read_attempts=0, read_fails=0,
                              e2ee_ratio=1.0, conv_count=14)
    assert not r["stalled"]
    # 有读取样本且有成功 → 支一支二都不命中（读得到内容就不是锁）
    r = s.record_inbox_health("messenger", "D", unread=5,
                              read_attempts=4, read_fails=2,
                              e2ee_ratio=0.9, conv_count=14)
    assert not r["stalled"]
    assert s.inbox_health()["messenger:D"]["stall_kind"] == ""


def test_steady_fail_branch_detects_unread_zero_lockout():
    """支三（P3，198 实测形态）：读取窗全败 + 大面积占位，但 unread 已被失败读取
    消费成 0——支一（要 unread≥1）与支二（要零样本）都漏判，账号一路零告警。"""
    s = _store()
    # 命中：unread=0 + 读取 4/4 全败 + 19 会话 74% 占位
    r = s.record_inbox_health("messenger", "A", unread=0,
                              read_attempts=4, read_fails=4,
                              e2ee_ratio=0.74, conv_count=19)
    assert r["stalled"] and r["went_stalled"]
    assert r["hint_code"] == "e2ee_relogin"
    assert s.inbox_health()["messenger:A"]["stall_kind"] == "steady_fail"


def test_steady_fail_guards_against_false_alarms():
    s = _store()
    # 读取全败但占位比例低（真空会话/对端撤回）→ 不误报
    r = s.record_inbox_health("messenger", "A", unread=0,
                              read_attempts=4, read_fails=4,
                              e2ee_ratio=0.1, conv_count=19)
    assert not r["stalled"]
    # 占位高但读取有成功（解密其实可用）→ 不报
    r = s.record_inbox_health("messenger", "B", unread=0,
                              read_attempts=4, read_fails=1,
                              e2ee_ratio=0.8, conv_count=19)
    assert not r["stalled"]
    # 会话太少（小账号）→ 不判
    r = s.record_inbox_health("messenger", "C", unread=0,
                              read_attempts=4, read_fails=4,
                              e2ee_ratio=1.0, conv_count=3)
    assert not r["stalled"]


def test_pin_required_hint_flows_through_from_worker_detail():
    """worker 确认缺 PIN → detail=e2ee_pin_required → 提示码原样透传
    （比泛泛 e2ee_relogin 更精确，坐席该做的是填 PIN 而非完整重登）。"""
    from src.integrations.platform_session_health import derive_inbox_hint
    assert derive_inbox_hint(
        stalled=True, detail="e2ee_pin_required") == "e2ee_pin_required"
    s = _store()
    s.record_inbox_health("messenger", "P", unread=3, read_attempts=3,
                          read_fails=3, detail="e2ee_pin_required")
    assert s.inbox_health()["messenger:P"]["hint_code"] == "e2ee_pin_required"


def test_pin_missing_branch_stalls_without_statistical_evidence():
    """支四（P0 2026-08-14，173 实测盲区回归钉）：worker 亲证 PIN 浮层在场
    （detail=e2ee_pin_required 只在 detectPinPrompt 确认后携带）→ 无条件半死。

    173 实测形态：占位比 0.39（低于 0.6 阈值）、读取窗零失败 → 前三支全不命中，
    stall_since=0 把弹窗/横幅/看门狗全闸掉，坐席全程零 PIN 提示。浮层本身就是
    确定性证据，不需要统计旁证。
    """
    s = _store()
    # 173 形态：占位比 0.39 + 零读取样本 + detail 带 PIN 码 → 必须 stall
    r = s.record_inbox_health("messenger", "A", unread=2,
                              read_attempts=0, read_fails=0,
                              e2ee_ratio=0.39, conv_count=18,
                              detail="e2ee_pin_required")
    assert r["stalled"] and r["went_stalled"]
    assert r["hint_code"] == "e2ee_pin_required"
    h = s.inbox_health()["messenger:A"]
    assert h["stall_kind"] == "pin_missing"
    # 甚至零未读也 stall（PIN 缺失与未读数无关——半死是解密能力缺失）
    r = s.record_inbox_health("messenger", "B", unread=0,
                              read_attempts=0, read_fails=0,
                              e2ee_ratio=0.2, conv_count=18,
                              detail="e2ee_pin_required")
    assert r["stalled"]
    # 恢复：PIN 配好自愈后 worker 不再带该 detail → recovered，计时清零
    r = s.record_inbox_health("messenger", "A", unread=2,
                              read_attempts=2, read_fails=0,
                              e2ee_ratio=0.1, conv_count=18, detail="")
    assert r["recovered"] and not r["stalled"]
    assert float(s.inbox_health()["messenger:A"]["stall_since"]) == 0.0
    # 护栏：其他 detail 值不触发支四（只认精确码，防任意字符串误升格）
    r = s.record_inbox_health("messenger", "C", unread=2,
                              read_attempts=0, read_fails=0,
                              e2ee_ratio=0.2, conv_count=18,
                              detail="something_else")
    assert not r["stalled"]


def test_pin_missing_kind_yields_to_statistical_branches():
    """支一命中时 stall_kind 归 read_fail（更具体的失败形态优先），但 hint 码
    仍按 detail 透传 e2ee_pin_required——两字段语义正交。"""
    s = _store()
    s.record_inbox_health("messenger", "P2", unread=3, read_attempts=3,
                          read_fails=3, detail="e2ee_pin_required")
    h = s.inbox_health()["messenger:P2"]
    assert h["stall_kind"] == "read_fail"
    assert h["hint_code"] == "e2ee_pin_required"


def test_bad_inputs_clamped_softly():
    s = _store()
    # fails 超过 attempts / 非法类型：夹紧不抛
    r = s.record_inbox_health("messenger", "A", unread="7",
                              read_attempts="3", read_fails="9")
    assert r["stalled"]  # 夹紧后 3/3 全败 + 未读 7
    h = s.inbox_health()["messenger:A"]
    assert h["read_fails"] == 3 and h["unread"] == 7
    r = s.record_inbox_health("messenger", "A", unread=None,
                              read_attempts=object(), read_fails=None)
    assert not r["stalled"]  # 解析失败一律归零 → 不判 stall


def test_due_inbox_stalls_escalation_throttle():
    s = _store()
    s.record_inbox_health("messenger", "B", unread=3,
                          read_attempts=3, read_fails=3)
    # 刚进入 stall：min_age 内不 due
    assert s.due_inbox_stalls(min_age_sec=1200, interval_sec=14400) == {}
    # 拨龄 40 分钟 → due 一次（原子标记 last_remind_ts）
    s._inbox_health["messenger:B"]["stall_since"] = time.time() - 2400
    due = s.due_inbox_stalls(min_age_sec=1200, interval_sec=14400)
    assert list(due.keys()) == ["messenger:B"]
    assert due["messenger:B"]["down_sec"] >= 2400
    # interval 内不重复
    assert s.due_inbox_stalls(min_age_sec=1200, interval_sec=14400) == {}
    # 恢复心跳清零两个时间戳 → 之后不再 due
    s.record_inbox_health("messenger", "B", unread=3,
                          read_attempts=3, read_fails=0)
    assert s.due_inbox_stalls(min_age_sec=0, interval_sec=0) == {}


def test_dump_and_prom_expose_inbox_health():
    s = _store()
    s.record_inbox_health("messenger", "A", unread=2,
                          read_attempts=3, read_fails=3)
    s.record_inbox_health("messenger", "ok", unread=1,
                          read_attempts=3, read_fails=0)
    d = s.dump()
    assert d["inbox_stalled"] == ["messenger:A"]
    assert d["inbox_stalled_count"] == 1
    assert d["inbox_health"]["messenger:ok"]["read_fails"] == 0
    prom = s.dump_prom()
    assert 'platform_inbox_stalled{session="messenger:A"} 1' in prom
    assert 'platform_inbox_stalled{session="messenger:ok"} 0' in prom


def test_inbox_health_distinct_key_cap():
    s = _store()
    for i in range(200):
        s.record_inbox_health("messenger", f"acct{i}", unread=1,
                              read_attempts=2, read_fails=2)
    assert len(s.inbox_health()) <= 64


# ── push 端点 ────────────────────────────────────────────────────────────────

def test_endpoint_records_heartbeat():
    c = _client()
    r = c.post("/api/internal/protocol/inbox-health", json={
        "platform": "messenger", "account_id": "100",
        "unread": 5, "read_attempts": 4, "read_fails": 4,
        "last_inbound_ts": 1780000000,
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "stalled": True}
    h = _store().inbox_health()["messenger:100"]
    assert h["unread"] == 5 and h["last_inbound_ts"] == 1780000000.0
    # 心跳只记录不即时告警（告警统一由看门狗升级式外发）
    from src.integrations.shared.event_bus import get_event_bus
    assert [e for e in get_event_bus().recent_events(20)
            if e["type"] == "platform_session_alert"] == []


def test_endpoint_pin_heal_observability_passthrough():
    """P1（2026-08-14）：PIN 自愈战绩随心跳落健康行——新 worker 带
    pin_state/pin_heal_* 则原样存；老 worker 不带 → 不写 pin_heal 键
    （「没报」与「报了 0」必须可区分，否则看板会把老 worker 显成零尝试）。"""
    c = _client()
    r = c.post("/api/internal/protocol/inbox-health", json={
        "platform": "messenger", "account_id": "PH1",
        "unread": 2, "read_attempts": 0, "read_fails": 0,
        "e2ee_ratio": 0.39, "conv_count": 18,
        "detail": "e2ee_pin_required",
        "pin_state": "missing",
        "pin_heal_attempts": 3, "pin_heal_ok": 1, "pin_heal_fail": 2,
    })
    assert r.status_code == 200 and r.json()["stalled"] is True
    h = _store().inbox_health()["messenger:PH1"]
    assert h["pin_state"] == "missing"
    assert h["pin_heal"] == {"attempts": 3, "ok": 1, "fail": 2}
    # 老 worker：不带 pin 键 → pin_state 空、pin_heal 键不存在
    c.post("/api/internal/protocol/inbox-health", json={
        "platform": "messenger", "account_id": "PH2",
        "unread": 5, "read_attempts": 4, "read_fails": 4,
    })
    h2 = _store().inbox_health()["messenger:PH2"]
    assert h2["pin_state"] == "" and "pin_heal" not in h2
    # 脏值（负数/非数字）不落 pin_heal、不抛
    r = c.post("/api/internal/protocol/inbox-health", json={
        "platform": "messenger", "account_id": "PH3",
        "unread": 1, "pin_heal_attempts": "x", "pin_state": 12345,
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    h3 = _store().inbox_health()["messenger:PH3"]
    assert "pin_heal" not in h3 and h3["pin_state"] == "12345"


def test_endpoint_missing_fields_rejected_softly():
    c = _client()
    r = c.post("/api/internal/protocol/inbox-health",
               json={"platform": "messenger"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    r = c.post("/api/internal/protocol/inbox-health", json={})
    assert r.json()["ok"] is False


def test_endpoint_bad_payload_never_crashes():
    c = _client()
    r = c.post("/api/internal/protocol/inbox-health", json={
        "platform": "messenger", "account_id": "100",
        "unread": "x", "read_attempts": None, "read_fails": {"a": 1},
        "last_inbound_ts": "y",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True  # 非法数值归零记录，不抛


# ── 看门狗升级提醒 ───────────────────────────────────────────────────────────

def _wd(monkeypatch, *, enabled: bool = True, expected_online: bool = True,
        after_min: float = 20, interval_min: float = 240):
    bus = _Bus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    monkeypatch.setattr(
        hw.HealthWatchdog, "_session_expected_online",
        staticmethod(lambda key: expected_online))
    conf = {"health_watchdog": {"inbox_read_stall_remind": {
        "enabled": enabled, "after_min": after_min,
        "interval_min": interval_min}}}
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._config_manager = SimpleNamespace(config=conf)
    w.total_inbox_read_stall_reminders = 0
    return w, bus


def _make_stalled(key_acct: str = "100", *, aged_sec: float = 2400.0):
    s = _store()
    s.record_inbox_health("messenger", key_acct, unread=4,
                          read_attempts=5, read_fails=5)
    s._inbox_health[f"messenger:{key_acct}"]["stall_since"] = (
        time.time() - aged_sec)
    return s


def test_watchdog_alerts_on_aged_stall(monkeypatch):
    _make_stalled()
    w, bus = _wd(monkeypatch)
    w._check_inbox_read_stall(now=time.time())
    assert len(bus.events) == 1
    name, p = bus.events[0]
    assert name == "platform_session_alert"
    assert p["status"] == "inbox_stalled"
    assert p["platform"] == "messenger" and p["account_id"] == "100"
    assert p["rate_key"].endswith(":inbox_stall")  # 独立限流键，不挤掉线告警的窗
    assert p["unread"] == 4 and p["down_minutes"] >= 40
    assert "读不到" in p["detail"]
    assert w.total_inbox_read_stall_reminders == 1
    # 同轮之后 interval 内不重复
    w._check_inbox_read_stall(now=time.time())
    assert len(bus.events) == 1


def test_watchdog_placeholder_kind_gets_distinct_copy(monkeypatch):
    """支二命中的告警文案必须点名「加密占位」而非「读取连败 0/0」——
    运维照 0/0 排查读取链路是死胡同，占位比例才指向 E2EE 解密。"""
    s = _store()
    s.record_inbox_health("messenger", "100", unread=5,
                          read_attempts=0, read_fails=0,
                          e2ee_ratio=0.86, conv_count=14)
    s._inbox_health["messenger:100"]["stall_since"] = time.time() - 2400
    w, bus = _wd(monkeypatch)
    w._check_inbox_read_stall(now=time.time())
    assert len(bus.events) == 1
    _, p = bus.events[0]
    assert "加密占位" in p["detail"] and "86%" in p["detail"]
    assert "连续失败" not in p["detail"]


def test_watchdog_fresh_stall_not_due_yet(monkeypatch):
    _make_stalled(aged_sec=60.0)  # 刚 1 分钟：after_min=20 内不提
    w, bus = _wd(monkeypatch)
    w._check_inbox_read_stall(now=time.time())
    assert bus.events == []


def test_watchdog_disabled_is_silent(monkeypatch):
    _make_stalled()
    w, bus = _wd(monkeypatch, enabled=False)
    w._check_inbox_read_stall(now=time.time())
    assert bus.events == []


def test_watchdog_skips_accounts_not_expected_online(monkeypatch):
    """运营自己登出/删除的号不催——与会话掉线提醒同口径。"""
    _make_stalled()
    w, bus = _wd(monkeypatch, expected_online=False)
    w._check_inbox_read_stall(now=time.time())
    assert bus.events == []


def test_watchdog_silent_when_healthy_or_no_heartbeat(monkeypatch):
    # 无任何心跳（未接 worker 的部署）→ 天然静默
    w, bus = _wd(monkeypatch)
    w._check_inbox_read_stall(now=time.time())
    assert bus.events == []
    # 有心跳但健康 → 静默
    _store().record_inbox_health("messenger", "ok", unread=2,
                                 read_attempts=3, read_fails=0)
    w._check_inbox_read_stall(now=time.time())
    assert bus.events == []


def test_watchdog_recovery_stops_reminders(monkeypatch):
    s = _make_stalled()
    w, bus = _wd(monkeypatch)
    w._check_inbox_read_stall(now=time.time())
    assert len(bus.events) == 1
    # 恢复心跳（真读到内容）→ 计时清零，之后即便再跑也不提
    s.record_inbox_health("messenger", "100", unread=4,
                          read_attempts=5, read_fails=1)
    w._check_inbox_read_stall(now=time.time() + 10 * 86400)
    assert len(bus.events) == 1
