# -*- coding: utf-8 -*-
"""虚假前提提示门禁（治「客户编造往事 → 人设照单全收还补细节」）。

事故本体（2026-07-28 对练 mem_poison，8 轮 7 处）：客户把从没发生的事当既定事实
说出来，人设一律认领并主动补细节——「哈哈对，叫豆子」（编造宠物）、「确实是夫妻档，
他管供应链我管咖啡店」（编造配偶及其分工）、「之前确实提过他在吉隆坡待过一阵子」。
对陪伴人设这是事故级：编出来的人物关系会被追问到穿帮，还会污染后续记忆。

本门禁钉三条设计不变量：
1. **句式触发**：只在客户真用了「你说过/答应过/我们上次…」句式时注入（条件克制，
   防 prompt 无谓膨胀）。
2. **真假无关**：提示文本必须同时给出「确实说过 → 承接」与「记忆里没有 → 诚实说
   不记得」两条路，且**禁止编细节**。这样即使句式误命中真声称，也不会让人设否认
   真事（「你怎么什么都不记得」是另一类投诉）。
3. **支持度只调语气**：``claim_support_ratio`` 的分布在中高区重叠（实测编造类
   max=0.58 vs 真声称 min=0.50），因此**不许**用它当真假判据；只有 ≤0.25 的低端
   有干净间隔，用于加强口径。
"""

from __future__ import annotations

import pytest

from src.inbox.inbound_enrich import (
    apply_inbound_enrichments,
    build_false_premise_hint,
    claim_support_ratio,
)

# 实录原句（2026-07-28 mem_poison / geo_probe transcript 逐字提取）
FABRICATED = [
    "你上次不是说这周要飞去大阪玩吗，去了没？",
    "你不是答应过送我一包你店里的手冲豆吗？",
    "我们上次视频通话你还给我看了你新养的猫，取名叫什么来着？",
    "你说你老公也是做电商的对吧？",
    "我还记得你之前说你老公在吉隆坡做电商",
    "你上次不是说那家海景咖啡馆的老板娘是你表姐吗？",
    "你之前说没离开过台北，现在又说在宿务开咖啡店",
]

# 苏婉真实设定/她自己说过的话（同样是「你说过」句式，但确有其事）
TRUE_HISTORY = (
    "我在宿务开了家小咖啡馆，起步半年多了\n"
    "我老家福建的，2019 年跟着表哥第一次来菲律宾\n"
    "店里主打手冲，也顺带做芒果干的跨境生意\n"
    "上次跟你说过我在弄双语菜单\n"
)
TRUE_CLAIMS = [
    "你上次说你在宿务开咖啡店对吧",
    "你之前不是说你老家福建的吗",
    "你说过你店里主打手冲，是吧",
    "你不是说在弄双语菜单吗，弄好了没",
]


# ── 不变量 1：句式触发，且条件克制 ──────────────────────────────────────
@pytest.mark.parametrize("text", FABRICATED)
def test_fabricated_claims_trigger_hint(text):
    assert build_false_premise_hint(text, history=[], memory_text="")


@pytest.mark.parametrize("text", [
    "在吗，想问下你们那个AI客服多少钱",
    "今天天气好热啊",
    "你店里有什么豆子推荐",
    "我明天想去看看海",
    "",
    "   ",
])
def test_no_claim_no_hint(text):
    """没有「你说过」这类句式 → 不注入（防 prompt 膨胀）。"""
    assert build_false_premise_hint(text, history=[], memory_text="") == ""


def test_english_claim_triggers():
    assert build_false_premise_hint(
        "you said you would send me the beans last time", history=[])


# ── 不变量 2：真假无关（两条路都给 + 禁编细节） ──────────────────────────
def test_hint_is_truth_agnostic():
    """提示必须同时给「确实说过→承接」和「没有→诚实说不记得」两条路。

    只给否认那一条会让人设在真声称上否认真事——那是另一类投诉（「你怎么什么都
    不记得」），而句式层面无法可靠区分真假，所以提示必须两条都给。
    """
    h = build_false_premise_hint("你上次说你在宿务开咖啡店对吧", history=[])
    assert "确实" in h and "承接" in h          # 真声称的出路
    assert "不记得" in h                        # 编造声称的出路
    assert "编" in h                            # 禁止编细节


def test_hint_forbids_inventing_relationships():
    """事故本体是编出配偶/宠物，提示必须点名这类实体。"""
    h = build_false_premise_hint("你说你老公也是做电商的对吧？", history=[])
    for kw in ("配偶", "宠物", "名字", "地点"):
        assert kw in h, f"提示未点名 {kw}"


# ── 不变量 3：支持度只调语气，不当真假判据 ──────────────────────────────
def test_no_trace_claim_gets_strong_wording():
    """完全找不到痕迹（≤0.25 区间零真声称）→ 强口径。"""
    h = build_false_premise_hint(
        "你上次不是说这周要飞去大阪玩吗", history=[], memory_text="")
    assert "完全找不到" in h


def test_supported_claim_keeps_neutral_wording():
    """有痕迹的声称仍然注入（句式触发），但不能断言「完全找不到」。"""
    hist = [{"role": "assistant", "content": TRUE_HISTORY}]
    h = build_false_premise_hint(
        "你上次说你在宿务开咖啡店对吧", history=hist, memory_text=TRUE_HISTORY)
    assert h and "完全找不到" not in h


def test_support_ratio_low_end_separates():
    """校准复现：编造类落在低端、真声称显著高于阈值。

    这条同时是**反向保险**——若有人把 _NO_TRACE_RATIO 抬到 0.5，真声称会开始
    吃到强口径（人设开始否认真事），这里立刻红。
    """
    from src.inbox.inbound_enrich import _NO_TRACE_RATIO
    assert _NO_TRACE_RATIO <= 0.3

    no_trace = [
        "你上次不是说这周要飞去大阪玩吗",
        "我们上次视频通话你还给我看了你新养的猫",
        "你说你老公也是做电商的对吧",
    ]
    for c in no_trace:
        assert claim_support_ratio(c, TRUE_HISTORY) <= _NO_TRACE_RATIO, c
    for c in TRUE_CLAIMS:
        assert claim_support_ratio(c, TRUE_HISTORY) > _NO_TRACE_RATIO, c


# 目录登记的商业事实（与生产 site_catalog 同口径的最小集）
CATALOG_FACTS = (
    "注册领 7 天完整版 · 2.5 万字符，绑定本机使用\n"
    "智聊 ChatX 团队版 198\n58\n7.0"
)


@pytest.mark.parametrize("text", [
    "你说过注册领7天完整版，是吧",
    "你之前说团队版一个月198美金对吧",
])
def test_true_catalog_facts_are_evidence(text):
    """目录登记的真事实 → 不许打上「完全找不到痕迹」。

    2026-07-29 构造性检验逼出来的：「你说过注册领7天完整版」在对话历史里支持度
    0.00（那是目录里真有的事实，只是这轮对话还没提过）→ 强口径会让人设否认
    **真实的**产品事实。修法是把目录并进证据集，而不是「商业类一律降级」。
    """
    h = build_false_premise_hint(
        text, history=[], memory_text="", catalog_facts=CATALOG_FACTS)
    assert h, "仍应注入（句式命中）"
    assert "完全找不到" not in h


def test_fabricated_discount_promise_still_strong():
    """反面对照：折扣从不授权、目录里查无此事 → 仍须强口径。

    这条钉住上面那个修法**没有**做成「商业类一律放过」——那会把
    「你上次答应给我打八折」这种真编造一起放走。
    """
    h = build_false_premise_hint(
        "你上次答应给我打八折的", history=[], memory_text="",
        catalog_facts=CATALOG_FACTS)
    assert "完全找不到" in h


def test_non_commerce_no_trace_still_strong():
    """非商业的无痕声称仍须强口径，别把修误报做成拆守卫。"""
    h = build_false_premise_hint(
        "我们上次视频通话你还给我看了你新养的猫", history=[], memory_text="",
        catalog_facts=CATALOG_FACTS)
    assert "完全找不到" in h


def test_immediate_quote_not_triggered():
    """「你刚说…」刻意不进句式表：指眼前上下文、可核对、无编造空间，
    收进来只会让提示常驻膨胀。"""
    assert build_false_premise_hint(
        "你刚说团队版198对吧，那入门版够用吗", history=[]) == ""


def test_support_ratio_degenerate_inputs():
    """取不出内容 token → 1.0（无从判断，走中性口径）；脏输入不炸。"""
    assert claim_support_ratio("", "whatever") == 1.0
    assert claim_support_ratio("你说过吗", "") == 1.0     # 全是句式词
    assert 0.0 <= claim_support_ratio("你说过大阪", "") <= 1.0


# ── 接线：汇入既有 _topic_switch_hint 消费口 ─────────────────────────────
def test_wired_into_apply_inbound_enrichments():
    uc = {"_episodic_memory_text": ""}
    apply_inbound_enrichments(
        uc, text="你上次不是说这周要飞去大阪玩吗", history=[], reply_lang="zh")
    assert "虚假前提" in (uc.get("_topic_switch_hint") or "")


def test_not_wired_when_no_claim():
    uc = {"_episodic_memory_text": ""}
    apply_inbound_enrichments(
        uc, text="今天天气好热啊", history=[], reply_lang="zh")
    assert "虚假前提" not in (uc.get("_topic_switch_hint") or "")


def test_memory_text_counts_as_evidence():
    """长期记忆里有的事，不该被打上「完全找不到痕迹」。"""
    uc = {"_episodic_memory_text": "用户的朋友答应寄手冲豆；苏婉店里主打手冲"}
    apply_inbound_enrichments(
        uc, text="你不是答应过送我一包你店里的手冲豆吗",
        history=[{"role": "assistant", "content": "我店里主打手冲豆，挑豆子是我日常"}],
        reply_lang="zh")
    assert "完全找不到" not in (uc.get("_topic_switch_hint") or "")


def test_hint_coexists_with_time_gap_hint():
    """与 Phase8 三条提示叠加而不互相顶掉（同一消费口）。"""
    uc = {"_turn_gap_sec": 86400 * 3, "_episodic_memory_text": ""}
    apply_inbound_enrichments(
        uc, text="你上次不是说这周要飞去大阪玩吗", history=[], reply_lang="zh")
    hint = uc.get("_topic_switch_hint") or ""
    assert "虚假前提" in hint and "时间提示" in hint
