# -*- coding: utf-8 -*-
"""目标达成报表门禁（P0 2026-08-09：账号×客户完成情况）。

覆盖：
- store.account_matrix：账号聚合口径（终态落窗 / done_rate 分母排除 cancelled /
  won=result 前缀 / won_amount 汇总 won_meta 事件 / active 不落窗 / 主力模板）；
- store.contact_outcomes：状态筛选（done/ended/active/all）+ 账号过滤 + 分页 total；
- report.matrix_report：顶层合计与加权平均天数；
- report.contacts_report：客户名富化 + **followed_up 客观推导**（done 后有出站
  ＝已跟进；inbox store 不可用 → None 不误标）+ won_meta 金额 + 推荐后续链；
- InboxStore.last_outbound_ts_map：只认出站、批量 IN 查询；
- 路由：/api/goals/report/accounts + /contacts（200 形状 / 403 disabled /
  非法 status 400；报表只读 viewer 可读）。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals import report as goal_report
from src.companion.goals.store import GoalStore, reset_goal_store
from src.web.routes.goal_routes import register_goal_routes

# NOW 锚到「今天正午」（本地时区，与 daily_outcomes 本地日分桶同口径）：
# 裸 time.time() 是时间炸弹——test_daily_outcomes 用 0.1/0.2 天前造「今天」的
# 完成，凌晨 00:00-04:48 跑测试时 4.8h 前落进昨天桶必红（2026-08-18 04:35 实锤）。
# 锚正午后 NOW-0.2d=07:12 同日恒成立，任何时刻跑都稳定。
_lt = time.localtime()
NOW = time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 12, 0, 0, 0, 0, -1))
DAY = 86400.0


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc
    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


def _mk_store() -> GoalStore:
    return GoalStore(":memory:")


def _goal(store, *, account_id="a1", chat_key="100",
          template="conversion_unlock", status="active", done_ago_days=0.0,
          result="", won_amount=None, start_ago_days=5.0):
    g = store.create_goal(
        conversation_id=f"telegram:{account_id}:{chat_key}",
        platform="telegram", account_id=account_id, chat_key=chat_key,
        template=template, now=NOW - start_ago_days * DAY)
    assert g is not None
    gid = g["goal_id"]
    if status != "active":
        store.update_goal_fields(
            gid, status=status, done_at=NOW - done_ago_days * DAY,
            result=result)
    if won_amount is not None:
        store.add_event(gid, "won_meta", json.dumps(
            {"amount": won_amount, "product": "pro"}))
    return store.get_goal(gid)


class _FakeInbox:
    def __init__(self, names=None, out_ts=None):
        self._names = names or {}
        self._out = out_ts or {}

    def get_conversations_for_ids(self, ids):
        return {cid: {"display_name": self._names[cid]}
                for cid in ids if cid in self._names}

    def last_outbound_ts_map(self, ids):
        return {cid: self._out[cid] for cid in ids if cid in self._out}


# ── store.account_matrix ────────────────────────────────────────────────────

def test_account_matrix_aggregation():
    store = _mk_store()
    # a1：done×2（其一 order 带金额）+ failed×1 + cancelled×1 + active×1
    _goal(store, chat_key="1", status="done", done_ago_days=1,
          result="order:pro:O1", won_amount=100)
    _goal(store, chat_key="2", status="done", done_ago_days=2,
          result="unlocked:item")
    _goal(store, chat_key="3", status="failed", done_ago_days=1)
    _goal(store, chat_key="4", status="cancelled", done_ago_days=1)
    _goal(store, chat_key="5", status="active")
    # a2：仅 active（零终态账号也要出现在矩阵）
    _goal(store, account_id="a2", chat_key="9", status="active")
    rows = store.account_matrix(NOW - 30 * DAY, now=NOW)
    by = {(r["platform"], r["account_id"]): r for r in rows}
    a1 = by[("telegram", "a1")]
    assert a1["done"] == 2 and a1["failed"] == 1 and a1["cancelled"] == 1
    assert a1["active"] == 1
    # done_rate 分母 = done+failed+expired（排除 cancelled）= 3
    assert a1["done_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert a1["won"] == 1                       # 只有 order:/manual: 算 won
    assert a1["won_amount"] == 100.0
    assert a1["top_template"] == "conversion_unlock"
    assert a1["avg_days_to_done"] is not None
    a2 = by[("telegram", "a2")]
    assert a2["active"] == 1 and a2["done"] == 0
    # 排序：done 多的在前
    assert rows[0]["account_id"] == "a1"


def test_account_matrix_window_excludes_old_terminals():
    store = _mk_store()
    _goal(store, chat_key="1", status="done", done_ago_days=40,
          result="manual:agent")
    rows = store.account_matrix(NOW - 30 * DAY, now=NOW)
    assert rows == [] or all(r["done"] == 0 for r in rows)


# ── store.contact_outcomes ──────────────────────────────────────────────────

def test_contact_outcomes_filters_and_pagination():
    store = _mk_store()
    for i in range(3):
        _goal(store, chat_key=f"d{i}", status="done", done_ago_days=i + 1,
              result="manual:agent")
    _goal(store, chat_key="f1", status="failed", done_ago_days=1)
    _goal(store, chat_key="act", status="active")
    _goal(store, account_id="b7", chat_key="x", status="done",
          done_ago_days=1, result="order:p:1")

    res = store.contact_outcomes(NOW - 30 * DAY, status="done")
    assert res["total"] == 4
    # done_at DESC：最近完成的在前
    assert res["rows"][0]["done_at"] >= res["rows"][-1]["done_at"]

    res = store.contact_outcomes(NOW - 30 * DAY, status="done",
                                 account_id="b7")
    assert res["total"] == 1 and res["rows"][0]["account_id"] == "b7"

    res = store.contact_outcomes(NOW - 30 * DAY, status="ended")
    assert res["total"] == 5                    # done×4 + failed×1

    res = store.contact_outcomes(NOW - 30 * DAY, status="active")
    assert res["total"] == 1 and res["rows"][0]["status"] == "active"

    res = store.contact_outcomes(NOW - 30 * DAY, status="")
    assert res["total"] == 6                    # 全部（active + 落窗终态）

    res = store.contact_outcomes(NOW - 30 * DAY, status="done",
                                 limit=2, offset=2)
    assert res["total"] == 4 and len(res["rows"]) == 2


# ── report 纯函数 ───────────────────────────────────────────────────────────

def test_matrix_report_totals():
    store = _mk_store()
    _goal(store, chat_key="1", status="done", done_ago_days=1,
          result="order:pro:O1", won_amount=50)
    _goal(store, account_id="a2", chat_key="2", status="done",
          done_ago_days=2, result="manual:agent", won_amount=70)
    _goal(store, account_id="a2", chat_key="3", status="expired",
          done_ago_days=1)
    out = goal_report.matrix_report(store, days=30, now=NOW)
    t = out["totals"]
    assert t["done"] == 2 and t["expired"] == 1 and t["won"] == 2
    assert t["won_amount"] == 120.0
    assert t["done_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert t["avg_days_to_done"] is not None
    assert len(out["accounts"]) == 2


def test_contacts_report_enrichment_and_followed_up():
    store = _mk_store()
    g1 = _goal(store, chat_key="1", status="done", done_ago_days=1,
               result="order:pro:O1", won_amount=88)
    g2 = _goal(store, chat_key="2", status="done", done_ago_days=2,
               result="manual:agent")
    inbox = _FakeInbox(
        names={g1["conversation_id"]: "小美"},
        out_ts={
            g1["conversation_id"]: NOW,                    # done 之后有出站
            g2["conversation_id"]: NOW - 3 * DAY,          # 出站在 done 之前
        })
    out = goal_report.contacts_report(
        store, inbox, days=30, status="done", now=NOW)
    rows = {r["goal_id"]: r for r in out["rows"]}
    r1, r2 = rows[g1["goal_id"]], rows[g2["goal_id"]]
    assert r1["contact_name"] == "小美" and r2["contact_name"] == ""
    assert r1["followed_up"] is True and r2["followed_up"] is False
    assert r1["amount"] == 88 and r1["result_kind"] == "order"
    assert r1["days_to_done"] is not None
    assert r1["template_name"]
    assert "rec_chain" in r1                    # 完成行带推荐后续链（可为空串）


def test_contacts_report_marks_miss_kind_on_terminal_rows():
    """P5 三分法进报表行：failed/expired 行带 miss_kind，done/active 行不带。"""
    store = _mk_store()
    g_off = _goal(store, chat_key="1", status="expired", done_ago_days=1)
    store.upsert_action(g_off["goal_id"], "2026-08-08",
                        push_level="direct", status="consumed")
    _goal(store, chat_key="2", status="failed", done_ago_days=1)
    g_done = _goal(store, chat_key="3", status="done", done_ago_days=1,
                   result="manual:agent")

    class _Inbox(_FakeInbox):
        def count_inbound_between(self, cid, since, until):
            return 0

    out = goal_report.contacts_report(
        store, _Inbox(), days=30, status="ended", now=NOW)
    rows = {r["goal_id"]: r for r in out["rows"]}
    assert rows[g_off["goal_id"]]["miss_kind"] == "offered"
    assert "miss_kind" not in rows[g_done["goal_id"]]
    silent = [r for r in out["rows"]
              if r["status"] == "failed"][0]
    assert silent["miss_kind"] == "silent"


def test_inbox_count_inbound_between(tmp_path):
    from src.inbox.models import InboxConversation, InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a1:100"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a1",
        chat_key="100", display_name="A", last_ts=NOW, last_text="hi"))
    for i, (d, ts) in enumerate((("in", NOW - 50), ("out", NOW - 40),
                                 ("in", NOW - 30), ("in", NOW - 5))):
        store.ingest_message(InboxMessage(
            conversation_id=cid, platform_msg_id=f"m{i}", direction=d,
            text="x", ts=ts))
    assert store.count_inbound_between(cid, NOW - 60, NOW - 10) == 2
    assert store.count_inbound_between(cid, NOW - 60, 0) == 3   # 无上限
    assert store.count_inbound_between("", NOW - 60, 0) == 0


def test_contacts_report_without_inbox_store_degrades():
    store = _mk_store()
    g = _goal(store, chat_key="1", status="done", done_ago_days=1,
              result="manual:agent")
    out = goal_report.contacts_report(
        store, None, days=30, status="done", now=NOW)
    row = out["rows"][0]
    assert row["goal_id"] == g["goal_id"]
    # inbox 不可用 → 名字空串、followed_up=None（未知，不误标「待跟进」）
    assert row["contact_name"] == "" and row["followed_up"] is None


# ── P2：趋势 / 热力 / 周报窗口段 ────────────────────────────────────────────

def test_daily_outcomes_fills_zero_days_and_counts_won():
    store = _mk_store()
    _goal(store, chat_key="1", status="done", done_ago_days=0.1,
          result="order:pro:O1")
    _goal(store, chat_key="2", status="done", done_ago_days=0.2,
          result="unlocked:item")
    _goal(store, chat_key="3", status="done", done_ago_days=40)   # 窗口外
    rows = store.daily_outcomes(days=14, now=NOW)
    assert len(rows) == 14                          # 无数据日也有零行
    assert all(set(r) == {"day", "done", "won"} for r in rows)
    today = rows[-1]
    assert today["done"] == 2 and today["won"] == 1  # won 只认 order:/manual:
    assert sum(r["done"] for r in rows) == 2         # 窗口外不计


def test_account_template_matrix_cells():
    store = _mk_store()
    _goal(store, chat_key="1", status="done", done_ago_days=1,
          result="manual:agent")
    _goal(store, chat_key="2", status="done", done_ago_days=1,
          result="manual:agent")
    _goal(store, account_id="b2", chat_key="3", status="done",
          done_ago_days=1, template="conversion_subscribe",
          result="order:p:1")
    _goal(store, chat_key="9", status="failed", done_ago_days=1)   # 非 done 不进热力
    cells = store.account_template_matrix(NOW - 30 * DAY)
    assert cells["telegram:a1"]["conversion_unlock"] == 2
    assert cells["telegram:b2"]["conversion_subscribe"] == 1
    assert "failed" not in str(cells)


def test_outcome_counts_window_semantics():
    store = _mk_store()
    _goal(store, chat_key="1", status="done", done_ago_days=1,
          result="order:pro:O1", won_amount=100)
    _goal(store, chat_key="2", status="expired", done_ago_days=2)
    _goal(store, chat_key="3", status="done", done_ago_days=10,
          result="manual:agent", won_amount=50)     # 窗口外（上周）
    tw = store.outcome_counts(NOW - 7 * DAY, NOW)
    assert tw == {"done": 1, "failed": 0, "expired": 1, "won": 1,
                  "won_amount": 100.0}
    lw = store.outcome_counts(NOW - 14 * DAY, NOW - 7 * DAY)
    assert lw["done"] == 1 and lw["won_amount"] == 50.0


def test_matrix_report_includes_trend_and_heat():
    store = _mk_store()
    g = _goal(store, chat_key="1", status="done", done_ago_days=1,
              result="manual:agent")
    out = goal_report.matrix_report(store, days=30, now=NOW)
    assert len(out["trend"]) == 30
    assert sum(r["done"] for r in out["trend"]) == 1
    heat = out["heat"]
    assert heat["accounts"] == ["telegram:a1"]
    assert heat["templates"][0]["id"] == "conversion_unlock"
    assert heat["templates"][0]["name"]            # 模板显示名非空
    assert heat["cells"]["telegram:a1"]["conversion_unlock"] == 1
    assert g["goal_id"]
    # 零数据：空容器（前端据此隐藏区块）
    empty = goal_report.matrix_report(_mk_store(), days=7, now=NOW)
    assert empty["heat"] == {"accounts": [], "templates": [], "cells": {}}
    assert sum(r["done"] for r in empty["trend"]) == 0


# ── InboxStore.last_outbound_ts_map ─────────────────────────────────────────

def test_inbox_last_outbound_ts_map(tmp_path):
    from src.inbox.models import InboxConversation, InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a1:100"
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a1",
        chat_key="100", display_name="Alice", last_ts=NOW, last_text="hi"))
    t0 = NOW - 100
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m1", direction="in",
        text="hello", ts=t0))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m2", direction="out",
        text="hey", ts=t0 + 10))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m3", direction="out",
        text="again", ts=t0 + 20))
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="m4", direction="in",
        text="last inbound", ts=t0 + 30))
    got = store.last_outbound_ts_map([cid, "telegram:a1:missing"])
    assert got.get(cid) == pytest.approx(t0 + 20, abs=0.5)
    assert "telegram:a1:missing" not in got
    assert store.last_outbound_ts_map([]) == {}


# ── 路由 ───────────────────────────────────────────────────────────────────

def _build_client(enabled=True):
    goals = {"enabled": enabled, "db_path": ":memory:"}
    cfg = {"companion": {"goals": goals}}
    sess = {"role": "", "user": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(
        app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    return TestClient(app), sess


def test_report_routes_disabled_403():
    client, _ = _build_client(enabled=False)
    assert client.get("/api/goals/report/accounts").status_code == 403
    assert client.get("/api/goals/report/contacts").status_code == 403


def test_report_routes_shapes_and_filters():
    client, sess = _build_client()
    r = client.post("/api/goals", json={
        "template": "conversion_unlock",
        "conversation_id": "telegram:a1:100"})
    assert r.status_code == 200
    gid = r.json()["goal"]["goal_id"]
    r = client.post(f"/api/goals/{gid}/status", json={
        "action": "done", "meta": {"amount": 66, "product": "pro"}})
    assert r.status_code == 200

    r = client.get("/api/goals/report/accounts?days=30")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["window_days"] == 30
    assert d["totals"]["done"] == 1 and d["totals"]["won"] == 1
    assert d["totals"]["won_amount"] == 66.0
    assert d["accounts"][0]["account_id"] == "a1"
    # P2：趋势与热力随响应带出
    assert len(d["trend"]) == 30 and sum(x["done"] for x in d["trend"]) == 1
    assert d["heat"]["cells"]["telegram:a1"]["conversion_unlock"] == 1

    r = client.get("/api/goals/report/contacts?days=30&status=done")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1
    row = d["rows"][0]
    assert row["goal_id"] == gid and row["result_kind"] == "manual"
    assert row["amount"] == 66

    # viewer 只读也能看报表（报表是读数面，不是写操作）
    sess["role"] = "viewer"
    assert client.get("/api/goals/report/accounts").status_code == 200

    # 非法 status → 400
    sess["role"] = ""
    r = client.get("/api/goals/report/contacts?status=bogus")
    assert r.status_code == 400


def test_readiness_exposes_scan_loop_heartbeat():
    """P3：readiness 带常备扫描循环心跳——「没跑」和「没货」从外面分得出来
    （P0 扫描器挂死调度器上静默从未运行的教训）。未挂载=空 dict 如实外露。"""
    client, _ = _build_client()
    r = client.get("/api/goals/readiness")
    assert r.status_code == 200
    assert r.json().get("scan_loop") == {}          # 测试 app 没挂循环
    client.app.state.goal_scan_state = {
        "ticks": 7, "gated": "", "settled_total": 3}
    d = client.get("/api/goals/readiness").json()
    assert d["scan_loop"]["ticks"] == 7
    assert d["scan_loop"]["settled_total"] == 3
