# -*- coding: utf-8 -*-
"""「替我做」规划器门禁（实施58 P2，2026-08-23）。

核心不变量：LLM 输出**永远不被直接执行**——parse 后逐步过
actions.plan_action 结构性复验，未知动作/坏参数/越权级别/超步数全部丢弃
且留痕（dropped 带 reason）。解析器要吃得下真实 LLM 的脏输出
（markdown 围栏/前后缀废话/字符串内花括号）。
"""
from __future__ import annotations

from src.assistant import agent_planner as ap
from src.assistant import actions as act


# ── prompt ───────────────────────────────────────────────────────────────
def test_prompt_contains_catalog_and_whitelist():
    # 实施91 起 runner 动作（pc_*）只在**有可用受控机**时才进目录——总闸关/
    # 无配对机器时列了也必被拒，纯噪音（agent_planner 内有注释钉此语义）。
    # 本门禁随契约分两档：无受控机=runner 动作必须不出现、其余全出现；
    # 有受控机=全量出现（「目录↔prompt 不漂移」的原始约束在此档全量成立）。
    p = ap.build_planner_prompt("把回复调快点", "master",
                                ["/workspace", "/reply-settings"], lang="zh")
    for aid, spec in act.ACTIONS.items():
        if spec["kind"] == "runner":
            assert aid not in p, f"无受控机时 runner 动作 {aid} 不该进目录"
        else:
            assert aid in p
    assert "/reply-settings" in p
    assert "JSON" in p
    p2 = ap.build_planner_prompt("把回复调快点", "master",
                                 ["/workspace", "/reply-settings"], lang="zh",
                                 pc_machines=["seat-01"])
    for aid in act.ACTIONS:
        assert aid in p2, f"有受控机时动作 {aid} 必须进目录（目录↔prompt 漂移）"


def test_prompt_agent_role_excludes_l2():
    p = ap.build_planner_prompt("goal", "agent", ["/workspace"], lang="en")
    assert "set_reply_delay" not in p
    assert "goto_page" in p


# ── parse ────────────────────────────────────────────────────────────────
def test_parse_plain_and_fenced_and_dirty():
    obj = {"say": "ok", "steps": [{"action": "goto_page",
                                   "params": {"path": "/workspace"}}]}
    import json

    raw = json.dumps(obj, ensure_ascii=False)
    assert ap.parse_plan_json(raw) == obj
    assert ap.parse_plan_json("```json\n" + raw + "\n```") == obj
    assert ap.parse_plan_json("好的，计划如下：\n" + raw + "\n以上。") == obj
    # 字符串里的花括号不干扰配平
    tricky = '{"say": "含 {花括号} 与 \\"引号\\"", "steps": []}'
    got = ap.parse_plan_json("前缀 " + tricky + " 后缀")
    assert got and "花括号" in got["say"]


def test_parse_garbage_returns_none():
    assert ap.parse_plan_json("") is None
    assert ap.parse_plan_json("我做不到") is None
    assert ap.parse_plan_json("{broken json") is None
    assert ap.parse_plan_json("[1,2,3]") is None


# ── validate ─────────────────────────────────────────────────────────────
def _steps(*items):
    return {"say": "s", "steps": list(items)}


def test_validate_drops_unknown_and_bad_params():
    v = ap.validate_plan(_steps(
        {"action": "rm_rf", "params": {}},
        {"action": "set_reply_delay", "params": {"min_sec": 9, "max_sec": 2}},
        {"action": "goto_page", "params": {"path": "/workspace"}},
    ), {}, "master", nav_paths={"/workspace"})
    assert v["ok"] is True
    assert [s["action"] for s in v["steps"]] == ["goto_page"]
    reasons = {d["action"]: d["reason"] for d in v["dropped"]}
    assert reasons["rm_rf"] == "unknown_action"
    assert reasons["set_reply_delay"] == "bad_params"


def test_validate_role_forbidden_l2():
    v = ap.validate_plan(_steps(
        {"action": "toggle_voice_reply", "params": {"enabled": True}},
    ), {}, "agent", nav_paths={"/workspace"})
    assert v["ok"] is False and v["reason"] == "no_valid_steps"
    assert v["dropped"][0]["reason"] == "forbidden"


def test_validate_clamps_max_steps():
    items = [{"action": "goto_page", "params": {"path": "/workspace"}}
             for _ in range(8)]
    v = ap.validate_plan(_steps(*items), {}, "master",
                         nav_paths={"/workspace"}, max_steps=3)
    assert len(v["steps"]) == 3
    assert sum(1 for d in v["dropped"] if d["reason"] == "max_steps") == 5


def test_validate_l2_carries_diff_and_clean_params():
    cfg = {"inbox": {"l2_autosend": {"deliver_delay": {"min_sec": 1,
                                                       "max_sec": 4}}}}
    v = ap.validate_plan(_steps(
        {"action": "set_reply_delay",
         "params": {"min_sec": 3, "max_sec": 8, "evil_extra": "x"}},
    ), cfg, "master")
    # page 未知 → 确定性导航会在设置步前插 goto（详见 _maybe_insert_home_nav），
    # 本用例只关心 L2 步本身的 diff/clean_params 契约
    s = v["steps"][-1]
    assert s["action"] == "set_reply_delay"
    assert s["level"] == "L2" and s["diff"]
    assert s["params"] == {"min_sec": 3, "max_sec": 8}  # 越界键被剥掉
    olds = {d["path"]: d["old"] for d in s["diff"]}
    assert olds["inbox.l2_autosend.deliver_delay.min_sec"] == 1


def test_validate_nav_step_params_roundtrip():
    v = ap.validate_plan(_steps(
        {"action": "goto_page", "params": {"path": "/reply-settings"}},
    ), {}, "master", nav_paths={"/reply-settings"})
    s = v["steps"][0]
    assert s["kind"] == "nav" and s["goto"] == "/reply-settings"
    assert s["params"] == {"path": "/reply-settings"}


def test_validate_none_input():
    v = ap.validate_plan(None, {}, "master")
    assert v["ok"] is False and v["reason"] == "parse_failed"


def test_say_clamped():
    v = ap.validate_plan({"say": "x" * 900, "steps": []}, {}, "master")
    assert len(v["say"]) <= ap.MAX_SAY_CHARS


# ── P1：设置现状快照 + clarify 追问 ──────────────────────────────────────
def test_prompt_carries_settings_snapshot():
    cfg = {"inbox": {"auto_draft": {"automation_mode": "review",
                                    "platform_modes": {"messenger": "review"}},
                     "l2_autosend": {"voice": {"enabled": True}}}}
    p = ap.build_planner_prompt("g", "master", ["/workspace"], lang="zh",
                                config=cfg)
    assert "当前设置现状" in p
    assert "回复档位: AI拟稿·人审后发" in p
    assert "自动语音回复: 开" in p
    assert "messenger=AI拟稿·人审后发" in p
    # 不给 config＝零快照（旧行为），不是空块
    p0 = ap.build_planner_prompt("g", "master", ["/workspace"], lang="zh")
    assert "当前设置现状" not in p0


def test_snapshot_pure_fn_and_en():
    snap = ap.build_settings_snapshot(
        {"inbox": {"work_schedule": {"enabled": True}}}, lang="en")
    assert "Work schedule: on" in snap
    assert "snapshot" in snap


def test_validate_ask_passthrough_only_when_no_steps():
    v = ap.validate_plan({"say": "含糊", "ask": "要调快还是调慢？",
                          "steps": []}, {}, "master")
    assert v["ok"] is False and v["ask"] == "要调快还是调慢？"
    # 有可执行步时 ask 丢弃（有步还追问＝拖泥带水）
    v2 = ap.validate_plan({"say": "s", "ask": "还要吗？", "steps": [
        {"action": "goto_page", "params": {"path": "/workspace"}}]},
        {}, "master", nav_paths={"/workspace"})
    assert v2["ok"] is True and v2["ask"] == ""
    # ask 超长截断
    v3 = ap.validate_plan({"say": "s", "ask": "问" * 300, "steps": []},
                          {}, "master")
    assert len(v3["ask"]) <= 120


# ── 确定性「带你去」导航（2026-08-30，「原来会带我去页面现在不带了」）─────
_NAV = {"/workspace", "/reply-settings", "/rpa-overview"}


def test_home_nav_inserted_before_settings_step():
    """改设置且人不在对应页面 → 首个设置步前确定性插 goto（不靠 LLM 心情）。"""
    v = ap.validate_plan(_steps(
        {"action": "set_automation_mode", "params": {"mode": "review"}},
    ), {}, "master", nav_paths=_NAV, page="/workspace")
    assert [s["action"] for s in v["steps"]] == \
        ["goto_page", "set_automation_mode"]
    nav = v["steps"][0]
    assert nav["kind"] == "nav" and nav["goto"] == "/reply-settings"
    assert nav["level"] == "L1" and nav["params"] == {"path": "/reply-settings"}
    assert nav["label"]
    # companion 能力开关走能力看板
    v2 = ap.validate_plan(_steps(
        {"action": "toggle_selfie", "params": {"enabled": True}},
    ), {}, "master", nav_paths=_NAV, page="/workspace")
    assert v2["steps"][0]["goto"] == "/rpa-overview"


def test_home_nav_skipped_when_already_on_page():
    for page in ("/reply-settings", "/reply-settings?tab=voice"):
        v = ap.validate_plan(_steps(
            {"action": "set_automation_mode", "params": {"mode": "review"}},
        ), {}, "master", nav_paths=_NAV, page=page)
        assert [s["action"] for s in v["steps"]] == ["set_automation_mode"]


def test_home_nav_not_duplicated_when_llm_already_planned_goto():
    v = ap.validate_plan(_steps(
        {"action": "goto_page", "params": {"path": "/reply-settings"}},
        {"action": "set_automation_mode", "params": {"mode": "review"}},
    ), {}, "master", nav_paths=_NAV, page="/workspace")
    assert [s["action"] for s in v["steps"]] == \
        ["goto_page", "set_automation_mode"]


def test_home_nav_skipped_when_home_not_whitelisted():
    v = ap.validate_plan(_steps(
        {"action": "set_automation_mode", "params": {"mode": "review"}},
    ), {}, "master", nav_paths={"/workspace"}, page="/workspace")
    assert [s["action"] for s in v["steps"]] == ["set_automation_mode"]


def test_home_nav_untouched_for_non_settings_plans():
    v = ap.validate_plan(_steps(
        {"action": "goto_page", "params": {"path": "/workspace"}},
        {"action": "query_ai_status", "params": {}},
    ), {}, "master", nav_paths=_NAV, page="/knowledge")
    assert [s["action"] for s in v["steps"]] == \
        ["goto_page", "query_ai_status"]
