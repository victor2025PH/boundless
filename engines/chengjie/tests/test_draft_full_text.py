# -*- coding: utf-8 -*-
"""实施74 阶段3 门禁（B122）：草稿/填入提供整段模式（静态接线契约）。

0827 03:32 建议：生成回复分段填入对话框，坐席改稿时碍事 → dpick 弹窗新增
「整段填入」（`_dpickJoinWhole` 标点连排 + `draftPickFillWhole`）。
全自动 split_send 出站分段节奏**不动**（本批只加填入形态，不碰出站拆分）。
JS 纯函数行为由浏览器端承担，这里钉接线与词条契约（哑按钮/键解析另有全库门禁）。
"""
from __future__ import annotations

from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _tpl() -> str:
    return (_ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"
            ).read_text(encoding="utf-8")


def test_whole_fill_wired():
    src = _tpl()
    assert "function _dpickJoinWhole(" in src
    assert "function draftPickFillWhole(" in src
    assert 'onclick="draftPickFillWhole()"' in src
    assert "dpick.fill_whole" in src  # 埋点分桶（裁决可读）


def test_whole_fill_i18n_keys_bilingual():
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    for k in ("inbox.dpick.fill_whole", "inbox.dpick.fill_whole_t"):
        assert k in ZH and k in EN


def test_line_fill_semantics_untouched():
    """旧「填入输入框」（按行）语义保留——两个按钮并存，坐席按场景选。"""
    src = _tpl()
    assert 'onclick="draftPickFill()"' in src
    assert "parts.join('\\n')" in src


def test_autosend_split_send_untouched():
    """反向门禁：本批不碰出站拆分——reply_split 的对外契约函数仍在。"""
    src = (_ENGINE_ROOT / "src" / "inbox" / "reply_split.py").read_text(
        encoding="utf-8")
    assert "def split_reply_parts(" in src
