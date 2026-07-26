"""导演循环门禁 —— 五方法接口的决策语义与四处「像不像真人」的关键设计。

导演是纯决策层：不发消息、不落库、不看时钟（时间一律由 ``now=`` 注入）。所以它
100% 可测，也**必须**被测死——它错了不会报错，只会让戏演得像机器人：自己接自己
的话、真人问了没人理、越聊越像广告、真人已经热聊起来了还在念稿。

本文件按四类不变量组织：

1. **让路与退场**（``should_yield_to_human`` / ``should_terminate``）——判错的
   代价最大：真人接管本该是绿色成就，继续念稿就是当着真人的面演戏；
2. **选人三层决策**（响应式覆盖 > 自我接话规避 > 刷屏闸）——顺序即优先级；
3. **软广衰减**——含 2026-07-25 实测事故的回归钉：``soft=0`` 的开场拍被地板抬成 1；
4. **记账推进**（``advance_beat``）——出队、游标、状态机、清真人待回应。

时间全部注入固定时间戳，测试里没有任何 ``sleep``。
"""
from __future__ import annotations

import pytest

from src.companion.group_show.director import DirectorConfig, GroupShowDirector
from src.companion.group_show.playbook import (
    DEFAULT_MUST_NOT,
    Beat,
    CastMember,
    Casting,
    Playbook,
    ShowEvent,
    ShowState,
)

T0 = 1_700_000_000.0  # 固定起点，全文件共用（绝不 time.time()）


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _playbook(beats, *, soft_ad_level=5, products=("matrixx",)) -> Playbook:
    return Playbook(id="pb", name="测试戏", products=products,
                    soft_ad_level=soft_ad_level, beats=tuple(beats))


def _full_cast() -> Casting:
    return Casting(members=(
        CastMember("asker", "a_ask", "p_ask"),
        CastMember("advocate", "a_adv", "p_adv"),
        CastMember("skeptic", "a_skp", "p_skp"),
        CastMember("bystander", "a_bys", "p_bys"),
    ))


def _state(beats=None, casting=None, **kw) -> ShowState:
    beats = beats if beats is not None else [
        Beat("b1", "asker", "抛痛点"),
        Beat("b2", "advocate", "种草", product="matrixx"),
        Beat("b3", "skeptic", "泼冷水"),
        Beat("b4", "bystander", "附和"),
    ]
    base = dict(session_id="s1", group_key="g1", playbook=_playbook(beats),
                casting=casting if casting is not None else _full_cast(),
                started_at=T0)
    base.update(kw)
    return ShowState(**base)


def _director(**kw) -> GroupShowDirector:
    config = kw.pop("config", None)
    return GroupShowDirector(_state(**kw), config)


def _say(director, account, beat_id, text="说了点什么", ts=T0, kind="line"):
    """让某个号发一拍（走 ``advance_beat``，即真实的记账路径）。"""
    director.advance_beat(ShowEvent(
        seq=director.state.next_seq, ts=ts, speaker_account=account,
        role="", beat_id=beat_id, text=text, kind=kind))


# ── DirectorConfig：脏配置一律回落缺省 ──────────────────────────────────────


def test_config_from_none_gives_the_conservative_defaults():
    """配置缺失是常态（新部署没写这段），缺省值必须是能直接上线的保守档。"""
    cfg = DirectorConfig.from_config(None)
    assert cfg.human_yield_seconds == 45.0
    assert cfg.human_takeover_lines == 3
    assert cfg.max_lines == 40
    assert cfg.soft_floor == 1


@pytest.mark.parametrize("junk", ["abc", None, [], {}, object()])
def test_dirty_config_values_fall_back_instead_of_crashing_the_show(junk):
    """一个写错的 YAML 值不能让整场戏开不起来——静默回落缺省是正确取舍。"""
    cfg = DirectorConfig.from_config({
        "human_yield_seconds": junk, "human_takeover_lines": junk,
        "max_lines": junk, "max_share_per_account": junk,
        "soft_decay_per_mention": junk, "soft_floor": junk,
        "max_duration_seconds": junk,
    })
    assert cfg == DirectorConfig.from_config(None)


def test_partial_config_only_overrides_what_it_mentions():
    cfg = DirectorConfig.from_config({"max_lines": 5})
    assert cfg.max_lines == 5 and cfg.human_takeover_lines == 3


def test_numeric_strings_are_accepted_because_yaml_quotes_happen():
    assert DirectorConfig.from_config({"max_lines": "7"}).max_lines == 7


# ── 让路窗 ──────────────────────────────────────────────────────────────────


def test_no_human_yet_means_nothing_to_yield_to():
    """开场没有真人说过话，不该凭空进入让路状态（那会让戏永远开不了口）。"""
    assert _director().should_yield_to_human(now=T0) is False


def test_inside_the_window_the_bots_shut_up():
    """真人发言后秒回是最刺眼的机械特征——何况人家可能在跟别人说话。"""
    d = _director()
    d.observe_human("你们在聊啥", ts=T0)
    assert d.should_yield_to_human(now=T0 + 1) is True
    assert d.should_yield_to_human(now=T0 + 44.9) is True


def test_the_window_does_expire_so_the_show_can_resume():
    """让路是「等一会儿」不是「永久闭嘴」——窗过后要能继续演，否则戏就烂尾了。"""
    d = _director()
    d.observe_human("你们在聊啥", ts=T0)
    assert d.should_yield_to_human(now=T0 + 45.0) is False
    assert d.should_yield_to_human(now=T0 + 999) is False


def test_yield_window_length_is_configurable():
    d = _director(config={"human_yield_seconds": 10.0})
    d.observe_human("hi", ts=T0)
    assert d.should_yield_to_human(now=T0 + 9) is True
    assert d.should_yield_to_human(now=T0 + 11) is False


def test_observe_human_records_the_line_into_the_event_stream():
    """真人原话要进事件流：既是 ECP 的上下文素材，也是复盘时看「戏是被谁打断的」。"""
    d = _director()
    d.observe_human("这个多少钱", sender="小王", ts=T0 + 5)
    ev = d.state.last_event()
    assert ev.kind == "human" and ev.text == "这个多少钱"
    assert ev.speaker_account == "小王" and ev.ts == T0 + 5
    assert d.state.last_human_ts == T0 + 5


def test_an_empty_human_message_still_yields_but_leaves_no_reply_target():
    """真人发了个表情/图（无文本）：仍要让路，但没有「原话」可回应，不该编一句出来。"""
    d = _director()
    d.observe_human("真的假的？", ts=T0)
    d.observe_human("   ", ts=T0 + 1)
    assert d.should_yield_to_human(now=T0 + 2) is True
    assert d.next_directive().respond_to_human.endswith("真的假的？"), (
        "空消息不该覆盖掉上一句真正需要回应的原话")


# ── 收场判定 ────────────────────────────────────────────────────────────────


def test_human_takeover_is_the_best_ending_and_outranks_everything_else():
    """**真人自己聊起来了就该退场**——戏的目的就是把场子点着，点着了还念稿最蠢。

    这个 reason 在看板上应该是绿色成就。它排在 budget/timeout 之前，是因为哪怕
    预算也用完了，归因上也该记成「人接管了」而不是「我们发满了」。
    """
    d = _director(config={"human_takeover_lines": 3})
    for i in range(3):
        d.observe_human(f"真人第{i}句", ts=T0 + i)
    assert d.should_terminate(now=T0 + 10) == (True, "human_takeover")


def test_our_own_line_resets_the_takeover_streak():
    """「连续」真人发言才算接管——中间被我们的号插了一句就重新数。"""
    d = _director(config={"human_takeover_lines": 3})
    d.observe_human("一", ts=T0)
    d.observe_human("二", ts=T0 + 1)
    _say(d, "a_ask", "b1", ts=T0 + 2)
    d.observe_human("三", ts=T0 + 3)
    assert d.should_terminate(now=T0 + 4)[0] is False


def test_budget_cap_stops_a_runaway_show():
    """条数预算是防失控刷屏的闸——它算的是真发出去的 line/media，不含真人发言。"""
    d = _director(config={"max_lines": 2, "human_takeover_lines": 99})
    _say(d, "a_ask", "b1", ts=T0)
    assert d.should_terminate(now=T0 + 1)[0] is False
    _say(d, "a_adv", "b2", ts=T0 + 1)
    assert d.should_terminate(now=T0 + 2) == (True, "budget")


def test_timeout_ends_a_show_that_dragged_on():
    """一场戏铺开可能一两个小时，超时兜底防「演到天亮」。"""
    d = _director(config={"max_duration_seconds": 100.0})
    assert d.should_terminate(now=T0 + 99)[0] is False
    assert d.should_terminate(now=T0 + 101) == (True, "timeout")


def test_empty_queue_means_completed():
    """所有拍演完＝正常收场。这也是空剧本的结局（不会卡在原地空转）。"""
    d = _director(beats=[])
    assert d.should_terminate(now=T0) == (True, "completed")


def test_aborted_status_outranks_every_other_reason():
    """外部（Kill-Switch / 运营）把状态置 aborted 时，导演必须立刻认账。"""
    d = _director()
    d.state.status = "aborted"
    assert d.should_terminate(now=T0) == (True, "aborted")


def test_a_healthy_show_in_progress_reports_no_reason():
    d = _director()
    assert d.should_terminate(now=T0 + 1) == (False, "")


# ── 选人：基础与降级 ────────────────────────────────────────────────────────


def test_first_beat_goes_to_the_slot_the_playbook_asked_for():
    assert _director().select_next_speaker().slot == "asker"


def test_no_beats_left_means_nobody_speaks():
    assert _director(beats=[]).select_next_speaker() is None


def test_an_empty_cast_means_nobody_speaks():
    """选角全空（号全在冷却）→ 返回 None 让 runtime 判「这场不演」，而不是抛。"""
    assert _director(casting=Casting()).select_next_speaker() is None


def test_a_beat_whose_slot_was_never_cast_falls_back_to_a_substitute():
    """号不够时的降级演出：这一拍宁可让别人顶，也不能整拍消失让弧线断掉。"""
    cast = Casting(members=(CastMember("advocate", "a_adv", "p_adv"),))
    d = _director(beats=[Beat("b1", "skeptic", "泼冷水")], casting=cast)
    assert d.select_next_speaker().account_id == "a_adv"


def test_substitute_prefers_the_most_capable_role_available():
    """替补顺序 advocate > asker > skeptic > bystander：懂产品的人顶最不容易穿帮。"""
    cast = Casting(members=(CastMember("bystander", "a_bys", "p1"),
                            CastMember("advocate", "a_adv", "p2")))
    d = _director(beats=[Beat("b1", "ghost", "某意图")], casting=cast)
    assert d.select_next_speaker().account_id == "a_adv"


def test_substitute_falls_back_to_whoever_is_left():
    """连标准四角都没有（全是自定义槽）时，随便谁上也比整拍失踪强。"""
    cast = Casting(members=(CastMember("custom", "a_x", "p"),))
    d = _director(beats=[Beat("b1", "ghost", "i")], casting=cast)
    assert d.select_next_speaker().account_id == "a_x"


# ── 选人①：真人提问的响应式覆盖 ────────────────────────────────────────────


@pytest.mark.parametrize("question", [
    "这个会不会封号啊?", "多少钱？", "怎么用的", "靠谱吗", "能不能试用",
    "有没有人用过", "价格贵不贵", "安全吗",
])
def test_a_human_question_hands_the_mic_to_someone_who_can_answer(question):
    """真人问了产品/疑问 → 让 advocate 接，而不是机械念下一拍。

    让 bystander 去答技术疑问是典型穿帮点：附和角突然懂参数，一眼看出是分配好的。
    """
    d = _director()
    d.observe_human(question, ts=T0)
    assert d.select_next_speaker().slot == "advocate"


def test_a_plain_human_remark_does_not_hijack_the_script():
    """真人只是闲聊一句（没问什么）→ 照常按剧本走，不必每句话都抢着响应。

    过度响应同样是机械特征：真群里没人对每一句「哈哈」都正经接话。
    """
    d = _director()
    d.observe_human("哈哈哈哈", ts=T0)
    assert d.select_next_speaker().slot == "asker"


def test_the_responder_is_never_the_account_that_just_spoke():
    """同一个号连着回两条＝抢答，宁可换个人接。

    真人插话会追加一条 ``kind="human"`` 事件顶在最后，所以「上一个发言者」必须**回溯**
    最近一条 line/media，只看末条会让这条护栏恒不生效（2026-07-25 修复的死代码）。
    """
    d = _director()
    _say(d, "a_adv", "b1", ts=T0)
    d.observe_human("这个多少钱？", ts=T0 + 1)
    picked = d.select_next_speaker()
    assert picked is not None and picked.account_id != "a_adv"


def test_self_reply_guard_survives_a_human_interjection():
    """真人插一句非提问后，自我接话规避仍须生效。

    与上面同一个根因：护栏若只看末条事件，真人一发言就集体失灵——而真人刚说完话
    正是最该防「同一个号自己接自己」的时刻。
    """
    d = _director()
    _say(d, "a_adv", "b1", ts=T0)
    d.observe_human("哈哈哈哈", ts=T0 + 1)          # 非提问：不触发响应式覆盖
    picked = d.select_next_speaker()
    assert picked is not None and picked.account_id != "a_adv"


def test_responsive_pick_walks_down_to_whoever_is_available():
    """只剩 asker 时也得有人接——「没人理真人」比「回答的人不够专业」糟糕得多。"""
    cast = Casting(members=(CastMember("asker", "a_ask", "p"),))
    d = _director(casting=cast)
    d.observe_human("这个靠谱吗？", ts=T0)
    assert d.select_next_speaker().account_id == "a_ask"


def test_the_pending_human_line_is_cleared_once_someone_answered():
    """回应过就不该反复回应同一句话（那会变成对着一句话说三遍）。"""
    d = _director()
    d.observe_human("多少钱？", ts=T0)
    assert d.next_directive().respond_to_human != ""
    _say(d, "a_adv", "b1", ts=T0 + 60)
    assert d.next_directive().respond_to_human == ""


# ── 选人②：自我接话规避 ────────────────────────────────────────────────────


def test_the_queue_is_reordered_so_nobody_answers_themselves():
    """真人群里几乎没人自己接自己的话——队头角色恰是上一个发言者时要重排。

    这正是「拍队列」而非「游标」的存在理由：游标模型只能顺序播，做不到临场插队。
    """
    beats = [Beat("b1", "asker", "i1"), Beat("b2", "asker", "i2"),
             Beat("b3", "advocate", "i3")]
    d = _director(beats=beats)
    _say(d, "a_ask", "b1", ts=T0)
    assert d.select_next_speaker().account_id == "a_adv"


def test_reordering_never_drops_a_beat():
    """重排是插队不是丢拍——被推后的那一拍必须还在队列里等着演。"""
    beats = [Beat("b1", "asker", "i1"), Beat("b2", "asker", "i2"),
             Beat("b3", "advocate", "i3")]
    d = _director(beats=beats)
    _say(d, "a_ask", "b1", ts=T0)
    d.select_next_speaker()
    assert {b.id for b in d._queue} == {"b2", "b3"}


def test_no_reorder_when_there_is_nobody_else_to_swap_in():
    """只有一个演员时无从规避，宁可让他接自己也不能卡死不发（戏会烂尾）。"""
    cast = Casting(members=(CastMember("asker", "a_ask", "p"),))
    beats = [Beat("b1", "asker", "i1"), Beat("b2", "asker", "i2")]
    d = _director(beats=beats, casting=cast)
    _say(d, "a_ask", "b1", ts=T0)
    assert d.select_next_speaker().account_id == "a_ask"


def test_no_reorder_when_the_last_event_was_a_human_line():
    """真人刚说完话，我们的号接话不算「自己接自己」，不该触发重排。"""
    beats = [Beat("b1", "asker", "i1"), Beat("b2", "advocate", "i2")]
    d = _director(beats=beats)
    d.observe_human("嗯嗯", ts=T0)
    assert d.select_next_speaker().slot == "asker"


# ── 选人③：刷屏闸 ──────────────────────────────────────────────────────────


def test_share_gate_stays_out_of_the_way_while_the_sample_is_tiny():
    """前几条谈占比没意义（第一条必然是 100%），过早触发只会把开场搅乱。"""
    beats = [Beat(f"b{i}", "asker", "i") for i in range(6)]
    d = _director(beats=beats, config={"max_share_per_account": 0.45})
    _say(d, "a_ask", "b0", ts=T0)
    _say(d, "a_ask", "b1", ts=T0 + 1)
    assert d.select_next_speaker().account_id == "a_ask"


def test_share_gate_swaps_in_the_quietest_account_when_one_号_hogs_the_room():
    """单号占比超阈值 → 本拍换人，换给发言最少的那个（把话筒真正分出去）。"""
    beats = [Beat(f"b{i}", "asker", "i") for i in range(8)]
    d = _director(beats=beats, config={"max_share_per_account": 0.45})
    for i in range(4):
        _say(d, "a_ask", f"b{i}", ts=T0 + i)
    picked = d.select_next_speaker()
    assert picked is not None and picked.account_id != "a_ask"


def test_share_gate_gives_up_gracefully_when_there_is_no_one_else():
    """只有一个演员时刷屏闸无处可换——必须让他继续说，不能返回 None 把戏卡死。"""
    cast = Casting(members=(CastMember("asker", "a_ask", "p"),))
    beats = [Beat(f"b{i}", "asker", "i") for i in range(8)]
    d = _director(beats=beats, casting=cast, config={"max_share_per_account": 0.1})
    for i in range(4):
        _say(d, "a_ask", f"b{i}", ts=T0 + i)
    assert d.select_next_speaker().account_id == "a_ask"


# ── directive：意图而非台词 ────────────────────────────────────────────────


def test_a_directive_carries_intent_and_never_a_line_of_dialogue():
    """**本子系统的根基**：导演只下意图，台词由人设 LLM 现场生成。

    directive 上一旦长出「台词」字段，同一剧本每次演出都不同的反模板化设计就没了。
    """
    d = _director()
    directive = d.next_directive()
    assert directive.intent == "抛痛点"
    assert not hasattr(directive, "text") and not hasattr(directive, "line")


def test_every_directive_ships_with_the_ban_list():
    """禁止项离生成最近、权重最高，任何一拍漏带都会让那一拍裸奔。"""
    assert _director().next_directive().must_not == DEFAULT_MUST_NOT


def test_no_directive_when_there_is_nothing_left_to_play():
    assert _director(beats=[]).next_directive() is None


def test_the_opening_line_has_nothing_to_reference():
    """第一拍无从承接——要求它「顺着上一条说」会逼 LLM 编造一个不存在的上文。"""
    assert _director().next_directive().reference_last is False


def test_later_beats_reference_the_previous_line_for_continuity():
    d = _director()
    _say(d, "a_ask", "b1", ts=T0)
    assert d.next_directive().reference_last is True


def test_a_pending_human_line_outranks_continuity():
    """真人问了却继续顺着上一条自说自话，比不说话更假——承接让位于回应。"""
    d = _director()
    _say(d, "a_ask", "b1", ts=T0)
    d.observe_human("这个多少钱？", ts=T0 + 1)
    directive = d.next_directive()
    assert directive.reference_last is False
    assert "多少钱" in directive.respond_to_human


def test_the_human_quote_carries_the_speaker_name():
    """回应时要能叫出名字（"小王说的那个"），否则回应会显得对空气说话。"""
    d = _director()
    d.observe_human("你们都在用啥", sender="小王", ts=T0)
    assert d.next_directive().respond_to_human.startswith("小王：")


def test_an_anonymous_human_is_called_群友():
    """解析不出昵称时用中性称呼，绝不把 account_id 或空名字塞进 prompt。"""
    d = _director()
    d.observe_human("你们都在用啥", ts=T0)
    assert d.next_directive().respond_to_human.startswith("群友：")


def test_directive_carries_product_and_media_from_the_beat():
    d = _director()
    _say(d, "a_ask", "b1", ts=T0)
    directive = d.next_directive()
    assert directive.beat_id == "b2" and directive.product == "matrixx"


# ── 软广衰减 ────────────────────────────────────────────────────────────────


def test_soft_level_decays_as_the_show_keeps_mentioning_the_product():
    """「越聊越像广告」是群戏第二常见的翻车方式（第一是台词模板化）。

    每发生一拍带产品的发言，后续强度自动降一档——这条衰减是自动的、不需要剧本
    作者手工递减，因为手工递减必然被忘掉。
    """
    beats = [Beat("b1", "asker", "i", product="matrixx", soft=8),
             Beat("b2", "advocate", "i", product="matrixx", soft=8),
             Beat("b3", "skeptic", "i", product="matrixx", soft=8)]
    d = _director(beats=beats, config={"soft_decay_per_mention": 2})
    assert d.next_directive().soft_level == 8
    _say(d, "a_ask", "b1", ts=T0)
    assert d.next_directive().soft_level == 6
    _say(d, "a_adv", "b2", ts=T0 + 1)
    assert d.next_directive().soft_level == 4


def test_beats_without_a_product_do_not_trigger_decay():
    """衰减的计数口径是「带产品的拍」，纯闲聊拍不该消耗种草额度。"""
    beats = [Beat("b1", "asker", "i", soft=8), Beat("b2", "advocate", "i", soft=8)]
    d = _director(beats=beats)
    _say(d, "a_ask", "b1", ts=T0)
    assert d.next_directive().soft_level == 8


def test_decay_never_falls_below_the_floor():
    """地板的本意是「别把种草拍衰减成哑巴」——一场长戏不该把 advocate 减到失声。"""
    beats = [Beat(f"b{i}", "advocate", "i", product="matrixx", soft=3)
             for i in range(6)]
    d = _director(beats=beats, config={"soft_decay_per_mention": 5, "soft_floor": 2})
    for i in range(4):
        _say(d, "a_adv", f"b{i}", ts=T0 + i)
    assert d.next_directive().soft_level == 2


def test_an_explicit_soft_zero_beat_is_never_lifted_to_the_floor():
    """**2026-07-25 首场真 LLM 排练的事故回归钉**：开场拍被标成 soft=1。

    剧本写 0 是「这拍纯闲聊，一个字产品都不许提」的硬意图（所有模板的开场拍都是
    0——一上来就推＝广告）。地板只该托住被衰减压低的种草拍，把明写 0 的拍抬到 1
    是越权，直接后果是每一场戏的第一句话都带上了推销味。
    """
    beats = [Beat("b1", "asker", "纯闲聊开场", soft=0),
             Beat("b2", "advocate", "种草", product="matrixx", soft=6)]
    d = _director(beats=beats, config={"soft_floor": 3})
    assert d.next_directive().soft_level == 0


def test_a_beat_without_its_own_soft_inherits_the_playbook_level():
    beats = [Beat("b1", "asker", "i")]
    state = _state(beats=beats)
    state.playbook = _playbook(beats, soft_ad_level=9)
    assert GroupShowDirector(state).next_directive().soft_level == 9


# ── advance_beat：记账与推进 ───────────────────────────────────────────────


def test_advancing_dequeues_the_beat_and_moves_the_cursor():
    """游标是续演的落点：进程重启后从这里接着演，算错就会重演或跳拍。"""
    d = _director()
    _say(d, "a_ask", "b1", ts=T0)
    assert d.state.beat_cursor == 1
    assert [b.id for b in d._queue] == ["b2", "b3", "b4"]


def test_the_first_line_flips_the_show_from_pending_to_running():
    d = _director()
    assert d.state.status == "pending"
    _say(d, "a_ask", "b1", ts=T0)
    assert d.state.status == "running"


def test_a_beat_consumed_out_of_order_is_dequeued_by_id():
    """响应式覆盖会消费队列中间的拍——必须按 id 精确出队，否则那一拍会被演两次。"""
    d = _director()
    _say(d, "a_skp", "b3", ts=T0)
    assert [b.id for b in d._queue] == ["b1", "b2", "b4"]


def test_advancing_clears_the_pending_human_and_the_takeover_streak():
    """我们的号一开口，「连续真人发言」就该归零、待回应原话就该销账。"""
    d = _director()
    d.observe_human("多少钱？", ts=T0)
    d.observe_human("在吗", ts=T0 + 1)
    _say(d, "a_adv", "b1", ts=T0 + 60)
    assert d.should_terminate(now=T0 + 61)[0] is False
    assert d.next_directive().respond_to_human == ""


def test_the_event_lands_in_the_stream_for_ecp_and_scoring():
    """事件流是 ECP 上下文与自然度评测的唯一素材，记账即入流。"""
    d = _director()
    _say(d, "a_ask", "b1", text="我先说一句", ts=T0)
    assert [e.text for e in d.state.events] == ["我先说一句"]


def test_resuming_mid_show_only_queues_the_remaining_beats():
    """断点续演：从库里恢复 ``beat_cursor=2`` 后只该演剩下的拍，不能从头重演。

    「同一批号在同一个群把同一个话题又演一遍」是最刺眼的机器特征。
    """
    d = GroupShowDirector(_state(beat_cursor=2))
    assert [b.id for b in d._queue] == ["b3", "b4"]
    assert d.select_next_speaker().slot == "skeptic"


def test_a_cursor_past_the_end_resumes_into_a_finished_show():
    """脏游标（超出 beats 长度）应落到「演完了」，而不是切片异常或无限空转。"""
    d = GroupShowDirector(_state(beat_cursor=99))
    assert d.should_terminate(now=T0)[0] is True


# ── 调用顺序：隐式契约 ──────────────────────────────────────────────────────


def test_next_directive_must_be_called_after_select_next_speaker():
    """**这是一条会咬人的隐式契约**：``select_next_speaker`` 会重排队列，因此
    ``next_directive`` 必须在它之后调，否则拿到的是重排前那一拍的意图。

    五方法接口的文档顺序（① 选人 ② 给意图）已经暗示了这个次序，但代码层面没有
    任何强制。这里把现状钉住：调换顺序会让「说话的人」和「他要表达的意图」对不上
    ——advocate 顶着 asker 的意图开口，是很难从日志里看出来的那种错。
    """
    beats = [Beat("b1", "asker", "i1"), Beat("b2", "asker", "i2"),
             Beat("b3", "advocate", "i3")]
    d = _director(beats=beats)
    _say(d, "a_ask", "b1", ts=T0)

    stale = d.next_directive().beat_id          # 错误顺序：重排还没发生
    speaker = d.select_next_speaker()
    fresh = d.next_directive().beat_id          # 正确顺序

    assert stale == "b2" and fresh == "b3"
    assert speaker.slot == "advocate", "重排后开口的是 advocate，意图也应换成 b3"


def test_two_directors_over_the_same_inputs_decide_identically():
    """确定性：导演不掷骰子。同样的状态与配置必须给出同样的决策。

    否则 dry-run 排练出来的这场戏，真发时会演成另一场——排练也就失去了意义。
    """
    def run():
        d = _director()
        trace = []
        for _ in range(4):
            speaker = d.select_next_speaker()
            directive = d.next_directive()
            if speaker is None or directive is None:
                break
            trace.append((speaker.account_id, directive.beat_id, directive.soft_level))
            _say(d, speaker.account_id, directive.beat_id, ts=T0)
        return trace

    assert run() == run()
