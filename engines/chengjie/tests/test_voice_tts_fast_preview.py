"""tts-test「fast 快速档」+ GET /api/voice/effective-config 契约门禁（渠道中心试听）。

Hermetic 离线：假 TTSPipeline（不碰真 TTS/GPU/网络）、PersonaManager 打断（不依赖
真人设文件）、licensing 配额放行、预览目录指向 tmp_path（不写生产 tmp_tts_preview/）。

与渠道中心前端约定的契约（一字不差）：
- POST /api/voice/tts-test  body.fast=true → 强制 backend=edge_tts + 剥 voice_profile +
  音色不含 "Neural" 时置 zh-CN-XiaoxiaoNeural；链内超时 15s / 外层 20s。
  响应成功与失败路径都带 ``fast``；成功再带 ``requested_backend``（fast=被替换前的
  后端名；非 fast=当前 voice_cfg backend）。不传 fast → 旧行为不变（45s/50s）。
- GET /api/voice/effective-config?platform=&persona_id=[&chat_key=&account_id=] →
  固定 12 键：ok/platform/persona_id/persona_source/backend/voice/is_clone/ready/
  hub_strict/hub_risk/channel_backend/reference_audio（2026-08-05 P1 增
  is_clone/ready/hub_strict/hub_risk 四键=坐席音色状态条数据源；chat_key/account_id
  为可选入参，不传=旧解析行为）；reference_audio 只给 basename（不泄露目录）；
  异常 → {ok:false,error}（不抛 500）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.routes.voice_routes as voice_routes_mod
from src.web.routes.voice_routes import register_voice_routes


def _noop_auth(request: Request):
    return None


class _CfgMgr:
    """最小 config_manager 替身：voice_routes 只读 .config。"""

    def __init__(self, config):
        self.config = config


def _base_config():
    return {
        "telegram": {"voice_reply": {
            "backend": "avatar_clone",
            "voice": "lin_xiaoyu",
            "voice_profile": {
                "enabled": True,
                "speaker_id": "lin_xiaoyu",
                "reference_audio_path": "assets/voices/lin/ref.wav",
            },
        }},
        "whatsapp_rpa": {"voice_output": {"backend": "edge_tts"}},
    }


class _FakeTTSResult:
    """形状对齐 voice_routes 消费的 TTSResult 字段（ok/audio_path/duration_sec/
    provider/voice/format/extra/error）。"""

    def __init__(self, audio_path: str):
        self.ok = True
        self.audio_path = audio_path
        self.error = ""
        self.provider = "fake_edge"
        self.voice = "fake-voice"
        self.format = "mp3"
        self.duration_sec = 1.5
        self.extra = {}


class _FakeTTSPipeline:
    """记录传入 voice_cfg 与 synthesize kwargs；真写一个小音频文件供 rename/stat。"""

    calls: list = []

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})

    async def synthesize(self, text, **kwargs):
        type(self).calls.append({"cfg": dict(self.cfg), "kwargs": dict(kwargs)})
        out_dir = Path(self.cfg.get("out_dir") or ".")
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"fake-{len(type(self).calls)}.mp3"
        p.write_bytes(b"0" * 1024)
        return _FakeTTSResult(str(p))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    _FakeTTSPipeline.calls = []
    # voice_routes 在函数体内延迟 `from src.ai.tts_pipeline import TTSPipeline`
    # → monkeypatch 打在源模块属性上即可生效。
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _FakeTTSPipeline)
    # licensing 配额放行（同为延迟 import；默认无授权本就放行，钉死防环境漂移）
    monkeypatch.setattr("src.licensing.quota_store.check_license_quota",
                        lambda *a, **k: {"allowed": True})

    # 不依赖真人设文件：PersonaManager 一律抛错 → persona_voice 各层走文档化兜底
    # （resolve_effective_voice_context 内层 try/except → source=fallback）。
    from src.utils.persona_manager import PersonaManager

    def _boom(*a, **k):
        raise RuntimeError("no persona files in hermetic test")

    monkeypatch.setattr(PersonaManager, "get_instance", classmethod(_boom))
    # 预览文件写进 tmp_path，不污染生产 tmp_tts_preview/
    monkeypatch.setattr(voice_routes_mod, "_TTS_PREVIEW_DIR", tmp_path / "previews")
    yield


@pytest.fixture
def client():
    app = FastAPI()
    register_voice_routes(app, api_auth=_noop_auth,
                          config_manager=_CfgMgr(_base_config()))
    return TestClient(app)


# ── 任务 1：fast 快速档 ─────────────────────────────────────────────


def test_fast_true_forces_edge_and_reports_requested_backend(client):
    r = client.post("/api/voice/tts-test", json={"text": "你好呀", "fast": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["fast"] is True
    assert body["requested_backend"] == "avatar_clone"
    # 假管线收到的 voice_cfg：edge 后端 / 克隆 profile 已剥 / Neural 音色
    assert len(_FakeTTSPipeline.calls) == 1
    call = _FakeTTSPipeline.calls[0]
    assert call["cfg"]["backend"] == "edge_tts"
    assert "voice_profile" not in call["cfg"]
    assert "Neural" in call["cfg"]["voice"]
    # fast 档链内超时收紧到 15s
    assert call["kwargs"]["timeout_sec"] == 15.0


def test_fast_keeps_existing_edge_neural_voice(client):
    r = client.post("/api/voice/tts-test", json={
        "text": "hello", "fast": True,
        "voice_cfg_override": {"voice": "en-US-AriaNeural"},
    })
    assert r.json()["ok"] is True
    # 已是 edge 音色 id（含 Neural）→ 保留，不被顶成中文默认音色
    assert _FakeTTSPipeline.calls[0]["cfg"]["voice"] == "en-US-AriaNeural"


def test_no_fast_keeps_old_behavior(client):
    r = client.post("/api/voice/tts-test", json={"text": "你好呀"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["fast"] is False
    assert body["requested_backend"] == "avatar_clone"
    call = _FakeTTSPipeline.calls[0]
    # 后端/克隆 profile 原样进管线（逐字节等价旧逻辑）
    assert call["cfg"]["backend"] == "avatar_clone"
    assert isinstance(call["cfg"].get("voice_profile"), dict)
    assert call["cfg"]["voice"] == "lin_xiaoyu"
    assert call["kwargs"]["timeout_sec"] == 45.0


def test_failure_paths_carry_fast_flag(client, monkeypatch):
    # 合成抛异常路径
    async def _raise(self, text, **kwargs):
        raise RuntimeError("synth down")

    monkeypatch.setattr(_FakeTTSPipeline, "synthesize", _raise)
    body = client.post("/api/voice/tts-test",
                       json={"text": "hi", "fast": True}).json()
    assert body["ok"] is False
    assert body["fast"] is True
    assert "requested_backend" not in body  # 契约：失败路径只加 fast

    # result.ok=False 路径
    async def _not_ok(self, text, **kwargs):
        res = _FakeTTSResult("")
        res.ok = False
        res.error = "backend unavailable"
        return res

    monkeypatch.setattr(_FakeTTSPipeline, "synthesize", _not_ok)
    body2 = client.post("/api/voice/tts-test", json={"text": "hi"}).json()
    assert body2["ok"] is False
    assert body2["fast"] is False
    assert body2["error"] == "backend unavailable"


# ── 任务 2：GET /api/voice/effective-config 契约 ────────────────────


_CONTRACT_KEYS = {"ok", "platform", "persona_id", "persona_source",
                  "backend", "voice", "is_clone", "ready",
                  "hub_strict", "hub_risk",
                  "channel_backend", "reference_audio"}


def test_effective_config_contract_telegram(client):
    r = client.get("/api/voice/effective-config", params={"platform": "telegram"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == _CONTRACT_KEYS
    assert body["ok"] is True
    assert body["platform"] == "telegram"
    assert body["backend"] == "avatar_clone"
    assert body["voice"] == "lin_xiaoyu"
    assert body["channel_backend"] == "avatar_clone"
    # basename only：不泄露目录、无路径分隔符
    assert body["reference_audio"] == "ref.wav"
    assert "/" not in body["reference_audio"]
    assert "\\" not in body["reference_audio"]


def test_effective_config_channel_mapping_and_unknown_platform(client):
    body = client.get("/api/voice/effective-config",
                      params={"platform": "whatsapp"}).json()
    assert body["ok"] is True
    assert body["channel_backend"] == "edge_tts"

    body2 = client.get("/api/voice/effective-config",
                       params={"platform": "not_a_platform"}).json()
    assert body2["ok"] is True
    assert body2["channel_backend"] == ""
    assert set(body2) == _CONTRACT_KEYS


def test_effective_config_never_500(client, monkeypatch):
    # resolver 整体炸掉 → {ok:false,error}，不抛 500（前端静默隐藏面板）
    import src.ai.persona_voice as pv

    def _explode(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(pv, "resolve_effective_voice_context", _explode)
    r = client.get("/api/voice/effective-config")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "resolver exploded" in body["error"]
