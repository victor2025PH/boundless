"""#192 人设工作室页头回归钉（J-7 D，2026-09-05）。

skuio（G9DACM）：① 页头带数字的控件宽度不足被截成「2…/7…」；② 「新建人设」页头右上与
工具栏最右各一个；③ 标签云约 90 个标签占满三四行。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "personas.html"
_CORE = _ROOT / "src" / "web" / "static" / "js" / "persona_studio_core.js"


def test_single_new_persona_button_in_page_header_and_toolbar():
    s = _TPL.read_text(encoding="utf-8")
    topbar = s[s.index('<div class="topbar">'):s.index("<!-- ── Tab navigation")]
    assert "openCreateFlow()" not in topbar, "页头右上不得再有「新建人设」（保留人设池工具栏那个）"
    toolbar = s[s.index('<div class="pp-header">'):s.index('id="bio-orphan-banner"')]
    assert toolbar.count("openCreateFlow()") == 1, "工具栏保留且只保留一个「新建人设」"


def test_header_numeric_widgets_do_not_shrink():
    s = _TPL.read_text(encoding="utf-8")
    assert re.search(r"\.pp-header \.sort-sel,\.pp-header \.pp-count,\.pp-header \.btn,#dash-refresh-btn[^{]*\{flex:none\}", s)
    assert re.search(r"\.fresh-badge,\.tb-cnt,\.pp-count\{white-space:nowrap\}", s)
    m = re.search(r'id="kbd-help-btn"[^>]*style="([^"]*)"', s)
    assert m and "min-width:1.75rem" in m.group(1) and "width:1.75rem;padding:0;" not in m.group(1), \
        "快捷键按钮不得再用定宽（定宽+overflow:hidden＝文字被截）"


def test_tag_cloud_collapses_to_top_n_with_more_toggle():
    js = _CORE.read_text(encoding="utf-8")
    assert "var TAG_CLOUD_TOP = 12" in js
    assert "function _toggleTagCloud()" in js
    assert "psn_tags_more" in js and "psn_tags_less" in js
    # 当前选中的长尾标签不能被折叠藏掉（否则筛选态无从取消）
    assert "_activeTag && !show.some" in js
    # 标签值走属性转义 + dataset 取值，不再拼进 onclick 引号串
    assert 'onclick="_clickTagText(this.dataset.tag)"' in js
    assert "_clickTagText(\\'" not in js
    # 静态 JS 改了要 bump 缓存戳
    tpl = _TPL.read_text(encoding="utf-8")
    assert "persona_studio_core.js?v=20260730a" not in tpl
    assert re.search(r"persona_studio_core\.js\?v=2026090[5-9]", tpl)


def test_tag_keys_bilingual():
    from src.web.i18n_packs.persona_studio import EN, ZH
    for k in ("psn_tags_more", "psn_tags_less"):
        assert k in ZH and k in EN
    assert "{n}" in ZH["psn_tags_more"] and "{n}" in EN["psn_tags_more"]
