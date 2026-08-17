# -*- coding: utf-8 -*-
"""外部 API 版本生命周期门禁（跨供应商）。

这批门禁守的是**一类缺陷**，不是某一家的版本号：
「我们钉了一个外部 API 版本，供应商公布了它的死期，而我们没有任何东西在跟踪它」。

2026-08-03 一次排查里同时发现两个实例，失败模式还不一样：

    Meta Graph v19.0   官方 2026-05-21 移除    已死 74 天   过期即 4xx（会吵）
    Shopify  2024-01   官方 2025-01-16 到期    已死 564 天  过期**不报错**，静默换版本

Shopify 那个更值得警惕：它永远不会在日志里留下任何痕迹，破坏性变更被悄悄应用，
你以为在用 2024-01，其实在用别的。这种缺陷**只可能被 CI 里的日期门禁抓住**。

三条不变量：
1. 源码里不许出现散落的外部版本字面量（只能有各家 SSOT 一处）——治成因；
2. 登记表里每个 pin 都必须离死期足够远——把「两年后线上静默失败」提前成「今天 CI 红」；
3. 登记表自己不许漏登记——否则只是把原 bug 搬到上一层。

第 2 条注定有一天会自己变红，那正是它的用途。红了的正确处理是**升版本**，不是改
到期日——那些日期是供应商公布的事实，改它等于把闹钟按掉继续睡（故有锚点门禁钉住）。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pytest

from src.ecommerce_tools.shopify_connector import (
    DEFAULT_API_VERSION,
    VERSION_EOL,
    ShopifyConnector,
)
from src.utils import external_api_lifecycle as lc

_SRC = Path(__file__).resolve().parent.parent / "src"


# ── 不变量 1：源码零散落 ────────────────────────────────────────────────────

def test_no_unregistered_version_literals():
    """除各家 SSOT 外，src/ 下不得写死任何外部 API 版本号。

    这是 v19.0 事故的**成因**：版本号散落在三个文件里各写各的，其中一个悄悄过期。
    只要版本号只能从一个地方来，就不会再有「另外那处忘了改」。
    """
    offenders = []
    for rule in lc.LITERAL_RULES:
        pat = re.compile(rule.pattern)
        for path in _SRC.rglob("*.py"):
            rel_to_src = path.relative_to(_SRC).as_posix()
            if rel_to_src in rule.owner_files:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in pat.finditer(text):
                line = text[: m.start()].count("\n") + 1
                rel = path.relative_to(_SRC.parent).as_posix()
                offenders.append(f"{rel}:{line}  {m.group(0)}   → {rule.hint}")
    assert not offenders, "以下位置写死了外部 API 版本号：\n  " + "\n  ".join(offenders)


def test_literal_rules_are_not_vacuous():
    """每条扫描规则都必须真能匹配它要抓的东西。

    正则写错＝扫不到任何东西＝门禁恒绿。那比没有门禁更糟：它会让人以为有人在守。
    """
    samples = {
        r"graph\.facebook\.com/v\d+\.\d+": "https://graph.facebook.com/v19.0/me",
        r"""GRAPH_API_VERSION\s*=\s*["']v\d+\.\d+["']""": 'GRAPH_API_VERSION = "v19.0"',
        r"/admin/api/20\d{2}-\d{2}": "https://x.myshopify.com/admin/api/2024-01/orders.json",
        r"discord\.com/api/v\d+": "https://discord.com/api/v10/users/@me",
    }
    for rule in lc.LITERAL_RULES:
        assert rule.pattern in samples, f"新增规则 {rule.key} 请同时补一条自检样本"
        assert re.search(rule.pattern, samples[rule.pattern]), (
            f"规则 {rule.key} 的正则匹配不到它自己的典型样本，等于没在守"
        )


def test_owner_files_exist():
    """SSOT 白名单指向的文件必须真的存在（重构改名后规则会失效变成恒绿）。"""
    for rule in lc.LITERAL_RULES:
        for rel in rule.owner_files:
            assert (_SRC / rel).is_file(), f"规则 {rule.key} 的 owner 文件不存在：{rel}"


# ── 不变量 2：登记表里每个 pin 都还活着 ─────────────────────────────────────

def test_every_pin_has_runway():
    """所有在用外部版本都必须离死期 > CRITICAL_DAYS。

    ⚠ 这条红了**不要改到期日**——那是供应商公布的事实。正确处理是照 pin 的
    remediation 升版本。
    """
    bad = []
    for key, st in lc.health_report().items():
        if not st.healthy or st.level in ("warn",):
            bad.append(
                f"{key}={st.pin.version} 档位={st.level} 剩余={st.days_left} "
                f"（{st.pin.owner}）→ {st.pin.remediation}"
            )
    assert not bad, "以下外部 API 版本需要处理：\n  " + "\n  ".join(bad)


def test_pin_keys_are_unique():
    keys = [p.key for p in lc.collect_pins()]
    assert len(keys) == len(set(keys)), f"pin key 重复：{keys}"


def test_every_literal_rule_has_a_pin():
    """有扫描规则却没登记 pin ＝ 管住了写法却没人跟踪死期，属半截防线。"""
    pin_keys = {p.key.split(":")[0] for p in lc.collect_pins()}
    missing = {r.key for r in lc.LITERAL_RULES} - pin_keys
    assert not missing, f"这些规则没有对应的 pin，死期无人跟踪：{missing}"


# ── 不变量 3：`查不到死期` 绝不能被当成 `没有死期` ───────────────────────────

def test_lookup_miss_is_unknown_not_unpublished():
    """回归钉：表里查不到的版本必须判 ``unknown``（不健康）。

    若判成 ``unpublished``（健康），把 DEFAULT 写成一个拼错的/早已退役的版本时，
    门禁会笑着放行——那正是本模块要消灭的静默。
    """
    _, _, level = lc.status_in_table("2024-01", VERSION_EOL)
    assert level == "unknown"
    assert not lc.PinStatus(
        lc.ApiPin("x", "x", "2024-01", None, lc.FAIL_SILENT, "", "", ""), None, level
    ).healthy


def test_vendor_without_published_eol_is_healthy():
    """Discord 这种官方不公布死期的，是事实陈述，不该被判不健康。"""
    st = lc.health_report()["discord"]
    assert st.level == "unpublished" and st.healthy


def test_pin_with_missing_eol_is_flagged_unknown():
    """没标 ``eol_unpublished`` 却又没有到期日 → 判 unknown（这是查不到，不是没有）。"""
    pin = lc.ApiPin("t", "t", "9999-99", None, lc.FAIL_HARD, "", "", "")
    assert pin.eol_unpublished is False
    days, level = lc.status_for(pin.eol)
    # status_for 单看日期无法区分二者，区分发生在 health_report——这里钉住那个约定
    assert level == "unpublished" and days is None


@pytest.mark.parametrize(
    "days_out,expect",
    [(-1, "expired"), (0, "critical"), (59, "critical"), (60, "warn"),
     (179, "warn"), (180, "ok"), (900, "ok")],
)
def test_status_levels(days_out, expect):
    from datetime import timedelta
    today = date(2026, 8, 3)
    _, level = lc.status_for(today + timedelta(days=days_out), today)
    assert level == expect


def test_worst_level_takes_the_worst():
    """一处过期就是过期，不被健康的条目平均掉。"""
    assert lc.worst_level(date(2030, 1, 1)) == "expired"


# ── Shopify 专项 ────────────────────────────────────────────────────────────

def test_shopify_default_is_in_official_table():
    assert DEFAULT_API_VERSION in VERSION_EOL


def test_shopify_eol_anchors_match_official():
    """锚住几个官方公布值，防止门禁红时有人改日期而不是升版本。

    来源 https://shopify.dev/docs/api/usage/versioning（季度发版，至少支持 12 个月）。
    """
    assert VERSION_EOL["2025-07"] == date(2026, 7, 16)
    assert VERSION_EOL["2026-01"] == date(2027, 1, 16)
    assert VERSION_EOL["2026-07"] == date(2027, 7, 16)


def test_shopify_2024_01_regression():
    """回归钉：曾经钉了 19 个月的 ``2024-01`` 现在必须被判为不可用。"""
    _, _, level = lc.status_in_table("2024-01", VERSION_EOL)
    assert level == "unknown"


def test_stale_config_version_warns_but_is_not_rewritten(caplog):
    """运维显式指定了陈旧版本：必须告警，但**不得**替他改掉。

    与告警通道那边同一条判断——运维写下的值可能正是为了钉住某个行为，我们无权
    静默改写；但也不能装作没看见，尤其 Shopify 过期后线上完全没有信号。
    """
    with caplog.at_level(logging.WARNING):
        c = ShopifyConnector(shop="x.myshopify.com", access_token="t",
                             api_version="2024-01")
    assert "2024-01" in c._base()          # 没被偷偷改成默认版
    assert any("2024-01" in r.getMessage() for r in caplog.records), "陈旧版本必须告警"


def test_unset_version_uses_ssot_and_is_silent(caplog):
    """不填版本＝跟随 SSOT，且不该有噪音告警。"""
    with caplog.at_level(logging.WARNING):
        c = ShopifyConnector(shop="x.myshopify.com", access_token="t")
    assert f"/admin/api/{DEFAULT_API_VERSION}" in c._base()
    assert not [r for r in caplog.records if "api_version" in r.getMessage()]


def test_current_version_config_is_silent(caplog):
    """显式填了当前版本也不该告警（否则运维会学会无视这条日志）。"""
    with caplog.at_level(logging.WARNING):
        ShopifyConnector(shop="x.myshopify.com", access_token="t",
                         api_version=DEFAULT_API_VERSION)
    assert not caplog.records


# ── 日历盲区：watchdog 巡检 ─────────────────────────────────────────────────
# CI 门禁只在「有人提交代码」时跑，而这类失效是日历驱动的。下面这批守的是
# 「几个月没人动仓库时，谁还在盯着死期」——以及更重要的，**它什么时候该闭嘴**。

def _pin(level_days, *, key="t", mode=lc.FAIL_HARD, unpublished=False):
    from datetime import timedelta
    eol = None if level_days is None else date.today() + timedelta(days=level_days)
    return lc.ApiPin(key, f"测试 {key}", "vX", eol, mode, "owner.py", "", "升版本",
                     eol_unpublished=unpublished)


def _watchdog(cfg=None):
    """只装配本方法用得到的字段——绕开 HealthWatchdog 构造器的一大堆依赖。"""
    from types import SimpleNamespace

    from src.inbox.health_watchdog import HealthWatchdog

    wd = HealthWatchdog.__new__(HealthWatchdog)
    wd._config_manager = SimpleNamespace(config=cfg or {})
    wd._last_api_version_ts = 0.0
    wd._apiver_alerted = False
    wd.total_api_version_alerts = 0
    return wd


@pytest.fixture
def sent(monkeypatch):
    """截获 notify_host，返回收到的 (title, message, key) 列表。"""
    out = []

    def fake(title, message, *, key="", cooldown_sec=0.0):
        out.append((title, message, key))
        return True

    monkeypatch.setattr("src.utils.host_alert.notify_host", fake)
    return out


def _fake_report(monkeypatch, pins):
    rep = {}
    for p in pins:
        if p.eol is None and not p.eol_unpublished:
            rep[p.key] = lc.PinStatus(p, None, "unknown")
        else:
            days, level = lc.status_for(p.eol)
            rep[p.key] = lc.PinStatus(p, days, level)
    monkeypatch.setattr("src.utils.external_api_lifecycle.health_report",
                        lambda *a, **k: rep)


def test_watchdog_silent_when_everything_healthy(monkeypatch, sent):
    _fake_report(monkeypatch, [_pin(400)])
    _watchdog()._check_api_version_expiry()
    assert sent == []


def test_watchdog_silent_on_warn_tier(monkeypatch, sent):
    """半年内到期只进看板/门禁，不轰人——这里出声就是噪音。"""
    _fake_report(monkeypatch, [_pin(120)])   # warn 区间
    _watchdog()._check_api_version_expiry()
    assert sent == []


def test_watchdog_alerts_on_expired(monkeypatch, sent):
    _fake_report(monkeypatch, [_pin(-30)])
    wd = _watchdog()
    wd._check_api_version_expiry()
    assert len(sent) == 1 and "已过期 30 天" in sent[0][1]
    assert wd.total_api_version_alerts == 1 and wd._apiver_alerted


def test_watchdog_calls_out_silent_failure_mode(monkeypatch, sent):
    """静默 fall-forward 那类必须单独点名——它过期后线上一点信号都没有。"""
    _fake_report(monkeypatch, [_pin(10, mode=lc.FAIL_SILENT)])
    _watchdog()._check_api_version_expiry()
    assert "静默改用别的版本" in sent[0][1]


def test_watchdog_alerts_on_unknown_version(monkeypatch, sent):
    _fake_report(monkeypatch, [_pin(None)])   # eol 查不到且未标 unpublished
    _watchdog()._check_api_version_expiry()
    assert "不在官方支持列表内" in sent[0][1]


def test_watchdog_ignores_vendor_without_eol(monkeypatch, sent):
    """Discord 这种官方不公布死期的，不该被当成问题反复轰人。"""
    _fake_report(monkeypatch, [_pin(None, unpublished=True)])
    _watchdog()._check_api_version_expiry()
    assert sent == []


def test_watchdog_throttles(monkeypatch, sent):
    _fake_report(monkeypatch, [_pin(-1)])
    wd = _watchdog()
    wd._check_api_version_expiry(now=1000.0)
    wd._check_api_version_expiry(now=1000.0 + 60)      # 窗口内
    assert len(sent) == 1
    wd._check_api_version_expiry(now=1000.0 + 43201)   # 窗口外
    assert len(sent) == 2


def test_watchdog_sends_recovery_once(monkeypatch, sent):
    """修好之后要补一条恢复，否则运维不确定到底修没修好；但只补一次。"""
    _fake_report(monkeypatch, [_pin(-1)])
    wd = _watchdog()
    wd._check_api_version_expiry(now=1000.0)
    _fake_report(monkeypatch, [_pin(400)])
    wd._check_api_version_expiry(now=1000.0 + 43201)
    assert sent[-1][2] == "apiver:recovered" and not wd._apiver_alerted
    wd._check_api_version_expiry(now=1000.0 + 86402)
    assert len([s for s in sent if s[2] == "apiver:recovered"]) == 1


def test_watchdog_can_be_disabled(monkeypatch, sent):
    _fake_report(monkeypatch, [_pin(-99)])
    cfg = {"health_watchdog": {"api_version_expiry": {"enabled": False}}}
    _watchdog(cfg)._check_api_version_expiry()
    assert sent == []
