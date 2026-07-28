# -*- coding: utf-8 -*-
"""P17 坐席「工作目标」UI 改版门禁——守住可发现性/交互/视觉/埋点的源码不变量。

后端 agenda/undo/won_meta 已有专测；本文件守**坐席 UI 层**不回退：
- 组件不再 403=整卡消失（可配置灰字提示）
- 埋点走既有 ui-event 通道
- 宿主英雄行 / tab accent / agenda chips / 深链 / 驱动草稿接线存在
- 图标走 data-cp-ic=target（非 emoji）
"""

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GOAL_JS = _REPO / "shared" / "copilot" / "components" / "cp-goal.js"
_INBOX = _REPO / "src" / "web" / "templates" / "unified_inbox.html"
_CSS = _REPO / "src" / "web" / "static" / "workspace" / "unified-inbox.css"
_CHROME = _REPO / "shared" / "copilot" / "sidebar-chrome.js"


@pytest.fixture(scope="module")
def goal_js() -> str:
    return _GOAL_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def inbox_html() -> str:
    return _INBOX.read_text(encoding="utf-8")


def test_goal_component_disabled_hint_not_blind_hide(goal_js: str):
    assert "_showDisabledHint" in goal_js
    assert "inbox.goal.disabled_hint" in goal_js
    assert "console.debug" in goal_js
    # 默认仍可隐藏，但开启 hint 时出灰字
    assert "__forbidden" in goal_js


def test_goal_component_ui_funnel_uses_existing_beacon(goal_js: str):
    assert "/api/telemetry/ui-event" in goal_js
    for action in ("goal_card_expose", "goal_set_click", "goal_feedback_adopt",
                   "goal_feedback_reject", "goal_drive_draft"):
        assert action in goal_js, f"missing funnel action {action}"


def test_goal_component_interaction_upgrades(goal_js: str):
    assert "cp_goal_form_prefs_v1" in goal_js          # C1 偏好记忆
    assert "beat_undo" in goal_js                      # C2 撤销
    assert "cp-goal-drive-draft" in goal_js            # C3 驱动草稿
    assert "won_meta" in goal_js or "won_product" in goal_js  # C4 归因
    assert "celebrate" in goal_js                      # C5 里程碑庆祝
    assert "keydown" in goal_js and "data-prof-key" in goal_js  # C6 回车提交


def test_goal_component_visual_upgrades(goal_js: str):
    assert "gl-ms-cur" in goal_js                      # D2 当前里程碑常显
    assert "gl-sec" in goal_js and "direct" in goal_js  # D3/D5 分区+直说色
    assert "more_toggle" in goal_js                    # D4 放弃进溢出菜单
    assert "_emptyIllust" in goal_js                   # D6 空态插画
    assert "gl-badge.muted" in goal_js or "muted" in goal_js  # D7 终态降压


def test_inbox_host_discoverability(inbox_html: str):
    assert 'data-cp-ic="target"' in inbox_html
    assert 'id="cp-hero-goal"' in inbox_html
    assert "goal-agenda-filters" in inbox_html
    assert "_renderHeroGoal" in inbox_html
    assert "_updateCustomerTabBadge" in inbox_html
    assert "accent" in inbox_html
    assert "card" in inbox_html and "_handleGoalDeepLink" in inbox_html
    assert "cp-goal-drive-draft" in inbox_html
    assert "cp-goal.js?v=20260728" in inbox_html


def test_target_icon_registered():
    chrome = _CHROME.read_text(encoding="utf-8")
    assert "target:" in chrome
    inbox = _INBOX.read_text(encoding="utf-8")
    assert "target:" in inbox


def test_goal_css_tokens_present():
    css = _CSS.read_text(encoding="utf-8")
    for sel in (".cp-hero-goal", ".goal-agenda-filters", ".goal-af-chip",
                ".conv-goal-mark", ".ws-cp-tab-badge.accent"):
        assert sel in css, f"missing CSS {sel}"


# ── Phase 2（同日晚）：窄屏入口 / 自动展开 / 驱动草稿防覆盖 / 深链时序 ──


def test_goal_quick_entry_narrow_screen(inbox_html: str):
    """U6：右栏收起时顶栏「工作目标」快捷芯片——按 featureOn+会话选中 控显，走内联 onclick 须挂 window。"""
    assert 'id="goal-quick-btn"' in inbox_html
    assert "_openGoalQuick" in inbox_html
    assert "_syncGoalQuickBtn" in inbox_html
    # 内联 onclick → 必须在全局暴露块登记（完整可达性由哑按钮门禁复核，这里钉暴露行存在）
    assert "\n  _openGoalQuick," in inbox_html


def test_goal_auto_expose_on_customer_tab(inbox_html: str):
    """待反馈时切「客户&关系」tab 自动展开目标卡——每会话仅一次（防打扰）。"""
    assert "_maybeAutoExposeGoal" in inbox_html
    assert "_goalAutoExposed" in inbox_html
    assert "goal_auto_expand" in inbox_html


def test_goal_drive_draft_no_clobber(inbox_html: str):
    """驱动草稿：坐席已手打内容时不覆盖 composer（意图仅在空 composer 时预填）。"""
    assert "!ta.value.trim()" in inbox_html


def test_goal_deep_link_defers_when_conv_param(inbox_html: str):
    """?conv=&card=goal 同行时不许先弹「请先选择会话」——消费延后到会话打开。"""
    assert "hasConv" in inbox_html
    assert "if(!hasConv) _tryConsumeGoalDeepLink()" in inbox_html
