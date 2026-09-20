"""i18n 包重复键快检：同一语言 dict 里同名键出现两次＝后者静默覆盖前者。

用法：python tools/check_i18n_dup_keys.py src/web/i18n_packs/xxx.py [...]
按 AST 逐个 dict 字面量检查（不执行模块）。
"""
from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path


def main(paths: list[str]) -> int:
    rc = 0
    for p in paths:
        tree = ast.parse(Path(p).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            dups = [k for k, c in Counter(keys).items() if c > 1]
            if dups:
                rc = 1
                print(f"{p}: line {node.lineno} dict has duplicate keys: {dups[:12]}")
        print(f"{p}: checked")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
