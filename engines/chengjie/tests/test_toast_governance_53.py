# -*- coding: utf-8 -*-
"""#53/#48 门禁（实施90 二批）：右下角提示治理四件套 + 工作链失败风暴收口。

金标＝0830 实录：① 811 原图「告警通道未接通」胶囊盖住工具箱/语音面板且无法
关闭（四票）；② 28DTZS 诊断包「条件自动启动一轮 10-20 条、step0 集体失败、
每失败一弹红框刷屏」，且 step 失败**原因**从未落账（日志轮转后根因失传）。
"""
from __future__ import annotations

import time
from pathlib import Path

from src.inbox.store import InboxStore
from src.inbox.workflow_runner import STEP_RETRY_DELAY_SEC, WorkflowRunner

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _store_with_conv(last_ts: float) -> InboxStore:
    store = InboxStore(":memory:")
    n = time.time()
    store._conn.execute(
        """INSERT INTO conversations
           (conversation_id, platform, display_name, last_ts, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        ("telegram:a1:100", "telegram", "客户A", last_ts, n, n))
    store._conn.commit()
    return store


# ── #48-①：失败原因落账（根因可追） ─────────────────────────────────────────

def test_task_step_failure_records_reason_48():
    """task 步在 contacts 未接线时失败，step 账本 detail 必须带机器可读原因。"""
    store = _store_with_conv(time.time())
    store.upsert_workflow_chain({
        "chain_id": "c_task", "name": "推进链",
        "steps": [{"action_type": "task", "note": "跟进"}],
    })
    store.start_chain_execution(
        "c_task", "telegram:a1:100", {}, schedule_first_step=True)
    runner = WorkflowRunner(store, contacts_store=None)
    now = time.time()
    runner.process_due_executions(now=now)                       # 首败 → 重试
    runner.process_due_executions(now=now + STEP_RETRY_DELAY_SEC + 5)  # 重试耗尽
    ex = store.list_chain_executions(status="failed")
    assert len(ex) == 1
    rows = store._conn.execute(
        "SELECT ok, detail FROM workflow_step_log").fetchall()
    assert rows, "步级账本必须有行"
    assert any((not r[0]) and "no_contacts_store" in str(r[1]) for r in rows), \
        [tuple(r) for r in rows]


def test_execute_step_failure_reasons_pinned_48():
    """三个失败分支都必须写 error 原因（detail 恒空=值守根因失传的直接原因）。"""
    src = (_ENGINE_ROOT / "src" / "inbox" / "workflow_runner.py").read_text(
        encoding="utf-8")
    for reason in ("no_contacts_store", "no_contact_id", "task_store_error",
                   "note_store_error", "tag_apply_error"):
        assert reason in src, reason


# ── #48-②：自动开链可执行性预检（必失败的链不批量开） ────────────────────────

def test_auto_start_skips_undeliverable_task_chain_48():
    """task 链在 contacts 未接线时不自动开；可执行链照常开。"""
    old = time.time() - 10 * 86400
    store = _store_with_conv(old)
    store.upsert_workflow_chain({
        "chain_id": "c_task", "name": "task 链", "enabled": True,
        "trigger_conditions": {"silence_days": 3},
        "steps": [{"action_type": "task", "note": "跟进"}],
    })
    store.upsert_workflow_chain({
        "chain_id": "c_note", "name": "note 链", "enabled": True,
        "trigger_conditions": {"silence_days": 3},
        "steps": [{"action_type": "note", "note": "记一笔"}],
    })
    runner = WorkflowRunner(store, contacts_store=None)
    started = runner.auto_start_chains(max_per_day=30)
    assert started >= 1
    execs = store._conn.execute(
        "SELECT chain_id FROM workflow_executions").fetchall()
    chain_ids = {str(r[0]) for r in execs}
    assert "c_task" not in chain_ids, "必失败链不得被自动批量开出"
    assert "c_note" in chain_ids


def test_auto_start_blocker_pure_48():
    store = InboxStore(":memory:")
    r_none = WorkflowRunner(store, contacts_store=None)
    assert r_none._auto_start_blocker(
        {"steps_json": '[{"action_type": "task"}]'}) != ""
    assert r_none._auto_start_blocker(
        {"steps_json": '[{"action_type": "note"}]'}) == ""

    class _FakeContacts:
        pass

    r_ok = WorkflowRunner(store, contacts_store=_FakeContacts())
    assert r_ok._auto_start_blocker(
        {"steps_json": '[{"action_type": "task"}]'}) == ""
    # 解析失败按可开（运行时失败账本兜底，那里现在有原因了）
    assert r_none._auto_start_blocker({"steps_json": "not-json"}) == ""


# ── #53：四件套静态契约（模板/总线热更新直上生产，静态钉住防回退） ────────────

def test_notify_bus_has_aggregation_and_dismiss_53():
    src = (_ENGINE_ROOT / "src" / "web" / "static" / "workspace"
           / "notify-bus.js").read_text(encoding="utf-8")
    assert "data-ntf-count" in src            # ④ 同类聚合计数
    assert "dismissMs" in src                 # ① 常驻胶囊可关闭
    assert "aitr.ntf.ong.snooze." in src      # ✕ 静默跨标签页
    assert "_ongSnoozed" in src               # 静默期内 set 忽略


def test_wf_failed_toast_aggregated_53():
    """workspace_base 工作链失败提醒必须走总线 domKey 聚合（10-20 弹→1 卡×N）。"""
    src = (_ENGINE_ROOT / "src" / "web" / "templates"
           / "workspace_base.html").read_text(encoding="utf-8")
    assert "domKey:'wf_failed'" in src
    assert "base.sse.wf_failed_open" in src   # ② 点击直达处理页
    assert "/workflows" in src


def test_alertlink_capsule_dismissible_53():
    """「告警通道未接通」胶囊必须带 dismissMs（811 原图：盖住工具箱无法关闭）。"""
    src = (_ENGINE_ROOT / "src" / "web" / "templates"
           / "_alertlink_connect.html").read_text(encoding="utf-8")
    assert "dismissMs" in src


def test_wf_failed_open_key_bilingual_53():
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")
    en = get_translations("en")
    assert zh.get("base.sse.wf_failed_open")
    assert en.get("base.sse.wf_failed_open")
