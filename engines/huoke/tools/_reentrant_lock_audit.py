"""一次性审计器：找出「持有非重入锁时又调用另一个重新获取同一把锁的方法」的自死锁隐患。

背景：2026-08-13 修复 src/ai/llm_client.py UsageStats.snapshot() 的真死锁
（持 self._lock 后调 latency_p95_sec() 二次抢同一把 threading.Lock）。本脚本用
AST 在全 src/ 里排查同类模式，防止漏网 / 复发。

判据（保守，只报高置信）：
  1) 类里有属性 = threading.Lock()（非 RLock）——记为受保护锁 attr。
  2) 某方法体顶层出现 `with self.<lockattr>:`——记为「持锁方法」。
  3) 在该 with 块内调用了 self.<other>()，且 <other> 也是同一把锁的持锁方法。
命中即高危自死锁。RLock 不报（可重入）。

用法：python tools/_reentrant_lock_audit.py [根目录，默认 src]
退出码：发现隐患=1，干净=0。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _lock_attrs(classnode: ast.ClassDef) -> dict[str, str]:
    """返回 {attr_name: 'Lock'|'RLock'}，覆盖 dataclass field(default_factory=..) 与
    __init__ 里的 self.x = threading.Lock()/RLock() 两种写法。"""
    found: dict[str, str] = {}

    def _kind_from_call(call: ast.Call) -> str | None:
        # threading.Lock() / threading.RLock() / Lock() / RLock() /
        # field(default_factory=threading.Lock)
        def _name(n: ast.AST) -> str:
            if isinstance(n, ast.Attribute):
                return n.attr
            if isinstance(n, ast.Name):
                return n.id
            return ""
        fn = _name(call.func)
        if fn in ("Lock", "RLock"):
            return fn
        if fn == "field":
            for kw in call.keywords:
                if kw.arg == "default_factory":
                    return _name(kw.value)  # threading.Lock -> "Lock"
        return None

    for node in ast.walk(classnode):
        # dataclass: x: threading.Lock = field(default_factory=threading.Lock)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Call):
                k = _kind_from_call(node.value)
                if k:
                    found[node.target.id] = k
        # self.x = threading.Lock()
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            k = _kind_from_call(node.value)
            if not k:
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) \
                        and tgt.value.id == "self":
                    found[tgt.attr] = k
                elif isinstance(tgt, ast.Name):
                    found[tgt.id] = k
    return found


def _methods_holding_lock(classnode: ast.ClassDef, lock_attr: str) -> set[str]:
    """方法名集合：方法体里存在 `with self.<lock_attr>:`。"""
    holders: set[str] = set()
    for m in classnode.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for w in ast.walk(m):
            if isinstance(w, ast.With):
                for item in w.items:
                    ce = item.context_expr
                    if isinstance(ce, ast.Attribute) and ce.attr == lock_attr \
                            and isinstance(ce.value, ast.Name) and ce.value.id == "self":
                        holders.add(m.name)
    return holders


def _self_calls_inside_with(method: ast.FunctionDef, lock_attr: str) -> list[tuple[str, int]]:
    """在该方法内 `with self.<lock_attr>:` 块体里被调用的 self.<name>()，返回 [(name,lineno)]。"""
    calls: list[tuple[str, int]] = []
    for w in ast.walk(method):
        if not isinstance(w, ast.With):
            continue
        holds = any(
            isinstance(it.context_expr, ast.Attribute)
            and it.context_expr.attr == lock_attr
            and isinstance(it.context_expr.value, ast.Name)
            and it.context_expr.value.id == "self"
            for it in w.items
        )
        if not holds:
            continue
        for n in ast.walk(w):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "self":
                calls.append((n.func.attr, n.lineno))
    return calls


def audit_file(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits: list[str] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        locks = _lock_attrs(cls)
        for attr, kind in locks.items():
            if kind != "Lock":   # RLock 可重入，跳过
                continue
            holders = _methods_holding_lock(cls, attr)
            if len(holders) < 2:
                continue
            for m in cls.body:
                if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if m.name not in holders:
                    continue
                for called, lineno in _self_calls_inside_with(m, attr):
                    if called in holders and called != m.name:
                        hits.append(
                            f"{path}:{lineno}  {cls.name}.{m.name}() 持 self.{attr} "
                            f"(非重入 Lock) 时调用 self.{called}()，而 {called}() 也抢同锁 → 自死锁"
                        )
    return hits


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "src")
    all_hits: list[str] = []
    for py in sorted(root.rglob("*.py")):
        all_hits.extend(audit_file(py))
    if all_hits:
        print(f"[reentrant-lock-audit] 发现 {len(all_hits)} 处自死锁隐患：")
        for h in all_hits:
            print("  " + h)
        return 1
    print("[reentrant-lock-audit] 干净：未发现「持非重入锁时重复抢同锁」的自死锁模式。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
