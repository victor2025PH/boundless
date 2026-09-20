"""容量层门禁 —— 「能铺多少群」这个数必须取最紧的一条轴，不能取最响的那条。

这一层存在的全部理由是一句话：三条轴各自的容量差好几倍，而运营只会记住一个数。
所以本文件的重点不是「函数返回值对不对」，而是**它会不会在某个组合下给出乐观的谎**。
"""
from __future__ import annotations

import pytest

from src.companion.group_show.attendance import (
    NORMAL_PAIR_CO,
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
    max_safe_groups,
)
from src.companion.group_show.capacity import (
    AXIS_MEMBER,
    AXIS_PAIR,
    AXIS_ROLE,
    MIN_ADVOCATE_RATIO,
    pair_capacity,
    plan_capacity,
    pool_needed,
    recommend_mix,
    role_capacity,
)
from src.companion.group_show.performance import speaker_budget
from src.companion.group_show.roles import NORMAL_ROLE_CO


# ── 核心谎言：solo 在发言轴上「无上限」，但真实上限只有 30 ──────────────────


def test_solo_looks_unlimited_on_the_speech_axis_alone():
    """先固定住那个**会骗人的**读数——它本身没错，错的是拿它当最终答案。"""
    budget = speaker_budget(pool=10, groups=100)
    solo = [o for o in budget["options"] if o["speakers"] == 1][0]
    assert solo["unlimited"] is True and solo["max_groups"] is None


def test_capacity_refuses_to_call_solo_unlimited():
    """同一套输入，容量层必须给出**有限**的答案，并点名瓶颈是主推轴。"""
    plan = plan_capacity(pool=10, groups=100, speakers=1)
    assert plan["max_groups"] == 10 * NORMAL_ROLE_CO, "每场都带货时就是 号数×3"
    assert plan["binding"] == AXIS_ROLE
    assert plan["fits"] is False


def test_squeezing_speakers_stops_helping_once_the_role_axis_binds():
    """把开口人数从 3 压到 1，真实容量**不再增长**——这正是发言轴单独看会误导的地方。"""
    caps = {k: plan_capacity(pool=10, groups=100, speakers=k)["max_groups"]
            for k in (4, 3, 2, 1)}
    assert caps[4] < caps[3], "4 人时发言轴还是瓶颈，压下来确实有用"
    assert caps[3] == caps[2] == caps[1] == 10 * NORMAL_ROLE_CO, (
        "3 人往下主推轴已接管，再压嘴巴一个群都多不出来")


def test_the_binding_axis_names_the_lever_that_actually_moves_it():
    """瓶颈轴必须自带处方。只说「卡在主推轴」而不说怎么办，运营的动作还是去补号。"""
    plan = plan_capacity(pool=10, groups=100, speakers=1)
    axis = [a for a in plan["axes"] if a["axis"] == plan["binding"]][0]
    assert "带货" in axis["lever"]


# ── 第二个杠杆：带货密度 ────────────────────────────────────────────────────


def test_thinning_the_selling_density_is_what_unlocks_a_hundred_groups():
    assert role_capacity(pool=10, advocate_ratio=1.0) == 30
    assert role_capacity(pool=10, advocate_ratio=0.5) == 60
    # 30/0.3 在浮点里是 99.999...，截断会得到 99 —— 那会让建议与自检互相打架
    assert role_capacity(pool=10, advocate_ratio=0.3) == 100


def test_not_selling_at_all_is_genuinely_unconstrained_on_this_axis():
    """纯陪聊铺群不受主推集中度约束。报 0 会把一种完全安全的玩法误伤成不可行。"""
    assert role_capacity(pool=10, advocate_ratio=0) is None
    plan = plan_capacity(pool=10, groups=500, speakers=1, advocate_ratio=0)
    assert plan["max_groups"] is None and plan["fits"] is True


def test_recommendation_answers_the_actual_question_ten_accounts_hundred_groups():
    """运营真正问的那句话，要能一次给全：几张嘴 + 多大密度。"""
    mix = recommend_mix(pool=10, groups=100)
    assert mix["fits"] is True
    assert mix["speakers"] == 1, "90 < 100，两人同台撑不住"
    assert mix["advocate_ratio"] == pytest.approx(0.3, abs=0.01)


def test_recommendation_sacrifices_liveliness_before_revenue():
    """次序不能反：先压开口人数（戏冷清一点），后压带货密度（少一次转化）。

    反过来的话工具会一路把带货压到 5% 去换热闹，运营看一眼就知道不能用，
    然后连同整条护栏一起绕开。
    """
    # 90 个群：两人同台正好撑得住 → 不该为了保带货密度而把嘴巴压到 1
    mix = recommend_mix(pool=10, groups=90)
    assert mix["speakers"] == 2 and mix["advocate_ratio"] == pytest.approx(1 / 3,
                                                                          abs=0.01)


def test_a_roomy_pool_keeps_both_knobs_wide_open():
    mix = recommend_mix(pool=30, groups=20)
    assert mix["fits"] and mix["advocate_ratio"] == 1.0
    assert mix["speakers"] >= 3, "余量充足时不该无谓地把戏演冷清"


def test_an_impossible_target_says_how_many_accounts_it_would_take():
    """铺不动的时候不能只说「不行」，得说差多少。"""
    mix = recommend_mix(pool=2, groups=5000)
    assert mix["fits"] is False
    assert mix["pool_needed"] > 2


# ── seats：solo 的语义分水岭 ────────────────────────────────────────────────


def test_all_accounts_joining_every_group_is_not_solo_no_matter_who_speaks():
    """10 个号全加进 100 个群、只让 1 个说话——成员轴照样是灾难，必须报出来。"""
    plan = plan_capacity(pool=10, groups=100, seats=3, speakers=1)
    assert plan["binding"] == AXIS_MEMBER
    assert plan["max_groups"] == max_safe_groups(pool=10, seats=3)
    assert plan["fits"] is False


def test_one_account_per_group_zeroes_both_pair_axes():
    """真 solo（每群只进一个号）时两条号对轴同时归零，只剩主推轴。"""
    plan = plan_capacity(pool=10, groups=100, seats=1, speakers=1)
    axes = {a["axis"]: a for a in plan["axes"]}
    assert AXIS_MEMBER not in axes, "每群只进一个号，成员面不可能有号对"
    # 发言轴要**留在表里但标不设限**：删掉它运营会以为这条轴没查，
    # 而「查过了，确实不设限」与「没查」在风险沟通上是两回事。
    assert axes[AXIS_PAIR]["unlimited"] is True
    assert plan["binding"] == AXIS_ROLE


def test_axes_not_asked_about_are_absent_rather_than_padded():
    """没问到的轴不进列表——占位大数会参与取最小，缺省值一改容量就莫名缩水。"""
    plan = plan_capacity(pool=10, groups=50)
    assert {a["axis"] for a in plan["axes"]} == {AXIS_ROLE}


# ── 单号负载：只报事实，不编阈值 ────────────────────────────────────────────


def test_per_account_load_is_reported_as_a_fact_without_a_verdict():
    """solo 铺得越开这个数越大，是它唯一还在涨的成本；但阈值不该由本层瞎编。"""
    plan = plan_capacity(pool=10, groups=100, speakers=1, advocate_ratio=0)
    assert plan["groups_per_account"] == 10
    assert plan["verdict"] == VERDICT_SAFE, "负载高不该自动变成警报"


# ── 判词与建议 ──────────────────────────────────────────────────────────────


def test_verdict_separates_a_near_miss_from_a_hopeless_plan():
    """超 50% 以内调密度就能进来，翻倍以上必须动号池——两种处方不该共用一盏灯。"""
    assert plan_capacity(pool=10, groups=30, speakers=1)["verdict"] == VERDICT_SAFE
    assert plan_capacity(pool=10, groups=40, speakers=1)["verdict"] == VERDICT_WARN
    assert plan_capacity(pool=10, groups=100, speakers=1)["verdict"] == VERDICT_DANGER


def test_advice_states_the_gap_and_then_a_workable_mix():
    plan = plan_capacity(pool=10, groups=100, speakers=1)
    text = " ".join(plan["advice"])
    assert "100" in text and "30" in text, "得说清差多少"
    assert "30%" in text, "得给出能落地的带货密度"


def test_advice_never_contradicts_itself_when_seats_are_the_real_blocker():
    """真实事故：建议里同时出现「铺不动，得补到 18 个号」和「这么改就能铺满」。

    根因是重算建议时把 ``seats`` 丢了，于是那句「改成这样」只看了演出面。两句自相矛盾
    的话摆在同一屏上，比不给建议还糟——运营会照着乐观的那句做，因为它是他想听的。
    """
    plan = plan_capacity(pool=10, groups=100, seats=3, speakers=1)
    assert plan["binding"] == AXIS_MEMBER
    text = " ".join(plan["advice"])
    assert "铺不满" in text, "成员轴撑不住就得直说"
    assert "要铺满" not in text, "不能在同一屏上又说铺不动、又说这么改就行"
    assert "每群少进" in text, "成员轴唯一能归零的出路必须说出来（比补号便宜得多）"


def test_advice_reads_as_a_sentence_at_full_selling_density():
    """「只有 100% 的场次带货」是病句，而这句话是整条建议的落点。"""
    # 4 人同台撑不住 40 个群（发言轴 35），但降到 3 人就够，且主推轴 45 > 40 不用摊薄
    plan = plan_capacity(pool=15, groups=40, speakers=4)
    assert plan["fits"] is False and plan["binding"] == AXIS_PAIR
    text = " ".join(plan["advice"])
    assert "要铺满" in text, "这组参数就是为了走「改成这样就能铺满」那条分支"
    assert "只有 100%" not in text and "每场都可以带货" in text


def test_advice_survives_a_plan_with_no_ceiling_at_all():
    plan = plan_capacity(pool=10, groups=50, speakers=1, advocate_ratio=0)
    assert plan["advice"] and plan["max_groups"] is None


# ── 与别的轴口径一致（漂了看板上就会出现两个数） ────────────────────────────


def test_pair_axis_reuses_attendance_and_does_not_recompute_it():
    for k in (2, 3, 4):
        assert pair_capacity(pool=12, speakers=k) == max_safe_groups(pool=12, seats=k)
    assert pair_capacity(pool=12, speakers=1) is None


def test_role_axis_matches_the_table_published_in_the_roles_module():
    """roles.py 的 docstring 印了 30/60/90 这张表，这里不能算出别的数。"""
    for pool, expect in ((10, 30), (20, 60), (30, 90)):
        assert role_capacity(pool=pool, advocate_ratio=1.0) == expect
    assert NORMAL_PAIR_CO == 3 and NORMAL_ROLE_CO == 3, "两条轴的常人区间同量级"


# ── 脏输入一律降级，不抛 ────────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs", [
    {"pool": None, "groups": "x"},
    {"pool": -5, "groups": -5, "speakers": -1, "advocate_ratio": "nope"},
    {"pool": object(), "groups": object()},
])
def test_garbage_degrades_to_a_shaped_empty_result(kwargs):
    plan = plan_capacity(**kwargs)
    assert isinstance(plan, dict) and "axes" in plan and "max_groups" in plan
    assert isinstance(recommend_mix(pool=kwargs.get("pool"),
                                    groups=kwargs.get("groups")), dict)


def test_pool_needed_never_returns_zero_for_a_real_target():
    assert pool_needed(groups=0) == 0
    assert pool_needed(groups=100) >= 1
    # 每群进 3 个号时，成员轴常常才是真瓶颈，比主推轴要的号多得多
    assert pool_needed(groups=100, seats=3) > pool_needed(groups=100)
    assert pool_needed(groups=100, advocate_ratio=MIN_ADVOCATE_RATIO) >= 1
