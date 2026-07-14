# -*- coding: utf-8 -*-
"""Facebook 视频/Reels ADB 自动发布者。

发布流程:
1. 打开 Facebook → 点击 Reels 标签或 "+" 按钮
2. 选择 "Reel" / "Video" 选项
3. 从相册选择视频
4. 添加文案和话题标签
5. 点击 Share 发布

坐标系说明：
  所有 FB_COORDS 使用相对坐标（0.0~1.0），
  运行时通过 _rel_to_abs() 转换为实际像素坐标。
  基准分辨率：1080×2400（常见 Android FHD+ 竖屏）

已处理的弹窗/权限:
  - 存储权限对话框（Allow / Deny）
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

# Facebook App 包名（主版本 / 轻量版）
PACKAGE_FACEBOOK = "com.facebook.katana"
PACKAGE_FACEBOOK_LITE = "com.facebook.lite"

# ─── Facebook 界面相对坐标表 ───────────────────────────────────────────────
# 格式: (relative_x, relative_y)  范围 0.0 ~ 1.0
FB_COORDS = {
    "reels_tab":        (0.50, 0.94),   # 底部 Reels 标签
    "create_button":    (0.50, 0.065),  # 右上角 "+" 创建按钮
    "reel_option":      (0.25, 0.55),   # 菜单中的 "Reel" 选项
    "gallery_first":    (0.17, 0.42),   # 相册第一个缩略图
    "next_button":      (0.85, 0.07),   # "下一步/Next"
    "caption_field":    (0.50, 0.25),   # 文案输入框
    "share_button":     (0.85, 0.88),   # "分享/Share"
    "done_button":      (0.85, 0.07),   # "完成/Done"
}

# 需要自动跳过的权限/弹窗文字（多语言）
_DISMISS_LABELS = [
    # 权限允许
    "Allow", "ALLOW", "允许", "Autoriser", "Permitir",
    # 跳过类
    "Not Now", "Not now", "Skip", "Later", "Maybe Later",
    "以后再说", "暂不", "跳过",
    # 确认类
    "OK", "Got it", "Continue", "CONTINUE", "确定",
    # 通知权限
    "Turn On", "Turn Off",
    # 更新提示
    "Update", "Cancel", "取消",
]


class FacebookPublisher(BasePublisher):
    """
    Facebook Reels/Video ADB 自动化发布器。

    支持发布竖版短视频到 Facebook Reels；
    自动处理权限弹窗、多语言界面和轻量版 App。
    """

    def __init__(
        self,
        device_id: Optional[str] = None,
        config_path: Optional[str] = None,
        use_lite: bool = False,
    ) -> None:
        super().__init__(device_id=device_id, config_path=config_path)
        self._package = PACKAGE_FACEBOOK_LITE if use_lite else PACKAGE_FACEBOOK

    # ─────────────────────────────────────────────
    # 包名
    # ─────────────────────────────────────────────

    def get_package_name(self) -> str:
        return self._package

    @property
    def app_package(self) -> str:
        return self._package

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
        处理 Facebook 请求存储权限的弹窗。
        通常出现在首次进入相册时。
        """
        for allow_text in ("Allow", "Allow all", "允许", "Photos and videos",
                           "Allow access to media", "Continue"):
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
        从 Facebook 主界面导航到 Reels 上传入口。

        流程：
          1. 先处理启动后可能出现的弹窗
          2. 尝试点击 "Reels" 标签（底部导航）
          3. 如果找不到 Reels 标签，点击 "+" 或 "Create" 按钮
          4. 在弹出菜单中点击 "Reel" / "Video" / "视频" 选项

        Returns:
            True 表示成功进入 Reels/Video 上传页
        """
        # 1. 先处理启动后可能出现的弹窗
        self._wait(1)
        self._dismiss_dialogs()

        # 2. 尝试点击底部 "Reels" 标签
        log.debug("尝试点击 Facebook Reels 标签")
        reels_found = False
        for reels_text in ("Reels", "REELS", "短视频"):
            if self._click_element(text=reels_text, timeout=4):
                log.info("点击 Reels 标签成功")
                reels_found = True
                self._wait(2)
                break

        if not reels_found:
            # 坐标兜底：底部 Reels 标签
            rx, ry = self._rel_to_abs(*FB_COORDS["reels_tab"])
            self._tap_fuzzy(rx, ry)
            self._wait(2)

        # 3. 如果仍未进入 Reels 创建流，尝试点击 "+" 或 "Create" 按钮
        # 先检查是否已出现创建/上传相关关键词
        create_keywords = ("Create", "创建", "Upload", "上传", "Add", "添加")
        already_in_create = any(self._find_element(text=kw) for kw in create_keywords)

        if not already_in_create:
            log.debug("Reels 标签未直达创建页，尝试点击 '+' / 'Create' 按钮")
            create_clicked = False
            for create_text in ("Create", "创建", "+"):
                if self._click_element(text=create_text, timeout=4):
                    create_clicked = True
                    log.debug("点击创建按钮: '%s'", create_text)
                    self._wait(1.5)
                    break

            if not create_clicked:
                # 坐标兜底：右上角创建按钮
                cx, cy = self._rel_to_abs(*FB_COORDS["create_button"])
                self._tap_fuzzy(cx, cy)
                self._wait(1.5)

        # 4. 在弹出菜单中查找并点击 Reel / Video 选项
        log.debug("查找 Reel/Video 菜单选项")
        for option_text in ("Reel", "Video", "视频", "REEL", "VIDEO", "Short video"):
            if self._click_element(text=option_text, timeout=5):
                log.info("选择发布类型: '%s'", option_text)
                self._wait(2)
                return True

        # 坐标兜底：菜单中的 Reel 选项
        ox, oy = self._rel_to_abs(*FB_COORDS["reel_option"])
        self._tap_fuzzy(ox, oy)
        self._wait(2)

        # 验证是否进入了上传页（出现相册/Gallery关键词）
        for kw in ("Gallery", "相册", "图库", "Camera", "相机", "Add a reel", "Select video"):
            if self._find_element(text=kw):
                log.info("已进入 Facebook Reels 上传页")
                return True

        log.warning("无法确认是否进入 Reels 上传页，继续执行")
        return True

    # ─────────────────────────────────────────────
    # 选择视频文件
    # ─────────────────────────────────────────────

    def select_video_file(self, remote_path: str) -> bool:
        """
        在 Facebook Reels 页面从相册中选择推送的视频。

        流程：
          1. 处理存储权限弹窗
          2. 查找 Gallery / 相册 按钮并点击（若有）
          3. 点击相册第一个视频缩略图
          4. 点击 "Next" / "下一步"

        Args:
            remote_path: 设备上的视频路径（用于文件名匹配）

        Returns:
            True 表示成功选择视频并进入下一步
        """
        # 1. 处理权限弹窗
        self._handle_storage_permission()
        self._wait(0.5)

        # 2. 查找并点击 Gallery / 相册 按钮
        for gallery_text in ("Gallery", "相册", "图库", "GALLERY", "Library", "Media"):
            if self._click_element(text=gallery_text, timeout=4):
                log.debug("切换到相册视图: '%s'", gallery_text)
                self._wait(1)
                break

        # 3. 尝试按文件名查找视频
        filename = remote_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        log.debug("在相册中查找视频: %s", filename)

        if self._click_element(text=filename, timeout=3):
            log.info("通过文件名选中视频: %s", filename)
        else:
            # 点击相册第一个（最新）视频缩略图
            log.debug("按文件名未找到，点击第一个缩略图")
            gx, gy = self._rel_to_abs(*FB_COORDS["gallery_first"])
            if not self._tap_fuzzy(gx, gy):
                log.warning("点击相册缩略图失败")
                return False

        self._wait(1)

        # 4. 点击 "Next" / "下一步"
        log.debug("点击 Next 进入下一步")
        for label in ("Next", "下一步", "ADD", "Add", "NEXT", "Continue"):
            if self._click_element(text=label, timeout=5):
                log.info("视频已选择，进入下一步")
                self._wait(2)
                return True

        # 坐标兜底：Next 按钮
        nx, ny = self._rel_to_abs(*FB_COORDS["next_button"])
        self._tap_fuzzy(nx, ny)
        self._wait(2)

        return True

    # ─────────────────────────────────────────────
    # 填写帖子详情
    # ─────────────────────────────────────────────

    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """
        填写 Facebook Reels 的文案和话题标签。

        含 emoji 或中文的文案先写入剪贴板再粘贴。
        话题标签（最多5个）追加在正文之后，换行分隔。

        Args:
            caption:  帖子正文
            hashtags: 话题列表（不含 # 前缀），最多取5个

        Returns:
            True 表示填写完成
        """
        # 1. 组合最终文案（hashtags 最多5个，避免太多）
        limited_tags = hashtags[:5] if hashtags else []
        tag_str = " ".join(f"#{t}" for t in limited_tags)
        full_text = f"{caption}\n{tag_str}".strip() if tag_str else caption
        log.debug("Facebook Reels 文案: %s", full_text[:80])

        # 2. 点击文案输入框
        focused = False
        for placeholder in (
            "Write a caption...", "添加说明", "Say something about this reel",
            "Describe your reel", "Add a description", "Add a caption",
            "What's on your mind", "Caption",
        ):
            if self._click_element(text=placeholder, timeout=3):
                focused = True
                log.debug("通过占位文字点击输入框: '%s'", placeholder)
                break

        if not focused:
            for res_id in (
                "com.facebook.katana:id/caption",
                "com.facebook.katana:id/post_text_input",
                "com.facebook.lite:id/caption",
            ):
                if self._click_element(resource_id=res_id, timeout=2):
                    focused = True
                    break

        if not focused:
            # 坐标兜底
            cx, cy = self._rel_to_abs(*FB_COORDS["caption_field"])
            self._tap_fuzzy(cx, cy)

        self._wait(0.5)

        # 3. 判断是否含 emoji 或非 ASCII 字符
        has_complex = any(ord(c) > 127 for c in full_text)
        if has_complex:
            log.debug("文案含 emoji/中文，使用剪贴板粘贴")
            self._copy_to_clipboard(full_text)
            self._wait(0.5)
            self._press_key(279)  # KEYCODE_PASTE
        else:
            self._type_text(full_text)

        self._wait(0.5)

        # 4. 收起软键盘（避免遮挡后续按钮）
        self._press_key(4)  # KEYCODE_BACK 收起键盘
        self._wait(0.5)

        log.info("Facebook Reels 文案填写完成")
        return True

    # ─────────────────────────────────────────────
    # 确认发布
    # ─────────────────────────────────────────────

    def confirm_publish(self) -> bool:
        """
        点击 Facebook 的 "Share"（分享/发布）按钮。

        Facebook Reels 最终发布按钮文字通常是 "Share"（英文）
        或 "分享"（中文）/ "Post"。

        Returns:
            True 表示点击成功
        """
        log.debug("查找并点击 Facebook 'Share' 发布按钮")

        for label in ("Share", "分享", "Post", "发布", "Publish", "SHARE", "POST"):
            if self._click_element(text=label, timeout=8):
                log.info("点击发布按钮: '%s'", label)
                self._wait(2)
                return True

        # 坐标兜底：Share 按钮
        sx, sy = self._rel_to_abs(*FB_COORDS["share_button"])
        result = self._tap_fuzzy(sx, sy)
        if result:
            log.info("按坐标点击分享按钮")
            self._wait(2)
        else:
            log.warning("点击 Share 按钮失败")
        return result

    # ─────────────────────────────────────────────
    # 验证发布结果
    # ─────────────────────────────────────────────

    def verify_published(self) -> Optional[str]:
        """
        等待 Facebook 显示发布成功信号。

        Facebook Reels 发布后通常会：
        - 显示 "Posted" / "Reel shared" / "Your reel" Toast
        - 跳回 Reels 信息流页面

        等待最多 45 秒（Reels 上传编码较慢）。

        Returns:
            伪 post_id（时间戳字符串）；未检测到成功信号返回 None
        """
        log.debug("等待 Facebook Reels 发布成功信号（最多45s）...")

        success_keywords = [
            "Posted",
            "Reel shared",
            "Your reel",
            "分享成功",
            "已发布",
            "已分享",
            "Reel uploaded",
            "Your video is being shared",
            "Video posted",
            "Publishing",
            "Uploading",
        ]

        deadline = time.time() + 45
        while time.time() < deadline:
            for kw in success_keywords:
                if self._find_element(text=kw):
                    log.info("检测到 Facebook 发布成功信号: '%s'", kw)
                    return f"facebook_{int(time.time())}"
            self._wait(5)

        # 检查是否已回到主页或 Reels 信息流
        for home_kw in (
            "Home", "Reels", "Watch", "Marketplace", "Notifications",
            "主页", "短视频", "通知", "市场",
        ):
            if self._find_element(text=home_kw):
                log.info("已返回 Facebook 主界面，视为发布成功")
                return f"facebook_{int(time.time())}"

        log.warning("未检测到 Facebook 发布成功信号")
        return None
