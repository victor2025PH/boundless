# -*- coding: utf-8 -*-
"""系统语言自动跟随门禁（2026-08-27）。

契约（登录/未登录页通用，中间件三级链）：
1. ``?lang=`` 显式压制一切；
2. ``ui_lang`` cookie（显式选择 / 登录回填）优先于推断；
3. 两者皆无 → ``Accept-Language`` 在 UI_LANGS 白名单内协商（zh 家族按
   TW/HK/MO/Hant 细分繁体；q 值降序）；
4. 推断**不落 cookie**（跟随而非固化：换系统语言界面即跟走）；
5. 全部无信号 → zh。
"""

import pytest

from src.web.i18n_packs import negotiate_ui_lang


@pytest.mark.parametrize("header,want", [
    ("zh-TW,zh;q=0.9,en;q=0.8", "zh_hant"),
    ("zh-HK", "zh_hant"),
    ("zh-Hant-TW", "zh_hant"),
    ("zh-MO;q=0.7", "zh_hant"),
    ("zh-CN,zh;q=0.9", "zh"),
    ("zh", "zh"),
    ("zh-Hans-SG", "zh"),
    ("en-US,en;q=0.9", "en"),
    ("en-GB", "en"),
    ("vi-VN,vi;q=0.9,en-US;q=0.8", "vi"),
    ("th-TH,th;q=0.9", "th"),
    ("id-ID", "id"),
    # q 值优先级：低 q 的 en 输给高 q（默认 1.0）的 vi
    ("en;q=0.5,vi", "vi"),
    # 首选不在白名单 → 顺延下一个候选
    ("ja,en;q=0.8", "en"),
    ("fr-FR,de;q=0.9", ""),
    ("*", ""),
    ("", ""),
    ("garbage;;q=x,,", ""),
])
def test_negotiate_ui_lang(header, want):
    assert negotiate_ui_lang(header) == want


def _html_lang(client, path="/login", **kwargs):
    r = client.get(path, **kwargs)
    assert r.status_code == 200
    head = r.text[:300]
    import re
    m = re.search(r'<html lang="([^"]+)"', head)
    assert m, f"页面无 <html lang>: {head!r}"
    return m.group(1)


def test_login_page_follows_accept_language(client):
    """无 cookie 无 ?lang → 登录页跟随浏览器/系统语言（含 locale 收口）。"""
    assert _html_lang(client, headers={"Accept-Language": "vi-VN,vi;q=0.9"}) == "vi-VN"
    assert _html_lang(client, headers={"Accept-Language": "zh-TW"}) == "zh-Hant"
    assert _html_lang(client, headers={"Accept-Language": "en-US,en;q=0.5"}) == "en-US"
    # 白名单外 → 默认简中
    assert _html_lang(client, headers={"Accept-Language": "fr-FR"}) == "zh-CN"


def test_explicit_choice_beats_system_language(client):
    """cookie（显式选择/登录回填）> 推断；?lang= > 一切。"""
    client.cookies.set("ui_lang", "en")
    assert _html_lang(client, headers={"Accept-Language": "vi-VN"}) == "en-US"
    assert _html_lang(client, path="/login?lang=th",
                      headers={"Accept-Language": "vi-VN"}) == "th-TH"
    client.cookies.delete("ui_lang")


def test_negotiation_does_not_set_cookie(client):
    """推断不落 cookie——跟随而非固化（换浏览器语言界面即跟走）。"""
    r = client.get("/login", headers={"Accept-Language": "vi-VN"})
    assert r.status_code == 200
    assert "ui_lang" not in r.cookies, "自动推断不得写 ui_lang cookie"


def test_dirty_query_falls_to_cookie_not_negotiation(client):
    """逐级独立校验：脏 ?lang= 落到合法 cookie，而非吞掉它直接跳推断。"""
    client.cookies.set("ui_lang", "th")
    got = _html_lang(client, path="/login?lang=garbage",
                     headers={"Accept-Language": "vi-VN"})
    client.cookies.delete("ui_lang")
    assert got == "th-TH", f"脏 query 吞掉了合法 cookie: {got}"


def test_set_lang_auto_clears_cookie_and_junk_is_noop(client):
    """auto=回到跟随态（删 cookie）；白名单外值=纯 no-op（不写脏 cookie）。"""
    client.get("/set_lang?lang=vi", follow_redirects=False)
    assert client.cookies.get("ui_lang") == "vi"
    r = client.get("/set_lang?lang=auto", follow_redirects=False)
    assert r.status_code == 303
    assert not client.cookies.get("ui_lang"), "auto 未清除 ui_lang cookie"
    # 清除后回到跟随态
    assert _html_lang(client, headers={"Accept-Language": "zh-TW"}) == "zh-Hant"
    # 脏值 no-op：不写 cookie
    client.get("/set_lang?lang=hacker", follow_redirects=False)
    assert not client.cookies.get("ui_lang"), "白名单外值写了脏 cookie"


@pytest.mark.parametrize("header,want", [
    ("ja-JP,ja;q=0.9,en;q=0.8", "ja"),
    ("ko", "ko"),
    ("fr;q=0.3,de", "de"),          # q 高者胜
    ("pt-BR;q=0.9,es;q=0.9", "pt"),  # 同 q 取先出现
    ("*", ""),
    ("", ""),
])
def test_primary_lang_tag(header, want):
    from src.web.i18n_packs import primary_lang_tag
    assert primary_lang_tag(header) == want


def test_unsupported_demand_counter(client):
    """「想要却没有」：推断落空时记浏览器首选语言——语种扩张的需求分布数据源。"""
    from src.web.ui_lang_stats import get_ui_lang_stats
    st = get_ui_lang_stats()
    st.reset()
    client.get("/login", headers={"Accept-Language": "ja-JP,ja;q=0.9,en;q=0.1"})
    client.get("/login", headers={"Accept-Language": "ja-JP"})
    client.get("/login", headers={"Accept-Language": "ko-KR"})
    d = st.dump()
    # ja-JP,…,en;q=0.1 会被 en 兜住（negotiated）不计落空；纯 ja/ko 才计
    assert d["unsupported_by_lang"].get("ja", 0) == 1
    assert d["unsupported_by_lang"].get("ko", 0) == 1
    assert d["by_source"].get("negotiated", 0) >= 1


def test_ui_lang_stats_source_classification(client):
    """遥测来源分类：query/cookie/negotiated/default 各归各桶。"""
    from src.web.ui_lang_stats import get_ui_lang_stats
    st = get_ui_lang_stats()
    st.reset()
    client.get("/login", headers={"Accept-Language": "vi-VN"})           # negotiated
    client.get("/login?lang=th", headers={"Accept-Language": "vi-VN"})   # query
    client.cookies.set("ui_lang", "en")
    client.get("/login", headers={"Accept-Language": "vi-VN"})           # cookie
    client.cookies.delete("ui_lang")
    client.get("/login")                                                 # default
    d = st.dump()
    assert d["by_source"].get("negotiated", 0) >= 1
    assert d["by_source"].get("query", 0) >= 1
    assert d["by_source"].get("cookie", 0) >= 1
    assert d["by_source"].get("default", 0) >= 1
    assert d["negotiated_by_lang"].get("vi", 0) >= 1
