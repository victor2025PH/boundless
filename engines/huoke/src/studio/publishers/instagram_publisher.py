# -*- coding: utf-8 -*-
"""
Instagram Reels 发布器 — ADB 自动化发布竖版视频到 Instagram Reels。

发布流程:
1. 打开 Instagram → 点击 "+"
2. 选择 "Reels" 选项
3. 从相册选择视频
4. 添加文案和话题标签
5. 分享发布

坐标系说明：
  所有 INSTAGRAM_COORDS 使用相对坐标（0.0~1.0），
  运行时通过 _rel_to_abs() 转换为实际像素坐标。
  基准分辨率：1080×2400（常见 Android FHD+ 竖屏）

已处理的弹窗/权限:
  - 存储权限对话框（Allow / Deny）
  - 通知权限对话框
  - 相机访问权限
  - 更新提示
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .base_publisher import BasePublisher, PublishResult

log = logging.getLogger(__name__)

# Instagram App 包名
PACKAGE_INSTAGRAM = "com.instagram.android"

# ─── Instagram 界面相对坐标表 ──────────────────────────────────────────────
# 格式: (relative_x, relative_y)  范围 0.0 ~ 1.0
INSTAGRAM_COORDS = {
    # 底部导航栏 —— "+" 新建内容按钮（居中）
    "plus_button":          (0.50, 0.945),
    # 新建内容弹出菜单 —— "Reels" 选项（从底部弹出的菜单第一项）
    "reels_option":         (0.50, 0.72),
    # 相册首个缩略图（左上角第一格）
    "gallery_first":        (0.17, 0.55),
    # 相册右上角 "下一步 / Next" 按钮
    "gallery_next":         (0.88, 0.055),
    # Reels 编辑页 "下一步 / Next"（右上角）
    "reels_edit_next":      (0.88, 0.055),
    # 最终分享页 "分享 / Share" 按钮（蓝色大按钮）
    "share_button":         (0.50, 0.88),
    # 描述文字输入框（"Write a caption..."）
    "caption_field":        (0.50, 0.22),
    # 话题标签 "#" 快捷按钮（工具栏）
    "hashtag_shortcut":     (0.15, 0.35),
    # 存储权限 "Allow" 按钮（Android 权限弹窗）
    "permission_allow":     (0.72, 0.73),
    # 存储权限 "Deny" / "Don't allow" 按钮
    "permission_deny":      (0.28, 0.73),
    # 发布成功后的主页 Feed 区域（用于验证跳转）
    "home_feed":            (0.50, 0.50),
}

# 需要自动跳过的权限/弹窗文字（多语言）
_DISMISS_LABELS = [
    # 权限允许
    "Allow", "ALLOW", "允许", "Autoriser", "Permitir",
    # 权限拒绝（某些情况下需要拒绝以跳过）
    "Not Now", "Not now", "Nicht jetzt", "Pas maintenant",
    # 更新/提示类弹窗
    "Skip", "Later", "Maybe Later", "以后再说", "暂不",
    "OK", "Got it", "Continue", "CONTINUE",
    # 通知权限
    "Turn On", "Turn Off",
]


class InstagramPublisher(BasePublisher):
    """
    Instagram Reels ADB 自动化发布器。

    专门针对 Reels（短视频）发布流程；普通 Feed 帖子暂不支持。
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
        return PACKAGE_INSTAGRAM

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
        处理 Instagram 请求存储权限的弹窗。
        通常出现在首次进入相册时。
        允许权限以便访问媒体文件。
        """
        # Android 12+ 使用 "Photos and videos" 分类权限
        for allow_text in ("Allow", "Allow all", "允许", "Photos and videos"):
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
        从 Instagram 主界面导航到 Reels 上传入口。

        流程：
          1. 关闭可能存在的欢迎/权限弹窗
          2. 点击底部 "+" 按钮
          3. 在弹出菜单中点击 "Reels" 选项

        Returns:
            True 表示成功进入 Reels 编辑页
        """
        # 1. 先处理启动后可能出现的弹窗
        self._wait(1)
        self._dismiss_dialogs()

        # 2. 点击 "+" 发布按钮
        log.debug("点击 Instagram '+' 按钮")
        plus_clicked = False
        for res_id in (
            "com.instagram.android:id/creation_tab",
            "com.instagram.android:id/tab_icon_new_post",
        ):
            if self._click_element(resource_id=res_id, timeout=3):
                plus_clicked = True
                break

        if not plus_clicked:
            px, py = self._rel_to_abs(*INSTAGRAM_COORDS["plus_button"])
            if not self._tap(px, py):
                log.error("点击 '+' 按钮失败")
                return False

        self._wait(1.5)

        # 3. 点击 "Reels" 选项（底部菜单弹出后）
        log.debug("点击 'Reels' 菜单项")
        for reels_text in ("Reel", "Reels", "REELS"):
            if self._click_element(text=reels_text, timeout=5):
                log.info("已选择 Reels 模式")
                self._wait(2)
                return True

        # 坐标兜底
        rx, ry = self._rel_to_abs(*INSTAGRAM_COORDS["reels_option"])
        self._tap(rx, ry)
        self._wait(2)

        # 验证是否进入 Reels 编辑页
        # Instagram Reels 页面通常有 "Audio" / "Text" 工具栏
        for kw in ("Audio", "Effects", "相册", "Gallery", "Text", "Align"):
            if self._find_element(text=kw):
                log.info("成功进入 Instagram Reels 页")
                return True

        log.warning("无法确认是否进入 Reels 编辑页，继续执行")
        return True

    # ─────────────────────────────────────────────
    # 选择视频文件
    # ─────────────────────────────────────────────

    def select_video_file(self, remote_path: str) -> bool:
        """
        在 Instagram Reels 页面从相册中选择推送的视频。

        流程：
          1. 处理存储权限弹窗
          2. 切换到相册视图（若当前在相机模式）
          3. 点击最新视频（即刚推送的文件）
          4. 点击 "下一步 / Next"

        Args:
            remote_path: 设备上的视频路径（用于文件名匹配）
        """
        # 1. 处理权限弹窗
        self._handle_storage_permission()
        self._wait(0.5)

        filename = remote_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

        # 2. 确保在相册视图（不在相机实时拍摄模式）
        # Instagram Reels 进入后默认可能是相机，需要切到相册
        for gallery_text in ("Gallery", "相册", "GALLERY", "Library"):
            if self._click_element(text=gallery_text, timeout=3):
                log.debug("切换到相册视图")
                self._wait(1)
                break

        # 3. 尝试按文件名查找
        log.debug("在相册中查找视频: %s", filename)
        if self._click_element(text=filename, timeout=3):
            log.info("通过文件名选中视频: %s", filename)
        else:
            # 点击相册第一个（最新）视频缩略图
            log.debug("按文件名未找到，点击第一个缩略图")
            gx, gy = self._rel_to_abs(*INSTAGRAM_COORDS["gallery_first"])
            if not self._tap(gx, gy):
                log.error("点击相册缩略图失败")
                return False

        self._wait(1)

        # 4. 点击 "下一步 / Next" → 进入 Reels 编辑工具页
        log.debug("点击 Next（进入 Reels 编辑页）")
        for label in ("Next", "下一步", "ADD", "Add"):
            if self._click_element(text=label, timeout=5):
                log.info("视频已选择，进入编辑页")
                self._wait(2)
                return True

        # 坐标兜底
        nx, ny = self._rel_to_abs(*INSTAGRAM_COORDS["gallery_next"])
        self._tap(nx, ny)
        self._wait(2)

        # 再点一次 Next（从编辑工具页 → 发帖描述页）
        for label in ("Next", "下一步", "Share", "分享"):
            if self._click_element(text=label, timeout=5):
                log.info("进入 Reels 描述页")
                self._wait(1.5)
                return True

        ex, ey = self._rel_to_abs(*INSTAGRAM_COORDS["reels_edit_next"])
        self._tap(ex, ey)
        self._wait(1.5)

        return True

    # ─────────────────────────────────────────────
    # 填写帖子详情
    # ─────────────────────────────────────────────

    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """
        填写 Instagram Reels 的文案和话题标签。

        含 emoji 的文案先写入剪贴板再粘贴。
        话题标签追加在正文之后。

        Args:
            caption:  帖子正文
            hashtags: 话题列表（不含 # 前缀）
        """
        # 1. 组合最终文案
        tag_str = " ".join(f"#{t}" for t in hashtags) if hashtags else ""
        full_text = f"{caption}\n{tag_str}".strip() if tag_str else caption
        log.debug("Instagram Reels 文案: %s", full_text[:80])

        # 2. 点击描述输入框
        focused = False
        for placeholder in (
            "Write a caption...", "添加说明...", "Agregar un pie de foto",
            "Write caption", "Caption",
        ):
            if self._click_element(text=placeholder, timeout=3):
                focused = True
                log.debug("通过占位文字点击输入框: '%s'", placeholder)
                break

        if not focused:
            for res_id in (
                "com.instagram.android:id/caption",
                "com.instagram.android:id/caption_text_view",
            ):
                if self._click_element(resource_id=res_id, timeout=2):
                    focused = True
                    break

        if not focused:
            # 坐标兜底
            cx, cy = self._rel_to_abs(*INSTAGRAM_COORDS["caption_field"])
            self._tap(cx, cy)

        self._wait(0.8)

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

        # 4. 关闭键盘（避免遮挡后续按钮）
        self._press_key(4)  # KEYCODE_BACK 收起软键盘
        self._wait(0.5)

        log.info("Instagram Reels 文案填写完成")
        return True

    # ─────────────────────────────────────────────
    # 确认发布
    # ─────────────────────────────────────────────

    def confirm_publish(self) -> bool:
        """
        点击 Instagram 的 "Share"（分享/发布）按钮。

        Instagram 的最终发布按钮文字通常是 "Share"（英文）或 "分享"（中文）。
        """
        log.debug("查找并点击 Instagram 'Share' 发布按钮")

        for label in ("Share", "分享", "Compartir", "Teilen", "Partager", "发布", "Post"):
            if self._click_element(text=label, timeout=8):
                log.info("点击发布按钮: '%s'", label)
                return True

        # 坐标兜底
        sx, sy = self._rel_to_abs(*INSTAGRAM_COORDS["share_button"])
        result = self._tap(sx, sy)
        if result:
            log.info("按坐标点击分享按钮")
        return result

    # ─────────────────────────────────────────────
    # 验证发布结果
    # ─────────────────────────────────────────────

    def verify_published(self) -> Optional[str]:
        """
        等待 Instagram 显示发布成功信号。

        Instagram Reels 发布后通常会：
        - 跳回个人主页 Profile
        - 显示 "Your reel has been shared" Toast
        - 显示上传进度条消失

        等待最多 45 秒（Reels 编码较慢）。

        Returns:
            伪 post_id（时间戳）；未检测到成功信号返回 None
        """
        log.debug("等待 Instagram Reels 发布成功信号（最多45s）...")

        success_keywords = [
            "Your reel has been shared",
            "Reel shared",
            "shared",
            "Published",
            "已分享",
            "已发布",
            "Reel uploaded",
            "Reels",
        ]

        # Instagram 发布有时需要较长时间（视频转码）
        deadline = time.time() + 45
        while time.time() < deadline:
            for kw in success_keywords:
                if self._find_element(text=kw):
                    log.info("检测到 Instagram 发布成功信号: '%s'", kw)
                    import time as _t
                    return f"instagram_{int(_t.time())}"
            self._wait(2)

        # 检查是否已回到主页或 Profile 页
        for home_kw in (
            "Home", "Search", "Reels", "Shop", "Profile",
            "主页", "搜索", "购物", "个人主页",
        ):
            if self._find_element(text=home_kw):
                log.info("已返回 Instagram 主界面，视为发布成功")
                import time as _t
                return f"instagram_{int(_t.time())}"

        log.warning("未检测到 Instagram 发布成功信号")
        return None
