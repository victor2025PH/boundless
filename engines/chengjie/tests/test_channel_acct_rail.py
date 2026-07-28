"""渠道中心壳层「统一账号入口轨」契约。

四渠道共用 ``#chc-acct-rail``：深链 ``/workspace?drawer=1&connect=<plat>``；
Telegram 另附 A 线主会话运维 steps + ``chcLoadTgMainStatus`` 徽章态。
禁止在渠道中心再嵌「扫码修好主号」假登录。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates" / "workspace_channels.html"
_BODIES = {
    "telegram": _ROOT / "src" / "web" / "templates" / "_channel_body_telegram.html",
    "line": _ROOT / "src" / "web" / "templates" / "_channel_body_line.html",
    "whatsapp": _ROOT / "src" / "web" / "templates" / "_channel_body_whatsapp.html",
    "messenger": _ROOT / "src" / "web" / "templates" / "_channel_body_messenger.html",
}

_ACCT_KEYS = (
    "chc_acct_title", "chc_acct_badge_seat",
    "chc_acct_badge_main_ok", "chc_acct_badge_main_connecting", "chc_acct_badge_main_off",
    "chc_acct_body_tg", "chc_acct_body_line", "chc_acct_body_wa", "chc_acct_body_msg",
    "chc_acct_cta", "chc_acct_inbox",
    "chc_acct_ops_sum", "chc_acct_ops_1", "chc_acct_ops_2", "chc_acct_ops_3", "chc_acct_ops_4",
    "chc_acct_hero_btn",
)


def test_shell_acct_rail_deeplink_and_ops():
    txt = _TPL.read_text(encoding="utf-8")
    assert txt.count('id="chc-acct-rail"') == 1
    assert 'id="chc-acct-ops"' in txt
    assert "/workspace?drawer=1&amp;connect={{ channel }}" in txt
    assert "chcLoadTgMainStatus" in txt
    assert re.search(r"^async function chcLoadTgMainStatus\(", txt, re.M)
    assert "/api/telegram/account-info" in txt
    # WA 掉线指引也走深链（不再裸 /workspace）
    assert "/workspace?drawer=1&amp;connect=whatsapp" in txt


def test_rpa_heroes_have_manage_account_deeplink():
    for plat, path in _BODIES.items():
        if plat == "telegram":
            # TG 深链在正文 CTA + 壳层轨，不强制 hero 按钮
            html = path.read_text(encoding="utf-8")
            assert f"/workspace?drawer=1&amp;connect={plat}" in html
            continue
        html = path.read_text(encoding="utf-8")
        assert f"/workspace?drawer=1&amp;connect={plat}" in html, plat
        assert "chc_acct_hero_btn" in html, plat


def test_acct_i18n_bilingual_and_honest_ab_line():
    from src.web.i18n_packs import channel_center as cc
    assert set(cc.ZH) == set(cc.EN)
    for k in _ACCT_KEYS:
        assert cc.ZH.get(k, "").strip(), k
        assert cc.EN.get(k, "").strip(), k
    assert "主会话" in cc.ZH["chc_acct_body_tg"]
    assert "多开" in cc.ZH["chc_acct_body_tg"]
    assert "code.txt" in cc.ZH["chc_acct_ops_2"]
    assert "restart_instance.ps1" in cc.ZH["chc_acct_ops_3"]
