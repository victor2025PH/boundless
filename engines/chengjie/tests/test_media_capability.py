"""入站多媒体能力就绪自检 + 一键预设纯函数单测（零 IO）。"""

from __future__ import annotations

from src.companion.media_capability import (
    MEDIA_CAPS, MEDIA_PRESETS, build_media_preset, collect_media_status,
    evaluate_media_cap, media_runtime_signals, preset_backend_warnings,
    asr_backend_ready, selfie_backend_ready, vision_backend_ready,
    _backend_hint,
)


# ── 托管版提示去黑话（2026-07-29）：不向用户暴露 yaml 键/密钥 ──────────────

def test_backend_hint_selfhost_keeps_actionable_keys():
    h = _backend_hint("vision", managed=False)
    assert "vision.base_url" in h  # 自建版保留可操作键名

def test_backend_hint_managed_hides_yaml_jargon():
    for be in ("vision", "asr", "selfie"):
        h = _backend_hint(be, managed=True)
        assert "base_url" not in h and "api_key" not in h and "." not in h.split("：")[0]

def test_backend_hint_managed_is_honest_and_actionable():
    """托管版文案不得再一律「由服务端统一提供，如未生效请联系客服」（2026-07-31）。

    那句话在两种最常见情形下都是错的：识图多半只是**供给没发生**（令牌后到 / 热重载
    抹掉），点一下重试就好、不该开工单；而语音识别与出图**压根没随安装包**
    （backend.spec 排除 whisper/torch 系；出图属交付分级 C 类），说「服务端统一提供」
    是承诺一个不存在的东西。
    """
    assert "重试接入" in _backend_hint("vision", managed=True)
    assert "重试接入" not in _backend_hint("vision", managed=True, reason="no_token")
    for be in ("asr", "selfie"):
        h = _backend_hint(be, managed=True)
        assert "服务端统一提供" not in h
        assert "未" in h  # 如实说「未接入 / 未包含」

def test_backend_hint_managed_autodetect_env(monkeypatch):
    monkeypatch.setenv("AITR_MANAGED_EDITION", "1")
    assert "vision.base_url" not in _backend_hint("vision")  # 托管版不甩 yaml 键
    monkeypatch.setenv("AITR_MANAGED_EDITION", "0")
    assert "vision.base_url" in _backend_hint("vision")


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


def _asr_module(monkeypatch, installed: bool):
    """本地 ASR 包装没装（安装包里没装、开发机上装了）——探针结果不能看运行环境的脸色。"""
    monkeypatch.setattr(
        "src.companion.media_capability._module_installed", lambda _n: installed)


def test_asr_backend_ready_requires_real_local_module(monkeypatch):
    """修假绿（2026-07-31）：本地 provider 不再无条件算就绪。

    旧实现对任何非 openai provider 直接 return True。而安装包的 PyInstaller 配置
    显式 excludes 掉 whisper/faster_whisper/ctranslate2/torch —— 成品里根本没有本地
    ASR，自检却常亮绿灯，客户的语音被静默降级成「[语音]」占位，没人会怀疑到这里。
    """
    _asr_module(monkeypatch, False)
    assert not asr_backend_ready({"voice_recognition": {"provider": "faster_whisper"}})
    assert not asr_backend_ready({})  # 缺省 faster_whisper，同样要真装了才算
    _asr_module(monkeypatch, True)
    assert asr_backend_ready({"voice_recognition": {"provider": "faster_whisper"}})


def test_asr_backend_ready_remote_endpoints(monkeypatch):
    _asr_module(monkeypatch, False)
    # 云端 openai 需 key
    assert not asr_backend_ready(
        {"voice_recognition": {"provider": "openai", "openai": {"api_key": ""}}})
    assert asr_backend_ready(
        {"voice_recognition": {"provider": "openai", "openai": {"api_key": "sk"}}})
    # 本机/局域网 OpenAI 兼容端点无需 key，有地址即可（与 OpenAITranscriber 同口径）
    assert asr_backend_ready(
        {"voice_recognition": {"provider": "qwen3_asr",
                               "openai": {"base_url": "http://192.168.0.176:8000/v1"}}})


def test_asr_backend_ready_counts_fallback_cascade(monkeypatch):
    """级联任一级可用即算就绪（create_transcriber 就是这么建的，探针不能只看主级）。"""
    _asr_module(monkeypatch, False)
    cfg = {"voice_recognition": {
        "provider": "faster_whisper",  # 主级不可用（本机没装）
        "fallback": [{"provider": "openai", "openai": {"api_key": "sk"}}],
    }}
    assert asr_backend_ready(cfg)


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


def test_needs_backend_carries_reason_and_retryable(monkeypatch):
    """状态要带机器可读原因码：UI 据此决定出「重试接入」还是出真话，而不是一律甩客服。"""
    monkeypatch.setenv("AITR_MANAGED_EDITION", "1")
    _asr_module(monkeypatch, False)
    cfg = {"licensing": {"hosted_ai": {"enabled": True}},
           "ai": {"api_key": "cx.tok"},
           "vision": {"enabled": True},
           "voice_recognition": {"enabled": True, "provider": "faster_whisper"}}
    rep = collect_media_status(cfg)
    vis = _status(rep, "vision_inbound")
    # 托管态 + 有令牌 + 没注入 → 供给没发生，可自助重试
    assert vis["reason"] == "not_provisioned" and vis["retryable"] is True
    asr = _status(rep, "asr_inbound")
    # 本机没装识别模型：这不是重试能解决的，别给假希望
    assert asr["reason"] == "no_local_module" and asr["retryable"] is False
    assert rep["summary"]["retryable"] is True
    assert rep["summary"]["managed"] is True


def test_enabled_with_backend_is_active(monkeypatch):
    _asr_module(monkeypatch, True)
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


def test_preset_backend_warnings_none_when_ready(monkeypatch):
    _asr_module(monkeypatch, True)
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
