# -*- coding: utf-8 -*-
"""P2-198「直出模式 + 诊断包」静态接线门禁（2026-07-31）。

直出模式：正文按客户语言直出 + 坐席 UI 语言对照（gloss，只读）——替换掉
「LLM 生成英文 → 语言守卫改写成中文 → 出站再翻回英文」的三次 LLM 中转链
（198 日志实测：90 分钟 40+ 次守卫改写、每回复 9 秒）。
诊断包：/api/admin/diagnostic-bundle（198 排障「日志几乎为零」教训的产品化）。
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


# ── 直出模式（gloss）────────────────────────────────────────────────────────

def test_persona_reply_attaches_gloss_on_both_paths():
    src = (REPO / "src" / "inbox" / "persona_reply.py").read_text(encoding="utf-8")
    assert "async def _attach_gloss(" in src
    assert src.count("_attach_gloss(app, out, reply, resolved_lang, gloss_lang)") == 2


def test_smart_reply_route_passes_gloss_lang_to_both_modes():
    src = (REPO / "src" / "web" / "routes" / "unified_inbox_desktop_routes.py"
           ).read_text(encoding="utf-8")
    assert src.count("gloss_lang=gloss_lang") == 2   # opener + reply 两条产线


def test_cp_draft_sends_ui_lang_and_renders_readonly_gloss():
    for p, js in _both("components/cp-draft.js"):
        assert "payload.gloss_lang" in js, p
        assert 'class="tr gloss"' in js, p
        assert "cp.draft.gloss" in js, p
        # 对照区块绝不能有「填入」按钮——把对照填进输入框发出去=P0 修掉的中文泄漏
        gseg = js.split('class="tr gloss"', 1)[1].split("`", 1)[0]
        assert "data-act" not in gseg, f"{p} 对照区块不得带可点按钮"


def test_cp_i18n_gloss_key_bilingual_in_both_trees():
    for p, js in _both("i18n/cp-i18n.js"):
        assert js.count('"cp.draft.gloss"') >= 2, p


# ── 诊断包 ─────────────────────────────────────────────────────────────────

def test_diagnostic_bundle_route_wired():
    src = (REPO / "src" / "web" / "routes" / "ops_overview_routes.py"
           ).read_text(encoding="utf-8")
    assert '"/api/admin/diagnostic-bundle"' in src
    assert "build_diagnostic_bundle" in src
    assert "probe" in src            # settings 卡片探活口（旧后端 404 → 整卡隐藏）


def test_diagnostic_route_registered_in_inventory():
    inv = (REPO / "tests" / "test_admin_route_inventory.py").read_text(encoding="utf-8")
    assert "/api/admin/diagnostic-bundle\tGET" in inv


def test_settings_page_diag_card_self_consistent():
    tpl = (REPO / "src" / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
    assert 'id="diag-card" style="display:none"' in tpl     # 默认隐藏
    assert "diagnostic-bundle?probe=1" in tpl               # 探活后才显示
    assert "diag_btn" in tpl and "diag_hint" in tpl


def test_diag_i18n_keys_bilingual():
    pack = (REPO / "src" / "web" / "i18n_packs" / "feature_center.py"
            ).read_text(encoding="utf-8")
    for k in ("diag_title", "diag_sub", "diag_btn", "diag_hint"):
        assert pack.count(f'"{k}"') == 2, k                 # ZH + EN 各一次
