# -*- coding: utf-8 -*-
"""人设发声链门禁 —— 钉死「有人设的声音」与「宁可不说也不说错」两条不变量。

这些用例保护的不是函数行为，而是两类事故：
- 四个号一个味儿（人设没真接上，多号同台一眼假）；
- 群里蹦出「作为一个AI助手」（一句话暴露整批号，且截图会传播）。

全程假 ai_client / 假 persona_manager，不碰真 LLM、不碰真人设库。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.companion.group_show.playbook import (
    Beat,
    BeatDirective,
    CastMember,
    Playbook,
    Role,
)
from src.companion.group_show.runtime import persona_line_generator, rehearse
from src.companion.group_show.voice import (
    build_persona_preamble,
    check_line_violations,
    inject_persona,
    persona_generator,
    resolve_persona_voice,
)

# ── 夹具 ────────────────────────────────────────────────────────────────────

SPEAKER = CastMember(slot="advocate", account_id="acc_a",
                     persona_id="lin_jiaxin", display_name="林佳欣")

#: ECP 投影出来的样子：首条 system（群感知 + 本拍意图 + 禁止项），其后群消息
ECP_MESSAGES: List[Dict[str, str]] = [
    {"role": "system", "content": "【你是谁】你就是群里的「林佳欣」…（群聊场景）"},
    {"role": "user", "content": "[群友 陈美玲] 这个会不会封号啊"},
    {"role": "assistant", "content": "我用了三个月还行"},
]

DIRECTIVE = BeatDirective(beat_id="b3", intent="种草：说说多号切换省了多少事",
                          soft_level=4)

_PROFILE = {
    "name": "林佳欣",
    "role": "做跨境电商的姑娘",
    "personality": {"traits": ["爽快", "话密"], "style": "短句连打，爱用「哈哈」"},
    "speaking": {"forbidden_phrases": ["有什么可以帮您"]},
    "identity": {"deny_ai": True},
}

_AI_LEAK = "作为一个AI助手，我建议你先了解一下产品。"
_SERVICE_TONE = "您好，有什么可以帮您的吗？"


class FakeAI:
    """记录每次收到的 prompt，按序吐预设回复（用完回落默认台词）。"""

    def __init__(self, *replies: Any) -> None:
        self.replies: List[Any] = list(replies)
        self.prompts: List[str] = []

    async def chat(self, prompt: str, strategy_overrides: Any = None) -> str:
        self.prompts.append(prompt)
        if not self.replies:
            return "哈哈 我这边一直挺稳的"
        nxt = self.replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return str(nxt)

    @property
    def calls(self) -> int:
        return len(self.prompts)


class FakePM:
    """只认显式注册的 profile —— 与真 PersonaManager 的 profile store 同语义。"""

    def __init__(self, profiles: Dict[str, Dict[str, Any]] | None = None) -> None:
        self.profiles = dict(profiles or {})

    def get_persona_by_id(self, pid: str):
        return self.profiles.get(str(pid))

    def format_persona_block(self, chat_id: str = "", *, detail: str = "full",
                             account_persona_id: str = "", record_usage: bool = True,
                             **_kw: Any) -> str:
        p = self.profiles.get(str(account_persona_id))
        if not p:
            return ""
        style = (p.get("personality") or {}).get("style", "")
        return f"你是{p['name']}，{p['role']}。说话风格：{style}。"


def _pm() -> FakePM:
    return FakePM({"lin_jiaxin": _PROFILE})


async def _run(gen, messages=None):
    return await gen(SPEAKER, messages if messages is not None else ECP_MESSAGES,
                     DIRECTIVE)


# ── 纯函数：身份锚点 ────────────────────────────────────────────────────────


def test_preamble_carries_persona_and_group_nickname():
    """锚点里必须同时有「怎么说话」和「群里你叫什么」——两者缺一都会露馅。"""
    out = build_persona_preamble("你是林佳欣，做跨境电商的姑娘。", SPEAKER)
    assert "做跨境电商的姑娘" in out
    assert "林佳欣" in out
    # 群昵称优先于人设名（人设名与群昵称不一致时会自报另一个名字）
    assert "群里" in out and "昵称" in out
    # 禁止复述人设本身（prompt 泄漏式暴露）
    assert "不要把它念出来" in out or "复述" in out


def test_preamble_is_empty_without_desc():
    """无人设 → 空锚点，让调用方原样走无人设链路（而不是插一段空壳提示）。"""
    assert build_persona_preamble("", SPEAKER) == ""
    assert build_persona_preamble("   ", SPEAKER) == ""


def test_preamble_falls_back_to_account_id_without_display_name():
    anon = CastMember(slot="skeptic", account_id="acc_z", persona_id="p9")
    assert "acc_z" in build_persona_preamble("你是某某。", anon)


# ── 纯函数：注入 ────────────────────────────────────────────────────────────


def test_inject_puts_persona_first_and_keeps_ecp_order():
    out = inject_persona(ECP_MESSAGES, "【人设】爽快话密")
    assert out[0] == {"role": "system", "content": "【人设】爽快话密"}
    # ECP 的 system（群感知/意图/禁止项）仍紧随其后，顺序一条不乱
    assert [m["content"] for m in out[1:]] == [m["content"] for m in ECP_MESSAGES]


def test_inject_does_not_mutate_the_original_messages():
    """原列表被改会污染 runtime 的事件流投影，且重试时锚点会被叠加两次。"""
    snapshot = [dict(m) for m in ECP_MESSAGES]
    out = inject_persona(ECP_MESSAGES, "【人设】爽快话密")
    assert ECP_MESSAGES == snapshot
    assert len(ECP_MESSAGES) == 3 and len(out) == 4
    # 连元素 dict 都是新的：调用方改返回值不该反噬原始投影
    out[1]["content"] = "改了"
    assert ECP_MESSAGES[0]["content"] == snapshot[0]["content"]


def test_inject_without_preamble_returns_plain_copy():
    out = inject_persona(ECP_MESSAGES, "")
    assert [m["content"] for m in out] == [m["content"] for m in ECP_MESSAGES]
    assert out is not ECP_MESSAGES and out[0] is not ECP_MESSAGES[0]


def test_inject_tolerates_empty_history():
    assert inject_persona([], "【人设】x")[0]["role"] == "system"
    assert inject_persona([], "") == []


# ── 人设解析 ────────────────────────────────────────────────────────────────


def test_resolve_persona_reads_registered_profile():
    persona, desc = resolve_persona_voice(SPEAKER, _pm())
    assert persona.get("name") == "林佳欣"
    assert "做跨境电商的姑娘" in desc


def test_resolve_persona_unknown_id_degrades_instead_of_guessing():
    """id 查不到就诚实地无人设——绝不能静默回落到域默认人设（那会让多号又同味）。"""
    persona, desc = resolve_persona_voice(SPEAKER, FakePM({}))
    assert persona == {} and desc == ""


def test_resolve_persona_survives_a_broken_manager():
    class Boom:
        def get_persona_by_id(self, pid):
            raise RuntimeError("persona store down")

    assert resolve_persona_voice(SPEAKER, Boom()) == ({}, "")


def test_resolve_persona_falls_back_to_fields_when_block_unavailable():
    """manager 没有 format_persona_block（或它抛了）→ 从人设字段手搓描述。"""
    class OnlyLookup:
        def get_persona_by_id(self, pid):
            return _PROFILE

    _persona, desc = resolve_persona_voice(SPEAKER, OnlyLookup())
    assert "林佳欣" in desc and "爽快" in desc


# ── 生成器：人设真的进了 prompt ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persona_desc_reaches_the_model_before_the_ecp_prompt():
    ai = FakeAI("哈哈 我三个号切着用 一点不卡")
    out = await _run(persona_generator(ai, persona_manager=_pm()))
    assert out == "哈哈 我三个号切着用 一点不卡"
    prompt = ai.prompts[0]
    assert "做跨境电商的姑娘" in prompt
    # 身份锚点在最前，ECP 的群感知/意图仍在后面（离生成更近权重更高）
    assert prompt.index("做跨境电商的姑娘") < prompt.index("【你是谁】")
    assert "[群友 陈美玲] 这个会不会封号啊" in prompt   # ECP 投影原样带过去


@pytest.mark.asyncio
async def test_generator_does_not_mutate_caller_messages():
    snapshot = [dict(m) for m in ECP_MESSAGES]
    await _run(persona_generator(FakeAI("好啊"), persona_manager=_pm()))
    assert ECP_MESSAGES == snapshot


@pytest.mark.asyncio
async def test_unknown_persona_still_produces_a_line():
    """人设取不到＝降级为无人设裸生成，绝不阻断（少一层人设 ≫ 不说话）。"""
    ai = FakeAI("那我也试试")
    out = await _run(persona_generator(ai, persona_manager=FakePM({})))
    assert out == "那我也试试"
    assert "【你的人设" not in ai.prompts[0]


# ── 守卫：宁可不说，也不说错 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_self_exposure_triggers_exactly_one_retry():
    ai = FakeAI(_AI_LEAK, "哈哈 我自己也在用 挺省事的")
    out = await _run(persona_generator(ai, persona_manager=_pm()))
    assert out == "哈哈 我自己也在用 挺省事的"
    assert ai.calls == 2
    # 重试 prompt 带修正指令，但不复述违规原文（复述会提高复发率）
    assert "重要修正" in ai.prompts[1]
    assert _AI_LEAK not in ai.prompts[1]


@pytest.mark.asyncio
async def test_two_violations_in_a_row_yield_silence():
    """两次都违规就这一拍不说话——runtime 的连续跳过检测会把它显式暴露出来。"""
    ai = FakeAI(_AI_LEAK, "作为一个人工智能，我没有个人体验。")
    out = await _run(persona_generator(ai, persona_manager=_pm()))
    assert out == ""
    assert ai.calls == 2


@pytest.mark.asyncio
async def test_persona_forbidden_phrase_is_caught_too():
    """客服腔（人设 forbidden_phrases）与 AI 自曝同等对待。"""
    ai = FakeAI(_SERVICE_TONE, "哈哈 都行 你随便问")
    out = await _run(persona_generator(ai, persona_manager=_pm()))
    assert out == "哈哈 都行 你随便问" and ai.calls == 2


@pytest.mark.asyncio
async def test_ai_self_exposure_is_blocked_even_without_a_persona():
    """群里冒一句「作为AI」暴露的是整批号，与这个号有没有绑人设无关。"""
    ai = FakeAI(_AI_LEAK, _AI_LEAK)
    out = await _run(persona_generator(ai, persona_manager=FakePM({})))
    assert out == "" and ai.calls == 2


@pytest.mark.asyncio
async def test_clean_line_never_retries():
    ai = FakeAI("哈哈 稳的")
    assert await _run(persona_generator(ai, persona_manager=_pm())) == "哈哈 稳的"
    assert ai.calls == 1


@pytest.mark.asyncio
async def test_guard_off_returns_raw_output():
    """排练时想看模型原始输出的逃生口——真发链路别关。"""
    ai = FakeAI(_AI_LEAK)
    out = await _run(persona_generator(ai, persona_manager=_pm(), guard=False))
    assert out == _AI_LEAK and ai.calls == 1


def test_check_line_violations_pure_function():
    assert check_line_violations(_AI_LEAK) != []
    assert check_line_violations(_SERVICE_TONE, _PROFILE) != []
    assert check_line_violations("哈哈 我也这么觉得", _PROFILE) == []
    assert check_line_violations("", _PROFILE) == []


# ── 降级：任何异常都只换来沉默，不炸整场 ────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_client_exception_is_swallowed():
    ai = FakeAI(RuntimeError("cloud down"))
    assert await _run(persona_generator(ai, persona_manager=_pm())) == ""


@pytest.mark.asyncio
async def test_empty_model_output_is_not_retried():
    """空输出不是违规，直接让 runtime 跳过这一拍（重试只为救违规）。"""
    ai = FakeAI("   ")
    assert await _run(persona_generator(ai, persona_manager=_pm())) == ""
    assert ai.calls == 1


@pytest.mark.asyncio
async def test_legacy_chat_signature_without_strategy_overrides():
    class OldAI:
        def __init__(self) -> None:
            self.prompts: List[str] = []

        async def chat(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "老签名也能出话"

    ai = OldAI()
    assert await _run(persona_generator(ai, persona_manager=_pm())) == "老签名也能出话"
    assert len(ai.prompts) == 1


# ── 端到端：真的能被 rehearse 驱动 ──────────────────────────────────────────


def _candidates(n: int = 4):
    return [
        {"account_id": f"a{i}", "platform": "telegram",
         "persona_id": "lin_jiaxin", "display_name": f"演员{i}",
         "health": "online", "fingerprint_group": f"fp{i}"}
        for i in range(1, n + 1)
    ]


def _playbook(n_beats: int = 4) -> Playbook:
    roles = (Role("asker"), Role("advocate"), Role("bystander"), Role("skeptic"))
    slots = ("asker", "advocate", "skeptic", "bystander")
    beats = tuple(
        Beat(id=f"b{i}", role=slots[(i - 1) % 4], intent=f"意图{i}")
        for i in range(1, n_beats + 1))
    return Playbook(id="pb", name="pb", system="growth", soft_ad_level=4,
                    roles=roles, beats=beats)


@pytest.mark.asyncio
async def test_rehearse_runs_with_the_persona_generator():
    """异步人设生成器能被排练主循环正常 await，整场戏照演。"""
    ai = FakeAI()   # 无预设 → 每拍都回默认台词
    r = await rehearse(_playbook(4), _candidates(4), seed=3,
                       generate=persona_generator(ai, persona_manager=_pm()))
    assert r.terminate_reason == "completed"
    assert r.line_count == 4 and r.skipped == 0
    assert ai.calls == 4
    # 每一拍都带上了人设锚点（不是只有第一拍接上）
    assert all("做跨境电商的姑娘" in p for p in ai.prompts)


@pytest.mark.asyncio
async def test_rehearse_survives_a_permanently_leaking_model():
    """模型一直自曝身份 → 一条都不发，且 runtime 显式报「生成侧异常」而非假装收场。"""
    ai = FakeAI(*([_AI_LEAK] * 40))
    r = await rehearse(_playbook(8), _candidates(4),
                       generate=persona_generator(ai, persona_manager=_pm()))
    assert r.line_count == 0
    assert r.terminate_reason == "generation_failed"


@pytest.mark.asyncio
async def test_runtime_convenience_entrypoint():
    ai = FakeAI("哈哈 挺好用")
    gen = persona_line_generator(ai, persona_manager=_pm())
    assert await _run(gen) == "哈哈 挺好用"
