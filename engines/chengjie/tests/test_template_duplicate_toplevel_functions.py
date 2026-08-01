"""前端门禁：同页顶层 `function NAME` 同名双定义（后者静默覆盖前者）。

实锤（2026-08-01，unified_inbox）：注解 @ 推荐的 `_pickMention` 与 WhatsApp 群
@ 成员选择器的 `_pickMention` 同页同名——group 版声明在后，覆盖全局绑定，
注解推荐面板的「点选/回车选人」全部静默失效（`_mentionFiltered[i]` 为空直接
return，无任何报错）。既有哑按钮门禁只查「函数存在且全局可达」，此类
「存在、可达、但被同名者顶包」是它的盲区，本门禁补上。

判据：同一模板文件（全部内联 <script> 拼合成同一全局命名空间）里，
顶层作用域（括号深度 0、非表达式位）的 `function NAME` 声明出现 ≥2 次。
`var/let/const` 重复刻意不管——`var` 重复声明合并是 JS 常态且多为分块初始化。

已存在的历史命中登记 `_PENDING_DUPS`（真 bug 待各自工作流处置，本门禁保绿 +
债务可见）；`test_pending_dups_ledger_not_stale` 防表过期（修好了必须摘牌）。
"""

import re
from pathlib import Path

from tests import _inline_handler_scan as scan

TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"

# 假阳性登记（文件名 → {函数名: 原因}）：Jinja 互斥分支等「原始模板里两次、
# 渲染后只剩一次」的形态。与 test_template_unique_ids 的 _ACCEPTED_DUP_IDS 同范式。
_ACCEPTED_DUPS: dict = {
    "knowledge.html": {
        # 950 {% if %} 真实现 / 986 {% else %} no-op stub —— 渲染后二取一，非双定义
        "_scheduleConflictCheck": "Jinja if/else 互斥分支（功能关闭时渲染 no-op 桩）",
    },
}

# 真 bug 待处置登记（文件名 → 函数名集合）：确认是同页双定义、后者已在覆盖前者，
# 归属工作流修复后必须摘牌（防表过期门禁看住）。
_PENDING_DUPS: dict = {
    "knowledge.html": {
        # 801 vs 1846：两版实现（v1 有 feedback 分支 / v2 null-safe），后者胜出
        "switchTab",
        # 830 vs 1861：两套 debounce 计时器（_debounceTimer/350ms vs _searchTimer/300ms）
        "debounceLoadEntries",
    },
}


def _toplevel_function_decls(js: str):
    """某 <script> 正文里顶层作用域的 function 声明名列表（含重复）。"""
    masked = scan._mask(js)
    depth = [0] * (len(masked) + 1)
    d = 0
    for idx, ch in enumerate(masked):
        depth[idx] = d
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d = d - 1 if d > 0 else 0
    names = []
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", masked):
        s = m.start()
        if depth[s] != 0:
            continue
        k = s - 1
        while k >= 0 and masked[k].isspace():
            k -= 1
        # 排除具名函数表达式 / IIFE（`= function f` / `(function f`）——不进全局
        if k >= 0 and masked[k] in "=(,:?&|":
            continue
        names.append(m.group(1))
    return names


def _dups_in_file(path: Path) -> set:
    html = path.read_text(encoding="utf-8")
    seen: dict = {}
    for body in scan._script_bodies(html):
        for name in _toplevel_function_decls(body):
            seen[name] = seen.get(name, 0) + 1
    return {n for n, c in seen.items() if c >= 2}


def _scan_all() -> dict:
    out = {}
    for f in sorted(TPL_DIR.rglob("*.html")):
        dups = _dups_in_file(f)
        if dups:
            out[f.name] = dups
    return out


def _allowed(fn: str) -> set:
    return set(_ACCEPTED_DUPS.get(fn, {})) | _PENDING_DUPS.get(fn, set())


def test_no_duplicate_toplevel_function_declarations():
    found = _scan_all()
    fresh = {
        fn: sorted(names - _allowed(fn))
        for fn, names in found.items()
        if names - _allowed(fn)
    }
    assert not fresh, (
        "同页顶层 function 同名双定义（后者覆盖前者，前者一切内联调用静默失效）："
        f"{fresh}；请重命名其一（参考 _pickMention → _pickNoteMention）；"
        "Jinja 互斥分支类假阳性登记 _ACCEPTED_DUPS 附原因，真 bug 登记 _PENDING_DUPS。"
    )


def test_dup_ledgers_not_stale():
    found = _scan_all()
    stale = {}
    for fn, names in _PENDING_DUPS.items():
        gone = names - found.get(fn, set())
        if gone:
            stale[fn] = sorted(gone)
    for fn, entries in _ACCEPTED_DUPS.items():
        gone = set(entries) - found.get(fn, set())
        if gone:
            stale.setdefault(fn, []).extend(sorted(gone))
    assert not stale, f"双定义台账里这些条目已不复现，请摘牌：{stale}"
