# -*- coding: utf-8 -*-
"""链自动推进（exec_mode=auto）门禁——P2 2026-08-13。

核心不变量：
- 链层**绝不自建发送**：自动步唯一产物是一条 L2 pending 草稿
  （source_kind='inbox' + source_id='wf:{exec}:{step}' 唯一键幂等），
  投递完全交给既有 autosend 管线；
- 四重闸缺一回落 remind（提醒档），拍绝不静默丢：全局开关 / 链 exec_mode /
  会话 auto_ai / 静默窗（窗内=顺延不执行）；
- 预算双层（tick + 每日 DB 口径）超限回落 remind；
- 生成失败/无入站上下文 → 发经典提醒 toast（降级可见）。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.inbox.store import InboxStore
from src.inbox.workflow_auto_step import (
    build_auto_instruction,
    in_quiet_hours,
    make_auto_step_hook,
    quiet_defer_until,
    resolve_auto_advance_cfg,
)
from src.inbox.workflow_runner import WorkflowRunner

CONV = "telegram:acc1:peer_auto"
CHAIN = "auto_mode_chain"

STEPS = [
    {"action_type": "template", "note": "第一步话术", "delay_hours": 0},
    {"action_type": "template", "note": "第二步话术", "delay_hours": 24},
]


class _AppState:
    """最小 app.state 替身：只带 inbox_store（生成路径另行 monkeypatch）。"""

    def __init__(self, store):
        self.inbox_store = store


def _mk_store(tmp_path, *, exec_mode="auto"):
    s = InboxStore(tmp_path / "auto_step.db")
    s.upsert_workflow_chain({
        "chain_id": CHAIN, "name": "自动档链", "steps": STEPS,
        "trigger_conditions": {}, "enabled": True, "exec_mode": exec_mode,
    })
    return s


def _cfg(enabled=True, **kw):
    aa = {"enabled": enabled}
    aa.update(kw)
    return {"inbox": {"workflows": {"auto_advance": aa}}}


# ── 纯函数 ───────────────────────────────────────────────────────────────────

class TestPureFns:
    def test_cfg_defaults_off(self):
        c = resolve_auto_advance_cfg({})
        assert c["enabled"] is False
        assert c["quiet_start"] == 23 and c["quiet_end"] == 8
        assert c["max_per_tick"] >= 1 and c["max_per_day"] >= 1
        assert resolve_auto_advance_cfg(None)["enabled"] is False

    def test_quiet_hours_wrap_and_disable(self):
        assert in_quiet_hours(23, 23, 8) is True      # 跨午夜起点
        assert in_quiet_hours(3, 23, 8) is True
        assert in_quiet_hours(8, 23, 8) is False      # 窗止即出窗
        assert in_quiet_hours(12, 23, 8) is False
        assert in_quiet_hours(10, 9, 17) is True      # 不跨午夜
        assert in_quiet_hours(3, 8, 8) is False       # start==end=不启用

    def test_quiet_defer_until_future_and_deterministic(self):
        now = time.mktime(time.strptime("2026-08-13 02:00", "%Y-%m-%d %H:%M"))
        u1 = quiet_defer_until(now, 8, CONV)
        u2 = quiet_defer_until(now, 8, CONV)
        assert u1 == u2                                # 同会话抖动确定性
        assert u1 > now
        assert time.localtime(u1).tm_hour == 8         # 落在窗止小时（+15min 内抖动）
        # 已过今日窗止 → 顺延到明天
        now_pm = time.mktime(time.strptime("2026-08-13 12:00", "%Y-%m-%d %H:%M"))
        u3 = quiet_defer_until(now_pm, 8, CONV)
        assert u3 > now_pm and time.localtime(u3).tm_mday == 14

    def test_instruction_carries_chain_step_note(self):
        s = build_auto_instruction("报价跟单", 1, "确认对方的态度")
        assert "报价跟单" in s and "第2步" in s and "确认对方的态度" in s
        assert "不要复读" in s                          # 自动跟进防复读钉子


# ── hook 闸门与预算 ─────────────────────────────────────────────────────────

class TestHookGates:
    def test_disabled_returns_none(self, tmp_path):
        store = _mk_store(tmp_path)
        assert make_auto_step_hook(_AppState(store), _cfg(enabled=False)) is None
        assert make_auto_step_hook(_AppState(store), {}) is None

    def test_non_auto_ai_conversation_falls_back_remind(self, tmp_path):
        store = _mk_store(tmp_path)
        store.set_automation_mode(CONV, "review", source="test")
        hook = make_auto_step_hook(_AppState(store), _cfg())
        v = hook(CONV, STEPS[0], {"exec_id": "e1", "current_step": 0})
        assert v == {"action": "remind"}

    def test_quiet_hours_defer(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        store.set_automation_mode(CONV, "auto_ai", source="test")
        # 静默窗全天（0-0 是禁用；用 0-23 覆盖当前小时不可靠 → 直接钉当前小时在窗内）
        cur_h = time.localtime().tm_hour
        hook = make_auto_step_hook(
            _AppState(store),
            _cfg(quiet_start=cur_h, quiet_end=(cur_h + 1) % 24))
        v = hook(CONV, STEPS[0], {"exec_id": "e1", "current_step": 0})
        assert v["action"] == "defer" and v["until"] > time.time()

    async def test_auto_verdict_and_tick_budget(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        store.set_automation_mode(CONV, "auto_ai", source="test")
        scheduled = []

        async def _fake_stage(app_state, st, ex, note, **_kw):
            # **_kw：实施93 起 hook 会透传 cta_raw/cfg_root（步骤级转化目标）
            scheduled.append(note)
            return True

        import src.inbox.workflow_auto_step as mod
        monkeypatch.setattr(mod, "_generate_and_stage", _fake_stage)
        hook = make_auto_step_hook(
            _AppState(store), _cfg(quiet_start=0, quiet_end=0, max_per_tick=2))
        ex = {"exec_id": "e1", "chain_id": CHAIN, "chain_name": "自动档链",
              "current_step": 0}
        assert hook(CONV, STEPS[0], ex)["action"] == "auto"
        assert hook(CONV, STEPS[0], ex)["action"] == "auto"
        # 第 3 次超 tick 预算 → 回落提醒
        assert hook(CONV, STEPS[0], ex)["action"] == "remind"
        await asyncio.sleep(0)          # 让已调度任务跑完
        assert len(scheduled) == 2

    async def test_daily_budget_via_db(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        store.set_automation_mode(CONV, "auto_ai", source="test")
        # 预填今日 1 条 wf: 草稿 → max_per_day=1 时直接回落
        store.upsert_draft({
            "source_kind": "inbox", "source_id": "wf:seed:0",
            "conversation_id": CONV, "platform": "telegram",
            "account_id": "acc1", "chat_key": "peer_auto",
            "draft_text": "x", "autopilot_level": "L2", "risk_level": "low",
            "status": "pending",
        })
        hook = make_auto_step_hook(
            _AppState(store), _cfg(quiet_start=0, quiet_end=0, max_per_day=1))
        v = hook(CONV, STEPS[0], {"exec_id": "e1", "current_step": 0})
        assert v == {"action": "remind"}


# ── 生成任务：草稿落库 / 幂等 / 降级 ─────────────────────────────────────────

class TestGenerateAndStage:
    def _seed_messages(self, store):
        # 直插最小行（ingest_message 收 NormalizedMessage 对象；这里只需要
        # list_recent_messages 能读出一条入站原话）
        now = time.time()
        store._conn.execute(
            "INSERT INTO messages (message_id, conversation_id, direction,"
            " text, ts, ingested_at) VALUES (?,?,?,?,?,?)",
            ("m1", CONV, "in", "你们这个怎么收费？", now - 60, now))
        store._conn.commit()

    async def test_ok_path_stages_l2_draft_idempotent(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        self._seed_messages(store)

        async def _fake_gen(**kw):
            assert "第1步" in kw.get("agent_instruction", "")
            return {"ok": True, "reply": "自动跟进正文", "reply_lang": "zh"}

        import src.inbox.persona_reply as pr
        monkeypatch.setattr(pr, "generate_persona_reply", _fake_gen)
        import src.inbox.workflow_auto_step as mod
        events = []
        monkeypatch.setattr(
            mod, "_publish_step_event",
            lambda conv, ex, text, auto: events.append((text, auto)))
        ex = {"exec_id": "eX", "chain_id": CHAIN, "chain_name": "自动档链",
              "conversation_id": CONV, "current_step": 0}
        ok = await mod._generate_and_stage(_AppState(store), store, ex, "第一步话术")
        assert ok is True
        rows = store.list_drafts(source_kind="inbox", status="pending")
        wf = [r for r in rows if str(r.get("source_id") or "").startswith("wf:")]
        assert len(wf) == 1
        d = wf[0]
        assert d["autopilot_level"] == "L2" and d["risk_level"] == "low"
        assert d["draft_text"] == "自动跟进正文"
        assert d["source_id"] == "wf:eX:0"
        assert events and events[-1][1] is True        # auto=True 事件
        # 幂等：同 exec+step 重放 → 仍 1 条（uq_drafts_source upsert）
        ok2 = await mod._generate_and_stage(_AppState(store), store, ex, "第一步话术")
        assert ok2 is True
        rows2 = store.list_drafts(source_kind="inbox", status="pending")
        assert len([r for r in rows2
                    if str(r.get("source_id") or "").startswith("wf:")]) == 1

    async def test_generation_failure_degrades_to_remind(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        self._seed_messages(store)

        async def _fake_gen(**kw):
            return {"ok": False, "reply": ""}

        import src.inbox.persona_reply as pr
        monkeypatch.setattr(pr, "generate_persona_reply", _fake_gen)
        import src.inbox.workflow_auto_step as mod
        events = []
        monkeypatch.setattr(
            mod, "_publish_step_event",
            lambda conv, ex, text, auto: events.append((text, auto)))
        ex = {"exec_id": "eY", "chain_id": CHAIN, "chain_name": "自动档链",
              "conversation_id": CONV, "current_step": 0}
        ok = await mod._generate_and_stage(_AppState(store), store, ex, "第一步话术")
        assert ok is False
        assert not [r for r in store.list_drafts(source_kind="inbox", status="pending")
                    if str(r.get("source_id") or "").startswith("wf:")]
        assert events and events[-1] == ("第一步话术", False)   # 经典提醒降级

    async def test_no_inbound_context_degrades(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)                     # 零入站消息
        import src.inbox.workflow_auto_step as mod
        events = []
        monkeypatch.setattr(
            mod, "_publish_step_event",
            lambda conv, ex, text, auto: events.append((text, auto)))
        ex = {"exec_id": "eZ", "chain_id": CHAIN, "chain_name": "自动档链",
              "conversation_id": CONV, "current_step": 0}
        ok = await mod._generate_and_stage(_AppState(store), store, ex, "第一步话术")
        assert ok is False and events[-1][1] is False


# ── runner 集成：verdict 三态 ────────────────────────────────────────────────

class TestRunnerIntegration:
    def _start(self, store):
        return store.start_chain_execution(CHAIN, CONV, {"agent": "t"},
                                           schedule_first_step=True)

    def test_defer_reschedules_without_advancing(self, tmp_path):
        store = _mk_store(tmp_path)
        eid = self._start(store)
        until = time.time() + 7200
        runner = WorkflowRunner(
            store, auto_step_hook=lambda c, s, e: {"action": "defer", "until": until})
        runner.process_due_executions()
        ex = store.get_workflow_execution(eid)
        assert int(ex["current_step"]) == 0             # 没推进
        assert float(ex["next_step_at"]) == pytest.approx(until, abs=2)
        assert store.workflow_step_stats(time.time() - 60) == {}  # 没落账

    def test_auto_advances_with_auto_draft_detail(self, tmp_path):
        store = _mk_store(tmp_path)
        eid = self._start(store)
        calls = []

        def _hook(c, s, e):
            calls.append(str(s.get("note")))
            return {"action": "auto"}

        runner = WorkflowRunner(store, auto_step_hook=_hook)
        runner.process_due_executions()
        ex = store.get_workflow_execution(eid)
        assert int(ex["current_step"]) == 1             # 已推进到第二步
        assert calls == ["第一步话术"]
        stats = store.workflow_step_stats(time.time() - 60)
        assert stats[CHAIN]["0"]["ok"] == 1             # detail=auto_draft 落账

    def test_remind_verdict_keeps_old_behavior(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        eid = self._start(store)
        published = []
        runner = WorkflowRunner(
            store, auto_step_hook=lambda c, s, e: {"action": "remind"})
        monkeypatch.setattr(
            runner, "_publish_step_event",
            lambda conv, ex, text, at: published.append(text))
        runner.process_due_executions()
        assert published == ["第一步话术"]              # 经典提醒照发
        assert int(store.get_workflow_execution(eid)["current_step"]) == 1

    def test_remind_chain_never_consults_hook(self, tmp_path):
        store = _mk_store(tmp_path, exec_mode="remind")
        eid = self._start(store)
        calls = []
        runner = WorkflowRunner(
            store, auto_step_hook=lambda c, s, e: calls.append(1) or {"action": "auto"})
        runner.process_due_executions()
        assert calls == []                              # remind 链零咨询
        assert int(store.get_workflow_execution(eid)["current_step"]) == 1


# ── 自动开链每日预算 ────────────────────────────────────────────────────────

class TestAutoStartBudget:
    def test_budget_caps_and_persists(self, tmp_path, monkeypatch):
        store = InboxStore(tmp_path / "auto_start.db")
        store.upsert_workflow_chain({
            "chain_id": "trig_chain", "name": "沉默唤回",
            "steps": STEPS, "trigger_conditions": {"silence_days": 3},
            "enabled": True,
        })
        runner = WorkflowRunner(store)
        monkeypatch.setattr(
            runner, "_find_chain_candidates",
            lambda *a, **k: ["t:a:c1", "t:a:c2", "t:a:c3"])
        assert runner.auto_start_chains(max_per_day=2) == 2
        assert store.count_auto_started_chains_since(0) == 2
        # 预算已耗尽（DB 口径，模拟重启后仍然记得）
        assert runner.auto_start_chains(max_per_day=2) == 0
        # 不限额=旧行为（剩余那条补上）
        assert runner.auto_start_chains(max_per_day=0) == 1
