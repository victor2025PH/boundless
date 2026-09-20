# -*- coding: utf-8 -*-
"""#144（0902 skuio 工单）门禁：出站近重复守卫「跨轮误拦」+「拦下后静默收尾」。

事故（HXP9YD 日志 13386/13455，WA 英文/西语两会话）：客户发新消息 → AI 回复与
119s/88s 前**上一轮**的回复相近（sim 0.55/0.59）→ similar 档拦下 → 重写没救回
→ 静默收尾，两会话此后到 12:11 零后续（YEBHMS 日志）。

承诺修法：
  ① similar 档只比对「本稿所答的最新入站」之后的出站——上一轮的回复与本稿相近
     是两轮各答一次，不是双发；同 burst 双稿仍拦；dup 档（≥0.90 原样复读）不变；
  ② 拦下后绝不静默：重写链结局打 WARNING；重写失败且客户有新入站在等 → 会话打
     「需人工」（reason=dup_guard_blocked）进待处理清单；
  ③ 观测：拦截计数拆同轮 / 跨轮，跨轮放行单独计数。
"""
from __future__ import annotations

import logging
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.inbox.outbound_dup_guard import (  # noqa: E402
    attempt_dup_rewrite,
    latest_inbound_ts,
    near_duplicate_of_recent,
    near_duplicate_report,
    peer_awaiting_reply,
)

NOW = 2_000_000.0
# 事故原句（13386）：上一轮回复 vs 本轮回复——开头相同、内容词高重合（similar 档）
PREV_REPLY = "Oh, sorry! I just said the cat in your sticker looks cute, like it's daydreaming."
NEW_REPLY = "Oh, sorry! I just meant the cat in your sticker looks so cute and sleepy."


def _rows_two_rounds():
    """两轮对话：客户 A → 回复 R1 → 客户 B（新入站）；本稿答 B。"""
    return [
        {"direction": "in", "text": "what did you say about my sticker?", "ts": NOW - 200},
        {"direction": "out", "text": PREV_REPLY, "ts": NOW - 119},
        {"direction": "in", "text": "again? which cat?", "ts": NOW - 30},
    ]


# ── ① 纯函数：similar 档只比对本轮 ───────────────────────────────────────────

def test_similar_hit_without_since_boundary_is_legacy_behavior():
    hit = near_duplicate_of_recent(NEW_REPLY, _rows_two_rounds(), now=NOW)
    assert hit and hit["level"] == "similar"


def test_cross_round_similar_released_with_since_boundary():
    rows = _rows_two_rounds()
    since = latest_inbound_ts(rows, before_ts=NOW - 20 + 2.0)   # 本稿 created=NOW-20
    assert since == NOW - 30
    rep = near_duplicate_report(NEW_REPLY, rows, now=NOW, similar_since_ts=since)
    assert rep["hit"] is None                       # 不拦
    cr = rep["cross_round_similar"]
    assert cr and cr["level"] == "similar" and cr["same_round"] is False
    assert cr["matched_text"].startswith("Oh, sorry!")
    assert near_duplicate_of_recent(
        NEW_REPLY, rows, now=NOW, similar_since_ts=since) is None


def test_same_burst_similar_still_blocked():
    """同一 burst：客户连发两条 → R1 已发（晚于最新入站）→ 相近的 R2 仍拦。"""
    rows = [
        {"direction": "in", "text": "hey are you there", "ts": NOW - 40},
        {"direction": "in", "text": "hello??", "ts": NOW - 38},
        {"direction": "out", "text": PREV_REPLY, "ts": NOW - 10},
    ]
    since = latest_inbound_ts(rows, before_ts=NOW - 35 + 2.0)
    assert since == NOW - 38
    rep = near_duplicate_report(NEW_REPLY, rows, now=NOW, similar_since_ts=since)
    assert rep["hit"] and rep["hit"]["level"] == "similar"
    assert rep["hit"]["same_round"] is True
    assert rep["cross_round_similar"] is None


def test_dup_level_ignores_since_boundary():
    """dup 档不变：跨轮原样复读（≥0.90）仍拦，且标 same_round=False 供拆计数。"""
    rows = _rows_two_rounds()
    since = latest_inbound_ts(rows, before_ts=NOW - 20 + 2.0)
    rep = near_duplicate_report(PREV_REPLY, rows, now=NOW, similar_since_ts=since)
    assert rep["hit"] and rep["hit"]["level"] == "dup"
    assert rep["hit"]["same_round"] is False


def test_latest_inbound_ts_and_peer_awaiting_reply_edge_cases():
    assert latest_inbound_ts(None) == 0.0
    assert latest_inbound_ts([{"direction": "out", "ts": 5}]) == 0.0
    assert latest_inbound_ts([{"direction": "in", "ts": "bad"}, {"direction": "in", "ts": 7}]) == 7
    # before_ts 之后的入站不算（创建后才到的属下一轮，fresh_guard 管）
    assert latest_inbound_ts(
        [{"direction": "in", "ts": 7}, {"direction": "in", "ts": 9}], before_ts=8) == 7
    assert peer_awaiting_reply(None) is False
    assert peer_awaiting_reply([{"direction": "in", "ts": 1}, {"direction": "out", "ts": 2}]) is False
    assert peer_awaiting_reply([{"direction": "out", "ts": 1}, {"direction": "in", "ts": 2}]) is True
    # 失败留痕不算「已回复」
    assert peer_awaiting_reply([
        {"direction": "in", "ts": 1},
        {"direction": "out", "ts": 2, "status": "failed"}]) is True


# ── ② 重写链结局可见 + since 透传 ────────────────────────────────────────────

async def test_rewrite_outcomes_logged_at_warning(caplog):
    async def boom(text, matched):
        raise RuntimeError("llm down")

    cfg = {"enabled": True, "window_sec": 180.0, "block_similar": True,
           "rewrite_retry": True}
    rows = _rows_two_rounds()
    hit = {"level": "similar", "matched_text": PREV_REPLY, "similarity": 0.55}
    with caplog.at_level(logging.WARNING, logger="src.inbox.outbound_dup_guard"):
        out = await attempt_dup_rewrite(
            text=NEW_REPLY, hit=hit, rows=rows, cfg=cfg, rewrite_fn=boom,
            source="autosend", now=NOW, conv_id="whatsapp:a:b")
        assert out is None
        # 无 rewrite_fn 也要有结局（skipped），不能静默
        out2 = await attempt_dup_rewrite(
            text=NEW_REPLY, hit=hit, rows=rows, cfg=cfg, rewrite_fn=None,
            source="autosend", now=NOW, conv_id="whatsapp:a:b")
        assert out2 is None
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("outcome=attempted" in m and "whatsapp:a:b" in m for m in msgs)
    assert any("outcome=error" in m and "llm down" in m for m in msgs)
    assert any("outcome=skipped" in m for m in msgs)


async def test_rewrite_recheck_honours_since_boundary():
    """重写稿再核与首检同 since：换过说法的重写稿不能被上一轮回复再拦一次。"""
    rows = _rows_two_rounds()
    since = NOW - 30
    # 与上一轮回复共享 ≥12 归一化字符的开头（similar 档「共同前缀」判据）
    rewritten = "Oh, sorry! I just said the sticker cat looked adorable, that's all."

    async def rw(text, matched):
        return rewritten

    cfg = {"enabled": True, "window_sec": 180.0, "block_similar": True,
           "rewrite_retry": True}
    hit = {"level": "similar", "matched_text": PREV_REPLY, "similarity": 0.6}
    # 无 since → 重写稿与上一轮回复同开头 → still_dup
    assert await attempt_dup_rewrite(
        text=NEW_REPLY, hit=hit, rows=rows, cfg=cfg, rewrite_fn=rw, now=NOW) is None
    # 有 since → 上一轮不参与 similar → rescued
    assert await attempt_dup_rewrite(
        text=NEW_REPLY, hit=hit, rows=rows, cfg=cfg, rewrite_fn=rw, now=NOW,
        similar_since_ts=since) == rewritten


# ── B 线 worker 端到端 ────────────────────────────────────────────────────────

class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.tags = {}
        self.meta = {}

    def list_recent_messages(self, cid, limit=8):
        return list(self.rows)

    def get_conv_tags(self, cid):
        return list(self.tags.get(cid, []))

    def set_conv_tags(self, cid, tags):
        self.tags[cid] = list(tags)

    def set_handoff_meta(self, cid, meta):
        self.meta[cid] = meta


class _FakeSvc:
    def __init__(self, store):
        self.queue = []
        self._store = store

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        return {"ok": True}


def _conv():
    return f"whatsapp:acc144:{uuid.uuid4().hex[:10]}"


def _draft(conv, text, created_ts, draft_id="d1"):
    plat, acc, ck = conv.split(":")
    return {
        "draft_id": draft_id, "autopilot_level": "L2", "final_text": text,
        "platform": plat, "account_id": acc, "chat_key": ck,
        "conversation_id": conv, "created_at": created_ts,
        "peer_text": "again? which cat?",
    }


def _worker(svc, sent, **cfg_extra):
    from src.inbox.autosend_worker import AutosendWorker

    async def _send_cb(platform, account_id, chat_key, text):
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    guard = {"enabled": True, "window_sec": 180.0, "block_similar": True,
             "rewrite_retry": True}
    guard.update(cfg_extra)
    return AutosendWorker(draft_service=svc, send_callback=_send_cb,
                          sleep=_sleep, dup_guard_cfg=guard)


@pytest.mark.asyncio
async def test_worker_sends_reply_to_new_inbound_despite_similar_prev_round():
    """验收①：英文会话连续两轮客户新消息、回复相近 → 第二条照发（跨轮放行计数）。"""
    now = time.time()
    conv = _conv()
    store = _FakeStore([
        {"direction": "in", "text": "what did you say about my sticker?", "ts": now - 200},
        {"direction": "out", "text": PREV_REPLY, "ts": now - 119},
        {"direction": "in", "text": "again? which cat?", "ts": now - 30},
    ])
    svc = _FakeSvc(store)
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, NEW_REPLY, now - 20)]
    await w._tick()
    assert sent == [NEW_REPLY]
    assert w.total_dup_blocked == 0
    assert w.total_dup_cross_round_released == 1
    assert store.tags == {}                    # 正常发出，不打标
    snap = w.status_snapshot()
    assert snap["total_dup_cross_round_released"] == 1
    assert snap["total_dup_blocked_same_round"] == 0


@pytest.mark.asyncio
async def test_worker_still_blocks_same_burst_double_send():
    """验收②：同一 burst 双稿仍拦（同轮计数），客户已有回复 → 不打「需人工」。"""
    now = time.time()
    conv = _conv()
    store = _FakeStore([
        {"direction": "in", "text": "hey are you there", "ts": now - 40},
        {"direction": "in", "text": "hello??", "ts": now - 38},
        {"direction": "out", "text": PREV_REPLY, "ts": now - 10},
    ])
    svc = _FakeSvc(store)
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, NEW_REPLY, now - 35, "d2")]
    await w._tick()
    assert sent == []
    assert w.total_dup_blocked == 1
    assert w.total_dup_blocked_same_round == 1
    assert w.total_dup_blocked_cross_round == 0
    assert w.total_dup_cross_round_released == 0
    assert store.tags == {}                    # 最新一条是我方出站＝客户已有回复


@pytest.mark.asyncio
async def test_worker_blocked_and_rewrite_failed_tags_needs_human(caplog):
    """验收③：拦截且重写失败、客户有新入站在等 → 会话进待处理清单并标近重复拦截。"""
    now = time.time()
    conv = _conv()
    # 跨轮**原样复读**（dup 档，不受 since 影响）→ 拦；无 rewrite_fn → 救不回
    store = _FakeStore([
        {"direction": "in", "text": "what did you say?", "ts": now - 100},
        {"direction": "out", "text": PREV_REPLY, "ts": now - 90},
        {"direction": "in", "text": "sorry what?", "ts": now - 30},
    ])
    svc = _FakeSvc(store)
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(conv, PREV_REPLY, now - 20, "d3")]
    with caplog.at_level(logging.WARNING):
        await w._tick()
    assert sent == []
    assert w.total_dup_blocked == 1
    assert w.total_dup_blocked_cross_round == 1
    assert w.total_dup_blocked_flagged == 1
    from src.integrations.protocol_autoreply import HANDOFF_TAG
    assert store.tags.get(conv) == [HANDOFF_TAG]
    assert store.meta[conv]["reason"] == "dup_guard_blocked"
    assert store.meta[conv]["source"] == "system"
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("outcome=skipped" in m for m in msgs)          # 重写结局可见
    assert any("dup_guard_blocked" in m and conv in m for m in msgs)


@pytest.mark.asyncio
async def test_worker_rescued_rewrite_is_sent_and_not_tagged():
    now = time.time()
    conv = _conv()
    store = _FakeStore([
        {"direction": "in", "text": "hey", "ts": now - 40},
        {"direction": "out", "text": PREV_REPLY, "ts": now - 10},
        {"direction": "in", "text": "hello??", "ts": now - 5},
    ])
    rewritten = "Haha no worries, I was only talking about the sleepy sticker cat."

    async def rw(text, matched):
        return rewritten

    svc = _FakeSvc(store)
    sent = []
    w = _worker(svc, sent, rewrite_fn=rw)
    # 本稿答 now-5 的入站，但 R1（now-10）早于它 → similar 不比对；用 dup 档验证重写链
    svc.queue = [_draft(conv, PREV_REPLY, now - 3, "d4")]
    await w._tick()
    assert sent == [rewritten]
    assert w.total_dup_rewritten == 1 and w.total_dup_blocked == 0
    assert store.tags == {}


# ── 接线 ratchet ─────────────────────────────────────────────────────────────

def test_wiring_ratchet_144():
    worker = (REPO / "src/inbox/autosend_worker.py").read_text(encoding="utf-8")
    assert "_dup_guard_report" in worker
    assert "similar_since_ts" in worker
    assert 'reason="dup_guard_blocked"' in worker
    assert "total_dup_cross_round_released" in worker
    tg = (REPO / "src/client/telegram_client.py").read_text(encoding="utf-8")
    assert "similar_since_ts=_dg_since" in tg            # A 线同修
    assert 'reason="dup_guard_blocked"' in tg
    html = (REPO / "src/web/templates/unified_inbox.html").read_text(encoding="utf-8")
    assert "dup_guard_blocked:'inbox.handoff.r_dup_guard_blocked'" in html
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    assert "近重复拦截" in ZH["inbox.handoff.r_dup_guard_blocked"]
    assert "near-duplicate" in EN["inbox.handoff.r_dup_guard_blocked"]
