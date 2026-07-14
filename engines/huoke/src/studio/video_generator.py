# -*- coding: utf-8 -*-
"""
AI视频生成器 — 使用 fal.ai Wan2.6 文生视频。

成本: ~$0.08/秒 × 15秒 ≈ $1.20/条视频
用途: TikTok/Instagram Reels 的高质量视频内容
模型: wan/v2.6/text-to-video (当前最优性价比)
"""

import logging
import os
import uuid
from pathlib import Path
from typing import Callable, Optional

import requests

from src.host.device_registry import config_file, data_dir

logger = logging.getLogger(__name__)

# 视频输出目录（项目根 data/studio/videos）
VIDEOS_DIR = data_dir() / "studio" / "videos"

# fal.ai 视频生成模型
VIDEO_MODEL = "wan/v2.6/text-to-video"
IMAGE_TO_VIDEO_MODEL = "wan/v2.1/i2v-480p"   # 更便宜的图生视频模型

# 成本估算（美元/秒）
_COST_PER_SECOND = {
    "standard": 0.08,   # wan/v2.6/text-to-video
    "premium":  0.15,   # 高质量/更慢模型
    "budget":   0.04,   # 低分辨率/快速模型
}

# 平台 → 宽高比映射
_PLATFORM_RATIO = {
    "tiktok":      "9:16",
    "instagram":   "9:16",
    "facebook":    "9:16",
    "whatsapp":    "9:16",
    "linkedin":    "16:9",
    "twitter":     "16:9",
    "youtube":     "16:9",
    "xiaohongshu": "1:1",
    "rednote":     "1:1",
}


def get_aspect_ratio_for_platform(platform: str) -> str:
    """根据目标平台返回推荐的视频宽高比。

    Args:
        platform: 平台名称（大小写不敏感），如 "tiktok"、"youtube"、"xiaohongshu"

    Returns:
        宽高比字符串，如 "9:16"、"16:9"、"1:1"；未知平台默认 "9:16"。
    """
    ratio = _PLATFORM_RATIO.get(platform.lower().strip(), "9:16")
    logger.debug("平台 '%s' → 宽高比 %s", platform, ratio)
    return ratio


def estimate_cost(duration: int, model: str = "standard") -> float:
    """估算视频生成费用（美元）。

    Args:
        duration: 视频时长（秒）
        model:    计费档位，"standard"（默认）/ "premium" / "budget"

    Returns:
        预估费用，单位美元（USD）。
    """
    rate = _COST_PER_SECOND.get(model, _COST_PER_SECOND["standard"])
    cost = round(duration * rate, 4)
    logger.debug("费用估算 | 时长=%ds | 档位=%s | 费用=$%.4f", duration, model, cost)
    return cost


def _get_fal_key() -> str:
    """内部辅助：获取 FAL_KEY 环境变量或配置文件中的 API Key。"""
    key = os.environ.get("FAL_KEY", "").strip()
    if key:
        return key
    config_path = config_file("fal_key.txt")
    if config_path.exists():
        key = config_path.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise EnvironmentError(
        "未找到 FAL_KEY。请设置环境变量 FAL_KEY 或将 Key 写入 config/fal_key.txt"
    )


def _download_video(url: str, dest: Path) -> str:
    """下载视频文件并保存到本地路径。"""
    try:
        resp = requests.get(url, timeout=300, stream=True)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info("视频已保存 → %s", dest)
        return str(dest)
    except Exception as exc:
        logger.error("视频下载失败: %s", exc)
        raise RuntimeError(f"视频下载失败: {exc}") from exc


def generate_video(
    prompt: str,
    duration: int = 15,
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
    output_path: Optional[str] = None,
    seed: Optional[int] = None,
    on_progress: Optional[Callable] = None,
) -> str:
    """使用 Wan2.6 文生视频模型生成完整视频。

    Args:
        prompt:       英文视频描述提示词，建议描述场景、动作、风格、镜头语言
        duration:     视频时长（秒），默认 15 秒，估算成本约 $1.20
        aspect_ratio: 宽高比，"9:16"（默认）/ "16:9" / "1:1"
        resolution:   分辨率，"720p"（默认）/ "480p" / "1080p"
        output_path:  可选，指定输出文件路径；不填则自动生成 UUID 文件名
        seed:         可选随机种子，用于复现相同结果
        on_progress:  可选进度回调函数，接收队列状态更新对象

    Returns:
        保存到本地的 MP4 视频文件绝对路径字符串。

    Raises:
        EnvironmentError: FAL_KEY 未配置时抛出。
        RuntimeError:     视频生成或下载失败时抛出。
    """
    import fal_client

    api_key = _get_fal_key()
    os.environ.setdefault("FAL_KEY", api_key)

    estimated = estimate_cost(duration, "standard")
    logger.info(
        "生成视频 | 模型=%s | 时长=%ds | 比例=%s | 预估费用=$%.4f",
        VIDEO_MODEL, duration, aspect_ratio, estimated,
    )

    arguments: dict = {
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    if seed is not None:
        arguments["seed"] = seed

    def _on_queue_update(update):
        if on_progress:
            try:
                on_progress(update)
            except Exception:
                pass
        if hasattr(update, "logs") and update.logs:
            for log in update.logs:
                logger.debug("[fal] %s", log.get("message", ""))

    try:
        result = fal_client.subscribe(VIDEO_MODEL, arguments=arguments, on_queue_update=_on_queue_update)
    except Exception as exc:
        logger.error("fal.ai 视频生成失败: %s", exc)
        raise RuntimeError(f"fal.ai 视频生成失败: {exc}") from exc

    # 提取视频 URL
    video_url = (
        (result.get("video") or {}).get("url")
        or result.get("video_url")
        or ""
    )
    if not video_url:
        raise RuntimeError(f"fal.ai 未返回视频 URL，响应: {result}")

    dest = Path(output_path) if output_path else VIDEOS_DIR / f"{uuid.uuid4()}.mp4"
    return _download_video(video_url, dest)


def generate_video_from_image(
    image_path: str,
    prompt: str,
    duration: int = 5,
    output_path: Optional[str] = None,
) -> str:
    """使用图生视频模型（I2V）将静态图片转换为短视频，成本更低。

    Args:
        image_path: 本地图片文件路径（JPG / PNG），将上传至 fal.ai
        prompt:     描述动态效果的提示词，如 "slow zoom in, cinematic lighting"
        duration:   视频时长（秒），默认 5 秒，I2V 通常 ≤10 秒
        output_path: 可选，指定输出文件路径

    Returns:
        保存到本地的 MP4 视频文件绝对路径字符串。

    Raises:
        FileNotFoundError: 图片文件不存在时抛出。
        RuntimeError:      上传或视频生成失败时抛出。
    """
    import fal_client

    api_key = _get_fal_key()
    os.environ.setdefault("FAL_KEY", api_key)

    img_path = Path(image_path)
    if not img_path.exists():
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    estimated = estimate_cost(duration, "budget")
    logger.info(
        "图生视频 | 模型=%s | 图片=%s | 时长=%ds | 预估费用=$%.4f",
        IMAGE_TO_VIDEO_MODEL, img_path.name, duration, estimated,
    )

    # 上传图片到 fal.ai
    try:
        image_url = fal_client.upload_file(str(img_path))
        logger.info("图片已上传 → %s", image_url)
    except Exception as exc:
        logger.error("图片上传失败: %s", exc)
        raise RuntimeError(f"图片上传失败: {exc}") from exc

    arguments = {
        "image_url": image_url,
        "prompt": prompt,
        "duration": duration,
    }

    try:
        result = fal_client.subscribe(IMAGE_TO_VIDEO_MODEL, arguments=arguments)
    except Exception as exc:
        logger.error("I2V 生成失败: %s", exc)
        raise RuntimeError(f"图生视频失败: {exc}") from exc

    video_url = (
        (result.get("video") or {}).get("url")
        or result.get("video_url")
        or ""
    )
    if not video_url:
        raise RuntimeError(f"I2V 未返回视频 URL，响应: {result}")

    dest = Path(output_path) if output_path else VIDEOS_DIR / f"{uuid.uuid4()}.mp4"
    return _download_video(video_url, dest)
