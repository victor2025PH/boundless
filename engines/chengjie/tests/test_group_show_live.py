"""真发链路门禁 —— 这是全仓最该严的一份测试：它守的是「会不会有一批号被封」。

三类不变量，按代价从高到低：

1. **双锁 + 指纹硬前置**：任何一把锁没开齐，一条消息都不许出去。
2. **至多一次**：投递失败绝不重发（重复一句话是最刺眼的机器痕迹），护栏拦下立刻整场
   中止（继续演就是跟 Kill-Switch 对抗）。
3. **与排练同种子对等**：同一剧本同一种子下，真发（挂假出口）与排练必须逐行产出同一份
   台词、同一份节奏。这条是「预览是真的」这个承诺的唯一强制手段——一旦两条驱动漂移，
   运营照着排练验收通过的戏，真发出来会是另一场。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from src.companion.group_show.live import (
    MAX_CONSECUTIVE_SEND_FAILURES,
    MAX_LIVE_LINES,
    LiveResult,
    format_live,
    is_solo_playbook,
    live_enabled,
    orchestrator_sender,
    perform,
    plan_live,
)
from src.companion.group_show.playbook import Beat, Playbook, Role
from src.companion.group_show.runtime import rehearse, stub_generator
from src.companion.group_show.schedule import in_quiet_hours

_LIVE_SRC = (Path(__file__).resolve().parents[1] / "src" / "companion"
             / "group_show" / "live.py")

#: 双锁全开的配置。测试里每处都显式带上它，免得「忘了开锁」被误读成「功能坏了」。
ARMED = {"companion": {"group_show": {"live": {"enabled": True}}}}

#: 逐拍互不重叠的 CJK intent 池。复读闸（``MAX_LINE_SIMILARITY``）按内容词 overlap
#: 判重，而 ``stub_generator`` 台词的内容词全部来自 intent——旧写法 ``f"意图{i}"``
#: 的数字会被内容词提取剥掉，所有拍全塌成 ``{意, 图}``、两两 overlap=1.0，一进真发
#: 循环整场都被当复读弃拍（16 条结构用例齐红）。真 LLM 的台词天然逐拍不同，夹具必须
#: 还原这个性质：给每拍切一块专属连续 CJK，任意两拍内容词零交集。
_INTENT_CJK = "".join(chr(0x4E00 + i) for i in range(600))


def _intent(i: int) -> str:
    lo = (int(i) - 1) * 3
    return _INTENT_CJK[lo:lo + 3] or f"拍{i}"


def _playbook(n_beats: int = 4) -> Playbook:
    roles = (Role("asker"), Role("advocate"), Role("bystander"), Role("skeptic"))
    slots = ("asker", "advocate", "skeptic", "bystander")
    beats = tuple(
        Beat(id=f"b{i}", role=slots[(i - 1) % 4], intent=_intent(i),
             product="matrixx" if i % 2 == 0 else "", soft=0 if i == 1 else 3)
        for i in range(1, n_beats + 1)
    )
    return Playbook(id="pb", name="pb", system="growth", products=("matrixx",),
                    soft_ad_level=4, roles=roles, beats=beats)


def _candidates(n: int = 4):
    return [
        {"account_id": f"a{i}", "platform": "telegram",
         "persona_id": f"p{i}", "display_name": f"演员{i}",
         "health": "online", "fingerprint_group": f"fp{i}"}
        for i in range(1, n + 1)
    ]


def _fps(n: int = 4):
    return {f"a{i}": f"fp{i}" for i in range(1, n + 1)}


class _Clock:
    """假时钟：``sleep`` 直接把时间往前推。

    这样真发能在毫秒内跑完一场四十分钟的戏，且 ``at_seconds`` 与排练的虚拟时钟**同口径**
    ——对等性测试因此不只能比台词，还能比节奏。
    """

    def __init__(self, t0: float = 1_000.0) -> None:
        self.t = float(t0)
        self.slept: list = []

    def now(self) -> float:
        return self.t

    async def sleep(self, sec: float) -> None:
        self.slept.append(float(sec))
        self.t += float(sec)


class _Sender:
    """假投递出口，可编排每一条的结果。"""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list = []

    async def __call__(self, account_id: str, text: str):
        self.calls.append((account_id, text))
        if self.results:
            return self.results.pop(0)
        return {"delivered": True, "message_id": f"m{len(self.calls)}"}


async def _perform(**over):
    clock = over.pop("clock", None) or _Clock()
    sender = over.pop("sender", None) or _Sender()
    kwargs = dict(
        group_key="g1", send=sender, generate=stub_generator(),
        confirm_live=True, app_config=ARMED, fingerprint_groups=_fps(),
        sleep=clock.sleep, now=clock.now, seed=7,
        # 钟点显式传：默认会从 time.localtime(now()) 取，而假时钟的 t0 换个时区就落进禁演
        # 窗（UTC 下 t0=1000 是凌晨 0 点）——整套测试会因为跑在哪台机器上而红/绿。
        hour=14,
    )
    kwargs.update(over)
    # 用 in 判断而不是 `or 默认值`：空号池（``candidates=[]``）是一个要测的真场景，
    # 而 `[] or _candidates()` 会把它悄悄换成健康号池，测试于是在测别的东西。
    pb = kwargs.pop("playbook", None) or _playbook()
    cands = kwargs.pop("candidates") if "candidates" in kwargs else _candidates()
    res = await perform(pb, cands, **kwargs)
    return res, sender, clock


# ── 一、架构级安全钉子 ──────────────────────────────────────────────────────


def test_live_never_reaches_for_a_worker_by_itself():
    """真发模块自己**不许** import 任何编排器/worker——出口必须由调用方递进来。

    这条不是风格洁癖。真发路径的依赖面越小，「这行代码会不会把消息发出去」这个问题就越
    容易回答；一旦模块内部能自己 ``get_account_orchestrator()`` 拿到出口，双锁就形同虚设
    （某个分支绕过参数直接发），而审这件事需要读完整个模块。
    """
    code = "\n".join(
        ln for ln in _LIVE_SRC.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )
    # 去掉文档字符串块，只看真代码（docstring 里正要解释「为什么出口由调用方递进来」）
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    for pattern in ("get_account_orchestrator", "AccountOrchestrator",
                    "account_orchestrator", "telegram_client", "pyrogram",
                    "send_via_adapters"):
        assert pattern not in code, f"真发模块不该自己拿发送出口: {pattern}"


def test_rehearsal_has_no_way_to_deliver_anything():
    """排练侧的物理隔离不许被这次改动削弱：``rehearse`` 的签名里不能有投递出口。"""
    import inspect
    params = set(inspect.signature(rehearse).parameters)
    for leak in ("send", "sender", "orchestrator", "confirm_live"):
        assert leak not in params, f"排练不该拿得到投递出口: {leak}"


# ── 二、双锁 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_without_the_explicit_confirmation_nothing_goes_out():
    res, sender, _ = await _perform(confirm_live=False)
    assert res.terminate_reason == "not_armed"
    assert res.armed is False
    assert res.sent == 0 and sender.calls == []


@pytest.mark.asyncio
async def test_without_the_config_switch_nothing_goes_out():
    res, sender, _ = await _perform(app_config={})
    assert res.terminate_reason == "not_armed"
    assert res.sent == 0 and sender.calls == []


@pytest.mark.asyncio
async def test_both_locks_open_actually_delivers():
    res, sender, _ = await _perform()
    assert res.armed is True
    assert res.sent == 4 and len(sender.calls) == 4
    assert res.terminate_reason == "completed"
    assert all(ln.delivered and ln.message_id for ln in res.lines)


@pytest.mark.asyncio
async def test_no_outlet_at_all_is_refused_rather_than_crashing():
    """两把锁都开了但没给出口——拒演，而不是抛 AttributeError。

    区别在于可观测性：异常穿到后台任务里会被吞掉，运营只看到「没反应」；如实返回
    ``not_armed`` 则导播台能把原因显示出来。
    """
    res = await perform(_playbook(), _candidates(), group_key="g1",
                        confirm_live=True, app_config=ARMED,
                        fingerprint_groups=_fps(), generate=stub_generator())
    assert res.terminate_reason == "not_armed"
    assert "投递出口" in " ".join(res.warnings)


def test_the_config_lock_defaults_to_closed():
    assert live_enabled(None) is False
    assert live_enabled({}) is False
    assert live_enabled({"companion": {"group_show": {}}}) is False
    assert live_enabled(ARMED) is True


def test_a_garbage_config_reads_as_closed_not_open():
    """配置读挂了要按「没开」算。安全侧的默认方向只有一个。"""
    assert live_enabled({"companion": "不是字典"}) is False


# ── 三、指纹表是真发硬前置 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_going_live_without_a_fingerprint_table_is_refused():
    """排练里「没传指纹表」只是 warn，真发里必须硬拦。

    ``validate_casting`` 在没有表时只能如实说「本次跳过了同台关联复查」——而**跳过的检查
    和通过的检查在返回值里长得一模一样**。同台关联是关联封号第一杀手，这个前置条件必须
    由真发路径自己挡在门外。
    """
    res, sender, _ = await _perform(fingerprint_groups=None)
    assert res.terminate_reason == "no_fingerprints"
    assert res.sent == 0 and sender.calls == []
    assert "指纹" in " ".join(res.warnings)


@pytest.mark.asyncio
async def test_a_failed_health_check_stops_the_show_before_the_first_message():
    """体检硬错 → 一条都不发（与排练同一套判据，只是这里的代价是真的）。"""
    res, sender, _ = await _perform(candidates=[])
    assert res.terminate_reason == "invalid"
    assert sender.calls == []


# ── 三之二、真发不许降级演（防自问自答）────────────────────────────────────


@pytest.mark.asyncio
async def test_one_account_is_not_allowed_to_play_four_people():
    """单号 + 四角色剧本 → 拒演。

    排练里这只是 ``warn: 像双簧``，导演会用替补让这个号顶掉所有空槽。在真群里那条降级路径
    的产物是**自问自答**：同一个号先以 asker 提问、再以 advocate 回答——水军最经典、群友最
    认得出的破绽。单号的正确形态是一套单角色剧本，而不是一个号演四个人。
    """
    res, sender, _ = await _perform(candidates=_candidates(1),
                                    fingerprint_groups=_fps(1))
    assert res.terminate_reason == "understaffed"
    assert sender.calls == []
    assert "自问自答" in " ".join(res.warnings)


@pytest.mark.asyncio
async def test_a_solo_playbook_with_a_single_account_is_perfectly_fine():
    """真正的单号形态：一套单角色剧本 —— 这条必须放行，否则「拒绝降级」就变成「禁止单号」。

    单角色剧本的那个角色只能是 ``advocate``（``validate_casting`` 硬性要求剧本声明核心角色）。
    这在语义上恰好是对的：单号炒群的那个号就是推手。也正因为它每一拍都在演推手，solo 模式的
    绑定约束才落在**角色轴**上——与容量层给出的结论一字不差（见 ``capacity`` 模块）。
    """
    solo = Playbook(id="solo", name="solo", system="growth",
                    products=("matrixx",), soft_ad_level=2,
                    roles=(Role("advocate"),),
                    beats=tuple(Beat(id=f"b{i}", role="advocate",
                                     intent=_intent(i), soft=0 if i == 1 else 2)
                                for i in range(1, 4)))
    res, sender, _ = await _perform(playbook=solo, candidates=_candidates(1),
                                    fingerprint_groups=_fps(1))
    assert res.terminate_reason == "completed"
    assert res.sent == 3
    assert {a for a, _t in sender.calls} == {"a1"}


@pytest.mark.asyncio
async def test_a_role_declared_but_never_scheduled_does_not_block_anything():
    """声明了却一拍都没安排的角色，没人演毫无影响——不该因此拒演。"""
    pb = Playbook(id="q", name="q", system="growth", products=("matrixx",),
                  soft_ad_level=2,
                  roles=(Role("asker"), Role("advocate"), Role("skeptic")),
                  beats=(Beat(id="b1", role="asker", intent="问", soft=0),
                         Beat(id="b2", role="advocate", intent="答", soft=2)))
    res, _s, _c = await _perform(playbook=pb, candidates=_candidates(2),
                                 fingerprint_groups=_fps(2))
    assert res.terminate_reason == "completed" and res.sent == 2


# ── 二b、复读闸（同场台词去重） ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_repeating_generator_is_retried_with_a_ban_then_skipped():
    """初稿与已发雷同 → 带禁令重试一次；重试仍雷同 → 弃拍，绝不把复读发出去。

    生成器无论怎么问都复读同一句：第 1 拍正常发出（还没有比对面），其后每拍
    触发重试仍复读 → 连续弃拍到 MAX_CONSECUTIVE_SKIPS，按生成侧故障显式收场。
    「同一卖点两分钟内换个句式再安利一遍」是首场灰度实录里最扎眼的水军痕迹，
    宁可这拍不说话。
    """
    def _gen(_speaker, _messages, _directive):
        return "统一后台收消息真的省事不用来回切号"

    res, sender, _ = await _perform(generate=_gen)
    assert res.sent == 1 and len(sender.calls) == 1
    assert res.repetition_retries == 3      # b2/b3/b4 各触发一次带禁令重试
    assert res.terminate_reason == "generation_failed"


@pytest.mark.asyncio
async def test_the_anti_repeat_retry_rescues_the_beat_when_it_finds_new_words():
    """重试带上了禁令、生成器换了说法 → 这一拍照常发出，戏一条不损失。"""
    def _gen(_speaker, _messages, directive):
        if any("已经说过" in rule for rule in (directive.must_not or ())):
            return _intent(99) + _intent(100)    # 重试稿：与任何已发零交集
        return "统一后台收消息真的省事不用来回切号"

    res, sender, _ = await _perform(playbook=_playbook(2), generate=_gen)
    assert res.sent == 2 and len(sender.calls) == 2
    assert res.repetition_retries == 1
    assert res.terminate_reason == "completed"


@pytest.mark.asyncio
async def test_an_empty_slot_says_which_of_the_four_guardrails_emptied_it():
    """拒演信息必须带**归因**——四种空槽原因的处置是相反的。

    只报「缺角 advocate」时，运营最自然的动作是去买号；而这在 ``budget`` / ``pair`` 两种
    原因下恰好是反的（护栏正在正常工作，加号只会让它拦得更多）。归因错一次的代价是一次
    错误采购，加一次「怎么加了号还是演不了」的信任损失。
    """
    # 号池只有 1 个号、剧本要 4 个角色 → 空槽原因是 pool
    res, _s, _c = await _perform(candidates=_candidates(1),
                                 fingerprint_groups=_fps(1))
    blob = " ".join(res.warnings)
    assert res.terminate_reason == "understaffed"
    assert "号不够" in blob, "pool 原因必须译成「补号／配独立出口」而不是含糊的缺角"


# ── 三之三、禁演时段（轴五在真发路径上的那半条）──────────────────────────────


@pytest.mark.asyncio
async def test_nobody_performs_at_three_in_the_morning():
    """凌晨开演 → 拒演。这条特征不需要任何统计就能看出来。

    排期器排表时早就绕开了安静时段，但导播台上「现在演一场」这个按钮前面没有任何一张表；
    真发路径必须自己带这道闸门。
    """
    res, sender, _ = await _perform(hour=3)
    assert res.terminate_reason == "quiet_hours"
    assert sender.calls == [], "一条都不许发出去"
    assert "禁演时段" in " ".join(res.warnings)


@pytest.mark.asyncio
async def test_the_edges_of_the_quiet_window_are_half_open():
    """窗口是 ``[起, 止)``：23 点禁、8 点放——与 ``companion_proactive`` 判深夜同一口径。

    两处口径不同的后果是护栏漏一半：主动触达觉得该睡了、群戏觉得还能演。
    """
    assert (await _perform(hour=23))[0].terminate_reason == "quiet_hours"
    assert (await _perform(hour=7))[0].terminate_reason == "quiet_hours"
    assert (await _perform(hour=8))[0].sent > 0
    assert (await _perform(hour=22))[0].sent > 0


@pytest.mark.asyncio
async def test_an_empty_window_is_how_you_turn_the_gate_off():
    """``[0, 0]`` ＝ 空窗 ＝ 不禁。

    刻意让「关掉闸门」和「配一个窗」走同一个配置项：两个开关一定会出现「窗配了但没打开」
    这种状态，而它看起来跟「窗生效了」一模一样。
    """
    night = {"companion": {"group_show": {
        "live": {"enabled": True, "quiet_hours": [0, 0]}}}}
    res, _s, _c = await _perform(hour=3, app_config=night)
    assert res.sent > 0


@pytest.mark.asyncio
async def test_a_custom_window_is_honoured():
    """运营配的窗要真生效（不是只认那个缺省常量）。"""
    day_off = {"companion": {"group_show": {
        "live": {"enabled": True, "quiet_hours": [12, 15]}}}}
    assert (await _perform(hour=13, app_config=day_off))[0].terminate_reason \
        == "quiet_hours"
    assert (await _perform(hour=3, app_config=day_off))[0].sent > 0


@pytest.mark.asyncio
async def test_an_unreadable_clock_lets_the_show_through():
    """钟点算不出来 → 放行，不拿一个坏钟点去禁演。

    这里刻意不做保守处理：禁演会把整条真发链路默默锁死，而运营对付「怎么都发不出去」的
    唯一手段就是把闸门关掉——那才是真的没有闸门。
    """
    res, _s, _c = await _perform(hour="不是钟点")   # type: ignore[arg-type]
    assert res.sent > 0
    # 谓词层的同一条不变量（``None`` 与脏值都不构成禁演）
    assert in_quiet_hours(None) is False
    assert in_quiet_hours("三点") is False
    # 但真的是深夜时它必须说是
    assert in_quiet_hours(3) is True


# ── 四、与排练同种子对等（防两条驱动漂移）──────────────────────────────────


@pytest.mark.asyncio
async def test_live_and_rehearsal_produce_the_same_show_from_the_same_seed():
    """同种子下真发与排练必须**逐行一致**：台词、角色、节奏全都一样。

    这条测试是「排练即预览」这个承诺的唯一强制手段。两条驱动各有一个主循环（虚拟时钟 vs
    真等待），决策却必须来自同一批函数；哪天有人只改了一条路径的选角顺序或节奏调用，
    这里立刻红——否则运营照着排练验收通过的戏，真发出来会是另一场，而这种偏差只有在真群
    里才会暴露。
    """
    pb, cands, fps = _playbook(6), _candidates(), _fps()
    reh = await rehearse(pb, cands, group_key="g1", generate=stub_generator(),
                         seed=7, fingerprint_groups=fps)
    clock = _Clock()
    live = await perform(pb, cands, group_key="g1", send=_Sender(),
                         generate=stub_generator(), confirm_live=True,
                         app_config=ARMED, fingerprint_groups=fps, seed=7,
                         sleep=clock.sleep, now=clock.now, hour=14)

    spoken = [ln for ln in reh.lines if ln.kind in ("line", "media")]
    assert live.sent == len(spoken) > 0
    assert [(ln.account_id, ln.role, ln.text) for ln in live.lines] == \
           [(ln.account_id, ln.role, ln.text) for ln in spoken]
    # 节奏也要对得上——运营验收看的是那份时刻表
    assert [ln.at_seconds for ln in live.lines] == [ln.at_seconds for ln in spoken]


@pytest.mark.asyncio
async def test_generation_latency_is_paid_out_of_the_pause_not_added_to_it():
    """LLM 那几秒算在「正在打字」里，不该额外拉长间隔。

    不扣的话每个间隔都被推长，实际节奏与排练那份时刻表越走越偏；而节奏本身就是一条被平台
    观测的特征，「每条之间都比预期慢 3 秒」是系统性的偏差而不是随机抖动。
    """
    clock = _Clock()

    async def _slow_gen(_speaker, _messages, directive):
        clock.t += 5.0            # 模拟 5 秒 LLM 延迟
        # 台词取逐拍互异的 intent：固定前缀+beat_id 的内容词全同（b1/b2 会被剥），
        # 复读闸一开这些拍全被弃，测的就不再是节奏而是去重了。
        return directive.intent

    fast = _Clock()
    quick, _, _ = await _perform(clock=fast)

    slow = await perform(_playbook(), _candidates(), group_key="g1",
                         send=_Sender(), generate=_slow_gen, confirm_live=True,
                         app_config=ARMED, fingerprint_groups=_fps(), seed=7,
                         sleep=clock.sleep, now=clock.now, hour=14)
    assert slow.sent == quick.sent
    # 每一拍的间隔预算里已经扣掉了 5 秒生成耗时 → 总时长不该被推长 5×N
    assert slow.duration_seconds <= quick.duration_seconds + 1.0


# ── 五、至多一次：失败不重发，拦截立刻收场 ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_delivery_is_never_retried():
    """投递失败 → 这一拍作废，**不重发**。

    「发送失败」这个信号本身不可靠：消息可能已经到了平台、只是回执丢在网络上。此时重试
    就是实打实的重复发送，而真人不会把同一句话说两遍。少一句话没人注意，重复一句话一眼假。
    """
    sender = _Sender({"delivered": False, "error": "timeout"})
    res, sender, _ = await _perform(sender=sender)
    assert res.failed == 1
    # 第一拍作废后不再出现在后续投递里（没有任何一条文本被发两次）
    texts = [t for _a, t in sender.calls]
    assert len(texts) == len(set(texts)), f"出现了重复投递: {texts}"
    assert res.sent == len(texts) - 1


@pytest.mark.asyncio
async def test_consecutive_delivery_failures_end_the_show():
    sender = _Sender({"delivered": False}, {"delivered": False},
                     {"delivered": False})
    res, sender, _ = await _perform(sender=sender)
    assert res.terminate_reason == "send_failed"
    assert res.failed == MAX_CONSECUTIVE_SEND_FAILURES
    assert res.sent == 0


@pytest.mark.asyncio
async def test_a_blocked_send_aborts_the_whole_show_not_just_the_beat():
    """被护栏拦下 → **整场**中止。

    ``blocked`` 意味着 Kill-Switch / 反封号闸 / 会话不健康正在生效。它拦第一条就会拦后面
    每一条，继续演只是跟安全系统对抗；而「失败」是技术抖动，处方相反。两者混成一类，
    要么因一次网络抖动放弃整场戏，要么在急停已按下时继续硬发十几条。
    """
    sender = _Sender({"delivered": True, "message_id": "m1"},
                     {"delivered": False, "blocked": "kill_switch"})
    res, sender, _ = await _perform(sender=sender)
    assert res.terminate_reason == "blocked"
    assert res.blocked_by == "kill_switch"
    assert res.sent == 1
    assert len(sender.calls) == 2, "拦截之后不该再试第三条"


@pytest.mark.asyncio
async def test_a_transient_failure_is_told_apart_from_a_guardrail_block():
    """一次抖动不该放弃整场（与上一条互为对照，证明两条路径真的分开了）。"""
    sender = _Sender({"delivered": False}, {"delivered": True, "message_id": "m"})
    res, _s, _ = await _perform(sender=sender)
    assert res.terminate_reason not in ("blocked", "send_failed")
    assert res.sent >= 1 and res.failed == 1


# ── 六、硬顶：配置改不动 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_typo_in_the_line_budget_cannot_flood_a_real_group():
    """``max_lines: 500`` 这种手滑在真群里是一个号连发五百条——硬顶必须兜住。

    能被配置调大的护栏，在「配置写错」这个故障模式下等于不存在。所以这个上限刻意不读配置。
    """
    res, sender, _ = await _perform(
        playbook=_playbook(80),
        config={"max_lines": 500, "max_duration_seconds": 999_999,
                "max_share_per_account": 1.0})
    assert res.terminate_reason == "hard_line_cap", \
        "必须是硬顶收场——若是 budget/completed，说明这条测试其实没碰到硬顶"
    assert res.sent == MAX_LIVE_LINES
    assert len(sender.calls) == MAX_LIVE_LINES


@pytest.mark.asyncio
async def test_a_crawling_generator_cannot_keep_a_show_on_the_air_all_day():
    """时长硬顶：``max_duration_seconds: 999999`` 也不能让一场戏挂一整天。

    这里刻意用**慢生成**而不是多拍数来触顶——那是真实场景：LLM 端点降级到每条几分钟时，
    条数远没到上限、墙钟却已经走了几小时，而一个号在群里断断续续说一整天比连发二十条更假。
    """
    clock = _Clock()

    async def _crawl(_speaker, _messages, directive):
        clock.t += 600.0        # 每拍生成慢到十分钟
        return f"台词{directive.beat_id}"

    res = await perform(
        _playbook(80), _candidates(), group_key="g1", send=_Sender(),
        generate=_crawl, confirm_live=True, app_config=ARMED,
        fingerprint_groups=_fps(), seed=7, sleep=clock.sleep, now=clock.now,
        hour=14, config={"max_lines": 500, "max_duration_seconds": 999_999,
                         "max_share_per_account": 1.0})
    assert res.terminate_reason == "hard_time_cap"
    assert res.sent < MAX_LIVE_LINES, "这条要测的是墙钟，不是条数"


@pytest.mark.asyncio
async def test_passing_the_whole_app_config_into_the_director_slot_is_called_out():
    """把整份 app 配置传进 ``config``（导演参数）位 → 必须点名。

    症状是最难查的那种：戏照演，只是导演参数**静默回落缺省**，于是运营手里那份排练时刻表
    对不上真发。没有异常、没有报错，只有一份对不上的节奏。
    """
    res, _s, _c = await _perform(config=ARMED)
    assert any("app 配置" in w for w in res.warnings)
    assert res.sent > 0, "只是点名，不该因此拒演（形状探测是启发式的）"


@pytest.mark.asyncio
async def test_the_default_generator_refuses_to_put_a_placeholder_in_a_real_group():
    """没传生成器时的缺省必须是「不说话」。

    排练的缺省是 ``stub_generator``（产 ``[p1·b1] 意图1`` 这种占位串），在离线正合适。
    真发若沿用同一个缺省，就会把占位串**发进真群**——这是那种「测试全绿、上线社死」的
    失败模式，只能靠缺省值本身的选择来防。
    """
    res, sender, _ = await _perform(generate=None)
    assert res.sent == 0 and sender.calls == []
    assert res.terminate_reason == "generation_failed"


# ── 七、逐拍闸门：跨群冷却 ──────────────────────────────────────────────────


class _Watch:
    """假闸门：按号给固定的「还要等多久」，等过一次就放行。"""

    def __init__(self, waits: dict, once: bool = True) -> None:
        self.waits = dict(waits)
        self.once = once
        self.asked: list = []

    def delay_for(self, account, at):
        self.asked.append((str(account), float(at)))
        wait = int(self.waits.get(str(account), 0))
        if wait and self.once:
            self.waits[str(account)] = 0
        return wait


@pytest.mark.asyncio
async def test_a_cooling_account_is_waited_for_rather_than_skipped():
    """真发有真时钟，所以能**等**——这比跳过好：跳过让戏少一句话，等一会儿什么都不损失。

    （排练里没有这个选项：虚拟时钟里「等」等于没等。）
    """
    watch = _Watch({"a1": 30})
    res, sender, clock = await _perform(watch=watch)
    assert res.gate_waits == 1
    assert 30.0 in clock.slept
    assert res.skipped == 0, "等完就该照常开口，不该丢这一拍"
    assert res.sent == 4


@pytest.mark.asyncio
async def test_the_gate_is_re_asked_every_beat_not_just_at_casting():
    """闸门必须逐拍问。一场戏演几十分钟，开演那一刻的快照撑不到收场。"""
    watch = _Watch({})
    res, _s, _c = await _perform(watch=watch)
    assert len(watch.asked) >= res.sent, "每一拍都该重新问一次冷却"


@pytest.mark.asyncio
async def test_an_absurdly_long_cooldown_skips_the_beat_instead_of_stalling():
    """顶太久就跳过这一拍——等半小时不如让这拍不说话，戏还能往下演。"""
    watch = _Watch({"a1": 9_999}, once=False)
    res, _s, clock = await _perform(watch=watch)
    assert res.skipped >= 1
    assert 9_999.0 not in clock.slept, "不该真等三小时"


@pytest.mark.asyncio
async def test_no_watch_means_the_cooldown_gate_is_simply_absent():
    """不传 watch ＝ 不挂这条闸门（与它上线前一字不差），而不是全禁。"""
    res, sender, _ = await _perform(watch=None)
    assert res.sent == 4


# ── 八、落库时序 ────────────────────────────────────────────────────────────


class _Store:
    def __init__(self) -> None:
        self.sessions: list = []
        self.events: list = []
        self.members: list = []

    def save_session(self, state):
        self.sessions.append((state.status, bool(state.dry_run),
                              len(state.events)))

    def append_event(self, sid, ev):
        self.events.append((sid, ev.seq, ev.kind, ev.speaker_account))

    def record_membership(self, group_key, account_id, **kw):
        self.members.append((group_key, account_id, kw.get("source")))


@pytest.mark.asyncio
async def test_the_session_is_on_record_before_the_first_message_leaves():
    """开演前先落库：中途被杀时台账里至少知道「有这么一场戏」。"""
    store = _Store()
    res, _s, _c = await _perform(store=store)
    assert store.sessions, "开演前没落库 → 被杀就查不到这场戏"
    assert store.sessions[0][0] == "pending"
    assert store.sessions[0][1] is False, "真发必须以 dry_run=0 入库，否则不进暴露台账"
    assert store.sessions[-1][0] == "done"
    assert res.sent == 4


@pytest.mark.asyncio
async def test_progress_is_flushed_after_each_delivered_line():
    """每条真发成功后刷 status/beat_cursor——否则坐席全程看见 pending/0。

    灰度实战（测试123）里事件台账有 3 条，session 行却仍 pending+cursor=0：
    只 append_event 不 save_session 的盲区。
    """
    store = _Store()
    res, _s, _c = await _perform(store=store)
    # 开演 pending + 每条 delivered 一次 + 收场 done
    assert len(store.sessions) >= 1 + res.sent + 1
    mid = [s for s in store.sessions if s[0] == "running"]
    assert mid, "演中必须刷成 running，不能一直 pending"
    assert mid[-1][2] >= 1, "刷进度时 events 长度应已跟上"


@pytest.mark.asyncio
async def test_only_delivered_lines_land_in_the_ledger():
    """没发出去的话不进台账。

    先落库再发的话，一条其实没发出去的消息会进共现矩阵 → 风险读数虚高、运营对着不存在的
    消息排查。方向反了同样糟（漏记会让暴露被低估），所以时序只能是「发成功之后」。
    """
    store = _Store()
    sender = _Sender({"delivered": False}, {"delivered": True, "message_id": "m"})
    res, _s, _c = await _perform(store=store, sender=sender)
    kinds = [k for _sid, _seq, k, _a in store.events]
    assert "skip" not in kinds, "作废的拍不该进事件台账"
    assert len(store.events) == res.sent


@pytest.mark.asyncio
async def test_being_cancelled_leaves_an_honest_record_and_still_propagates():
    """取消必须往上传（否则调用方以为停干净了），但已发出的那几条要如实落库。"""
    store = _Store()
    calls = {"n": 0}

    async def _dies(account_id, text):
        calls["n"] += 1
        if calls["n"] > 2:
            raise asyncio.CancelledError()
        return {"delivered": True, "message_id": "m"}

    with pytest.raises(asyncio.CancelledError):
        await _perform(store=store, sender=_dies)
    assert store.sessions[-1][0] == "aborted"
    assert len(store.events) == 2, "已经发出去的两条必须留在台账里"


@pytest.mark.asyncio
async def test_taking_the_stage_is_recorded_as_being_in_the_group():
    """上台即证明在群里 —— 成员关系必须登记。

    不登记的话成员共现矩阵会**低估**暴露面（「一场戏都没演过 ⇒ 安全」那类假安全的变体）：
    这批号明明已经同框在这个群里，矩阵却看不见，于是运营会以为还有余量继续往里加号。
    """
    store = _Store()
    res, _s, _c = await _perform(store=store)
    assert {a for _g, a, _src in store.members} == {"a1", "a2", "a3", "a4"}
    assert all(g == "g1" and src == "live" for g, _a, src in store.members)
    assert res.sent == 4


@pytest.mark.asyncio
async def test_a_discarded_beat_never_reaches_the_exposure_ledger():
    """真库端到端：作废的拍不进共现台账（没发出去的话，平台侧一个字都看不到）。

    这条走真 ``GroupShowStore`` 而不是假 store——``performance_ledger`` 的两条过滤
    （``dry_run=0`` / ``kind IN SPOKEN_KINDS``）写反任何一条，整个风险读数就是假的，
    而假 store 验不出那两条 SQL。
    """
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(":memory:")
    sender = _Sender({"delivered": False},
                     {"delivered": True, "message_id": "m"})
    res, _s, _c = await _perform(store=store, sender=sender,
                                 session_id="live_sid")
    ledger = store.performance_ledger()
    assert ledger.get("g1"), "真发过的号必须进演出台账，否则下一场的闸门看不到它"
    # 作废那一拍的号若从未成功发言，就不该出现在台账里
    spoke = {ln.account_id for ln in res.lines}
    assert set(ledger["g1"]) == spoke
    assert store.memberships().get("g1"), "成员台账也该有这一群"


@pytest.mark.asyncio
async def test_a_broken_store_does_not_stop_a_show_already_on_the_air():
    """写库出错不该让一场**已经发出去**的戏中止——消息已在群里，此刻停只会让记录更残缺。"""

    class _Boom:
        def save_session(self, _s):
            raise RuntimeError("disk full")

        def append_event(self, *_a):
            raise RuntimeError("disk full")

    res, _s, _c = await _perform(store=_Boom())
    assert res.sent == 4 and res.terminate_reason == "completed"


# ── 九、出口适配器 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_adapter_binds_platform_and_group_so_the_loop_cannot_mix_them_up():
    seen = {}

    class _Orch:
        async def send(self, platform, account_id, chat_key, text):
            seen.update(platform=platform, account=account_id,
                        chat=chat_key, text=text)
            return {"delivered": True, "message_id": "m9"}

    send = orchestrator_sender(_Orch(), platform="telegram", group_key="-100123")
    res = await send("a1", "喂")
    assert res["delivered"] and res["message_id"] == "m9"
    assert seen == {"platform": "telegram", "account": "a1",
                    "chat": "-100123", "text": "喂"}


@pytest.mark.asyncio
async def test_the_adapter_turns_a_crash_into_a_failure_not_an_exception():
    """异常在适配器里就地转成 ``delivered=False``。

    让异常穿到主循环，那里就只剩一个 ``except``，「护栏拦下」与「链路抖动」又混成一类了
    ——而这两者的处方相反。
    """

    class _Boom:
        async def send(self, *_a):
            raise RuntimeError("worker 掉了")

    res = await orchestrator_sender(_Boom(), platform="telegram",
                                    group_key="g")("a1", "喂")
    assert res["delivered"] is False and "worker" in res["error"]


# ── 十、渲染 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unarmed_result_says_so_loudly_instead_of_looking_empty():
    """未武装的渲染必须一眼看出「没发」，否则会被读成「演了但没人说话」。"""
    res, _s, _c = await _perform(confirm_live=False)
    text = format_live(res)
    assert "未武装" in text
    assert "confirm_live" in text


@pytest.mark.asyncio
async def test_a_live_render_is_visually_distinct_from_a_rehearsal_render():
    """真发与排练的渲染要长得不一样——运营一眼要能看出「这是真发过的」。"""
    res, _s, _c = await _perform()
    text = format_live(res)
    assert "真发" in text and "已发 4 条" in text
    assert not re.search(r"═══ 排练", text)


def test_a_bare_result_is_renderable_without_a_show():
    assert "真发" in format_live(LiveResult(session_id="s", playbook_id="p",
                                           group_key="g"))


# ── 预检与真发共用同一份判定 ────────────────────────────────────────────────


def test_plan_live_and_perform_refuse_the_same_four_role_single_account_case():
    """预检红灯的 reason 必须与真发拒演的 terminate_reason 同词汇表。"""
    plan = plan_live(_playbook(), _candidates(1), group_key="g1",
                     confirm_live=True, app_config=ARMED,
                     fingerprint_groups=_fps(1), require_outlet=False,
                     hour=14)
    assert plan.ok is False and plan.reason == "understaffed"


@pytest.mark.asyncio
async def test_plan_live_green_means_perform_actually_starts():
    solo = Playbook(id="solo", name="solo", system="growth",
                    products=("matrixx",), soft_ad_level=2,
                    roles=(Role("advocate"),),
                    beats=tuple(Beat(id=f"b{i}", role="advocate",
                                     intent=_intent(i), soft=0)
                                for i in range(1, 3)))
    plan = plan_live(solo, _candidates(1), group_key="g1",
                     confirm_live=True, app_config=ARMED,
                     fingerprint_groups=_fps(1), require_outlet=False,
                     hour=14)
    assert plan.ok and plan.solo
    res, sender, _ = await _perform(playbook=solo, candidates=_candidates(1),
                                    fingerprint_groups=_fps(1))
    assert res.sent == 2 and sender.calls


def test_is_solo_playbook_only_counts_roles_that_have_beats():
    pb = Playbook(id="q", name="q", system="growth", products=("matrixx",),
                  soft_ad_level=2,
                  roles=(Role("advocate"), Role("skeptic")),
                  beats=(Beat(id="b1", role="advocate", intent="说", soft=0),))
    assert is_solo_playbook(pb) is True


def test_shipped_solo_matrixx_playbook_loads_and_is_solo():
    from src.companion.group_show.playbook import load_playbook, validate_playbook

    path = (Path(__file__).resolve().parents[1]
            / "config" / "playbooks" / "solo_matrixx.yaml")
    if not path.is_file():
        pytest.skip("solo_matrixx.yaml 未部署")
    pb = load_playbook(path)
    assert pb is not None and is_solo_playbook(pb)
    hard = [w for w in validate_playbook(pb) if not w.startswith("warn:")]
    assert not hard


def test_plan_live_surfaces_understaffed_even_during_quiet_hours():
    """禁演时段不能遮住缺角——否则深夜预检永远只说「别演」，修完时段又撞墙。"""
    plan = plan_live(_playbook(), _candidates(1), group_key="g1",
                     confirm_live=True, app_config=ARMED,
                     fingerprint_groups=_fps(1), require_outlet=False,
                     hour=3)
    assert plan.ok is False and plan.reason == "understaffed"
    assert plan.casting is not None
    assert plan.in_quiet_hours is True
    assert any("禁演" in w for w in plan.warnings)


def test_plan_live_check_mode_does_not_need_an_outlet():
    """--check 不该因为没传编排器就红——它本来就不发。"""
    solo = Playbook(id="solo", name="solo", system="growth",
                    products=("matrixx",), soft_ad_level=2,
                    roles=(Role("advocate"),),
                    beats=(Beat(id="b1", role="advocate", intent="说", soft=0),))
    plan = plan_live(solo, _candidates(1), confirm_live=True, app_config=ARMED,
                     fingerprint_groups=_fps(1), require_outlet=False, hour=14)
    assert plan.ok and plan.has_outlet is False
    assert plan.in_quiet_hours is False


# ── 真人插话：让路 + 接管熔断 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_yields_the_floor_when_a_human_speaks():
    """真人插一句 → 让路窗内不抢话，窗过后再演（与排练 human_script 同序）。"""
    fed = {"done": False}

    def feed(*, since_ts=0.0):
        if fed["done"]:
            return []
        fed["done"] = True
        return [("老王", "你们说的是啥工具", 1001.0)]

    res, sender, clock = await _perform(
        human_feed=feed,
        config={"human_yield_seconds": 2, "human_takeover_lines": 99},
    )
    assert res.humans_observed >= 1
    assert res.human_yields >= 1
    assert 2.0 in clock.slept
    assert res.sent > 0 and res.terminate_reason == "completed"


@pytest.mark.asyncio
async def test_live_human_takeover_is_the_best_ending():
    """连续真人发言达阈值 → human_takeover 收场，一条都不必硬发。"""
    def feed(*, since_ts=0.0):
        if since_ts >= 1003:
            return []
        return [("a", "嗯", 1001.0), ("b", "我也觉得", 1002.0),
                ("c", "链接发我", 1003.0)]

    res, sender, _ = await _perform(human_feed=feed)
    assert res.terminate_reason == "human_takeover"
    assert res.humans_observed >= 3
    assert sender.calls == []


# ── 十、复读闸 ──────────────────────────────────────────────────────────────
#
# 2026-07-27 首场灰度实录：LLM 看得见自己刚说过的话，仍把 b3 的卖点换个句式在 b4
# 又安利了一遍。同一场戏里反复念同一个点，是比发言频率更早暴露的水军特征。


@pytest.mark.asyncio
async def test_a_repeated_line_is_retried_with_a_ban_and_the_fresh_take_is_sent():
    """复读被抓 → 带「别再说这个点」禁令重试一次 → 重试出了新内容就用新内容。"""
    texts = iter([
        "工具后台收消息真的省心",        # 拍1：正常发出
        "后台收消息工具真的省心啊",      # 拍2 初稿：与拍1雷同
        "周末打算去山里走走透透气",      # 拍2 重试：全新内容
    ])
    directives = []

    def gen(_speaker, _history, directive):
        directives.append(directive)
        return next(texts)

    res, sender, _ = await _perform(generate=gen, playbook=_playbook(2))
    assert res.sent == 2 and res.skipped == 0
    assert res.repetition_retries == 1
    assert len(sender.calls) == 2
    assert sender.calls[1][1] == "周末打算去山里走走透透气", "发出的必须是重试稿"
    # 重试指令必须带上防复读禁令（must_not 会被 ecp 渲染进【绝对不要】）
    assert len(directives) == 3
    assert any("说过" in ban for ban in directives[2].must_not)
    assert not any("说过" in ban for ban in directives[0].must_not), \
        "首稿不该背禁令——禁令只在抓到复读后追加"


@pytest.mark.asyncio
async def test_a_stubborn_repeat_is_dropped_not_sent():
    """重试后仍雷同 → 弃拍。宁可戏少一句，也不把复读发进真群。"""
    def gen(*_a):
        return "工具后台收消息真的省心"   # 每拍每稿都复读同一句

    res, sender, _ = await _perform(generate=gen, playbook=_playbook(3))
    assert res.sent == 1, "只有第一条允许出去"
    assert len(sender.calls) == 1, "平台侧只该见过一条"
    assert res.repetition_retries >= 1
    assert res.skipped >= 1, "顽固复读走既有弃拍电路，连续弃拍有 generation_failed 兜底"


@pytest.mark.asyncio
async def test_distinct_lines_pay_zero_retry_cost():
    """各拍内容互异时复读闸零开销：不重试、不弃拍——闸门不能向正常场次收税。"""
    texts = iter(["今天路上堵到怀疑人生", "中午吃了家新开的柳州螺蛳粉",
                  "下午的会开到一半停电了", "晚上准备补个剧早点睡"])

    def gen(*_a):
        return next(texts)

    res, sender, _ = await _perform(generate=gen)
    assert res.sent == 4
    assert res.repetition_retries == 0 and res.skipped == 0
    assert len(sender.calls) == 4
