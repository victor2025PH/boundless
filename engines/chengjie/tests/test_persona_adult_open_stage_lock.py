# -*- coding: utf-8 -*-
"""成人不设限 × 尺度锁 × 漏斗语气指令 三个开关的 prompt 门禁（2026-09-19，173 陪伴机）。

覆盖不变量：
  · A3 ``adult_policy: open``（人设显式或机器级 ``adult_grader.default_policy``）→ 人设
    ``boundaries.topics_to_avoid`` 里的成人类条目不进 prompt（compact + full 同源），其它回避条目
    （政治 / 诈骗赌博）逐字保留；非 open 时逐字旧行为。
  · A4 ``companion.stage_lock: false`` → 脾气 / banter_profanity 不看漏斗阶段，initial 也放开；
    默认（未配置 / true）仍只在 intimate / steady 放开——旧门禁 test_persona_humanization 不变。
  · A4' open 时【尺度·亲密闲聊】红线去掉「性内容」，其余红线（人身攻击 / 歧视仇恨 / 痛处）保留；
    非 open 逐字旧文案。
  · prompt 里同时出现【成人话题·不设限】段（adult_grader.prompt_block 机器级 open 也注入）。
  · A5 ``companion.funnel_directive: false`` → skill_manager 不再往 user_context 写 ``_funnel_directive``
    （用 relationship_stager.stage_directive 纯函数 + 开关判定复现同一逻辑）。
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from src.utils.persona_manager import PersonaManager, _filter_adult_topics


class _Cfg:
    def __init__(self, cfg: Dict[str, Any]):
        self.config = cfg


def _pm(cfg: Dict[str, Any] | None = None) -> PersonaManager:
    pm = PersonaManager()
    if cfg is not None:
        pm.attach_config_manager(_Cfg(cfg))
    return pm


_PERSONA: Dict[str, Any] = {
    "id": "p_claire", "name": "Claire", "role": "调酒师",
    "personality": {"traits": ["直接"], "style": "慵懒", "temperament": "被冷落会吃醋"},
    "speaking": {"language_follow": True, "banter_profanity": True},
    "boundaries": {"topics_to_avoid": ["政治极端", "成人色情", "诈骗赌博"]},
}


def _open_persona() -> Dict[str, Any]:
    p = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _PERSONA.items()}
    p["boundaries"] = {**_PERSONA["boundaries"], "adult_policy": "open"}
    return p


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    # 进程级运行时配置别串台：机器级 open 只走 attach_config_manager 注入的 cfg
    from src.compliance import runtime as _rt
    monkeypatch.setattr(_rt, "_PROVIDER", None, raising=False)
    yield


# ── _filter_adult_topics 纯函数 ─────────────────────────────────────────────────

def test_filter_adult_topics_only_strips_adult_entries():
    src = ["政治极端", "成人色情", "诈骗赌博", "性话题", "Adult content", "NSFW stuff", "宗教", "  "]
    assert _filter_adult_topics(src) == ["政治极端", "诈骗赌博", "宗教"]
    assert _filter_adult_topics([]) == [] and _filter_adult_topics(None) == []


# ── A3：topics_to_avoid 成人条目 ───────────────────────────────────────────────

def test_full_prompt_drops_adult_topic_when_persona_open():
    out = _pm({})._format_persona_instructions(_open_persona(), funnel_stage="initial")
    assert "成人色情" not in out
    assert "政治极端" in out and "诈骗赌博" in out
    assert "成人话题·不设限" in out


def test_compact_prompt_drops_adult_topic_when_persona_open():
    out = _pm({})._format_persona_compact(_open_persona())
    assert "成人色情" not in out
    assert "政治极端" in out and "诈骗赌博" in out
    assert "成人话题·不设限" in out


def test_machine_default_open_drops_adult_topic_for_plain_persona():
    cfg = {"adult_grader": {"default_policy": "open"}}
    full = _pm(cfg)._format_persona_instructions(_PERSONA, funnel_stage="initial")
    comp = _pm(cfg)._format_persona_compact(_PERSONA)
    for out in (full, comp):
        assert "成人色情" not in out and "政治极端" in out
        assert "成人话题·不设限" in out


def test_non_open_keeps_adult_topic_verbatim():
    for cfg in ({}, {"adult_grader": {"default_policy": "soft_reply"}}):
        full = _pm(cfg)._format_persona_instructions(_PERSONA, funnel_stage="initial")
        comp = _pm(cfg)._format_persona_compact(_PERSONA)
        for out in (full, comp):
            assert "成人色情" in out and "政治极端" in out and "诈骗赌博" in out
            assert "成人话题·不设限" not in out


# ── A4：stage_lock ─────────────────────────────────────────────────────────────

def test_stage_lock_default_keeps_old_gate():
    pm = _pm({})
    out = pm._format_persona_instructions(_PERSONA, funnel_stage="initial")
    assert "先收着点脾气" in out and "尺度·亲密闲聊" not in out
    out2 = pm._format_persona_instructions(_PERSONA, funnel_stage="intimate")
    assert "可以像真人一样有小情绪" in out2 and "尺度·亲密闲聊" in out2
    # 显式 true 等价默认
    out3 = _pm({"companion": {"stage_lock": True}})._format_persona_instructions(_PERSONA, funnel_stage="initial")
    assert "先收着点脾气" in out3 and "尺度·亲密闲聊" not in out3


@pytest.mark.parametrize("stage", ["", "initial", "warming", "intimate", "steady"])
def test_stage_lock_off_opens_temperament_and_banter_at_any_stage(stage):
    pm = _pm({"companion": {"stage_lock": False}})
    out = pm._format_persona_instructions(_PERSONA, funnel_stage=stage)
    assert "可以像真人一样有小情绪" in out and "先收着点脾气" not in out
    assert "尺度·亲密闲聊" in out and "不人身攻击" in out


def test_stage_lock_off_string_false_also_works():
    pm = _pm({"companion": {"stage_lock": "false"}})
    out = pm._format_persona_instructions(_PERSONA, funnel_stage="initial")
    assert "尺度·亲密闲聊" in out


def test_stage_lock_off_still_requires_banter_optin():
    p = {**_PERSONA, "speaking": {"language_follow": True}}
    out = _pm({"companion": {"stage_lock": False}})._format_persona_instructions(p, funnel_stage="initial")
    assert "尺度·亲密闲聊" not in out


# ── A4'：红线文案按成人政策 ───────────────────────────────────────────────────

def test_banter_redline_drops_sex_clause_when_open():
    cfg = {"companion": {"stage_lock": False}, "adult_grader": {"default_policy": "open"}}
    out = _pm(cfg)._format_persona_instructions(_PERSONA, funnel_stage="initial")
    assert "尺度·亲密闲聊" in out
    seg = out[out.index("尺度·亲密闲聊"):]
    seg = seg[:seg.index("绝不是攻击")]
    assert "性内容" not in seg
    assert "不人身攻击" in seg and "歧视/仇恨" in seg and "不针对对方痛处" in seg


def test_banter_redline_keeps_sex_clause_when_not_open():
    out = _pm({"companion": {"stage_lock": False}})._format_persona_instructions(_PERSONA, funnel_stage="initial")
    seg = out[out.index("尺度·亲密闲聊"):]
    assert "歧视/仇恨/性内容" in seg


# ── A5：funnel_directive 开关（与 skill_manager 判定同式） ─────────────────────

def _fd_enabled(cfg: Dict[str, Any]) -> bool:
    comp = (cfg.get("companion") or {}) if isinstance(cfg, dict) else {}
    if isinstance(comp, dict) and "funnel_directive" in comp:
        return comp.get("funnel_directive") not in (False, 0, "0", "false", "off", "no")
    return True


def test_funnel_directive_switch_semantics():
    assert _fd_enabled({}) is True
    assert _fd_enabled({"companion": {}}) is True
    assert _fd_enabled({"companion": {"funnel_directive": True}}) is True
    for v in (False, 0, "0", "false", "off", "no"):
        assert _fd_enabled({"companion": {"funnel_directive": v}}) is False, v


def test_skill_manager_source_has_funnel_directive_gate():
    """源码级钉子：skill_manager 注入 _funnel_directive 前必须查 companion.funnel_directive。"""
    import inspect
    from src.skills import skill_manager as sm
    src = inspect.getsource(sm)
    i = src.index('user_context["_funnel_directive"] = _directive')
    window = src[max(0, i - 2500):i]
    assert '"funnel_directive" in _comp_fd' in window
    assert 'user_context.pop("_funnel_directive", None)' in window


def test_stage_directive_initial_is_the_brake_we_switch_off():
    from src.contacts.relationship_stager import stage_directive
    d = stage_directive("INITIAL", 10)
    assert "不主动宣示亲密关系" in d


# ── A4''：companion_relationship 自然化附加块「关系仍偏新·克制撒娇」也看 stage_lock ─────

def test_natural_dialogue_new_relation_clause_follows_stage_lock():
    from src.utils.companion_relationship import build_natural_dialogue_prompt_addon
    st = {"stage": "initial", "exchange_count": 0}
    default = build_natural_dialogue_prompt_addon(st, {"enabled": True}, user_message="hi there how are you doing tonight")
    assert "关系仍偏新" in default and "克制撒娇" in default
    assert "禁止**" in default or "禁止" in default          # 其余自然化条目照旧
    off = build_natural_dialogue_prompt_addon(st, {"enabled": True, "stage_lock": False},
                                              user_message="hi there how are you doing tonight")
    assert "关系仍偏新" not in off and "克制撒娇" not in off
    assert "对话自然化" in off and "禁止" in off
    # 显式 true / 字符串 false
    on = build_natural_dialogue_prompt_addon(st, {"enabled": True, "stage_lock": True}, user_message="x" * 30)
    assert "关系仍偏新" in on
    off2 = build_natural_dialogue_prompt_addon(st, {"enabled": True, "stage_lock": "false"}, user_message="x" * 30)
    assert "关系仍偏新" not in off2


# ── P2（第二轮）：compact 是生产主用格式——open 的升温口吻 / 未成年硬停、尺度锁关时的性情与脏话
#    一行版都要在 compact 里，否则长会话被裁剪后热度与棱角全掉线 ────────────────────────

def test_open_block_carries_heat_voice_and_minor_stop_in_both_modes():
    from src.inbox.adult_grader import OPEN_HEAT_VOICE, OPEN_MINOR_STOP, prompt_block
    for compact in (True, False):
        blk = prompt_block(_open_persona(), compact=compact)
        assert "成人话题·不设限" in blk and "升温口吻" in blk and "唯一硬停" in blk
        assert OPEN_HEAT_VOICE in blk and OPEN_MINOR_STOP in blk
    # 非 open 政策不带升温口吻
    assert "升温口吻" not in prompt_block({"boundaries": {"adult_policy": "mark_only"}})
    assert prompt_block(_PERSONA, cfg={}) == ""


def test_compact_prompt_has_heat_voice_when_open():
    comp = _pm({"adult_grader": {"default_policy": "open"}})._format_persona_compact(_PERSONA)
    assert "升温口吻" in comp and "第一人称" in comp and "唯一硬停" in comp
    assert "升温口吻" not in _pm({})._format_persona_compact(_PERSONA)


def test_compact_prompt_carries_temperament_and_banter_when_stage_lock_off():
    comp = _pm({"companion": {"stage_lock": False}})._format_persona_compact(_PERSONA)
    assert "【真实性情】被冷落会吃醋" in comp and "客服式安抚" in comp
    assert "尺度·亲密闲聊" in comp and "性内容" in comp          # 非 open：红线仍含性内容
    # 默认（尺度锁开）compact 维持旧行为：一行都不带
    old = _pm({})._format_persona_compact(_PERSONA)
    assert "真实性情" not in old and "尺度·亲密闲聊" not in old
    # banter 仍需人设 opt-in
    p = {**_PERSONA, "speaking": {"language_follow": True}}
    no_banter = _pm({"companion": {"stage_lock": False}})._format_persona_compact(p)
    assert "尺度·亲密闲聊" not in no_banter and "真实性情" in no_banter


def test_compact_banter_redline_drops_sex_clause_when_open_and_unlocked():
    cfg = {"companion": {"stage_lock": False}, "adult_grader": {"default_policy": "open"}}
    comp = _pm(cfg)._format_persona_compact(_PERSONA)
    assert "尺度·亲密闲聊" in comp
    assert "性内容" not in comp
    assert "成人话题·不设限" in comp
