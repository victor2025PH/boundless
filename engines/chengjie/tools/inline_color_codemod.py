# -*- coding: utf-8 -*-
"""内联硬编码颜色 → 主题 token 迁移工具（P3-1 暗色收口，2026-07-23）。

背景
----
templates/**.html 存在 600+ 个含硬编码颜色（#hex / rgb()）的内联 style 属性。
base.html 系 25 个页面**已有真实暗色主题**（[data-theme=dark] + OS 偏好自动进入），
这些内联色在暗色下不翻转 = 现行破损（浅色徽章刺眼、灰字对比度塌）；
workspace_base 系页面当前恒亮色，收口后变"暗色就绪"。

策略（与 tools/fetch_codemod.py 同族的确定性文本迁移）
----
1. 只动 style 属性值（双/单引号两种），CSS 块/JS 逻辑色不碰；
2. **属性感知**：同一色值按声明属性归 ink(color/fill…)/bg(background*)/bd(border*/outline)
   三种角色查映射表——`color:#fff`（永远白，常量）与 `background:#fff`（暗色翻深表面）
   是两个 token；box-shadow 等不映射（留台账）；
3. 替换为 `var(--th-xxx, <原字面量>)`：亮色字节级零变化；页面若漏挂 token CSS
   也有 fallback 兜底，绝无"变量未定义→黑字/透明"事故；
4. 已在 var() 里的颜色（fallback）先掩码跳过 → 幂等可重跑；
5. --emit-css 从同一张表生成 static/theme-tokens.css（:root 亮值=字面量本身 +
   [data-theme=dark] 暗值）——表是单源，门禁校验模板 fallback ≡ :root 值。

用法
----
  python tools/inline_color_codemod.py --dry      # 干跑：每文件替换数
  python tools/inline_color_codemod.py --apply    # 应用（保留原文件 EOL：按字节读写）
  python tools/inline_color_codemod.py --emit-css # 生成 src/web/static/theme-tokens.css
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
TPL_ROOT = REPO / "src" / "web" / "templates"
CSS_OUT = REPO / "src" / "web" / "static" / "theme-tokens.css"

STYLE_ATTR = re.compile(r"""style\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)
COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")

# ---------------------------------------------------------------------------
# 映射表（单源）：(role, 归一化字面量) -> (token, 暗色值 | None=两主题同值)
# 亮色值恒 = 字面量本身（保证迁移后亮色渲染零变化），故表里不重复存。
# 暗色值语言对齐 base.html 既有暗色号（--red #f85149 / --green #3fb950 /
# --amber #e3b341 / --p #58a6ff / 卡面 #17181d / 输入面 #1e2026 / 边 #26272e）。
# ---------------------------------------------------------------------------
_E = [
    # ── ink：中性灰阶（亮色 400-900 → 暗色反转为浅灰阶梯） ──
    ("ink", "#94a3b8", "--th-ink-slate4", "#8a8f97"),
    ("ink", "#64748b", "--th-ink-slate5", "#a0a3ab"),
    ("ink", "#6b7280", "--th-ink-gray5", "#a0a3ab"),
    ("ink", "#9ca3af", "--th-ink-gray4", "#8a8f97"),
    ("ink", "#cbd5e1", "--th-ink-slate3", None),  # 亮色下已用于深底部件，常量
    ("ink", "#d1d5db", "--th-ink-gray3", None),
    ("ink", "#f1f5f9", "--th-ink-slate1", None),  # 深底部件上的浅字，常量
    ("ink", "#475569", "--th-ink-slate6", "#c9ccd3"),
    ("ink", "#374151", "--th-ink-gray7", "#d7d9de"),
    ("ink", "#334155", "--th-ink-slate7", "#d7d9de"),
    ("ink", "#1e293b", "--th-ink-slate8", "#ececf0"),
    ("ink", "#0f172a", "--th-ink-slate9", "#ececf0"),
    ("ink", "#fff", "--th-ink-inverse", None),  # 反白字：永远在彩底上，常量
    ("ink", "#ffffff", "--th-ink-inverse", None),
    # ── ink：语义色 ──
    ("ink", "#dc2626", "--th-ink-red6", "#f85149"),
    ("ink", "#ef4444", "--th-ink-red5", "#f85149"),
    ("ink", "#f87171", "--th-ink-red4", None),
    ("ink", "#b91c1c", "--th-ink-red7", "#f26d5f"),
    ("ink", "#991b1b", "--th-ink-red8", "#f26d5f"),
    ("ink", "#c0392b", "--th-ink-flatred", "#f26d5f"),
    ("ink", "#f85149", "--th-ink-ghred", None),
    ("ink", "#fecaca", "--th-ink-red2", None),
    ("ink", "#f59e0b", "--th-ink-amber5", "#e3b341"),
    ("ink", "#d97706", "--th-ink-amber6", "#e3b341"),
    ("ink", "#b45309", "--th-ink-amber7", "#e3b341"),
    ("ink", "#92400e", "--th-ink-amber8", "#d9a83c"),
    ("ink", "#856404", "--th-ink-bswarn", "#e3b341"),
    ("ink", "#10b981", "--th-ink-emerald5", "#3fb950"),
    ("ink", "#059669", "--th-ink-emerald6", "#3fb950"),
    ("ink", "#065f46", "--th-ink-emerald8", "#56d364"),
    ("ink", "#22c55e", "--th-ink-green5", "#3fb950"),
    ("ink", "#16a34a", "--th-ink-green6", "#3fb950"),
    ("ink", "#15803d", "--th-ink-green7", "#56d364"),
    ("ink", "#2563eb", "--th-ink-blue6", "#58a6ff"),
    ("ink", "#3b82f6", "--th-ink-blue5", "#58a6ff"),
    ("ink", "#1e40af", "--th-ink-blue8", "#79b8ff"),
    ("ink", "#0ea5e9", "--th-ink-sky5", None),
    ("ink", "#06b6d4", "--th-ink-cyan5", None),
    ("ink", "#0891b2", "--th-ink-cyan6", "#22b8cf"),
    ("ink", "#7c3aed", "--th-ink-violet6", "#a78bfa"),
    ("ink", "#8b5cf6", "--th-ink-violet5", "#a78bfa"),
    ("ink", "#6d28d9", "--th-ink-violet7", "#b794f6"),
    ("ink", "#5b21b6", "--th-ink-violet8", "#c4b5fd"),
    ("ink", "#6366f1", "--th-ink-indigo5", "#a5b4fc"),
    ("ink", "#a5b4fc", "--th-ink-indigo3", None),
    ("ink", "#1d4ed8", "--th-ink-blue7", "#79b8ff"),
    ("ink", "#155724", "--th-ink-bsok", "#56d364"),
    ("ink", "#198754", "--th-ink-bsgreen", "#3fb950"),
    ("ink", "#166534", "--th-ink-green8", "#56d364"),
    ("ink", "#7f1d1d", "--th-ink-red9", "#f26d5f"),
    ("ink", "#2d6cdf", "--th-ink-blue2d", "#79b8ff"),
    ("ink", "#a855f7", "--th-ink-purple5", "#c084fc"),
    # ── bg：表面/软底/实底 ──
    ("bg", "#fff", "--th-bg-surface", "#1e2026"),
    ("bg", "#ffffff", "--th-bg-surface", "#1e2026"),
    ("bg", "#f1f5f9", "--th-bg-slate1", "#1e2026"),
    ("bg", "#e5e7eb", "--th-bg-gray2", "#26272e"),
    ("bg", "#fee2e2", "--th-bg-red1", "rgba(248,81,73,.15)"),
    ("bg", "#fff3cd", "--th-bg-bswarn", "rgba(227,179,65,.15)"),
    ("bg", "#1e293b", "--th-bg-slate8", None),  # 刻意深底部件，常量
    ("bg", "#0f172a", "--th-bg-slate9", None),
    ("bg", "#1a1f2e", "--th-bg-inkpanel", None),
    ("bg", "#dc2626", "--th-bg-red6", "#e0524a"),
    ("bg", "#ef4444", "--th-bg-red5", "#e0524a"),
    ("bg", "#991b1b", "--th-bg-red8", None),
    ("bg", "#7f1d1d", "--th-bg-red9", None),
    ("bg", "#f59e0b", "--th-bg-amber5", "#c98f2f"),
    ("bg", "#d97706", "--th-bg-amber6", "#c98f2f"),
    ("bg", "#92400e", "--th-bg-amber8", None),
    ("bg", "#10b981", "--th-bg-emerald5", "#2ea043"),
    ("bg", "#059669", "--th-bg-emerald6", "#238636"),
    ("bg", "#22c55e", "--th-bg-green5", "#2ea043"),
    ("bg", "#16a34a", "--th-bg-green6", "#2ea043"),
    ("bg", "#15803d", "--th-bg-green7", "#238636"),
    ("bg", "#2563eb", "--th-bg-blue6", "#316dca"),
    ("bg", "#3b82f6", "--th-bg-blue5", "#316dca"),
    ("bg", "#1e40af", "--th-bg-blue8", None),
    ("bg", "#0ea5e9", "--th-bg-sky5", None),
    ("bg", "#06b6d4", "--th-bg-cyan5", None),
    ("bg", "#0891b2", "--th-bg-cyan6", None),
    ("bg", "#7c3aed", "--th-bg-violet6", "#7f56d9"),
    ("bg", "#8b5cf6", "--th-bg-violet5", None),
    ("bg", "#6d28d9", "--th-bg-violet7", None),
    ("bg", "#6366f1", "--th-bg-indigo5", None),
    ("bg", "#5b7cf6", "--th-bg-brand", None),
    # ── bg：品牌/语义半透明浅晕（自带 alpha，暗色只微调亮度） ──
    ("bg", "rgba(91,124,246,.06)", "--th-bg-brand06", "rgba(88,166,255,.08)"),
    ("bg", "rgba(91,124,246,.08)", "--th-bg-brand08", "rgba(88,166,255,.1)"),
    ("bg", "rgba(91,124,246,.1)", "--th-bg-brand10", "rgba(88,166,255,.12)"),
    ("bg", "rgba(91,124,246,.15)", "--th-bg-brand15", "rgba(88,166,255,.2)"),
    ("bg", "rgba(91,124,246,.18)", "--th-bg-brand18", "rgba(88,166,255,.2)"),
    ("bg", "rgba(91,124,246,.22)", "--th-bg-brand22", "rgba(88,166,255,.25)"),
    ("bg", "rgba(59,130,246,.1)", "--th-bg-blue10", "rgba(88,166,255,.12)"),
    ("bg", "rgba(45,108,223,.08)", "--th-bg-blue08", "rgba(88,166,255,.1)"),
    ("bg", "rgba(245,158,11,.06)", "--th-bg-amber06", "rgba(227,179,65,.08)"),
    ("bg", "rgba(245,158,11,.08)", "--th-bg-amber08", "rgba(227,179,65,.1)"),
    ("bg", "rgba(245,158,11,.1)", "--th-bg-amber10", "rgba(227,179,65,.12)"),
    ("bg", "rgba(245,158,11,.12)", "--th-bg-amber12", "rgba(227,179,65,.15)"),
    ("bg", "rgba(245,158,11,.15)", "--th-bg-amber15", "rgba(227,179,65,.18)"),
    ("bg", "rgba(245,158,11,.28)", "--th-bg-amber28", "rgba(227,179,65,.3)"),
    ("bg", "rgba(239,68,68,.06)", "--th-bg-red06", "rgba(248,81,73,.08)"),
    ("bg", "rgba(239,68,68,.08)", "--th-bg-red08", "rgba(248,81,73,.1)"),
    ("bg", "rgba(99,102,241,.06)", "--th-bg-ind06", None),
    ("bg", "rgba(99,102,241,.12)", "--th-bg-ind12", "rgba(139,148,255,.15)"),
    ("bg", "rgba(99,102,241,.15)", "--th-bg-ind15", "rgba(139,148,255,.18)"),
    ("bg", "rgba(99,102,241,.18)", "--th-bg-ind18", "rgba(139,148,255,.2)"),
    ("bg", "rgba(16,185,129,.05)", "--th-bg-grn05", "rgba(63,185,80,.08)"),
    ("bg", "rgba(16,185,129,.06)", "--th-bg-grn06", "rgba(63,185,80,.1)"),
    ("bg", "rgba(16,185,129,.1)", "--th-bg-grn10", "rgba(63,185,80,.14)"),
    ("bg", "rgba(16,185,129,.15)", "--th-bg-grn15", "rgba(63,185,80,.18)"),
    ("bg", "rgba(139,92,246,.06)", "--th-bg-vio06", None),
    ("bg", "rgba(139,92,246,.15)", "--th-bg-vio15", None),
    ("bg", "rgba(168,85,247,.12)", "--th-bg-pur12", None),
    ("bg", "rgba(168,85,247,.16)", "--th-bg-pur16", None),
    ("bg", "rgba(148,163,184,.05)", "--th-bg-slt05", None),
    ("bg", "#94a3b8", "--th-bg-slate4", None),
    ("bg", "#25d366", "--th-bg-wagreen", None),
    ("bg", "#1b2038", "--th-bg-inkchip", None),
    ("bg", "#a855f7", "--th-bg-purple5", None),
    # ── bg：遮罩层（两主题同值） ──
    ("bg", "rgba(0,0,0,.3)", "--th-bg-scrim30", None),
    ("bg", "rgba(0,0,0,.45)", "--th-bg-scrim45", None),
    ("bg", "rgba(0,0,0,.5)", "--th-bg-scrim50", None),
    ("bg", "rgba(0,0,0,.55)", "--th-bg-scrim55", None),
    ("bg", "rgba(0,0,0,.6)", "--th-bg-scrim60", None),
    # ── bg：Tailwind 50/100 软底（与语义 ink 配对成徽章；暗色转半透明同族） ──
    ("bg", "#ecfdf5", "--th-bg-emerald50", "rgba(63,185,80,.12)"),
    ("bg", "#d1fae5", "--th-bg-emerald100", "rgba(63,185,80,.16)"),
    ("bg", "#dcfce7", "--th-bg-green100", "rgba(63,185,80,.16)"),
    ("bg", "#d4edda", "--th-bg-bsok", "rgba(63,185,80,.15)"),
    ("bg", "#eff6ff", "--th-bg-blue50", "rgba(88,166,255,.1)"),
    ("bg", "#dbeafe", "--th-bg-blue100", "rgba(88,166,255,.15)"),
    ("bg", "#fef3c7", "--th-bg-amber100", "rgba(227,179,65,.16)"),
    ("bg", "#ede9fe", "--th-bg-violet100", "rgba(167,139,250,.16)"),
    ("bg", "#faf5ff", "--th-bg-purple50", "rgba(167,139,250,.1)"),
    ("bg", "#f3f4f6", "--th-bg-gray100", "rgba(236,236,240,.08)"),
    ("bg", "#fff5f5", "--th-bg-red50", "rgba(248,81,73,.1)"),
    ("bg", "#fef2f2", "--th-bg-red50tw", "rgba(248,81,73,.1)"),
    # ── bd：边框 ──
    ("bd", "#d1d5db", "--th-bd-gray3", "#30313a"),
    ("bd", "#e5e7eb", "--th-bd-gray2", "#26272e"),
    ("bd", "#f1f5f9", "--th-bd-slate1", "rgba(236,236,240,.1)"),
    ("bd", "#cbd5e1", "--th-bd-slate3", "#30313a"),
    ("bd", "#e2e8f0", "--th-bd-slate2", "#30313a"),
    ("bd", "#334155", "--th-bd-slate7", None),
    ("bd", "#1e293b", "--th-bd-slate8", None),
    ("bd", "#dc2626", "--th-bd-red6", "#f85149"),
    ("bd", "#f87171", "--th-bd-red4", None),
    ("bd", "#fecaca", "--th-bd-red2", "rgba(248,81,73,.35)"),
    ("bd", "#10b981", "--th-bd-emerald5", "#3fb950"),
    ("bd", "#16a34a", "--th-bd-green6", "#3fb950"),
    ("bd", "#2563eb", "--th-bd-blue6", "#58a6ff"),
    ("bd", "#3b82f6", "--th-bd-blue5", "#58a6ff"),
    ("bd", "#f59e0b", "--th-bd-amber5", "#e3b341"),
    ("bd", "#7c3aed", "--th-bd-violet6", "#a78bfa"),
    ("bd", "#6366f1", "--th-bd-indigo5", None),
    ("bd", "rgba(91,124,246,.15)", "--th-bd-brand15", "rgba(88,166,255,.2)"),
    ("bd", "rgba(91,124,246,.2)", "--th-bd-brand20", "rgba(88,166,255,.25)"),
    ("bd", "rgba(245,158,11,.2)", "--th-bd-amber20", "rgba(227,179,65,.25)"),
    ("bd", "rgba(245,158,11,.25)", "--th-bd-amber25", "rgba(227,179,65,.3)"),
    ("bd", "rgba(245,158,11,.4)", "--th-bd-amber40", "rgba(227,179,65,.45)"),
    ("bd", "rgba(99,102,241,.3)", "--th-bd-ind30", "rgba(139,148,255,.35)"),
    ("bd", "rgba(59,130,246,.3)", "--th-bd-blue30", "rgba(88,166,255,.35)"),
    ("bd", "rgba(239,68,68,.2)", "--th-bd-red20", "rgba(248,81,73,.25)"),
    ("bd", "rgba(16,185,129,.35)", "--th-bd-grn35", "rgba(63,185,80,.4)"),
    ("bd", "rgba(16,185,129,.15)", "--th-bd-grn15", "rgba(63,185,80,.2)"),
    ("bd", "rgba(16,185,129,.2)", "--th-bd-grn20", "rgba(63,185,80,.25)"),
    ("bd", "rgba(91,124,246,.18)", "--th-bd-brand18", "rgba(88,166,255,.2)"),
    ("bd", "rgba(91,124,246,.22)", "--th-bd-brand22", "rgba(88,166,255,.25)"),
    ("bd", "rgba(139,92,246,.15)", "--th-bd-vio15", None),
    ("bd", "rgba(245,158,11,.28)", "--th-bd-amber28", "rgba(227,179,65,.3)"),
    ("bd", "#fca5a5", "--th-bd-red3", "rgba(248,81,73,.4)"),
    ("bd", "#c4b5fd", "--th-bd-violet3", "rgba(167,139,250,.4)"),
    ("bd", "#2d6cdf", "--th-bd-blue2d", "#58a6ff"),
]

def _norm(lit: str) -> str:
    return lit.lower().replace(" ", "").replace("\t", "")


def canon_color(lit: str) -> str:
    """颜色等价规范化：小写去空白 + 3/4 位 hex 展开（#fff ≡ #ffffff）
    + rgba alpha 前导零折叠（rgba(0,0,0,0.5) ≡ rgba(0,0,0,.5)）。"""
    s = _norm(lit)
    if s.startswith("#") and len(s) in (4, 5):
        s = "#" + "".join(ch * 2 for ch in s[1:])
    s = re.sub(r"(?<=[,(])0\.(\d)", r".\1", s)
    return s


MAPPING = {(role, canon_color(lit)): (tok, dark) for role, lit, tok, dark in _E}

_INK_PROPS = {"color", "-webkit-text-fill-color", "caret-color",
              "text-decoration-color", "fill", "stroke"}


def _classify(prop: str) -> str | None:
    p = prop.strip().lower()
    if p in _INK_PROPS:
        return "ink"
    if p.startswith("background"):
        return "bg"
    if p.startswith("border") or p.startswith("outline"):
        return "bd"
    return None  # box-shadow / text-shadow / filter…：不映射


def _mask_var_spans(val: str) -> str:
    """把 var(...) 段（含嵌套括号）替换成等长 \x00，防止 fallback 里的颜色被二次包裹。"""
    out = list(val)
    i = 0
    low = val.lower()
    while True:
        j = low.find("var(", i)
        if j < 0:
            break
        depth = 0
        k = j + 3  # 指向 '('
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
            out[m] = "\x00"
        i = end
    return "".join(out)


def _prop_before(masked: str, pos: int) -> str:
    """取 pos 处颜色所属声明的属性名（从最近的 ';' 起向前找 'prop:'）。"""
    head = masked[:pos]
    seg = head.rsplit(";", 1)[-1]
    if ":" not in seg:
        return ""
    return seg.split(":", 1)[0]


def rewrite_style_value(val: str) -> tuple[str, int]:
    """返回 (新值, 替换数)。只替换映射表命中的 (role, literal)。"""
    masked = _mask_var_spans(val)
    repls: list[tuple[int, int, str]] = []
    for cm in COLOR.finditer(masked):
        role = _classify(_prop_before(masked, cm.start()))
        if role is None:
            continue
        orig = val[cm.start():cm.end()]
        hit = MAPPING.get((role, canon_color(orig)))
        if not hit:
            continue
        token, _dark = hit
        repls.append((cm.start(), cm.end(), f"var({token},{orig})"))
    if not repls:
        return val, 0
    out = val
    for s, e, new in sorted(repls, reverse=True):
        out = out[:s] + new + out[e:]
    return out, len(repls)


def process_file(p: pathlib.Path, apply: bool) -> tuple[int, int]:
    """返回 (替换的颜色数, 修改的 style 属性数)。按字节读写保 EOL。"""
    raw = p.read_bytes()
    txt = raw.decode("utf-8")
    n_colors = 0
    n_attrs = 0
    out: list[str] = []
    last = 0
    for m in STYLE_ATTR.finditer(txt):
        quote = '"' if m.group(2) is not None else "'"
        val = m.group(2) if m.group(2) is not None else m.group(3)
        new_val, n = rewrite_style_value(val or "")
        if n:
            n_colors += n
            n_attrs += 1
            out.append(txt[last:m.start()])
            out.append(f"style={quote}{new_val}{quote}")
            last = m.end()
    if n_colors and apply:
        out.append(txt[last:])
        p.write_bytes("".join(out).encode("utf-8"))
    return n_colors, n_attrs


def emit_css() -> str:
    lines = [
        "/* theme-tokens.css —— 内联样式主题 token（P3-1 暗色收口，2026-07-23）",
        " * 单源生成：tools/inline_color_codemod.py --emit-css（勿手改，改表后重生成）。",
        " * 亮色值 = 迁移前的字面量本身（迁移后亮色渲染字节级零变化）；",
        " * 暗色值语言对齐 base.html 既有 [data-theme=dark] 色板。",
        " * 模板里的用法形如 var(--th-ink-red6,#dc2626)——fallback 恒等于 :root 亮值，",
        " * 由 tests/test_template_inline_color_ratchet.py 门禁校验一致性。 */",
        ":root{",
    ]
    seen: dict[str, str] = {}
    for role, lit, tok, _dark in _E:
        if tok in seen:
            if canon_color(seen[tok]) != canon_color(lit):
                raise SystemExit(f"token {tok} 亮值冲突: {seen[tok]} vs {lit}")
            continue
        seen[tok] = lit
        lines.append(f"  {tok}:{lit};")
    lines.append("}")
    # 两套暗色开关都认：base.html 系用 html[data-theme]（localStorage('theme')，
    # 亮/暗二态），workspace_base 系用 html[data-cp-theme]（localStorage('cp_theme')，
    # auto/亮/暗三态 + 顶栏 cpCycleTheme 按钮）。选择器并列=同一份暗值两处生效，
    # 工作台开暗色时迁移过的内联色随 --tk-*/--bdg-* 一起翻转，不再半暗半亮。
    lines.append('[data-theme="dark"],[data-cp-theme="dark"]{')
    dark_seen: dict[str, str] = {}
    for _role, _lit, tok, dark in _E:
        if dark is None or tok in dark_seen:
            if tok in dark_seen and dark is not None and dark_seen[tok] != dark:
                raise SystemExit(f"token {tok} 暗值冲突: {dark_seen[tok]} vs {dark}")
            continue
        dark_seen[tok] = dark
        lines.append(f"  {tok}:{dark};")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--dry"
    if mode == "--emit-css":
        CSS_OUT.write_text(emit_css(), encoding="utf-8", newline="\n")
        print(f"wrote {CSS_OUT}")
        return
    apply = mode == "--apply"
    total_c = total_a = 0
    for p in sorted(TPL_ROOT.rglob("*.html")):
        c, a = process_file(p, apply)
        if c:
            rel = p.relative_to(TPL_ROOT).as_posix()
            print(f"  {rel}: {c} colors in {a} attrs")
            total_c += c
            total_a += a
    print(f"{'APPLIED' if apply else 'DRY'}: {total_c} colors / {total_a} attrs")


if __name__ == "__main__":
    main()
