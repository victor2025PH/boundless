# -*- coding: utf-8 -*-
"""自动化覆盖率趋势库门禁（P2 2026-08-09）。

守四条：
1. REPLACE 语义——同日多次快照最后一次胜出（覆盖率是状态不是流量，绝不累加）；
2. recent 旧→新排序 + 天数上限；
3. watchdog 接线：默认关零 IO、开了按 interval 节流、零会话不落行；
4. 路由：trend 段只在开关开时出现（关=键缺席，前端不画线）。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from src.inbox.automation_coverage_trend import (
    CoverageTrendStore,
    trend_cfg,
)
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore

_DAY = 86400.0


def _snap(convs=10, eff=7, pending=2):
    return {
        "totals": {"conversations": convs, "effective_auto": eff,
                   "capped_auto": 1, "takeover_manual": 1,
                   "by_mode": {"auto_ai": eff + 1, "manual": 1, "review": 1,
                               "multi_choice": 0}},
        "drafts": {"pending": pending, "by_age": {"stale": 1}},
    }


def test_replace_semantics_same_day(tmp_path):
    s = CoverageTrendStore(tmp_path / "trend.db")
    now = time.time()
    s.record_snapshot(_snap(convs=10, eff=5), now=now)
    s.record_snapshot(_snap(convs=12, eff=9), now=now)   # 同日重写
    rows = s.recent(days=7)
    assert len(rows) == 1
    assert rows[0]["conversations"] == 12 and rows[0]["effective_auto"] == 9
    s.close()


def test_recent_ordering_and_limit(tmp_path):
    s = CoverageTrendStore(tmp_path / "trend.db")
    now = time.time()
    for i in range(5):
        s.record_snapshot(_snap(convs=10 + i), now=now - (4 - i) * _DAY)
    rows = s.recent(days=3)
    assert len(rows) == 3
    assert [r["conversations"] for r in rows] == [12, 13, 14]   # 旧→新
    s.close()


def test_trend_cfg_defaults():
    assert trend_cfg(None) == {"enabled": False, "interval_min": 60.0}
    c = trend_cfg({"ops": {"automation_coverage_trend": {
        "enabled": True, "interval_min": 1}}})
    assert c["enabled"] is True
    assert c["interval_min"] >= 5.0    # 下限护栏


import pytest


@pytest.fixture(autouse=True)
def _reset_trend_singleton():
    """单例替身复位：测试内注入 tmp 库，退出必须清掉——留一个已 close 的替身
    会让同进程后续启用趋势的测试拿到死连接。"""
    import src.inbox.automation_coverage_trend as tr
    yield
    tr._singleton = None


def _watchdog(store, cfg, trend_store):
    from src.inbox import health_watchdog as hw
    import src.inbox.automation_coverage_trend as tr

    app = SimpleNamespace(state=SimpleNamespace(inbox_store=store))
    cm = SimpleNamespace(config=cfg)
    wd = hw.HealthWatchdog(app=app, config_manager=cm)
    # 单例替身：测试绝不写真实数据区
    tr._singleton = trend_store
    return wd


def test_watchdog_snapshot_gated_and_throttled(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:a:1", platform="telegram", account_id="a",
        chat_key="1", chat_type="private", display_name="N"))
    store.set_automation_mode("telegram:a:1", "auto_ai", source="human")
    ts = CoverageTrendStore(tmp_path / "trend.db")

    # 默认关 → 零落库
    wd = _watchdog(store, {"inbox": {"auto_draft": {"automation_mode": "auto_ai"}}}, ts)
    wd._check_coverage_trend()
    assert ts.recent() == []

    # 开 → 落一行；同 interval 内再 tick 不重写。
    # 日键是本地日期。贴着本地午夜跑时 now+3700 会跨日，REPLACE 写到新的一天，
    # recent()[0] 仍是第一行（manual 停在 0）。钉在本地正午，+3700 仍是同一天。
    cfg_on = {"inbox": {"auto_draft": {"automation_mode": "auto_ai"}},
              "ops": {"automation_coverage_trend": {"enabled": True,
                                                    "interval_min": 60}}}
    wd2 = _watchdog(store, cfg_on, ts)
    lt = time.localtime()
    now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))
    wd2._check_coverage_trend(now=now)
    assert len(ts.recent()) == 1
    assert ts.recent()[0]["conversations"] == 1
    store.set_automation_mode("telegram:a:1", "manual", source="human")
    wd2._check_coverage_trend(now=now + 60)          # 1 分钟后：节流挡住
    assert ts.recent()[0]["manual"] == 0             # 未重写
    wd2._check_coverage_trend(now=now + 3700)        # 过 interval：重写当日行
    assert ts.recent()[0]["manual"] == 1
    store.close()
    ts.close()


def test_watchdog_skips_empty_store(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")   # 零会话
    ts = CoverageTrendStore(tmp_path / "trend.db")
    cfg_on = {"ops": {"automation_coverage_trend": {"enabled": True}}}
    wd = _watchdog(store, cfg_on, ts)
    wd._check_coverage_trend()
    assert ts.recent() == []                    # 空库冷启动不污染趋势
    store.close()
    ts.close()


def test_route_trend_section_gated(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.inbox.automation_coverage_trend as tr
    from src.web.routes import ops_overview_routes as R

    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="telegram:a:1", platform="telegram", account_id="a",
        chat_key="1", chat_type="private", display_name="N"))
    store.set_automation_mode("telegram:a:1", "auto_ai", source="human")
    ts = CoverageTrendStore(tmp_path / "trend.db")
    ts.record_snapshot(_snap(), now=time.time())
    tr._singleton = ts

    def _client(cfg):
        app = FastAPI()
        ctx = SimpleNamespace(
            api_auth=lambda request: True,
            api_write=lambda perm: (lambda: True),
            page_auth=lambda request: True, templates=None,
            config_manager=SimpleNamespace(config=cfg),
            audit_store=None, user_store=None, token=None,
            telegram_client=None)
        R.register_ops_overview_routes(app, ctx)
        app.state.inbox_store = store
        return TestClient(app, raise_server_exceptions=True)

    base = {"inbox": {"auto_draft": {"automation_mode": "auto_ai"}}}
    d_off = _client(base).get("/api/admin/automation-coverage").json()
    assert "trend" not in d_off                     # 关=键缺席
    cfg_on = {**base, "ops": {"automation_coverage_trend": {"enabled": True}}}
    d_on = _client(cfg_on).get("/api/admin/automation-coverage?days=7").json()
    assert isinstance(d_on.get("trend"), list) and len(d_on["trend"]) == 1
    assert d_on["trend"][0]["conversations"] == 10
    store.close()
    ts.close()
