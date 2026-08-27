"""人设档案「填了就算数」契约门禁（实施67 P1-11a，B75 `_352` 报障沉淀）。

事故：客户包交付档 ``persona_block_detail: compact``，而 compact 编译器只输出约
1/3 字段——用户在工作室填的背景故事/父母职业（context.family）/英文名
（names.english）/好恶/专属记忆等全部**静默丢弃**，AI 只能即兴编（实录：职业被
编成「商业咨询」，用户直问「核心逻辑是否被改」）。

三层契约：
1. **字段防丢弃**：工作室编辑器可填的每个档案字段，full 编译输出必须包含其内容
   ——以后编辑器加字段没接编译＝这里先红。
2. **tier 升档**：用户显式绑定的人设（conv_override/chat_binding/account_profile）
   在 compact 配置下自动升 full（用户意志＝档案就是拿来用的）；域/默认 tier 维持
   compact 省 token 语义。
3. **compact 名字事实**：西方名字（英文名/西文全名/昵称）属「小而硬」事实，
   compact 也必须在场（被问英文名答不上＝与年龄同级穿帮）。
"""

from __future__ import annotations

import pytest

from src.utils.persona_manager import PersonaManager


# 工作室编辑器（personas.html pe-pf-* 表单）全字段样本——每个值都带唯一锚点串，
# 断言「锚点出现在编译输出里」＝该字段真的进了 prompt。
_FULL_PROFILE = {
    "id": "zhang_haoran_test",
    "name": "张浩然",
    "role": "建筑设计师",
    "age": 34,
    "gender": "male",
    "names": {
        "full_western": "Harold Zhang",
        "english": "Harold",
        "nickname": "阿浩",
        "usage_notes": "英文场合用 Harold",
    },
    "background": "生于杭州，在同济读的建筑，毕业后进了本地设计院做住宅项目",
    "appearance": "short black hair, wire-frame glasses, lean build",
    "personality": {
        "traits": ["沉稳", "务实"],
        "style": "话不多但句句在点上",
        "quirks": "口头禅是「讲道理」",
        "humor": "冷幽默",
        "temperament": "慢热",
    },
    "speaking": {
        "forbidden_phrases": ["亲爱的用户"],
        "reply_length": "short",
    },
    "identity": {"deny_ai": True, "deny_ai_reply": "我是张浩然本人"},
    "boundaries": {"topics_to_avoid": ["政治站队"]},
    "tastes": {
        "likes": ["单一麦芽威士忌", "Barolo红酒"],
        "dislikes": ["香菜"],
        "opinions": ["设计要为人服务"],
    },
    "context": {
        "family": {
            "father": "父亲是退休中学物理老师",
            "mother": "母亲经营一家花店",
        },
        "hobbies": ["攀岩", "手冲咖啡"],
        "specific_memories": ["2019年独自去过冰岛自驾两周"],
        "emotional_triggers": {
            "positive": "聊到建筑大师作品会兴奋",
            "negative": "被催稿会烦躁",
            "deep_empathy": "对异乡打拼的人共情",
        },
        "schedule": {"work_hours": "早九晚六，偶尔加班到九点"},
    },
}

# 字段 → 输出锚点（full 编译必须逐一在场）。锚点选「用户填的内容原文片段」，
# 编译层改措辞不影响；改到把内容丢了才红。
_FIELD_ANCHORS = {
    "name": "张浩然",
    "role": "建筑设计师",
    "age": "34岁",
    "names.full_western": "Harold Zhang",
    "names.english": "Harold",
    "names.nickname": "阿浩",
    "background": "同济读的建筑",
    "appearance": "wire-frame glasses",
    "context.family.father": "退休中学物理老师",
    "context.family.mother": "经营一家花店",
    "context.hobbies": "攀岩",
    "context.specific_memories": "冰岛自驾",
    "context.emotional_triggers.positive": "建筑大师作品",
    "context.schedule": "早九晚六",
    "tastes.likes": "Barolo红酒",
    "tastes.dislikes": "香菜",
    "tastes.opinions": "设计要为人服务",
    "personality.traits": "沉稳",
    "personality.style": "句句在点上",
    "personality.quirks": "讲道理",
    "speaking.forbidden_phrases": "亲爱的用户",
    "boundaries.topics_to_avoid": "政治站队",
}


@pytest.fixture()
def pm():
    PersonaManager.reset()
    m = PersonaManager.get_instance()
    yield m
    PersonaManager.reset()


def test_full_format_covers_every_studio_field(pm):
    """契约1：编辑器可填字段 ⊆ full 编译面——缺一即红（防新增字段静默丢）。"""
    out = pm._format_persona_instructions(dict(_FULL_PROFILE))
    missing = [k for k, anchor in _FIELD_ANCHORS.items() if anchor not in out]
    assert not missing, (
        f"以下档案字段用户填了却没进 full prompt（静默丢弃＝B75 复发）：{missing}"
    )


def test_compact_keeps_western_name_facts(pm):
    """契约3：compact 也必须带西方名字事实行（小而硬，答不上=当场穿帮）。"""
    out = pm._format_persona_compact(dict(_FULL_PROFILE))
    assert "Harold" in out, "compact 丢英文名＝被问英文名只能现编"
    assert "Harold Zhang" in out
    assert "阿浩" in out


def test_explicit_binding_upgrades_compact_to_full(pm):
    """契约2：显式绑定（chat_binding）+ compact 配置 → 自动升 full。

    判据＝档案长文锚点（背景/父母职业/好恶）在输出中——compact 永远不含它们。
    """
    pm.upsert_profile("zhang_haoran_test", dict(_FULL_PROFILE))
    pm.bind_chat_persona_by_profile_id("conv_test_1", "zhang_haoran_test")
    out = pm.format_persona_block(
        "conv_test_1", detail="compact", record_usage=False)
    for anchor in ("同济读的建筑", "退休中学物理老师", "Barolo红酒"):
        assert anchor in out, (
            f"显式绑定会话在 compact 配置下档案字段 {anchor!r} 未进 prompt"
            "（tier 升档失效＝B75 复发）"
        )


def test_account_profile_tier_also_upgrades(pm):
    """账号级绑定（account_persona_id）同样升档。"""
    pm.upsert_profile("zhang_haoran_test", dict(_FULL_PROFILE))
    out = pm.format_persona_block(
        "", detail="compact", account_persona_id="zhang_haoran_test",
        record_usage=False)
    assert "退休中学物理老师" in out


def test_unbound_conversation_keeps_compact(pm):
    """未绑定（域/默认 tier）维持 compact 省 token 语义——升档只属显式绑定。"""
    pm.upsert_profile("zhang_haoran_test", dict(_FULL_PROFILE))
    pm.set_domain_persona(dict(_FULL_PROFILE))
    out = pm.format_persona_block(
        "conv_unbound_x", detail="compact", record_usage=False)
    # 域 tier：compact 形态＝不含长文档案（背景故事），但名字事实在场
    assert "同济读的建筑" not in out, "未绑定会话不应升档（token 预算语义）"
    assert "张浩然" in out
    assert "Harold" in out, "compact 名字事实行必须在场"


def test_detail_none_still_respected(pm):
    """detail=none 是显式全关，tier 升档不得越权。"""
    pm.upsert_profile("zhang_haoran_test", dict(_FULL_PROFILE))
    pm.bind_chat_persona_by_profile_id("conv_test_2", "zhang_haoran_test")
    out = pm.format_persona_block(
        "conv_test_2", detail="none", record_usage=False)
    assert out == ""
