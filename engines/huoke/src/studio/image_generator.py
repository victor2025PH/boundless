# -*- coding: utf-8 -*-
"""
AI图片生成器 — 使用 fal.ai FLUX.1 Schnell。

成本: ~$0.003/张图片（最快的FLUX模型）
用途: 图文混剪的素材图片生成
每条视频生成 6 张图片，总成本约 $0.018
"""

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

import requests

from src.host.device_registry import config_file, data_dir

logger = logging.getLogger(__name__)

# 图片输出目录（项目根 data/studio/images）
IMAGES_DIR = data_dir() / "studio" / "images"

# fal.ai FLUX Schnell 模型标识
FAL_MODEL = "fal-ai/flux/schnell"

# 支持的宽高比及对应平台说明
ASPECT_RATIO_INFO = {
    "9:16": "TikTok / Instagram Reels / Facebook Story / WhatsApp Status",
    "1:1":  "小红书 / Instagram Square",
    "16:9": "LinkedIn / Twitter / YouTube 封面",
}

# 配置文件路径（备用 key 存储）
_CONFIG_PATH = config_file("fal_key.txt")


def get_fal_key() -> str:
    """获取 fal.ai API Key。

    优先读取环境变量 FAL_KEY，其次读取配置文件 config/fal_key.txt。

    Returns:
        API Key 字符串。

    Raises:
        EnvironmentError: 当两处均未找到 Key 时抛出。
    """
    key = os.environ.get("FAL_KEY", "").strip()
    if key:
        return key
    if _CONFIG_PATH.exists():
        key = _CONFIG_PATH.read_text(encoding="utf-8").strip()
        if key:
            logger.debug("从配置文件读取 FAL_KEY")
            return key
    raise EnvironmentError(
        "未找到 FAL_KEY。请设置环境变量 FAL_KEY 或将 Key 写入 config/fal_key.txt"
    )


def check_fal_key() -> bool:
    """检查 fal.ai API Key 是否已配置。

    Returns:
        True 表示 Key 可用，False 表示未配置。
    """
    try:
        get_fal_key()
        return True
    except EnvironmentError:
        return False


def generate_image(
    prompt: str,
    aspect_ratio: str = "9:16",
    output_path: Optional[str] = None,
    seed: Optional[int] = None,
) -> str:
    """使用 FLUX Schnell 生成单张 AI 图片并保存到本地。

    Args:
        prompt:       英文图片描述提示词，建议详细描述画面内容、风格、光线等
        aspect_ratio: 宽高比，支持 "9:16"（默认）、"1:1"、"16:9"
        output_path:  可选，指定输出文件路径；不填则自动生成 UUID 文件名
        seed:         可选随机种子，用于复现相同结果

    Returns:
        保存到本地的图片文件绝对路径字符串。

    Raises:
        EnvironmentError: FAL_KEY 未配置时抛出。
        RuntimeError:     fal.ai 调用或图片下载失败时抛出。
    """
    import fal_client  # 延迟导入，避免未安装时影响模块加载

    api_key = get_fal_key()
    os.environ.setdefault("FAL_KEY", api_key)

    if aspect_ratio not in ASPECT_RATIO_INFO:
        logger.warning("未知宽高比 '%s'，使用默认 '9:16'", aspect_ratio)
        aspect_ratio = "9:16"

    logger.info("生成图片 | 模型=%s | 比例=%s | 提示词长度=%d字", FAL_MODEL, aspect_ratio, len(prompt))

    arguments: dict = {
        "prompt": prompt,
        "image_size": aspect_ratio.replace(":", "x"),  # fal 接受 "9x16" 格式或预设名
        "num_inference_steps": 4,   # Schnell 推荐 4 步
        "num_images": 1,
        "enable_safety_checker": True,
    }
    # fal-ai/flux/schnell 接受 image_size 为预设字符串
    _size_map = {"9:16": "portrait_16_9", "1:1": "square", "16:9": "landscape_16_9"}
    arguments["image_size"] = _size_map.get(aspect_ratio, "portrait_16_9")
    if seed is not None:
        arguments["seed"] = seed

    try:
        result = fal_client.subscribe(FAL_MODEL, arguments=arguments)
    except Exception as exc:
        logger.error("fal.ai 调用失败: %s", exc)
        raise RuntimeError(f"fal.ai 图片生成失败: {exc}") from exc

    # 提取图片 URL
    images = result.get("images") or []
    if not images:
        raise RuntimeError(f"fal.ai 未返回图片数据，响应: {result}")
    image_url = images[0].get("url") or images[0]
    if not isinstance(image_url, str):
        raise RuntimeError(f"无法解析图片 URL: {images[0]}")

    # 下载并保存图片
    if output_path:
        dest = Path(output_path)
    else:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        dest = IMAGES_DIR / f"{uuid.uuid4()}.jpg"

    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        resp = requests.get(image_url, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        logger.info("图片已保存 → %s", dest)
        return str(dest)
    except Exception as exc:
        logger.error("图片下载失败: %s", exc)
        raise RuntimeError(f"图片下载失败: {exc}") from exc


def generate_image_batch(
    prompts: List[str],
    aspect_ratio: str = "9:16",
    output_dir: Optional[str] = None,
    max_workers: int = 4,
) -> List[str]:
    """并行批量生成多张 AI 图片。

    Args:
        prompts:     提示词列表，每个元素对应一张图片
        aspect_ratio: 统一宽高比，所有图片使用相同比例
        output_dir:  可选，指定输出目录；不填则使用默认 data/studio/images/
        max_workers: 最大并行线程数，默认 4

    Returns:
        按输入顺序排列的本地文件路径列表；某张失败时对应位置为空字符串。
    """
    if not prompts:
        return []

    save_dir = Path(output_dir) if output_dir else IMAGES_DIR
    save_dir.mkdir(parents=True, exist_ok=True)

    results: List[str] = [""] * len(prompts)
    logger.info("批量生成图片 | 数量=%d | 并发=%d", len(prompts), max_workers)

    def _task(idx: int, prompt: str) -> tuple[int, str]:
        out = save_dir / f"{uuid.uuid4()}.jpg"
        path = generate_image(prompt, aspect_ratio, str(out))
        return idx, path

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_task, i, p): i for i, p in enumerate(prompts)}
        for future in as_completed(futures):
            try:
                idx, path = future.result()
                results[idx] = path
            except Exception as exc:
                idx = futures[future]
                logger.error("第 %d 张图片生成失败: %s", idx, exc)

    success = sum(1 for r in results if r)
    logger.info("批量完成 | 成功=%d / 总计=%d", success, len(prompts))
    return results
