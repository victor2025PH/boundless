"""对话翻译 UI 语种目录必须与 HY-MT 模型卡 34 码同一份。

前端 ``_XL_CATALOG`` 是坐席能点到的目标语；后端 ``HYMT_TARGET_LANGS`` /
``_AGENT_LANG_ALLOWED`` 是能落库/能路由的目标语。两边分叉 = 芯片能选、
一键开启/发→X 被 400 打回。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.ai.translation_engines import HYMT_TARGET_LANGS
from src.web.routes import unified_inbox_translate_routes as xlate_routes

_ENGINE = Path(__file__).resolve().parents[1]
_INBOX = _ENGINE / "src" / "web" / "templates" / "unified_inbox.html"


def _catalog_codes_from_inbox() -> set[str]:
    src = _INBOX.read_text(encoding="utf-8")
    m = re.search(r"const _XL_CATALOG=\[(.*?)\];", src, re.S)
    assert m, "_XL_CATALOG 数组在 unified_inbox.html 里找不到"
    codes = re.findall(r"code:'([a-z0-9-]+)'", m.group(1))
    assert codes, "_XL_CATALOG 一项 code 都没解析到"
    return set(codes)


def test_xl_catalog_matches_hymt_langs():
    ui = _catalog_codes_from_inbox()
    hymt = set(HYMT_TARGET_LANGS)
    assert ui == hymt, (
        f"UI 目录与 HY-MT 模型卡不一致。\n"
        f"  只在 UI：{sorted(ui - hymt)}\n"
        f"  只在 HY-MT：{sorted(hymt - ui)}"
    )
    assert len(ui) == 34


def test_agent_lang_allowlist_is_hymt():
    assert set(xlate_routes._AGENT_LANG_ALLOWED) == set(HYMT_TARGET_LANGS)


def test_hymt_public_alias_is_frozenset_of_private():
    # 防止有人改 _HYMT_LANGS 却忘了公开别名（或反过来各写一份）。
    from src.ai import translation_engines as te
    assert te.HYMT_TARGET_LANGS == frozenset(te._HYMT_LANGS)


def test_catalog_endonyms_avoid_cjk_and_cover_non_cjk():
    src = _INBOX.read_text(encoding="utf-8")
    m = re.search(r"const _XL_CATALOG=\[(.*?)\];", src, re.S)
    assert m, "_XL_CATALOG 数组在 unified_inbox.html 里找不到"
    body = m.group(1)
    cjk = re.findall(r"[\u4e00-\u9fff]", body)
    assert not cjk, f"目录字面量含 CJK（应走 dash.lang.* / 非汉字自称）: {cjk[:8]!r}"
    n_endonym = len(re.findall(r"\be:'", body))
    assert n_endonym >= 28, f"非汉字自称太少: {n_endonym}"


def test_hymt_eval_covers_all_34_targets():
    from src.eval.dataset import load_translation_samples

    samples = load_translation_samples(
        str(_ENGINE / "config" / "eval" / "translation_samples_hymt.yaml")
    )
    fwd = {
        s.target_lang
        for s in samples
        if s.target_lang != "zh"
        and (not s.source_lang or s.source_lang in ("zh", "zh-cn"))
    }
    missing = set(HYMT_TARGET_LANGS) - fwd - {"zh"}
    assert not missing, f"HY-MT 宽集缺 zh→xx 样本: {sorted(missing)}"
