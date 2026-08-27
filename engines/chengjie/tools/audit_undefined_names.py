# -*- coding: utf-8 -*-
"""审计 Python **未定义名**（pyflakes F821），只读，随时可跑。

**为什么需要它**（2026-08-27 实锤事故）：``src/inbox/health_watchdog.py`` 的
``_check_true_probes`` 里写了 ``Path(...)``，而该模块**没有顶层 pathlib 导入**
（同文件另两处同类函数都是函数内局部导入）。这是**语法合法**的代码，于是：

  · ``gate_sweep`` 的 step 0（``py_compile`` 语法扫描）扫不出来——它只证明「能解析」；
  · 该方法抛出的 ``NameError`` 被 ``_tick`` 外层 except 以 **DEBUG 级**吞掉；
  · 结果：四域真活探针从重启起整段不跑，日志零正面痕迹，唯一信号是
    「``[true_probe] 轮次完成``」那行**不再出现**——一个需要人主动去数的负面信号。

潜伏了一整个重启周期才被发现。同一个 ``_tick`` 里还有约 20 个 ``_check_*`` 方法，
任何一个漏个局部 import 都会以完全相同的方式静默死掉。模板侧早有内联 JS 语法门禁
（一个语法错误 brick 整个收件箱），Python 侧此前没有对应物——本工具补这个真空。

**范围刻意很窄**：只管「未定义名」这一类。不做行宽/import 排序/未使用变量等——那会
翻出上千条既有告警、波及所有线，属于另一个立项，且**假阳性会让门禁被所有人无视**。

为什么用 pyflakes 而不是手写 AST 检查器：要正确处理 comprehension 作用域、walrus、
global/nonlocal、类体作用域、嵌套函数闭包、try/except 内条件导入、``if TYPE_CHECKING``、
``from x import *``，假阳性风险很高。pyflakes 纯 Python、无需配置、这些全都处理过了。

**两类命中，后果完全不同**（门禁台账逐条标注，别当成同一回事）：

  REAL        运行到该行必抛 ``NameError``。本仓高发形态＝外层 ``except`` 把它吞掉，
              于是整个功能静默死亡。**首跑当天就捞出 6 处既有的**（缺 import ×4、
              一处编码损坏吃掉了赋值语句、一处嵌套 helper 误取 ``request``），均已修复。
  annotation  只出现在**不会被求值**的注解位（模块有 ``from __future__ import
              annotations``，或字符串注解，或函数内局部变量注解——后者 Python 从不求值）。
              不会崩，但仍是「写了个不存在的名字」，留在台账里可见。

基线（2026-08-27）：src/ 首跑 23 处/12 文件 → 修完 REAL 后 **13 处/6 文件**；
``tools/`` 与 ``scripts/`` 一直是 **0**。

用法::

    python tools/audit_undefined_names.py                 # src/ tools/ scripts/ 报告
    python tools/audit_undefined_names.py --strict        # 有命中则退出码 1
    python tools/audit_undefined_names.py --dirs tests    # 换扫描目录
    python tools/audit_undefined_names.py --json          # 机器可读

**pyflakes 缺失 → 打印 SKIP 并 exit 0**（本仓惯例，同 ``tools/verify_*.py`` 系列）：
门禁缺工具时不许污染回归信号。

**语法错误的文件一律跳过、绝不算命中**：共享工作树上 sibling 半存盘是常态
（gate_sweep step 0 因此刻意只 warn），把「此刻正在存盘」渲染成红灯只会制造瞬态红。

配套硬门禁＝``tests/test_python_undefined_names.py``（棘轮：既有违规登记在案、
天花板只降不升、只禁新增），本工具是它的**单一事实源**（分类逻辑不许两处各写一套）。
"""
from __future__ import annotations

import argparse
import json as _json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ENGINE_ROOT = Path(__file__).resolve().parents[1]

#: 门禁默认覆盖面。tests/ 刻意不在内——测试里的未定义名**跑一次就大声失败**，
#: 而本门禁存在的理由恰恰是「静默死在生产路径上」。（实测 tests/ 10 处/6 文件，
#: 想纳管加 --dirs tests 即可，成本 ~15s。）
DEFAULT_DIRS: Tuple[str, ...] = ("src", "tools", "scripts")

_SKIP_DIR_PARTS = frozenset({
    "__pycache__", "node_modules", ".git", ".venv", "venv", "env",
    "site-packages", ".mypy_cache", ".pytest_cache", ".tox", "build", "dist",
})

# `# noqa` / `# noqa: F821,F401` —— 站点级豁免。刻意支持：这是通用惯例，且本仓已在用
# （tests/test_cockpit.py 的 `def _noop_auth(request: "Request")  # noqa: F821 - 注解用`）。
# 比远处的台账更好的一点是：豁免就写在出事那行旁边，review diff 时看得见。
_NOQA = re.compile(r"#\s*noqa(?::\s*(?P<codes>[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*))?",
                   re.IGNORECASE)
_OUR_CODE = "F821"


@dataclass(frozen=True)
class Finding:
    """一处未定义名。``path`` 是相对引擎根的 posix 路径（台账键必须跨平台稳定）。"""
    path: str
    line: int
    name: str

    def __str__(self) -> str:  # 报告/断言消息用
        return f"{self.path}:{self.line}  undefined name '{self.name}'"


@dataclass
class ScanResult:
    findings: List[Finding]
    #: 解析不了的文件（sibling 半存盘 / BOM / py2 残留）——只报告，**从不**算命中
    unparseable: List[Tuple[str, str]]
    files_scanned: int

    def by_file(self) -> Dict[str, Set[str]]:
        """``{相对路径: {未定义名, ...}}``——棘轮台账的比对口径。"""
        out: Dict[str, Set[str]] = {}
        for f in self.findings:
            out.setdefault(f.path, set()).add(f.name)
        return out


def pyflakes_version() -> Optional[str]:
    """返回已装 pyflakes 版本；未安装返回 None（调用方据此 SKIP）。"""
    try:
        import pyflakes
    except Exception:
        return None
    return getattr(pyflakes, "__version__", "unknown")


def iter_python_files(dirs: Sequence[str] = DEFAULT_DIRS,
                      root: Path = ENGINE_ROOT) -> List[Path]:
    """收集待扫描 .py（排序＝结果确定性；跳过缓存/依赖目录）。"""
    files: List[Path] = []
    for d in dirs:
        base = root / d
        if not base.is_dir():
            continue
        for f in base.rglob("*.py"):
            if _SKIP_DIR_PARTS & set(f.parts):
                continue
            files.append(f)
    return sorted(set(files))


def _noqa_suppressed(source_line: str) -> bool:
    """该行是否带可豁免本检查的 noqa（裸 noqa 或显式列了 F821）。"""
    m = _NOQA.search(source_line)
    if not m:
        return False
    codes = m.group("codes")
    if not codes:
        return True  # 裸 `# noqa` 抑制一切
    return _OUR_CODE in {c.strip().upper() for c in codes.split(",")}


def scan_file(path: Path, root: Path = ENGINE_ROOT) -> Tuple[List[Finding], Optional[str]]:
    """扫一个文件 → ``(findings, unparseable_reason)``。

    只消费 pyflakes 的 ``UndefinedName``（F821）。同族的 F822（``__all__`` 里的死名）/
    F823（先用后赋的局部名）**刻意不纳入**：本次立项范围就是 F821，且实测基线里两者
    都为 0——真要纳管应当另立一项、单独跑基线，而不是顺手扩大。
    """
    from pyflakes import api as _api          # 延迟导入：未装 pyflakes 时本模块仍可 import
    from pyflakes import messages as _msgs
    from pyflakes.reporter import Reporter

    collected: List[Finding] = []
    problems: List[str] = []

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return [], f"read failed: {exc!r}"

    lines = text.splitlines()
    # 两侧都 resolve 再相减：本机 D:\boundless 是指向 D:\workspace\boundless 的目录联接，
    # 只 resolve 一边会让 relative_to 落空 → 台账键退化成绝对路径（跨机不稳定）。
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        rel = path.as_posix()

    class _Collector(Reporter):
        def __init__(self) -> None:  # 不调用父类 __init__（它要两个 stream）
            pass

        def unexpectedError(self, filename, msg):
            problems.append(str(msg))

        def syntaxError(self, filename, msg, lineno, offset, text_):
            problems.append(f"{msg} (line {lineno})")

        def flake(self, message):
            if not isinstance(message, _msgs.UndefinedName):
                return
            lineno = int(getattr(message, "lineno", 0) or 0)
            src = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
            if _noqa_suppressed(src):
                return
            collected.append(Finding(rel, lineno, str(message.message_args[0])))

    try:
        _api.check(text, str(path), _Collector())
    except Exception as exc:  # pyflakes 自身炸了也不许拖垮门禁
        return [], f"pyflakes raised: {exc!r}"

    if problems:
        return [], problems[0]
    return collected, None


def _scan_one(payload: Tuple[str, str]) -> Tuple[List[Tuple[str, int, str]], Optional[str], str]:
    """进程池 worker（顶层函数才可 pickle）。"""
    path_s, root_s = payload
    found, bad = scan_file(Path(path_s), Path(root_s))
    return [(f.path, f.line, f.name) for f in found], bad, path_s


def scan(dirs: Sequence[str] = DEFAULT_DIRS, root: Path = ENGINE_ROOT,
         jobs: int = 1) -> ScanResult:
    """扫描 ``dirs`` 并返回 :class:`ScanResult`。

    **默认串行（``jobs=1``），这是刻意的。** 实测 src+tools+scripts（1395 文件）串行
    ~24s、8 进程池 ~8s；但进程池在 Windows 走 spawn，会**重新导入调用方的 ``__main__``**
    —— 调用方若没写 ``if __name__ == "__main__":`` 卫句，整个脚本会被每个 worker 重跑一遍
    （本工具自查时实测重跑 8 次）。省下的 16s 在门禁文件里本就被共享 conftest 的
    ~4s/测试 装夹开销淹没，**不值得用一个 fork-bomb 脚枪去换**。

    人在安静机器上手跑大批量可以 ``--jobs 8`` 显式提速；此时调用方**必须**有 main 卫句。
    子进程内一律强制串行（把递归钉死在一层，别让脚枪变成炸弹）。
    """
    files = iter_python_files(dirs, root)
    findings: List[Finding] = []
    unparseable: List[Tuple[str, str]] = []

    use_pool = jobs > 1 and len(files) > 120
    if use_pool:
        import multiprocessing
        if multiprocessing.current_process().name != "MainProcess":
            use_pool = False  # 已在子进程里：绝不再套一层（无卫句调用方的递归止损）

    if use_pool:
        try:
            from concurrent.futures import ProcessPoolExecutor
            payload = [(str(f), str(root)) for f in files]
            with ProcessPoolExecutor(max_workers=jobs) as ex:
                for rows, bad, path_s in ex.map(_scan_one, payload, chunksize=24):
                    if bad:
                        unparseable.append((Path(path_s).name, bad))
                    findings.extend(Finding(*r) for r in rows)
            findings.sort(key=lambda f: (f.path, f.line, f.name))
            unparseable.sort()
            return ScanResult(findings, unparseable, len(files))
        except Exception as exc:
            # 绝不静默：无 main 卫句时这里收到的是 RuntimeError，而调用方脚本正在被
            # 重复执行——闷掉它就会像「跑了 8 遍还以为只跑了一遍」那样难查。
            print(f"[audit_undefined_names] 进程池不可用，退回串行: {exc!r}"
                  f"（调用方缺 `if __name__ == \"__main__\":` 卫句？）", file=sys.stderr)
            findings, unparseable = [], []   # 丢弃半截结果重来

    for f in files:
        rows, bad = scan_file(f, root)
        if bad:
            unparseable.append((f.name, bad))
        findings.extend(rows)
    findings.sort(key=lambda x: (x.path, x.line, x.name))
    unparseable.sort()
    return ScanResult(findings, unparseable, len(files))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dirs", nargs="*", default=list(DEFAULT_DIRS),
                    help=f"扫描目录（相对引擎根），默认 {' '.join(DEFAULT_DIRS)}")
    ap.add_argument("--strict", action="store_true", help="有命中则退出码 1")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并行进程数（默认 1=串行；见 scan() docstring 里为什么不默认并行）")
    args = ap.parse_args(argv)

    ver = pyflakes_version()
    if ver is None:
        print("SKIP: pyflakes 未安装（pip install pyflakes）——不污染回归信号")
        return 0

    res = scan(args.dirs, ENGINE_ROOT, jobs=args.jobs)

    if args.as_json:
        print(_json.dumps({
            "pyflakes": ver,
            "dirs": list(args.dirs),
            "files_scanned": res.files_scanned,
            "findings": [{"path": f.path, "line": f.line, "name": f.name}
                         for f in res.findings],
            "unparseable": [{"file": n, "reason": r} for n, r in res.unparseable],
        }, ensure_ascii=False, indent=2))
        return 1 if (args.strict and res.findings) else 0

    print(f"pyflakes {ver} | 扫描 {res.files_scanned} 个 .py | 目录: {' '.join(args.dirs)}")
    by_file = res.by_file()
    if not res.findings:
        print("未定义名: 0  (clean)")
    else:
        print(f"未定义名: {len(res.findings)} 处 / {len(by_file)} 个文件\n")
        cur = None
        for f in res.findings:
            if f.path != cur:
                cur, names = f.path, sorted(by_file[f.path])
                print(f"  {f.path}   [{', '.join(names)}]")
            print(f"      L{f.line}: {f.name}")
    if res.unparseable:
        print(f"\n跳过（解析失败，不计命中；sibling 半存盘是常态）: {len(res.unparseable)}")
        for n, r in res.unparseable[:10]:
            print(f"  {n}: {r}")
    return 1 if (args.strict and res.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
