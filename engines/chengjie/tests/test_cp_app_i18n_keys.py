# -*- coding: utf-8 -*-
"""副驾 App 壳静态词条 + 出图错误码 i18n 契约门禁（2026-08-22）。

实锤背景：8-22 早晨坐席壳里「AI 生成图片」卡标题显示裸键 ``cp.app.h_image``——
键本身在 cp-i18n.js 里**有**，但 app.html 引字典的 ``?v=`` 停在 20260821a、
webview 用的是缓存旧字典（缺 8-22 新增键 → ``t()`` 回落裸键名）。缓存问题靠
bump 双戳修，**漏键问题**从此由本门禁静态钉死：

① app.html 所有 ``data-cp-i18n``/``-ph``/``-title`` 引用键必须存在于 zh+en 词典
   （缺任一语言＝对应语言界面裸键）；
② 出图失败错误码契约：路由 ``KNOWN_ERROR_CODES`` 每个码（除 unknown 走通用
   回落）必须有 ``cp.image.errc_<code>``/``errs_<code>`` 双语词条——后端新增码
   忘配词条时这里先红，坐席不再看见生错误码当标题。
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_cp_i18n_parity import _parse

_REPO = Path(__file__).resolve().parents[1]
_APP = _REPO / "shared" / "copilot" / "app.html"

_ATTR = re.compile(r'data-cp-i18n(?:-ph|-title)?="([^"]+)"')

# 前端自造码（不经后端）：网络层失败。与后端契约表并集参与词条校验。
_FRONTEND_ONLY_CODES = ("network",)


def _dicts():
    zh, en, suspects = _parse()
    assert not suspects, f"cp-i18n.js 词条格式漂移：{suspects[:5]}"
    return zh, en


def test_app_html_static_keys_exist_bilingual():
    zh, en = _dicts()
    keys = set(_ATTR.findall(_APP.read_text(encoding="utf-8")))
    assert keys, "app.html 未扫出任何 data-cp-i18n 键——解析器疑似失效"
    miss_zh = sorted(k for k in keys if k not in zh)
    miss_en = sorted(k for k in keys if k not in en)
    assert not miss_zh, f"app.html 引用键缺 zh 词条（中文界面裸键）：{miss_zh}"
    assert not miss_en, f"app.html 引用键缺 en 词条（英文界面裸键）：{miss_en}"


def test_image_error_code_i18n_contract():
    from src.web.routes.image_gen_routes import KNOWN_ERROR_CODES

    zh, en = _dicts()
    need = [c for c in KNOWN_ERROR_CODES if c != "unknown"]
    need += list(_FRONTEND_ONLY_CODES)
    missing = []
    for code in need:
        for k in (f"cp.image.errc_{code}", f"cp.image.errs_{code}"):
            if k not in zh or k not in en:
                missing.append(k)
    # 通用回落词条（unknown / 未配码时的标题与建议）必须在
    for k in ("cp.image.err_ttl_generic", "cp.image.errs_unknown"):
        if k not in zh or k not in en:
            missing.append(k)
    assert not missing, (
        "出图错误码缺 i18n 词条（坐席将看到生错误码/裸键当标题）："
        f"{sorted(set(missing))}")


def test_image_error_suggestions_do_not_leak_internal_hosts():
    """建议文案是给坐席看的人话：不得夹内网 IP/端口（内部拓扑不外泄，
    技术细节属「技术详情」折叠区）。"""
    zh, en = _dicts()
    ip = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|:\d{4,5}\b")
    bad = [k for k, v in list(zh.items()) + list(en.items())
           if k.startswith("cp.image.err") and ip.search(v)]
    assert not bad, f"错误文案泄漏内网地址：{sorted(set(bad))}"
