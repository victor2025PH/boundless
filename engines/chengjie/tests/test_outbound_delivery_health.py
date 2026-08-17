"""出站投递健康判定门禁（纯函数 + watchdog 接线）。

本文件的重点**不是**「能报出来」，而是那些**不该报**的路径：
Discord 不能私信陌生人、24h 客服窗口过期、对方注销——这些每天都在发生，
一旦混进告警，运维一周内就学会无视它（AGENTS.md 里「L3 草稿烂了 167h、
根因是报进了虚空」记的就是这种死法）。所以下面「不该告警」的用例数量
刻意多于「该告警」的。
"""

from __future__ import annotations

import time

import pytest

from src.integrations.shared.outbound_delivery_health import (
    BUSINESS_REASONS,
    GATE_BLOCKED,
    diagnose,
    is_infra_reason,
    snapshot,
    summarize,
    worst_kind,
)


def _snap(platforms=None, reasons=None):
    """造一份 stats.dump() 形状的输入，再过一遍真的 snapshot()。"""
    return snapshot({
        "by_platform": platforms or {},
        "by_reason": reasons or {},
    })


# ── 原因分类：这条线画错了，整个告警的可信度就没了 ──────────────────────────

@pytest.mark.parametrize("reason", sorted(BUSINESS_REASONS))
def test_business_reasons_are_never_infra(reason):
    """平台业务规则类失败一律不算「有人能修」。"""
    assert is_infra_reason(reason) is False


@pytest.mark.parametrize("reason", ["timeout", "network", "auth_failed",
                                    "no_worker", "no_client", "http_500",
                                    "exc_ConnectionResetError"])
def test_infra_reasons_are_infra(reason):
    assert is_infra_reason(reason) is True


def test_unknown_reason_defaults_to_infra():
    """没见过的原因按 infra 处理——新故障静默比多响一次贵得多。"""
    assert is_infra_reason("something_we_have_never_seen") is True
    assert is_infra_reason("") is True


# ── 不该告警的路径（本文件的主角）────────────────────────────────────────────

def test_forbidden_flood_does_not_alert():
    """Discord 全是 403：产品信号（社群覆盖不够），不是故障。"""
    base = _snap({"discord": {"sent": 0, "failed": 0}}, {"discord": {}})
    cur = _snap({"discord": {"sent": 2, "failed": 40}},
                {"discord": {"forbidden": 40}})
    assert diagnose(base, cur) == []


def test_window_expired_flood_does_not_alert():
    base = _snap({"whatsapp": {"sent": 0, "failed": 0}}, {"whatsapp": {}})
    cur = _snap({"whatsapp": {"sent": 5, "failed": 30}},
                {"whatsapp": {"window_expired": 30}})
    assert diagnose(base, cur) == []


def test_low_volume_high_rate_does_not_alert():
    """窗口内只发了 2 条挂了 1 条＝50%，但样本太小，不该吵醒任何人。"""
    base = _snap({"telegram": {"sent": 0, "failed": 0}}, {"telegram": {}})
    cur = _snap({"telegram": {"sent": 1, "failed": 1}},
                {"telegram": {"timeout": 1}})
    assert diagnose(base, cur) == []


def test_high_volume_low_rate_does_not_alert():
    """底噪：8 条 timeout 但分母 400 条 ⇒ 2%，属正常抖动。"""
    base = _snap({"telegram": {"sent": 0, "failed": 0}}, {"telegram": {}})
    cur = _snap({"telegram": {"sent": 392, "failed": 8}},
                {"telegram": {"timeout": 8}})
    assert diagnose(base, cur) == []


def test_first_sample_never_alerts():
    """基线==当前（刚启动只有一个样本）⇒ 增量恒 0，不能凭历史累计值误报。"""
    cur = _snap({"telegram": {"sent": 10, "failed": 900}},
                {"telegram": {"timeout": 900}})
    assert diagnose(cur, cur) == []


def test_counter_reset_does_not_produce_negative_alerts():
    """进程重启后计数器归零：增量为负 ⇒ 夹 0，不该报也不该崩。"""
    base = _snap({"telegram": {"sent": 500, "failed": 300}},
                 {"telegram": {"timeout": 300}})
    cur = _snap({"telegram": {"sent": 1, "failed": 0}}, {"telegram": {}})
    assert diagnose(base, cur) == []


def test_queued_growing_but_also_sending_is_not_stuck():
    """队列在涨、但同平台也真发出去了 ⇒ 只是排队，不是卡死。"""
    base = _snap({"line": {"queued": 0, "sent": 0}})
    cur = _snap({"line": {"queued": 20, "sent": 18}})
    assert [f for f in diagnose(base, cur) if f["kind"] == "stuck_queue"] == []


def test_empty_input_is_safe():
    assert diagnose({}, {}) == []
    assert diagnose(_snap(), _snap()) == []
    assert summarize([]) == ""
    assert worst_kind([]) == ""


# ── 该告警的路径 ─────────────────────────────────────────────────────────────

def test_infra_failure_alerts_with_reason_breakdown():
    base = _snap({"telegram": {"sent": 0, "failed": 0}}, {"telegram": {}})
    cur = _snap({"telegram": {"sent": 4, "failed": 16}},
                {"telegram": {"timeout": 12, "network": 4}})
    findings = diagnose(base, cur)
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "infra" and f["platform"] == "telegram"
    assert f["count"] == 16 and f["attempts"] == 20 and f["rate"] == 0.8
    # 原因分布要进告警——运维第一步就是看「是超时还是鉴权」。
    assert f["reasons"] == {"timeout": 12, "network": 4}


def test_mixed_business_and_infra_only_counts_infra():
    """同平台既有 403 又有 timeout：分母算全部、分子只算 infra。

    这是本模块最容易写错的一处——业务性失败撑大分母**是对的**（它们确实是
    尝试过的发送），但绝不能进分子。
    """
    base = _snap({"discord": {"sent": 0, "failed": 0}}, {"discord": {}})
    cur = _snap({"discord": {"sent": 10, "failed": 30}},
                {"discord": {"forbidden": 20, "timeout": 10}})
    findings = diagnose(base, cur)
    assert len(findings) == 1
    assert findings[0]["count"] == 10          # 只有 timeout
    assert findings[0]["attempts"] == 40       # 分母含 403
    assert findings[0]["reasons"] == {"timeout": 10}


def test_gate_blocked_is_its_own_kind_not_infra():
    """闸门拦截单列：处置动作是「去看开关」而不是「去看网络」。"""
    base = _snap({"whatsapp": {"blocked": 0}}, {"whatsapp": {}})
    cur = _snap({"whatsapp": {"blocked": 25}}, {"whatsapp": {"kill_switch": 25}})
    findings = diagnose(base, cur)
    assert [f["kind"] for f in findings] == ["blocked"]
    assert findings[0]["count"] == 25
    assert set(findings[0]["reasons"]) <= GATE_BLOCKED


def test_stuck_queue_alerts_when_nothing_actually_sent():
    base = _snap({"line": {"queued": 0, "sent": 0}})
    cur = _snap({"line": {"queued": 12, "sent": 0}})
    findings = diagnose(base, cur)
    assert [f["kind"] for f in findings] == ["stuck_queue"]
    assert findings[0]["count"] == 12


def test_platforms_are_judged_independently():
    """一个平台的业务性洪水不得稀释另一个平台的真故障。

    没有这条不变量，Discord 每天几百条 403 会把 telegram 的网络故障按下去
    （分母被撑大、率被摊薄）——这正是「按平台分开判」存在的唯一理由。
    """
    base = _snap({"discord": {"sent": 0, "failed": 0},
                  "telegram": {"sent": 0, "failed": 0}},
                 {"discord": {}, "telegram": {}})
    cur = _snap({"discord": {"sent": 5, "failed": 300},
                 "telegram": {"sent": 4, "failed": 8}},
                {"discord": {"forbidden": 300}, "telegram": {"timeout": 8}})
    findings = diagnose(base, cur)
    assert [(f["platform"], f["kind"]) for f in findings] == [("telegram", "infra")]


def test_findings_sorted_infra_first_then_by_size():
    base = _snap({"a": {"sent": 0, "failed": 0}, "b": {"blocked": 0},
                  "c": {"queued": 0, "sent": 0}},
                 {"a": {}, "b": {}, "c": {}})
    cur = _snap({"a": {"sent": 2, "failed": 20}, "b": {"blocked": 99},
                 "c": {"queued": 9, "sent": 0}},
                {"a": {"timeout": 20}, "b": {"kill_switch": 99}, "c": {}})
    kinds = [f["kind"] for f in diagnose(base, cur)]
    # infra 最该先看（有人能修且客户在等），blocked 排最后（往往是运维自己关的）
    assert kinds == ["infra", "stuck_queue", "blocked"]
    assert worst_kind(diagnose(base, cur)) == "infra"


def test_summary_is_human_readable():
    base = _snap({"telegram": {"sent": 0, "failed": 0}}, {"telegram": {}})
    cur = _snap({"telegram": {"sent": 4, "failed": 16}},
                {"telegram": {"timeout": 12, "network": 4}})
    text = summarize(diagnose(base, cur))
    assert "telegram" in text and "链路故障" in text and "timeout" in text


# ── 阈值可调（运营按自己的流量调，别逼人改代码）──────────────────────────────

def test_thresholds_are_configurable():
    base = _snap({"telegram": {"sent": 0, "failed": 0}}, {"telegram": {}})
    cur = _snap({"telegram": {"sent": 1, "failed": 2}},
                {"telegram": {"timeout": 2}})
    assert diagnose(base, cur) == []                       # 默认阈值下不报
    assert diagnose(base, cur, min_infra=2, min_infra_rate=0.5)  # 调低就报


# ── watchdog 接线：判定对了但没接上去，等于没做 ──────────────────────────────

class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, etype, data):
        self.events.append((etype, data))


class _CM:
    def __init__(self, cfg):
        self.config = cfg


@pytest.fixture
def watchdog(monkeypatch):
    from src.inbox import health_watchdog as hw
    from src.integrations.shared import outbound_delivery_stats as ods

    bus = _Bus()
    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus",
                        lambda: bus)
    stats = ods.get_outbound_delivery_stats()
    stats.reset()
    wd = hw.HealthWatchdog(app=None, config_manager=_CM({
        "health_watchdog": {"outbound_delivery_remind": {
            "enabled": True, "window_min": 30, "interval_min": 60,
            "min_infra": 5, "min_infra_rate": 0.25,
        }},
    }))
    yield wd, bus, stats
    stats.reset()


def test_watchdog_first_tick_is_silent(watchdog):
    """启动第一 tick 必须闭嘴——否则每次重启都会凭历史累计值放一次假警报。"""
    wd, bus, stats = watchdog
    for _ in range(30):
        stats.record("telegram", outcome="failed", reason="timeout")
    wd._check_outbound_delivery(now=1000.0)
    assert bus.events == []


def test_watchdog_alerts_then_debounces_then_recovers(watchdog):
    wd, bus, stats = watchdog
    wd._check_outbound_delivery(now=1000.0)          # 基线
    for _ in range(20):
        stats.record("telegram", outcome="failed", reason="timeout")
    stats.record("telegram", outcome="sent")
    wd._check_outbound_delivery(now=1300.0)
    assert [e[0] for e in bus.events] == ["outbound_delivery_alert"]
    payload = bus.events[0][1]
    assert payload["kind"] == "infra"
    assert "telegram" in payload["summary"]
    # rate_key 按类别分家：链路故障与「闸门没打开」挤同一限流窗会互相吞掉。
    assert payload["rate_key"] == "outbound_delivery:infra"

    # 去抖：同一窗内不重复轰人
    bus.events.clear()
    wd._check_outbound_delivery(now=1400.0)
    assert bus.events == []

    # 恢复：窗口滚过去、增量清零 → 补一条恢复通知（且只补一条）
    bus.events.clear()
    wd._check_outbound_delivery(now=1300.0 + 4000.0)
    wd._check_outbound_delivery(now=1300.0 + 4100.0)
    assert [e[1].get("recovered") for e in bus.events] == [True]


def test_watchdog_respects_disable_switch(watchdog, monkeypatch):
    wd, bus, stats = watchdog
    wd._config_manager.config["health_watchdog"]["outbound_delivery_remind"][
        "enabled"] = False
    wd._check_outbound_delivery(now=1000.0)
    for _ in range(50):
        stats.record("telegram", outcome="failed", reason="timeout")
    wd._check_outbound_delivery(now=1300.0)
    assert bus.events == []


def test_watchdog_never_raises_when_stats_are_broken(watchdog, monkeypatch):
    """观测坏掉不得拖垮巡检——watchdog 是自愈链的一环，它自己不能是故障源。"""
    wd, bus, _ = watchdog
    monkeypatch.setattr(
        "src.integrations.shared.outbound_delivery_stats.get_outbound_delivery_stats",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    wd._check_outbound_delivery(now=time.time())   # 不抛即通过
    assert bus.events == []
