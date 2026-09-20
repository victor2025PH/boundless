# -*- coding: utf-8 -*-
"""B41「同稿双投」投递幂等钉门禁（2026-08-22）。

事故金标（impl49 B41，2026-08-21 内测实录）：全自动打通当晚，同一组回复字节级
相同 ×2 发给真实客户 Nicks。根因面＝inbox 草稿按会话固定 source_id upsert，
第二次生成把已投递的行复活（status→pending）后 worker 再投一遍。

锁定不变量：
  - 同 (会话,草稿) 同文第二次投递被拒（复活行 revive 场景），不算投递错误；
  - 不同文本的复活行放行（客户又说话、真的重新生成＝合法新回复）；
  - 人工通过链与自动链共用同一登记表（人工×自动竞态同稿只出门一次）；
  - 重试项（_attempt>0）豁免——上一轮失败未登记指纹，重发是 recoverable 语义；
  - 投递失败释放在途位（重试可再 claim），只有成功才登记指纹；
  - run() 双循环护栏：同一 draft_service 第二个自动循环拒绝启动；
  - mark_draft_sent 的 CAS 语义：只从 0 写一次，成功投递后 DB 有跨重启证据。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.deliver_once import DeliverOnceRegistry, text_fp

TEXT = "Ah 3-2... he's closing the gap, I'd say that justifies a celebration!"
TEXT2 = "明天早上九点方便给你打电话吗？"


def _conv() -> str:
    return f"test-b41-{uuid.uuid4().hex[:12]}"


class _FakeSvc:
    def __init__(self):
        self.queue = []
        self._store = None

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        return {"ok": True}


def _draft(conv, text, draft_id="d1"):
    return {
        "draft_id": draft_id, "autopilot_level": "L2",
        "final_text": text, "platform": "telegram",
        "account_id": "a1", "chat_key": "c1", "conversation_id": conv,
    }


def _worker(svc, sent, *, fail_first=0):
    calls = {"n": 0}

    async def _send_cb(platform, account_id, chat_key, text):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            raise RuntimeError("transient boom")
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    return AutosendWorker(
        draft_service=svc, send_callback=_send_cb, sleep=_sleep,
        config={"recoverable": {"enabled": True, "max_attempts": 3,
                                "backoff_base_sec": 0, "backoff_max_sec": 0}},
    )


# ── 登记表纯语义 ─────────────────────────────────────────────────────────────

class TestRegistry:
    def test_claim_release_lifecycle(self):
        reg = DeliverOnceRegistry()
        assert reg.claim("c", "d", "hello") == ""
        # 在途独占：同稿并发第二方被拒
        assert reg.claim("c", "d", "hello") == "in_flight"
        reg.release("c", "d", delivered=True, text="hello")
        # 已投同文：拒
        assert reg.claim("c", "d", "hello") == "dup_text"
        # 已投不同文：放行（复活行合法新内容）
        assert reg.claim("c", "d", "another reply") == ""

    def test_failure_release_allows_retry(self):
        reg = DeliverOnceRegistry()
        assert reg.claim("c", "d", "hello") == ""
        reg.release("c", "d", delivered=False)
        assert reg.claim("c", "d", "hello") == ""      # 失败后重试可再拿

    def test_ttl_expiry_reopens(self):
        reg = DeliverOnceRegistry()
        assert reg.claim("c", "d", "hi", now=1000.0) == ""
        reg.release("c", "d", delivered=True, text="hi", now=1000.0)
        assert reg.claim("c", "d", "hi", now=1000.0 + 3600) == "dup_text"
        assert reg.claim("c", "d", "hi", now=1000.0 + 25 * 3600) == ""

    def test_empty_draft_id_never_blocks(self):
        reg = DeliverOnceRegistry()
        assert reg.claim("c", "", "hi") == ""
        assert reg.claim("c", "", "hi") == ""          # 不登记不闸

    def test_key_scoped_by_conversation(self):
        reg = DeliverOnceRegistry()
        reg.claim("c1", "d1", "hi")
        reg.release("c1", "d1", delivered=True, text="hi")
        assert reg.claim("c2", "d1", "hi") == ""       # 跨会话同名 draft 不误伤

    def test_text_fp_whitespace_insensitive(self):
        assert text_fp("a  b\nc") == text_fp("a b c")
        assert text_fp("a b") != text_fp("a c")


# ── worker 集成：事故金标 ────────────────────────────────────────────────────

class TestWorkerIntegration:
    @pytest.mark.asyncio
    async def test_revived_same_text_blocked(self):
        """B41 金标：同 draft 同文两轮投递，第二轮被幂等钉拒（复活行场景）。"""
        conv = _conv()
        svc = _FakeSvc()
        sent = []
        w = _worker(svc, sent)
        svc.queue = [_draft(conv, TEXT, "d1")]
        await w._tick()
        assert sent == [TEXT]
        # 复活：同 draft_id 同文再次 pending（upsert 固定 source_id 语义）
        svc.queue = [_draft(conv, TEXT, "d1")]
        await w._tick()
        assert sent == [TEXT]                          # 没有第二发
        assert w.total_skipped_already_sent == 1
        assert w.total_deliver_errors == 0             # 拦截不算错误
        snap = w.status_snapshot()
        assert snap["total_skipped_already_sent"] == 1
        assert snap["deliver_once"]["blocked_dup_text"] == 1

    @pytest.mark.asyncio
    async def test_revived_new_text_passes(self):
        """复活行带**新内容**＝合法新回复，必须放行。"""
        conv = _conv()
        svc = _FakeSvc()
        sent = []
        w = _worker(svc, sent)
        svc.queue = [_draft(conv, TEXT, "d1")]
        await w._tick()
        svc.queue = [_draft(conv, TEXT2, "d1")]
        await w._tick()
        assert sent == [TEXT, TEXT2]
        assert w.total_skipped_already_sent == 0

    @pytest.mark.asyncio
    async def test_transient_failure_then_retry_delivers_once(self):
        """瞬时失败 → 释放在途位 → 重试队列重发成功（豁免不误伤自愈）。"""
        conv = _conv()
        svc = _FakeSvc()
        sent = []
        w = _worker(svc, sent, fail_first=1)
        svc.queue = [_draft(conv, TEXT, "d1")]
        await w._tick()                                # 首投失败进重试队列
        assert sent == [] and w.total_retry_scheduled == 1
        await w._tick()                                # 重试成功
        assert sent == [TEXT]
        assert w.total_skipped_already_sent == 0

    @pytest.mark.asyncio
    async def test_human_and_auto_share_registry(self):
        """人工通过与自动链竞态：同稿只出门一次（后到方拿 deliver_once 拒因）。"""
        conv = _conv()
        svc = _FakeSvc()
        sent = []
        w = _worker(svc, sent)
        svc.queue = [_draft(conv, TEXT, "d1")]
        await w._tick()                                # 自动链先投成功
        res = await w.deliver_human_approved({
            "draft_id": "d1", "conversation_id": conv,
            "platform": "telegram", "account_id": "a1", "chat_key": "c1",
            "final_text": TEXT,
        })
        assert res.get("ok") is False
        assert str(res.get("error") or "").startswith("deliver_once:")
        assert sent == [TEXT]                          # 仍只有一发
        assert w.total_human_deliver_errors == 0       # 保护性跳过不算人工投递失败


# ── run() 双循环护栏 ────────────────────────────────────────────────────────

class TestDoubleRunGuard:
    @pytest.mark.asyncio
    async def test_second_loop_refused(self):
        svc = _FakeSvc()

        async def _sleep(d):
            await asyncio.sleep(0)

        w1 = AutosendWorker(draft_service=svc, sleep=_sleep,
                            config={"startup_delay_sec": 0,
                                    "min_interval_sec": 5})
        w2 = AutosendWorker(draft_service=svc, sleep=_sleep,
                            config={"startup_delay_sec": 0,
                                    "min_interval_sec": 5})
        t1 = asyncio.create_task(w1.run())
        await asyncio.sleep(0.05)                      # 让 w1 进入循环
        assert w1._running is True
        t2 = asyncio.create_task(w2.run())
        await asyncio.sleep(0.05)
        assert w2._running is False                    # 第二循环被拒
        w1.stop()
        w1.notify_new_l2()                             # 提前唤醒退出等待
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=10)
        # w1 退出后重新启动不受影响（键已释放）
        t3 = asyncio.create_task(w1.run())
        await asyncio.sleep(0.05)
        assert w1._running is True
        w1.stop()
        w1.notify_new_l2()
        await asyncio.wait_for(t3, timeout=10)

    def test_deliver_only_never_registers(self):
        """deliver_only 实例 enabled=false → run() 早退，不占用循环位。"""
        svc = _FakeSvc()
        w = AutosendWorker(draft_service=svc, config={"enabled": False},
                           deliver_only=True)
        asyncio.run(w.run())
        assert w._running is False


# ── mark_draft_sent DB CAS ──────────────────────────────────────────────────

class TestMarkDraftSent:
    def test_cas_only_once_and_worker_marks(self, tmp_path):
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "b41.db")
        conv = _conv()
        did = store.upsert_draft({
            "source_kind": "inbox", "source_id": conv,
            "conversation_id": conv, "platform": "telegram",
            "account_id": "a1", "chat_key": "c1",
            "draft_text": TEXT, "autopilot_level": "L2",
            "risk_level": "low", "status": "pending",
        })
        assert store.mark_draft_sent(did) is True
        d = store.get_draft(did)
        assert float(d.get("sent_at") or 0) > 0
        assert store.mark_draft_sent(did) is False     # 二次写被 CAS 拒
        assert store.mark_draft_sent("") is False

    @pytest.mark.asyncio
    async def test_worker_writes_sent_at_on_success(self, tmp_path):
        from src.inbox.store import InboxStore
        store = InboxStore(tmp_path / "b41w.db")
        conv = _conv()
        did = store.upsert_draft({
            "source_kind": "inbox", "source_id": conv,
            "conversation_id": conv, "platform": "telegram",
            "account_id": "a1", "chat_key": "c1",
            "draft_text": TEXT, "autopilot_level": "L2",
            "risk_level": "low", "status": "pending",
        })
        svc = _FakeSvc()
        svc._store = store
        sent = []
        w = _worker(svc, sent)
        svc.queue = [dict(_draft(conv, TEXT, did))]
        await w._tick()
        assert sent == [TEXT]
        assert float(store.get_draft(did).get("sent_at") or 0) > 0


# ── 热接线入口（P1 一键全自动的地基）──────────────────────────────────────

class TestApplySendCallbacks:
    @pytest.mark.asyncio
    async def test_rewire_arms_and_disarms_auto_chain(self):
        conv = _conv()
        svc = _FakeSvc()
        sent = []

        async def _cb(platform, account_id, chat_key, text):
            sent.append(text)
            return {"ok": True}

        async def _sleep(d):
            return None

        w = AutosendWorker(draft_service=svc, send_callback=None, sleep=_sleep)
        assert w.status_snapshot()["deliver_enabled"] is False
        w.apply_send_callbacks(send_callback=_cb)
        assert w.status_snapshot()["deliver_enabled"] is True
        svc.queue = [_draft(conv, TEXT, "d1")]
        await w._tick()
        assert sent == [TEXT]                          # 热接线后真投递
        w.apply_send_callbacks(send_callback=None)     # 撤能力（deliver 关）
        assert w.status_snapshot()["deliver_enabled"] is False
        svc.queue = [_draft(conv, TEXT2, "d2")]
        await w._tick()
        assert sent == [TEXT]                          # 不再投递（仅标记）

    @pytest.mark.asyncio
    async def test_ensure_auto_loop_upgrades_deliver_only(self):
        svc = _FakeSvc()

        async def _sleep(d):
            await asyncio.sleep(0)

        w = AutosendWorker(draft_service=svc, config={
            "enabled": False, "startup_delay_sec": 0, "min_interval_sec": 5,
        }, deliver_only=True, sleep=_sleep)
        assert w.ensure_auto_loop() is True
        await asyncio.sleep(0.05)
        assert w._running is True and w._deliver_only is False
        assert w.ensure_auto_loop() is False           # 已在跑=幂等
        w.stop()
        w.notify_new_l2()
        await asyncio.sleep(0.05)
