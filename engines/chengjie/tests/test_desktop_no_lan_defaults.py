"""L-6 B：桌面客户机不再带办公室内网默认地址（skuio 机 C6EFRS「上游」三条的共同根因）。

钉住：
  - is_desktop_client / lan_default_or_empty 单一判定；
  - AvatarWhisperTranscriber / AvatarVoiceClient 的 STT 缺省 192.168.0.140:7854 在桌面态失效
    （未配置 → 空串 → 直接让位，不探内网）；非桌面部署行为不变；
  - health_watchdog LAN GPU 巡检桌面默认关（显式 enabled: true 可开）；
  - apply_hosted_asr 网关接管时摘掉内网备级并可逆还原；本机离线转写与回环端点保留；
  - spoken_style L4 改写桌面态须显式 rewrite_llm；
  - 干净包 config.desktop.min.yaml + 桌面态解析出的端点里没有 192.168.；
  - 语音识别翻译失败带人话 message；AIClient.embedding_status 三态。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.desktop_mode import is_desktop_client, lan_default_or_empty

ENGINE_ROOT = Path(__file__).resolve().parents[1]
LAN_STT = "http://192.168.0.140:7854"


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    yield


@pytest.fixture
def server(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    yield


# ── 判定 ──────────────────────────────────────────────────────────────

def test_is_desktop_client_env_and_config(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    assert is_desktop_client() is False
    assert is_desktop_client({"app": {"desktop_mode": True}}) is True
    assert is_desktop_client({"app": {"desktop_mode": False}}) is False
    monkeypatch.setenv("AITR_DESKTOP_MODE", "true")
    assert is_desktop_client() is True
    assert is_desktop_client({"app": {}}) is True


def test_lan_default_or_empty(desktop):
    assert lan_default_or_empty("", LAN_STT) == ""
    assert lan_default_or_empty(None, LAN_STT) == ""
    assert lan_default_or_empty("http://10.0.0.5:7854", LAN_STT) == "http://10.0.0.5:7854"


def test_lan_default_kept_on_server(server):
    assert lan_default_or_empty("", LAN_STT) == LAN_STT


# ── 转录器 / 语音客户端的 STT 缺省 ───────────────────────────────────

def test_avatar_whisper_transcriber_no_lan_default_on_desktop(desktop, tmp_path):
    from src.voice_transcriber import AvatarWhisperTranscriber

    t = AvatarWhisperTranscriber({"temp_dir": str(tmp_path)})
    assert t.base_url == ""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF....WAVEfake")
    # 未配置端点 → 直接让位（None），不解析令牌、不发请求
    assert asyncio.run(t._transcribe_impl(str(wav), "auto")) is None
    explicit = AvatarWhisperTranscriber({"temp_dir": str(tmp_path), "base_url": "http://10.9.9.9:7854/"})
    assert explicit.base_url == "http://10.9.9.9:7854"


def test_avatar_whisper_transcriber_keeps_lan_default_on_server(server, tmp_path):
    from src.voice_transcriber import AvatarWhisperTranscriber

    assert AvatarWhisperTranscriber({"temp_dir": str(tmp_path)}).base_url == LAN_STT


def test_avatar_voice_client_stt_unconfigured_on_desktop(desktop):
    from src.ai.avatar_voice import AvatarVoiceClient

    c = AvatarVoiceClient({})
    assert c.stt_base_url == ""
    assert c.stt(b"\x00\x01") is None
    assert c.translate("你好") is None
    h = c.stt_health()
    assert h["reachable"] is False and h.get("unconfigured") is True


def test_avatar_voice_client_stt_default_on_server(server):
    from src.ai.avatar_voice import AvatarVoiceClient

    assert AvatarVoiceClient({}).stt_base_url == LAN_STT
    assert AvatarVoiceClient({"stt": {"base_url": "http://10.1.1.1:7854"}}).stt_base_url == "http://10.1.1.1:7854"


# ── 看门狗 LAN GPU 巡检门 ────────────────────────────────────────────

def test_lan_gpu_remind_gate(desktop):
    from src.inbox.health_watchdog import lan_gpu_probe_targets, lan_gpu_remind_enabled

    skuio_like = {
        "avatar_voice": {"enabled": False, "_lan_seed": True, "colloquial": {
            "llm_endpoints": [{"base_url": "http://192.168.0.173:11434"},
                              {"base_url": "http://192.168.0.198:11434"}]}},
    }
    # 种子留下的 173/198 仍会被派生成探针目标 —— 所以门必须在桌面态关掉
    assert lan_gpu_probe_targets(skuio_like) == ["http://192.168.0.173:11434", "http://192.168.0.198:11434"]
    assert lan_gpu_remind_enabled(skuio_like) is False
    assert lan_gpu_remind_enabled({**skuio_like, "health_watchdog": {"lan_gpu_remind": {"enabled": True}}}) is True
    assert lan_gpu_remind_enabled({}) is False


def test_lan_gpu_remind_default_on_for_server(server):
    from src.inbox.health_watchdog import lan_gpu_remind_enabled

    assert lan_gpu_remind_enabled({}) is True
    assert lan_gpu_remind_enabled({"health_watchdog": {"lan_gpu_remind": {"enabled": False}}}) is False


# ── 托管 ASR：网关接管时摘掉内网备级 ─────────────────────────────────

def _vr_seed():
    return {"voice_recognition": {
        "enabled": True, "_lan_seed": True,
        "provider": "openai", "base_url": "http://192.168.0.176:8765/v1", "api_key": "x",
        "model": "large-v3-turbo",
        "fallback": [
            {"provider": "avatar_whisper"},                                   # 无地址＝代码缺省 140:7854
            {"provider": "openai_compatible", "base_url": "http://192.168.0.198:8765/v1"},
            {"provider": "openai_compatible", "base_url": "http://127.0.0.1:9911/v1"},  # 本机自跑服务
            {"provider": "faster_whisper"},                                  # 本机离线
        ],
    }}


def test_apply_hosted_asr_gateway_first_drops_lan_fallbacks(desktop):
    from src.ai.hosted_gateway import apply_hosted_asr

    cfg = _vr_seed()
    gw = "https://bd2026.cc/api/ai/v1"
    assert apply_hosted_asr(cfg, gw, gateway_first=True) is True
    vr = cfg["voice_recognition"]
    assert vr["base_url"] == gw and vr["provider"] == "openai_compatible"
    urls = [str((e.get("avatar") or {}).get("base_url") or e.get("base_url") or "") for e in vr["fallback"]]
    assert not any("192.168." in u for u in urls), urls
    provs = [e.get("provider") for e in vr["fallback"]]
    assert "avatar_whisper" not in provs
    assert "faster_whisper" in provs
    assert any(u.startswith("http://127.0.0.1") for u in urls)
    assert len(vr["_lan_fallback"]) == 2
    # 幂等：再来一轮不重复堆栈
    assert apply_hosted_asr(cfg, gw, gateway_first=True) is True
    assert len(vr["_lan_fallback"]) == 2


def test_apply_hosted_asr_lan_back_restores_fallbacks(desktop):
    from src.ai.hosted_gateway import apply_hosted_asr

    cfg = _vr_seed()
    gw = "https://bd2026.cc/api/ai/v1"
    apply_hosted_asr(cfg, gw, gateway_first=True)
    apply_hosted_asr(cfg, gw, gateway_first=False)
    vr = cfg["voice_recognition"]
    assert vr["base_url"] == "http://192.168.0.176:8765/v1"        # 主位还原
    provs = [e.get("provider") for e in vr["fallback"]]
    assert "avatar_whisper" in provs and "faster_whisper" in provs
    assert "_lan_fallback" not in vr
    assert vr["fallback"][-1].get("_hosted_asr_entry") is True          # 网关仍是最后一级


# ── spoken_style L4 改写 ───────────────────────────────────────────────

def _cm(root: dict):
    """spoken_style_bridge 读的是 ConfigManager 形态（.config）。"""
    return SimpleNamespace(config=root)


def test_rewrite_backend_requires_explicit_llm_on_desktop(desktop, monkeypatch):
    from src.ai import spoken_style_bridge as ssb

    monkeypatch.setattr(ssb, "_REWRITE_UNCONFIGURED_LOGGED", False)
    base = _cm({"ai": {"spoken_style": {"enabled": True, "rewrite": True}}})
    assert ssb.rewrite_backend_available(base) is False
    explicit = _cm({"ai": {"spoken_style": {"enabled": True, "rewrite": True,
                                            "rewrite_llm": "https://bd2026.cc/api/ai/v1/chat/completions"}}})
    assert ssb.rewrite_backend_available(explicit) is True


def test_rewrite_backend_config_flag_without_env(server):
    from src.ai import spoken_style_bridge as ssb

    # 没有 env 但 config 写了 app.desktop_mode → 同样按桌面态处理
    cm = _cm({"app": {"desktop_mode": True},
              "ai": {"spoken_style": {"enabled": True, "rewrite": True}}})
    assert ssb.rewrite_backend_available(cm) is False


def test_rewrite_backend_default_ok_on_server(server):
    from src.ai import spoken_style_bridge as ssb

    assert ssb.rewrite_backend_available(_cm({"ai": {"spoken_style": {"enabled": True, "rewrite": True}}})) is True


# ── 干净包配置在桌面态解析不含 192.168. ─────────────────────────────

def test_clean_desktop_config_resolves_without_lan_addresses(desktop):
    import yaml

    from src.ai.avatar_voice import AvatarVoiceClient
    from src.inbox.health_watchdog import lan_gpu_probe_targets
    from src.voice_transcriber import AvatarWhisperTranscriber

    text = (ENGINE_ROOT / "config" / "config.desktop.min.yaml").read_text(encoding="utf-8")
    assert "192.168." not in text, "干净包种子配置不得带办公室内网地址"
    cfg = yaml.safe_load(text) or {}
    assert lan_gpu_probe_targets(cfg) == []
    assert "192.168." not in AvatarVoiceClient.from_config(cfg).stt_base_url
    vr = dict(cfg.get("voice_recognition") or {})
    vr.setdefault("temp_dir", "./temp/test_desktop_no_lan")
    assert "192.168." not in AvatarWhisperTranscriber(vr).base_url


# ── 语音识别翻译失败人话 ─────────────────────────────────────────────

def test_asr_failure_message_attached():
    from src.web.routes.unified_inbox_translate_routes import _attach_asr_failure_message

    req = SimpleNamespace(state=SimpleNamespace(ui_lang="zh"))
    assert _attach_asr_failure_message(req, {"ok": False, "reason": "asr_failed"})["message"] == "转录服务暂不可用，稍后重试"
    assert _attach_asr_failure_message(req, {"ok": False, "reason": "asr_error"})["message"] == "转录服务暂不可用，稍后重试"
    assert _attach_asr_failure_message(req, {"ok": False, "reason": "no_speech"})["message"] == "未识别到语音内容"
    ok = {"ok": True, "transcript": "hi"}
    assert "message" not in _attach_asr_failure_message(req, ok)
    en = SimpleNamespace(state=SimpleNamespace(ui_lang="en"))
    assert "unavailable" in _attach_asr_failure_message(en, {"ok": False, "reason": "asr_failed"})["message"]


# ── 嵌入三态 ─────────────────────────────────────────────────────────

def test_embedding_status_three_states():
    from src.ai.ai_client import AIClient

    class _Cfg:
        config_path = None
        config = {"web_admin": {"site_name": "T"}, "ai": {}}

        def get_ai_config(self):
            return {}

    c = AIClient(_Cfg())
    c._use_openai_compat = True
    c._embedding_model = ""
    c._oa_embed_clients = []
    c._oa_client = None
    assert c.embedding_status()["state"] == "unconfigured"
    c._embedding_model = "bge-m3"
    c._oa_embed_clients = [("https://bd2026.cc/api/ai/v1", object())]
    assert c.embedding_status()["state"] == "ready"
    c._embed_unreachable_until = 10 ** 12
    c._embed_fail_streak = 3
    st = c.embedding_status()
    assert st["state"] == "circuit_open" and st["fail_streak"] == 3
    assert st["endpoints"] == ["https://bd2026.cc/api/ai/v1"]
