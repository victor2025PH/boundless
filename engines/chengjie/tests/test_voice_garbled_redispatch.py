# -*- coding: utf-8 -*-
"""#161 念错（garbled）→ 改派 Edge 标准声：tts-test 路由级行为门禁。

事故（2026-09-03 钧机 1.0.71 报告 22:46-23:04，证据群 mid=1145）：日语克隆连
三次 CER>0.35、重合成仍是「ゾオパパパ」乱音，而服务端终审只有「有声/无声」
两档 —— 字节有能量即判 voiced 放行，坐席只能靠耳朵事后发现，还被标成「疑似
无声」（归因也是错的：明明有声，是念错）。

本文件钉住修复后的三段行为：
  ① 判乱码 → 就地改派 Edge 同语种标准声重合成，响应里 speech 回到 voiced，
     并带上 fallback_from / fallback_reason=lang_unsupported（前端照常亮
     「非克隆声」+「本条使用标准声」）；
  ② 改派不可行/仍失败 → 如实回 speech=garbled（前端禁发），绝不放行；
  ③ 正常产物（CER≈0）零影响 —— 这是 99% 的流量。

Hermetic：假 TTSPipeline（按 backend 决定给什么 synth_verify，不碰真 GPU）、
PersonaManager 打断、配额放行、预览目录进 tmp_path。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import src.web.routes.voice_routes as voice_routes_mod
from src.web.routes.voice_routes import register_voice_routes

#: 1145 证据群的回验形状：转出 22 字、与送稿无关、重合成过一次仍这么差
_GARBLED_SV = {"cer": 0.85, "retried": 1, "lang": "ja", "hyp_chars": 22}
_CLEAN_SV = {"cer": 0.03, "retried": 0, "lang": "ja", "hyp_chars": 30}
#: 日文送稿（足够长，detect_text_lang 判得出 ja）
_JA_TEXT = "どこにいるの。ご飯は食べた？一緒に遊びに行こうよ。"


def _noop_auth(request: Request):
    return None


class _CfgMgr:
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
        # 语种能力闸走「能力未知不拦」分支，好让请求真的走到合成与终审
        # ——本文件测的是**事后**那条腿（表来不及更新时的兜底）。
        "avatar_voice": {"enabled": True},
    }


class _FakeResult:
    def __init__(self, audio_path, provider, fmt="wav", extra=None):
        self.ok = True
        self.audio_path = audio_path
        self.error = ""
        self.provider = provider
        self.voice = "fake-voice"
        self.format = fmt
        self.duration_sec = 2.0
        self.extra = dict(extra or {})


class _FakePipeline:
    """按 backend 分叉：克隆链给乱码回验，edge_tts 给干净回验。"""

    calls: list = []
    clone_sv: dict = dict(_GARBLED_SV)
    edge_ok: bool = True

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or {})

    async def synthesize(self, text, **kwargs):
        backend = str(self.cfg.get("backend") or "").lower()
        type(self).calls.append({"backend": backend,
                                 "voice": self.cfg.get("voice"),
                                 "text": text})
        if backend == "edge_tts":
            if not type(self).edge_ok:
                r = _FakeResult("", "edge_tts", "mp3")
                r.ok = False
                r.error = "edge down"
                return r
            p = self._write("mp3")
            return _FakeResult(str(p), "edge_tts", "mp3",
                               {"synth_verify": dict(_CLEAN_SV)})
        p = self._write("wav")
        return _FakeResult(str(p), "avatar_clone", "wav",
                           {"synth_verify": dict(type(self).clone_sv)})

    def _write(self, suffix):
        out_dir = Path(self.cfg.get("out_dir") or ".")
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"fake-{len(type(self).calls)}.{suffix}"
        p.write_bytes(b"0" * 2048)
        return p


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    _FakePipeline.calls = []
    _FakePipeline.clone_sv = dict(_GARBLED_SV)
    _FakePipeline.edge_ok = True
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _FakePipeline)
    monkeypatch.setattr("src.licensing.quota_store.check_license_quota",
                        lambda *a, **k: {"allowed": True})
    # 能量探测：假音频不是真 wav，钉成「有能量」＝事故当时的字节事实
    monkeypatch.setattr("src.ai.avatar_voice.detect_silent_audio",
                        lambda *a, **k: False)

    from src.utils.persona_manager import PersonaManager

    def _boom(*a, **k):
        raise RuntimeError("no persona files in hermetic test")

    monkeypatch.setattr(PersonaManager, "get_instance", classmethod(_boom))
    monkeypatch.setattr(voice_routes_mod, "_TTS_PREVIEW_DIR",
                        tmp_path / "previews")
    voice_routes_mod._TTS_JOBS.clear()
    yield
    voice_routes_mod._TTS_JOBS.clear()


@pytest.fixture
def client():
    app = FastAPI()
    register_voice_routes(app, api_auth=_noop_auth,
                          config_manager=_CfgMgr(_base_config()))
    with TestClient(app) as c:
        yield c


def _preview(client, text=_JA_TEXT):
    r = client.post("/api/voice/tts-test", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


# ══ ① 判乱码 → 改派 Edge，且改派这件事对坐席可见 ═══════════════════════════

def test_garbled_clone_redispatched_to_edge(client):
    d = _preview(client)
    assert d["ok"] is True
    vm = d["voice_meta"]
    # 事故形态下**绝不**放行乱音：终审回到 voiced 是因为换了会念日语的音色
    assert vm["speech"] == "voiced", vm
    assert d["provider"] == "edge_tts"
    # 改派必须留痕：前端据此亮「非克隆声」+「本条使用标准声（日语暂不支持克隆）」
    assert vm["fallback_from"] == "avatar_clone"
    assert vm["fallback_reason"] == "lang_unsupported"
    assert vm["fallback_lang"] == "ja"
    # 第二发确实打的是 edge，且音色按语种取（不是中文兜底声念日语）
    backends = [c["backend"] for c in _FakePipeline.calls]
    assert backends == ["avatar_clone", "edge_tts"], backends
    assert _FakePipeline.calls[1]["voice"] == "ja-JP-NanamiNeural"


def test_redispatched_preview_file_is_the_edge_take(client):
    """响应里的 url/bytes 必须指向改派后的产物——指着旧乱音文件＝坐席点播放
    听到的还是「ゾオパパパ」，而元数据说一切正常，比不修更糟。"""
    d = _preview(client)
    assert d["filename"].endswith(".mp3"), d["filename"]
    assert d["url"].endswith(d["filename"])
    assert d["bytes"] > 0
    assert d["format"] == "mp3"


# ══ ② 改派不可行/失败 → 如实 garbled（禁发），绝不谎报 ═════════════════════

def test_edge_redispatch_failure_keeps_garbled(client):
    _FakePipeline.edge_ok = False
    d = _preview(client)
    assert d["ok"] is True
    assert d["voice_meta"]["speech"] == "garbled", d["voice_meta"]
    # 保留原克隆产物（有东西可听、可报障），但前端按 garbled 禁发
    assert d["provider"] == "avatar_clone"


def test_unmappable_language_keeps_garbled(client, monkeypatch):
    """连 Edge 都没有该语种音色 → 没有可改派的目标，如实 garbled 不硬改。"""
    monkeypatch.setattr("src.ai.lang_voice_route.default_edge_voice_for_lang",
                        lambda *a, **k: "")
    d = _preview(client)
    assert d["voice_meta"]["speech"] == "garbled"
    assert [c["backend"] for c in _FakePipeline.calls] == ["avatar_clone"]


# ══ ③ 正常产物零影响（99% 流量的回归护栏）═══════════════════════════════════

def test_clean_take_untouched(client):
    _FakePipeline.clone_sv = dict(_CLEAN_SV)
    d = _preview(client)
    assert d["voice_meta"]["speech"] == "voiced"
    assert d["provider"] == "avatar_clone"
    assert d["voice_meta"]["fallback_reason"] == ""
    assert [c["backend"] for c in _FakePipeline.calls] == ["avatar_clone"]


def test_zh_clean_take_untouched(client):
    _FakePipeline.clone_sv = {"cer": 0.05, "retried": 0, "hyp_chars": 18}
    d = _preview(client, text="今天过得怎么样呀？记得早点休息哦。")
    assert d["voice_meta"]["speech"] == "voiced"
    assert d["provider"] == "avatar_clone"
    assert [c["backend"] for c in _FakePipeline.calls] == ["avatar_clone"]
