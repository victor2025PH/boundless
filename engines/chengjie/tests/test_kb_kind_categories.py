# -*- coding: utf-8 -*-
"""知识库分类 / 模板按在用人设 kind 追加（P1-2，2026-09-29）门禁。

8E56 实锤：陪伴域机器上做客服的客户，分类表只有人设背景 / 日常话题…，「活动 / 注册 / 会员」
无处可放。现在分类表 = 域变体 ∪ 在用 kind 集合；纯陪聊机（没有客服 / 销售人设）一个字不变。
"""
from __future__ import annotations

from src.utils import kb_store as ks
from src.utils.persona_kind import kinds_in_use

_COMPANION_CATS = ["人设背景", "日常话题", "情感回应", "关系推进", "边界与安全", "其他"]


def test_effective_categories_union_and_other_last():
    assert ks.effective_kb_categories((), _COMPANION_CATS) == _COMPANION_CATS
    got = ks.effective_kb_categories(("support",), _COMPANION_CATS)
    assert got[:5] == _COMPANION_CATS[:5]
    for c in ks.KIND_KB_CATEGORIES["support"]:
        assert c in got
    assert got[-1] == "其他"
    both = ks.effective_kb_categories(("sales", "support"), _COMPANION_CATS)
    assert both.count("价格与支付") == 1 and both.count("活动优惠") == 1
    assert "异议处理" in both and "下单流程" in both


def test_kind_kb_categories_order_and_dedupe():
    assert ks.kind_kb_categories(()) == []
    sup = ks.kind_kb_categories(["support"])
    assert sup == ks.KIND_KB_CATEGORIES["support"]
    both = ks.kind_kb_categories(["sales", "support"])
    assert both[: len(sup)] == sup and len(both) == len(set(both))


def test_new_entry_templates_prepend_support_examples_on_companion_machine():
    cats = ks.effective_kb_categories(("support",), _COMPANION_CATS)
    tpls = ks.new_entry_templates("companion", cats, kinds=("support",))
    keys = [t["key"] for t in tpls]
    assert keys[:3] == ["support_promo", "support_register", "support_vip"]
    assert tpls[0]["category"] == "活动优惠" and "活动" in tpls[0]["triggers"]
    assert "详见后台" not in tpls[0]["example_reply_zh"]
    # 陪伴三例仍在其后
    assert "companion_address" in keys
    # 没有客服人设：模板原样（只有陪伴三例）
    plain = ks.new_entry_templates("companion", _COMPANION_CATS)
    assert [t["key"] for t in plain] == ["companion_address", "companion_avoid", "companion_topics"]


def test_new_entry_templates_category_normalized_to_effective_table():
    """分类表里没有客服集时，客服模板分类落「其他」（不制造孤儿分类）。"""
    tpls = ks.new_entry_templates("companion", _COMPANION_CATS, kinds=("support",))
    assert tpls[0]["category"] == "其他"


def test_template_examples_pass_kinds_through():
    from src.utils.kb_sheet_io import TEMPLATE_EXAMPLE_PREFIX, template_examples
    cats = ks.effective_kb_categories(("support",), _COMPANION_CATS)
    ex = template_examples("companion", cats, limit=2, kinds=["support"])
    assert len(ex) == 2 and ex[0]["title"].startswith(TEMPLATE_EXAMPLE_PREFIX)
    assert ex[0]["category"] == "活动优惠"


def test_kinds_in_use_reads_persona_profiles(monkeypatch):
    from src.utils.persona_manager import PersonaManager
    pm = PersonaManager.get_instance()
    monkeypatch.setattr(pm, "_profile_personas", {
        "q567": {"id": "q567", "role": "售后支持专员", "tags": ["客服"]},
        "lin": {"id": "lin", "role": "温柔陪聊 / 治愈系闺蜜"},
        "plain": {"id": "plain", "name": "小美"},          # 推不出 → 机器缺省，不计
    })
    assert kinds_in_use({"business_domain": "companion"}) == ("companion", "support")
    monkeypatch.setattr(pm, "_profile_personas", {"plain": {"id": "plain", "name": "小美"}})
    assert kinds_in_use({"business_domain": "companion"}) == ()
