# -*- coding: utf-8 -*-
"""Q-30 E（#319）门禁：切档瞬间同句只发一次。

事故（UG6KJG，2026-09-12）：Vanessa × Coop，01:48:44 ``resume by=mode_select`` 后
「Thanks Cooper you too Hope Reno treats you well」连发两条一字不差。

根因：让位载荷进 ``_retry_queue``（``_yield_defer``）；接回置 ``next_ts=0``；同会话
另有 pending 新稿 → 两稿 ``draft_id`` 不同，B41 (会话,草稿) 键各自成立；近重复默认关；
``_attempt>0`` 豁免被让位改期打破。

钉住：
  1. 让位中入站 → 新稿 pending + deferred 载荷 → ``mode_select`` 放行 → 恰好一条出站；
  2. 两稿不同 draft_id 同文 → 第二条被 claim 文本键拦；
  3. 真失败重试仍可重发（B41）；
  4. UG6KJG 回放：日志 ``dup_suppressed`` + ``by=yield_resume``。
"""
from __future__ import annotations

import logging

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.deliver_once import (
    TEXT_KEY_MIN_LEN, DeliverOnceRegistry, text_fp,
)

LOGGER = "src.inbox.autosend_worker"
UG_CID = "whatsapp:17345893728:12098296949"
UG_TEXT = "Thanks Cooper you too Hope Reno treats you well"


class _KVStore:
    def __init__(self):
        self.kv, self.tags, self.meta, self.modes = {}, {}, {}, {}
        self.status = {}
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
        cur = self.status.get(draft_id, ("pending", ""))[0]
        if expected_statuses and cur not in expected_statuses:
            return False
        self.status[draft_id] = (status, decided_by)
        return True

    def cancel_pending_l2_drafts(self, cid, *, decided_by="mode_downgraded"):
        n = 0
        for did, (st, _) in list(self.status.items()):
            if st in ("pending", "enriching"):
                self.status[did] = ("cancelled", decided_by)
                n += 1
        self.cancelled_pending.append((cid, decided_by))
        return n

    def list_recent_messages(self, cid, limit=8):
        return []


class _Svc:
    def __init__(self, store, drafts):
        self._store = store
        self._drafts = list(drafts)
        self.resolved = []

    def list_drafts(self, status="pending", limit=200, conversation_id=""):
        out = []
        for d in self._drafts:
            st = self._store.status.get(d["draft_id"], ("pending", ""))[0]
            if st != status:
                continue
            if conversation_id and str(d.get("conversation_id") or "") != conversation_id:
                continue
            out.append(d)
        return out[:limit]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        self._store.status[draft_id] = ("approved", by)
        return {"ok": True}


def _draft(cid, did, text, **kw):
    parts = str(cid).split(":", 2)
    plat = parts[0] if parts else "whatsapp"
    acct = parts[1] if len(parts) > 1 else "a1"
    chat = parts[2] if len(parts) > 2 else "peer"
    d = {"draft_id": did, "autopilot_level": "L2", "final_text": text,
         "platform": plat, "account_id": acct, "chat_key": chat,
         "conversation_id": cid, "peer_text": "hello?", "created_at": 0}
    d.update(kw)
    return d


def _worker(store, drafts, *, sleep=None, delay=0.0, fail_first=0):
    sent = []
    calls = {"n": 0}

    async def _send(p, a, c, text, **kw):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            raise RuntimeError("transient boom")
        sent.append(text)
        return {"ok": True}

    async def _nosleep(sec):
        return None

    svc = _Svc(store, drafts)
    cfg = {
        "deliver_delay": {"min_sec": delay, "max_sec": delay},
        "enabled": True,
        "recoverable": {"enabled": True, "max_attempts": 3,
                        "backoff_base_sec": 0, "backoff_max_sec": 0},
    }
    w = AutosendWorker(draft_service=svc, config=cfg, send_callback=_send,
                       sleep=sleep or _nosleep)
    return w, svc, sent


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


# ── 登记表：同会话同文第二把键 ───────────────────────────────────────────────

def test_text_key_blocks_same_sentence_different_draft():
    assert len(UG_TEXT) >= TEXT_KEY_MIN_LEN
    reg = DeliverOnceRegistry()
    assert reg.claim(UG_CID, "d1", UG_TEXT) == ""
    assert reg.claim(UG_CID, "d2", UG_TEXT) == "in_flight"
    reg.release(UG_CID, "d1", delivered=True, text=UG_TEXT)
    assert reg.has_delivered(UG_CID, "d1", UG_TEXT) is True
    assert reg.claim(UG_CID, "d2", UG_TEXT) == "dup_text"


def test_text_key_skips_short_slogan():
    reg = DeliverOnceRegistry()
    assert reg.claim("c", "d1", "嗯嗯好的") == ""
    assert reg.claim("c", "d2", "嗯嗯好的") == ""          # 口头禅不上文本键
    assert text_fp("a  b") == text_fp("a b")


def test_failure_clears_text_inflight_so_retry_can_claim():
    reg = DeliverOnceRegistry()
    assert reg.claim(UG_CID, "d1", UG_TEXT) == ""
    reg.release(UG_CID, "d1", delivered=False, text=UG_TEXT)
    assert reg.has_delivered(UG_CID, "d1", UG_TEXT) is False
    assert reg.claim(UG_CID, "d1", UG_TEXT) == ""


# ── 门禁 1：切档放行只出一条 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mode_select_resume_consumes_sibling_pending_sends_once(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w = None

    async def _sleep(sec):
        w._agent_sent_ts[UG_CID] = __import__("time").time()

    w, svc, sent = _worker(st, [_draft(UG_CID, "d1", UG_TEXT)], sleep=_sleep, delay=5.0)
    await w._tick()
    assert sent == [] and svc.resolved == ["d1"]
    assert st.status["d1"][0] == "approved"
    assert len(w._retry_queue) == 1 and w._retry_queue[0]["item"].get("_yield_defer")

    svc._drafts.append(_draft(UG_CID, "d2", UG_TEXT))
    r = w.resume_agent_yield(UG_CID, by="mode_select")
    assert r["had_yield"] is True and r["released"] == 1
    assert st.status["d2"] == ("consumed", "yield_resumed_dup")
    assert w.total_dup_suppressed >= 1
    assert any("[autosend] dup_suppressed" in x and f"conv={UG_CID}" in x
               and "by=yield_resume" in x for x in _msgs(caplog))

    async def _quiet(sec):
        return None
    w._sleep = _quiet
    await w._tick()
    assert sent == [UG_TEXT]


# ── 门禁 2：不同 draft_id 同文，第二条被 claim 拦 ────────────────────────────

@pytest.mark.asyncio
async def test_second_draft_same_text_blocked_by_claim():
    st = _KVStore()
    w, svc, sent = _worker(st, [_draft(UG_CID, "d1", UG_TEXT)])
    await w._tick()
    assert sent == [UG_TEXT]
    svc._drafts.append(_draft(UG_CID, "d2", UG_TEXT))
    await w._tick()
    assert sent == [UG_TEXT]
    assert w.total_skipped_already_sent == 1


# ── 门禁 3：真失败重试仍可重发（B41）────────────────────────────────────────

@pytest.mark.asyncio
async def test_true_failure_retry_still_redelivers():
    st = _KVStore()
    w, svc, sent = _worker(st, [_draft(UG_CID, "d1", UG_TEXT)], fail_first=1)
    await w._tick()
    assert sent == [] and w.total_retry_scheduled == 1
    await w._tick()
    assert sent == [UG_TEXT]
    assert w.total_skipped_already_sent == 0


# ── 门禁 4：UG6KJG 回放 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ug6kjg_replay_dup_suppressed_log(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st = _KVStore()
    w = None

    async def _sleep(sec):
        w._agent_sent_ts[UG_CID] = __import__("time").time()

    w, svc, sent = _worker(st, [_draft(UG_CID, "yield1", UG_TEXT)],
                           sleep=_sleep, delay=5.0)
    await w._tick()
    svc._drafts.append(_draft(UG_CID, "new2", UG_TEXT))
    w.resume_agent_yield(UG_CID, by="mode_select")
    assert any("[autosend] resume by=mode_select" in x and f"conv={UG_CID}" in x
               for x in _msgs(caplog))
    assert any("[autosend] dup_suppressed" in x and "by=yield_resume" in x
               and "dropped=" in x for x in _msgs(caplog))

    async def _quiet(sec):
        return None
    w._sleep = _quiet
    await w._tick()
    assert sent == [UG_TEXT]
    assert w.status_snapshot()["total_dup_suppressed"] >= 1
