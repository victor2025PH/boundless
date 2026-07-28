# -*- coding: utf-8 -*-
"""文档轨实体别名（M9）：``build_doc_entity_aliases`` 纯函数门禁。

语料全部取自真实 33k 文档的形态（脱敏改写）：丈夫从不以「丈夫 José」出现、
只有「相恋，西班牙人（José Luis Jerónimo）」事件共现；且他在叙事里同时是
财务总监的儿子（第三人称领属）——这是把 profile 轨紧邻语义换成共现票选的
全部原因，也是本文件负例的主要来源。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.companion.persona_entity_alias import (  # noqa: E402
    DOC_MIN_VOTES,
    build_doc_entity_aliases,
)

# 复刻真实文档关键形态的最小语料（每个真亲属中英各一行 = 各 2 票）
_DOC = "\n".join([
    "性别：女",
    "婚否：离婚没孩子",
    "父亲名字：José Leandro Navarro （西班牙人）2019年5月 春天逝世 享年 68岁",
    "Father's Name: José Leandro Navarro (Spanish), passed away in May 2019",
    "工作了3个月之后2017年1月和一个西班牙人相恋，西班牙人（José Luis Jerónimo）"
    "25岁 在相识相恋了7个月之后双方商量并且回到马德里结婚",
    "Three months into this job, I met and started a relationship with a Spanish"
    " man, José Luis Jerónimo. After dating for 7 months, we got married.",
    "在11月低的时候她把她儿子介绍给我，西班牙人（José Luis Jerónimo）25岁和我同龄",
    "In late November, she introduced her son, José Luis Jerónimo, who was 25.",
    "姑姑名字：María Joséfa Navarro 西班牙人 父亲的妹妹 58岁",
    "Aunt's Name: María Joséfa Navarro, Spanish, father's younger sister, 58.",
])


def _out():
    return build_doc_entity_aliases(_DOC)


# ── 正向：三个真亲属都抽对 ───────────────────────────────────────────────────

def test_husband_extracted_via_event_words_not_kinship_adjacency():
    """「相恋/结婚/married」共现即可——全文没有一处「丈夫+名字」紧邻。"""
    out = _out()
    for key in ("老公", "丈夫", "前夫", "husband"):
        assert "josé luis jerónimo" in out.get(key, ()), key
        assert "josé luis" in out.get(key, ()), key


def test_father_and_aunt_extracted_from_card_lines():
    out = _out()
    assert "josé leandro navarro" in out.get("父亲", ())
    assert "josé leandro navarro" in out.get("爸爸", ())
    # 姑姑行里同时有「父亲的妹妹」——最近标记（姑姑）胜出，父亲不沾 María
    assert "maría joséfa navarro" in out.get("姑姑", ())
    assert "maría joséfa navarro" not in out.get("父亲", ())
    assert "maría joséfa navarro" not in out.get("妹妹", ())


def test_shared_fragment_banned_across_persons():
    """父亲 José Leandro × 前夫 José Luis 都派生 "josé" → 两边都不发。

    发了 = 查爸爸时中前夫段（错误注入是稳定的，比漏更糟）。全名/双词片段
    互不相同，保留。
    """
    out = _out()
    for key, vals in out.items():
        assert "josé" not in vals, f"{key} 泄漏了跨人共享片段"


def test_third_person_possessive_does_not_vote():
    """「她把她儿子介绍给我」/「her son」——别人的儿子，不给 son 投票。

    不挡的话 son 票与 partner 票打平 → 整个人被歧义弃权 → 丈夫再次丢失
    （正是真实文档踩的坑）。
    """
    out = _out()
    assert "josé luis jerónimo" not in out.get("儿子", ())
    assert "儿子" not in out or "josé luis" not in out["儿子"]
    # 而 partner 票不受牵连（见 test_husband_*）


def test_header_junk_never_becomes_name():
    """``Aunt's Name:`` 这类表头是 Titlecase 双词，形态上像人名——黑名单挡。"""
    out = _out()
    flat = {v for vals in out.values() for v in vals}
    for junk in ("aunt's name", "father's name", "name"):
        assert junk not in flat


# ── 负向：安全护栏逐条 ───────────────────────────────────────────────────────

def test_ascii_marker_is_word_bounded():
    """"season"/"person"/"reason" 里的 son 不算儿子标记（真实语料实锤）。"""
    doc = "\n".join([
        "That season, José Luis Jerónimo and I traveled a lot.",
        "For that reason, José Luis Jerónimo stayed in Madrid.",
    ])
    assert build_doc_entity_aliases(doc) == {}


def test_place_tail_names_are_not_persons():
    """求婚地点 Palacio de Cristal 是场所不是老公。"""
    doc = "\n".join([
        "他在马德里的 Palacio de Cristal 向我求婚",
        "He proposed to me at Palacio de Cristal in Madrid.",
    ])
    flat = {v for vals in build_doc_entity_aliases(doc).values() for v in vals}
    assert not any("palacio" in v or "cristal" in v for v in flat)


def test_single_token_latin_never_extracted():
    """单词 Titlecase 是城市/品牌重灾区（Barcelona）——多词硬性要求。"""
    doc = "\n".join([
        "我和前夫在 Barcelona 结婚",
        "My ex-husband and I got married in Barcelona.",
    ])
    assert build_doc_entity_aliases(doc) == {}


def test_below_min_votes_dropped():
    """全文只共现一次 → 不产出（宁可漏不可错；双语文档真亲属天然 ≥2 票）。"""
    doc = "父亲名字：Carlos Miguel Santos 西班牙人"
    assert DOC_MIN_VOTES >= 2
    assert build_doc_entity_aliases(doc) == {}
    assert build_doc_entity_aliases(doc + "\n" + doc) != {}


def test_role_tie_means_abstain():
    """两角色同票 = 归属歧义，整个人弃权。"""
    doc = "\n".join([
        "父亲带着 Marco Antonio Ruiz 来看我",
        "哥哥带着 Marco Antonio Ruiz 去了机场",
    ])
    out = build_doc_entity_aliases(doc)
    flat = {v for vals in out.values() for v in vals}
    assert not any("marco" in v for v in flat)


def test_equidistant_markers_abstain_that_occurrence():
    """名字两侧等距出现不同角色标记 → 该次出现弃权（不是随机二选一）。"""
    # 「父亲␣名字␣母亲」两侧各隔 1 个空格 = 真等距（对称构造钉语义）
    doc = "\n".join([
        "父亲 Diego Fernández Gómez 母亲一起来了",
        "父亲 Diego Fernández Gómez 母亲一起来了",
    ])
    out = build_doc_entity_aliases(doc)
    flat = {v for vals in out.values() for v in vals}
    # 等距歧义 → 0 票 → 不产出（若实现改成「随便挑一个」这里会开始产出）
    assert not any("diego" in v for v in flat)


def test_gender_routes_partner_event_keys():
    base = [
        "和一个西班牙人相恋，西班牙人（Pablo Andrés Serrano）30岁，我们结婚了",
        "I started a relationship with Pablo Andrés Serrano and we got married.",
    ]
    male = build_doc_entity_aliases("\n".join(["性别：男"] + base))
    assert "pablo andrés serrano" in male.get("老婆", ())
    assert "老公" not in male
    unknown = build_doc_entity_aliases("\n".join(base))
    assert "pablo andrés serrano" in unknown.get("老公", ())
    assert "pablo andrés serrano" in unknown.get("老婆", ())


def test_deterministic_and_lowercase():
    a, b = _out(), _out()
    assert a == b
    for vals in a.values():
        for v in vals:
            assert v == v.lower()


def test_dirty_inputs_return_empty():
    for bad in (None, "", 123, "   \n  ", "只有中文没有名字", "x" * 200_000):
        out = build_doc_entity_aliases(bad)
        assert isinstance(out, dict)
