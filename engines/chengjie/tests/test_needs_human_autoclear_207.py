# -*- coding: utf-8 -*-
"""#207（L-3 A）「需人工」闭环：AI 回上即摘标 / crisis 不自动摘 / 会话头原因条。

事故（BJZFSX，1.0.74）：Loki 会话 09-05 02:57 被 AutosendWorker dup 拦截后打
「需人工」(reason=dup_guard_blocked)，11:24 AI 自动回复成功，但 clear_needs_human
只在坐席人工发消息时调用 → 红标挂到 09-06 仍在，且与「AI」蓝标同亮，点进无原因无入口。
"""
from __future__ import annotations

import time
from pathlib import Path

from src.inbox.store import InboxStore
from src.integrations.protocol_autoreply import (
    HANDOFF_AUTO_CLEAR_REASONS,
    HANDOFF_TAG,
    auto_clear_needs_human,
    handoff_auto_clearable,
    needs_human_by_reason,
    sweep_stale_needs_human,
    tag_needs_human,
)
from src.inbox.normalizer import conv_id

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_PAYLOAD = {"platform": "whatsapp", "account_id": "17345893506", "chat_key": "13308422244"}
_CID = conv_id("whatsapp", "17345893506", "13308422244")


# ── 判定 ─────────────────────────────────────────────────────────────────────

def test_auto_clearable_whitelist_is_ai_could_not_reply_class():
    assert "dup_guard_blocked" in HANDOFF_AUTO_CLEAR_REASONS
    assert "high_risk" not in HANDOFF_AUTO_CLEAR_REASONS
    assert handoff_auto_clearable({"reason": "dup_guard_blocked", "source": "system", "ts": 1.0})
    assert handoff_auto_clearable({"reason": "send_error", "source": "system", "ts": 1.0})
    # crisis（wellbeing 打的）/ high_risk / 人工 / 无元数据 → 一律保留
    assert not handoff_auto_clearable({"reason": "crisis:self_harm", "source": "wellbeing", "ts": 1.0})
    assert not handoff_auto_clearable({"reason": "high_risk", "source": "system", "ts": 1.0})
    assert not handoff_auto_clearable({"reason": "dup_guard_blocked", "source": "manual", "ts": 1.0})
    assert not handoff_auto_clearable({})
    assert not handoff_auto_clearable(None)


# ── dup 打标 → AI 成功回复 → 标消失 ───────────────────────────────────────────

def test_dup_tag_then_ai_reply_clears(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    assert tag_needs_human(st, _PAYLOAD, reason="dup_guard_blocked", now=1000.0)
    assert HANDOFF_TAG in st.get_conv_tags(_CID)
    assert auto_clear_needs_human(st, _CID, trigger="autosend_delivered")
    assert HANDOFF_TAG not in st.get_conv_tags(_CID)
    assert st.get_handoff_meta(_CID) == {}
    # 幂等：没标了再调 → False，不抛
    assert not auto_clear_needs_human(st, _CID, trigger="autosend_delivered")
    st.close()


def test_crisis_tag_survives_ai_reply(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    assert tag_needs_human(st, _PAYLOAD, reason="crisis:self_harm",
                           source="wellbeing", now=1000.0)
    assert not auto_clear_needs_human(st, _CID, trigger="autosend_delivered")
    assert HANDOFF_TAG in st.get_conv_tags(_CID)
    assert st.get_handoff_meta(_CID)["reason"] == "crisis:self_harm"
    st.close()


def test_high_risk_and_manual_tags_survive(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    assert tag_needs_human(st, _PAYLOAD, reason="high_risk", now=1000.0)
    assert not auto_clear_needs_human(st, _CID, trigger="autoreply_sent")
    assert HANDOFF_TAG in st.get_conv_tags(_CID)
    # 坐席经标签面板手打（无元数据）→ 保留
    cid2 = conv_id("whatsapp", "17345893506", "manual1")
    st.set_conv_tags(cid2, [HANDOFF_TAG, "vip"])
    assert not auto_clear_needs_human(st, cid2, trigger="autoreply_sent")
    assert st.get_conv_tags(cid2) == [HANDOFF_TAG, "vip"]
    st.close()


def test_auto_clear_never_raises_on_broken_store():
    class _Broken:
        def get_conv_tags(self, cid):
            raise RuntimeError("db locked")
    assert not auto_clear_needs_human(_Broken(), "x", trigger="t")
    assert not auto_clear_needs_human(None, "x", trigger="t")


# ── 启动扫描：已消化（末条为出站且晚于打标）→ 摘；客户仍在等 → 留 ────────────

def _seed_conv(st: InboxStore, cid: str, *, last_dir: str, last_ts: float) -> None:
    from src.inbox.models import InboxConversation, InboxMessage
    plat, acct, ck = cid.split(":", 2)
    conv = InboxConversation(conversation_id=cid, platform=plat, account_id=acct,
                             chat_key=ck, display_name=ck, last_text="hi",
                             last_ts=last_ts, unread=0)
    msgs = [
        InboxMessage(conversation_id=cid, direction="in", text="hello",
                     ts=last_ts - 100, platform_msg_id="m1"),
        InboxMessage(conversation_id=cid, direction=last_dir, text="Then sleep easy",
                     ts=last_ts, platform_msg_id="m2"),
    ]
    st.ingest_batch(conv, msgs)


def test_startup_sweep_clears_consumed_keeps_waiting(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    # Loki 形态：02:57 打标，11:24 AI 出站成功 → 已消化
    loki = conv_id("whatsapp", "17345893506", "13308422244")
    _seed_conv(st, loki, last_dir="out", last_ts=now - 3600)
    tag_needs_human(st, _PAYLOAD, reason="dup_guard_blocked", now=now - 7200)
    # 客户仍在等（末条入站）→ 保留
    wait_cid = conv_id("whatsapp", "17345893506", "waiting1")
    _seed_conv(st, wait_cid, last_dir="in", last_ts=now - 600)
    tag_needs_human(st, {"platform": "whatsapp", "account_id": "17345893506",
                         "chat_key": "waiting1"}, reason="dup_guard_blocked", now=now - 900)
    # crisis 即便已有出站也保留
    cr_cid = conv_id("whatsapp", "17345893506", "crisis1")
    _seed_conv(st, cr_cid, last_dir="out", last_ts=now - 60)
    tag_needs_human(st, {"platform": "whatsapp", "account_id": "17345893506",
                         "chat_key": "crisis1"}, reason="crisis:self_harm",
                    source="wellbeing", now=now - 3000)

    res = sweep_stale_needs_human(st, now=now)
    assert res == {"scanned": 3, "cleared": 1, "kept": 2}
    assert HANDOFF_TAG not in st.get_conv_tags(loki)
    assert HANDOFF_TAG in st.get_conv_tags(wait_cid)
    assert HANDOFF_TAG in st.get_conv_tags(cr_cid)
    # 幂等：再扫无变化
    assert sweep_stale_needs_human(st, now=now)["cleared"] == 0
    st.close()


def test_startup_sweep_tolerates_missing_capabilities():
    class _Old:
        pass
    assert sweep_stale_needs_human(_Old()) == {"scanned": 0, "cleared": 0, "kept": 0}
    assert sweep_stale_needs_human(None) == {"scanned": 0, "cleared": 0, "kept": 0}


def test_needs_human_by_reason_splits_counts(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    for i, (reason, src) in enumerate((("dup_guard_blocked", "system"),
                                       ("dup_guard_blocked", "system"),
                                       ("crisis:self_harm", "wellbeing"))):
        cid = conv_id("whatsapp", "a", f"c{i}")
        _seed_conv(st, cid, last_dir="in", last_ts=now - i)
        tag_needs_human(st, {"platform": "whatsapp", "account_id": "a", "chat_key": f"c{i}"},
                        reason=reason, source=src, now=now)
    manual = conv_id("whatsapp", "a", "m")
    _seed_conv(st, manual, last_dir="in", last_ts=now)
    st.set_conv_tags(manual, [HANDOFF_TAG])
    got = needs_human_by_reason(st)
    assert got == {"dup_guard_blocked": 2, "crisis": 1, "manual": 1}
    st.close()


# ── 接线契约（静态）─────────────────────────────────────────────────────────

def test_autosend_worker_calls_auto_clear_once_on_delivery():
    src = (_ENGINE_ROOT / "src" / "inbox" / "autosend_worker.py").read_text(encoding="utf-8")
    assert src.count("auto_clear_needs_human(") == 1, "autosend_worker 只许在投递成功处加一处摘标调用"
    i_ok = src.index("_delivered_ok = True")
    i_call = src.index("auto_clear_needs_human(")
    assert i_ok < i_call, "摘标必须在 _delivered_ok = True 之后（只有真送达才摘）"
    assert 'trigger="autosend_delivered"' in src


def test_protocol_autoreply_hook_clears_on_sent():
    src = (_ENGINE_ROOT / "src" / "integrations" / "protocol_autoreply.py"
           ).read_text(encoding="utf-8")
    assert 'trigger="autoreply_sent"' in src
    assert 'elif res.get("sent"):' in src


def test_frontend_reason_bar_and_badge_exclusion_wired():
    tpl = (_ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
           ).read_text(encoding="utf-8")
    assert "function _handoffBarText(" in tpl
    assert "_handoffBarGo()" in tpl and "_handoffBarAck(" in tpl
    assert "acct-out-banner handoff" in tpl
    assert "_convNeedsHuman(c)?'has-handoff':''" in tpl, "列表行须带 has-handoff 供 AI 徽标降灰"
    css = (_ENGINE_ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css"
           ).read_text(encoding="utf-8")
    assert ".conv-item.has-handoff .conv-handler.h-ai" in css
    assert ".acct-out-banner.handoff" in css


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    for k in ("inbox.handoff.bar", "inbox.handoff.bar_generic", "inbox.handoff.bar_go",
              "inbox.handoff.bar_ack", "inbox.handoff.bar_ack_ok",
              "inbox.handoff.bar_ack_fail", "inbox.handoff.r_crisis"):
        assert k in ZH and k in EN, k
