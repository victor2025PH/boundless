# -*- coding: utf-8 -*-
"""P1-198 副驾体验收口的静态接线门禁（2026-07-31）。

覆盖四项客户实测反馈的接线层（行为各有单测，这里守「写了函数没挂线」）：
  ① 工坊「开启新话题」：路由 mode 分发 + cp-draft 模式按钮 + payload.mode；
  ② 情绪标记状态化：路由回 current_mood + cp-next-actions 状态行/高亮 + 互斥写入；
  ③ 回访任务诚实化：缺 contact_id 如实报错 + 可关闭 flag；
  ④ 英雄卡去重：只在客户&关系 tab 显示（_syncHeroVisibility 定义 + 三处接线）。
共享组件双树一致性由既有 gate（shared ↔ desktop/renderer/shared 同步）守，
这里对**两棵树都**断言关键锚点，防「只改一棵树」的半吊子同步。
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

_TREES = (
    REPO / "shared" / "copilot",
    REPO / "desktop" / "renderer" / "shared" / "copilot",
)


def _both(rel: str):
    for base in _TREES:
        p = base / rel
        yield p, p.read_text(encoding="utf-8")


# ── ① 工坊开新话题 ─────────────────────────────────────────────────────────

def test_smart_reply_route_dispatches_opener_mode():
    src = (REPO / "src" / "web" / "routes" / "unified_inbox_desktop_routes.py"
           ).read_text(encoding="utf-8")
    assert 'if mode == "opener":' in src
    assert "generate_topic_opener" in src


def test_cp_draft_has_opener_mode_in_both_trees():
    for p, js in _both("components/cp-draft.js"):
        assert 'data-act="mode-opener"' in js, p
        assert 'payload.mode = "opener"' in js, p
        assert "!messages.length && !opener" in js, p


def test_cp_i18n_has_new_keys_bilingual_in_both_trees():
    keys = (
        "cp.draft.mode_reply", "cp.draft.mode_opener", "cp.draft.mode_opener_t",
        "cp.nba.mood_current", "cp.nba.mood_none",
        "cp.persona.legacy_acct_btn", "cp.persona.legacy_acct_hint",
        "cp.persona.legacy_acct_pick_first",
    )
    for p, js in _both("i18n/cp-i18n.js"):
        for k in keys:
            # reg(zh, en) 双字典各出现一次 → 每键至少 2 次
            assert js.count(f'"{k}"') >= 2, f"{p} 缺双语词条 {k}"


# ── ② 情绪标记状态化 ───────────────────────────────────────────────────────

def test_next_actions_route_returns_current_mood_and_exclusive_write():
    src = (REPO / "src" / "web" / "routes" / "unified_inbox_workflow_routes.py"
           ).read_text(encoding="utf-8")
    assert '"current_mood": current_mood' in src
    assert "MOOD_TAGS" in src            # 单一事实源引用（词表别抄第二份）
    # 情绪标签互斥（P1-198 续，2026-08-02 起收口）：路由不再内联剔旧值，统一走
    # effective_mood.apply_mood_tag（组内互斥 + arbitration 列双写的唯一 IO 入口，
    # 与工作链 runner / 批量打标同源——旧断言钉的内联实现已被该收口取代）。
    assert "apply_mood_tag" in src
    assert '"mood_manual"' in src        # 转向状态随响应回给前端徽标


def test_cp_next_actions_shows_mood_state_in_both_trees():
    for p, js in _both("components/cp-next-actions.js"):
        assert "current_mood" in js, p
        assert "moodline" in js, p
        assert "cp.nba.mood_current" in js, p
        # 失败原因透传（诚实化：宿主 toast 能显示「为什么不行」）
        assert 'error: err' in js, p


# ── ③ 回访任务诚实化 ───────────────────────────────────────────────────────

def test_task_action_reports_honest_errors_and_flag():
    src = (REPO / "src" / "web" / "routes" / "unified_inbox_workflow_routes.py"
           ).read_text(encoding="utf-8")
    assert "err.ws.task_no_contact" in src
    assert "err.ws.task_contacts_disabled" in src
    assert "err.ws.task_create_failed" in src
    assert 'get("follow_up_task", True)' in src
    assert "followup_task_enabled=_followup_on" in src


def test_host_toast_prefers_backend_error():
    tpl = (REPO / "src" / "web" / "templates" / "unified_inbox.html"
           ).read_text(encoding="utf-8")
    assert "d.error?String(d.error):window.T('inbox.cp.exec_fail')" in tpl


# ── ④ 英雄卡去重 ──────────────────────────────────────────────────────────

def test_hero_card_scoped_to_customer_tab():
    tpl = (REPO / "src" / "web" / "templates" / "unified_inbox.html"
           ).read_text(encoding="utf-8")
    assert "function _syncHeroVisibility()" in tpl
    # 定义 1 处 + 接线 ≥3 处（tab ctrl onChange / restore 初始 / setWsCpTab 降级 / else 初始）
    assert tpl.count("_syncHeroVisibility()") >= 4
    assert "(_wsCpTab==='customer')?'':'none'" in tpl


# ── ⑤ 人设账号级默认（legacy 模式入口）────────────────────────────────────

def test_cp_persona_legacy_account_entry_in_both_trees():
    for p, js in _both("components/cp-persona.js"):
        assert 'data-act="legacy-acct"' in js, p
        assert "_onLegacyAccount" in js, p
        assert "cp.persona.legacy_acct_pick_first" in js, p
        # legacy body 必须带确认弹窗挂载点（账号级换绑恒弹确认，无静默路径）
        assert js.count('data-role="cfm-slot"') >= 2, p
