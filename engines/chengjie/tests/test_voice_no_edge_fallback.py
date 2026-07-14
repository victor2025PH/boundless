"""B 线 autosend no_edge_fallback：7852 不可达时拒发 edge，回落文字。"""
from __future__ import annotations

import pytest

from src.inbox import voice_autosend as va


def test_no_edge_fallback_enabled():
    assert va.no_edge_fallback_enabled({"no_edge_fallback": True})
    assert not va.no_edge_fallback_enabled({"no_edge_fallback": False})
    assert not va.no_edge_fallback_enabled({})


def test_preflight_skips_when_7852_down(monkeypatch):
    cfg = {"avatar_voice": {"enabled": True, "base_url": "http://127.0.0.1:7852"}}
    vb = {"no_edge_fallback": True}
    voice_cfg = {"backend": "avatar_clone", "voice_profile": {}}
    monkeypatch.setattr(va, "_peek_prerender_hit", lambda *a, **k: False)
    monkeypatch.setattr(va, "_avatar_clone_ready", lambda c: False)
    assert va.preflight_voice_synth(cfg, vb, "lin_xiaoyu", "你好", voice_cfg=voice_cfg) == "7852_unready"


def test_preflight_allows_prerender_when_7852_down(monkeypatch):
    cfg = {"avatar_voice": {"enabled": True}}
    vb = {"no_edge_fallback": True}
    voice_cfg = {"backend": "avatar_clone", "voice_profile": {}}
    monkeypatch.setattr(va, "_peek_prerender_hit", lambda *a, **k: True)
    monkeypatch.setattr(va, "_avatar_clone_ready", lambda c: False)
    assert va.preflight_voice_synth(cfg, vb, "lin_xiaoyu", "你好", voice_cfg=voice_cfg) is None


def test_reject_edge_fallback():
    meta = {"provider": "edge_tts", "fallback_from": "avatar_clone"}
    assert va._reject_edge_fallback(meta, True)
    assert not va._reject_edge_fallback(meta, False)
    assert not va._reject_edge_fallback(
        {"provider": "avatar_clone", "fallback_from": ""}, True)


class _EdgeResult:
    def __init__(self):
        self.ok = True
        self.audio_path = ""
        self.provider = "edge_tts"
        self.voice = "zh-CN-XiaoxiaoNeural"
        self.latency_ms = 50
        self.duration_sec = 1.2
        self.error = ""
        self.extra = {"fallback_from": "avatar_clone"}


class _EdgeTTS:
    def __init__(self, cfg):
        self.cfg = cfg
        assert cfg.get("fallback_on_error") is False

    async def synthesize(
        self, text, timeout_sec=45.0, emotion=None, pre_colloquialized=False, **_kw,
    ):
        r = _EdgeResult()
        r.audio_path = "/tmp/fake.ogg"
        return r

@pytest.mark.asyncio
async def test_synth_rejects_edge_when_no_edge_fallback(monkeypatch, tmp_path):
    import src.ai.persona_voice as pv

    fake_audio = tmp_path / "fake.ogg"
    fake_audio.write_bytes(b"OGG")

    monkeypatch.setattr(
        pv, "resolve_effective_voice_context",
        lambda *a, **k: {
            "voice_cfg": {
                "backend": "avatar_clone",
                "voice_profile": {"reference_audio_path": "x.wav"},
            },
            "emotion": None,
        },
    )
    monkeypatch.setattr(va, "preflight_voice_synth", lambda *a, **k: None)
    monkeypatch.setattr(va, "resolve_voice_autosend_cfg", lambda c: {"no_edge_fallback": True})
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _EdgeTTS)

    path, meta = await va._synth_ogg(
        {"inbox": {"l2_autosend": {"voice": {"no_edge_fallback": True}}}},
        "lin_xiaoyu", "测试一句", out_dir=str(tmp_path))
    assert path is None
    assert meta.get("edge_rejected")
    assert va.pop_synth_failure_reason() == "edge_rejected"


def test_pop_synth_failure_reason():
    va._set_synth_failure("7852_unready")
    assert va.pop_synth_failure_reason() == "7852_unready"
    assert va.pop_synth_failure_reason() == "synth_failed"
