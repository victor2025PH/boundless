# -*- coding: utf-8 -*-
"""Q-17 #277② 门禁：风控三级分级 + commitment 只评出站 + 摘标冷却（``src/inbox/risk_grader.py``）。

事故（Cameron 15635715247）：04:39:45 人工摘标 → 04:50:12 客户一句「Trying to show you the pictures they
have sent me…」→ ``keyword_risk_hits`` 对客户入站也跑 detect_commitment / detect_commitment_claim →
``commitment:media`` high → 第三次「需人工」。

硬门禁（指令 E 段）：
  · 15 句客户**叙述**（地址 / 电话 / 他们发来的照片 / 被偷的钱 / 周末计划）走完整 ``DraftService.auto_generate_draft``
    → **零**「需人工」、零 risk_hold、档位 L2；
  · 5 句真高风险（索钱 / 威胁 / 露骨施压 / 未成年 / 诈骗）→ 打标 + risk_hold + L1（现状不变，威胁 / 未成年 / 诈骗为新表）；
  · 人工摘标后 30 分钟同类不重打（第二句同类 → ``cooldown_skipped=true``）、不同类可打、更高级别可打、31 分钟后可打；
  · 保持期间（标在场 + 持有活跃）再命中只 ``touch`` 不重写 meta；
  · ``keyword_risk_hits`` 旧签名逐字旧口径（Q-2 钉桩「Just need your address」→ high commitment:contact）、
    ``direction="in"`` 只认索要句式 request:<kind>、``direction="out"`` 出站答应语仍 high；
  · 人设 ``boundaries.risk_overrides`` 只对中 / 低类别生效，高类别有地板。
Q-2 ``test_commitment_gate`` / Q-15 ``test_adult_grader_gate`` 与本文件同组进 R 批门禁清单。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import FastAPI, Request   # 顶层导入：__future__ annotations 下 FastAPI 需在模块 globals 解析 Request
from fastapi.testclient import TestClient

from src.ai.chat_assistant_service import quick_analyze, quick_risk
from src.inbox import autosend_policy as pol
from src.inbox import autosend_shadow_log as shadow_log
from src.inbox import risk_grader as rg
from src.inbox import risk_hold
from src.inbox.commitment_guard import detect_request
from src.inbox.drafts import DraftService, keyword_risk_hits
from src.inbox.normalizer import conv_id
from src.inbox.store import InboxStore
from src.integrations.protocol_autoreply import HANDOFF_TAG, clear_needs_human, tag_needs_human

_PLAT, _ACCT = "telegram", "acct1"


@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    d = tmp_path / "shadow"
    monkeypatch.setenv(shadow_log.ENV_DIR, str(d))
    monkeypatch.delenv(pol.ENV_POLICY_MODE, raising=False)
    shadow_log.get_stats().reset()
    shadow_log._reset_pending_for_tests()
    return d


@pytest.fixture
def store(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    yield st
    st.close()


@pytest.fixture
def svc(store, shadow_env, monkeypatch):
    # 人设解析走桩（无 config → None，与生产「账号未绑人设」同路）；域缺省别被进程级 active 污染
    from src.utils import business_domain as bd
    monkeypatch.setattr(bd, "_ACTIVE", None)
    return DraftService(inbox_store=store, risk_fn=quick_risk)


def _conv(ck: str) -> Dict[str, Any]:
    return {"conversation_id": conv_id(_PLAT, _ACCT, ck), "platform": _PLAT,
            "account_id": _ACCT, "chat_key": ck, "display_name": "T"}


def _payload(ck: str) -> Dict[str, Any]:
    return {"platform": _PLAT, "account_id": _ACCT, "chat_key": ck}


# ── ① 15 句叙述：零「需人工」 ────────────────────────────────────────────────

_NARRATIVE = [
    "Trying to show you the pictures they have sent me to find out if you in the picture",   # Cameron 原句
    "my address is on the form they sent, so the parcel should arrive Monday",
    "they asked for my phone number but I refused, felt sketchy",
    "the money he stole from me last year still makes me angry",
    "weekend plans? probably just laundry and a nap honestly",
    "I finally moved, my new address is closer to work now",
    "she sent me pictures of her cat haha, so fluffy",
    "my phone number changed last month, got a new sim",
    "he kept asking for my address so I blocked him",
    "I lost my passport at the airport once, nightmare",
    "we met up with my cousin this weekend and had hotpot",
    "I met my boss today and he was grumpy as usual",
    "the scammer took his savings, so sad for him",
    "I do not give my number to strangers online, sorry not sorry",
    "my friend showed me pictures of her trip, your address book must be full of travelers too",
]


@pytest.mark.parametrize("text", _NARRATIVE)
def test_narrative_15_never_needs_human(svc, store, text):
    ck = "nar_" + str(abs(hash(text)) % 100000)
    conv = _conv(ck)
    cid = conv["conversation_id"]
    did = svc.auto_generate_draft(conv, text, automation_mode="auto_ai")
    assert did, text
    row = store.get_draft(did)
    assert row["autopilot_level"] == "L2", (text, row["autopilot_level"], row["risk_reasons"])
    assert HANDOFF_TAG not in list(store.get_conv_tags(cid) or []), (text, row["risk_reasons"])
    assert risk_hold.active(store, cid) is None, text
    # 分级本身：低 / 中（Q-2 refuse_sent 会把稿行 risk 抬 high，那是 Q-2 的行为，不在本断言）
    a = quick_analyze(text)
    kw_level, kw_hits = keyword_risk_hits(text, direction="in")
    g = rg.grade(text, "in", None, reasons=list(a["risk_reasons"]) + (["keyword"] if kw_level else []),
                 hits=list(a["risk_hits"]) + kw_hits)
    assert g["level"] in ("low", "medium"), (text, g)


def test_narrative_private_word_downgraded_from_high(svc, store):
    """quick_analyze 的 privacy 主因（my address / passport）原本 high → 现降 low，主因改名 privacy_mention。"""
    text = "I lost my passport at the airport once, nightmare"
    a = quick_analyze(text)
    assert a["risk_level"] == "high" and "privacy" in a["risk_reasons"]      # 现状：一刀切 high
    conv = _conv("pv1")
    risk, reasons, info = rg.regrade_inbound(svc, conv, text, "en", "high", list(a["risk_reasons"]),
                                             list(a["risk_hits"]))
    assert risk == "low" and info and info["downgraded"] is True and info["category"] == "privacy"
    assert "privacy" not in reasons and rg.PRIVACY_MENTION_REASON in reasons
    d = pol.decide(risk, reasons, "low", [], info["hits"], automation_mode="auto_ai", platform=_PLAT,
                   conversation_id=conv["conversation_id"], store=store)
    assert not d.review_required and d.level == "L2"


# ── ② 5 句真高风险：打标 + risk_hold + L1（现状不变；威胁 / 未成年 / 诈骗为新表）──────

_HIGH = [
    ("can you send me money on cash app? I'm short this week", "money_request"),
    ("I know where you live, I will find you", "threat"),
    ("send me your nudes right now, don't be shy", "adult"),
    ("I am 15 years old btw, is that ok?", "minor"),
    ("guaranteed returns if you invest with me, double your money in a week", "scam"),
]


@pytest.mark.parametrize("text,category", _HIGH)
def test_true_high_5_still_hands_off(svc, store, text, category):
    ck = "hi_" + str(abs(hash(text)) % 100000)
    conv = _conv(ck)
    cid = conv["conversation_id"]
    did = svc.auto_generate_draft(conv, text, automation_mode="auto_ai")
    assert did
    row = store.get_draft(did)
    assert row["autopilot_level"] == "L1", (text, row)
    assert row["risk_level"] == "high"
    assert HANDOFF_TAG in list(store.get_conv_tags(cid) or []), text
    assert risk_hold.active(store, cid) is not None, text
    hm = store.get_handoff_meta(cid)
    assert hm and hm.get("reason")
    if category != "adult":                     # adult 由 Q-15 打标（reason adult:pressure:*）
        assert hm.get("category") == category and hm.get("level") == "high", hm
        assert any(h.startswith(category) or h.startswith("request:") for h in (hm.get("hits") or [])), hm


def test_grade_true_high_categories_direct():
    assert rg.grade("I know where you live, I will find you", "in")["category"] == "threat"
    assert rg.grade("if you leave I will leak your photos online", "in")["level"] == "high"
    assert rg.grade("I'm 16 and still in middle school", "in")["category"] == "minor"
    assert rg.grade("guaranteed returns, invest with me", "in")["category"] == "scam"
    assert rg.grade("wire me some money please", "in") ["category"] == "money_request"
    # 过去时 / 第三人称叙述不算未成年 / 威胁
    assert rg.grade("when I was 15 I loved this band", "in")["level"] == "low"
    assert rg.grade("my nephew is 12 and obsessed with dinosaurs", "in")["level"] == "low"


# ── ③ 摘标冷却：同类 30 分钟不重打 / 不同类可打 / 更高级别可打 / 到期可打 ──────────

def test_untag_cooldown_same_category_not_retagged(svc, store):
    conv = _conv("cd1")
    cid = conv["conversation_id"]
    did = svc.auto_generate_draft(conv, "I know where you live, I will find you", automation_mode="auto_ai")
    assert did and HANDOFF_TAG in store.get_conv_tags(cid)
    # 坐席手发一条 → 摘标（同 unified_inbox_send_routes._mark_send）→ 冷却 threat/high 30 分钟
    assert clear_needs_human(store, cid, actor="agent_send") is True
    cd = risk_hold.cooldown_record(store, cid)
    assert cd and cd["category"] == "threat" and cd["level"] == "high" and cd["by"] == "agent_send"
    assert risk_hold.active(store, cid) is None
    # 10 分钟后同类再来一句 → 不重打标、不登记持有
    did2 = svc.auto_generate_draft(conv, "you'll regret this, I will find you and hunt you down",
                                   automation_mode="auto_ai")
    assert did2
    assert HANDOFF_TAG not in list(store.get_conv_tags(cid) or [])
    assert risk_hold.active(store, cid) is None
    assert not store.get_handoff_meta(cid)
    # 不同类（索钱）→ 照打
    did3 = svc.auto_generate_draft(conv, "wire me some money please, cash app me now", automation_mode="auto_ai")
    assert did3 and HANDOFF_TAG in store.get_conv_tags(cid)
    assert store.get_handoff_meta(cid).get("category") == "money_request"


def test_cooldown_higher_level_can_retag_and_expires():
    class _KV:
        def __init__(self):
            self.kv: Dict[str, str] = {}

        def get_app_setting(self, k, d=""):
            return self.kv.get(k, d)

        def set_app_setting(self, k, v, updated_by=""):
            self.kv[k] = v
            return True

    st = _KV()
    t0 = 1_800_000_000.0
    rec = risk_hold.cooldown(st, "c1", "request_media", level="medium", by="agent_ack", now=t0)
    assert rec and rec["until"] == t0 + 30 * 60
    # 同类同级 / 更低级 → 挡；更高级 → 放
    assert risk_hold.cooldown_blocks(st, "c1", "request_media", "medium", now=t0 + 60)
    assert risk_hold.cooldown_blocks(st, "c1", "request_media", "low", now=t0 + 60)
    assert risk_hold.cooldown_blocks(st, "c1", "request_media", "high", now=t0 + 60) is None
    # 不同类 → 放；空类别 → 不挡
    assert risk_hold.cooldown_blocks(st, "c1", "threat", "medium", now=t0 + 60) is None
    assert risk_hold.cooldown_blocks(st, "c1", "", "medium", now=t0 + 60) is None
    # 到期
    assert risk_hold.cooldown_blocks(st, "c1", "request_media", "medium", now=t0 + 31 * 60) is None
    assert risk_hold.cooldown_record(st, "c1", now=t0 + 31 * 60) is None
    # 空类别不登记
    assert risk_hold.cooldown(st, "c1", "", now=t0) is None


def test_tag_needs_human_respects_cooldown_and_logs(store, caplog):
    import logging
    import time as _time
    ck = "cd2"
    cid = conv_id(_PLAT, _ACCT, ck)
    t0 = _time.time()          # clear_needs_human 的冷却按真实时钟登记，打标 now 需与之同基
    assert tag_needs_human(store, _payload(ck), reason="high_risk", source="system", now=t0,
                           level="high", category="threat", hits=["threat:i will find you"]) is True
    hm = store.get_handoff_meta(cid)
    assert hm["category"] == "threat" and hm["level"] == "high" and hm["hits"] == ["threat:i will find you"]
    assert clear_needs_human(store, cid, actor="agent_ack") is True
    with caplog.at_level(logging.INFO):
        assert tag_needs_human(store, _payload(ck), reason="high_risk", source="system", now=t0 + 600,
                               level="high", category="threat") is False
    assert any("cooldown_skipped=true" in r.getMessage() and "category=threat" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
    assert HANDOFF_TAG not in store.get_conv_tags(cid)
    assert risk_hold.active(store, cid, now=t0 + 600) is None
    # 到期后可打，且 打标 行带 level= category= cooldown_skipped=false
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert tag_needs_human(store, _payload(ck), reason="high_risk", source="system", now=t0 + 31 * 60,
                               level="high", category="threat") is True
    assert any("cooldown_skipped=false" in r.getMessage() and "level=high" in r.getMessage()
               and "category=threat" in r.getMessage() for r in caplog.records)


def test_system_auto_clear_does_not_start_cooldown(store):
    ck = "cd3"
    cid = conv_id(_PLAT, _ACCT, ck)
    assert tag_needs_human(store, _payload(ck), reason="high_risk", source="system", level="high", category="scam")
    assert clear_needs_human(store, cid, actor="system:startup_sweep") is True
    assert risk_hold.cooldown_record(store, cid) is None
    # 人工摘标才冷却；原因码无类别时按 classify_reason 推（commitment:media → request_media）
    assert tag_needs_human(store, _payload(ck), reason="commitment:media", source="commitment_guard")
    assert clear_needs_human(store, cid, actor="agent_send") is True
    cd = risk_hold.cooldown_record(store, cid)
    assert cd and cd["category"] == "request_media"


# ── ④ 保持期间幂等：标在场 + 持有活跃 → 只 touch，不重写 meta ───────────────────

def test_hold_active_second_hit_only_touches(store):
    ck = "idem1"
    cid = conv_id(_PLAT, _ACCT, ck)
    t0 = 1_800_000_000.0
    assert tag_needs_human(store, _payload(ck), reason="high_risk", source="system", now=t0,
                           level="high", category="threat", hits=["threat:a"]) is True
    meta1 = dict(store.get_handoff_meta(cid))
    assert tag_needs_human(store, _payload(ck), reason="high_risk", source="system", now=t0 + 120,
                           level="high", category="threat", hits=["threat:b"]) is False
    assert store.get_handoff_meta(cid) == meta1                  # 横幅内容不变（不重弹）
    rec = risk_hold.record(store, cid)
    assert rec["set_ts"] == t0 and rec["last_hit"] == "threat:b" and rec["hit_count"] == 2
    assert risk_hold.active(store, cid, now=t0 + 120) == "needs_human"


def _legacy_adult_hold(store, ck: str, *, age_sec: float = 3600.0):
    """1.0.82 遗留形态：adult 持有（24h、非泛因、set 于 age_sec 前）+ 需人工标（adult:explicit）。"""
    import time as _time
    cid = conv_id(_PLAT, _ACCT, ck)
    t0 = _time.time() - age_sec
    risk_hold.set(store, cid, "adult", ["nudes"], ttl_h=24.0, by="adult_grader", now=t0)
    assert tag_needs_human(store, _payload(ck), reason="adult:explicit:nudes", source="adult_grader", now=t0,
                           level="medium", category="adult", hits=["adult:nudes"])
    assert risk_hold.active(store, cid) == "adult" and HANDOFF_TAG in store.get_conv_tags(cid)
    return cid


def test_q27_low_inbound_releases_old_hold_and_next_draft_is_l2(svc, store, caplog):
    """Q-27 #301 C / E 回放②（华哥全自动会话）：旧 adult hold 后下一条 benign 入站 → hold clear（by=low_inbound）
    + 摘标 → 本条稿 L2（不 forced=L1、不继承旧 shadow）。"""
    import logging
    conv = _conv("q27_hold1")
    cid = _legacy_adult_hold(store, "q27_hold1")
    with caplog.at_level(logging.INFO):
        did = svc.auto_generate_draft(conv, "good morning dear, slept well?", automation_mode="auto_ai")
    assert did
    row = store.get_draft(did)
    assert row["autopilot_level"] == "L2", (row["autopilot_level"], row["risk_reasons"])
    assert risk_hold.active(store, cid) is None and risk_hold.record(store, cid)["cleared_by"] == "low_inbound"
    assert HANDOFF_TAG not in store.get_conv_tags(cid) and not store.get_handoff_meta(cid)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[risk_hold] clear" in m and "by=low_inbound" in m for m in msgs), msgs
    assert any("[risk_hold] release" in m and "hold=adult" in m and "trigger=low_inbound" in m for m in msgs), msgs
    assert not any("forced=L1" in m for m in msgs), msgs
    # 系统释放不登记冷却：随后真高风险仍可打标
    assert risk_hold.cooldown_record(store, cid) is None
    did2 = svc.auto_generate_draft(conv, "I know where you live, I will find you", automation_mode="auto_ai")
    assert did2 and store.get_draft(did2)["autopilot_level"] == "L1" and HANDOFF_TAG in store.get_conv_tags(cid)


def test_q27_fresh_hold_not_released_by_same_inbound_low_grade(svc, store):
    """持有是本条链刚设的（<60s）→ 低分级不清它（防 commitment_guard 刚 set 就被清）；stop_contact 也不清。"""
    conv = _conv("q27_hold2")
    cid = conv["conversation_id"]
    risk_hold.set(store, cid, "commitment", ["meet"], by="commitment_guard")
    assert rg.release_hold_on_low(store, cid) == "" and risk_hold.active(store, cid) == "commitment"
    import time as _time
    risk_hold.set(store, cid, "stop_contact", ["stop"], by="stop_contact", now=_time.time() - 600)
    assert rg.release_hold_on_low(store, cid) == "" and risk_hold.active(store, cid) == "stop_contact"
    # 旧的普通持有 → 释放
    risk_hold.set(store, cid, "privacy", ["phone"], by="system", now=_time.time() - 600)
    assert rg.release_hold_on_low(store, cid, category="narrative", hits=["narrative:phone number"]) == "privacy"
    assert risk_hold.active(store, cid) is None


def test_q27_keep_path_low_clears_medium_keeps_with_hold_category(store, caplog):
    """Q-27 #301 C：tag_needs_human 保持路径——level=low → 不保持直接 clear（持有 + 标）；level=medium →
    保持，且日志行带本条 level/category/hits 与持有自身 hold_level/hold_category/hold_hits（不再 `category=- hits=-`）。"""
    import logging
    ck = "q27_keep1"
    cid = _legacy_adult_hold(store, ck)
    with caplog.at_level(logging.INFO):
        assert tag_needs_human(store, _payload(ck), reason="high_risk", source="system",
                               level="medium", category="request_media", hits=["request:media"]) is False
    m = next(r.getMessage() for r in caplog.records if "[needs_human] 保持 " in r.getMessage())
    assert "level=medium" in m and "category=request_media" in m and "hits=request:media" in m
    assert "hold=adult" in m and "hold_category=adult" in m and "hold_hits=adult:nudes" in m and "category=-" not in m
    assert risk_hold.active(store, cid) == "adult" and HANDOFF_TAG in store.get_conv_tags(cid)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert tag_needs_human(store, _payload(ck), reason="high_risk", source="system",
                               level="low", category="", hits=[]) is False
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[needs_human] 保持→clear" in m and "level=low" in m and "hold=adult" in m for m in msgs), msgs
    assert any("[risk_hold] clear" in m and "by=low_inbound" in m for m in msgs), msgs
    assert risk_hold.active(store, cid) is None and HANDOFF_TAG not in store.get_conv_tags(cid)
    assert risk_hold.cooldown_record(store, cid) is None          # 系统动作不登记冷却


# ── ⑤ keyword_risk_hits 方向：旧签名逐字 / in 只认索要 / out 答应语 ──────────────

def test_keyword_risk_hits_legacy_signature_verbatim():
    # Q-2 钉桩（test_commitment_guard）：旧签名仍跑 detect_commitment ∪ detect_commitment_claim
    level, hits = keyword_risk_hits("Just need your address")
    assert level == "high" and "commitment:contact" in hits
    level, hits = keyword_risk_hits("Saturday noon sounds lovely, I'll make sure to have some fresh tea ready")
    assert level == "high" and "commitment:meet" in hits
    # 支付词表不分方向
    for d in (None, "in", "out"):
        assert keyword_risk_hits("send the deposit to account 1234 first", d)[0] == "high"


def test_keyword_risk_hits_inbound_only_request_phrasing():
    # 叙述：三条降 low、不跑 commitment_claim
    level, hits = keyword_risk_hits("they asked for my phone number but I refused", "in")
    assert level == "low" and not any(h.startswith("commitment:") for h in hits), hits
    level, hits = keyword_risk_hits("my address is on the form they sent", "in")
    assert level == "low" and not any(h.startswith("commitment:") for h in hits), hits
    level, hits = keyword_risk_hits("Trying to show you the pictures they have sent me", "in")
    assert level is None and hits == []
    # 索要：request:<kind> medium（钱 high）
    level, hits = keyword_risk_hits("what is your phone number?", "in")
    assert level == "medium" and "request:contact" in hits
    level, hits = keyword_risk_hits("send me a pic?", "in")
    assert level == "medium" and "request:media" in hits
    level, hits = keyword_risk_hits("wanna come over Saturday?", "in")
    assert level == "medium" and "request:meet" in hits           # 承诺兜底正则客户侧只到 medium
    level, hits = keyword_risk_hits("can you wire me some money", "in")
    assert level == "high" and "request:money" in hits
    assert detect_request("Just need your address") == "contact"
    assert detect_request("she sent me pictures of her cat") is None
    assert detect_request("他们要我的电话我没给") is None


def test_keyword_risk_hits_outbound_claim_still_high():
    level, hits = keyword_risk_hits("my address is 42 Elm Street, I'll text you the rest", "out")
    assert level == "high" and "commitment:contact" in hits, hits
    g = rg.grade("Saturday noon sounds lovely, I'll make sure to have some fresh tea ready", "out")
    assert g["level"] == "high" and g["category"] == "request_meet" and "commitment:meet" in g["hits"]


# ── ⑥ 中级：request 只打 risk:medium 不进 needs_human；人设覆写 ─────────────────

def test_medium_request_tags_risk_medium_only(svc, store):
    conv = _conv("md1")
    cid = conv["conversation_id"]
    did = svc.auto_generate_draft(conv, "send me a pic? pretty please", automation_mode="auto_ai")
    assert did
    tags = list(store.get_conv_tags(cid) or [])
    assert rg.MEDIUM_TAG in tags and HANDOFF_TAG not in tags, tags
    assert risk_hold.active(store, cid) is None
    assert store.get_draft(did)["autopilot_level"] == "L2"


def test_persona_overrides_only_medium_low_with_floor():
    p = {"boundaries": {"risk_overrides": {"request_media": "low", "money_request": "low",
                                           "privacy": "high", "bogus": "high", "threat": "低"}}}
    ov = rg.risk_overrides_of(p)
    assert ov == {"request_media": "low", "privacy": "high"}
    assert rg.grade("send me a pic?", "in", p)["level"] == "low"
    assert rg.grade("wire me some money please", "in", p)["level"] == "high"         # 高类别地板
    assert rg.grade("I lost my passport once", "in", p, reasons=["privacy"], hits=["passport"])["level"] == "high"
    tbl = rg.public_table(p)
    ids = [r["id"] for r in tbl]
    assert {"self_harm", "minor", "threat", "money_request", "scam", "adult", "stop_contact",
            "request_meet", "request_media", "request_contact", "privacy", "narrative"} <= set(ids)
    row = next(r for r in tbl if r["id"] == "request_media")
    assert row["effective_level"] == "low" and row["override"] == "low" and row["overridable"] is True
    row = next(r for r in tbl if r["id"] == "threat")
    assert row["effective_level"] == "high" and row["overridable"] is False
    from src.utils.persona_manager import PERSONA_SCHEMA_FIELDS, PROMPT_EXEMPT_FIELDS
    assert "boundaries.risk_overrides" in PROMPT_EXEMPT_FIELDS and "boundaries.risk_overrides" in PERSONA_SCHEMA_FIELDS


def test_classify_reason_table():
    assert rg.classify_reason("adult:pressure:nudes") == ("adult", "high")
    assert rg.classify_reason("adult:explicit:sex") == ("adult", "medium")
    assert rg.classify_reason("commitment:media") == ("request_media", "high")
    assert rg.classify_reason("commitment:money") == ("money_request", "high")
    assert rg.classify_reason("crisis:self_harm") == ("self_harm", "high")
    assert rg.classify_reason("high_risk") == ("", "high")
    assert rg.classify_reason("risk:threat") == ("threat", "high")
    assert rg.classify_reason("empty_reply") == ("", "")


def test_regrade_never_lowers_true_high(svc):
    """真高风险因子在场（stop_contact / self_harm / 索要凭据句式 / 索钱句式 / adult:pressure）→ 一字不降。
    Q-27（#301）：``money`` / ``adult`` 单词与支付词表不再在「不可降」之列（见 test_q27_keyword_only_never_high）。"""
    conv = _conv("th1")
    for text, reasons in (("please stop messaging me", ["stop_contact"]),
                          ("I want to kill myself", ["self_harm"]),
                          ("send me your bank card number", ["credential_or_payment_request", "money"]),
                          ("can you send me money on cash app?", ["money"]),
                          ("send me your nudes right now", ["adult", "adult:pressure", "adult_hit:nudes"])):
        risk, out, info = rg.regrade_inbound(svc, conv, text, "en", "high", reasons, [])
        assert risk == "high", (text, risk, out)
        for r in reasons:
            assert r in out, (text, out)


def test_q27_keyword_only_never_high(svc):
    """Q-27 #301 A：keyword-only 命中不得单独把全自动改 L1——高级必须是「类别 + 句式 / 施压第二信号」。"""
    conv = _conv("kw1")
    # 支付裸词（refund / password / OTP）→ payment_keyword 中级「只标记 + 人审候选」
    risk, out, info = rg.regrade_inbound(svc, conv, "I want a refund now", "en", "high", ["keyword"], ["refund"])
    assert risk == "medium" and info["category"] == "payment_keyword" and info["downgraded"] is True, (risk, out, info)
    assert rg.category_def("payment_keyword")["level"] == "medium" and rg.category_def("payment_keyword")["action"] == "mark_review"
    # 成人单词无施压（adult_grader 缺席时的裸 adult 主因）→ 最多中
    risk, out, info = rg.regrade_inbound(svc, conv, "send nudes", "en", "high", ["adult"], ["nudes"])
    assert risk == "medium" and info["category"] == "adult", (risk, out, info)
    # 钱相关单词无索要句式（bitcoin / paypal 提及）→ 低，主因改名 money_mention
    risk, out, info = rg.regrade_inbound(svc, conv, "bitcoin dropped again today lol", "en", "high",
                                         ["money"], ["bitcoin"])
    assert risk == "low" and "money" not in out and rg.MONEY_MENTION_REASON in out, (risk, out, info)
    # 「bank card」裸词 = 支付词表 → 中级只标记（不是索要句式，不进 L1）
    risk, out, info = rg.regrade_inbound(svc, conv, "I lost my bank card yesterday, such a hassle", "en", "high",
                                         ["money"], ["bank card"])
    assert risk == "medium" and info["category"] == "payment_keyword" and "money" not in out, (risk, out, info)
    # 原 high 却没有任何类别撑着（未知主因）→ 降 low，不进 L1
    risk, out, info = rg.regrade_inbound(svc, conv, "hello there", "en", "high", ["mystery"], [])
    assert risk == "low" and info["downgraded"] is True, (risk, out, info)
    # 高级表逐字：五类 + stop_contact 硬停 —— 一字不放松
    assert {c["id"] for c in rg.CATEGORIES if c["level"] == "high"} == {"self_harm", "minor", "threat", "money_request", "scam"}
    assert rg.TRUE_HIGH_REASONS == {"self_harm", "stop_contact", "credential_or_payment_request"}


# ── ⑦ D 段：回复设置页「风控分级」卡 路由 + i18n 包 ─────────────────────────────

class _FakeCM:
    def __init__(self, tmp_path):
        self.config_path = str(tmp_path / "config" / "config.yaml")
        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        self.config = {"persona_persistence": {"enabled": True}}


@pytest.fixture
def rk_client(tmp_path):
    from src.utils.persona_manager import PersonaManager
    from src.web.routes.reply_settings_routes import register_reply_settings_routes

    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    pm.upsert_profile("p_rk", {"name": "Mia", "boundaries": {"meeting_policy": "soft"}})
    app = FastAPI()

    async def _noop(request: Request):
        return None

    cm = _FakeCM(tmp_path)
    app.state.config_manager = cm
    register_reply_settings_routes(app, page_auth=_noop, api_auth=_noop, templates=None, config_manager=cm)
    try:
        yield TestClient(app), pm
    finally:
        PersonaManager.reset()


def test_rk_route_table_and_overrides_roundtrip(rk_client):
    client, pm = rk_client
    r = client.get("/api/reply-settings/risk-grader")
    assert r.status_code == 200, r.text[:400]
    d = r.json()
    assert d.get("ok") is True and d["enabled"] is True and d["persona_id"] == "", d
    ids = [r["id"] for r in d["categories"]]
    assert ids == [c["id"] for c in rg.CATEGORIES]
    assert set(d["overridable"]) == set(rg.OVERRIDABLE) and d["cooldown_min"] == 30
    assert [p["id"] for p in d["personas"]] == ["p_rk"] and d["personas"][0]["overrides"] == {}
    for r in d["categories"]:
        assert r["words"]["zh"] and r["words"]["en"] and r["action"] and r["level"] in rg.LEVELS

    # 保存：合法覆写 + 空串＝删；地板以下裁到地板由 risk_overrides_of 负责
    out = client.post("/api/reply-settings/risk-grader/overrides",
                      json={"persona_id": "p_rk", "overrides": {"request_media": "low", "complaint": "", "privacy": "high"}}).json()
    assert out["ok"] is True and out["overrides"] == {"request_media": "low", "privacy": "high"}
    assert out["persisted"] is True
    assert pm.get_persona_by_id("p_rk")["boundaries"]["risk_overrides"] == {"request_media": "low", "privacy": "high"}
    assert pm.get_persona_by_id("p_rk")["boundaries"]["meeting_policy"] == "soft"     # 深合并不吞邻键
    row = next(r for r in out["categories"] if r["id"] == "request_media")
    assert row["effective_level"] == "low" and row["override"] == "low"

    d2 = client.get("/api/reply-settings/risk-grader", params={"persona": "p_rk"}).json()
    assert d2["persona_id"] == "p_rk"
    assert d2["personas"][0]["overrides"] == {"request_media": "low", "privacy": "high"}

    # 清空 → 键整段移除
    out2 = client.post("/api/reply-settings/risk-grader/overrides", json={"persona_id": "p_rk", "overrides": {}}).json()
    assert out2["ok"] is True and out2["overrides"] == {}
    assert "risk_overrides" not in pm.get_persona_by_id("p_rk")["boundaries"]


def test_rk_route_rejects_high_category_and_bad_level(rk_client):
    client, pm = rk_client
    for bad in ({"threat": "low"}, {"money_request": "medium"}, {"adult": "low"}, {"request_meet": "banana"}):
        out = client.post("/api/reply-settings/risk-grader/overrides", json={"persona_id": "p_rk", "overrides": bad}).json()
        assert out["ok"] is False and out["errors"][0]["code"] == "bad_enum", bad
    assert "risk_overrides" not in (pm.get_persona_by_id("p_rk").get("boundaries") or {})
    out = client.post("/api/reply-settings/risk-grader/overrides", json={"persona_id": "nope", "overrides": {}}).json()
    assert out["ok"] is False and out["errors"][0]["code"] == "not_found"
    out = client.post("/api/reply-settings/risk-grader/overrides", json={"persona_id": "", "overrides": {}}).json()
    assert out["ok"] is False


def test_rk_i18n_pack_covers_every_category_level_action():
    from src.web.i18n_packs import risk_grader as pack
    for cat in (c["id"] for c in rg.CATEGORIES):
        k = f"inbox.risk.cat_{cat}"
        assert k in pack.ZH and k in pack.EN and k in pack.ZH_HANT, k
    for lv in rg.LEVELS:
        assert f"inbox.risk.lv_{lv}" in pack.ZH and f"rps_rk_lv_{lv}" in pack.ZH
    for act in {c["action"] for c in rg.CATEGORIES}:
        assert f"rps_rk_act_{act}" in pack.ZH and f"rps_rk_act_{act}" in pack.EN and f"rps_rk_act_{act}" in pack.ZH_HANT, act
    assert set(pack.ZH) == set(pack.EN) == set(pack.ZH_HANT)
    for k in ("inbox.risk.tag_medium", "inbox.risk.hold_chip", "inbox.handoff.r_risk", "inbox.handoff.r_risk_hit", "rps_rk_title"):
        assert k in pack.ZH
