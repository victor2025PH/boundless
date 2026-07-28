"""渠道中心四页 hero 骨架统一契约。

历史：Telegram 页自带一套 `.tg-hero*` 私有骨架，与其余三页的共享
`.rpa-hero`（`_rpa_shared_styles.html`）视觉分裂。统一后四页同用
`.rpa-hero` + `theme-<plat>` 头像 + `.rpa-hero-actions`（内含
「管理账号」深链），Telegram 仅保留 JS 契约必需的元素 ID 与
本页复用类（`.tg-online-dot` / `.tg-stat` / `.tg-hero-refresh`）。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL_DIR = _ROOT / "src" / "web" / "templates"
_SHARED = _TPL_DIR / "_rpa_shared_styles.html"
_BODIES = {
    "telegram": _TPL_DIR / "_channel_body_telegram.html",
    "line": _TPL_DIR / "_channel_body_line.html",
    "whatsapp": _TPL_DIR / "_channel_body_whatsapp.html",
    "messenger": _TPL_DIR / "_channel_body_messenger.html",
}

# hero 内 JS 消费的 Telegram 元素 ID（loadAccountInfo 的写入点，动了即哑面板）
_TG_HERO_IDS = ("h-dot", "h-name", "h-sub", "st-msg", "st-vin", "st-tts", "st-gate")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_shared_styles_define_all_four_themes():
    css = _read(_SHARED)
    for plat in _BODIES:
        assert f".rpa-hero-avatar.theme-{plat}" in css, plat


def test_all_four_bodies_use_shared_hero_skeleton():
    for plat, path in _BODIES.items():
        html = _read(path)
        assert 'class="rpa-hero"' in html, plat
        assert f"rpa-hero-avatar theme-{plat}" in html, plat
        assert "rpa-hero-actions" in html, plat
        # 「管理账号」深链四页齐平（hero 级入口）
        assert f"/workspace?drawer=1&amp;connect={plat}" in html, plat
        assert "chc_acct_hero_btn" in html, plat


def test_hero_section_order_is_uniform():
    """信息 → KPI → 动作，四页一致（首个 hero 即页顶 hero，首次出现序即块内序）。"""
    for plat, path in _BODIES.items():
        html = _read(path)
        i_info = html.index("rpa-hero-info")
        i_kpi = html.index("rpa-kpi-row")
        i_act = html.index("rpa-hero-actions")
        assert i_info < i_kpi < i_act, (
            f"{plat}: hero 区块顺序漂移 info={i_info} kpi={i_kpi} actions={i_act}")


def test_telegram_private_hero_skeleton_is_gone():
    html = _read(_BODIES["telegram"])
    # 私有骨架类不得再出现在标记里（CSS 里保留的复用类不在此列）
    for cls in ('class="tg-hero"', "tg-hero-avatar", "tg-hero-info",
                "tg-hero-name", "tg-hero-sub", 'class="tg-stats"'):
        assert cls not in html, f"Telegram 私有 hero 骨架残留: {cls}"


def test_telegram_hero_keeps_js_contract_ids():
    html = _read(_BODIES["telegram"])
    hero_m = re.search(r'<div class="rpa-hero">(.*?)\n</div>', html, re.S)
    assert hero_m, "找不到 telegram .rpa-hero 块"
    hero = hero_m.group(1)
    for el_id in _TG_HERO_IDS:
        assert f'id="{el_id}"' in hero, f"hero 内 JS 契约 ID 丢失: {el_id}"
    # 三态连接点仍由 .tg-online-dot 驱动（_setConnDot 的 class 语义）
    assert 'class="tg-online-dot offline" id="h-dot"' in hero


def test_telegram_reused_classes_still_defined():
    html = _read(_BODIES["telegram"])
    # 账号页统计卡 / 各卡刷新钮 / 三态点：标记仍在用，样式必须还在
    for cls in (".tg-online-dot", ".tg-stat{", ".tg-stat-n{",
                ".tg-stat-l{", ".tg-hero-refresh{"):
        assert cls in html, f"复用类样式被误删: {cls}"
    assert 'class="tg-stat"' in html      # 账号页统计卡
    assert 'class="tg-hero-refresh"' in html  # 账号/日志/快照刷新钮


# ── 卡片层统一（hero 之后第二层骨架）────────────────────────────────────────

def test_shared_styles_define_card_family():
    css = _read(_SHARED)
    for cls in (".rpa-card{", ".rpa-card-head{", ".rpa-card-icon{",
                ".rpa-card-icon.blue{", ".rpa-card-icon.green{",
                ".rpa-card-icon.purple{", ".rpa-card-title{",
                ".rpa-sect-div{"):
        assert cls in css, f"共享卡片族缺规则: {cls}"


def test_telegram_private_card_skeleton_is_gone():
    """`.tg-card` 私有克隆已整体迁到共享 `.rpa-card` 族——标记与 CSS 双清零，
    防止后续新卡片又照旧模板抄回私有类。"""
    html = _read(_BODIES["telegram"])
    assert "tg-card" not in html, "Telegram 私有卡片类残留（标记或 CSS）"
    assert "tg-sect-div" not in html
    assert html.count('class="rpa-card"') >= 10
    assert html.count('class="rpa-card-head"') >= 10
    assert 'class="rpa-sect-div"' in html


def test_whatsapp_stays_on_shared_card_family():
    """WA 是共享卡片族的存量正统（74×），别被反向"统一"回私有类。"""
    html = _read(_BODIES["whatsapp"])
    assert html.count("rpa-card") >= 50
