# -*- coding: utf-8 -*-
"""流失预警轻量榜 / 坐席 QA 聚合的**真 SQL 执行**门禁（2026-08-01 幽灵列事故回归网）。

事故：``conversation_meta`` 从未有过 ``claimed_by`` 列（会话认领态只活在进程内
AgentCoordinator），但 Phase 34/35 的两条 SQL 直接引用 ``cm.claimed_by`` →
``batch_agent_qa_stats`` / ``list_churn_risk_conversations`` 自出生起在**所有部署**
上 OperationalError（路由 500），而此前没有任何测试真正执行过这两条 SQL——
`/api/workspace/churn-risks` 与 agent_perf 的流失表在生产上一直是死的，直到
真浏览器门禁（tools/verify_relations_ui.py）把它钓出来。

本文件的存在理由＝**让「SQL 引用不存在的列」这类缺陷在 CI 就红**：
对真 InboxStore（全套 DDL+迁移）跑真查询 + 经真路由端到端打一发。
"""
from __future__ import annotations

import sys
import time as _t
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore

NOW = _t.time()
DAY = 86400


@pytest.fixture
def store(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    yield st
    st.close()


def _conv(store, cid, *, last_off_d, name="客户", platform="telegram"):
    parts = cid.split(":")
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform=platform, account_id=parts[1],
        chat_key=parts[2], display_name=name, chat_type="private",
        last_ts=NOW - last_off_d * DAY))
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            "INSERT INTO messages(message_id, conversation_id, direction, ts, "
            "ingested_at) VALUES (?, ?, 'in', ?, ?)",
            (f"m_{cid}", cid, NOW - last_off_d * DAY, NOW))
        store._conn.commit()  # noqa: SLF001


def test_list_churn_risk_sql_executes_and_contract(store):
    """真 SQL 必须可执行；返回行带 ChurnPredictor 需要的全部键（含占位 claimed_by）。"""
    _conv(store, "telegram:a:1001", last_off_d=20)
    _conv(store, "telegram:a:1002", last_off_d=0.1)  # 活跃 → 不入候选
    rows = store.list_churn_risk_conversations(silence_days=7, limit=50)
    assert len(rows) == 1
    r = rows[0]
    for key in ("conversation_id", "platform", "display_name", "contact_id",
                "last_ts", "claimed_by", "churn_risk", "qa_score", "archived"):
        assert key in r, f"missing key: {key}"
    assert r["conversation_id"] == "telegram:a:1001"
    assert r["claimed_by"] == ""  # 占位契约：认领态未落库，恒空


def test_batch_agent_qa_stats_sql_executes(store):
    """真 SQL 必须可执行（认领未落库 → 聚合自然为空，但绝不能 500）。"""
    _conv(store, "telegram:a:2001", last_off_d=1)
    store.compute_and_store_qa_score("telegram:a:2001")
    out = store.batch_agent_qa_stats(days=30)
    assert isinstance(out, list)


def test_churn_risks_route_end_to_end(store):
    """经真路由打一发：/api/workspace/churn-risks 必须 ok 且沉默会话上榜。"""
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from src.web.routes.unified_inbox_qa_churn_routes import register_qa_churn_routes

    _conv(store, "telegram:a:3001", last_off_d=20, name="老客A")
    _conv(store, "telegram:a:3002", last_off_d=9, name="老客B")

    app = FastAPI()
    register_qa_churn_routes(app, api_auth=lambda request: None)
    app.state.inbox_store = store
    tc = TestClient(app)
    d = tc.get("/api/workspace/churn-risks?silence_days=7&limit=50").json()
    assert d["ok"] is True
    items = d["items"]
    assert len(items) >= 1  # 20 天沉默 + 末条入站 → 必为 medium+
    top = items[0]
    assert top["conversation_id"]
    assert top["risk_level"] in ("high", "medium")
    assert isinstance(top.get("reasons"), list) and top["reasons"]
    # KPI 汇总字段契约（页面 KPI 卡消费）
    assert "high_count" in d and "medium_count" in d