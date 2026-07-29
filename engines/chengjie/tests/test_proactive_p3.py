# -*- coding: utf-8 -*-
"""主动触达 P3 门禁（2026-07-29，媒体形态反哺）。

锁四条不变量：
  1. 配置解析：默认关（新子系统约定）；坏值回默认；边界夹紧。
  2. 倍率纯函数：双臂样本不足不动；sqrt 软化 + [max_cut, max_boost] 夹紧；
     text 基线零回复的边界语义；有效概率夹 [0.02, 0.95]。
  3. collect：真 store 端到端——outreach_log 三臂回复率 → 倍率，与看板同口径；
     lookback 窗外触达不计。
  4. 只换形态不换总量：反哺只改 probability，其余闸门（min_intimacy/length）
     语义原样（经 verdict 复核）。
"""

from __future__ import annotations

import time

from src.companion.proactive_media_feedback import (
    collect_media_feedback,
    effective_media_probability,
    media_probability_factor,
    parse_media_feedback_cfg,
)
from src.companion.proactive_topic import (
    photo_share_verdict,
    voice_gate_verdict,
)

_ON = parse_media_feedback_cfg({"media_feedback": {"enabled": True}})


# ── 1. 配置解析 ─────────────────────────────────────────────────────────

def test_parse_defaults_disabled():
    cfg = parse_media_feedback_cfg({})
    assert cfg["enabled"] is False  # 新子系统默认关，overlay 显式开
    assert cfg["min_sent_per_arm"] == 8
    assert cfg["max_boost"] == 1.5 and cfg["max_cut"] == 0.5


def test_parse_bad_values_and_clamps():
    cfg = parse_media_feedback_cfg({"media_feedback": {
        "enabled": True, "min_sent_per_arm": "bad",
        "max_boost": 0.3, "max_cut": 0.01,
    }})
    assert cfg["min_sent_per_arm"] == 8   # 坏值回默认
    assert cfg["max_boost"] == 1.0        # boost 不得 <1
    assert cfg["max_cut"] == 0.1          # cut 保底 0.1


# ── 2. 倍率纯函数 ───────────────────────────────────────────────────────

def test_factor_disabled_and_insufficient():
    off = parse_media_feedback_cfg({})
    assert media_probability_factor(0.5, 100, 0.3, 100, off) == (1.0, "disabled")
    # 任一臂样本不足 → 不动（冷启动照配置探索）
    assert media_probability_factor(0.5, 3, 0.3, 100, _ON)[1] == "insufficient"
    assert media_probability_factor(0.5, 100, 0.3, 3, _ON)[1] == "insufficient"


def test_factor_sqrt_and_clamps():
    # 同回复率 → 1.0
    f, r = media_probability_factor(0.36, 20, 0.36, 20, _ON)
    assert r == "ok" and abs(f - 1.0) < 1e-9
    # 回复率减半 → sqrt(0.5)≈0.707
    f, _ = media_probability_factor(0.18, 20, 0.36, 20, _ON)
    assert abs(f - 0.7071) < 0.001
    # 4 倍 → sqrt(4)=2 → 封顶 1.5
    f, _ = media_probability_factor(0.8, 20, 0.2, 20, _ON)
    assert f == 1.5
    # 归零 → 保底 0.5
    f, _ = media_probability_factor(0.0, 20, 0.4, 20, _ON)
    assert f == 0.5


def test_factor_zero_text_baseline():
    # text 全无回应 + 该形态有回应 → 顶格；双双为零 → 无信号不动
    assert media_probability_factor(0.2, 20, 0.0, 20, _ON) == (1.5, "ok")
    assert media_probability_factor(0.0, 20, 0.0, 20, _ON) == (1.0, "ok")


def test_effective_probability_clamped():
    assert effective_media_probability(0.5, 1.5) == 0.75
    assert effective_media_probability(0.5, 0.5) == 0.25
    assert effective_media_probability(0.9, 1.5) == 0.95   # 上限防机械
    assert effective_media_probability(0.01, 0.5) == 0.02  # 下限保探索
    assert effective_media_probability(0.5, 1.0) == 0.5


# ── 3. collect 端到端（真 store）────────────────────────────────────────

def _seed_store(tmp_path):
    from src.inbox.models import InboxConversation, InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    # 10 条 text 触达：3 条得到回复（rate 0.3）
    for i in range(10):
        cid = f"tg:a:t{i}"
        conv = InboxConversation(
            conversation_id=cid, platform="telegram", chat_key=str(i))
        msgs = []
        sent_ts = now - 2 * 86400 + i * 60
        if i < 3:
            msgs.append(InboxMessage(
                conversation_id=cid, platform_msg_id=f"r{i}",
                direction="in", text="回复", ts=sent_ts + 600))
        if msgs:
            store.ingest_batch(conv, msgs)
        else:
            store.ingest_batch(conv, [InboxMessage(
                conversation_id=cid, platform_msg_id=f"o{i}",
                direction="out", text="开场", ts=sent_ts)])
        store.record_outreach(cid, batch_id="proactive_topic:text",
                              note="gentle_checkin", ts=sent_ts)
    # 10 条 voice 触达：6 条得到回复（rate 0.6 → factor sqrt(2)≈1.414）
    for i in range(10):
        cid = f"tg:a:v{i}"
        conv = InboxConversation(
            conversation_id=cid, platform="telegram", chat_key=f"v{i}")
        sent_ts = now - 2 * 86400 + i * 60
        if i < 6:
            store.ingest_batch(conv, [InboxMessage(
                conversation_id=cid, platform_msg_id=f"vr{i}",
                direction="in", text="回复", ts=sent_ts + 600)])
        else:
            store.ingest_batch(conv, [InboxMessage(
                conversation_id=cid, platform_msg_id=f"vo{i}",
                direction="out", text="开场", ts=sent_ts)])
        store.record_outreach(cid, batch_id="proactive_topic:voice",
                              note="gentle_checkin", ts=sent_ts)
    # photo 只有 2 条（样本不足 → factor 1.0）
    for i in range(2):
        cid = f"tg:a:p{i}"
        conv = InboxConversation(
            conversation_id=cid, platform="telegram", chat_key=f"p{i}")
        store.ingest_batch(conv, [InboxMessage(
            conversation_id=cid, platform_msg_id=f"po{i}",
            direction="out", text="开场", ts=now - 86400)])
        store.record_outreach(cid, batch_id="proactive_topic:photo",
                              note="follow_up", ts=now - 86400)
    return store, now


def test_collect_media_feedback_end_to_end(tmp_path):
    store, now = _seed_store(tmp_path)
    snap = collect_media_feedback(store, _ON, now=now)
    assert snap["text"]["sent"] == 10 and snap["text"]["factor"] == 1.0
    assert snap["voice"]["sent"] == 10
    assert abs(snap["voice"]["rate"] - 0.6) < 0.01
    assert abs(snap["voice"]["factor"] - 1.414) < 0.01  # sqrt(0.6/0.3)
    assert snap["photo"]["reason"] == "insufficient"
    assert snap["photo"]["factor"] == 1.0


def test_collect_lookback_excludes_old(tmp_path):
    store, now = _seed_store(tmp_path)
    # 30 天前再塞 10 条 voice 全无回复——lookback=14 天必须无视它们
    for i in range(10):
        store.record_outreach(
            f"tg:a:old{i}", batch_id="proactive_topic:voice",
            note="gentle_checkin", ts=now - 30 * 86400)
    snap = collect_media_feedback(store, _ON, now=now)
    assert snap["voice"]["sent"] == 10  # 窗外不计
    assert abs(snap["voice"]["factor"] - 1.414) < 0.01


def test_collect_no_store_safe():
    assert collect_media_feedback(None, _ON) == {}


# ── 4. 只换形态不换总量：其余闸门语义不动 ───────────────────────────────

def test_adjusted_probability_keeps_other_gates():
    # 反哺后 probability=0.75：长度闸仍然先拒
    v = {"enabled": True, "min_chars": 4, "max_chars": 80, "probability": 0.75}
    assert voice_gate_verdict(v, "嗯", 0.0)[1] == "length"
    # min_intimacy 仍然先拒（boost 不放生客进照片池）
    p = {"enabled": True, "min_intimacy": 20, "probability": 0.375}
    assert photo_share_verdict(
        p, mode="follow_up", intimacy=5, rand01=0.0)[1] == "min_intimacy"
    # 概率抬升后中签（rand 0.5 < 0.75）
    ok, why = voice_gate_verdict(v, "你好呀朋友", 0.5)
    assert ok and why == "ok"
