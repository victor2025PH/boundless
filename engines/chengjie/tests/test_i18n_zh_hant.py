# -*- coding: utf-8 -*-
"""繁体中文 (zh_hant) UI 语言门禁（zh_hant P2，2026-08-27）。

与 vi/th/id 不同的三个契约：
1. **全量覆盖**：zh_hant 由 scripts/i18n_hant.py 确定性转换生成，覆盖率必须
   ≥99%（新简体键涨多了 = 该 regen 了：``python -m scripts.i18n_hant generate``）。
2. **底语言 = 简体**：缺键回落 zh 而非 en（EXTRA_LANG_BASE），繁体坐席看简体可读。
3. **转换质量抽检**：台湾用语到位（儲存/話術）、术语钉生效（台不作臺）、
   占位符守恒（通用门禁 test_i18n_extra_langs 亦覆盖）。
"""

from src.web.i18n_packs import EXTRA_LANG_BASE, EXTRA_LANGS, UI_LOCALES, collect_all
from src.web.web_i18n import _merge_views, get_translations


def test_zh_hant_registered_in_single_sources():
    assert "zh_hant" in EXTRA_LANGS
    assert UI_LOCALES.get("zh_hant") == "zh-Hant"
    assert EXTRA_LANG_BASE.get("zh_hant") == "zh"


def test_zh_hant_full_coverage_ratchet():
    zh = get_translations("zh")
    _z, _e, extras = collect_all()
    ov = extras.get("zh_hant", {})
    coverage = len(set(ov) & set(zh)) / max(len(zh), 1)
    assert coverage >= 0.99, (
        f"zh_hant 覆盖率 {coverage:.2%} < 99% —— 简体新键攒多了，"
        "请跑 python -m scripts.i18n_hant generate 重新生成")


def test_zh_hant_conversion_quality_spots():
    view = get_translations("zh_hant")
    assert view.get("save") == "儲存", view.get("save")
    assert "話術" in view.get("templates", ""), view.get("templates")
    assert view.get("care") == "主動關懷", view.get("care")
    # 品牌名不受转换伤害（智聊简繁同形）
    assert "智聊" in view.get("brand", "")


def test_zh_hant_no_tai_variant_pin():
    """术语钉「臺→台」生效：全量值里不得出现「臺」（工作台/平台/后台惯用台）。"""
    _z, _e, extras = collect_all()
    bad = [k for k, v in extras.get("zh_hant", {}).items() if "臺" in v]
    assert not bad, f"臺 未钉成 台: {bad[:10]}"


def test_zh_hant_missing_key_falls_back_to_simplified():
    """底语言合并语义单元验证：zh_hant 缺键 → 简体（不是英文、不是裸键名）。"""
    out = _merge_views(
        {"zh": {"k1": "简体甲", "k2": "简体乙"}, "en": {"k1": "A", "k2": "B"}},
        {}, {}, {"zh_hant": {"k2": "繁體乙"}},
        ("zh_hant",), {"zh_hant": "zh"})
    assert out["zh_hant"]["k2"] == "繁體乙"
    assert out["zh_hant"]["k1"] == "简体甲"  # 缺键 → 简体底
    # 对照：默认英文底的扩展语行为不受影响
    out2 = _merge_views(
        {"zh": {"k1": "简"}, "en": {"k1": "A"}}, {}, {},
        {"vi": {}}, ("vi",), {"zh_hant": "zh"})
    assert out2["vi"]["k1"] == "A"


def test_zh_hant_in_switcher_template():
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
           / "_i18n_bootstrap.html").read_text(encoding="utf-8")
    assert "zh_hant" in tpl, "语言选择器缺繁体入口"
    assert "\\u7e41\\u9ad4\\u4e2d\\u6587" in tpl, "选择器繁体自称（繁體中文）缺失"
