# -*- coding: utf-8 -*-
"""共享 copilot 组件库主题 token 体检（D8，GOALS_UI_REVAMP_PLAN 配套工程项）。

组件样式写 ``var(--cp-x, <亮色fallback>)``——token 没进主题表时亮色宿主看不出
问题（fallback 恰是亮色值），**暗色宿主拿到的也是亮色值**：深底亮块/深蓝字贴
深底，肉眼验收才暴露。2026-07-28 首扫即抓到 3 个真实缺口（``--cp-accent-deep``
直说 pill / ``--cp-accent-bg`` / ``--cp-bg-soft``），本门禁让这类缺口在 CI 先红。

三个不变量：
1. 明暗两份主题表定义**同一组**语义 token（单边漏定义=另一主题下取 fallback）；
2. 组件/宿主引用的每个 ``--cp-*`` 都有定义（比例尺 tokens.css 或明暗主题表）；
3. 语义色 token（非比例尺）必须**两份主题都有**——只进 light 不进 dark 等于没修。

修法：在 ``shared/copilot/theme-{light,dark}.css`` 补 token（暗色值按语义取
「同等强调度」而非「同色值」，如 accent-deep 暗色取亮档），拷贝同步桌面份
（``test_copilot_shared_sync`` 守双份一致）。
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SHARED = _ROOT / "shared" / "copilot"
_TOKENS = _SHARED / "tokens.css"
_DARK = _SHARED / "theme-dark.css"
_LIGHT = _SHARED / "theme-light.css"
# 宿主模板也直接用 --cp-*（英雄卡/卡框），一并纳入引用面
_HOST_TEMPLATES = (
    _ROOT / "src" / "web" / "templates" / "unified_inbox.html",
)

_DEF_RE = re.compile(r"(--cp-[a-z0-9-]+)\s*:")
_REF_RE = re.compile(r"var\(\s*(--cp-[a-z0-9-]+)")


def _defined(path: Path) -> set:
    return set(_DEF_RE.findall(path.read_text(encoding="utf-8")))


def _references() -> dict:
    """引用面：shared/copilot 全树（js/html/css）+ 宿主模板 → {token: [文件]}。"""
    refs: dict = {}
    scan = [p for p in _SHARED.rglob("*")
            if p.is_file() and p.suffix in (".js", ".html", ".css")
            and p.name not in ("theme-dark.css", "theme-light.css", "tokens.css")]
    scan += [p for p in _HOST_TEMPLATES if p.is_file()]
    for p in scan:
        for tok in _REF_RE.findall(p.read_text(encoding="utf-8")):
            refs.setdefault(tok, set()).add(p.name)
    return refs


def test_theme_files_exist():
    for p in (_TOKENS, _DARK, _LIGHT):
        assert p.is_file(), f"缺 {p}"


def test_dark_light_define_same_token_set():
    """明暗主题语义 token 集合一致（单边漏定义＝另一主题静默拿 fallback）。"""
    dark, light = _defined(_DARK), _defined(_LIGHT)
    assert dark == light, (
        "明暗主题 token 集合漂移——两份都要定义：\n"
        f"  只在 dark : {sorted(dark - light)}\n"
        f"  只在 light: {sorted(light - dark)}")


def test_every_referenced_token_is_defined():
    """组件/宿主引用的 --cp-* 必须有定义（tokens.css 比例尺 或 明暗主题表）。"""
    defined = _defined(_TOKENS) | (_defined(_DARK) & _defined(_LIGHT))
    missing = {tok: sorted(files) for tok, files in _references().items()
               if tok not in defined}
    assert not missing, (
        "引用了未定义的主题 token（暗色宿主将拿到组件内亮色 fallback）——"
        "在 theme-{light,dark}.css 两份里补定义并同步桌面份：\n  "
        + "\n  ".join(f"{k} <- {v}" for k, v in sorted(missing.items())))


def test_semantic_tokens_not_in_scale_file():
    """语义色不得混进 tokens.css（比例尺表跟主题无关，色值写死进去=明暗同色）。"""
    color_like = re.compile(
        r"(--cp-[a-z0-9-]+)\s*:\s*(?:#|rgb|hsl|color-mix)", re.I)
    leaked = color_like.findall(_TOKENS.read_text(encoding="utf-8"))
    # 阴影是「带透明度的结构值」不是语义色，放行
    leaked = [t for t in leaked if not t.startswith("--cp-shadow")]
    assert not leaked, (
        f"tokens.css 混入语义色 {leaked}——移到 theme-light/theme-dark 双表")


def test_accent_wires_to_brand_growth():
    """副驾强调色必须接到 --bl-growth（网页收件箱 + 桌面壳同一套语义）。

    未接线时组件仍显示某种蓝（旧 Tailwind #2563eb），肉眼「差不多对」，
    但改 platform/brand 智连蓝不会跟——静默漂移。fallback 字面量也须是
    #1e8cf2（品牌色本身），禁止回落到 #2563eb。
    """
    light = _LIGHT.read_text(encoding="utf-8")
    dark = _DARK.read_text(encoding="utf-8")
    assert "--cp-accent:" in light and "--bl-growth" in light, (
        "theme-light.css 的 --cp-accent 须引用 --bl-growth"
    )
    assert "--cp-accent-hover:" in light and "--cp-accent-hover:" in dark, (
        "明暗主题都须定义 --cp-accent-hover（桌面 style.css 主按钮 hover 依赖）"
    )
    assert "--bl-growth" in dark, (
        "theme-dark.css 的 accent 阶也须引用 --bl-growth*（暗底取亮档）"
    )
    for label, css in (("light", light), ("dark", dark)):
        assert "#2563eb" not in css and "#3b82f6" not in css, (
            f"theme-{label}.css 仍含旧 Tailwind 蓝字面量——应改为 "
            f"var(--bl-growth…) + 品牌色 fallback"
        )
