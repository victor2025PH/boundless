# -*- coding: utf-8 -*-
"""ops_events 分级保留期门禁（数据主权凭证 vs 运维健康史，2026-08-28）。

背景：资产中心「快照 / 导出台账」是「资产保全」这个承诺的**唯一证据面**，而
`ops_events` 原本对所有 kind 一律 90 天裁剪 —— 三个月后「那批客户数据是谁在什么
时候导出/清掉的」就永久查不到了。现在数据主权类 kind（导出/清除/迁移/快照）走
730 天，其余不变。

本门禁钉住两侧都不许漂：凭证类**不得**被 90 天窗口删掉，普通运维事件**仍然**要被
删掉（否则等于整表放宽，高频的风控/限速事件会让表无界增长）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ops.ops_events import (
    _CUSTODY_KINDS,
    _CUSTODY_RETAIN_DAYS,
    _DAY,
    _RETAIN_DAYS,
    OpsEventStore,
)


def _store(tmp_path) -> OpsEventStore:
    return OpsEventStore(tmp_path / "ops_events.db")


def _all_kinds(st: OpsEventStore):
    return [r["kind"] for r in st.recent(limit=500)]


def test_custody_kinds_survive_the_90_day_window(tmp_path):
    """120 天前的导出/清除凭证必须还在（普通窗口是 90 天）。"""
    st = _store(tmp_path)
    old = time.time() - 120 * _DAY
    for k in _CUSTODY_KINDS:
        st.record(k, account_id="a1", platform="telegram", reason="ok", ts=old)
    st.record("account_pause", account_id="a1", reason="flood", ts=old)

    st._prune_locked(time.time() - _RETAIN_DAYS * _DAY)

    kinds = set(_all_kinds(st))
    assert set(_CUSTODY_KINDS) <= kinds, "凭证类被 90 天窗口误删"
    assert "account_pause" not in kinds, "普通运维事件应当已被裁剪"


def test_ordinary_ops_events_still_pruned(tmp_path):
    """整表放宽是错的：高频运维事件仍须按 90 天裁剪，否则表无界增长。"""
    st = _store(tmp_path)
    st.record("rate_limit_hit", account_id="a1", ts=time.time() - 100 * _DAY)
    st.record("circuit_open", account_id="a1", ts=time.time() - 91 * _DAY)
    st.record("rate_limit_hit", account_id="a1", ts=time.time() - 10 * _DAY)

    st._prune_locked(time.time() - _RETAIN_DAYS * _DAY)

    assert _all_kinds(st) == ["rate_limit_hit"], "只该留下窗口内那条"


def test_custody_kinds_eventually_pruned_too(tmp_path):
    """凭证也不是永不过期——超过自己的保留期同样清掉（表不能无界）。"""
    st = _store(tmp_path)
    way_old = time.time() - (_CUSTODY_RETAIN_DAYS + 30) * _DAY
    st.record("account_export", account_id="a1", ts=way_old)
    st.record("account_export", account_id="a1", ts=time.time() - 200 * _DAY)

    st._prune_locked(time.time() - _RETAIN_DAYS * _DAY)

    rows = st.recent(limit=10)
    assert len(rows) == 1, "只有超两年那条该被清"
    assert rows[0]["ts"] > way_old


def test_custody_cutoff_does_not_follow_the_caller_window(tmp_path):
    """凭证截止线必须自己算，不能跟随入参——跟随了这条豁免就等于没生效。"""
    st = _store(tmp_path)
    st.record("account_purge", account_id="a1", ts=time.time() - 300 * _DAY)
    # 调用方传一个**极宽**的窗口（普通事件几乎全删），凭证仍应存活
    st._prune_locked(time.time() - 1 * _DAY)
    assert _all_kinds(st) == ["account_purge"]


def test_retention_constants_are_sane():
    assert _CUSTODY_RETAIN_DAYS > _RETAIN_DAYS
    # 台账 API 的默认 kind 集与豁免集必须一致，否则「看板上看得见但已被删」
    from src.web.routes.asset_center_routes import _DEFAULT_LEDGER_KINDS
    assert set(_DEFAULT_LEDGER_KINDS) == set(_CUSTODY_KINDS), (
        "资产台账展示的 kind 集与保留期豁免集漂移了——"
        "看板会展示一个注定被 90 天裁剪掉的类型")


def test_prune_runs_on_construction_without_error(tmp_path):
    """构造期就会 prune 一次（含两条 DELETE）；SQL 写错这里先炸。"""
    st = _store(tmp_path)
    st.record("account_export", account_id="a1")
    st2 = OpsEventStore(tmp_path / "ops_events.db")
    assert "account_export" in _all_kinds(st2)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
