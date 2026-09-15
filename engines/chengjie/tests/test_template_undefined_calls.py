# -*- coding: utf-8 -*-
"""模板内联 JS「调用了全站找不到定义的私有函数」门禁（2026-09-15 `_psnArRender` 事故沉淀）。

事故：`personas.html` 的 `_fillProfileTabFields` 里加了一行 `_psnArRender(p, bnd)`，函数
本体从未落盘（Q-26 C 半成品 hunk）。模板热更新直上生产 → 新建 / 编辑人设一律
ReferenceError，人设工坊整体不可用 **3.5 天**，零告警。复盘两层：
① **流程**：``test_template_undefined_identifiers`` 其实抓得到这个形状（事后回放确认）——
   但那条线没跑 gate_sweep 就把半成品留在了工作树，而 sweep 常年 18–27 条他线红，一条
   新红淹在噪音里。门禁存在 ≠ 门禁被看。
② **机制**：那条门禁的声明收集刻意「过度包含」（``var x = NAME(...)`` 连 RHS 一起收作
   已声明，换零误报）——代价是 ``const row = _rowByPid(pid)`` / ``const badge = … ?
   _intentBadge(t) : ''`` 这类**调用结果被赋值**的形状永远漏判。本门禁首跑即在
   ``_rpa_shared_scripts.html`` / ``_channel_body_whatsapp.html`` 抓出这两个在库的真
   ReferenceError（批量操作全哑 / 带意图标签的 WA 会话展开即崩）。
   两条门禁互补：那条管**任意名字 + 宽声明**，本条管**私有前缀名 + 严格 LHS 声明**。

判据（刻意窄，宁漏勿误）：
- 只看**掩码后**（剥字符串 / 模板字面量 / 注释 / 正则）的内联 ``<script>`` 正文里的
  调用位 ``NAME(``；
- 只看 ``^_[A-Za-z]\\w+$`` 形态的名字——本仓私有 helper 的命名约定（``_pfSetVal`` /
  ``_setAdultPolicy`` / ``_psnArRender``…）。裸名（``esc`` / ``loadX``）可能来自任意
  CDN / 共享脚本，``__x`` 双下划线名多为跨壳 window 契约，两类都不在本门禁口径；
- 定义面＝本模板任意作用域的 function / const / let / var / window 暴露 + 函数形参
  + 本模板 ``<script src="/static/…">`` 引用的本地静态 JS + base / workspace_base /
  全部 ``_*.html`` partial（``{% extends %}`` / ``{% include %}`` 跨文件全局）。

``_PENDING`` 登记「当前确实坏着、需产品决策」的条目（保 CI 绿 + 债务可见），
``test_pending_still_broken`` 防其过期。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._inline_handler_scan import (
    _CALL,
    _mask,
    _script_bodies,
    ambient_globals,
    defined,
    local_static_globals,
)

TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
STATIC_ROOT = Path(__file__).resolve().parents[1] / "src" / "web" / "static"

_PRIVATE = re.compile(r"^_[A-Za-z]\w+$")
_PARAMS = re.compile(r"(?:function\b[^(]*|=>|\bcatch)\s*\(([^)]*)\)")
_ARROW_SINGLE = re.compile(r"(?<![\w$])(_[A-Za-z]\w*)\s*=>")

#: 已知坏着的条目：{模板文件名: {函数名, ...}}。加条目必须附原因注释。
_PENDING: dict = {}


_DECL_STMT = re.compile(r"\b(?:var|let|const)\s+([^;{}]*?)(?=;|\n|$)")


def _param_names(js: str) -> set:
    names = set()
    for m in _PARAMS.finditer(js):
        for tok in re.split(r"[,\s=]+", m.group(1)):
            tok = tok.strip().lstrip(".")
            if _PRIVATE.match(tok):
                names.add(tok)
    for m in _ARROW_SINGLE.finditer(js):
        names.add(m.group(1))
    # 逗号声明列表 `var _a=0,_b=null,_closeFn=null;`——`defined()` 只认首个名字
    for m in _DECL_STMT.finditer(js):
        for seg in m.group(1).split(","):
            tok = seg.strip().split("=")[0].strip()
            if _PRIVATE.match(tok):
                names.add(tok)
    return names


def _base_defined() -> set:
    d = set()
    for name in ("base.html", "workspace_base.html"):
        p = TPL_DIR / name
        if p.is_file():
            d |= defined(p.read_text(encoding="utf-8"))
    return d


def undefined_private_calls(html: str, *, ambient: set, base_defs: set) -> list:
    """返回本模板内联 JS 调用了、但任何定义面都找不到的私有函数名（排序去重）。"""
    bodies = _script_bodies(html)
    if not bodies:
        return []
    joined = "\n".join(bodies)
    known = defined(html) | _param_names(joined) | ambient | base_defs
    known |= local_static_globals(html, STATIC_ROOT)
    calls = set()
    for body in bodies:
        for m in _CALL.finditer(_mask(body)):
            name = m.group(1)
            if _PRIVATE.match(name):
                calls.add(name)
    return sorted(calls - known)


def _all_templates():
    return sorted(TPL_DIR.rglob("*.html"))


def _scan_all() -> dict:
    amb = ambient_globals(TPL_DIR)
    base_defs = _base_defined()
    out = {}
    for f in _all_templates():
        html = f.read_text(encoding="utf-8")
        missing = undefined_private_calls(html, ambient=amb, base_defs=base_defs)
        if missing:
            out[f.name] = missing
    return out


def test_no_undefined_private_calls():
    found = _scan_all()
    failures = {}
    for fname, names in found.items():
        unexpected = [n for n in names if n not in _PENDING.get(fname, set())]
        if unexpected:
            failures[fname] = unexpected
    assert not failures, (
        "模板内联 JS 调用了**全站找不到定义**的私有函数（运行时 ReferenceError，"
        "模板热更新会直接把它送上生产）：\n    "
        + "\n    ".join(f"{k}: {v}" for k, v in failures.items())
        + "\n  修法：补上函数定义 / 改正拼写 / 删掉调用；若是半成品请先别落盘调用行。"
    )


def test_pending_still_broken():
    """_PENDING 登记的条目一旦修好就该从表里删掉（防台账过期）。"""
    found = _scan_all()
    stale = {}
    for fname, names in _PENDING.items():
        still = set(found.get(fname, []))
        gone = set(names) - still
        if gone:
            stale[fname] = sorted(gone)
    assert not stale, f"_PENDING 里这些条目已不再坏，请从表中移除：{stale}"


def test_scanner_self_check():
    """探测器自证：调用未定义的私有函数必被抓；定义了 / 形参 / 字符串里的不抓。"""
    html = (
        "<script>\n"
        "function _ok(){}\n"
        "function run(_cb){ _ok(); _cb(); _missing(1); var s = '_inString()'; /* _inComment() */ }\n"
        "</script>"
    )
    got = undefined_private_calls(html, ambient=set(), base_defs=set())
    assert got == ["_missing"], got


def test_scanner_catches_the_incident_shape():
    """复刻事故形状：调用行落盘、定义缺席。"""
    html = "<script>function _fill(p){ _setAdultPolicy(p.a); _psnArRender(p, p.b); }\nfunction _setAdultPolicy(x){}</script>"
    got = undefined_private_calls(html, ambient=set(), base_defs=set())
    assert got == ["_psnArRender"]
