# -*- coding: utf-8 -*-
"""外链 i18n 词典包门禁（P2 传输减重，2026-08-07）。

背景：`_i18n_bootstrap.html` 曾把整本词典内联进每页（zh 877KB/页 tojson 后），
现改外链 `/i18n/ws-i18n.js?v=<内容指纹>` + immutable 缓存。这里钉住：
1. 指纹契约（8 hex、同代稳定、跨语言各异）；
2. 路由行为（200/JS 类型/immutable 头/lang 白名单）；
3. **restart 安全契约**——模板在「无 i18n_fp（旧进程）」与「有 i18n_fp（新进程）」
   两种上下文下都必须自洽（模板热更新先于重启上生产，旧进程没有该路由）；
4. 词条数据回归钉：任何语言不得再混入孤立代理字符（EN 曾有 \\ud83d\\udd04
   成对代理转义，utf-8 编码必炸——json ensure_ascii=False / 直出 utf-8 的路径全中招）。
"""

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.i18n_bundle_routes import register_i18n_bundle_routes
from src.web.web_i18n import get_translations, get_translations_fingerprint


# ── 1. 指纹契约 ──────────────────────────────────────────────────────────────

def test_fingerprint_shape_and_stability():
    fp1 = get_translations_fingerprint("zh")
    fp2 = get_translations_fingerprint("zh")
    assert re.fullmatch(r"[0-9a-f]{8}", fp1)
    assert fp1 == fp2, "同代词典指纹必须稳定（immutable 缓存的前提）"


def test_fingerprint_differs_across_langs():
    fps = {lang: get_translations_fingerprint(lang) for lang in ("zh", "en", "vi")}
    assert len(set(fps.values())) == 3, f"三语指纹应各异: {fps}"


# ── 2. 路由行为 ──────────────────────────────────────────────────────────────

@pytest.fixture()
def bundle_client():
    # ⚠ 勿命名为 `client`：会遮蔽 conftest 的全 app `client`，连带把依赖它的
    # `auth_client` 也劫持到这个迷你 app 上（本文件端到端用例就曾因此 404）。
    app = FastAPI()
    # 整包端点公开；api_auth 只被同模块的 /api/i18n/bundle 切片端点用（另有
    # tests/test_i18n_bundle_routes.py 门禁），这里给 no-op 即可。
    register_i18n_bundle_routes(app, api_auth=lambda _r: None)
    return TestClient(app)


def test_bundle_route_serves_js_with_immutable_cache(bundle_client):
    r = bundle_client.get("/i18n/ws-i18n.js?lang=zh&v=deadbeef")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["x-i18n-fp"] == get_translations_fingerprint("zh")
    body = r.text
    assert body.startswith("window.WS_I18N=")
    assert 'window.WS_LANG="zh"' in body
    assert 'window.WS_LOCALE="zh-CN"' in body
    # 嵌的 JSON 必须可解析且键量与词典一致
    blob = body[len("window.WS_I18N="):body.index(';window.WS_LANG')]
    assert len(json.loads(blob)) == len(get_translations("zh"))


def test_bundle_route_lang_whitelist(bundle_client):
    r = bundle_client.get("/i18n/ws-i18n.js?lang=..%2Fetc")
    assert r.status_code == 200
    assert 'window.WS_LANG="zh"' in r.text, "未知 lang 必须回落 zh 而非报错/穿越"


def test_bundle_route_en_vi_encodable(bundle_client):
    """EN/VI 曾因孤立代理字符无法 utf-8 编码——路由三语都必须能出货。"""
    for lang in ("en", "vi"):
        r = bundle_client.get(f"/i18n/ws-i18n.js?lang={lang}")
        assert r.status_code == 200, lang
        assert f'window.WS_LANG="{lang}"' in r.text


# ── 3. 模板双分支（restart 安全契约）────────────────────────────────────────

def _render_bootstrap(**ctx):
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader
    root = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(root)))
    return env.get_template("_i18n_bootstrap.html").render(**ctx)


def test_bootstrap_without_fp_inlines_dict_like_before():
    html = _render_bootstrap(i18n=get_translations("zh"), ui_lang="zh")
    assert 'window.WS_I18N = {"' in html, "旧进程分支必须内联整包（历史行为）"
    assert "/i18n/ws-i18n.js" not in html, "旧进程没有该路由，绝不能渲染外链"


def test_bootstrap_with_fp_links_bundle_and_drops_inline():
    fp = get_translations_fingerprint("zh")
    html = _render_bootstrap(i18n=get_translations("zh"), ui_lang="zh", i18n_fp=fp)
    assert f"/i18n/ws-i18n.js?lang=zh&v={fp}" in html
    assert 'window.WS_I18N = {"' not in html, "外链分支不得再内联词典（减重就白做了）"
    assert "window.WS_I18N = window.WS_I18N || {}" in html, "外链失败兜底必须在"


# ── 4. 真 app 端到端（_enrich_context 注入 fp → 页面真的走外链）──────────────

def test_full_app_pages_externalize_dict(auth_client):
    """整链证明：create_app 的 _enrich_context 注入 i18n_fp → 已登录页面引用
    外链词典包且不再内联整包；外链端点经真 app（含中间件栈）可公开取得。"""
    page = auth_client.get("/", follow_redirects=True)
    assert page.status_code == 200
    body = page.text
    assert "/i18n/ws-i18n.js?lang=" in body, "页面未引用外链词典包（fp 注入断链？）"
    assert 'window.WS_I18N = {"' not in body, "词典仍被内联（减重失效）"

    bundle = auth_client.get("/i18n/ws-i18n.js?lang=zh")
    assert bundle.status_code == 200
    assert "immutable" in bundle.headers.get("cache-control", "")
    assert bundle.text.startswith("window.WS_I18N=")


# ── 5. 词条数据回归钉 ────────────────────────────────────────────────────────

def test_no_lone_surrogates_in_any_language():
    """词条不得含孤立代理字符（U+D800..DFFF）。

    Python 源里写 \\ud83d\\udd04 这类成对代理**不会**像 JSON 那样合并成一个
    astral 码点，而是留下两个孤立代理 → 任何 utf-8 直出路径（外链词典包
    ensure_ascii=False、日志、文件写出）当场 UnicodeEncodeError。
    正确写法：真实 emoji 字符或 \\U0001F504 单码点转义。
    """
    offenders = []
    for lang in ("zh", "en", "vi"):
        for k, v in get_translations(lang).items():
            s = str(v)
            if any(0xD800 <= ord(c) <= 0xDFFF for c in s):
                offenders.append((lang, k))
    assert not offenders, f"孤立代理字符词条: {offenders[:6]}"
