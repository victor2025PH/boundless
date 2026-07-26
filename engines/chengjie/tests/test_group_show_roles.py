"""角色集中度门禁 —— 成员轴、发言轴之外第三条暴露面：单号自曝。

前两条轴都是**号对**口径（谁和谁是一伙的）。这一条守的是另一件事：一个号自己在 40 个群
里都是那个「强烈推荐」的人，群主翻一下它的历史发言就完了，根本不需要找它的同伙。

这一层守的不变量分三类，每一类漏了都是真事故：

1. **只管推销角色**。宽口径会把 bystander／asker 这些**掩护**角色一起拦掉 → 号不够用 →
   运营把整条护栏关掉 → 连主推都不拦了。这是本文件最重要的一组用例。
2. **主指标是最坏那一个**。九个号干净、一个号在 40 个群主推，平均值只有 4.9 看着「略高」
   ——按平均值报警等于放行一个已经彻底暴露的号。
3. **闸门不许把戏卡死**。无台账、空台账、脏台账一律放行；无数据不该变成无演出，
   会卡死的护栏一定会被关掉。
"""
from __future__ import annotations

from src.companion.group_show.attendance import (
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
)
from src.companion.group_show.roles import (
    CONCENTRATION_WARN,
    FLEET_MIN_ACCOUNTS,
    NORMAL_ROLE_CO,
    SELLING_SLOTS,
    admits_role,
    is_selling_slot,
    role_headroom,
    role_metrics,
    role_report,
)


def _pool(n: int):
    return [f"a{i:02d}" for i in range(n)]


class _Boom:
    """``__str__`` 会炸的对象——台账里混进这种东西不该让整张卡白屏。"""

    def __str__(self) -> str:
        raise RuntimeError("boom")

    def __repr__(self) -> str:  # 保留可读 repr，断言失败时还能看清是什么炸了
        return "<_Boom>"


# ── 掩护角色不是把柄（本文件最重要的一组） ──────────────────────────────────


def test_cover_roles_are_never_gated_no_matter_how_many_groups():
    """路人／提问／质疑铺再多群都不该被拦——真人就是这样，这些角色零信号。

    拦掉掩护角色的后果不是「更安全」：掩护角色先耗尽 → 每场只剩主推能上 → 风险拉满；
    在那之前运营早就把整条护栏关掉了。
    """
    counts = {
        ("a01", "bystander"): 999,
        ("a01", "asker"): 50,
        ("a01", "skeptic"): 30,
    }
    for slot in ("bystander", "asker", "skeptic"):
        assert admits_role("a01", slot, counts) is True
        assert role_headroom("a01", slot, counts) == NORMAL_ROLE_CO


def test_cover_roles_do_not_show_up_in_the_headline_number():
    """掩护角色也不该推高 ``max_role_co``：看板上一个 999 会让运营以为这个号已经废了。"""
    counts = {("a01", "bystander"): 999, ("a02", "asker"): 400}
    metrics = role_metrics(counts)
    assert metrics["max_role_co"] == 0
    assert metrics["worst"] is None
    assert metrics["verdict"] == VERDICT_SAFE
    assert metrics["by_account"] == {"a01": 0, "a02": 0}


def test_cover_and_selling_roles_keep_separate_budgets():
    """同一个号在 40 个群当路人，不该吃掉它演主推的余量（反过来也一样）。"""
    counts = {("a01", "bystander"): 40, ("a01", "advocate"): 1}
    assert role_headroom("a01", "advocate", counts) == NORMAL_ROLE_CO - 1
    assert role_headroom("a01", "bystander", counts) == NORMAL_ROLE_CO
    assert role_metrics(counts)["max_role_co"] == 1


def test_cover_only_accounts_still_count_in_the_denominator():
    """只演过掩护角色的号是**在编演员**：不进分子，但必须进分母。

    漏掉它们会让「超标号占比」随着干净的号变多而**上升**，指标方向就反了。
    """
    metrics = role_metrics({("a01", "advocate"): 2, ("a02", "bystander"): 50})
    assert metrics["by_account"] == {"a01": 2, "a02": 0}
    assert metrics["accounts"] == 2


def test_selling_slot_check_is_case_insensitive():
    """槽名是人手写的 YAML，``Advocate`` 差一个大写就整条闸门静默失效——最坏的失效方式。"""
    assert is_selling_slot("Advocate") is True
    assert is_selling_slot("ADVOCATE") is True
    assert is_selling_slot("bystander") is False
    saturated = {("a01", "advocate"): NORMAL_ROLE_CO}
    assert admits_role("a01", "Advocate", saturated) is False
    assert role_metrics({("a01", "Advocate"): 5})["max_role_co"] == 5


# ── 闸门：到线才拦 ──────────────────────────────────────────────────────────


def test_headroom_counts_down_from_the_normal_line():
    counts = {("a01", "advocate"): 2}
    assert role_headroom("a01", "advocate", counts) == NORMAL_ROLE_CO - 2
    assert role_headroom("a01", "advocate", counts, limit=1) == 0     # 已越线
    assert role_headroom("a09", "advocate", counts) == NORMAL_ROLE_CO  # 没演过＝满余量


def test_the_gate_blocks_at_the_line_and_not_before():
    counts = {("a01", "advocate"): NORMAL_ROLE_CO - 1}
    assert admits_role("a01", "advocate", counts) is True

    counts["a01", "advocate"] = NORMAL_ROLE_CO
    assert admits_role("a01", "advocate", counts) is False
    assert role_headroom("a01", "advocate", counts) == 0
    assert admits_role("a02", "advocate", counts) is True   # 别人不受牵连


def test_a_zero_or_negative_limit_turns_the_gate_off():
    counts = {("a01", "advocate"): 99}
    assert admits_role("a01", "advocate", counts, limit=0) is True
    assert admits_role("a01", "advocate", counts, limit=-3) is True
    assert role_headroom("a01", "advocate", counts, limit=0) == 0


def test_an_empty_or_missing_ledger_never_blocks_anybody():
    """无数据不该变成无演出：护栏刚上线、台账还没攒出来时必须与没有护栏一字不差。"""
    assert admits_role("a01", "advocate", {}) is True
    assert admits_role("a01", "advocate", None) is True
    assert role_headroom("a01", "advocate", {}) == NORMAL_ROLE_CO
    assert role_headroom("a01", "advocate", None) == NORMAL_ROLE_CO


# ── 主指标是最坏那一个 ──────────────────────────────────────────────────────


def test_the_headline_number_is_the_worst_account_not_the_average():
    """九个号干净、一个号在 40 个群主推——平均值 4.9 看着「略高」，实际这副牌已经废了。

    按平均值定档等于放行一个彻底暴露的号，这是本模块最不能出的错。
    """
    counts = {(f"a{i:02d}", "advocate"): 1 for i in range(9)}
    counts[("a09", "advocate")] = 40

    metrics = role_metrics(counts)
    assert metrics["max_role_co"] == 40
    assert metrics["worst"] == ["a09", "advocate"]
    assert metrics["avg_role_co"] < 5           # 均值毫无警示性
    assert metrics["verdict"] == VERDICT_DANGER
    assert metrics["top_roles"][0] == ["a09", "advocate", 40]


def test_one_account_slightly_over_the_line_is_a_warning_not_a_disaster():
    metrics = role_metrics({("a01", "advocate"): NORMAL_ROLE_CO + 1},
                           pool=_pool(10))
    assert metrics["verdict"] == VERDICT_WARN
    assert metrics["hot_account_count"] == 1


def test_a_whole_fleet_selling_escalates_even_below_the_alarm_line():
    """半个班底都在超量主推 ⇒ 扎眼的不再是某个号，而是「这是一支带货队伍」。

    对聚类算法来说后者致命得多，所以它能独立把判词升到 danger，不必等最坏那个号爆表。
    """
    metrics = role_metrics({(f"a{i:02d}", "advocate"): 5 for i in range(6)})
    assert metrics["max_role_co"] < 8            # 没到危险线
    assert metrics["fleet_ratio"] == 1.0
    assert metrics["verdict"] == VERDICT_DANGER


def test_two_accounts_are_too_few_to_call_it_a_fleet():
    """号少于三个时「100% 的号都超标」是平凡真、不含信息——那就是某个号的问题。"""
    metrics = role_metrics({("a01", "advocate"): 5, ("a02", "advocate"): 5})
    assert metrics["accounts"] < FLEET_MIN_ACCOUNTS
    assert metrics["verdict"] == VERDICT_WARN


def test_everything_inside_the_normal_line_is_safe():
    metrics = role_metrics({(f"a{i:02d}", "advocate"): NORMAL_ROLE_CO
                            for i in range(5)})
    assert metrics["verdict"] == VERDICT_SAFE
    assert metrics["hot_account_count"] == 0


# ── 号池进分母 ──────────────────────────────────────────────────────────────


def test_pool_puts_accounts_that_never_played_into_the_denominator():
    """一个主推都没演过的号也是可调度的演员——不计入分母，超标率就是假的。"""
    counts = {("a01", "advocate"): 4}
    bare = role_metrics(counts)
    assert bare["accounts"] == 1 and bare["fleet_ratio"] == 1.0

    wide = role_metrics(counts, pool=_pool(10))
    assert wide["accounts"] == 10
    assert wide["by_account"]["a05"] == 0
    assert wide["fleet_ratio"] == 0.1
    assert wide["capacity"] == 10 * NORMAL_ROLE_CO


def test_a_bare_string_pool_is_one_account_not_three_characters():
    """``pool="a01"`` 直接 ``iter`` 会拆成三个「账号」，分母凭空多两个号、集中度被稀释。"""
    assert role_metrics({}, pool="a01")["accounts"] == 1


# ── 容量：这条轴买不到杠杆 ──────────────────────────────────────────────────


def test_selling_capacity_is_linear_in_the_pool_and_the_advice_says_so():
    """压缩每场开口人数能把号对轴的容量翻三倍，但每场照样只有一个主推。

    所以主推容量 ＝ ``号数 × 3``，线性，只能靠加号——建议里必须把这个数字算给运营看，
    否则他会以为「再少让一个号开口」也能解决这条轴。
    """
    counts = {(f"a{i:02d}", "advocate"): 4 for i in range(10)}
    metrics = role_metrics(counts)
    assert metrics["capacity"] == 30 and metrics["used"] == 40
    assert metrics["saturation"] > 1.0

    advice = role_report(counts)["advice"]
    assert any("14" in line for line in advice), advice   # ceil(40/3) = 14 个号


# ── 占比：3/3 比 3/50 可疑得多 ──────────────────────────────────────────────


def test_three_of_three_groups_is_worse_than_three_of_fifty():
    """绝对数相同，占比不同：只在 3 个群说过话且全在主推 ＝ 这个号从没干过别的。"""
    counts = {("a01", "advocate"): 3}
    tight = role_report(counts, speaking_groups={"a01": 3})
    wide = role_report(counts, speaking_groups={"a01": 50})

    assert tight["concentration"]["a01"] == 1.0
    assert wide["concentration"]["a01"] < CONCENTRATION_WARN
    assert tight["worst_concentration"] == ["a01", 1.0]
    assert wide["worst_concentration"][1] < CONCENTRATION_WARN

    # 两边绝对数都在常人区间，判词一样；能把它们区分开的只有占比这条建议
    assert tight["verdict"] == wide["verdict"] == VERDICT_SAFE
    assert any("100%" in line for line in tight["advice"]), tight["advice"]
    assert not any("100%" in line for line in wide["advice"]), wide["advice"]


def test_concentration_is_omitted_when_the_caller_did_not_supply_the_denominator():
    """没给发言台账就别猜分母——猜错会把一个干净的号报成全职托。"""
    report = role_report({("a01", "advocate"): 3})
    assert "concentration" not in report
    assert "worst_concentration" not in report
    assert "concentration" not in role_report({}, speaking_groups="nonsense")


def test_a_single_selling_group_does_not_trigger_the_concentration_advice():
    """1/1 恒等于 100%，拿它报警只会刷屏，运营很快就不看这张卡了。"""
    report = role_report({("a01", "advocate"): 1}, speaking_groups={"a01": 1})
    assert report["concentration"]["a01"] == 1.0
    assert report["worst_concentration"] is None
    assert not any("100%" in line for line in report["advice"])


# ── 建议要能直接执行 ────────────────────────────────────────────────────────


def test_advice_names_a_specific_account_to_hand_the_role_over_to():
    """「建议注意风险」不是建议。运营要的是「把 a01 的主推让给 aXX」。"""
    counts = {("a01", "advocate"): 9}
    report = role_report(counts, pool=_pool(5))
    assert report["verdict"] == VERDICT_DANGER

    handoff = report["handoffs"][0]
    assert handoff["account"] == "a01" and handoff["slot"] == "advocate"
    assert handoff["count"] == 9
    assert handoff["to"] in {"a00", "a02", "a03", "a04"}
    assert handoff["to_headroom"] == NORMAL_ROLE_CO
    assert any("a01" in line and handoff["to"] in line
               for line in report["advice"]), report["advice"]


def test_each_overloaded_account_gets_a_different_stand_in():
    """把五个超标的主推全塞给同一个闲号，只是把问题挪个地方。"""
    counts = {("a01", "advocate"): 9, ("a02", "advocate"): 8}
    report = role_report(counts, pool=_pool(6))
    stand_ins = [h["to"] for h in report["handoffs"]]
    assert len(stand_ins) == 2
    assert all(stand_ins) and len(set(stand_ins)) == 2


def test_when_nobody_can_take_over_the_advice_says_add_accounts():
    """挑不出接手人时必须如实说挑不出来——那是「该补号了」的硬信号，不是沉默的时候。"""
    counts = {(f"a{i:02d}", "advocate"): 9 for i in range(3)}
    report = role_report(counts)
    assert report["handoffs"][0]["to"] == ""
    assert any("补号" in line for line in report["advice"]), report["advice"]


def test_a_clean_ledger_still_reports_how_much_room_is_left():
    """全绿也要给读数：卡片上一句「还能再排几场」比一个绿灯有用得多。"""
    report = role_report({("a01", "advocate"): 2}, pool=_pool(4))
    assert report["verdict"] == VERDICT_SAFE
    assert report["handoffs"] == []
    assert report["advice"]


def test_an_axis_with_zero_selling_history_says_so_explicitly():
    report = role_report({("a01", "bystander"): 20}, pool=_pool(4))
    assert report["roles"]["used"] == 0
    assert report["advice"]


# ── 形状契约：接线方照着这些字段写 ──────────────────────────────────────────


def test_metrics_shape_is_stable_even_with_no_data():
    """空结果也必须形状完整：卡片拿不到数可以显示 0，但不该白屏。"""
    assert set(role_metrics(None)) == {
        "accounts", "max_role_co", "worst", "avg_role_co", "by_account",
        "hot_account_count", "fleet_ratio", "used", "capacity", "saturation",
        "verdict", "top_roles", "selling_slots",
    }
    assert role_metrics(None)["selling_slots"] == list(SELLING_SLOTS)


def test_report_shape_is_stable_even_with_no_data():
    assert set(role_report(None)) == {"roles", "verdict", "handoffs", "advice"}
    assert set(role_report(None, speaking_groups={})) == {
        "roles", "verdict", "handoffs", "advice",
        "concentration", "worst_concentration",
    }


# ── 脏输入一律不抛 ──────────────────────────────────────────────────────────


def test_metrics_survive_every_shape_of_garbage():
    """这是旁路度量，挂了不该拖垮整条编排链——脏输入一律软降级成空报告。"""
    for bad in (None, {}, "not-a-mapping", [], 7, object(),
                {"a": "b"}, {("a",): 3}, {("a", "b", "c"): 3},
                {("a01", "advocate"): "x"}, {("a01", "advocate"): None},
                {("a01", "advocate"): -5}, {_Boom(): 3},
                {(_Boom(), "advocate"): 3}, {("a01", _Boom()): 3}):
        metrics = role_metrics(bad)
        assert metrics["max_role_co"] == 0, bad
        assert metrics["worst"] is None, bad
        assert metrics["verdict"] == VERDICT_SAFE, bad


def test_a_two_letter_string_key_is_not_parsed_as_an_account_and_a_slot():
    """``tuple("ab")`` ＝ ``("a", "b")``：字符串键会被静默解析成「号 a 演过角色 b」，
    混进度量之后再也认不出来。所以键必须是真的二元组。"""
    assert role_metrics({"ab": 3})["accounts"] == 0
    assert role_metrics({"a01advocate": 3})["accounts"] == 0


def test_the_gate_survives_every_shape_of_garbage():
    """算不出来一律放行：护栏不该反过来把正常演出卡死。"""
    assert role_headroom(None, None, None) == NORMAL_ROLE_CO
    assert role_headroom("a01", "advocate", "not-a-mapping") == NORMAL_ROLE_CO
    assert role_headroom(_Boom(), _Boom(), {}) == NORMAL_ROLE_CO
    # 负数/非数值的群数是上游 bug，按 0 记（绝不反过来给这个号「加余量」）
    for junk in (-5, "x", None, _Boom()):
        counts = {("a01", "advocate"): junk}
        assert role_headroom("a01", "advocate", counts) == NORMAL_ROLE_CO
    assert admits_role(None, None, None) is True
    assert admits_role("a01", "advocate", "not-a-mapping") is True
    assert admits_role(_Boom(), "advocate", {("a01", "advocate"): 9}) is True
    assert admits_role("a01", "", {("a01", "advocate"): 9}) is True


def test_report_survives_every_shape_of_garbage():
    report = role_report(None, speaking_groups="nonsense", pool=_Boom())
    assert report["verdict"] == VERDICT_SAFE
    assert report["roles"]["accounts"] == 0
    assert report["advice"]
    assert "concentration" not in report

    dirty = role_report({("a01", "advocate"): 9},
                        speaking_groups={_Boom(): 5, "a01": "x"},
                        pool=[_Boom(), "a02"])
    assert dirty["verdict"] == VERDICT_DANGER
    assert dirty["concentration"]["a01"] == 1.0   # 分母查不到 → 按最坏记
    assert dirty["advice"]


# ── 确定性 ──────────────────────────────────────────────────────────────────


def test_results_are_deterministic_across_processes_and_input_order():
    """打平局不能用内置 ``hash()``——它对 str 每进程加盐，换个进程运营就换一套换人方案。"""
    pairs = [((f"a{i:02d}", "advocate"), i % 7) for i in range(12)]
    forward = dict(pairs)
    backward = dict(reversed(pairs))

    speaking = {f"a{i:02d}": 10 for i in range(12)}
    first = role_report(forward, speaking_groups=speaking, pool=_pool(12))
    assert first == role_report(forward, speaking_groups=speaking, pool=_pool(12))
    assert first == role_report(backward, speaking_groups=speaking, pool=_pool(12))


# ── 接进选角：闸门只有真挂在选角上才算数 ────────────────────────────────────


def _book():
    from src.companion.group_show.playbook import Beat, Playbook, Role

    return Playbook(
        id="pb", name="剧本", system="growth",
        roles=(Role("advocate"), Role("asker"), Role("bystander")),
        beats=(Beat(id="b1", role="advocate", intent="种草"),),
    )


def _cands(n: int):
    return [{"account_id": f"a{i:02d}", "platform": "telegram",
             "persona_id": f"p{i:02d}", "display_name": f"号{i}",
             "health": "online", "fingerprint_group": f"fp{i:02d}"}
            for i in range(n)]


def test_casting_hands_the_selling_slot_to_a_fresher_account():
    """a00 已经在 3 个群主推到线 → 主推换人，但它仍可以上台演掩护角色。

    「换个人演主推」而不是「把这个号踢出这场戏」是本条闸门的全部分寸：踢人会让演员表
    塌一格，塌到运营受不了就会关护栏。
    """
    from src.companion.group_show.casting import cast_roles

    counts = {("a00", "advocate"): NORMAL_ROLE_CO}
    casting = cast_roles(_book(), _cands(4), role_counts=counts, seed="g1")
    advocate = casting.by_slot("advocate")
    assert advocate is not None and advocate.account_id != "a00"
    assert "a00" in {m.account_id for m in casting.members}


def test_casting_spreads_the_selling_slot_by_remaining_headroom():
    """同台历史打平时优先挑没怎么演过主推的号——主推容量是线性的，只能靠均摊用满。"""
    from src.companion.group_show.casting import cast_roles

    counts = {("a00", "advocate"): 2, ("a01", "advocate"): 2,
              ("a02", "advocate"): 0, ("a03", "advocate"): 2}
    casting = cast_roles(_book(), _cands(4), role_counts=counts, seed="g1")
    assert casting.by_slot("advocate").account_id == "a02"


def test_casting_leaves_the_selling_slot_empty_when_everybody_is_maxed_out():
    """全员到线时宁可这场不种草——戏照演（提问/路人还在），只是不带货。"""
    from src.companion.group_show.casting import cast_roles

    counts = {(f"a{i:02d}", "advocate"): NORMAL_ROLE_CO for i in range(4)}
    casting = cast_roles(_book(), _cands(4), role_counts=counts, seed="g1")
    assert casting.by_slot("advocate") is None
    assert "advocate" in casting.unfilled
    assert casting.by_slot("asker") is not None      # 掩护角色照常填


def test_casting_without_a_role_ledger_behaves_exactly_as_before():
    """无台账＝无行为变化。护栏默认开着也不许改动任何既有调用的结果。"""
    from src.companion.group_show.casting import cast_roles

    assert (cast_roles(_book(), _cands(4), seed="g1")
            == cast_roles(_book(), _cands(4), role_counts={}, seed="g1"))


def test_casting_role_limit_zero_turns_the_gate_off():
    from src.companion.group_show.casting import cast_roles

    counts = {(f"a{i:02d}", "advocate"): 99 for i in range(4)}
    casting = cast_roles(_book(), _cands(4), role_counts=counts, role_limit=0,
                         seed="g1")
    assert casting.by_slot("advocate") is not None
