# -*- coding: utf-8 -*-
"""工作台模板内联脚本语法门禁：``unified_inbox.html`` / ``workspace_base.html`` 的每段 <script> 交给 ``node --check``。

这两个文件几万行、多条开发线并行追加，一个漏掉的括号会让整个工作台白屏而所有 Python 测试照常绿。
Jinja 表达式先做无害替换（``{{ x }}``→``0``，``{% %}``→删）。本机没有 node → 跳过（不制造假绿也不阻塞）。
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATES = ("src/web/templates/unified_inbox.html", "src/web/templates/workspace_base.html",
              "src/web/templates/connect_guide.html")
_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)(?![^>]*type=\"(?:application/json|text/template|text/x-template|application/ld\+json)\")"
    r"[^>]*>(.*?)</script>", re.S)


def _inline_scripts(html: str):
    for blk in _SCRIPT_RE.findall(html):
        if blk.strip():
            js = re.sub(r"\{\{.*?\}\}", "0", blk, flags=re.S)
            js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
            yield blk, js


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用：跳过内联脚本语法门禁")
@pytest.mark.parametrize("rel", _TEMPLATES)
def test_inline_scripts_parse(rel):
    path = _ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} 不存在")
    html = path.read_text(encoding="utf-8")
    errors = []
    for blk, js in _inline_scripts(html):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(js)
            tmp = fh.name
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            m = re.search(r":(\d+)\n", r.stderr)
            start = html[: html.find(blk)].count("\n") + 1
            where = start + (int(m.group(1)) - 1 if m else 0)
            errors.append(f"{rel}:{where}: {r.stderr.strip().splitlines()[-1][:160]}")
    assert not errors, "\n".join(errors)
