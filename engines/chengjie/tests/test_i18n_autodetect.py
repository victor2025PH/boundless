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


@pytest.mark.parametrize("referer,want", [
    # 桌面壳首帧钉的 ?lang= 必须被剥掉，其余参数保留
    ("http://127.0.0.1:18799/workspace?lang=zh_hant&theme=dark", "/workspace?theme=dark"),
    ("http://h/workspace?theme=dark&lang=vi&conv=abc", "/workspace?theme=dark&conv=abc"),
    ("http://h/workspace?lang=en", "/workspace"),
    ("http://h/login", "/login"),
    # next 里嵌套的 lang 也剥（登录页上切语言 → 登录后回跳别再钉回去）
    ("http://h/login?next=%2Fworkspace%3Flang%3Dzh_hant%26theme%3Ddark",
     "/login?next=%2Fworkspace%3Ftheme%3Ddark"),
    ("http://h/login?next=%2Fworkspace%3Flang%3Den", "/login?next=%2Fworkspace"),
    # 无 / 坏 Referer → 首页；回 /set_lang 自身成环 → 首页；开放重定向只取站内 path
    ("", "/"),
    (None, "/"),
    ("http://h/set_lang?lang=zh", "/"),
    ("https://evil.com/phish?lang=zh", "/phish"),
    ("garbage", "/"),
])
def test_set_lang_redirect_target(referer, want):
    from src.web.login_redirect import set_lang_redirect_target
    assert set_lang_redirect_target(referer) == want


def test_set_lang_redirect_strips_query_so_cookie_wins(client):
    """切语言不生效事故（2026-09-12）端到端：页面 URL 钉着 ?lang=zh_hant，点「简体」→
    /set_lang 写 cookie 后回跳地址不得再带 lang，随后页面按 cookie 渲染简体。"""
    r = client.get("/set_lang?lang=zh", follow_redirects=False,
                   headers={"Referer": "http://testserver/login?lang=zh_hant&theme=dark"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?theme=dark"
    assert client.cookies.get("ui_lang") == "zh"
    assert _html_lang(client, path=r.headers["location"],
                      headers={"Accept-Language": "zh-TW"}) == "zh-CN"
    # auto 同样剥 lang：回到跟随系统后页面按 Accept-Language 走
    r = client.get("/set_lang?lang=auto", follow_redirects=False,
                   headers={"Referer": "http://testserver/login?lang=zh&theme=dark"})
    assert r.headers["location"] == "/login?theme=dark"
    assert _html_lang(client, path=r.headers["location"],
                      headers={"Accept-Language": "zh-TW"}) == "zh-Hant"


def test_lang_source_exposed_to_page(auth_client):
    """语言菜单状态单源：含 _i18n_bootstrap 的页面拿到 WS_LANG_SRC=query/cookie/negotiated/default
    （2026-09-12；此前 ✓ 看 query 派生的 WS_LANG、跟随态看 cookie 有无，可同真同假）。"""
    import re
    client = auth_client
    # 登录回填可能已写 ui_lang cookie（语言跟人走）——先清掉，从无显式选择起测
    client.cookies.delete("ui_lang")

    def _src(path="/workspace", **kw):
        r = client.get(path, **kw)
        assert r.status_code == 200, r.status_code
        m = re.search(r'window\.WS_LANG_SRC = window\.WS_LANG_SRC \|\| "([a-z]*)"', r.text)
        assert m, "页面未下发 WS_LANG_SRC"
        return m.group(1)

    assert _src("/workspace?lang=th", headers={"Accept-Language": "vi-VN"}) == "query"
    assert _src(headers={"Accept-Language": "vi-VN"}) == "negotiated"
    assert _src(headers={"Accept-Language": "fr-FR"}) == "default"
    client.cookies.set("ui_lang", "en")
    assert _src(headers={"Accept-Language": "vi-VN"}) == "cookie"
    client.cookies.delete("ui_lang")
    # 提示词条 zh/en 齐备（query 钉住时菜单顶部说明）
    from src.web.web_i18n import get_translations
    assert get_translations("zh").get("base.lang.pinned_hint")
    assert get_translations("en").get("base.lang.pinned_hint")


@pytest.mark.parametrize("referer,nxt,want", [
    # next 优先于 Referer，剥 lang，保 hash（工作台会话/标签 hash 路由）
    ("http://x/workspace?lang=zh_hant&theme=dark", "/workspace?lang=zh_hant&theme=dark#conv=abc",
     "/workspace?theme=dark#conv=abc"),
    ("http://x/login", "/workspace#tab=inbox", "/workspace#tab=inbox"),
    # next 非法（外站 / 协议相对 / 反斜杠 / 自身成环）→ 退回 Referer 规则
    ("http://x/personas?lang=en", "https://evil.com/", "/personas"),
    ("http://x/personas", "//evil.com/x", "/personas"),
    ("http://x/personas", "/set_lang?lang=zh", "/personas"),
    ("", "", "/"),
    # hash 里带控制字符 → 丢 hash 不丢路径
    ("", "/workspace#a\x01b", "/workspace"),
])
def test_set_lang_redirect_target_prefers_next_and_keeps_hash(referer, nxt, want):
    from src.web.login_redirect import set_lang_redirect_target
    assert set_lang_redirect_target(referer, nxt) == want


def test_set_lang_next_param_end_to_end(client):
    r = client.get("/set_lang?lang=en&next=" + "/login?lang=zh_hant%26theme=dark%23sec",
                   follow_redirects=False,
                   headers={"Referer": "http://testserver/login?lang=zh_hant"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?theme=dark#sec"
    assert client.cookies.get("ui_lang") == "en"


def test_lang_coverage_exposed_and_drives_beta(client):
    """β/覆盖说明由服务端覆盖率驱动（不再手写）：页面下发 WS_LANG_COVERAGE；
    机翻语种 <0.9、繁体 ≥0.9；zh/en 恒 1。"""
    import json
    import re
    from src.web.i18n_packs import EXTRA_LANGS
    from src.web.web_i18n import get_ui_lang_coverage
    cov = get_ui_lang_coverage()
    assert set(cov) >= set(EXTRA_LANGS) | {"zh", "en"}
    assert cov["zh"] == 1.0 and cov["en"] == 1.0
    assert cov["zh_hant"] >= 0.9, cov
    assert all(0.0 <= v <= 1.0 for v in cov.values())
    r = client.get("/login")
    m = re.search(r'window\.WS_LANG_COVERAGE = window\.WS_LANG_COVERAGE \|\| (\{[^\n]*\});', r.text)
    assert m, "页面未下发 WS_LANG_COVERAGE"
    assert json.loads(m.group(1))["zh_hant"] == cov["zh_hant"]
    assert "window.wsSwitchLang = function" in r.text
    # 新词条 zh/en 齐备
    from src.web.web_i18n import get_translations
    for k in ("base.lang.follow_now", "base.lang.switched_to"):
        assert "{lang}" in get_translations("zh")[k] and "{lang}" in get_translations("en")[k]


def test_ui_lang_native_covers_all_ui_langs():
    """母语自称表与白名单同步（切换入口尾注用；漏一语就显示语言码）。"""
    from src.web.i18n_packs import UI_LANGS, UI_LANG_NATIVE
    assert set(UI_LANG_NATIVE) == set(UI_LANGS)
    assert all(v.strip() for v in UI_LANG_NATIVE.values())


def test_lang_switch_entry_unified_on_auth_and_admin_pages(client, auth_client):
    """三套切换 UI 收敛（2026-09-12）：登录/初始化/管理后台的语言入口都挂六语选择器
    （wsToggleLang）并显示当前语言母语自称，不再是 zh⇄en 二元 'EN'/'ZH' 硬编码。"""
    import re
    r = client.get("/login?lang=vi")
    assert r.status_code == 200
    assert "window.wsToggleLang = function" in r.text, "登录页未 include _i18n_bootstrap"
    assert re.search(r'onclick="if\(window\.wsToggleLang\)\{event\.preventDefault\(\);window\.wsToggleLang\(event\);\}"', r.text)
    assert "(Tiếng Việt)" in r.text
    assert "(EN)" not in r.text and "(ZH)" not in r.text
    # 管理后台外壳（base.html 用户菜单）——/personas 继承 base.html
    r = auth_client.get("/personas?lang=zh_hant")
    assert r.status_code == 200, r.status_code
    assert "ud-item" in r.text and "wsToggleLang(event)" in r.text
    assert "(繁體中文)" in r.text
    assert "(EN)" not in r.text and "(ZH)" not in r.text


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


def test_ui_lang_stats_switch_back_window():
    """同会话切到 X 后窗口内切回原语＝X 的放弃信号；窗口外/不同会话/非回切不计。"""
    from src.web.ui_lang_stats import UiLangStats
    st = UiLangStats()
    assert st.record_switch("s1", "zh", "vi", now=1000.0) is False
    assert st.record_switch("s1", "vi", "zh", now=1000.0 + 120) is True      # 2 分钟内切回
    assert st.record_switch("s2", "zh", "th", now=2000.0) is False
    assert st.record_switch("s2", "th", "zh", now=2000.0 + 600) is False     # 超窗口
    assert st.record_switch("s3", "zh", "id", now=3000.0) is False
    assert st.record_switch("s3", "id", "en", now=3000.0 + 10) is False      # 换到第三语不算回切
    assert st.record_switch("", "zh", "vi", now=4000.0) is False             # 无会话键只计切换
    d = st.dump()
    assert d["switch_back_within_5m_from"] == {"vi": 1}
    assert d["switches_to"]["vi"] == 2 and d["switches_to"]["zh"] == 2
    # 会话记忆有上限（FIFO），不会无界增长
    for i in range(st._MAX_SWITCH_SESSIONS + 50):
        st.record_switch(f"x{i}", "zh", "en", now=5000.0 + i)
    assert len(st._last_switch) <= st._MAX_SWITCH_SESSIONS


def test_ui_lang_stats_query_cookie_conflict(client):
    """?lang= 压过了不同的 cookie → 冲突计数（按 query 语种分桶）；/set_lang 自身不算。"""
    from src.web.ui_lang_stats import get_ui_lang_stats
    st = get_ui_lang_stats()
    st.reset()
    client.cookies.set("ui_lang", "zh")
    client.get("/login?lang=zh_hant")           # 冲突：URL 钉繁体，用户选的是简体
    client.get("/login?lang=zh")                # 一致：不算
    client.get("/set_lang?lang=vi", follow_redirects=False)   # 切换动作：不算冲突
    d = st.dump()
    assert d["query_cookie_conflicts_by_query_lang"] == {"zh_hant": 1}
    assert d["switches_to"].get("vi") == 1
    client.cookies.delete("ui_lang")
