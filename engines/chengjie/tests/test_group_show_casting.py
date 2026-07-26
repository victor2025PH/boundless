"""群戏选角门禁（非指纹侧）—— 候选清洗、填坑顺序、确定性、软优化、降级。

指纹分组 / ``allow_shared_host`` / :data:`SHARED_HOST_GROUP` 那条「同组不得同台」
的硬约束由 ``tests/test_group_show_linkage.py`` 完整覆盖，本文件**刻意不重复**：
这里所有候选都显式带上互不相同的 ``fingerprint_group``，把指纹这一维钉成常量，
好让其余四条不变量单独可观测——

1. **健康过滤**：缺 ``health`` 字段与 ``health=offline`` 同权（宁可少上一个人）；
2. **确定性**：同输入同输出，且**与候选列表先后顺序无关**（不用 ``random`` 的
   全部意义就在这里——一场戏为什么是这几个号，必须能离线复现、能进回归门禁）；
3. **优先级降级**：号不够时按 ``SLOT_PRIORITY`` 保核心角，其余进 ``unfilled``，
   而不是报错；
4. **软优化零代价**：性别搭配永远不许偷走一个本可填上的槽。

以及贯穿全模块的那条承诺：**不向调用方抛异常**——运营数据脏是常态，选角炸了会把
整条编排链带走。
"""
from __future__ import annotations

import itertools

import pytest

from src.companion.group_show.casting import (
    HEALTH_OK,
    MIN_HEALTHY_CAST,
    SLOT_PRIORITY,
    cast_roles,
    eligible_candidates,
    slot_fill_order,
    validate_casting,
)
from src.companion.group_show.playbook import CastMember, Casting, Playbook, Role

# 一张覆盖全部候选的指纹表：把「同组互斥」这一维钉成常量，让其余维度单独可观测。
_FP_TABLE = {f"a{i}": f"proxy:px-{i}" for i in range(12)}


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _playbook(slots=("asker", "advocate", "bystander", "skeptic"), pid="pb") -> Playbook:
    """最小可用剧本：选角只读 id 与 roles。"""
    return Playbook(id=pid, name="测试剧本",
                    roles=tuple(Role(slot=s, desc="") for s in slots))


def _cand(account_id, **kw):
    """健康在线、指纹各自独立的候选（本文件的基准形状）。"""
    base = {
        "account_id": account_id,
        "platform": "telegram",
        "persona_id": f"p_{account_id}",
        "display_name": f"名字_{account_id}",
        "health": HEALTH_OK,
        "fingerprint_group": f"proxy:px-{account_id}",
    }
    base.update(kw)
    return base


def _cands(n):
    return [_cand(f"a{i}") for i in range(n)]


def _hard(problems):
    return [p for p in problems if not p.startswith("warn:")]


def _no_fp_noise(problems):
    """剔掉「未传指纹表」那条 warn——它属于 linkage 侧覆盖范围，不是本文件的观察对象。"""
    return [p for p in problems if "关联复查" not in p]


# ── 候选清洗：健康过滤 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("health", ["offline", "banned", "unknown", "", "   ", None, True])
def test_only_online_accounts_enter_the_pool(health):
    """非 ``online`` 一律不上台。

    注意 ``True`` / ``None`` 这类脏值也必须被拒——它们最容易在「字段没写全」时冒出来，
    而一个状态不明的号上台的风险和一个已知挂掉的号完全不同（前者可能正在风控观察期）。
    """
    assert eligible_candidates([_cand("a1", health=health)]) == []


def test_missing_health_field_is_treated_as_unusable():
    """**边界，也是最容易写反的一处**：缺字段 ≠ 默认健康。

    群戏是多号同台的高危动作，「不知道这号还活着没」与「知道它挂了」在风险上同权。
    把缺失当默认在线，等于让一批状态未知的号一起进群说话。
    """
    raw = _cand("a1")
    raw.pop("health")
    assert eligible_candidates([raw]) == []


@pytest.mark.parametrize("health", ["ONLINE", " Online ", "online"])
def test_health_matching_is_case_and_whitespace_tolerant(health):
    """运营数据大小写不统一是常态，不该因为写了 ``Online`` 就把好号丢掉。"""
    assert len(eligible_candidates([_cand("a1", health=health)])) == 1


# ── 候选清洗：丢弃与去重 ────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_id", [None, "", "   "])
def test_candidates_without_account_id_are_dropped(bad_id):
    """没有账号 id 就发不出消息，更要命的是无法做去重与指纹归组。"""
    assert eligible_candidates([_cand(bad_id)]) == []


@pytest.mark.parametrize("junk", [None, "a string", 42, ["nested"], object()])
def test_non_mapping_candidates_are_dropped_silently(junk):
    """候选池来自外部编排器，混进非字典元素时应跳过而不是掀翻整场选角。"""
    assert eligible_candidates([junk, _cand("a1")])[0]["account_id"] == "a1"


def test_duplicate_registration_keeps_the_first_entry():
    """同一个号被登记两次（不同人设/不同显示名）→ 以首条为准，且只上台一次。

    重复不是理论问题：候选池常由「在线号 + 本群历史发言号」两处合并而来。若不去重，
    同一个号会被分到两个角色，在群里自问自答——比指纹更容易被人眼直接抓到。
    """
    pool = eligible_candidates([
        _cand("a1", persona_id="first"),
        _cand("a1", persona_id="second"),
    ])
    assert len(pool) == 1 and pool[0]["persona_id"] == "first"


def test_same_id_on_two_platforms_is_two_different_accounts():
    """去重键是 ``platform:account_id``——两个平台上的同名 id 是两个真实账号。"""
    pool = eligible_candidates([
        _cand("777", platform="telegram"),
        _cand("777", platform="whatsapp"),
    ])
    assert len(pool) == 2


def test_pool_preserves_input_order():
    """池子不排序（真正的挑选顺序在 ``cast_roles`` 里按角色槽算），顺序语义要稳。"""
    ids = [c["account_id"] for c in eligible_candidates(_cands(5))]
    assert ids == ["a0", "a1", "a2", "a3", "a4"]


def test_numeric_account_ids_are_stringified():
    """从库里捞出来的 id 可能是 int，下游全按字符串比对，必须在入口归一。"""
    pool = eligible_candidates([_cand(12345)])
    assert pool[0]["account_id"] == "12345"
    assert pool[0]["account_key"] == "telegram:12345"


def test_platform_defaults_to_telegram_and_is_lowercased():
    pool = eligible_candidates([_cand("a1", platform=" TeleGram "),
                                _cand("a2", platform=None)])
    assert [c["platform"] for c in pool] == ["telegram", "telegram"]


@pytest.mark.parametrize("empty", [None, [], ()])
def test_empty_candidate_input_yields_an_empty_pool(empty):
    assert eligible_candidates(empty) == []


# ── 填坑顺序 ────────────────────────────────────────────────────────────────


def test_standard_slots_fill_in_priority_not_declaration_order():
    """填坑顺序是**优先级序**（advocate → asker → skeptic → bystander），与剧本
    声明顺序无关。

    这个顺序就是「号不够时砍谁」的答案：advocate 缺位＝这场戏没有落点（白演还白
    担风险），bystander 只是锦上添花，第一个被砍。剧本作者把 bystander 写在最前面
    也不该改变这个安全序。
    """
    reversed_declaration = _playbook(("bystander", "skeptic", "advocate", "asker"))
    assert slot_fill_order(reversed_declaration) == (
        "advocate", "asker", "skeptic", "bystander")
    assert SLOT_PRIORITY["advocate"] < SLOT_PRIORITY["bystander"]


def test_custom_slots_queue_behind_the_standard_four_in_declaration_order():
    """剧本自定义槽统一排在标准四角之后，内部按作者写的顺序（谁写在前谁先填）。"""
    pb = _playbook(("zeta", "bystander", "alpha", "advocate"))
    assert slot_fill_order(pb) == ("advocate", "bystander", "zeta", "alpha")


def test_repeated_slot_declaration_collapses_to_one_seat():
    """剧本里重复声明同一槽是 playbook 层的问题，选角只当一个槽处理，不因此拒演。"""
    assert slot_fill_order(_playbook(("asker", "asker", "advocate"))) == (
        "advocate", "asker")


def test_slot_fill_order_on_a_playbook_without_roles():
    assert slot_fill_order(_playbook(())) == ()
    assert slot_fill_order(None) == ()


# ── 确定性（本模块不用 random 的全部意义） ──────────────────────────────────


def test_casting_is_reproducible_across_runs():
    """同输入跑两次必须字节级一致——排练要能复现，回归门禁才钉得住这场戏。"""
    pb, cands = _playbook(), _cands(4)
    first = cast_roles(pb, cands, fingerprint_groups=_FP_TABLE)
    second = cast_roles(pb, cands, fingerprint_groups=_FP_TABLE)
    assert first == second


def test_casting_is_independent_of_candidate_order():
    """**关键不变量**：候选池顺序（编排器返回顺序、库排序）不得影响谁演什么。

    候选顺序是外部实现细节——今天按 last_seen 排、明天按 id 排，选角结果就跟着变，
    「同一场戏重跑一致」的承诺立刻失效，dry-run 排练的结论也就不能推广到真发。
    """
    pb = _playbook()
    results = {
        tuple((m.slot, m.account_id) for m in
              cast_roles(pb, list(perm), fingerprint_groups=_FP_TABLE).members)
        for perm in itertools.permutations(_cands(4))
    }
    assert len(results) == 1, f"候选顺序改变了选角结果：{results}"


def test_rotation_makes_different_playbooks_pick_different_leads():
    """确定性 ≠ 僵化：换一场戏应自然换一批人（crc32 打散），否则同一个号永远当主推。"""
    cands = _cands(6)
    leads = {
        cast_roles(_playbook(pid=f"pb_{i}"), cands,
                   fingerprint_groups=_FP_TABLE).by_slot("advocate").account_id
        for i in range(8)
    }
    assert len(leads) > 1, "八个不同剧本全选同一个主推，轮换打散没生效"


def test_the_same_account_never_plays_two_roles():
    """一个号在群里自问自答是最容易被人眼抓到的破绽（比指纹更直白）。"""
    casting = cast_roles(_playbook(), _cands(4), fingerprint_groups=_FP_TABLE)
    ids = [m.account_id for m in casting.members]
    assert len(ids) == len(set(ids))


# ── 输出形状 ────────────────────────────────────────────────────────────────


def test_members_and_unfilled_come_back_in_declaration_order():
    """分配顺序是优先级序，**输出顺序是声明序**——两者刻意分开，方便人对着剧本读。"""
    pb = _playbook(("bystander", "skeptic", "advocate", "asker"))
    casting = cast_roles(pb, _cands(2), fingerprint_groups=_FP_TABLE)
    assert [m.slot for m in casting.members] == ["advocate", "asker"]
    assert casting.unfilled == ("bystander", "skeptic")


def test_cast_member_carries_persona_and_display_name_through():
    """人设 id 决定台词口吻、显示名决定 ECP 里别人怎么称呼他，都不能在选角里丢。"""
    m = cast_roles(_playbook(("advocate",)), [_cand("a1")],
                   fingerprint_groups=_FP_TABLE).by_slot("advocate")
    assert m.persona_id == "p_a1" and m.display_name == "名字_a1"


# ── 号不够时的降级 ──────────────────────────────────────────────────────────


def test_short_roster_keeps_the_core_roles_and_drops_the_filler():
    """只有 2 个号演四角戏 → 保 advocate + asker，砍 skeptic/bystander，且不报错。"""
    casting = cast_roles(_playbook(), _cands(2), fingerprint_groups=_FP_TABLE)
    assert set(casting.filled_slots) == {"advocate", "asker"}
    assert set(casting.unfilled) == {"skeptic", "bystander"}


def test_zero_candidates_yields_an_empty_cast_not_an_exception():
    """一个可用号都没有是运营常态（全在冷却/全离线），导演看到空表判「这场不演」。"""
    casting = cast_roles(_playbook(), [])
    assert casting.members == ()
    assert set(casting.unfilled) == set(_playbook().slots)


def test_surplus_candidates_do_not_overfill_the_roster():
    """号多于槽时只上够用的人——多余的号留在池子里，不该硬塞进戏里。"""
    casting = cast_roles(_playbook(("advocate", "asker")), _cands(6),
                         fingerprint_groups=_FP_TABLE)
    assert len(casting.members) == 2


@pytest.mark.parametrize("n_slots,n_cands", [(4, 0), (4, 1), (4, 3), (4, 4), (2, 5), (0, 3)])
def test_filled_count_equals_min_slots_and_available_accounts(n_slots, n_cands):
    """结构性结论：填上的槽数恒等于 ``min(槽数, 可用组数)``，与挑人顺序无关。

    每选中一个号就锁死它整个指纹组，所以「选人」与「消耗一个组」严格一一对应。
    这条等式一旦不成立，说明某个软优化偷走了一个本可填上的槽。
    """
    slots = ("advocate", "asker", "skeptic", "bystander")[:n_slots]
    casting = cast_roles(_playbook(slots), _cands(n_cands),
                         fingerprint_groups=_FP_TABLE)
    assert len(casting.members) == min(n_slots, n_cands)
    assert len(casting.members) + len(casting.unfilled) == n_slots


# ── 空槽归因：四种原因，四种相反的处置 ──────────────────────────────────────


def test_a_short_roster_says_the_slots_are_empty_because_of_the_pool():
    casting = cast_roles(_playbook(), _cands(2), fingerprint_groups=_FP_TABLE)
    assert dict(casting.blocked) == {"skeptic": "pool", "bystander": "pool"}
    assert casting.reason_for("skeptic") == "pool"
    assert casting.reason_for("advocate") == ""      # 填上的槽没有原因可言


def test_budget_capped_slots_are_labelled_budget_not_a_shortage():
    """最要紧的一条：预算压出来的空槽是**护栏在正常工作**，运营不该去补号。"""
    casting = cast_roles(_playbook(), _cands(6), fingerprint_groups=_FP_TABLE,
                         max_speakers=2)
    assert len(casting.members) == 2
    assert set(dict(casting.blocked).values()) == {"budget"}


def test_a_saturated_pair_is_labelled_pair_not_a_shortage():
    from src.companion.group_show.attendance import NORMAL_PAIR_CO

    counts = {("a0", "a1"): NORMAL_PAIR_CO}
    casting = cast_roles(_playbook(("advocate", "asker")), _cands(2),
                         fingerprint_groups=_FP_TABLE, co_performance=counts)
    assert len(casting.members) == 1
    assert casting.reason_for(casting.unfilled[0]) == "pair"


def test_a_burned_out_shill_is_labelled_role_not_a_shortage():
    from src.companion.group_show.roles import NORMAL_ROLE_CO

    roles = {(f"a{i}", "advocate"): NORMAL_ROLE_CO for i in range(2)}
    casting = cast_roles(_playbook(("advocate", "bystander")), _cands(2),
                         fingerprint_groups=_FP_TABLE, role_counts=roles,
                         role_limit=NORMAL_ROLE_CO)
    assert "advocate" in casting.unfilled
    assert casting.reason_for("advocate") == "role"


def test_attribution_never_invents_a_reason_for_a_filled_slot():
    """``blocked`` 与 ``unfilled`` 必须同集合同序——两张表对不上，前端就会串行。"""
    casting = cast_roles(_playbook(), _cands(3), fingerprint_groups=_FP_TABLE)
    assert [s for s, _ in casting.blocked] == list(casting.unfilled)


def test_attribution_is_additive_and_changes_nothing_else():
    """归因是**只增字段**：同一批入参的演员表与空槽一字不变，否则这就是隐性行为改动。"""
    plain = cast_roles(_playbook(), _cands(2), fingerprint_groups=_FP_TABLE)
    assert plain.members == cast_roles(_playbook(), _cands(2),
                                       fingerprint_groups=_FP_TABLE).members
    assert Casting().blocked == ()           # 默认值，旧调用方零感知


# ── 性别搭配软优化 ──────────────────────────────────────────────────────────


def test_gender_optimization_breaks_up_an_all_one_gender_cast():
    """清一色同性别的演员表一眼假（真群里不可能五个人全是女生夸同一个产品）。

    优化只在「已选中的人清一色同一个已知性别」时启动，并且只在**已经通过硬过滤**的
    候选里换一个挑——所以它永远不会与安全约束打架。
    """
    genders = {"p_a0": "female", "p_a1": "male", "p_a2": "female", "p_a3": "male"}
    casting = cast_roles(_playbook(), _cands(4), fingerprint_groups=_FP_TABLE,
                         persona_gender=genders)
    picked = {genders[m.persona_id] for m in casting.members}
    assert picked == {"male", "female"}, "四个人两种性别可选，不该选出清一色"


def test_gender_optimization_never_costs_a_seat():
    """**软优化零代价**：开与不开，填上的槽数必须完全相同。

    这是「安全/表现力」优先级的具体体现——任何软优化都不得越过硬约束，也不得让
    一个本可填上的角色空着。
    """
    cands = _cands(4)
    genders = {"p_a0": "female", "p_a1": "male", "p_a2": "female", "p_a3": "male"}
    without = cast_roles(_playbook(), cands, fingerprint_groups=_FP_TABLE)
    with_opt = cast_roles(_playbook(), cands, fingerprint_groups=_FP_TABLE,
                          persona_gender=genders)
    assert len(with_opt.members) == len(without.members)
    assert with_opt.unfilled == without.unfilled


def test_gender_optimization_is_a_no_op_when_only_one_gender_exists():
    """全池都是同一个性别时无从优化，应安静地按原顺序选，而不是空着或重复挑。"""
    genders = {f"p_a{i}": "female" for i in range(4)}
    casting = cast_roles(_playbook(), _cands(4), fingerprint_groups=_FP_TABLE,
                         persona_gender=genders)
    assert len(casting.members) == 4


def test_gender_aliases_are_normalized_into_the_same_bucket():
    """运营手填数据什么都有：``男``/``M``/``male`` 必须归成同一个桶。

    不归一的后果是反向的——三个都写「男」的号会被当成三种不同性别，于是
    「清一色」永远判不出来，软优化静默失效（没有任何报错）。
    """
    cands = [_cand("a0"), _cand("a1"), _cand("a2"), _cand("a3")]
    genders = {"p_a0": "男", "p_a1": "M", "p_a2": " Male ", "p_a3": "女"}
    casting = cast_roles(_playbook(), cands, fingerprint_groups=_FP_TABLE,
                         persona_gender=genders)
    norm = {"p_a0": "male", "p_a1": "male", "p_a2": "male", "p_a3": "female"}
    picked = {norm[m.persona_id] for m in casting.members}
    assert picked == {"male", "female"}


@pytest.mark.parametrize("junk", [None, "not a mapping", 42, [("p_a0", "male")]])
def test_dirty_persona_gender_is_ignored_not_fatal(junk):
    """性别表是可选的锦上添花输入，传垃圾就当没传，绝不能影响选角能不能跑。"""
    casting = cast_roles(_playbook(), _cands(4), fingerprint_groups=_FP_TABLE,
                         persona_gender=junk)
    assert len(casting.members) == 4


def test_unknown_gender_values_keep_their_own_bucket():
    """无法识别的非空性别（如 ``nb``）保留为独立桶，不该被并进 male/female。"""
    genders = {"p_a0": "nb", "p_a1": "nb"}
    casting = cast_roles(_playbook(("advocate", "asker")), _cands(2),
                         fingerprint_groups=_FP_TABLE, persona_gender=genders)
    assert len(casting.members) == 2


# ── 降级而非报错 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("junk_playbook", [None, "not a playbook", 42, object()])
def test_cast_roles_degrades_to_an_empty_cast_on_a_junk_playbook(junk_playbook):
    """剧本对象不对劲 → 空演员表（导演判「不演」），而不是把编排链炸掉。"""
    casting = cast_roles(junk_playbook, _cands(3))
    assert casting.members == ()


@pytest.mark.parametrize("junk_candidates", [42, object(), "abc"])
def test_cast_roles_degrades_when_the_candidate_pool_is_not_iterable(junk_candidates):
    """**最保守的降级**：谁也不上台、全部槽标空。

    「带着半张来路不明的演员表硬开」比「这场先不演」危险得多——空表是可观测的
    失败，半张表是会真发消息的失败。
    """
    casting = cast_roles(_playbook(), junk_candidates)
    assert casting.members == ()
    assert set(casting.unfilled) == set(_playbook().slots)


def test_cast_roles_survives_candidates_whose_str_explodes():
    """候选可能是 ORM 行对象，连 ``__str__`` 都可能炸——一个坏元素不能带走整池。"""
    class Explosive:
        def __str__(self):  # noqa: D105
            raise RuntimeError("boom")

    casting = cast_roles(_playbook(), [{"account_id": Explosive(), "health": "online"},
                                       _cand("a1")], fingerprint_groups=_FP_TABLE)
    assert [m.account_id for m in casting.members] == ["a1"]


@pytest.mark.parametrize("junk", [42, object()])
def test_eligible_candidates_should_not_raise_on_non_iterable_input(junk):
    assert eligible_candidates(junk) == []


# ── validate_casting：非指纹类校验项 ────────────────────────────────────────


def test_empty_cast_is_a_hard_stop():
    """空演员表＝一个可用号都没有，这场戏不能开——必须是硬错而不是 warn。"""
    problems = validate_casting(Casting(), _playbook())
    assert _hard(problems) == ["演员表为空——一个可用号都没有，这场戏不能开"]


def test_one_account_holding_two_roles_is_hard():
    """一号分饰两角＝群里自问自答。演员表可能来自持久化状态或人工编辑，开演前必查。"""
    casting = Casting(members=(CastMember("advocate", "a1", "p1"),
                               CastMember("asker", "a1", "p1")))
    problems = _hard(validate_casting(casting, _playbook(),
                                      fingerprint_groups=_FP_TABLE))
    assert any("分饰两角" in p for p in problems)


def test_same_account_id_check_is_platform_aware():
    """跨平台同 id 是两个真实账号，不该被误报成分饰两角。"""
    casting = Casting(members=(CastMember("advocate", "777", "p1", platform="telegram"),
                               CastMember("asker", "777", "p2", platform="whatsapp")))
    problems = _hard(validate_casting(casting, _playbook(),
                                      fingerprint_groups={"777": "proxy:px-1"}))
    assert not any("分饰两角" in p for p in problems)


def test_a_slot_assigned_twice_is_hard():
    """两个号抢同一个槽 → 导演按槽取人只会取到第一个，另一个号整场干坐着。"""
    casting = Casting(members=(CastMember("advocate", "a1", "p1"),
                               CastMember("advocate", "a2", "p2")))
    problems = _hard(validate_casting(casting, _playbook(),
                                      fingerprint_groups=_FP_TABLE))
    assert any("重复分配" in p for p in problems)


def test_a_slot_the_playbook_never_declared_is_hard():
    """演员表里的野槽永远拿不到 directive，这号只会在群里干坐着（静默失败）。"""
    casting = Casting(members=(CastMember("advocate", "a0", "p0"),
                               CastMember("ghost", "a1", "p1")))
    problems = _hard(validate_casting(casting, _playbook(),
                                      fingerprint_groups=_FP_TABLE))
    assert any("未声明的角色槽" in p and "ghost" in p for p in problems)


def test_missing_advocate_is_hard_and_says_which_kind_of_missing():
    """核心角缺位＝这场戏没有落点，白演还白担风险；两种缺法要给不同的解法提示。

    「剧本声明了但没配上人」要去补号，「剧本压根没声明」要去补剧本——文案分开写
    是为了让运营看一眼就知道下一步做什么，而不是两种情况都回一句「advocate 缺失」。
    """
    declared = Casting(members=(CastMember("asker", "a1", "p1"),))
    msg_declared = _hard(validate_casting(declared, _playbook(),
                                          fingerprint_groups=_FP_TABLE))
    assert any("种草角缺位" in p for p in msg_declared)

    undeclared = Casting(members=(CastMember("asker", "a1", "p1"),))
    msg_undeclared = _hard(validate_casting(undeclared, _playbook(("asker",)),
                                            fingerprint_groups=_FP_TABLE))
    assert any("先补剧本" in p for p in msg_undeclared)


def test_missing_skeptic_is_only_a_warning():
    """一边倒好评可信度打折，但戏能演——软警告，不阻断。"""
    casting = Casting(members=(CastMember("advocate", "a0", "p0"),
                               CastMember("asker", "a1", "p1"),
                               CastMember("bystander", "a2", "p2")))
    problems = validate_casting(casting, _playbook(), fingerprint_groups=_FP_TABLE)
    assert _hard(problems) == []
    assert any(p.startswith("warn:") and "skeptic" in p for p in problems)


def test_a_two_person_cast_gets_the_double_act_warning():
    """两个人一唱一和的「双簧感」很扎眼，值得提醒，但不该阻断（有时只有两个号）。"""
    casting = Casting(members=(CastMember("advocate", "a0", "p0"),
                               CastMember("asker", "a1", "p1")))
    problems = validate_casting(casting, _playbook(), fingerprint_groups=_FP_TABLE)
    assert any("双簧" in p and p.startswith("warn:") for p in problems)
    assert MIN_HEALTHY_CAST == 3


def test_all_one_gender_cast_is_flagged_as_a_warning():
    """选角侧的软优化可能因为池子本身就单一而失效，校验侧要如实把结果说出来。"""
    casting = Casting(members=(CastMember("advocate", "a0", "p0"),
                               CastMember("asker", "a1", "p1"),
                               CastMember("skeptic", "a2", "p2")))
    problems = validate_casting(
        casting, _playbook(), fingerprint_groups=_FP_TABLE,
        persona_gender={"p0": "female", "p1": "女", "p2": "f"})
    assert any("清一色" in p and p.startswith("warn:") for p in problems)


def test_one_known_gender_is_not_enough_to_call_it_uniform():
    """只有一个号知道性别时说「清一色」是噪音——阈值是 2 个已知同性别起。"""
    casting = Casting(members=(CastMember("advocate", "a0", "p0"),
                               CastMember("asker", "a1", "p1"),
                               CastMember("skeptic", "a2", "p2")))
    problems = validate_casting(casting, _playbook(), fingerprint_groups=_FP_TABLE,
                                persona_gender={"p0": "female"})
    assert not any("清一色" in p for p in problems)


def test_accounts_without_a_persona_are_named_in_a_warning():
    """没绑人设 → 台词退化成默认口吻 → 那个号一说话就串味，必须点名到号。"""
    casting = Casting(members=(CastMember("advocate", "a0", "p0"),
                               CastMember("asker", "a1", ""),
                               CastMember("skeptic", "a2", "  ")))
    problems = validate_casting(casting, _playbook(), fingerprint_groups=_FP_TABLE)
    named = [p for p in problems if "没绑人设" in p]
    assert named and "a1" in named[0] and "a2" in named[0]


def test_unfilled_slots_are_reported_as_a_warning_excluding_advocate():
    """降级演法要如实上报；advocate 已经在硬错里点过名，不重复刷屏。"""
    casting = Casting(members=(CastMember("advocate", "a0", "p0"),
                               CastMember("asker", "a1", "p1"),
                               CastMember("skeptic", "a2", "p2")),
                      unfilled=("bystander",))
    problems = validate_casting(casting, _playbook(), fingerprint_groups=_FP_TABLE)
    assert any("未配上的角色槽" in p and "bystander" in p for p in problems)


def test_a_freshly_cast_full_roster_passes_clean():
    """**闭环锚点**：选角自己产出的完整演员表必须通过自己的复查。

    ``cast_roles`` 与 ``validate_casting`` 是一对生产者/校验者，两边对不上就说明
    其中一方的语义偷偷漂了。
    """
    casting = cast_roles(_playbook(), _cands(4), fingerprint_groups=_FP_TABLE)
    problems = _no_fp_noise(validate_casting(casting, _playbook(),
                                             fingerprint_groups=_FP_TABLE))
    assert problems == []


def test_validator_that_explodes_still_returns_a_verdict():
    """校验器自己炸也必须给出「不通过」结论，绝不能把异常上抛给开演决策点。

    这是本模块最后一道门：它抛异常，调用方就只能在「崩了」和「当作通过」之间选，
    而后者意味着一张没验过的演员表直接上台真发。
    """
    class Explosive:
        @property
        def members(self):
            raise RuntimeError("boom")

    problems = validate_casting(Explosive(), _playbook())
    assert problems and any("校验异常" in p and "不通过" in p for p in problems)


@pytest.mark.parametrize("junk_playbook", [None, "nope", 42])
def test_validate_casting_tolerates_a_junk_playbook(junk_playbook):
    """剧本对象不对时仍要能给出结论（演员表本身的问题照样查得出来）。"""
    casting = Casting(members=(CastMember("advocate", "a0", "p0"),))
    problems = validate_casting(casting, junk_playbook, fingerprint_groups=_FP_TABLE)
    assert isinstance(problems, list)
