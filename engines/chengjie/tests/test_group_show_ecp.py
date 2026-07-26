# -*- coding: utf-8 -*-
"""ECP 自我视角投影门禁 —— 群戏「反串味」的结构层防线。

这个文件守的是一条比功能更重要的东西：**角色不知道群里还有同伙**。投影一旦把
「我们的号」和「真实群友」区别对待（哪怕只是多一个标记），角色就会开始互相捧哏、
抢答、称呼对方昵称——那正是群控最典型的识别特征，代价是整批账号。

另一条同等级别的是账号隔离墙：本模块只吃群内公开事件流，不吃任何 1:1 私聊记忆。
这条靠**签名与源码里根本没有那个入口**来保证，所以下面有一条架构级钉子而不是行为断言。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from src.companion.group_show.ecp import (
    DEFAULT_MAX_TURNS,
    SPEAKER_PREFIX_LABEL,
    UNKNOWN_SPEAKER_NAME,
    format_speaker_line,
    project_history_for,
)
from src.companion.group_show.playbook import (
    DEFAULT_MUST_NOT,
    BeatDirective,
    CastMember,
    ShowEvent,
)

_ECP_SRC = (Path(__file__).resolve().parents[1] / "src" / "companion"
            / "group_show" / "ecp.py")


def _member(account: str = "me", name: str = "小美") -> CastMember:
    return CastMember(slot="advocate", account_id=account, persona_id="p1",
                      display_name=name)


def _directive(**kw) -> BeatDirective:
    base = {"beat_id": "b1", "intent": "抛出多号切换的痛点"}
    base.update(kw)
    return BeatDirective(**base)


def _ev(seq: int, who: str, text: str, kind: str = "line") -> ShowEvent:
    return ShowEvent(seq=seq, ts=float(seq), speaker_account=who,
                     role="advocate", beat_id="b1", text=text, kind=kind)


def _bodies(messages):
    """剥掉 [群友 X] 前缀，取回每条消息的正文（用于溯源断言）。"""
    return [m["content"].split("] ", 1)[-1] if m["content"].startswith("[")
            else m["content"] for m in messages]


# ── 账号隔离墙（捅破即事故） ────────────────────────────────────────────────


def test_projection_has_no_private_memory_channel_at_all():
    """签名与源码里都不存在「私聊/记忆」入口——想传都传不进来。

    这条不是行为测试而是架构钉：只要入口存在，某次「顺手把 episodic memory 也带上，
    角色会更有记忆感」的优化就能让角色在群里说出别人的私聊内容。对被点名的用户是隐私
    事故，对其余群友是「这几个号互相认识」的铁证。
    """
    params = set(inspect.signature(project_history_for).parameters)
    assert params == {"speaker", "history", "directive", "group_hint",
                      "name_resolver", "max_turns"}, (
        f"投影函数多出/少了入参，请确认没有引入第二类上下文来源：{sorted(params)}")

    src = _ECP_SRC.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    for forbidden in ("episodic", "EpisodicMemory", "context_store",
                      "ContextStore", "get_bullets_for_prompt", "user_context",
                      "conversation_meta"):
        assert forbidden not in code, f"ECP 不得引用私聊记忆通道: {forbidden}"


def test_projection_cannot_tell_our_own_accounts_from_real_humans():
    """我们的其他号与真实群友，在当前 speaker 眼里必须完全同形。

    投影函数连 casting 都拿不到，因此**结构上无法**区分——这是刻意的。角色一旦认出
    同伙就会配合演戏（互相捧哏、抢答），那是群控最容易被抓的形状。
    """
    teammate = _ev(1, "our_other_bot", "这个我一直在用")
    stranger = _ev(2, "real_person_998", "这个我一直在用")

    msgs = project_history_for(_member(), [teammate, stranger],
                               directive=_directive())
    shaped = msgs[1:]
    assert [m["role"] for m in shaped] == ["user", "user"]
    # 除了发言人名字，两条必须逐字节同形
    assert shaped[0]["content"].replace("our_other_bot", "X") == \
           shaped[1]["content"].replace("real_person_998", "X")


def test_projected_content_is_fully_traceable_to_the_inputs():
    """输出正文只能来自 history 原文——不存在第四种内容来源。

    「凭空多出一句」在群里就是角色开始编造共同经历。这条把「无中生有」钉死在结构层，
    比事后靠 must_not 文本兜底可靠。
    """
    history = [_ev(1, "other", "切号切到崩溃"), _ev(2, "me", "我统一挂后台了")]
    msgs = project_history_for(_member(), history, directive=_directive(),
                               group_hint="这是一个跨境电商交流群")
    assert set(_bodies(msgs[1:])) == {"切号切到崩溃", "我统一挂后台了"}


def test_directive_intent_never_leaks_into_the_dialogue_stream():
    """导演意图只能进 system，绝不能混进对话历史。

    意图混进 user/assistant 流 ＝ 角色以为「有人刚才说过这句剧本指令」，轻则复读，
    重则把导演口吻当群友发言接下去。
    """
    intent = "抛出多号切换的痛点"
    msgs = project_history_for(
        _member(), [_ev(1, "other", "哈喽")], directive=_directive(intent=intent))
    assert intent in msgs[0]["content"]
    assert all(intent not in m["content"] for m in msgs[1:])


# ── 视角投影语义 ────────────────────────────────────────────────────────────


def test_own_lines_are_assistant_and_carry_no_prefix():
    """自己说过的话必须是 assistant 且不带 [群友] 前缀。

    带了前缀，模型就会把自己的历史发言当成「别人说的」，于是复读、或者以第三人称
    评论自己刚说过的话。
    """
    msgs = project_history_for(
        _member("me"), [_ev(1, "me", "我统一挂后台了")], directive=_directive())
    assert msgs[1] == {"role": "assistant", "content": "我统一挂后台了"}


def test_first_message_is_always_a_non_empty_system_prompt():
    """无论历史多脏多空，首条恒为非空 system——身份锚点不可缺席。"""
    for history in ([], None, [_ev(1, "x", "")], [None]):
        msgs = project_history_for(_member(), history or [],
                                   directive=_directive())
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"].strip()


def test_identity_anchor_comes_first_and_bans_come_last():
    """身份锚点在最前、禁止项在最后——离生成越近权重越高。

    顺序被「整理代码」时打乱不会报错，只会让长历史把人格冲淡、让禁止项失效，
    表现为线上慢慢开始串味，很难归因。
    """
    d = _directive(must_not=("不要自我介绍", "不要发链接"))
    content = project_history_for(_member(), [], directive=d)[0]["content"]
    assert content.index("【你是谁】") == 0
    assert content.index("【绝对不要】") > content.index("【你是谁】")
    assert content.rindex("【绝对不要】") == max(
        content.rindex(tag) for tag in ("【你是谁】", "【分寸】", "【绝对不要】"))


def test_human_interjection_is_carried_into_the_prompt():
    """真人插话原文必须进 system——当着真人的面继续念剧本是最容易被察觉的假。"""
    content = project_history_for(
        _member(), [], directive=_directive(respond_to_human="这个会不会封号啊"),
    )[0]["content"]
    assert "这个会不会封号啊" in content


# ── 退化与脏输入（不抛异常 + 保守结论） ────────────────────────────────────


def test_control_events_and_blank_text_never_enter_the_context():
    """yield/terminate 与空文本不进上下文——群里根本没有这条消息。

    塞进去会让模型以为「有人发了一句空话」，进而去追问或复读那段空白。
    """
    history = [
        _ev(1, "other", "真实发言"),
        _ev(2, "me", "导演让路", kind="yield"),
        _ev(3, "me", "收尾", kind="terminate"),
        _ev(4, "other", "   "),
        _ev(5, "other", ""),
    ]
    msgs = project_history_for(_member(), history, directive=_directive())
    assert _bodies(msgs[1:]) == ["真实发言"]


def test_truncation_happens_after_filtering_not_before():
    """截断必须发生在过滤之后，否则一串控制事件会把真对话挤出窗口。

    表现为「角色突然失忆、上下文空空」——排查时只会看到 messages 很短，看不出原因。
    """
    history = ([_ev(i, "me", "控制", kind="yield") for i in range(1, 9)]
               + [_ev(9, "other", "真话一"), _ev(10, "other", "真话二")])
    msgs = project_history_for(_member(), history, directive=_directive(),
                               max_turns=2)
    assert _bodies(msgs[1:]) == ["真话一", "真话二"]


def test_only_the_most_recent_turns_survive_the_window():
    """只保留最近 N 条，且保的是**最近**那几条（不是最早）。"""
    history = [_ev(i, "other", f"第{i}句") for i in range(1, 11)]
    msgs = project_history_for(_member(), history, directive=_directive(),
                               max_turns=3)
    assert _bodies(msgs[1:]) == ["第8句", "第9句", "第10句"]


def test_non_positive_max_turns_yields_system_only():
    """max_turns <= 0 ＝ 明确要求「不带历史」，就该只给 system，不能悄悄给默认窗口。"""
    history = [_ev(i, "other", f"第{i}句") for i in range(1, 5)]
    for bad in (0, -1, -999):
        msgs = project_history_for(_member(), history, directive=_directive(),
                                   max_turns=bad)
        assert len(msgs) == 1 and msgs[0]["role"] == "system"


def test_garbage_max_turns_degrades_to_the_default_window():
    """max_turns 传进垃圾值时安静回落默认窗口，绝不抛——排期是运行期热路径。"""
    history = [_ev(i, "other", f"第{i}句") for i in range(1, 41)]
    for bad in ("abc", None, object()):
        msgs = project_history_for(_member(), history, directive=_directive(),
                                   max_turns=bad)
        assert len(msgs) == DEFAULT_MAX_TURNS + 1


def test_long_history_is_always_bounded():
    """超长历史必须被窗口夹住——prompt 撑爆是「整场戏静默停摆」的常见根因。"""
    history = [_ev(i, "other" if i % 2 else "me", f"第{i}句")
               for i in range(1, 1001)]
    msgs = project_history_for(_member(), history, directive=_directive())
    assert len(msgs) == DEFAULT_MAX_TURNS + 1


def test_name_resolver_failure_never_breaks_the_speaking_chain():
    """名字解析器抛异常/返空，一律回落 account_id——难看好过整场戏停摆。"""
    def _boom(_a):
        raise RuntimeError("账号表挂了")

    for resolver in (_boom, lambda a: "", lambda a: None, lambda a: "   "):
        msgs = project_history_for(_member(), [_ev(1, "acc_77", "在吗")],
                                   directive=_directive(),
                                   name_resolver=resolver)
        assert msgs[1]["content"] == f"[{SPEAKER_PREFIX_LABEL} acc_77] 在吗"


def test_unknown_speaker_gets_a_vague_label_not_an_invented_name():
    """解析不出名字时用含糊称呼，绝不编一个具体的人名出来。

    编出来的名字会被角色当真去称呼，群里立刻出现一个不存在的人。
    """
    assert format_speaker_line("", "你好") == \
           f"[{SPEAKER_PREFIX_LABEL} {UNKNOWN_SPEAKER_NAME}] 你好"
    assert format_speaker_line(None, None) == \
           f"[{SPEAKER_PREFIX_LABEL} {UNKNOWN_SPEAKER_NAME}] "


def test_events_missing_fields_are_skipped_instead_of_crashing():
    """缺字段的畸形事件被跳过而不是掀翻投影——上游任何一处漏字段都不该停演。"""
    class _Bare:
        pass

    class _OnlyKind:
        kind = "line"

    msgs = project_history_for(
        _member(), [_Bare(), _OnlyKind(), _ev(1, "other", "正常一句")],
        directive=_directive())
    assert _bodies(msgs[1:]) == ["正常一句"]


def test_extreme_soft_levels_are_clamped_not_crashed():
    """软广强度越界/非数字都要给出一档口径提示，且 0 分必须是「完全不提产品」。

    这个值来自人手写 YAML，`soft: 十` 迟早会出现；抛异常＝这一拍直接哑掉。
    """
    zero = project_history_for(_member(), [],
                               directive=_directive(soft_level=0))[0]["content"]
    assert "完全不要提任何产品" in zero
    for bad in (-5, 999, None, "abc", 3.7):
        content = project_history_for(
            _member(), [], directive=_directive(soft_level=bad))[0]["content"]
        assert "【分寸】" in content


def test_blank_must_not_entries_do_not_emit_an_empty_ban_section():
    """全是空白的禁止项不该渲染出一个空的【绝对不要】块。

    空标题会让模型去猜「到底不要什么」，比不写更糟。
    """
    content = project_history_for(
        _member(), [], directive=_directive(must_not=("", "   ")))[0]["content"]
    assert "【绝对不要】" not in content


def test_speaker_without_account_id_sees_everything_as_others():
    """speaker 没有 account_id 时，宁可把自己的话也当别人说的（保守侧）。

    反过来（把所有无主发言都认成自己）会让角色认领别人的立场，那是串味事故；
    这里的保守选择最多只是少一点「我说过」的语感。
    """
    msgs = project_history_for(
        CastMember(slot="s", account_id="", persona_id="p"),
        [_ev(1, "", "无主发言")], directive=_directive())
    assert msgs[1]["role"] == "user"


# ── 确定性 ──────────────────────────────────────────────────────────────────


def test_projection_is_deterministic():
    """同输入必须同输出——排练结果要能复现，否则「改了剧本有没有变好」无从对比。"""
    history = [_ev(i, "me" if i % 2 else "other", f"第{i}句")
               for i in range(1, 12)]
    args = dict(directive=_directive(), group_hint="跨境电商群",
                name_resolver=lambda a: f"名字-{a}", max_turns=5)
    assert project_history_for(_member(), history, **args) == \
           project_history_for(_member(), history, **args)


# ── 逐 speaker 的视角切换（ECP 的字面含义） ────────────────────────────────


def test_the_same_history_projects_differently_for_each_speaker():
    """**「自我视角」这四个字的字面检验**：同一份群历史投给不同的号，
    assistant/user 的归属必须整体翻转。

    上面那批用例验的是「别人一律同形」，这一条验的是另一半——「我」这个锚点真的
    随 speaker 移动。若投影对所有人产出同一份 messages，ECP 就退化成了上帝视角，
    复读与串味两类事故会同时回来，而且函数看起来仍然「工作正常」。
    """
    history = [_ev(1, "bot_a", "甲说的话"), _ev(2, "bot_b", "乙说的话")]
    from_a = project_history_for(_member("bot_a"), history, directive=_directive())
    from_b = project_history_for(_member("bot_b"), history, directive=_directive())
    assert [m["role"] for m in from_a[1:]] == ["assistant", "user"]
    assert [m["role"] for m in from_b[1:]] == ["user", "assistant"]


def test_the_identity_anchor_falls_back_to_the_account_id():
    """没配显示名时锚点用 account_id——绝不能渲染成「你就是群里的「」」这种空壳。"""
    content = project_history_for(
        CastMember(slot="advocate", account_id="acc_42", persona_id="p",
                   display_name=""), [], directive=_directive())[0]["content"]
    assert "acc_42" in content


# ── system 各段的存在条件与措辞 ─────────────────────────────────────────────


def test_the_intent_is_labelled_as_an_intent_rather_than_a_script():
    """必须明写「这只是意图，不是台词」，否则 LLM 会把意图原样复述出来。

    那是最直白的模板化：全群看到一句「抛出多号切换的痛点」。剧本只写意图不写台词的
    整套设计，最后就卡在这一句提示词上。
    """
    content = project_history_for(_member(), [], directive=_directive())[0]["content"]
    assert "不是台词" in content


def test_optional_sections_are_omitted_rather_than_left_empty():
    """没有群提示/产品/物料/真人插话时不该留下空标题——空段落比不写更糟。

    模型看到一个「【这条涉及的东西】」后面什么都没有，会自己去猜是什么东西。
    """
    content = project_history_for(_member(), [], directive=_directive())[0]["content"]
    for tag in ("【群里的情况】", "【这条涉及的东西】", "【这条会配一份材料】",
                "【有真人在群里说话"):
        assert tag not in content


def test_group_hint_product_and_media_are_all_woven_in_when_present():
    """物料那段还要带上「别自己描述图里内容」——角色瞎描述配图是很典型的穿帮。"""
    content = project_history_for(
        _member(), [], group_hint="这是个跨境电商交流群",
        directive=_directive(product="matrixx", media="产品卡"))[0]["content"]
    assert "跨境电商交流群" in content
    assert "matrixx" in content and "产品卡" in content
    assert "别自己描述图里内容" in content


@pytest.mark.parametrize("level,marker", [
    (0, "完全不要提任何产品"),
    (2, "最多顺口带一句"),
    (5, "像分享自己的经验"),
    (9, "不要广告腔"),
])
def test_each_soft_ad_band_maps_to_its_own_wording(level, marker):
    """分档而非线性：LLM 对「7 分力度」无感，对「可以推荐但别用广告腔」有感。

    四档必须各自映射到不同措辞——塌成一两档，``soft_ad_level`` 与软广衰减这一整套
    机制在生成侧就等于没接上（导演算得再准，提示词还是同一句）。
    """
    content = project_history_for(
        _member(), [], directive=_directive(soft_level=level))[0]["content"]
    assert marker in content


def test_the_ban_list_is_passed_through_verbatim_from_the_directive():
    """禁止项由 directive 原样透传，ECP 不自作主张补充。

    钉住这条分工是为了让「谁负责私聊兜底」有唯一答案：ECP 是结构层（签名上没有私聊
    入口），文本层那句话归 ``DEFAULT_MUST_NOT``。若有人自造 directive 时换掉
    ``must_not``，文本层兜底就会消失——这个耦合应当是显式的，而不是被发现的。
    """
    default_prompt = project_history_for(
        _member(), [], directive=BeatDirective(beat_id="b", intent="i"))[0]["content"]
    assert "私聊" in default_prompt
    for item in DEFAULT_MUST_NOT:
        assert item in default_prompt

    custom_prompt = project_history_for(
        _member(), [], directive=_directive(must_not=("只禁这一条",)))[0]["content"]
    assert "只禁这一条" in custom_prompt and "私聊" not in custom_prompt


# ── 输出形状与只读性 ────────────────────────────────────────────────────────


def test_every_message_is_a_plain_role_content_pair():
    """输出要能直接送 LLM：只有 role/content 两个键，role 只有三种取值。

    多出一个自定义键（比如有人顺手加个 ``speaker`` 方便调试），OpenAI 兼容端点会
    直接 400——而那是在真发路径上、一整场戏都发不出去。
    """
    history = [_ev(1, "me", "自己说"), _ev(2, "other", "别人说")]
    for m in project_history_for(_member("me"), history, directive=_directive()):
        assert set(m) == {"role", "content"}
        assert m["role"] in ("system", "user", "assistant")
        assert isinstance(m["content"], str)


def test_projection_does_not_mutate_the_history_it_was_given():
    """投影是只读的——调用方拿着同一份历史还要接着投给这一拍的其他候选人。"""
    history = [_ev(1, "me", "甲"), _ev(2, "other", "乙")]
    before = [(e.seq, e.speaker_account, e.text, e.kind) for e in history]
    project_history_for(_member("me"), history, directive=_directive())
    assert [(e.seq, e.speaker_account, e.text, e.kind) for e in history] == before
    assert len(history) == 2
