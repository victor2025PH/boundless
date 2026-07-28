# -*- coding: utf-8 -*-
"""口语化「内容保持」门禁（2026-07-28 实录事故）。

事故：本地模型把要念的话
  「嗨，我在宿务这边刚开完店，你吃饭了吗」
改写成了对这句话的**回答**
  「刚忙完啊我吃过了你那边店开起来肯定一堆事吧顾不上吃饭也正常可别饿着自己啊」
——38 字对 18 字刚好卡在 ``max_expand=1.8`` 上限（18×1.8+8=40.4）内，长度/语种两道
旧闸全过，人设语音说出的话与要说的话完全无关（真机 STT 回转实测抓到）。

双层守卫各管一半，本门禁两边都钉：
- ``lost_anchors``（纯函数）：数字/金额/型号丢了或被改即拒。确定性、零误伤；
  余弦抓不到这类（「198→168」语义几乎不变，实测 cos=0.974）。
- ``_semantically_same``（余弦，fail-open）：答非所问/换主题即拒。anchor 抓不到
  这类（没有数字可丢）。阈值 0.78 由实测校准：真改写 0.862~0.986 / 漂移 0.526~0.696。

语义层用**注入的确定性假嵌入**，CI 不依赖局域网 GPU。
"""

from __future__ import annotations

import asyncio

import pytest

from src.ai.voice_colloquial_llm import (
    DEFAULT_MIN_SIMILARITY,
    _semantically_same,
    lost_anchors,
    sanitize_llm_output,
)

CORE_A = "嗨，我在宿务这边刚开完店，你吃饭了吗"
CORE_B = "团队版一个月198美金，你先看看合适不"
INCIDENT = "刚忙完啊我吃过了你那边店开起来肯定一堆事吧顾不上吃饭也正常可别饿着自己啊"


# ── 第一层：事实锚点（纯函数，零网络）────────────────────────────────────
def test_anchor_rejects_tampered_number():
    """实录同族：报价被悄悄改小（198→168）必须拒——这是最贵的一类漂移。"""
    bad = "团队版嘛一个月168美金，你先瞅瞅合不合适"
    assert lost_anchors(CORE_B, bad) == ["198"]
    assert sanitize_llm_output(bad, CORE_B) is None


def test_anchor_rejects_dropped_number():
    """数字整个消失（改写把报价说没了）同样拒。"""
    bad = "团队版你先瞅瞅合不合适嘛，价格官网都写着"
    assert lost_anchors(CORE_B, bad) == ["198"]
    assert sanitize_llm_output(bad, CORE_B) is None


def test_anchor_keeps_legit_rewrite_with_number():
    """正常口语化（数字原样、只加语气词）必须放行——误伤即毁掉口语化能力。"""
    good = "团队版嘛，一个月198美金，你先瞅瞅合不合适"
    assert lost_anchors(CORE_B, good) == []
    assert sanitize_llm_output(good, CORE_B) == good


def test_anchor_ignores_texts_without_anchors():
    """原文没有数字/型号 → 锚点层不介入（该由语义层判）。"""
    assert lost_anchors(CORE_A, INCIDENT) == []
    assert lost_anchors("因此我们无需担心", "所以咱不用担心") == []


def test_anchor_normalizes_thousands_separator():
    """「2,250」与「2250」是同一个事实，不得因千分位判成丢锚点。"""
    assert lost_anchors("一共2,250比索", "一共2250比索嘛") == []


def test_anchor_ignores_single_digit_noise():
    """单位数刻意不入锚点集（「3点」被合法改成「三点」不该判违规）。"""
    assert lost_anchors("我3点到", "我三点到哈") == []


def test_anchor_covers_latin_product_token():
    """套餐/型号这类拉丁 token 也是事实锚点。"""
    assert lost_anchors("走 autochat-team 这档", "走那个便宜档就行") != []


# ── 第二层：语义地板（fail-open）──────────────────────────────────────────
class _FakeEmbedder:
    """确定性假嵌入：按预置相似度表返回可控向量（CI 不碰局域网 GPU）。"""

    def __init__(self, sim: float):
        self._sim = float(sim)

    async def embed(self, texts):
        import math
        # 构造两个夹角 arccos(sim) 的单位向量 → 余弦恰为 self._sim
        theta = math.acos(max(-1.0, min(1.0, self._sim)))
        return [[1.0, 0.0], [math.cos(theta), math.sin(theta)]]


def _same(sim: float, min_similarity: float = DEFAULT_MIN_SIMILARITY) -> bool:
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _semantically_same(_FakeEmbedder(sim), CORE_A, "任意改写", min_similarity))


@pytest.mark.parametrize("sim", [0.526, 0.684, 0.696, 0.77])
def test_semantic_rejects_drift_band(sim):
    """实测漂移带（换主题 0.526 / 答非所问 0.684 / 回答式 0.696）必须拒。"""
    assert _same(sim) is False


@pytest.mark.parametrize("sim", [0.862, 0.886, 0.897, 0.975, 0.986])
def test_semantic_keeps_legit_band(sim):
    """实测真改写带（0.862~0.986）必须放行。"""
    assert _same(sim) is True


def test_semantic_threshold_sits_in_measured_gap():
    """阈值必须落在实测间隔内——防有人随手把它调进任一带里。"""
    assert 0.70 < DEFAULT_MIN_SIMILARITY < 0.86


def test_semantic_fail_open_without_embed():
    """client 没有 embed → 放行（守卫故障不该拖死口语化）。"""
    class NoEmbed:
        pass

    got = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _semantically_same(NoEmbed(), CORE_A, "完全无关的一句话", 0.78))
    assert got is True


def test_semantic_fail_open_on_endpoint_error():
    """嵌入端点抛异常 → 放行。"""
    class Boom:
        async def embed(self, texts):
            raise RuntimeError("endpoint down")

    got = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _semantically_same(Boom(), CORE_A, "完全无关的一句话", 0.78))
    assert got is True


def test_semantic_fail_open_on_empty_vectors():
    """端点返回空向量 → 放行。"""
    class Empty:
        async def embed(self, texts):
            return []

    got = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _semantically_same(Empty(), CORE_A, "完全无关的一句话", 0.78))
    assert got is True


def test_semantic_disabled_by_zero_threshold():
    """阈值 ≤0 = 关闭本层（运营可一键回旧行为）。"""
    got = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _semantically_same(_FakeEmbedder(0.1), CORE_A, "无关", 0.0))
    assert got is True


# ── 旧闸不得回退 ─────────────────────────────────────────────────────────
def test_existing_length_and_language_guards_intact():
    """长度/语种旧闸行为不变（本次只加内容保持，不放松任何既有红线）。"""
    # 腰斩
    assert sanitize_llm_output("刚开完店", CORE_A) is None
    # 串语言
    assert sanitize_llm_output("Just closed the shop, have you eaten yet my friend",
                              CORE_A) is None
    # 空
    assert sanitize_llm_output("", CORE_A) is None
    assert sanitize_llm_output("有效改写", "") is None


def test_incident_case_needs_semantic_layer_not_anchor():
    """把事故本体钉死：它绕过长度/语种/锚点三闸，只有语义层能拦。

    这条断言的价值＝有人日后删掉语义层时，这里立刻红并指出原因。
    """
    assert lost_anchors(CORE_A, INCIDENT) == []          # 锚点抓不到
    assert sanitize_llm_output(INCIDENT, CORE_A) == INCIDENT  # 纯函数层放行
    assert _same(0.684) is False                          # 语义层拦住（实测 cos）
