"""入站视频关键帧抽取 + 宫格拼图（供 Vision 图像理解模型"看懂"视频内容用）。

为什么这么做
============
视觉大模型（qwen2.5vl 等）吃的是图片，不能整段吞视频。业界通用做法是从视频里
**均匀抽若干关键帧、拼成一张宫格图**交给模型一次识别——既保留时间维度的信息
（先后画面），又把视频理解成本压到「一次图片识别」，比逐帧调用省 token/时延。

依赖
====
- 外部 ``ffmpeg`` / ``ffprobe``（本仓库 voice_sender / avatar_voice 已在用）。
- ``Pillow`` 拼图。

全部**软失败**：任一环节缺依赖/异常 → 返回 None，调用方回落占位「[视频]」，
绝不抛异常打断入站主链路。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def ffmpeg_available() -> bool:
    """ffmpeg 与 ffprobe 是否都在 PATH 上。"""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def has_audio_stream(video_path: str) -> bool:
    """ffprobe 探测视频是否含音频流（无音轨的静音视频/GIF 跳过 ASR，省时）。"""
    if shutil.which("ffprobe") is None:
        return False
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=20,
        )
        return "audio" in (out.stdout or "")
    except Exception:
        return False


def extract_audio_wav(video_path: str, out_wav: str) -> Optional[str]:
    """抽视频音轨为 16k 单声道 WAV（喂 ASR 最稳格式）。无音轨/失败返回 None。"""
    if shutil.which("ffmpeg") is None:
        return None
    if not has_audio_stream(video_path):
        return None
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", str(video_path),
             "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", str(out_wav)],
            capture_output=True, timeout=120,
        )
        p = Path(out_wav)
        # WAV 头 44 字节；> 1KB 才算有实际采样（滤掉空轨）
        if p.exists() and p.stat().st_size > 1024:
            return str(out_wav)
        return None
    except Exception:
        return None


def _probe_duration(video_path: str) -> float:
    """用 ffprobe 取视频时长（秒）；拿不到返回 0.0（调用方按未知时长降级）。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=20,
        )
        return max(0.0, float((out.stdout or "").strip()))
    except Exception:
        return 0.0


def _grab_frame(video_path: str, ts: float, out_path: str) -> bool:
    """在 ``ts`` 秒处抽 1 帧存到 ``out_path``；成功返回 True。

    ``-ss`` 置于 ``-i`` 前用关键帧快速定位（够快且对内容概览足够精准）。
    """
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", f"{max(0.0, ts):.2f}",
             "-i", str(video_path), "-frames:v", "1", "-q:v", "3",
             str(out_path)],
            capture_output=True, timeout=30,
        )
        p = Path(out_path)
        return p.exists() and p.stat().st_size > 0
    except Exception:
        return False


def extract_frames_montage(
    video_path: str,
    out_path: str,
    *,
    frames: int = 4,
    cell_width: int = 512,
    cols: int = 2,
) -> Optional[Tuple[str, float, int]]:
    """抽 ``frames`` 帧（时间上均匀分布）拼成宫格图存 ``out_path``。

    返回 ``(montage_path, duration_sec, n_frames)``；失败返回 None。

    抽帧点避开纯黑首尾帧（取 8%~92% 区间均匀分布）；时长未知时从 0 起每秒 1 帧。
    """
    if not ffmpeg_available():
        logger.warning("[video_frames] ffmpeg/ffprobe 不可用，跳过视频抽帧")
        return None
    try:
        from PIL import Image
    except Exception:
        logger.warning("[video_frames] Pillow 不可用，跳过视频抽帧")
        return None

    dur = _probe_duration(video_path)
    n = max(1, int(frames))
    if dur > 0:
        if n == 1:
            points = [dur * 0.5]
        else:
            lo, hi = dur * 0.08, dur * 0.92
            step = (hi - lo) / (n - 1)
            points = [lo + i * step for i in range(n)]
    else:
        points = [float(i) for i in range(n)]

    tmpdir = Path(tempfile.mkdtemp(prefix="vframes_"))
    frame_files: List[str] = []
    try:
        for i, ts in enumerate(points):
            fp = tmpdir / f"f{i}.jpg"
            if _grab_frame(video_path, ts, str(fp)):
                frame_files.append(str(fp))
        if not frame_files:
            logger.warning("[video_frames] 未抽到任何帧: %s", video_path)
            return None

        pil_imgs = []
        for fp in frame_files:
            try:
                im = Image.open(fp).convert("RGB")
                w, h = im.size
                if w > cell_width and w > 0:
                    im = im.resize((cell_width, max(1, int(h * cell_width / w))))
                pil_imgs.append(im)
            except Exception:
                logger.debug("[video_frames] 帧读取失败: %s", fp, exc_info=True)
        if not pil_imgs:
            return None

        if len(pil_imgs) == 1:
            pil_imgs[0].save(out_path, "JPEG", quality=85)
            return (out_path, dur, 1)

        ncols = min(max(1, int(cols)), len(pil_imgs))
        nrows = (len(pil_imgs) + ncols - 1) // ncols
        cell_w = max(im.size[0] for im in pil_imgs)
        cell_h = max(im.size[1] for im in pil_imgs)
        canvas = Image.new("RGB", (cell_w * ncols, cell_h * nrows), (16, 16, 16))
        for idx, im in enumerate(pil_imgs):
            r, c = divmod(idx, ncols)
            canvas.paste(im, (c * cell_w, r * cell_h))
        canvas.save(out_path, "JPEG", quality=85)
        return (out_path, dur, len(pil_imgs))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


__all__ = [
    "ffmpeg_available", "extract_frames_montage",
    "has_audio_stream", "extract_audio_wav",
]
