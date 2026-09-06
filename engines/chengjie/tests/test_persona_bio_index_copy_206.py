# -*- coding: utf-8 -*-
"""L-2 F（#206 P3 + #192 残 P3，2026-09-06）。

#206（2VKZKC）：档案 tab「长传记已入库」行的「补齐向量」——术语（向量 / 句向量 / 实体别名 / 检索自测）
是研发词；点击后的提示被底部按钮栏遮住且 1-2 秒消失；状态行已显「183/183」齐全时按钮仍可点。
钉住：术语改人话；索引齐全 → 按钮 disabled + 注明；结果改为按钮旁内联状态、按人设记忆、不自动消失；
Embedding 不可用给原因。

#192 残（E42974）：页头右上「孤立 7 ▾」——窄窗（≤860px）隐藏用户名后按钮只剩头像首字符 + 箭头。
钉住：用户按钮带 title / aria-label（换图标待截图确认，不猜改）。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "personas.html"
_BASE = _ROOT / "src" / "web" / "templates" / "base.html"
_I18N = _ROOT / "src" / "web" / "i18n_packs" / "persona_studio.py"
_WEB_I18N = _ROOT / "src" / "web" / "web_i18n.py"


def test_bio_index_copy_is_plain_language():
    i18n = _I18N.read_text(encoding="utf-8")
    zh = i18n[:i18n.index('"psn_bio_cov_gap": "(')]   # 简中块在英文块之前
    assert '"psn_bio_reembed_btn": "让 AI 能检索这份传记"' in zh
    assert '"psn_bio_reembed_nothing": "检索索引已齐全，无需补齐"' in zh
    assert '"psn_bio_reembed_full_note": "索引齐全，无需补齐"' in zh
    assert "检索服务暂不可用" in re.search(r'"psn_bio_reembed_embed_off": "([^"]+)"', zh).group(1)
    for key in ("psn_bio_reembed_btn", "psn_bio_coverage", "psn_bio_cov_gap", "psn_bio_probe_title",
                "psn_bio_reembed_running", "psn_bio_reembed_tip"):
        val = re.search(r'"%s": "([^"]+)"' % key, zh).group(1)
        for jargon in ("向量", "嵌入", "实体别名", "检索自测"):
            assert jargon not in val, (key, val)
    # zh / en 两份都有新键
    for key in ("psn_bio_reembed_full_tip", "psn_bio_reembed_full_note"):
        assert len(re.findall(r'"%s":' % key, i18n)) == 2, key


def test_full_index_disables_button_and_notes_inline():
    src = _TPL.read_text(encoding="utf-8")
    seg = src[src.index("async function _loadBioStatus()"):src.index("async function _deleteBioDoc()")]
    assert "Number(meta.emb_chunks) >= Number(meta.chunks)" in seg
    assert "(_idxFull ? ' disabled' : '')" in seg
    assert "psn_bio_reembed_full_tip" in seg and "psn_bio_reembed_full_note" in seg
    # 结果内联、不 toast、换人设即清
    end = src[src.index("function _bioReembedEnd(msg, kind)"):src.index("async function _reembedBioDoc()")]
    assert "_toast(" not in end, "结果提示不再走会消失/被遮的 toast"
    assert "_bioReembedLastNote = {pid: _editingProfileId" in end
    sync = src[src.index("function _bioReembedSyncBusy()"):src.index("function _bioReembedEnd(msg, kind)")]
    assert "ln.pid === _editingProfileId" in sync
    assert "embed_unavailable" in src and "psn_bio_reembed_embed_off" in src


def test_header_user_button_has_tooltip_and_aria():
    html = _BASE.read_text(encoding="utf-8")
    m = re.search(r'<button class="user-btn"[^>]*>', html)
    assert m and 'title="' in m.group(0) and 'aria-label="' in m.group(0)
    assert "user_menu_t" in m.group(0)
    w = _WEB_I18N.read_text(encoding="utf-8")
    assert len(re.findall(r'"user_menu_t":', w)) == 2
