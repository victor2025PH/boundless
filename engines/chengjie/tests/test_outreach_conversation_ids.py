# -*- coding: utf-8 -*-
"""批量触达「点名名单」门禁（P1 2026-08-09，目标报表勾选完成客户的通道）。

设计不变量：``OutreachFilters.conversation_ids`` 非空＝跳过圈选筛子（运营已
显式选人，沉默天数/标签/归档不适用），但**资格管线原样过**——cooldown 与
账号配额是防打扰/防风控的护栏，点名也不能绕；绕开管线裸循环单发正是本
设计要堵住的路。

覆盖：直取路径顺序/去重/查无剔除/平台过滤；build_plan 上冷却与配额照拦；
language 字段随行（预览语言分布的数据源）；空 ids 走旧圈选路径零回归。
"""

from __future__ import annotations

import time

from src.inbox.models import InboxConversation
from src.inbox.outreach_planner import OutreachFilters, OutreachPlanner
from src.inbox.store import InboxStore

DAY = 86400.0
NOW = 1_000_000.0


def _store_with_convs(specs):
    store = InboxStore(":memory:")
    for s in specs:
        conv = InboxConversation(
            conversation_id=s["cid"], platform=s.get("platform", "telegram"),
            account_id=s.get("account_id", "a1"),
            chat_key=s["cid"].split(":")[-1],
            display_name=s.get("name", s["cid"]), last_ts=s.get("last_ts", NOW),
        )
        store.ingest_batch(conv, [])
        if s.get("language"):
            store._conn.execute(
                "UPDATE conversations SET language = ? WHERE conversation_id = ?",
                (s["language"], s["cid"]))
            store._conn.commit()
    return store


class _FakeLimiter:
    def __init__(self, caps):
        self._caps = caps

    def remaining_for(self, account_id, *, now=None):
        return self._caps.get(account_id, 0)


def test_ids_path_keeps_order_dedupes_and_drops_missing():
    store = _store_with_convs([
        {"cid": "telegram:a1:1"},
        {"cid": "telegram:a1:2"},
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(conversation_ids=[
        "telegram:a1:2", "telegram:a1:1", "telegram:a1:2",   # 重复
        "telegram:a1:missing",                                # 查无
    ]), now=NOW)
    assert [t.conversation_id for t in seg] == [
        "telegram:a1:2", "telegram:a1:1"]


def test_ids_path_skips_segment_filters_but_respects_platform():
    """显式选人：沉默/归档筛子不适用；平台过滤仍尊重（防跨平台误选）。"""
    store = _store_with_convs([
        {"cid": "telegram:a1:1", "last_ts": NOW - 0.01 * DAY},  # 刚聊过
        {"cid": "line:a2:9", "platform": "line", "account_id": "a2"},
    ])
    store.set_conv_archived("telegram:a1:1", True)
    p = OutreachPlanner(store)
    ids = ["telegram:a1:1", "line:a2:9"]
    seg = p.select_segment(OutreachFilters(
        conversation_ids=ids, min_silent_days=30), now=NOW)
    assert [t.conversation_id for t in seg] == ids       # 沉默/归档不拦
    seg = p.select_segment(OutreachFilters(
        conversation_ids=ids, platform="telegram"), now=NOW)
    assert [t.conversation_id for t in seg] == ["telegram:a1:1"]


def test_ids_path_language_populated():
    store = _store_with_convs([
        {"cid": "telegram:a1:1", "language": "en"},
        {"cid": "telegram:a1:2"},
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(
        conversation_ids=["telegram:a1:1", "telegram:a1:2"]), now=NOW)
    langs = {t.conversation_id: t.language for t in seg}
    assert langs["telegram:a1:1"] == "en"
    assert langs["telegram:a1:2"] == "unknown"   # 库列默认值，如实透传
    # to_dict 随行导出（预览前端消费）
    assert "language" in seg[0].__dict__


def test_build_plan_guardrails_still_apply_to_ids():
    """点名名单照过资格管线：冷却拦、账号配额拦，理由如实进 skipped。"""
    store = _store_with_convs([
        {"cid": "telegram:a1:1"},
        {"cid": "telegram:a1:2"},
        {"cid": "telegram:a1:3"},
    ])
    # cid:1 处于触达冷却（近 1 天内触达过）
    store.record_outreach("telegram:a1:1", batch_id="b0", ts=NOW - 1 * DAY)
    p = OutreachPlanner(
        store, limiter=_FakeLimiter({"a1": 1}), cooldown_days=14)
    plan = p.build_plan(OutreachFilters(conversation_ids=[
        "telegram:a1:1", "telegram:a1:2", "telegram:a1:3"]), now=NOW)
    reasons = {s["conversation_id"]: s["reason"] for s in plan.skipped}
    assert reasons.get("telegram:a1:1") == "cooldown"
    # 配额 1：第二个进 eligible，第三个被 account_cap 拦
    assert [t.conversation_id for t in plan.eligible] == ["telegram:a1:2"]
    assert reasons.get("telegram:a1:3") == "account_cap"


def test_empty_ids_falls_back_to_segment_scan():
    store = _store_with_convs([
        {"cid": "telegram:a1:1", "last_ts": NOW - 5 * DAY},
        {"cid": "telegram:a1:2", "last_ts": NOW - 1 * DAY},
    ])
    p = OutreachPlanner(store)
    seg = p.select_segment(OutreachFilters(min_silent_days=3), now=NOW)
    assert [t.conversation_id for t in seg] == ["telegram:a1:1"]
