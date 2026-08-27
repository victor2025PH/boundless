# -*- coding: utf-8 -*-
"""跨作用域「幽灵引用」门禁（2026-08-22 顶栏药丸全灭事故的机制化沉淀；同日收紧到 IIFE 颗粒度）。

事故：workspace_base.html 的 ``_setPill``/``_pillIc`` 定义在一个 IIFE 里，消费方在**另一个**
IIFE 里裸引用——运行时 ReferenceError 被 promise 的 ``.catch(function(){})`` 吞掉：页面零报错、
遥测零信号，顶栏四颗行动药丸静默全灭数周。首版门禁按 <script> 块为颗粒度当天又抓到 ``_ui``
同款（确认弹窗图标/SLA 下钻行/偏好关闭钮/授权横幅四处条件性炸）。

**Phase2 收紧（同日）**：同一个 <script> 里的多个顶层 IIFE 之间同样不共享作用域——块级
颗粒度对「块内跨 IIFE」是漏报区。现按 **IIFE 作用域**建模：

    作用域 = 每个顶层 ``(function(){...})()``/``(async function...)``/``((...)=>{...})()``
             一个；非 IIFE 的顶层代码（含顶层 if/try 块内部）归**全局池**。
    可达   = 本作用域内任何形式的名字（函数声明 / var·let·const 含解构 / 一切形参 /
             catch 参数——宽口径刻意吞掉遮蔽类误报）∪ 全局池 ∪ window 暴露 ∪
             本地静态 JS 全局 ∪ base/partials 全局 ∪ 内建。
    违规   = 某作用域（或全局代码）裸调用 NAME，全站不可达，**且** NAME 恰好在同文件
             另一个 IIFE 作用域里有定义——运行时必 ReferenceError 的确定性形态。

    刻意不抓「同步调用晚于声明块执行」的时序类（静态判定会误报）；刻意把顶层块语句
    （if/try 等）内容记作全局（sloppy mode 函数声明近似全局，且方向是减少误报）。

维护：良性命中登记 ``_ACCEPTED``（附原因）、真 bug 待修登记 ``_PENDING``，两表有防过期
测试；探测器自证（事故原型必报警 / window 挂载必放行 / 块内 IIFE 对必报警）。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests._inline_handler_scan import (
    _mask,
    _script_bodies,
    _strip_html_comments,
    ambient_globals,
    local_static_globals,
    window_exposed,
)

TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
STATIC_ROOT = Path(__file__).resolve().parents[1] / "src" / "web" / "static"

# JS/DOM 内建与宿主全局：只在「同名恰好也被某 IIFE 局部定义」时才进入候选，
# 故只需覆盖模板里实际撞过名的（按全站扫描校准维护）。
_BUILTIN_ALLOW = {
    "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "requestAnimationFrame", "alert", "confirm", "prompt", "String", "Number",
    "Boolean", "Array", "Object", "JSON", "Date", "Math", "RegExp", "Promise",
    "Error", "Map", "Set", "WeakMap", "parseInt", "parseFloat", "isNaN",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "CustomEvent", "EventSource", "URLSearchParams", "Blob", "FormData",
    "AbortController", "Audio", "Image", "WebSocket", "MutationObserver",
    "IntersectionObserver", "ResizeObserver", "structuredClone", "atob", "btoa",
    # 宿主全局（首轮 IIFE 颗粒度全扫校准）：ui-build 探针 IIFE 有 `var URL='…txt'`
    # 局部字符串遮蔽，别的作用域用的是真 `new URL(...)` 内建——按名字撞车属误报。
    "URL", "TextDecoder", "TextEncoder", "Notification", "File", "FileReader",
}

_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "function", "return", "typeof",
    "new", "delete", "void", "in", "of", "do", "else", "try", "finally",
    "throw", "case", "break", "continue", "instanceof", "yield", "await",
    "async", "class", "extends", "super", "this", "let", "var", "const",
}

# 良性命中登记（原因必填；防过期由 test_accepted_not_stale 守）。
# 形如 ("模板相对路径", "函数名"): "原因"
_ACCEPTED: dict = {}

# 已确认的真 bug、待 owner 决策修复（保 CI 绿 + 债务可见；修好即删行）。
_PENDING: dict = {}

_FUNC_DECL_RE = re.compile(r"(?<![\w$.])function\s+([A-Za-z_$][\w$]*)\s*\(")
_DECL_STMT_RE = re.compile(r"(?<![\w$.])(?:var|let|const)\s+([^=;\n]+?)(?:=|;|\n)")
_PARAMS_RE = re.compile(
    r"(?:function\s*[\w$]*\s*\(([^()]*)\))|(?:catch\s*\(([^()]*)\))"
    r"|(?:\(([^()]*)\)\s*=>)|(?<![\w$.])([A-Za-z_$][\w$]*)\s*=>"
)
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_CALL_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
_IIFE_HEAD_RE = re.compile(r"^\(\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>)")


def _scope_defs(text: str) -> set:
    """作用域内以任何形式引入的名字（宽口径防误报：遮蔽形参/解构都算已定义）。"""
    names = set(_FUNC_DECL_RE.findall(text))
    for m in _DECL_STMT_RE.finditer(text):
        names |= set(_IDENT_RE.findall(m.group(1)))
    for m in _PARAMS_RE.finditer(text):
        for g in m.groups():
            if g:
                names |= set(_IDENT_RE.findall(g))
    return names - _KEYWORDS


def _calls(text: str) -> set:
    return {m.group(1) for m in _CALL_RE.finditer(text)} - _KEYWORDS


def _segment_scopes(masked: str):
    """把一个（已掩码的）script 体切成 (全局残余文本, [各顶层 IIFE 的内文])。

    只识别顶层 ``(function...`` / ``(async function...`` / ``((...)=>`` 形态；其余顶层
    括号/块语句一律留在全局残余（其内容按全局池处理＝减少误报的保守方向）。
    """
    scopes = []
    global_parts = []
    i, last, depth, n = 0, 0, 0, len(masked)
    while i < n:
        ch = masked[i]
        if depth == 0 and ch == "(" and _IIFE_HEAD_RE.match(masked[i : i + 200]):
            # 吃整个平衡括号段（IIFE 本体）
            d = 0
            j = i
            while j < n:
                c = masked[j]
                if c in "([{":
                    d += 1
                elif c in ")]}":
                    d -= 1
                    if d == 0:
                        break
                j += 1
            global_parts.append(masked[last:i])
            scopes.append(masked[i : j + 1])
            # 吃随后的调用括号 (…)（若有）
            k = j + 1
            while k < n and masked[k] in " \t\r\n":
                k += 1
            if k < n and masked[k] == "(":
                d2 = 0
                while k < n:
                    c = masked[k]
                    if c in "([{":
                        d2 += 1
                    elif c in ")]}":
                        d2 -= 1
                        if d2 == 0:
                            break
                    k += 1
                k += 1
            last = k
            i = k
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        i += 1
    global_parts.append(masked[last:])
    return "\n".join(global_parts), scopes


_AMBIENT_CACHE: dict = {}


def _ambient_cached() -> set:
    """ambient_globals 对所有模板恒同（base+partials），进程内算一次——
    否则 63 个模板 × 全量 partial 解析把整个门禁拖到分钟级。"""
    if "v" not in _AMBIENT_CACHE:
        try:
            _AMBIENT_CACHE["v"] = set(ambient_globals(TPL_DIR))
        except Exception:
            _AMBIENT_CACHE["v"] = set()
    return _AMBIENT_CACHE["v"]


def scan_template(html: str, tpl_path: Path) -> list:
    """返回 [(函数名, 调用作用域标签, 定义作用域标签)] 违规清单（IIFE 颗粒度）。"""
    html = _strip_html_comments(html)
    bodies = _script_bodies(html)
    if not bodies:
        return []
    # 全文件作用域收集：scopes = [(标签, 文本)]；全局池 = 各块全局残余之和
    scopes = []
    global_text_parts = []
    for bi, body in enumerate(bodies):
        masked = _mask(body)
        g, ss = _segment_scopes(masked)
        global_text_parts.append(g)
        for si, s in enumerate(ss):
            scopes.append((f"块{bi}·IIFE{si}", s))
    global_text = "\n".join(global_text_parts)
    global_defs = _scope_defs(global_text)
    exposed = window_exposed(html)
    try:
        statics = local_static_globals(html, STATIC_ROOT)
    except Exception:
        statics = set()
    ambient = _ambient_cached()
    reachable = global_defs | exposed | statics | ambient | _BUILTIN_ALLOW

    scope_defs = [(_tag, _scope_defs(txt)) for _tag, txt in scopes]

    out = []
    # ① 各 IIFE 作用域内的裸调用
    for idx, (tag, txt) in enumerate(scopes):
        own = scope_defs[idx][1]
        for name in _calls(txt):
            if name in own or name in reachable:
                continue
            owner = next((t for j, (t, d) in enumerate(scope_defs) if j != idx and name in d), None)
            if owner:
                out.append((name, tag, owner))
    # ② 全局代码里的裸调用（全局 → 某 IIFE 局部 同样必炸）
    for name in _calls(global_text):
        if name in reachable:
            continue
        owner = next((t for t, d in scope_defs if name in d), None)
        if owner:
            out.append((name, "全局代码", owner))
    return out


def _iter_templates():
    for p in sorted(TPL_DIR.rglob("*.html")):
        yield p


_SITE_SCAN_CACHE: dict = {}


def _site_hits() -> list:
    """全站扫描结果（进程内缓存）：两个测试共用，避免 63 模板扫两遍。"""
    if "v" not in _SITE_SCAN_CACHE:
        hits = []
        for p in _iter_templates():
            html = p.read_text(encoding="utf-8", errors="replace")
            rel = p.relative_to(TPL_DIR).as_posix()
            for name, caller, owner in scan_template(html, p):
                hits.append((rel, name, caller, owner))
        _SITE_SCAN_CACHE["v"] = hits
    return _SITE_SCAN_CACHE["v"]


def test_no_cross_scope_ghost_refs():
    """核心不变量：不允许「IIFE 局部定义、跨作用域裸调用、全站不可达」的幽灵引用。"""
    violations = []
    for rel, name, caller, owner in _site_hits():
        key = (rel, name)
        if key in _ACCEPTED or key in _PENDING:
            continue
        violations.append(
            f"{rel}: {caller} 裸调用 {name}()，但它只在 {owner} 内定义"
            f"（无 window 挂载/顶层声明）→ 运行时必 ReferenceError")
    assert not violations, (
        "跨作用域幽灵引用（_setPill 药丸全灭同款事故形态）：\n  " + "\n  ".join(violations)
        + "\n修法：在定义处挂 window（如 window.X=X），或把定义搬进调用作用域；"
          "良性命中请附原因登记 _ACCEPTED。"
    )


def test_detector_catches_cross_block_shape():
    """自证 ①：跨 <script> 块的事故原型必须报警。

    探针名刻意用不可能撞名的 ``_synthPillX9``——事故原名 ``_setPill`` 修复后已挂 window
    进入 ambient 全局集，用原名当探针会被真实修复「豁免」掉（首版实测踩过）。
    """
    synthetic = (
        "<script>(function(){function _synthPillX9(el,h){el.innerHTML=h;}})();</script>"
        "<div id=x></div>"
        "<script>(function(){function go(){_synthPillX9(document.getElementById('x'),'hi');}"
        "go();})();</script>"
    )
    hits = scan_template(synthetic, Path("synthetic.html"))
    assert any(n == "_synthPillX9" for n, _, _ in hits), "探测器抓不到跨块事故原型＝门禁失效"


def test_detector_catches_intra_block_iife_pair():
    """自证 ②（Phase2 收紧的存在理由）：同一 <script> 内两个 IIFE 之间的幽灵引用必须报警。"""
    synthetic = (
        "<script>\n"
        "(function(){function _synthHelperQ7(a){return a;}})();\n"
        "(function(){var v=_synthHelperQ7(1);console.log(v);})();\n"
        "</script>"
    )
    hits = scan_template(synthetic, Path("synthetic.html"))
    assert any(n == "_synthHelperQ7" for n, _, _ in hits), "块内跨 IIFE 漏报＝收紧未生效"


def test_detector_respects_window_export_and_params():
    """自证 ③：window 挂载必须放行；形参遮蔽（IIFE 注入依赖的常见模式）不得误报。"""
    exported = (
        "<script>(function(){function _synthPillX9(el,h){el.innerHTML=h;}"
        "window._synthPillX9=_synthPillX9;})();</script>"
        "<script>(function(){_synthPillX9(document.getElementById('x'),'hi');})();</script>"
    )
    assert not [n for n, _, _ in scan_template(exported, Path("s.html")) if n == "_synthPillX9"], \
        "window 暴露仍被误报＝规则过宽"
    param_shadow = (
        "<script>(function(){function _synthEscZ3(s){return s;}})();</script>"
        "<script>(function(_synthEscZ3){var t=_synthEscZ3('x');console.log(t);})(String);</script>"
    )
    assert not [n for n, _, _ in scan_template(param_shadow, Path("s.html")) if n == "_synthEscZ3"], \
        "形参遮蔽被误报＝宽口径 defs 失效"


def test_accepted_not_stale():
    """_ACCEPTED/_PENDING 防过期：登记项必须仍然「命中扫描」，修好了就删行。"""
    live = {(rel, name) for rel, name, _, _ in _site_hits()}
    stale = [k for k in list(_ACCEPTED) + list(_PENDING) if k not in live]
    assert not stale, f"登记项已不再命中，请清理台账：{stale}"
