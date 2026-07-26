"""开演排期门禁 —— 第四条暴露轴（时间）的不变量。

前三条轴（成员 / 号对共现 / 角色）都是**静态**的：把它们洗干净之后，一批号在一批群里
从任何一张快照上看都很正常。这一层守的是快照之外的东西——**把时间戳拉到一起做聚合时**
才显形的特征。它有个讨厌的性质：每一条单独看都不像 bug，排期表照样能执行、戏照样能演，
所以没有门禁钉着就不会有人发现漏了。

四类不变量，各自对应一种真实的识别手段：

1. **时段**（活动窗 / 安静窗）——深夜开演肉眼可见，且安静窗必须**硬**过活动窗，
   否则配置手滑就能把一整批戏排进凌晨三点。
2. **规律性**（等距 / 整点 / 每天同一时刻）——做一次自相关就出来，是这条轴上最好抓的。
   注意它是**默认会发生**的：纯函数同输入同输出，不刻意打散就天然周期。
3. **密度**（一个小时里挤几场）——100 个群塞进 13 小时是真实处境，排期器不许自作主张
   砍掉排不下的群，但必须如实把密度报出来让人去决策。
4. **同号跨群间隔**——这条不由排期保证（排期时还没选角），由运行期闸门兜；闸门的边界
   （恰好等于阈值 / 缺记录 / 关闭）写反任何一个，要么全员开不了口，要么护栏形同虚设。
"""
from __future__ import annotations

import statistics as st
from typing import Any, Dict, List

from src.companion.group_show.attendance import (
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
)
from src.companion.group_show.schedule import (
    DEFAULT_ACTIVE_HOURS,
    HOT_HOUR_SHOWS,
    MIN_CROSS_GROUP_GAP_SEC,
    QUIET_HOURS,
    ROUND_MINUTES,
    SAME_GROUP_MIN_GAP_SEC,
    admits_at_time,
    next_allowed_ts,
    plan_show_times,
    schedule_metrics,
)

#: 排期这一天的「本地零点」。取一个能被 86400 整除的 epoch，测试里读小时数直观；
#: 本模块不做时区换算，具体取哪一天不影响任何断言。
DAY = 1_753_488_000.0


class _Explodes:
    """``__str__`` 会炸的对象——外部台账里真的会混进这种东西（ORM 行、懒加载代理）。"""

    def __str__(self) -> str:
        raise RuntimeError("boom")


def _groups(n: int) -> List[str]:
    return [f"gg{i:03d}" for i in range(n)]


def _gaps(plan: List[Dict[str, Any]]) -> List[float]:
    times = [row["at"] for row in plan]
    return [times[i + 1] - times[i] for i in range(len(times) - 1)]


def _cv(values: List[float]) -> float:
    """变异系数：等距 ⇒ 0，分层抖动 ⇒ 0.37 上下。"""
    mean = st.mean(values)
    return st.pstdev(values) / mean if mean else 0.0


# ── 时段：活动窗是偏好，安静窗是事实 ────────────────────────────────────────


def test_every_show_lands_inside_the_active_window():
    """排在活动时段外＝人设那个点不该在线。这条一破，后面所有节奏优化都没有意义。"""
    plan = plan_show_times(_groups(40), day_start_ts=DAY, seed="s")
    assert len(plan) == 40
    start, end = DEFAULT_ACTIVE_HOURS
    assert all(start <= row["hour"] < end for row in plan), sorted(
        {row["hour"] for row in plan})
    # hour 字段必须与 at 对得上，否则度量端按 hour 判时段、执行端按 at 发消息，两边会错开
    for row in plan:
        assert row["hour"] == int((row["at"] - DAY) // 3600) % 24


def test_quiet_hours_win_when_they_overlap_the_active_window():
    """两个窗口冲突时安静窗必须赢。

    反过来（活动窗覆盖安静窗）意味着运营在配置里写宽一点就能把戏排进深夜——那不是
    「灵活」，那是把这条护栏做成了摆设。
    """
    plan = plan_show_times(_groups(30), day_start_ts=DAY,
                           active_hours=(6, 23), quiet_hours=(20, 9), seed="s")
    assert plan
    assert all(9 <= row["hour"] < 20 for row in plan), sorted(
        {row["hour"] for row in plan})
    assert schedule_metrics(plan, quiet_hours=(20, 9))["quiet_violations"] == 0


def test_a_night_window_wraps_midnight_instead_of_collapsing():
    """夜场 ``(20, 3)`` 是跨午夜窗，不是「起点大于终点＝非法」——按非法处理会静默排出空表。"""
    plan = plan_show_times(_groups(8), day_start_ts=DAY,
                           active_hours=(20, 3), seed="s")
    assert len(plan) == 8
    # 20..22 可用，23 点起进默认安静时段被掐掉（安静窗对夜场同样是硬的）
    assert QUIET_HOURS == (23, 8)
    assert set(row["hour"] for row in plan) <= {20, 21, 22}


def test_an_active_window_swallowed_by_quiet_hours_plans_nothing():
    """活动窗整个落在安静时段 ⇒ 今天不演。宁可不演也不在深夜演——放宽这一条等于
    把「安静时段是硬的」那句话作废。"""
    assert plan_show_times(_groups(5), day_start_ts=DAY,
                           active_hours=(1, 5)) == []


# ── 规律性：这条轴上最好抓的特征 ────────────────────────────────────────────


def test_no_show_opens_on_the_hour_or_the_half_hour():
    """整点开演是机器排期最刺眼的一条：人不会掐着表开口。

    秒位也要抖——只抖到分钟同样是机器痕迹，而且它比整点更隐蔽（分钟看起来很随机，
    秒却清一色是 0）。
    """
    plan = plan_show_times(_groups(60), day_start_ts=DAY, seed="s")
    assert all(row["minute"] not in ROUND_MINUTES for row in plan)
    assert schedule_metrics(plan)["round_minute_ratio"] == 0.0

    seconds = {int(row["at"]) % 60 for row in plan}
    assert len(seconds) > 20, seconds          # 秒位真的在抖，不是固定几个值
    assert schedule_metrics(plan)["whole_minute_ratio"] < 0.1


def test_gaps_have_real_variance_instead_of_a_fixed_step():
    """「每 N 分钟一场」是自相关一算就出来的。可断言的判据有两条，都要过：

    * 相邻间隔的**变异系数 >= 0.25**（等距 ⇒ 0；本模块的分层抖动理论值 0.37）；
    * ``regularity``（贴着中位数 ±10% 的间隔占比）**<= 0.4**（等距 ⇒ 1.0）。

    只断言「有抖动」是没用的：等距 + 1 秒抖动同样「有抖动」，却照样是完美的周期信号。
    多个 seed 一起跑，防的是「碰巧这一个种子排得好」。
    """
    for seed in ("s0", "s1", "s2", "s3", "s4"):
        plan = plan_show_times(_groups(40), day_start_ts=DAY, seed=seed)
        gaps = _gaps(plan)
        assert _cv(gaps) >= 0.25, (seed, _cv(gaps))
        assert schedule_metrics(plan)["regularity"] <= 0.4, seed


def test_the_same_seed_replays_the_same_table():
    """同输入同输出是排练与回放的前提：运营照着表执行到一半刷新页面，不能换一套。

    这也是为什么抖动走 ``zlib.crc32`` 而不是 ``random`` / 内置 ``hash()``——后者对 str
    每进程加盐，换个 worker 进程就是另一张表。
    """
    first = plan_show_times(_groups(30), day_start_ts=DAY, seed="s")
    again = plan_show_times(_groups(30), day_start_ts=DAY, seed="s")
    assert first == again
    # 群清单顺序不同不该换表（上游可能来自 set / 不同排序的查询）
    shuffled = plan_show_times(list(reversed(_groups(30))),
                               day_start_ts=DAY, seed="s")
    assert {r["group"]: r["at"] for r in shuffled} == {
        r["group"]: r["at"] for r in first}


def test_a_different_seed_reshuffles_the_whole_day():
    """换 seed 要真的换一张表，不能只挪几秒——否则「重排一次」这个操作是假的。"""
    first = plan_show_times(_groups(30), day_start_ts=DAY, seed="s1")
    other = plan_show_times(_groups(30), day_start_ts=DAY, seed="s2")
    a = {r["group"]: r["at"] for r in first}
    b = {r["group"]: r["at"] for r in other}
    moved = sum(1 for g in a if abs(a[g] - b[g]) > 600)
    assert moved >= 25, moved


def test_the_next_day_reshuffles_even_with_an_unchanged_seed():
    """守的是「每天同一时刻开演」——这条特征**默认会发生**：纯函数同输入同输出，
    调用方每天拿同一份群清单和同一个 seed 跑，排出来的就是同一张表。

    对策是把 ``day_start_ts`` 拌进抖动种子，而不是指望调用方每天记得换 seed（那种约定
    迟早忘，而忘了之后表面上一切正常）。同一天重跑仍要逐行一致，上一条测试钉着。
    """
    today = {r["group"]: r["at"] - DAY
             for r in plan_show_times(_groups(30), day_start_ts=DAY, seed="s")}
    tomorrow = {r["group"]: r["at"] - (DAY + 86400.0)
                for r in plan_show_times(_groups(30),
                                         day_start_ts=DAY + 86400.0, seed="s")}
    moved = sum(1 for g in today if abs(today[g] - tomorrow[g]) > 600)
    assert moved >= 25, moved


# ── 同一个群一天多场 ────────────────────────────────────────────────────────


def test_multiple_shows_per_group_keep_a_real_gap():
    """同一个群一小时内演两场，群里的人自己就会觉得「刚才不是聊过一轮吗」。"""
    plan = plan_show_times(_groups(20), day_start_ts=DAY, per_day=3, seed="s")
    assert len(plan) == 60

    per_group: Dict[str, List[float]] = {}
    for row in plan:
        per_group.setdefault(row["group"], []).append(row["at"])
    assert all(len(v) == 3 for v in per_group.values())
    assert {row["nth"] for row in plan} == {1, 2, 3}

    for group, times in per_group.items():
        times.sort()
        for i in range(len(times) - 1):
            assert times[i + 1] - times[i] >= SAME_GROUP_MIN_GAP_SEC, group


def test_the_same_group_does_not_play_at_a_fixed_period():
    """满足了最小间隔还不够：「每 4 小时准时一场」照样是周期信号，而且比扎堆更好抓。

    最容易踩的实现是「按上次开演时间排序去填下一轮」——它保证了最小间隔，却把同群
    间隔钉成常数。这里要求**至少六成的群**两段间隔相差 10% 以上。
    """
    plan = plan_show_times(_groups(20), day_start_ts=DAY, per_day=3, seed="s")
    per_group: Dict[str, List[float]] = {}
    for row in plan:
        per_group.setdefault(row["group"], []).append(row["at"])

    uneven = 0
    for times in per_group.values():
        times.sort()
        first, second = times[1] - times[0], times[2] - times[1]
        if abs(first - second) > 0.1 * min(first, second):
            uneven += 1
    assert uneven >= 12, uneven


# ── 密度：排不下是真实处境，不是异常 ────────────────────────────────────────


def test_a_hundred_groups_in_thirteen_hours_plans_all_of_them():
    """10 个号铺 100 个群是本子系统的典型规模，13 小时活动窗塞 100 场就是它的日常。

    排期器**不许**自作主张砍掉排不下的群（运营会以为那些群今天不用演），要如实排满，
    再把密度问题交给度量报出来由人决策。
    """
    plan = plan_show_times(_groups(100), day_start_ts=DAY, seed="s")
    assert len(plan) == 100
    assert len({row["group"] for row in plan}) == 100
    assert all(row["at"] < row2["at"] or row["group"] <= row2["group"]
               for row, row2 in zip(plan, plan[1:]))

    metrics = schedule_metrics(plan)
    assert metrics["count"] == 100
    assert metrics["peak_hour_count"] > HOT_HOUR_SHOWS
    assert metrics["verdict"] == VERDICT_WARN
    assert any("小时" in line for line in metrics["advice"])


def test_a_comfortable_day_reads_clean():
    """反面钉子：场次配得下时必须报 safe。天天报警的看板等于没有看板。"""
    metrics = schedule_metrics(
        plan_show_times(_groups(13), day_start_ts=DAY, seed="s"))
    assert metrics["verdict"] == VERDICT_SAFE
    assert metrics["quiet_violations"] == 0
    assert metrics["advice"]                 # 干净也要给一句读数，别留空


# ── 脏输入：一律软降级，绝不抛 ──────────────────────────────────────────────


def test_an_empty_or_unusable_group_list_plans_nothing():
    assert plan_show_times([], day_start_ts=DAY) == []
    assert plan_show_times(None, day_start_ts=DAY) == []
    assert plan_show_times(123, day_start_ts=DAY) == []


def test_a_bare_string_is_one_group_not_a_pile_of_letters():
    """裸字符串可遍历：直接 ``iter`` 会把 ``"gg1"`` 拆成三个「群」，排出来的表看着完全
    正常，实际在给三个不存在的群排戏——最难发现的那类静默破口。"""
    plan = plan_show_times("gg1", day_start_ts=DAY, seed="s")
    assert [row["group"] for row in plan] == ["gg1"]


def test_an_attendance_ledger_can_be_handed_over_as_is():
    """``plan_attendance().assignments`` 直接喂进来是最顺手的用法。按「一行群记录」处理
    会静默排出空表，而空表在页面上跟「今天不用演」长得一模一样。"""
    plan = plan_show_times({"gg1": ["a", "b"], "gg2": ["a", "c"]},
                           day_start_ts=DAY, seed="s")
    assert sorted(row["group"] for row in plan) == ["gg1", "gg2"]
    # 单行群记录仍按一个群处理（两种形状必须都认）
    assert [r["group"] for r in plan_show_times({"group_id": "gg9"},
                                                day_start_ts=DAY)] == ["gg9"]


def test_per_day_zero_means_no_shows_today():
    """``0`` 是说得通的意思（今天不演）。这里刻意不像 ``schedule_joins`` 那样钳成 1——
    那边 0 会转死循环、钳位是防御，这边偷偷改成 1 等于替运营做了「开演」的决定。"""
    assert plan_show_times(_groups(3), day_start_ts=DAY, per_day=0) == []
    assert plan_show_times(_groups(3), day_start_ts=DAY, per_day=-2) == []
    # 上限兜住离谱配置，不至于把内存排爆
    assert len(plan_show_times(_groups(2), day_start_ts=DAY, per_day=999)) <= 12


def test_planning_survives_every_kind_of_garbage():
    """配置是人手写的 YAML、群清单来自库和前端 body——每一种脏法都会真的出现。

    这里只要求「不抛 + 形状是 list」：排期是编排链路上的一环，炸了会拦住整场演出。
    """
    dirty = [
        dict(groups=_groups(3), day_start_ts=None),
        dict(groups=_groups(3), day_start_ts="abc"),
        dict(groups=_groups(3), day_start_ts=-86400.0),
        dict(groups=_groups(3), day_start_ts=float("nan")),
        dict(groups=_groups(3), day_start_ts=DAY, per_day="x"),
        dict(groups=_groups(3), day_start_ts=DAY, active_hours=(22, 9)),
        dict(groups=_groups(3), day_start_ts=DAY, active_hours="abc"),
        dict(groups=_groups(3), day_start_ts=DAY, active_hours=(99, -7)),
        dict(groups=_groups(3), day_start_ts=DAY, active_hours=(9, 9)),
        dict(groups=_groups(3), day_start_ts=DAY, quiet_hours=None),
        dict(groups=_groups(3), day_start_ts=DAY, quiet_hours=(0, 0)),
        dict(groups=_groups(3), day_start_ts=DAY, seed=None),
        dict(groups=_groups(3), day_start_ts=DAY, seed=_Explodes()),
        dict(groups=[None, "", _Explodes(), "gg1", "gg1"], day_start_ts=DAY),
        dict(groups=[{"nothing": 1}, {"chat_key": "gg2"}], day_start_ts=DAY),
        dict(groups=iter(["gg1", "gg2"]), day_start_ts=DAY),
    ]
    for kwargs in dirty:
        got = plan_show_times(**kwargs)
        assert isinstance(got, list), kwargs
        for row in got:
            assert set(row) == {"group", "at", "hour", "minute", "nth"}
            assert row["at"] == row["at"]        # 绝不放 NaN 进时间戳

    # 反了的活动窗按跨午夜解释，剩下的可用小时仍然避开安静时段
    flipped = plan_show_times(_groups(3), day_start_ts=DAY, active_hours=(22, 9))
    assert flipped and all(row["hour"] in (22, 8) for row in flipped)
    # 群标识全脏 ⇒ 只剩去重后的那一个
    assert len(plan_show_times([None, "", _Explodes(), "gg1", "gg1"],
                               day_start_ts=DAY)) == 1


# ── 度量：burstiness 取最坏那个小时 ─────────────────────────────────────────


def test_burstiness_reports_the_worst_hour_not_the_average():
    """平均值必然等于「1/可用小时数」，只反映时段配置、跟排得好不好毫无关系。

    这张表 11 场里 8 场挤在 14 点：平均每小时 2.75 场看着很健康，实际废了。
    平台聚合时间戳时抓的就是这个尖峰。
    """
    plan = [{"group": f"gg{i}", "at": DAY + 14 * 3600 + i * 211 + 37, "hour": 14}
            for i in range(8)]
    plan += [{"group": f"hh{i}", "at": DAY + (10 + i) * 3600 + 133, "hour": 10 + i}
             for i in range(3)]
    metrics = schedule_metrics(plan)
    assert metrics["count"] == 11
    assert metrics["peak_hour"] == 14
    assert metrics["peak_hour_count"] == 8
    assert metrics["burstiness"] == 8 / 11
    assert metrics["verdict"] == VERDICT_DANGER
    # 建议要点名「哪几个群、哪个小时」，「注意风险」这种话没法执行
    assert any("14" in line and "gg" in line for line in metrics["advice"])


def test_regularity_is_one_for_a_metronome():
    """精确每 30 分钟一场 ⇒ ``regularity`` 满分、判词 danger。

    这是本层最该抓住的一张表，因为它在任何静态视图里都完全正常：群不重合、号不重合、
    角色也轮换了，只有把时间戳排成一列才看得出来它是定时任务。
    """
    plan = [{"group": f"gg{i}", "at": DAY + 9 * 3600 + i * 1800,
             "hour": (9 + i // 2) % 24} for i in range(20)]
    metrics = schedule_metrics(plan)
    assert metrics["regularity"] == 1.0
    assert metrics["round_minute_ratio"] == 1.0
    assert metrics["whole_minute_ratio"] == 1.0
    assert metrics["verdict"] == VERDICT_DANGER
    assert any("等距" in line for line in metrics["advice"])


def test_regularity_ignores_a_couple_of_outliers():
    """用中位数而不是均值：跨过一段禁演时段留下的一两个长间隔，不该把「其余全部等距」
    这个事实洗白。"""
    times = [DAY + 9 * 3600 + i * 900 for i in range(12)]
    times += [t + 5 * 3600 for t in times[:3]]           # 三个离群的长间隔
    plan = [{"group": f"gg{i}", "at": t, "hour": int((t - DAY) // 3600) % 24}
            for i, t in enumerate(sorted(times))]
    assert schedule_metrics(plan)["regularity"] >= 0.7


def test_quiet_violations_are_counted_and_escalate():
    """深夜开演：人设该睡觉的时候演戏，既不像真人又拿不到互动。一场是手滑，
    四分之一以上是整张表的时段配错了——两者判词要分开。"""
    one_bad = [{"group": f"gg{i}", "at": DAY + 10 * 3600 + i * i * 431 + i * 97,
                "hour": 10 + (i * i * 431 + i * 97) // 3600} for i in range(9)]
    one_bad.append({"group": "gg9", "at": DAY + 3 * 3600 + 41, "hour": 3})
    metrics = schedule_metrics(one_bad)
    assert metrics["quiet_violations"] == 1
    assert metrics["verdict"] == VERDICT_WARN

    all_night = [{"group": f"gg{i}", "at": DAY + 2 * 3600 + i * 611, "hour": 2}
                 for i in range(6)]
    assert schedule_metrics(all_night)["verdict"] == VERDICT_DANGER
    assert any("安静时段" in line
               for line in schedule_metrics(all_night)["advice"])


def test_quiet_window_is_configurable_for_a_night_shift_playbook():
    """夜场剧本按默认作息体检会被报一堆假违规，报几次之后这张卡就没人看了。"""
    night = [{"group": f"gg{i}", "at": DAY + 2 * 3600 + i * 811, "hour": 2}
             for i in range(6)]
    assert schedule_metrics(night, quiet_hours=(9, 18))["quiet_violations"] == 0


def test_daily_repeat_catches_the_same_time_every_day():
    """跨天实况里「每天 14 点准时开演」——比扎堆更好抓（自相关一算就出来），
    而单日视角完全看不见它。"""
    plan = [{"group": f"gg{d}", "at": DAY + d * 86400 + 14 * 3600 + d * 37,
             "hour": 14} for d in range(10)]
    metrics = schedule_metrics(plan)
    assert metrics["span_hours"] > 24
    assert metrics["daily_repeat"] == 1.0
    assert metrics["verdict"] == VERDICT_DANGER
    assert any("钟点" in line for line in metrics["advice"])


def test_daily_repeat_stays_quiet_inside_a_single_day():
    """一天之内每个钟头最多出现一次，这个数会退化成 burstiness 的复读——不该重复报警。"""
    metrics = schedule_metrics(
        plan_show_times(_groups(30), day_start_ts=DAY, seed="s"))
    assert metrics["daily_repeat"] == 0.0


def test_tiny_plans_do_not_trigger_trivially_true_ratios():
    """1 场戏的「最挤小时占比」恒为 100%，2 个间隔「都等于中位数」也恒为真。
    平凡真不含信息，报出来只会训练运营忽略这张卡。"""
    tiny = [{"group": "gg1", "at": DAY + 11 * 3600 + 77, "hour": 11}]
    metrics = schedule_metrics(tiny)
    assert metrics["count"] == 1
    assert metrics["burstiness"] == 0.0
    assert metrics["regularity"] == 0.0
    assert metrics["verdict"] == VERDICT_SAFE
    # 但安静时段是按单场判的，样本再少也要报
    assert schedule_metrics(
        [{"group": "gg1", "at": DAY + 3 * 3600, "hour": 3}]
    )["verdict"] == VERDICT_WARN


def test_metrics_read_a_freshly_planned_day_end_to_end():
    """端到端：排期器的产出直接喂给度量，中间不需要任何形状转换。"""
    metrics = schedule_metrics(
        plan_show_times(_groups(40), day_start_ts=DAY, per_day=2, seed="s"))
    assert metrics["count"] == 80
    assert metrics["quiet_violations"] == 0
    assert metrics["round_minute_ratio"] == 0.0
    assert 0.0 < metrics["span_hours"] <= 13.0
    assert set(metrics["by_hour"]) <= set(range(*DEFAULT_ACTIVE_HOURS))


def test_metrics_tolerate_garbage():
    """看板拿不到数也不该白屏：任何脏输入都要给一份形状完整的空报告。"""
    shape = set(schedule_metrics(
        plan_show_times(_groups(5), day_start_ts=DAY)).keys())
    for junk in (None, "nonsense", 42, [1, 2, 3], [{"at": "x"}], [{}],
                 iter([]), {"at": DAY}, [_Explodes()],
                 [{"group": _Explodes(), "at": DAY, "hour": 99}],
                 [{"group": "g", "at": float("inf"), "hour": 1}],
                 [{"group": "g", "at": DAY, "hour": True}]):
        metrics = schedule_metrics(junk)
        assert set(metrics.keys()) == shape, junk
        assert metrics["verdict"] in (VERDICT_SAFE, VERDICT_WARN, VERDICT_DANGER)
        assert isinstance(metrics["advice"], list)
    assert schedule_metrics(None)["count"] == 0
    assert schedule_metrics([{"group": "g", "at": DAY, "hour": True}])["count"] == 1
    assert schedule_metrics(_groups(3), quiet_hours="abc")["count"] == 0


def test_metrics_sort_a_shuffled_real_world_log():
    """实况来自库查询，顺序没保证。不先排序的话间隔会算出负数，规律性直接失真。"""
    rows = [{"group": f"gg{i}", "at": DAY + 10 * 3600 + i * 703 + 11, "hour": 10}
            for i in range(6)]
    forward = schedule_metrics(rows)
    backward = schedule_metrics(list(reversed(rows)))
    assert forward["median_gap_sec"] == backward["median_gap_sec"] == 703
    assert forward["min_gap_sec"] > 0


def test_metrics_skip_the_quiet_check_when_the_hour_is_missing():
    """实况可能没带 ``hour``。从时间戳反推小时要假定一个时区，而时区只有调用方知道——
    猜一个的代价是把下午的戏报成深夜违规，假告警比少一个指标贵得多。"""
    rows = [{"group": f"gg{i}", "at": DAY + 2 * 3600 + i * 900} for i in range(6)]
    metrics = schedule_metrics(rows)
    assert metrics["count"] == 6
    assert metrics["quiet_violations"] == 0
    assert metrics["peak_hour"] is None
    assert metrics["by_hour"] == {}
    assert metrics["burstiness"] > 0        # 扎堆不依赖 hour，照样算得出来


# ── 运行期闸门：同一个号的跨群间隔 ──────────────────────────────────────────


def test_exactly_at_the_minimum_gap_is_admitted():
    """阈值语义是「至少隔这么久」，边界含在内。差一秒来回拉扯没有任何风险意义，
    却会让排期器在边界上反复顺延。"""
    last = {"a1": 1000.0}
    assert admits_at_time("a1", 1000.0 + MIN_CROSS_GROUP_GAP_SEC, last) is True
    assert admits_at_time("a1", 1000.0 + MIN_CROSS_GROUP_GAP_SEC - 1, last) is False


def test_a_missing_record_admits():
    """冷启动、重启、换库之后这张表必然是空的。保守解释（没记录＝当刚说过）会让整批号
    第一天全部开不了口，而运营对付「全都不发言」的唯一手段就是把护栏关掉。"""
    assert admits_at_time("a1", 5000.0, {}) is True
    assert admits_at_time("a1", 5000.0, {"a2": 4999.0}) is True
    assert admits_at_time("a1", 5000.0, None) is True
    assert admits_at_time("a1", 5000.0, {"a1": None}) is True
    assert admits_at_time("a1", 5000.0, {"a1": "not-a-time"}) is True


def test_a_zero_or_negative_gap_turns_the_guard_off():
    last = {"a1": 4999.0}
    assert admits_at_time("a1", 5000.0, last) is False
    assert admits_at_time("a1", 5000.0, last, min_gap=0) is True
    assert admits_at_time("a1", 5000.0, last, min_gap=-1) is True


def test_a_record_in_the_future_blocks_and_pushes_forward():
    """判据是**有向**的：记录在未来（时钟漂移，或同一批表里已排定的更晚一场）一律拦下。

    这里刻意不用「间隔取绝对值」那套对称语义——踩过一次：某个号被顶到 9:20 之后，表里
    9:04 那一场会因为「跟 9:20 也隔了 16 分钟」而被放行，于是这个号的发言被排成 9:20、
    9:04，游标再也推不动、护栏静默失效（下面 ``test_the_guard_spaces_a_busy_account``
    正是那条链路）。有向语义顺便还偏保守，两头都对。
    """
    last = {"a1": 9000.0}
    assert admits_at_time("a1", 8800.0, last) is False
    assert admits_at_time("a1", 9000.0 - MIN_CROSS_GROUP_GAP_SEC, last) is False
    assert next_allowed_ts("a1", 8800.0, last) == 9000.0 + MIN_CROSS_GROUP_GAP_SEC


def test_next_allowed_ts_returns_the_asked_time_when_nothing_blocks():
    """放行时绝不凭空往后推：调用方拿它当「这场几点开」直接用，无条件加个间隔会让整张
    排期表每过一道护栏就整体后移，几轮下来全被挤出活动时段。"""
    assert next_allowed_ts("a1", 5000.0, {}) == 5000.0
    assert next_allowed_ts("a1", 5000.0, {"a2": 4999.0}) == 5000.0
    assert next_allowed_ts("a1", 5000.0, {"a1": 4999.0}, min_gap=0) == 5000.0
    assert next_allowed_ts("a1", 5000.0, {"a1": 100.0}) == 5000.0


def test_next_allowed_ts_pushes_to_exactly_last_plus_gap():
    """被挡下时给的是「最早什么时候能上」，好让调用方把这场往后**挪**而不是取消。
    取消是过度反应——跨群间隔是节奏问题，不是「这个号有问题」，而且会让那个群当天彻底
    没动静。"""
    got = next_allowed_ts("a1", 5000.0, {"a1": 4999.0})
    assert got == 4999.0 + MIN_CROSS_GROUP_GAP_SEC
    assert admits_at_time("a1", got, {"a1": 4999.0}) is True   # 挪过去必定放行
    # 挪一次就够，不该越挪越远
    assert next_allowed_ts("a1", got, {"a1": 4999.0}) == got


def test_the_runtime_guards_never_raise():
    """闸门算不出来一律放行：护栏不该反过来卡死演出（与 performance.admits_speaker 同款
    立场）。这里连 ``__str__`` 会炸的账号对象都要扛住。"""
    junk = [
        ("a1", None, {"a1": 1.0}),
        (None, 5000.0, {"a1": 1.0}),
        (_Explodes(), 5000.0, {"a1": 1.0}),
        ("a1", "abc", {"a1": 1.0}),
        ("a1", float("nan"), {"a1": 1.0}),
        ("a1", -5000.0, {"a1": -6000.0}),
        ("a1", 5000.0, "not-a-mapping"),
        ("a1", 5000.0, [("a1", 1.0)]),
        ("a1", 5000.0, {"a1": float("inf")}),
    ]
    for account, when, last in junk:
        assert isinstance(admits_at_time(account, when, last), bool)
        got = next_allowed_ts(account, when, last)
        assert isinstance(got, float) and got == got
    assert admits_at_time("a1", 5000.0, {"a1": 1.0}, min_gap="x") is True
    assert next_allowed_ts("a1", 5000.0, {"a1": 1.0}, min_gap=None) == 5000.0


def test_the_guard_spaces_a_busy_account_across_groups():
    """端到端：一个号被排在五个群里连着开口，闸门把它们摊成至少 10 分钟一档。

    这条是特征 1 的落点——排期只管「这个群几点开演」，「这个号能不能开口」要到选角
    之后才判得了，所以两段必须能拼起来用。顺带钉住 ``next_allowed_ts`` 的**单调性**：
    顶后的时刻恒 ``>= when_ts``，否则这个游标循环会把发言排成倒序（真踩过）。
    """
    last: Dict[str, float] = {}
    at = DAY + 9 * 3600
    accepted = []
    for _ in range(5):
        when = next_allowed_ts("a1", at, last)
        assert admits_at_time("a1", when, last) is True
        last["a1"] = when
        accepted.append(when)
        at += 90.0                            # 排期把五场排得只差 90 秒
    gaps = [accepted[i + 1] - accepted[i] for i in range(4)]
    assert all(g >= MIN_CROSS_GROUP_GAP_SEC for g in gaps), gaps
