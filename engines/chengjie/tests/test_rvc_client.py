"""RVC 变声客户端 + AvatarHub 集群鉴权头（X-AH-Svc）单测。全离线。"""
from __future__ import annotations

import base64
import json

import pytest

from src.ai import rvc_client as R
from src.ai.voice_clone_client import VoiceCloneClient


# ── RVC 纯函数 ────────────────────────────────────────────

def test_build_convert_payload():
    raw = R.build_convert_payload("QUJD", r"D:\w\CN_丁真.pth", pitch=2, protect=0.5)
    d = json.loads(raw.decode())
    assert d["audio_base64"] == "QUJD"
    assert d["pth_path"] == r"D:\w\CN_丁真.pth"
    assert d["pitch"] == 2 and d["protect"] == 0.5
    assert d["f0method"] == "rmvpe"


def test_parse_convert_response_ok():
    audio = b"\x00\x01WAV"
    body = json.dumps({"ok": True, "audio_base64": base64.b64encode(audio).decode()}).encode()
    assert R.parse_convert_response(body) == audio


def test_parse_convert_response_ok_false_raises():
    body = json.dumps({"ok": False, "error": "bad pth"}).encode()
    with pytest.raises(RuntimeError, match="bad pth"):
        R.parse_convert_response(body)


def test_parse_convert_response_no_audio_raises():
    with pytest.raises(RuntimeError, match="no audio"):
        R.parse_convert_response(json.dumps({"ok": True}).encode())


def test_resolve_pth_path():
    assert R.resolve_pth_path("CN_丁真", r"D:\w").replace("/", "\\") == r"D:\w\CN_丁真.pth"
    # 已是绝对路径/带 .pth → 原样
    assert R.resolve_pth_path(r"D:\x\CN_特朗普.pth") == r"D:\x\CN_特朗普.pth"
    assert R.resolve_pth_path("") == ""


# ── RVC 客户端鉴权头 ──────────────────────────────────────

def test_rvc_convert_sends_auth_header(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return json.dumps({"ok": True, "audio_base64": base64.b64encode(b"OUT").decode()}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        captured["url"] = req.full_url
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(R.urllib.request, "urlopen", _fake_urlopen)
    c = R.RvcClient({"base_url": "http://svc:6242", "svc_token": "TOK123", "weights_dir": r"D:\w"})
    out = c.convert(b"INWAV", "CN_丁真")
    assert out == b"OUT"
    # X-AH-Svc 头带上了（urllib 会把 header 名首字母大写化 → X-ah-svc）
    hdrs = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs.get("x-ah-svc") == "TOK123"
    assert captured["url"] == "http://svc:6242/convert"
    # pth 路径按音色名拼出
    assert json.loads(captured["body"].decode())["pth_path"].endswith("CN_丁真.pth")


def test_rvc_health_sends_auth_header(monkeypatch):
    # RVC 无 /health，存活探测走 /inputDevices（非 /health 端点）→ 需带令牌，
    # 否则 401/403 被误判不可达（实测踩过：convert 通但 health 返 False）。
    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in dict(req.headers).items()}
        return _Resp()

    monkeypatch.setattr(R.urllib.request, "urlopen", _fake_urlopen)
    c = R.RvcClient({"base_url": "http://svc:6242", "svc_token": "TOK123"})
    assert c.health_ok() is True
    assert captured["headers"].get("x-ah-svc") == "TOK123"   # /inputDevices 需带鉴权头


# ── VoiceCloneClient（Fish 7855）鉴权头 ───────────────────

def test_clone_client_sends_svc_header(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return json.dumps({"ok": True, "audio_base64": base64.b64encode(b"WAVOUT").decode()}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "src.ai.voice_clone_client.urllib.request.urlopen",
        lambda req, timeout=None: (captured.update(
            headers={k.lower(): v for k, v in dict(req.headers).items()},
            url=req.full_url) or _Resp()))

    c = VoiceCloneClient({"base_url": "http://svc:7855", "svc_token": "TOK9"})
    out = c._request_clone("你好", "QUJD", "zh", "", "")
    assert out == b"WAVOUT"
    assert captured["headers"].get("x-ah-svc") == "TOK9"
    assert captured["url"] == "http://svc:7855/v1/tts/clone"


def test_clone_client_token_from_env(monkeypatch):
    monkeypatch.setenv("AH_SVC_TOKEN", "ENVTOK")
    c = VoiceCloneClient({"base_url": "http://svc:7855", "svc_token_env": "AH_SVC_TOKEN"})
    assert c.svc_token == "ENVTOK"


def test_clone_client_no_token_no_header():
    c = VoiceCloneClient({"base_url": "http://svc:7855"})
    assert c.svc_token == ""      # 无令牌 → 不带头（向后兼容）


# ── TTSPipeline × RVC 后处理 ──────────────────────────────

def test_pipeline_rvc_cache_key_differs_by_voice():
    from src.ai.tts_pipeline import TTSPipeline
    base = {"backend": "avatar_clone", "rvc": {"enabled": True},
            "voice_profile": {"enabled": True, "backend": "avatar_clone",
                              "owner_consent": True,
                              "reference_audio_path": "x.wav"}}
    p1 = TTSPipeline({**base, "voice_profile": {**base["voice_profile"], "rvc_voice": "CN_丁真"}})
    p2 = TTSPipeline({**base, "voice_profile": {**base["voice_profile"], "rvc_voice": "古天乐2"}})
    from src.ai.voice_emotion import NEUTRAL
    k1 = p1._cache_key("你好", "v", "avatar_clone", NEUTRAL)
    k2 = p2._cache_key("你好", "v", "avatar_clone", NEUTRAL)
    assert k1 != k2       # 不同 rvc_voice → 不同缓存键（防串音）


def test_pipeline_rvc_disabled_no_target():
    from src.ai.tts_pipeline import TTSPipeline
    p = TTSPipeline({"backend": "avatar_clone",
                     "voice_profile": {"enabled": True, "rvc_voice": "CN_丁真"}})
    assert p._rvc_target_voice() == ""   # rvc 未启用 → 不变声


def test_pipeline_maybe_apply_rvc_success(monkeypatch, tmp_path):
    import asyncio
    from src.ai.tts_pipeline import TTSPipeline, TTSResult
    wav = tmp_path / "out.wav"
    wav.write_bytes(b"ORIGINAL")
    p = TTSPipeline({"backend": "avatar_clone", "rvc": {"enabled": True},
                     "voice_profile": {"enabled": True, "rvc_voice": "CN_丁真"}})

    import src.ai.rvc_client as R
    monkeypatch.setattr(R.RvcClient, "convert", lambda self, data, name, **kw: b"RVC_" + data)

    rv = TTSResult(ok=True, text="你好", provider="avatar_clone", format="wav",
                   audio_path=str(wav))
    out = asyncio.run(p._maybe_apply_rvc(rv))
    assert wav.read_bytes() == b"RVC_ORIGINAL"          # 已变声覆盖
    assert out.extra.get("rvc_voice") == "CN_丁真"
    assert "rvc:CN_丁真" in out.provider


def test_pipeline_maybe_apply_rvc_skips_mp3(monkeypatch, tmp_path):
    import asyncio
    from src.ai.tts_pipeline import TTSPipeline, TTSResult
    mp3 = tmp_path / "out.mp3"
    mp3.write_bytes(b"MP3DATA")
    p = TTSPipeline({"backend": "edge_tts", "rvc": {"enabled": True},
                     "voice_profile": {"enabled": True, "rvc_voice": "CN_丁真"}})
    rv = TTSResult(ok=True, text="hi", provider="edge_tts", format="mp3",
                   audio_path=str(mp3))
    out = asyncio.run(p._maybe_apply_rvc(rv))
    assert mp3.read_bytes() == b"MP3DATA"     # mp3 不变声（RVC 只吃 WAV）
    assert "rvc" not in out.extra


def test_pipeline_maybe_apply_rvc_failure_keeps_original(monkeypatch, tmp_path):
    import asyncio
    from src.ai.tts_pipeline import TTSPipeline, TTSResult
    wav = tmp_path / "out.wav"
    wav.write_bytes(b"ORIGINAL")
    p = TTSPipeline({"backend": "avatar_clone", "rvc": {"enabled": True},
                     "voice_profile": {"enabled": True, "rvc_voice": "CN_丁真"}})

    import src.ai.rvc_client as R

    def _boom(self, data, name, **kw):
        raise RuntimeError("rvc down")
    monkeypatch.setattr(R.RvcClient, "convert", _boom)

    rv = TTSResult(ok=True, text="你好", provider="avatar_clone", format="wav",
                   audio_path=str(wav))
    out = asyncio.run(p._maybe_apply_rvc(rv))
    assert wav.read_bytes() == b"ORIGINAL"    # 失败保留原音
    assert out.ok is True                     # 不影响主流程
