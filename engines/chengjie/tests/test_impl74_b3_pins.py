# -*- coding: utf-8 -*-
"""实施74 三批静态钉：B60 群组区可滚 + B47 丢弃留副本。

- B60（0823 _307 原报障已不复现，真浏览器六态实测 footer 可见未遮挡——由
  verify_inbox_density 6c 段常驻钉住）：本批实锤修复＝renderGroupSection 曾用
  display='block' 覆写 CSS 的 flex，群组区 body 不受 46% 封顶的 flex 分配约束，
  群一多超出部分滚不到（40 群实测）。
- B47（0822 _273 + 22:42 追报）：「忽略草稿条 → 丢弃」＝内容永久丢失。修复＝
  确认丢弃前强制 _saveDraft() 暂存副本（找回口=既有「发现未保存草稿」恢复条）；
  「编辑器锁死」半边与 B54 同根（Electron confirm 焦点丢失），已由 0826 批
  P1-16 的 _focus_selfheal 双基座覆盖（personas 继承 base.html）。
"""
from __future__ import annotations

import re
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ENGINE_ROOT / rel).read_text(encoding="utf-8")


# ── B60 ──────────────────────────────────────────────────────────────────────

def test_group_section_render_uses_flex():
    src = _read("src/web/templates/unified_inbox.html")
    m = re.search(r"function renderGroupSection\(\)\{(.*?)\n\}", src, re.S)
    assert m, "缺 renderGroupSection"
    body = m.group(1)
    assert "sec.style.display='flex'" in body
    # 反向：本函数内 block 覆写绝不回潮（页面其他区域的同名变量不在此限）
    assert "sec.style.display='block'" not in body


def test_group_sec_body_flex_scroll_css():
    css = _read("src/web/static/workspace/unified-inbox.css")
    m = re.search(r"\.group-sec-body\{([^}]*)\}", css)
    assert m, "缺 .group-sec-body 规则"
    body = m.group(1)
    assert "min-height:0" in body and "flex:1" in body
    assert "overflow-y:auto" in body


def test_density_gate_carries_b60_assertions():
    tool = _read("tools/verify_inbox_density.py")
    assert "_B60_JS" in tool and "_B60_EXPAND_JS" in tool
    assert "6c. B60" in tool


# ── B47 ──────────────────────────────────────────────────────────────────────

def test_discard_stashes_draft_copy():
    src = _read("src/web/templates/personas.html")
    m = re.search(r"function _closeDrawer\(\)\s*\{(.*?)\n\}", src, re.S)
    assert m, "缺 _closeDrawer"
    body = m.group(1)
    # 暂存必须发生在 confirm 之后、关抽屉之前（丢弃永远留可找回副本）
    assert "_saveDraft()" in body
    assert body.index("confirm(") < body.index("_saveDraft()")
    assert body.index("_saveDraft()") < body.index("classList.remove('open')")


def test_draft_restore_chain_intact():
    """找回口＝既有恢复条：apply/dismiss/checkDraft 三件不许被顺手删。"""
    src = _read("src/web/templates/personas.html")
    for fn in ("function _applyDraft(", "function _dismissDraft(",
               "function _checkDraft("):
        assert fn in src, fn


def test_discard_copy_bilingual_and_honest():
    from src.web.i18n_packs.persona_studio import EN, ZH
    assert "暂存" in ZH["psn_js_002"]
    assert "stash" in EN["psn_js_002"].lower()
