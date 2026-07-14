# -*- coding: utf-8 -*-
"""
小红书（RED / Xiaohongshu）图文/视频发布器 — ADB 自动化发布视频到小红书。

发布流程:
1. 打开小红书 → 点击底部 "+" 发布按钮
2. 在弹出菜单中选择 "发视频"
3. 从相册选择视频 → 两步 "下一步"
4. 填写标题（前30字）和正文描述（最多1000字）
5. 添加话题标签（最多8个，格式 #话题）
6. 点击 "发布" 按钮并验证发布成功

坐标系说明：
  所有 XHS_COORDS 使用相对坐标（0.0~1.0），
  运行时通过 _rel_to_abs() 转换为实际像素坐标。
  基准分辨率：1080×2400（常见 Android FHD+ 竖屏）

已处理的弹窗/权限:
  - 存储权限对话框（Allow / 允许）
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

# 小红书 App 包名（优先国内版，国际版备用）
PACKAGE_XHS_DOMESTIC = "com.xingin.xhs"        # 国内版
PACKAGE_XHS_INTL     = "com.xingin.discover"   # 国际版（RED）

# ─── 小红书界面相对坐标表 ──────────────────────────────────────────────────────
# 格式: (relative_x, relative_y)  范围 0.0 ~ 1.0
XHS_COORDS = {
    # 底部发布 "+" 按钮
    "publish_tab":      (0.83, 0.945),
    # 弹出菜单 "发视频" 选项
    "video_option":     (0.50, 0.72),
    # 相册第一个视频缩略图（左上角第一格）
    "gallery_first":    (0.17, 0.42),
    # 右上角 "下一步" 按钮
    "next_button":      (0.85, 0.08),
    # 标题输入框
    "title_field":      (0.50, 0.18),
    # 正文/描述输入框
    "desc_field":       (0.50, 0.32),
    # "添加话题" #号按钮
    "topic_button":     (0.15, 0.55),
    # 最终 "发布" 按钮
    "publish_button":   (0.85, 0.92),
}

# 小红书标题最大字符数
XHS_TITLE_MAX_LEN = 30

# 小红书描述最大字符数
XHS_DESC_MAX_LEN = 1000

# 小红书话题最大数量
XHS_HASHTAG_MAX = 8

# 需要自动关闭/允许的弹窗文字（多语言）
_DISMISS_LABELS = [
    "Allow", "ALLOW", "允许", "始终允许",
    "Allow all", "Photos and videos",
    "Not Now", "Not now", "以后再说", "暂不",
    "Skip", "Later", "跳过",
    "OK", "确定", "Got it", "Continue", "知道了",
    "Turn On", "开启",
]


class XiaohongshuPublisher(BasePublisher):
    """
    小红书（RED / Xiaohongshu）ADB 自动化视频发布器。

    支持国内版（com.xingin.xhs）和国际版（com.xingin.discover）。
    构造函数可通过 use_international=True 切换包名。
    """

    def __init__(
        self,
        device_id: Optional[str] = None,
        config_path: Optional[str] = None,
        use_international: bool = False,
    ) -> None:
        super().__init__(device_id=device_id, config_path=config_path)
        self._package = PACKAGE_XHS_INTL if use_international else PACKAGE_XHS_DOMESTIC

    # ─────────────────────────────────────────────
    # 包名
    # ─────────────────────────────────────────────

    def get_package_name(self) -> str:
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

    def _handle_permission_dialogs(self) -> None:
        """
        处理小红书请求存储/相机等权限的弹窗。
        通常出现在首次进入相册或拍摄页时。
        """
        for allow_text in (
            "Allow", "Allow all", "允许", "始终允许",
            "Photos and videos", "允许访问照片和视频",
        ):
            if self._find_element(text=allow_text):
                log.debug("授予权限: '%s'", allow_text)
                self._click_element(text=allow_text, timeout=3)
                self._wait(0.5)

    # ─────────────────────────────────────────────
    # 导航到上传入口
    # ─────────────────────────────────────────────

    def navigate_to_upload(self) -> bool:
        """
        从小红书主界面导航到视频上传入口。

        流程：
          1. 关闭可能存在的弹窗
          2. 查找底部 "+" / "发布" / "Create" 按钮并点击
          3. 等待弹出菜单（1.5s）
          4. 点击 "发视频" / "Video" 选项

        Returns:
            True 表示成功进入视频选择页
        """
        # 1. 先处理启动后可能出现的弹窗
        self._wait(1)
        self._dismiss_dialogs()

        # 2. 点击底部 "+" 发布按钮
        log.debug("点击小红书 '+' 发布按钮")
        publish_clicked = False

        # 优先通过 resource-id 查找（更可靠）
        for res_id in (
            "com.xingin.xhs:id/publish",
            "com.xingin.xhs:id/add",
            "com.xingin.discover:id/publish",
            "com.xingin.discover:id/add",
        ):
            if self._click_element(resource_id=res_id, timeout=3):
                publish_clicked = True
                log.debug("通过 resource-id 点击发布按钮: '%s'", res_id)
                break

        if not publish_clicked:
            # 通过文字查找
            for label in ("发布", "+", "Create", "创作"):
                elem = self._find_element(text=label)
                if elem:
                    self._tap(elem["x"], elem["y"])
                    publish_clicked = True
                    log.debug("通过文字点击发布按钮: '%s'", label)
                    break

        if not publish_clicked:
            # 坐标兜底
            log.debug("按坐标点击 publish_tab")
            px, py = self._rel_to_abs(*XHS_COORDS["publish_tab"])
            if not self._tap(px, py):
                log.error("点击发布按钮失败")
                return False

        # 3. 等待弹出菜单出现
        self._wait(1.5)

        # 4. 点击 "发视频" / "Video" 选项
        log.debug("点击 '发视频' 菜单项")
        for label in ("发视频", "视频", "Video", "video", "发布视频"):
            elem = self._find_element(text=label)
            if elem:
                self._tap(elem["x"], elem["y"])
                log.info("已选择发视频模式: '%s'", label)
                self._wait(2)
                return True

        # 坐标兜底
        vx, vy = self._rel_to_abs(*XHS_COORDS["video_option"])
        self._tap(vx, vy)
        self._wait(2)

        # 验证是否进入视频选择页（相册应该出现）
        for kw in ("相册", "Gallery", "最近", "Recent", "视频", "拍摄"):
            if self._find_element(text=kw):
                log.info("成功进入小红书视频上传页")
                return True

        log.warning("无法确认是否进入视频上传页，继续执行")
        return True

    # ─────────────────────────────────────────────
    # 选择视频文件
    # ─────────────────────────────────────────────

    def select_video_file(self, remote_path: str) -> bool:
        """
        在小红书视频上传页面从相册中选择推送的视频。

        流程：
          1. 处理存储权限弹窗（Allow/允许）
          2. 等待相册加载（1.5s）
          3. 点击第一个视频（gallery_first 坐标）
          4. 查找并点击 "下一步" / "Next"（第一步，进入预览）
          5. 等待预览加载（2s）
          6. 再次点击 "下一步"（第二步，进入编辑/填写页）

        Args:
            remote_path: 设备上的视频路径（仅用于日志记录）

        Returns:
            True 表示成功进入帖子详情填写页
        """
        # 1. 处理权限弹窗
        self._handle_permission_dialogs()

        # 2. 等待相册加载
        self._wait(1.5)

        # 3. 尝试按文件名查找视频
        filename = remote_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        log.debug("在相册中查找视频: %s", filename)

        found_by_name = self._click_element(text=filename, timeout=3)
        if found_by_name:
            log.info("通过文件名选中视频: %s", filename)
        else:
            # 点击相册第一个（最新）视频缩略图
            log.debug("按文件名未找到，点击第一个缩略图")
            gx, gy = self._rel_to_abs(*XHS_COORDS["gallery_first"])
            if not self._tap_fuzzy(gx, gy):
                log.error("点击相册缩略图失败")
                return False

        self._wait(1)

        # 4. 点击 "下一步 / Next"（第一步：视频预览）
        log.debug("点击第一个 '下一步'（进入视频预览）")
        first_next_clicked = False
        for label in ("下一步", "Next", "NEXT", "继续"):
            if self._click_element(text=label, timeout=5):
                first_next_clicked = True
                log.info("点击第一个下一步: '%s'", label)
                break

        if not first_next_clicked:
            nx, ny = self._rel_to_abs(*XHS_COORDS["next_button"])
            self._tap(nx, ny)
            log.debug("按坐标点击第一个下一步")

        # 5. 等待预览加载
        self._wait(2)

        # 6. 再次点击 "下一步"（第二步：进入编辑/填写页）
        log.debug("点击第二个 '下一步'（进入帖子编辑页）")
        second_next_clicked = False
        for label in ("下一步", "Next", "NEXT", "继续", "完成"):
            if self._click_element(text=label, timeout=5):
                second_next_clicked = True
                log.info("点击第二个下一步: '%s'", label)
                break

        if not second_next_clicked:
            nx, ny = self._rel_to_abs(*XHS_COORDS["next_button"])
            self._tap(nx, ny)
            log.debug("按坐标点击第二个下一步")

        self._wait(1.5)
        log.info("视频已选择，进入帖子编辑页")
        return True

    # ─────────────────────────────────────────────
    # 填写帖子详情
    # ─────────────────────────────────────────────

    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """
        填写小红书帖子的标题、正文描述和话题标签。

        规则：
          - 标题：caption 前30字符（小红书标题有长度限制）
          - 描述：完整 caption（超出1000字截断）
          - 话题：最多8个，通过 topic_button 或追加 #话题 格式添加

        Args:
            caption:  帖子正文（同时用于提取标题前30字）
            hashtags: 话题列表（不含 # 前缀），最多取前8个
        """
        # ── 准备内容 ────────────────────────────────────────────────────────
        title = caption[:XHS_TITLE_MAX_LEN].strip()

        # 描述：完整文案，超出1000字截断
        desc_body = caption[:XHS_DESC_MAX_LEN].strip()

        # 话题：最多8个
        tags = hashtags[:XHS_HASHTAG_MAX]
        tag_str = " ".join(f"#{t}" for t in tags) if tags else ""

        log.debug("小红书标题: %s", title)
        log.debug("小红书描述: %s...", desc_body[:60])
        log.debug("小红书话题: %s", tag_str)

        # ── 1. 填写标题 ─────────────────────────────────────────────────────
        log.debug("点击标题输入框")
        title_focused = False
        for res_id in (
            "com.xingin.xhs:id/title",
            "com.xingin.xhs:id/et_title",
            "com.xingin.discover:id/title",
            "com.xingin.discover:id/et_title",
        ):
            if self._click_element(resource_id=res_id, timeout=2):
                title_focused = True
                break

        if not title_focused:
            for placeholder in ("添加标题", "Add a title", "标题", "Title"):
                if self._click_element(text=placeholder, timeout=2):
                    title_focused = True
                    log.debug("通过占位文字点击标题框: '%s'", placeholder)
                    break

        if not title_focused:
            tx, ty = self._rel_to_abs(*XHS_COORDS["title_field"])
            self._tap(tx, ty)

        self._wait(0.5)

        # 输入标题（含中文，走剪贴板）
        has_complex_title = any(ord(c) > 127 for c in title)
        if has_complex_title:
            self._copy_to_clipboard(title)
            self._wait(0.3)
            self._press_key(279)  # KEYCODE_PASTE
        else:
            self._type_text(title)

        self._wait(0.5)

        # ── 2. 填写正文描述 ──────────────────────────────────────────────────
        log.debug("点击描述输入框")
        desc_focused = False
        for res_id in (
            "com.xingin.xhs:id/desc",
            "com.xingin.xhs:id/et_desc",
            "com.xingin.xhs:id/content",
            "com.xingin.discover:id/desc",
            "com.xingin.discover:id/et_desc",
            "com.xingin.discover:id/content",
        ):
            if self._click_element(resource_id=res_id, timeout=2):
                desc_focused = True
                break

        if not desc_focused:
            for placeholder in (
                "添加正文", "Add content", "说点什么", "写点什么",
                "描述", "正文", "Description",
            ):
                if self._click_element(text=placeholder, timeout=2):
                    desc_focused = True
                    log.debug("通过占位文字点击描述框: '%s'", placeholder)
                    break

        if not desc_focused:
            dx, dy = self._rel_to_abs(*XHS_COORDS["desc_field"])
            self._tap(dx, dy)

        self._wait(0.5)

        # 输入描述（含中文，走剪贴板）
        has_complex_desc = any(ord(c) > 127 for c in desc_body)
        if has_complex_desc:
            self._copy_to_clipboard(desc_body)
            self._wait(0.3)
            self._press_key(279)  # KEYCODE_PASTE
        else:
            self._type_text(desc_body)

        self._wait(0.5)

        # ── 3. 添加话题标签 ──────────────────────────────────────────────────
        if tags:
            log.debug("添加小红书话题标签: %s", tag_str)

            # 方案A：查找 "#" / "话题" / "添加话题" 按钮，逐个输入
            topic_btn_found = False
            for label in ("添加话题", "#", "话题", "Topic", "Hashtag", "Add topic"):
                elem = self._find_element(text=label)
                if elem:
                    self._tap(elem["x"], elem["y"])
                    topic_btn_found = True
                    log.debug("通过文字找到话题按钮: '%s'", label)
                    self._wait(0.5)
                    break

            if not topic_btn_found:
                # 坐标兜底
                tbx, tby = self._rel_to_abs(*XHS_COORDS["topic_button"])
                self._tap(tbx, tby)
                self._wait(0.5)

            # 方案B（更可靠）：在描述末尾追加 #话题 格式
            # 先将光标移至末尾再追加
            self._press_key(123)  # KEYCODE_MOVE_END
            self._wait(0.3)

            # 追加换行 + 话题字符串
            append_text = "\n" + tag_str
            has_complex_tags = any(ord(c) > 127 for c in append_text)
            if has_complex_tags:
                self._copy_to_clipboard(append_text)
                self._wait(0.3)
                self._press_key(279)  # KEYCODE_PASTE
            else:
                self._type_text(append_text)

            self._wait(0.5)

        # ── 4. 收起键盘 ─────────────────────────────────────────────────────
        self._press_key(4)   # KEYCODE_BACK 收起软键盘
        self._wait(0.8)

        log.info("小红书帖子详情填写完成")
        return True

    # ─────────────────────────────────────────────
    # 确认发布
    # ─────────────────────────────────────────────

    def confirm_publish(self) -> bool:
        """
        点击小红书最终 "发布" 按钮。

        小红书发布按钮文字通常是 "发布"（中文）或 "Publish" / "Post"（英文）。

        Returns:
            True 表示成功点击发布按钮
        """
        log.debug("查找并点击小红书 '发布' 按钮")

        for label in ("发布", "Publish", "Post", "发布笔记", "发布视频", "Submit"):
            if self._click_element(text=label, timeout=8):
                log.info("点击发布按钮: '%s'", label)
                self._wait(2)
                return True

        # resource-id 方式
        for res_id in (
            "com.xingin.xhs:id/publish_btn",
            "com.xingin.xhs:id/btn_publish",
            "com.xingin.xhs:id/publish",
            "com.xingin.discover:id/publish_btn",
            "com.xingin.discover:id/btn_publish",
        ):
            if self._click_element(resource_id=res_id, timeout=3):
                log.info("通过 resource-id 点击发布按钮: '%s'", res_id)
                self._wait(2)
                return True

        # 坐标兜底
        pbx, pby = self._rel_to_abs(*XHS_COORDS["publish_button"])
        result = self._tap(pbx, pby)
        if result:
            log.info("按坐标点击发布按钮")
            self._wait(2)
        return result

    # ─────────────────────────────────────────────
    # 验证发布结果
    # ─────────────────────────────────────────────

    def verify_published(self) -> Optional[str]:
        """
        等待小红书显示发布成功信号。

        小红书发布后通常会：
          - 显示 "发布成功" / "已发布" Toast
          - 跳回首页 Feed 或个人主页
          - 显示 "审核中" 也视为已提交成功

        等待最多 45 秒（视频处理较慢）。

        Returns:
            伪 post_id（格式 "xhs_<时间戳>"）；超时未检测到成功信号返回 None
        """
        log.debug("等待小红书发布成功信号（最多45s）...")

        success_keywords = [
            # 中文成功提示
            "发布成功", "已发布", "发布完成",
            "审核中", "正在发布", "上传成功",
            # 英文成功提示
            "Published", "Post published", "Upload complete",
            "Under review", "Submitted",
        ]

        # 首页/个人主页回到主界面的关键词
        home_keywords = [
            "首页", "发现", "消息", "我", "关注",
            "Home", "Explore", "Inbox", "Profile", "Following",
        ]

        deadline = time.time() + 45
        while time.time() < deadline:
            # 检查发布成功提示
            for kw in success_keywords:
                if self._find_element(text=kw):
                    log.info("检测到小红书发布成功信号: '%s'", kw)
                    return f"xhs_{int(time.time())}"
            self._wait(2)

        # 超时后再检查是否已回到主界面（也视为发布完成）
        for kw in home_keywords:
            if self._find_element(text=kw):
                log.info("已返回小红书主界面，视为发布成功")
                return f"xhs_{int(time.time())}"

        log.warning("未检测到小红书发布成功信号，发布可能失败或正在后台处理")
        return None
