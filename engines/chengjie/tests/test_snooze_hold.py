# -*- coding: utf-8 -*-
"""「搁置并停止自动回复」门禁（实施49 P1-12，2026-08-20）。

内测反馈 B17：坐席点「搁置」只是把会话移出待接管队列，**AI 照常自动回复**——
「我先不管这个人」的真实意思是「也别让 AI 去搭话」。本批把静音做成搁置面板上的
一个显式勾选，实现复用 takeover_rearm 的 source 编码术。

本文件钉住四组不变量（每组都对应一个「错了就是事故」的场景）：

1. **原档必须活着**：静音写 ``snooze_hold_from:<原档>``，取消搁置还原到它——
   还原成全局默认会把「这个客户是全自动」悄悄降成「人审」，坐席不会察觉。
2. **只还原自己按下的那次**：坐席期间自己改过档 / 守卫降档 / 转接管，一律不动
   （更新的意图优先）。反向误伤比不还原更糟：把坐席刚设的 manual 改回全自动 =
   AI 抢答真人正在处理的会话。
3. **接管不得吞掉搁置静音**：搁置静音期坐席随手发一条消息 → record_agent_takeover
   若覆盖 source，「别管它」就被降级成「30 分钟后自动接回」；且自动接回 sweep
   永远不许扫到搁置静音行（那是显式决定，不该被超时推翻）。
4. **状态可见**：``snooze_hold_state`` 是回复区常驻 pill 的唯一数据源——没有它，
   「静音不会自动解除」这个刻意设计就变成了隐形事故。
"""
from __future__ import annotations

import time

from src.inbox.snooze_hold import (
    SNOOZE_HOLD_SOURCE,
    apply_snooze_hold,
    is_snooze_hold_source,
    release_snooze_hold,
    restore_mode_for,
    snooze_hold_prev_mode,
    snooze_hold_state,
)
from src.inbox.store import InboxStore
from src.inbox.takeover_rearm import record_agent_takeover, sweep_takeover_rearm

_CFG_REVIEW = {"inbox": {"auto_draft": {"automation_mode": "review"}}}


# ── 纯函数：source 词汇 ────────────────────────────────────────────────


def test_source_vocabulary_round_trip():
    assert is_snooze_hold_source(SNOOZE_HOLD_SOURCE)
    assert is_snooze_hold_source("snooze_hold_from:auto_ai")
    assert snooze_hold_prev_mode("snooze_hold_from:auto_ai") == "auto_ai"
    # 邻居词汇不得误认（还原只认自己写的那种）
    for other in ("human", "takeover", "takeover_from:auto_ai", "guard:budget",
                  "rearm", "sweep", "bootstrap", "", None):
        assert not is_snooze_hold_source(other), other
    # 垃圾/不存在的档位 → 无记录（回落全局默认，绝不写进 set_automation_mode）
    assert snooze_hold_prev_mode("snooze_hold_from:nonsense") == ""
    assert snooze_hold_prev_mode(SNOOZE_HOLD_SOURCE) == ""


def test_restore_mode_prefers_recorded_then_global_default():
    assert restore_mode_for("snooze_hold_from:auto_ai", _CFG_REVIEW) == "auto_ai"
    # 无记录 → 全局默认
    assert restore_mode_for(SNOOZE_HOLD_SOURCE, _CFG_REVIEW) == "review"
    # 全局默认本身就是 manual → 无事可做（不写一次「manual→manual」的假还原）
    assert restore_mode_for(
        SNOOZE_HOLD_SOURCE,
        {"inbox": {"auto_draft": {"automation_mode": "manual"}}}) == ""


# ── 静音写入（真 InboxStore）───────────────────────────────────────────


def test_hold_records_prev_mode(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:1"
    store.set_automation_mode(cid, "auto_ai", source="human")
    src = apply_snooze_hold(store, cid)
    assert src == "snooze_hold_from:auto_ai"
    meta = store.get_automation_mode_meta(cid)
    assert meta["mode"] == "manual" and meta["source"] == src
    store.close()


def test_reschedule_does_not_wash_prev_mode(tmp_path):
    """改期/重复搁置必须原样保留首次记录的原档——否则第二次会记成 manual，
    取消搁置时「还原」就变成把全自动会话钉死在人审。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:2"
    store.set_automation_mode(cid, "auto_ai", source="human")
    apply_snooze_hold(store, cid)
    assert apply_snooze_hold(store, cid) == "snooze_hold_from:auto_ai"
    assert release_snooze_hold(store, cid, _CFG_REVIEW) == "auto_ai"
    store.close()


def test_hold_over_takeover_pierces_to_original_mode(tmp_path):
    """先接管（已 manual）再搁置：真原档在 takeover_from: 里，必须穿透取出。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:3"
    store.set_automation_mode(cid, "auto_ai", source="human")
    record_agent_takeover(store, cid)          # → manual / takeover_from:auto_ai
    assert apply_snooze_hold(store, cid) == "snooze_hold_from:auto_ai"
    assert release_snooze_hold(store, cid, _CFG_REVIEW) == "auto_ai"
    store.close()


def test_hold_without_explicit_row_falls_back_to_global_default(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:4"
    assert apply_snooze_hold(store, cid) == SNOOZE_HOLD_SOURCE
    assert release_snooze_hold(store, cid, _CFG_REVIEW) == "review"
    assert store.get_automation_mode_meta(cid)["mode"] == "review"
    store.close()


def test_old_store_signature_degrades_honestly():
    """旧 store / 假件无 source 形参：仍切 manual，但返回空串（不谎报已打标）——
    路由据此回执 muted=false，前端于是不会说「已停 AI」。"""
    class OldStore:
        def __init__(self):
            self.calls = []

        def set_automation_mode(self, cid, mode):
            self.calls.append((cid, mode))

    s = OldStore()
    assert apply_snooze_hold(s, "c:x") == ""
    assert s.calls == [("c:x", "manual")]


# ── 释放：只还原自己按下的那次 ────────────────────────────────────────


def test_release_ignores_other_sources(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    for cid, source in (("c:human", "human"), ("c:guard", "guard:budget"),
                        ("c:tko", "takeover_from:auto_ai")):
        store.set_automation_mode(cid, "manual", source=source)
        assert release_snooze_hold(store, cid, _CFG_REVIEW) == ""
        assert store.get_automation_mode_meta(cid)["mode"] == "manual"
    store.close()


def test_release_ignores_agent_override_during_snooze(tmp_path):
    """搁置期坐席自己把档位拨回全自动 → 取消搁置不得再写一次（更新的意图优先）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:5"
    store.set_automation_mode(cid, "review", source="human")
    apply_snooze_hold(store, cid)
    store.set_automation_mode(cid, "auto_ai", source="human")   # 坐席改档
    assert release_snooze_hold(store, cid, _CFG_REVIEW) == ""
    assert store.get_automation_mode_meta(cid)["mode"] == "auto_ai"
    store.close()


def test_release_is_idempotent(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:6"
    store.set_automation_mode(cid, "auto_ai", source="human")
    apply_snooze_hold(store, cid)
    assert release_snooze_hold(store, cid, _CFG_REVIEW) == "auto_ai"
    assert release_snooze_hold(store, cid, _CFG_REVIEW) == ""   # 第二次无事可做
    store.close()


# ── 与接管/自动接回的边界 ─────────────────────────────────────────────


def test_takeover_does_not_swallow_snooze_hold(tmp_path):
    """搁置静音期坐席手动发一条消息：source 必须保持 snooze_hold_*，
    否则「别管它」被悄悄降级成「30 分钟后 AI 自动接回」。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:7"
    store.set_automation_mode(cid, "auto_ai", source="human")
    apply_snooze_hold(store, cid)
    assert record_agent_takeover(store, cid) == "snooze_hold_from:auto_ai"
    assert store.get_automation_mode_meta(cid)["source"] == "snooze_hold_from:auto_ai"
    store.close()


def test_rearm_sweep_never_restores_snooze_hold(tmp_path):
    """自动接回 sweep 只扫 source LIKE 'takeover%'——搁置静音是显式决定，
    不该被超时推翻。这条同时钉住 store 侧的 LIKE 口径不被顺手放宽。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.set_automation_mode("c:hold", "auto_ai", source="human")
    apply_snooze_hold(store, "c:hold")
    store.set_automation_mode("c:tko", "auto_ai", source="human")
    record_agent_takeover(store, "c:tko")
    with store._lock:      # 两行都做旧，只有接管那条该被接回
        store._conn.execute(
            "UPDATE conversation_settings SET updated_at=?", (time.time() - 7200,))
        store._conn.commit()
    cfg = dict(_CFG_REVIEW)
    cfg["inbox"] = dict(cfg["inbox"])
    cfg["inbox"]["takeover_rearm"] = {"enabled": True, "after_minutes": 30}
    res = sweep_takeover_rearm(store, cfg)
    assert res["restored"] == 1 and res["restored_cids"] == ["c:tko"]
    assert store.get_automation_mode_meta("c:hold")["mode"] == "manual"
    store.close()


# ── 可见化（回复区常驻 pill 的数据源）─────────────────────────────────


def test_hold_state_snapshot_only_for_own_rows(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:8"
    store.set_automation_mode(cid, "auto_ai", source="human")
    apply_snooze_hold(store, cid)
    st = snooze_hold_state(store.get_automation_mode_meta(cid), _CFG_REVIEW)
    assert st and st["restore_mode"] == "auto_ai" and st["held_at"] > 0
    # 非搁置静音态一律 None（前端不渲染，pill 不会张冠李戴到接管上）
    assert snooze_hold_state({"mode": "manual", "source": "takeover"}, _CFG_REVIEW) is None
    assert snooze_hold_state({"mode": "auto_ai", "source": SNOOZE_HOLD_SOURCE},
                             _CFG_REVIEW) is None
    assert snooze_hold_state(None, _CFG_REVIEW) is None
    store.close()
