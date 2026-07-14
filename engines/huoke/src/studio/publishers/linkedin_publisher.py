# -*- coding: utf-8 -*-
"""LinkedIn 视频 ADB 自动发布者。

发布流程:
1. 打开 LinkedIn → 点击底部 "Post" 按钮
2. 选择 "Video" 选项
3. 从相册选择视频
4. 添加文案和话题标签（最多3000字符，hashtags最多5个）
5. 点击 Post 发布

坐标系说明：
  所有 LI_COORDS 使用相对坐标（0.0~1.0），
  运行时通过 _rel_to_abs() 转换为实际像素坐标。
  基准分辨率：1080×2400（常见 Android FHD+ 竖屏）

已处理的弹窗/权限:
  - 存储/媒体权限对话框（Allow / 允许）
  - 通知权限对话框
  - 相机访问权限
  - 更新提示 / 登录提示
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .base_publisher import BasePublisher, PublishResult

log = logging.getLogger(__name__)

# LinkedIn App 包名
PACKAGE_LINKEDIN = "com.linkedin.android"

# ─── LinkedIn 界面相对坐标表 ───────────────────────────────────────────────
# 格式: (relative_x, relative_y)  范围 0.0 ~ 1.0
LI_COORDS = {
    "post_button":      (0.50, 0.945),  # 底部 "Post" 创建按钮
    "media_option":     (0.10, 0.75),   # 底部工具栏媒体/图片图标
    "gallery_first":    (0.17, 0.40),   # 相册第一个缩略图
    "next_button":      (0.85, 0.08),   # "Next"/"下一步"
    "text_field":       (0.50, 0.25),   # 正文输入框
    "post_confirm":     (0.85, 0.07),   # "Post"/"发布"确认按钮
}

# LinkedIn 正文最大字符数
_LI_MAX_CAPTION = 3000
_LI_CAPTION_WARN = 2800

# 需要自动跳过的权限/弹窗文字（多语言）
_DISMISS_LABELS = [
    # 权限允许
    "Allow", "ALLOW", "允许", "Autoriser", "Permitir",
    "Allow all", "Allow access",
    # 跳过类
    "Not Now", "Not now", "Skip", "Later", "Maybe Later",
    "以后再说", "暂不", "跳过",
    # 确认类
    "OK", "Got it", "Continue", "CONTINUE", "确定",
    # 通知权限
    "Turn On",
    # 更新提示
    "Cancel", "取消",
]


class LinkedInPublisher(BasePublisher):
    """
    LinkedIn 视频 ADB 自动化发布器。

    支持发布视频到 LinkedIn 动态；
    自动处理权限弹窗、多语言界面。
    正文最多 3000 字符，超过 2800 时自动截断。
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
        return PACKAGE_LINKEDIN

    @property
    def app_package(self) -> str:
        return PACKAGE_LINKEDIN

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

    def _handle_storage_permission(self) -> None:
        """
        处理 LinkedIn 请求存储/媒体权限的弹窗。
        通常出现在首次进入相册时。
        """
        for allow_text in (
            "Allow", "Allow all", "允许",
            "Photos and videos", "Allow access to media",
            "Continue", "ALLOW",
        ):
            if self._find_element(text=allow_text):
                log.debug("授予存储权限: '%s'", allow_text)
                self._click_element(text=allow_text, timeout=3)
                self._wait(0.5)
                return

    # ─────────────────────────────────────────────
    # 导航到上传入口
    # ─────────────────────────────────────────────

    def navigate_to_upload(self) -> bool:
        """
        从 LinkedIn 主界面导航到视频发布入口。

        流程：
          1. 先处理启动后可能出现的弹窗
          2. 查找底部 "+" / "Post" 按钮（text="Post" 或 resource_id 含 "share_box"）
          3. 找不到则按 post_button 坐标点击
          4. 等待底部选项弹出（查找 "Video" / "视频" / "Photo" 选项）
          5. 点击 "Video" 选项

        Returns:
            True 表示成功进入视频上传页
        """
        # 1. 先处理启动后可能出现的弹窗
        self._wait(1)
        self._dismiss_dialogs()

        # 2. 查找底部 "Post" / "+" 按钮
        log.debug("查找 LinkedIn 底部 Post/+ 按钮")
        post_clicked = False

        # 优先按文字查找
        for post_text in ("Post", "发布", "+", "Share", "Create"):
            if self._click_element(text=post_text, timeout=4):
                log.info("点击创建按钮: '%s'", post_text)
                post_clicked = True
                self._wait(1.5)
                break

        if not post_clicked:
            # 尝试按 resource_id 查找（含 "share_box"）
            for res_id in (
                "com.linkedin.android:id/share_box",
                "com.linkedin.android:id/post_button",
                "com.linkedin.android:id/fab",
            ):
                if self._click_element(resource_id=res_id, timeout=3):
                    log.info("通过 resource_id 点击创建按钮: '%s'", res_id)
                    post_clicked = True
                    self._wait(1.5)
                    break

        if not post_clicked:
            # 坐标兜底：底部 Post 按钮
            log.debug("按坐标点击 post_button")
            px, py = self._rel_to_abs(*LI_COORDS["post_button"])
            self._tap_fuzzy(px, py)
            self._wait(1.5)

        # 3. 等待底部选项弹出，查找 Video 选项
        log.debug("查找 Video/视频 发布选项")
        for option_text in ("Video", "视频", "VIDEO", "Upload video", "视频上传"):
            if self._click_element(text=option_text, timeout=6):
                log.info("选择发布类型: '%s'", option_text)
                self._wait(2)
                return True

        # 坐标兜底：media_option（底部工具栏媒体图标）
        log.debug("按坐标点击 media_option")
        mx, my = self._rel_to_abs(*LI_COORDS["media_option"])
        self._tap_fuzzy(mx, my)
        self._wait(1.5)

        # 再次尝试找 Video 选项
        for option_text in ("Video", "视频", "VIDEO"):
            if self._click_element(text=option_text, timeout=4):
                log.info("选择 Video 选项: '%s'", option_text)
                self._wait(2)
                return True

        # 验证是否进入了上传页（相册/Gallery关键词）
        for kw in ("Gallery", "相册", "图库", "Camera", "相机", "Select video", "Choose video"):
            if self._find_element(text=kw):
                log.info("已进入 LinkedIn 视频上传页")
                return True

        log.warning("无法确认是否进入视频上传页，继续执行")
        return True

    # ─────────────────────────────────────────────
    # 选择视频文件
    # ─────────────────────────────────────────────

    def select_video_file(self, remote_path: str) -> bool:
        """
        在 LinkedIn 页面从相册中选择推送的视频。

        流程：
          1. 处理存储/媒体权限弹窗
          2. 等待相册加载（1.5s）
          3. 点击第一个视频缩略图 gallery_first
          4. 点击 Next/下一步

        Args:
            remote_path: 设备上的视频路径（用于文件名匹配）

        Returns:
            True 表示成功选择视频并进入下一步
        """
        # 1. 处理权限弹窗
        self._handle_storage_permission()
        self._wait(0.5)

        # 2. 等待相册加载
        self._wait(1.5)

        # 3. 尝试按文件名查找视频
        filename = remote_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        log.debug("在相册中查找视频: %s", filename)

        if self._click_element(text=filename, timeout=3):
            log.info("通过文件名选中视频: %s", filename)
        else:
            # 点击相册第一个（最新）视频缩略图
            log.debug("按文件名未找到，点击第一个缩略图")
            gx, gy = self._rel_to_abs(*LI_COORDS["gallery_first"])
            if not self._tap_fuzzy(gx, gy):
                log.warning("点击相册缩略图失败")
                return False

        self._wait(1)

        # 4. 点击 "Next" / "下一步"
        log.debug("点击 Next 进入下一步")
        for label in ("Next", "下一步", "NEXT", "Continue", "ADD", "Add"):
            if self._click_element(text=label, timeout=5):
                log.info("视频已选择，进入下一步")
                self._wait(2)
                return True

        # 坐标兜底：Next 按钮
        nx, ny = self._rel_to_abs(*LI_COORDS["next_button"])
        self._tap_fuzzy(nx, ny)
        self._wait(2)

        return True

    # ─────────────────────────────────────────────
    # 填写帖子详情
    # ─────────────────────────────────────────────

    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """
        填写 LinkedIn 帖子的文案和话题标签。

        LinkedIn 正文上限 3000 字符，超过 2800 自动截断。
        含 emoji 或中文的文案先写入剪贴板再粘贴。
        话题标签（最多5个）追加在正文之后，换行分隔。

        Args:
            caption:  帖子正文
            hashtags: 话题列表（不含 # 前缀），最多取5个

        Returns:
            True 表示填写完成
        """
        # 1. 截断过长的 caption
        if len(caption) > _LI_CAPTION_WARN:
            log.warning("LinkedIn 正文超过 %d 字符，截断至 %d", _LI_CAPTION_WARN, _LI_CAPTION_WARN)
            caption = caption[:_LI_CAPTION_WARN]

        # 2. 组合最终文案（hashtags 最多5个）
        limited_tags = hashtags[:5] if hashtags else []
        tag_str = " ".join(f"#{t}" for t in limited_tags)
        full_text = f"{caption}\n{tag_str}".strip() if tag_str else caption

        # 确保总长不超过 3000 字符
        if len(full_text) > _LI_MAX_CAPTION:
            full_text = full_text[:_LI_MAX_CAPTION]

        log.debug("LinkedIn 帖子文案: %s", full_text[:80])

        # 3. 点击文案输入框
        focused = False
        for placeholder in (
            "What do you want to talk about?",
            "说说你的想法...",
            "Add a comment",
            "Write something",
            "What's on your mind",
            "Caption",
            "Add text",
        ):
            if self._click_element(text=placeholder, timeout=3):
                focused = True
                log.debug("通过占位文字点击输入框: '%s'", placeholder)
                break

        if not focused:
            for res_id in (
                "com.linkedin.android:id/share_creation_text_input",
                "com.linkedin.android:id/post_text_input",
                "com.linkedin.android:id/text_field",
                "com.linkedin.android:id/caption",
            ):
                if self._click_element(resource_id=res_id, timeout=2):
                    focused = True
                    log.debug("通过 resource_id 点击输入框: '%s'", res_id)
                    break

        if not focused:
            # 坐标兜底
            log.debug("按坐标点击 text_field")
            cx, cy = self._rel_to_abs(*LI_COORDS["text_field"])
            self._tap_fuzzy(cx, cy)

        self._wait(0.5)

        # 4. 判断是否含 emoji 或非 ASCII 字符
        has_complex = any(ord(c) > 127 for c in full_text)
        if has_complex:
            log.debug("文案含 emoji/中文，使用剪贴板粘贴")
            self._copy_to_clipboard(full_text)
            self._wait(0.5)
            self._press_key(279)  # KEYCODE_PASTE
        else:
            self._type_text(full_text)

        self._wait(0.5)

        # 5. 收起软键盘（避免遮挡后续按钮）
        self._press_key(4)  # KEYCODE_BACK 收起键盘
        self._wait(0.5)

        log.info("LinkedIn 帖子文案填写完成")
        return True

    # ─────────────────────────────────────────────
    # 确认发布
    # ─────────────────────────────────────────────

    def confirm_publish(self) -> bool:
        """
        点击 LinkedIn 的 "Post"（发布）确认按钮。

        LinkedIn 最终发布按钮文字通常是 "Post"（英文）
        或 "发布"（中文）/ "Share"。

        Returns:
            True 表示点击成功
        """
        log.debug("查找并点击 LinkedIn 'Post' 发布确认按钮")

        for label in ("Post", "发布", "Share", "分享", "Publish", "POST", "SHARE"):
            if self._click_element(text=label, timeout=8):
                log.info("点击发布确认按钮: '%s'", label)
                self._wait(2)
                return True

        # 坐标兜底：post_confirm 按钮
        px, py = self._rel_to_abs(*LI_COORDS["post_confirm"])
        result = self._tap_fuzzy(px, py)
        if result:
            log.info("按坐标点击发布确认按钮")
            self._wait(2)
        else:
            log.warning("点击 Post 确认按钮失败")
        return result

    # ─────────────────────────────────────────────
    # 验证发布结果
    # ─────────────────────────────────────────────

    def verify_published(self) -> Optional[str]:
        """
        等待 LinkedIn 显示发布成功信号。

        LinkedIn 发布后通常会：
        - 显示 "Post shared" / "分享成功" / "Your post" Toast
        - 跳回 LinkedIn 动态信息流

        等待最多 40 秒。

        Returns:
            伪 post_id（时间戳字符串）；未检测到成功信号返回 None
        """
        log.debug("等待 LinkedIn 发布成功信号（最多40s）...")

        success_keywords = [
            "Post shared",
            "分享成功",
            "Your post",
            "已发布",
            "已分享",
            "Video shared",
            "Post published",
            "Your video",
            "Uploading",
            "Processing",
        ]

        deadline = time.time() + 40
        while time.time() < deadline:
            for kw in success_keywords:
                if self._find_element(text=kw):
                    log.info("检测到 LinkedIn 发布成功信号: '%s'", kw)
                    return f"linkedin_{int(time.time())}"
            self._wait(5)

        # 检查是否已回到主页或信息流
        for home_kw in (
            "Home", "My Network", "Jobs", "Messaging", "Notifications",
            "主页", "我的网络", "工作", "消息", "通知",
        ):
            if self._find_element(text=home_kw):
                log.info("已返回 LinkedIn 主界面，视为发布成功")
                return f"linkedin_{int(time.time())}"

        log.warning("未检测到 LinkedIn 发布成功信号")
        return None
