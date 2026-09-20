# -*- coding: utf-8 -*-
"""人设长传记检索层（D2 + H1 混合检索）门禁——全部离线，不碰生产库不调 LLM/网络。

覆盖：
1. store —— 分块入库（段落边界/超长硬切/200k 截断）、meta、整体替换、delete、
   坏输入/DB 异常软失败；embedding 列幂等演进。
2. search —— CJK bigram / 拉丁词 / min_hits / top_k；无 embed = 旧关键词行为；
   stub embed 近义命中（前夫→丈夫）+ 闲聊拒入；混合权重。
3. build_bio_block —— 预算硬夹、无命中 None、无库存 None。
4. 路由 —— TestClient：flag 关 403 / POST→GET→DELETE / 空文本 400 / 超长 413。
5. 注入接线 —— flag 开且检索命中时 ``_persona_bio_block`` 进 user_context。
"""

import asyncio
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.companion import persona_bio_store as pbs
from src.companion.persona_bio_store import (
    MAX_BIO_CHARS,
    PersonaBioStore,
    split_bio_chunks,
)


# ── 语料：4 段各 ~380 字（保证段落不被贪心合并成同一块）─────────────────────

def _pad(s: str, n: int = 380) -> str:
    return s + "。" + "记" * max(0, n - len(s) - 1)


_P_BIRTH = _pad("美月1994年出生在意大利都灵，父亲佐藤健一是建筑师，母亲经营一家花店")
_P_UNI = _pad("2013年考入都灵大学经济系，大学四年都在这座城市度过，课余在市中心的酒店兼职前台")
_P_EX = _pad("前夫Marco是都灵本地人，经营一家家族餐厅，两人2019年和平离婚，Marco后来搬去了米兰")
_P_WORK = _pad("2021年移居巴塞罗那，现任五星级酒店运营总监助理，负责客房与前台的协调工作")
_BIO = "\n\n".join([_P_BIRTH, _P_UNI, _P_EX, _P_WORK])

_PID = "mizuki_sato"

# I1 语义单元分块后每段 380 字 → 2 个单元（正文块 + 尾碎片），全库 8 个。
# 断言里不写死 8，跟随实现算，避免以后调 UNIT_* 常量时满文件改数字。
_BIO_UNITS = len(split_bio_chunks(_BIO))


# ── 确定性 stub embed（hash bag-of-tokens；近义词共享槽位；绝不打网络）─────

_STUB_DIM = 8
# 近义/同族 → 同一槽；天气等闲聊槽与传记槽正交，保证 sem 进不了 floor。
_STUB_SLOTS = {
    "前夫": 0, "丈夫": 0, "离婚": 0, "marco": 0, "妻子": 0,
    "大学": 1, "城市": 1, "都灵": 1, "经济": 1,
    "酒店": 2, "巴塞": 2, "运营": 2,
    "天气": 3, "今天": 3,
}


def _stub_embed(text: str) -> Optional[List[float]]:
    """确定性 bag-of-markers → 固定维 L2 向量（同义词共槽 → 高余弦）。"""
    s = str(text or "").lower()
    vec = [0.0] * _STUB_DIM
    hit = False
    for marker, slot in _STUB_SLOTS.items():
        if marker in s:
            vec[slot] += 1.0
            hit = True
    if not hit:
        # 无标记 → 均匀微噪声槽，互余弦接近但远低于 sem_floor=0.55 对正交传记块
        # （实际对正交块 cosine≈0）；给空文本返回 None 模拟部分失败。
        if not s.strip():
            return None
        vec[_STUB_DIM - 1] = 0.01
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


@pytest.fixture(autouse=True)
def _bio_db(tmp_path):
    """模块级单例指到 tmp 库；钉死 embed_fn=None（禁懒取真端点，保离线）。

    实体别名 provider 同样显式禁用：默认值会懒取 ``PersonaManager`` 单例，
    既让既有用例的期望 token 集漂移，又把测试耦合到别的用例留下的全局单例。
    需要它的用例自己 ``set_alias_provider`` 打开。
    """
    pbs.reset_persona_bio_store()
    pbs.set_embed_fn(None)  # 显式禁用：既有用例走纯关键词旧路径
    pbs.set_alias_provider(None)
    pbs.configure_persona_bio_store(str(tmp_path / "bio_test.db"), embed_fn=None)
    yield
    pbs.reset_persona_bio_store()
    pbs.set_alias_provider(None)


# ── 1. 分块纯函数 ────────────────────────────────────────────────────────────

def test_split_chunks_paragraph_boundary():
    """段落边界优先：不同段的内容绝不落进同一单元。

    I1 起单元目标 240 字（不再是 600 大块），380 字的段落切成「正文块 + 尾
    碎片」两个单元 → 原「chunks[1] 就是第二段开头」的写法不再成立，改为
    「每段首句各自成为某个单元的开头」。
    """
    chunks = split_bio_chunks(_BIO)
    assert len(chunks) == _BIO_UNITS >= 8
    assert chunks[0].startswith("美月1994年")
    assert any(c.startswith("2013年考入都灵大学") for c in chunks)
    # 单元正文 ≤ UNIT_MAX，叠加重叠前缀最多再多 UNIT_OVERLAP
    assert all(len(c) <= pbs.UNIT_MAX_CHARS + pbs.UNIT_OVERLAP_CHARS for c in chunks)
    # 段落纯度：任一单元不得同时含两段的特征词
    for c in chunks:
        assert not ("都灵大学经济系" in c and "前夫Marco" in c)


def test_split_chunks_unit_sizes_and_sentence_boundary():
    """单元长度落在 [UNIT_MIN, UNIT_MAX+OVERLAP]，且按句末标点切（不腰斩句子）。"""
    sents = [f"第{i:02d}年她在都灵大学经济系修读了不同的课程并做了详细笔记。"
             for i in range(24)]
    chunks = split_bio_chunks("".join(sents))
    assert len(chunks) >= 3
    for c in chunks:
        assert pbs.UNIT_MIN_CHARS <= len(c) <= pbs.UNIT_MAX_CHARS + pbs.UNIT_OVERLAP_CHARS
    # 除首单元（无重叠前缀）外都以句末标点收尾；句子不被切断
    assert all(c.endswith("。") for c in chunks)


def test_split_chunks_overlap_only_inside_paragraph():
    """相邻单元重叠：同段内切出的相邻单元有重叠，跨段边界没有。"""
    para = "".join(f"第{i:02d}条都灵大学的记录说明文字内容补充。" for i in range(30))
    units = split_bio_chunks(para)
    assert len(units) >= 2
    for prev, cur in zip(units, units[1:]):
        tail = prev[-pbs.UNIT_OVERLAP_CHARS:]
        assert cur.startswith(tail.lstrip())     # 段内相邻 → 重叠前缀

    two = split_bio_chunks("甲段内容。" * 40 + "\n\n" + "乙段内容。" * 40)
    first_of_b = next(u for u in two if u.startswith("乙段内容"))
    assert "甲段" not in first_of_b               # 跨段不加重叠


def test_split_chunks_breaks_on_script_switch():
    """中英脚本切换 = 主题边界：成规模的中/英段各自成单元，inline 专名不误切。"""
    cjk = "她在都灵大学读经济系并留在这座城市。" * 10
    latin = "She studied economics at the University of Turin. " * 6
    units = split_bio_chunks(cjk + latin)
    assert len(units) >= 2
    for u in units:
        assert not ("都灵大学" in u and "University of Turin" in u)
    # inline 拉丁专名（< _SCRIPT_MIN_RUN）不得把中文叙事切碎
    inline = split_bio_chunks(_P_EX)
    assert any("前夫Marco是都灵本地人" in u for u in inline)


def test_split_chunks_merges_short_paragraphs():
    text = "第一段。\n第二段。\n\n第三段。"
    chunks = split_bio_chunks(text)
    assert chunks == ["第一段。\n第二段。\n第三段。"]  # 短段碎片回并成一块


def test_split_chunks_title_line_merges_with_next_unit():
    """字段式标题行（冒号收尾）归属**下文**：标题 + 内容 = 一个语义单元。

    A/B 校准（2026-07-28，真实 33k 文档）：``"现在的工作："`` 被并给前一单元后
    单独成 5 字碎片，短文本向量方向不稳 → 拿到全场最高余弦把正确答案挤到第二。
    """
    lead = "第一段的叙事内容在这里反复展开说明背景。" * 7      # 140 字，自成单元
    body = "她在巴塞罗那的五星级酒店担任运营总监助理。" * 7     # 147 字
    units = split_bio_chunks(lead + "\n现在的工作：\n" + body)

    title_unit = next(u for u in units if "现在的工作：" in u)
    assert title_unit.startswith("现在的工作：")     # 标题在自己单元的**开头**
    assert "五星级酒店" in title_unit               # 后文内容跟着标题走
    assert "现在的工作" not in units[0]              # 没被并进前一单元
    assert len(units) == 2

    # 对照：不带冒号的同样短行仍按旧行为并给**前**一单元
    plain = split_bio_chunks(lead + "\n现在的工作\n" + body)
    assert plain[0].endswith("现在的工作")


def test_split_chunks_title_merge_falls_back_when_oversized():
    """标题 + 后文会超 ``unit_max`` → 退回与前一单元合并（旧行为）。"""
    lead = "甲甲甲甲甲甲甲甲甲。" * 4        # 40 字
    body = "乙乙乙乙乙乙乙乙乙乙乙乙。" * 5   # 65 字（单单元）
    units = split_bio_chunks(
        lead + "\n现在的工作：\n" + body,
        target_chars=60, min_chars=30, max_unit_chars=70, overlap_chars=0,
    )
    # 6 + 1 + 65 = 72 > 70 → 不与后并；40 + 1 + 6 = 47 ≤ 70 → 与前并
    assert units[0].endswith("现在的工作：")
    assert units[1].startswith("乙乙")
    assert not any("现在的工作" in u and "乙乙" in u for u in units)


def test_split_chunks_hard_cut_oversized_paragraph():
    """无句读超长串 → 按 target 硬切；同段相邻单元各带 OVERLAP 前缀。"""
    chunks = split_bio_chunks("长" * 1500)
    assert chunks[0] == "长" * pbs.UNIT_TARGET_CHARS
    assert all(len(c) >= pbs.UNIT_MIN_CHARS for c in chunks)
    assert all(len(c) <= pbs.UNIT_MAX_CHARS + pbs.UNIT_OVERLAP_CHARS for c in chunks)
    # 首单元外每个单元多带 OVERLAP 字前缀 → 总长 = 原文 + (n-1)*OVERLAP
    assert (sum(len(c) for c in chunks)
            == 1500 + (len(chunks) - 1) * pbs.UNIT_OVERLAP_CHARS)
    assert split_bio_chunks("") == []
    assert split_bio_chunks("   \n\n  ") == []


def test_split_chunks_legacy_mode_restores_old_behavior():
    """``chunk_mode: legacy`` 回退旧「段落贪心合并到 600」行为（回滚闸）。"""
    assert [len(c) for c in split_bio_chunks("长" * 1500, mode="legacy")] == [600, 600, 300]
    assert len(split_bio_chunks(_BIO, mode="legacy")) == 4
    pbs.set_retrieval_cfg({"chunk_mode": "legacy"})
    try:
        assert len(split_bio_chunks(_BIO)) == 4        # 配置口也认
    finally:
        pbs.set_retrieval_cfg(None)


# ── 1b. store：入库 / meta / 替换 / delete / 软失败 ──────────────────────────

def test_replace_and_meta_roundtrip(tmp_path):
    store = PersonaBioStore(str(tmp_path / "s.db"))
    res = store.replace_bio_doc(_PID, _BIO)
    assert res == {"chunks": _BIO_UNITS, "chars": len(_BIO)}
    meta = store.get_bio_meta(_PID)
    assert meta["chunks"] == _BIO_UNITS and meta["chars"] == len(_BIO)
    assert meta["updated_ts"] > 0
    assert meta["preview"] == _P_BIRTH[:120]
    # 覆盖度三计数（M9 收尾）：无 embed_fn 入库 → 向量/句向量都是 0（缺口可见），
    # 语料含「前夫Marco」共现但不足 min_votes → 别名 0；键必须存在。
    assert meta["emb_chunks"] == 0 and meta["sents"] == 0
    assert meta["alias_keys"] == 0
    assert store.get_bio_meta("nobody") is None


def test_meta_coverage_counts_reflect_vectors_and_aliases(tmp_path):
    """有向量/别名的库存，meta 三计数如实反映（运维不再靠裸 SQL 看缺口）。"""
    store = PersonaBioStore(str(tmp_path / "cov.db"), embed_fn=lambda t: [1.0, 0.0])
    store.replace_bio_doc(_PID, _HUSBAND_DOC)
    meta = store.get_bio_meta(_PID)
    assert meta["emb_chunks"] == meta["chunks"] > 0
    assert meta["sents"] > 0
    assert meta["alias_keys"] > 0


def test_replace_truncates_at_200k(tmp_path):
    store = PersonaBioStore(str(tmp_path / "s.db"))
    res = store.replace_bio_doc(_PID, "五" * (MAX_BIO_CHARS + 5000))
    assert res["chars"] == MAX_BIO_CHARS
    assert store.get_bio_meta(_PID)["chars"] == MAX_BIO_CHARS


def test_replace_swaps_old_chunks_entirely(tmp_path):
    store = PersonaBioStore(str(tmp_path / "s.db"))
    store.replace_bio_doc(_PID, _BIO)
    assert store.search_bio(_PID, "你前夫Marco现在在哪")   # 旧内容可命中
    new_doc = _pad("新版本传记：她在东京银座的百货公司上班")
    store.replace_bio_doc(_PID, new_doc)
    assert store.search_bio(_PID, "你前夫Marco现在在哪") == []   # 旧块整体清掉
    meta = store.get_bio_meta(_PID)
    # I1：380 字段落 → 2 个语义单元（原 legacy 大块口径是 1）
    assert meta["chunks"] == len(split_bio_chunks(new_doc)) == 2
    assert "东京" in meta["preview"]


def test_delete_bio_doc(tmp_path):
    store = PersonaBioStore(str(tmp_path / "s.db"))
    store.replace_bio_doc(_PID, _BIO)
    assert store.delete_bio_doc(_PID) is True
    assert store.get_bio_meta(_PID) is None
    assert store.search_bio(_PID, "你前夫Marco现在在哪") == []
    assert store.delete_bio_doc(_PID) is False   # 二次删无货
    assert store.delete_bio_doc("") is False


def test_units_are_contiguous_reverses_overlap_prefix():
    """同段判据＝反解 `_apply_overlap` 的重叠前缀（不给库表加段落列）。"""
    prev = "他带我去了那家餐厅，晚饭吃到很晚才回酒店休息。"
    cur = prev[-8:] + "第二天早上我们又去了海边散步。"
    assert pbs.units_are_contiguous(prev, cur, 8)
    assert not pbs.units_are_contiguous(prev, "毫不相干的另一段开头。", 8)
    assert not pbs.units_are_contiguous(prev, cur, 0)      # 关重叠 → 无信号
    assert not pbs.units_are_contiguous("", cur, 8)


def test_stitch_units_removes_duplicated_overlap():
    a = "第一段正文写到这里就被长度切开了所以有下一半。"
    b = a[-6:] + "下一半继续讲同一件事直到段落结束。"
    stitched = pbs.stitch_units([a, b], 6)
    assert stitched.startswith(a) and stitched.count(a[-6:]) == 1
    # 不连续的两块 → 换行相接，不硬拼成一句
    assert "\n" in pbs.stitch_units([a, "另起一段。"], 6)


def test_neighbor_span_only_eats_same_paragraph_neighbours():
    same_a = "甲段前半" * 12
    same_b = same_a[-5:] + "甲段后半" * 12
    other = "乙段独立内容" * 10
    texts = [other, same_a, same_b, other]
    # 命中 same_a → 向右吃到 same_b，左边的 other 不连续不吃
    assert pbs.neighbor_span(texts, 1, 5) == (1, 3)
    # 命中孤立段 → 只有自己
    assert pbs.neighbor_span(texts, 0, 5) == (0, 1)
    assert pbs.neighbor_span([], 0, 5) == (0, 0)
    assert pbs.neighbor_span(texts, 99, 5) == (0, 0)


def test_neighbor_span_capped_by_total_chars():
    """总长封顶：放开会把整段灌进句池，无关句抢走注入预算。"""
    a = "甲" * 300
    b = a[-5:] + "乙" * 300
    c = b[-5:] + "丙" * 300
    texts = [a, b, c]
    assert pbs.neighbor_span(texts, 1, 5, max_chars=10_000) == (0, 3)
    assert pbs.neighbor_span(texts, 1, 5, max_chars=400) == (1, 2)   # 谁都吃不下


def test_neighbor_expand_surfaces_answer_from_adjacent_unit(tmp_path):
    """答案跨单元边界的一类（实录：求婚现场命中、地名在隔壁单元）。

    可控 embed 精确复刻真实现象：**单元级**向量被大段填充稀释（够不到准入门槛
    → 隔壁单元不会自己成为命中），而**句级**向量很锐利。所以答案只能靠「命中
    单元把同段邻居并进句池」捞出来——这正是 neighbor_expand 要解的那一类。
    """
    Q = "他在哪里跟你求婚的"
    near = [0.95, math.sqrt(1 - 0.95 ** 2), 0.0]

    def _fn(text: str):
        s = str(text or "")
        if s == Q:
            return [1.0, 0.0, 0.0]
        if "水晶宫" in s and len(s) < 60:      # 句级：锐利
            return list(near)
        return [0.0, 0.0, 1.0]                 # 单元级/其余：与查询正交

    store = PersonaBioStore(str(tmp_path / "nb.db"), embed_fn=_fn)
    # 尺寸经 split_bio_chunks 实测校准：求婚句落 unit[0]、水晶宫句落 unit[1]，
    # 两者同段（重叠前缀可反解）——正是要复刻的拓扑。
    scene = "他忽然跪下来跟我求婚，我当时整个人都懵了眼泪直接掉下来。" + "情。" * 140
    tail = "那个地方叫水晶宫，玻璃屋顶下面全是湖水的反光。" + "景。" * 80
    store.replace_bio_doc(_PID, scene + tail)
    units = pbs.split_bio_chunks(scene + tail)
    assert "水晶宫" not in units[0] and "水晶宫" in units[1]

    try:
        pbs.set_retrieval_cfg({"neighbor_expand": False})
        off = store.build_bio_block(_PID, Q) or ""
        pbs.set_retrieval_cfg({"neighbor_expand": True})
        on = store.build_bio_block(_PID, Q) or ""
    finally:
        pbs.set_retrieval_cfg(None)

    assert "求婚" in off and "水晶宫" not in off      # 关：地名在隔壁，捞不到
    assert "水晶宫" in on                              # 开：句级重排把它捞出来


def test_sem_veto_blocks_lexical_coincidence_but_spares_real_hits(tmp_path):
    """词面巧合（单个泛化词命中）被语义否决，真关键词命中不受影响。

    实录：问「家里有宠物吗」，`pet` 全库零命中，只因 `home` 命中
    `work-from-home` 就把工作段注入了——而那块语义分只有 0.44。
    """
    def _fn(text: str):
        s = str(text or "")
        if "pet" in s.lower():                 # 查询：与两块都不太像
            return [1.0, 0.0, 0.0]
        if "hometown" in s.lower():            # 词面命中但语义远（0.30）
            return [0.30, math.sqrt(1 - 0.30 ** 2), 0.0]
        return [0.70, math.sqrt(1 - 0.70 ** 2), 0.0]   # 语义近（0.70）

    store = PersonaBioStore(str(tmp_path / "veto.db"), embed_fn=_fn)
    store.replace_bio_doc(
        _PID,
        "I left my hometown and moved to Barcelona for a work-from-home job "
        "that finally paid enough to cover rent." + " detail." * 40
        + "\n\nWe keep a small dog and a cat in the flat, and the pet food "
        "budget is honestly out of control." + " more." * 40)

    q = "Do you have any pets at home?"
    try:
        pbs.set_retrieval_cfg({"sem_veto": 0.0})
        off = [h["idx"] for h in store.search_bio(_PID, q, top_k=5)]
        pbs.set_retrieval_cfg({"sem_veto": 0.45})
        on = [h["idx"] for h in store.search_bio(_PID, q, top_k=5)]
    finally:
        pbs.set_retrieval_cfg(None)

    hometown = [r["idx"] for r in store._chunk_view(_PID) if "hometown" in r["low"]]
    pet = [r["idx"] for r in store._chunk_view(_PID) if "pet food" in r["low"]]
    assert hometown and pet
    assert set(hometown) & set(off)              # 关：词面巧合放进来了
    assert not (set(hometown) & set(on))         # 开：语义否决
    assert set(pet) & set(on)                    # 真命中不受牵连


_HUSBAND_DOC = "\n".join([
    "性别：女",
    "2017年1月和一个西班牙人相恋，西班牙人（José Luis Jerónimo）25岁，"
    "相恋7个月之后我们在马德里结婚了。" + "情。" * 80,
    "In January 2017, I started a relationship with José Luis Jerónimo,"
    " and after seven months we got married in Madrid. " + "more. " * 40,
    "后来因为种种原因我们协议离婚了，我搬回了巴塞罗那一个人生活。" + "记。" * 80,
])


def test_doc_aliases_persisted_and_used_in_keyword_search(tmp_path):
    """M9 端到端（纯关键词路径）：入库自动派生「老公→josé luis」→ 查询命中。

    文档里「老公/丈夫」二字从未出现——修复前这条查询在关键词轨恒 0。
    """
    store = PersonaBioStore(str(tmp_path / "da.db"), embed_fn=None)
    store.replace_bio_doc(_PID, _HUSBAND_DOC)

    al = store._doc_aliases(_PID)
    assert "josé luis jerónimo" in al.get("老公", ())

    hits = store.search_bio(_PID, "你老公对你好吗", top_k=5)
    assert any("josé luis jerónimo" in h["text"].lower() for h in hits)


def test_doc_aliases_flag_off_restores_old_behavior(tmp_path):
    store = PersonaBioStore(str(tmp_path / "da2.db"), embed_fn=None)
    store.replace_bio_doc(_PID, _HUSBAND_DOC)
    try:
        pbs.set_retrieval_cfg({"doc_aliases": False})
        hits = store.search_bio(_PID, "你老公对你好吗", top_k=5)
    finally:
        pbs.set_retrieval_cfg(None)
    assert not any("josé luis jerónimo" in h["text"].lower() for h in hits)


def test_delete_bio_doc_removes_doc_aliases(tmp_path):
    store = PersonaBioStore(str(tmp_path / "da3.db"), embed_fn=None)
    store.replace_bio_doc(_PID, _HUSBAND_DOC)
    assert store._doc_aliases(_PID)
    store.delete_bio_doc(_PID)
    assert store._doc_aliases(_PID) == {}
    row = store._conn.execute(
        "SELECT 1 FROM persona_bio_aliases WHERE persona_id = ?",
        (_PID,)).fetchone()
    assert row is None


def test_reembed_backfills_doc_aliases_for_legacy_stock(tmp_path):
    """别名表是 M9 后加的：老库存跑一次 reembed 就该自动补上，不必重传文档。"""
    def _fn(text: str):
        return [1.0, 0.0]

    store = PersonaBioStore(str(tmp_path / "da4.db"), embed_fn=_fn)
    store.replace_bio_doc(_PID, _HUSBAND_DOC)
    with store._lock:   # 模拟 M9 之前导入的库存：没有别名行
        store._conn.execute(
            "DELETE FROM persona_bio_aliases WHERE persona_id = ?", (_PID,))
        store._conn.commit()
    store._invalidate_chunk_view(_PID)

    res = store.reembed_bio_doc(_PID)
    assert res and res["aliases_added"] == 1
    assert "josé luis jerónimo" in store._doc_aliases(_PID).get("老公", ())
    # 已有非空别名的库存不重算
    res2 = store.reembed_bio_doc(_PID)
    assert res2["aliases_added"] == 0


def test_reembed_bumps_ts_so_other_instances_see_new_vectors(tmp_path):
    """reembed 必须升 meta.updated_ts——跳进程缓存按 ts 校验（M6），不升的话
    「A 实例点补齐向量，B 实例的无向量视图缓存到重启」。回填路径曾漏了这步。"""
    db = str(tmp_path / "da5.db")
    writer0 = PersonaBioStore(db, embed_fn=None)
    writer0.replace_bio_doc(_PID, _HUSBAND_DOC)

    reader = PersonaBioStore(db, embed_fn=None)
    assert all(r["emb"] is None for r in reader._chunk_view(_PID))  # 缓存无向量视图

    writer = PersonaBioStore(db, embed_fn=lambda t: [1.0, 0.0])
    res = writer.reembed_bio_doc(_PID)
    assert res and res["updated"] > 0

    view = reader._chunk_view(_PID)   # 不重启、不手动失效
    assert any(r["emb"] is not None for r in view)


def test_explain_query_aliases_shows_triggers_and_hits(tmp_path):
    """自测面板的别名可见性：触发键 / 值 / 哪些值真命中了返回块。"""
    pbs.configure_persona_bio_store(str(tmp_path / "ex.db"), embed_fn=None)
    try:
        pbs.replace_bio_doc(_PID, _HUSBAND_DOC)
        hits = pbs.search_bio(_PID, "你老公对你好吗", top_k=5)
        out = pbs.explain_query_aliases(
            _PID, "你老公对你好吗", [h["text"] for h in hits])
        by_key = {r["key"]: r for r in out}
        assert "老公" in by_key
        assert "josé luis jerónimo" in by_key["老公"]["values"]
        assert "josé luis jerónimo" in by_key["老公"]["hits"]   # 真命中才进 hits
        assert "丈夫" in by_key["老公"]["values"]                # 全局表值共存
        # 没触发的键不出现；异常/空 query 软失败
        assert pbs.explain_query_aliases(_PID, "今天天气怎么样", []) == []
        assert pbs.explain_query_aliases("", "", None) == []
    finally:
        pbs.reset_persona_bio_store()


def test_sem_veto_never_fires_without_vectors(tmp_path):
    """无向量（老库/embed 不可用）时 sem=0，否决线不得把纯关键词轨全灭。"""
    store = PersonaBioStore(str(tmp_path / "veto2.db"), embed_fn=None)
    store.replace_bio_doc(_PID, "我在巴塞罗那的一家五星级酒店做前台接待。" * 6)
    try:
        pbs.set_retrieval_cfg({"sem_veto": 0.9, "semantic": True})
        hits = store.search_bio(_PID, "你在哪家酒店工作")
    finally:
        pbs.set_retrieval_cfg(None)
    assert hits, "缺向量时 sem 恒为 0，否决线不该生效"


def test_sem_veto_stands_down_when_embedding_channel_is_blind(tmp_path):
    """全库语义分都趴在地板上 → 嵌入轨对本条查询失灵，不给它否决权。

    这正是 I1/M2 要救的「单元向量被填充稀释、句向量才锐利」那一类：若让一个
    整体失灵的轨去否决唯一有效的关键词轨，答案会被静默掐掉。
    """
    def _fn(text: str):
        s = str(text or "")
        return [1.0, 0.0, 0.0] if s == "他在哪里跟你求婚的" else [0.0, 0.0, 1.0]

    store = PersonaBioStore(str(tmp_path / "veto3.db"), embed_fn=_fn)
    store.replace_bio_doc(_PID, "他忽然跪下来跟我求婚，我当时整个人都懵了。" + "情。" * 120)
    try:
        pbs.set_retrieval_cfg({"sem_veto": 0.45})
        hits = store.search_bio(_PID, "他在哪里跟你求婚的")
    finally:
        pbs.set_retrieval_cfg(None)
    assert hits, "全库 sem≈0 属嵌入轨失灵，否决线必须让位给关键词"


def test_span_slots_widen_with_pool_but_stay_capped():
    """名额随池内单元数放宽——否则扩了句池却不给名额，等于白扩。

    实录：地名句在扩展池里排第 3（0.935），旧的 2 个名额刚好把它切掉；放宽后
    A/B 命中率 90.9%→93.2%。但必须硬封顶：预算是稀缺资源，放太多会挤掉后面命中。
    """
    assert min(pbs._MAX_SENTS_PER_UNIT * 1, pbs._MAX_SENTS_PER_SPAN) == 2  # 单单元不变
    assert min(pbs._MAX_SENTS_PER_UNIT * 2, pbs._MAX_SENTS_PER_SPAN) == 3
    assert min(pbs._MAX_SENTS_PER_UNIT * 9, pbs._MAX_SENTS_PER_SPAN) == \
        pbs._MAX_SENTS_PER_SPAN                                            # 封顶生效

    units = ["甲句得分高。乙句次之。", "丙句第三。丁句垫底。"]
    scores = {"甲句得分高。": 0.9, "乙句次之。": 0.6, "丙句第三。": 0.3, "丁句垫底。": 0.0}
    picked = pbs.pick_span_sentences(
        units, set(), max_sents=3, sim_fn=lambda s: scores.get(s, 0.0))
    assert picked == ["甲句得分高。", "乙句次之。", "丙句第三。"]   # 保文档序
    assert "丁句垫底。" not in picked                            # 0 分不进


def test_span_ranking_keeps_per_unit_sentence_keys():
    """跨单元选句必须**逐单元分句**，不能先拼成整段。

    实录（真实 33k 文档）：一句 240 字的答案句本有句向量（sem 0.467），拼进相邻单元
    后被重新切成 408 字的新字符串 → 查不到向量 → 语义分 0 → 被 `sc > 0` 过滤掉。
    扩展句池反而弄丢了本来有的信号，所以这条不变量比「能不能扩」更要紧。
    """
    a = "甲单元第一句。答案在这里出现。"
    b = "答案在这里出现。乙单元后半句。"        # 与 a 共享一句（重叠所致）
    vecs = {"答案在这里出现。": 0.9, "甲单元第一句。": 0.1, "乙单元后半句。": 0.1}

    seen: List[str] = []

    def sim_fn(sent: str) -> float:
        seen.append(sent)
        return vecs.get(sent, 0.0)      # 拼接产生的新串 → 0，模拟查不到向量

    picked = pbs.pick_span_sentences([a, b], set(), max_sents=1, sim_fn=sim_fn)
    assert picked == ["答案在这里出现。"]
    # 送进 sim_fn 的必须都是入库期存在的句键，不得出现跨单元拼出的新串
    assert all(s in vecs for s in seen), seen
    # 重叠导致的重复句只输出一次
    assert pbs.pick_span_sentences([a, b], set(), max_sents=3,
                                   sim_fn=sim_fn).count("答案在这里出现。") == 1


def test_pick_unit_sentences_unchanged_by_span_refactor():
    """单单元路径行为不变（pick_unit_sentences 现在是 span 版的特例）。"""
    unit = "无关的开头一句。这里提到都灵大学。再一句无关的。"
    assert pbs.pick_unit_sentences(unit, {"都灵", "大学"}) == ["这里提到都灵大学。"]
    assert pbs.pick_unit_sentences(unit, {"完全没有的词"}) == []


def test_neighbor_expand_degrades_when_view_missing(tmp_path):
    """取不到单元视图 → 退回原文，绝不阻断注入。"""
    pbs.replace_bio_doc(_PID, _BIO)
    pbs.set_retrieval_cfg({"neighbor_expand": True})
    try:
        store = pbs.get_persona_bio_store()
        hits = store.search_bio(_PID, "你前夫Marco现在在哪")
        assert hits
        pools = store._sentence_pools("no-such-persona", hits,
                                      pbs._retrieval_settings())
        assert pools == [[h["text"]] for h in hits]
    finally:
        pbs.set_retrieval_cfg(None)


def test_list_bio_personas_is_the_backfill_roster(tmp_path):
    """运维补向量 CLI 的选人源＝传记库自身，删档即出列。"""
    store = PersonaBioStore(str(tmp_path / "s.db"))
    assert store.list_bio_personas() == []
    store.replace_bio_doc("zhao", _BIO)
    store.replace_bio_doc("mizuki", _BIO)
    assert store.list_bio_personas() == ["mizuki", "zhao"]   # 稳定序
    store.delete_bio_doc("zhao")
    assert store.list_bio_personas() == ["mizuki"]


def test_bad_input_soft_fail(tmp_path):
    store = PersonaBioStore(str(tmp_path / "s.db"))
    assert store.replace_bio_doc("", _BIO) is None
    assert store.replace_bio_doc(_PID, "") is None
    assert store.replace_bio_doc(_PID, "   \n  ") is None
    assert store.replace_bio_doc(_PID, None) is None
    assert store.get_bio_meta("") is None
    assert store.search_bio(_PID, "") == []
    assert store.build_bio_block(_PID, "") is None


def test_module_api_soft_fail_on_db_error(monkeypatch):
    """DB 层异常 → 模块级 API 返回 None/空/False，绝不外抛（聊天链保命线）。"""
    def _boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(PersonaBioStore, "replace_bio_doc", _boom)
    monkeypatch.setattr(PersonaBioStore, "get_bio_meta", _boom)
    monkeypatch.setattr(PersonaBioStore, "delete_bio_doc", _boom)
    monkeypatch.setattr(PersonaBioStore, "search_bio", _boom)
    assert pbs.replace_bio_doc(_PID, _BIO) is None
    assert pbs.get_bio_meta(_PID) is None
    assert pbs.delete_bio_doc(_PID) is False
    assert pbs.search_bio(_PID, "都灵大学") == []
    assert pbs.build_bio_block(_PID, "都灵大学") is None

    monkeypatch.setattr(pbs, "get_persona_bio_store", lambda: None)   # 建库失败态
    assert pbs.replace_bio_doc(_PID, _BIO) is None
    assert pbs.build_bio_block(_PID, "都灵大学") is None


# ── 2. search：关键词检索 ────────────────────────────────────────────────────

def test_search_cjk_bigram_hit():
    pbs.replace_bio_doc(_PID, _BIO)
    hits = pbs.search_bio(_PID, "你大学在哪个城市读的呀")
    assert hits and "都灵大学" in hits[0]["text"]      # 大学+城市 双命中锁定大学块
    # 无 embed 路径：score 仍为 keyword 命中数（H1 兼容口径）
    assert hits[0]["score"] >= 2


def test_search_latin_word_hit():
    pbs.replace_bio_doc(_PID, _BIO)
    hits = pbs.search_bio(_PID, "你前夫Marco现在在哪")
    assert hits and "Marco" in hits[0]["text"]        # 前夫(bigram)+marco(拉丁) 双命中
    # 单 token 查询（物理上最多 1 命中）→ min_hits 按 token 数收底，不恒空
    hits2 = pbs.search_bio(_PID, "Marco")
    assert hits2 and "Marco" in hits2[0]["text"]


def test_search_min_hits_filters_generic_question():
    pbs.replace_bio_doc(_PID, _BIO)
    assert pbs.search_bio(_PID, "今天天气如何") == []
    assert pbs.search_bio("nobody", "你大学在哪个城市") == []


def test_query_tokens_drop_conversational_stop_bigrams():
    """真机冒烟回归（2026-07-27，真实 33k 文档）：「你大学在哪个城市读的呀」的
    虚词 bigram（你大/在哪/哪个/读的/的呀）曾把高虚词密度的无关块顶成最高分。
    查询侧任一字符是会话虚词的 bigram 必须剔除；全虚词查询 → 空集 → 不注入。"""
    toks = pbs.query_tokens("你大学在哪个城市读的呀")
    assert "大学" in toks and "城市" in toks
    for bad in ("你大", "学在", "在哪", "哪个", "个城", "读的", "的呀"):
        assert bad not in toks
    assert pbs.query_tokens("你怎么了呀") == set()
    # 内容词不受影响（前/名/家 等刻意不进虚词集）
    toks2 = pbs.query_tokens("前夫叫什么名字")
    assert "前夫" in toks2 and "名字" in toks2


def test_search_top_k_and_ordering(tmp_path):
    # embed_fn=None → 纯关键词；score=命中数，强块排前
    store = PersonaBioStore(str(tmp_path / "rank.db"), embed_fn=None)
    strong = _pad("alpha beta gamma delta 项目全记录")   # 4 拉丁 token 全中
    weak = _pad("alpha beta 项目部分记录")               # 只中 2 个
    store.replace_bio_doc(_PID, strong + "\n\n" + weak)
    hits = store.search_bio(_PID, "alpha beta gamma delta")
    assert len(hits) == 2
    assert hits[0]["score"] > hits[1]["score"]
    assert "gamma" in hits[0]["text"]
    top1 = store.search_bio(_PID, "alpha beta gamma delta", top_k=1)
    assert len(top1) == 1 and top1[0]["idx"] == hits[0]["idx"]


# ── 2b. H1：embedding 列 + 混合检索（stub，零网络）──────────────────────────

def _read_embeddings(store: PersonaBioStore, persona_id: str):
    with store._lock:
        return store._conn.execute(
            "SELECT idx, embedding FROM persona_bio_chunks"
            " WHERE persona_id = ? ORDER BY idx", (persona_id,),
        ).fetchall()


def test_embedding_column_schema_and_keyword_fallback(tmp_path):
    """入库后 embedding 列可读写；无 embed_fn 时列空且检索=旧关键词行为。"""
    store = PersonaBioStore(str(tmp_path / "emb.db"), embed_fn=None)
    store.replace_bio_doc(_PID, _BIO)
    rows = _read_embeddings(store, _PID)
    assert len(rows) == _BIO_UNITS
    assert all((r["embedding"] is None or r["embedding"] == "") for r in rows)
    hits = store.search_bio(_PID, "你大学在哪个城市读的呀")
    assert hits and "都灵大学" in hits[0]["text"]
    assert hits[0]["score"] >= 2          # 纯关键词：score=命中数
    assert "rank" not in hits[0]          # 旧路径不附 hybrid 字段


def test_embedding_written_with_stub(tmp_path):
    store = PersonaBioStore(str(tmp_path / "emb2.db"), embed_fn=_stub_embed)
    store.replace_bio_doc(_PID, _BIO)
    rows = _read_embeddings(store, _PID)
    assert all(r["embedding"] and r["embedding"].startswith("[") for r in rows)
    # reembed：块/句向量与文档别名都已在入库期备齐 → 三个计数都是 0
    assert store.reembed_bio_doc(_PID) == {
        "chunks": _BIO_UNITS, "updated": 0, "sents_added": 0,
        "aliases_added": 0}


def test_hybrid_synonym_hit_ex_husband(tmp_path):
    """「前夫」→ 别名扩展命中「丈夫/离婚」块；闲聊拒入。

    再优化（真机冒烟后）：纯语义擦边不可靠，改为 expand_query_tokens 别名
    整词扩展——kw 不再为 0，但命中块必须是感情叙事。
    """
    store = PersonaBioStore(str(tmp_path / "hyb.db"), embed_fn=_stub_embed)
    pbs.set_retrieval_cfg({
        "semantic": True, "keyword_weight": 0.4, "semantic_weight": 0.6,
        "sem_floor": 0.55, "sem_floor_kw0": 0.62,
    })
    # 感情块刻意用「丈夫/离婚」而非「前夫」，复现词汇错配
    love = _pad("她的丈夫是本地商人，两人2019年和平离婚后各自开始新生活")
    uni = _pad("2013年考入都灵大学经济系，大学四年都在这座城市度过")
    store.replace_bio_doc(_PID, love + "\n\n" + uni)

    hits = store.search_bio(_PID, "你前夫叫什么")
    assert hits, "近义查询应命中感情块（别名扩展或语义）"
    assert "丈夫" in hits[0]["text"] or "离婚" in hits[0]["text"]
    assert hits[0].get("kw", 0) >= 1   # 别名「丈夫/离婚」计入关键词
    assert hits[0]["score"] == int(round(hits[0]["rank"] * 100))

    assert store.search_bio(_PID, "今天天气真好") == []


def test_expand_query_tokens_ex_husband_aliases():
    toks = pbs.expand_query_tokens("你前夫叫什么名字")
    assert "前夫" in toks
    assert "丈夫" in toks and "离婚" in toks
    assert "husband" in toks or "divorce" in toks


def test_expand_query_tokens_kinship_aliases():
    """口语称谓 ↔ 书面称谓（真实 33k 文档：母亲 29 次 / 父亲 37 次，
    「妈妈」「爸爸」全文零出现 → kw 恒 0 被 sem_floor_kw0 拒掉）。"""
    mom = pbs.expand_query_tokens("你妈妈现在做什么")
    assert "母亲" in mom and "老妈" in mom and "mother" in mom
    dad = pbs.expand_query_tokens("你爸爸怎么了")
    assert "父亲" in dad and "老爸" in dad
    assert "母亲" in pbs.expand_query_tokens("我妈上周去了东京")     # 单字口语也触发
    assert "兄弟" in pbs.expand_query_tokens("你哥哥多大了")
    assert "姊妹" in pbs.expand_query_tokens("你姐姐是做什么工作的")
    assert "丈夫" in pbs.expand_query_tokens("你老公做什么工作")
    # 别名只映射称谓本身：不得引入「离婚」那类状态词（会中名片「婚姻状况：离婚」）
    for toks in (mom, dad):
        assert "离婚" not in toks and "婚否" not in toks


def test_expand_query_tokens_occupation_and_school_aliases():
    """职业/学历词汇桥（A/B 校准 2026-07-28：命中率 86.4%→90.9% 的唯一来源）。

    文档写「职业：设计师」「大阪府立枚方高等学校」，客户问「工作」「高中」——
    这两个词全文零出现 → kw 恒 0，块语义分 0.53~0.58 又够不到 sem_floor_kw0=0.62，
    整块空注入。别名把这一跳补上。
    """
    job = pbs.expand_query_tokens("你爸爸是干什么工作的")
    assert "职业" in job and "岗位" in job
    assert "父亲" in job                              # 亲属别名同时生效，互不干扰
    assert "职业" in pbs.expand_query_tokens("你现在在哪上班")
    assert "职业" in pbs.expand_query_tokens("What is your occupation?")
    school = pbs.expand_query_tokens("你高中在哪上的")
    assert "高等学校" in school
    assert "中学校" in pbs.expand_query_tokens("你初中在哪读的")
    # 只放同义名词：不得引入状态/评价词（会命中人设名片，亲属别名踩过）
    for toks in (job, school):
        assert "离婚" not in toks and "婚否" not in toks


def test_occupation_alias_hits_written_form_unit():
    """端到端：问「工作」命中只写「职业：」的书面单元（原先 kw=0 空注入）。"""
    doc = (
        "父亲名字：\n佐藤健一\n职业：建筑设计师，在大阪一家事务所做了三十年。"
        + "记" * 120
        + "\n\n学历：\n大阪府立枚方高等学校毕业，之后进入京都的专门学校。"
        + "录" * 120
    )
    pbs.replace_bio_doc(_PID, doc)
    # 口径同 A/B 校准：看**注入块集合**里有没有该事实（top_k 会一起带出名片短行，
    # 名次不是这条不变量关心的事——空注入才是原先的病）。
    def _hit(query: str, want: str) -> bool:
        return any(want in h["text"] for h in pbs.search_bio(_PID, query))

    assert _hit("你爸爸是干什么工作的", "设计师")
    assert _hit("你高中在哪上的", "枚方高等学校")


def test_kinship_alias_hits_written_form_unit():
    """口语提问命中书面语单元（同时验证「标题行归属下文」的端到端收益）。"""
    doc = (
        "母亲名字：\nMisaki，日式餐厅店主，现在在大阪经营自己的小店。" + "记" * 100
        + "\n\n父亲名字：\n佐藤健一，2018年9月被诊断出患有肺癌。" + "录" * 100
    )
    pbs.replace_bio_doc(_PID, doc)
    hits = pbs.search_bio(_PID, "你妈妈现在做什么")
    assert hits and "日式餐厅店主" in hits[0]["text"]
    hits2 = pbs.search_bio(_PID, "你爸爸怎么了")
    assert hits2 and "肺癌" in hits2[0]["text"]


def test_kinship_alias_does_not_let_profile_card_win():
    """新别名自查：名片式字段块不得靠亲属别名抢走叙事段（``_PROFILE_CARD_RE`` 降权）。"""
    card = "性别：女\n婚否：离婚\n母亲：在世\n父亲：已故"
    story = "母亲名字：Misaki，在大阪经营一家日式餐厅；父亲2018年确诊肺癌。"
    q = "你妈妈现在做什么"
    raw = {t for t in pbs.query_tokens(q) if t not in pbs._QUESTION_SHELL_TOKS}
    all_t = pbs.expand_query_tokens(q) - pbs._QUESTION_SHELL_TOKS
    assert pbs._keyword_score_weighted(raw, all_t, card) == 0
    assert pbs._keyword_score_weighted(raw, all_t, story) >= 1


def test_profile_card_demoted_for_divorce_alias():
    """名片「婚否/Marital Status: Divorced」不得靠离婚别名胜过叙事块。"""
    card = "日本姓名：美月\nMarital Status: Divorced, no children\n英文名字：Mizuki"
    story = "工作了3个月之后和一个西班牙人José相恋，2018年5月底我们离婚了，协议离婚。"
    raw = {"前夫", "名字"}
    all_t = pbs.expand_query_tokens("你前夫叫什么名字")
    assert pbs._keyword_score_weighted(raw, all_t, card) == 0
    assert pbs._keyword_score_weighted(raw, all_t, story) >= 1


def test_chitchat_query_short_circuits():
    assert pbs._is_chitchat_query(pbs.query_tokens("今天天气真好"))
    assert not pbs._is_chitchat_query(pbs.query_tokens("你前夫叫什么名字"))


def test_excerpt_around_hits_prefers_match_window():
    """长块前半无关、后半含命中词 → 截窗应露出命中词（大学/都灵）。
    即便 budget 很大（≥块长），也不得从块首截（真机冒烟：start 被拉回 0）。"""
    blob = (
        "枚方市立第三中学校 Ages 12-15 大阪府立枚方高等学校 "
        + ("填充。" * 40)
        + "毕业于都灵大学 位于意大利都灵 专业酒店管理"
    )
    out = pbs.excerpt_around_hits(blob, {"大学", "都灵"}, 80)
    assert "都灵大学" in out
    assert not out.startswith("枚方市立")
    out_wide = pbs.excerpt_around_hits(blob, {"大学", "都灵"}, 2000)
    assert "都灵大学" in out_wide
    assert not out_wide.startswith("枚方市立")


def test_question_shell_tokens_stripped_from_scoring():
    """「叫什么名字」不得让所有含「英文名字」的块得分。"""
    q = "你前夫叫什么名字"
    raw = pbs.query_tokens(q)
    assert "名字" in raw
    # search 路径会剔除壳 token；加权打分时叙事离婚块应高于纯名片名字块
    card = "英文名字：Mizuki Isabella Navarro\nMarital Status: Divorced"
    story = "2018年5月底我们协议离婚，西班牙人José Luis Jerónimo是我前夫。"
    score_raw = {t for t in raw if t not in pbs._QUESTION_SHELL_TOKS}
    all_t = pbs.expand_query_tokens(q) - pbs._QUESTION_SHELL_TOKS
    assert pbs._keyword_score_weighted(score_raw, all_t, card) == 0
    assert pbs._keyword_score_weighted(score_raw, all_t, story) >= 1


def test_sem_floor_kw0_rejects_weak_english_fragment(tmp_path):
    """kw=0 时抬高 sem 门槛：擦边余弦（0.56）不得准入；过线（0.70）才进。"""
    # 可控 embed：query 固定 q；弱块≈0.56 余弦；强块≈0.87 余弦（手算点积）
    q = [1.0, 0.0, 0.0, 0.0]
    weak = [0.56, math.sqrt(1 - 0.56 ** 2), 0.0, 0.0]   # cosine≈0.56
    strong = [0.87, math.sqrt(1 - 0.87 ** 2), 0.0, 0.0]  # cosine≈0.87

    def _fn(text: str):
        s = str(text or "")
        if s == "PROBE_Q":
            return list(q)
        if "WEAK_CHUNK" in s:
            return list(weak)
        if "STRONG_CHUNK" in s:
            return list(strong)
        # I1：语义单元分块后每段还会切出「纯填充」尾单元（不含标记词）。
        # 旧 fallback 返回 q（cosine=1.0）会让填充块假装满分 → 改返回正交向量。
        return [0.0, 0.0, 1.0, 0.0]

    store = PersonaBioStore(str(tmp_path / "floor.db"), embed_fn=_fn)
    pbs.set_retrieval_cfg({
        "semantic": True, "keyword_weight": 0.4, "semantic_weight": 0.6,
        "sem_floor": 0.55, "sem_floor_kw0": 0.62,
    })
    store.replace_bio_doc(
        _PID,
        _pad("WEAK_CHUNK english only marriage fragment")
        + "\n\n"
        + _pad("STRONG_CHUNK english only deep match"),
    )
    # 查询用拉丁专名，避免 CJK 亲和降权干扰；且无别名/关键词命中 → 纯语义路径
    hits = store.search_bio(_PID, "PROBE_Q")
    assert hits and len(hits) == 1
    assert "STRONG_CHUNK" in hits[0]["text"]
    assert hits[0]["kw"] == 0
    assert hits[0]["sem"] >= 0.62


def _short_vs_long_store(tmp_path):
    """首单元 3 字（高余弦 0.9）+ 次单元 150 字（余弦 0.7）——短碎片噪声复现台。"""
    def _fn(text: str):
        s = str(text or "")
        if s in ("PROBE_Q", "紫罗兰花"):
            return [1.0, 0.0, 0.0]
        if "兰花" in s:
            return [0.9, math.sqrt(1 - 0.9 ** 2), 0.0]
        if "长单元" in s:
            return [0.7, math.sqrt(1 - 0.7 ** 2), 0.0]
        return [0.0, 0.0, 1.0]

    store = PersonaBioStore(str(tmp_path / "semmin.db"), embed_fn=_fn)
    store.replace_bio_doc(
        _PID,
        "兰花。\n\n" + "这是长单元的叙事内容需要足够长以通过语义准入门槛。" * 6,
    )
    return store


def test_sem_min_chars_rejects_short_unit_on_pure_semantic(tmp_path):
    """kw==0 的纯语义准入加长度门槛：3 字碎片 sem=0.9 也不准入，长单元 0.7 准入。

    A/B 校准（2026-07-28）：sem_floor_kw0 从 0.55 扫到 0.65 读数完全不动——
    误注入不是阈值问题而是短碎片向量方向不稳，掐长度比抬阈值精准。
    """
    store = _short_vs_long_store(tmp_path)
    pbs.set_retrieval_cfg({
        "semantic": True, "keyword_weight": 0.4, "semantic_weight": 0.6,
        "sem_floor": 0.55, "sem_floor_kw0": 0.62,
    })
    hits = store.search_bio(_PID, "PROBE_Q", top_k=5)
    assert hits and all(h["kw"] == 0 for h in hits)
    assert all("兰花" not in h["text"] for h in hits)          # 高余弦短碎片被掐
    assert any("长单元" in h["text"] for h in hits)             # 低余弦长单元照进

    # 门槛可配：调 0 → 恢复「只看 sem_floor_kw0」的旧行为
    pbs.set_retrieval_cfg({
        "semantic": True, "keyword_weight": 0.4, "semantic_weight": 0.6,
        "sem_floor": 0.55, "sem_floor_kw0": 0.62, "sem_min_chars": 0,
    })
    pbs.clear_query_embedding_cache()
    loose = store.search_bio(_PID, "PROBE_Q", top_k=5)
    assert any("兰花" in h["text"] for h in loose)


def test_sem_min_chars_not_applied_when_keyword_hits(tmp_path):
    """kw>0（有关键词证据）的短单元不受长度门槛限制——即便走的是 sem 准入。"""
    store = _short_vs_long_store(tmp_path)
    pbs.set_retrieval_cfg({
        "semantic": True, "keyword_weight": 0.4, "semantic_weight": 0.6,
        "sem_floor": 0.55, "sem_floor_kw0": 0.62,
    })
    # 「紫罗兰花」→ 3 个 bigram，min_hits=3 ⇒ eff_min=3；短单元只中「兰花」
    # （加权 kw=2 < 3）⇒ 关键词轨不准入，靠 sem=0.9 ≥ sem_floor 进——不被长度掐。
    hits = store.search_bio(_PID, "紫罗兰花", top_k=5, min_hits=3)
    short = [h for h in hits if "兰花" in h["text"]]
    assert short and short[0]["kw"] == 2 and short[0]["sem"] >= 0.55


def test_hybrid_keyword_ranks_ahead_when_sem_close(tmp_path):
    """混合权重：sem 接近时，纯关键词命中更多的块仍排前。"""
    store = PersonaBioStore(str(tmp_path / "w.db"), embed_fn=_stub_embed)
    pbs.set_retrieval_cfg({
        "semantic": True, "keyword_weight": 0.4, "semantic_weight": 0.6,
        "sem_floor": 0.55,
    })
    # 两块都含「大学」槽 → stub sem 接近；强块额外叠更多 query 拉丁词
    strong = _pad("大学 alpha beta gamma 项目全记录")
    weak = _pad("大学 其他经历补充说明文字")
    store.replace_bio_doc(_PID, strong + "\n\n" + weak)
    hits = store.search_bio(_PID, "大学 alpha beta gamma")
    assert len(hits) >= 1
    assert "alpha" in hits[0]["text"]
    assert hits[0]["kw"] >= hits[-1].get("kw", 0)


# ── 3. build_bio_block ───────────────────────────────────────────────────────

def test_build_block_budget_clamp():
    pbs.replace_bio_doc(_PID, _BIO)
    block = pbs.build_bio_block(_PID, "你大学在哪个城市读的呀")
    assert block and block.startswith("【人设资料参考】")
    assert "绝不照读" in block and "都灵大学" in block
    assert len(block) <= 600                          # 默认预算硬夹

    small = pbs.build_bio_block(_PID, "你大学在哪个城市读的呀", budget_chars=200)
    assert small and len(small) <= 200 and "都灵大学" in small
    # 预算连头部都塞不下 → None（不发半截头）
    assert pbs.build_bio_block(_PID, "你大学在哪个城市读的呀", budget_chars=10) is None


def test_build_block_no_hit_and_no_stock_none():
    pbs.replace_bio_doc(_PID, _BIO)
    assert pbs.build_bio_block(_PID, "今天天气如何") is None      # 无命中
    assert pbs.build_bio_block("nobody", "你大学在哪个城市") is None  # 无库存


# ── 3b. I1 句级重排 + 注入组装 ───────────────────────────────────────────────

def test_rank_sentences_hit_first_and_scores():
    """命中句排最前；无命中句得 0 分；纯函数不碰网络。"""
    unit = ("枚方市立第三中学校的时间线记录在这里。"
            "她2013年考入都灵大学经济系。"
            "课余在市中心的酒店兼职前台。")
    ranked = pbs.rank_sentences(unit, {"都灵", "大学"})
    assert len(ranked) == 3
    assert "都灵大学" in ranked[0][0] and ranked[0][1] > 0
    assert ranked[-1][1] == 0.0
    assert pbs.rank_sentences("", {"都灵"}) == []


def test_rank_sentences_semantic_opt_in():
    """``sim_fn`` 给出时叠加语义分（默认不传 = 纯关键词，热路零额外嵌入）。"""
    unit = "第一句没有任何命中。第二句也没有命中词。"
    plain = pbs.rank_sentences(unit, {"都灵"})
    assert [s for _, s in plain] == [0.0, 0.0]
    ranked = pbs.rank_sentences(unit, {"都灵"},
                                sim_fn=lambda s: 1.0 if "第二句" in s else 0.0)
    assert ranked[0][0].startswith("第二句") and ranked[0][1] > 0


def test_pick_unit_sentences_keeps_source_order():
    """取最相关的 1~2 句，但**按原文顺序**输出（不打乱叙事）。"""
    unit = ("她2013年考入都灵大学经济系。"
            "中间这句完全无关只是填充说明。"
            "毕业后她留在都灵工作了两年。")
    picked = pbs.pick_unit_sentences(unit, {"都灵", "大学"})
    assert len(picked) == 2
    assert picked[0].startswith("她2013年")          # 原文序，不是分数序
    assert "毕业后她留在都灵" in picked[1]
    assert "填充说明" not in "".join(picked)
    assert pbs.pick_unit_sentences(unit, {"完全对不上的词"}) == []


def test_build_block_injects_sentences_not_whole_unit():
    """注入的是命中句，不再是整单元照搬（填充噪声不该占预算）。"""
    pbs.replace_bio_doc(_PID, _BIO)
    block = pbs.build_bio_block(_PID, "你大学在哪个城市读的呀")
    assert block and "都灵大学经济系" in block
    assert "记记记记记记记记记记" not in block      # 段尾填充不进注入块
    assert len(block) <= 300                        # 句级注入远比整块紧凑


def test_build_block_dedups_repeated_sentence_across_units():
    """跨单元按句去重：同一句出现在两个命中单元里，注入块只留一份。"""
    same = "她2013年考入都灵大学经济系。"
    p1 = same + "父亲是建筑师，母亲经营一家花店。" + "记" * 150
    p2 = same + "课余在市中心的酒店兼职前台。" + "录" * 150
    pbs.replace_bio_doc(_PID, p1 + "\n\n" + p2)
    hits = pbs.search_bio(_PID, "你大学在哪个城市读的呀", top_k=3)
    assert len(hits) >= 2                            # 两个单元都命中
    block = pbs.build_bio_block(_PID, "你大学在哪个城市读的呀")
    assert block and block.count(same) == 1


def test_build_block_top_k_from_config():
    """注入取 top_k（配置口，默认 3）个命中单元。"""
    pbs.replace_bio_doc(_PID, _BIO)
    pbs.set_retrieval_cfg({"top_k": 1})
    try:
        one = pbs.build_bio_block(_PID, "你大学在哪个城市读的呀")
    finally:
        pbs.set_retrieval_cfg(None)
    three = pbs.build_bio_block(_PID, "你大学在哪个城市读的呀")
    assert one and three
    assert one.count("\n- ") == 1
    assert three.count("\n- ") > one.count("\n- ")


# ── 3b2. I5 查询分词补强（真实 33k 文档 A/B 校准 2026-07-28）────────────────

def test_query_tokens_filter_english_function_words():
    """拉丁侧原先没有停用词表：``Do you have any pets at home?`` 里 have/any
    与 pets 同权，泛用词把「work-from-home」段顶上去而 pet 一个都没命中。
    功能词剔除、内容词（含 home/work 这类可作锚点的实词）保留。"""
    toks = pbs.query_tokens("Do you have any pets at home?")
    assert "pets" in toks and "home" in toks
    for bad in ("you", "have", "any", "the", "what", "where"):
        assert bad not in toks
    # work/job/name 是内容锚点，不能当虚词滤掉
    assert "work" in pbs.query_tokens("Where do you work now?")


def test_query_tokens_fold_plural_to_singular():
    """文档写单数、客户问复数 → 关键词轨够不着（``sisters`` vs ``Sister``）。"""
    toks = pbs.query_tokens("Do you have any brothers or sisters?")
    assert {"sisters", "sister", "brothers", "brother"} <= toks
    # -ss 不折（class≠clas）；短词不折（its 已是虚词，另取 3 字样本）
    assert "cla" not in pbs.query_tokens("Which class did you take")


def test_query_tokens_compact_bigram_across_stop_char():
    """虚词夹在两个内容字中间时，逐 bigram 的虚词过滤会把这对内容字一起带走：
    ``你结过婚吗`` 的四个 bigram 全含虚词 → token 空集 → 整条查询检索不到。
    压掉虚词后重新成对可救回（``结过婚`` → ``结婚``）。"""
    toks = pbs.query_tokens("你结过婚吗")
    assert "结婚" in toks
    # 回归：全虚词查询仍是空集（压缩后没有成对的内容字）
    assert pbs.query_tokens("你怎么了呀") == set()
    assert pbs.query_tokens("在吗") == set()


# ── 3b3. I5 句向量入库期预算（句级语义轨热路零网络）─────────────────────────

def _read_sent_vectors(store: PersonaBioStore, persona_id: str):
    with store._lock:
        return store._conn.execute(
            "SELECT sent_key, embedding FROM persona_bio_sents"
            " WHERE persona_id = ?", (persona_id,),
        ).fetchall()


def test_sentence_vectors_precomputed_at_ingest(tmp_path):
    """入库即备好句向量；短句（<12 字）不占额外嵌入。"""
    store = PersonaBioStore(str(tmp_path / "sent.db"), embed_fn=_stub_embed)
    store.replace_bio_doc(_PID, _BIO)
    rows = _read_sent_vectors(store, _PID)
    assert rows and all(r["embedding"].startswith("[") for r in rows)
    # 句数应多于单元数（每单元切出若干句）
    assert len(rows) >= _BIO_UNITS

    store.replace_bio_doc("shorty", "太短。也短。" * 5)
    assert _read_sent_vectors(store, "shorty") == []


def test_sentence_semantic_makes_no_per_sentence_network_call(tmp_path):
    """句级语义轨只读入库期向量：注入一次只嵌 query 一次，绝不按句现算。

    这条开关原先的实现是 ``_embed_text(每一句)``——top_k×每单元句数 ≈ 十余次
    往返压在聊天热路上，是它一直被关着的唯一原因。
    """
    calls: List[str] = []
    store = PersonaBioStore(str(tmp_path / "sentsem.db"),
                            embed_fn=_counting_embed(calls))
    store.replace_bio_doc(_PID, _BIO)
    calls.clear()                                   # 入库期的账不算在热路上
    pbs.clear_query_embedding_cache()

    pbs.set_retrieval_cfg({"sentence_semantic": True})
    try:
        q = "你大学在哪个城市读的呀"
        block = store.build_bio_block(_PID, q)
    finally:
        pbs.set_retrieval_cfg(None)
    assert block
    assert calls == [q]                             # 有且只有 query 那一次


def test_sentence_semantic_degrades_without_stock(tmp_path):
    """老库句向量表为空 → 退化成纯关键词排序，**不**在热路补打嵌入。"""
    calls: List[str] = []
    store = PersonaBioStore(str(tmp_path / "legacy.db"),
                            embed_fn=_counting_embed(calls))
    store.replace_bio_doc(_PID, _BIO)
    with store._lock:
        store._conn.execute("DELETE FROM persona_bio_sents")
        store._conn.commit()
    calls.clear()
    pbs.clear_query_embedding_cache()

    pbs.set_retrieval_cfg({"sentence_semantic": True})
    try:
        q = "你大学在哪个城市读的呀"
        block = store.build_bio_block(_PID, q)
    finally:
        pbs.set_retrieval_cfg(None)
    assert block and "都灵大学经济系" in block      # 关键词轨照常出活
    assert calls == [q]


def test_reembed_backfills_sentence_vectors(tmp_path):
    """句向量表是后加的：老库跑一次 reembed 就能回填，不必重导文档。"""
    store = PersonaBioStore(str(tmp_path / "backfill.db"), embed_fn=_stub_embed)
    store.replace_bio_doc(_PID, _BIO)
    with store._lock:
        store._conn.execute("DELETE FROM persona_bio_sents")
        store._conn.commit()
    assert _read_sent_vectors(store, _PID) == []

    out = store.reembed_bio_doc(_PID)
    assert out and out["sents_added"] > 0
    assert _read_sent_vectors(store, _PID)
    assert store.reembed_bio_doc(_PID)["sents_added"] == 0   # 幂等


def test_delete_bio_doc_clears_sentence_vectors(tmp_path):
    store = PersonaBioStore(str(tmp_path / "del.db"), embed_fn=_stub_embed)
    store.replace_bio_doc(_PID, _BIO)
    assert _read_sent_vectors(store, _PID)
    store.delete_bio_doc(_PID)
    assert _read_sent_vectors(store, _PID) == []


# ── 3b4. I5 单元视图缓存（热路每条消息都跑，写入必须失效）───────────────────

def test_chunk_view_cached_across_queries(tmp_path):
    """解析后的单元视图按人设常驻：第二次检索不再回表/重解 JSON。"""
    store = PersonaBioStore(str(tmp_path / "view.db"), embed_fn=None)
    store.replace_bio_doc(_PID, _BIO)
    store.search_bio(_PID, "你大学在哪个城市读的呀")

    fetches: List[str] = []
    orig = store._fetch_chunks
    store._fetch_chunks = lambda pid: (fetches.append(pid), orig(pid))[1]
    store.search_bio(_PID, "你前夫Marco现在在哪")
    assert fetches == []                       # 命中视图缓存，零回表
    view = store._chunk_view(_PID)
    assert view and view[0]["low"] == view[0]["text"].lower()


def test_chunk_view_invalidated_on_replace_and_delete(tmp_path):
    """陈旧视图 = 把删掉/改掉的传记继续喂给 AI，属正确性问题而非性能问题。"""
    store = PersonaBioStore(str(tmp_path / "inval.db"), embed_fn=None)
    store.replace_bio_doc(_PID, _BIO)
    assert store.search_bio(_PID, "你前夫Marco现在在哪")

    store.replace_bio_doc(_PID, _pad("完全换掉的传记：她是京都的陶艺师傅横山"))
    assert store.search_bio(_PID, "你前夫Marco现在在哪") == []
    assert store.search_bio(_PID, "陶艺师傅")

    store.delete_bio_doc(_PID)
    assert store.search_bio(_PID, "陶艺师傅") == []


def test_chunk_view_sees_writes_from_another_process(tmp_path):
    """跨进程写入必须可见——本机是双实例部署（智聊/通译共用 config/），回填还走
    独立 CLI 进程。纯进程内失效会造成两类静默失效：① A 实例导入传记、B 实例永远
    查不到；② 导入**之前**查过一次的人设把空视图缓存到重启。用两个 store 实例
    指向同一个库文件来复刻另一个进程。"""
    db = str(tmp_path / "xproc.db")
    reader = PersonaBioStore(db, embed_fn=None)
    writer = PersonaBioStore(db, embed_fn=None)

    assert reader.search_bio(_PID, "陶艺") == []            # ① 先把空视图缓存起来
    writer.replace_bio_doc(_PID, _pad("她是京都的陶艺师傅横山，做了二十年"))
    assert reader.search_bio(_PID, "陶艺")                  # 空缓存不得粘住

    writer.replace_bio_doc(_PID, _pad("换成大阪的甜点师傅小林，开了家蛋糕店"))
    assert reader.search_bio(_PID, "陶艺") == []            # ② 旧内容不得粘住
    assert reader.search_bio(_PID, "甜点")

    writer.delete_bio_doc(_PID)
    assert reader.search_bio(_PID, "甜点") == []


def test_chunk_view_lru_bounded(tmp_path):
    """视图缓存有人设数上限，不随人设数无限涨。"""
    store = PersonaBioStore(str(tmp_path / "lru.db"), embed_fn=None)
    for i in range(pbs._CHUNK_CACHE_MAX_PERSONAS + 3):
        pid = f"p{i}"
        store.replace_bio_doc(pid, _BIO)
        store.search_bio(pid, "你大学在哪个城市读的呀")
    assert len(store._chunk_cache) == pbs._CHUNK_CACHE_MAX_PERSONAS


def test_retrieval_cfg_files_parsed_once_but_hot_reloadable(tmp_path, monkeypatch):
    """`_retrieval_settings` 每次检索都调 → 原实现等于每条入站消息把两个千行
    yaml 重新 safe_load 一遍（实测 ~200ms×2）。按 (mtime,size) 签名缓存，
    但**不能**因此丢掉配置热更新语义。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    cfg = tmp_path / "config" / "config.local.yaml"

    loads: List[int] = []
    real = yaml.safe_load
    monkeypatch.setattr(yaml, "safe_load",
                        lambda *a, **k: (loads.append(1), real(*a, **k))[1])

    cfg.write_text(yaml.safe_dump({"personas": {"bio_retrieval": {"top_k": 5}}}),
                   encoding="utf-8")
    pbs.clear_retrieval_cfg_file_cache()
    assert pbs._retrieval_settings()["top_k"] == 5
    n = len(loads)
    for _ in range(5):
        assert pbs._retrieval_settings()["top_k"] == 5
    assert len(loads) == n                      # 稳态零解析

    cfg.write_text(yaml.safe_dump({"personas": {"bio_retrieval": {"top_k": 7}}}),
                   encoding="utf-8")
    assert pbs._retrieval_settings()["top_k"] == 7   # 改盘即时生效
    assert len(loads) > n


# ── 3c. I1 查询嵌入缓存 / 失败冷却 ───────────────────────────────────────────

def _counting_embed(calls):
    def _fn(text: str):
        calls.append(str(text or ""))
        return [1.0, 0.0, 0.0, 0.0]
    return _fn


def test_query_embedding_cached_per_query(tmp_path):
    """同 query 二次检索不再触发 embed_fn；不同 query 会触发；reset 清缓存。"""
    calls: List[str] = []
    store = PersonaBioStore(str(tmp_path / "cache.db"),
                            embed_fn=_counting_embed(calls))
    store.replace_bio_doc(_PID, _BIO)
    calls.clear()                                   # 入库嵌入不计（走另一条链）

    q = "你大学在哪个城市读的呀"
    store.search_bio(_PID, q)
    assert calls.count(q) == 1
    store.search_bio(_PID, q)
    assert calls.count(q) == 1                      # 命中 LRU，零网络
    store.search_bio(_PID, "你前夫Marco现在在哪")
    assert calls.count("你前夫Marco现在在哪") == 1   # 新 query 照常嵌

    pbs.clear_query_embedding_cache()
    store.search_bio(_PID, q)
    assert calls.count(q) == 2


def test_query_embedding_failure_cooldown(tmp_path):
    """嵌入异常 → 记一次 embed_fail，冷却窗内不再调 embed_fn（走纯关键词）。"""
    calls: List[str] = []

    def _boom(text: str):
        calls.append(str(text or ""))
        raise RuntimeError("embed endpoint down")

    store = PersonaBioStore(str(tmp_path / "cool.db"), embed_fn=_boom)
    store.replace_bio_doc(_PID, _BIO)               # 入库嵌入全失败 → 列为空
    calls.clear()

    hits = store.search_bio(_PID, "你大学在哪个城市读的呀")
    assert calls == ["你大学在哪个城市读的呀"]
    assert hits and "rank" not in hits[0]           # 无向量 → 纯关键词旧路径
    assert pbs.retrieval_stats_snapshot()["embed_fail"] == 1

    store.search_bio(_PID, "你前夫Marco现在在哪")     # 冷却窗内：不同 query 也不打
    assert len(calls) == 1
    assert pbs.retrieval_stats_snapshot()["embed_fail"] == 1

    pbs.clear_query_embedding_cache()               # 解除冷却
    store.search_bio(_PID, "你前夫Marco现在在哪")
    assert len(calls) == 2


# ── 3d. I1 注入观测 ──────────────────────────────────────────────────────────

def test_retrieval_stats_counts():
    pbs.reset_retrieval_stats()
    assert pbs.retrieval_stats_snapshot() == {
        "queries": 0, "hits": 0, "empty": 0, "embed_fail": 0,
        "hit_rate": 0.0, "avg_hits": 0.0,
    }
    pbs.replace_bio_doc(_PID, _BIO)
    pbs.search_bio(_PID, "你大学在哪个城市读的呀")     # 命中
    pbs.search_bio(_PID, "今天天气如何")               # 空
    snap = pbs.retrieval_stats_snapshot()
    assert snap["queries"] == 2 and snap["hits"] == 1 and snap["empty"] == 1
    assert snap["hit_rate"] == 0.5 and snap["avg_hits"] >= 0.5
    pbs.search_bio("", "任何问题")                     # 无 persona → 不计
    assert pbs.retrieval_stats_snapshot()["queries"] == 2
    pbs.reset_retrieval_stats()
    assert pbs.retrieval_stats_snapshot()["queries"] == 0


# ── 3.9 人设实体别名（M9：全局静态表 ⊕ 档案派生）─────────────────────────

# 父亲只以拉丁全名出现的语料：叙事段一次「爸爸/父亲」都不写（真实档案的常见
# 形态）——不派生别名时「你爸爸做什么工作」关键词分恒 0。
_ALIAS_DAD = _pad(
    "José Leandro Navarro在潘普洛纳开事务所，主持过市政厅广场的翻修，"
    "是当地小有名气的建筑师，作品拿过区里的设计奖")
_ALIAS_BIO = "\n\n".join([_ALIAS_DAD, _P_UNI, _P_WORK])
_ALIAS_PROFILE = {
    "id": "alias_p",
    "gender": "female",
    "background": "父亲José Leandro Navarro（西班牙建筑师），母亲Misaki Fujimura。",
}


def test_persona_aliases_merge_global_table_with_derived_names():
    pbs.set_alias_provider(lambda _pid: _ALIAS_PROFILE)
    out = pbs.persona_query_aliases("alias_p")
    dad = {str(v).lower() for v in out.get("爸爸", ())}
    assert "josé leandro navarro" in dad          # 档案派生
    assert "父亲" in out.get("爸爸", ())           # 全局表未被覆盖掉
    assert "misaki fujimura" in {str(v).lower() for v in out.get("妈妈", ())}


def test_derived_alias_surfaces_unit_that_only_uses_full_name():
    """端到端：叙事段只写全名时，派生别名让关键词轨够得着。"""
    st = PersonaBioStore(":memory:")
    st.replace_bio_doc("alias_p", _ALIAS_BIO)
    q = "你爸爸是干什么工作的"

    # 无别名：只有「工作」这一词命中现职段，父亲段一次都够不着
    pbs.set_alias_provider(None)
    assert not any("建筑师" in h["text"] for h in st.search_bio("alias_p", q))

    pbs.set_alias_provider(lambda _pid: _ALIAS_PROFILE)
    assert any("建筑师" in h["text"] for h in st.search_bio("alias_p", q))


def test_alias_derivation_cached_until_profile_content_changes(monkeypatch):
    """派生只在档案内容变了才重算——热路每条消息都会问一次。"""
    import src.companion.persona_entity_alias as pea

    calls = {"n": 0}
    real = pea.build_entity_aliases

    def counting(profile, **kw):
        calls["n"] += 1
        return real(profile, **kw)

    monkeypatch.setattr(pea, "build_entity_aliases", counting)

    profile = dict(_ALIAS_PROFILE)
    pbs.set_alias_provider(lambda _pid: profile)
    for _ in range(4):
        pbs.persona_query_aliases("alias_p")
    assert calls["n"] == 1

    # 同长度改写：长度/条数签名会漏，内容 sha1 不会
    profile["background"] = "父亲Jose Leandro Navarro（西班牙建筑设计），母亲Misaki Fujimura。"
    out = pbs.persona_query_aliases("alias_p")
    assert calls["n"] == 2
    assert "jose leandro navarro" in {str(v).lower() for v in out.get("爸爸", ())}


def test_alias_provider_failure_falls_back_to_global_table():
    def boom(_pid):
        raise RuntimeError("profile store down")

    pbs.set_alias_provider(boom)
    assert pbs.persona_query_aliases("alias_p") is pbs._QUERY_ALIASES


@pytest.mark.parametrize("profile", [None, {}, "not a dict", 42])
def test_alias_provider_junk_profile_falls_back(profile):
    pbs.set_alias_provider(lambda _pid: profile)
    assert pbs.persona_query_aliases("alias_p") is pbs._QUERY_ALIASES


def test_alias_disabled_and_blank_pid_use_global_table():
    pbs.set_alias_provider(None)
    assert pbs.persona_query_aliases("alias_p") is pbs._QUERY_ALIASES
    pbs.set_alias_provider(lambda _pid: _ALIAS_PROFILE)
    assert pbs.persona_query_aliases("") is pbs._QUERY_ALIASES


def test_expand_query_tokens_defaults_to_global_table():
    """显式传表只影响调用方；缺省仍是全局表（既有调用点行为不变）。"""
    base = pbs.expand_query_tokens("你爸爸是干什么工作的")
    with_extra = pbs.expand_query_tokens(
        "你爸爸是干什么工作的", {"爸爸": ("josé leandro",)})
    assert "josé leandro" not in base
    assert "josé leandro" in with_extra


# ── 4. 路由（TestClient，仿 test_persona_doc_import 建 app 模式）─────────────

_HDRS = {"Authorization": "Bearer test-token"}
_JSON_HDRS = {**_HDRS, "Content-Type": "application/json"}


def _build_app(tmp_path, doc_import_enabled):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": [],
                     "doc_import": {"enabled": bool(doc_import_enabled)}},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    from src.utils.persona_manager import PersonaManager

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    from src.web.admin import create_app
    return create_app(cm)


@pytest.fixture
def app_off(tmp_path):
    yield _build_app(tmp_path, False)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


@pytest.fixture
def app_on(tmp_path):
    yield _build_app(tmp_path, True)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_bio_routes_flag_off_403(app_off):
    async with _client(app_off) as c:
        r = await c.post(f"/api/personas/{_PID}/bio-doc",
                         headers=_JSON_HDRS, json={"text": _BIO})
        assert r.status_code == 403
        r = await c.get(f"/api/personas/{_PID}/bio-doc", headers=_HDRS)
        assert r.status_code == 403
        r = await c.delete(f"/api/personas/{_PID}/bio-doc", headers=_HDRS)
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_bio_routes_post_get_delete_roundtrip(app_on):
    async with _client(app_on) as c:
        # POST（profile 不存在也可入库：导入向导先入库后保存人设）
        r = await c.post(f"/api/personas/{_PID}/bio-doc",
                         headers=_JSON_HDRS, json={"text": _BIO})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["chunks"] == _BIO_UNITS
        assert body["chars"] == len(_BIO)

        # GET meta
        r = await c.get(f"/api/personas/{_PID}/bio-doc", headers=_HDRS)
        assert r.status_code == 200
        meta = r.json()["meta"]
        assert meta["chunks"] == _BIO_UNITS and meta["chars"] == len(_BIO)
        assert meta["preview"].startswith("美月1994年")

        # DELETE → 再 GET meta=null → 二次 DELETE deleted=false
        r = await c.delete(f"/api/personas/{_PID}/bio-doc", headers=_HDRS)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "deleted": True}
        r = await c.get(f"/api/personas/{_PID}/bio-doc", headers=_HDRS)
        assert r.json() == {"ok": True, "meta": None}
        r = await c.delete(f"/api/personas/{_PID}/bio-doc", headers=_HDRS)
        assert r.json() == {"ok": True, "deleted": False}


@pytest.mark.asyncio
async def test_bio_search_route_flag_off_403(app_off):
    async with _client(app_off) as c:
        r = await c.post(f"/api/personas/{_PID}/bio-doc/search",
                         headers=_JSON_HDRS, json={"query": "你大学在哪读的"})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_bio_search_route_empty_query_400(app_on):
    async with _client(app_on) as c:
        r = await c.post(f"/api/personas/{_PID}/bio-doc/search",
                         headers=_JSON_HDRS, json={"query": "   "})
        assert r.status_code == 400
        r = await c.post(f"/api/personas/{_PID}/bio-doc/search",
                         headers=_JSON_HDRS, json={})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_bio_search_route_no_stock_is_empty_not_error(app_on):
    async with _client(app_on) as c:
        r = await c.post("/api/personas/nobody/bio-doc/search",
                         headers=_JSON_HDRS, json={"query": "你大学在哪读的"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["hits"] == []
        assert body["block"] is None and body["chunks"] == 0


@pytest.mark.asyncio
async def test_bio_search_route_returns_hits_block_stats(app_on):
    pbs.reset_retrieval_stats()
    async with _client(app_on) as c:
        await c.post(f"/api/personas/{_PID}/bio-doc",
                     headers=_JSON_HDRS, json={"text": _BIO})
        r = await c.post(f"/api/personas/{_PID}/bio-doc/search",
                         headers=_JSON_HDRS,
                         json={"query": "你大学在哪个城市读的呀", "top_k": 9})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["chunks"] == _BIO_UNITS
        assert 1 <= len(body["hits"]) <= 5          # top_k 夹 1..5
        top = body["hits"][0]
        assert set(top) == {"idx", "score", "kw", "sem", "text"}
        assert "都灵大学" in top["text"] and len(top["text"]) <= 300
        assert "都灵大学" in (body["block"] or "")
        # 自测一次只计一次检索（hits 复用给 build_bio_block，不重复计数）
        assert body["stats"]["queries"] == 1 and body["stats"]["hits"] == 1


@pytest.mark.asyncio
async def test_bio_routes_validation(app_on):
    async with _client(app_on) as c:
        r = await c.post(f"/api/personas/{_PID}/bio-doc",
                         headers=_JSON_HDRS, json={"text": "   "})
        assert r.status_code == 400
        r = await c.post(f"/api/personas/{_PID}/bio-doc",
                         headers=_JSON_HDRS,
                         json={"text": "x" * (MAX_BIO_CHARS + 1)})
        assert r.status_code == 413


# ── 5. 注入接线（仿 test_bazi_wiring 轻量绑定）───────────────────────────────

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


class _SM:
    _persona_bio_cfg = _SMcls._persona_bio_cfg
    _inject_persona_bio_context = _SMcls._inject_persona_bio_context

    def __init__(self, enabled=True):
        self.config = SimpleNamespace(
            config={"personas": {"bio_retrieval": {"enabled": enabled}}})
        self.logger = logging.getLogger("test_persona_bio")


def test_inject_flag_on_hit_sets_block():
    pbs.replace_bio_doc(_PID, _BIO)
    sm = _SM(enabled=True)
    ctx = {"account_persona_id": _PID}
    sm._inject_persona_bio_context(ctx, "你大学在哪个城市读的呀")
    blk = ctx.get("_persona_bio_block") or ""
    assert "人设资料参考" in blk and "都灵大学" in blk


def test_inject_flag_off_noop_and_clears_stale():
    pbs.replace_bio_doc(_PID, _BIO)
    sm = _SM(enabled=False)
    ctx = {"account_persona_id": _PID, "_persona_bio_block": "残留"}
    sm._inject_persona_bio_context(ctx, "你大学在哪个城市读的呀")
    assert "_persona_bio_block" not in ctx


def test_inject_no_hit_or_no_persona_noop():
    pbs.replace_bio_doc(_PID, _BIO)
    sm = _SM(enabled=True)
    ctx = {"account_persona_id": _PID}
    sm._inject_persona_bio_context(ctx, "今天天气如何")     # 无命中
    assert "_persona_bio_block" not in ctx
    ctx2 = {}
    sm._inject_persona_bio_context(ctx2, "你大学在哪个城市")  # 无 persona 绑定
    assert "_persona_bio_block" not in ctx2


def test_inject_store_exception_soft(monkeypatch):
    """检索层异常 → 静默跳过，绝不让聊天链崩。"""
    pbs.replace_bio_doc(_PID, _BIO)

    def _boom(*a, **k):
        raise RuntimeError("retrieval exploded")

    monkeypatch.setattr(pbs, "build_bio_block", _boom)
    sm = _SM(enabled=True)
    ctx = {"account_persona_id": _PID}
    sm._inject_persona_bio_context(ctx, "你大学在哪个城市读的呀")
    assert "_persona_bio_block" not in ctx


def test_ai_client_consumes_persona_bio_block():
    from src.ai.ai_client import AIClient

    class _Cfg:
        config_path = None
        config = {"web_admin": {"site_name": "T"}, "ai": {}}

        def get_ai_config(self):
            return {}

    client = AIClient(_Cfg())
    out = client._build_context_prompt({
        "channel": "telegram",
        "_persona_bio_block": "【人设资料参考】以下是资料片段：\n- 2013年考入都灵大学",
    })
    assert "人设资料参考" in out and "都灵大学" in out
    out2 = client._build_context_prompt({"channel": "telegram"})
    assert "人设资料参考" not in out2
