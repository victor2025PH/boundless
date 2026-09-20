# -*- coding: utf-8 -*-
"""#230 会话列表多选模式专用工具条（M-4 C，2026-09-06 证据包 4A4FAQ）。

事故：进入多选后标题栏右侧「✕ 退出多选」按钮宽度不足，文字折成三行（✕ 退/出多/选）不可辨——
根因是 toggleBatchMode 把 26px 的 icon-only 图标按钮 textContent 换成了整句文字，而标题行已被
「LINE 对话 · 会话/联系人 · ● · ☑」占满。修法：图标按钮只切 active/title；进入多选后标题行整体
切换为专用工具条「已选 N 项 ｜ 归档 · 标签 · 删除 ｜ ✕」（替换会话/联系人分段与值守胶囊），
按钮 nowrap + flex:none，最窄侧栏不折行；批量删除与右键菜单同门（can_delete_conv）同端点。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HTML = (REPO / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
CSS = (REPO / "src" / "web" / "static" / "workspace" / "unified-inbox.css").read_text(encoding="utf-8")


def _block(src: str, a: str, b: str) -> str:
    i = src.index(a)
    return src[i:src.index(b, i)]


def test_toggle_no_longer_replaces_icon_with_text():
    fn = _block(HTML, "function toggleBatchMode(){", "function _updateBabCount(){")
    assert "btn.textContent" not in fn, "icon-only 按钮不得再被换成整句文字（#230 折三行根因）"
    assert "btn.classList.toggle('active', _batchMode)" in fn
    assert "'inbox.batch.exit_t' : 'inbox.batch.toggle_t'" in fn
    assert "lt.classList.toggle('batch-on', _batchMode)" in fn
    assert "lbb.hidden = !_batchMode" in fn
    # 删除按钮与右键菜单同门：服务端 msg-ops meta 允许才出现
    assert "_msgOpsMeta && _msgOpsMeta.can_delete_conv" in fn


def test_toolbar_markup_in_title_row():
    title = _block(HTML, '<div class="list-title">', '<div class="search-row">')
    bar = _block(title, 'id="list-batch-bar"', "</div>")
    assert 'role="toolbar"' in bar and " hidden" in bar
    assert 'id="lbb-count"' in bar
    for bid, fn in (("lbb-archive", "batchArchive(true)"), ("lbb-tag", "batchTagPrompt()"),
                    ("lbb-delete", "batchDeletePrompt()")):
        m = re.search(r'<button[^>]*id="%s"[^>]*>' % bid, bar)
        assert m and fn in m.group(0), bid
        assert "disabled" in m.group(0) and 'data-i18n-title="inbox.batch.' in m.group(0), bid
    x = re.search(r'<button[^>]*class="lbb-x"[^>]*>', bar)
    assert x and 'onclick="toggleBatchMode()"' in x.group(0) and 'data-i18n-title="inbox.batch.exit_t"' in x.group(0)
    # 多选图标按钮仍是 icon-only 且不带文字节点
    tb = re.search(r'<button class="batch-toggle icon-only" id="batch-toggle-btn"[^>]*>(.*?)</button>', title, re.S)
    assert tb and "<svg" in tb.group(1) and not re.search(r">[^<]*[\u4e00-\u9fff]+[^<]*<", tb.group(1))


def test_count_and_disabled_state_follow_selection():
    fn = _block(HTML, "function _updateBabCount(){", "/* #230 批量删除会话")
    assert "document.getElementById('lbb-count')" in fn
    assert "['lbb-archive','lbb-tag','lbb-delete']" in fn and "b.disabled = !(n > 0)" in fn


def test_batch_delete_uses_same_endpoint_and_confirm_as_row_menu():
    fn = _block(HTML, "async function batchDeletePrompt(){", "/* ===== Phase 21")
    assert "/api/unified-inbox/conversations/delete" in fn
    assert "_appConfirm(window.Tf('inbox.batch.del_confirm'" in fn
    assert "_convVanishLocal(convKey(c))" in fn
    assert "toggleBatchMode();" in fn
    assert "inbox.batch.deleted_part" in fn and "inbox.batch.deleted_n" in fn


def test_css_swaps_title_row_for_toolbar_and_keeps_buttons_nowrap():
    assert ".list-title.batch-on #list-title-main,.list-title.batch-on .list-pane-seg,.list-title.batch-on .standby-wrap,.list-title.batch-on .batch-toggle{display:none;}" in CSS
    assert ".list-title.batch-on .list-batch-bar{display:flex;}" in CSS
    btn = re.search(r"\.list-batch-bar \.lbb-btn\{([^}]*)\}", CSS).group(1)
    assert "white-space:nowrap" in btn and "flex:none" in btn
    assert re.search(r"\.list-batch-bar \.lbb-x\{[^}]*margin-left:auto", CSS)
    # 样式改动随缓存戳上线（模板 ?v= 与 CSS 同批）
    v = re.search(r'unified-inbox\.css\?v=(\w+)', HTML).group(1)
    assert v >= "20260906f", v


def test_i18n_keys_bilingual():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    for k in ("inbox.batch.exit_t", "inbox.batch.archive_t", "inbox.batch.tag_t", "inbox.batch.del_t",
              "inbox.batch.del_confirm", "inbox.batch.deleted_n", "inbox.batch.deleted_part",
              "inbox.bab.count", "inbox.convops.tags", "inbox.convdel.btn"):
        assert ZH.get(k) and EN.get(k), k
    assert "{n}" in ZH["inbox.batch.del_confirm"] and "{n}" in EN["inbox.batch.del_confirm"]
