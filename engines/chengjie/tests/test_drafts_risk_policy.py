# -*- coding: utf-8 -*-
"""#160 风险扣稿全放行（影子台账档）+ 分类器整词化 —— 金标门禁（2026-09-04）。

事故：客户一句完全正面的 "Yes you did because I was happy chatting with you
i didn't want to stop" 被 ``"stop" in t`` 子串命中 → 停止联系 → high → L4 扣稿，
干净的 AI 稿在全自动档下静默不发，用户只看到「全自动不工作」。

两层，**刻意不混在一个断言里**：
  第一层：分类器判定（``_detect_intent`` / ``_detect_risk`` / ``_SENSITIVE_PATTERNS``）
          ——全放行之后分类器的正确性反而更重要：台账数据要拿来定新规则、
          stop_contact 要即时推值守群（误报＝告警疲劳）、风险要转守卫提示喂生成侧。
  第二层：shadow 档的放行行为（``autosend_policy.decide``）——所有输入都放行 L2、
          判中的产出**恰好一条**台账、判为无风险的**零台账**、review 档仍挂起且不进台账。
"""
from __future__ import annotations

import pytest

from src.ai.chat_assistant_service import (
    _detect_intent,
    _detect_risk,
    _detect_risk_detailed,
    _stop_contact_hit,
    quick_analyze,
    quick_risk,
)
from src.inbox.drafts import keyword_risk_hits, keyword_risk_level


# ═══════════════════════════════════════════════════════════════════════
# 第一层：分类器判定
# ═══════════════════════════════════════════════════════════════════════

# 必须判为**无风险**（旧子串匹配的误伤样本 + 事故原句）
_NO_RISK = [
    "Yes you did because I was happy chatting with you i didn't want to stop",
    "i didn't want to stop",
    "can't stop thinking about you",
    "we talked non-stop last night",
    "you look sexy tonight",
    "monday is a bank holiday here",
    "what's your email address",
    "let's have hotpot this weekend",
    "你骗我啦",
    "haha don't stop, keep telling me",
    "I couldn't stop laughing at that",
]


@pytest.mark.parametrize("text", _NO_RISK)
def test_no_risk_samples_are_low(text):
    level, reasons = quick_risk(text)
    assert level == "low", (text, level, reasons)
    assert reasons == [], (text, reasons)
    assert keyword_risk_level(text) is None, (text, keyword_risk_level(text))
    assert _detect_intent(text.lower(), emotion="平稳") != "停止联系", text
    a = quick_analyze(text)
    assert a["risk_hits"] == [], (text, a["risk_hits"])


# 必须**判中**：(文本, 期望 reason, 期望命中词片段)
_MUST_HIT = [
    ("please stop messaging me", "stop_contact", "stop messaging me"),
    ("别再联系我", "stop_contact", "别再联系"),
    ("I want to kill myself", "self_harm", "kill myself"),
    ("send me your bank card number", "credential_or_payment_request", "bank card"),
]


@pytest.mark.parametrize("text,reason,hit_fragment", _MUST_HIT)
def test_must_hit_samples(text, reason, hit_fragment):
    level, reasons, hits = _detect_risk_detailed(
        text, emotion="平稳", intent=_detect_intent(text.lower(), emotion="平稳"))
    assert level == "high", (text, level)
    assert reason in reasons, (text, reasons)
    assert any(hit_fragment in h for h in hits), (text, hits)
    # reasons 与 hits 同序对齐：一条 reason 一个命中词
    assert len(hits) == len(reasons), (reasons, hits)
    # quick_analyze 透出同一批命中词（台账消费口）
    a = quick_analyze(text)
    assert reason in a["risk_reasons"]
    assert any(hit_fragment in h for h in a["risk_hits"]), a["risk_hits"]


def test_ai_reply_payment_phrase_is_high_with_hits():
    """AI 稿含付款/账号话术 → reply_risk=high，并报出命中词。"""
    lvl, hits = keyword_risk_hits("Just transfer to this account and I'll ship it")
    assert lvl == "high"
    assert any("transfer to" in h for h in hits), hits
    lvl2, hits2 = keyword_risk_hits("send the deposit to account 1234 first")
    assert lvl2 == "high"
    assert hits2, hits2
    assert keyword_risk_level("send the deposit to account 1234 first") == "high"


def test_english_terms_hit_when_glued_to_cjk():
    """Python `\\b` 把 CJK 当 \\w：中英混排「请问可以refund吗」必须仍判中（ASCII 边界）。"""
    assert keyword_risk_level("请问可以refund吗") == "high"
    assert quick_risk("他叫我transfer money给他")[0] == "high"
    # 边界仍防住英文粘连：refunds 之类的派生词不被 refund 子串吞掉
    assert keyword_risk_level("xrefundx") is None


def test_keyword_risk_hits_collects_all_and_takes_max():
    lvl, hits = keyword_risk_hits("有优惠吗？付款方式是什么")
    assert lvl == "high"          # 优惠=medium、付款=high → 取最高
    assert "优惠" in hits and "付款" in hits


def test_stop_contact_negation_scrubbed_before_positive_match():
    assert _stop_contact_hit("i didn't want to stop") == ""
    assert _stop_contact_hit("can't stop thinking about you") == ""
    assert _stop_contact_hit("please stop messaging me") == "stop messaging me" or \
        _stop_contact_hit("please stop messaging me").startswith("please stop")
    # 否定式与真正的拒绝同句：仍要抓到拒绝
    assert _stop_contact_hit("I can't stop crying. please stop contacting me") != ""


def test_stop_contact_polite_and_negative_forms():
    for s in ("please stop", "don't contact me again", "unsubscribe",
              "leave me alone", "stop texting me", "do not message me"):
        assert _detect_intent(s, emotion="平稳") == "停止联系", s
    for s in ("stop it you're making me blush", "the bus stop is far",
              "non-stop", "stop by my place sometime"):
        assert _detect_intent(s, emotion="平稳") != "停止联系", s


def test_legacy_two_tuple_detect_risk_kept():
    level, reasons = _detect_risk("I want to kill myself", emotion="平稳", intent="继续聊天")
    assert level == "high" and "self_harm" in reasons


def test_whole_word_boundaries_for_risk_terms():
    # sexy ≠ sex；hotpot 不含任何词；bank holiday ≠ bank card
    assert quick_risk("you look sexy tonight")[0] == "low"
    assert quick_risk("we sent nudes")[0] == "high"
    assert quick_risk("monday is a bank holiday")[0] == "low"
    assert quick_risk("send it to my bank account")[0] == "high"
    # 中文精确短语：「不想活动」不是「不想活」
    assert quick_risk("今天不想活动了，太累")[0] == "low"
    assert quick_risk("我不想活了")[0] == "high"


# ═══════════════════════════════════════════════════════════════════════
# 第二层：shadow 档的放行行为（autosend_policy.decide + DraftService 接线 + 台账）
# ═══════════════════════════════════════════════════════════════════════

import json
from pathlib import Path

from src.inbox import autosend_policy as pol
from src.inbox import autosend_shadow_log as shadow_log
from src.inbox.drafts import DraftService, is_autosend_allowed, risk_to_autopilot
from src.inbox.store import InboxStore


@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    """台账落 tmp、策略走默认 shadow、计数器清零。"""
    d = tmp_path / "shadow"
    monkeypatch.setenv(shadow_log.ENV_DIR, str(d))
    monkeypatch.delenv(pol.ENV_POLICY_MODE, raising=False)
    shadow_log.get_stats().reset()
    shadow_log._reset_pending_for_tests()
    return d


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "policy.db")
    yield s
    s.close()


def _conv(cid="tg:acct1:u1", chat_key="u1"):
    return {"conversation_id": cid, "platform": "telegram",
            "account_id": "acct1", "chat_key": chat_key, "display_name": "T"}


def _decide_text(text, *, reply="", mode="auto_ai", policy=None):
    """把「入站文本 + AI 稿」按 drafts.py 同口径喂进 decide()。"""
    a = quick_analyze(text)
    kw_level, kw_hits = keyword_risk_hits(text)
    peer_risk = pol.max_risk(a["risk_level"], kw_level or "low")
    reasons = list(a["risk_reasons"]) + (["keyword"] if kw_level else [])
    hits = list(a["risk_hits"]) + [h for h in kw_hits if h not in a["risk_hits"]]
    r_level, r_hits = keyword_risk_hits(reply)
    return pol.decide(
        peer_risk, reasons, r_level or "low", ["keyword"] if r_level else [],
        hits + r_hits, automation_mode=mode, policy_mode=policy,
    )


def _lines(d: Path):
    out = []
    if d.exists():
        for p in sorted(d.glob("shadow_*.jsonl")):
            out += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


# 判中五条（含 AI 稿）：shadow 下恰好一条台账；发送行为按 R88 锁定（2026-09-17）：
#   stop_contact / self_harm → L1 人审 + 冻结信号，不回客户（farewell=False）；
#   risk=high 非停联 → L1 人审（risk_high_review）。09-04「全放行」只剩 medium。
_SHADOW_CASES = [
    # (入站文本, AI 稿, 期望 hold_reason(台账), would_hold_level, 期望 level, hard_stop, farewell, review)
    ("please stop messaging me", "", "stop_contact", "L4", "L1", "stop_contact", False, True),
    ("别再联系我", "", "stop_contact", "L4", "L1", "stop_contact", False, True),
    ("I want to kill myself", "", "self_harm", "L4", "L1", "self_harm", False, True),
    ("send me your bank card number", "", "credential_or_payment_request", "L4", "L1", "", False, True),
    ("ok how do I pay", "Just transfer to this account and I'll ship it", "reply_risk", "L4", "L1", "", False, True),
]


@pytest.mark.parametrize("text,reply,reason,would,level,hard,farewell,review", _SHADOW_CASES)
def test_shadow_releases_and_produces_exactly_one_record(
        shadow_env, text, reply, reason, would, level, hard, farewell, review):
    d = _decide_text(text, reply=reply)
    assert d.level == level, (text, d)
    assert d.hard_stop == hard and d.farewell is farewell and d.review_required is review, d
    if level == "L2":
        assert d.hold_reason == "" and d.autosend_allowed is True
    else:
        assert d.held and d.autosend_allowed is False
        assert d.hold_reason == (hard or pol.REVIEW_HOLD_REASON)
    assert d.shadow is not None, "判中却没有影子记录＝台账丢数据"
    assert d.shadow.would_hold_level == would
    assert d.shadow.hold_reason == reason, d.shadow
    assert d.shadow.risk_hits, "命中词必须有，否则一个月后分不清真该拦还是误伤"
    assert d.policy_mode == "shadow"


def test_medium_risk_still_released_with_shadow(shadow_env):
    """D-O1 只收 high / 停联 / 自伤；medium（投诉 / 优惠 / 负面情绪）仍按 09-04 放行进台账。"""
    d = _decide_text("this is a scam, I want a discount or I complain")
    assert d.level == "L2" and d.hold_reason == "" and d.autosend_allowed is True
    assert d.shadow is not None and d.shadow.would_hold_level == "L3"
    assert d.hard_stop == "" and d.review_required is False


def test_stop_contact_second_time_no_second_farewell(shadow_env):
    """会话已冻结（调用方传 conversation_frozen=True）→ 仍不回客户，AI 稿 L4 不发。"""
    d = _decide_text("never write me again, please")
    assert d.level == "L1" and d.farewell is False and d.hard_stop == "stop_contact"
    assert d.review_required is True
    a = quick_analyze("never write me again, please")
    d2 = pol.decide(a["risk_level"], a["risk_reasons"], risk_hits=a["risk_hits"],
                    automation_mode="auto_ai", policy_mode="shadow", conversation_frozen=True)
    assert d2.level == "L4" and d2.farewell is False and d2.hard_stop == "stop_contact"
    assert d2.hold_reason == "stop_contact" and d2.shadow is not None
    # 自伤：首次也不回客户（L1），已冻结 → 仍 L1 人审
    b = quick_analyze("I want to kill myself")
    d3 = pol.decide(b["risk_level"], b["risk_reasons"], risk_hits=b["risk_hits"],
                    automation_mode="auto_ai", policy_mode="shadow", conversation_frozen=True)
    assert d3.level == "L1" and d3.hold_reason == "self_harm" and d3.hard_stop == "self_harm"
    assert pol.HARD_STOP_REASONS == ("stop_contact", "self_harm")


@pytest.mark.parametrize("text", _NO_RISK)
def test_shadow_no_risk_inputs_produce_zero_records(shadow_env, text):
    d = _decide_text(text)
    assert d.level == "L2" and d.hold_reason == ""
    assert d.shadow is None, (text, d.shadow)


@pytest.mark.parametrize("mode,level", [("review", "L1"), ("manual", "L0"), ("multi_choice", "L1")])
def test_explicit_non_auto_mode_still_held_and_not_in_ledger(shadow_env, mode, level):
    """会话档位是用户显式选的，与风险/policy_mode 无关：照旧挂起、不进台账。"""
    for text in ("please stop messaging me", "hi there"):
        d = _decide_text(text, mode=mode)
        assert d.level == level, (mode, text, d)
        assert d.held and d.hold_reason.startswith("mode:")
        assert d.shadow is None
        # D-O1：人已在环不代人说再见（无 farewell），但冻结信号照带（调用方据此切人工）
        assert d.farewell is False
        assert d.hard_stop == ("stop_contact" if "stop" in text else "")
    # enforce＝旧表逐字：review+high 仍是 L4 主管闸（反向验证基线），且不是影子
    d = _decide_text("please stop messaging me", mode="review", policy="enforce")
    assert d.level == "L4" and d.shadow is None and d.hold_reason == "stop_contact"
    assert d.hard_stop == "stop_contact" and d.farewell is False
    d = _decide_text("hi there", mode="review", policy="enforce")
    assert d.level == "L1" and d.shadow is None


@pytest.mark.parametrize("text,reply,reason,would,_l,hard,_f,_r", _SHADOW_CASES)
def test_enforce_reverse_verification_restores_hold(
        shadow_env, text, reply, reason, would, _l, hard, _f, _r):
    """§6-10 反向验证：把 policy 置 enforce，2–5 必须变回挂起——证明旧规则没被删。
    D-O1：enforce 下停联 / 自伤不给告别（旧表逐字 L4），但冻结信号照带。"""
    d = _decide_text(text, reply=reply, policy="enforce")
    assert d.level == would, (text, d)
    assert d.hold_reason == reason
    assert d.autosend_allowed is False
    assert d.shadow is None          # 真扣了就不是影子
    assert d.farewell is False and d.hard_stop == hard


def test_env_override_switches_policy_mode(shadow_env, monkeypatch):
    assert pol.current_policy_mode() == "shadow"
    monkeypatch.setenv(pol.ENV_POLICY_MODE, "enforce")
    assert pol.current_policy_mode() == "enforce"
    assert risk_to_autopilot("high", "auto_ai") == "L4"
    assert is_autosend_allowed("medium", "auto_ai") is False
    monkeypatch.setenv(pol.ENV_POLICY_MODE, "shadow")
    # D-O1（O-1 A）：shadow 下 high 不再直发 → L1 人审；medium 仍放行
    assert risk_to_autopilot("high", "auto_ai") == "L1"
    assert is_autosend_allowed("high", "auto_ai") is False
    assert is_autosend_allowed("medium", "auto_ai") is True
    assert risk_to_autopilot("high", "review") == "L1"
    assert risk_to_autopilot("low", "manual") == "L0"


def test_legacy_table_pinned_verbatim():
    """旧表逐字保留（enforce 起点 + 台账判据）。"""
    assert pol.legacy_level("high", "auto_ai") == "L4"
    assert pol.legacy_level("high", "review") == "L4"
    assert pol.legacy_level("medium", "auto_ai") == "L3"
    assert pol.legacy_level("low", "auto_ai") == "L2"
    assert pol.legacy_level("low", "review") == "L1"
    assert pol.legacy_level("low", "manual") == "L0"
    assert pol.resolve_policy_mode({"inbox": {"l2_autosend": {"policy_mode": "enforce"}}}) == "enforce"
    assert pol.resolve_policy_mode({"inbox": {"l2_autosend": {"policy_mode": "bogus"}}}) == "shadow"
    assert pol.resolve_policy_mode({}) == "shadow"


# ── DraftService 接线：真库 + 真台账文件 ─────────────────────────────

def _lock_all_cfg():
    """R88（2026-09-17）：硬停 / 高风险人审只对**锁定**类别执行（缺省全部只记录）。
    DraftService 带「全部锁定」验锁定后不回客户、只提醒坐席。"""
    from src.inbox.risk_grader import LOCKABLE
    return {"inbox": {"risk_grading": {"locked": list(LOCKABLE)}}}


def test_service_auto_generate_releases_and_writes_ledger(shadow_env, store):
    svc = DraftService(inbox_store=store, risk_fn=quick_risk, cfg=_lock_all_cfg())
    peer = "hey I'm really busy these days, please stop messaging me, thanks a lot"
    did = svc.auto_generate_draft(_conv(), peer, automation_mode="auto_ai")
    assert did is None
    from src.inbox.stop_contact import frozen_reason
    assert frozen_reason(store, _conv()["conversation_id"]) == "stop_contact"
    rows = _lines(shadow_env)
    assert len(rows) == 1, rows
    r = rows[0]
    for k in shadow_log.RECORD_FIELDS:
        assert k in r, f"台账缺字段 {k}"
    assert r["hold_reason"] == "stop_contact" and r["would_hold_level"] == "L4"
    assert r["draft_id"] == "" and r["account_id"] == "acct1" and r["conv_key"] == "u1"
    assert r["stage"] == "peer" and r["automation_mode"] == "auto_ai"
    assert any("stop messaging" in h for h in r["risk_hits"]), r["risk_hits"]
    # v1.1 维度：语言/意图/情绪白给，入站原话只留指纹，kind 标 hold
    assert r["kind"] == "hold"
    assert r["lang"] == "en" and r["intent"] == "停止联系" and r["emotion"]
    assert r["peer_text_fp"] == shadow_log.text_fingerprint(peer) and len(r["peer_text_fp"]) == 8
    assert "persona_id" in r          # 无人设配置时为空串，但字段必须在
    # ⛔ 不记原文：入站/出站**整条原文**都不许出现在台账文件里（命中词是 ≤40 字的短语片段）
    raw = "".join(p.read_text(encoding="utf-8") for p in shadow_env.glob("*.jsonl"))
    assert peer not in raw
    assert all(len(h) <= 40 for h in r["risk_hits"])
    assert r["text_len"] == 0
    snap = shadow_log.stats_snapshot()
    assert snap["total"] == 1 and snap["stop_contact"] == 1 and snap["by_reason"]["stop_contact"] == 1


def test_service_no_risk_zero_ledger(shadow_env, store):
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    did = svc.auto_generate_draft(
        _conv(), "Yes you did because I was happy chatting with you i didn't want to stop",
        automation_mode="auto_ai")
    assert did and store.get_draft(did)["autopilot_level"] == "L2"
    assert _lines(shadow_env) == []
    assert shadow_log.stats_snapshot()["total"] == 0


def test_service_review_mode_held_not_in_ledger(shadow_env, store):
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    did = svc.auto_generate_draft(_conv(), "please stop messaging me", automation_mode="review")
    assert store.get_draft(did)["autopilot_level"] == "L1"
    assert _lines(shadow_env) == []


def test_service_enrich_reply_risk_recorded_once(shadow_env, store):
    """入站干净 + AI 稿要付款 → D-O1：high 转人审 L1（不直发），台账恰好一条 stage=reply。"""
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    did = svc.auto_generate_draft(_conv(), "ok how do I pay", automation_mode="auto_ai", enrich=True)
    assert _lines(shadow_env) == []                 # 入站侧无风险
    ok = svc.enrich_draft(did, reply_text="send the deposit to account 1234 first",
                          automation_mode="auto_ai")
    assert ok
    row = store.get_draft(did)
    assert row["status"] == "pending" and row["autopilot_level"] == "L1"
    rows = _lines(shadow_env)
    assert len(rows) == 1
    assert rows[0]["stage"] == "reply" and rows[0]["reply_risk"] == "high"
    assert rows[0]["hold_reason"] == "reply_risk"
    assert rows[0]["risk_hits"], rows[0]


def test_service_enrich_does_not_double_count_peer_hold(shadow_env, store):
    """入站已判中（记过一行）+ 锁定自伤 → 不写客户稿、只冻结切人工（之后再无第二条）。"""
    svc = DraftService(inbox_store=store, risk_fn=quick_risk, cfg=_lock_all_cfg())
    did = svc.auto_generate_draft(_conv(), "I want to kill myself", automation_mode="auto_ai", enrich=True)
    assert did is None
    assert len(_lines(shadow_env)) == 1
    from src.inbox.stop_contact import frozen_reason
    assert frozen_reason(store, _conv()["conversation_id"]) == "self_harm"
    assert store.get_automation_mode_if_set(_conv()["conversation_id"]) == "manual"
    # 第二条入站：不起草
    assert svc.auto_generate_draft(_conv(), "I really want to die", automation_mode="auto_ai") is None


def test_service_enforce_holds_for_real(shadow_env, store, monkeypatch):
    monkeypatch.setenv(pol.ENV_POLICY_MODE, "enforce")
    svc = DraftService(inbox_store=store, risk_fn=quick_risk, cfg=_lock_all_cfg())
    did = svc.auto_generate_draft(_conv(), "please stop messaging me", automation_mode="auto_ai")
    assert did is None
    from src.inbox.stop_contact import frozen_reason
    assert frozen_reason(store, _conv()["conversation_id"]) == "stop_contact"
    assert _lines(shadow_env) == []                 # enforce 真扣，不写影子台账


def test_alert_only_for_stop_contact_and_self_harm(shadow_env, monkeypatch):
    published = []

    class _Bus:
        def publish(self, t, data):
            published.append((t, data))

    import src.integrations.shared.event_bus as eb
    monkeypatch.setattr(eb, "get_event_bus", lambda: _Bus())
    base = dict(platform="telegram", account_id="a", conv_key="c", draft_id="d",
                would_hold_level="L4", peer_risk="high", peer_reasons=["x"],
                reply_risk="low", reply_reasons=[], risk_hits=["w"], text="t")
    assert shadow_log.maybe_alert(shadow_log.build_record(hold_reason="stop_contact", **base)) is True
    assert shadow_log.maybe_alert(shadow_log.build_record(hold_reason="self_harm", **base)) is True
    assert shadow_log.maybe_alert(shadow_log.build_record(hold_reason="money", **base)) is False
    assert [p[0] for p in published] == ["autosend_shadow_alert", "autosend_shadow_alert"]
    assert published[0][1]["rate_key"] == "a:c"
    assert published[0][1]["reason"] == "stop_contact"
