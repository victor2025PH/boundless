"""出席矩阵门禁 —— 多号多群模式下的头号可检测特征：共同出席。

这一层守的不变量比「代码不崩」重要得多：**排出来的表必须真的把共同出席压下去**。
所以本文件里有相当一部分是「量化断言」——直接跑 100 个群的排班，断言最坏一对的
共享群数落在真人正常区间。度量口径漂了、贪心退化了，这里先红。
"""
from __future__ import annotations

import pytest

from src.companion.group_show.attendance import (
    ALARM_PAIR_CO,
    NORMAL_PAIR_CO,
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
    attendance_metrics,
    cast_entanglement,
    co_attendance,
    format_plan,
    max_safe_groups,
    plan_attendance,
    recommend_pool_size,
    rotate_roles,
    schedule_joins,
    split_ledger,
)


def _pool(n: int):
    return [f"a{i:02d}" for i in range(n)]


def _groups(n: int):
    return [f"g{i:03d}" for i in range(n)]


# ── 共同出席计数 ────────────────────────────────────────────────────────────


def test_co_attendance_counts_unordered_pairs():
    """号对无序：(A,B) 与 (B,A) 必须记到同一个键上，否则重合度会被算成一半。"""
    counter = co_attendance({"g1": ["b", "a"], "g2": ["a", "b"], "g3": ["a", "c"]})
    assert counter[("a", "b")] == 2
    assert counter[("a", "c")] == 1
    assert ("b", "a") not in counter


def test_co_attendance_ignores_duplicate_membership():
    """同一个号在一个群里登记两次不该让它跟自己/别人多算一次。"""
    counter = co_attendance({"g1": ["a", "a", "b"]})
    assert counter == {("a", "b"): 1}


def test_co_attendance_tolerates_dirty_rows():
    counter = co_attendance({"g1": ["a", "", None, "b"], "g2": None})
    assert counter == {("a", "b"): 1}


# ── 度量与判词 ──────────────────────────────────────────────────────────────


def test_zero_co_attendance_pairs_are_counted_in_the_average():
    """从没同台过的号对共享 0 个群，必须进分母。

    只对「有交集的号对」求平均会把均值抬高——号池越大、稀疏得越好，被漏掉的 0 值对
    就越多，于是「排得越好，平均分越难看」，指标方向反了。
    """
    m = attendance_metrics({"g1": ["a", "b"]}, pool=["a", "b", "c", "d"])
    assert m["accounts"] == 4  # c/d 一个群都没排到，也要计入
    assert m["zero_pairs"] == 5  # C(4,2)=6 对，只有 (a,b) 非零
    assert m["avg_pair_co"] == pytest.approx(1 / 6, abs=0.01)


def test_verdict_is_safe_when_every_pair_is_within_normal_range():
    assignments = {f"g{i}": ["a", "b"] for i in range(NORMAL_PAIR_CO)}
    assert attendance_metrics(assignments)["verdict"] == VERDICT_SAFE


def test_verdict_warns_once_a_pair_exceeds_normal():
    assignments = {f"g{i}": ["a", "b"] for i in range(NORMAL_PAIR_CO + 1)}
    m = attendance_metrics(assignments)
    assert m["verdict"] == VERDICT_WARN
    assert m["max_pair_co"] == NORMAL_PAIR_CO + 1


def test_verdict_is_danger_at_the_alarm_threshold():
    assignments = {f"g{i}": ["a", "b"] for i in range(ALARM_PAIR_CO)}
    assert attendance_metrics(assignments)["verdict"] == VERDICT_DANGER


def test_a_widespread_mild_overlap_is_danger_even_below_the_alarm_line():
    """整个班底两两轻度超标，比「个别一对重度超标」更致命。

    一对号共享 10 个群只暴露那 2 个号；所有号两两共享 4 个群，暴露的是整个班底——
    聚类算法直接把这批号圈成一个簇。所以判词不能只看 max，必须让团伙率能独立升级。
    """
    over = NORMAL_PAIR_CO + 1
    assignments = {f"g{i}": ["a", "b", "c", "d"] for i in range(over)}
    m = attendance_metrics(assignments)
    assert m["max_pair_co"] == over < ALARM_PAIR_CO
    assert m["clique_ratio"] == 1.0
    assert m["verdict"] == VERDICT_DANGER


def test_a_single_hot_pair_among_many_is_not_escalated_to_danger():
    """反面：只有一对超标、其余干净时，不该被团伙率误升级。"""
    assignments = {f"g{i}": ["a", "b"] for i in range(NORMAL_PAIR_CO + 1)}
    assignments.update({f"h{i}": [f"x{i}", f"y{i}"] for i in range(40)})
    m = attendance_metrics(assignments)
    assert m["hot_pair_count"] == 1
    assert m["clique_ratio"] < 0.05
    assert m["verdict"] == VERDICT_WARN


def test_metrics_never_raise_on_garbage():
    for bad in (None, {}, {"g": None}, {"g": [None, ""]}):
        assert attendance_metrics(bad)["verdict"] == VERDICT_SAFE


# ── 排班质量（量化断言）────────────────────────────────────────────────────


def test_the_headline_claim_twenty_accounts_cover_a_hundred_groups_safely():
    """产品主张的量化钉子：20 个号、每群 3 人，覆盖 100 个群仍在真人正常区间。

    这条断言就是整个模块存在的理由。它一旦红，说明贪心退化或度量口径漂了，
    对外说的「20 个号能安全铺 100 个群」立刻变成假话。
    """
    plan = plan_attendance(_pool(20), _groups(100), seats=3)
    m = plan.metrics
    assert m["max_pair_co"] <= NORMAL_PAIR_CO
    assert m["hot_pair_count"] == 0
    assert m["verdict"] == VERDICT_SAFE
    per = m["groups_per_account"]
    assert max(per.values()) - min(per.values()) <= 2, "每号暴露面要均衡"


def test_putting_every_account_in_every_group_is_flagged_as_danger():
    """最省事也最必封的配置：所有号进所有群。必须硬报，不能只是 warn。"""
    plan = plan_attendance(_pool(10), _groups(100), seats=10)
    assert plan.metrics["max_pair_co"] == 100
    assert plan.metrics["clique_ratio"] == 1.0
    assert plan.metrics["verdict"] == VERDICT_DANGER
    assert any(not p.startswith("warn:") for p in plan.problems)


def test_a_ten_account_pool_cannot_safely_cover_a_hundred_groups():
    """号池不够时不许装作没事——10 个号铺 100 个群，怎么排都超标。"""
    plan = plan_attendance(_pool(10), _groups(100), seats=4)
    assert plan.metrics["verdict"] == VERDICT_DANGER
    assert any("号池" in a for a in plan.advice)


def test_seats_larger_than_pool_is_called_out():
    plan = plan_attendance(_pool(2), _groups(5), seats=4)
    assert any("号池只有" in p for p in plan.problems)


def test_a_one_account_plan_is_never_presented_as_all_clear():
    """判词恒为 safe 的陷阱：一个号自己跟自己没有号对，共同出席确实是 0。

    那不叫安全，那叫这台戏演不成。与 naturalness「独角戏不算自然」同源的语义坑——
    指标算对了但结论会骗人。所以排班另出 ``staffed``，让页面不会只亮一盏绿灯。
    """
    plan = plan_attendance(_pool(1), _groups(20), seats=3)
    assert plan.metrics["max_pair_co"] == 0
    assert plan.metrics["verdict"] == VERDICT_SAFE     # 共同出席口径确实无风险
    assert plan.metrics["staffed"] is False            # 但这张表根本演不成
    assert plan.metrics["understaffed"] == 20
    assert any(not p.startswith("warn:") for p in plan.problems)


def test_a_fully_staffed_plan_reports_staffed():
    plan = plan_attendance(_pool(20), _groups(30), seats=3)
    assert plan.metrics["staffed"] is True
    assert plan.metrics["understaffed"] == 0


# ── 号池测算 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("groups", [20, 50, 100, 200])
def test_recommended_pool_size_actually_delivers_the_target(groups):
    """测算公式必须经得起实跑检验：按它给的号池排班，最坏一对真的达标。

    公式算的是**平均**共同出席，而贪心达成的**最坏值**比平均高约 1，所以实现里按
    ``target-1`` 反解留余量。这条参数化测试就是在钉那个余量选得对不对——余量给少了
    这里会红，给多了则是白白多要号（由下面的紧致性测试兜住）。
    """
    pool_size = recommend_pool_size(groups=groups, seats=3)
    plan = plan_attendance(_pool(pool_size), _groups(groups), seats=3)
    assert plan.metrics["max_pair_co"] <= NORMAL_PAIR_CO
    assert plan.metrics["verdict"] == VERDICT_SAFE


def test_recommended_pool_size_is_not_wastefully_conservative():
    """余量不能给过头：比推荐值少一半的号池应该真的排不安全。"""
    groups = 100
    pool_size = recommend_pool_size(groups=groups, seats=3)
    plan = plan_attendance(_pool(pool_size // 2), _groups(groups), seats=3)
    assert plan.metrics["max_pair_co"] > NORMAL_PAIR_CO


@pytest.mark.parametrize("pool_size", [8, 10, 15, 20, 30])
def test_max_safe_groups_is_the_inverse_and_holds_up(pool_size):
    """「我现在这些号能安全铺几个群」——反解同样要经得起实跑。"""
    limit = max_safe_groups(pool=pool_size, seats=3)
    plan = plan_attendance(_pool(pool_size), _groups(limit), seats=3)
    assert plan.metrics["verdict"] == VERDICT_SAFE


def test_the_pool_times_eight_heuristic_is_gone_for_good():
    """回归：8 个号覆盖 100 群时，建议里的安全群数必须是 18 不是 64。

    早先这里用「号池 × 8」拍脑袋，8 个号会给出 64 个群——而 8 个号每群 3 人铺 64 个
    群，最坏一对共享 8 个群，正好踩在 ALARM 线上。在平方关系上用线性系数，错得离谱，
    而且是**朝着危险方向**错，属于比不给建议更糟的那类 bug。
    """
    plan = plan_attendance(_pool(8), _groups(100), seats=3)
    cut = [a for a in plan.advice if "砍到" in a]
    assert cut, "号池不够时必须给出可执行的缩量建议"
    assert "18 个群" in cut[0]
    assert "64" not in cut[0]

    # 真按 64 个群排，确实是危险区——证明那条旧建议是有害的而非仅仅不精确
    bad = plan_attendance(_pool(8), _groups(64), seats=3)
    assert bad.metrics["max_pair_co"] >= ALARM_PAIR_CO


def test_pool_size_helpers_degrade_quietly_on_nonsense():
    assert recommend_pool_size(groups=0, seats=3) >= 0
    assert recommend_pool_size(groups=10, seats=1) >= 0
    assert max_safe_groups(pool=1, seats=3) == 0
    assert max_safe_groups(pool=10, seats=1) == 0


# ── 增量排班 ────────────────────────────────────────────────────────────────


def test_existing_membership_is_kept_and_only_the_delta_is_planned():
    """已加好的群是既成事实：不动存量，只排增量。

    运营不可能为了重排而退群重加，所以排班器必须能在存量之上做增量——否则这张表
    第二次跑就没法用了。
    """
    existing = {"g000": ["a00", "a01"]}
    plan = plan_attendance(
        _pool(10), _groups(5), seats=3, existing=existing)
    assert set(plan.already["g000"]) == {"a00", "a01"}
    assert plan.assignments["g000"][:2] == ("a00", "a01")
    assert len(plan.joins["g000"]) == 1
    assert plan.total_joins == 5 * 3 - 2


def test_existing_rows_outside_the_pool_or_group_list_are_ignored():
    """存量里混进不在册的号/群不该污染排班（也不该让运营看到不相干的群）。"""
    existing = {"g000": ["ghost", "a00"], "not_planned": ["a01"]}
    plan = plan_attendance(_pool(5), _groups(2), seats=2, existing=existing)
    assert plan.already == {"g000": ("a00",)}
    assert "not_planned" not in plan.assignments


def test_an_overfull_group_advises_silence_rather_than_quitting():
    """存量超编时刻意不建议退群。

    退群动作本身有痕迹，而且消除不了已经产生的共同出席历史——收益远小于代价。
    正确解是让多出来的号在该群不发言，靠后续新群稀释重合度。
    """
    existing = {"g000": ["a00", "a01", "a02", "a03"]}
    plan = plan_attendance(_pool(6), _groups(3), seats=2, existing=existing)
    overfull = [p for p in plan.problems if "存量在场号已超过" in p]
    assert overfull
    assert "不建议退群" in overfull[0]


# ── 历史群（不排但要算） ────────────────────────────────────────────────────


def test_history_groups_count_toward_the_risk_even_though_they_are_not_planned():
    """历史群贡献的共同出席必须进度量，否则这个工具第二次用就开始说谎。

    真实用法是分批铺：这批 10 个群、下批 10 个。只算本批 → 每批都显示「最坏一对
    共享 2 个群，安全」，而这两个号身上可能早就压着 80 个共同的老群了。
    """
    # 两个号已经在 12 个老群里天天见面。
    history = {f"old{i:03d}": ["a00", "a01"] for i in range(12)}
    plan = plan_attendance(_pool(8), _groups(4), seats=2, history=history)

    assert plan.metrics["history_groups"] == 12
    assert plan.metrics["planned_groups"] == 4
    # 度量报的是「总共」共享几个群，不是「这一批」。
    assert plan.metrics["max_pair_co"] >= 12
    assert plan.metrics["verdict"] == "danger"
    # 历史群本身不出现在排班表里——运营这次不管它们。
    assert not any(g.startswith("old") for g in plan.assignments)
    assert not any(g.startswith("old") for g in plan.joins)


def test_history_steers_the_greedy_away_from_already_entangled_pairs():
    """已经在历史里高度重合的两个号，新群里应该被拆开。"""
    history = {f"old{i:03d}": ["a00", "a01"] for i in range(10)}
    plan = plan_attendance(_pool(6), _groups(6), seats=2, history=history)
    together = [g for g, members in plan.assignments.items()
                if {"a00", "a01"} <= set(members)]
    assert not together, f"历史上已经共享 10 个群，新群还把它们排在一起: {together}"


def test_a_group_in_both_the_plan_and_the_history_is_only_counted_once():
    """同一个群同时出现在群清单与历史里时按存量算，不能重复计一遍共同出席。"""
    groups = _groups(3)
    plan = plan_attendance(
        _pool(6), groups, seats=2,
        existing={"g000": ["a00", "a01"]},
        history={"g000": ["a00", "a01"]})
    assert plan.metrics["history_groups"] == 0
    counter = co_attendance(plan.assignments)
    assert counter.get(("a00", "a01"), 0) == 1


def test_history_makes_the_advice_size_the_pool_against_total_coverage():
    """号池够不够要按「历史 + 本次」的总覆盖算。

    只按本次算会给出「20 个号够铺这 20 个新群」，而这些号身上已经压着 80 个老群。
    """
    history = {f"old{i:03d}": [f"a{i % 6:02d}", f"a{(i + 1) % 6:02d}"]
               for i in range(60)}
    plan = plan_attendance(_pool(6), _groups(10), seats=3, history=history)
    sizing = [a for a in plan.advice if "号池从" in a]
    assert sizing, plan.advice
    # 建议的号池要能撑住 70 个群，不是 10 个。
    want = int(sizing[0].split("补到")[1].split("个")[0])
    assert want >= recommend_pool_size(groups=70, seats=3)


def test_history_exposure_counts_toward_the_busy_account_warning():
    """「这个号显不显眼」要按**总敞口**算，不是按这一批排了几个群。

    只看本批的话，一个已经挂在 32 个老群里的号、这批只派 2 个群，会显示「最忙的号
    进 2 个群」——一条让人放心的假话。而它恰恰是最该先歇一歇的那个号。
    """
    history = {f"old{i:03d}": ["a00"] for i in range(32)}   # 独号群：只压敞口不压号对
    plan = plan_attendance(_pool(6), _groups(5), seats=2, history=history)

    assert plan.metrics["total_groups_per_account"]["a00"] >= 32
    assert plan.metrics["groups_per_account"]["a00"] <= 2, "本批本来就没派它多少"
    assert any("加群本身要分摊到多天" in p for p in plan.problems)


def test_an_account_buried_in_history_groups_is_not_handed_more_work():
    """负载均衡同样要按总敞口算，否则每批新群都从零起算。

    一个号挂了 20 个老群、在这张新表里却显示 0，贪心就会把它当「最闲的」优先派活，
    一批一批地喂到爆——而它在共同出席上还是干净的（老群里都是它自己），压不住。
    """
    history = {f"old{i:03d}": ["a00"] for i in range(20)}
    plan = plan_attendance(_pool(4), _groups(3), seats=2, history=history)
    assert not any("a00" in members for members in plan.assignments.values()), \
        "已经挂了 20 个老群的号又被派了新活"


# ── 开演前的出席体检 ────────────────────────────────────────────────────────


def _ledger(pairs: int, *members: str):
    return {f"old{i:03d}": list(members) for i in range(pairs)}


def test_a_clean_cast_passes_the_pre_show_check():
    report = cast_entanglement(["a00", "a01"], _ledger(2, "a00", "a01"))
    assert report["verdict"] == VERDICT_SAFE
    assert not report["problems"]


def test_a_cast_that_already_co_attends_everywhere_is_called_out():
    """排班表是规划工具，开演时没人强制看它。

    不在开演前查一次，就会出现「矩阵建议 A/B 分开，实际却让 A/B 在第 40 个群里
    又一起演一台」——护栏存在但没接线，等于没有。
    """
    report = cast_entanglement(["a00", "a01", "a02"],
                               _ledger(9, "a00", "a01", "a02"))
    assert report["verdict"] == VERDICT_DANGER
    assert report["max_pair_co"] == 9
    assert report["problems"] and "同框" in report["problems"][0]


def test_the_current_group_is_excluded_from_its_own_evidence():
    """「在这个群同框」是本场的前提，不是历史证据——算进去等于自己吓自己。"""
    ledger = {"here": ["a00", "a01"], "old": ["a00", "a01"]}
    report = cast_entanglement(["a00", "a01"], ledger, group_key="here")
    assert report["max_pair_co"] == 1


def test_the_check_only_looks_at_accounts_actually_on_stage():
    """台账里别的号再怎么重合，也不该算到本场演员头上。"""
    ledger = _ledger(9, "b00", "b01")
    assert cast_entanglement(["a00", "a01"], ledger)["verdict"] == VERDICT_SAFE


def test_a_solo_cast_has_nothing_to_check():
    for cast in ([], ["a00"], None):
        assert cast_entanglement(cast, _ledger(9, "a00", "a01"))["pairs"] == []


def test_the_check_reads_cast_member_objects_and_dicts_alike():
    """调用方手上是 ``CastMember``，测试里常用 dict——两种都得认。"""
    from src.companion.group_show.playbook import CastMember

    ledger = _ledger(9, "a00", "a01")
    members = (CastMember(slot="advocate", account_id="a00", persona_id="p0"),
               CastMember(slot="skeptic", account_id="a01", persona_id="p1"))
    assert cast_entanglement(members, ledger)["verdict"] == VERDICT_DANGER
    dicts = [{"account_id": "a00"}, {"account_id": "a01"}]
    assert cast_entanglement(dicts, ledger)["verdict"] == VERDICT_DANGER


@pytest.mark.parametrize("bad", ["nope", 7, {"g": "not-a-list"}, None])
def test_the_pre_show_check_never_blocks_a_show_on_bad_input(bad):
    """体检挂了不该拦住整场戏——它是信息，不是闸门。"""
    report = cast_entanglement(["a00", "a01"], bad)
    assert isinstance(report.get("problems"), list)
    assert all(not p or p.startswith("warn:") or "同框" in p
               for p in report["problems"])


def test_the_text_report_never_shows_a_bare_green_verdict_on_an_unrunnable_plan():
    """CLI 文本档与页面必须同口径。

    号池凑不满席位时判词恒为 safe（没有号对，共同出席确实是 0）。页面上已经用
    「号池不足」的红条盖掉了绿灯，文本档漏掉同样会让人以为可以照着干。
    """
    text = format_plan(plan_attendance(_pool(1), _groups(8), seats=3))
    assert "safe" in text
    assert "执行不了" in text, "文本档把「演不成」显示成了绿灯"


def test_split_ledger_is_the_single_source_of_the_existing_vs_history_rule():
    """台账拆分规则只能有一处实现（导播台与 CLI 共用）。

    各写一遍就会漂，而漂的后果是两边给出不同的表，运营会以为其中一份算错了。
    """
    ledger = {"g000": ["a00"], "old1": ["a01"], "": ["a02"], "old2": []}
    existing, history = split_ledger(ledger, _groups(2))
    assert existing == {"g000": ["a00"]}
    assert history == {"old1": ["a01"], "old2": []}
    assert "" not in existing and "" not in history


def test_split_ledger_survives_junk_on_either_side():
    """两个入参都可能是脏的（库读出来的、body 里来的）。"""
    assert split_ledger(None, None) == ({}, {})
    assert split_ledger("nope", _groups(1)) == ({}, {})
    existing, history = split_ledger({"g000": ["a00"]}, "not-a-list")
    assert existing == {} and history == {"g000": ["a00"]}


@pytest.mark.parametrize("junk", [
    {"": ["a00"]}, {"old": None}, {"old": ["ghost"]}, {None: ["a00"]},
    "not-a-dict", ["old", "a00"], 7,
])
def test_a_junk_history_degrades_instead_of_voiding_the_whole_plan(junk):
    """历史表来自库/前端 body，形状没保证。

    它是**辅助**信息：传错时降级成「不知道存量」继续排，比让主输入完好的那张表
    整个作废好得多——后者会让运营以为号池或群清单有问题，查错方向都反了。
    """
    plan = plan_attendance(_pool(4), _groups(2), seats=2, history=junk)
    assert plan.assignments, f"history={junk!r} 把排班弄空了"


@pytest.mark.parametrize("junk", ["not-a-dict", ["g000", "a00"], 7])
def test_a_junk_existing_map_degrades_the_same_way(junk):
    """``existing`` 与 ``history`` 同源同风险，兜底口径必须一致。"""
    plan = plan_attendance(_pool(4), _groups(2), seats=2, existing=junk)
    assert plan.assignments, f"existing={junk!r} 把排班弄空了"


# ── 确定性 ──────────────────────────────────────────────────────────────────


def test_the_same_input_always_yields_the_same_plan():
    """运营要照这张表分几天手工加群，重跑时不能换一套。

    实现刻意用 crc32 而非内置 ``hash()``——后者对 str 每进程加盐，换个进程就换一套
    排班，这份表就成了废纸。
    """
    a = plan_attendance(_pool(12), _groups(30), seats=3)
    b = plan_attendance(_pool(12), _groups(30), seats=3)
    assert a.assignments == b.assignments
    assert a.joins == b.joins


def test_a_different_seed_yields_a_different_plan():
    a = plan_attendance(_pool(12), _groups(30), seats=3, seed="one")
    b = plan_attendance(_pool(12), _groups(30), seats=3, seed="two")
    assert a.assignments != b.assignments


# ── 指纹组次级优化 ──────────────────────────────────────────────────────────


def test_accounts_sharing_an_exit_are_kept_out_of_the_same_group_when_possible():
    """同出口的号尽量别派进同一个群——这是 linkage 那条轴在矩阵层的次级优化。"""
    pool = ["p1", "p2", "h1", "h2"]
    fp = {"p1": "proxy:1", "p2": "proxy:2", "h1": "host:x", "h2": "host:x"}
    # 4 个号共 6 种配对，其中 5 种不同出口——群数不超过 5 时完全可以绕开 (h1,h2)
    plan = plan_attendance(pool, _groups(4), seats=2, fingerprint_groups=fp)
    both_host = [
        gid for gid, members in plan.assignments.items()
        if {"h1", "h2"} <= set(members)]
    assert not both_host, "有别的配对可选时，不该把两个同宿主号凑一组"


def test_co_attendance_outranks_exit_diversity_when_the_two_conflict():
    """两条轴冲突时，共同出席赢——这是模块的核心排序，值得单独钉住。

    4 个号只有 6 种配对，要铺 6 个群就必须把每种配对都用一次，包括那对同宿主的。
    此时若坚持回避同出口，就得让某个配对重复出现，反而推高共同出席——拿主要风险
    去换次要风险，是亏的。所以这里**应该**出现 (h1,h2) 同组，而不是把它当 bug 修掉。
    """
    pool = ["p1", "p2", "h1", "h2"]
    fp = {"p1": "proxy:1", "p2": "proxy:2", "h1": "host:x", "h2": "host:x"}
    plan = plan_attendance(pool, _groups(6), seats=2, fingerprint_groups=fp)
    assert plan.metrics["max_pair_co"] == 1, "每种配对恰好用一次＝共同出席的最优解"
    assert any({"h1", "h2"} <= set(m) for m in plan.assignments.values())


def test_a_uniform_fingerprint_table_does_not_break_planning():
    """全号池同一个出口（本仓当下的真实状态）时，指纹项人人相同，排班照常。"""
    pool = _pool(6)
    fp = {a: "host:117" for a in pool}
    plan = plan_attendance(pool, _groups(10), seats=3, fingerprint_groups=fp)
    assert all(len(v) == 3 for v in plan.assignments.values())


# ── 单号进群上限 ────────────────────────────────────────────────────────────


def test_the_per_account_cap_is_respected_even_if_groups_go_unfilled():
    """上限是硬的：宁可留下排不满的群，也不让某个号无限加群。"""
    plan = plan_attendance(
        _pool(4), _groups(20), seats=3, max_groups_per_account=5)
    per = plan.metrics["groups_per_account"]
    assert max(per.values()) <= 5
    assert any("排不满" in p for p in plan.problems)


def test_a_very_busy_account_gets_called_out():
    """号对重合度达标 ≠ 单个号安全：挂在几十个群里的号本身就显眼。"""
    plan = plan_attendance(_pool(40), _groups(600), seats=3)  # 均摊 45 个群/号
    assert max(plan.metrics["groups_per_account"].values()) > 30
    assert any("加群本身要分摊到多天" in p for p in plan.problems)


# ── 加群排期 ────────────────────────────────────────────────────────────────


def test_no_two_of_our_accounts_join_the_same_group_on_the_same_day():
    """整个排期功能存在的主要理由。

    一个群里前后脚进来三个陌生人、随后这三个人开始一唱一和——群主不需要任何技术手段
    就能看出来。这是排班矩阵覆盖不到的**时间轴**，而运营拿到静态表最自然的动作恰恰是
    「今天全加完」。
    """
    plan = plan_attendance(_pool(12), _groups(20), seats=3)
    sched = schedule_joins(plan)
    for day in sched["days"]:
        groups_today = [t["group"] for t in day]
        assert len(groups_today) == len(set(groups_today)), "同一天同一个群进了两个号"


def test_an_account_never_exceeds_its_daily_join_quota():
    plan = plan_attendance(_pool(6), _groups(30), seats=3)
    sched = schedule_joins(plan, per_account_per_day=2)
    for day in sched["days"]:
        per: dict = {}
        for task in day:
            per[task["account"]] = per.get(task["account"], 0) + 1
        assert max(per.values()) <= 2


def test_every_planned_join_lands_in_the_schedule_exactly_once():
    """排期不能漏任务，也不能重复派——它是运营的执行清单，错一条就是漏加/重复加。"""
    plan = plan_attendance(_pool(10), _groups(25), seats=3)
    sched = schedule_joins(plan)
    scheduled = [(t["group"], t["account"])
                 for day in sched["days"] for t in day]
    planned = [(g, a) for g, members in plan.joins.items() for a in members]
    assert sorted(scheduled) == sorted(planned)


def test_three_seats_take_at_least_three_days_per_group():
    """每群每天只进一个号 ⇒ 3 个座位天然摊到 3 天以上，这是好事不是缺陷。"""
    plan = plan_attendance(_pool(20), _groups(5), seats=3)
    assert schedule_joins(plan, per_account_per_day=99)["day_count"] >= 3


def test_the_schedule_is_deterministic():
    """运营照着执行了一半，第二天重开页面不能换一套。"""
    plan = plan_attendance(_pool(10), _groups(20), seats=3)
    assert schedule_joins(plan)["days"] == schedule_joins(plan)["days"]


def test_schedule_joins_never_raises():
    assert schedule_joins(None)["day_count"] == 0
    assert schedule_joins(plan_attendance(None, None, seats=3))["day_count"] == 0
    empty = plan_attendance(_pool(3), _groups(2), seats=2,
                            existing={"g000": ["a00", "a01"],
                                      "g001": ["a01", "a02"]})
    assert schedule_joins(empty)["day_count"] == 0   # 全是存量，没有新任务


# ── 跨群角色轮换 ────────────────────────────────────────────────────────────


def test_roles_rotate_so_nobody_is_always_the_shill():
    """出席矩阵管不到内容指纹：A 若在每个群里都是夸产品那个，单群观察者就能识破。"""
    plan = plan_attendance(_pool(20), _groups(100), seats=3)
    r = rotate_roles(plan.assignments, ("advocate", "skeptic", "bystander"))
    assert r["max_role_share"] <= 0.5
    assert not [p for p in r["problems"] if not p.startswith("warn:")]


def test_one_account_never_holds_two_roles_in_the_same_group():
    plan = plan_attendance(_pool(8), _groups(10), seats=3)
    r = rotate_roles(plan.assignments, ("advocate", "skeptic", "bystander"))
    for gid, per_group in r["casting"].items():
        assert len(set(per_group.values())) == len(per_group), gid


def test_fewer_seats_than_slots_leaves_slots_empty_rather_than_reusing_accounts():
    """在场人数少于槽位＝降级演，不是错误；但绝不能靠一号两角凑满。"""
    plan = plan_attendance(_pool(4), _groups(3), seats=2)
    r = rotate_roles(plan.assignments, ("advocate", "skeptic", "bystander"))
    for per_group in r["casting"].values():
        assert len(per_group) == 2
        assert len(set(per_group.values())) == 2


def test_a_single_slot_is_not_reported_as_role_imbalance():
    """只有一个槽时占比恒为 100%，那是没得轮换，不是失衡——不该报警。"""
    plan = plan_attendance(_pool(6), _groups(10), seats=1)
    r = rotate_roles(plan.assignments, ("bystander",))
    assert r["max_role_share"] == 1.0
    assert r["problems"] == []


def test_rotate_roles_never_raises():
    for bad in (None, {}, {"g": None}, {"g": [None]}):
        assert isinstance(rotate_roles(bad, ("advocate",))["casting"], dict)
    assert rotate_roles({"g": ["a"]}, ())["problems"]


# ── 软失败契约 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_pool", [None, 42, object(), 3.5])
def test_an_unusable_pool_degrades_to_an_empty_plan(bad_pool):
    """本包对外承诺不抛：号池是外部数据，什么形状都可能来。"""
    plan = plan_attendance(bad_pool, _groups(3), seats=2)
    assert plan.assignments == {}
    assert plan.problems


def test_a_bare_string_pool_is_one_account_not_a_pile_of_characters():
    """回归：裸字符串是可遍历的，直接 iter 会把 "tg1" 拆成 t/g/1 三个「账号」。

    那是静默且荒谬的破口——排班表看起来完全正常，实际上在给三个不存在的号排班。
    传单个 id 的意图很明确，按单元素处理即可；号池只有 1 个的后果由问题清单如实说。
    """
    plan = plan_attendance("tg1", _groups(2), seats=2)
    assert plan.metrics["groups_per_account"] == {"tg1": 2}
    assert any("号池只有 1 个" in p for p in plan.problems)


def test_an_unusable_group_list_says_so_plainly():
    plan = plan_attendance(_pool(3), 42, seats=2)
    assert any("群清单为空" in p for p in plan.problems)


def test_zero_seats_is_rejected_with_a_readable_reason():
    assert any("大于 0" in p for p in plan_attendance(
        _pool(3), _groups(3), seats=0).problems)


def test_registry_shaped_rows_are_accepted_directly():
    """能直接喂注册表行，省掉调用方一层手工洗数据（洗数据的地方最容易漏字段）。"""
    rows = [{"account_id": "tg1", "platform": "telegram"},
            {"account_id": "tg2", "platform": "telegram"}]
    groups = [{"group_id": "g1"}, {"chat_key": "g2"}]
    plan = plan_attendance(rows, groups, seats=2)
    assert set(plan.assignments) == {"g1", "g2"}
    assert set(plan.assignments["g1"]) == {"tg1", "tg2"}


def test_the_production_registry_shape_plans_without_crashing():
    """回归：本仓 2026-07 真实注册表形状（8 个号、proxy/fingerprint 全空）。"""
    rows = [
        {"account_id": "8244899900", "platform": "telegram", "proxy_id": ""},
        {"account_id": "8755679833", "platform": "telegram", "proxy_id": ""},
        {"account_id": "6834964252", "platform": "telegram", "proxy_id": ""},
        {"account_id": "639270135480", "platform": "whatsapp", "proxy_id": ""},
        {"account_id": "639273815533", "platform": "whatsapp", "proxy_id": ""},
        {"account_id": "639649321471", "platform": "whatsapp", "proxy_id": ""},
        {"account_id": "8127518232", "platform": "telegram", "proxy_id": ""},
        {"account_id": "line_u1", "platform": "line", "proxy_id": ""},
    ]
    plan = plan_attendance(rows, _groups(18), seats=3)
    assert plan.metrics["verdict"] == VERDICT_SAFE
    assert plan.total_joins == 18 * 3


def test_format_plan_survives_an_empty_plan():
    assert format_plan(plan_attendance(None, None, seats=3))
