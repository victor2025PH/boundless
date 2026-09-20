# -*- coding: utf-8 -*-
"""挽回统计纯模块门禁（src/contacts/winback_stats.py）。

路由层集成测试在 tests/test_relations_health_routes.py（同口径经 HTTP 断言）；
本文件穷举模块语义：成熟度按真实 now 判、pending 不进分母、周环比窗口偏移、
只读连接工厂、recent 列表的回复/待观察标记。
"""
from __future__ import annotations

import sys
import time as _t
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.contacts.winback_stats import (
    compute_winback,
    readonly_conn,
    recent_reactivations,
)

NOW = _t.time()
DAY = 86400


@pytest.fixture
def store(tmp_path):
    st = ContactStore(db_path=tmp_path / "contacts.db")
    yield st
    st.close()


def _journey(store, ext):
    contact, _ci, _new = store.ensure_channel_identity(
        channel="telegram", account_id="a", external_id=ext,
        display_name=f"客户{ext}")
    j = store.get_journey_by_contact(contact.contact_id)
    return j.journey_id


def _event(store, jid, etype, ts):
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            "INSERT INTO journey_events(event_id, journey_id, trace_id, "
            "event_type, payload_json, ts) VALUES (?, ?, '', ?, '{}', ?)",
            (f"t_{jid}_{etype}_{int(ts)}", jid, etype, int(ts)))
        store._conn.commit()  # noqa: SLF001


def test_empty_store_zeroes(store):
    d = compute_winback(store._conn, store._lock, now=NOW)  # noqa: SLF001
    assert d["sent"] == 0 and d["matured"] == 0 and d["rate"] is None


def test_replied_pending_matured_semantics(store):
    ja = _journey(store, "a")
    _event(store, ja, "reactivation_sent", NOW - 10 * DAY)
    _event(store, ja, "msg_in", NOW - 9 * DAY)          # 挽回成功
    jb = _journey(store, "b")
    _event(store, jb, "reactivation_sent", NOW - 8 * DAY)  # 窗满未回=失败
    jc = _journey(store, "c")
    _event(store, jc, "reactivation_sent", NOW - 1 * DAY)  # 窗未满=pending
    d = compute_winback(store._conn, store._lock,  # noqa: SLF001
                        days=30, reply_window_days=7, now=NOW)
    assert (d["sent"], d["replied"], d["matured"], d["pending"]) == (3, 1, 2, 1)
    assert d["rate"] == 0.5


def test_reply_after_window_not_counted(store):
    j = _journey(store, "late")
    _event(store, j, "reactivation_sent", NOW - 20 * DAY)
    _event(store, j, "msg_in", NOW - 12 * DAY)  # 第 8 天才回（>7 天窗）
    d = compute_winback(store._conn, store._lock, now=NOW)  # noqa: SLF001
    assert d["replied"] == 0 and d["matured"] == 1 and d["rate"] == 0.0


def test_until_offset_gives_previous_window(store):
    j1 = _journey(store, "thisweek")
    _event(store, j1, "reactivation_sent", NOW - 2 * DAY)
    j2 = _journey(store, "lastweek")
    _event(store, j2, "reactivation_sent", NOW - 9 * DAY)
    _event(store, j2, "msg_in", NOW - 8 * DAY)
    cur = compute_winback(store._conn, store._lock,  # noqa: SLF001
                          days=7, now=NOW)
    prev = compute_winback(store._conn, store._lock,  # noqa: SLF001
                           days=7, until_offset_days=7, now=NOW)
    assert cur["sent"] == 1 and cur["pending"] == 1        # 本周：窗未满
    assert prev["sent"] == 1 and prev["replied"] == 1      # 上周：已挽回
    # 上周样本成熟度按真实 now 判——9 天前发送早已成熟
    assert prev["matured"] == 1 and prev["rate"] == 1.0


def test_recent_reactivations_names_and_flags(store):
    j1 = _journey(store, "n1")
    _event(store, j1, "reactivation_sent", NOW - 10 * DAY)
    _event(store, j1, "msg_in", NOW - 9.5 * DAY)
    j2 = _journey(store, "n2")
    _event(store, j2, "reactivation_sent", NOW - 0.5 * DAY)
    rows = recent_reactivations(store._conn, store._lock,  # noqa: SLF001
                                limit=10, now=NOW)
    assert len(rows) == 2
    latest, older = rows[0], rows[1]
    assert latest["journey_id"] == j2 and latest["pending"] is True
    assert older["journey_id"] == j1 and older["replied"] is True
    assert older["name"] == "客户n1"


def test_readonly_conn_cannot_write(store, tmp_path):
    ro = readonly_conn(tmp_path / "contacts.db")
    try:
        with pytest.raises(Exception):
            ro.execute("INSERT INTO journey_events(event_id, journey_id, "
                       "trace_id, event_type, payload_json, ts) "
                       "VALUES ('x','x','','msg_in','{}',1)")
        d = compute_winback(ro, None, now=NOW)
        assert d["ok"] is True
    finally:
        ro.close()
