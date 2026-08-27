# -*- coding: utf-8 -*-
"""危机审计「安全体检」空态门禁（2026-08-18 P1）。

生产常态是 0 危机事件（zhiliao 实测 0 行），旧空态是一行灰字「暂无数据」——
看起来像页面坏了。改造后的契约：
1. 无筛选的空列表 → 渲染好消息空态（🛡️ + ca_js010/012），并在「仅未处理」
   勾选时给「查看已处理历史」按钮（ca_js011 → caShowAll）；
2. 带 user 前缀筛选的空列表 → 仍是普通「无匹配」（空态不冒充搜索结果）；
3. caShowAll 是 innerHTML 拼出的 onclick 落点 → 必须顶层函数（全局可达）；
4. 三键 zh/en 双语齐备（pack crisis_audit_page）。
"""
from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"


def _src() -> str:
    return (_TPL / "crisis_audit.html").read_text(encoding="utf-8")


def test_empty_state_branches_on_filter():
    src = _src()
    assert "if (!prefix)" in src, "空态必须区分「无筛选」与「筛选无匹配」"
    for key in ("ca_js010", "ca_js011", "ca_js012"):
        assert f"window.T('{key}')" in src, f"空态缺 {key} 词条消费"
    # 无匹配分支仍走通用词条
    assert "em_js001" in src


def test_show_all_button_is_global_function():
    src = _src()
    assert "function caShowAll()" in src, "caShowAll 必须是顶层函数声明"
    assert "caShowAll()" in src and "ca_js011" in src
    # 按钮语义＝取消「仅未处理」后重载
    assert "ca-only-unhandled" in src


def test_empty_state_keys_bilingual():
    from src.web.i18n_packs.crisis_audit_page import EN, ZH

    for key in ("ca_js010", "ca_js011", "ca_js012"):
        assert ZH.get(key) and EN.get(key), f"{key} 缺双语"
    # 好消息语气钉住：空态标题必须把「没有事件」讲成正面信号
    assert "好消息" in ZH["ca_js010"]
    assert "good news" in EN["ca_js010"].lower()
