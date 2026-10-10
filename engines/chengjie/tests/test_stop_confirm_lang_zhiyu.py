"""STOP 确认多语（智语 2026-10-08）。

默认关：确认句与今天的 farewell 一致（en/zh 文案不能被 snippets 换掉）。
显式打开 ``compliance.stop_gate.multilingual_confirm`` 后，只对 farewell
没有的 tl / ceb / hi 改用 snippets 的 persona 句。
"""
from __future__ import annotations

import inspect

from src.compliance import stop_gate as sg
from src.inbox.stop_contact import farewell_text

EN_FAREWELL = "Okay, I won't message you again."
ZH_FAREWELL = "好，我不会再打扰你了。"
EN_SNIPPET = "Okay, got it — I won't message you again."
TL_SNIPPET = "Sige po, noted — hindi na ako magme-message ulit."
CEB_SNIPPET = "Sige, nasabtan nako — dili na ko mo-message nimo pag-usab."
HI_SNIPPET = "ठीक है, नोट कर लिया — अब आपको दोबारा मैसेज नहीं आएगा।"

TL_TEXT = "Stop. Hindi na po ako magme-message."
CEB_TEXT = "Stop. Unsa man ni nimo, dili na."
HI_TEXT = "बंद करो, अब मैसेज मत करो"
EN_TEXT = "Please stop messaging me"
ZH_TEXT = "别再发消息了"


def _on(value=True):
    return {"compliance": {"stop_gate": {"multilingual_confirm": value}}}


def test_guess_lang_stays_script_only():
    assert sg.guess_lang(EN_TEXT) == "en"
    assert sg.guess_lang(TL_TEXT) == "en"
    assert sg.guess_lang(CEB_TEXT) == "en"
    assert sg.guess_lang(ZH_TEXT) == "zh"
    assert sg.guess_lang(HI_TEXT) == "hi"
    assert sg.confirm_text("hi") == EN_FAREWELL
    assert sg.confirm_text("tl") == EN_FAREWELL


def test_default_off_matches_farewell_path():
    assert sg.multilingual_confirm_enabled(None) is False
    assert sg.multilingual_confirm_enabled({}) is False
    for text in (EN_TEXT, TL_TEXT, CEB_TEXT, HI_TEXT, ZH_TEXT):
        assert sg.resolve_confirm_text(text) == sg.confirm_text(sg.guess_lang(text))
    assert sg.resolve_confirm_text(EN_TEXT) == EN_FAREWELL
    assert sg.resolve_confirm_text(ZH_TEXT) == ZH_FAREWELL
    assert sg.resolve_confirm_text(TL_TEXT) == EN_FAREWELL
    assert sg.resolve_confirm_text(HI_TEXT) == farewell_text("hi") == EN_FAREWELL


def test_flag_values():
    for off in (False, "false", "off", "legacy", "0", "no", "", 0, None):
        assert sg.multilingual_confirm_enabled(_on(off)) is False
    for on in (True, "true", "on", "1", "yes", "enforce", "enabled", "ON", 1):
        assert sg.multilingual_confirm_enabled(_on(on)) is True


def test_flag_on_keeps_en_zh_farewell_and_fills_tl_ceb_hi():
    cfg = _on(True)
    assert sg.resolve_confirm_text(EN_TEXT, cfg) == EN_FAREWELL
    assert sg.resolve_confirm_text(EN_TEXT, cfg) != EN_SNIPPET
    assert sg.resolve_confirm_text(ZH_TEXT, cfg) == ZH_FAREWELL
    assert "记下了" not in sg.resolve_confirm_text(ZH_TEXT, cfg)
    assert sg.confirm_lang(TL_TEXT) == "tl"
    assert sg.resolve_confirm_text(TL_TEXT, cfg) == TL_SNIPPET
    assert sg.confirm_lang(CEB_TEXT) == "ceb"
    assert sg.resolve_confirm_text(CEB_TEXT, cfg) == CEB_SNIPPET
    assert sg.confirm_lang(HI_TEXT) == "hi"
    assert sg.resolve_confirm_text(HI_TEXT, cfg) == HI_SNIPPET
    # Taglish 没有单独 variant，跟 tl 走同一句 persona，不用 brand 口吻
    taglish = "Stop na, di na po ako magme-message"
    assert sg.confirm_lang(taglish) == "tl"
    assert sg.resolve_confirm_text(taglish, cfg) == TL_SNIPPET
    assert "kami" not in sg.resolve_confirm_text(taglish, cfg)


def test_missing_snippet_file_falls_back_to_farewell(monkeypatch, tmp_path):
    monkeypatch.setattr(sg, "_SNIPPET_PATH", tmp_path / "missing.yaml")
    cfg = _on("enforce")
    assert sg.resolve_confirm_text(TL_TEXT, cfg) == EN_FAREWELL
    assert sg.resolve_confirm_text(EN_TEXT, cfg) == EN_FAREWELL


def test_example_documents_confirm_off_and_empty_vertical_pack():
    from pathlib import Path
    import yaml
    text = (Path(__file__).resolve().parents[1] / "config" / "config.example.yaml").read_text(encoding="utf-8")
    assert "multilingual_confirm: false" in text
    data = yaml.safe_load(text)
    assert data["compliance"]["stop_gate"]["multilingual_confirm"] is False
    assert data["compliance"]["stop_gate"]["enabled"] is True
    assert data["vertical_pack"]["id"] == ""
    assert data["vertical_pack"]["allow_internal"] is False


def test_replybus_calls_resolve_confirm_text():
    from src.web.routes import replybus_routes
    src = inspect.getsource(replybus_routes._stop_gate_decision)
    assert "resolve_confirm_text(text, config)" in src
    assert "confirm_text(sg.guess_lang" not in src
