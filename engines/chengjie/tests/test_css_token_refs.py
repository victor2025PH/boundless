# -*- coding: utf-8 -*-
"""CSS 语义 token 引用完整性门禁（2026-08-10 接入弹窗「暗色看不清」事故沉淀）。

事故机制：模板 / CSS / 前端 JS 里写 ``var(--tk-amber-ink, #b45309)`` 这类**带亮色
回落值**的引用，而该 token 从未在任何主题块里定义 → 变量恒走回落值：亮色恰好
正确、**暗色下 ≈3:1 直接不可读**，且不报错不变红，只能靠人眼在暗色下撞见
（``--tk-amber-ink`` 在接入弹窗风险横幅 / 断线横幅 / AI 草稿提示 / 账号卡 stall
提示等 9+ 处踩过，修复＝workspace_base 补别名指向 ``--tk-warn-ink``）。

门禁不变量：``src/web`` 下模板、静态 CSS 与静态 JS 引用的 ``--tk-*`` / ``--bdg-*``
语义 token（这两族按约定必须在主题块里亮暗各有效，见
``.cursor/rules/frontend-theme.mdc``）必须在扫描范围内**至少有一处定义**。

已知边界（刻意接受）：定义按全局并集判，不建模 Jinja extends 继承链——
「A 页定义、B 页引用但 B 不继承 A」这种错位抓不到；本门禁的目标是掐灭
「全站零定义、恒走回落」这一类（正是事故形态），零假阳性优先于覆盖。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set

SRC_WEB = Path(__file__).resolve().parents[1] / "src" / "web"

_REF_RE = re.compile(r"var\(\s*(--(?:tk|bdg)-[a-zA-Z0-9_-]+)")
_DEF_RE = re.compile(r"(--(?:tk|bdg)-[a-zA-Z0-9_-]+)\s*:")

#: 刻意允许「引用了但全站未定义」的 token → 原因（新增必须附原因；理想恒空）。
_ALLOWED_UNDEFINED: Dict[str, str] = {}


def _scan_files() -> List[Path]:
    files = list((SRC_WEB / "templates").rglob("*.html"))
    files += list((SRC_WEB / "static").rglob("*.css"))
    files += list((SRC_WEB / "static").rglob("*.js"))
    return files


def _collect() -> tuple[Dict[str, List[str]], Set[str]]:
    refs: Dict[str, List[str]] = {}
    defs: Set[str] = set()
    for fp in _scan_files():
        text = fp.read_text(encoding="utf-8", errors="ignore")
        for m in _REF_RE.finditer(text):
            refs.setdefault(m.group(1), []).append(fp.name)
        for m in _DEF_RE.finditer(text):
            defs.add(m.group(1))
    return refs, defs


def test_semantic_token_refs_all_defined():
    refs, defs = _collect()
    undefined = {
        name: sorted(set(where))[:4]
        for name, where in refs.items()
        if name not in defs and name not in _ALLOWED_UNDEFINED
    }
    assert not undefined, (
        "以下语义 token 被引用但全站零定义（暗色下恒走亮色回落值=不可读；"
        "请在 workspace_base / 页面主题块补定义或别名，勿直接加豁免）：\n"
        + "\n".join(f"  {k}  <-  {v}" for k, v in sorted(undefined.items()))
    )


def test_allowlist_not_stale():
    # 豁免表里的 token 一旦真的有了定义，必须把豁免拆掉（防豁免表变垃圾场）。
    _, defs = _collect()
    stale = [k for k in _ALLOWED_UNDEFINED if k in defs]
    assert not stale, f"豁免表过期（token 已有定义，请移除豁免）：{stale}"


def test_amber_ink_alias_pinned():
    # 本次事故的修复锚：--tk-amber-ink 必须保持定义为 --tk-warn-ink 的别名
    # （workspace_base 主题块）。谁删了别名，全站 9+ 处旧引用立刻回到暗色不可读。
    base = (SRC_WEB / "templates" / "workspace_base.html").read_text(encoding="utf-8")
    assert re.search(r"--tk-amber-ink\s*:\s*var\(\s*--tk-warn-ink\s*\)", base), (
        "workspace_base.html 缺少 --tk-amber-ink → --tk-warn-ink 兼容别名"
    )
