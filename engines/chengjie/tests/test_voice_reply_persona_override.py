"""``telegram.voice_reply.persona_overrides``：按人设覆写 A 线语音触发/补文字。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.client.sender import TelegramSenderMixin

_VR = {
    "enabled": True,
    "trigger": "when_peer_voice",
    "persona_overrides": {
        "wujie_sales": {"trigger": "always", "send_text_summary": True},
    },
}


def _sender(persona_ids=("lin_xiaoyu",)):
    s = TelegramSenderMixin.__new__(TelegramSenderMixin)
    s.account_id = "6775500026"
    s.account_persona_ids = list(persona_ids)
    s.logger = logging.getLogger("test_vr_override")
    return s


def _msg(chat_id="8125712527"):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id))


def _patch_pid(monkeypatch, pid, pack_vp=None):
    import src.ai.persona_voice as pv
    import src.utils.persona_manager as pm_mod
    monkeypatch.setattr(pv, "resolve_effective_persona_id",
                        lambda *a, **k: pid)
    personas = {pid: {"id": pid, "voice_profile": pack_vp}} if pack_vp else {}
    fake_pm = SimpleNamespace(get_persona_by_id=lambda p: personas.get(p))
    monkeypatch.setattr(pm_mod.PersonaManager, "get_instance",
                        classmethod(lambda cls: fake_pm))


def test_persona_pack_fields_apply_without_config(monkeypatch):
    _patch_pid(monkeypatch, "my_sales", {
        "reply_trigger": "always", "text_voice_split": True, "human_feel": "lively"})
    vr = {"enabled": True, "trigger": "when_peer_voice"}
    out = _sender()._voice_reply_persona_override({}, vr, _msg())
    assert out["trigger"] == "always"
    assert out["text_voice_split"] is True
    assert vr["trigger"] == "when_peer_voice"


def test_config_override_beats_persona_pack(monkeypatch):
    _patch_pid(monkeypatch, "wujie_sales", {"reply_trigger": "never", "text_voice_split": True})
    out = _sender()._voice_reply_persona_override({}, _VR, _msg())
    assert out["trigger"] == "always"
    assert out["text_voice_split"] is True


@pytest.mark.parametrize("pack_vp", [
    {"reply_trigger": "", "text_voice_split": None},
    {"reply_trigger": "sometimes", "text_voice_split": "yes"},
])
def test_persona_pack_inherit_or_invalid_is_noop(monkeypatch, pack_vp):
    _patch_pid(monkeypatch, "my_sales", pack_vp)
    vr = {"enabled": True, "trigger": "when_peer_voice"}
    assert _sender()._voice_reply_persona_override({}, vr, _msg()) is vr


def test_override_hits_effective_persona(monkeypatch):
    _patch_pid(monkeypatch, "wujie_sales")
    out = _sender()._voice_reply_persona_override({}, _VR, _msg())
    assert out["trigger"] == "always"
    assert out["send_text_summary"] is True
    assert out["enabled"] is True
    assert "persona_overrides" not in out
    assert _VR["trigger"] == "when_peer_voice"


def test_other_persona_keeps_global(monkeypatch):
    _patch_pid(monkeypatch, "lin_xiaoyu")
    out = _sender()._voice_reply_persona_override({}, _VR, _msg())
    assert out is _VR


def test_resolver_failure_falls_back_to_account_persona(monkeypatch):
    import src.ai.persona_voice as pv

    def _boom(*a, **k):
        raise RuntimeError("registry down")

    _patch_pid(monkeypatch, "")
    monkeypatch.setattr(pv, "resolve_effective_persona_id", _boom)
    out = _sender(("wujie_sales",))._voice_reply_persona_override({}, _VR, _msg())
    assert out["trigger"] == "always"


def test_split_text_voice_two_distinct_parts():
    sp = TelegramSenderMixin.split_text_voice(
        "哈哈被你发现啦，我是 AI 呀～基础版一个月 199，三个号起步。你平时一天回多少条消息？")
    assert sp is not None
    voice, text = sp
    assert voice == "哈哈被你发现啦，我是 AI 呀～"
    assert text == "基础版一个月 199，三个号起步。你平时一天回多少条消息？"
    assert voice not in text and text not in voice


def test_split_text_voice_english_keeps_spaces():
    voice, text = TelegramSenderMixin.split_text_voice(
        "Yes, I'm an AI. I found you in the group! Want to try me?")
    assert voice == "Yes, I'm an AI."
    assert text == "I found you in the group! Want to try me?"


@pytest.mark.parametrize("txt", ["", "就一句话没有句号", "好的。嗯。", "我是小界呀，智聊的 AI。"])
def test_split_text_voice_unsplittable(txt):
    assert TelegramSenderMixin.split_text_voice(txt) is None


@pytest.mark.parametrize("vr", [
    {"enabled": True, "trigger": "when_peer_voice"},
    {"enabled": True, "persona_overrides": {}},
    {"enabled": True, "persona_overrides": {"wujie_sales": "bad"}},
])
def test_absent_or_malformed_is_noop(monkeypatch, vr):
    _patch_pid(monkeypatch, "wujie_sales")
    assert _sender()._voice_reply_persona_override({}, vr, _msg()) is vr
