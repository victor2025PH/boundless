# -*- coding: utf-8 -*-
"""Q-18 B/C（#292 #291）门禁：让位 = 延后不是丢弃。

华哥 7340576921 时间线（2026-09-11）：16:46:58 坐席手发 → `[inflight] cancel by=agent_send`
（正确）；16:50:13 客户新消息 → L2 稿 → 16:50:15 `abort=agent_sent stage=batch` —— **稿被丢弃，
60s 窗过后不再重试**，界面绿灯全自动却再无回复。

本文件钉住：手发后 60s 内入站 → defer 不丢（稿留 pending、同窗只落一行 defer 日志）/ 窗过
自动复检发出（resume=agent_window_passed）/ 切全自动立即放行（resume by=mode_select）/ 再手发
再让位 / presend 期让位走 _retry_queue 改期、到点重投 / 让位等待中坐席真发 → 稿取消（防同问
双答）/ 连续让位超上限按放弃 / mode_changed · risk_hold · needs_human 语义一字不变 /
autosend_policy.agent_yield_state 纯函数。
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from src.inbox import risk_hold as rh
from src.inbox.autosend_policy import (
    AGENT_YIELD_MAX_DEFERRALS, AGENT_YIELD_WINDOW_SEC, YIELD_DEFER_REASONS, agent_yield_state,
)
from src.inbox.autosend_worker import ABORT_REASONS, AutosendWorker

LOGGER = "src.inbox.autosend_worker"


class _KVStore:
    def __init__(self):
        self.kv, self.tags, self.meta, self.modes = {}, {}, {}, {}
        self.status = {}          # draft_id → (status, decided_by)
        self.cancelled_pending = []

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value

    def list_app_settings(self, prefix):
        return [{"key": k, "value": v} for k, v in self.kv.items() if k.startswith(prefix)]

    def get_conv_tags(self, cid):
        return list(self.tags.get(cid, []))

    def set_conv_tags(self, cid, tags):
        self.tags[cid] = list(tags)
        return True

    def get_handoff_meta(self, cid):
        return dict(self.meta.get(cid) or {})

    def set_handoff_meta(self, cid, meta):
        if meta:
            self.meta[cid] = dict(meta)
        else:
            self.meta.pop(cid, None)
        return True

    def get_automation_mode_if_set(self, cid):
        return self.modes.get(cid)

    def set_automation_mode(self, cid, mode, source=""):
        self.modes[cid] = mode

    def update_draft_status(self, draft_id, *, status, final_text="", decided_by="",
                            expected_statuses=("pending", "enriching")):
        self.status[draft_id] = (status, decided_by)
        return True

    def cancel_pending_l2_drafts(self, cid, *, decided_by="mode_downgraded"):
        self.cancelled_pending.append((cid, decided_by))
        return 0

    def list_recent_messages(self, cid, limit=8):
        return []


class _Svc:
    def __init__(self, store, drafts):
        self._store = store
        self._drafts = list(drafts)
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [d for d in self._drafts
                if self._store.status.get(d["draft_id"], ("pending", ""))[0] == "pending"]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        self._store.status[draft_id] = ("approved", by)
        return {"ok": True}


CID = "telegram:a1:huage"


def _draft(cid=CID, did="d1", text="hi there", **kw):
    d = {"draft_id": did, "autopilot_level": "L2", "final_text": text,
         "platform": "telegram", "account_id": "a1", "chat_key": "huage",
         "conversation_id": cid, "peer_text": "hello?", "created_at": 0}
    d.update(kw)
    return d


def _worker(store, drafts, *, sleep=None, delay=0.0):
    sent = []

    async def _send(p, a, c, text, **kw):
        sent.append(text)
        return {"ok": True}

    async def _nosleep(sec):
        return None

    svc = _Svc(store, drafts)
    cfg = {"deliver_delay": {"min_sec": delay, "max_sec": delay}, "enabled": True}
    w = AutosendWorker(draft_service=svc, config=cfg, send_callback=_send,
                       sleep=sleep or _nosleep)
    return w, svc, sent


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


# ── 纯函数 / 常量 ───────────────────────────────────────────────────────────

def test_yield_constants_and_state():
    assert set(YIELD_DEFER_REASONS) == {"agent_sent", "agent_typing"}
    assert set(YIELD_DEFER_REASONS) < set(ABORT_REASONS)
    assert AGENT_YIELD_WINDOW_SEC == 60.0 and AGENT_YIELD_MAX_DEFERRALS >= 3
    now = 1000.0
    assert agent_yield_state(0, 0, now=now)["active"] is False
    st = agent_yield_state(now - 37, 0, now=now)
    assert st["active"] and st["by"] == "agent_sent" and st["until"] == now - 37 + 60
    assert abs(st["remaining"] - 23) < 1e-6
    st = agent_yield_state(now - 200, now - 5, now=now)        # 发送窗已过、打字仍在窗内
    assert st["by"] == "agent_typing" and abs(st["remaining"] - 55) < 1e-6
    st = agent_yield_state(now - 5, now - 3, now=now)          # 两者都在窗内 → 发送优先
    assert st["by"] == "agent_sent"
    assert agent_yield_state(now - 61, now - 61, now=now)["active"] is False


# ── 门禁 1：手发后 60s 内入站 → defer 不丢 ─────────────────────────────────

@pytest.mark.asyncio
async def test_inbound_within_60s_after_agent_send_is_deferred_not_dropped(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w, svc, sent = _worker(st, [])
    w.note_agent_send(CID)                       # 16:46:58 坐席手发（此刻无稿在途）
    svc._drafts.append(_draft())                 # 16:50:13 客户新消息 → L2 稿
    await w._tick()
    await w._tick()                              # 窗内再过一 tick
    assert sent == [] and svc.resolved == []
    assert "d1" not in st.status                 # 没被改 cancelled —— 稿还在 pending
    # 手发那一刻的 cancel_pending_l2_drafts（Q-3 原语义：手发前已有的稿被人接）只在 16:46:58
    # 发生一次；之后到的 d1 不再被取消
    assert st.cancelled_pending == [(CID, "abort:agent_send")]
    assert w.total_yield_deferred == 1 and w.total_abort_recheck == 0
    ys = w.agent_yield_state(CID)
    assert ys["active"] and ys["by"] == "agent_sent" and ys["draft_id"] == "d1"
    assert ys["stage"] == "batch" and 0 < ys["remaining"] <= 60
    m = _msgs(caplog)
    defer_lines = [x for x in m if "[autosend] defer=agent_sent stage=batch draft=d1" in x]
    assert len(defer_lines) == 1 and f"conv={CID}" in defer_lines[0] and "until=" in defer_lines[0]
    assert not any("[autosend] abort=agent_sent" in x for x in m)


# ── 门禁 2：窗过自动复检发出 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_window_passed_auto_resumes_and_sends(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w, svc, sent = _worker(st, [_draft()])
    w.note_agent_send(CID)
    await w._tick()
    assert sent == [] and w.total_yield_deferred == 1
    w._agent_sent_ts[CID] = time.time() - AGENT_YIELD_WINDOW_SEC - 1     # 窗过
    await w._tick()
    assert sent == ["hi there"] and svc.resolved == ["d1"]
    assert st.status["d1"][0] == "approved"
    assert w.total_yield_resumed == 1 and w.agent_yield_state(CID)["active"] is False
    assert CID not in w._yield_defer
    assert any("[autosend] resume=agent_window_passed stage=batch draft=d1" in x
               for x in _msgs(caplog))


# ── 门禁 3：切全自动 = 明示接回，立即发 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_mode_select_auto_clears_yield_and_sends_immediately(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w, svc, sent = _worker(st, [_draft()])
    w.note_agent_send(CID)
    w.note_agent_typing(CID)
    await w._tick()
    assert sent == [] and w.agent_yield_state(CID)["active"]
    r = w.resume_agent_yield(CID, by="mode_select")
    assert r["had_yield"] is True
    assert CID not in w._agent_sent_ts and CID not in w._agent_typing_ts
    assert w.agent_yield_state(CID)["active"] is False
    await w._tick()                              # 不等 60s
    assert sent == ["hi there"]
    assert any(f"[autosend] resume by=mode_select conv={CID}" in x for x in _msgs(caplog))
    # 无让位时重选全自动是空操作
    assert w.resume_agent_yield("telegram:a1:nobody")["had_yield"] is False


# ── 门禁 4：再手发再让位 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_sends_again_yields_again(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w, svc, sent = _worker(st, [_draft()])
    w.note_agent_send(CID)
    await w._tick()
    w._agent_sent_ts[CID] = time.time() - 61
    await w._tick()
    assert sent == ["hi there"]
    w.note_agent_send(CID)                       # 坐席又手发
    svc._drafts.append(_draft(did="d2", text="second"))
    await w._tick()
    assert sent == ["hi there"] and "d2" not in st.status
    assert w.total_yield_deferred == 2 and w.agent_yield_state(CID)["draft_id"] == "d2"
    w._agent_sent_ts[CID] = time.time() - 61
    await w._tick()
    assert sent == ["hi there", "second"] and w.total_yield_resumed == 2


# ── presend 期让位：走 _retry_queue 改期、到点重投 ─────────────────────────

@pytest.mark.asyncio
async def test_presend_yield_requeues_and_sends_after_window(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w = None

    async def _sleep(sec):
        # 拟人等待期间坐席在别的界面手发（状态判定，非点名取消信号）
        w._agent_sent_ts[CID] = time.time()

    w, svc, sent = _worker(st, [_draft()], sleep=_sleep, delay=5.0)
    await w._tick()
    assert sent == [] and svc.resolved == ["d1"]
    assert st.status["d1"][0] == "approved"                       # 行没被改 cancelled
    assert len(w._retry_queue) == 1
    q = w._retry_queue[0]
    assert q["item"]["_yield_defer"] is True and q["item"]["_yield_by"] == "agent_sent"
    assert q["next_ts"] > time.time() + 50
    assert w.total_yield_deferred == 1 and w.total_abort_recheck == 0
    assert any("[autosend] defer=agent_sent stage=presend draft=d1" in x for x in _msgs(caplog))
    # 窗过：改期项到期 + 坐席不再活动 → 重投
    q["next_ts"] = 0.0
    w._agent_sent_ts[CID] = time.time() - 61

    async def _quiet(sec):
        return None
    w._sleep = _quiet
    await w._tick()
    assert sent == ["hi there"] and w._retry_queue == []
    assert w.total_yield_resumed == 1 and w.total_delivered == 1
    assert any("[autosend] resume=agent_window_passed stage=presend draft=d1" in x
               for x in _msgs(caplog))


@pytest.mark.asyncio
async def test_presend_yield_cancelled_when_agent_really_sends(caplog):
    """让位等待中坐席真发（cancel_inflight by=agent_send）→ 改期稿取消，不在窗过后再答一遍。"""
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w = None

    async def _sleep(sec):
        w._agent_typing_ts[CID] = time.time()

    w, svc, sent = _worker(st, [_draft()], sleep=_sleep, delay=5.0)
    await w._tick()
    assert len(w._retry_queue) == 1 and w.agent_yield_state(CID)["by"] == "agent_typing"
    n = w.note_agent_send(CID)
    assert n == 1 and w._retry_queue == []
    assert st.status["d1"] == ("cancelled", "abort:agent_send")
    assert any("[autosend] abort=agent_send stage=deferred draft=d1" in x for x in _msgs(caplog))
    w._agent_sent_ts[CID] = time.time() - 61
    await w._tick()
    assert sent == []


@pytest.mark.asyncio
async def test_inflight_cancel_signal_still_aborts_not_defers():
    """Q-3 原语义保留：拟人等待中坐席打字端点点名取消（行已 cancelled）→ 放弃，不 defer。"""
    st = _KVStore()
    w = None

    async def _sleep(sec):
        w.note_agent_typing(CID)

    w, svc, sent = _worker(st, [_draft()], sleep=_sleep, delay=5.0)
    await w._tick()
    assert sent == [] and st.status["d1"] == ("cancelled", "abort:agent_typing")
    assert w._retry_queue == [] and w.total_yield_deferred == 0


# ── 连续让位超上限 → 按放弃走老路 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_yield_exhausted_falls_back_to_abort(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w, svc, sent = _worker(st, [_draft()])
    w.note_agent_send(CID)
    w._yield_defer[CID] = {"by": "agent_sent", "until": 1.0, "since": 0.0, "draft_id": "d1",
                           "stage": "batch", "deferrals": AGENT_YIELD_MAX_DEFERRALS,
                           "first_ts": time.time() - 600}
    await w._tick()
    assert sent == [] and st.status["d1"] == ("cancelled", "abort:agent_sent")
    assert w.total_yield_exhausted == 1 and CID not in w._yield_defer
    m = _msgs(caplog)
    assert any("[autosend] yield_exhausted=agent_sent" in x for x in m)
    assert any("[autosend] abort=agent_sent stage=batch draft=d1" in x for x in m)


# ── mode_changed / risk_hold / needs_human 语义一字不变 ─────────────────────

@pytest.mark.asyncio
async def test_other_abort_codes_unchanged(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    from src.integrations.protocol_autoreply import HANDOFF_TAG
    st = _KVStore()
    c_mode, c_hold, c_tag = "telegram:a1:m", "telegram:a1:h", "telegram:a1:t"
    st.set_automation_mode(c_mode, "manual", source="human")
    rh.set(st, c_hold, "privacy", by="test")
    st.set_conv_tags(c_tag, [HANDOFF_TAG])
    w, svc, sent = _worker(st, [_draft(c_mode, "dm"), _draft(c_hold, "dh"), _draft(c_tag, "dt")])
    await w._tick()
    assert sent == [] and svc.resolved == []
    assert st.status["dm"] == ("cancelled", "mode_downgraded")
    assert st.status["dh"] == ("cancelled", "abort:risk_hold")
    assert st.status["dt"] == ("cancelled", "abort:needs_human")
    assert w.total_yield_deferred == 0 and w._yield_defer == {}
    m = _msgs(caplog)
    for code in ("mode_changed", "risk_hold", "needs_human"):
        assert any(f"[autosend] abort={code} stage=batch" in x for x in m)


# ── C（路由 / 诊断 / i18n）：切全自动 = 明示接回、chip 端点、GET automation 带状态 ────

def _route_app(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.inbox.store import InboxStore
    from src.web.routes.unified_inbox_stored_read_routes import register_stored_read_routes
    app = FastAPI()
    register_stored_read_routes(app, api_auth=lambda request=None: None)
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return app, store, TestClient(app)


def test_mode_select_auto_route_resumes_yield(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    app, store, client = _route_app(tmp_path)
    st = _KVStore()
    w, svc, sent = _worker(st, [])
    app.state.autosend_worker = w
    cid = "telegram:a1:u1"
    w.note_agent_send(cid)
    # GET automation 带让位真值（前端 45s 周期 / 手发后立即刷 → ay- chip）
    r = client.get("/api/unified-inbox/automation",
                   params={"platform": "telegram", "account_id": "a1", "chat_key": "u1"})
    assert r.status_code == 200, r.text
    ay = r.json()["agent_yield"]
    assert ay["active"] and ay["by"] == "agent_sent" and 0 < ay["remaining"] <= 60
    assert ay["window_sec"] == 60.0
    # 切到全自动 → 清让位 + resume by=mode_select
    r = client.post("/api/unified-inbox/automation",
                    json={"platform": "telegram", "account_id": "a1", "chat_key": "u1",
                          "mode": "auto_ai"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["agent_yield_resumed"]["had_yield"] is True and d["agent_yield_resumed"]["by"] == "mode_select"
    assert w.agent_yield_state(cid)["active"] is False
    assert any(f"[autosend] resume by=mode_select conv={cid}" in x for x in _msgs(caplog))
    # 切手动不碰让位状态字段（None），旧 cancel 语义照走
    w.note_agent_send(cid)
    r = client.post("/api/unified-inbox/automation",
                    json={"platform": "telegram", "account_id": "a1", "chat_key": "u1",
                          "mode": "manual"})
    assert r.status_code == 200 and r.json()["agent_yield_resumed"] is None
    assert w.agent_yield_state(cid)["active"] is True
    store.close()


def test_agent_yield_resume_endpoint(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    app, store, client = _route_app(tmp_path)
    st = _KVStore()
    w, svc, sent = _worker(st, [])
    app.state.autosend_worker = w
    cid = "telegram:a1:u1"
    w.note_agent_typing(cid)
    r = client.post("/api/unified-inbox/agent-yield/resume",
                    json={"platform": "telegram", "account_id": "a1", "chat_key": "u1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["conversation_id"] == cid and d["had_yield"] is True and d["by"] == "chip"
    assert d["agent_yield"]["active"] is False
    assert any(f"[autosend] resume by=chip conv={cid}" in x for x in _msgs(caplog))
    # conversation_id 直传；worker 缺席 → had_yield=False 不报错；缺参 400
    app.state.autosend_worker = None
    r = client.post("/api/unified-inbox/agent-yield/resume", json={"conversation_id": "x:y:z"})
    assert r.status_code == 200 and r.json()["had_yield"] is False and r.json()["agent_yield"] is None
    assert client.post("/api/unified-inbox/agent-yield/resume", json={}).status_code == 400
    store.close()


def test_reply_diagnosis_agent_yield_finding():
    from src.inbox.reply_diagnosis import diagnose_conversation
    st = _KVStore()
    w, svc, sent = _worker(st, [])
    cid = "telegram:a1:u1"
    w.note_agent_send(cid)
    w._yield_defer[cid] = {"by": "agent_sent", "until": time.time() + 40, "since": time.time() - 20,
                           "draft_id": "d9", "stage": "batch", "deferrals": 1, "first_ts": time.time()}
    out = diagnose_conversation(None, {}, platform="telegram", account_id="a1", chat_key="u1", worker=w)
    f = [x for x in out["findings"] if x["code"] == "agent_yield"]
    assert len(f) == 1 and f[0]["level"] == "warn"
    p = f[0]["params"]
    assert p["by"] == "agent_sent" and p["until"] > time.time() and 0 < p["remaining_sec"] <= 60
    assert p["draft_id"] == "d9" and p["stage"] == "batch" and p["deferrals"] == 1
    assert out["agent_yield"]["active"] is True
    # 无 worker / 无让位 → 无 finding（fail-open）
    out2 = diagnose_conversation(None, {}, platform="telegram", account_id="a1", chat_key="u2", worker=w)
    assert not [x for x in out2["findings"] if x["code"] == "agent_yield"]
    out3 = diagnose_conversation(None, {}, platform="telegram", account_id="a1", chat_key="u1")
    assert not [x for x in out3["findings"] if x["code"] == "agent_yield"]


def test_i18n_agent_yield_pack_three_langs():
    from src.web.i18n_packs import agent_yield as pack
    keys = {"inbox.ay.by_sent", "inbox.ay.by_typing", "inbox.ay.chip", "inbox.ay.chip_t",
            "inbox.ay.resumed", "inbox.ay.resume_fail", "inbox.ay.fix_resume", "inbox.diag.agent_yield"}
    for d in (pack.ZH, pack.EN, pack.ZH_HANT):
        assert set(d) == keys
        assert "{by}" in d["inbox.ay.chip"] and "{n}" in d["inbox.ay.chip"]
        assert "{until_hhmm}" in d["inbox.diag.agent_yield"] and "{by_label}" in d["inbox.diag.agent_yield"]


def test_template_ay_wiring_static():
    from pathlib import Path
    html = Path("src/web/templates/unified_inbox.html").read_text(encoding="utf-8")
    for needle in ("function _ayChipHtml(", "function _renderAyChip(", "async function _ayResume(",
                   "window._ayResume=_ayResume", "/api/unified-inbox/agent-yield/resume",
                   "_aySet(d.agent_yield||null)", "agent_yield:1", "_ayNoteLocal('agent_typing')",
                   "inbox.ay.fix_resume"):
        assert needle in html, needle


# ── D（#293）拦截台账：abort_ledger + worker/打标钩子 + 回复设置页端点 + why_no_reply ─────

def test_abort_ledger_record_normalize_rolling_and_summary():
    from src.inbox import abort_ledger as al
    st = _KVStore()
    assert al.normalize_code("abort:agent_send") == "agent_sent"
    assert al.normalize_code("mode_switch") == "mode_changed" == al.normalize_code("mode_downgraded")
    assert al.normalize_code("adult:pressure") == "adult" == al.normalize_code("high_risk", category="adult")
    assert al.normalize_code("risk:threat") == "risk_hold" and al.normalize_code("privacy", category="privacy") == "needs_human"
    assert al.normalize_code("work_schedule") == "work_schedule"
    assert al.record(None, conversation_id="x", code="adult") is False
    now = time.time()
    for i in range(al.MAX_ROWS + 30):
        assert al.record(st, conversation_id=f"c{i % 7}", code="agent_send", stage="batch", ts=now - 1000 + i)
    rows = al.rows(st)
    assert len(rows) == al.MAX_ROWS and rows[0]["conv"] == f"c{30 % 7}"     # 滚动：最早 30 条被挤掉
    al.record(st, conversation_id="old", code="risk_hold", ts=now - 25 * 3600)   # 窗外
    al.record(st, conversation_id="a", code="adult:explicit", hit=["nudes", "boobs"], stage="tag", ts=now - 5)
    s = al.summary(st, now=now, last_n=5)
    # 再写 2 行又挤掉 2 条最早的 agent_sent；窗外的 old 不计入 24h
    assert s["counts"]["agent_sent"] == al.MAX_ROWS - 2 and s["counts"]["adult"] == 1 and s["counts"]["risk_hold"] == 0
    assert set(al.REASONS) <= set(s["counts"]) and s["total"] == al.MAX_ROWS - 1 and s["stored"] == al.MAX_ROWS
    assert len(s["recent"]) == 5 and s["recent"][0]["conv"] == "a" and s["recent"][0]["hit"] == "nudes, boobs"
    mine = al.rows_for_conv(al.parse_rows(st.kv[al.KEY]), "a", now=now)
    assert len(mine) == 1 and mine[0]["code"] == "adult"
    assert al.parse_rows("not json") == [] and al.parse_rows('{"a":1}') == []


@pytest.mark.asyncio
async def test_worker_abort_points_write_ledger_and_defer_does_not():
    from src.inbox import abort_ledger as al
    from src.integrations.protocol_autoreply import HANDOFF_TAG
    st = _KVStore()
    c_mode, c_hold, c_tag = "telegram:a1:m", "telegram:a1:h", "telegram:a1:t"
    st.set_automation_mode(c_mode, "manual", source="human")
    rh.set(st, c_hold, "privacy", "my address is", by="test")
    st.set_conv_tags(c_tag, [HANDOFF_TAG])
    w, svc, sent = _worker(st, [_draft(c_mode, "dm"), _draft(c_hold, "dh"), _draft(c_tag, "dt"), _draft()])
    w.note_agent_send(CID)          # d1 让位 → 不进台账
    await w._tick()
    s = al.summary(st)
    assert s["counts"] == {**{k: 0 for k in al.REASONS}, "mode_changed": 1, "risk_hold": 1, "needs_human": 1}
    hold_row = [r for r in al.rows(st) if r["conv"] == c_hold][0]
    assert hold_row["hit"] == "my address is" and hold_row["reason"] == "privacy" and hold_row["stage"] == "batch"
    assert not [r for r in al.rows(st) if r["conv"] == CID]
    # 让位等待中坐席真发 → stage=deferred 进台账（agent_sent）
    w2 = None

    async def _sleep(sec):
        w2._agent_typing_ts["telegram:a1:p"] = time.time()

    st2 = _KVStore()
    w2, svc2, sent2 = _worker(st2, [_draft("telegram:a1:p", "dp")], sleep=_sleep, delay=5.0)
    await w2._tick()
    w2.note_agent_send("telegram:a1:p")
    s2 = al.summary(st2)
    assert s2["counts"]["agent_sent"] == 1 and s2["recent"][0]["stage"] == "deferred"


def test_tag_needs_human_writes_ledger_adult_vs_needs_human():
    from src.inbox import abort_ledger as al
    from src.integrations.protocol_autoreply import tag_needs_human
    st = _KVStore()
    p1 = {"platform": "telegram", "account_id": "a1", "chat_key": "u1"}
    p2 = {"platform": "telegram", "account_id": "a1", "chat_key": "u2"}
    assert tag_needs_human(st, p1, reason="adult:pressure", source="system")
    assert tag_needs_human(st, p2, reason="high_risk", source="system", level="high",
                           category="money_request", hits=["send me money"])
    assert tag_needs_human(st, p2, reason="high_risk", source="system") is False   # 标在场 → 保持，不重复计
    s = al.summary(st)
    assert s["counts"]["adult"] == 1 and s["counts"]["needs_human"] == 1 and s["total"] == 2
    row = [r for r in s["recent"] if r["conv"] == "telegram:a1:u2"][0]
    assert row["hit"] == "send me money" and row["stage"] == "tag" and row["reason"] == "high_risk"


def test_reply_settings_abort_ledger_route(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.inbox import abort_ledger as al
    from src.inbox.store import InboxStore
    from src.web.routes.reply_settings_routes import register_reply_settings_routes
    app = FastAPI()

    async def _noop(request=None):
        return None

    class _CM:
        config_path = str(tmp_path / "config" / "config.yaml")
        config = {}
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    app.state.config_manager = _CM()
    register_reply_settings_routes(app, page_auth=_noop, api_auth=_noop, templates=None,
                                   config_manager=app.state.config_manager)
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    client = TestClient(app)
    al.record(store, conversation_id="telegram:a1:u1", code="agent_typing", stage="batch")
    al.record(store, conversation_id="telegram:a1:u1", code="adult:explicit", stage="tag", hit="nudes")
    r = client.get("/api/reply-settings/abort-ledger")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["enabled"] and d["reasons"] == list(al.REASONS)
    assert d["counts"]["agent_typing"] == 1 and d["counts"]["adult"] == 1 and d["total"] == 2
    assert d["recent"][0]["code"] == "adult" and d["recent"][0]["hit"] == "nudes" and "name" in d["recent"][0]
    r = client.get("/api/reply-settings/abort-ledger", params={"hours": 1, "last": 1})
    assert r.status_code == 200 and len(r.json()["recent"]) == 1
    # store 缺席 → 全零、enabled=False
    app.state.inbox_store = None
    r = client.get("/api/reply-settings/abort-ledger")
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["total"] == 0
    store.close()


def test_i18n_abort_ledger_pack_three_langs_cover_reasons():
    from src.inbox import abort_ledger as al
    from src.web.i18n_packs import abort_ledger as pack
    for d in (pack.ZH, pack.EN, pack.ZH_HANT):
        assert set(pack.ZH) == set(d)
        for code in al.REASONS:
            assert d[f"rps_al_r_{code}"]
        assert "{n}" in d["rps_al_total"] and "{stage}" in d["rps_al_stage"]


def test_reply_settings_template_al_wiring_static():
    from pathlib import Path
    html = Path("src/web/templates/reply_settings.html").read_text(encoding="utf-8")
    for needle in ('id="rps-al-card"', "async function rpsAlLoad(", "/api/reply-settings/abort-ledger",
                   "rpsAlLoad();", "var RPS_AL_I18N", 'id="rps-al-tbody"'):
        assert needle in html, needle


def test_why_no_reply_cli_reads_ledger(tmp_path):
    import json as _json
    from src.inbox import abort_ledger as al
    from src.inbox.store import InboxStore
    import importlib
    wnr = importlib.import_module("tools.why_no_reply")
    root = tmp_path
    (root / "config").mkdir()
    store = InboxStore(root / "config" / "inbox.db")
    al.record(store, conversation_id="telegram:a1:u1", code="agent_sent", stage="batch")
    al.record(store, conversation_id="telegram:a1:u1", code="risk_hold", stage="presend", hit="my address")
    store.close()
    out = wnr.diagnose(root, "telegram", "a1", "u1")
    v = [x for x in out["verdicts"] if x["code"] == "abort_ledger"]
    assert len(v) == 1 and "24h 内被拦 2 次" in v[0]["msg"] and "my address" in v[0]["msg"]
    assert len(out["abort_ledger"]) == 2 and out["abort_ledger"][0]["code"] == "risk_hold"
    assert _json.dumps(out, ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_snapshot_exposes_yield_counters():
    st = _KVStore()
    w, svc, sent = _worker(st, [_draft()])
    w.note_agent_typing(CID)
    await w._tick()
    snap = w.status_snapshot()
    assert snap["total_yield_deferred"] == 1 and snap["yield_now"] == 1
    assert snap["total_yield_resumed"] == 0 and snap["total_yield_exhausted"] == 0
