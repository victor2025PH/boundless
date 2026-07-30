# -*- coding: utf-8 -*-
"""内联硬编码颜色 ratchet 门禁（P2-3 建账，P3-1 收口升级，2026-07-23）。

问题：`style="...#7f1d1d..."` 这类**内联 style 属性里的硬编码颜色**特异性最高、
无法被主题层（暗色模式 / 品牌白标 tokens）覆盖，是暗色主题收口的核心债务。
base.html 系页面已有真实暗色主题（[data-theme=dark] + OS 偏好自动进入），
内联硬编码色在暗色下不翻转＝现行破损。

P3-1 收口（tools/inline_color_codemod.py）：654 个色值按 (角色, 字面量) 映射成
`var(--th-*, <原字面量>)`——亮色渲染字节级零变化（fallback ≡ :root 亮值），
暗色由 static/theme-tokens.css 的 [data-theme=dark] 块翻转。

口径（刻意窄）：
  - 只数**静态 style 属性**（style="..." / style='...'）里含 #hex 或 rgb()/rgba()
    颜色字面量的属性个数（一个属性算 1，无论里面几个色值）；
  - **var(...) 段整体豁免**——`var(--th-x,#hex)` 的 fallback 是主题化后的兜底值，
    不算硬编码（先剥 var 段再找色值）；
  - `<style>` 块不算——页级 CSS 可以被更晚的主题层覆盖，且集中迁移更合适；
  - JS 动态拼的 el.style.x = '#fff' 不算（掩码/求值成本高，另行治理）；
  - style 属性只有布局值（display/width/gap…）不算——那些与主题无关。

配套不变量（本文件后三个测试）：
  1. theme-tokens.css 必须挂在全部根布局（漏挂＝该系页面暗色不翻转，
     但因 fallback 存在亮色仍正确——软失败，门禁把它变硬）；
  2. 模板里每个 var(--th-x, fallback) 的 fallback ≡ theme-tokens.css :root 亮值
     （颜色等价比较，#fff ≡ #ffffff）——防"改了 CSS 忘了模板"两处漂移；
  3. 模板引用的每个 --th-* token 必须在 theme-tokens.css 有定义（防拼错）。

台账维护：继续迁移后把对应文件数字改小；`test_inline_color_ledger_not_stale`
会在实际值低于天花板时点名要求收紧，防止台账虚高吞掉倒退空间。
"""
from __future__ import annotations

import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES_ROOT = _REPO / "src/web/templates"
THEME_CSS = _REPO / "src/web/static/theme-tokens.css"

_STYLE_ATTR = re.compile(r"""style\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)
_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")

# ── 账本：文件 → 含硬编码颜色的内联 style 属性数天花板（只许降不许升） ──
# 2026-07-23 P2-3 基线 638（43 模板）→ P3-1 三批收口后 103（26 模板，-84%）。
# 剩余=映射表外长尾一次性装饰色 + box-shadow 阴影色（阴影主题无关，刻意豁免
# 角色映射但仍计数），后续批次继续收紧。未列出的文件天花板=0。
_INLINE_COLOR_CEILINGS = {
    "_rpa_shared_scripts.html": 4,
    "agent_perf.html": 3,
    "ai_studio.html": 2,
    "base.html": 1,
    "developer.html": 1,
    "draft_review.html": 3,
    "knowledge.html": 4,
    # 渠道中心融合：四渠道正文迁 _channel_body_*.html（计数随内容平移）
    "_channel_body_line.html": 1,
    "_channel_body_messenger.html": 10,
    "ops/contacts.html": 1,
    "ops/mobile_handoffs.html": 1,
    "ops_overview.html": 2,
    # personas.html：2026-07-30 品牌收口把最后一个内联硬编码色转成 color-mix(var(--p)) → 0，除名
    "queue_monitor.html": 3,
    "relations_health.html": 3,
    "rpa_overview.html": 11,
    "settings.html": 9,
    "setup_wizard.html": 1,
    "strategies.html": 2,
    "_channel_body_telegram.html": 2,
    "unified_inbox.html": 10,  # 2026-07-30 品牌收口：检测语言 chip tint → color-mix(var(--tk-brand))
    "_channel_body_whatsapp.html": 13,
    "workflows.html": 1,
    "workspace_base.html": 4,   # 2026-07-29：AI 引导/试用横幅+升级弹窗色彩层已抽类（ws-aiguide-*/ws-aitrial-*/ws-upsell-*）
    "workspace_dashboard.html": 5,
    # workspace_usage.html：2026-07-30 品牌收口把图例点 #93c5fd → var(--bl-growth-300) → 0，除名
}

# theme-tokens.css 必须挂载的根布局（覆盖全部 43 个欠账页的 extends 链）
_TOKEN_ROOTS = (
    "base.html",
    "workspace_base.html",
    "ops_overview.html",
    "ops/contacts.html",
    "ops/merge_reviews.html",
    "ops/mobile_handoffs.html",
)


def _strip_var_spans(val: str) -> str:
    """把 var(...) 段（含嵌套括号，如 var(--x,rgba(0,0,0,.5))）替换成等长空白。"""
    out = list(val)
    low = val.lower()
    i = 0
    while True:
        j = low.find("var(", i)
        if j < 0:
            break
        depth = 0
        k = j + 3
        while k < len(val):
            if val[k] == "(":
                depth += 1
            elif val[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        end = k + 1 if k < len(val) else len(val)
        for m in range(j, end):
            out[m] = " "
        i = end
    return "".join(out)


def _count_inline_color_styles(text: str) -> int:
    n = 0
    for m in _STYLE_ATTR.finditer(text):
        val = m.group(2) if m.group(2) is not None else m.group(3)
        if _COLOR.search(_strip_var_spans(val or "")):
            n += 1
    return n


def _scan() -> dict:
    counts = {}
    for p in sorted(TEMPLATES_ROOT.rglob("*.html")):
        rel = p.relative_to(TEMPLATES_ROOT).as_posix()
        n = _count_inline_color_styles(p.read_text(encoding="utf-8", errors="replace"))
        if n:
            counts[rel] = n
    return counts


def test_inline_color_not_above_ceiling():
    counts = _scan()
    offenders = []
    for rel, n in counts.items():
        cap = _INLINE_COLOR_CEILINGS.get(rel, 0)
        if n > cap:
            offenders.append(f"{rel}: {n} > 天花板 {cap}")
    assert not offenders, (
        "内联 style 硬编码颜色超出台账天花板——新代码请用主题 token"
        "（var(--th-*,#亮值)，见 static/theme-tokens.css + tools/inline_color_codemod.py），"
        "不要写 style=\"...#hex...\"：\n  " + "\n  ".join(offenders)
    )


def test_inline_color_ledger_not_stale():
    counts = _scan()
    stale = []
    for rel, cap in _INLINE_COLOR_CEILINGS.items():
        actual = counts.get(rel, 0)
        if actual < cap:
            stale.append(f"{rel}: 实际 {actual} < 天花板 {cap}（请把台账收紧到 {actual}）")
    assert not stale, (
        "迁移成果未落进台账（天花板虚高会吞掉倒退空间）：\n  " + "\n  ".join(stale)
    )


# ═══════════════════════ P3-1 主题 token 配套不变量 ═══════════════════════

def _canon_color(lit: str) -> str:
    """颜色等价规范化：小写去空白 + 3/4 位 hex 展开（#fff ≡ #ffffff）
    + rgba alpha 前导零折叠（0.08 ≡ .08）——与 tools/inline_color_codemod.py 同口径。"""
    s = lit.lower().replace(" ", "").replace("\t", "")
    if s.startswith("#") and len(s) in (4, 5):
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return re.sub(r"(?<=[,(])0\.(\d)", r".\1", s)


def _parse_css_block(css: str, header: str) -> dict:
    """从 theme-tokens.css 抽 `header{...}` 块里的 --th-*:value 对。"""
    m = re.search(re.escape(header) + r"\{(.*?)\}", css, re.DOTALL)
    assert m, f"theme-tokens.css 缺 {header} 块"
    out = {}
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(";")
        if line.startswith("--th-") and ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


_VAR_TH = re.compile(r"var\((--th-[a-z0-9-]+)\s*,")


def _iter_template_var_refs():
    """yield (rel, token, fallback)：模板里每个 var(--th-x, fallback) 引用。"""
    for p in sorted(TEMPLATES_ROOT.rglob("*.html")):
        rel = p.relative_to(TEMPLATES_ROOT).as_posix()
        txt = p.read_text(encoding="utf-8", errors="replace")
        for m in _VAR_TH.finditer(txt):
            # fallback = 逗号后到配平右括号（可能含 rgba(...) 嵌套）
            k = m.end()
            depth = 1  # 已在 var( 内
            start = k
            while k < len(txt) and depth > 0:
                if txt[k] == "(":
                    depth += 1
                elif txt[k] == ")":
                    depth -= 1
                k += 1
            yield rel, m.group(1), txt[start:k - 1].strip()


def test_theme_tokens_css_mounted_in_roots():
    """token CSS 漏挂＝该系页面暗色不翻转（fallback 兜底亮色仍对——软失败变硬）。"""
    missing = []
    for root in _TOKEN_ROOTS:
        txt = (TEMPLATES_ROOT / root).read_text(encoding="utf-8", errors="replace")
        if "/static/theme-tokens.css" not in txt:
            missing.append(root)
    assert not missing, (
        "根布局缺 theme-tokens.css 挂载（<link rel=\"stylesheet\" "
        "href=\"/static/theme-tokens.css?v=...\">）：\n  " + "\n  ".join(missing)
    )


def test_th_token_fallbacks_match_css_light_values():
    """模板 fallback ≡ :root 亮值：迁移的"亮色零变化"承诺由此钉死。

    若要调整某 token 的亮色值：改 tools/inline_color_codemod.py 的表 →
    --emit-css 重生成 → 模板里旧 fallback 会被本门禁点名，跑 codemod 或手改对齐。
    """
    css = THEME_CSS.read_text(encoding="utf-8")
    light = _parse_css_block(css, ":root")
    bad = []
    for rel, token, fb in _iter_template_var_refs():
        defined = light.get(token)
        if defined is None:
            continue  # 未定义由下一个测试点名
        if _canon_color(fb) != _canon_color(defined):
            bad.append(f"{rel}: {token} fallback={fb!r} ≠ :root {defined!r}")
    assert not bad, "模板 var(--th-*) fallback 与 theme-tokens.css :root 亮值漂移：\n  " + "\n  ".join(bad[:40])


def test_th_tokens_all_defined():
    """模板引用的 --th-* 必须在 theme-tokens.css 有定义（防拼错静默回落）；
    暗色块只允许覆写 :root 已有的 token（防"只有暗值没有亮值"的幽灵 token）。"""
    css = THEME_CSS.read_text(encoding="utf-8")
    light = _parse_css_block(css, ":root")
    # 暗色块必须同时挂两套开关（base 系 data-theme / 工作台 data-cp-theme）——
    # 少一个=对应页面家族暗色不翻转（P3-2 教训：曾只认 data-theme，工作台白开）。
    dark = _parse_css_block(css, '[data-theme="dark"],[data-cp-theme="dark"]')
    undefined = sorted({
        f"{token}（{rel}）" for rel, token, _fb in _iter_template_var_refs()
        if token not in light
    })
    assert not undefined, "模板引用了未定义的主题 token：\n  " + "\n  ".join(undefined[:20])
    orphan_dark = sorted(set(dark) - set(light))
    assert not orphan_dark, "暗色块存在 :root 没有的 token：\n  " + "\n  ".join(orphan_dark)
