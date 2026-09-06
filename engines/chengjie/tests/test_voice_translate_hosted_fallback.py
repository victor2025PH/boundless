# -*- coding: utf-8 -*-
"""B27（实施49，2026-08-21）：翻译工具语音链的托管 ASR 回落 + 报错人话化。

内测实录：客户包提交语音文件 → 「处理失败: 语音转写未启用
(config.audio_pipeline.enabled)」。根因＝翻译三路由硬闸顶层 ``audio_pipeline``
（客户包死默认 enabled:false），而托管 ASR（ensure_hosted_asr）只注
``voice_recognition`` 段——收件箱语音识别好好的，翻译工具却报未启用。

钉住的不变量：
- ``resolve_effective_audio_cfg``：audio_pipeline 开=原样返回（内部部署零变化）；
  关+``voice_recognition._hosted_asr`` 标记在 → 按注入形状（gateway-first 主位 /
  LAN-first fallback 条目）派生 openai 后端配置，设备令牌当次取
  ``AITR_HOSTED_AI_KEY``；无标记/无令牌 → ``{}``（**env 单独在不派生**——标记
  才是真相，防测试/内部环境被杂散 env 污染）。
- 路由层：托管派生后转写真的跑（transcript 流转）；仍不可用时报错是人话，
  **绝不出现配置键**。
"""

import base64
import os
import tempfile

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.voice_translate import resolve_effective_audio_cfg

GW = "https://bd2026.cc/api/ai/v1"
TOKEN = "cx.test-token-123"


def _hosted_vr_gateway_first():
    """ensure_hosted_asr gateway-first 注入形状（LAN 不可达 → 网关主位）。"""
    return {
        "enabled": True,
        "_hosted_asr": True,
        "provider": "openai_compatible",
        "base_url": GW,
        "api_key": "hosted",
        "model": "large-v3-turbo",
        "timeout": 45,
        "max_retries": 0,
    }


def _hosted_vr_lan_first():
    """LAN-first 注入形状：主位是 LAN，网关在 fallback（_hosted_asr_entry）。"""
    return {
        "enabled": True,
        "_hosted_asr": True,
        "provider": "openai_compatible",
        "base_url": "http://192.168.0.176:9100/v1",
        "api_key": "lan-key",
        "fallback": [
            {"provider": "openai_compatible", "base_url": GW,
             "api_key": "hosted", "model": "large-v3-turbo",
             "timeout": 45, "max_retries": 0, "_hosted_asr_entry": True},
        ],
    }


# ── resolve_effective_audio_cfg 纯函数 ──────────────────────────────────────

def test_audio_pipeline_enabled_returned_unchanged(monkeypatch):
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", TOKEN)
    cfg = {"audio_pipeline": {"enabled": True, "backend": "faster_whisper",
                              "model_size": "base"},
           "voice_recognition": _hosted_vr_gateway_first()}
    out = resolve_effective_audio_cfg(cfg)
    assert out["backend"] == "faster_whisper" and out["enabled"] is True
    assert "_derived_hosted_asr" not in out


def test_no_marker_no_derive_even_with_env(monkeypatch):
    """env 单独在不派生——防内部/测试环境被杂散 env 污染（标记=真相）。"""
    monkeypatch.setenv("AITR_HOSTED_ASR_BASE_URL", GW)
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", TOKEN)
    assert resolve_effective_audio_cfg(
        {"audio_pipeline": {"enabled": False}}) == {}
    assert resolve_effective_audio_cfg(
        {"audio_pipeline": {"enabled": False},
         "voice_recognition": {"enabled": True}}) == {}


def test_gateway_first_derives_openai_cfg(monkeypatch):
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", TOKEN)
    out = resolve_effective_audio_cfg(
        {"audio_pipeline": {"enabled": False},
         "voice_recognition": _hosted_vr_gateway_first()})
    assert out["enabled"] is True and out["backend"] == "openai"
    assert out["base_url"] == GW
    assert out["api_key"] == TOKEN          # 占位 "hosted" 已解析成真令牌
    assert out["model"] == "large-v3-turbo"
    assert out["_derived_hosted_asr"] is True


def test_lan_first_derives_from_fallback_entry(monkeypatch):
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", TOKEN)
    out = resolve_effective_audio_cfg(
        {"audio_pipeline": {}, "voice_recognition": _hosted_vr_lan_first()})
    assert out["backend"] == "openai" and out["base_url"] == GW
    assert out["api_key"] == TOKEN


def test_marker_without_token_returns_empty(monkeypatch):
    monkeypatch.delenv("AITR_HOSTED_AI_KEY", raising=False)
    assert resolve_effective_audio_cfg(
        {"voice_recognition": _hosted_vr_gateway_first()}) == {}


def test_marker_with_env_base_fallback(monkeypatch):
    """标记在但形状缺 base（异常态）→ env AITR_HOSTED_ASR_BASE_URL 兜底。"""
    monkeypatch.setenv("AITR_HOSTED_ASR_BASE_URL", GW + "/")
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", TOKEN)
    out = resolve_effective_audio_cfg(
        {"voice_recognition": {"_hosted_asr": True, "api_key": "hosted"}})
    assert out["base_url"] == GW            # 尾斜杠已剥
    assert out["model"] == "large-v3-turbo"  # 缺省模型


# ── 路由层（与 test_media_translate_route 同款轻装配） ───────────────────────

class _Templates:
    def TemplateResponse(self, *a, **k):
        raise AssertionError("not used")


class FakeCM:
    def __init__(self, cfg):
        self.config = cfg


class FakeAI:
    async def chat(self, prompt, context=None):
        return "你好"


def _client(cfg=None):
    from src.web.routes.unified_inbox_routes import register_unified_inbox_routes
    app = FastAPI()

    def _auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=_auth, api_auth=_auth, templates=_Templates())
    app.state.ai_client = FakeAI()
    app.state.config_manager = FakeCM(cfg or {})
    return TestClient(app)


class _FakeRv:
    ok = True
    text = "hello world"
    language = "en"
    latency_ms = 5
    model = "fake-asr"
    error = ""
    extra: dict = {}
    duration_sec = 1.0


def _patch_transcriber(monkeypatch, seen):
    def _fake_build(audio_cfg, *, want_segments=False):
        seen.append(dict(audio_cfg))

        async def _tr(path):
            return _FakeRv()
        return _tr
    monkeypatch.setattr(
        "src.ai.voice_translate.build_audio_transcribe_fn", _fake_build)


def _hosted_cfg():
    return {"audio_pipeline": {"enabled": False},
            "voice_recognition": _hosted_vr_gateway_first()}


class _FakeWsTranscriber:
    """工作台 voice_transcriber 替身（M-5 D 起工具箱默认复用它）。"""
    last_error = ""

    async def transcribe_voice_message(self, path, language="zh"):
        return "hello world"


def _patch_workspace(monkeypatch, seen):
    def _fake_ws(full_cfg):
        seen.append(dict((full_cfg or {}).get("voice_recognition") or {}))
        return _FakeWsTranscriber()
    monkeypatch.setattr("src.ai.voice_translate.workspace_transcriber", _fake_ws)


def test_route_message_media_voice_uses_workspace_transcriber(monkeypatch):
    """B27 主链回归钉（M-5 D #225 改口径）：audio_pipeline 关 + 托管 ASR 在 → 语音
    消息翻译**复用工作台 voice_transcriber**（同入口同配置，含 apply_hosted_asr 注入
    形状）真转写；此前此形态直接 asr_disabled（B27 前）/ 另起 audio_pipeline 派生
    客户端（B27，5NXHUW 实录两链两套配置一超时一成功）。"""
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", TOKEN)
    seen = []
    _patch_workspace(monkeypatch, seen)
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(b"OggS fake")
    try:
        r = _client(_hosted_cfg()).post(
            "/api/unified-inbox/translate-message-media",
            json={"media_ref": path, "media_type": "voice"}).json()
        assert r.get("reason") != "asr_disabled"
        assert r.get("transcript") == "hello world"
        assert r.get("asr_chain") == "workspace"
        assert seen and seen[0]["base_url"] == GW and seen[0]["_hosted_asr"] is True
        assert r["received"]["bytes"] == 9
    finally:
        os.remove(path)


def test_route_translate_voice_uses_workspace_transcriber(monkeypatch):
    """B27 报障入口（翻译工具上传语音）同口径 + 回显已收到多少。"""
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", TOKEN)
    seen = []
    _patch_workspace(monkeypatch, seen)
    r = _client(_hosted_cfg()).post(
        "/api/unified-inbox/translate-voice",
        json={"audio_b64": base64.b64encode(b"OggS fake").decode(),
              "target_lang": "zh"}).json()
    assert r.get("reason") != "asr_disabled"
    assert r.get("transcript") == "hello world"
    assert r.get("asr_chain") == "workspace" and seen
    assert r["received"]["bytes"] == 9 and r["received"]["container"] == "ogg"


def test_route_translate_voice_derive_still_used_when_workspace_absent(monkeypatch):
    """B27 派生路径保留为回落：voice_recognition 未启用（工作台没 transcriber）但托管
    标记在 → 仍按 resolve_effective_audio_cfg 派生网关配置走 AudioPipeline。"""
    monkeypatch.setenv("AITR_HOSTED_AI_KEY", TOKEN)
    seen = []
    _patch_transcriber(monkeypatch, seen)
    cfg = {"audio_pipeline": {"enabled": False},
           "voice_recognition": {**_hosted_vr_gateway_first(), "enabled": False}}
    r = _client(cfg).post(
        "/api/unified-inbox/translate-voice",
        json={"audio_b64": base64.b64encode(b"OggS fake").decode(),
              "target_lang": "zh"}).json()
    assert r.get("reason") != "asr_disabled"
    assert r.get("transcript") == "hello world"
    assert r.get("asr_chain") == "pipeline"
    assert seen and seen[0]["_derived_hosted_asr"] is True


def test_route_still_gated_without_hosted_and_message_humane(monkeypatch):
    """托管不在 → 仍如实拦（reason 兼容旧前端），但文案是人话零配置键。"""
    monkeypatch.delenv("AITR_HOSTED_AI_KEY", raising=False)
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    try:
        r = _client({"audio_pipeline": {"enabled": False}}).post(
            "/api/unified-inbox/translate-message-media",
            json={"media_ref": path, "media_type": "voice"}).json()
        assert r["ok"] is False and r["reason"] == "asr_disabled"
        assert "config." not in str(r.get("message") or "")
        assert "audio_pipeline" not in str(r.get("message") or "")
    finally:
        os.remove(path)


def test_route_translate_voice_gated_message_humane(monkeypatch):
    monkeypatch.delenv("AITR_HOSTED_AI_KEY", raising=False)
    r = _client({"audio_pipeline": {"enabled": False}}).post(
        "/api/unified-inbox/translate-voice",
        json={"audio_b64": base64.b64encode(b"x").decode()}).json()
    assert r["ok"] is False and r["reason"] == "asr_disabled"
    assert "config." not in str(r.get("message") or "")


def test_route_translate_video_gate_messages_humane(monkeypatch):
    """video_disabled / asr_disabled 两道闸的文案都不得出现配置键。"""
    monkeypatch.delenv("AITR_HOSTED_AI_KEY", raising=False)
    c = _client({"audio_pipeline": {"enabled": False}})
    r = c.post("/api/unified-inbox/translate-video",
               json={"video_b64": base64.b64encode(b"x").decode()}).json()
    assert r["ok"] is False and r["reason"] == "video_disabled"
    assert "config." not in str(r.get("message") or "")
    c2 = _client({"audio_pipeline": {"enabled": False},
                  "media": {"video_translate": {"enabled": True}}})
    r2 = c2.post("/api/unified-inbox/translate-video",
                 json={"video_b64": base64.b64encode(b"x").decode()}).json()
    assert r2["ok"] is False and r2["reason"] == "asr_disabled"
    assert "config." not in str(r2.get("message") or "")
