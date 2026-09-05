"""全站模板「内联 handler 字符串实参裸 JSON.stringify」门禁（#191 沉淀）。

背景：`personas.html` 历史质检行的 onclick 曾这样拼：
    '<button onclick="_quizOpenHist(' + JSON.stringify(id) + ')">'
`JSON.stringify("5")` 产出带双引号的 `"5"`，塞进**双引号** HTML 属性里就把属性截成
`_quizOpenHist(` → 每一行点击必抛 `SyntaxError: Unexpected end of input` → 全局守卫
`_boot_error_guard.html` 弹「页面脚本出错，部分功能可能失效」红条（skuio #191，1.0.73）。
同一形状在 `_pdiShowSources` 的「📎 原文」链接上也有（从未能点开）。

静态门禁抓不到它（函数存在且挂全局，只是属性被截断），运行时守卫又拿不到函数名，
所以这里补一条**形状级**窄不变量：

  双引号内联事件属性 `on*="…"` 的生成串里，不得把 `JSON.stringify(...)` 的结果**裸拼**进去。
  两种形状都抓：
    - 字符串拼接：`on*="fn(' + JSON.stringify(x) + ')"`
    - 模板字面量：`on*="fn(${JSON.stringify(x)})"`

正解：`_escAttr(JSON.stringify(x))`（personas 里封装成 `_jsArgAttr`）、
`JSON.stringify(x).replace(/"/g, '&quot;')`（episodic_memory 的写法），或彻底改
事件委托 + `data-*` 属性。这些写法 `JSON.stringify(...)` 后面紧跟 `.replace`/被包一层函数，
不匹配本门禁的形状。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = [
    _ROOT / "src" / "web" / "templates",
    _ROOT / "src" / "web" / "static",
    _ROOT / "shared",
]
_ALL = sorted(
    p for d in _SCAN_DIRS if d.exists()
    for ext in ("*.html", "*.js")
    for p in d.rglob(ext)
    if "node_modules" not in p.parts
)

# JSON.stringify( ... ) 的实参：允许一层括号嵌套（String(x || '') 这类）。
_ARGS = r"(?:[^()]|\([^()]*\))*"
_CONCAT = re.compile(
    r"""\bon\w+="[^"\n]*'\s*\+\s*JSON\.stringify\(""" + _ARGS + r"""\)\s*\+\s*'"""
)
_TEMPLATE = re.compile(
    r"""\bon\w+="[^"\n]*\$\{\s*JSON\.stringify\(""" + _ARGS + r"""\)\s*\}"""
)

# 良性命中允许清单：{文件名: {命中片段, ...}}。当前为空。
_ALLOWLIST: dict[str, set[str]] = {}


def _violations(src: str) -> set:
    out = {m.group(0) for m in _CONCAT.finditer(src)}
    out |= {m.group(0) for m in _TEMPLATE.finditer(src)}
    return out


def test_no_raw_json_stringify_in_double_quoted_inline_handlers():
    failures = {}
    for f in _ALL:
        try:
            src = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits = _violations(src) - _ALLOWLIST.get(f.name, set())
        if hits:
            failures[str(f.relative_to(_ROOT))] = sorted(hits)
    assert not failures, (
        "有模板把 JSON.stringify(...) 裸拼进双引号内联事件属性——字符串实参自带的双引号会把属性截断，"
        "点击即 SyntaxError（#191 历史质检行「页面脚本出错」根因）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：_escAttr(JSON.stringify(x)) / .replace(/\"/g,'&quot;') / 事件委托 + data-* 属性。"
    )


def test_allowlist_not_stale():
    stale = {}
    for name, frags in _ALLOWLIST.items():
        cands = [p for p in _ALL if p.name == name]
        if not cands:
            continue
        src = cands[0].read_text(encoding="utf-8")
        gone = sorted(set(frags) - _violations(src))
        if gone:
            stale[name] = gone
    assert not stale, (
        "以下允许清单命中已消失（代码已改），请从 _ALLOWLIST 移除：\n"
        + "\n".join(f"  {k}: {v}" for k, v in stale.items())
    )


def test_scanner_self_check():
    # 抓：#191 原形（字符串拼接）
    assert _violations("""x = '<button onclick="_quizOpenHist(' + JSON.stringify(id) + ')">';""")
    # 抓：实参带一层嵌套
    assert _violations("""x = '<a onclick="f(' + JSON.stringify(String(k || '')) + ')">';""")
    # 抓：模板字面量形状
    assert _violations("""x = `<a onclick="f(${JSON.stringify(k)})">`;""")
    # 不抓：属性转义包了一层（personas `_jsArgAttr` / 直接 _escAttr）
    assert not _violations("""x = '<a onclick="f(' + _escAttr(JSON.stringify(k)) + ')">';""")
    assert not _violations("""x = '<a onclick="f(' + _jsArgAttr(k) + ')">';""")
    # 不抓：episodic_memory 的 .replace 写法
    assert not _violations(
        """x = '<a onclick="f(' + JSON.stringify(String(k || '')).replace(/"/g, '&quot;') + ')">';"""
    )
    # 不抓：JSON.stringify 用在属性之外（data-* 或 textContent）
    assert not _violations("""el.setAttribute('data-x', JSON.stringify(v)); s = 'a' + JSON.stringify(v) + 'b';""")
    # 不抓：单引号属性里放 JSON（双引号不冲突）
    assert not _violations('''x = "<a onclick='f(" + JSON.stringify(k) + ")'>";''')


def test_personas_quiz_history_row_is_attribute_safe():
    """#191 回归钉：历史质检行与「📎 原文」链接的实参必须经过 _jsArgAttr。"""
    src = (_ROOT / "src" / "web" / "templates" / "personas.html").read_text(encoding="utf-8")
    assert "onclick=\"_quizOpenHist(' + _jsArgAttr(id) + ')\"" in src
    assert "onclick=\"_pdiShowSources(' + _jsArgAttr(fk) + ')\"" in src
    assert re.search(r"function _jsArgAttr\(v\)\s*\{\s*return _escAttr\(JSON\.stringify\(", src)
