# -*- coding: utf-8 -*-
"""扫 src/host/*.py 找 module-level mutable global, 对比 conftest reset_targets
看缺漏 — 主动 audit 跨测试状态污染防漏 (Stage E.3, P2-⑨ 升级).

输出 3 类报告:
  1. SUSPICIOUS — module-level dict/list/Counter 但 conftest 没 reset
     (高概率污染源, 应加进 conftest inline_clears 或暴露 reset_*_for_tests)
  2. COVERED — module-level 状态有对应 reset_* 函数
  3. THREADS — module-level Thread / ThreadPoolExecutor / Event (需特殊
     shutdown 而不是 clear)

用法:
  python scripts/audit_module_globals.py
  python scripts/audit_module_globals.py --json   # 机读

不修代码, 只报告. 决策由人做.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import List, Tuple

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SRC = _ROOT / "src" / "host"
_CONFTEST = _ROOT / "tests" / "conftest.py"


# 怀疑 mutable 的类型 (Call.func id) — 这些在 module-top 赋值常是状态
_MUTABLE_BUILTINS = {"dict", "list", "set", "Counter", "OrderedDict",
                     "defaultdict"}
# 真 thread / executor — 需要 shutdown + cancel
_THREAD_TYPES = {"Thread", "ThreadPoolExecutor", "ProcessPoolExecutor",
                 "Timer"}
# sync primitives — 不持有 mutable state, 不需要 reset
_SYNC_PRIMITIVES = {"Lock", "RLock", "Semaphore", "BoundedSemaphore",
                    "Event", "Condition", "Barrier"}
# 这些是不可变的, 不计入
_IMMUTABLE_HINTS = {"frozenset", "tuple", "MappingProxyType"}


def _is_mutable_call(node: ast.AST) -> Tuple[bool, str]:
    """检查 RHS 是否构造 mutable. 返 (is_mutable, type_name)."""
    if isinstance(node, ast.Dict):
        return True, "dict"
    if isinstance(node, (ast.List, ast.Set)):
        return True, type(node).__name__.lower()
    if isinstance(node, ast.Call):
        fn = node.func
        name = ""
        if isinstance(fn, ast.Name):
            name = fn.id
        elif isinstance(fn, ast.Attribute):
            name = fn.attr
        if name in _MUTABLE_BUILTINS:
            return True, name
        if name in _THREAD_TYPES:
            return True, f"THREAD:{name}"
        if name in _SYNC_PRIMITIVES:
            # sync primitives 不持 state, 跳过
            return False, ""
    return False, ""


_MUTATING_METHODS = {"append", "extend", "pop", "popitem", "clear",
                     "update", "remove", "discard", "add", "insert",
                     "setdefault", "__setitem__", "__delitem__"}


def _module_mutates(tree: ast.AST, var_name: str) -> bool:
    """扫整个 module 找对 var_name 的真 mutation:
      - var[k] = X / del var[k]
      - var.append(...) / var.update(...) / var.clear() / ...
    排除只读 lookup (e.g. _RULES = [...] 之后只 iterate / 字典 read).
    """
    for node in ast.walk(tree):
        # subscript assign: var[k] = X
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name):
                    if tgt.value.id == var_name:
                        return True
        # del var[k]
        if isinstance(node, ast.Delete):
            for tgt in node.targets:
                if isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name):
                    if tgt.value.id == var_name:
                        return True
        # var.method(...)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (isinstance(node.func.value, ast.Name)
                    and node.func.value.id == var_name
                    and node.func.attr in _MUTATING_METHODS):
                return True
    return False


def _scan_module(path: Path) -> List[dict]:
    """返 module-level 赋值的 mutable global 信息列表 (只算真被 mutate 的)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        targets = []
        rhs = None
        if isinstance(node, ast.Assign):
            rhs = node.value
            for t in node.targets:
                if isinstance(t, ast.Name):
                    targets.append(t.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                targets.append(node.target.id)
                rhs = node.value
        if not targets or rhs is None:
            continue
        for var_name in targets:
            # 排除 ALL_CAPS 常量名 (惯例 immutable / 只读 lookup)
            if var_name.isupper() and not var_name.startswith("_"):
                continue
            is_mut, type_name = _is_mutable_call(rhs)
            if not is_mut:
                continue
            # thread 类型不查 mutate (它本身是 instance, 都需要 shutdown)
            if not type_name.startswith("THREAD:"):
                # 只读 lookup 表 (e.g. _RULES = [...] 之后只 iterate) 不计
                if not _module_mutates(tree, var_name):
                    continue
            out.append({
                "module": str(path.relative_to(_ROOT)).replace("\\", "/"),
                "var": var_name,
                "type": type_name,
                "lineno": node.lineno,
            })
    return out


def _conftest_coverage() -> Tuple[set, set]:
    """读 conftest 的 reset_targets + inline_clears, 返 (modules, vars)."""
    text = _CONFTEST.read_text(encoding="utf-8")
    # cheap: 找 ("module", "fn") tuple — module 名进 set
    import re
    mods_with_reset = set()
    for m in re.finditer(r'\("(src\.host\.[^"]+)",\s*"\w+"\)', text):
        mods_with_reset.add(m.group(1).replace(".", "/") + ".py")
    # inline_clears: ("module", "var", "type", "action")
    inline_vars = set()
    for m in re.finditer(
        r'\("(src\.host\.[^"]+)",\s*"(\w+)",\s*"\w+",\s*"\w+"\)',
        text,
    ):
        inline_vars.add(
            (m.group(1).replace(".", "/") + ".py", m.group(2))
        )
    return mods_with_reset, inline_vars


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="机读 JSON 输出")
    args = parser.parse_args()

    if not _SRC.exists():
        print(f"[FATAL] {_SRC} 不存在", file=sys.stderr)
        return 2

    all_globals = []
    for py in sorted(_SRC.glob("*.py")):
        all_globals.extend(_scan_module(py))

    mods_with_reset, inline_vars = _conftest_coverage()

    suspicious = []
    covered = []
    threads = []
    for g in all_globals:
        is_thread = g["type"].startswith("THREAD:")
        if is_thread:
            threads.append(g)
            continue
        # 模块有 reset_* 函数 → 视作 covered (粒度: module-level, 不深查 var)
        # 或者 (module, var) 在 inline_clears
        if g["module"] in mods_with_reset:
            covered.append(g)
        elif (g["module"], g["var"]) in inline_vars:
            covered.append(g)
        else:
            suspicious.append(g)

    if args.json:
        print(json.dumps({
            "suspicious": suspicious,
            "covered": covered,
            "threads": threads,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"\n{'=' * 70}")
    print(f"  conftest P2-⑨ Coverage Audit")
    print(f"{'=' * 70}")
    print(f"  scanned: {len(all_globals)} module-level mutable globals "
          f"({len(list(_SRC.glob('*.py')))} files)")
    print(f"  covered: {len(covered)}")
    print(f"  threads: {len(threads)} (need shutdown, not clear)")
    print(f"  SUSPICIOUS: {len(suspicious)} (uncovered, may pollute tests)")

    if suspicious:
        print(f"\n--- SUSPICIOUS (priority audit targets) ---")
        for g in suspicious[:30]:
            print(f"  {g['module']}:{g['lineno']}  {g['var']:<35} "
                  f"<{g['type']}>")
        if len(suspicious) > 30:
            print(f"  ... 和另外 {len(suspicious)-30} 个")

    if threads:
        print(f"\n--- THREADS (special handling) ---")
        for g in threads[:15]:
            print(f"  {g['module']}:{g['lineno']}  {g['var']:<35} "
                  f"<{g['type']}>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
