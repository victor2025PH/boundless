"""演出矩阵门禁 —— 成员矩阵冻结之后，唯一还能动的那条暴露面。

这一层守的不变量分三类，每一类漏了都是真事故：

1. **台账口径**（``performance_ledger``）：排练不算暴露、真人插话不算我们的号。写反
   任何一条，看板上的读数就是假的——而假的安全读数比没有读数危险得多。
2. **开口人数杠杆**（``speaker_budget``）：共现对次对开口人数是二次的，这条算错会让
   运营按错误的编制去养号（多养五个号 ≠ 少让一个号开口）。``speakers=1`` 的特判尤其
   要钉住：成员面「一个人不成群戏」返回 0，演出面「一个号开口＝零号对」是无上限。
3. **选角落地**（``cast_roles``）：算得对但没接进选角＝白算。同时必须证明**不传台账时
   既有行为一字不变**，否则这层优化就成了对存量编排的隐性改动。
"""
from __future__ import annotations

from typing import Tuple

from src.companion.group_show.attendance import (
    NORMAL_PAIR_CO,
    VERDICT_DANGER,
    VERDICT_SAFE,
    VERDICT_WARN,
)
from src.companion.group_show.casting import cast_roles
from src.companion.group_show.performance import (
    SOLO_SPEAKERS,
    added_co_performance,
    admits_speaker,
    co_performance,
    exposure_report,
    hot_pairs,
    pair_headroom,
    performance_metrics,
    rank_speakers,
    resolve_speaker_cap,
    speaker_budget,
)
from src.companion.group_show.playbook import (
    Beat, Casting, Playbook, Role, ShowEvent, ShowState,
)
from src.companion.group_show.store import GroupShowStore


def _pool(n: int):
    return [f"a{i:02d}" for i in range(n)]


def _candidates(n: int, **kw):
    return [
        {"account_id": f"a{i:02d}", "platform": "telegram",
         "persona_id": f"p{i:02d}", "health": "online",
         "fingerprint_group": f"fp{i:02d}", **kw}
        for i in range(n)
    ]


def _playbook(slots=("advocate", "asker", "skeptic", "bystander")):
    return Playbook(
        id="pb1", name="t", system="growth",
        roles=tuple(Role(slot=s, desc=s) for s in slots),
        beats=(Beat(id="b1", role="advocate", intent="i"),),
    )


# ── 演出矩阵：与成员矩阵同构、可直接对照 ────────────────────────────────────


def test_performance_metrics_shares_the_attendance_algorithm():
    """两个矩阵必须用同一套算法算，否则看板上那组对照数不可比——那是它唯一的用处。"""
    ledger = {"g1": ["a", "b"], "g2": ["a", "b"], "g3": ["a", "c"]}
    metrics = performance_metrics(ledger)
    assert metrics["max_pair_co"] == 2
    assert metrics["worst_pair"] == ["a", "b"]
    assert metrics["verdict"] == VERDICT_SAFE


def test_co_performance_counts_unordered_pairs():
    counts = co_performance({"g1": ["b", "a"], "g2": ["a", "b"]})
    assert counts[("a", "b")] == 2
    assert ("b", "a") not in counts


def test_performance_metrics_tolerates_garbage():
    """坏台账 → 形状完整的空报告，绝不抛（看板拿不到数也不该白屏）。"""
    assert performance_metrics(None)["max_pair_co"] == 0
    assert performance_metrics("nonsense")["accounts"] == 0
    assert performance_metrics({"g": 7})["groups"] == 1


# ── 开口人数预算：本模块最强的杠杆 ──────────────────────────────────────────


def test_one_speaker_per_show_is_unlimited_not_zero():
    """``seats=1`` 在成员面返回 0（一个人不成群戏），在演出面却是**零号对＝无上限**。

    直接套用 ``max_safe_groups`` 会把最安全的演法（单号出场）报成最危险，这个特判
    是本模块最容易写反的一行。
    """
    budget = speaker_budget(pool=10, groups=100000)
    solo = [o for o in budget["options"] if o["speakers"] == SOLO_SPEAKERS][0]
    assert solo["unlimited"] is True
    assert solo["fits"] is True
    assert solo["max_groups"] is None


def test_dropping_one_speaker_beats_adding_five_accounts():
    """量化钉子：开口人数是二次杠杆，这条错了运营会按错误编制去养号。

    10 个号每场 3 人开口只能覆盖 30 个群；同样 10 个号降到 2 人开口能覆盖 90 个。
    而把号池从 10 加到 15、仍按 3 人开口，也才 70 个——**少一个开口者 > 多五个号**。
    """
    budget10 = speaker_budget(pool=10, groups=100)
    by_k = {o["speakers"]: o for o in budget10["options"]}
    assert by_k[3]["max_groups"] == 30
    assert by_k[2]["max_groups"] == 90

    budget15 = speaker_budget(pool=15, groups=100)
    by_k15 = {o["speakers"]: o for o in budget15["options"]}
    assert by_k15[3]["max_groups"] == 70
    assert by_k[2]["max_groups"] > by_k15[3]["max_groups"]


def test_two_speaker_mode_triples_what_ten_accounts_can_cover():
    """P0 说「10 个号最多 30 个群」，那是把「每场几个号开口」当成了不可调的常量。

    松开它：同样 10 个号，2 人开口档能覆盖 90 个群。要真铺满 100 个群，补到 11 个号
    即可（``C(11,2)=55`` 对 → 110 ≥ 100），而不是 P0 建议的 18 个。
    """
    fits90 = speaker_budget(pool=10, groups=90)
    assert fits90["max_speakers"] == 2
    assert fits90["solo_only"] is False

    # 100 个群刚好越界（上限 90）——这里不许四舍五入，差一个群也要如实报不达标
    edge = speaker_budget(pool=10, groups=100)
    by_k = {o["speakers"]: o for o in edge["options"]}
    assert by_k[2]["max_groups"] == 90
    assert by_k[2]["fits"] is False
    assert edge["max_speakers"] == SOLO_SPEAKERS

    assert speaker_budget(pool=11, groups=100)["max_speakers"] == 2


def test_tiny_pool_falls_back_to_solo_only():
    """号池小到连两人同台都撑不住时，必须明说「只能单号演」而不是给个假的档位。"""
    budget = speaker_budget(pool=3, groups=500)
    assert budget["solo_only"] is True
    assert budget["max_speakers"] == SOLO_SPEAKERS


def test_speaker_budget_tolerates_garbage():
    assert speaker_budget(pool="x", groups=None)["pool"] == 0
    assert speaker_budget(pool=-5, groups=-5)["options"]


# ── 双读数报告 ──────────────────────────────────────────────────────────────


def test_report_shows_rotation_suppressing_a_frozen_membership():
    """成员面已超标（冻结）但演出面被轮换压住——报告要如实说「这条路是对的」。"""
    memberships = {f"g{i}": ["a", "b", "c"] for i in range(10)}
    performances = {f"g{i}": ["a", "b"] if i < 2 else ["a", "c"]
                    for i in range(10)}
    report = exposure_report(memberships, performances)
    assert report["membership"]["max_pair_co"] == 10
    assert report["performance"]["max_pair_co"] == 8
    assert 0 < report["suppression"] < 1
    assert any("冻结" in a for a in report["advice"])


def test_report_verdict_takes_the_worse_of_the_two_axes():
    """只报演出面等于告诉运营「你安全」，然后他继续加群——判词必须取更差的那个。"""
    memberships = {f"g{i}": ["a", "b"] for i in range(20)}   # 成员面爆表
    performances = {"g0": ["a", "b"]}                        # 演出面干净
    report = exposure_report(memberships, performances)
    assert report["performance"]["verdict"] == VERDICT_SAFE
    assert report["membership"]["verdict"] == VERDICT_DANGER
    assert report["verdict"] == VERDICT_DANGER


def test_report_tells_you_how_many_speakers_to_drop_to():
    """演出面超标时，建议必须是**可执行的数字**，不是「注意风险」这种废话。"""
    performances = {f"g{i}": ["a", "b", "c"] for i in range(20)}
    report = exposure_report({}, performances)
    assert report["performance"]["verdict"] != VERDICT_SAFE
    assert report["advice"]
    assert any("开口人数" in a or "开口" in a for a in report["advice"])


def test_clean_report_still_gives_a_budget_line():
    report = exposure_report({"g1": ["a", "b"]}, {"g1": ["a", "b"]})
    assert report["verdict"] == VERDICT_SAFE
    assert report["advice"]
    assert report["budget"]["max_speakers"] >= 1


def test_report_tolerates_garbage():
    report = exposure_report(None, "nonsense")
    assert report["verdict"] == VERDICT_SAFE
    assert report["membership"]["accounts"] == 0


# ── 排序：优先挑没同台过的 ──────────────────────────────────────────────────


def test_added_co_performance_scores_against_the_chosen_set():
    counts = {("a", "b"): 5, ("a", "c"): 1}
    assert added_co_performance("a", ["b", "c"], counts) == 6
    assert added_co_performance("a", ["c"], counts) == 1
    assert added_co_performance("a", ["a"], counts) == 0   # 不跟自己算
    assert added_co_performance("z", ["b"], counts) == 0


def test_added_co_performance_never_raises():
    assert added_co_performance(None, None, None) == 0
    assert added_co_performance("a", ["b"], "not-a-mapping") == 0


def test_rank_speakers_prefers_strangers():
    counts = {("a", "b"): 9, ("a", "c"): 0}
    ranked = rank_speakers(["b", "c"], co_performance_counts=counts, chosen=["a"])
    assert ranked[0] == "c"


def test_rank_speakers_is_deterministic_across_processes():
    """打平局不能用内置 ``hash()``——它对 str 每进程加盐，换进程换一套顺序。"""
    members = _pool(8)
    first = rank_speakers(members, seed="g1")
    assert first == rank_speakers(list(reversed(members)), seed="g1")
    assert first != rank_speakers(members, seed="g2") or len(set(first)) <= 1


def test_rank_speakers_tolerates_garbage():
    assert rank_speakers([]) == []
    assert rank_speakers(["a", "", None, "a"]) == ["a"]


# ── 落到选角上 ──────────────────────────────────────────────────────────────


def test_casting_without_a_ledger_is_bit_for_bit_unchanged():
    """不传台账时选角结果必须与改动前完全一致，否则这层优化是对存量编排的隐性改动。"""
    playbook, cands = _playbook(), _candidates(6)
    baseline = cast_roles(playbook, cands)
    assert cast_roles(playbook, cands, co_performance={}) == baseline
    assert cast_roles(playbook, cands, co_performance=None) == baseline


def test_casting_avoids_pairing_accounts_that_keep_co_starring():
    """传了台账，同台过很多次的两个号就不该继续被凑到一张演员表上。"""
    playbook = _playbook(("advocate", "asker"))
    cands = _candidates(3)
    lead = cast_roles(playbook, cands).members[0].account_id
    partner = cast_roles(playbook, cands).members[1].account_id
    others = [c["account_id"] for c in cands
              if c["account_id"] not in (lead, partner)]

    counts = {tuple(sorted((lead, partner))): 99}
    picked = cast_roles(playbook, cands, co_performance=counts)
    assert picked.members[0].account_id == lead
    assert picked.members[1].account_id in others


def test_fingerprint_isolation_still_outranks_co_performance():
    """同台历史是软优化，指纹同组是硬约束——软的永远不许压过硬的。"""
    playbook = _playbook(("advocate", "asker"))
    cands = _candidates(2)
    cands[0]["fingerprint_group"] = "same"
    cands[1]["fingerprint_group"] = "same"
    casting = cast_roles(playbook, cands, co_performance={})
    assert len(casting.members) == 1
    assert "asker" in casting.unfilled


def test_max_speakers_caps_the_cast_and_reports_the_rest_as_unfilled():
    """预算压缩演员表时，被压掉的槽要如实进 unfilled——导演得看出这场是被压成几个人的。"""
    playbook, cands = _playbook(), _candidates(6)
    casting = cast_roles(playbook, cands, max_speakers=2)
    assert len(casting.members) == 2
    assert len(casting.unfilled) == 2
    # 砍人按 SLOT_PRIORITY：种草角与抛痛点的角色保住
    assert {m.slot for m in casting.members} == {"advocate", "asker"}


def test_max_speakers_zero_means_no_cap():
    playbook, cands = _playbook(), _candidates(6)
    assert cast_roles(playbook, cands, max_speakers=0) == cast_roles(playbook, cands)
    assert cast_roles(playbook, cands, max_speakers=-1) == cast_roles(playbook, cands)


def test_max_speakers_tolerates_garbage():
    playbook, cands = _playbook(), _candidates(6)
    assert cast_roles(playbook, cands, max_speakers="x") == cast_roles(playbook, cands)


# ── 多轴判词合并 ────────────────────────────────────────────────────────────


def test_worst_verdict_takes_the_single_worst_axis():
    """三轴里有一条露馅，另外两条再干净也救不回来——平台按最可疑的特征下手。"""
    from src.companion.group_show.performance import worst_verdict

    assert worst_verdict("safe", "safe", "safe") == "safe"
    assert worst_verdict("safe", "warn", "safe") == "warn"
    assert worst_verdict("safe", "warn", "danger") == "danger"
    assert worst_verdict("danger", "safe") == "danger"


def test_worst_verdict_treats_unknown_as_safe_and_never_raises():
    """判词字段缺失/脏值不该让整张卡 500——降级成 safe，风险由其它轴照常报。"""
    from src.companion.group_show.performance import worst_verdict

    assert worst_verdict() == "safe"
    assert worst_verdict(None, "", 42, {"a": 1}) == "safe"
    assert worst_verdict(None, "danger") == "danger"


# ── 台账派生：两条过滤是这份数据的全部意义 ──────────────────────────────────


def _session(store, sid, group, *, dry_run, speakers, kind="line", ts=1000.0):
    state = ShowState(
        session_id=sid, group_key=group, platform="telegram",
        playbook=_playbook(), casting=Casting(), dry_run=dry_run, started_at=ts,
    )
    state.events = [
        ShowEvent(seq=i + 1, ts=ts, speaker_account=acct, role="advocate",
                  beat_id="b1", text="hi", kind=kind)
        for i, acct in enumerate(speakers)
    ]
    store.save_session(state)


def test_rehearsals_do_not_count_as_exposure():
    """排练一条消息都没发出去。算进来会让「多排练几次」凭空推高风险，运营就不敢排练了。"""
    store = GroupShowStore(":memory:")
    _session(store, "s1", "g1", dry_run=True, speakers=["a", "b"])
    assert store.performance_ledger() == {}

    _session(store, "s2", "g1", dry_run=False, speakers=["a", "b"])
    assert store.performance_ledger() == {"g1": ["a", "b"]}


def test_real_humans_interjecting_are_not_our_accounts():
    """``human`` 事件的 speaker 是群友本人，算进来等于把真实用户拉进关联分析。"""
    store = GroupShowStore(":memory:")
    _session(store, "s1", "g1", dry_run=False, speakers=["a"], kind="line")
    _session(store, "s2", "g1", dry_run=False, speakers=["real_person"],
             kind="human")
    _session(store, "s3", "g1", dry_run=False, speakers=["director"],
             kind="yield")
    assert store.performance_ledger() == {"g1": ["a"]}


def test_media_counts_as_speaking():
    store = GroupShowStore(":memory:")
    _session(store, "s1", "g1", dry_run=False, speakers=["a"], kind="media")
    assert store.performance_ledger() == {"g1": ["a"]}


def test_ledger_window_lets_old_co_starring_fade_out():
    """共现要能随时间淡出，否则跑几个月每一对都饱和，这个数就失去分辨力了。"""
    store = GroupShowStore(":memory:")
    _session(store, "old", "g1", dry_run=False, speakers=["a"], ts=1000.0)
    _session(store, "new", "g2", dry_run=False, speakers=["b"], ts=9000.0)
    assert set(store.performance_ledger()) == {"g1", "g2"}
    assert set(store.performance_ledger(since=5000.0)) == {"g2"}


def test_ledger_window_keeps_events_that_never_recorded_a_timestamp():
    """早期数据 ``ts`` 可能是 0；用 ``e.ts`` 硬过滤会把它们整批判成远古而消失。"""
    store = GroupShowStore(":memory:")
    state = ShowState(
        session_id="s1", group_key="g1", platform="telegram",
        playbook=_playbook(), casting=Casting(), dry_run=False, started_at=9000.0,
    )
    state.events = [ShowEvent(seq=1, ts=0.0, speaker_account="a", role="advocate",
                              beat_id="b1", text="hi", kind="line")]
    store.save_session(state)
    assert store.performance_ledger(since=5000.0) == {"g1": ["a"]}


def test_ledger_is_empty_when_the_store_is_degraded():
    store = GroupShowStore(":memory:")
    store._conn = None  # noqa: SLF001 —— 模拟建库失败的降级实例
    assert store.performance_ledger() == {}


def test_ledger_feeds_straight_into_the_matrix():
    """端到端：库里的真实发言 → 台账 → 矩阵，中间不需要任何形状转换。"""
    store = GroupShowStore(":memory:")
    _session(store, "s1", "g1", dry_run=False, speakers=["a", "b"])
    _session(store, "s2", "g2", dry_run=False, speakers=["a", "b"])
    _session(store, "s3", "g3", dry_run=False, speakers=["a", "c"])
    metrics = performance_metrics(store.performance_ledger())
    assert metrics["max_pair_co"] == 2
    assert metrics["worst_pair"] == ["a", "b"]


# ── 量化：轮换到底买回了多少 ────────────────────────────────────────────────


def test_rotation_recovers_most_of_the_safety_on_a_frozen_membership():
    """最能说明本层价值的一条：成员面已经烂透（10 个号全在 100 个群），只靠控制
    「每场谁开口」能把实际暴露从 100 压回常人区间。

    这是真实处境——群已经加了，退不掉。如果这条不成立，本模块就没有存在意义。
    """
    accounts = _pool(10)
    groups = [f"g{i:03d}" for i in range(100)]
    memberships = {g: list(accounts) for g in groups}
    assert performance_metrics(memberships)["max_pair_co"] == 100

    # 每场只让 2 个号开口，按「优先挑没同台过的」贪心铺满 100 场
    counts: dict = {}
    performances = {}
    for group in groups:
        chosen: list = []
        for _ in range(2):
            ranked = rank_speakers(
                [a for a in accounts if a not in chosen],
                co_performance_counts=counts, chosen=chosen, seed=group)
            chosen.append(ranked[0])
        performances[group] = chosen
        a, b = sorted(chosen)
        counts[(a, b)] = counts.get((a, b), 0) + 1

    metrics = performance_metrics(performances, pool=accounts)
    assert metrics["max_pair_co"] <= NORMAL_PAIR_CO, metrics
    assert metrics["verdict"] == VERDICT_SAFE

    report = exposure_report(memberships, performances, pool=accounts)
    assert report["suppression"] <= 0.05     # 压到成员面的 5% 以内
    assert report["verdict"] == VERDICT_DANGER   # 成员面仍然如实报警


# ── 号对余量闸门：预算之外的第二层护栏（反应性，认真账） ────────────────────


def test_headroom_counts_down_from_the_normal_line():
    counts = {("a", "b"): 2}
    assert pair_headroom("a", "b", counts) == NORMAL_PAIR_CO - 2
    assert pair_headroom("b", "a", counts) == NORMAL_PAIR_CO - 2   # 无序对
    assert pair_headroom("a", "z", counts) == NORMAL_PAIR_CO       # 没同台过＝满余量
    assert pair_headroom("a", "b", counts, limit=1) <= 0           # 已越线


def test_headroom_never_goes_negative_or_blows_up_on_garbage():
    """余量是给人读的数，负数没有意义；脏输入一律按满余量放行（护栏不该反过来卡死演出）。"""
    assert pair_headroom("a", "b", {("a", "b"): 99}) == 0
    assert pair_headroom("a", "a", {("a", "a"): 5}) == NORMAL_PAIR_CO   # 自己跟自己
    assert pair_headroom("a", "b", None) == NORMAL_PAIR_CO
    assert pair_headroom(None, "b", {}) == NORMAL_PAIR_CO
    assert pair_headroom("a", "b", "not-a-mapping") == NORMAL_PAIR_CO   # type: ignore[arg-type]


def test_gate_blocks_a_saturated_pair_but_not_a_fresh_one():
    counts = {("a", "b"): NORMAL_PAIR_CO}
    assert admits_speaker("b", ["a"], counts) is False
    assert admits_speaker("c", ["a"], counts) is True
    assert admits_speaker("b", ["a", "c"], counts) is False    # 任一对到线即出局


def test_the_first_speaker_is_always_admitted():
    """闸门最坏把一场戏压成独白，但绝不能压成空场——空场会让运营直接把护栏关掉。"""
    counts = {(x, y): 99 for x in "abc" for y in "abc" if x < y}
    assert admits_speaker("a", [], counts) is True
    assert admits_speaker("a", None, counts) is True


def test_gate_is_off_without_a_ledger_or_with_a_zero_limit():
    counts = {("a", "b"): 99}
    assert admits_speaker("b", ["a"], None) is True            # 无台账＝无行为变化
    assert admits_speaker("b", ["a"], counts, limit=0) is True  # 显式关闭


# ── 下钻：把「最坏一对共享 8 个群」变成可执行的动作 ─────────────────────────


def _tangled():
    """a×b 缠在四个群，a×c 只在一个群——「最坏一对」与「随便一对」要能分开。"""
    return {
        "g1": ["a", "b", "c"],
        "g2": ["a", "b"],
        "g3": ["a", "b"],
        "g4": ["a", "b"],
        "g5": ["c", "d"],
    }


def test_drilldown_names_the_pair_and_the_groups_behind_the_number():
    """光报「共现 4」运营没法动手；要说出是哪两个号、缠在哪几个群。"""
    top = hot_pairs(_tangled())
    assert top and top[0]["pair"] == ["a", "b"]
    assert top[0]["count"] == 4
    assert top[0]["groups"] == ["g1", "g2", "g3", "g4"]   # 台账原序＝缠上的先后
    assert top[0]["more"] == 0


def test_drilldown_says_whether_the_gate_is_already_blocking_this_pair():
    """证据与闸门判定必须同行——否则运营会手动躲一对早就被拦住的号（白躲）。"""
    top = {tuple(r["pair"]): r for r in hot_pairs(_tangled())}
    ab = top[("a", "b")]
    assert ab["count"] > NORMAL_PAIR_CO
    assert ab["blocked"] is True and ab["headroom"] == 0
    # 与真正执行拦截的那个闸门同一个判定，不能各算各的
    assert admits_speaker("b", ["a"], co_performance(_tangled())) is False

    ac = top[("a", "c")]
    assert ac["blocked"] is False and ac["headroom"] == NORMAL_PAIR_CO - 1
    assert admits_speaker("c", ["a"], co_performance(_tangled())) is True


def test_drilldown_truncates_but_says_how_much_it_hid():
    """列几个群够认出「哦是那批群」就行；剩下的报个数，别把卡片撑爆。"""
    ledger = {f"g{i}": ["a", "b"] for i in range(20)}
    row = hot_pairs(ledger, max_groups=3)[0]
    assert len(row["groups"]) == 3 and row["count"] == 20
    assert row["more"] == 17


def test_drilldown_order_is_stable_across_calls():
    """每刷新一次「最坏一对」都在换，运营会以为矩阵在抖。"""
    ledger = {"g1": ["a", "b"], "g2": ["c", "d"], "g3": ["e", "f"]}
    assert hot_pairs(ledger) == hot_pairs(ledger)
    assert [r["pair"] for r in hot_pairs(ledger)] == [["a", "b"], ["c", "d"],
                                                      ["e", "f"]]


def test_drilldown_reuses_a_precomputed_matrix_when_given_one():
    counts = co_performance(_tangled())
    assert hot_pairs(_tangled(), counts=counts) == hot_pairs(_tangled())


def test_drilldown_degrades_to_empty_instead_of_exploding():
    """下钻只是诊断，坏台账不该连累主读数。"""
    assert hot_pairs(None) == []
    assert hot_pairs("nope") == []          # type: ignore[arg-type]
    assert hot_pairs({"g1": None}) == []
    assert hot_pairs(_tangled(), max_pairs=0) == []


def test_report_carries_the_drilldown_on_both_axes_separately():
    """两条轴的同一对号含义不同：成员面是潜在风险，演出面是已经真发生过。"""
    memberships = {f"g{i}": ["a", "b"] for i in range(6)}
    performances = {"g0": ["a", "b"]}
    report = exposure_report(memberships, performances, pool=["a", "b"])

    member = report["membership"]["hot_pairs"]
    perf = report["performance"]["hot_pairs"]
    assert member[0]["count"] == 6 and member[0]["blocked"] is True
    assert perf[0]["count"] == 1 and perf[0]["blocked"] is False
    # 轮换正在起作用：同框六个群，真同台只有一个
    assert perf[0]["headroom"] > member[0]["headroom"]


def test_a_clean_ledger_drills_down_to_nothing():
    """没同框过的号池不该凭空生出一行「最坏一对」。"""
    report = exposure_report({"g1": ["a"], "g2": ["b"]}, {}, pool=["a", "b"])
    assert report["membership"]["hot_pairs"] == []
    assert report["performance"]["hot_pairs"] == []


# ── 预算 → 本场上限：软护栏的判定表 ─────────────────────────────────────────


def test_cap_defaults_to_the_budget_when_nobody_asked_for_a_number():
    got = resolve_speaker_cap(requested=0, budget=2)
    assert got["effective"] == 2 and got["source"] == "auto"
    assert got["capped"] is False and got["reason"]


def test_cap_honours_a_request_inside_the_budget():
    got = resolve_speaker_cap(requested=2, budget=3)
    assert got["effective"] == 2 and got["source"] == "explicit"
    assert got["capped"] is False


def test_over_budget_is_pushed_back_by_default():
    """默认走安全档：超预算自动压回，而不是打一行 warn 就照发。"""
    got = resolve_speaker_cap(requested=5, budget=2)
    assert got["effective"] == 2 and got["capped"] is True
    assert got["source"] == "capped"
    assert "5" in got["reason"] and "2" in got["reason"]


def test_over_budget_is_allowed_only_when_explicitly_asked_for():
    got = resolve_speaker_cap(requested=5, budget=2, allow_over=True)
    assert got["effective"] == 5 and got["source"] == "override"
    assert got["capped"] is False
    assert got["reason"]                     # 越界必须留一句能审计的话


def test_no_budget_means_no_cap():
    """还没有台账/群清单时预算算不出来，这时不该凭空拦人。"""
    got = resolve_speaker_cap(requested=0, budget=0)
    assert got["effective"] == 0 and got["source"] == "none"
    got = resolve_speaker_cap(requested=4, budget=0)
    assert got["effective"] == 4 and got["capped"] is False


def test_cap_resolution_shrugs_off_garbage():
    got = resolve_speaker_cap(requested="x", budget=None)
    assert got["effective"] == 0 and got["capped"] is False
    assert resolve_speaker_cap(requested=-3, budget=-9)["effective"] == 0


def test_budget_feeds_the_cap_end_to_end():
    """10 个号铺 90 个群 → 预算 2 人 → 没人指定时本场就自动限到 2。

    多铺 10 个群（100）连 2 人都撑不住、预算掉到独白——这个悬崖正是护栏要替运营记住的事，
    所以两边都钉一下。
    """
    assert speaker_budget(pool=10, groups=90)["max_speakers"] == 2
    assert resolve_speaker_cap(budget=2)["effective"] == 2

    tight = speaker_budget(pool=10, groups=100)
    assert tight["max_speakers"] == SOLO_SPEAKERS and tight["solo_only"] is True
    assert resolve_speaker_cap(requested=2, budget=1)["effective"] == 1


# ── 闸门落到选角上 ──────────────────────────────────────────────────────────


def test_casting_refuses_to_pair_two_accounts_that_are_already_at_the_line():
    """a00 与 a01 已经在三个群同台过：即便他们排序最优，也不能再一起上。"""
    counts = {("a00", "a01"): NORMAL_PAIR_CO}
    cast = cast_roles(_playbook(("advocate", "asker")), _candidates(3),
                      co_performance=counts)
    ids = {m.account_id for m in cast.members}
    assert len(ids) == 2
    assert ids != {"a00", "a01"}


def test_casting_leaves_the_slot_empty_rather_than_forcing_a_saturated_pair():
    """所有候选都跟场上的人到线了 → 宁可空槽降级演，也不硬凑。"""
    counts = {(x, y): NORMAL_PAIR_CO
              for x in ("a00", "a01") for y in ("a00", "a01") if x < y}
    cast = cast_roles(_playbook(("advocate", "asker")), _candidates(2),
                      co_performance=counts)
    assert len(cast.members) == 1            # 独白，但不是空场
    assert cast.unfilled == ("asker",)


def test_the_gate_is_default_on_yet_changes_nothing_without_a_ledger():
    """默认开着护栏不能以改动既有行为为代价——没台账时必须与关掉它逐字节一致。"""
    pb, cands = _playbook(), _candidates(6)
    assert cast_roles(pb, cands) == cast_roles(pb, cands, co_limit=0)


def test_the_gate_can_be_turned_off_for_a_deliberate_over_budget_run():
    counts = {("a00", "a01"): 99}
    off = cast_roles(_playbook(("advocate", "asker")), _candidates(2),
                     co_performance=counts, co_limit=0)
    assert {m.account_id for m in off.members} == {"a00", "a01"}


def test_gate_and_cap_compose_without_fighting_each_other():
    counts = {("a00", "a01"): NORMAL_PAIR_CO}
    cast = cast_roles(_playbook(), _candidates(6),
                      co_performance=counts, max_speakers=2)
    ids = {m.account_id for m in cast.members}
    assert len(ids) == 2 and ids != {"a00", "a01"}
    assert len(cast.unfilled) == 2           # 被人数上限压掉的两个槽如实进 unfilled


def test_the_lead_role_rotates_across_groups():
    """第一个槽选人时场上还没人、同台历史全为 0，排序全落在轮换键上。键里不带场次的话
    「谁演主推」跨群恒定——内容指纹层面刺眼，还会把号对共现全压在同一个号身上。"""
    pb, cands = _playbook(("advocate", "asker")), _candidates(8)
    leads = {cast_roles(pb, cands, seed=f"grp{i}").members[0].account_id
             for i in range(20)}
    assert len(leads) > 1, "20 个群全是同一个号演主推，场次扰动没生效"


def test_the_same_group_still_casts_identically():
    """扰动不能牺牲确定性：同一个群重跑必须逐字节一致（回放/续演都靠这条）。"""
    pb, cands = _playbook(), _candidates(6)
    assert cast_roles(pb, cands, seed="g1") == cast_roles(pb, cands, seed="g1")


def test_rotation_seed_multiplies_the_safe_show_count():
    """量化这条修复的价值：8 个号 / 每场 2 人 / 号对上限 3，理论容量 C(8,2)×3 = 84 场。

    不带场次扰动时只有 7 个号对会被用到（主推恒定），84 的容量只能兑现 21 场；
    带上之后 28 个号对全部铺开，兑现到八成以上。同样的号池，安全产能 ~4 倍。

    下限刻意留松（不钉具体场次）：贪心走哪条路径取决于 crc32 落点，换个剧本 id 或
    账号命名就会在 79~82 之间摆动。这条要守的是「量级」，不是某个哈希路径的产物。
    """
    def run(seeded: bool) -> Tuple[int, int]:
        counts: dict = {}
        cands, pb = _candidates(8), _playbook(("advocate", "asker"))
        done = 0
        for i in range(200):
            cast = cast_roles(pb, cands, max_speakers=2, co_performance=counts,
                              seed=f"grp{i}" if seeded else "")
            ids = sorted(m.account_id for m in cast.members)
            if len(ids) < 2:
                break
            counts[(ids[0], ids[1])] = counts.get((ids[0], ids[1]), 0) + 1
            done += 1
        return done, len(counts)

    flat_shows, flat_pairs = run(False)
    spun_shows, spun_pairs = run(True)
    assert flat_pairs <= 8 and spun_pairs == 28      # 7 个号对 → 全部 28 个
    assert spun_shows >= 3 * flat_shows, (flat_shows, spun_shows)
    assert spun_shows >= 70                          # 理论上限 84，兑现八成以上


def test_gate_keeps_the_worst_pair_off_the_line_across_a_long_run():
    """端到端：连演 200 场、每场 2 人，闸门必须让**最坏那一对**始终不越线。

    这正是人数上限管不到的地方——2 人上限从头到尾都满足，可如果每次都挑同一对，
    那一对照样能刷到 200。
    """
    accounts, counts = _candidates(8), {}
    for i in range(200):
        cast = cast_roles(_playbook(("advocate", "asker")), accounts,
                          co_performance=counts)
        ids = sorted(m.account_id for m in cast.members)
        if len(ids) < 2:
            break                            # 全员到线：闸门宁可停演也不越线
        counts[(ids[0], ids[1])] = counts.get((ids[0], ids[1]), 0) + 1
    assert counts, "一场都没排出来，闸门过严"
    assert max(counts.values()) <= NORMAL_PAIR_CO, counts
