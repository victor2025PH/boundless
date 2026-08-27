# -*- coding: utf-8 -*-
"""2026-08-16 管理面改造：侧栏 8 组重组的结构门禁。

钉住的决策（改这些语义请连同本文件一起改，而不是绕过）：
1. 完整模式恒 8 组、顺序固定——「用量与计费」夹在数据洞察与安全合规之间；
2. 「看数」只住数据洞察：主管四看板（队列/绩效/AI 质量/ROI）自命令面板孤儿
   收编；客户营收（原变现营收）留守组定义（侧栏显隐跟 monetization.enabled）；
   策略效果 2026-08-18 降出侧栏进 CMD_EXTRA（空壳对照，与 /diff 同例）；
3. 新组「用量与计费」＝用量与额度(/workspace/usage) + 会员中心（仍 master_only）
   ——membership 不得回流系统管理组；
4. 用量入口必须在简洁模式可达（老板默认简洁模式；SIMPLE_MORE 棘轮 ≤6 用满）；
5. 每组必须带一句话定位（note_key/note_zh，zh+en 双语齐备），两套侧栏模板
   （base.html / _ws_sidebar.html）都渲染 title 悬浮——分类语义防再漂移的 UI 锚点；
6. 改名双面一致：dashboard=今日概览 / monetization=客户营收（词条值级改名，
   侧栏与页面标题同源）；cmd_keys 保留旧名（老用户 Ctrl+K 搜旧名必须命中）；
7. 命令面板专属清单＝templates/import/work_goal/diff/strategy_analytics
   （五看板已升格，不许回流；策略效果只许降不许回流侧栏）。
"""
from __future__ import annotations

from pathlib import Path

from src.web.nav_schema import (
    CMD_EXTRA_ITEMS,
    NAV_GROUPS_FULL,
    NAV_ICONS,
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
    # 2026-08-18 P1：漏斗与四看板收成页群（analytics/ws_boards 两个入口 +
    # 页内 Tab），组内只留 5 项——单页不回流侧栏。
    assert items == ["dash", "ops", "analytics", "monetization", "ws_boards"]
    for gone in ("funnel", "ws_queue", "ws_perf", "ws_aiq", "ws_roi",
                 "strategy_analytics"):
        assert gone not in items, f"{gone} 已收进页群/命令面板，不得回流侧栏"
    assert "strategy_analytics" not in _group("section_ai_kb")["items"], (
        "策略效果不得回流 AI 与知识组")
    assert "strategy_analytics" in CMD_EXTRA_ITEMS
    assert NAV_ITEMS["strategy_analytics"]["path"] == "/strategy-analytics"


def test_supervisor_boards_are_real_nav_items():
    expect = {
        "ws_boards": "/workspace/queue",
        "ws_queue": "/workspace/queue",
        "ws_perf": "/workspace/agent-perf",
        "ws_aiq": "/workspace/ai-quality",
        "ws_roi": "/workspace/roi",
        "usage_center": "/workspace/usage",
    }
    # 全部在当前窗口打开（2026-08-16 老板点名：桌面壳里 _blank 弹独立壳窗
    # ＝「弹出面板」，且 /workspace/* 子页不在壳内 __openUniqueUrl 复用范围、
    # 每点必新弹；与其他洞察面板行为一致）。勿回退 _blank/winname。
    # 2026-08-18 P1：侧栏入口收成 ws_boards（落地队列页），四单页仍是真
    # NAV_ITEMS（URL/标签/Ctrl+K 直达不变），只是不再各占一行侧栏。
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
    # diff（版本对比）2026-08-18 随上游 /templates 同例降出侧栏：快照只在保存
    # 话术模板/回复策略等旧配置流时产生，不用那些编辑流的部署里是常驻空页；
    # 审计页/仪表盘「可回滚」深链与 /api/rollback 全保留，命令面板仍可搜。
    assert set(CMD_EXTRA_ITEMS) == {
        "templates", "import", "work_goal", "diff", "strategy_analytics",
        "funnel", "ws_queue", "ws_perf", "ws_aiq", "ws_roi",
    }, (
        "命令面板专属清单＝低频工具 + 页群单页直达；页群单页有 ws_boards/"
        "analytics 常驻侧栏入口 + 页内 Tab，不是 2026-08-14 的孤儿化回退")


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


def test_strategy_analytics_stays_searchable_not_sidebar():
    ctx = get_nav_context()
    side_paths = set()
    for g in ctx["nav_groups"]:
        for it in g["items"]:
            if isinstance(it, dict):
                side_paths.add(it.get("path"))
    assert "/strategy-analytics" not in side_paths
    cmd_paths = {it.get("path") for it in ctx["nav_cmd_items"] if isinstance(it, dict)}
    assert "/strategy-analytics" in cmd_paths


def test_monetization_hidden_unless_product_on():
    hidden = get_nav_context({})
    shown = get_nav_context({"monetization": {"enabled": True}})

    def _paths(ctx):
        out = set()
        for g in ctx["nav_groups"]:
            for it in g["items"]:
                if isinstance(it, dict):
                    out.add(it.get("path"))
        return out

    assert "/monetization" not in _paths(hidden)
    assert "/monetization" not in {
        it.get("path") for it in hidden["nav_cmd_items"] if isinstance(it, dict)
    }
    assert "/monetization" in _paths(shown)


def test_full_mode_sidebar_icons_unique():
    """缺省过滤后，以及矩阵+变现全开后，侧栏可见项图标不得撞车。"""
    cfgs = (
        {},
        {"monetization": {"enabled": True}},
        {"ui_visibility": {"matrix_nav": True, "group_show": True},
         "monetization": {"enabled": True}},
    )
    for cfg in cfgs:
        seen = {}
        for g in get_nav_context(cfg)["nav_groups"]:
            for it in g["items"]:
                if not isinstance(it, dict):
                    continue
                ic, path = it.get("icon"), it.get("path")
                assert ic, f"{path} 缺 icon"
                assert ic in NAV_ICONS, f"icon {ic} 未登记 SVG"
                assert ic not in seen, f"icon {ic} 撞车：{seen[ic]} vs {path}"
                seen[ic] = path


def test_gs_shortcut_goes_to_strategies():
    base = (_TPL / "base.html").read_text(encoding="utf-8")
    assert "s:'/strategies'" in base
    assert "s:'/strategy-analytics'" not in base
    assert "get('strategies','回复策略')" in base


def test_admin_pv_beacon_wired():
    partial = (_TPL / "_page_view_beacon.html").read_text(encoding="utf-8")
    assert "pv_'+slug" in partial or "pv_'" in partial
    assert "/api/telemetry/ui-event" in partial
    assert "p==='/workspace'" in partial
    for name in ("base.html", "workspace_base.html"):
        src = (_TPL / name).read_text(encoding="utf-8")
        assert '_page_view_beacon.html' in src, f"{name} 未挂 PV 埋点"


# ── 页群 Tab（2026-08-18 P1）────────────────────────────────────────────

# 页群成员 → 模板文件（成员表改动必须同步这里，Tab 接线检查按它展开）
_CLUSTER_TEMPLATES = {
    "ws_queue": "queue_monitor.html",
    "ws_perf": "agent_perf.html",
    "ws_aiq": "ai_quality.html",
    "ws_roi": "workspace_roi.html",
    "analytics": "analytics.html",
    "funnel": "funnel.html",
}


def test_page_clusters_resolve_and_flow_to_context():
    from src.web.nav_schema import PAGE_CLUSTERS

    for cid, ids in PAGE_CLUSTERS.items():
        assert len(ids) > 1, f"页群 {cid} 少于 2 页＝没有互切意义"
        for iid in ids:
            assert iid in NAV_ITEMS, f"页群 {cid} 引用不存在的项 {iid}"
    ctx = get_nav_context()
    assert set(ctx["nav_clusters"]) == set(PAGE_CLUSTERS)
    # 过滤视图（锁定/显隐）也必须透传 nav_clusters——成员页自带权限闸
    ctx2 = get_nav_context({})
    assert set(ctx2["nav_clusters"]) == set(PAGE_CLUSTERS)


def test_cluster_member_templates_render_tabs():
    """每个页群成员模板都自报 cluster_id/cluster_here 并挂 Tab 条 partial。

    cluster_here 必须与 NAV_ITEMS 路径逐字一致——写错＝该页 Tab 全灰无高亮。
    """
    from src.web.nav_schema import PAGE_CLUSTERS

    assert (_TPL / "_cluster_tabs.html").is_file()
    for cid, ids in PAGE_CLUSTERS.items():
        for iid in ids:
            tpl_name = _CLUSTER_TEMPLATES[iid]
            src = (_TPL / tpl_name).read_text(encoding="utf-8")
            assert f'cluster_id = "{cid}"' in src, f"{tpl_name} 未自报页群 {cid}"
            here = NAV_ITEMS[iid]["path"]
            assert f'cluster_here = "{here}"' in src, (
                f"{tpl_name} 的 cluster_here 与 NAV_ITEMS 路径不一致")
            assert '_cluster_tabs.html' in src, f"{tpl_name} 未挂 Tab 条"


def test_cluster_partial_defensive_and_zero_js():
    """partial 对缺 nav_clusters 的最小上下文静默；纯链接零 JS 零 id。"""
    src = (_TPL / "_cluster_tabs.html").read_text(encoding="utf-8")
    assert "(nav_clusters or {})" in src, "缺席上下文必须静默（渲染类测试/热更窗口）"
    assert "<script" not in src and "onclick" not in src, "Tab 条必须纯链接"
    assert 'id="' not in src, "partial 不得引入 id（重复 id 门禁）"


def test_ws_boards_entry_bilingual_and_searchable():
    zh, en = get_translations("zh"), get_translations("en")
    assert zh.get("nav_ws_boards") == "主管看板" and en.get("nav_ws_boards")
    assert zh.get("nav_cluster_aria") and en.get("nav_cluster_aria")
    ctx = get_nav_context()
    cmd_paths = {it.get("path") for it in ctx["nav_cmd_items"] if isinstance(it, dict)}
    # 页群单页 Ctrl+K 直达仍在
    for p in ("/workspace/agent-perf", "/workspace/ai-quality",
              "/workspace/roi", "/funnel"):
        assert p in cmd_paths, f"{p} 应保持命令面板可搜"
