# -*- coding: utf-8 -*-
"""
视频后处理器 — 使用 MoviePy 合成最终发布视频。

功能:
1. 图文混剪合成：图片序列 + 配音 + 字幕 + BGM → 最终视频
2. AI视频后处理：添加字幕 + 水印 + 格式适配
3. 竖版格式化：任意比例 → 9:16 (TikTok/Instagram)
4. 平台格式适配：不同分辨率和时长要求
"""

import os
import uuid
import logging
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

# MoviePy 2.x imports
try:
    from moviepy import (
        VideoFileClip,
        ImageClip,
        AudioFileClip,
        TextClip,
        CompositeVideoClip,
        concatenate_videoclips,
        ColorClip,
    )
    MOVIEPY_AVAILABLE = True
except ImportError:
    logger.warning("moviepy not installed. Video processing disabled.")
    MOVIEPY_AVAILABLE = False

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# ---------------------------------------------------------------------------
# Platform specs
# ---------------------------------------------------------------------------

PLATFORM_SPECS: Dict[str, Dict[str, Any]] = {
    "tiktok":       {"width": 1080, "height": 1920, "max_duration": 60,  "fps": 30},
    "instagram":    {"width": 1080, "height": 1920, "max_duration": 90,  "fps": 30},
    "xiaohongshu":  {"width": 1080, "height": 1080, "max_duration": 60,  "fps": 30},
    "linkedin":     {"width": 1920, "height": 1080, "max_duration": 600, "fps": 30},
    "telegram":     {"width": 1280, "height": 720,  "max_duration": 120, "fps": 30},
}


def get_platform_spec(platform: str) -> dict:
    """返回平台规格参数。"""
    spec = PLATFORM_SPECS.get(platform.lower())
    if spec is None:
        logger.warning("Unknown platform '%s', falling back to tiktok spec.", platform)
        spec = PLATFORM_SPECS["tiktok"]
    return dict(spec)


# 别名，兼容外部调用
get_platform_specs = get_platform_spec


def _ensure_output_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _default_output(suffix: str = ".mp4") -> str:
    """生成默认输出路径 data/studio/final/{uuid}.mp4"""
    base = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "studio", "final")
    )
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{uuid.uuid4().hex}{suffix}")


def _make_text_clip(text: str, width: int, fontsize: int = 48,
                    duration: float = 3.0, position: str = "bottom") -> Optional[Any]:
    """创建带黑色阴影的白色字幕 Clip，失败时返回 None。"""
    try:
        txt = TextClip(
            text=text,
            font_size=fontsize,
            color="white",
            stroke_color="black",
            stroke_width=2,
            method="caption",
            size=(width - 40, None),
        ).with_duration(duration)
        return txt
    except Exception as exc:
        logger.warning("TextClip failed (ImageMagick required?): %s", exc)
        return None


def _crop_to_aspect(clip: Any, target_w: int, target_h: int) -> Any:
    """将 clip 裁剪 / 填充为目标分辨率。"""
    src_w, src_h = clip.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h

    if abs(src_ratio - target_ratio) < 0.01:
        return clip.resized((target_w, target_h))

    if src_ratio > target_ratio:
        # 源视频更宽 — 缩放高度后裁剪宽度
        new_h = target_h
        new_w = int(src_ratio * new_h)
        resized = clip.resized((new_w, new_h))
        x1 = (new_w - target_w) // 2
        return resized.cropped(x1=x1, y1=0, x2=x1 + target_w, y2=target_h)
    else:
        # 源视频更高 — 缩放宽度后裁剪高度
        new_w = target_w
        new_h = int(new_w / src_ratio)
        resized = clip.resized((new_w, new_h))
        y1 = (new_h - target_h) // 2
        return resized.cropped(x1=0, y1=y1, x2=target_w, y2=y1 + target_h)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_slideshow(
    image_paths: List[str],
    audio_path: Optional[str] = None,
    output_path: Optional[str] = None,
    duration_per_image: float = 2.5,
    platform: str = "tiktok",
    caption_text: Optional[str] = None,
    bgm_path: Optional[str] = None,
) -> str:
    """
    从图片列表合成幻灯片视频。

    - 每张图片展示 duration_per_image 秒，含 Ken Burns 缩放效果
    - 如有 audio_path，以其时长为准调整幻灯片总时长
    - bgm 混入音量 20%
    - caption_text 显示前 3 秒（底部白色+黑色阴影）
    - 返回输出文件路径
    """
    if not MOVIEPY_AVAILABLE:
        raise RuntimeError("moviepy is not installed.")

    spec = get_platform_spec(platform)
    w, h, fps = spec["width"], spec["height"], spec["fps"]
    output_path = output_path or _default_output()
    _ensure_output_dir(output_path)

    # 计算每张图时长
    if audio_path and os.path.exists(audio_path):
        try:
            vo_clip = AudioFileClip(audio_path)
            total_duration = vo_clip.duration
            vo_clip.close()
        except Exception:
            total_duration = len(image_paths) * duration_per_image
    else:
        total_duration = len(image_paths) * duration_per_image

    per_image = total_duration / max(len(image_paths), 1)

    clips = []
    for img_path in image_paths:
        if not os.path.exists(img_path):
            logger.warning("Image not found: %s — skipping.", img_path)
            continue
        try:
            img_clip = ImageClip(img_path).with_duration(per_image)
            # 裁剪到目标尺寸
            img_clip = _crop_to_aspect(img_clip, w, h)

            # Ken Burns 缩放效果 (1.0 → 1.08)
            if NUMPY_AVAILABLE:
                def make_zoom(clip_ref):
                    def zoom_effect(t):
                        scale = 1.0 + 0.08 * (t / clip_ref.duration)
                        new_w = int(clip_ref.w * scale)
                        new_h = int(clip_ref.h * scale)
                        frame = clip_ref.get_frame(t)
                        # 简单返回原帧，避免复杂依赖
                        return frame
                    return zoom_effect
                # 使用 resized 做轻量 Ken Burns
                img_clip = img_clip.resized(lambda t: 1.0 + 0.04 * t / per_image)

            clips.append(img_clip)
        except Exception as exc:
            logger.error("Failed to process image %s: %s", img_path, exc)

    if not clips:
        raise ValueError("No valid images found in image_paths.")

    video = concatenate_videoclips(clips, method="compose")

    # 混音
    audio_clips = []
    if audio_path and os.path.exists(audio_path):
        try:
            vo = AudioFileClip(audio_path).with_duration(video.duration)
            audio_clips.append(vo)
        except Exception as exc:
            logger.warning("Voiceover load failed: %s", exc)

    if bgm_path and os.path.exists(bgm_path):
        try:
            from moviepy import AudioFileClip as AFC
            bgm = AFC(bgm_path).with_duration(video.duration).multiply_volume(0.2)
            audio_clips.append(bgm)
        except Exception as exc:
            logger.warning("BGM load failed: %s", exc)

    if audio_clips:
        try:
            from moviepy import CompositeAudioClip
            mixed = CompositeAudioClip(audio_clips)
            video = video.with_audio(mixed)
        except Exception as exc:
            logger.warning("Audio mix failed: %s", exc)

    # 字幕叠加
    layers = [video]
    if caption_text:
        txt_clip = _make_text_clip(caption_text, w, fontsize=48, duration=3.0)
        if txt_clip is not None:
            txt_clip = txt_clip.with_position(("center", h - 200))
            layers.append(txt_clip)

    if len(layers) > 1:
        final = CompositeVideoClip(layers)
    else:
        final = video

    logger.info("Writing slideshow to %s", output_path)
    final.write_videofile(
        output_path,
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        logger=None,
    )
    final.close()
    return output_path


def process_ai_video(
    video_path: str,
    caption: Optional[str] = None,
    output_path: Optional[str] = None,
    platform: str = "tiktok",
    add_cta_overlay: bool = True,
    cta_text: Optional[str] = None,
) -> str:
    """
    对 AI 生成视频进行后处理：格式适配 + 字幕 + CTA。

    返回输出文件路径。
    """
    if not MOVIEPY_AVAILABLE:
        raise RuntimeError("moviepy is not installed.")

    spec = get_platform_spec(platform)
    w, h, fps = spec["width"], spec["height"], spec["fps"]
    output_path = output_path or _default_output()
    _ensure_output_dir(output_path)

    video = VideoFileClip(video_path)
    video = _crop_to_aspect(video, w, h)

    layers = [video]

    # 底部字幕
    if caption:
        txt = _make_text_clip(caption, w, fontsize=42, duration=video.duration)
        if txt is not None:
            txt = txt.with_position(("center", h - 180))
            layers.append(txt)

    # CTA 叠加（最后 3 秒）
    if add_cta_overlay:
        cta = cta_text or "Link in bio ↑"
        cta_duration = min(3.0, video.duration)
        cta_start = max(0.0, video.duration - cta_duration)
        cta_clip = _make_text_clip(cta, w, fontsize=52, duration=cta_duration)
        if cta_clip is not None:
            cta_clip = cta_clip.with_start(cta_start).with_position(("center", h // 2))
            layers.append(cta_clip)

    final = CompositeVideoClip(layers) if len(layers) > 1 else video

    logger.info("Writing processed video to %s", output_path)
    final.write_videofile(
        output_path,
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        logger=None,
    )
    final.close()
    video.close()
    return output_path


def add_subtitles_from_script(
    video_path: str,
    script_lines: List[dict],
    output_path: Optional[str] = None,
) -> str:
    """
    根据脚本时间轴叠加字幕。

    script_lines 格式: [{"text": "...", "start": 0.0, "end": 2.5}, ...]
    白色文字 + 底部 20% 黑色背景条。
    """
    if not MOVIEPY_AVAILABLE:
        raise RuntimeError("moviepy is not installed.")

    output_path = output_path or _default_output()
    _ensure_output_dir(output_path)

    video = VideoFileClip(video_path)
    w, h = video.size
    layers = [video]

    for line in script_lines:
        text = line.get("text", "").strip()
        start = float(line.get("start", 0.0))
        end = float(line.get("end", start + 2.0))
        duration = end - start
        if not text or duration <= 0:
            continue

        # 黑色背景条（底部 20%）
        try:
            bg = ColorClip(size=(w, h // 5), color=(0, 0, 0)).with_opacity(0.6)
            bg = bg.with_duration(duration).with_start(start).with_position((0, h * 4 // 5))
            layers.append(bg)
        except Exception:
            pass

        txt_clip = _make_text_clip(text, w, fontsize=40, duration=duration)
        if txt_clip is not None:
            txt_clip = txt_clip.with_start(start).with_position(("center", h * 4 // 5 + 10))
            layers.append(txt_clip)

    final = CompositeVideoClip(layers) if len(layers) > 1 else video
    logger.info("Writing subtitled video to %s", output_path)
    final.write_videofile(
        output_path,
        fps=video.fps or 30,
        codec="libx264",
        audio_codec="aac",
        logger=None,
    )
    final.close()
    video.close()
    return output_path


def resize_to_platform(
    video_path: str,
    platform: str,
    output_path: Optional[str] = None,
) -> str:
    """将视频调整为平台规格，必要时裁剪。返回输出路径。"""
    if not MOVIEPY_AVAILABLE:
        raise RuntimeError("moviepy is not installed.")

    spec = get_platform_spec(platform)
    w, h, fps = spec["width"], spec["height"], spec["fps"]
    output_path = output_path or _default_output()
    _ensure_output_dir(output_path)

    video = VideoFileClip(video_path)
    video = _crop_to_aspect(video, w, h)

    logger.info("Resizing video for platform '%s' → %s", output_path, platform)
    video.write_videofile(
        output_path,
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        logger=None,
    )
    video.close()
    return output_path
