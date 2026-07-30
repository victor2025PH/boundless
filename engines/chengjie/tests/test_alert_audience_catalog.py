# -*- coding: utf-8 -*-
"""告警受众目录门禁（2026-07-31 告警分层产品化）。

事故背景：告警此前只有「手打逗号分隔别名」的订阅框，终端客户看不懂技术黑话、
也不知道有哪些别名。按受众劈成 business（终端能懂能行动）/ technical（开发者向，
折叠进「高级」）。本门禁钉住：
- 目录里每个别名都真实存在于 _EVENT_ALIASES（防笔误/改名后目录指向死别名）；
- business 与 technical 不重叠（一个告警只能属一类受众）；
- 关键业务告警必须在 business（platform_session 账号掉线是终端最该收的，绝不能漂进
  technical 被折叠——那等于把最重要的告警藏起来）；
- alert_audience() 与目录口径一致。
"""

from src.inbox.webhook_notifier import (
    _EVENT_ALIASES,
    _BUSINESS_ALERTS,
    _TECHNICAL_ALERTS,
    alert_audience,
    alert_catalog,
)


def test_catalog_aliases_all_exist_in_event_aliases():
    """目录别名必须是真别名（否则终端勾了却永远收不到）。"""
    for alias in list(_BUSINESS_ALERTS) + list(_TECHNICAL_ALERTS):
        assert alias in _EVENT_ALIASES, f"目录别名 {alias!r} 不在 _EVENT_ALIASES"


def test_business_technical_disjoint():
    overlap = set(_BUSINESS_ALERTS) & set(_TECHNICAL_ALERTS)
    assert not overlap, f"别名同时属两类受众：{sorted(overlap)}"


def test_label_keys_are_cp_i18n_namespaced():
    """label_key 走 cp-i18n（前端 T() 取词）——统一 cp.alert.* 命名空间。"""
    for k in list(_BUSINESS_ALERTS.values()) + list(_TECHNICAL_ALERTS.values()):
        assert k.startswith("cp.alert."), f"label_key {k!r} 不在 cp.alert.* 命名空间"


def test_critical_business_alerts_present():
    """终端最该收的业务告警绝不能漏/漂进 technical（漏 = 把最重要的告警藏起来）。"""
    must = {"platform_session", "escalation", "draft_backlog", "queue_alert"}
    missing = must - set(_BUSINESS_ALERTS)
    assert not missing, f"关键业务告警缺失/未归 business：{sorted(missing)}"
    for a in must:
        assert alert_audience(a) == "business", f"{a} 受众应为 business"


def test_technical_alerts_not_leaking_to_business():
    """纯技术信号不能混进 business（否则终端被看不懂的告警淹没）。"""
    for a in ("csrf_reject", "memory_key_drift", "orchestrator_worker",
              "host_alert", "human_deliver"):
        assert alert_audience(a) == "technical", f"{a} 受众应为 technical"


def test_catalog_shape():
    cat = alert_catalog()
    assert set(cat) == {"business", "technical"}
    for group in cat.values():
        for e in group:
            assert set(e) == {"alias", "label_key"}
    # 目录条数与源表一致（防遗漏/重复）
    assert len(cat["business"]) == len(_BUSINESS_ALERTS)
    assert len(cat["technical"]) == len(_TECHNICAL_ALERTS)


def test_alert_audience_other_for_feed_events():
    """数据流/集成事件（非告警）归 other，不进面板订阅目录（避免噪音）。"""
    for a in ("new_message", "crm_sync", "draft_created", "all"):
        assert alert_audience(a) == "other", f"{a} 不应进面板受众目录"
