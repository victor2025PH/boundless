# -*- coding: utf-8 -*-
"""品牌令牌桥不变量门禁（2026-07-30）。

背景
----
坐席端 / 管理端 / 认证页的主色已统一到无界品牌智连蓝（platform/brand 的
`--bl-growth`）。收口过程中暴露出两类**静默失效**——它们的共同点是「看起来
还挺正常，所以没人发现」，正是最该用门禁钉住的那种：

  A. **令牌引用写了、令牌却从未定义**：base.html 从未定义 `--accent`，而派生页
     有 16 处**无 fallback** 的 `var(--accent)`。CSS 里 var() 未定义且无 fallback
     时整条声明在计算值时失效 → 选中态没有强调色、`.pcard-focused` 的 outline
     不显示（键盘焦点框看不见，属可访问性缺陷）。补一行别名即修复。
  B. **后置覆盖被顺序打破**：`th-brand-bridge.css` 靠「排在 theme-tokens.css
     之后、同特异性后者胜」来把 `--th-*-brand*` 覆盖成品牌色。若有人重排
     `<head>`，覆盖会静默失效、颜色悄悄退回旧紫蓝 #5b7cf6。

为什么不直接改 theme-tokens.css
----
`static/theme-tokens.css` 是 `tools/inline_color_codemod.py --emit-css` 的产物，
其设计不变量是「亮色值恒 = 迁移前的字面量本身」，并由
`test_template_inline_color_ratchet.py` 校验「模板 fallback ≡ :root 亮值」。
为换品牌色去手改它或改生成器色表，会破坏那个不变量并把该门禁弄红。
所以走**后置覆盖**（th-brand-bridge.css）。

⚠️ 由此产生三层语义，勿混淆（也勿把 bridge 的值写回 theme-tokens.css）：
    theme-tokens.css :root 亮值 = 旧值（供 ratchet 与模板 fallback 对齐）
    th-brand-bridge  :root      = 品牌值（**实际渲染值**）
    模板里的 fallback            = 旧值（仅当变量未定义时才会用到，实际不走）

本文件守的不变量
----
 1. `brand.css` + `brand-fonts.css` 必须挂在全部四个根壳
    （漏挂 → `--bl-*` 全部回落 fallback，视觉「差不多对」故属静默降级）；
 2. `th-brand-bridge.css` 必须挂在 `theme-tokens.css` **之后**（见上文 B）；
 3. bridge 覆盖的令牌集合 ≡ theme-tokens.css **亮色块**里 `--th-*-brand*` 全集
    （双向校验：漏覆盖 → codemod 将来新增的 brand 令牌保留旧紫蓝；
      多覆盖 → 令牌改名后 bridge 里那条变死代码，两种都是静默失效）；
 4. 模板内不得再内联 `@font-face` 规则（曾因内联在 workspace_base 导致
    登录 / 初始化页拿不到品牌字；已抽成 brand-fonts.css 单一源，防回归）；
 5. `base.html` 必须定义 `--accent` 别名（见上文 A）；
 6. `brand-fonts.css` 的 `@font-face` src 必须用**相对** `fonts/...`
    （绝对 `/static/brand/fonts/...` 在 Electron `file://` 宿主下 404）；
 7. 桌面壳 `desktop/renderer/index.html` 必须挂 brand.css + brand-fonts.css，
    且排在 copilot 主题表之前（theme 的 `--cp-accent: var(--bl-growth)` 才有值）。

口径（刻意窄）
----
  - 只检查根壳模板、品牌 CSS、桌面壳入口，不全站扫描；
  - 第 4 条用 `@font-face\\s*\\{` 匹配**真规则**，不误伤注释里提到的字样。
"""
from __future__ import annotations

import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parents[1]
_TPL = _REPO / "src" / "web" / "templates"
_STATIC = _REPO / "src" / "web" / "static"
_DESKTOP_INDEX = _REPO / "desktop" / "renderer" / "index.html"

THEME_CSS = _STATIC / "theme-tokens.css"
BRIDGE_CSS = _STATIC / "brand" / "th-brand-bridge.css"
FONTS_CSS = _STATIC / "brand" / "brand-fonts.css"

# 四个根壳：管理台 / 坐席工作台 / 登录 / 初始化。都需要品牌令牌与品牌字。
SHELLS = ("base.html", "workspace_base.html", "login.html", "setup.html")
# 只有这两个壳引入 theme-tokens.css，故只有它们需要 bridge 的顺序保证。
SHELLS_WITH_THEME_TOKENS = ("base.html", "workspace_base.html")

_BRAND_TOKEN = re.compile(r"--th-[a-z]+-brand[0-9]*")
_FONT_FACE_RULE = re.compile(r"@font-face\s*\{")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_css_comments(css: str) -> str:
    """剥掉 CSS 注释后再解析。

    被检查的 CSS 在说明性注释里会**故意**提到旧色值与令牌名（用于解释「为什么
    要替换掉它」）；不剥离就会把文档本身判成违规。这类「口径太宽误伤注释」是本
    仓门禁反复踩过的坑——参见 inline_color_ratchet 先剥 var() 段、window.T 键
    门禁跳过字符串/注释的做法。同理 :root 块内的注释若提到令牌名，也会污染
    令牌集合统计，故所有解析统一基于剥注释后的文本。
    """
    return _CSS_COMMENT.sub("", css)


def _root_block(css: str) -> str:
    """取 `:root{ ... }` 块正文（先剥注释；CSS 以单独一行的 `}` 收尾）。"""
    m = re.search(r":root\s*\{(.*?)\n\}", _strip_css_comments(css), re.DOTALL)
    assert m, "未能定位 :root 块"
    return m.group(1)


def test_brand_css_linked_in_all_shells():
    """品牌令牌与品牌字必须挂在四个根壳上（漏挂＝静默降级为 fallback）。"""
    for shell in SHELLS:
        text = _read(_TPL / shell)
        assert "/static/brand/brand.css" in text, (
            f"{shell} 未引入 brand.css —— 所有 var(--bl-*) 会静默回落 fallback，"
            f"品牌色改 tokens.json 将不生效"
        )
        assert "/static/brand/brand-fonts.css" in text, (
            f"{shell} 未引入 brand-fonts.css —— 该壳拿不到品牌拉丁字 Montserrat，"
            f"与其它壳字形不一致"
        )


def test_th_brand_bridge_loads_after_theme_tokens():
    """bridge 必须排在 theme-tokens 之后，否则后置覆盖静默失效。"""
    for shell in SHELLS_WITH_THEME_TOKENS:
        text = _read(_TPL / shell)
        i_tokens = text.find("/static/theme-tokens.css")
        i_bridge = text.find("/static/brand/th-brand-bridge.css")
        assert i_tokens != -1, f"{shell} 未引入 theme-tokens.css"
        assert i_bridge != -1, f"{shell} 未引入 th-brand-bridge.css"
        assert i_bridge > i_tokens, (
            f"{shell}: th-brand-bridge.css 必须排在 theme-tokens.css 之后。"
            f"两者同为 :root 同特异性，靠后者胜实现覆盖；顺序颠倒会让品牌色"
            f"静默退回旧紫蓝 #5b7cf6（无报错、无门禁红，只是颜色不对）"
        )


def test_bridge_covers_exactly_light_brand_tokens():
    """bridge 的 :root 覆盖集合必须与 theme-tokens 亮色块的 brand 令牌全集相等。

    漏覆盖 → codemod 将来新增的 brand 令牌仍是旧紫蓝；
    多覆盖 → 令牌被改名 / 删除后，bridge 里那条成为死代码。
    两者都不会报错，只会让品牌色悄悄不一致，故双向校验。
    """
    theme_tokens = set(_BRAND_TOKEN.findall(_root_block(_read(THEME_CSS))))
    bridge_tokens = set(_BRAND_TOKEN.findall(_root_block(_read(BRIDGE_CSS))))

    assert theme_tokens, "theme-tokens.css 亮色块里没有 --th-*-brand* 令牌，请复核正则"

    missing = theme_tokens - bridge_tokens
    assert not missing, (
        f"th-brand-bridge.css 漏覆盖这些 brand 令牌 {sorted(missing)} —— "
        f"它们会保留 theme-tokens 里的旧紫蓝值。若 theme-tokens 重新生成过，"
        f"把新令牌补进 bridge 的 :root 即可"
    )
    stale = bridge_tokens - theme_tokens
    assert not stale, (
        f"th-brand-bridge.css 覆盖了 theme-tokens 里已不存在的令牌 {sorted(stale)} —— "
        f"疑似令牌改名/删除后遗留的死代码，请同步清理"
    )


def test_bridge_has_no_legacy_periwinkle():
    """bridge 自身不得再出现旧紫蓝——它存在的唯一目的就是替换掉这个色。"""
    css = _strip_css_comments(_read(BRIDGE_CSS))
    assert "rgba(91,124,246" not in css and "#5b7cf6" not in css, (
        "th-brand-bridge.css 里出现了旧紫蓝 #5b7cf6 / rgba(91,124,246,…)，"
        "与本文件的目的相悖"
    )


def test_no_inline_font_face_in_templates():
    """模板内不得内联 @font-face 规则（单一源在 brand-fonts.css）。

    历史：@font-face 曾内联在 workspace_base.html，于是只有「继承工作台壳的
    页面」拿得到品牌字，login/setup 这类独立根模板仍只有 Inter —— 同一产品
    登录前后拉丁字形不是一套。抽成 brand-fonts.css 后要防回归。
    """
    offenders = []
    for p in sorted(_TPL.rglob("*.html")):
        if _FONT_FACE_RULE.search(_read(p)):
            offenders.append(p.relative_to(_TPL).as_posix())
    assert not offenders, (
        f"这些模板内联了 @font-face 规则：{offenders}。请改为 "
        f"<link rel=stylesheet href=/static/brand/brand-fonts.css>，"
        f"保持字形定义单一源"
    )


def test_base_defines_accent_alias():
    """base.html 必须定义 --accent（派生页有无 fallback 的 var(--accent) 依赖它）。

    未定义时那些声明整条失效：选中态没有强调色、.pcard-focused 的 outline
    不显示（键盘焦点不可见）。定义成主色别名可同时保证「随品牌与日夜主题跟随」。
    """
    text = _read(_TPL / "base.html")
    m = re.search(r"--accent\s*:\s*([^;]+);", text)
    assert m, (
        "base.html 未定义 --accent —— 派生页（如 personas.html）有 16 处"
        "无 fallback 的 var(--accent)，未定义时整条属性失效"
    )
    value = m.group(1).strip()
    assert "--p" in value, (
        f"--accent 期望是主色别名 var(--p)，实测为 {value!r}。"
        f"写成固定色值会脱离品牌 SSOT 与日夜主题"
    )


def test_brand_fonts_use_relative_url():
    """@font-face src 必须相对本 CSS 解析（Electron file:// 下 /static/... 会 404）。"""
    css = _strip_css_comments(_read(FONTS_CSS))
    assert "url('fonts/Montserrat-Latin-VF.woff2" in css, (
        "brand-fonts.css 须用相对路径 fonts/Montserrat-Latin-VF.woff2……"
        "（相对本文件解析：Web→/static/brand/fonts/…，Desktop→brand/fonts/…）"
    )
    assert "/static/brand/fonts/" not in css, (
        "brand-fonts.css 禁止绝对 /static/brand/fonts/…——"
        "桌面壳 Electron 宿主不是站点根，绝对路径会静默 404、落回系统字"
    )


def test_desktop_shell_links_brand_before_copilot_theme():
    """桌面壳须挂品牌令牌/字，且排在 theme-dark 之前，--cp-accent 才能接到 --bl-growth。"""
    text = _read(_DESKTOP_INDEX)
    i_brand = text.find("brand/brand.css")
    i_fonts = text.find("brand/brand-fonts.css")
    i_theme = text.find("shared/copilot/theme-dark.css")
    assert i_brand != -1, "desktop/renderer/index.html 未引入 brand/brand.css"
    assert i_fonts != -1, "desktop/renderer/index.html 未引入 brand/brand-fonts.css"
    assert i_theme != -1, "desktop/renderer/index.html 未引入 theme-dark.css"
    assert i_brand < i_theme and i_fonts < i_theme, (
        "brand.css / brand-fonts.css 必须排在 theme-dark.css 之前；"
        "theme 里 --cp-accent: var(--bl-growth) 依赖先定义的 --bl-*，"
        "顺序颠倒会静默落到 fallback（颜色对了但改 tokens.json 不跟）"
    )


def test_detectors_actually_detect():
    """探测器有效性自证：用**构造数据**确认上面几条真能抓到违规。

    门禁全绿有两种可能：不变量真的成立，或者检测逻辑根本抓不到东西。后者是
    「永远绿的摆设」，比没有门禁更糟（给人虚假的安全感）。本仓对评测类代码
    一贯要求自证（例：bazi_chart_eval 篡改金标必 FAIL）。

    刻意用内存里的假数据而不是临时改真文件——本仓模板/CSS 是**热更新直达生产**，
    为了跑一次测试去破坏线上样式（哪怕几秒）不划算。
    """
    # 1) 顺序检测：bridge 排在 tokens 之前时，索引比较必须判为违规
    bad_order = (
        '<link rel="stylesheet" href="/static/brand/th-brand-bridge.css">\n'
        '<link rel="stylesheet" href="/static/theme-tokens.css">'
    )
    assert bad_order.find("/static/brand/th-brand-bridge.css") < bad_order.find(
        "/static/theme-tokens.css"
    ), "顺序检测失效：本该判定为「bridge 在 tokens 之前」"

    # 2) 注释剥离既要挡住文档里的旧色，也不能放过真实声明里的旧色
    assert "#5b7cf6" not in _strip_css_comments("/* 说明：替换掉 #5b7cf6 */\n:root{--x:#1e8cf2;}")
    assert "#5b7cf6" in _strip_css_comments(":root{--th-bg-brand:#5b7cf6;}"), (
        "剥注释后仍须能发现真实声明中的旧色，否则该检测形同虚设"
    )

    # 3) 令牌名提取：只认 --th-*-brand*，不误收其它 --th-* 令牌
    found = _BRAND_TOKEN.findall(
        "--th-bg-brand08:a; --th-bd-brand20:b; --th-ink-blue5:c; --th-bg-amber12:d;"
    )
    assert found == ["--th-bg-brand08", "--th-bd-brand20"], f"令牌提取口径异常：{found}"

    # 4) :root 块提取必须只取亮色块，不把 [data-theme=dark] 的声明混进来
    sample = ":root{\n  --th-bg-brand:#111;\n}\n[data-theme=\"dark\"]{\n  --th-bg-brand:#999;\n}\n"
    assert _root_block(sample).strip() == "--th-bg-brand:#111;", (
        "亮色块提取越界，会把暗色声明算进覆盖集合"
    )

    # 5) @font-face 规则匹配：认真规则、不误伤注释里提到的字样
    assert _FONT_FACE_RULE.search("@font-face{font-family:'X';}")
    assert not _FONT_FACE_RULE.search("<!-- @font-face 已收口到 brand-fonts.css -->")

    # 6) 相对字体路径检测：绝对 /static 必须被抓到
    assert "/static/brand/fonts/" in "src:url('/static/brand/fonts/X.woff2')"
    assert "/static/brand/fonts/" not in "src:url('fonts/X.woff2')"

    # 7) 桌面顺序检测：brand 排在 theme 后必须判违规
    bad_desk = (
        '<link href="shared/copilot/theme-dark.css">\n'
        '<link href="brand/brand.css">'
    )
    assert bad_desk.find("brand/brand.css") > bad_desk.find(
        "shared/copilot/theme-dark.css"
    ), "桌面顺序检测失效"
