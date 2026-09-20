"""P2（2026-08-18）：视频翻译（ffmpeg 抽音轨 → ASR → 翻译）单测。

护栏优先：ffmpeg 缺失软失败 / 时长上限拒绝（不静默截断）/ 全进程并发=1 /
无音轨视频如实失败 / mime 与文件名扩展双兜底。真 ffmpeg 用例生成 1 秒合成
视频（lavfi sine+color），缺 ffmpeg 自动 skip。
"""
import asyncio
import base64
import os
import subprocess

import pytest

from src.ai import video_translate as vt
from src.ai.video_translate import (
    VideoTranslateService,
    decode_video_to_temp,
    resolve_video_cfg,
)
from src.inbox.media_resolver import media_kind, resolve_for_translate

_HAS_FFMPEG = vt.ffmpeg_available()


class _StubVoice:
    """记录调用的假 VoiceTranslateService（并发计数器验证串行锁）。"""

    def __init__(self, delay: float = 0.0):
        self.calls = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._delay = delay

    async def translate_voice(self, path, *, target_lang="zh", source_lang="",
                              style="chat", want_segments=False):
        assert os.path.isfile(path), "抽出的 wav 在调用期必须存在"
        self.calls.append(path)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        if self._delay:
            await asyncio.sleep(self._delay)
        self.concurrent -= 1
        return {"ok": True, "transcript": "hello world",
                "translation": {"ok": True, "translated_text": "你好世界"}}


# ── media_resolver 归类 ────────────────────────────────────────────────


def test_media_kind_video_mappings():
    assert media_kind({"media_type": "video", "media_ref": "x.bin"}) == "video"
    assert media_kind({"media_type": "video_note", "media_ref": "x"}) == "video"
    assert media_kind({"media_type": "", "media_ref": "clip.mov"}) == "video"
    assert media_kind({"media_type": "", "media_ref": "a/b.mkv"}) == "video"
    # 扩展名推断层 .mp4/.webm 保持归 voice（存量语音备忘容器，刻意钉住不挪）
    assert media_kind({"media_type": "", "media_ref": "memo.mp4"}) == "voice"
    assert media_kind({"media_type": "", "media_ref": "memo.webm"}) == "voice"


def test_resolve_for_translate_video_remote_and_missing(tmp_path):
    _, kind, reason = resolve_for_translate(
        {"media_type": "video", "media_ref": "https://cdn.example/x.mp4"})
    assert kind == "video" and reason == "remote_unsupported"
    p = tmp_path / "v.mov"
    p.write_bytes(b"x")
    path, kind, reason = resolve_for_translate(
        {"media_type": "video", "media_ref": str(p)})
    assert reason == "ok" and kind == "video" and path == str(p)


# ── 护栏：ffmpeg 缺失 / 时长上限 / 并发=1 ─────────────────────────────


async def test_ffmpeg_unavailable_soft_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(vt.shutil, "which", lambda *_: None)
    svc = VideoTranslateService(_StubVoice())
    out = await svc.translate_video(str(tmp_path / "v.mp4"), target_lang="zh")
    assert out["ok"] is False and out["reason"] == "ffmpeg_unavailable"


async def test_too_long_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(vt, "probe_duration_sec", lambda _p: 16 * 60.0)
    stub = _StubVoice()
    svc = VideoTranslateService(stub, max_minutes=15)
    out = await svc.translate_video(str(tmp_path / "v.mp4"), target_lang="zh")
    assert out["ok"] is False and out["reason"] == "too_long"
    assert not stub.calls, "超限必须在抽轨/ASR 之前拒绝"


async def test_concurrency_serialized(monkeypatch, tmp_path):
    """两个并发视频任务必须串行过信号量（GPU 保护不变量）。"""
    def _fake_extract(_p):
        fd, out = __import__("tempfile").mkstemp(suffix=".wav")
        os.write(fd, b"RIFF" + b"\x00" * 60)
        os.close(fd)
        return out, "ok"

    monkeypatch.setattr(vt.shutil, "which", lambda *_: "ffmpeg")
    monkeypatch.setattr(vt, "probe_duration_sec", lambda _p: 1.0)
    monkeypatch.setattr(vt, "extract_audio_wav_sync", _fake_extract)
    stub = _StubVoice(delay=0.08)
    svc = VideoTranslateService(stub)
    await asyncio.gather(
        svc.translate_video(str(tmp_path / "a.mp4"), target_lang="zh"),
        svc.translate_video(str(tmp_path / "b.mp4"), target_lang="zh"),
    )
    assert len(stub.calls) == 2
    assert stub.max_concurrent == 1, "视频任务必须全进程串行"


# ── decode / cfg 纯函数 ───────────────────────────────────────────────


def _b64(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def test_decode_video_mime_and_filename_fallback():
    p, r = decode_video_to_temp(_b64(b"abc", "video/mp4"))
    assert p and r == "ok" and p.endswith(".mp4")
    os.remove(p)
    # 浏览器对 .mkv 常给 octet-stream → 按 filename 扩展兜底
    p, r = decode_video_to_temp(_b64(b"abc", "application/octet-stream"),
                                filename="clip.mkv")
    assert p and r == "ok" and p.endswith(".mkv")
    os.remove(p)
    p, r = decode_video_to_temp(_b64(b"abc", "application/octet-stream"),
                                filename="evil.exe")
    assert p is None and r.startswith("unsupported_mime")
    p, r = decode_video_to_temp(_b64(b"x" * (2 * 1024 * 1024), "video/mp4"), max_mb=1)
    assert p is None and r == "too_large"


def test_resolve_video_cfg_defaults():
    assert resolve_video_cfg({}) == {"enabled": False, "max_mb": 50, "max_minutes": 15}
    got = resolve_video_cfg({"media": {"video_translate": {
        "enabled": True, "max_mb": 20, "max_minutes": 5}}})
    assert got == {"enabled": True, "max_mb": 20, "max_minutes": 5}


# ── 真 ffmpeg 端到端（缺 ffmpeg 自动 skip）────────────────────────────


def _gen_video(tmp_path, *, with_audio: bool) -> str:
    out = str(tmp_path / ("a.mp4" if with_audio else "s.mp4"))
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest"]
    cmd += ["-pix_fmt", "yuv420p", out]
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    if r.returncode != 0:
        pytest.skip(f"ffmpeg 合成测试视频失败: {(r.stderr or b'')[-120:]!r}")
    return out


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg 不可用")
async def test_happy_path_real_extract(tmp_path):
    video = _gen_video(tmp_path, with_audio=True)
    stub = _StubVoice()
    svc = VideoTranslateService(stub)
    out = await svc.translate_video(video, target_lang="zh", source_lang="en")
    assert out["ok"] is True
    assert out["media_kind"] == "video"
    assert out.get("video_duration_sec", 0) > 0
    assert out["translation"]["translated_text"] == "你好世界"
    assert len(stub.calls) == 1 and stub.calls[0].endswith(".wav")
    assert not os.path.isfile(stub.calls[0]), "抽出的 wav 用完必须清理"


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg 不可用")
async def test_no_audio_track_honest_fail(tmp_path):
    video = _gen_video(tmp_path, with_audio=False)
    stub = _StubVoice()
    svc = VideoTranslateService(stub)
    out = await svc.translate_video(video, target_lang="zh")
    assert out["ok"] is False and out["reason"] == "no_audio_track"
    assert not stub.calls
