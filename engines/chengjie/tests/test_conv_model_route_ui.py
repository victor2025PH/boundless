# -*- coding: utf-8 -*-
"""会话级模型路由（conv_route）composer「模型」「模式」双面板静态骨架门禁（2026-09-12）。

钉住的不是样式而是**接线**：两个等尺寸入口在键盘图标之前、两个弹层复用 rt-pop 家族并进外点关闭白名单、
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
    assert "#model-btn" in m.group(0) and "#mode-btn" in m.group(0)
    assert 'class="rt-pop rt-pop-right" id="mode-pop"' in html


def test_select_chat_hydrates_conv_model_route():
    html = _html()
    i_sel = html.index("function selectChat(key){")
    i_hyd = html.index("_hydrateConvModelRoute(c)", i_sel)
    i_next = html.index("\nfunction ", i_sel + 10)
    assert i_hyd < i_next, "selectChat 内必须调用 _hydrateConvModelRoute(c)"


def test_inline_onclick_handlers_are_on_window():
    html = _html()
    for fn in ("toggleModelPop", "toggleModePop", "_mpPickOpen", "_mpToggleThinking", "_mpToggleSafety",
               "_mpToggleConsistency",
               "_mpHealth", "_mpBackToStandard", "_mpPickBack", "_hydrateConvModelRoute"):
        assert re.search(rf"window\.{re.escape(fn)}\s*=", html), f"{fn} 未挂 window（inline onclick 会 ReferenceError）"


def test_api_contract_paths_match_backend_routes():
    html = _html()
    assert "/api/unified-inbox/conv-model-route" in html
    assert "/api/ai/model-route/health" in html
    assert "/api/ai/model-route/stats" in html
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
    assert "#mode-btn.vio" in block and "#mode-pop.is-pick #mp-home{display:none;}" in block
    assert ".cmp-sel{" in block and ".mv-row{" in block


# ── 2026-09-12 拆双面板：模型（用谁答）/ 模式（按什么规矩答）──────────────────────

def test_two_equal_entries_and_two_panels():
    """两个入口同一个组件类（.cmp-sel，像素级等尺寸），各自 aria-controls 自己的面板；
    独立状态胶囊已退役（值合进按钮本身）。"""
    html = _html()
    assert 'class="cmp-sel" id="model-btn"' in html and 'class="cmp-sel" id="mode-btn"' in html
    assert 'aria-controls="model-pop"' in html and 'aria-controls="mode-pop"' in html
    assert 'id="rt-model-status"' not in html, "旧状态胶囊必须移除（两个入口一个面板正是被投诉的点）"
    for i in ("model-btn-val", "mode-btn-val", "model-btn-dot", "mv-list", "mv-notice", "mv-dataflow", "mv-manage", "mv-last"):
        assert f'id="{i}"' in html, i
    # 模式面板保留原五行 + 分组线；主行文案改「模式」
    assert 'data-i18n="inbox.mp.row_model">模式</span>' in html
    assert html.count('class="mp-group"') == 2
    css = CSS.read_text(encoding="utf-8")
    m = re.search(r"\.cmp-sel\{[^}]*height:26px[^}]*\}", css)
    assert m, ".cmp-sel 必须定高（两个按钮一样大靠它）"
    assert "#model-btn" not in css or "#model-btn.vio" not in css, "紫色只染「模式」按钮，模型按钮保持中性"


def test_model_panel_wiring_uses_catalog_and_all_health():
    html = _html()
    i = html.index("function _mvRender(")
    j = html.index("function _mvRenderLast(", i)
    block = html[i:j]
    assert "choices" in html[html.index("function _mpModels("):html.index("function _mpModelRow(")]
    assert "_mpSet({model:v})" in block
    assert "_mpSet({profile:'unrestricted'})" in block
    assert "opens_unrestricted" in block
    assert "inbox.mp.tag_unrestricted" in block
    assert 'href="/model-keys"' in html
    assert "/developer#dvmr" not in html
    assert "inbox.mp.unr_notice" in block and "inbox.mp.model_missing" in block and "inbox.mp.empty" in block
    assert "/api/ai/model-route/health?all=1" in html
    assert "/api/ai/model-route/health?profile=" in html
    assert "/api/ai/prompt-inspect?conv=" in html
    assert "/api/ai/model-route/stats" in html
    assert "function _mvLoadUsage(" in html and "function _mvUsageBits(" in html
    assert "inbox.mp.tag_seats" in html and "inbox.mp.tag_fail" in html
    assert "_mvUsageBits(m)" in block
    # 400 unknown_model → 重拉目录而不是装成功
    k = html.index("async function _mpSet(patch)")
    setblk = html[k:html.index("window._mpSet=_mpSet", k)]
    assert "unknown_model" in setblk
    assert "_mvPath(" in html and "inbox.mp.tag_hosted" in block and "inbox.mp.flow_hosted" in block
    assert "m.model+' @ '+m.host" not in block


def test_model_panel_vendor_lock_counts_and_not_listed_warn():
    """P3 授权闸：vendor_allowed=false 时非主链行锁住（不发请求、给升级提示）；
    列表带「本会话 N 条」；GET /models 在线但模型名不在列表 → 琥珀点。"""
    html = _html()
    i = html.index("function _mvRender(")
    j = html.index("function _mvRenderLast(", i)
    block = html[i:j]
    assert "vendor_allowed===false" in block and "data-locked" in block
    assert "inbox.mp.tag_locked" in block and "inbox.mp.vendor_locked" in block
    assert "inbox.mp.tag_used" in block and "model_not_listed" in block and "inbox.mp.h_not_listed" in block
    assert "cmpz_vendor_locked" in block
    # 服务端 403 vendor_locked 也走同一文案 + 重画锁态
    k = html.index("async function _mpSet(patch)")
    setblk = html[k:html.index("window._mpSet=_mpSet", k)]
    assert "vendor_locked" in setblk and "inbox.mp.vendor_locked" in setblk
    # 「上一条回复」改拉 50 条并按 model@host 计数
    m = html.index("async function _mvLoadLast(")
    lastblk = html[m:html.index("async function _mpHealth(", m)]
    assert "&limit=50" in lastblk and "_mp.counts=" in lastblk
    css = CSS.read_text(encoding="utf-8")
    for sel in (".mv-row.locked{", ".mv-tag.lock{", ".mv-tag.cnt{", ".mv-tag.seats{", ".mv-tag.fail{", ".mv-dot.warn{"):
        assert sel in css, sel
    import importlib
    pack = importlib.import_module("src.web.i18n_packs.inbox_workspace")
    for key in ("inbox.mp.tag_locked", "inbox.mp.tag_used", "inbox.mp.tag_seats", "inbox.mp.tag_fail",
                "inbox.mp.h_not_listed", "inbox.mp.vendor_locked",
                "inbox.mp.tag_unrestricted", "inbox.mp.vendor_local"):
        assert pack.ZH[key] and pack.EN[key], key
    assert "27B" not in pack.ZH["inbox.mp.vendor_local"]
    assert "办公室" not in pack.ZH["inbox.mp.tag_private"]
    assert "ChatX 27B" not in html and "办公室直连" not in html


def test_md_i18n_keys_present_in_both_langs():
    import importlib
    pack = importlib.import_module("src.web.i18n_packs.inbox_workspace")
    zh = {k for k in pack.ZH if k.startswith("inbox.md.")}
    en = {k for k in pack.EN if k.startswith("inbox.md.")}
    assert zh and zh == en
    html = _html()
    used = set(re.findall(r"['\"](inbox\.md\.[a-z0-9_]+)['\"]", html))
    assert used and not (used - zh)
    # 拆面板后文案不再自称「模型 ▾」；zh_hant 生成物里对应键已同步（否则繁体坐席看到旧字）
    assert pack.ZH["inbox.mp.btn"] == "模型" and pack.ZH["inbox.mp.row_model"] == "模式"
    hant = importlib.import_module("src.web.i18n_packs.zh_hant_auto")
    hv = getattr(hant, "ZH_HANT", {}) or {}
    assert hv.get("inbox.mp.btn") == "模型" and hv.get("inbox.mp.row_model") == "模式"
    assert "inbox.mp.status_t" not in pack.ZH and "inbox.mp.status_t" not in hv
