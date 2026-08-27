"""base.html（全部经典后台页共享宿主）禁止引用「仅 RPA partial 暴露」的全局标识符。

事故原型（2026-08-18，「?」术语球右键红条）：base.html tooltip 的 contextmenu 处理写了
`rpa&&rpa.toast?...`——`rpa` 只在 `_rpa_shared_scripts.html`（仅 RPA 渠道页 include）里
经 `window.rpa = {}` 挂载；其余 ~30 个 base 家族页面上它是**未声明标识符**，右键「?」球
即 `ReferenceError: rpa is not defined` → _boot_error_guard 红条「页面脚本出错（rpa）」
+ frontend-error 遥测被伪错误刷数。`&&` 防得住 undefined 属性，防不住未声明变量。

为什么三个既有静态门禁按设计都不覆盖（本文件补窄钉）：
- 哑按钮门禁只看内联 on*= 属性（此处是 addEventListener 回调体内引用）；
- 孤儿引用门禁只看 DOM id；
- 自由捕获门禁判据 (c) 要求「同页某闭包里有声明」（rpa 在 base.html 里无任何声明），
  且其 ambient_globals 把全部 `_*.html` partial 的全局并入环境集（为免 include 页误报）
  ——「引用了未 include 的 partial 的全局」这一类因此全站不可见。

窄不变量（宁漏勿误伤）：base.html 引用的裸标识符必须在自身（顶层声明 / window 挂载）
或其无条件 include 的 partial 里可达；RPA partial 不在其中。只钉
base.html × _rpa_shared_scripts.html 这一对——全站 include 图感知的通用化留给后续批次，
别在这里扩面（partial 全局名与各页闭包局部名重名会假阳）。
"""
import re
from pathlib import Path

from tests import _free_capture_scan as fc
from tests import _inline_handler_scan as scan

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_BASE = (_TPL_DIR / "base.html").read_text(encoding="utf-8")
_RPA_PARTIAL = (_TPL_DIR / "_rpa_shared_scripts.html").read_text(encoding="utf-8")

# 允许名单：若将来 base.html 出现与 partial 全局重名的合法闭包局部变量，登记于此并附原因。
_ALLOW: set = set()


def _partial_only_globals() -> set:
    """RPA partial 暴露、而 base.html（含其无条件 include 的其他 partial）不可达的全局名。"""
    exported = scan.window_exposed(_RPA_PARTIAL) | scan.global_scope_names(_RPA_PARTIAL)
    reachable = scan.global_scope_names(_BASE) | scan.window_exposed(_BASE)
    for m in re.finditer(r'{%\s*include\s+"([^"]+)"', _BASE):
        p = _TPL_DIR / m.group(1)
        if p.name == "_rpa_shared_scripts.html" or not p.exists():
            continue
        inc = p.read_text(encoding="utf-8")
        reachable |= scan.global_scope_names(inc) | scan.window_exposed(inc)
    return exported - reachable - _ALLOW


def _masked_base_js() -> str:
    """base.html 全部内联脚本，串/注释/正则/Jinja 掩码后拼接（只剩真实代码标识符）。"""
    html = scan._strip_html_comments(_BASE)
    return "\n".join(fc._masked(fc._strip_jinja(b)) for b in scan._script_bodies(html))


def test_detector_sees_rpa_namespace():
    """探测器有效性自证：partial 改挂载写法导致提取失效时这里先红，而不是门禁静默失明。"""
    assert "rpa" in _partial_only_globals()


def test_base_never_references_rpa_partial_globals():
    masked = _masked_base_js()
    hits = {}
    for name in sorted(_partial_only_globals()):
        pat = re.compile(r"(?<![.\w$])" + re.escape(name) + r"\b")
        n = len(pat.findall(masked))
        if n:
            hits[name] = n
    # 内联 on*= 属性体里的调用在非 RPA 页同样必崩，一并钉住。
    inline_hits = scan.referenced(_BASE) & _partial_only_globals()
    assert not hits and not inline_hits, (
        "base.html 引用了仅 _rpa_shared_scripts.html（RPA 页专属）暴露的全局——"
        "非 RPA 页运行必 ReferenceError（『?球右键红条』同款）。"
        f"脚本体命中: {hits}；内联 handler 命中: {sorted(inline_hits)}。"
        "修法：改用 base 原生设施（如 showToast），"
        "或把该能力挂进 base 自身 / 其无条件 include 的 partial。"
    )
