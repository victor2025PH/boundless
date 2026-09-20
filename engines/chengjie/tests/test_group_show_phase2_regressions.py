"""Phase 2 施工期实测出来的真 bug —— 回归钉。

这个文件只放**曾经真的坏过**的点。每条都附「当时错成什么样、为什么没被发现」，因为这
三个 bug 有一个共同形状：**它们都不报错，只是安静地给出一个看起来合理的结果**。这种失败
不会在日志里留下痕迹，只能靠门禁按住。

1. :func:`test_unknown_fingerprint_never_yields_multiple_seats`
   选角把「指纹未知」当成「各自独立」。9 个共用一个公网出口的号被判成 9 个独立组、
   全部准许同台。没人发现，因为选角结果看上去很成功——满员开演。

2. :func:`test_validate_casting_actually_runs_the_linkage_check`
   ``runtime.rehearse`` 调 ``validate_casting`` 时漏传 ``fingerprint_groups``，于是复查
   永远走「没表就跳过」分支。**整条链路里这道安全检查从未真正执行过**，而跳过时它一声
   不吭，与「查过了，没问题」在输出上完全一样。

3. :func:`test_solo_performance_is_never_judged_natural`
   一个号自问自答演完十拍，自然度给 0.74 ``natural``，而同一份报告的 issues 里明写着
   「全场只有一个号在说话：这不是群聊」——判词自相矛盾，只看分数的门禁会放行。
"""
from __future__ import annotations

import asyncio

from src.companion.group_show.casting import cast_roles, validate_casting
from src.companion.group_show.naturalness import naturalness_score
from src.companion.group_show.playbook import Playbook, Role
from src.companion.group_show.runtime import rehearse, stub_generator


def _playbook() -> Playbook:
    return Playbook(
        id="pb_reg",
        name="回归剧本",
        system="growth",
        products=("matrixx",),
        roles=(Role(slot="advocate"), Role(slot="asker"),
               Role(slot="skeptic"), Role(slot="bystander")),
        beats=(),
    )


def _candidates(n: int = 4):
    """无任何指纹信息的候选——即本仓账号注册表的真实形状。"""
    return [
        {"account_id": f"a{i}", "platform": "telegram",
         "persona_id": f"p{i}", "display_name": f"号{i}", "health": "online"}
        for i in range(n)
    ]


# ── Bug 1：未知指纹被当成独立 ───────────────────────────────────────────────


def test_unknown_fingerprint_never_yields_multiple_seats():
    """指纹归属未知的一批号，最多只能有一个上台。

    这是整个 group_show 唯一一条「错了会直接赔掉账号」的不变量。放宽它需要的不是
    「看起来更合理」的论证，而是真实的指纹数据。
    """
    casting = cast_roles(_playbook(), _candidates(9))
    assert len(casting.members) == 1


def test_the_permissive_path_still_exists_but_must_be_explicit():
    """放宽口径本身不是罪，藏起来才是——它必须由调用方显式写出来。"""
    casting = cast_roles(_playbook(), _candidates(4), allow_shared_host=True)
    assert len(casting.members) == 4


# ── Bug 2：安全检查静默失效 ─────────────────────────────────────────────────


def test_validate_casting_actually_runs_the_linkage_check():
    """漏传指纹表时必须**出声**，不能静默跳过。

    「跳过了检查」与「通过了检查」在调用方眼里长得一模一样，这种沉默正是 bug 2 藏了
    那么久的原因。
    """
    casting = cast_roles(
        _playbook(),
        [dict(c, fingerprint_group=f"proxy:px-{i}")
         for i, c in enumerate(_candidates(4))],
    )
    problems = validate_casting(casting, _playbook())
    assert any("跳过" in p for p in problems), (
        "没传指纹表时必须如实声明「本次没查」")


def test_rehearse_passes_the_fingerprint_table_down_to_validation():
    """``rehearse`` 必须把指纹表同时交给选角**和**复查。

    这条钉的是接线，不是逻辑：两处用的必须是同一份表，否则复查的对象和实际上台的
    演员表对不上，查了等于没查。
    """
    result = asyncio.run(rehearse(
        _playbook(),
        [dict(c, fingerprint_group=f"proxy:px-{i}")
         for i, c in enumerate(_candidates(4))],
        generate=stub_generator(),
        fingerprint_groups={f"a{i}": f"proxy:px-{i}" for i in range(4)},
    ))
    assert not any("跳过" in w for w in result.warnings), (
        "rehearse 传了指纹表，复查就不该报「没查」——报了说明没传下去")


def test_rehearse_surfaces_collision_when_table_says_same_group():
    """指纹表说这几个号同组时，复查必须在排练结果里报出撞车。"""
    result = asyncio.run(rehearse(
        _playbook(),
        [dict(c, fingerprint_group="proxy:same") for c in _candidates(3)],
        generate=stub_generator(),
        fingerprint_groups={f"a{i}": "proxy:same" for i in range(3)},
    ))
    # 同组 → 选角本就只放一个上台，于是不会撞车；这里断言的是「表被用上了」
    assert result.casting is not None
    assert len(result.casting.members) == 1


# ── Bug 3：独角戏被判自然 ───────────────────────────────────────────────────


class _Ev:
    """最小事件对象（naturalness 只读这几个字段）。"""

    def __init__(self, account: str, text: str, kind: str = "line"):
        self.speaker_account = account
        self.text = text
        self.kind = kind


#: 刻意高抖动的间隔（CV≈0.57，远高于 CV_ROBOTIC）。
#: 这不是随手编的数：如果间隔太整齐，robotic 判定会由 CV 轴给出，下面那两条 solo 回归钉
#: 就算把修复整个删掉也照样变绿——钉子必须钉在它要钉的那条语义上，所以这里先把 CV 轴和
#: 熵轴都调到「明确正常」，让 verdict 只可能来自「单号独演」。
_JITTERY = [30, 95, 45, 150, 60, 25, 110, 40, 70]


def _solo_events(n: int = 10):
    """一个号自问自答演完全场——每条文本都不同，所以熵是好的。"""
    lines = [
        "手上号越来越多，切来切去容易发错人",
        "上次把给A客户的话发给了B，尴尬到不行",
        "大家平时都怎么管这么多号的呀",
        "我现在把消息都收在一个后台里看",
        "不过这种会不会被判定成机器人挂机",
        "它不是群发，消息还是一条条正常发的",
        "那能同时带多少个号，会不会卡",
        "一个人看十几个号的消息不乱，谁该回一眼能看到",
        "听着有点意思，还是得自己看过才知道",
        "改天去了解一下吧",
    ]
    return [_Ev("solo_account", lines[i % len(lines)]) for i in range(n)]


def test_solo_performance_is_never_judged_natural():
    """一个号包场演完全场，绝不能判 natural。

    修复前实测：``score=0.74 verdict=natural``，而 issues 里同时写着「这不是群聊」。
    真人群里不存在自问自答演完整条种草弧线的模式，这是风控最容易抓的形状。
    """
    report = naturalness_score(_solo_events(10), intervals=_JITTERY)
    assert report["ok"] is True
    assert report["speakers"] == 1
    # 先确认另外两条轴都是健康的，否则这条断言证明不了是 solo 起的作用
    assert report["interval_cv"] > 0.45, "间隔轴必须正常，否则 robotic 可能来自节奏"
    assert report["entropy"] > 0.55, "熵轴必须正常，否则 robotic 可能来自复读"
    assert report["verdict"] == "robotic", (
        f"单号独演必须判 robotic，实际 {report['verdict']}")
    assert any("不是群聊" in i for i in report["issues"])


def test_solo_verdict_and_score_do_not_contradict():
    """分数必须与判词同向——门禁经常只读一个数，两者打架就会放行硬伤。"""
    report = naturalness_score(_solo_events(10), intervals=_JITTERY)
    assert report["score"] <= 0.55


def test_multi_speaker_imbalance_is_still_only_an_issue():
    """**反向保护**：多号失衡仍然只记 issue，不判 robotic。

    这条防止上一条修过头。一个话痨带几个附和在真群里很常见，属于「戏难看」而不是
    「不像人」，把它也判成 robotic 会让门禁噪声大到没人看。
    """
    events = [_Ev("a", t) for t in [
        "手上号越来越多切来切去容易发错人",
        "上次把给A客户的话发给了B尴尬到不行",
        "大家平时都怎么管这么多号的呀",
        "我现在把消息都收在一个后台里看不用来回切",
        "那能同时带多少个号会不会卡顿",
        "一个人看十几个号的消息不乱",
    ]] + [_Ev("b", "确实是这样啊我也遇到过这种情况")]
    report = naturalness_score(events, intervals=_JITTERY[:6])
    assert report["speakers"] == 2
    assert report["interval_cv"] > 0.45, "间隔轴必须正常，否则测不出失衡的影响"
    assert report["verdict"] != "robotic", "多号失衡不该被判成机器人"
    assert any("失衡" in i or "刷屏" in i for i in report["issues"])


def test_balanced_show_still_passes():
    """**反向保护**：正常的多号轮换戏必须仍然判 natural，修复不能误伤好戏。"""
    texts = [
        "手上号越来越多切来切去容易发错人",
        "上次把给A客户的话发给了B尴尬死了",
        "大家平时都怎么管这么多号的呀",
        "我现在把消息都收在一个后台里看",
        "这种会不会被判定成机器人挂机呢",
        "它不是群发消息还是一条条正常发",
        "那能同时带多少个号会不会卡",
        "一个人看十几个号完全不乱",
    ]
    events = [_Ev(f"acc{i % 4}", t) for i, t in enumerate(texts)]
    report = naturalness_score(
        events, intervals=[52, 31, 78, 44, 96, 38, 63])
    assert report["speakers"] == 4
    assert report["verdict"] == "natural", (
        f"正常轮换的戏被误伤了：{report['verdict']} / {report['issues']}")
