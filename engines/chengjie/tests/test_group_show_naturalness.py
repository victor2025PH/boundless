# -*- coding: utf-8 -*-
"""群戏自然度评测门禁 —— 这把尺子本身不能坏。

自然度报告是排练与影子跑批唯一的「这场戏像不像人」的量化结论，下游门禁经常**只读一个
数**（score）或**只读一个词**（verdict）。因此这个文件重点守两类东西：

1. **跨字段一致性**：score / verdict / issues / ok 四者不得自相矛盾。历史上真出过
   「issues 里写着『这不是群聊』而 verdict=natural」的报告（见
   ``test_group_show_phase2_regressions``），只看分数的门禁会直接放行硬伤。
2. **脏数据与退化输入下的保守结论**：评测器活在报表路径上，被畸形入参掀翻会让整份
   报表消失；而「样本不足却假装打分」比没有分数更危险——它会被当成真结论采信。
"""
from __future__ import annotations

import math

import pytest

from src.companion.group_show.naturalness import (
    CHAOTIC_SCORE_CAP,
    CV_ROBOTIC,
    ENTROPY_ROBOTIC,
    MIN_SAMPLES,
    ROBOTIC_SCORE_CAP,
    naturalness_score,
    shannon_entropy,
    speaker_balance,
)

#: 报告的键集合是对下游的契约——少一个键，看板/门禁就 KeyError
_REPORT_KEYS = {
    "ok", "insufficient", "samples", "score", "entropy", "entropy_measurable",
    "interval_cv", "cv_measurable", "balance", "cohesion", "top_share",
    "top_speaker", "speakers", "verdict", "issues",
}

#: 判定为「假」的三条致命判据在 issues 里的特征词
_FATAL_ISSUE_MARKS = ("一眼假", "掐着秒表", "不是群聊", "各说各话")

#: 高抖动间隔（CV≈0.57）：让节奏轴明确健康，避免 robotic 由 CV 轴误伤别的断言
_JITTERY = [30, 95, 45, 150, 60, 25, 110, 40, 70]

_NATURAL_LINES = [
    "手上号越来越多切来切去容易发错人",
    "上次把给A客户的话发给了B尴尬死了",
    "大家平时都怎么管这么多号的呀",
    "我现在把消息都收在一个后台里看",
    "这种会不会被判定成机器人挂机呢",
    "它不是群发消息还是一条条正常发",
    "那能同时带多少个号会不会卡",
    "一个人看十几个号完全不乱",
]


class _Ev:
    """最小事件对象（评测器只读这四个字段）。"""

    def __init__(self, account="a", text="", kind="line", ts=0.0):
        self.speaker_account = account
        self.text = text
        self.kind = kind
        self.ts = ts


def _natural_events(n=8):
    return [_Ev(f"acc{i % 4}", _NATURAL_LINES[i % len(_NATURAL_LINES)])
            for i in range(n)]


def _report_is_self_consistent(r):
    """报告内部一致性（下游只读其中一项也不会被误导）。"""
    assert set(r) == _REPORT_KEYS
    assert 0.0 <= r["score"] <= 1.0
    assert 0.0 <= r["entropy"] <= 1.0
    assert 0.0 <= r["balance"] <= 1.0
    assert r["interval_cv"] >= 0.0
    assert r["ok"] is not r["insufficient"]
    if r["verdict"] == "insufficient":
        assert r["ok"] is False
    else:
        assert r["ok"] is True
    if r["verdict"] == "robotic":
        assert r["score"] <= ROBOTIC_SCORE_CAP
    if r["verdict"] == "chaotic":
        assert r["score"] <= CHAOTIC_SCORE_CAP
    if r["verdict"] == "natural":
        assert not [i for i in r["issues"]
                    if any(m in i for m in _FATAL_ISSUE_MARKS)], r["issues"]
    return r


# ── 契约：ok / verdict / score / issues 不得互相打架 ────────────────────────


def test_insufficient_report_carries_the_same_keys_as_a_full_one():
    """样本不足的报告必须键集合完全一致——下游按键取值，缺键就是看板整块崩。"""
    assert set(naturalness_score([])) == _REPORT_KEYS
    assert set(naturalness_score(_natural_events(8))) == _REPORT_KEYS


def test_too_few_samples_refuse_to_score_instead_of_guessing():
    """少于阈值的样本必须明说「不予打分」，不能给一个像模像样的分数。

    把 2 条台词的熵当质量分是自欺欺人，而下游看到一个 0.7 会当成「这场戏挺好」。
    """
    for n in range(0, MIN_SAMPLES):
        r = naturalness_score(_natural_events(n))
        assert r["ok"] is False
        assert r["insufficient"] is True
        assert r["verdict"] == "insufficient"
        assert r["score"] == 0.0
        assert r["samples"] == n
        assert any("样本不足" in i for i in r["issues"])


@pytest.mark.parametrize("case", ["natural", "repetitive", "metronome",
                                  "solo", "chaotic", "garbage", "empty"])
def test_verdict_score_and_issues_never_contradict(case):
    """任意一场戏，判词、分数、问题清单三者必须同向。

    门禁通常只读其中一个，三者打架就意味着「按分数放行、按判词拦截」的结论不一致，
    硬伤会从最宽松的那个口子溜过去。
    """
    if case == "natural":
        r = naturalness_score(_natural_events(8), intervals=_JITTERY[:7])
    elif case == "repetitive":
        r = naturalness_score([_Ev(f"acc{i % 4}", "这个真的好用推荐大家试试")
                               for i in range(8)], intervals=_JITTERY[:7])
    elif case == "metronome":
        r = naturalness_score(_natural_events(8), intervals=[60.0] * 7)
    elif case == "solo":
        r = naturalness_score([_Ev("solo", t) for t in _NATURAL_LINES],
                              intervals=_JITTERY[:7])
    elif case == "chaotic":
        r = naturalness_score(
            [_Ev(f"acc{i}", w) for i, w in enumerate(
                ["abcdef", "ghijkl", "mnopqr", "stuvwx", "yzabcz", "qwerty"])],
            intervals=_JITTERY[:5])
    elif case == "garbage":
        r = naturalness_score([None, 1, "x", {"kind": "line", "text": "喵"}])
    else:
        r = naturalness_score([])
    _report_is_self_consistent(r)


def test_scoring_is_deterministic():
    """同输入必须同报告——排练读数会被反复采集比对，抖一下就没法归因。"""
    evs = _natural_events(9)
    assert naturalness_score(evs, intervals=_JITTERY) == \
           naturalness_score(evs, intervals=_JITTERY)


# ── 熵轴：模板复读 ──────────────────────────────────────────────────────────


def test_template_repetition_is_caught_as_robotic():
    """一批号翻来覆去说同一套话必须判 robotic——这是风控最容易抓的形状。"""
    r = naturalness_score([_Ev(f"acc{i % 4}", "这个真的好用推荐大家都去试试")
                           for i in range(10)], intervals=_JITTERY)
    assert r["entropy"] < ENTROPY_ROBOTIC
    assert r["verdict"] == "robotic"
    assert any("一眼假" in i for i in r["issues"])


def test_varied_talk_scores_higher_entropy_than_repetition():
    """守的是序关系而非某个具体数值：多样必须比复读得分高，阈值可调，方向不可反。"""
    varied = naturalness_score(_natural_events(8), intervals=_JITTERY[:7])
    repeat = naturalness_score([_Ev(f"acc{i % 4}", _NATURAL_LINES[0])
                                for i in range(8)], intervals=_JITTERY[:7])
    assert varied["entropy"] > repeat["entropy"]
    assert varied["score"] > repeat["score"]


def test_entropy_does_not_drift_with_show_length():
    """长戏不得因为「变长」本身被误判成模板化——滑窗口径存在的全部理由。

    对照组是同一份文本的整体熵：它会随长度显著下滑，那正是不能直接用它当分数的原因。
    """
    short = naturalness_score([_Ev(f"acc{i % 4}", _NATURAL_LINES[i % 8])
                               for i in range(8)], intervals=_JITTERY[:7])
    long = naturalness_score([_Ev(f"acc{i % 4}", _NATURAL_LINES[i % 8])
                              for i in range(40)], intervals=_JITTERY)
    assert abs(short["entropy"] - long["entropy"]) < 0.02
    naive_short = shannon_entropy("".join(_NATURAL_LINES))
    naive_long = shannon_entropy("".join(_NATURAL_LINES * 5))
    assert naive_long < naive_short - 0.05, "对照组失效，这条测试就证明不了什么"


def test_entropy_of_degenerate_text_is_zero_not_a_high_score():
    """空串/单字/算不出的输入一律给 0，绝不因为「没有重复」就给满分。

    反过来给高分会让「一场只有几个字的戏」在看板上显示为自然度最高的一场。
    """
    for bad in ("", " ", "a", None, "\n\n"):
        assert shannon_entropy(bad) == 0.0


def test_entropy_is_bounded_and_ranks_repetition_lower():
    """熵恒在 [0,1] 内，且完全复读必须低于完全不重复。"""
    assert shannon_entropy("啊" * 200) < shannon_entropy(
        "今天群里聊多号管理挺热闹的大家都在问会不会封号")
    for text in ("啊" * 200, "abcdefghij", "中英mix混排123"):
        assert 0.0 <= shannon_entropy(text) <= 1.0


def test_weird_ngram_size_does_not_crash():
    """n 传成 0/负数/非数字时安静回落，报表路径不该被一个参数掀翻。"""
    for n in (0, -3, "x", None, 5):
        assert 0.0 <= shannon_entropy("这是一段用来测试的群聊文本", n=n) <= 1.0


# ── 节奏轴：间隔 ────────────────────────────────────────────────────────────


def test_metronome_intervals_are_caught_even_when_text_is_good():
    """台词再自然，掐着秒表发也必须判 robotic——时间戳比文本更难伪装。"""
    r = naturalness_score(_natural_events(8), intervals=[60.0] * 7)
    assert r["interval_cv"] < CV_ROBOTIC
    assert r["verdict"] == "robotic"
    assert any("秒表" in i for i in r["issues"])


def test_single_interval_is_declared_unmeasurable_rather_than_perfect():
    """只有一个间隔时必须如实说「测不了」，不能算出 CV=0 反手判成机器人。"""
    r = naturalness_score(_natural_events(4), intervals=[42.0])
    assert r["cv_measurable"] is False
    assert any("间隔样本不足" in i for i in r["issues"])
    assert not any("秒表" in i for i in r["issues"])


def test_dirty_intervals_are_dropped_not_fatal():
    """间隔序列里的 None/字符串/负数/inf/nan 一律丢弃，剩下的照常算。"""
    clean = naturalness_score(_natural_events(6), intervals=[30, 95, 45, 150])
    dirty = naturalness_score(_natural_events(6), intervals=[
        30, "x", 95, None, -7, float("inf"), 45, float("nan"), 150])
    assert dirty["interval_cv"] == clean["interval_cv"]


def test_explicit_intervals_win_over_event_timestamps():
    """显式传入的间隔优先于落库 ts——runtime 手里的计划等待更贴近真实节奏。

    两者混用会让「同一场戏」在排练与回放里给出两个不同的节奏结论。
    """
    evs = [_Ev(f"acc{i % 4}", _NATURAL_LINES[i % 8], ts=1000.0 + i * 60)
           for i in range(8)]
    by_ts = naturalness_score(evs)
    by_arg = naturalness_score(evs, intervals=_JITTERY[:7])
    assert by_ts["interval_cv"] < CV_ROBOTIC          # ts 是等距的
    assert by_arg["interval_cv"] > CV_ROBOTIC         # 显式间隔是抖的
    assert by_ts["verdict"] == "robotic" and by_arg["verdict"] == "natural"


def test_missing_timestamps_do_not_fake_a_rhythm_verdict():
    """全部 ts 缺失（=0）时节奏轴退出判据，不能凭空判「节奏机械」。"""
    r = naturalness_score(_natural_events(8))
    assert r["cv_measurable"] is False
    assert not any("秒表" in i for i in r["issues"])


# ── 均衡轴 ──────────────────────────────────────────────────────────────────


def test_speaker_balance_calls_a_monologue_completely_unbalanced():
    """一个号从头说到尾不是群聊，均衡度必须给 0 而不是「只有一个号所以很均衡」。"""
    assert speaker_balance([_Ev("solo", t) for t in _NATURAL_LINES]) == 0.0
    assert speaker_balance([]) == 0.0
    assert speaker_balance(None) == 0.0


def test_speaker_balance_is_maximal_when_everyone_talks_equally():
    """完全均等轮换给满分，倾斜必须严格低于它——这是「戏好不好看」的刻度。"""
    even = speaker_balance([_Ev(f"acc{i % 4}", "x") for i in range(8)])
    skewed = speaker_balance([_Ev("acc0" if i else "acc1", "x")
                              for i in range(8)])
    assert math.isclose(even, 1.0, abs_tol=1e-9)
    assert 0.0 < skewed < even


def test_only_ai_lines_are_scored_not_human_or_control_events():
    """真人原话与导演控制事件不进 AI 的演技分——否则真人越活跃分越好看。

    那会让「群里真人一多，自然度自动变高」，把最需要收敛的场景判成最健康的场景。
    """
    evs = (_natural_events(6)
           + [_Ev("human1", "你们是不是一伙的", kind="human"),
              _Ev("acc0", "", kind="yield"),
              _Ev("acc0", "", kind="terminate")])
    r = naturalness_score(evs, intervals=_JITTERY[:5])
    assert r["samples"] == 6
    assert "human1" != r["top_speaker"]


def test_media_events_count_as_performance():
    """发物料也是一次「露面」，必须计入发言数与均衡度（否则甩图刷屏测不出来）。"""
    r = naturalness_score(
        [_Ev("acc0", "看这个截图", kind="media")] + _natural_events(5),
        intervals=_JITTERY[:5])
    assert r["samples"] == 6


# ── 脏输入与退化 ────────────────────────────────────────────────────────────


def test_non_iterable_input_is_treated_as_no_samples():
    """None / 字符串 / 数字这类根本不是事件流的入参，按「没样本」处理而不是抛。"""
    for bad in (None, "一段文本", 42, object()):
        r = naturalness_score(bad)
        assert r["ok"] is False and r["verdict"] == "insufficient"


def test_dirty_rows_do_not_flip_a_healthy_show_into_robotic():
    """混进 None / 畸形行不得把一场健康的戏判成假戏。

    评测跑在报表路径上，上游任何一条脏事件都可能出现；误判会让运营去「修」一场
    本来没问题的戏。
    """
    clean = naturalness_score(_natural_events(8), intervals=_JITTERY[:7])
    polluted = naturalness_score(
        [None] + _natural_events(8) + [None], intervals=_JITTERY[:7])
    assert clean["verdict"] == polluted["verdict"] == "natural"
    assert polluted["samples"] == clean["samples"]


def test_dict_shaped_events_are_accepted():
    """同形状 dict 也要能评——回放/看板拿到的常常是 JSON 反序列化后的字典。"""
    rows = [{"speaker_account": f"acc{i % 4}", "kind": "line",
             "text": _NATURAL_LINES[i % 8], "ts": 0.0} for i in range(8)]
    r = naturalness_score(rows, intervals=_JITTERY[:7])
    assert r["ok"] is True and r["samples"] == 8


def test_events_with_missing_fields_degrade_to_conservative_values():
    """缺字段的事件不该掀翻评测，且缺发言人时不能凭空并成一个「主力号」被判独角戏。"""
    class _Bare:
        pass

    r = naturalness_score([_Bare(), _Bare(), _Bare(), _Bare()])
    _report_is_self_consistent(r)
    assert r["verdict"] != "natural", "全是空白事件不该被判成自然"


def test_textless_show_declares_entropy_unmeasurable_out_loud():
    """一场没有任何文本的戏，熵轴必须**明说自己测不了**并退出判据。

    沉默地按「没检测到复读」处理，等于把「无从判断」伪装成「判断通过」——那是本仓
    反复吃过亏的失败形状（见 runtime 的空场伪装成 completed）。
    """
    r = naturalness_score([_Ev(f"acc{i % 3}", "") for i in range(8)],
                          intervals=_JITTERY[:7])
    assert r["entropy_measurable"] is False
    assert any("熵不可判" in i for i in r["issues"])
    _report_is_self_consistent(r)


def test_extremely_long_text_does_not_blow_up_the_scorer():
    """超长台词只是慢一点，不能算爆——一次报表失败会连带整份影子跑批读数丢失。"""
    r = naturalness_score([_Ev(f"acc{i % 4}", "这是一段很长的话" * 500)
                           for i in range(6)], intervals=_JITTERY[:5])
    _report_is_self_consistent(r)


# ── line_similarity（复读闸的尺子） ──────────────────────────────────────────


def test_line_similarity_separates_real_repeats_from_normal_flow():
    """标定钉：2026-07-27 首场灰度真实台词。

    b3/b4 是真实复读对（都在说「消息收在后台看、不用切」，overlap = 0.50 恰在
    阈值上）；其余任意两拍都是正常推进（≤ 0.25）。这五条是复读闸阈值的**标定
    语料**——尺子（_content_tokens 口径 / overlap 公式）或阈值任何一头漂移，
    这里先红，别让它在真群里用「又安利了一遍」的方式被发现。
    """
    from src.companion.group_show.live import MAX_LINE_SIMILARITY
    from src.companion.group_show.naturalness import line_similarity

    b1 = "哈哈我也觉得号多了好烦\n\n每次切来切去都忘了登哪个"
    b2 = "别提了\n\n我刚才把给客户A的吐槽发到客户B对话框了\n\n真的是社死现场"
    b3 = "哈哈我也是\n\n后来索性把消息都收在一个后台里看了\n\n至少不用来回切了"
    b4 = "哈哈是的  \n我现在基本都塞一个后台看  \n至少人清爽不少"
    b5 = "哈哈对，能少记一件事都算赚到\n\n你们一般还用别的什么小工具管账号吗？"

    assert line_similarity(b3, b4) >= MAX_LINE_SIMILARITY, "真实复读对必须被抓住"
    normal_pairs = [(b1, b2), (b1, b3), (b1, b4), (b1, b5),
                    (b2, b3), (b2, b4), (b2, b5), (b3, b5), (b4, b5)]
    for a, b in normal_pairs:
        assert line_similarity(a, b) < MAX_LINE_SIMILARITY, (a, b)


def test_line_similarity_edge_shapes_never_raise():
    """空串/None/纯表情这些退化形态一律 0.0——闸门对无从判断的输入必须放行。"""
    from src.companion.group_show.naturalness import line_similarity

    assert line_similarity("", "后台收消息") == 0.0
    assert line_similarity(None, None) == 0.0
    assert line_similarity("😂😂", "😂") == 0.0
    assert 0.0 <= line_similarity("同一句话", "同一句话") <= 1.0
