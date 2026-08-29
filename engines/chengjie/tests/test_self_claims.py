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


# ── #62（2026-08-30）：近时自述「你刚说过」——即时行为 / 时间承诺 ─────────────
# 金标语料=钧 0830 两例实锤原话（截图在单 #62）。
import time as _time

from src.inbox.self_claims import (
    build_recent_self_statement_hint,
    extract_recent_self_statements,
)


def _ht(*rows):
    """带 ts 的历史行：(role, content, ago_sec)。"""
    now = _time.time()
    return [{"role": r, "content": c, "ts": now - ago} for r, c, ago in rows]


def test_incident_tea_activity_anchored_31min():
    """01:32「正泡了杯茶发呆」→ 31 分钟后必须还在锚点里（事故窗口）。"""
    hist = _ht(
        ("assistant", "这么晚还不睡…我这边刚收工，正泡了杯茶发呆。", 31 * 60),
        ("user", "你天天喝茶吗？不用睡觉的吗", 5 * 60),
    )
    stmts = extract_recent_self_statements(hist)
    assert any(s["kind"] == "activity" and "泡了杯茶" in s["text"] for s in stmts)
    hint = build_recent_self_statement_hint(hist)
    assert "你刚说过" in hint and "泡了杯茶" in hint
    assert "分钟前" in hint  # 带时距引用


def test_incident_plan_anchored_10min():
    """03:29「下个月应该会过去一趟，到时候提前跟你约」→ 10 分钟后仍锚定。"""
    hist = _ht(
        ("assistant", "我下个月应该会过去一趟，到时候提前跟你约。", 10 * 60),
    )
    stmts = extract_recent_self_statements(hist)
    assert any(s["kind"] == "plan" and "下个月" in s["text"] for s in stmts)


def test_activity_expires_after_window():
    """8 小时前的「正泡着茶」不再锚定——过期锚点会制造反向事故。"""
    hist = _ht(("assistant", "我在泡茶，等会儿陪你聊。", 8 * 3600))
    assert extract_recent_self_statements(hist) == []


def test_plan_survives_longer_than_activity():
    """时间承诺窗口 72h：昨天说的「下个月过去」今天仍然锚定。"""
    hist = _ht(("assistant", "我下个月应该会过去一趟，到时候提前跟你约。", 24 * 3600))
    stmts = extract_recent_self_statements(hist)
    assert any(s["kind"] == "plan" for s in stmts)


def test_no_ts_positional_fallback():
    """A 线历史无 ts：activity 只认最近 3 条 assistant 消息。"""
    recent = _h(
        ("assistant", "我这边刚收工，正泡了杯茶发呆。"),
        ("assistant", "对了你今天过得怎么样。"),
    )
    stmts = extract_recent_self_statements(recent)
    assert any(s["kind"] == "activity" for s in stmts)
    assert stmts[0]["ago_sec"] is None
    # 同一句被垫到 4 条 assistant 之前 → 位置过窗，不锚
    old = _h(
        ("assistant", "我这边刚收工，正泡了杯茶发呆。"),
        ("assistant", "嗯嗯。"), ("assistant", "好呀。"),
        ("assistant", "对了你今天过得怎么样。"), ("assistant", "哈哈。"),
    )
    assert not any(s["kind"] == "activity"
                   for s in extract_recent_self_statements(old))


def test_latest_statement_wins_after_correction():
    """说法变过 → 锚最新版（02:03 白水替换 01:32 茶）：止住继续翻烙饼。"""
    hist = _ht(
        ("assistant", "我这边刚收工，正泡了杯茶发呆。", 40 * 60),
        ("assistant", "大半夜的哪能喝茶，刚泡了杯白水。", 3 * 60),
    )
    stmts = [s for s in extract_recent_self_statements(hist)
             if s["kind"] == "activity"]
    assert len(stmts) == 1 and "白水" in stmts[0]["text"]


def test_user_side_and_questions_not_anchored():
    hist = _ht(
        ("user", "我在泡茶呢。", 60),
        ("assistant", "你是不是在泡茶？", 30),
    )
    assert extract_recent_self_statements(hist) == []


def test_enrich_wires_recent_statement_hint():
    """A/B 共用消费口：enrich 后「你刚说过」块进 _topic_switch_hint。"""
    from src.inbox.inbound_enrich import apply_inbound_enrichments
    ctx: dict = {}
    apply_inbound_enrichments(
        ctx,
        text="你天天喝茶吗？",
        history=_ht(
            ("assistant", "这么晚还不睡…我这边刚收工，正泡了杯茶发呆。", 31 * 60),
        ),
        reply_lang="zh",
    )
    hint = ctx.get("_topic_switch_hint") or ""
    assert "你刚说过" in hint and "泡了杯茶" in hint
