# -*- coding: utf-8 -*-
"""WhatsApp Status/Story ADB 自动发布者。

WhatsApp Status（状态）等同于 Instagram Story，24小时后消失。
这是引流矩阵中的关键触点——通讯录好友自动看到视频状态，触发私信。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .base_publisher import BasePublisher, PublishResult

log = logging.getLogger(__name__)

WA_COORDS = {
    "status_tab":       (0.25, 0.945),  # 底部 "Status" 标签（第二个图标）
    "add_status":       (0.83, 0.83),   # 状态页绿色 "+" 按钮
    "gallery_option":   (0.50, 0.82),   # 弹出选项中 "Gallery"/"图库"
    "gallery_first":    (0.17, 0.42),   # 相册第一个视频缩略图
    "send_button":      (0.85, 0.88),   # 绿色发送/分享按钮
    "caption_field":    (0.40, 0.88),   # 底部文字输入栏
    "permission_allow": (0.72, 0.73),   # 权限弹窗 "允许/Allow"
}

# WhatsApp Status 视频限制: 30秒, 16MB
STATUS_MAX_CAPTION = 700  # WhatsApp状态文字上限


class WhatsAppPublisher(BasePublisher):
    """WhatsApp Status（状态）视频自动发布者。"""

    def get_package_name(self) -> str:
        return "com.whatsapp"

    @property
    def app_package(self) -> str:
        return "com.whatsapp"

    def navigate_to_upload(self) -> bool:
        """导航到 Status 创建页面。"""
        # 1. 点击底部 Status 标签
        clicked = self._click_element(text="Status") or self._click_element(text="状态")
        if not clicked:
            w, h = self._get_screen_size()
            x, y = self._rel_to_abs(*WA_COORDS["status_tab"])
            self._tap(x, y)
        self._wait(1.5)

        # 2. 点击 "+" 添加按钮
        add_clicked = (
            self._click_element(resource_id="com.whatsapp:id/fab")
            or self._click_element(text="Add to status")
            or self._click_element(text="添加到状态")
        )
        if not add_clicked:
            w, h = self._get_screen_size()
            x, y = self._rel_to_abs(*WA_COORDS["add_status"])
            self._tap(x, y)
        self._wait(1.2)

        # 3. 选择 Gallery/图库（文字或照片图标）
        gallery_clicked = (
            self._click_element(text="Gallery")
            or self._click_element(text="图库")
            or self._click_element(text="Photos & videos")
        )
        if not gallery_clicked:
            # 直接弹出相册选项时点击第一个图片/视频图标
            x, y = self._rel_to_abs(*WA_COORDS["gallery_option"])
            self._tap(x, y)
        self._wait(1.5)

        # 验证：相册已打开（出现 "Recent"/"最近" 等）
        opened = (
            self._find_element(text="Recent") is not None
            or self._find_element(text="最近") is not None
            or self._find_element(text="All videos") is not None
        )
        if not opened:
            log.warning("[whatsapp] 相册可能未成功打开")
        return True

    def select_video_file(self, video_remote_path: str) -> bool:
        """从相册选择视频。"""
        # 处理权限弹窗
        self._dismiss_permission_dialogs()
        self._wait(1.0)

        # 切换到 Videos 标签（如果有）
        self._click_element(text="Videos") or self._click_element(text="视频")
        self._wait(0.8)

        # 点击第一个缩略图
        x, y = self._rel_to_abs(*WA_COORDS["gallery_first"])
        if not self._tap_fuzzy(x, y, fuzz=20):
            log.warning("[whatsapp] 点击缩略图失败")
            return False
        self._wait(1.5)

        # 若出现 "Open" / "Open as Status" 对话框，确认
        self._click_element(text="Open") or self._click_element(text="打开")
        self._wait(0.8)

        log.debug("[whatsapp] 视频已选中")
        return True

    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """输入状态文字说明。"""
        # WhatsApp Status 的 caption 在发送前输入
        text = caption
        if len(text) > STATUS_MAX_CAPTION:
            text = text[:STATUS_MAX_CAPTION - 3] + "..."

        # hashtags 对 WhatsApp 效果有限，只加2个
        if hashtags:
            tags = " ".join(f"#{t.lstrip('#')}" for t in hashtags[:2])
            combined = f"{text}\n{tags}"
            if len(combined) <= STATUS_MAX_CAPTION:
                text = combined

        if not text.strip():
            return True  # 无需文字也能发布状态

        # 点击底部文字输入栏
        field_clicked = (
            self._click_element(resource_id="com.whatsapp:id/caption")
            or self._click_element(resource_id="com.whatsapp:id/caption_edit_text")
        )
        if not field_clicked:
            x, y = self._rel_to_abs(*WA_COORDS["caption_field"])
            self._tap(x, y)
        self._wait(0.5)

        # 输入文字
        has_special = any(ord(c) > 127 for c in text)
        if has_special:
            self._copy_to_clipboard(text)
            self._press_key(279)  # KEYCODE_PASTE
        else:
            self._type_text(text)
        self._wait(0.5)

        # 收起键盘
        self._press_key(4)  # KEYCODE_BACK
        self._wait(0.3)

        return True

    def confirm_publish(self) -> bool:
        """点击发送按钮发布状态。"""
        sent = (
            self._click_element(resource_id="com.whatsapp:id/send")
            or self._click_element(text="Send")
            or self._click_element(text="发送")
            or self._click_element(text="Share")
        )
        if not sent:
            x, y = self._rel_to_abs(*WA_COORDS["send_button"])
            self._tap(x, y)
        self._wait(2.0)
        return True

    def verify_published(self) -> Optional[str]:
        """等待状态发布完成。"""
        deadline = time.time() + 30  # WhatsApp Status 发布很快
        while time.time() < deadline:
            # 检查是否回到 Status 列表页（说明发布成功）
            back_to_status = (
                self._find_element(text="My status") is not None
                or self._find_element(text="我的状态") is not None
                or self._find_element(text="Status") is not None
                or self._find_element(text="Add to status") is not None
            )
            if back_to_status:
                log.info("[whatsapp] Status 发布成功")
                return f"whatsapp_{int(time.time())}"

            # 检查上传进度（进度条消失即成功）
            uploading = self._find_element(text="Uploading") or self._find_element(text="正在上传")
            if not uploading and time.time() > deadline - 20:
                # 进度条消失且还剩20秒内 → 认为成功
                log.info("[whatsapp] 上传完成（进度条消失）")
                return f"whatsapp_{int(time.time())}"

            self._wait(4)

        log.warning("[whatsapp] 发布超时，无法确认成功")
        return None

    def _dismiss_permission_dialogs(self):
        """处理 WhatsApp 存储/媒体权限弹窗。"""
        for _ in range(3):
            dismissed = (
                self._click_element(text="Allow")
                or self._click_element(text="允许")
                or self._click_element(text="OK")
                or self._click_element(text="Continue")
            )
            if not dismissed:
                break
            self._wait(0.5)
