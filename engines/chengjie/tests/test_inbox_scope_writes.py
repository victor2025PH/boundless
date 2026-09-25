# -*- coding: utf-8 -*-
"""视角写入收口门禁（2026-08-05「切平台残留旧会话」事故沉淀）。

unified_inbox 的「视角」由 platFilter / accountFilter 等全局变量构成，没有对象化的
单一事实源——每个赋值点都要人肉记得同步所有下游（rail 高亮 / 账号 chips / 列表标题 /
中栏会话联动 / hash）。实录已三次漏同步：切平台后中栏残留上一平台旧会话（错发风险）、
桌面深链 rail 高亮陈旧、?conv= 深链残留账号筛选把目标行挡在列表外。

机制化收口（**替代**「把读取点全改成 viewScope 对象」的大重构——15k 行热更新直上
生产的文件里改几百个读点，风险远大于收益；写入点收口 + 门禁钉死能达到同等不变量）：

  1. `platFilter` / `accountFilter` 的赋值只允许出现在**法定写入者**函数体内
     （或顶层 `let` 声明行）；其余任何位置的裸赋值 = 红。
  2. 会收窄视角的法定写入者必须调用联动器 `_syncThreadToScope(`——
     「写视角」与「联动中栏」是同一条不变量的两半。
     （`buildAccountChips` 例外：其写入是「账号不在册回落 all」的放宽自愈，放宽
     永不制造错位，且作为渲染函数它在 setAccountFilter 的联动之前执行。）

新增视角写入点时：优先复用 setPlatFilter / setAccountFilter；确需新写入者，把函数名
加进 `_ALLOWED_WRITERS`，并在体内调用 `_syncThreadToScope`（语义上是放宽也建议调——
统一律比逐点人脑判断可靠，放宽时它是 no-op）。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests import _inline_handler_scan as scan

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "unified_inbox.html"

# 法定写入者（函数名 → 是否必须调用联动器）
_ALLOWED_WRITERS = {
    "setPlatFilter": True,
    "setAccountFilter": True,
    "_applyView": True,        # 保存视图=多维一次性写入，自成法定入口
    "resetAllFilters": True,   # 放宽视角（统一律：仍要求调用，实为 no-op）
    "buildAccountChips": False,  # 「账号不在册回落 all」放宽自愈；渲染函数不强求联动
    # 顶栏账号坞同一条放宽自愈（选中号已不在册 → 回落 all）。渲染中调用
    # setAccountFilter 会重入 applyFilters，故与 chips 一样登记为放宽写入者。
    "buildAcctDock": False,
}

_ASSIGN_RE = re.compile(r"(?<![.\w$])(platFilter|accountFilter)\s*=(?![=])")


def _function_span(masked: str, name: str):
    """masked JS 里 `function NAME(...)  { ... }` 的正文区间（含花括号）；找不到返回 None。"""
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", masked)
    if not m:
        return None
    i = masked.find("{", m.end())
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(masked)):
        c = masked[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (m.start(), j + 1)
    return None


def _line_of(text: str, pos: int) -> str:
    a = text.rfind("\n", 0, pos) + 1
    b = text.find("\n", pos)
    return text[a:(len(text) if b < 0 else b)]


def _find_violations(html: str):
    """返回 (赋值点总数, 命中的法定写入者名集合, 违规清单)。"""
    total_hits = 0
    writers_seen = set()
    violations = []
    for body in scan._script_bodies(html):
        masked = scan._mask(body)
        spans = {}
        for name in _ALLOWED_WRITERS:
            sp = _function_span(masked, name)
            if sp:
                spans[name] = sp
                writers_seen.add(name)
        for m in _ASSIGN_RE.finditer(masked):
            total_hits += 1
            line = _line_of(body, m.start()).strip()
            if line.startswith("let ") or line.startswith("var ") or line.startswith("const "):
                continue  # 顶层声明
            if any(a <= m.start() < b for a, b in spans.values()):
                continue
            violations.append(f"{m.group(1)} 裸赋值（不在法定写入者内）: {line[:120]}")
    return total_hits, writers_seen, violations


def test_scope_writes_only_in_allowed_writers():
    html = _TPL.read_text(encoding="utf-8")
    total_hits, writers_seen, violations = _find_violations(html)
    assert total_hits >= 5, "扫描器疑似失效：platFilter/accountFilter 赋值点少于预期"
    assert writers_seen == set(_ALLOWED_WRITERS), (
        f"法定写入者函数缺失/改名（门禁需同步）：缺 {set(_ALLOWED_WRITERS) - writers_seen}")
    assert not violations, (
        "视角变量出现收口外裸赋值（改用 setPlatFilter/setAccountFilter，"
        "或把新写入者登记进 _ALLOWED_WRITERS 并调用 _syncThreadToScope）：\n" + "\n".join(violations))


def test_detector_catches_tampering():
    """探测器有效性自证：法定写入者之外的裸赋值必须被抓到（门禁不是摆设）。"""
    bad = "<script>\nfunction sneaky(){ platFilter='whatsapp'; }\n</script>"
    _, _, violations = _find_violations(bad)
    assert violations, "探测器失效：收口外裸赋值未被识别"
    # 法定写入者体内 + 声明行不误报
    ok = ("<script>\nlet platFilter='all';\n"
          "function setPlatFilter(p){ platFilter=p; _syncThreadToScope(); }\n</script>")
    _, _, v2 = _find_violations(ok)
    assert not v2, f"探测器误报：{v2}"


def test_scope_writers_call_thread_sync():
    html = _TPL.read_text(encoding="utf-8")
    missing = []
    for body in scan._script_bodies(html):
        masked = scan._mask(body)
        for name, must_sync in _ALLOWED_WRITERS.items():
            if not must_sync:
                continue
            sp = _function_span(masked, name)
            if not sp:
                continue
            seg = body[sp[0]:sp[1]]
            if "_syncThreadToScope(" not in seg:
                missing.append(name)
    assert not missing, f"法定写入者漏调联动器 _syncThreadToScope：{missing}"


def test_thread_sync_helpers_exist():
    """联动器三件套存在且谓词与 applyFilters 同源（_scopeMatch 单一谓词）。"""
    html = _TPL.read_text(encoding="utf-8")
    js = "\n".join(scan._script_bodies(html))
    for fn in ("_syncThreadToScope", "_threadMatchesScope", "_scopeMatch", "_closeThreadPane"):
        assert f"function {fn}(" in js, f"联动器函数缺失：{fn}"
    # openOldestWaiting（空态 CTA/快捷键 O）必须收在视角内——与切平台联动同一条不变量
    m = re.search(r"function openOldestWaiting\([\s\S]{0,400}", js)
    assert m and "_scopeMatch(" in m.group(0), "openOldestWaiting 未按视角过滤（O 键会跳出当前平台）"
