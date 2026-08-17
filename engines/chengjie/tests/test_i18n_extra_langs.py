# -*- coding: utf-8 -*-
"""扩展 UI 语言表驱动契约（xlate P3，2026-08-16）。

核心不变量（对 ``EXTRA_LANGS`` 全部语言通用，加语言零改测试）：
1. ``collect_all`` 的每个扩展语言 override 键 ⊆ ZH 合并视图（orphan/typo 点名）。
2. 占位符与 ZH 逐键一致（``Tf`` 按 ZH 口径喂参，多/少占位符 = 运行时事故）。
3. ``get_translations(lang)`` 对每个 UI 语言可用：覆盖键取 override、未覆盖键
   回落英文、绝不出现裸键名。
4. ``collect_packs`` 三元组兼容契约不破（vi 首发消费方钉着它）：
   pvi == collect_all().extras['vi']。
5. 白名单单一事实源自洽：UI_LANGS = (zh, en) + EXTRA_LANGS；UI_LOCALES 全覆盖；
   bundle 路由消费同一事实源。
"""

import re

from src.web.i18n_packs import (
    EXTRA_LANGS, UI_LANGS, UI_LOCALES, collect_all, collect_packs,
)
from src.web.web_i18n import get_translations

_PH_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _ph(s: str) -> set:
    return set(_PH_RE.findall(s or ""))


def test_extra_lang_overrides_subset_of_zh():
    _zh, _en, extras = collect_all()
    zh_all = get_translations("zh")
    for lg in EXTRA_LANGS:
        orphans = sorted(k for k in extras.get(lg, {}) if k not in zh_all)
        assert not orphans, f"{lg} override 键在 ZH 合并视图不存在: {orphans[:15]}"


def test_extra_lang_placeholders_match_zh_and_nonempty():
    _zh, _en, extras = collect_all()
    zh_all = get_translations("zh")
    for lg in EXTRA_LANGS:
        ov = extras.get(lg, {})
        empty = [k for k, v in ov.items() if not str(v or "").strip()]
        assert not empty, f"{lg} 空值: {empty[:10]}"
        bad = [(k, sorted(_ph(v)), sorted(_ph(zh_all.get(k, ""))))
               for k, v in ov.items()
               if k in zh_all and _ph(v) != _ph(zh_all[k])]
        assert not bad, f"{lg} 占位符与 ZH 不一致: {bad[:10]}"


def test_merged_views_override_and_fallback_for_every_lang():
    _zh, _en, extras = collect_all()
    en_view = get_translations("en")
    for lg in EXTRA_LANGS:
        view = get_translations(lg)
        ov = extras.get(lg, {})
        assert ov, f"{lg} 无任何覆盖键（词包丢失？）"
        # 覆盖键取 override
        for k in list(ov)[:8]:
            assert view.get(k) == ov[k], f"{lg} 覆盖未生效: {k}"
        # 未覆盖键回落英文（拿一个必然未覆盖的长尾键验证）
        probe = "inbox.xl.ocr_no_text"
        assert probe not in ov
        assert view.get(probe) == en_view.get(probe) and view.get(probe) != probe


def test_th_id_spot_keys_translated():
    """th/id 关键键真实到位（防「表加了、词包没写」的空转）。"""
    for lg, expect_sub in (("th", "แปล"), ("id", "Terjemah")):
        view = get_translations(lg)
        assert expect_sub in view.get("inbox.rt.xlate", ""), (
            f"{lg} 弹层入口未翻译: {view.get('inbox.rt.xlate')!r}")
        assert view.get("inbox.xl.quick_on") != get_translations("en").get("inbox.xl.quick_on")


def test_collect_packs_tuple_contract_preserved():
    pzh, pen, pvi = collect_packs()
    _zh2, _en2, extras = collect_all()
    assert pvi == extras.get("vi")
    assert pzh and pen


def test_ui_langs_single_source_consistency():
    assert UI_LANGS == ("zh", "en") + EXTRA_LANGS
    assert set(UI_LOCALES) == set(UI_LANGS)
    # bundle 路由消费同一事实源（防白名单漂移回字面量）
    from src.web.routes import i18n_bundle_routes as br
    assert br._LANGS is UI_LANGS
    assert br._LOCALES is UI_LOCALES
    # 每个 UI 语言 get_translations 可用且非空
    for lg in UI_LANGS:
        assert get_translations(lg), f"get_translations({lg!r}) 为空"
