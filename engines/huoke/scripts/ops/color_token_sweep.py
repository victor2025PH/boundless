# -*- coding: utf-8 -*-
"""前端 JS 颜色令牌清欠脚本（P4·2026-08-15）。

把 JS 字符串里 **CSS 语法位** 上的硬编码色值换成 dashboard.css 语义令牌。

安全判据（为什么图表不会被换坏）：
  * 只替换前一个字符是 ``:`` ``,`` ``(`` 或空格 的 hex——这是 CSS 文本里的取值位
    （``color:#22c55e`` / ``solid #ef4444`` / ``gradient(135deg,#8b5cf6,...)``）；
  * JS 取值（Chart.js/canvas/调色板数组）的 hex 永远紧贴引号：``borderColor:'#22c55e'``、
    ``['#2196F3',...]``——引号打头，天然豁免。``var()`` 进 canvas 会静默画黑，绝不能碰；
  * 后置负向断言 ``(?![0-9a-fA-F])`` 防 8 位带 alpha 的 ``#ef444466`` 被吞前 6 位
    （P3 实锤：裸替换产出过非法的 ``var(--red-strong)66``）。

用法（构建机）：
  python scripts/ops/color_token_sweep.py            # 干跑：各文件替换数汇总
  python scripts/ops/color_token_sweep.py --sample   # 干跑 + 抽样打印替换上下文
  python scripts/ops/color_token_sweep.py --apply    # 落盘
  python scripts/ops/color_token_sweep.py --counts   # 输出 test_color_ratchet 基线 dict
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
JS_DIR = ROOT / "src" / "host" / "static" / "js"
SKIP = {"jmuxer.min.js"}  # 三方产物

# hex → 语义令牌（与 dashboard.css :root 对齐；深色值=原字面量，浅色档自动获益）
MAPPING = {
    "#22c55e": "var(--green-strong)",
    "#4ade80": "var(--green-soft)",
    "#16a34a": "var(--green-deep)",
    "#ef4444": "var(--red-strong)",
    "#f87171": "var(--red)",
    "#3b82f6": "var(--blue-strong)",
    "#60a5fa": "var(--blue-soft)",
    "#8b5cf6": "var(--violet)",
    "#a78bfa": "var(--violet-soft)",
    "#a855f7": "var(--accent-2)",
    "#06b6d4": "var(--cyan)",
    "#f59e0b": "var(--amber)",
    "#eab308": "var(--gold)",
    "#fb923c": "var(--orange)",
}

HEX_ANY = re.compile(r"(?<!&)#[0-9a-fA-F]{6}(?![0-9a-fA-F])")


def _pattern(hx: str) -> re.Pattern:
    # 前置：CSS 取值位（冒号/逗号/括号/空格）；引号/反引号紧贴的 JS 取值不匹配
    return re.compile(r"(?<=[:,\( ])" + re.escape(hx) + r"(?![0-9a-fA-F])", re.IGNORECASE)


def _style_pattern(hx: str) -> re.Pattern:
    # 第三类（P5）：DOM 样式赋值位 el.style.xxx='#hex'——引号紧贴但目标是 CSS 属性，
    # var() 合法。仍与 Chart 配置（borderColor:'#hex'，无 .style. 前缀）严格区分。
    return re.compile(
        r"(?<=\.style\.)([a-zA-Z]+)(\s*=\s*['\"])" + re.escape(hx) + r"(?![0-9a-fA-F])",
        re.IGNORECASE,
    )


def sweep(apply: bool = False, sample: bool = False, js_style: bool = False) -> dict[str, int]:
    changed: dict[str, int] = {}
    for p in sorted(JS_DIR.glob("*.js")):
        if p.name in SKIP:
            continue
        text = p.read_text(encoding="utf-8")
        total = 0
        for hx, token in MAPPING.items():
            pats = [_style_pattern(hx)] if js_style else [_pattern(hx)]
            for pat in pats:
                if sample:
                    for m in list(pat.finditer(text))[:2]:
                        s = max(0, m.start() - 42)
                        ctx = text[s:m.end() + 12].replace("\n", "\\n")
                        print(f"  [{p.name}] …{ctx}…")
                if js_style:
                    text, n = pat.subn(r"\1\2" + token, text)
                else:
                    text, n = pat.subn(token, text)
                total += n
        if total:
            changed[p.name] = total
            if apply:
                p.write_text(text, encoding="utf-8", newline="")
    return changed


def counts() -> None:
    """输出 tests/test_color_ratchet.py 的 BASELINE dict（替换后重算）。"""
    host = ROOT / "src" / "host"
    targets = [host / "dashboard.py", host / "dashboard_parts" / "sidebar.py",
               host / "static" / "l2-dashboard.html"]
    targets += sorted(JS_DIR.glob("*.js"))
    for p in targets:
        if p.name in SKIP:
            continue
        n = len(HEX_ANY.findall(p.read_text(encoding="utf-8")))
        rel = str(p.relative_to(host)).replace("\\", "/")
        print(f'    "{rel}": {n},')


def damage_scan() -> int:
    """扫替换损伤：var(--x) 后面跟 2 位 hex = 8 位色被吞（理论上判据已防住，双保险）。"""
    bad = 0
    pat = re.compile(r"var\(--[a-z0-9-]+\)[0-9a-fA-F]{2}")
    for p in sorted(JS_DIR.glob("*.js")):
        if p.name in SKIP:
            continue
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            print(f"  损伤 [{p.name}]: …{m.group(0)}…")
            bad += 1
    return bad


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--counts" in args:
        counts()
        sys.exit(0)
    result = sweep(apply="--apply" in args, sample="--sample" in args,
                   js_style="--js-style" in args)
    mode = "已落盘" if "--apply" in args else "干跑"
    print(f"[{mode}] 共 {sum(result.values())} 处 / {len(result)} 个文件:")
    for name, n in sorted(result.items(), key=lambda x: -x[1]):
        print(f"  {name}: {n}")
    if "--apply" in args:
        bad = damage_scan()
        print(f"损伤扫描: {bad} 处")
        sys.exit(1 if bad else 0)
