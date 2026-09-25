"""P2（2026-08-18）：视频翻译——ffmpeg 抽音轨 → ASR 转写 → 翻译。

组合复用 :class:`VoiceTranslateService`（ASR 结果缓存 / 语言检测 / 术语翻译一条不
重造）；本模块只负责「视频容器 → 16kHz 单声道 wav」与三道护栏：

- **ffmpeg/ffprobe 软探测**：缺失 → ok=False 软失败，绝不抛给上层；
- **时长上限**（默认 15 分钟）：ffprobe 先探，超限拒绝并给诚实文案——刻意不做
  「静默截断前 N 分钟」（部分结果冒充完整结果比拒绝更糟）；
- **全进程并发=1**（模块级信号量）：抽轨 CPU + ASR GPU 都不便宜，视频任务串行，
  绝不挤占生产语音链（voice_reply / 坐席语音）的 176 卡。

**SRT 字幕 v1 刻意不产出**：ASR 契约（``TranscribeResult``）只回整段文本、无分段
时间戳（本地 faster-whisper 分段 timing 在管线里被折叠，远程 176 服务只回 text）——
伪造时间轴＝撒谎。打通路径（P3）＝audio_pipeline 透传 ``segments[{start,end,text}]``
+ 176 服务 verbose_json，届时接 ``translate_subtitle`` 产双语字幕。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 模块级串行锁：全进程同一时刻至多一个视频任务在抽轨/转写（GPU/CPU 保护）。
_VIDEO_SEM = asyncio.Semaphore(1)

_MAX_VIDEO_MB_DEFAULT = 50
_MAX_MINUTES_DEFAULT = 15
_EXTRACT_TIMEOUT_SEC = 300   # ffmpeg 抽轨兜底超时（时长探测失败时的最后防线）

_ALLOWED_VIDEO_MIME = {
    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm",
    "video/x-msvideo", "video/mpeg", "video/3gpp", "video/x-m4v",
}
_MIME_SUFFIX = {
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    "video/webm": ".webm", "video/x-msvideo": ".avi", "video/mpeg": ".mpg",
    "video/3gpp": ".3gp", "video/x-m4v": ".m4v",
}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe_duration_sec(path: str) -> Optional[float]:
    """ffprobe 探视频时长（秒）。探不到（缺 ffprobe/坏文件）→ None，调用方按兜底超时走。"""
    if not ffprobe_available():
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        val = float((r.stdout or "").strip().splitlines()[0])
        return val if val > 0 else None
    except Exception:
        return None


def _has_audio_stream(path: str) -> Optional[bool]:
    """ffprobe 探有无音频流。缺 ffprobe/探测失败 → None（不下结论，交给 ffmpeg 实跑）。"""
    if not ffprobe_available():
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        return bool((r.stdout or "").strip())
    except Exception:
        return None


def extract_audio_wav_sync(video_path: str) -> Tuple[Optional[str], str]:
    """ffmpeg 抽音轨为 16kHz 单声道 wav（ASR 标准口粮）。返回 (wav_path|None, reason)。

    同步实现（调用方经 to_thread 包）；产物由调用方负责清理。
    """
    if not ffmpeg_available():
        return None, "ffmpeg_unavailable"
    if _has_audio_stream(video_path) is False:
        return None, "no_audio_track"   # 无声视频：ffmpeg 会以非零退出，先探先答语义更准
    fd, out = tempfile.mkstemp(prefix="videoxl_", suffix=".wav")
    os.close(fd)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1",
             "-ar", "16000", "-f", "wav", out],
            capture_output=True, timeout=_EXTRACT_TIMEOUT_SEC)
        if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) <= 44:
            # 44 = 空 WAV 头：无音轨的视频（如 GIF 转制）在此如实失败
            try:
                os.remove(out)
            except Exception:
                pass
            tail = (r.stderr or b"")[-300:].decode("utf-8", "replace")
            logger.debug("[video-xlate] 抽轨失败 rc=%s tail=%s", r.returncode, tail)
            return None, "no_audio_track" if r.returncode == 0 else "extract_failed"
        return out, "ok"
    except subprocess.TimeoutExpired:
        try:
            os.remove(out)
        except Exception:
            pass
        return None, "extract_timeout"
    except Exception as exc:  # noqa: BLE001
        try:
            os.remove(out)
        except Exception:
            pass
        return None, f"extract_error:{type(exc).__name__}"


_VIDEO_FILE_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mpg", ".mpeg",
                   ".3gp", ".wmv", ".flv", ".webm"}


def decode_video_to_temp(video_b64: str, *, max_mb: int = _MAX_VIDEO_MB_DEFAULT,
                         filename: str = "") -> Tuple[Optional[str], str]:
    """（可带 data URL 头的）base64 视频落临时文件。返回 (path|None, reason)。

    浏览器对 .mkv 等给 ``application/octet-stream``（File.type 缺失）——此时按
    ``filename`` 扩展名白名单兜底；两头都认不出才拒绝。
    """
    raw = str(video_b64 or "").strip()
    if not raw:
        return None, "empty"
    mime = "video/mp4"
    if raw.startswith("data:"):
        header, _, payload = raw.partition(",")
        if not payload:
            return None, "bad_data_url"
        mime = header[5:].split(";")[0].strip().lower() or mime
        raw = payload
    ext = os.path.splitext(str(filename or "").lower())[1]
    if mime in _ALLOWED_VIDEO_MIME:
        suffix = _MIME_SUFFIX.get(mime, ".mp4")
    elif mime in ("application/octet-stream", "") and ext in _VIDEO_FILE_EXT:
        suffix = ext
    else:
        return None, f"unsupported_mime:{mime}"
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        return None, "decode_failed"
    if not data:
        return None, "empty_after_decode"
    if len(data) > max(1, int(max_mb)) * 1024 * 1024:
        return None, "too_large"
    try:
        fd, path = tempfile.mkstemp(prefix="videoxl_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path, "ok"
    except Exception as exc:  # noqa: BLE001
        return None, f"write_failed:{type(exc).__name__}"


class VideoTranslateService:
    """视频 → 抽音轨 → :class:`VoiceTranslateService`（转写+翻译）。"""

    def __init__(self, voice_service: Any, *, max_minutes: int = _MAX_MINUTES_DEFAULT) -> None:
        self._voice = voice_service
        self._max_minutes = max(1, int(max_minutes))

    async def translate_video(
        self,
        video_path: str,
        *,
        target_lang: str = "zh",
        source_lang: str = "",
        style: str = "chat",
        want_segments: bool = False,
    ) -> Dict[str, Any]:
        # 时长已知且超限时先拒绝：CI / 客户机没有 ffmpeg 时，不能把「视频过长」
        # 报成 ffmpeg_unavailable（probe 可被调用方替换，不依赖本机 ffprobe）。
        dur = await asyncio.to_thread(probe_duration_sec, video_path)
        if dur is not None and dur > self._max_minutes * 60:
            return {"ok": False, "reason": "too_long",
                    "message": f"视频过长（{dur/60:.1f} 分钟，上限 {self._max_minutes} 分钟）",
                    "video_duration_sec": dur}
        if not ffmpeg_available():
            return {"ok": False, "reason": "ffmpeg_unavailable",
                    "message": "服务器未安装 ffmpeg，无法抽取视频音轨"}
        async with _VIDEO_SEM:
            wav, reason = await asyncio.to_thread(extract_audio_wav_sync, video_path)
            if wav is None:
                msg = {
                    "no_audio_track": "视频没有音轨（无声视频无法转写）",
                    "extract_timeout": "音轨抽取超时",
                    "ffmpeg_unavailable": "服务器未安装 ffmpeg，无法抽取视频音轨",
                }.get(reason, "音轨抽取失败")
                return {"ok": False, "reason": reason, "message": msg}
            try:
                out = await self._voice.translate_voice(
                    wav, target_lang=target_lang, source_lang=source_lang,
                    style=style, want_segments=want_segments)
            finally:
                try:
                    os.remove(wav)
                except Exception:
                    pass
        out["media_kind"] = "video"
        if dur is not None:
            out["video_duration_sec"] = dur
        return out


def resolve_video_cfg(full_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """config.media.video_translate 解析（默认关；max_mb/max_minutes 带缺省）。"""
    try:
        vc = dict(((full_cfg or {}).get("media") or {}).get("video_translate") or {})
    except Exception:
        vc = {}
    return {
        "enabled": bool(vc.get("enabled", False)),
        "max_mb": int(vc.get("max_mb", _MAX_VIDEO_MB_DEFAULT) or _MAX_VIDEO_MB_DEFAULT),
        "max_minutes": int(vc.get("max_minutes", _MAX_MINUTES_DEFAULT) or _MAX_MINUTES_DEFAULT),
    }


__all__ = [
    "VideoTranslateService", "decode_video_to_temp", "extract_audio_wav_sync",
    "probe_duration_sec", "ffmpeg_available", "ffprobe_available",
    "resolve_video_cfg",
]
