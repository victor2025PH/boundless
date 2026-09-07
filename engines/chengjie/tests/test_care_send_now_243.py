"""N-1（#243，并 #244–#249，D-N4，2026-09-08）：主动关怀「立即发」真投递门禁。

实录事故（skuio 1.0.76，FHSZTA）：15:28:22 原文直发入队，15:28:31/50/58 点「立即发」三次
→ 后端只记 bring_forward、循环 15:29–16:29 一拍没捡；16:34:52 到期首拍捡到却只入 deferred
（send_in_min=13），同秒队列才懒初始化且**没有 drain loop** → 16:47:52 零出队；界面全程
「到点发」。三层各扣一段，85 分钟零动作零反馈。

覆盖：
- A 立即发＝同步直投：verbatim 行 send_now → 同一 send_callback 入队 + deliver_now 当场投
  → decision=sent + sent_at + care 行 sent（note=deferred:<row>）+ sent_hook；不吃
  enabled 灰度门；零错峰（defer_until==now）；三连点只发一条；
  暂态（sender 未就绪）→ decision=queued + retry_at，care 行 sent（队列稍后补投）；
  永久失败 → decision=failed + 原因，care 行**留 pending** note=fail:<原因>，再点可重试；
  dry_run → decision=dry_sampled 不真发、行留 pending；held 行 → decision=held 不发；
  守卫跳过（peer bot）→ decision=skipped + 原因；
- 路由 /send-now：not_pending / held / sent / queued / failed 全部结构化回话；派发器缺席
  退回 B 兜底（bring_forward + decision=brought_forward）；
- DeferredDispatcher.deliver_now：sent / sender_not_ready 推后 / 拒收 failed / 急停推后 /
  无 sender 推后；care 行 [care-deferred] 日志每条出队/推后/失败/送达都有；
- 静态钉：background_tasks 注入 deliver_now + 登记主 loop；模板按钮带 data-sendnow 防重复。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.contacts.care_dispatcher import CareDispatcher
from src.contacts.care_schedule import CareScheduleStore
from src.integrations.shared.deferred_outbox import (
    DeferredDispatcher,
    DeferredOutboxStore,
    DeferredSenderNotReady,
)

NOW = datetime(2026, 9, 7, 15, 28, 31).timestamp()
TEXT = "what are you doing now kim ?"
CONTACT = "telegram:7092595256:6088992099"
_REPO = __import__("pathlib").Path(__file__).resolve().parents[1]


class _AI:
    def __init__(self, reply="How was the interview?"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _verbatim_store(due_offset=3600.0):
    """复刻事故：一条原文直发，到期在一小时后（用户点「立即发」时远未到期）。"""
    s = CareScheduleStore(":memory:")
    rid = s.add_verbatim(contact_key=CONTACT, due_at=NOW + due_offset, text=TEXT,
                         platform="telegram", account_id="7092595256", chat_key="6088992099")
    return s, rid


class _Queue:
    """真 deferred store + dispatcher，sender 可编程（送达 / 未就绪 / 拒收 / 抛错）。"""

    def __init__(self, mode="ok"):
        self.store = DeferredOutboxStore(":memory:")
        self.mode = mode
        self.sent = []
        self.disp = DeferredDispatcher(
            store=self.store, kill_switch_check=lambda p, a: (False, "", ""),
            quiet_start_hour=0, quiet_end_hour=0, min_gap_sec=0)

        async def _sender(account_id, chat_key, text):
            if self.mode == "not_ready":
                raise DeferredSenderNotReady("no worker for telegram:7092595256")
            if self.mode == "reject":
                return False
            if self.mode == "boom":
                raise RuntimeError("socket closed")
            self.sent.append((account_id, chat_key, text))
            return True
        self.disp.register_sender("telegram", _sender)

    def send_callback(self):
        async def _send(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra):
            return self.store.enqueue(
                platform=channel, account_id=account_id, chat_key=chat_name,
                reply_text=reply, defer_until=defer_until, reason=reason,
                staleness_sec=staleness, extra=extra, now=NOW)
        return _send

    def deliver_now(self):
        async def _dn(row_id, platform):
            return await self.disp.deliver_now(int(row_id), now=NOW)
        return _dn


def _disp(store, q: _Queue, *, live=None, hooks=None, **kw):
    hooks = hooks if hooks is not None else []
    kw.setdefault("quiet_start_hour", 0)
    kw.setdefault("quiet_end_hour", 0)
    return CareDispatcher(
        store=store, ai_client=_AI(), send_callback=q.send_callback(),
        deliver_now=q.deliver_now(), sent_hook=lambda it: hooks.append(int(it["id"])),
        cfg_provider=(lambda: dict(live)) if live is not None else None, **kw)


# ── A ① 同步直投：发了、说了、只发一条 ──────────────────────────────────────
async def test_send_now_delivers_synchronously_and_reports_sent_at():
    s, rid = _verbatim_store()
    q = _Queue("ok")
    hooks = []
    d = _disp(s, q, live={"enabled": True, "dry_run": False}, hooks=hooks)
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["ok"] is True and res["decision"] == "sent"
    assert res["sent_at"] == NOW and res["text"] == TEXT and res["row_id"] == 1
    assert q.sent == [("7092595256", "6088992099", TEXT)]        # 客户端真收到
    it = s.get(rid)
    assert it["status"] == "sent" and it["note"] == "deferred:1" and it["sent_text"] == TEXT
    assert q.store.get(1)["status"] == "sent"
    assert hooks == [rid]                                        # 触达落账


async def test_send_now_zero_jitter_and_bypasses_enabled_gate():
    """15:27 之前 enabled=False 的机器上，运营亲手点「立即发」也得发；且 defer_until==now（零错峰）。"""
    s, rid = _verbatim_store()
    q = _Queue("ok")
    d = _disp(s, q, live={"enabled": False, "dry_run": False}, send_jitter_sec=(600.0, 1200.0),
              quiet_start_hour=0.0, quiet_end_hour=24.0)   # 全天安静窗也不顺延
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["decision"] == "sent" and res["defer_until"] == NOW
    assert q.store.get(1)["defer_until"] == NOW
    # 到期循环自身仍受灰度门：同一配置下 run_once 不派 care 行
    s2, _ = _verbatim_store(due_offset=-60)
    d2 = _disp(s2, _Queue("ok"), live={"enabled": False, "dry_run": False})
    assert await d2.run_once(now=NOW) == 0


async def test_three_clicks_send_exactly_once():
    s, rid = _verbatim_store()
    q = _Queue("ok")
    d = _disp(s, q, live={"enabled": True, "dry_run": False})
    r1 = await d.send_now(s.get(rid), now=NOW)
    r2 = await d.send_now(s.get(rid), now=NOW + 19)
    r3 = await d.send_now(s.get(rid), now=NOW + 27)
    assert r1["decision"] == "sent"
    assert r2["decision"] == r3["decision"] == "not_pending"
    assert len(q.sent) == 1 and q.store.count() == 1


# ── A ② 暂态 = 排队中；永久失败 = 留 pending + 原因，可再点 ───────────────────
async def test_send_now_transient_failure_is_queued_with_retry_eta():
    s, rid = _verbatim_store()
    q = _Queue("not_ready")
    d = _disp(s, q, live={"enabled": True, "dry_run": False})
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["ok"] is True and res["decision"] == "queued"
    assert res["reason"].startswith("sender_not_ready")
    assert res["defer_until"] > NOW                       # 预计重试时刻
    row = q.store.get(1)
    assert row["status"] == "pending" and row["reason"] == "sender_not_ready"
    assert s.get(rid)["status"] == "sent" and s.get(rid)["note"] == "deferred:1"   # 队列稍后补投
    # 通道恢复后 drain 补投
    q.mode = "ok"
    assert await q.disp.run_once(now=row["defer_until"] + 1) == 1
    assert q.sent and q.store.get(1)["status"] == "sent"


async def test_send_now_permanent_failure_keeps_row_pending_with_reason():
    s, rid = _verbatim_store()
    q = _Queue("reject")
    hooks = []
    d = _disp(s, q, live={"enabled": True, "dry_run": False}, hooks=hooks)
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["ok"] is False and res["decision"] == "failed"
    assert res["reason"] == "sender_returned_false"
    it = s.get(rid)
    assert it["status"] == "pending" and it["note"] == "fail:sender_returned_false"
    assert CareScheduleStore.fail_reason(it) == "sender_returned_false"
    assert hooks == []                                    # 没发出去不落触达账本
    assert q.store.get(1)["status"] == "failed"
    # 通道修好后再点一次 → 发出，note 被 deferred:<新行> 覆盖
    q.mode = "ok"
    res2 = await d.send_now(s.get(rid), now=NOW + 60)
    assert res2["decision"] == "sent" and s.get(rid)["status"] == "sent"
    assert s.get(rid)["note"] == "deferred:2" and CareScheduleStore.fail_reason(s.get(rid)) == ""


async def test_send_now_sender_exception_is_failed_not_silent():
    s, rid = _verbatim_store()
    q = _Queue("boom")
    d = _disp(s, q, live={"enabled": True, "dry_run": False})
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["decision"] == "failed" and res["reason"].startswith("RuntimeError:socket closed")
    assert s.get(rid)["status"] == "pending"
    assert s.get(rid)["note"].startswith("fail:RuntimeError")


async def test_send_now_queue_unavailable_reports_reason():
    """multiplatform_deferred 关（send_callback 返 0）→ 不再只写「gated」，卡片能看到原因。"""
    s, rid = _verbatim_store()

    async def _gated(*a):
        return 0
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_gated,
                       cfg_provider=lambda: {"enabled": True, "dry_run": False})
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["decision"] == "failed" and res["reason"] == "queue_unavailable"
    assert s.get(rid)["note"] == "fail:queue_unavailable"


async def test_send_now_without_sync_hook_reports_queued():
    """旧接线 / messenger：只入队不当场投 → 如实「排队中（预计 defer_until）」，不装已发。"""
    s, rid = _verbatim_store()
    q = _Queue("ok")
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=q.send_callback(),
                       cfg_provider=lambda: {"enabled": True, "dry_run": False})
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["decision"] == "queued" and res["reason"] == "no_sync_path"
    assert res["defer_until"] == NOW and q.sent == []
    assert s.get(rid)["status"] == "sent"


# ── A ③ dry / held / skipped 三态如实 ────────────────────────────────────────
async def test_send_now_in_dry_run_samples_but_never_sends():
    s, rid = _verbatim_store()
    q = _Queue("ok")
    d = _disp(s, q, live={"enabled": True, "dry_run": True})
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["ok"] is True and res["decision"] == "dry_sampled" and res["dry_run"] is True
    assert q.sent == [] and q.store.count() == 0
    it = s.get(rid)
    assert it["status"] == "pending" and it["dry_sampled_at"] == NOW
    # 再点一次不吃重拟冷却（运营要看现在这稿）
    res2 = await d.send_now(s.get(rid), now=NOW + 5)
    assert res2["decision"] == "dry_sampled"


async def test_send_now_held_row_is_not_sent():
    s, rid = _verbatim_store()
    s.mark_hold_for_preview(rid, sent_text=TEXT, reason="first_send")
    q = _Queue("ok")
    d = _disp(s, q, live={"enabled": True, "dry_run": False})
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["ok"] is False and res["decision"] == "held" and res["held"] == "first_send"
    assert q.sent == [] and s.get(rid)["status"] == "pending"


async def test_send_now_guard_skip_reports_reason():
    s, rid = _verbatim_store()
    q = _Queue("ok")
    d = _disp(s, q, live={"enabled": True, "dry_run": False}, peer_filter=lambda p, a, c: True)
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["decision"] == "skipped" and res["reason"] == "peer_bot_or_fleet"
    assert s.get(rid)["status"] == "skipped" and q.sent == []


async def test_send_now_never_raises_on_broken_hook():
    s, rid = _verbatim_store()
    q = _Queue("ok")

    async def _broken(row_id, platform):
        raise RuntimeError("hook exploded")
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=q.send_callback(),
                       deliver_now=_broken, cfg_provider=lambda: {"enabled": True, "dry_run": False})
    res = await d.send_now(s.get(rid), now=NOW)
    assert res["decision"] == "queued" and res["reason"] == "deliver_now_error"
    assert s.get(rid)["status"] == "sent"                # 行已入队，队列会补投


# ── deliver_now（队列侧）────────────────────────────────────────────────────
def _care_row(q: _Queue, **extra):
    return q.store.enqueue(platform="telegram", account_id="7092595256", chat_key="6088992099",
                           reply_text=TEXT, defer_until=NOW, reason="care:verbatim",
                           extra={"care": True, "care_id": 1, **extra}, now=NOW)


async def test_deliver_now_sends_and_logs_care_deferred(caplog):
    q = _Queue("ok")
    rid = _care_row(q)
    with caplog.at_level(logging.INFO, logger="src.integrations.shared.deferred_outbox"):
        res = await q.disp.deliver_now(rid, now=NOW)
    assert res == {"delivered": True, "status": "sent", "sent_at": NOW}
    lines = [r.getMessage() for r in caplog.records if "[care-deferred]" in r.getMessage()]
    assert any("deliver_now id=1" in ln and "care_id=1" in ln for ln in lines)
    assert any("sent id=1" in ln for ln in lines)
    # 已 sent 的行再投 → not_pending，不重复发
    res2 = await q.disp.deliver_now(rid, now=NOW)
    assert res2["delivered"] is False and res2["reason"] == "not_pending"
    assert len(q.sent) == 1


async def test_deliver_now_bypasses_quiet_and_pacing_but_not_kill_switch():
    q = _Queue("ok")
    q.disp._quiet_start, q.disp._quiet_end = 0.0, 24.0        # 全天安静窗
    q.disp._min_gap = 45.0
    q.disp._last_sent[("telegram", "7092595256")] = NOW - 1   # 刚发过（pacing 会推后）
    rid = _care_row(q)
    res = await q.disp.deliver_now(rid, now=NOW)
    assert res["delivered"] is True                           # 人工立即发：不顺延不 pacing
    blocked = DeferredDispatcher(store=q.store, kill_switch_check=lambda p, a: (True, "account", "ops"))
    blocked.register_sender("telegram", q.disp._senders["telegram"])
    rid2 = _care_row(q)
    res2 = await blocked.deliver_now(rid2, now=NOW)
    assert res2["delivered"] is False and res2["status"] == "pending"
    assert res2["reason"] == "kill_switch:account" and res2["retry_at"] > NOW
    assert q.store.get(rid2)["reason"] == "kill_switch:account"


async def test_deliver_now_transient_and_permanent_outcomes(caplog):
    q = _Queue("not_ready")
    rid = _care_row(q)
    with caplog.at_level(logging.INFO, logger="src.integrations.shared.deferred_outbox"):
        res = await q.disp.deliver_now(rid, now=NOW)
    assert res["status"] == "pending" and res["reason"].startswith("sender_not_ready:no worker")
    assert q.store.get(rid)["status"] == "pending" and q.store.get(rid)["defer_until"] > NOW
    assert any("pushed id=%d" % rid in r.getMessage() and "sender_not_ready" in r.getMessage()
               for r in caplog.records)
    q.mode = "reject"
    rid2 = _care_row(q)
    res2 = await q.disp.deliver_now(rid2, now=NOW)
    assert res2 == {"delivered": False, "status": "failed", "reason": "sender_returned_false"}
    assert q.store.get(rid2)["status"] == "failed"
    no_sender = DeferredDispatcher(store=q.store, kill_switch_check=lambda p, a: (False, "", ""))
    rid3 = _care_row(q)
    res3 = await no_sender.deliver_now(rid3, now=NOW)
    assert res3["reason"] == "no_sender" and q.store.get(rid3)["reason"] == "no_sender"
    assert (await no_sender.deliver_now(9999, now=NOW))["reason"] == "row_missing"


async def test_drain_logs_every_care_row_outcome(caplog):
    """D：出队 / 推后 / 失败 / 送达每条 [care-deferred] INFO；非 care 行不刷屏（DEBUG）。"""
    q = _Queue("ok")
    _care_row(q)
    q.store.enqueue(platform="telegram", account_id="7092595256", chat_key="x",
                    reply_text="hi", defer_until=NOW, reason="reactivation:x", now=NOW)
    with caplog.at_level(logging.INFO, logger="src.integrations.shared.deferred_outbox"):
        n = await q.disp.run_once(now=NOW + 1)
    assert n == 2
    care_lines = [r.getMessage() for r in caplog.records if "[care-deferred]" in r.getMessage()]
    assert any(ln.startswith("[care-deferred] dequeued id=1") and "late=1s" in ln for ln in care_lines)
    assert any(ln.startswith("[care-deferred] sent id=1") for ln in care_lines)
    assert not any("chat=x" in ln for ln in care_lines)


# ── 路由 ────────────────────────────────────────────────────────────────────
class _CM:
    def __init__(self, dry=False):
        self.config = {"companion": {"proactive_care": {"enabled": True, "dry_run": dry}}}
        self.config_path = ""

    def get_ai_config(self):
        return {"ai_name": "小雅"}


def _client(mode="ok", *, dry=False, with_dispatcher=True):
    from src.web.routes.care_routes import register_care_routes

    app = FastAPI()
    store = CareScheduleStore(":memory:")
    q = _Queue(mode)
    app.state.care_schedule_store = store
    app.state.ai_client = _AI()
    app.state.deferred_outbox_store = q.store
    disp = _disp(store, q, live={"enabled": True, "dry_run": dry}) if with_dispatcher else None
    app.state.care_engine = {"dispatcher": disp} if disp is not None else {}
    app.state.config_manager = _CM(dry)

    def _auth(request: Request):
        return True

    register_care_routes(app, api_auth=_auth, config_manager=app.state.config_manager)
    return TestClient(app), store, q


def test_route_send_now_sent_then_not_pending():
    c, store, q = _client("ok")
    rid = store.add_verbatim(contact_key=CONTACT, due_at=NOW + 3600, text=TEXT,
                             platform="telegram", account_id="7092595256", chat_key="6088992099")
    r = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r["ok"] is True and r["decision"] == "sent" and r["sent_at"] > 0
    assert r["id"] == rid and r["text"] == TEXT and r["row_id"] == 1
    assert q.sent and store.get(rid)["status"] == "sent"
    r2 = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r2["ok"] is False and r2["decision"] == "not_pending"
    assert len(q.sent) == 1
    # 历史 / 方案接口 join 出投递真相
    hist = c.get("/api/care/schedule?status=sent").json()["items"]
    assert hist[0]["delivery"]["status"] == "sent" and hist[0]["delivery"]["sent_at"] > 0


def test_route_send_now_queued_and_failed_are_structured():
    c, store, q = _client("not_ready")
    rid = store.add_verbatim(contact_key=CONTACT, due_at=NOW + 3600, text=TEXT,
                             platform="telegram", account_id="7092595256", chat_key="6088992099")
    r = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r["ok"] is True and r["decision"] == "queued"
    assert r["reason"].startswith("sender_not_ready") and r["defer_until"] > 0
    plan = c.get("/api/care/plan").json()
    rec = [x for x in plan["recent"]["items"] if x.get("kind") == "sent"]
    assert rec and rec[0]["delivery"]["status"] == "pending"
    assert rec[0]["delivery"]["defer_until"] > 0 and rec[0]["delivery"]["reason"] == "sender_not_ready"

    c2, store2, _ = _client("reject")
    rid2 = store2.add_verbatim(contact_key=CONTACT, due_at=NOW + 3600, text=TEXT,
                               platform="telegram", account_id="7092595256", chat_key="6088992099")
    r2 = c2.post(f"/api/care/schedule/{rid2}/send-now", json={}).json()
    assert r2["ok"] is False and r2["decision"] == "failed"
    assert r2["reason"] == "sender_returned_false"
    items = [it for g in c2.get("/api/care/plan").json()["groups"] for it in g["items"]]
    assert items[0]["fail_reason"] == "sender_returned_false"     # 卡片能显原因，行仍 pending


def test_route_send_now_held_and_dry_run():
    c, store, q = _client("ok", dry=True)
    rid = store.add_verbatim(contact_key=CONTACT, due_at=NOW + 3600, text=TEXT,
                             platform="telegram", account_id="7092595256", chat_key="6088992099")
    r = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r["ok"] is True and r["decision"] == "dry_sampled" and r["dry_run"] is True
    assert q.sent == [] and store.get(rid)["status"] == "pending"
    store.mark_hold_for_preview(rid, sent_text=TEXT, reason="first_send")
    r2 = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r2["ok"] is False and r2["decision"] == "held" and r2["held"] == "first_send"


def test_route_send_now_without_dispatcher_falls_back_to_bring_forward():
    import time as _t
    c, store, q = _client("ok", with_dispatcher=False)
    rid = store.add_verbatim(contact_key=CONTACT, due_at=_t.time() + 3600, text=TEXT,
                             platform="telegram", account_id="7092595256", chat_key="6088992099")
    before = store.get(rid)["due_at"]
    r = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r["ok"] is True and r["decision"] == "brought_forward"
    assert r["reason"] == "dispatcher_missing"
    assert store.get(rid)["due_at"] < before            # due_at 真改到 now（墙钟）
    assert c.post("/api/care/schedule/999/send-now", json={}).json()["decision"] == "not_pending"


def test_route_send_now_runs_on_engine_loop_when_registered():
    """web 线程的路由把 send_now 投回派发器所在的主 loop（run_coroutine_threadsafe）。"""
    import threading
    from src.web.routes.care_routes import register_care_routes

    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    try:
        app = FastAPI()
        store = CareScheduleStore(":memory:")
        q = _Queue("ok")
        seen = {}

        class _Disp:
            def effective_dry_run(self):
                return False

            def health_snapshot(self):
                return {"running": True}

            async def send_now(self, item, *, now=None):
                seen["loop"] = asyncio.get_running_loop()
                seen["thread"] = threading.current_thread().name
                return {"ok": True, "decision": "sent", "sent_at": NOW, "text": TEXT, "row_id": 7}

        app.state.care_schedule_store = store
        app.state.care_engine = {"dispatcher": _Disp(), "loop": loop}
        app.state.config_manager = _CM()

        def _auth(request: Request):
            return True
        register_care_routes(app, api_auth=_auth, config_manager=app.state.config_manager)
        rid = store.add_verbatim(contact_key=CONTACT, due_at=NOW + 3600, text=TEXT,
                                 platform="telegram", account_id="7092595256", chat_key="6088992099")
        r = TestClient(app).post(f"/api/care/schedule/{rid}/send-now", json={}).json()
        assert r["ok"] is True and r["decision"] == "sent" and r["id"] == rid
        assert seen["loop"] is loop and seen["thread"] == th.name
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(timeout=5)
        loop.close()


# ── B：bring_forward 兜底＝due_at=now + note=fwd + 立刻补一拍 ───────────────
def test_bring_forward_sets_due_now_and_fwd_note_but_keeps_hold():
    s, rid = _verbatim_store()
    s.mark_send_failed(rid, "sender_returned_false", now=NOW - 5)
    assert s.bring_forward(rid, now=NOW) is True
    it = s.get(rid)
    assert it["due_at"] == NOW and it["note"] == "fwd:%d" % int(NOW)
    assert CareScheduleStore.forwarded_at(it) == float(int(NOW))
    assert CareScheduleStore.fail_reason(it) == ""            # fail:* 被覆盖
    assert [r["id"] for r in s.list_due(now=NOW)] == [rid]    # 立刻到期
    # 待确认行：due_at 提前但 hold 备注保住（路由层本就先挡，这里守 store 语义）
    s2, rid2 = _verbatim_store()
    s2.mark_hold_for_preview(rid2, sent_text=TEXT, reason="first_send")
    assert s2.bring_forward(rid2, now=NOW) is True
    assert CareScheduleStore.hold_reason(s2.get(rid2)) == "first_send"
    assert s2.bring_forward(999, now=NOW) is False


def test_route_fallback_legacy_dispatcher_ticks_immediately():
    """派发器在场但没有 send_now（旧类）→ bring_forward 后立刻 run_once，不等 600s。"""
    import time as _t
    from src.web.routes.care_routes import register_care_routes

    app = FastAPI()
    store = CareScheduleStore(":memory:")
    q = _Queue("ok")
    ticks = []

    class _Legacy:
        _interval = 600.0

        def effective_dry_run(self):
            return False

        def health_snapshot(self):
            return {"running": True, "interval_sec": 600.0, "last_tick_ts": 0.0}

        async def run_once(self, *, now=None):
            ticks.append(_t.time())
            due = store.list_due()
            for it in due:
                rid = q.store.enqueue(platform=it["platform"], account_id=it["account_id"],
                                      chat_key=it["chat_key"], reply_text=it["source_text"],
                                      defer_until=_t.time(), reason="care:verbatim",
                                      extra={"care": True, "care_id": it["id"]})
                store.mark_sent(it["id"], note=f"deferred:{rid}", sent_text=it["source_text"])
            return len(due)

    app.state.care_schedule_store = store
    app.state.care_engine = {"dispatcher": _Legacy()}
    app.state.config_manager = _CM()

    def _auth(request: Request):
        return True
    register_care_routes(app, api_auth=_auth, config_manager=app.state.config_manager)
    c = TestClient(app)
    rid = store.add_verbatim(contact_key=CONTACT, due_at=_t.time() + 3600, text=TEXT,
                             platform="telegram", account_id="7092595256", chat_key="6088992099")
    r = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r["ok"] is True and r["decision"] == "brought_forward"
    assert r["reason"] == "legacy_dispatcher" and r["ticked"] is True and r["scheduled"] == 1
    assert abs(r["eta"] - _t.time()) < 5                      # 立刻补拍成功 → 预计＝现在
    assert len(ticks) == 1 and store.get(rid)["status"] == "sent"


def test_route_fallback_without_dispatcher_reports_eta_and_card_state():
    import time as _t
    c, store, q = _client("ok", with_dispatcher=False)
    rid = store.add_verbatim(contact_key=CONTACT, due_at=_t.time() + 3600, text=TEXT,
                             platform="telegram", account_id="7092595256", chat_key="6088992099")
    r = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r["decision"] == "brought_forward" and r["ticked"] is False
    assert r["eta"] >= _t.time() + 500                        # 无派发器：预计＝now+interval（缺省 600）
    items = [it for g in c.get("/api/care/plan").json()["groups"] for it in g["items"]]
    assert items[0]["forwarded_at"] > 0 and items[0]["note"].startswith("fwd:")
    assert items[0]["due_at"] <= _t.time()                    # 已提前到期，进「到期」组


# ── C：错峰不作用于人工指定时间 ─────────────────────────────────────────────
async def test_due_verbatim_row_dispatches_with_zero_jitter_but_event_rows_stagger():
    """到期派发：原文行 defer_until==now（零错峰）；AI 关怀行仍吃 send_jitter_sec。"""
    from src.contacts.care_commitment import CareCommitment
    s = CareScheduleStore(":memory:")
    vid = s.add_verbatim(contact_key=CONTACT, due_at=NOW - 60, text=TEXT,
                         platform="telegram", account_id="7092595256", chat_key="6088992099")
    eid = s.add_commitment(
        CareCommitment(due_at=NOW - 30, event_at=NOW - 30, topic="面试", sentiment="neutral",
                       anchor_text="x", source_text="明天面试", confidence=1.0),
        contact_key="tg:other", platform="telegram", account_id="7092595256", chat_key="1")
    q = _Queue("ok")
    d = CareDispatcher(store=s, ai_client=_AI("How did it go?"), send_callback=q.send_callback(),
                       context_provider=lambda ck: "ctx", send_jitter_sec=(600.0, 600.0),
                       quiet_start_hour=0, quiet_end_hour=0, max_per_tick=0)
    assert await d.run_once(now=NOW) == 2
    rows = {r["chat_key"]: r for r in q.store.list_recent(limit=10)}
    assert rows["6088992099"]["defer_until"] == NOW              # 原文到点：零偏移
    assert rows["1"]["defer_until"] == NOW + 600.0               # 自动关怀：错峰
    assert s.get(vid)["status"] == "sent" and s.get(eid)["status"] == "sent"


async def test_deliver_text_manual_rewrite_has_zero_jitter():
    s, rid = _verbatim_store(due_offset=-60)
    q = _Queue("ok")
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=q.send_callback(),
                       send_jitter_sec=(60.0, 1200.0), quiet_start_hour=0, quiet_end_hour=0)
    res = await d.deliver_text(s.get(rid), "改过的话", now=NOW)
    assert res["ok"] is True and res["defer_until"] == NOW


def test_projected_send_window_matches_dispatch_rules():
    from src.contacts.care_commitment import CareCommitment
    s = CareScheduleStore(":memory:")
    due = datetime(2026, 9, 7, 22, 50, 0).timestamp()                  # 22:50，安静窗 23–08
    vid = s.add_verbatim(contact_key=CONTACT, due_at=due, text=TEXT,
                         platform="telegram", account_id="7092595256", chat_key="6088992099")
    eid = s.add_commitment(
        CareCommitment(due_at=due, event_at=due, topic="面试", sentiment="neutral",
                       anchor_text="x", source_text="明天面试", confidence=1.0),
        contact_key="tg:other", platform="telegram", account_id="7092595256", chat_key="1")
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_Queue("ok").send_callback(),
                       send_jitter_sec=(60.0, 1200.0), quiet_start_hour=23, quiet_end_hour=8)
    v = d.projected_send_window(s.get(vid))
    assert v == {"eta_min": due, "eta_max": due, "jitter": False, "quiet_shifted": False}
    e = d.projected_send_window(s.get(eid))
    assert e["jitter"] is True and e["quiet_shifted"] is True
    assert e["eta_min"] == due + 60.0                                   # 22:51 仍在窗外
    assert datetime.fromtimestamp(e["eta_max"]).hour == 8               # 23:10 → 顺延到 08:00
    assert d.projected_send_window({"due_at": 0}) == {
        "eta_min": 0.0, "eta_max": 0.0, "jitter": False, "quiet_shifted": False}


def test_route_plan_items_carry_eta_and_template_shows_it():
    import time as _t
    c, store, q = _client("ok")
    due = _t.time() + 3600
    store.add_verbatim(contact_key=CONTACT, due_at=due, text=TEXT,
                       platform="telegram", account_id="7092595256", chat_key="6088992099")
    items = [it for g in c.get("/api/care/plan").json()["groups"] for it in g["items"]]
    assert items[0]["eta"]["eta_min"] == due and items[0]["eta"]["jitter"] is False
    tpl = (_REPO / "src" / "web" / "templates" / "care_schedule.html").read_text(encoding="utf-8")
    assert "_csEtaHtml(it)" in tpl and "cs8_eta_quiet" in tpl and "cs8_eta_jitter" in tpl


# ── D：deferred 队列随后端启动常备 + enabled 只做入队热闸 + 出队/每拍日志 ──────
class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **k):
        self.lines.append(msg % a if a else msg)

    warning = debug = info


class _QAssistant:
    """ensure_deferred_outbox / _enqueue_deferred_outbox 需要的最小 assistant 面。"""

    def __init__(self, tmp_path, enabled: bool):
        class _Cfg:
            pass
        self.config = _Cfg()
        self.config.config = {"companion": {"multiplatform_deferred": {
            "enabled": enabled, "interval_sec": 120, "quiet_start_hour": 0, "quiet_end_hour": 0}}}
        self.config.config_path = str(tmp_path / "config.yaml")
        self._deferred_outbox_dispatcher = None
        self._web_app = None
        self.logger = _Log()
        self.inbox_store = None
        self.telegram_client = None

    async def _maybe_translate_outbound(self, platform, account_id, chat_key, text):
        return text

    # main.py 的两个方法原样搬来（绑定到本 stub），钉住它们对 ensure/accepting 的用法
    def _ensure_deferred_outbox(self):
        from src.bootstrap.background_tasks import ensure_deferred_outbox
        return ensure_deferred_outbox(self)

    def _enqueue_deferred_outbox(self, channel, account_id, chat_name, reply,
                                 defer_until, reason, staleness_sec, extra) -> int:
        from src.bootstrap.background_tasks import deferred_outbox_accepting
        if not deferred_outbox_accepting(self):
            return 0
        dispatcher = self._ensure_deferred_outbox()
        if dispatcher is None:
            return 0
        return dispatcher._store.enqueue(
            platform=str(channel), account_id=str(account_id or "default"),
            chat_key=str(chat_name), reply_text=str(reply), defer_until=float(defer_until),
            reason=str(reason or ""), staleness_sec=float(staleness_sec), extra=extra or {})


async def test_deferred_queue_is_built_and_started_even_when_flag_off(tmp_path):
    """复刻 13:14 启动态：enabled=False → 队列仍建、drain loop 仍起；入队被热闸挡（返 0）。"""
    a = _QAssistant(tmp_path, enabled=False)
    disp = a._ensure_deferred_outbox()
    try:
        assert disp is not None and disp.is_running() is True         # 懒建路径也 start
        assert any("drain_loop=running" in ln and "accepting=False" in ln for ln in a.logger.lines)
        assert a._enqueue_deferred_outbox("telegram", "7092595256", "6088992099", TEXT,
                                          NOW, "care:verbatim", 86400, {"care": True}) == 0
        assert disp._store.count() == 0
        # 15:27 页面开闸（运行时改内存配置）→ 下一条直接入队，且 loop 早就在跑
        a.config.config["companion"]["multiplatform_deferred"]["enabled"] = True
        rid = a._enqueue_deferred_outbox("telegram", "7092595256", "6088992099", TEXT,
                                         NOW, "care:verbatim", 86400, {"care": True, "care_id": 1})
        assert rid == 1 and disp.is_running() is True
        assert a._ensure_deferred_outbox() is disp                    # 幂等
    finally:
        await disp.stop()


async def test_deferred_queue_drains_care_row_after_runtime_enable(tmp_path):
    """16:34:52 入队 → 到点必有人 drain（此前零协程）；投递结果 [care-deferred] 落日志。"""
    a = _QAssistant(tmp_path, enabled=True)
    sent = []
    disp = a._ensure_deferred_outbox()
    try:
        async def _sender(account_id, chat_key, text):
            sent.append(text)
            return True
        disp.register_sender("telegram", _sender)
        rid = a._enqueue_deferred_outbox("telegram", "7092595256", "6088992099", TEXT,
                                         NOW, "care:verbatim", 86400, {"care": True, "care_id": 1})
        assert rid == 1
        assert await disp.run_once(now=NOW + 1) == 1 and sent == [TEXT]
        assert disp._store.get(rid)["status"] == "sent"
    finally:
        await disp.stop()


def test_ensure_started_without_running_loop_is_safe():
    d = DeferredDispatcher(store=DeferredOutboxStore(":memory:"),
                           kill_switch_check=lambda p, a: (False, "", ""))
    assert d.ensure_started() is False and d.is_running() is False


async def test_care_loop_logs_every_tick_and_enabled_flip(caplog):
    """G7KEUT：「2.5h 零 tick 日志」→ 每拍一行；enabled 翻转一行；不当场发要写 defer_why。"""
    s, rid = _verbatim_store(due_offset=-60)
    q = _Queue("ok")
    live = {"enabled": False, "dry_run": False}
    d = _disp(s, q, live=live)
    with caplog.at_level(logging.INFO, logger="src.contacts.care_dispatcher"):
        assert await d.run_once(now=NOW) == 0                         # 13:14 关闸空转
        live["enabled"] = True                                        # 15:27 开闸
        assert await d.run_once(now=NOW + 600) == 1                   # 下一拍派出
        assert await d.run_once(now=NOW + 1200) == 0                  # 无事可派也写一行
    msgs = [r.getMessage() for r in caplog.records]
    assert any("首拍 enabled=False" in m for m in msgs)
    assert any("enabled 实时值变化 False → True" in m for m in msgs)
    ticks = [m for m in msgs if m.startswith("[care_dispatcher] tick now=")]
    assert len(ticks) >= 2
    assert any("due=1 dispatched=1" in m and "'enqueued': 1" in m for m in ticks)
    assert any("due=0 dispatched=0" in m for m in ticks)
    assert any("decision=enqueued" in m and "defer_why=none" in m for m in msgs)  # 原文行零错峰


def test_delivery_light_breaks_when_queue_flag_on_but_loop_dead():
    from src.contacts.care_advisor import build_lights
    base = dict(enabled=True, dry_run=False, capture_wired=True, capture_config_on=True,
                dispatch_running=True, multiplatform_deferred=True)
    assert build_lights(**base)["delivery"] == "ready"                       # 旧后端不知道
    assert build_lights(**base, delivery_running=True)["delivery"] == "ready"
    assert build_lights(**base, delivery_running=False)["delivery"] == "broken"   # 09-07 事故态
    assert build_lights(**dict(base, multiplatform_deferred=False),
                        delivery_running=False)["delivery"] == "off"


def test_routes_expose_queue_running():
    c, store, q = _client("ok")
    h = c.get("/api/care/health").json()
    assert h["delivery"]["queue_running"] is None                     # 测试 app 未挂 dispatcher
    c.app.state.deferred_outbox_dispatcher = q.disp
    assert c.get("/api/care/health").json()["delivery"]["queue_running"] is False
    from src.web.routes.deferred_outbox_routes import register_deferred_outbox_routes

    def _auth(request: Request):
        return True
    register_deferred_outbox_routes(c.app, api_auth=_auth)
    st = c.get("/api/deferred-outbox/status").json()
    assert st["enabled"] is True and st["running"] is False and st["accepting"] is False
    c.app.state.config_manager.config["companion"]["multiplatform_deferred"] = {"enabled": True}
    assert c.get("/api/deferred-outbox/status").json()["accepting"] is True


# ── E：卡片状态机——排队中再点「立即发」当场投那一行；按钮只在可发态出现 ─────────
async def test_deliver_queued_sends_the_existing_queue_row_without_duplicating():
    s, rid = _verbatim_store(due_offset=-60)
    q = _Queue("not_ready")
    d = _disp(s, q, live={"enabled": True, "dry_run": False})
    assert await d.run_once(now=NOW) == 1                       # 到期派发：入队（通道未就绪不影响入队）
    it = s.get(rid)
    assert it["status"] == "sent" and it["note"] == "deferred:1"
    q.mode = "ok"
    res = await d.deliver_queued(it, now=NOW + 30)              # 排队中再点「立即发」
    assert res["ok"] is True and res["decision"] == "sent" and res["row_id"] == 1
    assert res["text"] == TEXT and q.sent == [("7092595256", "6088992099", TEXT)]
    assert q.store.count() == 1 and q.store.get(1)["status"] == "sent"   # 没新建队列行
    res2 = await d.deliver_queued(s.get(rid), now=NOW + 60)
    assert res2["decision"] == "not_pending" and res2["reason"] == "already_sent"
    assert len(q.sent) == 1
    # 非排队态 / 坏备注 / 无同步钩子
    assert (await d.deliver_queued({"status": "pending", "note": ""}))["reason"] == "not_queued"
    assert (await d.deliver_queued({"status": "sent", "note": "deferred:x"}))["reason"] == "bad_note"
    d2 = CareDispatcher(store=s, ai_client=_AI(), send_callback=q.send_callback())
    r3 = await d2.deliver_queued({"status": "sent", "note": "deferred:1", "platform": "telegram"})
    assert r3["decision"] == "queued" and r3["reason"] == "no_sync_path"


def test_route_send_now_on_queued_row_delivers_and_history_shows_states():
    c, store, q = _client("not_ready")
    rid = store.add_verbatim(contact_key=CONTACT, due_at=NOW + 3600, text=TEXT,
                             platform="telegram", account_id="7092595256", chat_key="6088992099")
    r = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r["decision"] == "queued"                            # 通道未就绪 → 排队中
    hist = c.get("/api/care/schedule?status=sent").json()["items"]
    assert hist[0]["delivery"]["status"] == "pending" and hist[0]["delivery"]["defer_until"] > 0
    q.mode = "ok"
    r2 = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()   # 排队中再点
    assert r2["ok"] is True and r2["decision"] == "sent" and r2["row_id"] == 1
    assert q.sent and q.store.count() == 1
    hist2 = c.get("/api/care/schedule?status=sent").json()["items"]
    assert hist2[0]["delivery"]["status"] == "sent" and hist2[0]["delivery"]["sent_at"] > 0
    r3 = c.post(f"/api/care/schedule/{rid}/send-now", json={}).json()
    assert r3["ok"] is False and r3["decision"] == "not_pending" and r3["reason"] == "already_sent"


def test_template_state_machine_and_button_rules():
    tpl = (_REPO / "src" / "web" / "templates" / "care_schedule.html").read_text(encoding="utf-8")
    for fn in ("_csCardState", "_csSentState", "_csNextTickEta"):
        assert "function " + fn in tpl, fn
    for k in ("cs8_state_scheduled", "cs8_state_forwarded", "cs8_state_queued",
              "cs8_state_failed", "cs8_state_held", "cs8_dv_pending_eta"):
        assert k in tpl, k
    # 历史表：待确认行不出「立即发」；排队中行出「立即发」
    seg = tpl[tpl.index("function _csHistRowHtml"):tpl.index("function _csHistRender")]
    assert "_held?'':" in seg and "_queued=" in seg
    # 最近已发区块：排队中带 data-sendnow 按钮
    seg2 = tpl[tpl.index("function _csSampleRow"):tpl.index("function _csDayLabel")]
    assert "st.code==='queued'" in seg2 and 'data-sendnow="' in seg2
    from src.web.i18n_packs.care_page import EN, ZH
    for k in ("cs8_state_scheduled", "cs8_state_forwarded", "cs8_state_queued",
              "cs8_state_failed", "cs8_why_quiet_hours"):
        assert k in ZH and k in EN, k


# ── 静态钉 ──────────────────────────────────────────────────────────────────
def test_background_tasks_wires_deliver_now_and_loop():
    src = (_REPO / "src" / "bootstrap" / "background_tasks.py").read_text(encoding="utf-8")
    assert "deliver_now=_care_deliver_now" in src
    assert 'engine_state["loop"] = asyncio.get_running_loop()' in src
    assert "messenger_rpa_queue" in src
    # D：ensure 不再按开关早退（旧 `if not cfg.get("enabled", False): return None` 必须消失）
    seg = src[src.index("def ensure_deferred_outbox"):src.index("async def warmup_embeddings")]
    assert 'if not cfg.get("enabled", False):' not in seg
    assert "ensure_started()" in seg
    main_src = (_REPO / "main.py").read_text(encoding="utf-8")
    assert "deferred_outbox_accepting(self)" in main_src
    assert "多平台 deferred 队列未启用" not in main_src        # 启动期不再因关闸跳过 start


def test_template_send_now_button_is_debounced_and_reports():
    tpl = (_REPO / "src" / "web" / "templates" / "care_schedule.html").read_text(encoding="utf-8")
    assert tpl.count('data-sendnow="') >= 2            # 方案卡 + 历史表两处按钮
    assert "_csSendNowBusy" in tpl and "b.disabled=true" in tpl
    for k in ("cs8_sent_at", "cs8_queued_at", "cs8_dry_sampled", "cs8_send_fail", "cs8_held_now"):
        assert k in tpl, k
    from src.web.i18n_packs.care_page import EN, ZH
    for k in ("cs8_sending", "cs8_sent_at", "cs8_queued_at", "cs8_send_fail",
              "cs8_why_sender_not_ready", "cs8_why_queue_unavailable"):
        assert k in ZH and k in EN, k
