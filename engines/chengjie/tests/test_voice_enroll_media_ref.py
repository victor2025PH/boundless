# -*- coding: utf-8 -*-
"""P0「从会话语音消息一键导入克隆」门禁（2026-08-02）。

纯函数：
  - prepare_reference_audio — 策展+质检门：好样本过 / 过短拒 + force 放行 /
    无法解码拒 / ffmpeg 缺失降级存原件（绝不阻塞登记）/ 过长自动裁最佳片段 /
    首尾静音自动去除
  - build_source_ref — 溯源块形状（空字段不落键）
  - build_*_voice_profile(owner_consent=, source_ref=) — 三构造器透传

路由契约（/api/voice/enroll）：
  - media_ref 非 protocol_media 引用 / 路径穿越 → 400
  - media_ref 指向已清理文件 → 404
  - 消息导入缺 owner_consent → 400（在媒体解析之前拦）；上传显式否认同拒；
    上传不带字段＝旧客户端默认已确认（向后兼容）
  - 质检门：过短语音如实拒绝（200 {ok:false, reason:"ref_too_short"}），
    质检回显 quality 随响应返回
  - 全链成功：media_ref → 策展 wav 落 voice_samples → voice_profile 带
    owner_consent + source_ref(kind=inbox_message) + 逐字稿 sidecar
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from src.ai.voice_enroll import (
    build_avatar_voice_profile,
    build_lan_voice_profile,
    build_qwen_voice_profile,
    build_source_ref,
    prepare_reference_audio,
)


# ── 测试素材：可控「像说话」的合成 WAV（音高/能量都有起伏，别做纯平音叉）──────
def _wav_bytes(sec: float = 6.0, sr: int = 16000, *,
               silence_head: float = 0.0, silence_tail: float = 0.0) -> bytes:
    import numpy as np

    t = np.arange(int(sec * sr)) / sr
    freq = 220.0 + 30.0 * np.sin(2 * np.pi * 3.0 * t)          # 颤音=音高动态
    phase = np.cumsum(2 * np.pi * freq / sr)
    amp = 0.4 * (0.55 + 0.45 * np.sin(2 * np.pi * 1.7 * t))    # 起伏=能量动态
    a = amp * np.sin(phase)
    head = np.zeros(int(silence_head * sr))
    tail = np.zeros(int(silence_tail * sr))
    x = np.concatenate([head, a, tail])
    pcm = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ── prepare_reference_audio：策展 + 质检门 ───────────────────────────────────
def test_prepare_good_wav_passes(tmp_path):
    rv = prepare_reference_audio(_wav_bytes(6.0), ".wav", str(tmp_path), "v1")
    assert rv["ok"] is True and rv["reject_code"] == ""
    assert rv["audio_path"].endswith("v1.wav")
    assert Path(rv["audio_path"]).is_file()
    assert rv["duration_sec"] >= 3.0
    assert rv["level"] in ("ok", "warn")
    assert rv["degraded"] == ""


def test_prepare_too_short_rejected_then_force_passes(tmp_path):
    short = _wav_bytes(1.5)
    rv = prepare_reference_audio(short, ".wav", str(tmp_path), "v2")
    assert rv["ok"] is False and rv["reject_code"] == "too_short"
    assert rv["audio_path"] == "" and rv["issues"] and rv["tips"]
    # 主管逃生门：force 放行，素材照常落盘
    rv2 = prepare_reference_audio(short, ".wav", str(tmp_path), "v2", force=True)
    assert rv2["ok"] is True and Path(rv2["audio_path"]).is_file()


def test_prepare_long_audio_auto_curated(tmp_path):
    rv = prepare_reference_audio(_wav_bytes(30.0), ".wav", str(tmp_path), "v3")
    assert rv["ok"] is True
    assert rv["curated"] is True          # 30s → 自动裁到韵律最佳窗
    assert rv["duration_sec"] <= 9.5      # target 8s + 静音吸附余量
    assert Path(rv["audio_path"]).is_file()


def test_prepare_edge_silence_trimmed(tmp_path):
    rv = prepare_reference_audio(
        _wav_bytes(6.0, silence_head=3.0, silence_tail=3.0),
        ".wav", str(tmp_path), "v4")
    assert rv["ok"] is True and rv["curated"] is True
    # 裁剪产物首尾静音应基本消失（audit 同刻度）
    assert float(rv["metrics"].get("lead_silence_sec") or 0) <= 0.5
    assert float(rv["metrics"].get("trail_silence_sec") or 0) <= 0.5


def test_prepare_non_wav_converter_success(tmp_path):
    """非 WAV 源（如 ogg）经注入转换器成功 → 走同一策展链。"""
    def fake_conv(src_path: str):
        p = Path(src_path).with_suffix(".conv.wav")
        p.write_bytes(_wav_bytes(6.0))
        return str(p)

    rv = prepare_reference_audio(
        b"OggS-not-really-audio", ".ogg", str(tmp_path), "v5", converter=fake_conv)
    assert rv["ok"] is True and rv["audio_path"].endswith("v5.wav")
    # 中间产物（_src 源件 / 转换 wav）不残留
    assert not list(tmp_path.glob("*_src*")) and not list(tmp_path.glob("*.conv.wav"))


def test_prepare_convert_failed_rejects_then_force_degrades(tmp_path):
    rv = prepare_reference_audio(
        b"broken-bytes", ".ogg", str(tmp_path), "v6", converter=lambda p: None)
    assert rv["ok"] is False and rv["reject_code"] == "not_decodable"
    rv2 = prepare_reference_audio(
        b"broken-bytes", ".ogg", str(tmp_path), "v6",
        converter=lambda p: None, force=True)
    assert rv2["ok"] is True and rv2["degraded"] == "convert_failed_forced"
    assert rv2["audio_path"].endswith("v6.ogg")   # 原格式直存＝旧行为


def test_prepare_no_ffmpeg_degrades_not_blocks(tmp_path, monkeypatch):
    """ffmpeg 缺失（部署环境限制）→ 绝不阻塞登记，原格式直存 + degraded 注明。"""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    rv = prepare_reference_audio(b"OggS-x", ".ogg", str(tmp_path), "v7")
    assert rv["ok"] is True and rv["degraded"] == "no_ffmpeg"
    assert rv["audio_path"].endswith("v7.ogg")
    assert rv["level"] == "unknown"


# ── build_source_ref / 构造器透传 ────────────────────────────────────────────
def test_build_source_ref_shape():
    ref = build_source_ref(
        kind="inbox_message", media_ref="/static/protocol_media/telegram/a.ogg",
        platform="telegram", conversation_id="c1", message_id="m1",
        imported_by="admin", ts=123.0)
    assert ref["kind"] == "inbox_message" and ref["ts"] == 123.0
    assert ref["media_ref"].endswith("a.ogg")
    assert ref["imported_by"] == "admin"
    # 空字段不落键
    ref2 = build_source_ref(kind="upload")
    assert set(ref2.keys()) == {"kind", "ts"}


def test_builders_thread_consent_and_source():
    src = build_source_ref(kind="inbox_message", media_ref="/x", ts=1.0)
    vp_a = build_avatar_voice_profile(
        reference_audio_path="/a.wav", speaker_id="s",
        owner_consent=False, source_ref=src)
    vp_l = build_lan_voice_profile(
        reference_audio_path="/a.wav", speaker_id="s", base_url="http://h",
        owner_consent=False, source_ref=src)
    vp_q = build_qwen_voice_profile(
        voice="v", reference_audio_path="/a.wav",
        voice_profile_json_path="/j.json", speaker_id="s",
        owner_consent=False, source_ref=src)
    for vp in (vp_a, vp_l, vp_q):
        assert vp["owner_consent"] is False
        assert vp["source_ref"]["kind"] == "inbox_message"
    # 缺省向后兼容：owner_consent=True 且不带 source_ref 键
    vp_d = build_avatar_voice_profile(reference_audio_path="/a.wav", speaker_id="s")
    assert vp_d["owner_consent"] is True and "source_ref" not in vp_d


# ── 路由契约 ─────────────────────────────────────────────────────────────────
class _FakePM:
    def __init__(self):
        self.persona = {"id": "p1", "name": "P1"}
        self.upserted = None

    def get_persona_by_id(self, pid):
        return dict(self.persona) if pid == "p1" else None

    def upsert_profile(self, pid, persona):
        self.upserted = (pid, persona)

    def persist_profiles(self, _cm):
        return True


@pytest.fixture()
def fake_pm(monkeypatch):
    from src.utils import persona_manager as pm_mod
    pm = _FakePM()
    monkeypatch.setattr(pm_mod.PersonaManager, "get_instance",
                        classmethod(lambda _cls: pm))
    return pm


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    """protocol_media 根重定向到 tmp（绝不写仓库 static 目录）。"""
    from src.integrations import protocol_bridge as pb
    root = tmp_path / "pm"
    (root / "telegram").mkdir(parents=True)
    monkeypatch.setattr(pb, "protocol_media_root", lambda: root)
    return root


def _post_enroll(auth_client, **fields):
    data = {"persona_id": "p1", "preferred_name": "voicex", **fields}
    return auth_client.post("/api/voice/enroll", data=data, follow_redirects=False)


def test_media_ref_requires_consent_before_anything(auth_client, fake_pm):
    r = _post_enroll(auth_client, media_ref="/static/protocol_media/telegram/a.ogg")
    assert r.status_code == 400


def test_upload_explicit_consent_denied_rejected(auth_client, fake_pm):
    r = auth_client.post(
        "/api/voice/enroll",
        data={"persona_id": "p1", "preferred_name": "voicex", "owner_consent": "0"},
        files={"file": ("a.wav", io.BytesIO(_wav_bytes(4.0)), "audio/wav")},
        follow_redirects=False)
    assert r.status_code == 400


def test_media_ref_outside_whitelist_400(auth_client, fake_pm, media_root):
    r = _post_enroll(auth_client, media_ref="/static/other/a.ogg", owner_consent="1")
    assert r.status_code == 400


def test_media_ref_traversal_400(auth_client, fake_pm, media_root, tmp_path):
    # 根外真实文件 + ../ 穿越引用 → 容纳检查必须拦住
    outside = tmp_path / "secret.wav"
    outside.write_bytes(_wav_bytes(4.0))
    r = _post_enroll(
        auth_client, media_ref="/static/protocol_media/../secret.wav",
        owner_consent="1")
    assert r.status_code == 400


def test_media_ref_missing_file_404(auth_client, fake_pm, media_root):
    r = _post_enroll(
        auth_client, media_ref="/static/protocol_media/telegram/gone.ogg",
        owner_consent="1")
    assert r.status_code == 404


def test_media_ref_too_short_honest_reject(auth_client, fake_pm, media_root,
                                           tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # voice_samples 落 tmp，不污染仓库
    (media_root / "telegram" / "short.wav").write_bytes(_wav_bytes(1.5))
    r = _post_enroll(
        auth_client, media_ref="/static/protocol_media/telegram/short.wav",
        owner_consent="1")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False and d["reason"] == "ref_too_short"
    assert d["quality"]["issues"] and d["quality"]["tips"]
    assert fake_pm.upserted is None   # 拒绝时绝不写人设


def _force_cloud_path(monkeypatch):
    """双保险：即使将来测试配置开了 avatar_voice / voice_clone_lan，也绝不真打
    本机生产 7852 / LAN 主机——探活一律判不可用，落到已 mock 的云端分支。"""
    try:
        from src.ai import avatar_voice as av
        monkeypatch.setattr(av.AvatarVoiceClient, "health_ok",
                            lambda self: False, raising=False)
    except Exception:
        pass
    try:
        from src.ai import voice_clone_client as vcc
        monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok",
                            lambda self: False, raising=False)
    except Exception:
        pass


def test_media_ref_full_chain_provenance(auth_client, fake_pm, media_root,
                                         tmp_path, monkeypatch):
    """全链：media_ref → 策展 wav → 登记（云端 mock）→ vp 带 consent/溯源/逐字稿。"""
    monkeypatch.chdir(tmp_path)
    _force_cloud_path(monkeypatch)
    (media_root / "telegram" / "ok.wav").write_bytes(_wav_bytes(6.0))
    from src.ai import voice_enroll as ve
    monkeypatch.setattr(
        ve, "enroll_voice",
        lambda **kw: {"voice": "v-test-1", "target_model": "m-test"})
    r = _post_enroll(
        auth_client,
        media_ref="/static/protocol_media/telegram/ok.wav",
        owner_consent="1", reference_text="你好，这是逐字稿",
        platform="telegram", conversation_id="conv-9", message_id="msg-7")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["voice"] == "v-test-1"
    assert d["quality"]["level"] in ("ok", "warn")
    # 策展产物：voice_samples/<safe>.wav + 逐字稿 sidecar
    ref_path = Path(d["reference_audio_path"])
    assert ref_path.is_file() and ref_path.suffix == ".wav"
    assert ref_path.with_suffix(".txt").read_text(
        encoding="utf-8") == "你好，这是逐字稿"
    # voice_profile 溯源
    pid, persona = fake_pm.upserted
    vp = persona["voice_profile"]
    assert pid == "p1"
    assert vp["owner_consent"] is True
    assert vp["source_ref"]["kind"] == "inbox_message"
    assert vp["source_ref"]["conversation_id"] == "conv-9"
    assert vp["source_ref"]["message_id"] == "msg-7"
    assert vp["source_ref"]["media_ref"].endswith("ok.wav")
    assert vp["source_ref"].get("imported_by")


def test_upload_path_backward_compatible(auth_client, fake_pm, tmp_path, monkeypatch):
    """旧口径（纯文件上传、不带 consent 字段）行为不变：登记成功、默认已确认。"""
    monkeypatch.chdir(tmp_path)
    _force_cloud_path(monkeypatch)
    from src.ai import voice_enroll as ve
    monkeypatch.setattr(
        ve, "enroll_voice",
        lambda **kw: {"voice": "v-test-2", "target_model": "m-test"})
    r = auth_client.post(
        "/api/voice/enroll",
        data={"persona_id": "p1", "preferred_name": "voicey"},
        files={"file": ("b.wav", io.BytesIO(_wav_bytes(5.0)), "audio/wav")},
        follow_redirects=False)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    _pid, persona = fake_pm.upserted
    vp = persona["voice_profile"]
    assert vp["owner_consent"] is True
    assert vp["source_ref"]["kind"] == "upload"
