# -*- coding: utf-8 -*-
"""主题调色板 WCAG 对比度门禁（2026-08-04 白天模式可读性收口配套）。

背景
----
本站暗色是默认主题，白天模式的调色板长期是"暗色收口时字节级保留"的历史字面量，
从未按白底校准——2026-08-04 实测：三级文字 #9ca3af 白底 2.5:1、品牌蓝小字 3.4:1、
状态色配 12% 软底 2.1~3.8:1，全线低于 WCAG AA（正文 4.5:1）。当批已把
base.html :root 亮色板与 theme-tokens.css 灰阶亮值收口到达标值。

本门禁把"哪个前景色配哪个背景面"这组**设计契约**变成可执行断言：
- 改 base.html 调色板或 tools/inline_color_codemod.py 表（→ theme-tokens.css）时，
  任何把已登记搭配拉回阈值以下的值会被点名；
- 两主题分别校验（亮色修复不许以暗色回退为代价，反之亦然）；
- 半透明色按"层叠合成"算实效色（12% 软底先合到卡面上再算对比度），与浏览器渲染同口径。

口径
----
- 正文/交互小字 4.5:1（WCAG AA normal text）；图形/装饰 3.0:1。
- var(--brand-x,#fallback) 按 fallback 解析：brand.css 是品牌 SSOT，模板侧
  fallback 由品牌桥约定恒等于品牌真值，此处不解析 brand.css（保持零依赖）。
- `_KNOWN_DEBT`：历史遗留、当批刻意不动的搭配（如暗色侧栏 dim 分组标签），
  按当前实测值钉"不许更差"下限并注明去向——债务可见，不装绿。
"""
from __future__ import annotations

import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parents[1]
_BASE_HTML = _REPO / "src/web/templates/base.html"
_TOKENS_CSS = _REPO / "src/web/static/theme-tokens.css"
# 第三套体系：右栏副驾（shared/copilot），自成一套 --cp-*。2026-08-28 前不在本
# 门禁覆盖内 → 整个右栏漏掉了 2026-08-16 那次全站提亮，实测比它所在的外壳暗
# 一到两档（tiny 贴 surface-2 只有 4.19:1，已低于 AA）。纳入后回退会被点名。
_CP_DARK_CSS = _REPO / "shared/copilot/theme-dark.css"
_CP_LIGHT_CSS = _REPO / "shared/copilot/theme-light.css"
_CP_TOKENS_CSS = _REPO / "shared/copilot/tokens.css"
# 第四套：工作台外壳 --tk-*（workspace_base.html）。admin-theme.mdc 要求它与
# base.html/codemod 表「三处联动」，但此前只有前两处进门禁——联动全靠人记。
_WORKSPACE_HTML = _REPO / "src/web/templates/workspace_base.html"

_TEXT_AA = 4.5   # 正文/小字（含 .7~.85rem 的 chips/徽章/meta）
_GRAPHIC = 3.0   # 图形件（状态点/描边/大标题）

# ---------------------------------------------------------------------------
# CSS 变量抽取（轻量：只认 `--x: value;` 声明，剥注释；值保留原文）
# ---------------------------------------------------------------------------

def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _block(css: str, header_re: str) -> str:
    m = re.search(header_re + r"\s*\{(.*?)\}", css, re.S)
    assert m, f"缺 {header_re} 块"
    return m.group(1)


def _vars_of(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block):
        out[m.group(1)] = m.group(2).strip()
    return out


def _load_palettes() -> tuple[dict, dict]:
    """返回 (light, dark)：base.html 语义变量 + theme-tokens.css --th-* 合并视图。"""
    base = _strip_comments(_BASE_HTML.read_text(encoding="utf-8"))
    light = _vars_of(_block(base, r":root"))
    dark_overrides = _vars_of(_block(base, r'\[data-theme="dark"\]'))
    tok = _TOKENS_CSS.read_text(encoding="utf-8")
    tok_light = _vars_of(_block(_strip_comments(tok), r":root"))
    tok_dark = _vars_of(_block(_strip_comments(tok), r'\[data-theme="dark"\],\[data-cp-theme="dark"\]'))
    light = {**light, **tok_light}
    dark = {**light, **dark_overrides, **tok_dark}  # 暗色=亮色基础上的覆写（CSS 级联同口径）
    return light, dark


# ---------------------------------------------------------------------------
# 颜色数学（sRGB → 相对亮度 → WCAG 对比度；alpha 按层合成）
# ---------------------------------------------------------------------------

def _resolve(value: str, palette: dict[str, str], depth: int = 0) -> str:
    """解析变量引用：var(--x,fb) → palette[--x]（缺则 fb）；纯 --x 键直接查表。"""
    v = value.strip()
    assert depth < 8, f"变量解析过深: {value}"
    if v.startswith("--"):
        assert v in palette, f"调色板缺变量 {v}"
        return _resolve(palette[v], palette, depth + 1)
    m = re.fullmatch(r"var\((--[a-z0-9-]+)\s*(?:,\s*(.*))?\)", v, re.S)
    if m:
        name, fb = m.group(1), (m.group(2) or "").strip()
        if name in palette:
            return _resolve(palette[name], palette, depth + 1)
        assert fb, f"变量 {name} 未定义且无 fallback"
        return _resolve(fb, palette, depth + 1)
    # color-mix 折算（2026-08-23 白标批）：品牌淡底/描边改成
    # color-mix(in srgb, <c> P%, transparent) 引令牌（换肤自动跟随，verify_brand_reskin
    # 实锤 --ps 一个 rgba 字面量曾泄漏 472 处）。等价关系 ≡ rgba(c, P/100 × αc)，
    # 对比度数学零变化；只支持 over-transparent 形态（当前全站唯一用法）。
    mx = re.fullmatch(
        r"color-mix\(\s*in\s+srgb\s*,\s*(.+?)\s+([\d.]+)%\s*,\s*transparent\s*\)",
        v, re.S | re.I)
    if mx:
        cr, cg, cb, ca = _parse_rgba(_resolve(mx.group(1), palette, depth + 1))
        return f"rgba({cr:g},{cg:g},{cb:g},{ca * float(mx.group(2)) / 100:g})"
    return v


def _parse_rgba(color: str) -> tuple[float, float, float, float]:
    c = color.strip().lower()
    if c.startswith("#"):
        h = c[1:]
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return float(r), float(g), float(b), a
    m = re.fullmatch(r"rgba?\(([^)]+)\)", c)
    assert m, f"无法解析颜色: {color!r}"
    parts = [p.strip() for p in m.group(1).split(",")]
    r, g, b = (float(parts[i]) for i in range(3))
    a = float(parts[3]) if len(parts) > 3 else 1.0
    return r, g, b, a


def _composite(layers: list[str], palette: dict[str, str]) -> tuple[float, float, float]:
    """从底到顶合成不透明实效色；首层须不透明（页面底）。"""
    base: tuple[float, float, float] | None = None
    for spec in layers:
        r, g, b, a = _parse_rgba(_resolve(spec, palette))
        if base is None:
            assert a >= 0.999, f"底层必须不透明: {spec}"
            base = (r, g, b)
            continue
        base = tuple(a * ch + (1 - a) * bs for ch, bs in zip((r, g, b), base))  # type: ignore[assignment]
    assert base is not None
    return base  # type: ignore[return-value]


def _lum(rgb: tuple[float, float, float]) -> float:
    def f(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(fg_spec: str, bg_layers: list[str], palette: dict[str, str]) -> float:
    bg = _composite(bg_layers, palette)
    r, g, b, a = _parse_rgba(_resolve(fg_spec, palette))
    fg = tuple(a * ch + (1 - a) * bs for ch, bs in zip((r, g, b), bg))
    l1, l2 = _lum(fg), _lum(bg)  # type: ignore[arg-type]
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# 登记的设计契约：(说明, 前景, [背景层 底→顶], 阈值)
# 背景层写"真实渲染栈"：软底徽章 = 页面底 → 卡面 → 12% 软底。
# ---------------------------------------------------------------------------

_PAIRS_BOTH_THEMES = [
    # 文字三级 × 三种常见面（正文契约的核心）
    ("一级文字@卡片", "--t", ["--bg", "--card"], _TEXT_AA),
    ("二级文字@卡片", "--t2", ["--bg", "--card"], _TEXT_AA),
    ("三级文字@卡片（meta/时间戳/空态）", "--t3", ["--bg", "--card"], _TEXT_AA),
    ("一级文字@页面底", "--t", ["--bg"], _TEXT_AA),
    ("二级文字@页面底", "--t2", ["--bg"], _TEXT_AA),
    ("三级文字@页面底", "--t3", ["--bg"], _TEXT_AA),
    ("三级文字@输入面", "--t3", ["--bg", "--input"], _TEXT_AA),
    # 品牌主色做小字：卡面上可用 --p；**页面底**比卡面深一档，其上主色文字必须
    # 用 --pd（真浏览器实测：激活 tab 用 --p 在 --bg 上只有 4.18）。
    ("主色文字@卡片", "--p", ["--bg", "--card"], _TEXT_AA),
    ("主色深档@页面底（激活tab/页头链接）", "--pd", ["--bg"], _TEXT_AA),
    ("主色深档@品牌软底（tag-chip/.b-p）", "--pd", ["--bg", "--card", "--ps"], _TEXT_AA),
    # 状态色文字 × 同族 12% 软底（.b-g/.b-a/.b-r/.b-u 徽章家族）
    ("成功色@绿软底", "--green", ["--bg", "--card", "--gs"], _TEXT_AA),
    ("警示色@琥珀软底", "--amber", ["--bg", "--card", "--as"], _TEXT_AA),
    ("危险色@红软底", "--red", ["--bg", "--card", "--rs"], _TEXT_AA),
    ("重要色@紫软底", "--purple", ["--bg", "--card", "--prs"], _TEXT_AA),
    ("信息蓝@卡片", "--blue", ["--bg", "--card"], _TEXT_AA),
    # 状态色当图形（导航点/趋势箭头）
    ("成功色图形@卡片", "--green", ["--bg", "--card"], _GRAPHIC),
    ("警示色图形@卡片", "--amber", ["--bg", "--card"], _GRAPHIC),
]

_PAIRS_BOTH_THEMES += [
    # 侧栏三级文字（2026-08-04 P1.5：暗色 dim 档 .3→.48 收口后升为双主题契约；
    # sb-bg 暗色带 .98 alpha，统一垫 --bg）
    ("侧栏文字", "--sb-txt", ["--bg", "--sb-bg"], _TEXT_AA),
    ("侧栏高亮文字", "--sb-txt-hi", ["--bg", "--sb-bg"], _TEXT_AA),
    ("侧栏次级文字（分组标签/状态行）", "--sb-txt-dim", ["--bg", "--sb-bg"], _TEXT_AA),
    # on-primary 墨色（主色实底上的文字）：亮=白/暗=近黑，消费口 color:var(--p-ink,#fff)
    ("on-primary 墨色@主色面", "--p-ink", ["--bg", "--p"], _TEXT_AA),
    # 2026-08-16 提亮批补测对：二级文字也常落在输入面（表单说明/占位附注）
    ("二级文字@输入面", "--t2", ["--bg", "--input"], _TEXT_AA),
]

# ── 暗色提亮地板（2026-08-16「字与背景太接近」实录收口）──
# AA 4.5 是法定底线，但暗色下 4.5~5 的次级灰长时间盯着仍是「灰糊」观感；
# 本批把 --t2/--t3/--sb-* 与 --th-ink 灰阶各提一档后，用**高于 AA 的地板**
# 把成果钉死——提亮前的旧值（t3 卡面 5.0 / 输入面 4.6 / sb-txt 5.3）在这组
# 地板下全部会红，任何「顺手调暗一点」的回退会被立刻点名。仅暗色适用：
# 亮色板 2026-08-04 已按 AA 收口且白底灰阶再抬会伤层级，维持 4.5 契约。
_PAIRS_DARK_FLOORS = [
    ("暗色二级文字@卡面（提亮地板）", "--t2", ["--bg", "--card"], 8.0),
    ("暗色三级文字@卡面（提亮地板）", "--t3", ["--bg", "--card"], 5.5),
    ("暗色三级文字@输入面（提亮地板）", "--t3", ["--bg", "--input"], 5.5),
    ("暗色侧栏文字（提亮地板）", "--sb-txt", ["--bg", "--sb-bg"], 8.0),
    ("暗色侧栏次级文字（提亮地板）", "--sb-txt-dim", ["--bg", "--sb-bg"], 6.5),
    ("暗色 slate4 meta（提亮地板）", "--th-ink-slate4", ["--th-bg-surface"], 5.5),
    ("暗色 gray4 meta（提亮地板）", "--th-ink-gray4", ["--th-bg-surface"], 5.5),
    ("暗色 slate5 次级（提亮地板）", "--th-ink-slate5", ["--th-bg-surface"], 7.5),
    ("暗色 gray5 次级（提亮地板）", "--th-ink-gray5", ["--th-bg-surface"], 7.5),
]

_PAIRS_LIGHT_ONLY = [
    # 反白字@主色面：亮色 --p=品牌600，白字 4.6 达标；暗色见 _KNOWN_DEBT
    # （存量 white-on-primary 消费点未全部换 --p-ink 前，调色板级白字契约仍亮色单侧）。
    ("反白字@主色面（主按钮/开关轨道）", "#fff", ["--bg", "--p"], _TEXT_AA),
]

# --th-* 内联收口 token：文字灰阶/彩色文字系 × 其设计面
# （surface=浅色卡面 / inkpanel=刻意深底；彩色 500/600 档 2026-08-04 P1.5 收敛到 700 档）
_TOKEN_PAIRS_BOTH = [
    ("slate4 meta 文字@浅面", "--th-ink-slate4", ["--th-bg-surface"], _TEXT_AA),
    ("gray4 meta 文字@浅面", "--th-ink-gray4", ["--th-bg-surface"], _TEXT_AA),
    ("slate5 文字@浅面", "--th-ink-slate5", ["--th-bg-surface"], _TEXT_AA),
    ("gray5 文字@浅面", "--th-ink-gray5", ["--th-bg-surface"], _TEXT_AA),
    ("slate6 文字@浅面", "--th-ink-slate6", ["--th-bg-surface"], _TEXT_AA),
    ("gray7 文字@浅面", "--th-ink-gray7", ["--th-bg-surface"], _TEXT_AA),
    # 彩色文字系@浅面（有暗色覆写=浅面文字语义；两主题各自达标）
    ("红字@浅面", "--th-ink-red5", ["--th-bg-surface"], _TEXT_AA),
    ("红字6@浅面", "--th-ink-red6", ["--th-bg-surface"], _TEXT_AA),
    ("错误红4@浅面（wa 空态/加载失败）", "--th-ink-red4", ["--th-bg-surface"], _TEXT_AA),
    ("gh红@浅面（rpa 失配标记）", "--th-ink-ghred", ["--th-bg-surface"], _TEXT_AA),
    ("琥珀5@浅面", "--th-ink-amber5", ["--th-bg-surface"], _TEXT_AA),
    ("琥珀6@浅面", "--th-ink-amber6", ["--th-bg-surface"], _TEXT_AA),
    ("翠5@浅面", "--th-ink-emerald5", ["--th-bg-surface"], _TEXT_AA),
    ("翠6@浅面", "--th-ink-emerald6", ["--th-bg-surface"], _TEXT_AA),
    ("绿5@浅面", "--th-ink-green5", ["--th-bg-surface"], _TEXT_AA),
    ("绿6@浅面", "--th-ink-green6", ["--th-bg-surface"], _TEXT_AA),
    ("蓝5@浅面", "--th-ink-blue5", ["--th-bg-surface"], _TEXT_AA),
    ("蓝6@浅面", "--th-ink-blue6", ["--th-bg-surface"], _TEXT_AA),
    ("青6@浅面", "--th-ink-cyan6", ["--th-bg-surface"], _TEXT_AA),
    ("紫罗兰5@浅面", "--th-ink-violet5", ["--th-bg-surface"], _TEXT_AA),
    ("靛5@浅面", "--th-ink-indigo5", ["--th-bg-surface"], _TEXT_AA),
    ("紫5@浅面", "--th-ink-purple5", ["--th-bg-surface"], _TEXT_AA),
    # 图形系（图表线/图标描边，3:1 档；保 600 饱和度不压到 700）
    ("青5图形@浅面（analytics 折线）", "--th-ink-cyan5", ["--th-bg-surface"], _GRAPHIC),
    ("天蓝5图形@浅面（ai_studio 图标）", "--th-ink-sky5", ["--th-bg-surface"], _GRAPHIC),
    # 深面常量对（设计面即深底，两主题同值同面）
    ("浅粉字@深红实底（red2×bg-red8）", "--th-ink-red2", ["--th-bg-red8"], _TEXT_AA),
    ("浅青字@深青横幅（teal1×bg-teal8）", "--th-ink-teal1", ["--th-bg-teal8"], _TEXT_AA),
]

# ink×软底真实共现对（tmp 扫描 2026-08-04：只登记模板里实际存在的搭配）。
# 亮色单测：暗色的同族软底叠深面属 GitHub-dark 既有色板，个别 4.3 边缘（如
# 红字×红晕），不在本批调暗色——真实渲染由 tools/verify_theme_contrast_ui.py 兜。
_TOKEN_TINT_PAIRS_LIGHT = [
    ("红字@红晕（draft 徽章）", "--th-ink-red5", ["--th-bg-surface", "--th-bg-red08"], _TEXT_AA),
    ("琥珀6@琥珀晕", "--th-ink-amber6", ["--th-bg-surface", "--th-bg-amber15"], _TEXT_AA),
    ("翠6@绿晕", "--th-ink-emerald6", ["--th-bg-surface", "--th-bg-grn10"], _TEXT_AA),
    ("蓝5@蓝晕", "--th-ink-blue5", ["--th-bg-surface", "--th-bg-blue10"], _TEXT_AA),
    ("蓝6@蓝晕", "--th-ink-blue6", ["--th-bg-surface", "--th-bg-blue10"], _TEXT_AA),
    ("紫5@紫晕（monetization/relations）", "--th-ink-purple5", ["--th-bg-surface", "--th-bg-pur16"], _TEXT_AA),
    ("靛5@靛晕12（rpa 排队偏好 chips）", "--th-ink-indigo5", ["--th-bg-surface", "--th-bg-ind12"], _TEXT_AA),
    ("靛5@靛晕18（意图域 chips）", "--th-ink-indigo5", ["--th-bg-surface", "--th-bg-ind18"], _TEXT_AA),
    ("bs暖字@bs暖底（dashboard/knowledge 提示）", "--th-ink-bswarn", ["--th-bg-surface", "--th-bg-bswarn"], _TEXT_AA),
    ("bs绿字@bs绿底", "--th-ink-bsok", ["--th-bg-surface", "--th-bg-bsok"], _TEXT_AA),
]

_TOKEN_PAIRS_LIGHT_ONLY = [
    # 深底常量组：亮色下也躺在深 inkpanel 上（messenger 绑定面板等），这是它们的设计面。
    # 暗色不必测——inkpanel/surface 都是深面，slate3 浅字两处都成立且被 BOTH 组覆盖语义。
    ("slate3 浅字@深面板（设计契约=只用于深底）", "--th-ink-slate3", ["--th-bg-inkpanel"], _TEXT_AA),
    ("gray3 浅字@深面板", "--th-ink-gray3", ["--th-bg-inkpanel"], _TEXT_AA),
    ("slate1 浅字@深面板", "--th-ink-slate1", ["--th-bg-inkpanel"], _TEXT_AA),
    ("indigo3 代码徽章@墨面（.ec-code-badge）", "--th-ink-indigo3", ["--th-bg-inkchip"], _TEXT_AA),
]

# 历史遗留债务：当批刻意不动，钉"不许更差"下限 + 修复去向。
# （2026-08-04 P1.5 已除名：暗色侧栏 dim → .48 生效；--p-ink 已立 + base/rpa_overview
#   消费点已换。剩余=未换 --p-ink 的 white-on-primary 存量：personas.html
#   （.st-tab.active .tb-cnt / .pp-item-av）、settings.html 三个主按钮、
#   _channel_body_* 若干——多数当时处于其他 agent 线活跃编辑窗，随下批换完后
#   把上面 _PAIRS_LIGHT_ONLY 的白字契约升为双主题并清空本表。）
_KNOWN_DEBT = [
    ("暗色反白字@主色面（存量未换 --p-ink 的按钮/头像）", "dark", "#fff", ["--bg", "--p"], 2.5),
]


def _run_pairs(pairs, palette, theme: str) -> list[str]:
    bad = []
    for desc, fg, bg_layers, need in pairs:
        got = contrast(fg, bg_layers, palette)
        if got < need:
            bad.append(f"[{theme}] {desc}: {fg} on {'>'.join(bg_layers)} = {got:.2f} < {need}")
    return bad


def test_light_theme_contrast():
    light, _ = _load_palettes()
    bad = _run_pairs(_PAIRS_BOTH_THEMES + _PAIRS_LIGHT_ONLY, light, "light")
    bad += _run_pairs(_TOKEN_PAIRS_BOTH + _TOKEN_PAIRS_LIGHT_ONLY, light, "light")
    bad += _run_pairs(_TOKEN_TINT_PAIRS_LIGHT, light, "light")
    assert not bad, (
        "白天模式对比度低于登记阈值（改 base.html 调色板 / codemod 表须两侧联动，"
        "见本文件头部说明）：\n  " + "\n  ".join(bad)
    )


def test_dark_theme_contrast():
    _, dark = _load_palettes()
    bad = _run_pairs(_PAIRS_BOTH_THEMES, dark, "dark")
    bad += _run_pairs(_TOKEN_PAIRS_BOTH, dark, "dark")
    bad += _run_pairs(_PAIRS_DARK_FLOORS, dark, "dark")
    assert not bad, (
        "暗色模式对比度低于登记阈值（亮色修复不许以暗色回退为代价；"
        "「提亮地板」组是 2026-08-16 可读性收口的防回退钉）：\n  "
        + "\n  ".join(bad)
    )


def test_known_debt_not_worse():
    light, dark = _load_palettes()
    pal = {"light": light, "dark": dark}
    bad = []
    for desc, theme, fg, bg_layers, floor in _KNOWN_DEBT:
        got = contrast(fg, bg_layers, pal[theme])
        if got < floor:
            bad.append(f"[{theme}] {desc}: {got:.2f} < 债务下限 {floor}")
    assert not bad, "已登记债务进一步恶化：\n  " + "\n  ".join(bad)


def test_debt_registry_not_stale():
    """债务修复后必须从 _KNOWN_DEBT 除名并升格为正式契约（防债务表变永久白名单）。"""
    light, dark = _load_palettes()
    pal = {"light": light, "dark": dark}
    stale = []
    for desc, theme, fg, bg_layers, _floor in _KNOWN_DEBT:
        got = contrast(fg, bg_layers, pal[theme])
        if got >= _TEXT_AA:
            stale.append(f"[{theme}] {desc} 已达 {got:.2f}≥{_TEXT_AA}——请除名并登记为正式契约")
    assert not stale, "\n".join(stale)


# ---------------------------------------------------------------------------
# 右栏副驾 --cp-* （shared/copilot，两端共享的第三套体系）
# ---------------------------------------------------------------------------

def _load_cp_palettes() -> tuple[dict, dict]:
    light = _vars_of(_block(_strip_comments(_CP_LIGHT_CSS.read_text(encoding="utf-8")),
                            r':root\[data-cp-theme="light"\],\s*\.cp-theme-light'))
    dark = _vars_of(_block(_strip_comments(_CP_DARK_CSS.read_text(encoding="utf-8")),
                           r':root\[data-cp-theme="dark"\],\s*\.cp-theme-dark'))
    return light, dark


# 面＝组件真实渲染栈：面板底 --cp-surface，卡内次级面 --cp-surface-2
# （相册存货条/错误卡/骨架屏都在这一层，也正是对比度最差的那格）。
_CP_PAIRS = [
    ("副驾正文@面板面", "--cp-text", ["--cp-bg", "--cp-surface"], _TEXT_AA),
    ("副驾次级文字@面板面", "--cp-text-dim", ["--cp-bg", "--cp-surface"], _TEXT_AA),
    ("副驾次级文字@次级面", "--cp-text-dim", ["--cp-bg", "--cp-surface-2"], _TEXT_AA),
    ("副驾三级文字@面板面", "--cp-text-tiny", ["--cp-bg", "--cp-surface"], _TEXT_AA),
    ("副驾三级文字@次级面（存货条/错误卡）", "--cp-text-tiny",
     ["--cp-bg", "--cp-surface-2"], _TEXT_AA),
]

# 与 base.html 同档的**暗色提亮地板**（AA 只是法定底线；4.5~5 的次级灰长时间
# 盯着仍是「灰糊」——右栏是坐席全天盯的面板，标准不该低于它所在的外壳）。
# 仅暗色：与 _PAIRS_DARK_FLOORS 同一决策——白底灰阶再抬会压掉层级，亮色维持
# AA 4.5 契约（当前实测 7.56 / 5.40 / 4.93，均有余量）。
_CP_DARK_FLOORS = [
    ("副驾次级文字@面板面（提亮地板）", "--cp-text-dim", ["--cp-bg", "--cp-surface"], 8.0),
    ("副驾次级文字@次级面（提亮地板）", "--cp-text-dim", ["--cp-bg", "--cp-surface-2"], 6.5),
    ("副驾三级文字@面板面（提亮地板）", "--cp-text-tiny", ["--cp-bg", "--cp-surface"], 5.5),
    ("副驾三级文字@次级面（提亮地板）", "--cp-text-tiny",
     ["--cp-bg", "--cp-surface-2"], 5.5),
]


def test_copilot_rail_contrast_both_themes():
    light, dark = _load_cp_palettes()
    bad = _run_pairs(_CP_PAIRS, light, "cp-light")
    bad += _run_pairs(_CP_PAIRS, dark, "cp-dark")
    bad += _run_pairs(_CP_DARK_FLOORS, dark, "cp-dark")
    assert not bad, (
        "右栏副驾（shared/copilot）文字对比度低于登记阈值——改 theme-dark.css /"
        " theme-light.css 的 --cp-text-* 前先跑本门禁；两端共享，desktop/renderer"
        " 镜像须同步：\n  " + "\n  ".join(bad)
    )


def test_copilot_tiny_font_floor():
    """说明类字号地板 12px（admin-theme.mdc 既有纪律）。

    低对比度 × 小字号是乘法关系：只提亮不抬字号，10px 灰字照样看不清。
    --cp-fs-tiny 是 85 个引用点的单一开关，钉住它就守住了整个右栏。
    """
    css = _strip_comments(_CP_TOKENS_CSS.read_text(encoding="utf-8"))
    v = _vars_of(_block(css, r":root"))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)px", v.get("--cp-fs-tiny", ""))
    assert m, f"--cp-fs-tiny 不是 px 字面量: {v.get('--cp-fs-tiny')!r}"
    assert float(m.group(1)) >= 12, (
        f"--cp-fs-tiny={m.group(1)}px < 12px：右栏说明文字会重新变成「看不清」的"
        f"那一档（2026-08-28 收口）")


# 右栏残余硬编码小字号（<12px）**只减不增** ratchet。
# 提亮 token 只解决颜色；字号写死在各组件 styles() 里，token 够不着——两个因子
# 是乘法关系，只修一个仍是「看不清」。cp-image 本批已清零（0 条）；其余组件属
# 存量债务，按当前实测值封顶：新增即红，清理后必须把天花板同步降下来
# （test_tiny_font_ceilings_not_stale 防止表变成永久白名单）。
_TINY_FONT_RE = re.compile(r"font-size:\s*(?:[0-9]|1[01])(?:\.\d+)?px")
_COPILOT_DIR = _REPO / "shared" / "copilot"
_TINY_FONT_CEILINGS = {
    "app.html": 8,
    "components/cp-accounts.js": 6,
    "components/cp-chain-exec.js": 6,
    "components/cp-collab.js": 2,
    "components/cp-goal.js": 24,
    "components/cp-kb.js": 5,
    "components/cp-nurture.js": 7,
    "components/cp-origin.js": 1,
    "components/cp-persona.js": 9,
    "components/cp-tg-members.js": 2,
    "components/cp-voice.js": 1,
    "components/cp-xlate-tools.js": 5,
    # components/cp-image.js: 0（2026-08-28 全部迁到 var(--cp-fs-tiny)）——
    # 不登记＝天花板 0，任何回潮立刻点名。
}


def _tiny_font_counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(_COPILOT_DIR.rglob("*")):
        if not p.is_file() or p.suffix not in (".js", ".html", ".css"):
            continue
        n = len(_TINY_FONT_RE.findall(p.read_text(encoding="utf-8")))
        if n:
            out[p.relative_to(_COPILOT_DIR).as_posix()] = n
    return out


def test_copilot_tiny_font_ratchet():
    counts = _tiny_font_counts()
    over = [f"{rel}: {n} > 天花板 {_TINY_FONT_CEILINGS.get(rel, 0)}"
            for rel, n in counts.items() if n > _TINY_FONT_CEILINGS.get(rel, 0)]
    assert not over, (
        "右栏新增了 <12px 的硬编码字号（说明文字看不清的另一半因子）——改用 "
        "var(--cp-fs-tiny,12px)；确需更小请在 _TINY_FONT_CEILINGS 登记并说明：\n  "
        + "\n  ".join(sorted(over)))


def test_tiny_font_ceilings_not_stale():
    """清理过的文件必须把天花板同步降下来（防债务表变永久白名单）。"""
    counts = _tiny_font_counts()
    stale = [f"{rel}: 实测 {counts.get(rel, 0)} < 天花板 {cap}——请把天花板降到实测值"
             for rel, cap in _TINY_FONT_CEILINGS.items() if counts.get(rel, 0) < cap]
    assert not stale, "\n".join(sorted(stale))


def test_cp_image_tiny_fonts_stay_at_zero():
    """本批收口的面板钉死在 0（老板点名的「额度/提示」两行就出在这里）。"""
    n = _tiny_font_counts().get("components/cp-image.js", 0)
    assert n == 0, f"cp-image.js 又出现 {n} 处 <12px 硬编码字号"


# ---------------------------------------------------------------------------
# 工作台外壳 --tk-*（workspace_base.html）
# ---------------------------------------------------------------------------

def _load_tk_palettes() -> tuple[dict, dict]:
    css = _strip_comments(_WORKSPACE_HTML.read_text(encoding="utf-8"))
    light = _vars_of(_block(css, r":root"))
    dark_over = _vars_of(_block(css, r'\[data-cp-theme="dark"\]'))
    return light, {**light, **dark_over}   # 暗色=亮色基础上的覆写（CSS 级联同口径）


# 文字系（frontend-theme.mdc：语义**文字**一律走 --tk-*-ink，别拿 --tk-amber 当字色）
_TK_TEXT_PAIRS = [
    ("工作台正文@卡面", "--tk-text", ["--tk-bg", "--tk-surface"], _TEXT_AA),
    ("工作台次级文字@卡面", "--tk-text-muted", ["--tk-bg", "--tk-surface"], _TEXT_AA),
    ("工作台次级文字@次级面", "--tk-text-muted", ["--tk-bg", "--tk-surface-2"], _TEXT_AA),
    ("工作台次级文字@页面底", "--tk-text-muted", ["--tk-bg"], _TEXT_AA),
    ("成功墨@卡面", "--tk-ok-ink", ["--tk-bg", "--tk-surface"], _TEXT_AA),
    ("警示墨@卡面", "--tk-warn-ink", ["--tk-bg", "--tk-surface"], _TEXT_AA),
    ("危险墨@卡面", "--tk-danger-ink", ["--tk-bg", "--tk-surface"], _TEXT_AA),
    ("紫墨@卡面", "--tk-vio-ink", ["--tk-bg", "--tk-surface"], _TEXT_AA),
    # 别名（存量代码写 --tk-amber-ink，2026-08-10 接入弹窗实锤过它恒走亮色回落）
    ("琥珀墨别名@卡面", "--tk-amber-ink", ["--tk-bg", "--tk-surface"], _TEXT_AA),
]

# 填充/图形系：--tk-brand 是**填充与描边**用色（亮色下做正文只有 3.45:1），
# 按图形 3:1 校验——这正是 frontend-theme.mdc 那张表要表达的分工。
_TK_GRAPHIC_PAIRS = [
    ("品牌色图形@卡面（填充/圆点/描边）", "--tk-brand", ["--tk-bg", "--tk-surface"], _GRAPHIC),
]

# 暗色提亮地板：与 base.html --t2 同档（admin-theme.mdc 明写三处联动同档）
_TK_DARK_FLOORS = [
    ("暗色工作台次级文字@卡面（提亮地板）", "--tk-text-muted",
     ["--tk-bg", "--tk-surface"], 8.0),
    ("暗色工作台次级文字@次级面（提亮地板）", "--tk-text-muted",
     ["--tk-bg", "--tk-surface-2"], 6.5),
]


def test_workspace_shell_contrast_both_themes():
    light, dark = _load_tk_palettes()
    bad = _run_pairs(_TK_TEXT_PAIRS + _TK_GRAPHIC_PAIRS, light, "tk-light")
    bad += _run_pairs(_TK_TEXT_PAIRS + _TK_GRAPHIC_PAIRS, dark, "tk-dark")
    bad += _run_pairs(_TK_DARK_FLOORS, dark, "tk-dark")
    assert not bad, (
        "工作台外壳 --tk-* 对比度低于登记阈值——admin-theme.mdc 要求 base.html 色板 /"
        " codemod 表 / workspace --tk-* 三处联动同档，改一处先跑本门禁：\n  "
        + "\n  ".join(bad))


def test_workspace_shell_gate_detects_violation():
    """探测器自证：把次级文字调回一个已知糊值，门禁必须点名。"""
    _, dark = _load_tk_palettes()
    poisoned = dict(dark)
    poisoned["--tk-text-muted"] = "#6b7280"   # 暗卡面约 3.5:1
    bad = _run_pairs(_TK_TEXT_PAIRS, poisoned, "poisoned")
    assert any("次级文字" in b for b in bad), "糊值未被检出，门禁失效"


def test_copilot_gate_detects_violation():
    """探测器自证：把 --cp-text-tiny 调回收口前的旧值，门禁必须点名。"""
    _, dark = _load_cp_palettes()
    poisoned = dict(dark)
    poisoned["--cp-text-tiny"] = "#85888f"   # 2026-08-28 之前的值（次级面 4.19:1）
    bad = _run_pairs(_CP_PAIRS, poisoned, "poisoned")
    assert any("三级文字@次级面" in b for b in bad), "旧灰值未被检出，门禁失效"


def test_gate_detects_violation():
    """探测器有效性自证：往调色板塞一个已知糊值，门禁必须点名（评测不是摆设）。"""
    light, _ = _load_palettes()
    poisoned = dict(light)
    poisoned["--t3"] = "#cbd5e1"  # 1.5:1 的灾难值
    bad = _run_pairs(_PAIRS_BOTH_THEMES, poisoned, "poisoned")
    assert any("三级文字" in b for b in bad), "篡改 --t3 未被检出，门禁失效"
