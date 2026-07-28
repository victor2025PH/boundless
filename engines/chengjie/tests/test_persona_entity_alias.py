# -*- coding: utf-8 -*-
"""按人设派生的实体别名门禁——纯函数，不碰生产库/网络。

覆盖：真实形态 profile（照 config/profiles_runtime.yaml 结构构造）产出、
「José Luis」真实缺陷场景端到端、自由文本轨的严格边界、误抽负例、
上限、脏输入软失败、确定性、与全局表的合并语义。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.companion import persona_bio_store as pbs
from src.companion.persona_entity_alias import (
    MAX_VALUES_PER_KEY,
    build_entity_aliases,
    merge_query_aliases,
)


# ── 语料：照 profiles_runtime.yaml 的真实结构 ──────────────────────────────

def _lin_jiaxin() -> dict:
    """真实档案 lin_jiaxin 的等价缩写（context.family = dict[str, 自由文本]）。"""
    return {
        "name": "林佳欣",
        "role": "注册护士 / 线上艺术品竞拍爱好者，36岁，加拿大温哥华",
        "age": 36,
        "gender": "female",
        "background": "双子座，O型血，36岁，生长于温哥华，父亲林志强（英文名 Michael Lin）"
                      "是加拿大华裔IT高管，沉稳理性。",
        "names": {"english": "Fiona", "full_western": "Fiona Claire Lin"},
        "context": {
            "family": {
                "father": "林志强（Michael Lin），加拿大华裔IT高管，沉稳理性，是佳欣的精神支柱",
                "mother": "陈慧琳（Elena Lin），香港/菲律宾混血艺术品策展人，热情浪漫",
                "brother": "林景然（Jason Lin），佳士得拍卖公司香港市场经理",
                "daughter": "林佳悦（Jade），小学高年级，活泼好奇",
            },
            "specific_memories": [
                "女儿 Jade 书桌上的全家福：画里妈妈旁边留了一个空位",
                "哥哥 Jason 在佳士得拍卖一幅画时给她打电话报价",
            ],
            "hobbies": ["健身", "瑜伽"],
        },
    }


def _mizuki() -> dict:
    """触发本模块存在理由的真实缺陷：丈夫在文档里一律写全名 José Luis Jerónimo。"""
    return {
        "name": "美月",
        "gender": "female",
        "context": {
            "family": {
                "ex_husband": "José Luis Jerónimo，西班牙人，2018 年 5 月底协议离婚",
            },
        },
    }


# ── 主链路 ────────────────────────────────────────────────────────────────

def test_structured_family_yields_kin_entity_names():
    out = build_entity_aliases(_lin_jiaxin())
    assert "林志强" in out["爸爸"] and "林志强" in out["父亲"] and "林志强" in out["father"]
    assert "michael lin" in out["爸爸"]
    assert "陈慧琳" in out["妈妈"] and "elena lin" in out["母亲"]
    assert "林佳悦" in out["女儿"] and "jade" in out["女儿"]
    assert "林景然" in out["哥哥"] and "林景然" in out["弟弟"]


def test_given_name_fragment_emitted_but_never_single_char():
    """文档可能只写「志强」；但单字（林）会大面积误命中，绝不产出。"""
    out = build_entity_aliases(_lin_jiaxin())
    assert "志强" in out["父亲"]
    assert all(len(v) >= 2 for vals in out.values() for v in vals)


def test_jose_luis_defect_case_reaches_husband_keys():
    """客户问「你老公…」，档案里只有 ex_husband → 老公/丈夫/husband 三键都要能召回。"""
    out = build_entity_aliases(_mizuki())
    for key in ("老公", "丈夫", "husband", "前夫"):
        assert "josé luis jerónimo" in out[key]
        assert "josé luis" in out[key]
        assert "josé" in out[key]


def test_current_spouse_wins_over_ex():
    prof = {
        "gender": "female",
        "context": {"family": {
            "husband": "Diego Fernández，马德里建筑师",
            "ex_husband": "José Luis Jerónimo，西班牙人",
        }},
    }
    out = build_entity_aliases(prof)
    assert "diego fernández" in out["老公"]
    assert not any("josé" in v for v in out["老公"])
    assert "josé luis jerónimo" in out["前夫"]      # 前夫键仍如实指向前任


def test_partner_spills_by_gender():
    female = build_entity_aliases(
        {"gender": "female", "context": {"family": {"partner": "Diego Fernández"}}})
    assert "diego fernández" in female["老公"]
    male = build_entity_aliases(
        {"gender": "male", "context": {"family": {"partner": "Elena Rossi"}}})
    assert "elena rossi" in male["老婆"]
    unknown = build_entity_aliases(
        {"context": {"family": {"partner": "Elena Rossi"}}})
    assert "老公" not in unknown and "老婆" not in unknown   # 性别未知不猜
    assert "elena rossi" in unknown["伴侣"]


def test_children_and_parents_union_keys():
    out = build_entity_aliases(_lin_jiaxin())
    assert "林佳悦" in out["孩子"]
    assert "林志强" in out["父母"] and "陈慧琳" in out["父母"]


# ── 自由文本轨（只抽拉丁专名）───────────────────────────────────────────────

def test_free_text_adjacent_latin_name():
    prof = {"background": "工作了3个月之后和一个西班牙人相恋，我丈夫José Luis Jerónimo比我大五岁。"}
    out = build_entity_aliases(prof)
    assert "josé luis jerónimo" in out["老公"] and "josé" in out["丈夫"]


def test_free_text_explicit_naming_marker():
    prof = {"background": "父亲林志强（英文名 Michael Lin）是加拿大华裔IT高管，沉稳理性。"}
    out = build_entity_aliases(prof)
    assert "michael lin" in out["父亲"] and "michael" in out["爸爸"]
    # 自由文本里的 CJK 一律不抽（「父亲因此病倒」类误抽的根因）
    assert all("林志强" not in v for v in out["父亲"])


def test_free_text_english_kinship_term():
    out = build_entity_aliases({"background": "My husband José Luis Jerónimo is Spanish."})
    assert "josé luis jerónimo" in out["老公"]


def test_specific_memories_are_scanned():
    out = build_entity_aliases({
        "context": {"specific_memories": ["哥哥 Jason 在佳士得拍卖一幅画时给她打电话"]}})
    assert "jason" in out["哥哥"]


def test_family_as_plain_string_falls_back_to_text_track():
    """persona_manager 容忍 family 是 str/list；此时只走自由文本轨。"""
    out = build_entity_aliases({"context": {"family": "丈夫 José Luis Jerónimo，西班牙人"}})
    assert "josé luis jerónimo" in out["老公"]


# ── 误抽负例（这些如果产出，检索会**稳定地**注入错段）──────────────────────

@pytest.mark.parametrize("background", [
    "曾经拼了命创业，最后一地鸡毛；父亲因此病倒瘫痪，妹妹还在读大学。",   # 称谓后是动词短语
    "哥哥在 Bloomberg 工作，弟弟去 Madrid 读书。",                      # 「在/去」= 已拐进机构/地名
    "父亲是加拿大华裔IT高管，母亲是HR总监。",                            # 全大写缩写不是人名
    "我妈妈退休了，爸爸也退休了。",                                      # 无任何专名
    "妹妹和 Sarah 一起去了马尼拉。",                                     # 「和」隔开的不是妹妹的名字
    "老婆Ana很温柔。",                                                   # 3 字符拉丁片段太短（会命中 banana）
])
def test_free_text_false_positives_rejected(background):
    assert build_entity_aliases({"background": background}) == {}


@pytest.mark.parametrize("value", [
    "在世", "已故", "高中老师", "白天上班带娃", "退休", "她", "我最敬佩的人", "不详",
])
def test_structured_status_and_phrases_are_not_names(value):
    assert build_entity_aliases({"context": {"family": {"mother": value}}}) == {}


def test_unregistered_family_key_ignored():
    """context 下同层还有 german_cultural_heritage.words_he_uses 这类非亲属子键。"""
    prof = {"context": {"family": {"words_he_uses": "José Luis Jerónimo"}}}
    assert build_entity_aliases(prof) == {}


def test_surname_particles_never_stand_alone():
    out = build_entity_aliases({"context": {"family": {"wife": "María de la Cruz，西班牙人"}}})
    assert "maría de la cruz" in out["老婆"] and "maría" in out["老婆"]
    assert "de" not in out["老婆"] and "la" not in out["老婆"] and "cruz" not in out["老婆"]


def test_short_latin_surname_not_emitted_alone():
    """``lin`` 会命中 ``online``——打分是子串匹配不是分词。"""
    out = build_entity_aliases(_lin_jiaxin())
    assert all(v != "lin" for vals in out.values() for v in vals)


# ── 上限 / 健壮性 / 确定性 ─────────────────────────────────────────────────

def test_values_capped_per_key():
    prof = {"context": {"family": {
        "brother": "张景然（Karen Zhang），佳士得香港市场经理",
        "elder_brother": "林景明（Jason Lee），律师",
        "younger_brother": "陈志远（Bruce Chen），医生",
    }}}
    out = build_entity_aliases(prof)
    assert all(len(v) <= MAX_VALUES_PER_KEY for v in out.values())
    assert len(out["哥哥"]) == MAX_VALUES_PER_KEY


def test_max_keys_respected():
    out = build_entity_aliases(_lin_jiaxin(), max_keys=3)
    assert len(out) == 3


@pytest.mark.parametrize("profile", [
    None, "", "not a dict", 123, [], {}, ["José Luis"],
    {"context": "oops"},
    {"context": {"family": "   "}},
    {"context": {"family": []}},
    {"context": {"family": {"father": None}}},
    {"context": {"family": {"father": ""}}},
    {"context": {"family": {"father": {"name": "José"}}}},
    {"context": {"family": {"father": 12345}}},
    {"background": 12345},
    {"background": None, "context": {"specific_memories": {"a": "b"}}},
    {"gender": 5, "context": {"family": {"father": "  "}}},
])
def test_bad_input_returns_empty_without_raising(profile):
    assert build_entity_aliases(profile) == {}


def test_list_valued_family_entry():
    out = build_entity_aliases(
        {"context": {"family": {"brother": ["林景然（Jason Lin），经理", "陈志远，医生"]}}})
    assert "林景然" in out["哥哥"]


def test_deterministic_across_calls():
    a = build_entity_aliases(_lin_jiaxin())
    b = build_entity_aliases(_lin_jiaxin())
    assert a == b
    assert list(a.items()) == list(b.items())
    assert all(isinstance(v, tuple) for v in a.values())


def test_all_latin_values_lowercased():
    """``expand_query_tokens`` 对非 ASCII 不做 lower，而打分在 text.lower() 上做
    子串匹配——大写开头的 ``José`` 进了 token 集也永远命不中。"""
    out = build_entity_aliases(_mizuki())
    assert all(v == v.lower() for vals in out.values() for v in vals)


# ── 与全局表的合并 + 端到端证明缺陷被修 ───────────────────────────────────

def test_merge_appends_instead_of_overwriting():
    merged = merge_query_aliases(pbs._QUERY_ALIASES, build_entity_aliases(_mizuki()))
    assert "丈夫" in merged["老公"] and "husband" in merged["老公"]   # 全局值不丢
    assert "josé luis jerónimo" in merged["老公"]                     # 实体值补一跳
    assert merged["妈妈"] == pbs._QUERY_ALIASES["妈妈"]               # 未涉及的键原样


def test_merge_bad_input_soft_fails():
    assert merge_query_aliases({}, {}) == {}
    assert merge_query_aliases({"老公": ("丈夫",)}, None) == {"老公": ("丈夫",)}


def test_end_to_end_husband_query_now_scores_on_narrative_chunk():
    """真实缺陷复现：叙事段只写全名，「老公」关键词分恒 0 → 空注入。

    合并实体别名后同一段落 kw > 0（够不到 sem_floor_kw0 的语义分不再是唯一指望）。
    """
    story = ("那时候我在马德里的餐厅打工，José Luis Jerónimo 每周三都来，"
             "后来我们结婚，他对我很好，会陪我回大阪看母亲。")
    query = "你老公对你好吗"
    merged = merge_query_aliases(pbs._QUERY_ALIASES, build_entity_aliases(_mizuki()))

    raw = pbs.query_tokens(query)
    baseline = set(raw)
    for tip, alts in pbs._QUERY_ALIASES.items():
        if tip in query:
            baseline.update(a.lower() if a.isascii() else a for a in alts)
    assert pbs._keyword_score_weighted(raw, baseline, story) == 0   # 修复前：空注入

    with_entity = set(raw)
    for tip, alts in merged.items():
        if tip in query or tip.lower() in query.lower():
            with_entity.update(a.lower() if a.isascii() else a for a in alts)
    assert pbs._keyword_score_weighted(raw, with_entity, story) > 0
