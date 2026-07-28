"""渠道中心 Telegram 登录 ↔ 工作台账号抽屉深链契约。

产品不变量：渠道中心展示的是主会话（A 线）；工作台扫码是坐席多开号（B 线）。
UI 必须诚实说明，并深链到 ``/workspace?drawer=1&connect=telegram``，
由工作台 ``init()`` 打开抽屉 + 接入弹窗——禁止在渠道中心再嵌一套
「扫码修好主号」的假登录。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "src" / "web" / "templates"
_I18N = _ROOT / "src" / "web" / "i18n_packs" / "telegram_page.py"


def _read(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


_REQUIRED_KEYS = (
    "tg_login_offline_t",
    "tg_login_connecting_t",
    "tg_login_body",
    "tg_login_ops",
    "tg_login_cta",
    "tg_login_manage",
)


def test_channel_telegram_cta_deeplink_present():
    html = _read("_channel_body_telegram.html")
    assert 'id="tg-login-cta"' in html
    assert 'id="tg-login-cta-acc"' in html
    assert "/workspace?drawer=1&amp;connect=telegram" in html
    assert "_renderTgLoginCta" in html
    # 诚实文案键（禁止只写「去扫码登录」误导）
    assert "tg_login_body" in html
    assert "tg_login_ops" in html


def test_workspace_drawer_connect_deeplink_handler():
    html = _read("unified_inbox.html")
    assert "drawer" in html and "connect" in html
    assert "openDrawer" in html and "openConnect" in html
    # 消费后清参，避免刷新反复弹窗
    assert "searchParams.delete(" in html and "drawer" in html
    assert re.search(r"searchParams\.delete\(['\"]drawer['\"]\)", html)
    assert re.search(r"searchParams\.delete\(['\"]connect['\"]\)", html)
    assert "history.replaceState" in html
    assert "_wantDrawer" in html and "_CONNECT_PLATS" in html
    # 平台白名单，防任意 connect= 注入
    assert "'telegram'" in html and "'whatsapp'" in html
    assert "'line'" in html and "'messenger'" in html


def test_telegram_login_i18n_keys_bilingual():
    src = _I18N.read_text(encoding="utf-8")
    # 粗切 ZH / EN 两段 dict（模块约定 ZH = {...} EN = {...}）
    zh_m = re.search(r"\bZH\s*=\s*\{(.*?)\n\}", src, re.S)
    en_m = re.search(r"\bEN\s*=\s*\{(.*?)\n\}", src, re.S)
    assert zh_m and en_m, "telegram_page.py 找不到 ZH/EN dict"
    zh, en = zh_m.group(1), en_m.group(1)
    missing_zh = [k for k in _REQUIRED_KEYS if f'"{k}"' not in zh]
    missing_en = [k for k in _REQUIRED_KEYS if f'"{k}"' not in en]
    assert not missing_zh, f"ZH 缺键: {missing_zh}"
    assert not missing_en, f"EN 缺键: {missing_en}"
    # A/B 线诚实：中文文案须同时点出主会话与多开号
    assert "主会话" in zh and "多开" in zh
