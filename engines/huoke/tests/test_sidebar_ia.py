# -*- coding: utf-8 -*-
"""P4-1 侧栏信息架构：页面 id 零丢失 + 角色属性 + 品牌替换锚点仍在。

2026-08-14 IA v2（第二刀）契约更新：菜单收敛 56→32（NAV_SPEC 数据化 + 页面族），
分组更名 设备与集群→设备与网络 / 数据→数据与消息，集群管理移入系统组（仅管理员），
通知中心/告警规则/模板库 出管理员区并入页面族。结构细节见 tests/test_nav_spec.py。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.host.dashboard import DASHBOARD_HTML

_ROOT = Path(__file__).resolve().parents[1]
_CSS = (_ROOT / "src" / "host" / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
_CORE = (_ROOT / "src" / "host" / "static" / "js" / "core.js").read_text(encoding="utf-8")
_OV = (_ROOT / "src" / "host" / "static" / "js" / "overview.js").read_text(encoding="utf-8")

# 侧栏全部 data-page，缺一即侧栏丢页（hash 路由仍靠这些 id）。
# P2：chat 出栏（页面保留，入口=Ctrl+K 命令面板）；+cs-* 四个客服页面（lead-mesh 页面化）。
EXPECTED_PAGES = {
    "overview", "devices", "tasks",
    "cs-desk", "cs-inbox", "cs-search", "cs-command",
    "vpn-manage", "router-manage", "screens", "multi-screen", "groups",
    "plat-tiktok", "plat-telegram", "plat-whatsapp", "plat-facebook",
    "plat-linkedin", "plat-instagram", "plat-twitter",
    "workflows", "visual-workflow", "script-engine", "ai-script",
    "scheduled-jobs", "quick-actions", "sync-mirror", "op-replay",
    "batch-apk", "batch-text", "batch-upload", "app-manager", "screen-record",
    "analytics", "roi", "funnel", "data-export", "op-timeline", "audit",
    "health", "health-report", "perf-monitor", "logs", "notifications",
    "alert-rules", "device-assets", "studio",
    "cluster", "notify-center", "backup", "plugins", "tpl-market", "user-mgmt",
}

# IA v2：通知中心/告警规则→「消息与告警」族、模板库→脚本工房族（操作员可用）；
# 集群管理→系统组（加入集群/跨机运维=部署级动作，日常任务派发走任务中心即可）
ADMIN_ONLY_PAGES = {
    "cluster", "backup", "plugins", "user-mgmt",
}

IA_GROUP_LABELS = ("线索与转化", "获客作业", "设备与网络", "自动化", "数据与消息", "系统")


def _pages(html: str) -> set[str]:
    return set(re.findall(r'data-page="([^"]+)"', html))


def test_sidebar_keeps_every_hash_page():
    got = _pages(DASHBOARD_HTML)
    missing = EXPECTED_PAGES - got
    extra = got - EXPECTED_PAGES
    assert not missing, f"侧栏丢了页面: {sorted(missing)}"
    assert not extra, f"侧栏多了未登记页面: {sorted(extra)}"


def test_ia_group_labels_present():
    for label in IA_GROUP_LABELS:
        assert label in DASHBOARD_HTML, f"缺少分组「{label}」"


def test_admin_only_marks_system_group():
    idx = DASHBOARD_HTML.find('data-admin-only="1"')
    assert idx > 0, "系统组必须带 data-admin-only"
    admin_chunk = DASHBOARD_HTML[idx:]
    admin_pages = _pages(admin_chunk)
    assert admin_pages == ADMIN_ONLY_PAGES, (
        f"管理员区页面漂移: got={sorted(admin_pages)} expected={sorted(ADMIN_ONLY_PAGES)}"
    )
    # IA v2：集群管理关进系统组——加入集群/编号段/OTA 是部署级动作；
    # 操作员日常派发走任务中心（smart_scheduler 自动跨机路由），不再需要裸集群面板
    assert "cluster" in admin_pages, "集群管理应在系统组（仅管理员）"


def test_cs_keep_and_funnel_in_cs_group():
    assert 'data-cs-keep="1"' in DASHBOARD_HTML
    assert 'data-cs-section="1"' in DASHBOARD_HTML
    cs_start = DASHBOARD_HTML.find('data-cs-group="1"')
    cs_end = DASHBOARD_HTML.find("获客作业")
    cs = DASHBOARD_HTML[cs_start:cs_end]
    assert 'data-page="funnel"' in cs
    assert 'data-page="roi"' in cs


def test_brand_replace_anchors_still_in_template():
    """serve-time .replace() 的源串必须原样留在模板里，改成智拓字面量会让替换空转。"""
    assert "<title>OpenClaw 控制中心</title>" in DASHBOARD_HTML
    assert "<h1>OpenClaw</h1><small>智能群控中心</small>" in DASHBOARD_HTML
    assert "<span>OpenClaw v1.2.0</span>" in DASHBOARD_HTML


def test_head_sets_data_role_before_paint():
    head = DASHBOARD_HTML.split("</head>", 1)[0]
    assert "data-role" in head
    assert "oc_role" in head
    assert "dashboard.css?v=20260815p4" in head  # P4 资产版本（随 bump 更新断言）


def test_css_role_hide_and_js_hash_guard():
    assert 'html[data-role="operator"] [data-admin-only]' in _CSS
    assert 'html[data-role="customer_service"] .nav-section:not([data-cs-section])' in _CSS
    assert "function _navAllowed" in _OV
    assert "function _roleLabel" in _CORE
    assert "customer_service" in _CORE
    assert "showPage(" not in _CORE


# ── P4-2：侧栏拆分 + 总览重排 ──

def test_sidebar_extracted_module():
    """侧栏物理外移后：模块内容原样进入拼接结果，巨石文件里不再有第二份。"""
    from src.host.dashboard_parts.sidebar import SIDEBAR_HTML

    assert SIDEBAR_HTML.startswith('<aside class="sidebar">')
    assert SIDEBAR_HTML.rstrip().endswith("</aside>")
    assert SIDEBAR_HTML in DASHBOARD_HTML
    dashboard_src = (_ROOT / "src" / "host" / "dashboard.py").read_text(encoding="utf-8")
    assert "<aside" not in dashboard_src, "dashboard.py 不应再内嵌侧栏"
    assert DASHBOARD_HTML.count('<aside class="sidebar">') == 1


def test_nav_sections_have_persist_keys():
    from src.host.dashboard_parts.sidebar import SIDEBAR_HTML

    secs = set(re.findall(r'data-sec="([^"]+)"', SIDEBAR_HTML))
    assert secs == {"leads", "acquire", "devices", "auto", "data", "system"}
    for key in ("sec-leads", "sec-acquire", "sec-devices", "sec-auto", "sec-data", "sec-system"):
        assert f'data-i18n="{key}"' in SIDEBAR_HTML, f"节头缺 i18n 包裹: {key}"
        assert _CORE.count(f"'{key}'") >= 2, f"core.js 词典缺 {key}（zh+en 各一）"
    assert "oc_nav_collapsed" in _CORE, "折叠持久化丢失"


def test_overview_no_hardcoded_campaign_and_clickable_stats():
    assert "target_country:'italy'" not in DASHBOARD_HTML, "写死的意大利关注参数应已移除"
    for sid in ("today-watched", "today-followed", "today-dms",
                "today-autoreplied", "today-leads", "today-converts"):
        assert f'id="{sid}"' in DASHBOARD_HTML, f"今日成效丢了 {sid}（overview.js 加载器契约）"
    m = re.search(r'id="today-leads".*?</div>', DASHBOARD_HTML)
    assert m is not None
    leads_card_start = DASHBOARD_HTML.rfind("<div", 0, DASHBOARD_HTML.find('id="today-leads"'))
    leads_card = DASHBOARD_HTML[leads_card_start - 200: DASHBOARD_HTML.find('id="today-leads"')]
    assert "navigateToPage('funnel')" in leads_card, "新线索卡应直达转化漏斗"


def test_overview_block_order():
    """P2 三区顺序：①事故横幅置顶 ②一键操作 ③简报+今日成效；集群探针留在折叠区图表之后。"""
    banner = DASHBOARD_HTML.find('id="ov-escalation-banner"')
    actions = DASHBOARD_HTML.find("一键操作</h3>")  # 真标题；裸词会命中注释里的提法
    briefing = DASHBOARD_HTML.find('id="ov-daily-briefing"')
    more = DASHBOARD_HTML.find('id="ov-more-body"')
    probe = DASHBOARD_HTML.find("集群探针")
    battery_chart = DASHBOARD_HTML.find('id="chart-battery-bar"')
    assert 0 < banner < actions < briefing, "三区顺序应为 横幅→一键操作→简报"
    assert 0 < briefing < more, "运营洞察面板应收进「更多面板」折叠区"
    assert probe > battery_chart > 0, "集群探针应在图表区之后"


def test_boot_lang_apply_and_sw_cache_bumped():
    assert "开页即应用持久化语言" in _CORE
    pwa_src = (_ROOT / "src" / "host" / "routers" / "pwa.py").read_text(encoding="utf-8")
    assert "reachx-v6-rebrand" not in pwa_src
    assert "reachx-v11-p5" in pwa_src  # SW 缓存版本随发布波次 bump（陈旧混合缓存实锤教训）
    assert "core.js?v=20260815p5" in DASHBOARD_HTML  # P5 资产版本
