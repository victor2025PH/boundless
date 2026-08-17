"""URL 主题钉必须是窗口级的静态门禁（2026-08-15 事故沉淀）。

事故：`?theme=dark` 是**单个嵌入 webview** 的主题钉（桌面壳锁深色），而
`localStorage('cp_theme')` 是**整个 profile 共享**的治理键（后台日/夜滑块、
外观面板三态控件、服务端漫游都读写它）。把钉写进共享键 → 被钉窗口每次
applyTheme 又把它广播一遍 → 同分区里另一个后台窗口刚点的日/夜在 ~150ms 内
被盖回，表现是「后台切换白天晚上点了没反应」（点击有响应、埋点照常 +1、
所有单页门禁全绿，只有两窗口共享 localStorage 时才复现）。

行为侧由 `tools/verify_theme_pin_isolation.py`（真双标签页）压住；这里做**静态**
一层，因为那个浏览器门禁在实例没起时 SKIP，而这三个文件全是热更新直上生产。

守两条窄不变量：
 ① 三个读者都不许把 URL `theme` 参数写进 `cp_theme`；
 ② 钉必须参与「生效主题」计算（否则钉白钉，回到「深色壳里白聊天」那个老 bug）。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "src" / "web"

# 三个（也是全部）主题钉读者。新增读者请一并登记，否则它可以悄悄绕过本门禁。
_PIN_READERS = (
    _ROOT / "templates" / "workspace_base.html",
    _ROOT / "templates" / "unified_inbox.html",
    _ROOT / "static" / "workspace" / "appearance.js",
)

_SHARED_KEYS = ("cp_theme", "THEME_KEY", "KEY")


def _pin_var_names(src: str) -> set[str]:
    """找出「从 URL theme 参数取值」的那些变量名。"""
    names: set[str] = set()
    for m in re.finditer(
        r"(?:var|let|const)\s+(\w+)\s*=[^;\n]*?URLSearchParams\([^)]*\)\.get\(\s*['\"]theme['\"]",
        src,
    ):
        names.add(m.group(1))
    # `var x = null; ... x = _utp;` 两段式：把参与 dark/light 判定的中间变量也算进来
    for m in re.finditer(
        r"(?:var|let|const)\s+(\w+)\s*=\s*new URLSearchParams\([^)]*\)\.get\(\s*['\"]theme['\"]",
        src,
    ):
        names.add(m.group(1))
    for m in re.finditer(r"(\w+)\s*=\s*(\w+)\s*;", src):
        if m.group(2) in names:
            names.add(m.group(1))
    for m in re.finditer(
        r"(?:var|let|const)\s+(\w+)\s*=\s*\(?\s*window\.__cpThemePin\b", src
    ):
        names.add(m.group(1))
    return names


def test_url_theme_pin_never_written_to_shared_governance_key():
    """钉绝不许落进 cp_theme —— 那是跨窗口共享键，写进去就会盖别的窗口。"""
    offenders: list[str] = []
    for fp in _PIN_READERS:
        assert fp.exists(), f"主题钉读者不存在（改名了？请同步本门禁）：{fp}"
        src = fp.read_text(encoding="utf-8")
        pins = _pin_var_names(src)
        for m in re.finditer(r"setItem\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", src):
            key_expr, val_expr = m.group(1).strip(), m.group(2).strip()
            key_is_shared = any(k in key_expr for k in _SHARED_KEYS)
            if not key_is_shared:
                continue
            val_tokens = set(re.findall(r"\w+", val_expr))
            leaked = val_tokens & (pins | {"__cpThemePin"})
            if leaked:
                line = src[: m.start()].count("\n") + 1
                offenders.append(
                    f"{fp.name}:{line} setItem({key_expr}, {val_expr}) 把窗口级主题钉"
                    f"（{sorted(leaked)}）写进了跨窗口共享治理键"
                )
    assert not offenders, (
        "URL 主题钉泄漏进共享治理键 —— 会静默盖掉别的窗口刚做的日/夜切换：\n  "
        + "\n  ".join(offenders)
    )


def test_url_theme_pin_still_wins_inside_its_own_window():
    """钉必须仍参与本窗口的生效主题计算，否则「深色壳里白聊天」老 bug 回归。"""
    missing: list[str] = []
    for fp in _PIN_READERS:
        src = fp.read_text(encoding="utf-8")
        pins = _pin_var_names(src)
        if not pins:
            missing.append(f"{fp.name}: 找不到读取 URL theme 参数/window.__cpThemePin 的变量")
            continue
        # 钉变量必须在 setItem 之外被真正消费（返回/赋给 data-cp-theme/参与三元）。
        consumed = False
        for pin in pins:
            for m in re.finditer(rf"\b{re.escape(pin)}\b", src):
                seg = src[max(0, m.start() - 80): m.end() + 80]
                if "setItem" in seg and "return" not in seg:
                    continue
                if re.search(rf"(return|\?|\|\||setAttribute)[^\n]*\b{re.escape(pin)}\b", seg) \
                        or re.search(rf"\b{re.escape(pin)}\b[^\n]*(\?|\|\|)", seg):
                    consumed = True
                    break
            if consumed:
                break
        if not consumed:
            missing.append(f"{fp.name}: 主题钉读到了却没参与生效主题计算（钉白钉）")
    assert not missing, "主题钉在本窗口失效：\n  " + "\n  ".join(missing)


def test_explicit_pick_releases_the_pin_on_every_write_path():
    """钉是初始默认、不是锁：用户显式选档必须当场释放钉（2026-08-16 事故沉淀）。

    不释放的表现极具误导性——分段控件高亮会动、cp_theme 真写了、theme_ 埋点真 +1，
    但 eff() 钉优先 ⇒ data-cp-theme / cp-theme-css 一动不动，坐席报的就是
    「亮色/暗色点了没反应」（桌面壳 renderer 给嵌入 /workspace 强制加 ?theme=dark）。
    """
    base = (_ROOT / "templates" / "workspace_base.html").read_text(encoding="utf-8")
    inbox = (_ROOT / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    eng = (_ROOT / "static" / "workspace" / "appearance.js").read_text(encoding="utf-8")

    assert re.search(r"window\.cpReleaseThemePin\s*=", base), (
        "workspace_base 必须发布唯一释放入口 window.cpReleaseThemePin（各写路径别各自清钉）"
    )
    for name, src in (("workspace_base", base), ("unified_inbox", inbox)):
        for fn in ("cpSetTheme", "cpCycleTheme"):
            m = re.search(rf"window\.{fn}\s*=[\s\S]{{0,900}}?\n  \}};", src)
            if m is None:
                continue          # 该模板没有这条写路径（inbox 只覆写 cycle）
            assert "cpReleaseThemePin" in m.group(0), (
                f"{name}.{fn} 没释放 URL 主题钉 —— 被钉窗口里点了主题画面不会变"
            )

    # 引擎单一写路径：释放必须排在 was===mode 的空切档短路**之前**——
    # 「已是 auto + 钉住 dark」正是「点了没反应」最典型的一档，短路后就再没机会释放。
    m = re.search(r"function setNightMode\(mode\)\s*\{([\s\S]{0,700}?)\n  \}", eng)
    assert m, "appearance.js 找不到 setNightMode（改名了？请同步本门禁）"
    body = m.group(1)
    assert "releasePin()" in body, "setNightMode 必须释放 URL 主题钉（它是主题的单一写路径）"
    assert body.index("releasePin()") < body.index("was === mode"), (
        "releasePin() 必须在 was === mode 空切档短路之前 —— 否则「已是 auto + 钉住 dark」"
        "点『跟随系统』永远解不开钉"
    )


def test_pin_release_flag_is_window_scoped_not_partition_wide():
    """释放标记只能进 sessionStorage：钉是窗口级的，释放也必须是窗口级的。

    用 localStorage 存释放标记 = 一个窗口点亮色顺手解掉同分区**所有**窗口的钉，
    退回「深色壳里白聊天」那个老 bug（钉本来就是为它而存在）。
    """
    offenders: list[str] = []
    for fp in (_ROOT / "templates" / "workspace_base.html",
               _ROOT / "static" / "workspace" / "appearance.js"):
        src = fp.read_text(encoding="utf-8")
        assert "cp_theme_pin_off" in src, f"{fp.name}: 释放标记键名漂了（应为 cp_theme_pin_off）"
        for m in re.finditer(r"localStorage\.(?:set|get)Item\(\s*([^,)]+)", src):
            expr = m.group(1)
            if "PIN_OFF" in expr or "pin_off" in expr:
                line = src[: m.start()].count("\n") + 1
                offenders.append(f"{fp.name}:{line} 用 localStorage 存/读释放标记")
    assert not offenders, (
        "主题钉释放标记落进了跨窗口共享的 localStorage：\n  " + "\n  ".join(offenders)
    )


def test_pin_is_published_and_consumed_across_the_two_templates():
    """钉的跨模板传递契约：workspace_base 发布 window.__cpThemePin，unified_inbox 消费它。"""
    base = (_ROOT / "templates" / "workspace_base.html").read_text(encoding="utf-8")
    inbox = (_ROOT / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    assert "window.__cpThemePin" in base, (
        "workspace_base 必须把 URL 主题钉发布成 window.__cpThemePin（子模板与引擎靠它取钉）"
    )
    assert "window.__cpThemePin" in inbox, (
        "unified_inbox 必须消费 window.__cpThemePin，别各自再解析一遍 URL（两处解析必然漂移）"
    )
