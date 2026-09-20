# -*- coding: utf-8 -*-
"""小智问答检索评测门禁（实施74 P2，2026-08-27）。

检索是 BM25、纯确定性（无网络无模型），所以这是**常驻门禁**而不是 opt-in
评测——语料一改就能立刻知道有没有把原本能答的问题搞丢。

## 为什么这道门禁值钱

2026-08-27 语料从 32 条 how-to 扩到 57 条时，「补进去的条目能不能被真实
问法检索到」完全没有数据。首跑基线 top1 52% / top3 80%，8 条未命中直接
变成补关键词的照单；补完 top1 78% / top3 100%。

**过程中抓到一个纯靠直觉抓不到的问题**：给 `escalation-config` 补了
「人工客服」之后，它把「怎么新增一个客服人员」从 `add-agent-user` 手里抢走了
——补关键词有**交叉影响**，改任何一条都可能误伤别的条目。这正是本门禁存在
的理由：它把这种静默的相互挤占变成一条红。

## 金标写法纪律（改 GOLD 前先读）

问句必须用**用户会怎么打字**来写，**不许照抄条目标题**——照抄必然命中，
那是自证的同义反复、零鉴别力。低分是产出，不是失败。
"""
from __future__ import annotations

import os

import pytest

from src.eval.assistant_qa_eval import (
    GOLD,
    build_eval_kb,
    evaluate_assistant_qa,
    format_report,
    targets,
)


@pytest.fixture(scope="module")
def result():
    """整模块共用一次评测（建临时库 + 建 BM25 索引，重复跑纯属浪费）。"""
    return evaluate_assistant_qa(build_eval_kb())


def test_gold_ids_all_exist():
    """金标期望的条目必须真实存在——打错 id 会让门禁永远红且指向虚空。"""
    from src.assistant.seed_corpus import build_all_entries

    ids = {e["id"] for e in build_all_entries()}
    missing = sorted({g[1] for g in GOLD} - ids)
    assert not missing, f"金标引用了不存在的条目 id：{missing}"


def test_gold_questions_do_not_copy_titles(result):
    """金标问句不得与条目标题高度重合（防同义反复式「假绿」）。

    判据**只看中文连续片段**（≥6 字原样出现在标题里）。为什么不看拉丁字母：
    首版按「任意 ≥8 连续字符」判，把 `messenger 掉线了怎么重新登录` 误判成
    抄题——它只是和标题共享了产品名 *Messenger* 里的 `essenger`。产品名/平台名
    本来就该在问句里出现（用户就是这么打字的），真正要拦的是**抄中文措辞**。
    """
    import re

    from src.assistant.seed_corpus import build_all_entries

    def cjk_runs(text: str) -> str:
        return "".join(re.findall(r"[\u4e00-\u9fff]+", text))

    title_by_id = {e["id"]: str(e.get("title") or "") for e in build_all_entries()}
    bad = []
    for q, expect, _note in GOLD:
        t_cjk = cjk_runs(title_by_id.get(expect, ""))
        q_cjk = cjk_runs(q)
        for i in range(0, max(0, len(q_cjk) - 5)):
            frag = q_cjk[i:i + 6]
            if len(frag) == 6 and frag in t_cjk:
                bad.append((q, expect, frag))
                break
    assert not bad, (
        f"金标问句与标题中文措辞重合过高（同义反复，测不出检索能力）：{bad}"
    )


def test_retrieval_topk_floor(result):
    """期望条目进 top_k 的比例不得低于基线。

    这是最重要的一条：进不了 top_k = LLM 手里根本没有正确素材，
    后面答得再漂亮也是编的。
    """
    t = targets()
    assert result["topk_rate"] >= t["topk"], (
        f"top{result['top_k']} 命中率 {result['topk_rate']:.0%} < "
        f"下限 {t['topk']:.0%}\n" + format_report(result)
    )


def test_retrieval_top1_floor(result):
    """排第一的比例不得低于基线（用户一眼看到的就是对的那条）。"""
    t = targets()
    assert result["top1_rate"] >= t["top1"], (
        f"top1 命中率 {result['top1_rate']:.0%} < 下限 {t['top1']:.0%}\n"
        + format_report(result)
    )


def test_no_false_rejection_on_gold(result):
    """**没有一条该答的问题被拒答**——这是抬阈值时最不能踩的红线。

    误拒的代价：用户问了个我们明明知道的问题，却被回一句「不知道」。
    所以阈值只能设在正样本最低分之下（当前正样本最低 41.2，阈值 35）。
    """
    assert result["answerable_rate"] == 1.0, (
        f"有 {result['n'] - result['answerable']} 条金标被误拒"
        f"（min_score={result['min_score']}，正样本最低分 "
        f"{result['pos_min_score']}）——阈值抬过头了\n" + format_report(result)
    )


def test_calibration_margin_not_too_thin(result):
    """阈值与正样本最低分之间必须留够余量，否则语料一漂就开始误拒。

    2026-08-27 校准时对比过：阈值 40 能多拦 1 条负样本，但离最低正样本
    只剩 1.2 分——为多拦一条把整条问答链置于脆弱状态不划算，故取 35
    （余量 6.2）。这条守的就是那个决定不被悄悄改回去。
    """
    assert result["margin"] >= 3.0, (
        f"校准余量只剩 {result['margin']} 分（正样本最低 "
        f"{result['pos_min_score']} vs 阈值 {result['min_score']}）——"
        "语料稍有变动就会开始误拒真问题；要么降阈值，要么补关键词把低分条目抬上来"
    )


def test_refusal_rate_floor(result):
    """无依据的问题被拦下的比例不得低于基线。

    基线由 2026-08-27 校准得到：阈值 1.6 时只拦得住 1/15（纯乱码），
    抬到 35 后拦住 7/15。这条防止有人把阈值悄悄调回没有鉴别力的区间。
    """
    floor = float(os.environ.get("AITR_ASB_REFUSAL_TARGET", "0.45"))
    assert result["refusal_rate"] >= floor, (
        f"诚实拒答率 {result['refusal_rate']:.0%} < 下限 {floor:.0%}"
        f"（{result['refused']}/{result['neg_n']}）——阈值被调低了？\n"
        + format_report(result)
    )


def test_product_shaped_negatives_are_the_known_remaining_gap(result):
    """把「分数拦不住产品形状的无依据提问」这个已知缺口钉成可见事实。

    校准数据说得很清楚：negatives 里 `product` 一类（「怎么导出所有客户的
    手机号」「怎么把数据迁移到别的服务器」…）用的全是**产品词汇**，BM25 分数
    天然高（72~110），落在正样本分布中间——**单一分数阈值不可能拦住它们**，
    抬阈值只会先误拒真问题。

    这类才是最危险的：用户问了个产品里没有的能力，小智会拿一条沾边的条目
    自信地答。真正的解法是出站前再加一道语义核对（「检索到的条目真的能回答
    这个问题吗」），属下一阶段。

    这条断言**不是要求缺口保持存在**：等语义核对上线、product 拦截率上去，
    这条会红——红了就该把 baseline 抬上去并更新本说明，那正是应该发生的。
    """
    by_kind = result.get("refusal_by_kind") or {}
    prod = by_kind.get("product")
    assert prod and prod["n"] >= 6, "product 类负样本太少，代表性不足"
    rate = prod["refused"] / prod["n"]
    assert rate <= 0.5, (
        f"product 类拒答率已达 {rate:.0%}（{prod['refused']}/{prod['n']}）——"
        "光靠分数拦不到这个水平，多半是加了新机制；请把本用例改成正向断言"
        "并抬高 AITR_ASB_REFUSAL_TARGET"
    )


def test_report_is_actionable_on_failure(result):
    """失败信息必须逐条列出未命中项（否则红了也不知道补哪个词）。"""
    text = format_report(result)
    assert "top1" in text and "可作答" in text
    fake = dict(result)
    fake["misses"] = [{"q": "x", "expect": "howto:y", "got": ["howto:z"],
                       "score": 1.0, "note": "n"}]
    assert "补 keywords 照单" in format_report(fake)
