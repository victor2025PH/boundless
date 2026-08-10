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

from pathlib import Path

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


# ── label_key 必须真的取得到词（2026-08-08）──────────────────────────────────
# 上面那条只钉了命名空间，钉不住「键根本不存在」：`T()` 取不到词就原样回落键名，
# 面板于是给运营看一行 `cp.alert.lan_gpu`。这与刚修掉的「别名没归类 → 面板压根不
# 列」是同一个病的下一级——都是「加了一半」，而且都不会报错。补登三个别名时我自己
# 就有一半概率踩它，故一并钉死。
#
# 判据取 zh+en 各一次出现：cp-i18n.js 的形状是 `reg({zh…}, {en…})`，同一个键在文件
# 里正好出现两次；只补中文（漏 en）计数=1 → 红。刻意不引 JS 解析器：形状稳定、这条
# 判据零依赖且失败信息足够指路。双树镜像另有 test_copilot_shared_sync 守，不重复。
_CP_I18N = (Path(__file__).resolve().parents[1]
            / "shared" / "copilot" / "i18n" / "cp-i18n.js")


def test_catalog_label_keys_resolve_in_cp_i18n():
    src = _CP_I18N.read_text(encoding="utf-8")
    missing, half = [], []
    for alias, key in sorted({**_BUSINESS_ALERTS, **_TECHNICAL_ALERTS}.items()):
        n = src.count('"%s"' % key)
        if n == 0:
            missing.append("%s → %s" % (alias, key))
        elif n < 2:
            half.append("%s → %s（只出现 %d 次，疑似漏了 en）" % (alias, key, n))
    assert not missing, (
        "这些告警在面板上会显示成裸键名（运营看到一串 cp.alert.xxx）：%s。"
        "请在 shared/copilot/i18n/cp-i18n.js 的 cp.alert 分区补 zh+en 两套词条，"
        "并同步 desktop/renderer 镜像。" % missing)
    assert not half, "词条只补了一种语言：%s" % half


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


# ── 归类完备性 ratchet（2026-08-08）────────────────────────────────────────
# 缺陷形态：别名进了 _EVENT_ALIASES（技术上可订阅、代码里真的会 publish），却**忘了**
# 登记进任一受众目录 → alert_audience 返回 other → 「告警渠道」面板压根不列这一项 →
# 运营勾不到 → 该告警终身零外发，而且看起来一切正常。实锤三例：
#   · unanswered_inbound（客户在等没人回）——本机 8 条真实漏球全程零外发；
#   · voice_outage——它自己就是为「整链拒发 5 天零告警」建的告警，却也没有订阅入口；
#   · lan_gpu——GPU 主机整机下线。
# 三者已补登。本 ratchet 保证下一个别名不会再这样掉进缝里：**凡 _EVENT_ALIASES 里的
# 别名，要么归 business/technical，要么在下面这张「非告警」白名单里显式登记**。
#
# 白名单只收真正的数据流/生命周期/汇总推送——判据是「它描述的是一件正常发生的事，
# 不是一个需要人去处置的异常」。往这里加条目 = 声明「这不是告警」，请写清理由。
_NON_ALERT_FEED_ALIASES = {
    "all",              # 通配订阅，不是具体事件
    "new_message",      # 每条入站消息，正常流水
    "draft_created",    # 草稿生成，正常流水
    "L2_created",       # 同上，按分级切片
    "L3_created",
    "L4_created",
    "reassigned",       # 草稿改派，正常协作动作
    "crm_sync",         # 对外集成数据流
    "conv_archived",    # 会话生命周期
    "conv_tagged",
    "conv_note",
    "backlog_summary",  # 汇总播报（异常本身由 draft_backlog 负责）
    "report",           # 定期简报推送
    "ops_report",       # 运营/价值周报推送，同上
}


def test_every_alert_alias_is_classified():
    """别名要么有受众、要么显式声明「不是告警」——不允许静默落进 other。"""
    unclassified = sorted(
        a for a in _EVENT_ALIASES
        if a not in _BUSINESS_ALERTS
        and a not in _TECHNICAL_ALERTS
        and a not in _NON_ALERT_FEED_ALIASES
    )
    assert not unclassified, (
        "这些别名没有受众归类，「告警渠道」面板不会列出它们，运营永远勾不到 → "
        "终身零外发：%s。请归入 _BUSINESS_ALERTS / _TECHNICAL_ALERTS，或（若它"
        "确实不是告警）登记进本文件的 _NON_ALERT_FEED_ALIASES 并写明理由。"
        % unclassified)


def test_feed_allowlist_not_stale():
    """白名单不得残留已删除的别名（防它悄悄失去约束力）。"""
    stale = sorted(a for a in _NON_ALERT_FEED_ALIASES if a not in _EVENT_ALIASES)
    assert not stale, f"白名单里的别名已不存在于 _EVENT_ALIASES：{stale}"


def test_customer_waiting_family_is_business():
    """「客户在等」这一族必须归 business：终端运营是唯一能处置的人。"""
    for a in ("draft_backlog", "buried_conv", "unanswered_inbound"):
        assert alert_audience(a) == "business", f"{a} 受众应为 business"
