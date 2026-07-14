# -*- coding: utf-8 -*-
"""
Telegram 频道发布器 — 通过 Bot API 直接发布（无需ADB）。

Telegram Bot API 是最简单的发布方式:
- 无需账号授权
- 直接 HTTP 请求
- 支持视频/图片/文字
- 免费无限制

使用前提：
  1. 通过 @BotFather 创建 Bot，获得 bot_token
  2. 将 Bot 加入目标频道并设为管理员
  3. 获取频道 ID（私有频道: -100xxxxxxxxx，公开频道: @channel_name）

环境变量（优先级低于构造参数）:
  TELEGRAM_BOT_TOKEN   — Bot Token
  TELEGRAM_CHANNEL_ID  — 目标频道 ID
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests

from .base_publisher import BasePublisher, PublishResult

log = logging.getLogger(__name__)

# Telegram Bot API 基础 URL
_TG_API_BASE = "https://api.telegram.org/bot{token}/{method}"

# 文件扩展名 → 发布类型映射
_VIDEO_EXTS  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp"}
_PHOTO_EXTS  = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class TelegramPublisher(BasePublisher):
    """
    Telegram Bot API 发布器。

    不使用 ADB，直接通过 HTTPS 请求调用 Telegram Bot API。
    继承 BasePublisher 只是为了复用 PublishResult 和工厂接口。
    """

    def __init__(
        self,
        bot_token: str = "",
        channel_id: str = "",
        device_id: Optional[str] = None,
        config_path: Optional[str] = None,
        **kwargs,
    ) -> None:
        # 调用父类初始化（device_id 对 Telegram 无实际意义，但保持接口一致）
        super().__init__(device_id=device_id, config_path=config_path)

        # 从参数 → 配置文件 → 环境变量 依次获取凭据
        self._bot_token = (
            bot_token
            or self._config.get("telegram_bot_token", "")
            or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        )
        self._channel_id = (
            channel_id
            or self._config.get("telegram_channel_id", "")
            or os.environ.get("TELEGRAM_CHANNEL_ID", "")
        )

        if not self._bot_token:
            log.warning(
                "Telegram bot_token 未配置！"
                "请通过构造参数、config 或 TELEGRAM_BOT_TOKEN 环境变量提供。"
            )
        if not self._channel_id:
            log.warning(
                "Telegram channel_id 未配置！"
                "请通过构造参数、config 或 TELEGRAM_CHANNEL_ID 环境变量提供。"
            )

        # requests Session（复用连接，提升性能）
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "ContentStudio/1.0"})

    # ─────────────────────────────────────────────
    # 内部 API 调用工具
    # ─────────────────────────────────────────────

    def _api_url(self, method: str) -> str:
        return _TG_API_BASE.format(token=self._bot_token, method=method)

    def _call_api(
        self,
        method: str,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        timeout: int = 60,
    ) -> Optional[dict]:
        """
        调用 Telegram Bot API。

        Args:
            method:  API 方法名，如 "sendMessage"
            data:    表单字段
            files:   multipart 文件（用于 sendVideo/sendPhoto）
            timeout: HTTP 超时秒数

        Returns:
            API 响应中的 result 字段；失败返回 None
        """
        url = self._api_url(method)
        try:
            resp = self._session.post(url, data=data, files=files, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
        except requests.exceptions.Timeout:
            log.error("Telegram API 请求超时: %s", method)
            return None
        except requests.exceptions.RequestException as e:
            log.error("Telegram API 请求异常: %s  %s", method, e)
            return None
        except ValueError:
            log.error("Telegram API 响应不是有效 JSON")
            return None

        if not body.get("ok"):
            log.error(
                "Telegram API 返回错误: method=%s  code=%s  desc=%s",
                method,
                body.get("error_code"),
                body.get("description"),
            )
            return None

        return body.get("result")

    def _check_credentials(self) -> Optional[str]:
        """
        校验 bot_token 和 channel_id 是否已配置。

        Returns:
            错误信息字符串；配置正常返回 None
        """
        if not self._bot_token:
            return "bot_token 未配置"
        if not self._channel_id:
            return "channel_id 未配置"
        return None

    # ─────────────────────────────────────────────
    # 核心发布方法
    # ─────────────────────────────────────────────

    def publish_video_to_channel(
        self,
        video_path: str,
        caption: str = "",
    ) -> PublishResult:
        """
        向 Telegram 频道发送视频。

        使用 sendVideo API，multipart/form-data 上传本地文件。
        文件大小限制：普通 Bot API 上传 ≤50 MB；更大文件需先上传到服务器。

        Args:
            video_path: 本地视频文件路径
            caption:    视频说明文字（可含 Markdown）

        Returns:
            PublishResult
        """
        start_ts = time.time()
        platform = "telegram"

        # 凭据检查
        cred_err = self._check_credentials()
        if cred_err:
            return PublishResult(
                success=False, platform=platform, error=cred_err,
                duration_sec=time.time() - start_ts,
            )

        if not os.path.exists(video_path):
            return PublishResult(
                success=False, platform=platform,
                error=f"视频文件不存在: {video_path}",
                duration_sec=time.time() - start_ts,
            )

        file_size_mb = os.path.getsize(video_path) / 1024 / 1024
        log.info("上传视频到 Telegram: %s (%.1f MB)", video_path, file_size_mb)

        if file_size_mb > 50:
            log.warning("文件 %.1f MB 超过 Bot API 50MB 限制，上传可能失败", file_size_mb)

        # 构造请求
        data = {
            "chat_id": self._channel_id,
            "caption": caption[:1024],  # Telegram 限制 caption ≤ 1024 字符
            "parse_mode": "HTML",
            "supports_streaming": "true",
        }

        try:
            with open(video_path, "rb") as f:
                files = {"video": (os.path.basename(video_path), f, "video/mp4")}
                result = self._call_api("sendVideo", data=data, files=files, timeout=300)
        except OSError as e:
            return PublishResult(
                success=False, platform=platform, error=f"读取视频文件失败: {e}",
                duration_sec=time.time() - start_ts,
            )

        duration = time.time() - start_ts

        if result:
            message_id = str(result.get("message_id", ""))
            log.info("Telegram 视频发布成功 message_id=%s 耗时=%.1fs", message_id, duration)
            return PublishResult(
                success=True, platform=platform,
                post_id=message_id, duration_sec=duration,
            )

        return PublishResult(
            success=False, platform=platform,
            error="sendVideo API 调用失败",
            duration_sec=duration,
        )

    def publish_photo_to_channel(
        self,
        image_path: str,
        caption: str = "",
    ) -> PublishResult:
        """
        向 Telegram 频道发送图片。

        Args:
            image_path: 本地图片文件路径
            caption:    图片说明文字

        Returns:
            PublishResult
        """
        start_ts = time.time()
        platform = "telegram"

        cred_err = self._check_credentials()
        if cred_err:
            return PublishResult(
                success=False, platform=platform, error=cred_err,
                duration_sec=time.time() - start_ts,
            )

        if not os.path.exists(image_path):
            return PublishResult(
                success=False, platform=platform,
                error=f"图片文件不存在: {image_path}",
                duration_sec=time.time() - start_ts,
            )

        log.info("上传图片到 Telegram: %s", image_path)

        suffix = Path(image_path).suffix.lower()
        mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"

        data = {
            "chat_id": self._channel_id,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }

        try:
            with open(image_path, "rb") as f:
                files = {"photo": (os.path.basename(image_path), f, mime)}
                result = self._call_api("sendPhoto", data=data, files=files, timeout=120)
        except OSError as e:
            return PublishResult(
                success=False, platform=platform, error=f"读取图片文件失败: {e}",
                duration_sec=time.time() - start_ts,
            )

        duration = time.time() - start_ts

        if result:
            message_id = str(result.get("message_id", ""))
            log.info("Telegram 图片发布成功 message_id=%s", message_id)
            return PublishResult(
                success=True, platform=platform,
                post_id=message_id, duration_sec=duration,
            )

        return PublishResult(
            success=False, platform=platform,
            error="sendPhoto API 调用失败",
            duration_sec=duration,
        )

    def publish_text_to_channel(self, text: str) -> PublishResult:
        """
        向 Telegram 频道发送纯文本消息。

        Args:
            text: 消息内容（支持 HTML 格式）

        Returns:
            PublishResult
        """
        start_ts = time.time()
        platform = "telegram"

        cred_err = self._check_credentials()
        if cred_err:
            return PublishResult(
                success=False, platform=platform, error=cred_err,
                duration_sec=time.time() - start_ts,
            )

        log.info("发送文本消息到 Telegram 频道")

        data = {
            "chat_id": self._channel_id,
            "text": text[:4096],  # Telegram 消息最长 4096 字符
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }

        result = self._call_api("sendMessage", data=data)
        duration = time.time() - start_ts

        if result:
            message_id = str(result.get("message_id", ""))
            log.info("Telegram 文本消息发布成功 message_id=%s", message_id)
            return PublishResult(
                success=True, platform=platform,
                post_id=message_id, duration_sec=duration,
            )

        return PublishResult(
            success=False, platform=platform,
            error="sendMessage API 调用失败",
            duration_sec=duration,
        )

    # ─────────────────────────────────────────────
    # 覆写主发布方法（不走 ADB 流程）
    # ─────────────────────────────────────────────

    def publish(
        self,
        video_path: str,
        caption: str,
        hashtags: List[str] = [],
        schedule_time: Optional[datetime] = None,
    ) -> PublishResult:
        """
        根据文件类型自动路由到对应的发布方法。

        - 视频文件 → publish_video_to_channel
        - 图片文件 → publish_photo_to_channel
        - 无文件/纯文字 → publish_text_to_channel

        话题标签附加在 caption 末尾。
        """
        # 拼接话题标签
        tag_str = " ".join(f"#{t}" for t in hashtags) if hashtags else ""
        full_caption = f"{caption}\n{tag_str}".strip() if tag_str else caption

        # 判断文件类型
        if video_path and os.path.exists(video_path):
            suffix = Path(video_path).suffix.lower()
            if suffix in _VIDEO_EXTS:
                return self.publish_video_to_channel(video_path, full_caption)
            elif suffix in _PHOTO_EXTS:
                return self.publish_photo_to_channel(video_path, full_caption)
            else:
                log.warning("未知文件类型: %s，尝试作为视频发布", suffix)
                return self.publish_video_to_channel(video_path, full_caption)

        # 无有效文件：发送纯文本
        log.info("无媒体文件，发送纯文本消息")
        return self.publish_text_to_channel(full_caption)

    # ─────────────────────────────────────────────
    # 抽象方法的最小实现（Telegram 不需要 ADB）
    # ─────────────────────────────────────────────

    def get_package_name(self) -> str:
        """Telegram 不使用 ADB，包名无意义"""
        return "org.telegram.messenger"

    def navigate_to_upload(self) -> bool:
        """Telegram 不使用 ADB，无需导航"""
        return True

    def select_video_file(self, remote_path: str) -> bool:
        """Telegram 不使用 ADB，无需选择文件"""
        return True

    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """Telegram 不使用 ADB，无需填写 UI 表单"""
        return True

    def confirm_publish(self) -> bool:
        """Telegram 不使用 ADB，无需点击按钮"""
        return True

    def verify_published(self) -> Optional[str]:
        """Telegram 不使用 ADB，由 _call_api 直接返回结果"""
        return "telegram_direct"
