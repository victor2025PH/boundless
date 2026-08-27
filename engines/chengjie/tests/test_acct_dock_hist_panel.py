"""Account Dock「历史 N」治理面板接线 ratchet（2026-08-17）。

并行线写了面板骨架但 chip 仍走 toggleGhostAccounts，且确认框 altText 未接到
_appConfirm。本门禁钉住：chip 打开面板、确认框有第三键、面板脚有抽屉入口。
抽屉历史分区 HTML 不在本文件范围。
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / "src" / "web" / "templates" / "unified_inbox.html"
I18N = ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py"


def test_hist_chip_opens_panel_not_inline_expand():
    html = INBOX.read_text(encoding="utf-8")
    m = re.search(r'<div class="adk-item ac-toggle[^"]*"[^>]*id="adk-hist-chip"[^>]*>', html)
    assert m, "dock 必须有 id=adk-hist-chip（面板锚定 + 外点关闭依赖它）"
    chip = m.group(0)
    assert "toggleAdkHistPanel(event)" in chip
    assert "toggleGhostAccounts" not in chip
    assert "function toggleAdkHistPanel" in html
    assert "function closeAdkHistPanel" in html


def test_app_confirm_alt_text_wired_for_export_first():
    html = INBOX.read_text(encoding="utf-8")
    assert "opts.altText" in html
    assert "data-act=\"alt\"" in html
    assert "done('alt')" in html
    assert "purge_export_first" in html


def test_hist_panel_footer_has_drawer_and_sort_logged_out_first():
    html = INBOX.read_text(encoding="utf-8")
    assert 'data-act="drawer"' in html
    assert "inbox.dock.hist_drawer" in html
    assert "st==='logged_out'?0" in html
    assert "iflt_adk_longpress" in html
    assert "class=\"adk-more\"" in html


def test_hist_drawer_i18n_bilingual():
    src = I18N.read_text(encoding="utf-8")
    assert '"inbox.dock.hist_drawer": "在管理抽屉中打开"' in src
    assert '"inbox.dock.hist_drawer": "Open in account drawer"' in src
