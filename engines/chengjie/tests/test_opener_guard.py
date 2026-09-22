# -*- coding: utf-8 -*-
"""出站开场词守卫（P0-4 2026-09-21，Bug #340）· 判定 + autosend 接线门禁。

锁定：
  - 「Ha, / Haha! / 哈哈，」笑词家族归一为同一开场键；「Hey～」「嗯，」等感叹开场可摘；
  - 最近出站里同一开场出现 ≥ min_repeat 次 → 命中；不足 → 放行；
  - 实词开场复读（每条 "I think"）命中但不可摘 → 文本不动、只计数；
  - 摘后首字母大写、过短不摘；"Happy birthday" 不会被当成 Ha 开场；
  - worker：命中即发摘掉后的文本，快照计数；门关不动文本；真失败重试同样过守卫（幂等）。
"""

from __future__ import annotations

import uuid

import pytest

from src.inbox import opener_guard as og
from src.inbox.autosend_worker import AutosendWorker


def test_opener_key_families():
    assert og.opener_key("Ha, how was your day?") == "ha"
    assert og.opener_key("Hahaha! same here") == "ha"
    assert og.opener_key("哈哈，今天好累") == "哈"
    assert og.opener_key("呵呵 你说得对") == "哈"
    assert og.opener_key("Heyyy, what's up") == og.opener_key("Hey! there")
    assert og.opener_key("Happy birthday to you") == "happy"
    assert og.opener_key("I think you're right") == "i think"
    assert og.opener_key("今天天气不错呀") == "今天天气"
    assert og.opener_key("") == "" and og.opener_key("😊") == ""


def test_strip_opener():
    assert og.strip_opener("Ha, how was your day?") == "How was your day?"
    assert og.strip_opener("哈哈，今天好累") == "今天好累"
    assert og.strip_opener("Hey～ 明天见") == "明天见"
    assert og.strip_opener("I think you're right") == ""
    assert og.strip_opener("Happy birthday") == ""
    assert og.strip_opener("Ha, a") == ""


def test_inspect_threshold_and_strippable():
    recent = ["Ha, that's fun", "Haha! I know", "See you tomorrow"]
    hit = og.inspect("Ha, tell me more", recent)
    assert hit and hit["opener"] == "ha" and hit["count"] == 2 and hit["stripped"] == "Tell me more"
    assert og.inspect("Ha, tell me more", recent, min_repeat=3) is None
    assert og.inspect("Sounds good", recent) is None
    hit = og.inspect("I think so too", ["I think yes", "I think no"])
    assert hit and hit["stripped"] == ""
    assert og.inspect("", recent) is None


def test_recent_out_texts_filters_and_orders():
    rows = [
        {"direction": "out", "text": "old", "ts": 1, "status": "sent"},
        {"direction": "out", "text": "failed one", "ts": 5, "status": "failed"},
        {"direction": "in", "text": "peer", "ts": 6},
        {"direction": "out", "text": "new", "ts": 9, "status": "sent"},
        None,
    ]
    assert og.recent_out_texts(rows, window=6) == ["new", "old"]
    assert og.recent_out_texts(rows, window=1) == ["new"]


def test_resolve_cfg():
    c = og.resolve_cfg({})
    assert c["enabled"] is True and c["window"] == 6 and c["min_repeat"] == 2
    c = og.resolve_cfg({"inbox": {"opener_guard": {"enabled": False, "window": 99, "min_repeat": 0}}})
    assert c["enabled"] is False and c["window"] == 20 and c["min_repeat"] == 2
    assert og.resolve_cfg(None)["alert_after"] == og.DEFAULT_ALERT_AFTER
    assert og.resolve_cfg({"inbox": {"opener_guard": {"alert_after": -3}}})["alert_after"] == 0


# ── worker 接线 ───────────────────────────────────────────────────────────────

class _FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, conv, limit=200):
        return list(self.rows)


class _FakeSvc:
    def __init__(self, rows):
        self.queue = []
        self.resolved = []
        self._store = _FakeStore(rows)

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append((draft_id, action))
        return {"ok": True}


def _draft(conv, text, draft_id="d1"):
    return {
        "draft_id": draft_id, "autopilot_level": "L2",
        "final_text": text, "platform": "whatsapp",
        "account_id": "a1", "chat_key": "c1", "conversation_id": conv,
    }


def _out_rows(*texts):
    return [{"direction": "out", "text": t, "ts": 100.0 + i, "status": "sent"}
            for i, t in enumerate(texts)]


def _worker(svc, sent, *, enabled=True, send_cb=None, **kw):
    async def _send(platform, account_id, chat_key, text):
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    return AutosendWorker(
        draft_service=svc, send_callback=send_cb or _send, sleep=_sleep,
        opener_guard_cfg={"enabled": enabled, "window": 6, "min_repeat": 2}, **kw)


@pytest.mark.asyncio
async def test_worker_strips_repeated_ha():
    svc = _FakeSvc(_out_rows("Ha, that's fun", "Haha! I know"))
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(f"c-{uuid.uuid4().hex[:8]}", "Ha, tell me more about it")]
    await w._tick()
    assert sent == ["Tell me more about it"]
    assert w.total_opener_stripped == 1
    assert w.status_snapshot()["total_opener_stripped"] == 1
    assert w.status_snapshot()["opener_guard_enabled"] is True


@pytest.mark.asyncio
async def test_worker_leaves_text_when_not_repeated_or_not_strippable():
    svc = _FakeSvc(_out_rows("Ha, that's fun", "See you"))
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(f"c-{uuid.uuid4().hex[:8]}", "Ha, tell me more about it")]
    await w._tick()
    assert sent == ["Ha, tell me more about it"] and w.total_opener_stripped == 0
    svc = _FakeSvc(_out_rows("I think yes", "I think no"))
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(f"c-{uuid.uuid4().hex[:8]}", "I think so too, honestly")]
    await w._tick()
    assert sent == ["I think so too, honestly"]
    assert w.total_opener_repeat_seen == 1 and w.total_opener_stripped == 0


@pytest.mark.asyncio
async def test_worker_opener_monitor_alerts_once_per_key(monkeypatch):
    """P1 #340 抽样监控：同一开场键命中达 alert_after → ops_alert 一次；榜单进快照。"""
    from src.ops import ops_alert as _oa
    calls = []
    monkeypatch.setattr(_oa, "notify", lambda kind, text, **kw: calls.append((kind, text, kw)))
    svc = _FakeSvc(_out_rows("Ha, that's fun", "Haha! I know"))
    sent = []
    w = _worker(svc, sent)
    w._opener_guard_cfg["alert_after"] = 3
    for i in range(4):
        svc.queue = [_draft(f"c-{uuid.uuid4().hex[:8]}", "Ha, tell me more about it", draft_id=f"d{i}")]
        await w._tick()
    assert w.total_opener_stripped == 4
    assert len(calls) == 1 and calls[0][0] == "opener_repeat" and "ha" in calls[0][1]
    snap = w.status_snapshot()
    assert snap["opener_repeat_top"][0] == ("ha", 4)
    # alert_after<=0 → 只计数不喊
    w2 = _worker(_FakeSvc(_out_rows("Ha, a", "Ha, b")), [])
    w2._opener_guard_cfg["alert_after"] = 0
    calls.clear()
    for i in range(3):
        w2._svc.queue = [_draft(f"c-{uuid.uuid4().hex[:8]}", "Ha, tell me more about it", draft_id=f"e{i}")]
        await w2._tick()
    assert calls == [] and w2._opener_hits.get("ha") == 3


@pytest.mark.asyncio
async def test_worker_guard_disabled_noop():
    svc = _FakeSvc(_out_rows("Ha, that's fun", "Haha! I know"))
    sent = []
    w = _worker(svc, sent, enabled=False)
    svc.queue = [_draft(f"c-{uuid.uuid4().hex[:8]}", "Ha, tell me more about it")]
    await w._tick()
    assert sent == ["Ha, tell me more about it"]
    assert w.status_snapshot()["opener_guard_enabled"] is False


@pytest.mark.asyncio
async def test_worker_true_retry_still_stripped():
    svc = _FakeSvc(_out_rows("Ha, that's fun", "Haha! I know"))
    sent = []
    calls = {"n": 0}

    async def _send(platform, account_id, chat_key, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient boom")
        sent.append(text)
        return {"ok": True}

    w = _worker(svc, sent, send_cb=_send,
                config={"recoverable": {"enabled": True, "max_attempts": 3,
                                        "backoff_base_sec": 0, "backoff_max_sec": 0}})
    svc.queue = [_draft(f"c-{uuid.uuid4().hex[:8]}", "Ha, tell me more about it")]
    await w._tick()
    assert sent == [] and w.total_opener_stripped == 1
    await w._tick()
    # 重试不豁免：判定幂等，重发的仍是摘掉开场后的文本
    assert sent == ["Tell me more about it"] and w.total_opener_stripped == 2
