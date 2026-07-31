# -*- coding: utf-8 -*-
"""P1-198 自述一致性锚点（问题 6：AI 对自己说过的话前后矛盾）。

金标语料来自 198 实录会话（Steven 人设 × 英文客户）：AI 亲口说过
Divorced / daughter Maya / investment firm，隔窗后表述漂移。
设计不变量：只扫 assistant 侧、只引用原句不解释、问句不算自述、
每槽位取最新、误报保守（"I'm 10 minutes away" 不是年龄自述）。
"""
from __future__ import annotations

from src.inbox.self_claims import build_self_claims_hint, extract_self_claims


def _h(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


# ── 抽取语义 ────────────────────────────────────────────────────────────────
def test_extracts_198_style_english_claims():
    hist = _h(
        ("user", "So you married ?"),
        ("assistant", "Divorced, actually. It's been a few years now."),
        ("user", "Do you live alone?"),
        ("assistant", "Just me and my daughter, Maya. She's eight."),
        ("assistant", "Work-wise, I'm still the same — running my investment "
                      "firm in New York, mostly tech and cross-border deals."),
    )
    claims = extract_self_claims(hist)
    slots = {c["slot"] for c in claims}
    assert "marital" in slots
    assert "family" in slots
    assert "job" in slots
    marital = next(c for c in claims if c["slot"] == "marital")
    assert "Divorced" in marital["text"]  # 原句引用，不改写


def test_extracts_chinese_claims():
    hist = _h(
        ("assistant", "我今年41岁，在纽约经营一家投资公司。"),
        ("assistant", "我离过婚，现在是单身。"),
        ("assistant", "我女儿叫Maya，今年八岁。"),
    )
    slots = {c["slot"] for c in extract_self_claims(hist)}
    assert {"age", "marital", "family"} <= slots


def test_user_messages_never_scanned():
    """客户说的话绝不进自述账本（把客户的离婚当成 AI 的=事故）。"""
    hist = _h(
        ("user", "我离过婚，现在是单身。"),
        ("user", "My daughter is eight."),
        ("assistant", "谢谢你愿意跟我说这些。"),
    )
    assert extract_self_claims(hist) == []


def test_questions_are_not_claims():
    hist = _h(
        ("assistant", "你结婚了吗？"),
        ("assistant", "Are you married?"),
    )
    assert extract_self_claims(hist) == []


def test_latest_claim_wins_per_slot():
    """同槽位后说的覆盖先说的（锚定客户最近听到的版本，止住继续翻烙饼）。"""
    hist = _h(
        ("assistant", "我住在上海。"),
        ("assistant", "我现在在纽约生活。"),
    )
    claims = extract_self_claims(hist)
    homes = [c for c in claims if c["slot"] == "home"]
    assert len(homes) == 1 and "纽约" in homes[0]["text"]


def test_negation_preserved_verbatim():
    """否定句原样保留——句级引用的核心价值（解析式抽取会把否定丢掉）。"""
    hist = _h(("assistant", "我还没结过婚呢，一直单身。"),)
    claims = extract_self_claims(hist)
    assert claims and "没结过婚" in claims[0]["text"]


def test_conservative_no_false_age_from_eta():
    """"I'm 10 minutes away" 是到达时间不是年龄——保守词表必须放过。"""
    hist = _h(
        ("assistant", "I'm 10 minutes away."),
        ("assistant", "I'm 5 mins late, sorry!"),
    )
    assert all(c["slot"] != "age" for c in extract_self_claims(hist))


def test_im_sure_not_a_name_claim():
    hist = _h(
        ("assistant", "I'm sure you'll love it."),
        ("assistant", "I'm really glad we talked."),
    )
    assert all(c["slot"] != "name" for c in extract_self_claims(hist))


def test_name_claim_extracted():
    hist = _h(("assistant", "It's Steven, no worries."),)
    claims = extract_self_claims(hist)
    assert any(c["slot"] == "name" for c in claims)


def test_cap_and_no_crash_on_garbage():
    assert extract_self_claims(None) == []          # type: ignore[arg-type]
    assert extract_self_claims([{"bogus": 1}, "x"]) == []   # type: ignore[list-item]


# ── 提示拼装与接线 ──────────────────────────────────────────────────────────
def test_hint_quotes_and_instructs():
    hist = _h(("assistant", "Divorced, actually. It's been a few years now."),)
    hint = build_self_claims_hint(hist)
    assert "自述一致性" in hint
    assert "Divorced, actually" in hint
    assert "矛盾" in hint


def test_hint_empty_without_claims():
    assert build_self_claims_hint(_h(("assistant", "哈哈，好呀。"))) == ""


def test_enrich_wires_self_claims_into_topic_hint():
    from src.inbox.inbound_enrich import apply_inbound_enrichments
    ctx: dict = {}
    apply_inbound_enrichments(
        ctx,
        text="So are you seeing anyone?",
        history=_h(
            ("assistant", "Divorced, actually. It's been a few years now."),
            ("user", "Oh I see."),
        ),
        reply_lang="en",
    )
    hint = ctx.get("_topic_switch_hint") or ""
    assert "自述一致性" in hint and "Divorced" in hint
