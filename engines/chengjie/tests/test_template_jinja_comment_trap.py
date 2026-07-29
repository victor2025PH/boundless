# -*- coding: utf-8 -*-
"""Jinja 注释陷阱门禁：模板里「花括号紧贴井号」＝注释开始符，整段吞内容。

2026-07-29 实锤事故：渠道中心页在 <style> 里写了
``@media(max-width:640px){`` + ``#chc-acct-empty{...}``（连写），Jinja 把
「花括号+井号」当注释开始，吞掉了从这里到下一个注释结束符之间的一切——
``</style>``、``{% endblock %}``、``{% block content %}`` 开头、页头/tabs 全没了，
正文被塞进 <head> 渲染：顶栏跑到页面底部、壳布局全塌、div 失配 -3。

该错误 **Jinja 语法检查完全合法**（就是个注释）、纯文本门禁也看不出，只有
渲染层才炸——所以静态钉死字符模式本身：模板中「花括号+井号」连写只允许
两种合法形态：注释后跟空白（``左花括号# 说明``）或 trim 形（``左花括号#-``）。
CSS 的 id 选择器跟在花括号后时，必须加空格（``{ #id{...}``）。

新增合法用法若被误伤，把 (相对路径, 行号) 加进 _ALLOWLIST 并附原因。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL_DIR = _ROOT / "src" / "web" / "templates"

# 花括号+井号连写，且后面不是空白/换行/trim 减号 → 视为陷阱
_TRAP = re.compile(r"\{#(?![\s\-])")

# (相对路径, 行号) → 原因；当前应为空
_ALLOWLIST: dict = {}


def _scan(path: Path):
    hits = []
    text = path.read_text(encoding="utf-8")
    for ln, line in enumerate(text.split("\n"), 1):
        for m in _TRAP.finditer(line):
            hits.append((ln, line.strip()[:120], m.start()))
    return hits


def test_no_jinja_comment_trap_in_templates():
    bad = []
    for p in sorted(_TPL_DIR.rglob("*.html")):
        rel = p.relative_to(_ROOT).as_posix()
        for ln, snippet, col in _scan(p):
            if (rel, ln) in _ALLOWLIST:
                continue
            bad.append(f"{rel}:{ln}:{col}  {snippet}")
    assert not bad, (
        "模板出现「花括号紧贴井号」＝Jinja 注释开始符，会整段吞内容（渲染层才炸，"
        "语法检查不报错）。CSS id 选择器请在花括号后加空格：\n" + "\n".join(bad)
    )


def test_allowlist_not_stale():
    for (rel, ln) in _ALLOWLIST:
        p = _ROOT / rel
        assert p.exists(), f"allowlist 指向不存在的文件: {rel}"
        hits = {h[0] for h in _scan(p)}
        assert ln in hits, f"allowlist 过期（该行已无陷阱模式）: {rel}:{ln}"
