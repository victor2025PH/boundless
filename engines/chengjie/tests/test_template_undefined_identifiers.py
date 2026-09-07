# -*- coding: utf-8 -*-
"""模板 JS「无主标识符」门禁（2026-08-22 B39 workflows._TYPE_LABELS 整页崩事故的机制化沉淀）。

事故：workflows.html 的「自定义动作」面板 2026-08-14 整体下线时，把 ``_TYPE_LABELS``
（步骤类型→人话标签映射）连带删除，但工作链只读视图/编辑下拉/监控分步统计**三处渲染**
仍在引用——页面初始化 ReferenceError，列表永卡「加载中」，存活八天到内测实测才暴露。

既有门禁为什么都没抓到（本门禁的存在理由）：
    - 哑按钮门禁：只看内联 ``on*=`` handler 的**函数调用**；``_TYPE_LABELS`` 是脚本体内的
      **属性/下标读取**（``_TYPE_LABELS[k]`` / ``Object.keys(_TYPE_LABELS)``），不在其视野。
    - 孤儿 DOM 引用门禁：只看 ``getElementById('x').prop`` 的 DOM id，不看 JS 标识符。
    - 跨块幽灵引用门禁（test_template_cross_block_refs）：抓「定义在**另一个块的 IIFE 里**
      够不着」；``_TYPE_LABELS`` 是**全文件根本没有定义**——形态互补，两个门禁合起来
      才覆盖「删定义留引用」全谱。

不变量（窄而高置信，宁可漏报不误报）：
    模板某 <script> 块内、以**读取形态**出现的裸标识符 NAME
    （``NAME[…]`` 下标 / ``NAME.prop`` 属性基 / ``NAME(…)`` 调用），若同时满足：
      1. 全文件任何 <script> 块、任何深度都没有它的声明痕迹
         （var/let/const 语句段、function 名、任意函数形参、catch 形参、
          裸赋值 ``NAME =``——sloppy mode 隐式全局也算「有定义可达」）；
      2. 没有 ``window.NAME=`` / ``Object.assign(window,{NAME})`` 暴露；
      3. base/partials 的环境全局、模板引用的本地静态 JS 全局里也没有；
      4. 不是 JS/DOM 宿主内建、不是关键字；
    则该行一执行**必然** ReferenceError。

    声明收集刻意**过度包含**（var 语句连 RHS 标识符一起收、解构整段收）：代价是漏报
    个别真 bug，换来零误报——与孤儿引用门禁「刻意放过防御式引用」同一哲学。
    Jinja 段（``{{ }}``/``{% %}``/``{# #}``）在扫描前整段抹空，模板语法不进 JS 判定。

维护：
    - 良性命中登记 ``_ACCEPTED``（附原因），真 bug 待决策登记 ``_PENDING``；
      两表有防过期测试（修好/失效会点名清理），对齐 ``_ACCEPTED_DUP_IDS`` 文化。
    - 探测器自证：注入 B39 事故形态的合成样例必须报告违规——门禁不是摆设。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._inline_handler_scan import (
    _script_bodies,
    _strip_html_comments,
    ambient_globals,
    local_static_globals,
    window_exposed,
)

TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
STATIC_ROOT = Path(__file__).resolve().parents[1] / "src" / "web" / "static"

_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "function", "return", "typeof",
    "new", "delete", "void", "in", "of", "do", "else", "try", "finally",
    "throw", "case", "break", "continue", "instanceof", "yield", "await",
    "async", "class", "extends", "super", "this", "let", "var", "const",
    "true", "false", "null", "undefined", "debugger", "with", "static",
    "get", "set", "import", "export", "from", "as", "default",
}

# JS/DOM 宿主内建：读取形态（X.prop / X[…] / X(…)）里合法出现的全局。
# 按「模板里实际用到」维护，宁可白名单大——每一项都是真实宿主全局，不会掩护真 bug。
_HOST_GLOBALS = {
    # 语言内建
    "String", "Number", "Boolean", "Array", "Object", "JSON", "Date", "Math",
    "RegExp", "Promise", "Error", "TypeError", "RangeError", "Map", "Set",
    "WeakMap", "WeakSet", "Symbol", "Proxy", "Reflect", "Intl", "BigInt",
    "parseInt", "parseFloat", "isNaN", "isFinite", "NaN", "Infinity",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "structuredClone", "globalThis", "arguments", "eval", "escape", "unescape",
    # DOM / BOM
    "window", "document", "location", "console", "navigator", "history",
    "screen", "performance", "localStorage", "sessionStorage", "event",
    "self", "top", "parent", "frames", "crypto", "speechSynthesis",
    "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame",
    "requestIdleCallback", "matchMedia", "scrollTo", "scrollBy", "open",
    "close", "focus", "blur", "print", "postMessage", "dispatchEvent",
    "addEventListener", "removeEventListener", "getSelection",
    "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "alert", "confirm", "prompt", "atob", "btoa",
    # 构造器 / API 对象
    "CustomEvent", "EventSource", "URLSearchParams", "URL", "Blob", "File",
    "FormData", "AbortController", "Audio", "Image", "WebSocket", "Worker",
    "MutationObserver", "IntersectionObserver", "ResizeObserver",
    "XMLHttpRequest", "FileReader", "DOMParser", "Notification", "Option",
    "AudioContext", "webkitAudioContext", "MediaRecorder", "ClipboardItem",
    "BroadcastChannel", "TextDecoder", "TextEncoder", "IDBKeyRange",
    "indexedDB", "caches", "ResizeObserverEntry", "DataTransfer",
    "OffscreenCanvas", "ImageData", "Path2D", "KeyboardEvent", "MouseEvent",
    "HTMLElement", "Element", "Node", "NodeList", "CSS", "queueMicrotask",
    "Event", "InputEvent", "PointerEvent", "TouchEvent", "AudioWorkletNode",
    "OfflineAudioContext", "AnalyserNode", "GainNode",
}

# 良性命中登记（原因必填；防过期由 test_accepted_not_stale 守）。
# 形如 ("模板相对路径", "标识符"): "原因"
_ACCEPTED: dict = {}

# 已确认的真 bug、待产品/owner 决策修复（保 CI 绿 + 债务可见；修好即从表中删）。
_PENDING: dict = {}

# Jinja 模板段（先于 JS mask 抹空；{% %} / {{ }} / {# #} 内的标识符是 Jinja 不是 JS）
_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)


def _mask_js(js: str) -> str:
    """字符串/模板字面量/注释/正则 → 等长空白。

    是 ``_inline_handler_scan._mask`` 的本地 fork，修两个对本门禁致命的缺口
    （刻意不改共享核心——其他门禁按各自误差模型在用它，跨消费方语义变更风险大）：
      1. 正则字面量的 **flags 也一并抹掉**（``/x/i.test(l)`` 上游只抹到闭 ``/``，
         残留的 ``i`` 会被误读成「读取标识符 i」）；
      2. 模板字面量 ``${}`` 插值内的 **花括号配对**正确追踪（上游对插值里的
         ``=>{}``/对象字面量的 ``}`` 直接减深度 → 模板提前"闭合"，其后整块
         mask 相位错乱：真代码被当字符串抹掉、真字符串当代码漏出）。
    """
    out = []
    i, n = 0, len(js)
    prev = ""
    while i < n:
        c = js[i]
        two = js[i:i + 2]
        if two == "//":
            j = js.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j; continue
        if two == "/*":
            j = js.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i)); i = j; continue
        if c in "\"'":
            j = i + 1
            while j < n:
                if js[j] == "\\":
                    j += 2; continue
                if js[j] == c:
                    j += 1; break
                j += 1
            out.append(" " * (j - i)); i = j; prev = "x"; continue
        if c == "`":
            j = i + 1
            interp = []  # 每层插值的花括号净深（修 ${…=>{}…} 提前闭合）
            while j < n:
                if js[j] == "\\":
                    j += 2; continue
                if not interp and js[j] == "`":
                    j += 1; break
                if js[j] == "$" and j + 1 < n and js[j + 1] == "{":
                    interp.append(0); j += 2; continue
                if interp:
                    if js[j] == "{":
                        interp[-1] += 1
                    elif js[j] == "}":
                        if interp[-1] == 0:
                            interp.pop()
                        else:
                            interp[-1] -= 1
                j += 1
            out.append(" " * (j - i)); i = j; prev = "x"; continue
        if c == "/" and prev in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?",
                                  "{", "}", ";", "+", "-", "*", "%", "<", ">", "~", "^"):
            j = i + 1
            inclass = False; ok = False
            while j < n:
                ch = js[j]
                if ch == "\\":
                    j += 2; continue
                if ch == "[":
                    inclass = True
                elif ch == "]":
                    inclass = False
                elif ch == "/" and not inclass:
                    j += 1; ok = True; break
                elif ch == "\n":
                    break
                j += 1
            if ok:
                while j < n and (js[j].isalpha()):  # 正则 flags 一并抹
                    j += 1
                out.append(" " * (j - i)); i = j; prev = "x"; continue
        out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)

# 读取形态：下标 / 属性基 / 调用（lookbehind 排除 a.b 链身与 $、字面量毗邻）
_READ_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?:\[|\.(?![\d])|\()")

# 声明痕迹（全部过度包含）：
_DECL_VARSTMT_RE = re.compile(r"\b(?:var|let|const)\b([^;]*)")     # 语句段整段收
_DECL_FUNC_RE = re.compile(r"\bfunction\s*([A-Za-z_$][\w$]*)?\s*\(([^)]*)\)")
_DECL_ARROW_RE = re.compile(r"\(([^()]*)\)\s*=>|(?<![\w$.])([A-Za-z_$][\w$]*)\s*=>")
_DECL_CATCH_RE = re.compile(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)")
_DECL_ASSIGN_RE = re.compile(r"(?<![\w$.<>=!+\-*/%&|^])([A-Za-z_$][\w$]*)\s*=(?![=>])")
_DECL_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
# typeof 防御式引用：`typeof NAME==='function' && NAME()` 对未声明名安全（typeof 不抛），
# 与孤儿引用门禁「刻意放过防御式引用」同哲学——出现过 typeof 检查的名字整文件放行。
_TYPEOF_GUARD_RE = re.compile(r"\btypeof\s+([A-Za-z_$][\w$]*)")
# window.NAME 访问痕迹：`if(window.X) X.y()` 防御模式 / 外部（桌面壳、异步脚本）注入的
# 全局——作者显式经 window 取过的名字视同"知道它可选/外来"，整文件放行。
_WINDOW_ACCESS_RE = re.compile(r"\bwindow\s*\.\s*([A-Za-z_$][\w$]*)")
# for(x of y) / for(x in y)：无 = 的裸循环变量（sloppy 隐式全局），声明痕迹之一
_DECL_FORLOOP_RE = re.compile(r"\bfor\s*\(\s*([A-Za-z_$][\w$]*)\s+(?:of|in)\b")


def _strip_jinja(text: str) -> str:
    return _JINJA_RE.sub(lambda m: " " * len(m.group(0)), text)


def _declared_names(masked: str) -> set:
    """一个 <script> 块（masked 后）里的全部声明痕迹（任意深度，过度包含）。"""
    names: set = set()
    for m in _DECL_VARSTMT_RE.finditer(masked):
        names |= set(_IDENT_RE.findall(m.group(1)))
    for m in _DECL_FUNC_RE.finditer(masked):
        if m.group(1):
            names.add(m.group(1))
        names |= set(_IDENT_RE.findall(m.group(2)))
    for m in _DECL_ARROW_RE.finditer(masked):
        seg = m.group(1) or m.group(2) or ""
        names |= set(_IDENT_RE.findall(seg))
    names |= set(m.group(1) for m in _DECL_CATCH_RE.finditer(masked))
    names |= set(m.group(1) for m in _DECL_ASSIGN_RE.finditer(masked))
    names |= set(m.group(1) for m in _DECL_CLASS_RE.finditer(masked))
    names |= set(m.group(1) for m in _DECL_FORLOOP_RE.finditer(masked))
    names |= set(m.group(1) for m in _TYPEOF_GUARD_RE.finditer(masked))
    names |= set(m.group(1) for m in _WINDOW_ACCESS_RE.finditer(masked))
    return names - _KEYWORDS


def _read_names(masked: str) -> set:
    return set(m.group(1) for m in _READ_RE.finditer(masked)) - _KEYWORDS


def scan_html(html: str, ambient: set = frozenset(), static_globals: set = frozenset()) -> set:
    """返回「读取了但全文件无任何定义痕迹」的标识符集合。"""
    html = _strip_html_comments(html)
    bodies = [_mask_js(_strip_jinja(b)) for b in _script_bodies(html)]
    if not bodies:
        return set()
    declared: set = set()
    reads: set = set()
    for masked in bodies:
        declared |= _declared_names(masked)
        reads |= _read_names(masked)
    exposed = window_exposed(html)
    unknown = (reads - declared - exposed - set(ambient) - set(static_globals)
               - _HOST_GLOBALS - _KEYWORDS)
    return unknown


def _all_templates():
    return sorted(TPL_DIR.rglob("*.html"))


@pytest.fixture(scope="module")
def _ambient():
    """base/partials 全局 + 布局模板挂载的静态 JS 全局（子模板经 extends 继承）。

    子页自身没有 `<script src>` 标签、脚本却能用布局加载的库（如 workspace_base
    的 crm-widgets.js → window.CRMW）——per-file 扫描看不见，须并入环境。
    """
    amb = set(ambient_globals(TPL_DIR))
    for layout in ("base.html", "workspace_base.html"):
        p = TPL_DIR / layout
        if p.exists():
            html = p.read_text(encoding="utf-8", errors="replace")
            amb |= local_static_globals(html, STATIC_ROOT)
    return amb


def test_no_undefined_identifiers(_ambient):
    """全站模板：读取形态的裸标识符必须在文件内/环境全局里有定义痕迹。"""
    violations = {}
    for tpl in _all_templates():
        rel = tpl.relative_to(TPL_DIR).as_posix()
        html = tpl.read_text(encoding="utf-8", errors="replace")
        unknown = scan_html(
            html,
            ambient=_ambient,
            static_globals=local_static_globals(html, STATIC_ROOT),
        )
        hits = {
            n for n in unknown
            if (rel, n) not in _ACCEPTED and (rel, n) not in _PENDING
        }
        if hits:
            violations[rel] = sorted(hits)
    assert not violations, (
        "模板 JS 读取了全文件无定义的标识符（必然 ReferenceError，B39 形态）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(violations.items()))
        + "\n→ 补回定义 / 挂 window / 若为良性命中请登记 _ACCEPTED（附原因）"
    )


def test_workflows_type_labels_defined():
    """B39 回归钉：workflows.html 的 _TYPE_LABELS 三处渲染引用必须有定义。"""
    html = (TPL_DIR / "workflows.html").read_text(encoding="utf-8", errors="replace")
    stripped = _strip_html_comments(html)
    reads = 0
    has_def = False
    for body in _script_bodies(stripped):
        masked = _mask_js(_strip_jinja(body))
        reads += len(re.findall(r"(?<![\w$.])_TYPE_LABELS\s*[\[\.(]", masked))
        if "_TYPE_LABELS" in _declared_names(masked):
            has_def = True
    assert reads >= 3, "工作链页三处 _TYPE_LABELS 渲染引用不见了？（结构变了请同步本钉）"
    assert has_def, "_TYPE_LABELS 定义又被删了（B39 复发）——三处渲染共用，勿随面板下线"


def test_detector_catches_b39_shape():
    """探测器自证：删定义留引用的合成样例必须被抓到；补回定义即放行。"""
    broken = """
    <script>
    (function(){
      function render(s){ return _GHOST_LABELS[s.t] + _GHOST_LABELS.x + _ghostFn(s); }
      render({t:'a'});
    })();
    </script>
    """
    hits = scan_html(broken)
    assert "_GHOST_LABELS" in hits, "探测器没抓到 B39 形态（下标/属性读取）——门禁失效"
    assert "_ghostFn" in hits, "探测器没抓到无定义调用——门禁失效"

    fixed = """
    <script>
    (function(){
      var _GHOST_LABELS={a:1}; var _ghostFn=function(){return 1;};
      function render(s){ return _GHOST_LABELS[s.t] + _GHOST_LABELS.x + _ghostFn(s); }
      render({t:'a'});
    })();
    </script>
    """
    assert not scan_html(fixed), "补回定义后不应再报——探测器误报"


def test_detector_respects_scope_shapes():
    """零误报底线：形参 / catch / 解构 / 裸赋值 / window 暴露 / Jinja 段都不得误报。"""
    benign = """
    <script>
    (function(){
      var {aa, bb} = JSON.parse('{}');
      window.exposedFn = function(){ return aa.x + bb[0]; };
      fetch('/x').then(function(resp){ return resp.json(); })
        .catch(function(err){ console.warn(err.message); });
      var arrow = (evt) => evt.target;
      var single = evt2 => evt2.detail;
      implicitG = {k: 1};
      implicitG.k += 1;
      exposedFn();
      if(typeof maybeInjected==='function') maybeInjected();
      if(/error|fail/i.test('x')) console.log(1);
      for(k2 of [1,2]) console.log(k2.toFixed(0));
      var tpl = `a ${ [1].map(z=>{ return z; }).join('') } b`;
      afterTemplate.run();
      var afterTemplate = {run: function(){}};
      import('/static/x.js');
      {{ jinja_expr.attr }}
    })();
    </script>
    """
    assert scan_html(benign) == set(), "良性形态被误报——声明收集有缺口"


def test_accepted_not_stale(_ambient):
    """_ACCEPTED/_PENDING 防过期：登记的命中若已不再出现，点名清理。"""
    stale = []
    for (rel, name) in list(_ACCEPTED.keys()) + list(_PENDING.keys()):
        tpl = TPL_DIR / rel
        if not tpl.exists():
            stale.append((rel, name, "模板已不存在"))
            continue
        html = tpl.read_text(encoding="utf-8", errors="replace")
        unknown = scan_html(
            html,
            ambient=_ambient,
            static_globals=local_static_globals(html, STATIC_ROOT),
        )
        if name not in unknown:
            stale.append((rel, name, "已不再命中（修好了？）"))
    assert not stale, f"登记表过期条目，请清理：{stale}"


# ═══════════════════════════════════════════════════════════════════════════════
# 第二形态：「有定义但够不着」（#237 人设备份恢复 `_esc is not defined`，2026-09-07）
#
# 事故：personas.html 的 `_esc(v)` 是 `_exportFilteredCSV()` **体内**的 CSV 转义函数；L-2（09-06）
# 新写的备份恢复预览 / JSON 导入预览在**全局作用域**直接调 `_esc(...)`——首次真用（skuio 卸载重装后
# 人设池 0、恢复唯一备份）即 ReferenceError，被 try/catch 吞成「请求失败 _esc is not defined」。
# 上面的门禁抓不到：它只判「全文件零声明痕迹」，而 `_esc` 全文件是有声明的（任意深度都算）。
#
# 不变量（窄而高置信，宁可漏报不误报）：
#     标识符 NAME 在整个模板里的**唯一**声明痕迹是若干条**嵌套**（花括号深度 > 0）的
#     `function NAME(` 声明——没有 depth-0 的 function / var / let / const 声明，没有任何裸赋值
#     `NAME =`、形参、catch 形参、class、for-of 变量、typeof 守卫、window.NAME 访问 / 暴露、
#     环境全局、宿主内建——则 NAME 只在那些声明所在的**顶层块**（depth 0→1 的花括号区间）内可达；
#     出现在这些顶层块之外的读取（`NAME(` / `NAME.` / `NAME[`）**必然** ReferenceError。
#     刻意用「顶层块」而非「最近块」当可达范围：sloppy mode 下块内 function 声明会提升到所在函数
#     （Annex B），用最近块会误报；顶层块外一定够不着，零误报。
# ═══════════════════════════════════════════════════════════════════════════════

# 良性命中 / 待决策真 bug 登记（形如 ("模板相对路径", "标识符"): "原因"）
_ACCEPTED_SCOPE: dict = {}
_PENDING_SCOPE: dict = {}


def _top_level_ranges(masked: str) -> list:
    """masked 文本里 depth 0→1 的花括号区间 [(start, end)]（end 为闭括号下标；未闭合取文末）。"""
    ranges = []
    depth = 0
    start = -1
    for i, c in enumerate(masked):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    ranges.append((start, i))
                    start = -1
    if start >= 0:
        ranges.append((start, len(masked)))
    return ranges


def _brace_depths(masked: str) -> list:
    """depths[i] = 下标 i 处（该字符之前）的花括号深度；一次线性扫描，供多次查表。"""
    depths = [0] * (len(masked) + 1)
    depth = 0
    for i, c in enumerate(masked):
        depths[i] = depth
        if c == "{":
            depth += 1
        elif c == "}" and depth > 0:
            depth -= 1
    depths[len(masked)] = depth
    return depths


def _globalish_names(masked: str, depths: list) -> set:
    """一个块内、能让 NAME 从任何地方可达（或让我们不敢下结论）的声明痕迹——过度包含。"""
    names: set = set()
    for m in _DECL_FUNC_RE.finditer(masked):
        if m.group(1) and depths[m.start()] == 0:
            names.add(m.group(1))
        names |= set(_IDENT_RE.findall(m.group(2)))          # 形参：任何深度都放行
    for m in _DECL_VARSTMT_RE.finditer(masked):
        if depths[m.start()] == 0:
            names |= set(_IDENT_RE.findall(m.group(1)))
    for m in _DECL_ARROW_RE.finditer(masked):
        names |= set(_IDENT_RE.findall(m.group(1) or m.group(2) or ""))
    names |= set(m.group(1) for m in _DECL_CATCH_RE.finditer(masked))
    names |= set(m.group(1) for m in _DECL_ASSIGN_RE.finditer(masked))   # 裸赋值 = 隐式全局 / 别处 var
    names |= set(m.group(1) for m in _DECL_CLASS_RE.finditer(masked))
    names |= set(m.group(1) for m in _DECL_FORLOOP_RE.finditer(masked))
    names |= set(m.group(1) for m in _TYPEOF_GUARD_RE.finditer(masked))
    names |= set(m.group(1) for m in _WINDOW_ACCESS_RE.finditer(masked))
    return names - _KEYWORDS


def scan_nested_scope_leaks(html: str, ambient: set = frozenset(), static_globals: set = frozenset()) -> dict:
    """返回 {标识符: 越界读取次数}：只以嵌套 function 形态声明、却在声明所在顶层块之外被读取。"""
    html = _strip_html_comments(html)
    bodies = [_mask_js(_strip_jinja(b)) for b in _script_bodies(html)]
    if not bodies:
        return {}
    globalish: set = set()
    nested: dict = {}          # name -> [(block_idx, top_range)]
    reads: dict = {}           # name -> [(block_idx, pos)]
    for bi, masked in enumerate(bodies):
        depths = _brace_depths(masked)
        globalish |= _globalish_names(masked, depths)
        tops = _top_level_ranges(masked)
        for m in _DECL_FUNC_RE.finditer(masked):
            name = m.group(1)
            if not name or depths[m.start()] == 0:
                continue
            top = next((r for r in tops if r[0] <= m.start() <= r[1]), None)
            if top is not None:
                nested.setdefault(name, []).append((bi, top))
        for m in _READ_RE.finditer(masked):
            reads.setdefault(m.group(1), []).append((bi, m.start()))
    exposed = window_exposed(html)
    skip = (globalish | exposed | set(ambient) | set(static_globals) | _HOST_GLOBALS | _KEYWORDS)
    leaks: dict = {}
    for name, decls in nested.items():
        if name in skip:
            continue
        out = 0
        for bi, pos in reads.get(name, []):
            if not any(bi == dbi and r[0] <= pos <= r[1] for dbi, r in decls):
                out += 1
        if out:
            leaks[name] = out
    return leaks


def test_no_nested_function_read_out_of_scope(_ambient):
    """全站模板：只在别的函数体内声明的 function，不得在该顶层块之外被调用/读取（#237 形态）。"""
    violations = {}
    for tpl in _all_templates():
        rel = tpl.relative_to(TPL_DIR).as_posix()
        html = tpl.read_text(encoding="utf-8", errors="replace")
        leaks = scan_nested_scope_leaks(
            html,
            ambient=_ambient,
            static_globals=local_static_globals(html, STATIC_ROOT),
        )
        hits = {
            n: c for n, c in leaks.items()
            if (rel, n) not in _ACCEPTED_SCOPE and (rel, n) not in _PENDING_SCOPE
        }
        if hits:
            violations[rel] = hits
    assert not violations, (
        "模板 JS 在作用域外读取了只在别的函数体内声明的 function（必然 ReferenceError，#237 形态）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(violations.items()))
        + "\n→ 改用页面全局 helper（如 personas.html 的 _escHtml）/ 把声明提到顶层 / 良性命中登记 _ACCEPTED_SCOPE"
    )


def test_personas_restore_uses_global_escape_237():
    """#237 回归钉：personas.html 备份恢复 / JSON 导入预览不得再引用 CSV 内部的 _esc。"""
    html = (TPL_DIR / "personas.html").read_text(encoding="utf-8", errors="replace")
    leaks = scan_nested_scope_leaks(html)
    assert "_esc" not in leaks, f"_esc 又在 _exportFilteredCSV 之外被调用了（#237 复发）：{leaks}"
    body = "".join(_script_bodies(_strip_html_comments(html)))
    seg = body[body.index("function pbPickRestore"):body.index("window.pbExportOne = pbExportOne")]
    assert "_escHtml(" in seg and re.search(r"(?<![\w$.])_esc\(", _mask_js(seg)) is None
    assert "_pbFail(" in seg and "console.error(" in body[body.index("function _pbFail"):body.index("function _pbAlbumRefCount")], \
        "恢复失败必须 console.error 落 renderer.log（MJGHKQ：错误未落日志）"


def test_scope_detector_catches_237_shape():
    """探测器自证：#237 形态（别的函数体内 function、全局调用）必须被抓到；改成顶层声明即放行。"""
    broken = """
    <script>
    function exportCsv(){
      function _ghostEsc(v){ return String(v); }
      return [_ghostEsc(1)].join(',');
    }
    function renderPreview(d){ return '<b>' + _ghostEsc(d.name) + '</b>'; }
    renderPreview({name: 'x'});
    </script>
    """
    hits = scan_nested_scope_leaks(broken)
    assert hits.get("_ghostEsc") == 1, f"探测器没抓到 #237 形态：{hits}"

    fixed = """
    <script>
    function _ghostEsc(v){ return String(v); }
    function exportCsv(){ return [_ghostEsc(1)].join(','); }
    function renderPreview(d){ return '<b>' + _ghostEsc(d.name) + '</b>'; }
    renderPreview({name: 'x'});
    </script>
    """
    assert not scan_nested_scope_leaks(fixed), "顶层声明后不应再报——探测器误报"


def test_scope_detector_respects_reachable_shapes():
    """零误报底线：同顶层块内调用 / 形参同名 / 裸赋值提升 / window 暴露 / typeof 守卫 / 字符串里的同名都不报。"""
    benign = """
    <script>
    (function(){
      function helper(v){ return v; }
      function a(){ return helper(1); }
      if (true) { function hoisted(){ return 1; } }
      function b(){ return hoisted(); }   // Annex B：同一顶层块（IIFE）内可达
      a(); b();
    })();
    function outer(){ function asParam(x){ return x; } return asParam(1); }
    function useParam(asParam){ return asParam(2); }
    function makeG(){ function gImpl(){ return 3; } gFn = gImpl; }
    function useG(){ return gFn(); }
    function mk2(){ function exposedInner(){ return 4; } window.exposedInner = exposedInner; }
    function use2(){ return exposedInner(); }
    function mk3(){ function maybe(){ return 5; } }
    function use3(){ return typeof maybe === 'function' ? maybe() : 0; }
    function mk4(){ function inStr(){ return 6; } return inStr(); }
    var s = 'inStr(' + "inStr(" + `inStr(`;
    </script>
    """
    assert scan_nested_scope_leaks(benign) == {}, "良性形态被误报——放行集合有缺口"


def test_scope_registries_not_stale(_ambient):
    """_ACCEPTED_SCOPE/_PENDING_SCOPE 防过期。"""
    stale = []
    for (rel, name) in list(_ACCEPTED_SCOPE.keys()) + list(_PENDING_SCOPE.keys()):
        tpl = TPL_DIR / rel
        if not tpl.exists():
            stale.append((rel, name, "模板已不存在"))
            continue
        html = tpl.read_text(encoding="utf-8", errors="replace")
        leaks = scan_nested_scope_leaks(
            html, ambient=_ambient, static_globals=local_static_globals(html, STATIC_ROOT))
        if name not in leaks:
            stale.append((rel, name, "已不再命中（修好了？）"))
    assert not stale, f"作用域登记表过期条目，请清理：{stale}"
