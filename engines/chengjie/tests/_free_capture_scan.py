"""「IIFE 外全局函数引用闭包内标识符」自由变量捕获扫描核心（供 test_template_free_capture 复用）。

事故原型（contact360，2026-08-01）：`loadEngagement`/`computeEngagement` 定义在 IIFE **外**
（全局作用域），函数体却引用 IIFE **内**的 `CONTACT_ID`/`esc` → 启动序列一调用即
`ReferenceError`，把后续 `loadStlSide()`/`load()` 全掐死，整页永远卡「加载中」。
既有门禁全部抓不到：哑按钮门禁只验「内联 handler 引用的函数名全局可达」——函数本身
确实可达（在全局），崩的是**函数体内部的自由变量**；孤儿引用门禁只看 DOM id。

判据（窄不变量，宁漏勿误伤）——顶层（<script> 括号深度 0）声明的函数，其函数体**读取**
某标识符 X，且同时满足：
  (a) X 非该函数局部（形参 / 内部 var|let|const|function / 内嵌回调形参 / catch 形参 /
      纯赋值目标——非严格模式下裸赋值不抛，只有「读」才 ReferenceError）；
  (b) X 非页面全局可达（任一 <script> 顶层声明 ∪ window.X 写 **或读**（读到 window.X
      即作者断言全局存在，如 `const Chart=window.Chart` 别名场景）∪ base/partial ambient
      ∪ 浏览器内建 ∪ 共享脚本全局）；
  (c) X 在同页某**闭包**（IIFE / 任意函数体）内有 var|let|const|function 声明——
      「作者明明写了它，只是作用域够不着」。这是高置信关键：X 全站不见踪影多半来自
      外部 <script src>，刻意不 flag（防误报）；「声明在闭包里 + 顶层函数在读」
      = contact360 同款，运行时必 ReferenceError。

已知盲区（如实登记；运行时兜底由 _boot_error_guard.html 的全局错误守卫补）：
  - 模板字面量 `${...}` 插值里的引用（掩码器整串掩掉）→ 漏报；
  - 顶层语句 / 顶层回调（DOMContentLoaded 等匿名函数）体内的引用——v1 只分析
    「顶层具名函数声明 + 顶层 const|let|var 挂的函数表达式/箭头函数」；
  - `x += 1` 复合赋值的读取侧；`typeof x` 刻意放行（不抛，防御式惯用法）。
"""
import re

from tests import _inline_handler_scan as scan

# JS 保留字/字面量/常见非引用词（出现在标识符位也绝不按「自由变量读取」计）
_JS_RESERVED = {
    "if", "for", "while", "switch", "return", "catch", "function", "typeof",
    "new", "void", "do", "else", "delete", "in", "of", "await", "yield",
    "throw", "case", "instanceof", "var", "let", "const", "try", "finally",
    "break", "continue", "default", "debugger", "with", "class", "extends",
    "super", "this", "true", "false", "null", "undefined", "async", "static",
    "get", "set", "arguments", "NaN", "Infinity", "globalThis",
}

# BUILTINS（共享核心）之外模板里常用的浏览器/JS 全局，补齐防误报
_EXTRA_BUILTINS = {
    "Blob", "FormData", "URL", "WebSocket", "EventSource", "Audio", "Image",
    "FileReader", "AbortController", "IntersectionObserver", "MutationObserver",
    "ResizeObserver", "XMLHttpRequest", "history", "screen", "performance",
    "crypto", "atob", "btoa", "structuredClone", "getComputedStyle", "matchMedia",
    "scrollTo", "scrollBy", "innerWidth", "innerHeight", "devicePixelRatio",
    "CustomEvent", "Notification", "DOMParser", "TextDecoder", "TextEncoder",
    "Intl", "Symbol", "Proxy", "Reflect", "WeakMap", "WeakSet", "BigInt",
    "queueMicrotask", "cancelAnimationFrame", "requestIdleCallback", "Error",
    "TypeError", "RangeError", "SyntaxError", "ReferenceError", "AggregateError",
    "Element", "HTMLElement", "Node", "NodeList", "File", "ClipboardItem",
    "ResizeObserverEntry", "MediaRecorder", "AudioContext", "webkitAudioContext",
    "speechSynthesis", "SpeechSynthesisUtterance", "getSelection", "open", "close",
    "top", "parent", "self", "frames", "origin", "name", "status", "print",
    "addEventListener", "removeEventListener", "dispatchEvent", "postMessage",
}

_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_REGEX_FLAG_CHARS = set("dgimsuvy")


def _zap_regex_flags(js: str, masked: str) -> str:
    """共享掩码器掩掉正则字面量本体但**不掩尾部 flags**（`/x/g` 的 `g` 漏成裸标识符，
    全站扫描实测 30+ 处 `['g']` 假阳性全来自此）。判据：原文该处是 `/` 且已被掩码
    （＝正则/注释的收尾斜杠，除法斜杠不会被掩）→ 紧随的 dgimsuvy 连串是 flags，抹掉。"""
    out = list(masked)
    n = len(js)
    for k in range(1, n):
        if js[k - 1] == "/" and out[k - 1] == " " and js[k] in _REGEX_FLAG_CHARS \
                and out[k] == js[k]:
            j = k
            while j < n and js[j] in _REGEX_FLAG_CHARS and out[j] == js[j]:
                out[j] = " "
                j += 1
    return "".join(out)


def _masked(body: str) -> str:
    return _zap_regex_flags(body, scan._mask(body))


def _strip_jinja(js: str) -> str:
    """把 Jinja 表达式/标签/注释换成等长空白（保 offset），防 `{{ persona|tojson }}`
    里的 `persona` 被当 JS 标识符、防标签内引号干扰字符串掩码。"""
    return _JINJA.sub(lambda m: " " * len(m.group(0)), js)


def _depths(masked: str):
    """前缀括号深度数组（与共享核心 _global_names_in_block 同算法）。"""
    depth = [0] * (len(masked) + 1)
    d = 0
    for idx, ch in enumerate(masked):
        depth[idx] = d
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d = d - 1 if d > 0 else 0
    depth[len(masked)] = d
    return depth


def _match_close(masked: str, open_idx: int):
    """从 open_idx（某开括号）走到配对闭括号，返回其下标；不配对返回 None。"""
    bal = 0
    for i in range(open_idx, len(masked)):
        ch = masked[i]
        if ch in "([{":
            bal += 1
        elif ch in ")]}":
            bal -= 1
            if bal == 0:
                return i
    return None


_FN_DECL = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
_VAR_FN = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:function\b[^(]*|)\(")


def top_level_functions(masked: str, depth) -> list:
    """顶层「以函数为值」的定义：(名字, 形参串, body起, body止)。

    覆盖：`function NAME(){}` 声明、`const|let|var NAME = [async] function(){}`、
    `const|let|var NAME = [async] (params) => {…}`。箭头函数表达式体（无 {}）跳过。
    """
    out = []
    seen_spans = set()

    def _push(name, p0):
        p1 = _match_close(masked, p0)
        if p1 is None:
            return
        i = p1 + 1
        while i < len(masked) and masked[i].isspace():
            i += 1
        # 箭头：跳过 => 再找 {
        if masked[i:i + 2] == "=>":
            i += 2
            while i < len(masked) and masked[i].isspace():
                i += 1
        if i >= len(masked) or masked[i] != "{":
            return  # 表达式体箭头 / 非块体：跳过
        b1 = _match_close(masked, i)
        if b1 is None or (i, b1) in seen_spans:
            return
        seen_spans.add((i, b1))
        out.append((name, masked[p0 + 1:p1], i + 1, b1))

    for m in _FN_DECL.finditer(masked):
        s = m.start()
        if depth[s] != 0:
            continue
        # 排除表达式位（具名函数表达式 / IIFE）：`= function f`、`(function f`
        k = s - 1
        while k >= 0 and masked[k].isspace():
            k -= 1
        if k >= 0 and masked[k] in "=(,:?&|!":
            continue
        _push(m.group(1), m.end() - 1)

    for m in _VAR_FN.finditer(masked):
        if depth[m.start()] != 0:
            continue
        _push(m.group(1), m.end() - 1)
    return out


_DECL_SIMPLE = re.compile(r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)")
_DECL_DESTRUCT = re.compile(
    r"\b(?:var|let|const)\s*([\[{][^;=]*?[\]}])\s*(?:=|\bof\b|\bin\b)")
_INNER_FN = re.compile(r"\bfunction\s*([A-Za-z_$][\w$]*)?\s*\(([^)]*)\)")
_ARROW_PARAMS_PAREN = re.compile(r"\(([^()]*)\)\s*=>")
_ARROW_PARAM_BARE = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*=>")
_CATCH = re.compile(r"\bcatch\s*\(([^)]*)\)")
_ASSIGN_TARGET = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*=(?![=>])")


def _locals_of(body_masked: str, params_text: str) -> set:
    """函数体的局部名集合（形参 + 各类内部声明 + 内嵌回调形参 + 纯赋值目标）。
    刻意从宽——多归局部只会漏报，绝不误报。"""
    loc = set(_IDENT.findall(params_text or ""))
    for m in _DECL_SIMPLE.finditer(body_masked):
        loc.add(m.group(1))
    for m in _DECL_DESTRUCT.finditer(body_masked):
        loc |= set(_IDENT.findall(m.group(1)))
    for m in _INNER_FN.finditer(body_masked):
        if m.group(1):
            loc.add(m.group(1))
        loc |= set(_IDENT.findall(m.group(2)))
    for m in _ARROW_PARAMS_PAREN.finditer(body_masked):
        loc |= set(_IDENT.findall(m.group(1)))
    for m in _ARROW_PARAM_BARE.finditer(body_masked):
        loc.add(m.group(1))
    for m in _CATCH.finditer(body_masked):
        loc |= set(_IDENT.findall(m.group(1)))
    for m in _ASSIGN_TARGET.finditer(body_masked):
        loc.add(m.group(1))
    return loc - _JS_RESERVED


def _refs_of(body_masked: str) -> set:
    """函数体内「运行时会解析的标识符读取」。排除：保留字、属性访问（.x）、
    对象键/标签（x:，三元中段随之漏报=可接受）、纯赋值目标（x=，非严格模式不抛）、
    `typeof x` 出现过的名字**函数级豁免**（typeof 不抛且通常护住后续读取——防御式
    惯用法刻意放过）、对象方法简写（`foo(...) {`——定义非引用）。"""
    refs = set()
    n = len(body_masked)
    # 函数级 typeof 豁免：体内任何 `typeof X` ⇒ X 视为防御式引用整体放行
    typeof_guarded = set(re.findall(r"\btypeof\s+([A-Za-z_$][\w$]*)", body_masked))
    for m in _IDENT.finditer(body_masked):
        name = m.group(0)
        if name in _JS_RESERVED or name in typeof_guarded:
            continue
        s, e = m.start(), m.end()
        if s > 0 and body_masked[s - 1] == ".":
            continue  # 属性访问
        j = e
        while j < n and body_masked[j] in " \t":
            j += 1
        if j < n and body_masked[j] == ":":
            continue  # 对象键 / 标签
        if j < n and body_masked[j] == "=" and body_masked[j + 1:j + 2] not in ("=", ">"):
            continue  # 纯赋值目标
        if j < n and body_masked[j] == "(":
            # 方法简写消歧：`名(参){` 是定义不是调用（if/while 等关键字已被排除）
            close = _match_close(body_masked, j)
            if close is not None:
                t = close + 1
                while t < n and body_masked[t].isspace():
                    t += 1
                if t < n and body_masked[t] == "{":
                    continue
        refs.add(name)
    return refs


def _closure_declared(masked: str, depth) -> set:
    """深度 >0 处的 var|let|const|function 声明名＝「存在但被闭包罩住」的候选。"""
    names = set()
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)", masked):
        if depth[m.start()] > 0:
            names.add(m.group(1))
    for m in _DECL_SIMPLE.finditer(masked):
        if depth[m.start()] > 0:
            names.add(m.group(1))
    return names


def _window_touched(html: str) -> set:
    """window.X 的**读或写**都算「作者断言全局 X 存在」（写=暴露；读=外部脚本别名，
    如 `const Chart = window.Chart`——bare X 引用照样解析得到）。"""
    return set(re.findall(r"window\.([A-Za-z_$][\w$]*)", html))


# Node/CommonJS 模块环境全局（desktop/main.js、preload 等）：模块作用域内这些名字
# 天然可达；浏览器纯前端文件本不该用它们，但统一并进 ambient 只多放不误报
#（本门禁的 flag 前提是「同文件闭包里有声明」，与这些名字撞名的场景本就存疑）。
NODE_ENV_GLOBALS = {
    "require", "module", "exports", "process", "global",
    "__dirname", "__filename", "Buffer", "setImmediate", "clearImmediate",
}
# Service Worker 环境全局（src/web/static/pwa/sw.js）。
SW_ENV_GLOBALS = {
    "self", "caches", "clients", "registration", "importScripts", "skipWaiting",
}


def _analyze_bodies(bodies, window_touched, ambient, extra_allow) -> list:
    """script 体列表 → 命中列表（模板/裸 JS 两个入口共用的核心）。"""
    masked_list = [_masked(b) for b in bodies]
    depth_list = [_depths(mb) for mb in masked_list]

    page_globals, closure_names = set(), set()
    for body, mb, dp in zip(bodies, masked_list, depth_list):
        page_globals |= scan._global_names_in_block(body)
        closure_names |= _closure_declared(mb, dp)

    reachable = (page_globals | set(window_touched) | set(ambient)
                 | scan.BUILTINS | scan.SHARED_GLOBALS | _EXTRA_BUILTINS
                 | set(extra_allow))

    findings = []
    for mb, dp in zip(masked_list, depth_list):
        for name, params, b0, b1 in top_level_functions(mb, dp):
            body = mb[b0:b1]
            missing = sorted(
                (_refs_of(body) - _locals_of(body, params) - reachable - {name})
                & closure_names)
            if missing:
                findings.append((name, missing))
    return findings


def free_captures(html: str, ambient=frozenset(), extra_allow=frozenset()) -> list:
    """模板入口：返回 [(函数名, [够不着的标识符, ...]), ...]，无发现＝[]。"""
    bodies = [_strip_jinja(b) for b in scan._script_bodies(html)]
    return _analyze_bodies(bodies, _window_touched(html), ambient, extra_allow)


def free_captures_js(js_text: str, ambient=frozenset(), extra_allow=frozenset()) -> list:
    """裸 JS 文件入口（src/web/static / desktop / shared 树）：整文件视作单个
    script 体分析。「顶层」＝文件顶层——浏览器经典脚本里是真全局作用域；
    Node/CJS 与 ESM 里是模块作用域，但「顶层函数读闭包内声明」的必崩语义完全
    相同（模块作用域对文件内分析等价于全局）。无 Jinja 剥离（.js 不含模板语法）。"""
    return _analyze_bodies([js_text], _window_touched(js_text), ambient, extra_allow)
