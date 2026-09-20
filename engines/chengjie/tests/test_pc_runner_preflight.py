# -*- coding: utf-8 -*-
"""上生产逐级预检纯逻辑门禁（实施91，2026-08-31）。

assess_rollout 是决策核心：五级依赖关系 + 缺项枚举。IO（读配置/探 health）在
tools 里、不测。这里只钉「给定配置+health → 各级就绪/缺什么」的确定性判定。
"""
from tools.pc_runner_preflight import assess_rollout


def _by_key(stages):
    return {s["key"]: s for s in stages}


def test_all_off_only_stage1_needs_everything():
    st = _by_key(assess_rollout({}, {}))
    assert st["inspect"]["met"] is False
    # 缺总闸/机器/在线
    assert any("enabled" in m for m in st["inspect"]["missing"])
    # 下游全部因「先过①」未就绪
    for k in ("ui_actions", "vision", "shell", "trust"):
        assert st[k]["met"] is False


def test_stage1_met_when_enabled_machine_online():
    pc = {"enabled": True, "machines": [{"id": "zhuji"}]}
    healths = {"zhuji": {"ok": True, "version": "x"}}
    st = _by_key(assess_rollout(pc, healths))
    assert st["inspect"]["met"] is True
    # UI 动作仍缺 PC_RUNNER_ACTIONS
    assert st["ui_actions"]["met"] is False
    assert any("PC_RUNNER_ACTIONS" in m for m in st["ui_actions"]["missing"])


def test_stage2_met_unlocks_vision_shell_trust_gating():
    pc = {"enabled": True, "machines": [{"id": "zhuji"}],
          "vision": {"enabled": True}, "trust": {"enabled": True}}
    healths = {"zhuji": {"ok": True, "actions_enabled": True,
                         "shell_enabled": False}}
    st = _by_key(assess_rollout(pc, healths))
    assert st["ui_actions"]["met"] is True
    # ③视觉：②过 + vision.enabled → 就绪
    assert st["vision"]["met"] is True
    # ⑤信任：②过 + trust.enabled → 就绪
    assert st["trust"]["met"] is True
    # ④命令：②过但 shell 未开 → 未就绪，缺 PC_RUNNER_SHELL
    assert st["shell"]["met"] is False
    assert any("PC_RUNNER_SHELL" in m for m in st["shell"]["missing"])


def test_vision_gated_even_if_actions_on():
    # actions 开、vision.enabled 关 → ③视觉未就绪（缺 vision 闸）
    pc = {"enabled": True, "machines": [{"id": "z"}], "vision": {"enabled": False}}
    healths = {"z": {"ok": True, "actions_enabled": True}}
    st = _by_key(assess_rollout(pc, healths))
    assert st["vision"]["met"] is False
    assert any("vision.enabled" in m for m in st["vision"]["missing"])


def test_offline_machine_blocks_stage1():
    pc = {"enabled": True, "machines": [{"id": "z"}]}
    healths = {"z": {"ok": False, "error": "offline"}}
    st = _by_key(assess_rollout(pc, healths))
    assert st["inspect"]["met"] is False
    assert any("在线" in m for m in st["inspect"]["missing"])


def test_stage_shape_five_ordered():
    stages = assess_rollout({}, {})
    assert [s["n"] for s in stages] == [1, 2, 3, 4, 5]
    assert [s["key"] for s in stages] == [
        "inspect", "ui_actions", "vision", "shell", "trust"]
