# -*- coding: utf-8 -*-
"""工作台热更模板内联 JS 语法门禁（node --check；P1 2026-08-18 固化）。

**为什么需要它**：模板保存即上生产（Jinja auto_reload），内联 `<script>` 里一个
语法错误＝整页 JS 全灭（收件箱当场变砖），而静态门禁（哑按钮/重复 id/孤儿引用）
全是文本级扫描、不解析 JS。2026-08-18 翻译右键批首次手搓了这个检查，本工具固化。

做法：提取模板全部内联 `<script>` 块 → 掩掉 Jinja（`{{ }}`→`0`、`{% %}`/`{# #}`
剔除）→ 逐块 `node --check`。掩码对语法检查安全：表达式无论在串内串外，`0` 都是
合法 token；语句块整段剔除可能留下悬空块——故仅对**掩码后仍平衡**的块断言，
含复杂 Jinja 控制流的块如实跳过并计数（宁可漏检不误报）。

用法::

    python tools/verify_template_js_syntax.py             # 门禁模式（默认扫热区模板）
    python tools/verify_template_js_syntax.py --all       # 扫 templates/ 全部
    python tools/verify_template_js_syntax.py PATH...     # 指定文件

缺 node → SKIP exit 0（gate_sweep 语义：环境缺失不污染回归信号）。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
TPL_DIR = ENGINE / "src" / "web" / "templates"

# 默认靶=巨型热区模板（内联 JS 体量大、多线并发编辑、热更即生产）
DEFAULT_TARGETS = [
    TPL_DIR / "unified_inbox.html",
    TPL_DIR / "workspace_base.html",
    TPL_DIR / "ops_overview.html",
    TPL_DIR / "personas.html",
]

_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def mask_jinja(js: str) -> tuple[str, bool]:
    """掩 Jinja。返回 (掩码后文本, 是否含语句级 Jinja——含则调用方按跳过处理)。"""
    had_stmt = bool(re.search(r"\{%-?.*?-?%\}", js, re.S))
    s = re.sub(r"\{\{.*?\}\}", "0", js, flags=re.S)
    s = re.sub(r"\{%-?.*?-?%\}", "", s, flags=re.S)
    s = re.sub(r"\{#.*?#\}", "", s, flags=re.S)
    return s, had_stmt


def check_file(path: Path, node: str) -> tuple[int, int, int]:
    """返回 (checked, failed, skipped)。失败时打印 node 报错。"""
    try:
        tpl = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"  [SKIP] {path.name}: 读取失败 {exc}")
        return 0, 0, 1
    blocks = _SCRIPT_RE.findall(tpl)
    checked = failed = skipped = 0
    for i, block in enumerate(blocks):
        if not block.strip():
            continue
        masked, had_stmt = mask_jinja(block)
        if had_stmt:
            skipped += 1        # 语句级 Jinja 剔除可能致悬空块，如实跳过
            continue
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(masked)
            tmp = Path(f.name)
        try:
            r = subprocess.run([node, "--check", str(tmp)],
                               capture_output=True, text=True, timeout=30)
            checked += 1
            if r.returncode != 0:
                failed += 1
                print(f"  [FAIL] {path.name} block#{i}:")
                print("    " + (r.stderr or "").strip().replace("\n", "\n    ")[:1500])
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
    return checked, failed, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="模板路径（缺省=热区清单）")
    ap.add_argument("--all", action="store_true", help="扫 templates/ 全部 .html")
    args = ap.parse_args()

    node = shutil.which("node")
    if not node:
        print("[SKIP] 未找到 node，跳过模板 JS 语法门禁")
        return 0

    if args.paths:
        targets = [Path(p) for p in args.paths]
    elif args.all:
        targets = sorted(TPL_DIR.rglob("*.html"))
    else:
        targets = [p for p in DEFAULT_TARGETS if p.exists()]

    total_c = total_f = total_s = 0
    for p in targets:
        c, f, s = check_file(p, node)
        total_c += c
        total_f += f
        total_s += s
        tag = "FAIL" if f else "ok"
        print(f"[{tag}] {p.relative_to(ENGINE) if p.is_absolute() else p}: "
              f"checked={c} failed={f} skipped={s}")
    print(f"== 模板 JS 语法门禁: {total_c} 块检查, {total_f} 失败, {total_s} 跳过 ==")
    return 1 if total_f else 0


if __name__ == "__main__":
    sys.exit(main())
