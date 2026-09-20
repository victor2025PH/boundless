# -*- coding: utf-8 -*-
"""主动触达 P5 门禁（2026-07-29，checkin 效能门控 + 趋势渲染）。

锁四条不变量：
  1. 配置解析：默认关；max_skip 夹 [0, 0.9]。
  2. 跳过概率纯函数：样本不足/富臂无信号恒 0；比例语义 + 封顶；
     checkin 不差于富开场 → 0（绝不无据惩罚）。
  3. 确定性掷签：同会话同日恒定（tick 重掷=把跳过磨成延迟，必须日级确定）；
     大数弱收敛于概率。
  4. collect 端到端：真 store 两臂聚合与引擎/看板同口径；富臂不足自动 0
     （「等待期就是样本门槛」的机制自证）。
  5. 趋势渲染：收敛/回潮判词方向正确，空文件不抛。
"""

from __future__ import annotations

import time

from src.companion.proactive_mode_gate import (
    checkin_skip_probability,
    collect_mode_gate,
    parse_mode_gate_cfg,
    should_skip_checkin,
)

_ON = parse_mode_gate_cfg({"mode_gate": {"enabled": True}})


def _noon_today() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0,
                        lt.tm_wday, lt.tm_yday, -1))


# ── 1. 配置解析 ─────────────────────────────────────────────────────────

def test_parse_defaults_disabled_and_clamps():
    cfg = parse_mode_gate_cfg({})
    assert cfg["enabled"] is False
    assert cfg["min_checkin_sent"] == 30 and cfg["min_rich_sent"] == 15
    assert cfg["max_skip"] == 0.5
    over = parse_mode_gate_cfg({"mode_gate": {"max_skip": 2.0}})
    assert over["max_skip"] == 0.9  # 手滑也留 10% 探索量


# ── 2. 跳过概率 ─────────────────────────────────────────────────────────

def test_skip_prob_gates_and_semantics():
    off = parse_mode_gate_cfg({})
    assert checkin_skip_probability(0.1, 100, 0.5, 100, off) == (0.0, "disabled")
    # 任一臂样本不足 → 0
    assert checkin_skip_probability(0.1, 10, 0.5, 100, _ON)[1] == "insufficient"
    assert checkin_skip_probability(0.1, 100, 0.5, 5, _ON)[1] == "insufficient"
    # 富臂无回复 → 不迁怒 checkin
    assert checkin_skip_probability(0.1, 100, 0.0, 20, _ON) == (0.0, "no_signal")
    # checkin 0.1 vs rich 0.4：1-0.25=0.75 → 封顶 0.5
    p, r = checkin_skip_probability(0.1, 100, 0.4, 20, _ON)
    assert r == "ok" and p == 0.5
    # checkin 0.3 vs rich 0.4：跳 25%
    p, _ = checkin_skip_probability(0.3, 100, 0.4, 20, _ON)
    assert abs(p - 0.25) < 1e-9
    # checkin 不差 → 0
    assert checkin_skip_probability(0.5, 100, 0.4, 20, _ON)[0] == 0.0


# ── 3. 确定性掷签 ───────────────────────────────────────────────────────

def test_should_skip_deterministic_same_day():
    t0 = _noon_today()
    for cid in ("tg:a:1", "tg:a:2", "tg:a:3"):
        first = should_skip_checkin(cid, 0.5, now=t0)
        # 同日任何时刻重掷结果不变（否则 15min tick 会把跳过磨成延迟）
        for dh in (1, 3, 7):
            assert should_skip_checkin(cid, 0.5, now=t0 + dh * 3600) == first


def test_should_skip_rate_roughly_matches_prob():
    t0 = _noon_today()
    n = 2000
    hits = sum(
        1 for i in range(n)
        if should_skip_checkin(f"cid:{i}", 0.5, now=t0))
    assert 0.40 * n < hits < 0.60 * n  # 弱收敛即可（crc32 均匀性）
    assert not any(
        should_skip_checkin(f"cid:{i}", 0.0, now=t0) for i in range(50))
    assert all(
        should_skip_checkin(f"cid:{i}", 1.0, now=t0) for i in range(50))


# ── 4. collect 端到端 ───────────────────────────────────────────────────

def _seed(tmp_path, *, rich_n=20, rich_replied=10,
          checkin_n=40, checkin_replied=4):
    from src.inbox.models import InboxConversation, InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    d1 = now - 86400

    def _one(cid, note, ts, replied):
        store.record_outreach(cid, batch_id="proactive_topic:text",
                              note=note, ts=ts)
        if replied:
            store.ingest_batch(
                InboxConversation(conversation_id=cid, platform="telegram",
                                  chat_key=cid.split(":")[-1]),
                [InboxMessage(conversation_id=cid, platform_msg_id=f"r{cid}",
                              direction="in", text="回", ts=ts + 300)])

    for i in range(checkin_n):
        _one(f"tg:a:c{i}", "gentle_checkin", d1 + i, i < checkin_replied)
    for i in range(rich_n):
        note = "follow_up" if i % 2 == 0 else "life_share"
        _one(f"tg:a:r{i}", note, d1 + 7200 + i, i < rich_replied)
    return store, now


def test_collect_mode_gate_end_to_end(tmp_path):
    store, now = _seed(tmp_path)  # checkin 0.1 vs rich 0.5 → 1-0.2=0.8 → 封顶 0.5
    snap = collect_mode_gate(store, _ON, now=now)
    assert snap["checkin"]["sent"] == 40
    assert abs(snap["checkin"]["rate"] - 0.1) < 0.01
    assert snap["rich"]["sent"] == 20
    assert abs(snap["rich"]["rate"] - 0.5) < 0.01
    assert snap["rich"]["modes"] == {"follow_up": 10, "life_share": 10}
    assert snap["skip_prob"] == 0.5 and snap["reason"] == "ok"


def test_collect_waits_for_rich_arm(tmp_path):
    # 富臂只有 3 发（<15）——修复初期的真实形态：机制在场但不动作
    store, now = _seed(tmp_path, rich_n=3, rich_replied=2)
    snap = collect_mode_gate(store, _ON, now=now)
    assert snap["reason"] == "insufficient" and snap["skip_prob"] == 0.0
    assert collect_mode_gate(None, _ON) == {}


# ── 5. 趋势渲染 ─────────────────────────────────────────────────────────

def _trend_row(ts, spam, checkin_sent, rich_sent, streak3):
    return {
        "data_root": "X", "ts": ts, "days": 7.0, "spam_now": spam,
        "spam_prev": 0,
        "mode_ab": {
            "gentle_checkin": {"sent": checkin_sent, "responded": 1,
                               "response_rate": 0.1},
            "follow_up": {"sent": rich_sent, "responded": 2,
                          "response_rate": 0.5},
        },
        "kind_ab": {"text": {"sent": 10, "responded": 3, "response_rate": 0.3},
                    "voice": {"sent": 2, "responded": 1, "response_rate": 0.5}},
        "backoff": {"entries": 10, "streak1": 5, "streak2": 2,
                    "streak3plus": streak3},
        "optout_muted": 0,
    }


def test_render_trend_verdicts():
    from scripts.proactive_review import render_trend
    now = time.time()
    good = render_trend([
        _trend_row(now - 7 * 86400, 44, 48, 0, 5),
        _trend_row(now, 6, 10, 8, 2),
    ])
    assert "收敛" in good and "44" in good and "6" in good
    bad = render_trend([
        _trend_row(now - 7 * 86400, 5, 10, 8, 1),
        _trend_row(now, 30, 10, 8, 4),
    ])
    assert "回潮" in bad and "streak3+" in bad
    assert render_trend([])  # 空文件不抛
