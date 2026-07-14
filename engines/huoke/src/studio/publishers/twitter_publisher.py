# -*- coding: utf-8 -*-
"""X (Twitter) 视频 ADB 自动发布者。"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .base_publisher import BasePublisher, PublishResult

log = logging.getLogger(__name__)

# X (Twitter) App 包名
PACKAGE_TWITTER = "com.twitter.android"

# ─── X 界面相对坐标表 ───────────────────────────────────────────────────────
# 格式: (relative_x, relative_y)  范围 0.0 ~ 1.0
# 基准分辨率：1080×2400（常见 Android FHD+ 竖屏）
X_COORDS = {
    "compose_button":   (0.83, 0.89),   # 右下角蓝色 "+" 撰写按钮
    "media_button":     (0.08, 0.88),   # 编辑框底部图片/媒体图标
    "gallery_first":    (0.17, 0.40),   # 相册第一个缩略图
    "tweet_field":      (0.50, 0.20),   # 推文输入框
    "post_button":      (0.85, 0.07),   # 右上角 "Post"/"发布" 按钮
}

# 推文最大字符数（X 限制280，截断阈值留给 hashtags 的余量）
_TWEET_MAX_CHARS = 280
_CAPTION_TRUNCATE_AT = 240
# X 上 hashtag 数量限制（过多会被降权）
_MAX_HASHTAGS = 3

# 需要自动跳过的权限/弹窗文字（多语言）
_DISMISS_LABELS = [
    "Allow", "ALLOW", "允许", "Autoriser", "Permitir",
    "OK", "Ok", "Got it", "Continue", "CONTINUE",
    "Skip", "Later", "Maybe Later", "以后再说", "暂不",
    "Not Now", "Not now",
    "Turn On", "Turn Off",
]


class TwitterPublisher(BasePublisher):
    """
    X (Twitter) 视频 ADB 自动化发布器。

    发布流程：
      1. 打开 X → 点击撰写按钮 "+"
      2. 点击媒体图标打开相册
      3. 选择相册第一个视频缩略图
      4. 填写推文文案和话题标签（280字符限制）
      5. 点击 "Post" 发布
      6. 等待发布成功信号
    """

    def __init__(
        self,
        device_id: Optional[str] = None,
        config_path: Optional[str] = None,
    ) -> None:
        super().__init__(device_id=device_id, config_path=config_path)

    # ─────────────────────────────────────────────
    # 包名
    # ─────────────────────────────────────────────

    def get_package_name(self) -> str:
        return PACKAGE_TWITTER

    # ─────────────────────────────────────────────
    # 弹窗处理工具
    # ─────────────────────────────────────────────

    def _dismiss_dialogs(self, max_attempts: int = 3) -> None:
        """
        尝试关闭可能出现的权限弹窗或提示框。
        最多尝试 max_attempts 次，每次间隔 1s。
        """
        for _ in range(max_attempts):
            dismissed = False
            for label in _DISMISS_LABELS:
                elem = self._find_element(text=label)
                if elem:
                    log.debug("关闭弹窗: '%s'", label)
                    self._tap(elem["x"], elem["y"])
                    self._wait(0.8)
                    dismissed = True
                    break
            if not dismissed:
                break

    def _handle_media_permission(self) -> None:
        """
        处理 X 请求媒体/存储权限的弹窗。
        通常出现在首次进入相册时。
        """
        for allow_text in ("Allow", "Allow all", "允许", "Photos and videos",
                           "Allow access to media", "ALLOW"):
            if self._find_element(text=allow_text):
                log.debug("授予媒体权限: '%s'", allow_text)
                self._click_element(text=allow_text, timeout=3)
                self._wait(0.5)
                return

    # ─────────────────────────────────────────────
    # 导航到撰写入口
    # ─────────────────────────────────────────────

    def navigate_to_upload(self) -> bool:
        """
        从 X 主界面导航到推文撰写入口。

        流程：
          1. 关闭可能存在的欢迎/权限弹窗
          2. 查找撰写按钮（resource_id 或文字）
          3. 找不到则按坐标 compose_button 点击
          4. 等待推文输入框出现

        Returns:
            True 表示成功进入撰写页
        """
        # 1. 先处理启动后可能出现的弹窗
        self._wait(1)
        self._dismiss_dialogs()

        # 2. 查找撰写按钮
        log.debug("查找 X 撰写按钮")
        compose_clicked = False

        # 尝试 resource_id
        if self._click_element(
            resource_id="com.twitter.android:id/composer_write", timeout=4
        ):
            compose_clicked = True
            log.debug("通过 resource_id 点击撰写按钮")

        # 尝试文字匹配
        if not compose_clicked:
            for label in ("+", "Compose", "Post"):
                if self._click_element(text=label, timeout=3):
                    compose_clicked = True
                    log.debug("通过文字点击撰写按钮: '%s'", label)
                    break

        # 坐标兜底
        if not compose_clicked:
            log.debug("按坐标点击撰写按钮")
            cx, cy = self._rel_to_abs(*X_COORDS["compose_button"])
            self._tap_fuzzy(cx, cy)

        self._wait(1.5)

        # 3. 等待推文输入框出现
        log.debug("等待推文输入框出现")
        for res_id in (
            "com.twitter.android:id/tweet_text",
            "com.twitter.android:id/composer_text",
        ):
            if self._wait_for_element(resource_id=res_id, timeout=6):
                log.info("成功进入 X 撰写页（by resource_id）")
                return True

        for placeholder in ("What's happening", "有什么新鲜事", "What's happening?"):
            if self._find_element(text=placeholder):
                log.info("成功进入 X 撰写页（by placeholder）")
                return True

        log.warning("无法确认是否进入撰写页，继续执行")
        return True

    # ─────────────────────────────────────────────
    # 选择视频文件
    # ─────────────────────────────────────────────

    def select_video_file(self, remote_path: str) -> bool:
        """
        在 X 撰写页面从相册中选择推送的视频。

        流程：
          1. 查找媒体图标并点击打开相册
          2. 处理媒体权限弹窗
          3. 等待相册加载（2s）
          4. 点击第一个视频缩略图
          5. 等待1s
          6. 查找确认按钮（若存在）

        Args:
            remote_path: 设备上的视频路径
        """
        # 1. 查找媒体图标
        log.debug("查找 X 媒体图标")
        media_clicked = False

        if self._click_element(
            resource_id="com.twitter.android:id/attachment_image_view", timeout=4
        ):
            media_clicked = True
            log.debug("通过 resource_id 点击媒体图标")

        if not media_clicked:
            # 尝试其他常见媒体按钮 resource_id
            for res_id in (
                "com.twitter.android:id/gallery",
                "com.twitter.android:id/media_attach",
                "com.twitter.android:id/attachment_media_button",
            ):
                if self._click_element(resource_id=res_id, timeout=3):
                    media_clicked = True
                    log.debug("通过 resource_id 点击媒体图标: %s", res_id)
                    break

        if not media_clicked:
            # 坐标兜底
            log.debug("按坐标点击媒体图标")
            mx, my = self._rel_to_abs(*X_COORDS["media_button"])
            self._tap_fuzzy(mx, my)

        # 2. 处理媒体权限弹窗
        self._wait(1)
        self._handle_media_permission()

        # 3. 等待相册加载
        log.debug("等待相册加载")
        self._wait(2)

        # 4. 点击第一个视频缩略图
        log.debug("点击相册第一个视频缩略图")
        gx, gy = self._rel_to_abs(*X_COORDS["gallery_first"])
        if not self._tap_fuzzy(gx, gy):
            log.warning("点击相册缩略图失败")
            return False

        # 5. 等待1s
        self._wait(1)

        # 6. 查找确认按钮（若有）
        for label in ("Add", "Done", "选择", "确认", "OK"):
            elem = self._find_element(text=label)
            if elem:
                log.debug("点击相册确认按钮: '%s'", label)
                self._tap(elem["x"], elem["y"])
                self._wait(1)
                break

        log.info("视频已选择")
        return True

    # ─────────────────────────────────────────────
    # 填写推文详情
    # ─────────────────────────────────────────────

    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """
        填写 X 推文文案和话题标签。

        X 推文有280字符限制：
        - caption 超过240字符则截断（留余量给 hashtags）
        - 最多追加3个 hashtag（X 对过多 hashtag 降权）
        - 含 emoji/中文 → 剪贴板粘贴；否则直接 _type_text

        Args:
            caption:  推文正文
            hashtags: 话题列表（不含 # 前缀）
        """
        # 1. 截断 caption（留空间给 hashtags）
        if len(caption) > _CAPTION_TRUNCATE_AT:
            log.warning(
                "X caption 超过 %d 字符，截断至 %d 字符（原长度=%d）",
                _CAPTION_TRUNCATE_AT, _CAPTION_TRUNCATE_AT, len(caption),
            )
            caption = caption[:_CAPTION_TRUNCATE_AT]

        # 2. 组合 hashtags（最多3个）
        limited_tags = hashtags[:_MAX_HASHTAGS]
        if len(hashtags) > _MAX_HASHTAGS:
            log.warning(
                "X hashtag 数量 %d 超过限制，截断为 %d 个",
                len(hashtags), _MAX_HASHTAGS,
            )
        tag_str = " ".join(f"#{t}" for t in limited_tags) if limited_tags else ""
        full_text = f"{caption}\n{tag_str}".strip() if tag_str else caption

        # 最终长度检查
        if len(full_text) > _TWEET_MAX_CHARS:
            full_text = full_text[:_TWEET_MAX_CHARS]
            log.warning("推文超过280字符，已强制截断至 %d 字符", _TWEET_MAX_CHARS)

        log.debug("X 推文内容: %s", full_text[:100])

        # 3. 点击推文输入框
        log.debug("点击推文输入框")
        focused = False

        for res_id in (
            "com.twitter.android:id/tweet_text",
            "com.twitter.android:id/composer_text",
            "com.twitter.android:id/edit_tweet_text",
        ):
            if self._click_element(resource_id=res_id, timeout=3):
                focused = True
                log.debug("通过 resource_id 点击输入框: %s", res_id)
                break

        if not focused:
            for placeholder in ("What's happening", "有什么新鲜事", "What's happening?"):
                if self._click_element(text=placeholder, timeout=3):
                    focused = True
                    log.debug("通过占位文字点击输入框: '%s'", placeholder)
                    break

        if not focused:
            # 坐标兜底
            tx, ty = self._rel_to_abs(*X_COORDS["tweet_field"])
            self._tap(tx, ty)

        self._wait(0.5)

        # 4. 输入文案
        has_complex = any(ord(c) > 127 for c in full_text)
        if has_complex:
            log.debug("推文含 emoji/中文，使用剪贴板粘贴")
            self._copy_to_clipboard(full_text)
            self._wait(0.5)
            self._press_key(279)  # KEYCODE_PASTE
        else:
            self._type_text(full_text)

        self._wait(0.5)

        # 5. 收起软键盘
        self._press_key(4)  # KEYCODE_BACK
        self._wait(0.5)

        log.info("X 推文文案填写完成（%d 字符）", len(full_text))
        return True

    # ─────────────────────────────────────────────
    # 确认发布
    # ─────────────────────────────────────────────

    def confirm_publish(self) -> bool:
        """
        点击 X 的 "Post"（发布）按钮。

        优先通过 resource_id 查找，其次文字，最后坐标兜底。
        """
        log.debug("查找并点击 X 发布按钮")

        # 通过 resource_id
        if self._click_element(
            resource_id="com.twitter.android:id/button_tweet", timeout=5
        ):
            log.info("通过 resource_id 点击发布按钮")
            self._wait(2)
            return True

        # 通过文字
        for label in ("Post", "Tweet", "发布", "推文", "TWEET", "POST"):
            if self._click_element(text=label, timeout=5):
                log.info("通过文字点击发布按钮: '%s'", label)
                self._wait(2)
                return True

        # 坐标兜底
        log.debug("按坐标点击发布按钮")
        px, py = self._rel_to_abs(*X_COORDS["post_button"])
        result = self._tap_fuzzy(px, py)
        if result:
            log.info("按坐标点击发布按钮")
            self._wait(2)
        return result

    # ─────────────────────────────────────────────
    # 验证发布结果
    # ─────────────────────────────────────────────

    def verify_published(self) -> Optional[str]:
        """
        等待 X 显示发布成功信号。

        X 发布后通常会：
        - 显示 "Your post was sent" Toast
        - 显示 "Tweet sent" Toast
        - 跳回 Timeline/主页

        等待最多 30 秒（X 上传比 Facebook 快）。

        Returns:
            伪 post_id（时间戳字符串）；超时返回 None
        """
        log.debug("等待 X 发布成功信号（最多30s）...")

        success_keywords = [
            "Your post was sent",
            "Tweet sent",
            "Post sent",
            "已发布",
            "已发送",
            "sent",
        ]

        # 回到 Timeline 的标志性元素
        timeline_keywords = [
            "Home", "For you", "Following",
            "主页", "为你推荐", "正在关注",
            "What's happening",
        ]

        deadline = time.time() + 30
        while time.time() < deadline:
            # 检查发布成功 Toast
            for kw in success_keywords:
                if self._find_element(text=kw):
                    log.info("检测到 X 发布成功信号: '%s'", kw)
                    return f"x_{int(time.time())}"

            # 检查是否已回到 Timeline
            for kw in timeline_keywords:
                if self._find_element(text=kw):
                    log.info("已返回 X Timeline，视为发布成功")
                    return f"x_{int(time.time())}"

            self._wait(4)

        log.warning("未检测到 X 发布成功信号，已超时30s")
        return None
