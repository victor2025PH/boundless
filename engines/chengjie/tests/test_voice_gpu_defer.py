"""P2/P5：发图 in-flight defer 语音 + 7852 后台 nudge。"""
from __future__ import annotations

import pytest

from src.inbox import image_autosend as ia
from src.inbox import voice_autosend as va


def test_image_gen_inflight_counter():
    assert ia.image_gen_inflight() == 0
    ia._image_gen_begin()
    ia._image_gen_begin()
    assert ia.image_gen_inflight() == 2
    ia._image_gen_end()
    assert ia.image_gen_inflight() == 1
    ia._image_gen_end()
    assert ia.image_gen_inflight() == 0
    ia._image_gen_end()
    assert ia.image_gen_inflight() == 0


def test_defer_during_image_default_on():
    assert va.defer_during_image_enabled({})
    assert va.defer_during_image_enabled({"defer_during_image": True})
    assert not va.defer_during_image_enabled({"defer_during_image": False})


def test_preflight_nudges_boot_when_7852_down(monkeypatch):
    nudged = []

    def _fake_nudge(cfg):
        nudged.append(cfg)

    monkeypatch.setattr(va, "_nudge_7852_boot", _fake_nudge)
    monkeypatch.setattr(va, "_peek_prerender_hit", lambda *a, **k: False)
    monkeypatch.setattr(va, "_avatar_clone_ready", lambda c: False)
    cfg = {"avatar_voice": {"enabled": True}}
    vb = {"no_edge_fallback": True}
    voice_cfg = {"backend": "avatar_clone", "voice_profile": {}}
    reason = va.preflight_voice_synth(cfg, vb, "p1", "你好", voice_cfg=voice_cfg)
    assert reason == "7852_unready"
    assert len(nudged) == 1


def test_nudge_emotion_tts_boot_skips_when_healthy(monkeypatch):
    from src.ai import avatar_voice as av

    av.reset_caches()
    called = []

    class _FakeClient:
        boot_task_7852 = "EmotionTTS_Boot"

        def health_ok(self, *, use_cache=True):
            return True

        def _trigger_boot_task(self, name):
            called.append(name)

    monkeypatch.setattr(av, "AvatarVoiceClient", lambda cfg: _FakeClient())
    av.nudge_emotion_tts_boot({"avatar_voice": {"enabled": True}})
    import time
    time.sleep(0.05)
    assert called == []


def test_nudge_emotion_tts_boot_triggers_when_down(monkeypatch):
    from src.ai import avatar_voice as av

    av.reset_caches()
    called = []

    class _FakeClient:
        boot_task_7852 = "EmotionTTS_Boot"

        def health_ok(self, *, use_cache=True):
            return False

        def _trigger_boot_task(self, name):
            called.append(name)

    monkeypatch.setattr(av, "AvatarVoiceClient", lambda cfg: _FakeClient())
    av.nudge_emotion_tts_boot({"avatar_voice": {"enabled": True}})
    import time
    time.sleep(0.15)
    assert called == ["EmotionTTS_Boot"]
