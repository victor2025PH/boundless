"""人设 ``voice_profile.human_feel`` 档位 → ``avatar_voice.colloquial`` 叠加。"""
from __future__ import annotations

from src.ai.persona_voice import apply_persona_human_feel, resolve_voice_cfg


def _cfg(human_feel=None, colloquial=None):
    vp = {"enabled": True, "backend": "avatar_clone", "owner_consent": True,
          "reference_audio_path": "config/voice_refs/x.wav"}
    if human_feel is not None:
        vp["human_feel"] = human_feel
    av = {"base_url": "http://127.0.0.1:7865"}
    if colloquial is not None:
        av["colloquial"] = colloquial
    return {
        "avatar_voice": av,
        "personas": {"profiles": [{"id": "p1", "voice_profile": vp}]},
    }


def test_lively_enables_colloquial_when_global_off():
    full = _cfg("lively", {"enabled": False, "mode": "llm", "provider": "local"})
    col = resolve_voice_cfg("p1", full)["avatar_voice"]["colloquial"]
    assert col["enabled"] is True
    assert col["rewrite_intensity"] == "vivid"
    assert col["human_ticks"] is True and col["disfluency"] is True
    assert col["mode"] == "llm" and col["provider"] == "local"
    assert full["avatar_voice"]["colloquial"]["enabled"] is False


def test_lively_without_global_section():
    col = resolve_voice_cfg("p1", _cfg("lively"))["avatar_voice"]["colloquial"]
    assert col["enabled"] is True and col["think_prob"] == 0.28


def test_off_disables_for_this_persona_only():
    full = _cfg("off", {"enabled": True, "rewrite_intensity": "vivid"})
    assert resolve_voice_cfg("p1", full)["avatar_voice"]["colloquial"]["enabled"] is False
    assert resolve_voice_cfg(None, full)["avatar_voice"]["colloquial"]["enabled"] is True


def test_unset_or_unknown_keeps_global():
    for hf in (None, "", "loud"):
        full = _cfg(hf, {"enabled": False})
        assert resolve_voice_cfg("p1", full)["avatar_voice"]["colloquial"] == {"enabled": False}


def test_apply_noop_without_sections():
    assert apply_persona_human_feel({}) is False
    assert apply_persona_human_feel({"voice_profile": {"human_feel": "lively"}}) is False
