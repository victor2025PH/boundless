# -*- coding: utf-8 -*-
"""Q-30 C 门禁（#307，2026-09-12）：``ABORT_REASONS`` ∪ 台账码 ∪ ``conv_state`` 会吐出的 reason_code
∪ 「需人工」原因码——每个码要么有人话键（zh/en/繁体三份），要么明确走回落分支（原码 / 通用句）。

事故形态：红条直接晒 ``media_lie_caught_repeat``（#307）、状态带绿着而拟稿其实被同事名单拦了
（#312 #314）、W2CSGA 首回延迟全程不可见（#311）——码有、人话无。本文件让「新码没配人话」在
CI 先红。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.inbox import abort_ledger, conv_state
from src.inbox.autosend_worker import ABORT_REASONS
from src.web.web_i18n import get_translations

ROOT = Path(__file__).resolve().parents[1]
_HTML = (ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
_LANGS = ("zh", "en", "zh_hant")


def _has(key: str) -> bool:
    return all(bool(get_translations(lg).get(key)) for lg in _LANGS)


# ── 拦截台账：ABORT_REASONS 每个码归一后都在台账表且有 rps_al_r_<code> 人话 ──────────

def test_abort_reasons_all_land_in_ledger_table_with_human_text():
    for code in ABORT_REASONS:
        norm = abort_ledger.normalize_code(code)
        assert norm in abort_ledger.REASONS, (code, norm)
    for code in abort_ledger.REASONS:
        assert _has(f"rps_al_r_{code}"), f"台账码 {code} 缺人话键 rps_al_r_{code}"


# ── 状态带：conv_state 会吐出的每个 reason_text_key 都有键（含 Q-30 新码）─────────────

def _cs_text_keys_from_source() -> set:
    src = (ROOT / "src" / "inbox" / "conv_state.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'"(inbox\.cs\.[a-z_.]+)"', src))
    # f-string 形态 f"inbox.cs.sidecar_down.{kind}" → 四种 kind
    for kind in ("pin", "send_fail", "stalled", "offline"):
        keys.add(f"inbox.cs.sidecar_down.{kind}")
    # 缺省 text_key = f"inbox.cs.{state}"（sidecar_down 恒带 kind 后缀，无裸键）
    keys |= {f"inbox.cs.{s}" for s in conv_state.STATES if s != "sidecar_down"}
    # peer_guard 源的文案键来自 marker 的前缀表（conv_state 只透传）
    from src.inbox import peer_guard_marker as pg
    keys |= set(pg.TEXT_KEYS.values()) | {pg.GENERIC_TEXT_KEY}
    return keys


def test_conv_state_text_keys_exist_in_all_langs():
    keys = _cs_text_keys_from_source()
    assert {"inbox.cs.deferred.first_reply", "inbox.cs.deferred.first_contact",
            "inbox.cs.held.colleague", "inbox.cs.held.ops_group", "inbox.cs.held.peer_guard"} <= keys
    missing = sorted(k for k in keys if not _has(k))
    assert not missing, f"状态带文案键缺失（zh/en/繁体任一）：{missing}"
    # 悬浮说明（_t）也齐
    for k in ("inbox.cs.deferred.first_reply", "inbox.cs.deferred.first_contact",
              "inbox.cs.held.colleague", "inbox.cs.held.ops_group", "inbox.cs.held.peer_guard"):
        assert _has(k + "_t"), k + "_t"


def test_peer_guard_text_key_map_covers_never_auto_prefixes():
    from src.inbox import peer_guard_marker as pg
    for head in ("colleague", "ops_group"):
        assert pg.TEXT_KEYS[head].startswith("inbox.cs.held.") and _has(pg.TEXT_KEYS[head])
    assert _has(pg.GENERIC_TEXT_KEY)
    # peer_bot_guard 的两个名单码前缀与 marker 的表同源（改一边必改另一边）
    guard = (ROOT / "src" / "inbox" / "peer_bot_guard.py").read_text(encoding="utf-8")
    assert 'return f"ops_group:{ck}"' in guard and 'return f"colleague:{cand}"' in guard


# ── 「需人工」原因码：_HANDOFF_REASON_KEYS 每个键存在 + media_lie_caught_repeat 在表 ───────

def _handoff_reason_map() -> dict:
    m = re.search(r"var _HANDOFF_REASON_KEYS=\{(.*?)\};", _HTML, re.S)
    assert m, "_HANDOFF_REASON_KEYS 字面量不在"
    pairs = dict(re.findall(r"([a-z_]+):'([^']+)'", m.group(1)))
    for code, key in re.findall(r"_HANDOFF_REASON_KEYS\.([a-z_]+)='([^']+)';", _HTML):
        pairs[code] = key
    return pairs


def test_handoff_reason_keys_resolve_and_include_media_lie_caught_repeat():
    m = _handoff_reason_map()
    assert m.get("media_lie_caught_repeat") == "inbox.handoff.r_media_lie_caught_repeat"
    for code, key in m.items():
        assert _has(key), f"{code} → {key} 缺人话"
    zh = get_translations("zh")["inbox.handoff.r_media_lie_caught_repeat"]
    assert "没收到图" in zh and "2" in zh


def test_media_lie_caught_repeat_is_a_real_emitted_code():
    """码来自 autosend_helpers / persona_reply 两处 _tag_needs_human（同一字符串）。"""
    for rel in ("src/inbox/autosend_helpers.py", "src/inbox/persona_reply.py"):
        assert 'reason="media_lie_caught_repeat"' in (ROOT / rel).read_text(encoding="utf-8"), rel


# ── 回落分支：未知码不得晒空 ───────────────────────────────────────────────────

def test_frontend_fallbacks_for_unknown_codes_are_in_place():
    # 需人工 tip / 状态带 held：查表不中 → 原码 → 通用句（回落链三段都在）
    i = _HTML.index("function _csHeldWhy(p){")
    body = _HTML[i:i + 900]
    assert "_HANDOFF_REASON_KEYS[reason]" in body and "inbox.handoff.r_crisis" in body
    assert "return reason||window.T('inbox.handoff.bar_generic')" in body
    assert _has("inbox.handoff.bar_generic")
    # L1 原因码：查表不中 → inbox.l1r.other（带原码）
    j = _HTML.index("function _l1ReasonText(d){")
    assert "inbox.l1r.other" in _HTML[j:j + 400] and _has("inbox.l1r.other")


def test_l1_reason_codes_in_autodraft_have_human_text_or_fallback():
    """autodraft 会登记的 L1 原因码：有键的走键，其余靠 inbox.l1r.other 兜（这里只要求
    Q-30 引入的 peer_budget 与既有 global_default/account_default 有键）。"""
    for code in ("peer_budget", "global_default", "account_default", "manual_review", "risk_hold"):
        key = f"inbox.l1r.{code}"
        if code == "risk_hold":
            # 既有码走回落（inbox.l1r.other）——如实登记，不假装它有键
            assert not get_translations("zh").get(key) or _has(key)
            continue
        assert _has(key), key
