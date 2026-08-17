# -*- coding: utf-8 -*-
"""工作链执行操作（暂停/恢复/跳步/重试）门禁——P1 2026-08-12。

背景：工作链「零使用」复盘的行动闭环批次。操作语义全部钉在 store 层专用方法
（绝不走 update_workflow_execution 通用口——它的 current_step/next_step_at 是
无条件覆写的位置参数）：

- pause/resume＝「时钟停走」：暂停期间不消耗步间等待，恢复后按原节奏继续；
- skip＝与「该步刚完成」同语义：再下一步按其 delay_hours 从现在起算；
- retry＝失败步原位重跑（引擎自动重试预算重新计满）；
- complete 终态保留 current_step（旧实现把 failed 行的失败位置清零——重试与
  监控定位都读它，本文件含回归钉）。

路由层四端点 + caps.exec_ops 能力位（前端按钮显隐依据）一并覆盖。
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_workflow_routes import register_workflow_routes

CONV = "telegram:acc1:peer_ops"
CHAIN = "ops_test_chain"

STEPS = [
    {"action_type": "template", "note": "第一步话术", "delay_hours": 0},
    {"action_type": "template", "note": "第二步话术", "delay_hours": 24},
    {"action_type": "note", "note": "内部备注", "delay_hours": 2},
    {"action_type": "template", "note": "收口话术", "delay_hours": 48},
]


@pytest.fixture()
def store(tmp_path):
    s = InboxStore(tmp_path / "exec_ops.db")
    s.upsert_workflow_chain({
        "chain_id": CHAIN, "name": "操作测试链", "steps": STEPS,
        "trigger_conditions": {}, "enabled": True,
    })
    return s


def _start(store) -> str:
    return store.start_chain_execution(CHAIN, CONV, {"agent": "t"},
                                       schedule_first_step=True)


def _set(store, exec_id, **cols):
    """测试专用：直改执行行（模拟引擎推进到某状态）。"""
    sets = ", ".join(f"{k} = ?" for k in cols)
    store._conn.execute(
        f"UPDATE workflow_executions SET {sets} WHERE exec_id = ?",
        (*cols.values(), exec_id))
    store._conn.commit()


# ── store：pause / resume ────────────────────────────────────────────────────

class TestPauseResume:
    def test_pause_preserves_position_and_schedule(self, store):
        eid = _start(store)
        next_at = time.time() + 24 * 3600
        _set(store, eid, current_step=1, next_step_at=next_at)
        assert store.pause_workflow_execution(eid) is True
        ex = store.get_workflow_execution(eid)
        assert ex["status"] == "paused"
        assert int(ex["current_step"]) == 1          # 位置一根汗毛都不能动
        assert float(ex["next_step_at"]) == pytest.approx(next_at, abs=1)
        assert float(json.loads(ex["context_json"])["paused_at"]) == pytest.approx(
            time.time(), abs=5)

    def test_pause_only_running(self, store):
        eid = _start(store)
        _set(store, eid, status="completed")
        assert store.pause_workflow_execution(eid) is False
        assert store.pause_workflow_execution("no_such") is False

    def test_resume_shifts_wait_by_pause_duration(self, store):
        """时钟停走：暂停 1 小时 → next_step_at 顺延 1 小时（节奏不被暂停吃掉，
        也不会恢复瞬间连发积压步骤）。"""
        eid = _start(store)
        next_at = time.time() + 3600.0
        _set(store, eid, current_step=1, next_step_at=next_at)
        assert store.pause_workflow_execution(eid)
        # 把 paused_at 拨回 1 小时前（模拟暂停了 1 小时）
        ex = store.get_workflow_execution(eid)
        ctx = json.loads(ex["context_json"])
        ctx["paused_at"] = time.time() - 3600.0
        _set(store, eid, context_json=json.dumps(ctx))
        assert store.resume_workflow_execution(eid) is True
        ex = store.get_workflow_execution(eid)
        assert ex["status"] == "running"
        assert float(ex["next_step_at"]) == pytest.approx(next_at + 3600.0, abs=10)
        assert "paused_at" not in json.loads(ex["context_json"])  # 用后即清

    def test_resume_zero_next_at_not_shifted(self, store):
        eid = _start(store)
        _set(store, eid, next_step_at=0)
        assert store.pause_workflow_execution(eid)
        assert store.resume_workflow_execution(eid)
        assert float(store.get_workflow_execution(eid)["next_step_at"]) == 0

    def test_resume_only_paused(self, store):
        eid = _start(store)
        assert store.resume_workflow_execution(eid) is False   # running 不可恢复

    def test_paused_excluded_from_due_and_counted_as_inflight(self, store):
        eid = _start(store)
        _set(store, eid, next_step_at=time.time() - 10)        # 已到期
        assert store.pause_workflow_execution(eid)
        due = store.list_due_workflow_executions(time.time())
        assert all(d["exec_id"] != eid for d in due)           # 暂停=引擎不推进
        assert store.has_running_chain(CONV, CHAIN) is True    # 但占「在途」防重复开链

    def test_cancel_accepts_paused(self, store):
        eid = _start(store)
        assert store.pause_workflow_execution(eid)
        assert store.cancel_workflow_execution(eid) is True
        assert store.get_workflow_execution(eid)["status"] == "cancelled"


# ── store：skip / retry / complete 位置保留 ─────────────────────────────────

class TestSkipRetry:
    def test_skip_advances_with_next_step_delay(self, store):
        eid = _start(store)
        _set(store, eid, current_step=0, next_step_at=time.time() + 999)
        assert store.skip_workflow_step(eid) is True
        ex = store.get_workflow_execution(eid)
        assert int(ex["current_step"]) == 1
        # 与「第 0 步刚完成」同语义：第 1 步 delay_hours=24 → 从现在起算
        assert float(ex["next_step_at"]) == pytest.approx(
            time.time() + 24 * 3600, abs=10)
        assert json.loads(ex["last_result_json"])["skipped_step"] == 0
        # 环节账本如实落 skipped 行
        stats = store.workflow_step_stats(time.time() - 60)
        assert stats[CHAIN]["0"]["attempts"] == 1

    def test_skip_zero_delay_next_is_due_now(self, store):
        eid = _start(store)
        _set(store, eid, current_step=1, next_step_at=time.time() + 999)
        assert store.skip_workflow_step(eid) is True            # 跳过第 1 步
        ex = store.get_workflow_execution(eid)
        assert int(ex["current_step"]) == 2                     # 第 2 步 delay=2h
        assert float(ex["next_step_at"]) == pytest.approx(
            time.time() + 2 * 3600, abs=10)

    def test_skip_last_step_completes(self, store):
        eid = _start(store)
        _set(store, eid, current_step=3, next_step_at=time.time() + 999)
        assert store.skip_workflow_step(eid) is True
        ex = store.get_workflow_execution(eid)
        assert ex["status"] == "completed"
        assert int(ex["current_step"]) == 3                     # 终态位置保留

    def test_skip_only_running(self, store):
        eid = _start(store)
        assert store.pause_workflow_execution(eid)
        assert store.skip_workflow_step(eid) is False           # 暂停中先恢复再跳

    def test_retry_failed_resets_budget_and_reschedules(self, store):
        eid = _start(store)
        ctx = {"agent": "t", "step_retries": {"2": 1}}
        _set(store, eid, current_step=2, context_json=json.dumps(ctx))
        store.complete_workflow_execution(eid, status="failed")
        ex = store.get_workflow_execution(eid)
        assert ex["status"] == "failed"
        assert int(ex["current_step"]) == 2      # 回归钉：终态保留失败位置
        assert store.retry_workflow_execution(eid) is True
        ex = store.get_workflow_execution(eid)
        assert ex["status"] == "running"
        assert int(ex["current_step"]) == 2                     # 原步重跑
        assert float(ex["next_step_at"]) == pytest.approx(time.time(), abs=10)
        assert json.loads(ex["context_json"])["step_retries"] == {}  # 预算清零
        # next_step_at=now → 下一 tick 即到期可推进
        due = store.list_due_workflow_executions(time.time() + 1)
        assert any(d["exec_id"] == eid for d in due)

    def test_retry_only_failed(self, store):
        eid = _start(store)
        assert store.retry_workflow_execution(eid) is False


# ── 路由层：四端点 + caps + 富化字段 ────────────────────────────────────────

def _api_auth(request: Request):
    return None


def _build(tmp_path):
    from types import SimpleNamespace
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    register_workflow_routes(app, api_auth=_api_auth)
    store = InboxStore(tmp_path / "exec_ops_api.db")
    store.upsert_workflow_chain({
        "chain_id": CHAIN, "name": "操作测试链", "steps": STEPS,
        "trigger_conditions": {}, "enabled": True,
    })
    app.state.inbox_store = store
    app.state.config_manager = SimpleNamespace(config={}, config_path=None)
    return TestClient(app), store


class TestRoutes:
    def test_caps_and_actionable_note_in_conv_listing(self, tmp_path):
        client, store = _build(tmp_path)
        eid = _start(store)
        _set(store, eid, current_step=2, next_step_at=time.time() + 3600)
        r = client.get(f"/api/workspace/conv/{CONV}/chain-executions")
        assert r.status_code == 200
        d = r.json()
        assert d["caps"] == {"exec_ops": True}     # 前端按钮显隐的能力位
        ex = d["executions"][0]
        # current_step=2（下一步=内部备注），坐席该跟的是第 1 步已发出的话术
        assert ex["actionable_note"] == "第二步话术"
        assert ex["actionable_step_idx"] == 1

    def test_pause_resume_roundtrip(self, tmp_path):
        client, store = _build(tmp_path)
        eid = _start(store)
        r = client.post(f"/api/workspace/chain-executions/{eid}/pause")
        assert r.status_code == 200
        assert r.json()["execution"]["status"] == "paused"
        assert r.json()["execution"]["status_label"] == "已暂停"
        # 状态守卫：paused 不能再 pause / skip
        assert client.post(
            f"/api/workspace/chain-executions/{eid}/pause").status_code == 422
        assert client.post(
            f"/api/workspace/chain-executions/{eid}/skip-step").status_code == 422
        r = client.post(f"/api/workspace/chain-executions/{eid}/resume")
        assert r.status_code == 200
        assert r.json()["execution"]["status"] == "running"
        # running 不能 resume / retry
        assert client.post(
            f"/api/workspace/chain-executions/{eid}/resume").status_code == 422
        assert client.post(
            f"/api/workspace/chain-executions/{eid}/retry").status_code == 422

    def test_skip_and_retry_via_api(self, tmp_path):
        client, store = _build(tmp_path)
        eid = _start(store)
        r = client.post(f"/api/workspace/chain-executions/{eid}/skip-step")
        assert r.status_code == 200
        assert r.json()["execution"]["current_step"] == 1
        store.complete_workflow_execution(eid, status="failed")
        r = client.post(f"/api/workspace/chain-executions/{eid}/retry")
        assert r.status_code == 200
        assert r.json()["execution"]["status"] == "running"

    def test_cancel_paused_via_api(self, tmp_path):
        client, store = _build(tmp_path)
        eid = _start(store)
        assert client.post(
            f"/api/workspace/chain-executions/{eid}/pause").status_code == 200
        r = client.post(f"/api/workspace/chain-executions/{eid}/cancel")
        assert r.status_code == 200
        assert r.json()["execution"]["status"] == "cancelled"

    def test_404_unknown_exec(self, tmp_path):
        client, _ = _build(tmp_path)
        for op in ("pause", "resume", "skip-step", "retry"):
            assert client.post(
                f"/api/workspace/chain-executions/nope/{op}").status_code == 404
