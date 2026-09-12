# -*- coding: utf-8 -*-
"""会话级模型路由（conv_route）composer「模型 ▾」静态骨架门禁（2026-09-12）。

钉住的不是样式而是**接线**：入口在键盘图标之前、弹层复用 rt-pop 家族并进外点关闭白名单、
selectChat 切会话即 hydrate、所有 inline onclick 函数挂在 window、CSS 零裸 Tailwind 蓝、
i18n 键 zh/en 双全且不以拼接键调用 window.T（覆盖门禁抓不到拼接键）。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates" / "unified_inbox.html"
CSS = ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css"
PACK = ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"


def _html() -> str:
    return TPL.read_text(encoding="utf-8")


def test_model_btn_sits_right_before_keyboard_icon():
    html = _html()
    i_model = html.index('id="model-btn"')
    i_kbd = html.index('id="kbd-help-btn"')
    i_xl = html.index('id="xlate-toggle-btn"')
    assert i_xl < i_model < i_kbd, "「模型 ▾」必须在翻译组之后、键盘图标之前"
    between = html[i_model:i_kbd]
    # 中间只允许有模型弹层自己的骨架，不许再插别的工具按钮
    assert 'id="model-pop"' in between
    assert between.count('class="tiny-btn" id=') == 0 or 'id="mp-health-retry"' in between


def test_model_pop_is_rt_pop_family_and_in_outside_click_whitelist():
    html = _html()
    assert re.search(r'id="model-pop"[^>]*', html)
    assert 'class="rt-pop rt-pop-right" id="model-pop"' in html
    # 外点关闭白名单：按钮与状态胶囊都得放行，否则点开即被同一 click 关掉
    m = re.search(r"if\(e\.target\.closest\('\.rt-pop'\)[^\n]*\)\s*return;\n\s*_closeRtPops\(\);", html)
    assert m, "找不到 rt-pop 外点关闭处理器"
    assert "#model-btn" in m.group(0) and "#rt-model-status" in m.group(0)


def test_select_chat_hydrates_conv_model_route():
    html = _html()
    i_sel = html.index("function selectChat(key){")
    i_hyd = html.index("_hydrateConvModelRoute(c)", i_sel)
    i_next = html.index("\nfunction ", i_sel + 10)
    assert i_hyd < i_next, "selectChat 内必须调用 _hydrateConvModelRoute(c)"


def test_inline_onclick_handlers_are_on_window():
    html = _html()
    for fn in ("toggleModelPop", "_mpPickOpen", "_mpToggleThinking", "_mpToggleSafety",
               "_mpHealth", "_mpBackToStandard", "_mpPickBack", "_hydrateConvModelRoute"):
        assert re.search(rf"window\.{re.escape(fn)}\s*=", html), f"{fn} 未挂 window（inline onclick 会 ReferenceError）"


def test_api_contract_paths_match_backend_routes():
    html = _html()
    assert "/api/unified-inbox/conv-model-route" in html
    assert "/api/ai/model-route/health" in html
    from src.web.routes import conv_model_route_routes as m
    src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    assert "/api/unified-inbox/conv-model-route" in src and "/api/ai/model-route/health" in src
    # 403 分型头：前端按 X-Deny-Reason 区分「运营关闭」与「授权不足」
    assert "X-Deny-Reason" in src and "X-Deny-Reason" in html


def test_no_concatenated_i18n_keys_in_mp_block():
    """覆盖门禁按字面量抓 window.T('...') 键；拼接键 'inbox.mp.depth_'+k 会被抓成半截键并判缺翻译。"""
    html = _html()
    assert not re.search(r"window\.T\('inbox\.mp\.[a-z_]*'\s*\+", html)
    assert not re.search(r"window\.Tf\('inbox\.mp\.[a-z_]*'\s*\+", html)


def test_mp_i18n_keys_present_in_both_langs():
    import importlib
    pack = importlib.import_module("src.web.i18n_packs.inbox_workspace")
    zh = {k for k in pack.ZH if k.startswith("inbox.mp.")}
    en = {k for k in pack.EN if k.startswith("inbox.mp.")}
    assert zh and zh == en, f"zh/en 键不对称: zh-en={sorted(zh - en)} en-zh={sorted(en - zh)}"
    html = _html()
    used = set(re.findall(r"['\"](inbox\.mp\.[a-z0-9_]+)['\"]", html))
    missing = used - zh
    assert not missing, f"模板用到但词典没有: {sorted(missing)}"


def test_mp_confirms_use_app_confirm_not_native():
    """桌面壳里原生 confirm 标题是 telegram-ai-desktop，必须走站内 _appConfirm。"""
    html = _html()
    i = html.index("async function _mpSet(patch)")
    j = html.index("window._mpSet=_mpSet", i)
    block = html[i:j]
    assert "_appConfirm(" in block
    assert not re.search(r"(?<![_\w])confirm\(", block)
    assert "inbox.mp.confirm_unr_hd" in block and "tone:'vio'" in block
    assert "inbox.mp.confirm_safety_hd" in block and "tone:'danger'" in block
    css = CSS.read_text(encoding="utf-8")
    assert ".app-confirm-btn.vio" in css and "--tk-violet" in css


def test_css_uses_tokens_only_no_tailwind_blue_hex():
    css = CSS.read_text(encoding="utf-8")
    i = css.index("会话级模型路由（conv_route，2026-09-12）")
    j = css.index("弹层内双向翻译条改为竖向铺开", i)
    block = css[i:j]
    assert "--tk-violet" in block and "--tk-vio-ink" in block
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), "mp- 段不许裸 hex（走 --tk-* / --xl-* 令牌）"
    assert ".rt-model-status.vio" in block and "#model-pop.is-pick #mp-home{display:none;}" in block
