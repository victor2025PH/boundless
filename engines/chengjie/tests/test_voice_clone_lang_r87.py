# -*- coding: utf-8 -*-
"""R87 P2-2（S5NVGQ）：日语会话每条 clone_lang_unsupported:ja 把看门狗打成「语音出站断档 3/3」。

更好口径：语种能力缺口 ≠ 断档。钉四层：
A. voice_outage.is_capability_skip + record 跳过 clone_lang_unsupported / garbled。
B. log_skip_voice 同会话同原因第一次 WARNING、第二次 DEBUG；写 peek_clone_lang_skip。
C. conv_state.notes 带 voice_clone_lang，不改 state。
D. 模板 _csRenderNotes 认 voice_clone_lang；词条三语齐平。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace

from src.ai import voice_outage as vo
from src.ai.tts_pipeline import (
    log_skip_voice, note_clone_lang_skip, peek_clone_lang_skip,
    reset_clone_lang_skip_for_tests,
)
from src.inbox.conv_state import compute

_ROOT = Path(__file__).resolve().parents[1]
CID = "whatsapp:acc:66803566865"


def setup_function():
    reset_clone_lang_skip_for_tests()
    vo.reset_for_test()


def teardown_function():
    reset_clone_lang_skip_for_tests()
    vo.reset_for_test()


def _rv(detail="clone_lang_unsupported:ja", lang="ja"):
    return SimpleNamespace(
        extra={"clone_unavailable": detail, "clone_lang_blocked": lang,
               "system_voice": "ja-JP-KeitaNeural"})


# ── A ────────────────────────────────────────────────────────────────────────

def test_capability_skip_not_counted_as_outage():
    assert vo.is_capability_skip("clone_unavailable:clone_lang_unsupported:ja")
    assert vo.is_capability_skip("clone_lang_garbled:th")
    assert not vo.is_capability_skip("synth_failed")
    led = vo.VoiceOutageLedger()
    led.record_voice_attempt(False, "autosend", "clone_unavailable:clone_lang_unsupported:ja")
    led.record_voice_attempt(False, "autosend", "clone_lang_garbled:ja")
    led.record_voice_attempt(False, "autosend", "synth_failed")
    snap = led.outage_snapshot()
    assert snap["attempts_24h"] == 1
    assert snap["fail_reasons"] == {"synth_failed": 1}


# ── B ────────────────────────────────────────────────────────────────────────

def test_skip_voice_warns_once_then_debug(caplog):
    rv = _rv()
    with caplog.at_level(logging.DEBUG):
        assert log_skip_voice(rv, conv=CID) == "clone_unavailable"
        assert log_skip_voice(rv, conv=CID) == "clone_unavailable"
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING and "skip_voice" in r.getMessage()]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "skip_voice" in r.getMessage()]
    assert len(warns) == 1 and len(debugs) == 1
    rec = peek_clone_lang_skip(CID)
    assert rec and rec["lang"] == "ja" and rec["n"] == 2


def test_note_expires_and_ignores_empty_conv():
    t0 = time.time()
    assert note_clone_lang_skip("", "ja") is None
    note_clone_lang_skip(CID, "ja", now=t0)
    assert peek_clone_lang_skip(CID, now=t0 + 10)["lang"] == "ja"
    assert peek_clone_lang_skip(CID, now=t0 + 25 * 3600) is None


# ── C ────────────────────────────────────────────────────────────────────────

class _Store:
    def get_conversation(self, cid):
        return {"conversation_id": cid, "platform": "whatsapp"}

    def get_automation_mode(self, cid):
        return "auto_ai"

    def get_automation_mode_if_set(self, cid):
        return "auto_ai"

    def get_automation_mode_meta(self, cid):
        return {"mode": "auto_ai", "source": "human", "updated_at": 0.0}

    def get_app_setting(self, key, default=""):
        return default


def test_conv_state_note_does_not_change_state():
    note_clone_lang_skip(CID, "ja")
    out = compute(_Store(), CID, platform="whatsapp", account_id="acc")
    kinds = [n.get("kind") for n in (out.get("notes") or [])]
    assert "voice_clone_lang" in kinds
    n = next(x for x in out["notes"] if x["kind"] == "voice_clone_lang")
    assert n["lang"] == "ja" and n["text_key"] == "inbox.cs.note.voice_clone_lang"
    assert out["state"] != "held"


# ── D ────────────────────────────────────────────────────────────────────────

def test_template_and_pack():
    html = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(
        encoding="utf-8", errors="ignore")
    fn = html[html.index("function _csRenderNotes("):html.index("window._csNoteAction=")]
    assert "voice_clone_lang" in fn and "inbox.cs.note.voice_clone_lang" in fn
    assert "'album_no_match'" in fn
    from src.web.i18n_packs import voice_clone_lang_r87 as P
    assert set(P.ZH) == set(P.EN) == set(P.ZH_HANT)
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for k in P.ZH:
        assert ph(P.ZH[k]) == ph(P.EN[k]) == ph(P.ZH_HANT[k]), k
    from src.web.i18n_packs import collect_all
    zh, en, extras = collect_all()
    assert zh["inbox.cs.note.voice_clone_lang"] == P.ZH["inbox.cs.note.voice_clone_lang"]
