# -*- coding: utf-8 -*-
"""P-1 A（#259 #254 · U4ZAD7 / Y3C6PS / ZH3ZQ5）：起草即净化——草稿 = 将发文本。

钉住：
  - apply_draft_humanize：先 claim_guard 再 humanize；em dash 落草稿前已成逗号；meta 计数；
    [draft] 每稿一行 punct_fix=n style_fix=n；verbatim / manual / human 不净化（humanize=skip）；
  - enrich_draft 真库：AI 稿带 —、3 句 → 落库文本无 —、≤2 句；ZH3ZQ5 开场白零历史 → 中性句，
    历史含 hiking → 放行（只去破折号）；
  - 发送门兜底：已净化文本 punct_fix=0 无告警；带 — 的文本 → [outbound] gate_leak 告警一行；
  - AI 指纹计数：四格 rate / level（0 绿 / <2 黄 / ≥2 红）+ JSONL 回填；
  - care_dispatcher 静态钉：净化挂在 _avoid_disliked 之后、事实校验之前，verbatim 分支不经过。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from src.inbox import ai_fingerprint_stats as fp
from src.inbox.outbound_humanize import apply_draft_humanize, apply_outbound_humanize

ROOT = Path(__file__).resolve().parents[1]
ZH3ZQ5 = "I was just thinking about that hiking trail you mentioned a while back \u2014 did you ever end up going?"


@pytest.fixture(autouse=True)
def _fp_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv(fp.ENV_DIR, str(tmp_path / "fp"))
    fp._reset_for_tests(persist=True)
    yield
    fp._reset_for_tests(persist=False)


def test_apply_draft_humanize_claim_then_punct(caplog):
    caplog.set_level(logging.INFO)
    out, meta = apply_draft_humanize(ZH3ZQ5, conversation_id="whatsapp:a:b", draft_id="d1", lang="en",
                                     history_texts=[], memory_facts=[], cfg_root={})
    assert "\u2014" not in out and "mentioned" not in out
    assert meta["claim_action"] == "rewrite" and meta["skipped"] is False
    # 有锚点 → 只去破折号
    out2, meta2 = apply_draft_humanize(ZH3ZQ5, conversation_id="whatsapp:a:b", lang="en",
                                       history_texts=["we went hiking last month"], cfg_root={})
    assert "hiking trail you mentioned" in out2 and "\u2014" not in out2 and ", did you" in out2
    assert meta2["claim_action"] == "pass" and meta2["punct_fix"] == 1 and meta2["dash"] == 1
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[draft]")]
    assert len(lines) == 2
    assert "claim=rewrite" in lines[0] and "claim=pass" in lines[1] and "punct_fix=1" in lines[1]
    assert any("[claim-guard]" in r.getMessage() and "anchored=0 anchor=- action=rewrite" in r.getMessage()
               for r in caplog.records)


def test_apply_draft_humanize_trims_and_counts_style():
    txt = ("Just got back from the market; the tomatoes were unreal! At the end of the day, "
           "it's the small things, isn't it? What are you up to today?")
    out, meta = apply_draft_humanize(txt, lang="en", cfg_root={})
    assert ";" not in out and "isn't it" not in out and out.endswith("?")
    assert meta["punct_fix"] >= 1 and (meta["style_fix"] + meta["trimmed"]) >= 1


def test_bypass_origins_not_humanized(caplog):
    caplog.set_level(logging.INFO)
    raw = "I wrote this \u2014 with my own dash; keep it."
    for org in ("verbatim", "manual", "human", "identity"):
        out, meta = apply_draft_humanize(raw, origin=org, conversation_id="c", lang="en", cfg_root={})
        assert out == raw and meta["skipped"] is True
    assert sum(1 for r in caplog.records if "humanize=skip" in r.getMessage()) == 4
    # None 透传；配置关 → 原样
    assert apply_draft_humanize(None)[0] is None
    out, meta = apply_draft_humanize(raw, cfg_root={"inbox": {"l2_autosend": {"humanize": False}}})
    assert out == raw and meta["skipped"]


# ── enrich_draft 真库 ───────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    from src.inbox.store import InboxStore
    s = InboxStore(tmp_path / "p1a.db")
    yield s
    s.close()


def _conv(cid="whatsapp:acct1:u1", ck="u1"):
    return {"conversation_id": cid, "platform": "whatsapp", "account_id": "acct1",
            "chat_key": ck, "display_name": "T"}


def _seed(store, cid, texts):
    from src.inbox.models import InboxMessage
    now = time.time()
    for i, (direction, t) in enumerate(texts):
        store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id=f"m{i}", direction=direction,
                                          text=t, ts=now - (len(texts) - i) * 60.0))


def test_enrich_draft_stores_sanitized_text(store, monkeypatch, caplog):
    from src.inbox import autosend_policy as pol
    from src.inbox.drafts import DraftService
    monkeypatch.setattr(pol, "current_policy_mode", lambda: pol.POLICY_SHADOW)
    caplog.set_level(logging.INFO)
    svc = DraftService(inbox_store=store)

    # ① 零历史（首次接触）+ ZH3ZQ5 开场白 → 中性句、无 —
    _seed(store, "whatsapp:acct1:u1", [("in", "hi")])
    d1 = svc.auto_generate_draft(_conv(), "hi", automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d1, reply_text=ZH3ZQ5, reply_lang="en", automation_mode="auto_ai")
    t1 = store.get_draft(d1)["draft_text"]
    assert "\u2014" not in t1 and "mentioned" not in t1 and "hiking" not in t1 and t1.endswith("?")
    assert any("[claim-guard] conv=whatsapp:acct1:u1" in r.getMessage() and "action=rewrite" in r.getMessage()
               for r in caplog.records)
    assert any(r.getMessage().startswith("[draft] conv=whatsapp:acct1:u1") and "claim=rewrite" in r.getMessage()
               for r in caplog.records)

    # ② 历史里客户提过 hiking → 放行，只去破折号
    _seed(store, "whatsapp:acct1:u2", [("in", "went hiking on that trail near town, so pretty"),
                                        ("out", "sounds lovely"), ("in", "hey")])
    d2 = svc.auto_generate_draft(_conv("whatsapp:acct1:u2", "u2"), "hey", automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d2, reply_text=ZH3ZQ5, reply_lang="en", automation_mode="auto_ai")
    t2 = store.get_draft(d2)["draft_text"]
    assert "hiking trail you mentioned" in t2 and "\u2014" not in t2

    # ③ 三句 + 分号 + 感悟尾 → ≤2 句、无分号
    _seed(store, "whatsapp:acct1:u3", [("in", "morning")])
    d3 = svc.auto_generate_draft(_conv("whatsapp:acct1:u3", "u3"), "morning", automation_mode="auto_ai", enrich=True)
    ai = ("Morning! Just made coffee; it smells amazing. Life is really about these little moments. "
          "What's your plan today?")
    assert svc.enrich_draft(d3, reply_text=ai, reply_lang="en", automation_mode="auto_ai")
    t3 = store.get_draft(d3)["draft_text"]
    assert ";" not in t3 and "Life is really" not in t3 and t3.endswith("What's your plan today?")
    # 发送门再过一遍 → 幂等 punct_fix=0，无 gate_leak
    caplog.clear()
    gate = apply_outbound_humanize(t3, conversation_id="whatsapp:acct1:u3", lang="en", origin="auto",
                                   cfg_root={}, stage="deliver")
    assert gate == t3
    assert any("[outbound] conv=whatsapp:acct1:u3" in r.getMessage() and "punct_fix=0" in r.getMessage()
               for r in caplog.records)
    assert not any("[outbound-leak]" in r.getMessage() for r in caplog.records)


def test_send_gate_leak_warns_once_per_text(caplog):
    caplog.set_level(logging.INFO)
    out = apply_outbound_humanize("Coffee first \u2014 then we talk.", conversation_id="c1", lang="en",
                                  origin="auto", cfg_root={}, stage="deliver")
    assert "\u2014" not in out
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING and "[outbound-leak]" in r.getMessage()]
    assert len(warns) == 1 and "punct_fix=1" in warns[0].getMessage()
    snap = fp.snapshot()
    assert snap["gate_leak"]["hits"] == 1 and snap["gate_leak"]["total"] == 1


# ── AI 指纹四格 ─────────────────────────────────────────────────────────────────

def test_fingerprint_snapshot_levels_and_persistence(tmp_path):
    now = time.time()
    for _ in range(100):
        fp.record_draft(0, ts=now - 10)
    snap = fp.snapshot(now=now)
    assert snap["dash"] == {"hits": 0, "total": 100, "rate_pct": 0.0, "level": "green"}
    fp.record_draft(3, ts=now - 5)
    snap = fp.snapshot(now=now)
    assert snap["dash"]["hits"] == 1 and snap["dash"]["total"] == 101 and snap["dash"]["level"] == "yellow"
    for _ in range(5):
        fp.record_draft(1, ts=now - 4)
    assert fp.snapshot(now=now)["dash"]["level"] == "red"
    # 客服腔 / 引用 / 承诺
    fp.record_service_tone("clean", ts=now); fp.record_service_tone("rewrite", ts=now)
    fp.record_claim("pass", ts=now); fp.record_claim("rewrite", ts=now)
    snap = fp.snapshot(now=now)
    assert snap["service_tone"]["hits"] == 1 and snap["service_tone"]["total"] == 2 and snap["service_tone"]["level"] == "red"
    assert snap["claim_unanchored"]["hits"] == 1 and snap["claim_unanchored"]["total"] == 2
    assert snap["promise_no_action"]["total"] == 0 and snap["promise_no_action"]["level"] == "na" \
        and snap["promise_no_action"]["ready"] is False
    fp.record("promise_checked", ts=now); fp.record("promise_no_action", ts=now)
    assert fp.snapshot(now=now)["promise_no_action"]["ready"] is True
    # 窗口外不计
    fp.record_draft(1, ts=now - 30 * 3600)
    assert fp.snapshot(hours=24, now=now)["dash"]["total"] == 106
    # 磁盘回填：清进程内 → snapshot 从 JSONL 恢复
    files = list(fp.stats_dir().glob("fp_*.jsonl"))
    assert files
    fp._reset_for_tests(persist=True)
    snap2 = fp.snapshot(now=now)
    assert snap2["dash"]["total"] == 106 and snap2["service_tone"]["hits"] == 1
    assert snap2["level"] == "red"


def test_service_tone_guard_counts_at_draft_not_gate():
    from src.utils.persona_guard import rewrite_service_tone
    now = time.time()
    rewrite_service_tone("I hear you. Take care!")
    rewrite_service_tone("coffee first.")
    rewrite_service_tone("I hear you. Take care!", record_stats=False)
    snap = fp.snapshot(now=now + 1)
    assert snap["service_tone"]["total"] == 2 and snap["service_tone"]["hits"] == 1


# ── 接线静态钉 ───────────────────────────────────────────────────────────────

def test_wiring_static():
    drafts = (ROOT / "src" / "inbox" / "drafts.py").read_text(encoding="utf-8")
    assert drafts.count("apply_draft_humanize(") == 1
    i_late = drafts.index("_guard_late_reply_excuses(reply, draft, draft_id)")
    i_h = drafts.index("apply_draft_humanize(")
    i_dec = drafts.index("effective_risk = _max_risk(base_risk, reply_risk)")
    assert i_late < i_h < i_dec
    care = (ROOT / "src" / "contacts" / "care_dispatcher.py").read_text(encoding="utf-8")
    assert care.count("apply_draft_humanize(") == 1
    i_v = care.index("reply = care_verbatim_text(item)")
    i_d = care.index("reply = await self._avoid_disliked(prompt, reply, sid)")
    i_c = care.index("apply_draft_humanize(")
    i_f = care.index("verdict = check_care_reply(reply, profile, first_real_send=first_send)")
    assert i_v < i_d < i_c < i_f
    oh = (ROOT / "src" / "inbox" / "outbound_humanize.py").read_text(encoding="utf-8")
    assert "rewrite_service_tone(src, record_stats=False)" in oh
