# -*- coding: utf-8 -*-
"""2026-08-16 管理面改造：侧栏 8 组重组的结构门禁。

钉住的决策（改这些语义请连同本文件一起改，而不是绕过）：
1. 完整模式恒 8 组、顺序固定——「用量与计费」夹在数据洞察与安全合规之间；
2. 「看数」只住数据洞察：策略效果自 AI 与知识移入；主管四看板（队列/绩效/
   AI 质量/ROI）自命令面板孤儿收编；客户营收（原变现营收）留守；
3. 新组「用量与计费」＝用量与额度(/workspace/usage) + 会员中心（仍 master_only）
   ——membership 不得回流系统管理组；
4. 用量入口必须在简洁模式可达（老板默认简洁模式；SIMPLE_MORE 棘轮 ≤6 用满）；
5. 每组必须带一句话定位（note_key/note_zh，zh+en 双语齐备），两套侧栏模板
   （base.html / _ws_sidebar.html）都渲染 title 悬浮——分类语义防再漂移的 UI 锚点；
6. 改名双面一致：dashboard=今日概览 / monetization=客户营收（词条值级改名，
   侧栏与页面标题同源）；cmd_keys 保留旧名（老用户 Ctrl+K 搜旧名必须命中）；
7. 命令面板专属清单只剩 templates/import/work_goal（五看板已升格，不许回流）。
"""
from __future__ import annotations

from pathlib import Path

from src.web.nav_schema import (
    CMD_EXTRA_ITEMS,
    NAV_GROUPS_FULL,
    NAV_ITEMS,
    SIMPLE_MORE,
    get_nav_context,
)
from src.web.web_i18n import get_translations

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"

_EXPECTED_GROUP_ORDER = [
    "section_workbench", "section_channels", "section_ai_kb",
    "section_insights", "section_usage_billing", "section_compliance",
    "section_system", "section_support",
]


def _group(label_key: str) -> dict:
    return next(g for g in NAV_GROUPS_FULL if g["label_key"] == label_key)


def test_eight_groups_in_fixed_order():
    assert [g["label_key"] for g in NAV_GROUPS_FULL] == _EXPECTED_GROUP_ORDER


def test_usage_billing_group_members():
    grp = _group("section_usage_billing")
    assert grp["items"] == ["usage_center", "membership"]
    # membership 仍是 master_only（admin 只看得到用量与额度）
    assert NAV_ITEMS["membership"].get("master_only") is True
    # 不得回流系统管理组
    assert "membership" not in _group("section_system")["items"]


def test_insights_owns_all_analytics_pages():
    items = _group("section_insights")["items"]
    for i in ("dash", "ops", "analytics", "funnel", "strategy_analytics",
              "monetization", "ws_queue", "ws_perf", "ws_aiq", "ws_roi"):
        assert i in items, f"数据洞察组缺 {i}"
    assert "strategy_analytics" not in _group("section_ai_kb")["items"], (
        "策略效果是看数页，不得回流 AI 与知识组")


def test_supervisor_boards_are_real_nav_items():
    expect = {
        "ws_queue": "/workspace/queue",
        "ws_perf": "/workspace/agent-perf",
        "ws_aiq": "/workspace/ai-quality",
        "ws_roi": "/workspace/roi",
        "usage_center": "/workspace/usage",
    }
    # 五页全部在当前窗口打开（2026-08-16 老板点名：桌面壳里 _blank 弹独立壳窗
    # ＝「弹出面板」，且 /workspace/* 子页不在壳内 __openUniqueUrl 复用范围、
    # 每点必新弹；与其他洞察面板行为一致）。勿回退 _blank/winname。
    for iid, path in expect.items():
        it = NAV_ITEMS[iid]
        assert it["path"] == path
        assert not it.get("feature"), f"{iid} 不挂档位（页面路由自带主管闸）"
        assert not it.get("target"), f"{iid} 应在当前窗口打开（勿回退 _blank 弹窗）"
        assert not it.get("winname"), f"{iid} 应在当前窗口打开（勿回退命名窗弹窗）"
    # 边界：坐席工作台不在「当前窗口打开」决策内——坐席窗独立 + BC 探活去重是
    # 多窗治理主线的刻意设计（聊天现场是全站最贵状态，绝不被后台导航挤占）。
    assert NAV_ITEMS["workspace"].get("target") == "_blank"
    assert NAV_ITEMS["workspace"].get("winname") == "workspace"


def test_usage_center_reachable_in_simple_mode():
    assert "usage_center" in SIMPLE_MORE, "老板默认简洁模式，用量入口必须可达"
    ctx = get_nav_context()
    uc = next(c for c in ctx["nav_cmd_items"] if c["path"] == "/workspace/usage")
    assert uc["simple"] is True, "简洁模式命令面板必须能搜到用量与额度"


def test_cmd_extra_only_low_freq_tools_left():
    assert set(CMD_EXTRA_ITEMS) == {"templates", "import", "work_goal"}, (
        "五个主管看板已升格正式入口，不许回流命令面板孤儿清单")


def test_every_group_has_bilingual_note():
    zh, en = get_translations("zh"), get_translations("en")
    for g in NAV_GROUPS_FULL:
        nk = g.get("note_key")
        assert nk and g.get("note_zh"), f"{g['label_key']} 缺一句话定位"
        assert zh.get(nk) and en.get(nk), f"{nk} 缺双语词条"
        assert zh.get(g["label_key"]) and en.get(g["label_key"]), (
            f"{g['label_key']} 缺双语词条")


def test_rename_is_double_faced_and_legacy_searchable():
    zh = get_translations("zh")
    assert zh.get("dashboard") == "今日概览"
    assert zh.get("monetization") == "客户营收"
    # 旧名保留在 cmd_keys：改名后老用户搜旧名仍命中
    assert "数据概览" in NAV_ITEMS["dash"]["cmd_keys"]
    assert "变现营收" in NAV_ITEMS["monetization"]["cmd_keys"]


def test_both_sidebars_render_group_note_tooltip():
    base = (_TPL / "base.html").read_text(encoding="utf-8")
    wsb = (_TPL / "_ws_sidebar.html").read_text(encoding="utf-8")
    for tpl, name in ((base, "base.html"), (wsb, "_ws_sidebar.html")):
        assert "note_key" in tpl, f"{name} 未渲染分组一句话定位 title"
