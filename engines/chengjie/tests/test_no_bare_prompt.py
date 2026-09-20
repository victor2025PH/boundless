# -*- coding: utf-8 -*-
"""禁止前端再出现原生 ``prompt(`` 调用（#173 「批量挂链点了没反应」，2026-09-05）。

事故机制：Electron 渲染进程里 ``typeof prompt === "function"`` 为真，**调用即抛**
「is and will not be supported」——按钮看起来「静默死」，且 workflows 等独立窗口的
console 不进 renderer.log，事后零痕迹。alert/confirm 都正常，只有 prompt 是坑。

统一替代＝``window.uiPrompt(title, def, opts) → Promise<string|null>``
（``shared/copilot/ui-prompt.js``，经 ``_win_unique.html``（base + workspace_base
两壳）与 ``shared/copilot/app.html`` 三处接入；Esc/背板＝取消 null）。

两条不变量：
1. **ratchet**：``_LEDGER`` 是每个文件裸 prompt 调用数的**非增天花板**——存量债务
   如实登记（全部在桌面壳里是坏的），新增即红；修掉一处必须同步下调天花板
   （防天花板过期＝债务隐形）。
2. **地基在场**：ui-prompt.js 存在、两端镜像一致、三处宿主都引入了它、且它导出
   ``window.uiPrompt``——否则替换过去的调用点在桌面壳里会从「抛错」变成「静默不弹」。

匹配口径：``prompt(`` 前不是标识符字符/``.``/``$``（排除 ``window.prompt`` /
``uiPrompt`` / ``_promptModal`` 等）且括号内非空（排除注释里的 ``prompt()`` 提法）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates"
_STATIC = _ROOT / "src" / "web" / "static"
_COPILOT = _ROOT / "shared" / "copilot"
_COPILOT_DESKTOP = _ROOT / "desktop" / "renderer" / "shared" / "copilot"

_BARE_PROMPT = re.compile(r"(?<![\w.$])prompt\s*\(\s*[^)\s]")

# 存量债务台账（相对引擎根 posix 路径 → 天花板）。全部在 Electron 桌面壳里是坏的，
# 修一处下调一格；不在台账的文件天花板恒为 0。
_LEDGER: dict[str, int] = {
    "src/web/templates/_channel_body_messenger.html": 3,
    "src/web/templates/dashboard.html": 3,
    "src/web/templates/draft_review.html": 1,
    "src/web/templates/ops_overview.html": 1,
    "src/web/templates/singing.html": 1,
    "src/web/templates/unified_inbox.html": 6,
    "src/web/templates/workspace_dashboard.html": 1,
    "src/web/static/messenger/messenger_rpa.js": 6,
}


def _scan_files():
    files = []
    files += sorted(_TPL.rglob("*.html"))
    files += sorted(_STATIC.rglob("*.js"))
    files += sorted(p for p in _COPILOT.rglob("*") if p.suffix in (".js", ".html"))
    return [p for p in files if p.is_file() and ".min." not in p.name]


def _count(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    return len(_BARE_PROMPT.findall(text))


def _rel(p: Path) -> str:
    return p.relative_to(_ROOT).as_posix()


def test_no_new_bare_prompt_calls():
    over = []
    for p in _scan_files():
        n = _count(p)
        cap = _LEDGER.get(_rel(p), 0)
        if n > cap:
            over.append(f"{_rel(p)}: {n} > 天花板 {cap}")
    assert not over, (
        "出现新的原生 prompt( 调用——Electron 渲染进程调用即抛（按钮静默死）。"
        "请改用 window.uiPrompt(title, def).then(...)（/copilot/ui-prompt.js，"
        "Esc/背板=null）：\n  " + "\n  ".join(over))


def test_ledger_not_stale():
    """天花板只降不升：修掉一处必须同步下调，防台账过期把债务藏起来。"""
    stale = []
    for rel, cap in _LEDGER.items():
        p = _ROOT / rel
        if not p.is_file():
            stale.append(f"{rel}: 文件已不存在，删台账行")
            continue
        n = _count(p)
        if n < cap:
            stale.append(f"{rel}: 实际 {n} < 天花板 {cap}，请下调")
    assert not stale, "台账过期：\n  " + "\n  ".join(stale)


def test_ui_prompt_foundation_present():
    src = _COPILOT / "ui-prompt.js"
    assert src.is_file(), "缺 shared/copilot/ui-prompt.js（uiPrompt 单一定义）"
    body = src.read_text(encoding="utf-8")
    assert "window.uiPrompt = uiPrompt" in body, "ui-prompt.js 未导出 window.uiPrompt"
    for needle in ('"Escape"', "mousedown"):
        assert needle in body, f"ui-prompt.js 缺 {needle} 处理（Esc/背板关闭是契约）"
    mirror = _COPILOT_DESKTOP / "ui-prompt.js"
    assert mirror.is_file() and mirror.read_bytes() == src.read_bytes(), \
        "desktop/renderer/shared/copilot/ui-prompt.js 缺失或与 web 份不一致（跑 scripts/sync_copilot_mirror.py）"


@pytest.mark.parametrize("host", [
    _TPL / "_win_unique.html",
    _COPILOT / "app.html",
])
def test_hosts_include_ui_prompt(host: Path):
    text = host.read_text(encoding="utf-8")
    assert re.search(r'<script src="/copilot/ui-prompt\.js\?v=\w+"></script>', text), \
        f"{host.name} 未引入 /copilot/ui-prompt.js——该壳下的 uiPrompt 调用会静默不弹"


def test_regex_semantics():
    """匹配器自证：抓裸调用、放过 window.prompt / uiPrompt / 注释里的 prompt()。"""
    assert _BARE_PROMPT.search("var d=prompt(window.T('x'),'1');")
    assert _BARE_PROMPT.search("const r = prompt (T('k'))")
    assert not _BARE_PROMPT.search("window.prompt(x)")
    assert not _BARE_PROMPT.search("await window.uiPrompt(t, '')")
    assert not _BARE_PROMPT.search("_promptModal(a)")
    assert not _BARE_PROMPT.search("/* 替换 prompt() */")
    assert not _BARE_PROMPT.search("typeof prompt === 'function'")
