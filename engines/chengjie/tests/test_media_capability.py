"""入站多媒体能力就绪自检 + 一键预设纯函数单测（零 IO）。"""

from __future__ import annotations

from src.companion.media_capability import (
    MEDIA_CAPS, MEDIA_PRESETS, build_media_preset, collect_media_status,
    evaluate_media_cap, media_runtime_signals, preset_backend_warnings,
    asr_backend_ready, selfie_backend_ready, vision_backend_ready,
)


def _status(rep, key):
    return next(c for c in rep["capabilities"] if c["key"] == key)


# ── 后端探针 ─────────────────────────────────────────────────────────────

def test_vision_backend_ready():
    # Ollama/openai 兼容：需 provider ∈ openai_compatible/ollama/local + base_url(s)
    assert vision_backend_ready(
        {"vision": {"provider": "ollama", "base_urls": ["http://127.0.0.1:11434/v1"]}})
    assert vision_backend_ready(
        {"vision": {"provider": "openai_compatible", "base_url": "http://127.0.0.1:11434"}})
    # 智谱：需 api_key
    assert vision_backend_ready({"vision": {"provider": "zhipu", "api_key": "k"}})
    assert not vision_backend_ready({"vision": {"provider": "zhipu", "api_key": ""}})
    # 只有 base_urls 但 provider 仍是默认 zhipu → 不算就绪（与 has_any_vision_backend 同口径）
    assert not vision_backend_ready({"vision": {"base_urls": ["http://127.0.0.1:11434/v1"]}})
    assert not vision_backend_ready({})


def test_asr_backend_ready():
    # 本地 whisper 系 → 视为可用
    assert asr_backend_ready({"voice_recognition": {"provider": "faster_whisper"}})
    assert asr_backend_ready({})  # 缺省 faster_whisper
    # openai 需 key
    assert not asr_backend_ready(
        {"voice_recognition": {"provider": "openai", "openai": {"api_key": ""}}})
    assert asr_backend_ready(
        {"voice_recognition": {"provider": "openai", "openai": {"api_key": "sk"}}})


def test_selfie_backend_ready():
    assert not selfie_backend_ready({})
    assert not selfie_backend_ready(
        {"companion": {"selfie": {"provider": {"backend": "disabled"}}}})
    assert selfie_backend_ready(
        {"companion": {"selfie": {"provider": {"backend": "album", "album_dir": "x"}}}})
    assert selfie_backend_ready(
        {"companion": {"selfie": {"provider": {"backend": "openai", "api_key": "sk"}}}})
    assert selfie_backend_ready(
        {"companion": {"selfie": {"provider": {"backend": "command",
                                               "command_args": ["python", "x.py"]}}}})
    assert not selfie_backend_ready(
        {"companion": {"selfie": {"provider": {"backend": "command"}}}})


# ── 聚合状态 ─────────────────────────────────────────────────────────────

def test_empty_config_all_off():
    rep = collect_media_status({})
    assert rep["summary"]["total"] == len(MEDIA_CAPS)
    assert rep["summary"]["by_stage"]["off"] == len(MEDIA_CAPS)
    assert rep["summary"]["understand_active"] is False
    for c in rep["capabilities"]:
        assert c["stage"] == "off"


def test_enabled_but_no_backend_is_needs_backend():
    cfg = {"vision": {"enabled": True}}  # 开了识图但没配后端
    rep = collect_media_status(cfg)
    st = _status(rep, "vision_inbound")
    assert st["enabled"] is True
    assert st["backend_ready"] is False
    assert st["stage"] == "needs_backend"
    assert "未配识图后端" in st["hint"]


def test_enabled_with_backend_is_active():
    cfg = {"vision": {"enabled": True, "provider": "zhipu", "api_key": "k"},
           "voice_recognition": {"enabled": True, "provider": "faster_whisper"}}
    rep = collect_media_status(cfg)
    assert _status(rep, "vision_inbound")["stage"] == "active"
    assert _status(rep, "video_inbound")["stage"] == "active"   # 复用识图后端
    assert _status(rep, "asr_inbound")["stage"] == "active"
    # 入站理解三项都 active（selfie 不算入 understand）
    assert rep["summary"]["understand_active"] is True


# ── 一键预设 ─────────────────────────────────────────────────────────────

def test_presets_registry():
    assert set(MEDIA_PRESETS) == {"understand_all", "understand_and_selfie", "media_off"}
    assert build_media_preset("nope") is None


def test_understand_all_flags():
    spec = build_media_preset("understand_all")
    assert spec["flags"]["vision.enabled"] is True
    assert spec["flags"]["voice_recognition.enabled"] is True
    assert "companion.selfie.enabled" not in spec["flags"]


def test_media_off_flags():
    spec = build_media_preset("media_off")
    assert spec["flags"]["vision.enabled"] is False
    assert spec["flags"]["voice_recognition.enabled"] is False
    assert spec["flags"]["companion.selfie.enabled"] is False


def test_preset_backend_warnings_when_backend_missing():
    # understand_all 开识图+ASR，但无识图后端 → 该有一条 vision 告警
    warns = preset_backend_warnings("understand_all", {})
    assert any("识图后端" in w for w in warns)
    # media_off 全是关，无告警
    assert preset_backend_warnings("media_off", {}) == []


def test_preset_backend_warnings_none_when_ready():
    cfg = {"vision": {"provider": "zhipu", "api_key": "k"},
           "voice_recognition": {"provider": "faster_whisper"}}
    assert preset_backend_warnings("understand_all", cfg) == []


# ── 运行时质量信号 ───────────────────────────────────────────────────────

def test_runtime_signals_soft_and_shape():
    """无流量/无 stats 时返回 dict（可空），绝不抛；有段时结构正确。"""
    sig = media_runtime_signals()
    assert isinstance(sig, dict)
    for k in ("vision", "asr", "video"):
        if k in sig:
            assert "success_rate" in sig[k]


def test_collect_status_omits_runtime_when_empty(monkeypatch):
    """无真流量 → collect_media_status 不带 runtime 段（前端零流量不渲染健康行）。"""
    monkeypatch.setattr(
        "src.companion.media_capability.media_runtime_signals", lambda: {})
    rep = collect_media_status({})
    assert "runtime" not in rep


def test_collect_status_includes_runtime_when_present(monkeypatch):
    monkeypatch.setattr(
        "src.companion.media_capability.media_runtime_signals",
        lambda: {"vision": {"attempts": 10, "success_rate": 0.9,
                            "cache_hit_rate": 0.3, "fallbacks": 1}})
    rep = collect_media_status({"vision": {"enabled": True}})
    assert rep["runtime"]["vision"]["success_rate"] == 0.9


def test_runtime_signals_dedup_segment(monkeypatch):
    """有去重流量时 media_runtime_signals 带 dedup 段（image/video/fallback）。"""
    monkeypatch.setattr(
        "src.integrations.shared.media_dedup.dedup_metrics_snapshot",
        lambda: {"image_perturbed": 5, "video_perturbed": 2, "fallback": 1,
                 "fallback_reasons": {"no_ffmpeg": 1}, "total_perturbed": 7})
    sig = media_runtime_signals()
    assert sig.get("dedup", {}).get("image_perturbed") == 5
    assert sig["dedup"]["video_perturbed"] == 2
    assert sig["dedup"]["fallback"] == 1
