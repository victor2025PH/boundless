# -*- coding: utf-8 -*-
"""运维群降噪 P2.2（2026-09-10）：公网入口断了，卡片链接回落内网 + 摘要露一行。

09-09 实录：实例重启后 katie 隧道没回来，卡片照发、链接全 502，值班的人点了三张才明白不是自己的问题。
告警本身归 prod_edge_watchdog；这里只管「卡上的链接至少要能点开」：
- src/ops/public_link：两击判断（单次抖动不翻）、恢复即翻回、5xx 按不通、403 按通；
- webhook_notifier._build_card：断了 → 链接根换内网地址并在卡上说明；推不出内网地址 → 只说明；
- health_watchdog._check_public_link：从渠道配置收集 base_url、节流、探测、登记；
- 每日摘要多一行「公网入口」。
"""
from __future__ import annotations

import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.inbox import health_watchdog as hw  # noqa: E402
from src.inbox.remind_ledger import RemindLedger  # noqa: E402
from src.inbox.webhook_notifier import _build_card, _build_message  # noqa: E402
from src.ops import public_link as pl  # noqa: E402

BASE = "https://katie.example.cc"
LAN = "http://192.168.0.149:18799"


@pytest.fixture(autouse=True)
def _clean():
    pl._reset_for_tests()
    yield
    pl._reset_for_tests()


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_classifies_http_and_network_outcomes(monkeypatch):
    monkeypatch.setattr(pl.urllib.request, "urlopen", lambda *a, **k: _Resp(200))
    assert pl.probe(BASE) == (True, "HTTP 200")

    def _403(*a, **k):
        raise urllib.error.HTTPError(BASE, 403, "Forbidden", {}, None)
    monkeypatch.setattr(pl.urllib.request, "urlopen", _403)
    assert pl.probe(BASE) == (True, "HTTP 403")          # 无令牌被拒 = 链路通

    def _502(*a, **k):
        raise urllib.error.HTTPError(BASE, 502, "Bad Gateway", {}, None)
    monkeypatch.setattr(pl.urllib.request, "urlopen", _502)
    assert pl.probe(BASE) == (False, "HTTP 502")         # nginx 通、隧道断

    def _timeout(*a, **k):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(pl.urllib.request, "urlopen", _timeout)
    ok, detail = pl.probe(BASE)
    assert ok is False and "URLError" in detail
    assert pl.probe("") == (False, "no base_url")


def test_record_two_strikes_then_down_then_recover():
    t0 = 1_700_000_000.0
    assert pl.record(BASE, False, "HTTP 502", now=t0) is None       # 第一击不翻
    assert pl.is_down(BASE) is False
    assert pl.record(BASE, False, "HTTP 502", now=t0 + 300) is True  # 第二击判断
    assert pl.is_down(BASE) is True and pl.is_down() is True
    assert pl.snapshot()[BASE]["since"] == t0 + 300
    assert pl.record(BASE, False, "HTTP 502", now=t0 + 600) is None  # 持续断不重复翻
    assert pl.record(BASE, True, "HTTP 403", now=t0 + 900) is False  # 恢复即翻回
    assert pl.is_down(BASE) is False and pl.snapshot()[BASE]["strikes"] == 0
    # 一次抖动（坏→好）不翻
    pl.record(BASE, False, "x", now=t0 + 1200)
    assert pl.record(BASE, True, "HTTP 200", now=t0 + 1500) is None
    assert pl.is_down(BASE) is False


def _card(etype, data, links="login"):
    title, text = _build_message(etype, data)
    return _build_card(etype, data, title, text, BASE, links)


_DRAFT = {"stale_count": 5, "oldest_hours": 712.6, "min_age_hours": 24, "sla_uncovered": 5,
          "by_level": {"L1": 5}, "already_replied": 0, "reminder": False}


def test_card_links_fall_back_to_lan_when_public_down():
    card_ok = _card("draft_backlog_alert", _DRAFT)
    assert f"{BASE}/admin/ops" in card_ok and "公网入口" not in card_ok

    pl.set_lan_base(LAN)
    pl.record(BASE, False, "HTTP 502", now=1.0)
    pl.record(BASE, False, "HTTP 502", now=2.0)
    card_down = _card("draft_backlog_alert", _DRAFT)
    assert f"{LAN}/admin/ops" in card_down and BASE not in card_down
    assert "⚠️ 公网入口暂不可达，链接已换为内网地址（公司网络内打开）" in card_down
    # 说明行在正文之后、页脚之前
    lines = card_down.splitlines()
    assert lines.index("⚠️ 公网入口暂不可达，链接已换为内网地址（公司网络内打开）") == len(lines) - 2

    # 恢复后换回公网地址、说明消失
    pl.record(BASE, True, "HTTP 403", now=3.0)
    card_back = _card("draft_backlog_alert", _DRAFT)
    assert f"{BASE}/admin/ops" in card_back and "公网入口" not in card_back


def test_card_only_annotates_when_no_lan_address_and_skips_links_off():
    pl.record(BASE, False, "x", now=1.0)
    pl.record(BASE, False, "x", now=2.0)
    card = _card("draft_backlog_alert", _DRAFT)
    assert f"{BASE}/admin/ops" in card
    assert "⚠️ 公网入口暂不可达，链接可能暂时打不开" in card
    # links=off：卡上本无链接，不加说明
    card_off = _card("draft_backlog_alert", _DRAFT, links="off")
    assert "公网入口" not in card_off and "http" not in card_off


def _wd(monkeypatch, webhooks, cfg_extra=None):
    w = hw.HealthWatchdog.__new__(hw.HealthWatchdog)
    w._app = SimpleNamespace(state=SimpleNamespace())
    cfg = {"web_admin": {"port": 18799, "lan_base_url": LAN}, "health_watchdog": {}}
    if cfg_extra:
        cfg["health_watchdog"].update(cfg_extra)
    w._config_manager = SimpleNamespace(config=cfg)
    w.__dict__["_remind_ledger"] = RemindLedger(None)
    monkeypatch.setattr("src.integrations.notify_webhooks_store.effective_webhooks",
                        lambda _cfg: webhooks)
    return w


def test_watchdog_probe_collects_bases_throttles_and_records(monkeypatch):
    calls = []

    def _probe(base, **k):
        calls.append(base)
        return False, "HTTP 502"
    monkeypatch.setattr(pl, "probe", _probe)
    webhooks = [
        {"name": "tg-ywqz", "format": "telegram", "enabled": True, "links": "magic", "base_url": BASE},
        {"name": "tg-ops", "format": "telegram", "enabled": True, "links": "magic", "base_url": BASE + "/"},
        {"name": "off", "format": "telegram", "enabled": True, "links": "off", "base_url": "https://x.example"},
        {"name": "disabled", "format": "telegram", "enabled": False, "base_url": "https://y.example"},
        {"name": "feishu", "format": "feishu", "url": "https://open.feishu.cn/hook", "base_url": "https://z.example"},
    ]
    w = _wd(monkeypatch, webhooks)
    t0 = 1_700_000_000.0
    w._check_public_link(now=t0)
    assert calls == [BASE]                       # 去重 + 跳过 off / disabled / 非 IM 渠道
    assert pl.is_down(BASE) is False             # 一击不翻
    w._check_public_link(now=t0 + 60)            # 节流：5 分钟内不再探
    assert calls == [BASE]
    w._check_public_link(now=t0 + 301)
    assert calls == [BASE, BASE] and pl.is_down(BASE) is True
    assert pl.lan_base() == LAN                  # 内网地址已从配置注入，notifier 可直接取

    # 开关关掉：不探
    w2 = _wd(monkeypatch, webhooks, {"public_link_probe": {"enabled": False}})
    w2._check_public_link(now=t0 + 9999)
    assert calls == [BASE, BASE]


def test_digest_carries_public_link_state(monkeypatch):
    pl.record(BASE, False, "HTTP 502", now=1_700_000_000.0)
    pl.record(BASE, False, "HTTP 502", now=1_700_000_300.0)
    w = _wd(monkeypatch, [])
    rep = w._build_daily_digest(now=1_700_000_300.0 + 2 * 3600)
    assert rep["public_link"][BASE]["down"] is True
    assert rep["public_link"][BASE]["down_hours"] == 2.0
    title, text = _build_message("ops_digest_report", rep)
    assert "公网入口: ❌ katie.example.cc 不可达（已 2.0 小时，卡片链接暂用内网地址）" in \
        _build_card("ops_digest_report", rep, title, text, "", "off")

    pl.record(BASE, True, "HTTP 403", now=time.time())
    rep2 = w._build_daily_digest()
    title, text = _build_message("ops_digest_report", rep2)
    assert "公网入口: 正常" in _build_card("ops_digest_report", rep2, title, text, "", "off")
