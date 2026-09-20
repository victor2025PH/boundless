"""影子模式门禁 —— 一条架构级安全钉子 + 「沉默也算决策」的行为不变量。

最重要的是 :func:`test_shadow_cannot_send_real_messages`：影子模式的全部价值
建立在「它物理上发不出消息」之上。这条测试防的是将来某次「顺手加个真发分支」
把观察变成真发——那一次事故的代价是账号，不是一个 bug。

其余用例大多在测**行为常识**：真人刚说完话不许秒回、被抑制的沉默必须被记下来、
真人自己聊起来了戏就该收、生成侧挂了不许装作在观察。任一条回归，影子模式给出的
读数就会骗人——而影子模式唯一的产出就是读数。

全部用例注入时钟，**不 sleep 一秒**：让路窗过没过、下一拍到点没到，都是纯时间
条件，用真实等待来验它们是把 CI 时长换成零信息量。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from src.companion.group_show.playbook import (
    Beat,
    CastMember,
    Casting,
    Playbook,
    Role,
)
from src.companion.group_show.shadow import (
    ShadowDecision,
    ShadowRunner,
    format_decision,
    format_report,
)

T0 = 1_000_000.0

_SHADOW_SRC = (Path(__file__).resolve().parents[1] / "src" / "companion"
               / "group_show" / "shadow.py")
_CLI_SRC = (Path(__file__).resolve().parents[1] / "scripts"
            / "group_show_shadow.py")

_FORBIDDEN = ("AccountOrchestrator", "account_orchestrator", "orch.send",
              "send_via_adapters", "emit_incoming")


class _Clock:
    """可控时间源——测试用它拨表，代替 sleep。"""

    def __init__(self, start: float = T0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def _playbook(n_beats: int = 4) -> Playbook:
    slots = ("asker", "advocate", "skeptic", "bystander")
    beats = tuple(
        Beat(id=f"b{i}", role=slots[(i - 1) % 4], intent=f"意图{i}",
             product="matrixx" if i % 2 == 0 else "", soft=0 if i == 1 else 3)
        for i in range(1, n_beats + 1)
    )
    return Playbook(id="pb", name="pb", system="growth", products=("matrixx",),
                    soft_ad_level=4,
                    roles=(Role("asker"), Role("advocate"), Role("bystander"),
                           Role("skeptic")),
                    beats=beats)


def _casting() -> Casting:
    return Casting(members=(
        CastMember("asker", "a1", "p_a", "小A"),
        CastMember("advocate", "a2", "p_b", "小B"),
        CastMember("bystander", "a3", "p_c", "小C"),
        CastMember("skeptic", "a4", "p_d", "小D"),
    ))


def _runner(clock: _Clock, *, n_beats: int = 4, config=None, generate=None,
            store=None) -> ShadowRunner:
    return ShadowRunner(
        _playbook(n_beats), _casting(), group_key="tg:-100123",
        config=config, generate=generate, store=store, clock=clock)


# ── 架构级安全钉子 ──────────────────────────────────────────────────────────


def _strip_docs_and_comments(src: str) -> str:
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    return re.sub(r'"""[\s\S]*?"""', "", code)


def test_shadow_cannot_send_real_messages():
    """影子模块**物理上**不可能发消息：源码里不得出现任何发送出口的引用。

    这不是风格检查——它是「影子模式零封号风险」这句承诺的唯一强制手段。真发
    链路是另一个模块的事，届时唯一出口仍是编排器的发送方法（自带反封号闸）。
    """
    code = _strip_docs_and_comments(_SHADOW_SRC.read_text(encoding="utf-8"))
    for forbidden in _FORBIDDEN:
        assert forbidden not in code, f"影子模块不得引用发送出口: {forbidden}"


def test_shadow_cli_cannot_send_real_messages():
    """CLI 同样不许有发送出口——它才是真正被人手动跑起来的那个入口。"""
    code = _strip_docs_and_comments(_CLI_SRC.read_text(encoding="utf-8"))
    for forbidden in _FORBIDDEN:
        assert forbidden not in code, f"影子 CLI 不得引用发送出口: {forbidden}"


# ── 真人插话：让路即沉默，沉默要留痕 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_human_message_inside_yield_window_is_suppressed():
    """真人刚说完话 → 不抢话，但这次「选择沉默」必须被记成一条决策。"""
    clock = _Clock()
    r = _runner(clock)
    d = await r.on_inbound(sender_id="u1", sender_name="老王",
                           text="这个会不会封号啊？", ts=clock.advance(5))
    assert isinstance(d, ShadowDecision)
    assert d.suppressed == "yield"
    assert d.spoken is False
    assert d.text == "", "被抑制的决策不该烧 LLM 生成台词"
    # 但「本该谁开口」仍要留下来——运营要看的就是这个
    assert d.account_id and d.beat_id
    assert r.report()["suppressed_by_reason"] == {"yield": 1}


@pytest.mark.asyncio
async def test_human_message_outside_yield_window_gets_real_decision():
    """让路窗外（这里配成 0 秒）→ 产出真决策，且是**回应真人**而不是念稿。"""
    clock = _Clock()
    r = _runner(clock, config={"human_yield_seconds": 0})
    d = await r.on_inbound(sender_id="u1", sender_name="老王",
                           text="这个会不会封号啊？", ts=clock.advance(5))
    assert d is not None and d.suppressed == ""
    assert d.spoken is True
    assert d.reason == "respond_human", d
    assert "回应真人" in d.text, d.text
    assert r.report()["would_send"] == 1


@pytest.mark.asyncio
async def test_yield_window_is_not_reported_once_per_tick():
    """让路窗内每个 tick 都记一条会把报表刷爆——一次沉默只该记一次。"""
    clock = _Clock()
    r = _runner(clock)
    await r.on_inbound(sender_id="u1", sender_name="老王", text="在吗",
                       ts=clock.advance(5))
    assert await r.tick(now=clock.advance(1)) is None
    assert await r.tick(now=clock.advance(1)) is None
    assert r.report()["suppressed"] == 1


@pytest.mark.asyncio
async def test_reply_comes_after_the_yield_window_passes():
    """窗过之后的那一拍要真去回应真人——不能装作没看见继续念稿。"""
    clock = _Clock()
    r = _runner(clock, config={"human_yield_seconds": 45})
    await r.on_inbound(sender_id="u1", sender_name="老王",
                       text="这个多少钱？", ts=clock.advance(5))
    d = await r.tick(now=clock.advance(60))
    assert d is not None and d.spoken
    assert d.reason == "respond_human"
    assert "回应真人" in d.text


# ── 自发推进：群里没人说话，戏也要能往下走 ──────────────────────────────────


@pytest.mark.asyncio
async def test_tick_advances_the_show_when_nobody_talks():
    clock = _Clock()
    r = _runner(clock, n_beats=4)
    first = await r.tick()
    assert first is not None and first.spoken
    assert first.beat_id == "b1" and first.reason == "beat"

    # 还没到下一拍的点：真人不会每 10 秒说一句，这里返回 None 是对的
    assert await r.tick(now=clock.advance(1)) is None

    second = await r.tick(now=clock.advance(700))
    assert second is not None and second.spoken
    assert second.beat_id != first.beat_id
    assert second.account_id != first.account_id, "真人不会自己接自己的话"
    assert r.report()["would_send"] == 2


@pytest.mark.asyncio
async def test_show_terminates_when_all_beats_played():
    clock = _Clock()
    r = _runner(clock, n_beats=2)
    for _ in range(2):
        assert (await r.tick(now=clock.advance(700))).spoken
    end = await r.tick(now=clock.advance(700))
    assert end is not None and end.suppressed == "terminate:completed"
    assert r.terminated and r.terminate_reason == "completed"
    # 收场只报一次，之后安静
    assert await r.tick(now=clock.advance(700)) is None


# ── 真人接管：最好的结局 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_human_takeover_suppresses_everything_after():
    """真人连着刷屏 → 导演判接管 → 后续一切决策都带 terminate 抑制。"""
    clock = _Clock()
    r = _runner(clock, config={"human_takeover_lines": 2})
    first = await r.on_inbound(sender_id="u1", sender_name="老王", text="哈哈",
                               ts=clock.advance(5))
    assert first.suppressed == "yield"

    second = await r.on_inbound(sender_id="u2", sender_name="老李", text="确实",
                                ts=clock.advance(5))
    assert second is not None
    assert second.suppress_kind == "terminate"
    assert second.suppressed == "terminate:human_takeover"
    assert r.terminated and r.terminate_reason == "human_takeover"

    # 收场之后不许再产出任何「该说话」的决策
    assert await r.tick(now=clock.advance(700)) is None
    rep = r.report()
    assert rep["would_send"] == 0
    assert rep["suppressed_by_reason"] == {"yield": 1, "terminate": 1}


# ── 报表 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_structure_and_counts():
    clock = _Clock()
    r = _runner(clock, n_beats=4)
    await r.tick()                                   # 1 条真决策
    await r.on_inbound(sender_id="u1", sender_name="老王", text="在吗",
                       ts=clock.advance(30))          # 1 次让路沉默
    await r.tick(now=clock.advance(700))              # 窗过 → 1 条真决策

    rep = r.report()
    for key in ("session_id", "playbook_id", "group_key", "platform",
                "dry_run", "started_at", "last_at", "duration_seconds",
                "decisions", "would_send", "suppressed",
                "suppressed_by_reason", "human_messages", "skipped",
                "terminate_reason", "beats_total", "beats_played",
                "speakers", "naturalness"):
        assert key in rep, f"report() 缺字段: {key}"

    assert rep["dry_run"] is True
    assert rep["playbook_id"] == "pb"
    assert rep["group_key"] == "tg:-100123"
    assert rep["would_send"] == 2
    assert rep["suppressed"] == 1
    assert rep["suppressed_by_reason"] == {"yield": 1}
    assert rep["decisions"] == len(r.decisions) == 3
    assert rep["human_messages"] == 1
    assert rep["skipped"] == 0
    assert rep["beats_total"] == 4 and rep["beats_played"] == 2
    assert sum(rep["speakers"].values()) == 2
    # 时间跨度＝真实观察时长（注入时钟推了 730 秒）
    assert rep["duration_seconds"] == pytest.approx(730.0, abs=1.0)


@pytest.mark.asyncio
async def test_report_naturalness_is_honest_about_small_samples():
    """两条台词算不出可信的自然度——报表必须说「样本不足」而不是给个假分。"""
    clock = _Clock()
    r = _runner(clock, n_beats=2)
    for _ in range(2):
        await r.tick(now=clock.advance(700))
    nat = r.report()["naturalness"]
    assert nat.get("ok") is False and nat.get("verdict") == "insufficient"


@pytest.mark.asyncio
async def test_report_scores_naturalness_when_enough_lines():
    clock = _Clock()
    r = _runner(clock, n_beats=6)
    for _ in range(6):
        await r.tick(now=clock.advance(700))
    nat = r.report()["naturalness"]
    assert nat.get("ok") is True
    assert 0.0 <= float(nat.get("score", -1)) <= 1.0


# ── 降级与容错 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_generation_yields_no_decision_and_does_not_crash():
    """生成器返空：不产出决策、不崩；连着失败才判定生成侧挂了。"""
    clock = _Clock()
    r = _runner(clock, n_beats=8, generate=lambda s, m, d: "")
    assert await r.tick() is None
    assert r.report()["skipped"] == 1
    assert r.decisions == []

    for _ in range(2):
        assert await r.tick(now=clock.advance(700)) is None
    assert r.terminated and r.terminate_reason == "generation_failed"


@pytest.mark.asyncio
async def test_raising_generator_is_swallowed():
    def _boom(speaker, messages, directive):
        raise RuntimeError("LLM down")

    clock = _Clock()
    r = _runner(clock, generate=_boom)
    assert await r.tick() is None
    assert r.report()["skipped"] == 1


@pytest.mark.asyncio
async def test_async_generator_is_awaited():
    async def _agen(speaker, messages, directive):
        return f"异步-{directive.beat_id}"

    clock = _Clock()
    d = await _runner(clock, generate=_agen).tick()
    assert d is not None and d.text == "异步-b1"


@pytest.mark.asyncio
async def test_suppressed_decisions_never_call_the_generator():
    """沉默不该烧 LLM——被抑制时一次生成都不许发生。"""
    calls = {"n": 0}

    def _gen(speaker, messages, directive):
        calls["n"] += 1
        return "台词"

    clock = _Clock()
    r = _runner(clock, generate=_gen)
    await r.on_inbound(sender_id="u1", sender_name="老王", text="在吗",
                       ts=clock.advance(5))
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_generator_sees_human_line_as_group_message():
    """真人原话必须以 ECP 的「群友」视角进上下文，且带**显示名**而不是 account_id。"""
    seen = {}

    def _gen(speaker, messages, directive):
        seen["messages"] = messages
        return "台词"

    clock = _Clock()
    r = _runner(clock, config={"human_yield_seconds": 0}, generate=_gen)
    await r.on_inbound(sender_id="u10086", sender_name="老王", text="这个靠谱吗？",
                       ts=clock.advance(5))
    dump = "\n".join(m["content"] for m in seen["messages"])
    assert "[群友 老王] 这个靠谱吗？" in dump
    assert "u10086" not in dump


@pytest.mark.asyncio
async def test_broken_store_does_not_break_observation():
    class _Boom:
        def append_event(self, *a):
            raise RuntimeError("db down")

        def save_session(self, *a):
            raise RuntimeError("db down")

    clock = _Clock()
    r = _runner(clock, store=_Boom())
    assert (await r.tick()).spoken
    assert r.report()["would_send"] == 1


@pytest.mark.asyncio
async def test_store_receives_would_be_lines(tmp_path):
    """落库存的是「真发时会发出去的那些话」——回放与归因的唯一素材。"""
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(tmp_path / "gs.db")
    clock = _Clock()
    r = _runner(clock, n_beats=3, store=store)
    for _ in range(3):
        await r.tick(now=clock.advance(700))
    r.report()

    row = store.load_session(r.session_id)
    assert row is not None and row["group_key"] == "tg:-100123"
    assert row["dry_run"] is True
    kinds = [e["kind"] for e in store.events(r.session_id)]
    assert kinds.count("line") + kinds.count("media") == 3


@pytest.mark.asyncio
async def test_clock_defaults_to_wall_time_without_injection():
    """不注入时钟也要能跑（生产就是这么用的）。"""
    r = ShadowRunner(_playbook(2), _casting(), group_key="g")
    d = await r.tick()
    assert d is not None and d.at > 0


# ── 渲染 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_format_marks_suppressed_decisions_distinctly():
    clock = _Clock()
    r = _runner(clock)
    spoken = await r.tick()
    silent = await r.on_inbound(sender_id="u1", sender_name="老王", text="在吗",
                                ts=clock.advance(700))
    assert "[抑制:yield]" in format_decision(silent)
    assert "[抑制:" not in format_decision(spoken)
    text = format_report(r.report())
    assert "本该发出" in text and "主动沉默" in text


# ── CLI 开演前的三条体检（这三个函数真在改选角，不是打印装饰） ────────────────


@pytest.fixture(scope="module")
def cli():
    """按路径加载 ``scripts/group_show_shadow.py``（脚本不在包里，只能这么进）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_gs_shadow_cli", _CLI_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stock_ledger(store, *, groups: int, speakers_per_group: int, pool, ts):
    """往库里灌 ``groups`` 场真演出，每场 ``speakers_per_group`` 个号开口。"""
    from src.companion.group_show.playbook import Casting, Playbook, ShowEvent, ShowState

    n = len(pool)
    for g in range(groups):
        speakers = [pool[(g * speakers_per_group + k) % n]
                    for k in range(speakers_per_group)]
        state = ShowState(session_id=f"s{g}", group_key=f"grp{g}", platform="telegram",
                          playbook=Playbook(id="pb", name="pb"), casting=Casting(),
                          dry_run=False, started_at=ts)
        state.events = [
            ShowEvent(seq=i + 1, ts=ts, speaker_account=a, role="advocate",
                      beat_id="b1", text="hi", kind="line")
            for i, a in enumerate(speakers)
        ]
        store.save_session(state)
        for a in speakers:
            store.record_membership(f"grp{g}", a, source="manual")


def _plan(cli, store, pool, **kw):
    """开演前那一步：读账 → 拼参 → 出提示行（真发链路将来走的是同一条）。"""
    ctx = cli._show_context(store, None, pool, group_key="grp_new")   # noqa: SLF001
    cap = ctx.speaker_cap(requested=kw.get("requested", 0),
                          allow_over=kw.get("allow_over", False))
    return ctx, cap, cli._axis_notes(ctx, cap)                        # noqa: SLF001


def test_cli_helpers_soft_fail_without_a_store(cli):
    """``--db`` 没给就该安静退化——体检是旁路能力，不能把整场戏挡在门外。"""
    assert cli._open_store(None) is None          # noqa: SLF001
    assert cli._cast_attendance_problems(_casting(), None, "g") == []    # noqa: SLF001
    ctx, cap, notes = _plan(cli, None, ["a"])
    assert cap["effective"] == 0 and ctx.co_performance == {} and notes == []
    # 没台账 ＝ 不挂角色闸门，与这条轴上线前一字不差（凭空造约束比不拦更坏）
    assert ctx.role_limit == 0 and ctx.cast_kwargs()["max_speakers"] == 0


def test_cli_co_performance_decays_outside_the_window(cli, tmp_path):
    """共现必须随时间淡出：不衰减的话跑几个月每一对都饱和，排序就没有分辨力了。"""
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(tmp_path / "gs.db")
    pool = [f"a{i}" for i in range(4)]
    _stock_ledger(store, groups=6, speakers_per_group=2, pool=pool,
                  ts=time.time() - 200 * 86400)     # 200 天前，早于缺省 30 天窗
    try:
        assert _plan(cli, store, pool)[0].co_performance == {}
        _stock_ledger(store, groups=6, speakers_per_group=2, pool=pool,
                      ts=time.time() - 3600)        # 窗内再灌一批
        assert _plan(cli, store, pool)[0].co_performance
    finally:
        store.close()


def test_cli_pushes_an_over_budget_cast_back_into_budget(cli, tmp_path):
    """默认走安全档：超预算**真的把选角压回去**，而不是打一行 warn 就照演。"""
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(tmp_path / "gs.db")
    pool = [f"a{i}" for i in range(10)]
    _stock_ledger(store, groups=40, speakers_per_group=3, pool=pool,
                  ts=time.time() - 3600)
    try:
        ctx, cap, notes = _plan(cli, store, pool, requested=4)
        assert cap["capped"] is True and 0 < cap["effective"] < 4
        assert any("--over-budget" in s for s in notes), \
            "压回去了就必须告诉运营怎么突破，否则他只会去关护栏"
        # 压回去的数必须真的进到选角入参里，否则这一切只是打了行字
        assert ctx.cast_kwargs(requested=4)["max_speakers"] == cap["effective"]
    finally:
        store.close()


def test_cli_over_budget_flag_is_honoured_and_audited(cli, tmp_path):
    """越界是运营的权力，但必须是**他主动按的**，而且要留一行能事后查的话。"""
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(tmp_path / "gs.db")
    pool = [f"a{i}" for i in range(10)]
    _stock_ledger(store, groups=40, speakers_per_group=3, pool=pool,
                  ts=time.time() - 3600)
    try:
        _ctx, cap, notes = _plan(cli, store, pool, requested=4, allow_over=True)
        assert cap["effective"] == 4 and cap["source"] == "override"
        assert any("放行" in s for s in notes)
    finally:
        store.close()


def test_cli_says_so_when_a_ledger_could_not_be_read(cli):
    """少读一本账 → 共现表变空 → 长得跟「一切安全」一模一样。这个必须说破。"""
    class _Boom:
        def performance_ledger(self, **kw):
            raise RuntimeError("db gone")

    ctx, cap, notes = _plan(cli, _Boom(), ["a"])
    assert "shows" in ctx.degraded
    assert any("台账少读了" in s for s in notes), "一片绿最危险的地方是没人会怀疑它"


def test_cli_explains_that_an_empty_slot_came_from_the_gate_not_a_shortage(cli):
    """「缺角」看着像号少 → 运营去补号 → 完全没用。挡人的是闸门就得直说。"""
    from src.companion.group_show.attendance import NORMAL_PAIR_CO
    from src.companion.group_show.casting import cast_roles
    from src.companion.group_show.playbook import Playbook, Role

    pb = Playbook(id="pb", name="pb",
                  roles=(Role(slot="advocate", desc="a"), Role(slot="asker", desc="b")))
    actors = [{"account_id": f"a{i}", "platform": "telegram", "persona_id": f"p{i}",
               "health": "online", "fingerprint_group": f"fp{i}"} for i in range(2)]
    counts = {("a0", "a1"): NORMAL_PAIR_CO}
    casting = cast_roles(pb, actors, co_performance=counts)
    assert len(casting.members) == 1        # 闸门把第二个槽挡了

    note = cli._gate_note(casting)                               # noqa: SLF001
    assert note and "补号没用" in note[0]
    assert "asker" in note[0], "要指名是哪个槽，否则运营不知道动哪儿"

    # 号真的用完时不该乱指闸门（此时空槽的原因就是号少）
    short = cast_roles(pb, actors[:1], co_performance=counts)
    assert cli._gate_note(short) == []                           # noqa: SLF001
    # 预算压出来的空槽由 _axis_notes 负责解释，这里别重复说
    capped = cast_roles(pb, actors, co_performance=counts, max_speakers=1)
    assert cli._gate_note(capped) == []                          # noqa: SLF001
    # 没台账＝闸门根本没生效，空槽只能是号不够
    assert cli._gate_note(cast_roles(pb, actors)) == []           # noqa: SLF001


def test_cli_budget_stays_quiet_when_the_cast_fits(cli, tmp_path):
    """没超预算就别刷屏：每场都喊狼来了，真超的那次就没人看了。"""
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(tmp_path / "gs.db")
    pool = [f"a{i}" for i in range(12)]
    _stock_ledger(store, groups=3, speakers_per_group=2, pool=pool,
                  ts=time.time() - 3600)
    try:
        _ctx, cap, notes = _plan(cli, store, pool, requested=2)
        assert cap["capped"] is False and cap["source"] == "explicit"
        assert not [s for s in notes if "--over-budget" in s]
    finally:
        store.close()


def test_cli_auto_caps_when_nobody_passed_max_speakers(cli, tmp_path):
    """不写 --max-speakers 也该受保护：默认值就是预算，而不是「不限」。"""
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(tmp_path / "gs.db")
    pool = [f"a{i}" for i in range(10)]
    _stock_ledger(store, groups=40, speakers_per_group=3, pool=pool,
                  ts=time.time() - 3600)
    try:
        _ctx, cap, notes = _plan(cli, store, pool)
        assert cap["source"] == "auto" and cap["effective"] > 0
        assert any(s.startswith("note:") for s in notes), "自动限了要说一声"
    finally:
        store.close()


# ── 第四条轴：角色集中度 ────────────────────────────────────────────────────


def test_cli_role_plan_reads_the_ledger_and_hands_back_a_live_limit(cli, tmp_path):
    """角色台账要真读到库里去——读不到就等于闸门没挂，而它看起来一切正常。"""
    from src.companion.group_show.roles import NORMAL_ROLE_CO
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(tmp_path / "gs.db")
    pool = [f"a{i}" for i in range(4)]
    _stock_ledger(store, groups=6, speakers_per_group=2, pool=pool,
                  ts=time.time() - 3600)
    try:
        ctx = _plan(cli, store, pool)[0]
        assert ctx.role_limit == NORMAL_ROLE_CO
        assert ctx.role_counts, "库里明明演过 advocate，台账不能是空的"
        assert all(slot == "advocate" for _acct, slot in ctx.role_counts)
        # 闸门参数必须真的带上这两个键，否则台账读了也白读
        kwargs = ctx.cast_kwargs()
        assert kwargs["role_counts"] and kwargs["role_limit"] == NORMAL_ROLE_CO
    finally:
        store.close()


def test_cli_role_gate_actually_changes_who_gets_cast(cli, tmp_path):
    """报账不算接线：饱和的号必须**真的选不上**主推位，否则这条轴只是张好看的卡。"""
    from src.companion.group_show.casting import cast_roles
    from src.companion.group_show.playbook import Playbook, Role
    from src.companion.group_show.roles import NORMAL_ROLE_CO

    pb = Playbook(id="pb", name="pb", roles=(Role(slot="advocate", desc="a"),))
    actors = [{"account_id": a, "platform": "telegram", "persona_id": "p",
               "health": "online", "fingerprint_group": f"fp_{a}"}
              for a in ("burned", "fresh")]
    # burned 已经在 NORMAL_ROLE_CO 个群里当过主推；fresh 一次没有
    counts = {("burned", "advocate"): NORMAL_ROLE_CO}
    picked = cast_roles(pb, actors, role_counts=counts, role_limit=NORMAL_ROLE_CO)
    assert [m.account_id for m in picked.members] == ["fresh"]


def test_cli_gate_note_tells_the_role_gate_apart_from_the_pair_gate(cli):
    """两条闸门的**处方相反**：一个说别补号，一个说补号正好。说错就把人支反了。"""
    from src.companion.group_show.casting import cast_roles
    from src.companion.group_show.playbook import Playbook, Role
    from src.companion.group_show.roles import NORMAL_ROLE_CO

    pb = Playbook(id="pb", name="pb",
                  roles=(Role(slot="advocate", desc="a"),
                         Role(slot="bystander", desc="b")))
    actors = [{"account_id": f"a{i}", "platform": "telegram", "persona_id": "p",
               "health": "online", "fingerprint_group": f"fp{i}"} for i in range(2)]
    roles = {("a0", "advocate"): NORMAL_ROLE_CO, ("a1", "advocate"): NORMAL_ROLE_CO}
    casting = cast_roles(pb, actors, role_counts=roles, role_limit=NORMAL_ROLE_CO)
    assert "advocate" in casting.unfilled, "两个号都饱和了，主推位就该空着"

    note = cli._gate_note(casting)                            # noqa: SLF001
    assert note and "推销角色" in note[0] and "补新号" in note[0]


def test_cli_reports_two_different_gates_separately_not_as_one_verdict(cli):
    """一场戏可以同时撞上两条闸门，而它们的处方**相反**——合成一句话必错一半。

    这正是归因必须留在选角里、不能在外面重新推断的原因：外面只看得到「几个槽空着」，
    看不到每个槽各自是被谁挡的。
    """
    from src.companion.group_show.attendance import NORMAL_PAIR_CO
    from src.companion.group_show.casting import cast_roles
    from src.companion.group_show.playbook import Playbook, Role
    from src.companion.group_show.roles import NORMAL_ROLE_CO

    pb = Playbook(id="pb", name="pb",
                  roles=(Role(slot="advocate", desc="a"),
                         Role(slot="asker", desc="b"),
                         Role(slot="bystander", desc="c")))
    actors = [{"account_id": f"a{i}", "platform": "telegram", "persona_id": "p",
               "health": "online", "fingerprint_group": f"fp{i}"} for i in range(3)]
    # a0 先上（bystander 之外都轮不到它），a1/a2 与它同台已到线 → 号对闸门；
    # 同时所有号的主推位都已饱和 → 主推位是角色闸门挡的。
    casting = cast_roles(
        pb, actors,
        co_performance={("a0", "a1"): NORMAL_PAIR_CO, ("a0", "a2"): NORMAL_PAIR_CO,
                        ("a1", "a2"): NORMAL_PAIR_CO},
        role_counts={(f"a{i}", "advocate"): NORMAL_ROLE_CO for i in range(3)},
        role_limit=NORMAL_ROLE_CO)
    reasons = dict(casting.blocked)
    assert reasons.get("advocate") == "role"
    assert set(reasons.values()) == {"role", "pair"}, "两条闸门要各归各的"
    assert len(cli._gate_note(casting)) == 2, "两个相反的处方不能合并成一句"  # noqa: SLF001


# ── 第五条轴：跨群间隔 ──────────────────────────────────────────────────────


def test_cross_group_watch_flags_an_account_that_just_spoke_elsewhere(cli):
    """真人不会隔十秒换个群接着说。这条是静态特征洗干净之后最后一个把柄。"""
    now = time.time()

    class _Store:
        def last_spoke_at(self, **kw):
            return {"hot": now - 60.0}

    watch = cli.CrossGroupWatch(_Store(), None, group_key="g1")
    assert watch.delay_for("hot", now) > 0
    assert watch.delay_for("nobody", now) == 0, "没记录一律放行（冷启动不能全员禁言）"
    waiting = cli._waiting_notes(watch, ["hot", "nobody"])       # noqa: SLF001
    assert len(waiting) == 1 and "hot" in waiting[0]


def test_cross_group_watch_excludes_the_group_we_are_about_to_play_in(cli):
    """同一个群里连着说两句是正常对话。不排除本群，号会被自己挡住。"""
    seen = {}

    class _Store:
        def last_spoke_at(self, **kw):
            seen.update(kw)
            return {}

    class _Inbox:
        def group_last_spoke_at(self, **kw):
            seen["inbox_exclude"] = kw.get("exclude_group")
            return {}

    cli.CrossGroupWatch(_Store(), _Inbox(), group_key="g1").snapshot()
    assert seen.get("exclude_group") == "g1"
    assert seen.get("inbox_exclude") == "g1"


def test_cross_group_watch_takes_the_later_of_the_two_ledgers(cli):
    """只读编排库会给出「三天没说话」的假读数——日常自动回复同样让号在群里冒头。"""
    now = time.time()

    class _Store:
        def last_spoke_at(self, **kw):
            return {"a": now - 9999.0}

    class _Inbox:
        def group_last_spoke_at(self, **kw):
            return {"a": now - 30.0}

    watch = cli.CrossGroupWatch(_Store(), _Inbox(), group_key="g1")
    assert watch.delay_for("a", now) > 0, "两本账要取更晚的那个，否则闸门形同虚设"


def test_cross_group_watch_survives_a_ledger_that_blows_up(cli):
    """台账读挂了只该少一条提示，不该把整场观察打断。"""
    class _Boom:
        def last_spoke_at(self, **kw):
            raise RuntimeError("db gone")

    watch = cli.CrossGroupWatch(_Boom(), None, group_key="g1")
    assert watch.delay_for("a", time.time()) == 0
    assert watch.waiting(["a"]) == []


def test_cross_group_watch_re_reads_the_ledger_as_the_run_goes_on(cli):
    """影子一挂几十分钟。拿开场那一刻的读数判半小时后的决策 ＝ 闸门关掉一半。"""
    reads = []

    class _Store:
        def last_spoke_at(self, **kw):
            reads.append(1)
            return {}

    watch = cli.CrossGroupWatch(_Store(), None, group_key="g1", ttl=0.0)
    watch.snapshot()
    watch.snapshot()
    assert len(reads) == 2, "TTL 到期必须重读"

    reads.clear()
    warm = cli.CrossGroupWatch(_Store(), None, group_key="g1", ttl=3600.0)
    warm.snapshot()
    warm.snapshot()
    assert len(reads) == 1, "窗口内别反复扫库——决策是稀疏事件，不该按轮询频率压库"


def test_shadow_annotates_but_never_suppresses_a_delayed_line(cli, capsys):
    """影子模式的职责是**如实预演**。替真发少发一条，这份读数就不再是真发的样子。"""
    now = time.time()

    class _Store:
        def last_spoke_at(self, **kw):
            return {"hot": now - 60.0}

    class _Decision:
        account_id, display_name, role = "hot", "阿哲", "advocate"
        at, soft_level, media, suppressed = now, 0, "", ""
        text, reason, beat_id = "来啦", "beat", "b1"

    watch = cli.CrossGroupWatch(_Store(), None, group_key="g1")
    wait = cli._print_decision(_Decision(), watch)      # noqa: SLF001
    out = capsys.readouterr().out
    assert wait > 0 and "顶后" in out
    assert "来啦" in out, "标注归标注，这条台词本身照样要打出来"
