# -*- coding: utf-8 -*-
"""R87 P2-1：已知技术码不得作为界面可见回落文案直出。

不扫映射表键名（``dup_guard_blocked:'inbox.handoff.r_…'`` 是对的），只红：
把原码当 ``|| 'dup_guard_blocked'`` / ``textContent = …dup_guard_blocked`` 一类回落。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates"

# 曾经穿帮、且已有人话词条的码。新露一条就往这里加。
_CODES = (
    "dup_guard_blocked",
    "dormant:ignored",
    "translate_hold",
)


def _templates():
    return sorted(_TPL.glob("*.html"))


def test_known_codes_have_human_keys():
    from src.web.i18n_packs import collect_all
    zh, en, extras = collect_all()
    for k in (
        "inbox.handoff.r_dup_guard_blocked",
        "inbox.systag.dormant",
        "inbox.cs.xlate_hold",
        "rps_al_sub_dup_guard_blocked",
    ):
        assert k in zh and k in en, k
        assert not re.search(r"dup_guard_blocked|dormant:ignored", zh[k])


def test_no_raw_code_string_fallback_in_templates():
    """界面回落不得写成 || 'dup_guard_blocked' 这类原码。映射表键名除外。"""
    bad = []
    # 回落字面量：|| 'code' 或 || "code"
    lit = re.compile(
        r"""\|\|\s*['\"](?:dup_guard_blocked|dormant:ignored|translate_hold(?::\w+)?)['\"]""")
    # textContent / innerText 直接拼原码（映射函数调用除外）
    assign = re.compile(
        r"""(?:textContent|innerText)\s*=\s*[^;]{0,80}(?:dup_guard_blocked|dormant:ignored)""")
    for p in _templates():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if ":'inbox." in line or ":'rps_" in line:
                continue  # 映射表
            if lit.search(line) or assign.search(line):
                bad.append(f"{p.name}:{i}:{line.strip()[:120]}")
    assert not bad, "技术码当可见回落：\n" + "\n".join(bad)


def test_handoff_and_rps_maps_cover_dup_guard():
    inbox = (_TPL / "unified_inbox.html").read_text(encoding="utf-8", errors="ignore")
    rps = (_TPL / "reply_settings.html").read_text(encoding="utf-8", errors="ignore")
    assert "dup_guard_blocked:'inbox.handoff.r_dup_guard_blocked'" in inbox
    assert "rpsAlSubText" in rps and "rps_al_sub_dup_guard_blocked" in rps
