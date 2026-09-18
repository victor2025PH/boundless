# -*- coding: utf-8 -*-
"""R87 P1-3（X9B22T 15:09–15:50）：「Sure, give me a second, I'll take one now」×4 + 「I didn't forget, I just
didn't send it yet」——承诺发图未兑现一轮一轮复发；15:30 出站预览里直出 ``[PHOTO selfie cozy bedroom…`` 原码。

钉三层：
A. image_send_gate：``note_promise_retracted`` 记账 / ``promise_streak`` 30 分钟窗 / ``promise_streak_hint``
   ≥2 次才给硬提示；窗过归零。
B. 接线：autosend_helpers 撤回改写处记账；skill_manager 拟稿前把 hint 并进 ``_media_coherence_hint``。
C. drafts_routes：列表带 ``draft_text_display``（剥净）+ ``photo_directive``，``draft_text`` 原样；模板预览
   用 display 字段 + 小标签词条三语。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from src.inbox import image_send_gate as g

_ROOT = Path(__file__).resolve().parents[1]
CK = "whatsapp:12084403608:917983336714"


@pytest.fixture(autouse=True)
def _reset():
    g.reset_promise_streak_for_tests()
    yield
    g.reset_promise_streak_for_tests()


# ── A ────────────────────────────────────────────────────────────────────────

def test_streak_counts_within_window_and_expires():
    t0 = time.time()
    assert g.promise_streak(CK, now=t0) == 0 and g.promise_streak_hint(CK, now=t0) == ""
    assert g.note_promise_retracted(CK, now=t0) == 1
    assert g.promise_streak_hint(CK, now=t0 + 1) == ""          # 一次不提示（正常撤回改写）
    assert g.note_promise_retracted(CK, now=t0 + 60) == 2
    hint = g.promise_streak_hint(CK, now=t0 + 61)
    assert hint and "2 次" in hint and "绝不" in hint
    # 别的会话不受影响
    assert g.promise_streak("other:1:2", now=t0 + 61) == 0
    # 30 分钟窗过 → 归零
    assert g.promise_streak(CK, now=t0 + g.PROMISE_STREAK_WINDOW_SEC + 120) == 0
    assert g.promise_streak_hint(CK, now=t0 + g.PROMISE_STREAK_WINDOW_SEC + 120) == ""


def test_streak_pauses_follow_not_explicit_ask():
    t0 = time.time()
    g.note_promise_retracted(CK, now=t0)
    g.note_promise_retracted(CK, now=t0 + 10)
    assert g.promise_streak_blocks_follow(CK, g.TRIGGER_COMMITMENT, now=t0 + 11)
    assert g.promise_streak_blocks_follow(CK, g.TRIGGER_DIRECTIVE, now=t0 + 11)
    assert not g.promise_streak_blocks_follow(CK, g.TRIGGER_ASK, now=t0 + 11)
    assert not g.promise_streak_blocks_follow(CK, g.TRIGGER_KEYWORD, now=t0 + 11)


def test_streak_survives_reload_from_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    g.reset_promise_streak_for_tests()
    t0 = time.time()
    assert g.note_promise_retracted(CK, now=t0) == 1
    assert g.note_promise_retracted(CK, now=t0 + 8) == 2
    led = tmp_path / "logs" / "promise_streak_ledger.json"
    assert led.is_file()
    with g._PROMISE_LOCK:
        g._PROMISE_RETRACTS.clear()
        g._PROMISE_LOADED = False
    assert g.promise_streak(CK, now=t0 + 9) == 2
    assert g.promise_streak_blocks_follow(CK, g.TRIGGER_COMMITMENT, now=t0 + 9)


# ── B ────────────────────────────────────────────────────────────────────────

def test_wiring_helpers_and_skill_manager():
    h = (_ROOT / "src" / "inbox" / "autosend_helpers.py").read_text(encoding="utf-8", errors="ignore")
    assert "note_promise_retracted as _npr" in h and "streak=%d" in h
    i_retract = h.index('(_rsce if _sent_claim else _rpe)("retracted")')
    i_note = h.index("note_promise_retracted as _npr")
    assert i_retract < i_note < h.index('if _pact == "review" or not str(_new_text or "").strip():')
    s = (_ROOT / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8", errors="ignore")
    assert "promise_streak_hint as _psh" in s
    ia = (_ROOT / "src" / "inbox" / "image_autosend.py").read_text(encoding="utf-8", errors="ignore")
    assert "promise_streak_blocks_follow" in ia and "REASON_PROMISE_STREAK" in ia
    assert 'user_context["_media_coherence_hint"] = (\n                        (str(user_context.get("_media_coherence_hint") or "") + "\\n" + _ps_hint).strip())' in s


# ── C ────────────────────────────────────────────────────────────────────────

def test_drafts_list_display_field_wired_and_template_uses_it():
    r = (_ROOT / "src" / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8", errors="ignore")
    assert 'd["draft_text_display"] = _clean' in r and 'd["photo_directive"] = {' in r
    assert "extract_photo_directive as _epd" in r
    html = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8", errors="ignore")
    assert "dr.draft_text_display||dr.draft_text||dr.text" in html
    assert "d.draft_text_display||d.draft_text||d.text" in html
    assert "inbox.draft_mini.photo_directive" in html
    # 编辑仍送原文（含标记，投递链照常解析）
    assert "useDraftText('${esc(dr.draft_text||dr.text||'')}')" in html


def test_photo_directive_strip_roundtrip():
    from src.ai.photo_directive import extract_photo_directive
    clean, pd = extract_photo_directive("Goodnight, Coop.\n[PHOTO selfie cozy bedroom, warm lamp light]")
    assert pd and pd.get("kind") == "selfie" and "[PHOTO" not in clean and "Goodnight" in clean


def test_promise_streak_pack_three_langs():
    from src.web.i18n_packs import promise_streak_r87 as P
    assert set(P.ZH) == set(P.EN) == set(P.ZH_HANT) == {"inbox.draft_mini.photo_directive"}
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    k = "inbox.draft_mini.photo_directive"
    assert ph(P.ZH[k]) == ph(P.EN[k]) == ph(P.ZH_HANT[k]) == {"kind"}
    from src.web.i18n_packs import collect_all
    zh, en, extras = collect_all()
    assert zh[k] == P.ZH[k]
