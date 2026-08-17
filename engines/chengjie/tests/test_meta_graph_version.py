# -*- coding: utf-8 -*-
"""Meta Graph API 版本治理门禁。

这批门禁存在的理由是一次真实事故：2026-08-03 实测发现 `webhook_notifier` 的
Messenger 告警端点钉着 **v19.0**，而该版本 2026-05-21 就被 Meta 移除了——已经死了
74 天，没有任何人知道。原因不是谁写错了版本号，而是**版本号散落在三个文件里各写
各的，且没有任何机制知道它们什么时候会死**。

所以这里守两条不变量：
1. **源码里不许再出现散落的 Graph 版本字面量**（只能有 SSOT 一处）——这条能抓住
   v19.0 那类问题的**成因**；
2. **在用版本必须离官方停用日足够远**——这条把「两年后某天线上静默失败」提前成
   「今天 CI 变红，有人被点名去 bump 一行」。

第 2 条注定有一天会自己变红，那正是它的用途。红了的正确处理是**改
`DEFAULT_VERSION`**，不是改 `VERSION_EXPIRY` 里的日期——后者是 Meta 公布的事实，
改它等于把闹钟按掉继续睡，故另有锚点门禁钉住几个已公布日期。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pytest

from src.integrations import meta_graph_version as mgv

_SRC = Path(__file__).resolve().parent.parent / "src"
_SSOT = "meta_graph_version.py"

# 真实的版本字面量（v 后面必须是数字），不会误伤 `v\d+\.\d+` 这种正则源码
_VERSION_LITERAL = re.compile(r"graph\.facebook\.com/v\d+\.\d+")
_VERSION_CONST = re.compile(r"""GRAPH_API_VERSION\s*=\s*["']v\d+\.\d+["']""")


# ── 不变量 1：源码零散落 ────────────────────────────────────────────────────

def test_meta_is_registered_in_the_shared_lifecycle_registry():
    """Meta 必须在通用生命周期登记表里（扫描规则 + pin 各一份）。

    ★ 实际的全库扫描已收归 `tests/test_external_api_lifecycle.py` ★
    Shopify 后来被发现是同一类缺陷（钉版本 + 有公布死期），两家各写一套扫描必然
    漂移，故扫描器与规则表统一到通用层。这里只守「Meta 没有从那张表里掉出去」——
    掉出去就等于本文件开头那起 v19.0 事故的防线被悄悄拆了。
    """
    from src.utils import external_api_lifecycle as lc

    rule_keys = {r.key for r in lc.LITERAL_RULES}
    assert "meta_graph" in rule_keys, "Meta 的版本字面量扫描规则不见了"

    pin_keys = {p.key.split(":")[0] for p in lc.collect_pins()}
    assert "meta_graph" in pin_keys, "Meta 的版本没有被登记，死期无人跟踪"

    # SSOT 白名单必须仍指向本模块，否则扫描会放行散落在 SSOT 之外的字面量
    owners = {f for r in lc.LITERAL_RULES if r.key == "meta_graph" for f in r.owner_files}
    assert owners == {"integrations/meta_graph_version.py"}


def test_all_meta_integrations_share_one_version():
    """三个 Meta 集成必须从 SSOT 取版本（IG 经 facebook_webhook 间接复用）。"""
    from src.integrations import facebook_webhook, instagram_webhook, whatsapp_cloud

    assert facebook_webhook.GRAPH_BASE == mgv.graph_base("messenger")
    assert whatsapp_cloud.GRAPH_BASE == mgv.graph_base("whatsapp")
    # IG 不自己钉版本，直接复用 Messenger 的 base
    assert instagram_webhook.GRAPH_BASE == facebook_webhook.GRAPH_BASE


# ── 不变量 2：在用版本必须有跑道 ────────────────────────────────────────────

def test_pinned_versions_have_runway():
    """在用的每个版本都必须已知、未过期、且离停用日 > CRITICAL_DAYS。"""
    bad = []
    for product, ver in mgv.pinned_versions().items():
        st = mgv.version_status(ver)
        if st.level in ("expired", "critical", "unknown"):
            bad.append(f"{product}={ver} → {st.level}（停用日 {st.expiry}，"
                       f"剩 {st.days_left} 天）")
    assert not bad, (
        "Graph API 版本跑道不足：\n  " + "\n  ".join(bad)
        + f"\n\n处理方式：把 meta_graph_version.DEFAULT_VERSION 升到官方仍支持的"
          f"版本（表见 VERSION_EXPIRY），跑一遍 Meta 相关回归。"
          f"\n⚠ 不要通过修改 VERSION_EXPIRY 的日期来让本条变绿——那是 Meta 公布的"
          f"事实，改它只会让下次真的挂在生产上。"
    )


def test_expiry_table_anchors_match_official():
    """锚点：几个官方已公布的停用日必须原样保留。

    这条守的是上一条门禁的**有效性**。deadline 类门禁最常见的失效方式不是被删，
    而是被「顺手改一下日期」搞哑——加锚点后，那么做会立刻在这里红。
    """
    anchors = {
        "v19.0": date(2026, 5, 21),   # 本次事故的主角
        "v20.0": date(2026, 9, 24),
        "v21.0": date(2027, 1, 21),   # WhatsApp Cloud 原先钉的版本
        "v25.0": date(2028, 7, 29),
    }
    for ver, expected in anchors.items():
        assert mgv.VERSION_EXPIRY[ver] == expected, (
            f"{ver} 的停用日与 Meta 官方公布值不符"
        )


def test_v19_regression_is_recognised_as_dead():
    """事故回归钉：v19.0 必须被判为已停用。"""
    st = mgv.version_status("v19.0", today=date(2026, 8, 3))
    assert st.level == "expired"
    assert st.days_left is not None and st.days_left < 0
    assert not st.healthy


# ── 纯函数语义 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ver,today,level", [
    ("v25.0", date(2026, 8, 3), "ok"),           # 还有约两年
    ("v21.0", date(2026, 8, 3), "warn"),         # 剩 171 天，进提醒区
    ("v20.0", date(2026, 8, 3), "critical"),     # 剩 52 天，必须动手
    ("v19.0", date(2026, 8, 3), "expired"),      # 已死
    ("v26.0", date(2026, 8, 3), "unpublished"),  # 官方未公布死期
    ("v99.0", date(2026, 8, 3), "unknown"),      # 不在官方表里，多半笔误
])
def test_version_status_levels(ver, today, level):
    assert mgv.version_status(ver, today=today).level == level


def test_unknown_version_is_not_healthy():
    """不在官方表里的版本号不能算健康——否则一个笔误就能让到期门禁闭嘴。"""
    assert not mgv.version_status("v99.0").healthy
    assert mgv.version_status("v26.0").healthy  # 未公布死期 ≠ 不健康


def test_worst_level_takes_the_worst(monkeypatch):
    """整体健康取最差档，不能被健康的产品平均掉。"""
    monkeypatch.setattr(mgv, "PRODUCT_VERSIONS", {"whatsapp": "v19.0"})
    assert mgv.worst_level(today=date(2026, 8, 3)) == "expired"


def test_boundary_day_is_not_yet_expired():
    """停用当天仍算 critical 而非 expired（边界不多算一天）。"""
    st = mgv.version_status("v25.0", today=date(2028, 7, 29))
    assert st.days_left == 0
    assert st.level == "critical"


# ── 告警通道：端点推导与陈旧 url 提醒 ───────────────────────────────────────

def test_whatsapp_endpoint_derived_from_phone_id():
    """填 phone_id 即可拼出端点——运营配置里不再需要出现版本号。"""
    from src.inbox.webhook_notifier import _resolve_chat_endpoint

    url = _resolve_chat_endpoint("whatsapp", "", "TOKEN", phone_id="123456")
    assert url == f"{mgv.graph_base('whatsapp')}/123456/messages"


def test_messenger_endpoint_uses_ssot_version():
    """事故本体：messenger 端点不得再出现 v19.0。"""
    from src.inbox.webhook_notifier import _resolve_chat_endpoint

    url = _resolve_chat_endpoint("messenger", "", "PAGE_TOKEN")
    assert "v19.0" not in url
    assert url.startswith(mgv.graph_base("messenger") + "/me/messages")


def test_explicit_url_is_never_rewritten(caplog):
    """运营显式给的 url 原样使用（不静默篡改），但陈旧版本要出声。"""
    from src.inbox.webhook_notifier import _resolve_chat_endpoint

    stale = "https://graph.facebook.com/v19.0/123/messages"
    with caplog.at_level(logging.WARNING):
        out = _resolve_chat_endpoint("whatsapp", stale, "T", name="wa-ops")
    assert out == stale, "不得改写运营给的 url"
    assert "v19.0" in caplog.text and "wa-ops" in caplog.text


def test_healthy_explicit_url_is_silent(caplog):
    """版本健康的 url 不该刷警告（告警要有信噪比）。"""
    from src.inbox.webhook_notifier import _resolve_chat_endpoint

    good = f"{mgv.graph_base('whatsapp')}/123/messages"
    with caplog.at_level(logging.WARNING):
        _resolve_chat_endpoint("whatsapp", good, "T", name="wa-ops")
    assert "Graph API" not in caplog.text


def test_non_graph_url_is_untouched(caplog):
    """自建代理/非 Graph 端点不该被这套判断误伤。"""
    from src.inbox.webhook_notifier import _resolve_chat_endpoint

    proxy = "https://my-proxy.internal/wa/send"
    with caplog.at_level(logging.WARNING):
        assert _resolve_chat_endpoint("whatsapp", proxy, "T") == proxy
    assert caplog.text == ""
