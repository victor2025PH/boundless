# -*- coding: utf-8 -*-
"""logger 调用「% 占位符数 ≠ 实参数」静态门禁（Q-35 #316 4HK54G，2026-09-12）。

事故形状：坐席机日志里反复出现
``--- Logging error --- TypeError: not enough arguments for format string``（**无栈帧**——
logging 在 ``handleError`` 里吞掉了，只打印一段与业务无关的 ``Logging error``），排障
只能猜是哪条 ``logger.*`` 写坏了。HEAD 全仓扫出三处，全在 ``skill_manager.py``：
乱码把格式串 / 第四个实参吃掉后留下 ``"%s… %s case=%s chain=%s"`` 配 3 个参、
``"[Auto-Pilot] … %d …, switched"``（``switched`` 被卷进字符串）、以及格式串整行被注释
掉后 ``intent`` 本身当 msg 再跟三个多余实参。

门禁口径（AST，零运行时依赖）：
- 只看 ``<logger>.debug|info|warning|warn|error|exception|critical|fatal|log(...)``，接收者名
  以 ``log`` / ``logger`` / ``_log`` / ``LOG`` / ``LOGGER`` 结尾，或 ``logging.<level>``；
- msg 必须是**字面量**（含隐式拼接 / ``+`` 拼接的字面量）——f-string、``%`` 运算、``.format``
  是另一种味道，不在本门禁范围（它们不会触发 logging 内部格式化错误）；
- ``%%`` 不算占位；``%(name)s`` 映射式必须恰好一个实参（dict）；``*`` 宽度 / 精度各多吃一参；
- 带 ``*args`` 星号实参的调用跳过（数不出来）。

扫描范围：``src/`` ``scripts/`` ``tools/`` ``main.py``（引擎根）。红了就修那一行——要么补实参、
要么把 ``%`` 写成 ``%%``；**别**往 ``_ALLOWLIST`` 塞条目（当前为空，另有门禁防其过期）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("src", "scripts", "tools")
_SCAN_FILES = ("main.py",)
_SKIP_SEGMENTS = ("/node_modules/", "/.venv/", "/venv/", "/site-packages/",
                  "/__pycache__/", "/dist/", "/build/", "/.git/")

_LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "exception",
                     "critical", "fatal", "log"})
_RECEIVER_RE = re.compile(r"(?:^|_)(?:log|logger|LOG|LOGGER)$|(?:log|logger)$")
#: %[(name)][flags][width|*][.precision|*][length]type ；type 含 % 表示转义
_FMT_RE = re.compile(
    r"%(?:\((?P<name>[^)]*)\))?[-+ #0]*(?P<w>\*|\d+)?(?:\.(?P<p>\*|\d+))?[hlL]?"
    r"(?P<t>[diouxXeEfFgGcrsa%])")

#: 良性命中登记（``"path/relative.py:lineno"``）。当前为空；加条目必须附「为什么刻意如此」。
_ALLOWLIST: dict = {}


def count_placeholders(fmt: str) -> Tuple[int, bool]:
    """返回 (占位符消耗的实参数, 是否映射式)。``%%`` 不计；``*`` 宽度 / 精度各 +1。"""
    n = 0
    mapping = False
    for m in _FMT_RE.finditer(fmt):
        if m.group("t") == "%":
            continue
        if m.group("name") is not None:
            mapping = True
        n += 1
        if m.group("w") == "*":
            n += 1
        if m.group("p") == "*":
            n += 1
    return n, mapping


def _literal_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_str(node.left), _literal_str(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _is_logger_receiver(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "logging" or bool(_RECEIVER_RE.search(node.id))
    if isinstance(node, ast.Attribute):
        return bool(_RECEIVER_RE.search(node.attr))
    return False


def scan_source(src: str, rel: str) -> List[str]:
    """扫一份源码，返回违规描述列表（空＝干净）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LEVELS or not _is_logger_receiver(node.func.value):
            continue
        args = list(node.args)
        if node.func.attr == "log":
            args = args[1:]  # 第一参是 level
        if not args or any(isinstance(a, ast.Starred) for a in args):
            continue
        msg = _literal_str(args[0])
        if msg is None:
            continue
        need, mapping = count_placeholders(msg)
        given = len(args) - 1
        if mapping:
            if given != 1:
                out.append(f"{rel}:{node.lineno}  映射式 %(name)s 需恰好 1 个 dict 实参，实给 {given}"
                           f"  :: {msg[:80]!r}")
            continue
        if need != given:
            out.append(f"{rel}:{node.lineno}  占位符 {need} 个 / 实参 {given} 个  :: {msg[:80]!r}")
    return out


def _iter_scan_files():
    for d in _SCAN_DIRS:
        base = _ENGINE_ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            sp = str(p).replace("\\", "/")
            if any(seg in sp for seg in _SKIP_SEGMENTS):
                continue
            yield p
    for name in _SCAN_FILES:
        p = _ENGINE_ROOT / name
        if p.is_file():
            yield p


def test_logger_format_placeholders_match_args():
    offenders: List[str] = []
    for p in _iter_scan_files():
        rel = p.relative_to(_ENGINE_ROOT).as_posix()
        src = p.read_text(encoding="utf-8", errors="replace")
        for line in scan_source(src, rel):
            key = line.split("  ", 1)[0]
            if key in _ALLOWLIST:
                continue
            offenders.append(line)
    assert not offenders, (
        "logger 调用 % 占位符与实参数不等（运行时＝`--- Logging error --- TypeError`，"
        "无栈帧、吞在 logging 里，4HK54G 事故形状）：\n  " + "\n  ".join(offenders)
        + "\n改法：补齐实参 / 删多余实参；字面 % 写成 %%。")


def test_allowlist_not_stale():
    """登记的良性命中必须仍然存在，否则删条目（防表过期）。"""
    if not _ALLOWLIST:
        return
    live = set()
    for p in _iter_scan_files():
        rel = p.relative_to(_ENGINE_ROOT).as_posix()
        for line in scan_source(p.read_text(encoding="utf-8", errors="replace"), rel):
            live.add(line.split("  ", 1)[0])
    stale = sorted(k for k in _ALLOWLIST if k not in live)
    assert not stale, f"_ALLOWLIST 过期条目（已不再命中，请删除）：{stale}"


# ── 扫描器自证（门禁不是摆设：坏样本必红、好样本必绿）──────────────────────────

@pytest.mark.parametrize("src", [
    'logger.info("[x] a=%s b=%s", a)',                       # 少一参
    'logger.info("[x] done %d items, switched")',           # 实参被卷进字符串
    'self.logger.warning("%s: %s -> %s (%s)", i, f, t)',    # 少一参（属性接收者）
    'log.error("[x] %s" "%s", a, b, c)',                    # 隐式拼接 2 占位 / 3 参
    'logger.debug("[x] %(k)s", a, b)',                      # 映射式多参
    'logger.log(logging.INFO, "%s %s", a)',                 # .log 第一参是 level
    'logging.warning("%s", a, b)',                          # 模块级 logging
    'logger.info("%*d", a)',                                # * 宽度多吃一参
])
def test_scanner_flags_mismatch(src):
    assert scan_source(src, "x.py"), src


@pytest.mark.parametrize("src", [
    'logger.info("[x] a=%s b=%s", a, b)',
    'logger.info("[x] 100%% done: %d", n)',
    'logger.info("[x] %(k)s", {"k": 1})',
    'logger.info(f"[x] {a}")',                              # f-string：不在本门禁范围
    'logger.info("[x] %s" % a)',                            # % 运算：同上
    'logger.info(msg, a, b)',                               # 非字面量 msg：数不出，跳过
    'logger.info("[x] %s %s", *args)',                      # 星号实参：跳过
    'logger.exception("[x] 保存失败")',                       # 零占位零参
    'logger.log(logging.INFO, "%s %s", a, b)',
    'logger.info("%*.*f", w, p, v)',
    'other.info("%s", a, b)',                               # 非 logger 接收者：不管
])
def test_scanner_passes_clean(src):
    assert not scan_source(src, "x.py"), src
