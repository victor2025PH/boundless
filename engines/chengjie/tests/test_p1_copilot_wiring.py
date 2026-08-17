# -*- coding: utf-8 -*-
"""P1-198 副驾体验收口的静态接线门禁（2026-07-31）。

覆盖客户实测反馈的接线层（行为各有单测，这里守「写了函数没挂线」）：
  ① 工坊「开启新话题」：路由 mode 分发 + cp-draft 模式按钮 + payload.mode；
  ④ 英雄卡去重：只在客户&关系 tab 显示（_syncHeroVisibility 定义 + 三处接线）。
（原 ②③ 情绪标记状态化 / 回访任务诚实化属「AI 下一步」面板，2026-08-14 随
 面板整体下线删除；情绪词表/仲裁的行为门禁在 test_mood_steering.py。）
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
        "cp.persona.legacy_acct_btn", "cp.persona.legacy_acct_hint",
        "cp.persona.legacy_acct_pick_first",
        "cp.persona.usage_7d_t",
        "cp.persona.usage_7d_short",
    )
    for p, js in _both("i18n/cp-i18n.js"):
        for k in keys:
            # reg(zh, en) 双字典各出现一次 → 每键至少 2 次
            assert js.count(f'"{k}"') >= 2, f"{p} 缺双语词条 {k}"


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


def test_cp_persona_theme_tokens_and_polish_in_both_trees():
    """暗色选中卡不可读事故的回归钉 + 第二轮呈现层锚点。"""
    for p, js in _both("components/cp-persona.js"):
        assert "background:var(--cp-ok-bg" in js, p
        assert "background:#ecfdf5" not in js.replace("var(--cp-ok-bg,#ecfdf5)", ""), p
        assert "personaDiscColor" in js, p
        assert "personaListRank" in js, p
        assert "cp.persona.create_cta" in js, p
        assert "cp.persona.usage_7d_t" in js, p
        assert "cfm-swap" in js, p
        assert "ArrowDown" in js, p
        assert "is-ovf" in js, p
        assert "_syncPlistFade" in js, p
        assert "(cur + 1) % cards.length" in js, p
    for p, css in _both("theme-dark.css"):
        assert "--cp-ok-bg:" in css, p
        assert "--cp-warn-ink:" in css, p
    for p, css in _both("theme-light.css"):
        assert "--cp-ok-bg:" in css, p
        assert "--cp-warn-ink:" in css, p


def test_identity_bar_override_chip_wiring():
    """覆写态用独立 chip，不再把括号塞进整句（身份条扫读）。"""
    tpl = (REPO / "src" / "web" / "templates" / "unified_inbox.html"
           ).read_text(encoding="utf-8")
    assert "ib-chip" in tpl
    assert "inbox.ident.chip_conv" in tpl
    assert "ident_bar_jump" in tpl
    assert "ident_bar_jump_bind" in tpl
    assert "function _identJumpNoteConvert" in tpl
    assert "_identJumpKey" in tpl
    # 30s 转化窗＝度量语义的一部分：改值必须同步纪元注释 + tools/ident_bar_review.py
    assert "_IDENT_JUMP_BIND_MS=30000" in tpl
    assert 'class="ib-go"' in tpl
    assert "classList.toggle('ov'" in tpl
    css = (REPO / "src" / "web" / "static" / "workspace" / "unified-inbox.css"
           ).read_text(encoding="utf-8")
    assert ".ib-chip" in css
    assert ".identity-bar .ib-go" in css
    assert ".conv-persona-badge.ov::before" in css
    pack = (REPO / "src" / "web" / "i18n_packs" / "inbox_workspace.py"
            ).read_text(encoding="utf-8")
    assert pack.count('"inbox.ident.chip_conv"') >= 2
    assert pack.count('"inbox.ident.chip_conv_t"') >= 2
    for p, js in _both("i18n/cp-i18n.js"):
        assert '"仅此会话"' in js, p
        assert '"this chat only"' in js, p
        assert '"这个号默认"' in js, p
        assert '"改回这个号默认"' in js, p
        assert '"Use account default"' in js, p
