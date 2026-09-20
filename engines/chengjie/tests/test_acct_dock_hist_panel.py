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


# ── 在册未接管号的危险项（2026-08-28）：移除 vs 彻底删除二选一 ────────────────
# 实录：桌面工作台号（status=desktop）在历史分区只有查看/清未读/导出，坐席问
# 「为什么删不掉」。它注册表里有行且状态活跃 → 服务端 _purge_history_blocked
# 直接 409，所以正确出路是先 /remove 转 removed，再彻底删除。两处消费面
# （dock 治理表 + 抽屉历史行）必须同表，否则两个入口能力不同。
_REG_BRANCH = r"else if\(st==='desktop'\|\|st==='registered'\)\{"
_DEAD_BRANCH = r"if\(st==='logged_out'\|\|st==='removed'\|\|st==='history_only'\)\{"


def test_registered_accounts_offer_remove_in_both_faces():
    html = INBOX.read_text(encoding="utf-8")
    ends = [m.end() for m in re.finditer(_REG_BRANCH, html)]
    assert len(ends) == 2, (
        "_adkGovItems（dock 治理表）与 _acctHistRowHtml（抽屉历史行）必须同时给"
        "desktop/registered 的「移除」项——一处有一处无＝坐席在两个入口看到不同能力")
    for pos in ends:
        seg = html[pos:pos + 600]
        assert "removeAcct(" in seg, "该分支的动作必须是 removeAcct（注册表软删）"
        assert "inbox.acct.hist_remove_t" in seg, "必须带说明 tip（含会被重新登记的告知）"
        assert "purgeAcctHistory" not in seg, (
            "desktop/registered 不得直接 purge：服务端 _purge_history_blocked 会 409")


def test_purge_branch_still_gated_on_dead_statuses():
    """彻底删除仍只给三个死态，且与在册分支互斥（else if，不是并列）。"""
    html = INBOX.read_text(encoding="utf-8")
    ends = [m.end() for m in re.finditer(_DEAD_BRANCH, html)]
    assert len(ends) == 2
    for pos in ends:
        assert "purgeAcctHistory(" in html[pos:pos + 600]


def test_hist_remove_tip_bilingual():
    src = I18N.read_text(encoding="utf-8")
    assert src.count('"inbox.acct.hist_remove_t"') == 2, "zh/en 必须双语齐备"
    assert "转为「已移除」后可恢复" in src
    assert "re-registered by the next mirrored message" in src
