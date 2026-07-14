# -*- coding: utf-8 -*-
"""
TikTok 内容发布器 — ADB 自动化发布视频到 TikTok。

发布流程:
1. 打开 TikTok → 点击 "+" 按钮
2. 选择"上传" → 从相册选择视频
3. 填写文案和话题标签
4. 点击"发布"
5. 等待确认

坐标系说明：
  所有 TIKTOK_COORDS 使用相对坐标（0.0~1.0），
  运行时通过 _rel_to_abs() 转换为实际像素坐标。
  基准分辨率：1080×2400（Redmi 13C 实测）
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .base_publisher import BasePublisher, PublishResult

log = logging.getLogger(__name__)

# ─── TikTok 界面相对坐标表 ─────────────────────────────────────────────────
# 格式: (relative_x, relative_y)  范围 0.0 ~ 1.0
TIKTOK_COORDS = {
    # 底部导航栏 —— "+" 发布按钮（正中心偏下）
    "plus_button":        (0.50, 0.922),
    # 发布入口页面 —— "上传" / "Upload" 按钮（右下区域）
    "upload_button":      (0.75, 0.88),
    # 相册首个视频缩略图（左上第一格）
    "gallery_first":      (0.17, 0.38),
    # 相册右上 "下一步" / "Next"
    "gallery_next":       (0.85, 0.07),
    # 编辑页 "下一步" / "Next"（右上角）
    "edit_next":          (0.85, 0.07),
    # 发帖描述输入框（屏幕中上区域）
    "caption_field":      (0.50, 0.20),
    # 标签(#)按钮 / 话题入口
    "hashtag_button":     (0.12, 0.30),
    # 最终 "发布" / "Post" 按钮（右下角）
    "post_button":        (0.85, 0.88),
    # 发布成功提示区域（屏幕中心）
    "success_indicator":  (0.50, 0.50),
}

# TikTok 两个常见包名（国际版 / Trill）
PACKAGE_MUSICALLY = "com.zhiliaoapp.musically"
PACKAGE_TRILL     = "com.ss.android.ugc.trill"


class TikTokPublisher(BasePublisher):
    """
    TikTok ADB 自动化发布器。

    自动检测设备上安装的 TikTok 包名（musically / trill 二者均支持）。
    """

    def __init__(self, device_id: Optional[str] = None, config_path: Optional[str] = None) -> None:
        super().__init__(device_id=device_id, config_path=config_path)
        # 延迟检测实际包名，首次 _ensure_package() 时确定
        self._package: Optional[str] = None

    # ─────────────────────────────────────────────
    # 包名管理
    # ─────────────────────────────────────────────

    def _ensure_package(self) -> bool:
        """
        检测设备上实际安装的 TikTok 包名，结果缓存到 self._package。
        优先使用 musically（国际版）。
        """
        if self._package:
            return True

        for pkg in (PACKAGE_MUSICALLY, PACKAGE_TRILL):
            rc, out = self._adb(f"shell pm list packages {pkg}")
            if rc == 0 and pkg in out:
                self._package = pkg
                log.info("检测到 TikTok 包名: %s", pkg)
                return True

        log.error("设备上未安装 TikTok（musically / trill 均未找到）")
        return False

    def get_package_name(self) -> str:
        """返回当前设备上实际安装的 TikTok 包名"""
        if not self._package:
            self._ensure_package()
        return self._package or PACKAGE_MUSICALLY

    # ─────────────────────────────────────────────
    # 导航到上传入口
    # ─────────────────────────────────────────────

    def navigate_to_upload(self) -> bool:
        """
        从 TikTok 主界面点击 "+" → 再点击 "Upload"，进入相册选择页。

        Returns:
            True 表示成功到达相册页
        """
        # 1. 点击底部 "+" 发布按钮
        log.debug("点击 TikTok '+' 发布按钮")
        ax, ay = self._rel_to_abs(*TIKTOK_COORDS["plus_button"])
        if not self._tap(ax, ay):
            log.warning("点击 '+' 失败")
            return False

        self._wait(1.5)

        # 2. 尝试通过文字查找 "Upload" 按钮（多语言兼容）
        for label in ("Upload", "上传", "Subir", "Hochladen"):
            if self._click_element(text=label, timeout=3):
                log.debug("通过文字 '%s' 点击上传按钮", label)
                self._wait(1.5)
                return True

        # 3. 回退：按坐标点击上传区域
        log.debug("文字查找失败，按坐标点击上传按钮")
        ux, uy = self._rel_to_abs(*TIKTOK_COORDS["upload_button"])
        self._tap(ux, uy)
        self._wait(1.5)

        # 4. 验证是否进入相册（等待相册元素出现）
        # TikTok 相册页通常有 "Recents" / "最近" 或 Gallery 字样
        for keyword in ("Recents", "最近", "Gallery", "相册", "All"):
            if self._find_element(text=keyword):
                log.info("成功进入 TikTok 相册页")
                return True

        log.warning("无法确认是否进入相册页，尝试继续")
        return True   # 宽容处理，让后续步骤再判断

    # ─────────────────────────────────────────────
    # 选择视频文件
    # ─────────────────────────────────────────────

    def select_video_file(self, remote_path: str) -> bool:
        """
        在 TikTok 相册页选中指定视频。

        策略：
        1. 尝试用文件名在 UI 中匹配（部分设备显示文件名）
        2. 如果找不到，点击第一个缩略图（假设已推送的视频是最新的）
        3. 点击 "Next" 进入剪辑页，再点一次 "Next" 进入发帖页

        Args:
            remote_path: 设备上的视频路径（用于文件名匹配）
        """
        filename = remote_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

        # 等待相册加载
        self._wait(1)

        # 1. 尝试按文件名查找
        log.debug("尝试按文件名 '%s' 查找视频", filename)
        if self._click_element(text=filename, timeout=3):
            log.info("通过文件名找到视频: %s", filename)
        else:
            # 2. 点击相册第一个（最新）视频
            log.debug("文件名未匹配，点击相册第一个缩略图")
            gx, gy = self._rel_to_abs(*TIKTOK_COORDS["gallery_first"])
            if not self._tap(gx, gy):
                log.error("点击相册缩略图失败")
                return False

        self._wait(1)

        # 3. 点击 "下一步 / Next"（进入剪辑页）
        log.debug("点击 Next（进入剪辑页）")
        nx, ny = self._rel_to_abs(*TIKTOK_COORDS["gallery_next"])
        for label in ("Next", "下一步", "Siguiente"):
            if self._click_element(text=label, timeout=3):
                break
        else:
            self._tap(nx, ny)

        self._wait(2)

        # 4. 剪辑页再点一次 "下一步 / Next"（进入发帖描述页）
        log.debug("点击 Next（进入发帖描述页）")
        for label in ("Next", "下一步", "Post", "发布"):
            if self._click_element(text=label, timeout=3):
                log.info("已进入发帖描述页")
                self._wait(1.5)
                return True

        # 按坐标兜底
        ex, ey = self._rel_to_abs(*TIKTOK_COORDS["edit_next"])
        self._tap(ex, ey)
        self._wait(1.5)
        return True

    # ─────────────────────────────────────────────
    # 填写帖子详情
    # ─────────────────────────────────────────────

    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """
        填写 TikTok 发帖文案和话题标签。

        含 emoji 的文案先写入剪贴板再粘贴，避免 ADB input text 丢字。
        话题标签拼接在文案末尾，每个以 # 开头。

        Args:
            caption:  帖子正文
            hashtags: 话题列表（不含 # 前缀）
        """
        # 1. 组合最终文案（正文 + 标签）
        tag_str = " ".join(f"#{t}" for t in hashtags) if hashtags else ""
        full_text = f"{caption} {tag_str}".strip()
        log.debug("帖子文案: %s", full_text[:80])

        # 2. 点击描述输入框
        log.debug("点击发帖描述输入框")
        focused = False
        for res_id in (
            "com.zhiliaoapp.musically:id/caption_et",
            "com.ss.android.ugc.trill:id/caption_et",
            "caption",
        ):
            if self._click_element(resource_id=res_id, timeout=2):
                focused = True
                break

        if not focused:
            # 按坐标点击
            cx, cy = self._rel_to_abs(*TIKTOK_COORDS["caption_field"])
            self._tap(cx, cy)

        self._wait(0.8)

        # 3. 清空现有内容（全选 + 删除）
        # KEYCODE_CTRL_A = 277 (部分设备支持), 备用：长按选全
        self._press_key(277)  # CTRL+A
        self._wait(0.3)
        self._press_key(67)   # BACKSPACE

        # 4. 判断是否含 emoji 或中文，决定输入方式
        has_complex = any(ord(c) > 127 for c in full_text)
        if has_complex:
            # 含非 ASCII 字符：写剪贴板 → 粘贴
            log.debug("文案含 emoji/中文，使用剪贴板粘贴")
            self._copy_to_clipboard(full_text)
            self._wait(0.5)
            # CTRL+V 粘贴
            self._press_key(279)  # KEYCODE_PASTE
        else:
            # 纯 ASCII：直接 input text
            self._type_text(full_text)

        self._wait(0.5)
        log.info("帖子文案填写完成")
        return True

    # ─────────────────────────────────────────────
    # 确认发布
    # ─────────────────────────────────────────────

    def confirm_publish(self) -> bool:
        """
        点击 TikTok 的 "Post"（发布）按钮。

        优先按文字查找，兜底按坐标点击。
        """
        log.debug("查找并点击 'Post' 发布按钮")

        for label in ("Post", "发布", "Publicar", "Posten", "Publier"):
            if self._click_element(text=label, timeout=5):
                log.info("点击发布按钮成功: '%s'", label)
                return True

        # 坐标兜底
        px, py = self._rel_to_abs(*TIKTOK_COORDS["post_button"])
        result = self._tap(px, py)
        if result:
            log.info("按坐标点击发布按钮")
        return result

    # ─────────────────────────────────────────────
    # 验证发布结果
    # ─────────────────────────────────────────────

    def verify_published(self) -> Optional[str]:
        """
        等待 TikTok 显示发布成功信号。

        TikTok 发布后通常会：
        - 跳回主页 Feed
        - 显示 "Your video is being processed" Toast
        - 显示 "Posted" 提示

        等待最多 30 秒。

        Returns:
            伪 post_id（时间戳）；如果未检测到成功信号返回 None
        """
        log.debug("等待 TikTok 发布成功信号（最多30s）...")

        success_keywords = [
            "Posted", "已发布", "Processing", "processing",
            "Your video is being processed",
            "Video uploaded", "Upload complete",
            "发布成功",
        ]

        deadline = time.time() + 30
        while time.time() < deadline:
            for kw in success_keywords:
                if self._find_element(text=kw):
                    log.info("检测到发布成功信号: '%s'", kw)
                    # TikTok 不直接暴露 post_id，用时间戳代替
                    import time as _t
                    pseudo_id = f"tiktok_{int(_t.time())}"
                    return pseudo_id
            self._wait(2)

        # 如果没检测到成功信号，检查是否已回到主页（也算成功）
        for home_kw in ("For You", "Following", "为你", "关注"):
            if self._find_element(text=home_kw):
                log.info("已回到 TikTok 主页，视为发布成功")
                import time as _t
                return f"tiktok_{int(_t.time())}"

        log.warning("未检测到 TikTok 发布成功信号")
        return None
