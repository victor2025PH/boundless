# -*- coding: utf-8 -*-
"""P17 B3/E1 ops 今日清单抽屉 + UI 漏斗基线——ops_overview 源码不变量门禁。

后端 agenda（含 ?names=1 展示名信封）已有专测；本文件守 **ops 看板 UI 层**不回退：
- 「今日清单」按钮/抽屉容器存在，抽屉懒加载 `/api/goals/agenda?scope=today&names=1`
- names 缺席（旧进程）优雅回落 platform:chat_key / cid 前缀，绝不空白
- agenda 403（功能未开）→ 静默隐藏按钮（不弹错）
- 行深链 /workspace?conv=<cid>&card=goal 新标签打开（noopener）
- E1 基线：UI 漏斗行复用 loadGoals 已取的 metrics 响应（ui_events.by_action 的
  goal_ 前缀动作 Top8 chips），零数据显 —
- 全部新 ov2_goal_ag_* 键 zh/en 双语齐备（i18n pack 单源）
"""

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_OPS = _REPO / "src" / "web" / "templates" / "ops_overview.html"

# 抽屉/漏斗全部新静态键（window.T 与 Jinja get 消费；测试与实现同步维护）
_AG_KEYS = (
    "ov2_goal_ag_btn",
    "ov2_goal_ag_col_fb",
    "ov2_goal_ag_col_intent",
    "ov2_goal_ag_col_peer",
    "ov2_goal_ag_col_push",
    "ov2_goal_ag_empty",
    "ov2_goal_ag_fb_adopted",
    "ov2_goal_ag_fb_pending",
    "ov2_goal_ag_fb_rejected",
    "ov2_goal_ag_funnel",
    "ov2_goal_ag_hold",
    "ov2_goal_ag_pending",
    "ov2_goal_ag_push",
)


@pytest.fixture(scope="module")
def ops_html() -> str:
    return _OPS.read_text(encoding="utf-8")


def test_agenda_drawer_markers_present(ops_html: str):
    """抽屉三件套：按钮 + 容器 + 全局可达的 toggle/加载函数（inline onclick 消费）。"""
    assert 'id="goalAgendaBtn"' in ops_html
    assert 'id="goalAgendaWrap"' in ops_html
    assert 'onclick="toggleGoalAgenda()"' in ops_html
    assert "function toggleGoalAgenda()" in ops_html
    assert "async function _loadGoalAgenda(" in ops_html


def test_agenda_lazy_fetch_with_names(ops_html: str):
    """懒加载端点带 names=1（展示名信封 opt-in）；60s refreshAll 不携带 agenda 拉取。"""
    assert "/api/goals/agenda?scope=today&names=1&limit=100" in ops_html
    # 懒加载：agenda 只在 _loadGoalAgenda 内拉，不进 refreshAll 周期函数清单
    refresh_line = next(
        ln for ln in ops_html.splitlines() if "function refreshAll()" in ln)
    assert "_loadGoalAgenda" not in refresh_line
    assert "toggleGoalAgenda" not in refresh_line


def test_agenda_peer_label_degrades_without_names(ops_html: str):
    """生产旧进程可能还没 names 信封：names[cid] 缺席 → platform:chat_key / cid 前缀。"""
    assert "names[cid]" in ops_html
    assert "it.chat_key || cid.slice(0, 10)" in ops_html


def test_agenda_403_hides_affordance_silently(ops_html: str):
    """agenda 403（功能未开）→ 藏按钮 + 记 _goalAgendaOff，loadGoals 轮询不再复亮。"""
    body = ops_html[ops_html.index("async function _loadGoalAgenda"):]
    head = body[:1200]
    assert "403" in head
    assert "_goalAgendaOff = true" in head
    # loadGoals 侧亮灯受同一开关拦截
    assert "!_goalAgendaOff" in ops_html


def test_agenda_deep_link_new_tab(ops_html: str):
    """行深链 /workspace?conv=<cid>&card=goal，新标签 + noopener；动态文本全走转义。"""
    assert "'/workspace?conv='+encodeURIComponent(cid)+'&card=goal'" in ops_html
    seg = ops_html[ops_html.index("async function _loadGoalAgenda"):]
    assert 'target="_blank" rel="noopener"' in seg
    # 用户可控串（展示名/hold 原因/标题/平台）经 _esc 转义
    for marker in ("_esc(peer)", "_esc(hold)", "_esc(it.title||'')"):
        assert marker in seg, f"missing escaped render {marker}"


def test_agenda_counts_and_empty_state(ops_html: str):
    """计数摘要行（with_push/pending_feedback/hold）+ 空清单友好文案。"""
    seg = ops_html[ops_html.index("async function _loadGoalAgenda"):]
    for marker in ("c.with_push", "c.pending_feedback", "c.hold",
                   "ov2_goal_ag_empty"):
        assert marker in seg, f"missing counts/empty marker {marker}"


def test_funnel_baseline_line_reuses_metrics(ops_html: str):
    """E1：UI 漏斗行在 loadGoals 内读同一份 metrics 响应（不二次 fetch），
    只取 goal_ 前缀动作 Top8；标记键 ov2_goal_ag_funnel。"""
    assert "ov2_goal_ag_funnel" in ops_html
    lg = ops_html[ops_html.index("async function loadGoals()"):]
    lg = lg[:lg.index("\n}\n")]  # 只看 loadGoals 函数体
    assert "d.ui_events && d.ui_events.by_action" in lg
    assert "indexOf('goal_') === 0" in lg
    assert ".slice(0, 8)" in lg
    # 复用响应对象 d，不再单独 fetch metrics（函数内仅一次 metrics 拉取）
    assert lg.count("/api/workspace/metrics") == 1


def test_all_agenda_keys_bilingual():
    """每个新 ov2_goal_ag_* 键 zh/en 双语齐备（pack 单源；缺一门禁即红）。"""
    from src.web.i18n_packs.ops_overview_page import EN, ZH

    missing_zh = [k for k in _AG_KEYS if k not in ZH]
    missing_en = [k for k in _AG_KEYS if k not in EN]
    assert not missing_zh, f"ZH pack missing: {missing_zh}"
    assert not missing_en, f"EN pack missing: {missing_en}"
    # 值非空（占位空串会让 window.T 回落裸键名）
    empty = [k for k in _AG_KEYS if not str(ZH[k]).strip() or not str(EN[k]).strip()]
    assert not empty, f"empty translations: {empty}"


def test_agenda_keys_actually_consumed(ops_html: str):
    """防「键在册但没人用」：所有 _AG_KEYS 在模板里被 window.T / Jinja get 引用。"""
    unused = [k for k in _AG_KEYS if k not in ops_html]
    assert not unused, f"keys defined but not referenced in template: {unused}"
