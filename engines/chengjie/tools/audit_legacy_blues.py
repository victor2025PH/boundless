# -*- coding: utf-8 -*-
"""遗留旧蓝审计 / 收口工具（2026-07-30，品牌统一 P0 配套）。

背景
----
主色已统一到无界智连蓝（--bl-growth #1e8cf2，经 base/workspace/auth/desktop 四壳
的令牌桥生效）。但历史模板/静态资源里还散着两族旧蓝：

  * **periwinkle 紫蓝** `#5b7cf6` / `rgba(91,124,246,…)` —— base.html 壳的旧主色。
    它从来只当品牌强调色用（不是语义信息蓝），所以「裸写的 periwinkle」百分之百
    是品牌债务：主色变量已切智连蓝后，这些裸 tint（hover 边框、chip 底、面板底纹）
    和旁边 var(--p) 的文字形成肉眼可见的色差。→ 本工具可机械收口（--fix）。
  * **Tailwind 蓝** `#3b82f6` / `#2563eb` 等 —— 语义混杂：有的当品牌强调
    （主按钮/选中态），有的是**信息蓝**（info toast / KPI 调色板 / 图表系列色）。
    信息蓝是合法语义色，绝不能盲替换。→ 本工具只分文件报告，逐点人工判断。

三类**必须豁免**（不豁免会误伤合法用法）：
  1. ``var(--x, 旧蓝)`` 的 fallback —— inline_color_ratchet 门禁要求模板 fallback
     ≡ theme-tokens.css 亮值（历史字面量），实际渲染值已被 th-brand-bridge 接管；
  2. ``theme-tokens.css`` —— codemod 生成物，设计不变量就是「亮值恒=迁移前字面量」；
  3. 注释里提到旧色 —— 解释「为什么替换掉它」的文档（如 base.html 头注）。

--fix 的替换策略（只动 A 桶 periwinkle，Tailwind 族绝不自动改）：
  * ``rgba(91,124,246,α)`` → ``color-mix(in srgb, var(<主色变量>,#1e8cf2) α*100%, transparent)``
    —— 跟随品牌变量、跟随明暗主题、零新令牌、不碰生成文件；
  * ``#5b7cf6`` → ``var(<主色变量>,#1e8cf2)``；
  * 主色变量按**宿主壳**选（workspace 壳没有 --p，盲写会让整条声明失效）：
      模板 extends workspace_base → --tk-brand；其余模板 / static/js → --p；
      static/brand/** → --bl-growth（四壳都挂 brand.css）；
      static/workspace/** → --tk-brand；
    全部携带 #1e8cf2 fallback —— 变量未定义时也渲染品牌色，任何壳下安全。
  * ``src/web/kb_report.py``（自包含导出报表，不加载 brand.css）→ 纯字面量替换
    ``#1e8cf2`` / ``rgba(30,140,242,α)``；
  * 模板 ``<script>`` 块内 → 同样**字面量模式**：那里是 Chart.js 调色板 / canvas
    配色（`borderColor:'#5b7cf6'`），CSS 变量在 canvas 上下文不解析，写 var()
    会把图表画成无效色。static/js 下的 CSS-in-JS 注入样式串仍走变量模式
    （最终进 <style>，var 生效）。

用法：
    python tools/audit_legacy_blues.py            # 报告
    python tools/audit_legacy_blues.py --strict   # A 桶非空 exit 1（门禁用）
    python tools/audit_legacy_blues.py --fix      # 收口 A 桶（幂等）
配套门禁：tests/test_legacy_blue_ratchet.py（import 本模块的分类器当单一事实源）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

BRAND_HEX = "#1e8cf2"

PERI_HEX = re.compile(r"#5b7cf6\b", re.IGNORECASE)
PERI_RGBA = re.compile(r"rgba\(\s*91\s*,\s*124\s*,\s*246\s*,\s*(1|0|0?\.\d+)\s*\)")

# Tailwind 蓝族：默认只报告不自动改（品牌强调 vs 语义信息蓝需逐点人工判断）
TAILWIND = re.compile(
    r"#3b82f6\b|#2563eb\b|#1d4ed8\b|#60a5fa\b|#93c5fd\b|#bfdbfe\b|#dbeafe\b|#eff6ff\b"
    r"|rgba\(\s*59\s*,\s*130\s*,\s*246|rgba\(\s*37\s*,\s*99\s*,\s*235"
    r"|rgba\(\s*96\s*,\s*165\s*,\s*250",
    re.IGNORECASE,
)

# ── Tailwind 族「已判定全品牌」注册表 ─────────────────────────────────────
# 逐点人工审完、结论为「该文件里的 Tailwind 蓝全部是品牌强调用途（无语义信息蓝/
# 无类型调色板/无平台色）」的文件才可进表；值 = 该文件的强调变量名（与文件内既有
# var() 习惯一致）。进表后 --fix-tailwind 按下方色阶映射机械收口，门禁
# （test_legacy_blue_ratchet）把这些文件的裸 Tailwind 钉在 0。
# ⚠️ 混杂文件（如 workspace_base.html：info toast / 套餐阶梯 / 类型调色板都是
# 合法语义色）**不得**进此表——那些走手工逐点 + 门禁天花板台账。
TAILWIND_BRAND_JUDGED: dict[str, str] = {
    # 2026-07-30 全文件 100 处审毕：全部是伴随 var(--accent)/var(--tk-brand) 的
    # 品牌 tint（选中态/未读 pill/筛选 chip/焦点环/闪烁动画/AI 草稿条），
    # 语义色（成功绿/警示琥珀/危险红）在别的色族，本文件无信息蓝。
    "src/web/static/workspace/unified-inbox.css": "--accent",
}

# Tailwind → 智连蓝色阶映射（亮值取 brand.css 的 --bl-growth-* 阶）
_TW_HEX_MAP = {
    "#3b82f6": "var({acc},#1e8cf2)",
    "#2563eb": "var(--bl-growth-600,#0d76d9)",
    "#1d4ed8": "var(--bl-growth-700,#0b64b7)",
    "#60a5fa": "var(--bl-growth-400,#54a7f5)",
    "#93c5fd": "var(--bl-growth-300,#8bc4f8)",
    "#bfdbfe": "var(--bl-growth-200,#bcdcfb)",
    "#dbeafe": "var(--bl-growth-100,#ddeefd)",
    "#eff6ff": "var(--bl-growth-50,#eef6fe)",
}
_TW_RGBA = (
    (re.compile(r"rgba\(\s*59\s*,\s*130\s*,\s*246\s*,\s*(1|0|0?\.\d+)\s*\)"),
     "color-mix(in srgb, var({acc},#1e8cf2) {pct}, transparent)"),
    (re.compile(r"rgba\(\s*37\s*,\s*99\s*,\s*235\s*,\s*(1|0|0?\.\d+)\s*\)"),
     "color-mix(in srgb, var(--bl-growth-600,#0d76d9) {pct}, transparent)"),
    (re.compile(r"rgba\(\s*96\s*,\s*165\s*,\s*250\s*,\s*(1|0|0?\.\d+)\s*\)"),
     "color-mix(in srgb, var(--bl-growth-400,#54a7f5) {pct}, transparent)"),
)

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _mask_spans(text: str) -> list[tuple[int, int]]:
    """返回需豁免的区段 [(start,end)...]：注释 + var(...) 平衡括号段。

    var() 的 fallback 可含嵌套括号（rgba/渐变，甚至渐变里再套 rgba），正则数
    不了任意深度，走线性扫描配平。
    """
    spans: list[tuple[int, int]] = []
    for rx in (_CSS_COMMENT, _HTML_COMMENT):
        spans.extend(m.span() for m in rx.finditer(text))
    low = text.lower()
    i = 0
    while True:
        j = low.find("var(", i)
        if j < 0:
            break
        depth, k = 0, j + 3
        while k < len(text):
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        spans.append((j, k + 1))
        i = k + 1
    return spans


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def _scan_files() -> list[Path]:
    files: list[Path] = []
    files += sorted((REPO / "src/web/templates").rglob("*.html"))
    for ext in ("*.css", "*.js"):
        files += sorted((REPO / "src/web/static").rglob(ext))
    for ext in ("*.css", "*.js", "*.html"):
        files += sorted((REPO / "shared/copilot").rglob(ext))
    files += [REPO / "desktop/renderer/style.css", REPO / "desktop/renderer/index.html"]
    files += [REPO / "src/web/kb_report.py"]
    out = []
    for p in files:
        if not p.is_file():
            continue
        rel = p.relative_to(REPO).as_posix()
        # 生成物（亮值恒=历史字面量，由 ratchet 守）与桌面同步拷贝（源已扫）豁免
        if rel.endswith("static/theme-tokens.css"):
            continue
        if rel.startswith("desktop/renderer/shared/") or rel.startswith("desktop/renderer/brand/"):
            continue
        out.append(p)
    return out


def classify() -> dict:
    """A=裸 periwinkle（品牌债务，目标 0）；B=fallback 内（合法）；D=裸 Tailwind（人工）。"""
    bare_peri: list[tuple[str, int, str]] = []
    fallback_peri = 0
    tailwind_bare: dict[str, int] = {}
    for p in _scan_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        rel = p.relative_to(REPO).as_posix()
        spans = _mask_spans(text)
        for rx in (PERI_HEX, PERI_RGBA):
            for m in rx.finditer(text):
                if _in_spans(m.start(), spans):
                    fallback_peri += 1
                else:
                    line = text.count("\n", 0, m.start()) + 1
                    snippet = text[max(0, m.start() - 30): m.end() + 20].replace("\n", " ")
                    bare_peri.append((rel, line, snippet.strip()))
        n = sum(1 for m in TAILWIND.finditer(text) if not _in_spans(m.start(), spans))
        if n:
            tailwind_bare[rel] = n
    return {"bare_periwinkle": bare_peri, "fallback_periwinkle": fallback_peri,
            "tailwind_bare": tailwind_bare}


def _accent_var(p: Path, text: str) -> str:
    rel = p.relative_to(REPO).as_posix()
    if rel.startswith("src/web/static/brand/"):
        return "--bl-growth"
    if rel.startswith("src/web/static/workspace/"):
        return "--tk-brand"
    if rel.startswith("shared/copilot/") or rel.startswith("desktop/renderer/"):
        return "--cp-accent"
    if rel.startswith("src/web/templates/"):
        if 'extends "workspace_base.html"' in text[:400]:
            return "--tk-brand"
        return "--p"
    return "--p"


def _pct(alpha: str) -> str:
    v = float(alpha) * 100
    return f"{v:g}%"


_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)


def _script_spans(text: str, rel: str) -> list[tuple[int, int]]:
    """模板里 <script> 块的区段：其中的色值是 JS/canvas 字面量，须走字面量替换。"""
    if not rel.startswith("src/web/templates/"):
        return []
    return [m.span() for m in _SCRIPT_BLOCK.finditer(text)]


def fix() -> list[str]:
    changed: list[str] = []
    for p in _scan_files():
        text = p.read_text(encoding="utf-8")
        rel = p.relative_to(REPO).as_posix()
        spans = _mask_spans(text)
        script_spans = _script_spans(text, rel)
        file_literal = rel.endswith("kb_report.py")  # 自包含报表：无 CSS 变量可用
        var = _accent_var(p, text)

        edits: list[tuple[int, int, str]] = []
        for m in PERI_RGBA.finditer(text):
            if _in_spans(m.start(), spans):
                continue
            if file_literal or _in_spans(m.start(), script_spans):
                rep = f"rgba(30,140,242,{m.group(1)})"
            else:
                rep = f"color-mix(in srgb, var({var},{BRAND_HEX}) {_pct(m.group(1))}, transparent)"
            edits.append((m.start(), m.end(), rep))
        for m in PERI_HEX.finditer(text):
            if _in_spans(m.start(), spans):
                continue
            if file_literal or _in_spans(m.start(), script_spans):
                rep = BRAND_HEX
            else:
                rep = f"var({var},{BRAND_HEX})"
            edits.append((m.start(), m.end(), rep))
        if not edits:
            continue
        for s, e, rep in sorted(edits, reverse=True):
            text = text[:s] + rep + text[e:]
        p.write_text(text, encoding="utf-8")
        changed.append(f"{rel} ({len(edits)} 处, 变量 {var if not file_literal else '字面量'})")
    return changed


def fix_tailwind() -> list[str]:
    """只对 TAILWIND_BRAND_JUDGED 注册表内的文件做 Tailwind→智连蓝机械收口。"""
    changed: list[str] = []
    for rel, acc in TAILWIND_BRAND_JUDGED.items():
        p = REPO / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        spans = _mask_spans(text)
        edits: list[tuple[int, int, str]] = []
        for rx, tpl in _TW_RGBA:
            for m in rx.finditer(text):
                if _in_spans(m.start(), spans):
                    continue
                edits.append((m.start(), m.end(),
                              tpl.format(acc=acc, pct=_pct(m.group(1)))))
        for hexv, tpl in _TW_HEX_MAP.items():
            for m in re.finditer(re.escape(hexv) + r"\b", text, re.IGNORECASE):
                if _in_spans(m.start(), spans):
                    continue
                edits.append((m.start(), m.end(), tpl.format(acc=acc)))
        if not edits:
            continue
        for s, e, rep in sorted(edits, reverse=True):
            text = text[:s] + rep + text[e:]
        p.write_text(text, encoding="utf-8")
        changed.append(f"{rel} ({len(edits)} 处, 变量 {acc})")
    return changed


def main(argv: list[str]) -> int:
    if "--fix" in argv:
        changed = fix()
        print("已收口文件：" if changed else "无可收口的裸 periwinkle。")
        for c in changed:
            print(f"  {c}")
        # fix 后立即复检
        argv = [a for a in argv if a != "--fix"]
    if "--fix-tailwind" in argv:
        changed = fix_tailwind()
        print("Tailwind 已判定文件收口：" if changed else "注册表文件无可收口项。")
        for c in changed:
            print(f"  {c}")
        argv = [a for a in argv if a != "--fix-tailwind"]

    report = classify()
    bare = report["bare_periwinkle"]
    print(f"\nA 裸 periwinkle（品牌债务，应为 0）：{len(bare)}")
    for rel, line, snip in bare[:40]:
        print(f"  {rel}:{line}  {snip}")
    print(f"B var() fallback 内 periwinkle（棘轮要求保留）：{report['fallback_periwinkle']}")
    d = report["tailwind_bare"]
    print(f"D 裸 Tailwind 蓝（语义混杂，人工逐点判断）：{sum(d.values())} 处 / {len(d)} 文件")
    for rel, n in sorted(d.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {n:4d}  {rel}")
    if "--strict" in argv and bare:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
