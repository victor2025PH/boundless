"""排练引擎门禁 —— 整包整合测试 + 一条架构级安全钉子。

最重要的一条是 :func:`test_runtime_cannot_send_real_messages`：排练的安全性靠
**架构**（模块根本不 import 发送出口）而不是靠「记得把开关关上」。这条测试防的是
将来某次「顺手加个真发分支」把排练变成真发——那一次事故的代价是账号。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.companion.group_show.playbook import (
    Beat,
    Playbook,
    Role,
    load_playbook_dir,
)
from src.companion.group_show.runtime import (
    RehearsalResult,
    flatten_messages,
    format_rehearsal,
    rehearse,
    stub_generator,
)

_PLAYBOOK_DIR = Path(__file__).resolve().parents[1] / "config" / "playbooks"
_RUNTIME_SRC = (Path(__file__).resolve().parents[1] / "src" / "companion"
                / "group_show" / "runtime.py")


def _candidates(n: int = 4):
    """n 个互不同指纹组的健康号（指纹组不同才能同台）。"""
    return [
        {"account_id": f"a{i}", "platform": "telegram",
         "persona_id": f"p{i}", "display_name": f"演员{i}",
         "health": "online", "fingerprint_group": f"fp{i}"}
        for i in range(1, n + 1)
    ]


def _playbook(n_beats: int = 4) -> Playbook:
    roles = (Role("asker"), Role("advocate"), Role("bystander"), Role("skeptic"))
    slots = ("asker", "advocate", "skeptic", "bystander")
    beats = tuple(
        Beat(id=f"b{i}", role=slots[(i - 1) % 4], intent=f"意图{i}",
             product="matrixx" if i % 2 == 0 else "", soft=0 if i == 1 else 3)
        for i in range(1, n_beats + 1)
    )
    return Playbook(id="pb", name="pb", system="growth", products=("matrixx",),
                    soft_ad_level=4, roles=roles, beats=beats)


# ── 架构级安全钉子 ──────────────────────────────────────────────────────────


def test_runtime_cannot_send_real_messages():
    """排练模块**物理上**不可能发消息：源码里不得出现任何发送出口的引用。

    这不是风格检查——它是 Phase 1「零封号风险」承诺的唯一强制手段。真发链路
    （Phase 3）应另建模块，届时唯一出口仍是 orchestrator.send（自带反封号闸）。
    """
    src = _RUNTIME_SRC.read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#"))
    # 去掉文档字符串块，只看真代码（docstring 里会解释「为什么不 import」）
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    for forbidden in ("AccountOrchestrator", "account_orchestrator",
                      "orch.send", "send_via_adapters", "emit_incoming"):
        assert forbidden not in code, f"排练模块不得引用发送出口: {forbidden}"


# ── 主循环 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rehearsal_plays_full_arc():
    r = await rehearse(_playbook(4), _candidates(4), group_key="g1")
    assert isinstance(r, RehearsalResult)
    assert r.terminate_reason == "completed"
    assert r.line_count == 4
    assert r.dry_run is True
    # 时刻表严格递增＝节奏引擎在真的排期
    times = [ln.at_seconds for ln in r.lines]
    assert times == sorted(times) and times[0] > 0


@pytest.mark.asyncio
async def test_rehearsal_is_deterministic_with_same_seed():
    """同 seed 必须完全复现——排练结果要能拿去对比「改了剧本有没有变好」。"""
    a = await rehearse(_playbook(6), _candidates(4), seed=7)
    b = await rehearse(_playbook(6), _candidates(4), seed=7)
    assert [(l.account_id, l.text, l.at_seconds) for l in a.lines] == \
           [(l.account_id, l.text, l.at_seconds) for l in b.lines]


@pytest.mark.asyncio
async def test_different_seed_changes_rhythm_not_cast():
    a = await rehearse(_playbook(6), _candidates(4), seed=1)
    b = await rehearse(_playbook(6), _candidates(4), seed=2)
    assert [l.account_id for l in a.lines] == [l.account_id for l in b.lines]
    assert [l.at_seconds for l in a.lines] != [l.at_seconds for l in b.lines]


@pytest.mark.asyncio
async def test_no_account_speaks_twice_in_a_row():
    """真人群里几乎没人自己接自己——导演的重排逻辑在整合链路上也必须生效。"""
    r = await rehearse(_playbook(8), _candidates(4), seed=3)
    said = [ln.account_id for ln in r.lines if ln.kind != "human"]
    assert all(said[i] != said[i + 1] for i in range(len(said) - 1)), said


# ── 真人插话排练 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_human_cue_is_recorded_and_answered():
    """真人插话后，下一条我方发言必须是**回应真人**而不是继续念稿。"""
    r = await rehearse(
        _playbook(6), _candidates(4), seed=5,
        human_script=[(2, "真人甲", "这个会不会封号啊？")])
    kinds = [ln.kind for ln in r.lines]
    assert "human" in kinds
    idx = kinds.index("human")
    after = r.lines[idx + 1]
    assert "回应真人" in after.text, r.lines


@pytest.mark.asyncio
async def test_human_takeover_ends_the_show_early():
    """真人连着聊起来 → 戏优雅退场（这是成功不是失败）。"""
    r = await rehearse(
        _playbook(10), _candidates(4), seed=5,
        config={"human_takeover_lines": 2},
        human_script=[(1, "真人甲", "哈哈"), (1, "真人乙", "确实")])
    assert r.terminate_reason == "human_takeover"
    assert r.line_count < 10


@pytest.mark.asyncio
async def test_yield_window_pushes_the_clock():
    """让路窗要真的花掉虚拟时间——否则「不抢话」只是嘴上说说。"""
    quiet = await rehearse(_playbook(4), _candidates(4), seed=5)
    noisy = await rehearse(
        _playbook(4), _candidates(4), seed=5,
        config={"human_yield_seconds": 300, "human_takeover_lines": 99},
        human_script=[(1, "真人甲", "在吗")])
    assert noisy.duration_seconds > quiet.duration_seconds + 200


# ── 降级与容错 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_playbook_does_not_play():
    bad = Playbook(id="bad", name="bad",
                   roles=(Role("asker"),),
                   beats=(Beat(id="b1", role="ghost", intent="x"),))
    r = await rehearse(bad, _candidates(4))
    assert r.terminate_reason == "invalid" and r.lines == []


@pytest.mark.asyncio
async def test_no_healthy_candidate_does_not_play():
    dead = [{"account_id": "a1", "health": "offline", "persona_id": "p1"}]
    r = await rehearse(_playbook(4), dead)
    assert r.terminate_reason == "invalid" and r.lines == []


@pytest.mark.asyncio
async def test_persistent_generation_failure_is_reported_not_hidden():
    """生成一直失败必须**显式报错**，不能演成空场还说「正常收场」。

    2026-07-25 首次真 LLM 排练踩的坑：配置没 await → 10 拍全失败，结果却显示
    ``completed 共 0 条``。空场伪装成成功是最坏的失败模式——人会以为排练通过了。
    """
    r = await rehearse(_playbook(8), _candidates(4),
                       generate=lambda s, m, d: "")
    assert r.line_count == 0
    assert r.terminate_reason == "generation_failed"
    assert r.skipped >= 3


@pytest.mark.asyncio
async def test_occasional_generation_failure_does_not_abort():
    """偶发单拍失败只跳过这一拍，戏照演——连续失败才判定生成侧挂了。"""
    calls = {"n": 0}

    def _flaky(speaker, messages, directive):
        calls["n"] += 1
        return "" if calls["n"] == 2 else f"台词{calls['n']}"

    r = await rehearse(_playbook(6), _candidates(4), generate=_flaky)
    assert r.terminate_reason == "completed"
    assert r.skipped == 1 and r.line_count == 5


@pytest.mark.asyncio
async def test_async_generator_is_awaited():
    async def _agen(speaker, messages, directive):
        return f"异步-{directive.beat_id}"

    r = await rehearse(_playbook(3), _candidates(4), generate=_agen)
    assert all(ln.text.startswith("异步-") for ln in r.lines)


@pytest.mark.asyncio
async def test_generator_receives_projected_messages():
    """生成器拿到的必须是 ECP 投影后的 messages（自己的话是 assistant）。"""
    seen = {}

    def _gen(speaker, messages, directive):
        seen[directive.beat_id] = messages
        return f"台词{directive.beat_id}"

    await rehearse(_playbook(4), _candidates(4), generate=_gen)
    first = seen["b1"]
    assert first and first[0]["role"] == "system"
    assert "群聊" in first[0]["content"]


# ── 落库 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_receives_session_and_events(tmp_path):
    from src.companion.group_show.store import GroupShowStore

    store = GroupShowStore(tmp_path / "gs.db")
    r = await rehearse(_playbook(4), _candidates(4), store=store,
                       session_id="sid1", group_key="g9")
    row = store.load_session("sid1")
    assert row is not None and row["group_key"] == "g9"
    assert len(store.events("sid1")) == r.line_count


@pytest.mark.asyncio
async def test_broken_store_does_not_break_rehearsal():
    """落库是旁路能力，坏了也不能白演一场。"""
    class _Boom:
        def append_event(self, *a):
            raise RuntimeError("db down")

        def save_session(self, *a):
            raise RuntimeError("db down")

    r = await rehearse(_playbook(4), _candidates(4), store=_Boom())
    assert r.line_count == 4


# ── 渲染 ────────────────────────────────────────────────────────────────────


def test_flatten_messages_marks_own_lines():
    out = flatten_messages([
        {"role": "system", "content": "指令"},
        {"role": "assistant", "content": "我说的"},
        {"role": "user", "content": "[群友 小A] 他说的"},
        {"role": "user", "content": "   "},
    ])
    assert "（你之前说过）我说的" in out
    assert "[群友 小A] 他说的" in out
    assert out.count("\n\n") == 2   # 空内容被丢弃


@pytest.mark.asyncio
async def test_format_rehearsal_is_readable():
    r = await rehearse(_playbook(4), _candidates(4), seed=1,
                       human_script=[(2, "真人甲", "靠谱吗？")])
    text = format_rehearsal(r)
    assert "演员表" in text and "收场" in text and "自然度" in text
    assert re.search(r"\[\d{2}:\d{2}\]", text)
    assert "👤 真人甲" in text


# ── 出货模板真排练 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("pid", ["growth_matrixx", "lingo_lingox",
                                 "studio_voicex"])
@pytest.mark.asyncio
async def test_shipped_playbooks_actually_rehearse(pid):
    """每套出货模板都必须能被真正演完——这是「交付物可用」的端到端证明。"""
    pb = load_playbook_dir(_PLAYBOOK_DIR)[pid]
    r = await rehearse(pb, _candidates(4), seed=11,
                       generate=stub_generator())
    assert r.terminate_reason == "completed"
    assert r.line_count == len(pb.beats)
    assert r.duration_seconds > 60, "整场戏不该几十秒演完，那节奏一眼假"
