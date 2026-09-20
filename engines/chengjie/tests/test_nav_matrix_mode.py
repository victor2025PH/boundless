# -*- coding: utf-8 -*-
"""真机矩阵更名迁移 + 简洁模式精简（2026-08-03）的结构门禁。

钉住的决策（改动这些语义请连同本文件一起改，而不是绕过）：
1. 真机矩阵五页（矩阵总览 + 四渠道）不在简洁模式清单——简洁模式=值班/看店视角；
2. 五页收进完整模式「真机矩阵」组（section_channels），总览排第一；
3. 五页命令面板 simple=True（侧栏藏、Ctrl+K 仍可搜=藏而不废），且 cmd_keys 保留
   旧名（渠道总览/渠道中心），老用户搜旧名仍命中；
4. nav_matrix_items 上下文导航数据在场且与 MATRIX_ITEM_IDS 一致，两套侧栏模板
   都消费它（简洁模式深链进矩阵页不成孤岛）；
5. Telegram 刻意不挂 feature（主号=基础产品面，挂档会锁核心渠道进 flagship）；
   其余四项挂 rpa，档位锁定时 nav_matrix_items 同步打 locked 注解；
6. 简洁清单尺寸棘轮：主区 ≤8、折叠区 ≤6——「简洁模式再度膨胀」必须是显式决策。
"""
from __future__ import annotations

from pathlib import Path

from src.web.nav_schema import (
    MATRIX_ITEM_IDS,
    NAV_GROUPS_FULL,
    NAV_ITEMS,
    SIMPLE_CORE,
    SIMPLE_MORE,
    get_nav_context,
)

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"


def test_matrix_items_not_in_simple_lists():
    simple = set(SIMPLE_CORE) | set(SIMPLE_MORE)
    leaked = [i for i in MATRIX_ITEM_IDS if i in simple]
    assert not leaked, f"真机矩阵项回流简洁模式（应仅完整模式可见）: {leaked}"


def test_ops_heavy_pages_not_in_simple_lists():
    """运维/分析/记账类页面留在完整模式（精简的另一半，防单项回流）。"""
    simple = set(SIMPLE_CORE) | set(SIMPLE_MORE)
    heavy = {"personas", "ops", "analytics", "audit", "episodic",
             "relations_health", "monetization"}
    leaked = sorted(heavy & simple)
    assert not leaked, f"重运维页回流简洁模式: {leaked}"


def test_simple_lists_size_ratchet():
    assert len(SIMPLE_CORE) <= 8, (
        f"简洁主区膨胀到 {len(SIMPLE_CORE)} 项——精简是本次改版的核心承诺，"
        "新增前先确认它真是『每天要碰』的")
    assert len(SIMPLE_MORE) <= 6, f"简洁折叠区膨胀到 {len(SIMPLE_MORE)} 项"


def test_matrix_group_in_full_mode_overview_first():
    grp = next(g for g in NAV_GROUPS_FULL if g["label_key"] == "section_channels")
    assert grp["label_zh"] == "真机矩阵"
    items = [i for i in grp["items"] if isinstance(i, str)]
    for mid in MATRIX_ITEM_IDS:
        assert mid in items, f"完整模式真机矩阵组缺 {mid}"
    assert items[0] == "rpa_overview", "矩阵总览应排组内第一"
    assert "__domain_pages__" not in grp["items"], (
        "域动态页哨兵不应留在真机矩阵组（支付域渠道页≠矩阵运维，已挪工作台组尾）")


def test_domain_sentinel_lives_in_workbench_group():
    grp = next(g for g in NAV_GROUPS_FULL if g["label_key"] == "section_workbench")
    assert "__domain_pages__" in grp["items"]


def test_matrix_cmd_items_searchable_in_simple_with_legacy_names():
    ctx = get_nav_context()
    by_path = {it["path"]: it for it in ctx["nav_cmd_items"]}
    legacy = {
        "/rpa-overview": "渠道总览",
        "/workspace/channels/telegram": "渠道中心",
        "/workspace/channels/line": "渠道中心",
        "/workspace/channels/messenger": "渠道中心",
        "/workspace/channels/whatsapp": "渠道中心",
    }
    for mid in MATRIX_ITEM_IDS:
        path = NAV_ITEMS[mid]["path"]
        it = by_path.get(path)
        assert it is not None, f"命令面板缺矩阵项 {mid}"
        assert it["simple"] is True, (
            f"{mid} 在简洁模式命令面板不可搜——侧栏藏了，Ctrl+K 是唯一兜底入口")
        assert "真机矩阵" in it["cmd_keys"], f"{mid} cmd_keys 缺新名『真机矩阵』"
        assert legacy[path] in it["cmd_keys"], (
            f"{mid} cmd_keys 丢了旧名『{legacy[path]}』——改名后老用户搜旧名必须仍命中")


def test_nav_matrix_items_export_matches_ids():
    ctx = get_nav_context()
    items = ctx["nav_matrix_items"]
    assert [it["path"] for it in items] == [NAV_ITEMS[i]["path"] for i in MATRIX_ITEM_IDS]


def test_telegram_ungated_others_rpa():
    assert "feature" not in NAV_ITEMS["telegram"], (
        "Telegram 主号是基础产品面，挂档会把核心渠道锁进 flagship"
        "（test_page_feature_mapping_pure 另有钉）")
    for mid in ("rpa_overview", "line_rpa", "messenger_rpa", "whatsapp_rpa"):
        assert NAV_ITEMS[mid].get("feature") == "rpa", f"{mid} 应挂 rpa 档"


def test_matrix_items_get_locked_annotation_when_rpa_locked():
    # ui_visibility 显隐层（2026-08-14）缺省会剔除矩阵项——本测试只验 licensing
    # 锁标注解，显式开 matrix_nav 让矩阵项在场（显隐语义另见 test_ui_visibility）。
    cfg = {"licensing": {"feature_gate": {"enabled": True, "plan_override": "pro"}},
           "ui_visibility": {"matrix_nav": True}}
    ctx = get_nav_context(cfg)
    by_path = {it["path"]: it for it in ctx["nav_matrix_items"]}
    assert not by_path["/workspace/channels/telegram"].get("locked"), \
        "Telegram 无 feature，不应被锁"
    for mid in ("rpa_overview", "line_rpa", "messenger_rpa", "whatsapp_rpa"):
        assert by_path[NAV_ITEMS[mid]["path"]].get("locked") is True, (
            f"{mid} 在 pro 档（rpa=flagship）应带 locked 注解——上下文导航也要走锁标升级引导")


def test_sidebar_templates_consume_matrix_context():
    """静态接线钉：两套侧栏都消费 nav_matrix_items，且条件正确（防静默解线）。"""
    base = (_TPL / "base.html").read_text(encoding="utf-8")
    wsb = (_TPL / "_ws_sidebar.html").read_text(encoding="utf-8")
    assert "nav_matrix_items" in base and "active == 'rpa_overview'" in base, (
        "base.html 丢了矩阵上下文导航（简洁模式深链 /rpa-overview 将成孤岛）")
    assert "nav_matrix_items" in wsb and "/workspace/channels/" in wsb, (
        "_ws_sidebar.html 丢了矩阵上下文导航（渠道中心壳只有这条侧栏）")


def test_section_channels_i18n_says_matrix():
    from src.web.web_i18n import get_translations
    zh, en = get_translations("zh"), get_translations("en")
    assert zh.get("section_channels") == "真机矩阵"
    assert en.get("section_channels") == "Device Matrix"
    assert zh.get("rpa_overview") == "矩阵总览"
