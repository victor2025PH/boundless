# -*- coding: utf-8 -*-
"""小智 pc_inspect 动作 + 规划器集成门禁（实施91 P1-1，2026-08-30）。

守：pc_inspect 只读工具集与 runner 侧 READONLY_TOOLS 同步、机器/工具白名单
结构性校验、规划器仅在有受控机时才列 pc_inspect、runner 步载荷透传、
home-nav 不误插 runner 步、前端 execStep 有 runner 分支。
"""
from __future__ import annotations

import pytest

from src.assistant import actions as act
from src.assistant import agent_planner as planner
from src.assistant import runner_pairing as rp
from runner import pc_runner


@pytest.fixture(autouse=True)
def _clean():
    rp._reset_for_test()
    yield
    rp._reset_for_test()


_MACHINES = [
    {"id": "zhuji", "base_url": "http://127.0.0.1:18760", "label": "主机"},
    {"id": "kouxing", "base_url": "http://192.168.0.198:18760"},
]


# ── 动作结构 + 只读工具集与 runner 同步 ────────────────────────────────────
def test_pc_inspect_registered_readonly():
    spec = act.ACTIONS.get("pc_inspect")
    assert spec and spec["level"] == "L0" and spec["kind"] == "runner"
    # pc_inspect 允许的工具必须 ⊆ runner 侧只读白名单（漂移=可能放进动作工具）
    assert set(spec["tools"]) <= set(pc_runner.READONLY_TOOLS)
    # 且绝不含任何动作工具
    assert not (set(spec["tools"]) & set(pc_runner.ACTION_TOOLS))


def test_pc_inspect_in_catalog_all_roles():
    for role in ("agent", "master"):
        ids = {a["id"] for a in act.catalog(role, "zh")}
        assert "pc_inspect" in ids  # L0 所有角色可见


# ── plan_action 校验 ───────────────────────────────────────────────────────
def test_plan_rejects_machine_not_whitelisted():
    rp.load_machines(_MACHINES)
    bad = act.plan_action("pc_inspect",
                          {"machine": "ghost", "tool": "list_windows"}, {})
    assert not bad["ok"] and bad["error"] == "machine_not_available"


def test_plan_rejects_revoked_machine():
    rp.load_machines(_MACHINES)
    rp.revoke("zhuji")
    bad = act.plan_action("pc_inspect",
                          {"machine": "zhuji", "tool": "list_windows"}, {})
    assert not bad["ok"] and bad["error"] == "machine_not_available"


def test_plan_rejects_non_readonly_tool():
    rp.load_machines(_MACHINES)
    # 动作工具名混进 pc_inspect 必拒（pc_inspect 只认只读三工具）
    for tool in ("launch_app", "click", "run_command", "rm"):
        bad = act.plan_action("pc_inspect",
                              {"machine": "zhuji", "tool": tool}, {})
        assert not bad["ok"] and bad["error"] == "bad_params", tool


def test_plan_read_tree_requires_window():
    rp.load_machines(_MACHINES)
    miss = act.plan_action("pc_inspect",
                           {"machine": "zhuji", "tool": "read_tree"}, {})
    assert not miss["ok"] and "window" in str(miss["detail"])
    ok = act.plan_action("pc_inspect",
                         {"machine": "zhuji", "tool": "read_tree",
                          "window": "记事本"}, {})
    assert ok["ok"] and ok["runner"]["args"]["window"] == "记事本"


def test_plan_list_windows_needs_no_window():
    rp.load_machines(_MACHINES)
    ok = act.plan_action("pc_inspect",
                         {"machine": "kouxing", "tool": "list_windows"}, {})
    assert ok["ok"] and ok["kind"] == "runner" and ok["level"] == "L0"
    assert ok["runner"] == {"machine": "kouxing", "tool": "list_windows",
                            "args": {}}
    assert ok["clean_params"] == {"machine": "kouxing",
                                  "tool": "list_windows"}


# ── pc_launch / pc_focus L2 写动作（实施91 P1-2）──────────────────────────
def test_pc_actions_are_l2_runner_action_tools():
    for aid, tool in (("pc_launch", "launch_app"), ("pc_focus", "focus_window")):
        spec = act.ACTIONS.get(aid)
        assert spec and spec["level"] == "L2" and spec["kind"] == "runner", aid
        assert spec["tools"] == (tool,)
        assert tool in pc_runner.ACTION_TOOLS          # 是动作工具
        assert tool not in pc_runner.READONLY_TOOLS    # 绝不在只读集


def test_plan_pc_launch_whitelist_and_confirm():
    rp.load_machines(_MACHINES)
    ok = act.plan_action("pc_launch",
                         {"machine": "zhuji", "app_id": "notepad"}, {})
    assert ok["ok"] and ok["level"] == "L2" and ok["kind"] == "runner"
    assert ok["runner"] == {"machine": "zhuji", "tool": "launch_app",
                            "args": {"app_id": "notepad"}}
    assert ok["diff"] and ok["diff"][0]["new_h"].startswith("记事本")  # 确认卡素材
    # 非白名单 app / 任意路径 / 空 —— 结构性白名单一律拒
    for bad in ("cmd", r"C:\Windows\System32\cmd.exe", ""):
        r = act.plan_action("pc_launch", {"machine": "zhuji", "app_id": bad}, {})
        assert not r["ok"] and r["error"] == "bad_params", bad
    # 机器不在白名单必拒
    r = act.plan_action("pc_launch", {"machine": "ghost", "app_id": "notepad"}, {})
    assert not r["ok"] and r["error"] == "machine_not_available"


def test_plan_pc_launch_ignores_params_tool():
    # 动作面 tool 由 spec 钉死，绝不吃 params.tool（防借 pc_launch 混入别的工具）
    rp.load_machines(_MACHINES)
    ok = act.plan_action("pc_launch",
                         {"machine": "zhuji", "app_id": "notepad",
                          "tool": "click"}, {})
    assert ok["ok"] and ok["runner"]["tool"] == "launch_app"


def test_plan_pc_focus_requires_window():
    rp.load_machines(_MACHINES)
    miss = act.plan_action("pc_focus", {"machine": "zhuji"}, {})
    assert not miss["ok"] and "window" in str(miss["detail"])
    ok = act.plan_action("pc_focus",
                         {"machine": "zhuji", "window": "记事本"}, {})
    assert ok["ok"] and ok["level"] == "L2"
    assert ok["runner"] == {"machine": "zhuji", "tool": "focus_window",
                            "args": {"window": "记事本"}}
    assert ok["diff"][0]["new_h"].startswith("记事本")


def test_planner_lists_pc_actions_with_machines():
    rp.load_machines(_MACHINES)
    p = planner.build_planner_prompt(
        "打开记事本", "master", list(act.CORE_NAV_PATHS),
        lang="zh", pc_machines=["zhuji"])
    assert "pc_launch" in p and "pc_focus" in p and "notepad" in p
    # 无受控机 → 动作面同 pc_inspect 一样不列（免规划必被拒的步）
    p2 = planner.build_planner_prompt(
        "打开记事本", "master", list(act.CORE_NAV_PATHS),
        lang="zh", pc_machines=[])
    assert "pc_launch" not in p2 and "pc_focus" not in p2


def test_validate_plan_carries_pc_launch_l2():
    rp.load_machines(_MACHINES)
    v = planner.validate_plan(
        {"say": "", "steps": [{"action": "pc_launch",
                               "params": {"machine": "zhuji",
                                          "app_id": "notepad"}}]},
        {}, "master", nav_paths=set(act.CORE_NAV_PATHS), page="/workspace")
    assert v["ok"]
    step = v["steps"][0]
    assert step["kind"] == "runner" and step["level"] == "L2"
    assert step["runner"]["tool"] == "launch_app"


# ── P2-A 全能力档 L2 动作（实施91，2026-08-31）──────────────────────────────
def test_plan_pc_run_app_arbitrary_path():
    rp.load_machines(_MACHINES)
    ok = act.plan_action("pc_run_app",
                         {"machine": "zhuji", "path": r"C:\tool.exe",
                          "args": ["--x"]}, {})
    assert ok["ok"] and ok["level"] == "L2"
    assert ok["runner"]["tool"] == "launch_path"
    assert ok["runner"]["args"]["path"] == r"C:\tool.exe"
    assert ok["runner"]["args"]["args"] == ["--x"]
    assert ok["diff"][0]["new_h"].startswith(r"C:\tool.exe")
    r = act.plan_action("pc_run_app", {"machine": "zhuji"}, {})
    assert not r["ok"] and r["error"] == "bad_params"


def test_plan_pc_click_needs_target_and_window():
    rp.load_machines(_MACHINES)
    ok = act.plan_action("pc_click",
                         {"machine": "zhuji", "window": "记事本",
                          "target": "确定"}, {})
    assert ok["ok"] and ok["runner"]["tool"] == "click_control"
    assert ok["runner"]["args"] == {"window": "记事本", "target": "确定"}
    for miss in ({"machine": "zhuji", "window": "记事本"},
                 {"machine": "zhuji", "target": "确定"}):
        r = act.plan_action("pc_click", miss, {})
        assert not r["ok"] and r["error"] == "bad_params"


def test_plan_pc_click_target_needs_target_and_window():
    # P3-B 视觉定位点击：pseudo-tool click_target（路由编排截图→VLM→click_point）
    rp.load_machines(_MACHINES)
    ok = act.plan_action("pc_click_target",
                         {"machine": "zhuji", "window": "画图",
                          "target": "蓝色的提交按钮"}, {})
    assert ok["ok"] and ok["level"] == "L2"
    assert ok["runner"]["tool"] == "click_target"
    assert ok["runner"]["args"] == {"window": "画图", "target": "蓝色的提交按钮"}
    # 确认卡 diff 标「视觉定位」
    assert ok["diff"] and "视觉定位" in ok["diff"][0]["new_h"]
    for miss in ({"machine": "zhuji", "window": "画图"},
                 {"machine": "zhuji", "target": "按钮"}):
        r = act.plan_action("pc_click_target", miss, {})
        assert not r["ok"] and r["error"] == "bad_params"


def test_pc_click_target_never_auto_executes_even_when_trusted():
    # 视觉点击绕过控件名禁区闸 + 坐标是模型猜的 → always_confirm，信任档也不免确认
    assert act.ACTIONS["pc_click_target"].get("always_confirm") is True
    # 即便 trust_enabled + is_trusted，always_confirm 动作仍不自动执行
    assert act.runner_auto_execute(
        kind="runner", level="L2", always_confirm=True,
        trust_enabled=True, is_trusted=True) is False
    # 无信任档当然也不自动执行
    assert act.runner_auto_execute(
        kind="runner", level="L2", always_confirm=True,
        trust_enabled=False, is_trusted=False) is False


def test_plan_pc_type_and_keys():
    rp.load_machines(_MACHINES)
    ok = act.plan_action("pc_type",
                         {"machine": "zhuji", "window": "记事本",
                          "text": "你好"}, {})
    assert ok["ok"] and ok["runner"]["args"]["text"] == "你好"
    ok = act.plan_action("pc_keys",
                         {"machine": "zhuji", "window": "记事本",
                          "keys": "{Ctrl}s"}, {})
    assert ok["ok"] and ok["runner"]["args"]["keys"] == "{Ctrl}s"


def test_planner_lists_p2a_actions():
    rp.load_machines(_MACHINES)
    p = planner.build_planner_prompt(
        "在电脑上运行程序", "master", list(act.CORE_NAV_PATHS),
        lang="zh", pc_machines=["zhuji"])
    for aid in ("pc_run_app", "pc_click", "pc_type", "pc_keys"):
        assert aid in p, aid


# ── P2-B run_command（PowerShell；L2 always_confirm）─────────────────────────
def test_pc_run_command_is_l2_always_confirm():
    spec = act.ACTIONS.get("pc_run_command")
    assert spec and spec["level"] == "L2" and spec["kind"] == "runner"
    assert spec.get("always_confirm") is True
    assert spec["tools"] == ("run_command",)
    assert "run_command" in pc_runner.ACTION_TOOLS


def test_plan_pc_run_command():
    rp.load_machines(_MACHINES)
    ok = act.plan_action("pc_run_command",
                         {"machine": "zhuji", "command": "Get-Date"}, {})
    assert ok["ok"] and ok["level"] == "L2"
    assert ok["runner"]["tool"] == "run_command"
    assert ok["runner"]["args"]["command"] == "Get-Date"
    assert "⚠" in ok["diff"][0]["new_h"]                 # 危险标记
    r = act.plan_action("pc_run_command", {"machine": "zhuji"}, {})
    assert not r["ok"] and r["error"] == "bad_params"


# ── P2-C 信任档免确认判定（纯函数矩阵）──────────────────────────────────────
def test_runner_auto_execute_matrix():
    def ae(**kw):
        base = dict(kind="runner", level="L2", always_confirm=False,
                    trust_enabled=False, is_trusted=False)
        base.update(kw)
        return act.runner_auto_execute(**base)
    # L0 只读：永远直执行（不看信任档）
    assert ae(level="L0") is True
    assert ae(level="L0", trust_enabled=True, is_trusted=True) is True
    # L2 默认（信任档关）：走确认卡
    assert ae(is_trusted=True) is False
    # L2 信任档开 + 受信 + 非 always_confirm：免确认
    assert ae(trust_enabled=True, is_trusted=True) is True
    # L2 信任档开 + 受信 + always_confirm（run_command）：仍确认（永不免）
    assert ae(trust_enabled=True, is_trusted=True, always_confirm=True) is False
    # L2 信任档开但未受信：确认
    assert ae(trust_enabled=True, is_trusted=False) is False
    # L2 受信但信任档全局关：确认
    assert ae(trust_enabled=False, is_trusted=True) is False
    # 非 runner：一律不归本判定
    assert act.runner_auto_execute(kind="settings", level="L2",
                                   always_confirm=False, trust_enabled=True,
                                   is_trusted=True) is False


# ── 规划器集成 ─────────────────────────────────────────────────────────────
def test_prompt_lists_pc_inspect_only_with_machines():
    rp.load_machines(_MACHINES)
    # 有受控机 → pc_inspect + 受控机块进 prompt
    p = planner.build_planner_prompt(
        "看看 198 在干嘛", "master", list(act.CORE_NAV_PATHS),
        lang="zh", pc_machines=["zhuji", "kouxing"])
    assert "pc_inspect" in p and "可操作的电脑" in p and "kouxing" in p
    # 无受控机（enabled 关/未配对）→ pc_inspect 不进 prompt
    p2 = planner.build_planner_prompt(
        "随便", "master", list(act.CORE_NAV_PATHS), lang="zh",
        pc_machines=[])
    assert "pc_inspect" not in p2 and "可操作的电脑" not in p2


def test_prompt_pc_block_english():
    p = planner.build_planner_prompt(
        "inspect", "master", list(act.CORE_NAV_PATHS), lang="en",
        pc_machines=["zhuji"])
    assert "pc_inspect" in p and "Controllable PCs" in p


def test_validate_plan_carries_runner_payload():
    rp.load_machines(_MACHINES)
    parsed = {"say": "看一下", "steps": [
        {"action": "pc_inspect",
         "params": {"machine": "zhuji", "tool": "list_windows"}}]}
    v = planner.validate_plan(parsed, {}, "master",
                              nav_paths=set(act.CORE_NAV_PATHS),
                              page="/workspace")
    assert v["ok"], v
    step = v["steps"][0]
    assert step["kind"] == "runner"
    assert step["runner"]["machine"] == "zhuji"
    assert step["params"] == {"machine": "zhuji", "tool": "list_windows"}


def test_home_nav_not_inserted_for_runner_step():
    """runner 步在受控机上，不该触发小智端 goto_page 插入。"""
    rp.load_machines(_MACHINES)
    parsed = {"say": "", "steps": [
        {"action": "pc_inspect",
         "params": {"machine": "zhuji", "tool": "list_windows"}}]}
    v = planner.validate_plan(parsed, {}, "master",
                              nav_paths=set(act.CORE_NAV_PATHS),
                              page="/dashboard")
    assert v["ok"]
    assert [s["kind"] for s in v["steps"]] == ["runner"]  # 无 nav 插入


def test_agent_role_can_plan_pc_inspect():
    rp.load_machines(_MACHINES)
    v = planner.validate_plan(
        {"say": "", "steps": [{"action": "pc_inspect",
                               "params": {"machine": "zhuji",
                                          "tool": "list_windows"}}]},
        {}, "agent", nav_paths=set(act.CORE_NAV_PATHS), page="/workspace")
    assert v["ok"]


# ── 前端接线（静态证据）───────────────────────────────────────────────────
def test_agent_js_wires_runner_kind():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "shared" / "assistant" /
          "assistant-agent.js").read_text(encoding="utf-8")
    assert "step.kind === 'runner'" in js, "execStep 缺 runner 分支"
    for key in ("pc_saw_win", "pc_saw_tree", "pc_saw_shot", "pc_saw_fail"):
        assert js.count(key) >= 2, f"i18n 键 {key} 缺 zh/en 之一"
