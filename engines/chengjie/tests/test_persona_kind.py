# -*- coding: utf-8 -*-
"""人设工作类型（persona_kind，P0-1 2026-09-29）门禁。

锚定 8E56 机实况：客服模板建的人设 q567（role「售后支持专员」）此前没有任何字段
表达「这是客服」，草稿链按机器级陪聊判定跳过知识库。本测试钉住：显式 kind 最高优先、
存量人设按 role / tags 推断、推不出回落机器缺省、客服 / 销售 kind 必查知识库。
"""
from __future__ import annotations

import pytest

from src.utils import persona_kind as pk


def test_normalize_aliases():
    assert pk.normalize_kind("客服") == "support"
    assert pk.normalize_kind("Sales") == "sales"
    assert pk.normalize_kind("陪聊") == "companion"
    assert pk.normalize_kind("") == ""
    assert pk.normalize_kind("nonsense") == ""


def test_explicit_kind_wins_over_role_keywords():
    p = {"kind": "companion", "role": "售后支持专员", "tags": ["客服"]}
    assert pk.resolve_kind(p, {}) == ("companion", "explicit")


def test_support_template_role_infers_support_without_explicit_kind():
    """存量 1.101 人设：向导「专业客服」模板只写 role，没有 kind → 仍按客服处理。"""
    q567 = {"id": "support_k3a629", "name": "q567", "role": "售后支持专员",
            "tags": ["客服", "售后", "耐心"]}
    kind, src = pk.resolve_kind(q567, {"business_domain": "companion"})
    assert kind == "support"
    assert src in ("tag", "keyword")
    assert pk.kind_requires_kb(kind)


def test_sales_and_companion_inference():
    assert pk.infer_kind({"role": "私域导购顾问", "tags": ["导购", "电商"]})[0] == "sales"
    assert pk.infer_kind({"role": "温柔陪聊 / 治愈系闺蜜", "tags": ["温柔"]})[0] == "companion"
    # 客服词与销售词同现 → 客服优先（两者都查库，误判成陪聊才是事故方向）
    assert pk.infer_kind({"role": "售后 / 导购"})[0] == "support"


def test_default_follows_business_domain():
    assert pk.resolve_kind({"name": "小灵", "role": ""}, {"business_domain": "companion"}) == ("companion", "default")
    assert pk.resolve_kind({"name": "小灵", "role": ""}, {"business_domain": "sales"}) == ("sales", "default")
    assert pk.resolve_kind(None, {"business_domain": "sales"}) == ("sales", "default")


def test_kind_requires_kb_only_for_support_and_sales():
    assert pk.kind_requires_kb("support")
    assert pk.kind_requires_kb("sales")
    assert not pk.kind_requires_kb("companion")
    assert not pk.kind_requires_kb("")


def test_kind_view_shape():
    v = pk.kind_view({"kind": "support"}, {})
    assert v["kind"] == "support" and v["source"] == "explicit" and v["explicit"] == "support"
    assert v["label_zh"] == "客服" and v["label_en"] == "Support"


def test_kind_and_kb_access_registered_as_prompt_exempt():
    """kind / kb_access 是策略开关，不进人设 prompt 块；同时进 known_persona_top_keys（导入闸放行）。"""
    from src.utils.persona_manager import PROMPT_EXEMPT_FIELDS, known_persona_top_keys
    assert "kind" in PROMPT_EXEMPT_FIELDS
    assert "kb_access" in PROMPT_EXEMPT_FIELDS
    keys = known_persona_top_keys()
    assert "kind" in keys and "kb_access" in keys


@pytest.mark.parametrize("kind,lang,label", [
    ("support", "zh", "客服"), ("sales", "en", "Sales"), ("companion", "zh", "陪聊"),
    ("bogus", "zh", "陪聊"),
])
def test_kind_label(kind, lang, label):
    assert pk.kind_label(kind, lang) == label


def test_ident_kb_code_account_line():
    """P1-4：账号身份行知识库状态——客服必查、陪聊闲聊不查、kb_access 已开、未选人设。"""
    q567 = {"id": "q567", "role": "售后支持专员", "tags": ["客服"]}
    bestie = {"id": "lin", "role": "温柔陪聊 / 治愈系闺蜜"}
    assert pk.ident_kb_code(q567) == "must"
    assert pk.ident_kb_code(bestie) == "skip"
    assert pk.ident_kb_code({**bestie, "kb_access": True}) == "open"
    assert pk.ident_kb_code(q567, unselected=True) == "unselected"
    assert pk.ident_kb_code(None) == "unselected"
    assert pk.ident_kb_code({"name": "小灵"}) == "skip"   # 机器缺省不算 must
