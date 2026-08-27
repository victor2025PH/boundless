# -*- coding: utf-8 -*-
"""B41「同问双答」通道互斥门禁（2026-08-22）。

事故金标（impl49 B41 _241 实录）：会话挂着进行中的「新客破冰」跟进链
（exec_mode=auto），客户来一条消息 → 常规自动回复与链话术步在 2.3 秒内各
生成一份完整回复 → 客户收到两条「意思相同、措辞不同」的消息。

修复口径（impl49 定稿）：**同一会话同一时间仅允许一条通道出稿**——
  ① 链步 hook 判定期：常规通道活跃（有非链待处置稿 / 刚有出站）→ 链步顺延；
  ② 生成完成落库前二次复查：竞态窗内常规已出稿 → 链稿让位丢弃 + 降级提醒；
  ③ worker 批内仲裁：同会话同批 wf: 稿与常规稿并存 → 链稿取消（channel_mutex）。
三层全部「链让常规」：常规回复回应真实入站，优先级天然高于按节奏的营销跟进。
"""

from __future__ import annotations

import time
import uuid

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.store import InboxStore
from src.inbox.workflow_auto_step import (
    make_auto_step_hook,
    regular_channel_active,
    resolve_auto_advance_cfg,
    stats_snapshot,
)

CHAIN = "mutex_chain"
STEP = {"action_type": "template", "note": "跟进话术", "delay_hours": 0}


def _conv() -> str:
    return f"telegram:acc1:mx_{uuid.uuid4().hex[:10]}"


class _AppState:
    def __init__(self, store):
        self.inbox_store = store


def _mk_store(tmp_path):
    s = InboxStore(tmp_path / "mutex.db")
    s.upsert_workflow_chain({
        "chain_id": CHAIN, "name": "互斥链", "steps": [STEP],
        "trigger_conditions": {}, "enabled": True, "exec_mode": "auto",
    })
    return s


def _cfg(**kw):
    aa = {"enabled": True, "quiet_start": 0, "quiet_end": 0}
    aa.update(kw)
    return {"inbox": {"workflows": {"auto_advance": aa}}}


def _seed_inbound(store, conv, text="在吗？", age_sec=60):
    now = time.time()
    store._conn.execute(
        "INSERT INTO messages (message_id, conversation_id, direction,"
        " text, ts, ingested_at) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex[:10], conv, "in", text, now - age_sec, now))
    store._conn.commit()


def _seed_outbound(store, conv, age_sec):
    now = time.time()
    store._conn.execute(
        "INSERT INTO messages (message_id, conversation_id, direction,"
        " text, ts, ingested_at) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex[:10], conv, "out", "我方回复", now - age_sec, now))
    store._conn.commit()


def _seed_regular_draft(store, conv, status="pending"):
    return store.upsert_draft({
        "source_kind": "inbox", "source_id": conv,   # 常规链的会话固定幂等键
        "conversation_id": conv, "platform": "telegram",
        "account_id": "acc1", "chat_key": conv.rsplit(":", 1)[-1],
        "draft_text": "常规自动回复正文", "autopilot_level": "L2",
        "risk_level": "low", "status": status,
    })


# ── 判据纯函数 ───────────────────────────────────────────────────────────────

class TestRegularChannelActive:
    def test_pending_regular_draft_hits(self, tmp_path):
        store = _mk_store(tmp_path)
        conv = _conv()
        _seed_regular_draft(store, conv)
        assert regular_channel_active(
            store, conv, now=time.time()) == "pending_draft"

    def test_wf_draft_does_not_hit(self, tmp_path):
        """链自己的稿不算「常规通道活跃」（否则链自锁死循环）。"""
        store = _mk_store(tmp_path)
        conv = _conv()
        store.upsert_draft({
            "source_kind": "inbox", "source_id": "wf:e1:0",
            "conversation_id": conv, "platform": "telegram",
            "account_id": "acc1", "chat_key": "k",
            "draft_text": "链稿", "autopilot_level": "L2",
            "risk_level": "low", "status": "pending",
        })
        assert regular_channel_active(store, conv, now=time.time()) == ""

    def test_recent_outbound_hits_and_window(self, tmp_path):
        store = _mk_store(tmp_path)
        conv = _conv()
        _seed_outbound(store, conv, age_sec=30)
        assert regular_channel_active(
            store, conv, now=time.time(),
            outbound_cooldown_sec=600) == "recent_outbound"
        # 窗外出站不算
        conv2 = _conv()
        _seed_outbound(store, conv2, age_sec=3600)
        assert regular_channel_active(
            store, conv2, now=time.time(), outbound_cooldown_sec=600) == ""
        # 判据可关（0=不看出站）
        assert regular_channel_active(
            store, conv, now=time.time(), outbound_cooldown_sec=0) == ""

    def test_inbound_only_quiet_conv_passes(self, tmp_path):
        store = _mk_store(tmp_path)
        conv = _conv()
        _seed_inbound(store, conv)
        assert regular_channel_active(store, conv, now=time.time()) == ""

    def test_fail_open_on_broken_store(self):
        class _Boom:
            def list_drafts(self, **kw):
                raise RuntimeError("db down")

            def list_recent_messages(self, cid, limit=5):
                raise RuntimeError("db down")

        assert regular_channel_active(_Boom(), "c", now=time.time()) == ""
        assert regular_channel_active(None, "c", now=time.time()) == ""

    def test_cfg_knob_parsed(self):
        c = resolve_auto_advance_cfg(
            {"inbox": {"workflows": {"auto_advance": {
                "enabled": True, "mutex_outbound_cooldown_sec": 120}}}})
        assert c["mutex_outbound_cooldown_sec"] == 120
        assert resolve_auto_advance_cfg({})["mutex_outbound_cooldown_sec"] == 600


# ── hook：链让常规 → defer ───────────────────────────────────────────────────

class TestHookMutex:
    def test_pending_regular_draft_defers_step(self, tmp_path):
        store = _mk_store(tmp_path)
        conv = _conv()
        store.set_automation_mode(conv, "auto_ai", source="test")
        _seed_regular_draft(store, conv)
        before = stats_snapshot().get("deferred_mutex", 0)
        hook = make_auto_step_hook(_AppState(store), _cfg())
        v = hook(conv, STEP, {"exec_id": "e1", "chain_id": CHAIN,
                              "current_step": 0})
        assert v["action"] == "defer" and v["until"] > time.time()
        assert stats_snapshot()["deferred_mutex"] == before + 1

    def test_recent_outbound_defers_step(self, tmp_path):
        store = _mk_store(tmp_path)
        conv = _conv()
        store.set_automation_mode(conv, "auto_ai", source="test")
        _seed_outbound(store, conv, age_sec=30)
        hook = make_auto_step_hook(_AppState(store), _cfg())
        v = hook(conv, STEP, {"exec_id": "e1", "chain_id": CHAIN,
                              "current_step": 0})
        assert v["action"] == "defer"

    @pytest.mark.asyncio
    async def test_quiet_channel_proceeds_auto(self, tmp_path, monkeypatch):
        """常规通道安静（只有旧入站）→ 链步照常走 auto（互斥不误伤）。"""
        store = _mk_store(tmp_path)
        conv = _conv()
        store.set_automation_mode(conv, "auto_ai", source="test")
        _seed_inbound(store, conv)

        async def _fake_stage(app_state, st, ex, note):
            return True

        import src.inbox.workflow_auto_step as mod
        monkeypatch.setattr(mod, "_generate_and_stage", _fake_stage)
        hook = make_auto_step_hook(_AppState(store), _cfg())
        v = hook(conv, STEP, {"exec_id": "e1", "chain_id": CHAIN,
                              "current_step": 0})
        assert v["action"] == "auto"
        import asyncio as _aio
        await _aio.sleep(0)                             # 让已调度任务收尾


# ── 生成后二次复查：竞态窗让位 ───────────────────────────────────────────────

class TestPostGenerationRecheck:
    @pytest.mark.asyncio
    async def test_regular_draft_arriving_mid_generation_drops_stage(
            self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        conv = _conv()
        _seed_inbound(store, conv)

        async def _fake_gen(**kw):
            # 生成期间常规通道对同一入站出稿（B41 竞态窗）
            _seed_regular_draft(store, conv)
            return {"ok": True, "reply": "链稿正文", "reply_lang": "zh"}

        import src.inbox.persona_reply as pr
        monkeypatch.setattr(pr, "generate_persona_reply", _fake_gen)
        import src.inbox.workflow_auto_step as mod
        events = []
        monkeypatch.setattr(
            mod, "_publish_step_event",
            lambda conv_id, ex, text, auto: events.append((text, auto)))
        before = stats_snapshot().get("dropped_mutex", 0)
        ex = {"exec_id": "eM", "chain_id": CHAIN, "chain_name": "互斥链",
              "conversation_id": conv, "current_step": 0}
        ok = await mod._generate_and_stage(_AppState(store), store, ex, "跟进话术")
        assert ok is False
        assert stats_snapshot()["dropped_mutex"] == before + 1
        # 链稿没落库（wf: 前缀零行）
        assert not [r for r in store.list_drafts(
            source_kind="inbox", status="pending")
            if str(r.get("source_id") or "").startswith("wf:")]
        # 降级经典提醒（拍不黑洞）
        assert events and events[-1] == ("跟进话术", False)


# ── worker 批内仲裁：链稿让位取消 ────────────────────────────────────────────

class TestWorkerBatchArbiter:
    class _Svc:
        def __init__(self, store):
            self.queue = []
            self._store = store

        def list_drafts(self, status="pending", limit=200):
            batch, self.queue = self.queue, []
            return batch

        def resolve_with_audit(self, draft_id, action, by=""):
            return {"ok": True}

    def _item(self, conv, text, draft_id, source_id):
        return {
            "draft_id": draft_id, "autopilot_level": "L2",
            "final_text": text, "platform": "telegram",
            "account_id": "a1", "chat_key": "c1",
            "conversation_id": conv, "source_id": source_id,
        }

    @pytest.mark.asyncio
    async def test_wf_draft_cancelled_when_regular_present(self, tmp_path):
        store = _mk_store(tmp_path)
        conv = _conv()
        svc = self._Svc(store)
        sent = []

        async def _cb(platform, account_id, chat_key, text):
            sent.append(text)
            return {"ok": True}

        async def _sleep(d):
            return None

        # 真实落库两条稿（仲裁要写 cancelled 状态）
        wf_id = store.upsert_draft({
            "source_kind": "inbox", "source_id": "wf:eA:0",
            "conversation_id": conv, "platform": "telegram",
            "account_id": "a1", "chat_key": "c1",
            "draft_text": "链跟进正文", "autopilot_level": "L2",
            "risk_level": "low", "status": "pending",
        })
        reg_id = _seed_regular_draft(store, conv)
        w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
        svc.queue = [
            self._item(conv, "链跟进正文", wf_id, "wf:eA:0"),
            self._item(conv, "常规自动回复正文", reg_id, conv),
        ]
        await w._tick()
        assert sent == ["常规自动回复正文"]           # 只有常规稿出门
        assert w.total_skipped_mutex == 1
        assert w.status_snapshot()["total_skipped_mutex"] == 1
        assert store.get_draft(wf_id)["status"] == "cancelled"
        assert store.get_draft(wf_id)["decided_by"] == "channel_mutex"

    @pytest.mark.asyncio
    async def test_wf_only_batch_not_cancelled(self, tmp_path):
        """批内只有链稿（无常规稿竞争）→ 照常投递，不误伤。"""
        store = _mk_store(tmp_path)
        conv = _conv()
        svc = self._Svc(store)
        sent = []

        async def _cb(platform, account_id, chat_key, text):
            sent.append(text)
            return {"ok": True}

        async def _sleep(d):
            return None

        w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
        svc.queue = [self._item(conv, "链跟进正文", "dwf", "wf:eB:0")]
        await w._tick()
        assert sent == ["链跟进正文"] and w.total_skipped_mutex == 0

    @pytest.mark.asyncio
    async def test_cross_conversation_no_mutex(self, tmp_path):
        """不同会话的 wf 稿与常规稿互不相干。"""
        store = _mk_store(tmp_path)
        svc = self._Svc(store)
        sent = []

        async def _cb(platform, account_id, chat_key, text):
            sent.append(text)
            return {"ok": True}

        async def _sleep(d):
            return None

        w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
        svc.queue = [
            self._item(_conv(), "链跟进正文", "dwf2", "wf:eC:0"),
            self._item(_conv(), "常规正文", "dreg2", "convX"),
        ]
        await w._tick()
        assert len(sent) == 2 and w.total_skipped_mutex == 0
