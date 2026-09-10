#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导入扫雷：把 ``src/`` 下每个模块真 import 一遍 + 校验每个 ``from src.X import name`` 的 name 真存在。

来历（R79 / 实施97 入库树验证，2026-09-10）：``tmp/r79_import_sweep.py`` + ``tmp/r79_import_names_sweep.py``
两个一次性脚本——它们抓的是 **pytest 收集不到的那层**：只被生产进程 import、没有任何测试 import 的模块
（漏 import / 循环 import / 被删符号仍被引用），这些在 ``py_compile`` 与 pyflakes F821 之间有个缝：
文件能 parse、名字在文件内也「定义了」（from-import 语句本身就是定义），只有真 import 才炸。
共享工作树上多线并行时这类事故不少见（一线删了符号，另一线的文件还在 from-import 它）。

两段：
  1. ``modules``：``src/**/*.py`` 逐个 ``importlib.import_module``（跳过 ``__main__``）；任何异常都记坏。
  2. ``names``：AST 扫 ``from src.X import a, b``（绝对导入），``hasattr(module, name)`` 或 ``X.name`` 可 import。

退出码：0 = 全绿；1 = 有坏项（gate_sweep.ps1 折进总 exit code）。输出 ASCII/UTF-8 皆可（stdout 强制 UTF-8）。
用法（引擎根或任意 cwd）：``python scripts/import_sweep.py [--modules-only|--names-only] [--json]``

隔离：与 tests/conftest.py 同款——``AITR_DATA_DIR`` 指到进程级 tmp（import 期就会建库/写文件的模块不得
落进仓库 ``config/``），``HOST_ALERT_SILENT=1``（key 失效路径不弹系统弹窗）。耗时 ~25s（1200+ 模块）。
"""
from __future__ import annotations

import argparse
import ast
import importlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _isolate_env() -> None:
    os.environ.setdefault("HOST_ALERT_SILENT", "1")
    for k in list(os.environ):
        if k.startswith("AITR_") and k not in ("AITR_DATA_DIR",):
            os.environ.pop(k, None)
    root = Path(tempfile.mkdtemp(prefix="aitr-import-sweep-"))
    (root / "config").mkdir(parents=True, exist_ok=True)
    os.environ["AITR_DATA_DIR"] = str(root)
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")


def _module_name(p: Path) -> str | None:
    rel = p.relative_to(ENGINE_ROOT).with_suffix("")
    if rel.name == "__main__":
        return None
    parts = rel.parts[:-1] + (() if rel.name == "__init__" else (rel.name,))
    return ".".join(parts)


def sweep_modules() -> tuple[int, list[tuple[str, str]]]:
    bad: list[tuple[str, str]] = []
    n = 0
    for p in sorted((ENGINE_ROOT / "src").rglob("*.py")):
        mod = _module_name(p)
        if not mod:
            continue
        n += 1
        try:
            importlib.import_module(mod)
        except (ImportError, NameError, SyntaxError, AttributeError) as e:
            bad.append((mod, f"{type(e).__name__}: {e}"))
        except Exception as e:  # noqa: BLE001 - 扫雷要把一切 import 期异常都点名
            bad.append((mod, f"(other) {type(e).__name__}: {str(e)[:160]}"))
    return n, bad


_GUARD_EXC = {"Exception", "BaseException", "ImportError", "ModuleNotFoundError"}
_GUARDED_SKIPPED = [0]


def _guarded_imports(tree: ast.AST) -> set[int]:
    """``try: from src.X import y  except (ImportError|Exception|裸 except): ...`` 里的 ImportFrom 节点 id。

    这类是**有意的可选依赖 / 防环回落**（模块不存在就走旧行为），import 失败是设计内的，不算雷。
    只有 handler 能接住 ImportError 的 try 才算护住；``except ValueError`` 护不住仍要报。
    """
    out: set[int] = set()

    def _catches(h: ast.ExceptHandler) -> bool:
        if h.type is None:
            return True
        names = [h.type] if not isinstance(h.type, ast.Tuple) else list(h.type.elts)
        for n in names:
            nm = n.id if isinstance(n, ast.Name) else (n.attr if isinstance(n, ast.Attribute) else "")
            if nm in _GUARD_EXC:
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_catches(h) for h in node.handlers):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.ImportFrom):
                        out.add(id(sub))
    return out


def sweep_names() -> tuple[int, list[tuple[str, str]]]:
    bad: list[tuple[str, str]] = []
    checked = 0
    cache: dict[str, object] = {}
    for p in sorted((ENGINE_ROOT / "src").rglob("*.py")):
        rel = p.relative_to(ENGINE_ROOT).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            bad.append((rel, f"parse {type(e).__name__}: {e}"))
            continue
        guarded = _guarded_imports(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module and node.level == 0
                    and node.module.startswith("src.")):
                continue
            if id(node) in guarded:
                _GUARDED_SKIPPED[0] += 1
                continue
            m = node.module
            if m not in cache:
                try:
                    cache[m] = importlib.import_module(m)
                except Exception as e:  # noqa: BLE001
                    cache[m] = e
            mod = cache[m]
            if isinstance(mod, Exception):
                bad.append((f"{rel}:{node.lineno}", f"module {m} -> {type(mod).__name__}: {str(mod)[:120]}"))
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                checked += 1
                if hasattr(mod, a.name):
                    continue
                try:
                    importlib.import_module(f"{m}.{a.name}")
                except Exception:  # noqa: BLE001
                    bad.append((f"{rel}:{node.lineno}", f"{m} has no {a.name}"))
    return checked, bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--modules-only", action="store_true")
    g.add_argument("--names-only", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine-readable summary on the last line")
    args = ap.parse_args(argv)

    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    _isolate_env()
    os.chdir(ENGINE_ROOT)
    if str(ENGINE_ROOT) not in sys.path:
        sys.path.insert(0, str(ENGINE_ROOT))

    summary: dict[str, object] = {}
    total_bad = 0
    if not args.names_only:
        n, bad = sweep_modules()
        total_bad += len(bad)
        summary["modules"] = {"checked": n, "bad": len(bad)}
        print(f"[import_sweep] modules={n} bad={len(bad)}")
        for m, e in bad:
            print(f"  BAD {m} :: {e[:220]}")
    if not args.modules_only:
        n, bad = sweep_names()
        total_bad += len(bad)
        summary["names"] = {"checked": n, "bad": len(bad), "guarded_skipped": _GUARDED_SKIPPED[0]}
        print(f"[import_sweep] from-import names checked={n} bad={len(bad)} "
              f"(try/except-guarded optional imports skipped={_GUARDED_SKIPPED[0]})")
        for w, e in bad:
            print(f"  BAD {w} :: {e[:220]}")
    if args.json:
        print(json.dumps({"ok": total_bad == 0, **summary}, ensure_ascii=False))
    print("[import_sweep] " + ("ALL GREEN" if total_bad == 0 else f"FAILURES: {total_bad}"))
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
