"""渠道中心壳层「平台连接卡」契约（2026-07-29 起由「账号入口轨」升级）。

四渠道共用 ``#chc-acct-rail`` 连接卡：品牌图标 + 连接态徽章 + 账号摘要
（``chcLoadAcctSummary`` 拉 /api/accounts）+ 深链 ``/workspace?drawer=1&connect=<plat>``
（**单一事实源**：正文 hero 不再重复「管理账号」按钮）；会话健康行内嵌卡内；
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
    # 平台连接卡（2026-07-29）：账号摘要 + 非 TG 计数徽章
    "chc_acct_sum_total", "chc_acct_sum_on",
    "chc_acct_badge_on", "chc_acct_badge_all_off",
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


def test_shell_connection_card_summary_and_status():
    """平台连接卡（2026-07-29）：账号摘要 + 状态单一写入口 + 会话健康行内嵌卡内。"""
    txt = _TPL.read_text(encoding="utf-8")
    # 账号摘要（头像叠放 + 账号/在线计数），整块深链抽屉
    assert 'id="chc-acct-sum"' in txt
    assert 'id="chc-acct-sum-avs"' in txt
    assert re.search(r"^async function chcLoadAcctSummary\(", txt, re.M)
    assert "/api/accounts" in txt
    # data-status 单一写入口（TG=主会话 / MSG·WA=会话健康 / 其余=摘要兜底）
    assert re.search(r"^function chcSetRailStatus\(", txt, re.M)
    # 会话健康行内嵌进连接卡（strip 元素在 rail 块内，id 契约不变）
    rail_start = txt.index('id="chc-acct-rail"')
    strip_pos = txt.index('id="chc-session-strip"')
    assert strip_pos > rail_start
    # 零账号获客空态（P3）：能力卖点 chips 复用收件箱 inbox.acct.* 词条（单一词源）
    assert 'id="chc-acct-empty"' in txt
    assert "inbox.acct.empty_lead" in txt
    assert "inbox.acct.cap_autoreply" in txt
    # 品牌色注入（与收件箱 PC 映射同值域）
    for plat in _BODIES:
        assert f'data-plat="{plat}"' in txt or 'data-plat="{{ channel }}"' in txt


def test_manage_account_entry_is_single_source():
    """「管理账号」入口单一事实源（2026-07-29 去重钉）：只在壳层连接卡，
    正文 hero 不得再挂重复按钮——三层 CTA（入口轨/Hero/离线条）挤首屏的旧病不许回潮。
    Telegram 离线引导条（tg-login-cta，仅离线态显示）的深链属状态化指引，不在禁列。"""
    shell = _TPL.read_text(encoding="utf-8")
    assert "chc_acct_cta" in shell            # 壳层主 CTA 仍在
    for plat, path in _BODIES.items():
        html = path.read_text(encoding="utf-8")
        assert "chc_acct_hero_btn" not in html, f"{plat}: hero 重复「管理账号」按钮回潮"


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
