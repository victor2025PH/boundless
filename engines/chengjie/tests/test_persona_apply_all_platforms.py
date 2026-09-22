# -*- coding: utf-8 -*-
"""人设「应用到…」弹窗覆盖全部运行时登记平台（微信 PC / 微信客服 / QQ / 抖音 …）。

之前 /api/personas/status 只输出 tg/mrpa/wa/line 四桶，前端也只渲染这四个平台，
其他已接入的 APP 账号无法在弹窗里指定人设。现在 status 额外输出
``other_accounts``（registry 中除上述四平台外的所有行，带 ``platform`` 字段），
前端按 platform 分组渲染并统一走 registry assign 端点。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
SRC = (_ROOT / "src" / "web" / "routes" / "persona_routes.py").read_text(
    encoding="utf-8")
JS = (_ROOT / "src" / "web" / "static" / "js"
      / "persona_apply_modal.js").read_text(encoding="utf-8")
HTML = (_ROOT / "src" / "web" / "templates" / "personas.html").read_text(
    encoding="utf-8")


def _whitelist() -> str:
    wl = SRC[SRC.index("_REGISTRY_ASSIGN_PLATFORMS"):]
    return wl[:wl.index("}") + 1]


def test_registry_assign_whitelist_covers_all_runtime_platforms():
    wl = _whitelist()
    for plat in ("wechat", "wechat_kf", "qq", "qqbot", "zalo", "instagram",
                 "douyin", "tiktok", "line", "whatsapp", "messenger"):
        assert f'"{plat}"' in wl, plat
    # telegram 仍走专用 tg-account 端点
    assert '"telegram"' not in wl


def test_status_emits_other_accounts_from_registry():
    assert "other_accounts: list = []" in SRC
    assert '"other_accounts": other_accounts,' in SRC
    # 从登记表实际存在的 platform 枚举，而不是硬编码平台清单
    seg = SRC[SRC.index("other_accounts: list = []"):]
    seg = seg[:seg.index('"other_accounts": other_accounts')]
    assert "_rt_reg.list()" in seg
    assert re.search(r'_own_plats = \{"telegram", "messenger", "whatsapp", "line"', seg)
    assert '_xr["platform"] = _xp' in seg
    # 未用人设统计要把新桶算进去
    assert re.search(
        r"for acc in \(tg_accounts \+ mrpa_accounts \+ wa_accounts \+ line_accounts\s*"
        r"\+ other_accounts\)", SRC)


def test_apply_modal_renders_other_accounts_via_registry_endpoint():
    assert "d.other_accounts" in JS
    assert "function _accRow(plat, a, forceSrc)" in JS
    # 其他平台一律 registry 来源 → assign 走 /api/personas/registry-account/{platform}
    assert "_accRow(plat, o, 'registry')" in JS
    assert "String(o.platform || '').toLowerCase()" in JS
    # 未知平台有兜底标签，不会渲染成 undefined
    assert "function _platLabel(plat)" in JS
    assert "PLAT_LABEL[plat] || String(plat || '').toUpperCase()" in JS
    for plat in ("wechat:", "wechat_kf:", "qq:", "douyin:", "zalo:", "instagram:"):
        assert plat in JS, plat
    # 既有四桶不变
    assert "'tg', 'mrpa', 'wa', 'line'" in JS


def test_apply_modal_cache_busted():
    assert "persona_apply_modal.js?v=p4" in HTML
    assert "persona_apply_modal.js?v=p3" not in HTML
