# -*- coding: utf-8 -*-
"""群戏节奏引擎门禁 —— 时间维度上的穿帮不需要读内容就能被一眼看出。

这个文件守三类东西：

1. **确定性**：同 seed 必须同时刻表。排练器要靠它做「改了剧本节奏有没有变好」的对比，
   不确定性会让整套排练读数失去意义。
2. **档位语义不被抖动偷换**：加了长尾抖动之后，chatty/normal/slow 三档的**期望**
   必须仍然是各自的基础间隔——否则「调档」这个旋钮就是假的。
3. **脏输入不掀翻热路径**：pace/字数都来自人手写 YAML 与运行期估算，任何一处抛异常
   都意味着这一拍哑掉。
"""
from __future__ import annotations

import random

from src.companion.group_show.pacing import (
    DEFAULT_CHARS_PER_SEC,
    JITTER_SHARE,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    NOMINAL_TEXT_LEN,
    READING_CHARS_PER_SEC,
    base_seconds_for_pace,
    beat_interval_seconds,
    plan_schedule,
    reading_seconds,
    typing_seconds,
)
from src.companion.group_show.playbook import PACE_BASE_SECONDS, Beat


def _beat(pace: str = "normal", i: int = 1) -> Beat:
    return Beat(id=f"b{i}", role="advocate", intent="意图", pace=pace)


def _beats(n: int, pace: str = "normal"):
    return [_beat(pace, i) for i in range(1, n + 1)]


# ── 档位语义 ────────────────────────────────────────────────────────────────


def test_unknown_pace_degrades_to_normal_instead_of_raising():
    """人手写的 `pace: 快` 必须安静回落 normal——排期是热路径，抛了这一拍就哑了。

    真正的拼写检查在离线的 validate_playbook 里做，运行期只负责别停演。
    """
    for bad in ("快", "", None, 123, "  ", object()):
        assert base_seconds_for_pace(bad) == PACE_BASE_SECONDS["normal"]


def test_pace_is_case_and_whitespace_insensitive():
    """YAML 里写成 ` Chatty ` 也该认——大小写差异不该悄悄把节奏改成 normal。"""
    assert base_seconds_for_pace(" Chatty ") == PACE_BASE_SECONDS["chatty"]


def test_pace_tiers_stay_strictly_ordered_under_jitter():
    """同一串随机数下，chatty < normal < slow 必须逐样本成立。

    抖动是为了「形状像人」，不是为了把档位搅浑。三档失序意味着运营调档拿到的是噪声。
    """
    for i in range(200):
        vals = [
            beat_interval_seconds(_beat(p), rng=random.Random(i))
            for p in ("chatty", "normal", "slow")
        ]
        assert vals[0] < vals[1] < vals[2], (i, vals)


def test_jitter_does_not_shift_the_tier_mean():
    """抖动只改变形状（长尾），不得偷偷改变档位期望。

    如果均值漂了，「normal = 60 秒一条」这个运营心智模型就是错的，排出来的一场戏
    时长会和预期系统性偏差，而每一条单看都很正常——最难发现的那种偏差。
    """
    rng = random.Random(20260725)
    n = 4000
    total = sum(beat_interval_seconds(_beat("normal"), rng=rng)
                for _ in range(n))
    assert abs(total / n - PACE_BASE_SECONDS["normal"]) < 2.5


def test_jitter_produces_a_long_tail_not_a_narrow_band():
    """间隔分布必须有长尾：真人偶尔隔很久，均匀分布那种「40~80 秒徘徊」依旧整齐得反常。"""
    rng = random.Random(7)
    vals = [beat_interval_seconds(_beat("normal"), rng=rng) for _ in range(500)]
    base = PACE_BASE_SECONDS["normal"]
    assert max(vals) > base * 1.8, "没有长尾＝节奏依然像定时器"
    assert min(vals) < base * 0.75, "没有快回＝真人不会每次都等满一个档位"


# ── 打字 / 阅读时长 ─────────────────────────────────────────────────────────


def test_typing_and_reading_are_monotonic_in_length():
    """字越多耗时不得变少——这条一旦坏掉，长文会比短句更快冒出来，一眼假。"""
    assert typing_seconds(10) < typing_seconds(50) < typing_seconds(200)
    assert reading_seconds(10) < reading_seconds(50) < reading_seconds(200)
    assert typing_seconds(50) > reading_seconds(50), "打字必须比阅读慢"


def test_zero_and_garbage_lengths_cost_no_time():
    """没有上一条 / 字数算不出来时按 0 计，不抛也不瞎猜一个耗时。"""
    for bad in (0, -5, None, "abc", float("nan"), float("inf"), object()):
        assert typing_seconds(bad) == 0.0
        assert reading_seconds(bad) == 0.0


def test_non_positive_speed_falls_back_to_the_default_rate():
    """速度参数给 0/负数时回落默认值——除零会把整条发送链路炸掉。"""
    assert typing_seconds(70, chars_per_sec=0) == typing_seconds(70)
    assert typing_seconds(70, chars_per_sec=-3) == 70 / DEFAULT_CHARS_PER_SEC
    assert reading_seconds(60, chars_per_sec=0) == 60 / READING_CHARS_PER_SEC


# ── 单拍间隔 ────────────────────────────────────────────────────────────────


def test_interval_never_escapes_the_bounds():
    """任何输入下间隔都必须夹在 [MIN, MAX] 内。

    下界防「秒回长文」，上界防长尾抽出半小时把整场戏拖散——两头都是肉眼可见的穿帮。
    """
    rng = random.Random(1)
    for pace in ("chatty", "normal", "slow", "乱写"):
        for lens in ((0, 0), (1, 1), (5000, 5000), (-9, -9), (None, "x")):
            v = beat_interval_seconds(_beat(pace), prev_text_len=lens[0],
                                      next_text_len=lens[1], rng=rng)
            assert MIN_INTERVAL_SECONDS <= v <= MAX_INTERVAL_SECONDS


def test_absurdly_long_text_saturates_at_the_ceiling():
    """一条几万字的稿子不能排出几小时的等待——上界必须真的封顶。"""
    v = beat_interval_seconds(_beat("slow"), prev_text_len=10**6,
                              next_text_len=10**6, rng=random.Random(0))
    assert v == MAX_INTERVAL_SECONDS


def test_interval_is_reproducible_with_the_same_seeded_rng():
    """同 seed 的 rng 必须给出逐位相同的间隔——排练可复现的根。"""
    a = [beat_interval_seconds(_beat(), rng=random.Random(42)) for _ in range(5)]
    assert len(set(a)) == 1
    assert beat_interval_seconds(_beat(), rng=random.Random(42)) == a[0]


def test_beat_without_pace_does_not_crash():
    """给 None / 光板对象也要能排出一个合法间隔——上游任何一处传错都不该停演。"""
    for bad in (None, object(), "beat", 0):
        v = beat_interval_seconds(bad, rng=random.Random(3))
        assert MIN_INTERVAL_SECONDS <= v <= MAX_INTERVAL_SECONDS


def test_interval_without_rng_still_returns_a_sane_number():
    """不传 rng（真发场景要的就是不可预测）时仍须落在合法区间内。"""
    for _ in range(50):
        v = beat_interval_seconds(_beat("chatty"))
        assert MIN_INTERVAL_SECONDS <= v <= MAX_INTERVAL_SECONDS


def test_reading_and_typing_time_actually_enter_the_interval():
    """阅读/打字时长必须真的叠进间隔里，否则「读完再打」只是文档里的说法。"""
    short = beat_interval_seconds(_beat(), prev_text_len=0, next_text_len=0,
                                  rng=random.Random(11))
    long = beat_interval_seconds(_beat(), prev_text_len=200, next_text_len=200,
                                 rng=random.Random(11))
    assert long > short + 50


# ── 整场时刻表 ──────────────────────────────────────────────────────────────


def test_empty_and_single_beat_schedules_are_degenerate_but_valid():
    """空剧本给空表、单拍给 [0.0]——开场那条即刻发，不该凭空等一轮。"""
    assert plan_schedule([]) == []
    assert plan_schedule(None) == []
    assert plan_schedule(_beats(1)) == [0.0]


def test_schedule_is_strictly_increasing_and_starts_at_zero():
    """时刻表必须严格递增：两拍撞在同一秒＝两个号同时冒泡，最刺眼的机器特征。"""
    sched = plan_schedule(_beats(20), seed=5)
    assert len(sched) == 20
    assert sched[0] == 0.0
    assert all(b - a >= MIN_INTERVAL_SECONDS for a, b in zip(sched, sched[1:]))


def test_same_seed_reproduces_the_whole_schedule():
    """同 seed 同表——这是排练回归的前提，不确定就没法比较两版剧本。"""
    assert plan_schedule(_beats(12), seed=99) == plan_schedule(_beats(12), seed=99)


def test_different_seed_changes_the_rhythm():
    """不同 seed 必须真的换一套节奏，否则「随机化」是摆设，多场戏节奏会一模一样。"""
    assert plan_schedule(_beats(12), seed=1) != plan_schedule(_beats(12), seed=2)


def test_schedule_honours_text_lengths_when_they_are_known():
    """已知台词更长时，整场时长必须更长——排期与真发的观感要对得上。"""
    short = plan_schedule(_beats(8), text_lens=[5] * 8, seed=3)
    long = plan_schedule(_beats(8), text_lens=[300] * 8, seed=3)
    assert long[-1] > short[-1]


def test_missing_text_lens_fall_back_to_a_nominal_sentence():
    """缺省字数按一句群聊短句估——按 0 估会排出明显偏紧、跟真发对不上的表。"""
    default = plan_schedule(_beats(6), seed=4)
    nominal = plan_schedule(_beats(6), text_lens=[NOMINAL_TEXT_LEN] * 6, seed=4)
    assert default == nominal


def test_short_or_dirty_text_lens_do_not_break_the_schedule():
    """字数表短一截、混进 None/字符串/负数，都不能让整场排期失败。

    台词是逐拍生成的，排期时手里的长度表天然是残缺的——这是常态而不是异常。
    """
    dirty = [10, None, "abc", -5, float("nan")]
    sched = plan_schedule(_beats(8), text_lens=dirty, seed=6)
    assert len(sched) == 8
    assert all(b > a for a, b in zip(sched, sched[1:]))


def test_extra_text_lens_are_ignored():
    """字数表比 beats 长时多余项被忽略，不能影响已排好的那几拍。"""
    assert plan_schedule(_beats(3), text_lens=[20] * 3, seed=8) == \
           plan_schedule(_beats(3), text_lens=[20] * 30, seed=8)


def test_schedule_survives_broken_beat_objects():
    """beats 里混进 None 也要排完整场——一颗坏 beat 不该让整场戏排不出来。"""
    sched = plan_schedule([_beat(), None, _beat(), None], seed=2)
    assert len(sched) == 4
    assert all(b > a for a, b in zip(sched, sched[1:]))


def test_chatty_show_finishes_sooner_than_a_slow_one():
    """整场口径上 chatty 必须明显快于 slow，档位在「场」这一层也要成立。"""
    fast = plan_schedule(_beats(15, "chatty"), seed=13)[-1]
    slow = plan_schedule(_beats(15, "slow"), seed=13)[-1]
    assert fast < slow


def test_jitter_share_stays_within_a_sane_split():
    """抖动占比是刻度常量，改动它会同时移动全部三档的形状，必须显式可见。"""
    assert 0.0 < JITTER_SHARE < 1.0
