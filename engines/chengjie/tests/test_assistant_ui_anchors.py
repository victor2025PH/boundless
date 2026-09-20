# -*- coding: utf-8 -*-
"""小智 DOM 动作总线门禁（实施88 P0，2026-08-30）。

守四条不变量（改 ui_anchors.py / plan_action ui 分支 / 规划器前先读）：
1. **揭示类白名单**：手势只许 click/fill/show、等级只许 L1，且锚点清单
   **显式钉死**——新增锚点必须来这里改测试＝一次强制代码评审（发送/提交/
   删除类控件混进注册表是本总线唯一的灾难面）。
2. **选择器有模板证据**：每条锚点的 sel 必须能在其页面模板源码里找到对应
   物证（id=/data-anchor=/onclick=/data-help=）——模板重构把控件改没了
   这里先红，不留「流星飞向空气」的幽灵锚点。
3. **plan_action 结构性复验**：未注册锚点必拒、fill 缺 text 必拒、超长必拒；
   通过时 sel/gesture 只能来自注册表（LLM 提名的 anchor id 换出，永不透传
   LLM 生成的选择器）。
4. **规划器集成**：ui_act 进 prompt（含控件白名单块）、ui 步带 ui 载荷与
   锚点人话标签、不在目标页时自动插 goto_page（home-nav 对 ui 步生效）。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.assistant import actions as act
from src.assistant import agent_planner as planner
from src.assistant import ui_anchors as ua

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"

# page → 模板文件（锚点扩到新页面时在此登记）
_PAGE_TEMPLATES = {
    "/workspace": "unified_inbox.html",
    "/knowledge": "knowledge.html",
    "/personas": "personas.html",
    "/reply-settings": "reply_settings.html",
}

# 锚点清单钉死（新增/删除锚点必须同步改这里——强制评审点，见模块注释 1）
_PINNED_ANCHORS = {
    "open_account_drawer",
    "inbox_search",
    "kb_new_entry",
    "kb_search",
    "personas_new",
    "show_reply_budget_guard",
}


# ── 1. 注册表结构与白名单 ─────────────────────────────────────────────────
def test_registry_is_pinned():
    assert set(ua.UI_ANCHORS) == _PINNED_ANCHORS, (
        "锚点清单变了：新增揭示类控件请先自查「点它会不会发送/提交/删除」，"
        "然后同步更新 _PINNED_ANCHORS（这是刻意的强制评审点）")


def test_registry_schema_and_whitelists():
    for aid, a in ua.UI_ANCHORS.items():
        assert a["gesture"] in ua.GESTURES, f"{aid} 手势超白名单"
        assert a["level"] == "L1", (
            f"{aid} 等级 {a['level']}：DOM 总线当前只许 L1 揭示类；"
            "要上 L2 需先给前端补确认卡链再放开")
        assert a["page"] in act.CORE_NAV_PATHS, (
            f"{aid} 的 page 不在 CORE_NAV_PATHS——nav_schema 提取失败时"
            "home-nav 插步会断链")
        assert str(a.get("sel") or "").strip(), f"{aid} 缺 sel"
        for k in ("label_zh", "label_en", "desc_zh", "desc_en"):
            assert str(a.get(k) or "").strip(), f"{aid} 缺 {k}"


def _sel_evidence(sel: str) -> str:
    """CSS 选择器 → 模板源码物证子串。"""
    s = sel.strip()
    m = re.fullmatch(r"#([\w-]+)", s)
    if m:
        return f'id="{m.group(1)}"'
    m = re.fullmatch(r'\[data-anchor="([^"]+)"\]', s)
    if m:
        return f'data-anchor="{m.group(1)}"'
    m = re.fullmatch(r'\[onclick\^="([^"]+)"\]', s)
    if m:
        return f'onclick="{m.group(1)}'
    m = re.fullmatch(r'\w+\[data-help="([^"]+)"\]', s)
    if m:
        return f'data-help="{m.group(1)}"'
    raise AssertionError(
        f"不认识的选择器形态 {sel!r}：请在 _sel_evidence 里补该形态的"
        "物证推导（保持「每条锚点都有模板证据」不变量）")


def test_every_anchor_has_template_evidence():
    for aid, a in ua.UI_ANCHORS.items():
        tpl = _PAGE_TEMPLATES.get(a["page"])
        assert tpl, f"{aid} 的页面 {a['page']} 未登记模板映射"
        src = (_TPL_DIR / tpl).read_text(encoding="utf-8")
        needle = _sel_evidence(a["sel"])
        assert needle in src, (
            f"{aid} 的物证 {needle!r} 在 {tpl} 里找不到——控件被改名/删除，"
            "流星会飞向空气；同步改注册表或模板")


# ── 2. plan_action 复验 ──────────────────────────────────────────────────
def test_plan_rejects_unregistered_anchor():
    bad = act.plan_action("ui_act", {"anchor": "rm_rf_everything"}, {})
    assert not bad["ok"] and bad["error"] == "bad_params"
    empty = act.plan_action("ui_act", {}, {})
    assert not empty["ok"] and empty["error"] == "bad_params"


def test_plan_fill_requires_text_and_caps_length():
    miss = act.plan_action("ui_act", {"anchor": "kb_search"}, {})
    assert not miss["ok"] and "text" in str(miss["detail"])
    blank = act.plan_action("ui_act", {"anchor": "kb_search", "text": "  "},
                            {})
    assert not blank["ok"]
    long = act.plan_action(
        "ui_act",
        {"anchor": "kb_search", "text": "x" * (ua.FILL_TEXT_MAXLEN + 1)}, {})
    assert not long["ok"]
    ok = act.plan_action("ui_act", {"anchor": "kb_search", "text": "退款"},
                         {})
    assert ok["ok"] and ok["ui"]["text"] == "退款"


def test_plan_ui_payload_comes_from_registry_only():
    """LLM 能影响的只有 anchor id 与 fill 文本；sel/gesture/page 必须逐字
    来自注册表（试图经 params 塞 sel 一律被忽略）。"""
    ok = act.plan_action(
        "ui_act",
        {"anchor": "open_account_drawer", "sel": "#evil", "gesture": "fill"},
        {})
    assert ok["ok"]
    reg = ua.UI_ANCHORS["open_account_drawer"]
    assert ok["ui"]["sel"] == reg["sel"]
    assert ok["ui"]["gesture"] == reg["gesture"]
    assert ok["ui"]["page"] == reg["page"]
    assert ok["level"] == "L1" and ok["kind"] == "ui"


def test_plan_click_ignores_text():
    ok = act.plan_action(
        "ui_act", {"anchor": "open_account_drawer", "text": "whatever"}, {})
    assert ok["ok"] and ok["ui"]["text"] == ""


def test_ui_label_is_anchor_label_bilingual():
    zh = act.plan_action("ui_act", {"anchor": "personas_new"}, {}, lang="zh")
    en = act.plan_action("ui_act", {"anchor": "personas_new"}, {}, lang="en")
    assert zh["ui_label"] == ua.UI_ANCHORS["personas_new"]["label_zh"]
    assert en["ui_label"] == ua.UI_ANCHORS["personas_new"]["label_en"]
    assert zh["ui_label"] != en["ui_label"]


# ── 3. 规划器集成 ────────────────────────────────────────────────────────
def test_prompt_lists_ui_act_and_anchor_block():
    p = planner.build_planner_prompt("帮我搜知识库", "master",
                                     list(act.CORE_NAV_PATHS), lang="zh")
    assert "ui_act" in p and "页面控件白名单" in p
    for aid in _PINNED_ANCHORS:
        assert aid in p, f"锚点 {aid} 没进规划 prompt"
    pe = planner.build_planner_prompt("search kb", "master",
                                      list(act.CORE_NAV_PATHS), lang="en")
    assert "On-page control whitelist" in pe


def test_validate_plan_carries_ui_payload_and_label():
    parsed = {"say": "好", "steps": [
        {"action": "ui_act", "params": {"anchor": "kb_search",
                                        "text": "退款"}}]}
    v = planner.validate_plan(parsed, {}, "master",
                              nav_paths=set(act.CORE_NAV_PATHS),
                              page="/knowledge")
    assert v["ok"], v
    step = v["steps"][0]
    assert step["kind"] == "ui"
    assert step["ui"]["sel"] == ua.UI_ANCHORS["kb_search"]["sel"]
    assert step["label"] == ua.UI_ANCHORS["kb_search"]["label_zh"]
    assert step["params"] == {"anchor": "kb_search", "text": "退款"}


def test_validate_plan_drops_bad_anchor_honestly():
    parsed = {"say": "好", "steps": [
        {"action": "ui_act", "params": {"anchor": "nope"}}]}
    v = planner.validate_plan(parsed, {}, "master",
                              nav_paths=set(act.CORE_NAV_PATHS))
    assert not v["ok"]
    assert v["dropped"] and v["dropped"][0]["action"] == "ui_act"


def test_home_nav_inserted_before_offpage_ui_step():
    """人在 /dashboard、控件在 /knowledge → 自动插 goto_page 带路。"""
    parsed = {"say": "好", "steps": [
        {"action": "ui_act", "params": {"anchor": "kb_search",
                                        "text": "退款"}}]}
    v = planner.validate_plan(parsed, {}, "master",
                              nav_paths=set(act.CORE_NAV_PATHS),
                              page="/dashboard")
    assert v["ok"]
    kinds = [s["kind"] for s in v["steps"]]
    assert kinds == ["nav", "ui"]
    assert v["steps"][0]["goto"] == "/knowledge"


def test_home_nav_not_inserted_when_already_on_page():
    parsed = {"say": "好", "steps": [
        {"action": "ui_act", "params": {"anchor": "kb_search",
                                        "text": "退款"}}]}
    v = planner.validate_plan(parsed, {}, "master",
                              nav_paths=set(act.CORE_NAV_PATHS),
                              page="/knowledge")
    assert v["ok"]
    assert [s["kind"] for s in v["steps"]] == ["ui"]


def test_agent_role_can_use_ui_act():
    """坐席角色 L0/L1 可用 DOM 总线（揭示类与 goto 同信任级）。"""
    assert "L1" in act.allowed_levels_for_role("agent")
    v = planner.validate_plan(
        {"say": "", "steps": [{"action": "ui_act",
                               "params": {"anchor": "kb_search",
                                          "text": "a"}}]},
        {}, "agent", nav_paths=set(act.CORE_NAV_PATHS), page="/knowledge")
    assert v["ok"]


# ── 4. 同批扩容的横向契约 ────────────────────────────────────────────────
def test_humanize_flags_mapping_synced():
    """小智动作路由与 reply-settings 保存路由共用「拟人链开关 → worker
    形参」映射语义——两份字面量漂移=其中一路热更静默失效。"""
    from src.web.routes import assistant_action_routes as aar
    from src.web.routes import reply_settings_routes as rsr

    assert aar._HUMANIZE_FLAGS == rsr._HUMANIZE_FLAGS
    assert aar._DELAY_PREFIX == rsr._DELAY_PREFIX


# ── 5. 前端接线（静态证据；行为由真浏览器门禁 M8 系补）───────────────────
def test_agent_js_wires_ui_kind():
    js = (Path(__file__).resolve().parents[1] / "shared" / "assistant" /
          "assistant-agent.js").read_text(encoding="utf-8")
    assert "step.kind === 'ui'" in js, "execStep 缺 ui 分支"
    assert "asb_agent_ui_" in js, "DOM 总线缺埋点（没有埋点就没有裁决权）"
    for key in ("st_ui_click", "st_ui_fill", "st_ui_show", "ui_missing",
                "ui_on_pc"):
        assert js.count(key) >= 2, f"i18n 键 {key} 缺 zh/en 之一"
    # 诚实失败：找不到控件必须走 fail 而不是装 ok
    assert "stepDone(step, 'fail', t('ui_missing'))" in js
