# -*- coding: utf-8 -*-
"""品牌强调色回填：--bd-acc/--bd-acc2 对齐官网母品牌真值（2026-07-30，已执行）。

依据《官网对齐方案_BOUNDLESS.md》§5 既定政策：「若官网线上配色不同，以官网为准，
回填到 brand.css，让两端同源」。官网（bd2026.cc）单一真相在
platform/brand/tokens.json：无界蓝＝∞ 主标渐变蓝档 --bl-brand-blue，
幻紫＝--bl-brand-violet。

为什么敢盲替换（对比 chengjie 的 Tailwind 蓝要逐点人工判定）：本仓的旧强调色
由 brand.css 定义且只此一种语义（语义色另有 ok/warn/danger/gold 族），不存在
「信息蓝 vs 品牌蓝」歧义；散落在 Python 内嵌 UI 串 / HTML / JS 里的同值字面量
都是同一强调色的复制体。

替换表（旧值以 hex 码位拼接书写，防本脚本再次扫描时**改写自身**——首版实录：
自己的 docstring/替换表被替换成新值，模式退化成 new→new 空转）：
    4f·7a·ff（旧无界蓝）        -> #1e6bf0
    79 122 255 / 79,122,255     -> 30 107 240 / 30,107,240（rgb 三元组两种形态）
    a8·55·f7（旧幻紫）          -> #7a3bf5
    168,85,247                  -> 122,59,245

幂等：重复运行零改动（旧值已不存在）。产物核对交 tools/_tokens_lint.py。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

_OLD_BLUE = "#" + "4f" + "7a" + "ff"
_OLD_VIOLET = "#" + "a8" + "55" + "f7"

REPLACEMENTS = [
    (re.compile(_OLD_BLUE, re.IGNORECASE), "#1e6bf0"),
    (re.compile(r"\b79\s+122\s+255\b"), "30 107 240"),
    (re.compile(r"\b79\s*,\s*122\s*,\s*255\b"), "30,107,240"),
    (re.compile(_OLD_VIOLET, re.IGNORECASE), "#7a3bf5"),
    (re.compile(r"\b168\s*,\s*85\s*,\s*247\b"), "122,59,245"),
]

SCAN_EXT = {".css", ".js", ".html", ".json", ".py", ".svg", ".md", ".yaml", ".yml"}
SKIP_DIRS = {"__pycache__", "archive", "logs", "runtime", "temp", "history_audio",
             "voice_previews", "songs", "share_covers", "demo_record", "secrets",
             "bg_images", "clothes", "refs", "assets", "ui_snapshots", "publish",
             "data", "vendor", "node_modules", ".git"}


def main() -> int:
    changed = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SCAN_EXT:
            continue
        if p.resolve() == SELF:
            continue  # 自排除：替换表自身含目标模式
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        n_total = 0
        for rx, rep in REPLACEMENTS:
            text, n = rx.subn(rep, text)
            n_total += n
        if n_total:
            p.write_text(text, encoding="utf-8")
            changed.append((p.relative_to(ROOT).as_posix(), n_total))
    print(f"回填完成：{len(changed)} 文件 / {sum(n for _, n in changed)} 处")
    for rel, n in changed:
        print(f"  {n:3d}  {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
