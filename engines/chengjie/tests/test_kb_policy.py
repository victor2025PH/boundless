# -*- coding: utf-8 -*-
"""知识库单一决策点（kb_policy，P0-2 ～ P0-5，2026-09-29）门禁。

锚定 8E56 机实况：客服人设 q567 被问「那都有什么活动」，旧闸（域包 conversion + direct_chat +
无支付词）在检索前跳过知识库。本测试钉住：客服 / 销售人设必查；陪聊人设旧闸原样；
机器缺省 kind 不撬开闲聊闸；查无兜底一次固定话术、第二次转人工；注入措辞按 kind 分套。
"""
from __future__ import annotations

import pytest

from src.utils import kb_policy as kp

_CONV = {"domain": "conversion", "business_domain": "companion"}
_Q567 = {"id": "support_k3a629", "name": "q567", "role": "售后支持专员",
         "tags": ["客服", "售后", "耐心"]}
_BESTIE = {"id": "lin", "name": "林小雨", "role": "温柔陪聊 / 治愈系闺蜜", "tags": ["温柔"]}


@pytest.fixture(autouse=True)
def _clean():
    kp._reset_for_tests()
    yield
    kp._reset_for_tests()


# ── 决策 ──────────────────────────────────────────────────────────────────────

def test_incident_support_persona_must_query_on_chitchat_intent():
    """8E56 实锤：客服人设 + direct_chat + 「活动」不在支付词表 → 旧闸跳过；现在必查。"""
    d = kp.resolve_kb_policy(persona=_Q567, tier="account_profile", intent="direct_chat",
                             text="那都有什么活动", config=_CONV, chat_id="7380618071")
    assert d.mode == kp.MODE_MUST and d.must
    assert d.kind == "support" and d.reason == "kind:support"
    assert d.kind_source in ("tag", "keyword")


def test_explicit_sales_kind_must_query_even_on_greeting():
    d = kp.resolve_kb_policy(persona={"kind": "sales", "role": "x"}, intent="greeting",
                             text="hi", config=_CONV)
    assert d.must and d.kind == "sales" and d.kind_source == "explicit"


def test_companion_persona_keeps_old_chitchat_gate():
    d = kp.resolve_kb_policy(persona=_BESTIE, tier="", intent="direct_chat",
                             text="那都有什么活动", config=_CONV)
    assert d.skip and d.reason == "companion_chat" and d.kind == "companion"
    # 业务词放行（旧闸原样）
    d2 = kp.resolve_kb_policy(persona=_BESTIE, tier="", intent="direct_chat",
                              text="帮我查一下订单", config=_CONV)
    assert d2.mode == kp.MODE_OPTIONAL
    # 非闲聊意图放行
    d3 = kp.resolve_kb_policy(persona=_BESTIE, tier="", intent="channel_info",
                              text="今天好累", config=_CONV)
    assert d3.mode == kp.MODE_OPTIONAL


def test_companion_bound_tier_suppressed_on_a_line():
    """A 线口径：绑定陪聊人设（chat_binding / account_profile）抑制业务 KB；kb_access 放行。"""
    d = kp.resolve_kb_policy(persona=_BESTIE, tier="account_profile", intent="channel_info",
                             text="费率多少", config=_CONV)
    assert d.skip and d.reason == "persona_bound"
    d2 = kp.resolve_kb_policy(persona={**_BESTIE, "kb_access": True}, tier="account_profile",
                              intent="direct_chat", text="今天好累", config=_CONV)
    assert d2.mode == kp.MODE_OPTIONAL and d2.reason == "kb_access"


def test_machine_default_kind_does_not_force_must():
    """无人设 / 推不出 kind 的人设：机器缺省 sales 也不升 must——zhiliao 生产闲聊闸不变。"""
    d = kp.resolve_kb_policy(persona=None, tier="", intent="small_talk", text="今天好累",
                             config={"domain": "conversion", "business_domain": "sales"})
    assert d.skip and d.reason == "companion_chat" and d.kind_source == "default"
    d2 = kp.resolve_kb_policy(persona={"name": "小灵"}, tier="", intent="small_talk",
                              text="今天好累", config=_CONV)
    assert d2.skip and d2.reason == "companion_chat"


def test_info_query_bypasses_chitchat_gate_only_for_default_kind():
    """P1-1：人设没表态 kind（机器缺省）+ 客户在问一件事 → 不按闲聊跳过（optional / info_query）；
    显式 / 推断为陪聊的人设仍走旧闸；「今天好累」这类非提问仍跳过。"""
    plain = {"name": "小美"}
    d = kp.resolve_kb_policy(persona=plain, intent="direct_chat", text="那都有什么活动", config=_CONV)
    assert d.mode == kp.MODE_OPTIONAL and d.reason == "info_query" and d.asked is True
    assert d.kind_source == "default"
    d2 = kp.resolve_kb_policy(persona=plain, intent="small_talk", text="今天好累", config=_CONV)
    assert d2.skip and d2.reason == "companion_chat" and d2.asked is False
    # 陪聊人设同句：旧闸原样
    d3 = kp.resolve_kb_policy(persona=_BESTIE, intent="direct_chat", text="那都有什么活动", config=_CONV)
    assert d3.skip and d3.reason == "companion_chat" and d3.asked is True
    # 配置可关
    cfg_off = {**_CONV, "inbox": {"kb_policy": {"info_query_bypass": False}}}
    d4 = kp.resolve_kb_policy(persona=plain, intent="direct_chat", text="那都有什么活动", config=cfg_off)
    assert d4.skip and d4.reason == "companion_chat"


def test_biz_keywords_configurable():
    cfg = {**_CONV, "inbox": {"kb_policy": {"biz_keywords": ["活动", "VIP"]}}}
    d = kp.resolve_kb_policy(persona=_BESTIE, intent="direct_chat", text="有VIP吗", config=cfg)
    assert d.mode == kp.MODE_OPTIONAL and d.reason == "biz_keyword"
    pc = kp.policy_cfg(cfg)
    assert "订单" in pc["biz_keywords"] and "活动" in pc["biz_keywords"]
    pc2 = kp.policy_cfg({**_CONV, "inbox": {"kb_policy": {"biz_keywords": ["活动"], "biz_keywords_replace": True}}})
    assert pc2["biz_keywords"] == ("活动",)
    pc3 = kp.policy_cfg({"inbox": {"kb_policy": {"nohit_handoff_at": 3}}})
    assert pc3["nohit_handoff_at"] == 3
    assert kp.policy_cfg(None)["nohit_handoff_at"] == kp.NOHIT_HANDOFF_AT


def test_authoritative_wording_needs_persona_declared_kind():
    """机器缺省推出的 sales（zhiliao 生产）不用权威措辞——旧措辞不动。"""
    ctx = "▶ [产品] 智聊\n  【示例回复】: …"
    assert "权威" not in kp.format_kb_block(ctx, kind="sales", kind_source="default", companion_pack=True)
    assert "参考片段" in kp.format_kb_block(ctx, kind="sales", kind_source="default", companion_pack=True)
    assert "权威" in kp.format_kb_block(ctx, kind="sales", kind_source="keyword", companion_pack=True)


def test_media_desc_skips_for_every_kind():
    d = kp.resolve_kb_policy(persona=_Q567, intent="direct_chat",
                             text="[图片内容] 一包重庆怪味胡豆", config=_CONV)
    assert d.skip and d.reason == "media_desc"


def test_non_companion_pack_optional_by_default():
    d = kp.resolve_kb_policy(persona=None, intent="small_talk", text="今天好累",
                             config={"domain": "payment", "domain_plugins": {"payment": {"enabled": True}}})
    assert d.mode == kp.MODE_OPTIONAL and d.reason == "default"


def test_decision_as_dict_and_registry():
    d = kp.resolve_kb_policy(persona=_Q567, intent="direct_chat", text="活动", config=_CONV)
    d.hit = False
    d.refs = 0
    kp.note_decision("telegram:1:2", d)
    got = kp.peek_decision("telegram:1:2")
    assert got["mode"] == "must" and got["kind"] == "support" and got["hit"] is False
    assert "extra" not in got
    assert kp.peek_decision("nope") == {}


# ── 查无兜底 ──────────────────────────────────────────────────────────────────

def test_nohit_ladder_fixed_then_handoff():
    cid = "telegram:8414394703:7380618071"
    assert kp.nohit_bump(cid) == 1
    assert kp.nohit_count(cid) == 1
    assert kp.nohit_bump(cid) == 2
    kp.nohit_reset(cid)
    assert kp.nohit_count(cid) == 0
    assert kp.nohit_bump("") == 1     # 空键不登记但仍算第一次


def test_nohit_block_wording():
    b1 = kp.nohit_block(1, "这个我去核实一下再回你", lang="zh")
    assert "【知识库查无】" in b1 and "这个我去核实一下再回你" in b1 and "不得添加" in b1
    b2 = kp.nohit_block(kp.NOHIT_HANDOFF_AT, "x", lang="zh")
    assert "已转人工" in b2 and "不要再问同一个问题" in b2
    e1 = kp.nohit_block(1, "let me check", lang="en")
    assert "no answer" in e1 and "let me check" in e1


def test_fixed_nohit_reply_prefers_kb_template():
    class _KB:
        def get_direct_reply(self, key):
            return "运营改过的话术" if key == kp.NOHIT_TEMPLATE_KEY else None
    assert kp.fixed_nohit_reply(_KB(), "zh") == "运营改过的话术"
    assert kp.fixed_nohit_reply(None, "zh") == kp.NOHIT_REPLY_ZH
    assert kp.fixed_nohit_reply(None, "en") == kp.NOHIT_REPLY_EN


# ── 注入措辞 ──────────────────────────────────────────────────────────────────

def test_format_kb_block_by_kind():
    ctx = "▶ [活动] 新人注册礼\n  【示例回复】: 注册即送 …"
    sup = kp.format_kb_block(ctx, kind="support", companion_pack=True)
    assert "权威事实" in sup and "以条目为准" in sup and "参考片段" not in sup
    comp = kp.format_kb_block(ctx, kind="companion", companion_pack=True)
    assert "参考片段（语气用" in comp and "权威" not in comp
    pay_live = kp.format_kb_block("成功率：95%", kind="", companion_pack=False, live_channel=True)
    assert "已过期" in pay_live and "见上方实时数据" in pay_live
    pay_plain = kp.format_kb_block(ctx, kind="", companion_pack=False, live_channel=False)
    assert "已过期" not in pay_plain and "以条目为准" in pay_plain
    assert kp.format_kb_block("", kind="support") == ""
    en = kp.format_kb_block(ctx, kind="sales", lang="en")
    assert "authoritative" in en


def test_kb_store_material_header_is_neutral():
    """kb_store 材料头不再写「必须按步骤执行」——外层措辞由 format_kb_block 按 kind 决定。"""
    import inspect
    from src.utils.kb_store import KnowledgeBaseStore
    src = inspect.getsource(KnowledgeBaseStore._format_ai_context)
    assert "必须按步骤执行，不得跳过" not in src
    assert "【知识库条目】" in src
