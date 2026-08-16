# -*- coding: utf-8 -*-
"""侧栏菜单单一真相（NAV_SPEC）结构契约 — 2026-08-14 IA 重组配套门禁。

守三件事：
  1. 品牌替换锚点不丢（dashboard.py 服务期 .replace 依赖这些源串）；
  2. 菜单结构不悄悄膨胀/破相（32 个一级项、10 个页面族、角色属性齐全）；
  3. NAV_SPEC 引用的每个 page 在 dashboard.py 都有真实容器，死页不复活。
"""
import json
import re
from pathlib import Path

from src.host.dashboard_parts.sidebar import SIDEBAR_HTML, _nav_json

_DASH = (Path(__file__).resolve().parents[1] / "src" / "host" / "dashboard.py").read_text(
    encoding="utf-8"
)


def test_brand_anchors_preserved():
    """dashboard.py 服务期品牌替换依赖的源串必须原样存在。"""
    for anchor in ("<h1>OpenClaw</h1>", "OpenClaw v1.2.0", "智能群控中心"):
        assert anchor in SIDEBAR_HTML, f"品牌替换锚点丢失: {anchor}"


def test_nav_item_budget():
    """一级菜单项收敛在 31（P2：AI 指令收编进 Ctrl+K 命令面板）；新增菜单项须有意识地改这里。"""
    real_items = re.findall(r'<div class="nav-item(?: active)?"', SIDEBAR_HTML)
    assert len(real_items) == 31, f"一级菜单项应为 31, 实际 {len(real_items)}"


def test_family_mapping_complete():
    nav = json.loads(_nav_json())
    assert len(nav["families"]) == 10, "页面族应为 10 个"
    for root, fam in nav["families"].items():
        assert fam["tabs"], f"家族 {root} 没有页签"
        assert fam["tabs"][0]["page"] == root, f"家族 {root} 首页签必须是默认落点自身"
    # 子页映射与页签一一对应
    for child, root in nav["pageFamily"].items():
        assert child in nav["tabLabel"], f"子页 {child} 缺页签名"
        assert root in nav["families"], f"子页 {child} 指向未知家族 {root}"


def test_all_pages_exist_in_dashboard():
    """NAV_SPEC 引用的每个 data-page 都必须有 page-<id> 容器（防指向不存在的页面）。"""
    pages = set(re.findall(r'data-page="([a-z0-9-]+)"', SIDEBAR_HTML))
    missing = [pg for pg in sorted(pages) if f'id="page-{pg}"' not in _DASH]
    assert not missing, f"data-page 指向不存在的页面: {missing}"


def test_dead_pages_stay_dead():
    """已下线的死页不得复活（活引用判定：div id / loader 调用 / script 标签）。"""
    for dead in (
        'id="page-phrases"',
        'id="page-campaigns"',
        "loadPhrasesPage()",
        'src="/static/js/campaigns.js',
    ):
        assert dead not in _DASH, f"死页残留: {dead}"


def test_legacy_tt_divs_kept():
    """TikTok 页签靠 appendChild 搬这些 div 复用——绝不能删（2026-08-14 实锤）。"""
    for kept in (
        'id="page-conversations"',
        'id="page-leads"',
        'id="page-messages"',
        'id="page-account-farming"',
    ):
        assert kept in _DASH, f"TikTok 页签依赖的容器被误删: {kept}"


def test_role_attributes():
    """角色显隐契约：客服=总览(cs-keep)+线索与转化(cs-group)；系统组仅管理员。"""
    assert SIDEBAR_HTML.count('data-cs-group="1"') == 1
    assert SIDEBAR_HTML.count('data-cs-keep="1"') == 1
    assert 'data-cs-section="1"' in SIDEBAR_HTML
    assert 'data-admin-only="1"' in SIDEBAR_HTML
    assert 'id="cs-pending-badge"' in SIDEBAR_HTML


def test_nav_json_embedded():
    assert "window.__NAV=" in SIDEBAR_HTML
