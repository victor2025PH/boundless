# -*- coding: utf-8 -*-
"""CSS 注释陷阱门禁：注释正文里的「星号紧贴斜杠」提前闭合注释，并吞掉下一条规则。

2026-08-20 实锤事故（内测 B12「左侧会话列表『更多操作』点击没反应」）：
``unified-inbox.css`` 的说明注释里把 token 族写成 ``--text-*`` + ``/`` + ``--accent``
（斜杠分隔的枚举），那两个字符恰好是注释结束符 → 注释在此提前闭合，剩下的中文说明
变成非法 CSS，真正的结束符成了游离的结束符 = 解析错误，浏览器错误恢复把**紧随其后
的整条规则丢弃**。被吞的正是 ``.ws-ctx-menu``（右键/更多操作菜单的 ``position:fixed``
与宽度）→ 菜单退化成文档流末尾的普通块：全宽、落在首屏之外，坐席看到的就是「点了
毫无反应」。同一文件另一处（``--bdg-*`` + ``/`` + ``--tk-*-ink``）吞掉了
``.connect-mode-notice`` 的底色规则。

这类缺陷**不报错、不变红、CSS 文件本身花括号完全平衡**：浏览器只是静默少一条规则。
更阴的是两处都出现在「修配色」的说明注释里——即 2026-08-19 那次 B7 配色修复，改对了
规则内容，却被自己新写的注释把整条规则删掉了，于是修复从未生效、并顺带长出 B12。

不变量：CSS 注释里不得出现游离的注释结束符（等价于「注释正文不得含结束符」）。
写 token 通配枚举时用 ``--text-*`` 与 ``--accent`` 这类分词，别用斜杠紧贴星号。

扫描范围＝静态 CSS + 模板内联 ``<style>`` 块。模板先剥 Jinja 注释：那是服务端渲染时
就消失的文本，浏览器根本看不到（``_channel_body_telegram.html`` 的 Jinja 注释里正有
``st-*`` + ``/`` + ``f-*`` 这类写法，属真阴性，不剥就是假阳性）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
_SRC_WEB = _ROOT / "src" / "web"

_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)

#: (相对路径, 行号) → 原因；理想恒空。
_ALLOWLIST: dict = {}


def _stray_terminators(text: str, base_line: int = 1) -> List[int]:
    """按 CSS 注释语义扫描，返回出现「注释外的结束符」的行号。

    注释不可嵌套：``/*`` 之后的第一个 ``*/`` 就是结束。故注释正文里的 ``*/``
    会让其后真正的结束符变成游离符——即本函数抓的东西。
    """
    i, n, line, in_comment = 0, len(text), base_line, False
    hits: List[int] = []
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if in_comment:
            if text.startswith("*/", i):
                in_comment = False
                i += 2
                continue
            i += 1
            continue
        if text.startswith("/*", i):
            in_comment = True
            i += 2
            continue
        if text.startswith("*/", i):
            hits.append(line)
            i += 2
            continue
        i += 1
    return hits


def _scan(path: Path) -> List[int]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() == ".css":
        return _stray_terminators(text)
    hits: List[int] = []
    for m in _STYLE_BLOCK.finditer(text):
        body = m.group(1)
        # Jinja 注释整段在渲染时消失 → 用等长空白替换，保住行号对齐
        body = _JINJA_COMMENT.sub(lambda c: re.sub(r"[^\n]", " ", c.group(0)), body)
        hits += _stray_terminators(body, text[: m.start(1)].count("\n") + 1)
    return hits


def _targets() -> List[Path]:
    files = sorted((_SRC_WEB / "static").rglob("*.css"))
    files += sorted((_SRC_WEB / "templates").rglob("*.html"))
    return files


def test_css_comments_do_not_terminate_early():
    bad: List[str] = []
    for p in _targets():
        rel = p.relative_to(_ROOT).as_posix()
        for ln in _scan(p):
            if (rel, ln) in _ALLOWLIST:
                continue
            bad.append(f"{rel}:{ln}")
    assert not bad, (
        "以下位置出现游离的 CSS 注释结束符——说明上面某条注释的正文里写了星号紧贴斜杠"
        "（多见于 --a-*/--b 这种 token 枚举），注释被提前闭合，紧随其后的整条规则会被"
        "浏览器静默丢弃（不报错、花括号照样平衡）。请把枚举改成分词写法：\n"
        + "\n".join(bad)
    )


def test_menu_rule_survives_parsing():
    """B12 修复锚：右键/更多操作菜单的定位规则必须处在「注释外」，否则又被吞。

    只钉这一条最痛的：``.ws-ctx-menu`` 的 ``position:fixed`` 被吞 ⇒ 菜单跑到首屏
    之外 ⇒ 点击毫无反应（左键 ⋯ 与右键两个入口同时坏）。
    """
    css = _SRC_WEB / "static" / "workspace" / "unified-inbox.css"
    text = css.read_text(encoding="utf-8")
    assert not _stray_terminators(text), "unified-inbox.css 有游离注释结束符，规则会被吞"
    # 注释剥净后规则仍在（防「规则整块被搬进注释」这种等价失效）
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    m = re.search(r"\.ws-ctx-menu\s*\{([^}]*)\}", stripped)
    assert m, ".ws-ctx-menu 规则不见了（被注释掉或被删）"
    assert "position:fixed" in m.group(1).replace(" ", ""), (
        f".ws-ctx-menu 必须保持 position:fixed（否则菜单落在文档流末尾）：{m.group(1)!r}"
    )


def test_allowlist_not_stale():
    for (rel, ln) in _ALLOWLIST:
        p = _ROOT / rel
        assert p.exists(), f"allowlist 指向不存在的文件: {rel}"
        assert ln in set(_scan(p)), f"allowlist 过期（该行已无陷阱）: {rel}:{ln}"
