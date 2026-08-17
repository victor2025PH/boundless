# -*- coding: utf-8 -*-
"""客户资产趋势落库（contacts_asset_trend）门禁。

覆盖：
- store 快照语义 = **gauge**（同日重快照覆盖取最新，绝非 ui_event_trend 的计数累加——
  两个家族语义相反，抄错会把「未开口 149」累成 298）；
- series 只回有快照的日子（缺天不补零——补零会画出「资产归零」假凹坑）+ 窗口裁剪 + 升序；
- has_day 懒快照「一天一次」闸门；prune 保留期；
- `_snap_contacts_assets` 写侧口径与 ops「客户资产」卡同源：不筛平台、total<=0 不落、
  账号数封顶、include_chats=False、单账号异常跳过不炸整轮。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.web.contacts_asset_trend import (
    ContactsAssetTrendStore,
    configure_contacts_asset_trend,
    get_contacts_asset_trend_store,
    reset_contacts_asset_trend,
)
from src.web.routes.ops_overview_routes import _snap_contacts_assets

DAY = 86400.0
T0 = 1_754_000_000.0  # 固定基准时刻（UTC），测试全用显式 now 免跨日 flaky


def _store(tmp_path):
    return ContactsAssetTrendStore(tmp_path / "ca_trend.db")


# ── store 语义 ────────────────────────────────────────────────────────────

def test_snap_is_gauge_same_day_overwrites(tmp_path):
    st = _store(tmp_path)
    st.snap("whatsapp", "wa1", total=100, never_spoke=90, silent=5, now=T0)
    st.snap("whatsapp", "wa1", total=100, never_spoke=80, silent=6, now=T0 + 3600)
    days = st.series(days=7, now=T0)
    assert len(days) == 1
    assert days[0]["never_spoke"] == 80, "同日重快照必须覆盖（gauge），不是累加"
    assert days[0]["silent"] == 6
    assert days[0]["accounts"] == 1


def test_series_aggregates_accounts_and_sorts(tmp_path):
    st = _store(tmp_path)
    st.snap("whatsapp", "wa1", total=100, never_spoke=90, silent=5, now=T0)
    st.snap("telegram", "tg1", total=40, never_spoke=10, silent=20, now=T0)
    st.snap("whatsapp", "wa1", total=100, never_spoke=70, silent=5, now=T0 + DAY)
    st.snap("telegram", "tg1", total=42, never_spoke=8, silent=21, now=T0 + DAY)
    days = st.series(days=7, now=T0 + DAY)
    assert [d["accounts"] for d in days] == [2, 2]
    assert days[0]["day"] < days[1]["day"], "必须升序"
    assert days[0]["never_spoke"] == 100 and days[1]["never_spoke"] == 78
    assert days[1]["total"] == 142


def test_series_window_cut_and_no_zero_fill(tmp_path):
    st = _store(tmp_path)
    st.snap("whatsapp", "wa1", total=10, never_spoke=9, silent=0, now=T0)
    st.snap("whatsapp", "wa1", total=10, never_spoke=5, silent=0, now=T0 + 9 * DAY)
    days = st.series(days=7, now=T0 + 9 * DAY)
    assert len(days) == 1, "窗口外的旧快照必须裁掉；缺天不补零"
    assert days[0]["never_spoke"] == 5


def test_has_day_gate_and_prune(tmp_path):
    st = _store(tmp_path)
    assert not st.has_day(now=T0)
    st.snap("whatsapp", "wa1", total=10, never_spoke=9, silent=0, now=T0)
    assert st.has_day(now=T0)
    assert not st.has_day(now=T0 + DAY), "闸门按日隔离——次日必须重新快照"
    st.snap("whatsapp", "wa1", total=10, never_spoke=8, silent=0, now=T0 + 30 * DAY)
    removed = st.prune(retention_days=7, now=T0 + 30 * DAY)
    assert removed == 1
    assert st.series(days=90, now=T0 + 30 * DAY)[0]["never_spoke"] == 8


def test_snap_ignores_blank_identity(tmp_path):
    st = _store(tmp_path)
    st.snap("", "wa1", total=10, never_spoke=1, silent=0, now=T0)
    st.snap("whatsapp", "", total=10, never_spoke=1, silent=0, now=T0)
    assert st.series(days=7, now=T0) == []


# ── 模块单例闸门 ──────────────────────────────────────────────────────────

def test_singleton_configure_disabled_keeps_none(tmp_path):
    reset_contacts_asset_trend()
    try:
        assert get_contacts_asset_trend_store() is None
        configure_contacts_asset_trend(enabled=False, db_path=tmp_path / "x.db")
        assert get_contacts_asset_trend_store() is None
        st = configure_contacts_asset_trend(enabled=True, db_path=tmp_path / "x.db")
        assert st is not None and get_contacts_asset_trend_store() is st
    finally:
        reset_contacts_asset_trend()


# ── 懒快照写侧口径（与 ops 客户资产卡同源）───────────────────────────────

class _FakeInbox:
    def __init__(self, table):
        self.table = table          # {(plat, acct): summary dict}
        self.calls = []             # [(plat, acct, include_chats)]

    def protocol_contacts_summary(self, plat, acct, *, include_chats):
        self.calls.append((plat, acct, include_chats))
        if (plat, acct) == ("telegram", "boom"):
            raise RuntimeError("单账号炸不倒整轮")
        return self.table.get((plat, acct), {"total": 0, "never_spoke": 0, "silent": 0})


def _app(inbox):
    return SimpleNamespace(state=SimpleNamespace(inbox_store=inbox))


def test_snap_helper_skips_empty_and_survives_errors(tmp_path):
    st = _store(tmp_path)
    inbox = _FakeInbox({
        ("whatsapp", "wa1"): {"total": 157, "never_spoke": 149, "silent": 3},
        ("telegram", "tg1"): {"total": 39, "never_spoke": 10, "silent": 26},
        ("line", "ln1"): {"total": 0, "never_spoke": 0, "silent": 0},   # 无名单平台 → 自然过滤
    })
    accounts = [
        {"platform": "whatsapp", "account_id": "wa1"},
        {"platform": "telegram", "account_id": "boom"},   # summary 抛异常 → 跳过
        {"platform": "telegram", "account_id": "tg1"},
        {"platform": "line", "account_id": "ln1"},        # total=0 → 不落
        {"platform": "", "account_id": "x"},              # 脏行 → 跳过
    ]
    n = _snap_contacts_assets(_app(inbox), st, accounts=accounts, now_hint=T0)
    assert n == 2
    days = st.series(days=7, now=T0)
    assert days[0]["accounts"] == 2
    assert days[0]["never_spoke"] == 159
    # 口径：必须纯名单 include_chats=False（并集会把「未开口占比」分母扩到全部往来的人）
    assert all(ic is False for _, _, ic in inbox.calls)


def test_snap_helper_cap_and_missing_store(tmp_path):
    st = _store(tmp_path)
    inbox = _FakeInbox({("whatsapp", f"wa{i}"): {"total": 10, "never_spoke": 1, "silent": 0}
                        for i in range(5)})
    accounts = [{"platform": "whatsapp", "account_id": f"wa{i}"} for i in range(5)]
    assert _snap_contacts_assets(_app(inbox), st, accounts=accounts, cap=3, now_hint=T0) == 3
    assert st.series(days=7, now=T0)[0]["accounts"] == 3
    assert _snap_contacts_assets(_app(None), st, accounts=accounts) == 0
    assert _snap_contacts_assets(_app(inbox), None, accounts=accounts) == 0
