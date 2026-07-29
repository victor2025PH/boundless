# -*- coding: utf-8 -*-
"""主动触达 P4 门禁（2026-07-29，mode 级回复率观测 + 验收 CLI）。

锁三条不变量：
  1. store.outreach_note_response_stats：按 note 分桶的回复率与
     outreach_response_stats 同判定口径（窗口/lookback/仅 sent）。
  2. 验收 CLI collect_review：只读出数（mode/kind 回复率、事故模板句前后窗
     对比、退避直方图、opt-out 数），坏根软失败不抛。
  3. CLI 口径与 store 口径双向钉住（同一批种子数据两边读数一致）。
"""

from __future__ import annotations

import json
import time

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore


def _conv(cid: str) -> InboxConversation:
    return InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key=cid.split(":")[-1])


def _seed(tmp_path):
    """3 checkin（1 回）+ 2 follow_up（2 回）+ 窗外/failed 各 1 + 模板句出站。"""
    store = InboxStore(tmp_path / "config" / "inbox.db")
    now = time.time()
    d1 = now - 86400
    # gentle_checkin ×3：仅 1 条得到回复
    for i, replied in enumerate((True, False, False)):
        cid = f"tg:a:c{i}"
        store.record_outreach(cid, batch_id="proactive_topic:text",
                              note="gentle_checkin", ts=d1 + i * 60)
        if replied:
            store.ingest_batch(_conv(cid), [InboxMessage(
                conversation_id=cid, platform_msg_id=f"r{i}",
                direction="in", text="在呢", ts=d1 + i * 60 + 600)])
    # follow_up ×2（voice 形态）：都回了
    for i in range(2):
        cid = f"tg:a:f{i}"
        store.record_outreach(cid, batch_id="proactive_topic:voice",
                              note="follow_up", ts=d1 + 7200 + i * 60)
        store.ingest_batch(_conv(cid), [InboxMessage(
            conversation_id=cid, platform_msg_id=f"fr{i}",
            direction="in", text="哈哈对", ts=d1 + 7200 + i * 60 + 300)])
    # 窗外（30 天前）与 failed：不得计入
    store.record_outreach("tg:a:old", batch_id="proactive_topic:text",
                          note="gentle_checkin", ts=now - 30 * 86400)
    store.record_outreach("tg:a:bad", batch_id="proactive_topic:text",
                          note="gentle_checkin", status="failed", ts=d1)
    # 事故模板句：本窗 2 条、上一窗 5 条（收敛趋势）
    spam_conv = _conv("tg:a:spam")
    msgs = []
    for i in range(2):
        msgs.append(InboxMessage(
            conversation_id="tg:a:spam", platform_msg_id=f"s{i}",
            direction="out", text="嘿，好久没联系啦，最近还好吗", ts=d1 + i))
    for i in range(5):
        msgs.append(InboxMessage(
            conversation_id="tg:a:spam", platform_msg_id=f"sp{i}",
            direction="out", text="好久没联系，你最近怎么样",
            ts=now - 20 * 86400 + i))
    store.ingest_batch(spam_conv, msgs)
    return store, now


# ── 1. store 口径 ───────────────────────────────────────────────────────

def test_note_response_stats_semantics(tmp_path):
    store, now = _seed(tmp_path)
    out = store.outreach_note_response_stats(
        "proactive_topic:", response_window_days=3.0,
        lookback_days=14.0, now=now)
    assert out["gentle_checkin"]["sent"] == 3       # 窗外/failed 不计
    assert out["gentle_checkin"]["responded"] == 1
    assert abs(out["gentle_checkin"]["response_rate"] - 0.3333) < 0.001
    assert out["follow_up"] == {
        "sent": 2, "responded": 2, "response_rate": 1.0}
    assert store.outreach_note_response_stats("") == {}


def test_note_response_window_cutoff(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    cid = "tg:a:slow"
    store.record_outreach(cid, batch_id="proactive_topic:text",
                          note="gentle_checkin", ts=now - 10 * 86400)
    # 回复来得太晚（触达后 5 天 > 3 天窗）→ 不算回复
    store.ingest_batch(_conv(cid), [InboxMessage(
        conversation_id=cid, platform_msg_id="late",
        direction="in", text="迟到的回复", ts=now - 5 * 86400)])
    out = store.outreach_note_response_stats(
        "proactive_topic:", response_window_days=3.0,
        lookback_days=14.0, now=now)
    assert out["gentle_checkin"] == {
        "sent": 1, "responded": 0, "response_rate": 0.0}


# ── 2/3. 验收 CLI（与 store 同一批种子数据，口径双向钉住）──────────────

def test_collect_review_end_to_end(tmp_path):
    store, now = _seed(tmp_path)
    # 账本 + optout 文件
    cfg_dir = tmp_path / "config"
    (cfg_dir / "companion_proactive_cooldown.json").write_text(json.dumps({
        "tg:a:c0": {"ts": now, "streak": 1, "last_text": "x"},
        "tg:a:c1": {"ts": now, "streak": 2, "last_text": "y"},
        "tg:a:c2": {"ts": now, "streak": 5, "last_text": "z"},
        "tg:a:legacy": now - 100.0,
    }), "utf-8")
    (cfg_dir / "companion_optout_mute.json").write_text(json.dumps({
        "tg:a:m1": {"ts": now - 10, "until": now + 86400, "hit": "别再发了"},
        "tg:a:m2": {"ts": now - 10, "until": now - 5, "hit": "过期"},
    }), "utf-8")

    from scripts.proactive_review import collect_review, render_review
    rep = collect_review(tmp_path, days=14.0, now=now)
    # 与 store 口径一致
    st_out = store.outreach_note_response_stats(
        "proactive_topic:", response_window_days=3.0,
        lookback_days=14.0, now=now)
    assert rep["mode_ab"]["gentle_checkin"]["sent"] == \
        st_out["gentle_checkin"]["sent"]
    assert rep["mode_ab"]["follow_up"]["responded"] == 2
    assert rep["kind_ab"]["voice"]["sent"] == 2
    assert rep["kind_ab"]["voice"]["response_rate"] == 1.0
    # 事故模板句前后窗
    assert rep["spam_now"] == 2 and rep["spam_prev"] == 5
    # 账本直方图（legacy float 记 streak1）
    assert rep["backoff"] == {
        "entries": 4, "streak1": 2, "streak2": 1, "streak3plus": 1}
    assert rep["optout_muted"] == 1  # 过期条目不计
    # 渲染不抛且含关键行
    txt = render_review(rep)
    assert "收敛" in txt and "follow_up" in txt


def test_collect_review_bad_root_soft(tmp_path):
    from scripts.proactive_review import collect_review, render_review
    rep = collect_review(tmp_path / "nonexistent", days=14.0)
    assert rep["mode_ab"] == {} and rep["spam_now"] == -1
    assert render_review(rep)  # 空报告也可渲染
