# -*- coding: utf-8 -*-
"""
ADB 发布器基类 — 所有平台发布器的公共基础。

设计原则:
- 一套通用 ADB 操作框架，各平台只实现差异部分
- 发布前自动检查设备连接和App状态
- 发布失败自动截图保存供调试
- 所有操作有超时保护，不会永久卡住
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass
class PublishResult:
    """发布操作结果"""
    success: bool
    platform: str
    post_id: Optional[str] = None
    error: Optional[str] = None
    screenshot_path: Optional[str] = None
    duration_sec: float = 0.0

    def __str__(self) -> str:
        if self.success:
            return f"[{self.platform}] 发布成功 post_id={self.post_id} 耗时={self.duration_sec:.1f}s"
        return f"[{self.platform}] 发布失败 error={self.error} 耗时={self.duration_sec:.1f}s"


class BasePublisher(ABC):
    """
    ADB 发布器抽象基类。

    子类只需实现以下6个抽象方法：
      - get_package_name()
      - navigate_to_upload()
      - select_video_file()
      - fill_post_details()
      - confirm_publish()
      - verify_published()
    """

    # 设备上视频的临时存放目录
    REMOTE_VIDEO_DIR = "/sdcard/DCIM/studio/"

    def __init__(
        self,
        device_id: Optional[str] = None,
        config_path: Optional[str] = None,
    ) -> None:
        self._device_id = device_id
        self._config: Dict = {}
        self._screen_size: Optional[Tuple[int, int]] = None

        # 加载配置文件（如有）
        if config_path:
            self._load_config(config_path)

        log.debug(
            "%s 初始化完成 device_id=%s",
            self.__class__.__name__,
            device_id or "（自动选择）",
        )

    # ─────────────────────────────────────────────
    # 属性
    # ─────────────────────────────────────────────

    @property
    def device_id(self) -> Optional[str]:
        return self._device_id

    @property
    def adb_prefix(self) -> str:
        """生成 adb 命令前缀，有设备 ID 时精确指定"""
        if self._device_id:
            return f"adb -s {self._device_id}"
        return "adb"

    # ─────────────────────────────────────────────
    # 内部工具
    # ─────────────────────────────────────────────

    def _load_config(self, config_path: str) -> None:
        """加载 JSON 配置文件"""
        import json
        try:
            with open(config_path, encoding="utf-8") as f:
                self._config = json.load(f)
            log.debug("配置文件已加载: %s", config_path)
        except FileNotFoundError:
            log.warning("配置文件不存在: %s", config_path)
        except Exception as e:
            log.error("加载配置文件失败: %s", e)

    # ─────────────────────────────────────────────
    # ADB 基础操作
    # ─────────────────────────────────────────────

    def _adb(self, cmd: str, timeout: int = 30) -> Tuple[int, str]:
        """
        执行 ADB 命令。

        Args:
            cmd: adb 子命令，例如 "shell input tap 100 200"
            timeout: 超时秒数

        Returns:
            (returncode, output_text) 元组
        """
        full_cmd = f"{self.adb_prefix} {cmd}"
        log.debug("ADB 执行: %s", full_cmd)
        try:
            result = subprocess.run(
                full_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode != 0:
                log.debug("ADB 返回非零: %d  output=%s", result.returncode, output[:200])
            return result.returncode, output
        except subprocess.TimeoutExpired:
            log.warning("ADB 命令超时 (%ds): %s", timeout, full_cmd)
            return -1, "timeout"
        except Exception as e:
            log.error("ADB 命令异常: %s  error=%s", full_cmd, e)
            return -1, str(e)

    def _tap(self, x: int, y: int) -> bool:
        """点击屏幕坐标"""
        rc, _ = self._adb(f"shell input tap {x} {y}")
        return rc == 0

    def _swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 500,
    ) -> bool:
        """滑动屏幕"""
        rc, _ = self._adb(f"shell input swipe {x1} {y1} {x2} {y2} {duration_ms}")
        return rc == 0

    def _type_text(self, text: str) -> bool:
        """
        通过 ADB 输入文字。
        特殊字符（空格、中文等）会先 URL-encode，再用 am broadcast 方式输入；
        纯 ASCII 简单文字直接用 input text。
        """
        # URL-encode 特殊字符
        encoded = urllib.parse.quote(text, safe="")
        rc, _ = self._adb(f"shell input text '{encoded}'")
        if rc != 0:
            # 备用：直接引号包裹（某些设备需要）
            safe_text = text.replace("'", "\\'").replace('"', '\\"')
            rc, _ = self._adb(f'shell input text "{safe_text}"')
        return rc == 0

    def _press_key(self, keycode: int) -> bool:
        """发送按键事件（Android KeyEvent 代码）"""
        rc, _ = self._adb(f"shell input keyevent {keycode}")
        return rc == 0

    def _wait(self, seconds: float) -> None:
        """等待指定秒数"""
        time.sleep(seconds)

    def _wait_random(self, base_seconds: float, variance: float = 0.3) -> None:
        """等待 base_seconds * (1 ± variance) 的随机时间，模拟人类操作节奏。"""
        import random
        delta = base_seconds * variance
        actual = base_seconds + random.uniform(-delta, delta)
        actual = max(0.1, actual)  # 最少等 0.1 秒
        time.sleep(actual)

    def _tap_fuzzy(self, x: int, y: int, fuzz: int = 4) -> bool:
        """在目标坐标附近随机偏移 ±fuzz 像素后点击，模拟人类点击不精准。"""
        import random
        fx = x + random.randint(-fuzz, fuzz)
        fy = y + random.randint(-fuzz, fuzz)
        return self._tap(fx, fy)

    def _human_scroll(self, direction: str = "down", count: int = 3, device_id: str = None) -> None:
        """
        模拟人类滑动浏览：速度变化、停顿不均。
        direction: 'down' 或 'up'
        count: 滑动次数
        """
        import random
        did = device_id or self.device_id
        if not did:
            return
        # 屏幕中央区域随机滑动
        cx = random.randint(400, 600)
        for i in range(count):
            start_y = random.randint(600, 800) if direction == "down" else random.randint(300, 500)
            end_y   = random.randint(200, 400) if direction == "down" else random.randint(700, 900)
            duration = random.randint(300, 700)  # 滑动持续时间ms
            try:
                import subprocess
                subprocess.run(
                    ["adb", "-s", did, "shell", "input", "swipe",
                     str(cx), str(start_y), str(cx), str(end_y), str(duration)],
                    capture_output=True, timeout=5
                )
            except Exception:
                pass
            time.sleep(random.uniform(0.8, 2.5))  # 每次滑动后随机停留

    def _screenshot(self, save_path: Optional[str] = None) -> Optional[str]:
        """
        截取当前屏幕并拉取到本地。

        Args:
            save_path: 本地保存路径；为 None 时自动生成临时文件

        Returns:
            本地文件路径；失败返回 None
        """
        remote_path = "/sdcard/studio_screenshot_tmp.png"

        # 截图到设备
        rc, _ = self._adb(f"shell screencap -p {remote_path}")
        if rc != 0:
            log.error("截图失败")
            return None

        # 确定本地保存路径
        if save_path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = os.path.join(
                tempfile.gettempdir(),
                f"studio_screenshot_{ts}.png",
            )

        # 拉取文件
        rc, _ = self._adb(f"pull {remote_path} {save_path}")
        if rc != 0:
            log.error("截图 pull 失败: %s", save_path)
            return None

        log.debug("截图已保存: %s", save_path)
        return save_path

    def _push_file(self, local_path: str, remote_path: str) -> bool:
        """将本地文件推送到设备"""
        if not os.path.exists(local_path):
            log.error("本地文件不存在: %s", local_path)
            return False

        # 确保远端目录存在
        remote_dir = remote_path.rsplit("/", 1)[0]
        self._adb(f"shell mkdir -p {remote_dir}")

        rc, out = self._adb(f"push {local_path} {remote_path}", timeout=120)
        if rc != 0:
            log.error("文件推送失败: %s → %s  %s", local_path, remote_path, out)
            return False

        log.info("文件推送成功: %s → %s", local_path, remote_path)
        return True

    def _find_element(
        self,
        text: str = "",
        resource_id: str = "",
        class_name: str = "",
    ) -> Optional[Dict]:
        """
        通过 uiautomator dump 在当前屏幕上查找 UI 元素。

        Args:
            text: 元素显示文本（支持部分匹配）
            resource_id: 元素 resource-id 属性
            class_name: 元素 class 属性

        Returns:
            包含 {"x", "y", "width", "height"} 的字典；未找到返回 None
        """
        remote_dump = "/sdcard/studio_ui_dump.xml"
        rc, _ = self._adb(f"shell uiautomator dump {remote_dump}")
        if rc != 0:
            log.warning("uiautomator dump 失败")
            return None

        local_dump = os.path.join(tempfile.gettempdir(), "studio_ui_dump.xml")
        rc, _ = self._adb(f"pull {remote_dump} {local_dump}")
        if rc != 0:
            log.warning("ui dump pull 失败")
            return None

        try:
            tree = ET.parse(local_dump)
        except ET.ParseError as e:
            log.warning("ui dump XML 解析失败: %s", e)
            return None

        for node in tree.iter("node"):
            attrib = node.attrib

            # 按条件过滤
            if text and text.lower() not in attrib.get("text", "").lower():
                continue
            if resource_id and resource_id not in attrib.get("resource-id", ""):
                continue
            if class_name and class_name not in attrib.get("class", ""):
                continue

            # 解析 bounds 属性，格式: [x1,y1][x2,y2]
            bounds = attrib.get("bounds", "")
            m = re.findall(r"\d+", bounds)
            if len(m) < 4:
                continue

            x1, y1, x2, y2 = int(m[0]), int(m[1]), int(m[2]), int(m[3])
            return {
                "x": (x1 + x2) // 2,
                "y": (y1 + y2) // 2,
                "width": x2 - x1,
                "height": y2 - y1,
            }

        log.debug(
            "未找到元素 text=%r resource_id=%r class_name=%r",
            text,
            resource_id,
            class_name,
        )
        return None

    def _click_element(
        self,
        text: str = "",
        resource_id: str = "",
        timeout: int = 10,
    ) -> bool:
        """
        查找并点击元素。

        Args:
            text: 元素文本
            resource_id: 元素 resource-id
            timeout: 查找超时（秒）

        Returns:
            是否点击成功
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            elem = self._find_element(text=text, resource_id=resource_id)
            if elem:
                return self._tap(elem["x"], elem["y"])
            self._wait(1)

        log.warning("点击元素超时: text=%r resource_id=%r", text, resource_id)
        return False

    def _wait_for_element(
        self,
        text: str = "",
        resource_id: str = "",
        timeout: int = 15,
    ) -> bool:
        """
        等待指定元素出现在屏幕上。

        Returns:
            元素是否在超时内出现
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._find_element(text=text, resource_id=resource_id):
                return True
            self._wait(1)

        log.warning("等待元素超时: text=%r resource_id=%r", text, resource_id)
        return False

    def _get_screen_size(self) -> Tuple[int, int]:
        """
        获取设备屏幕分辨率。

        Returns:
            (width, height) 像素元组
        """
        if self._screen_size:
            return self._screen_size

        _, out = self._adb("shell wm size")
        # 输出格式: "Physical size: 1080x2400"
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            self._screen_size = (w, h)
            log.debug("屏幕分辨率: %dx%d", w, h)
            return self._screen_size

        # 默认兜底值（常见 FHD+ 竖屏）
        log.warning("无法获取屏幕分辨率，使用默认 1080x2400")
        self._screen_size = (1080, 2400)
        return self._screen_size

    def _rel_to_abs(self, rx: float, ry: float) -> Tuple[int, int]:
        """将相对坐标（0-1）转换为绝对像素坐标"""
        w, h = self._get_screen_size()
        return int(rx * w), int(ry * h)

    def _open_app(self, package_name: str) -> bool:
        """启动 App（通过 monkey 兼容无 launchable-activity 的包名）"""
        rc, out = self._adb(
            f"shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1"
        )
        if rc != 0:
            log.error("启动 App 失败: %s  %s", package_name, out)
            return False
        log.info("App 已启动: %s", package_name)
        return True

    def _close_app(self, package_name: str) -> bool:
        """强制停止 App"""
        rc, _ = self._adb(f"shell am force-stop {package_name}")
        return rc == 0

    def _is_connected(self) -> bool:
        """检查 ADB 设备是否已连接"""
        _, out = self._adb("devices")
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # 去掉头行 "List of devices attached"
        device_lines = [l for l in lines if "\t" in l and "device" in l]

        if not device_lines:
            log.warning("没有检测到已连接的 ADB 设备")
            return False

        # 如果指定了 device_id，确认它在列表中
        if self._device_id:
            for line in device_lines:
                if self._device_id in line:
                    return True
            log.warning("指定设备 %s 未在已连接列表中", self._device_id)
            return False

        return True

    def _clear_clipboard(self) -> None:
        """清空设备剪贴板"""
        # 用空字符串覆盖剪贴板
        self._adb(
            'shell am broadcast -a clipper.set -e text ""'
        )

    def _copy_to_clipboard(self, text: str) -> None:
        """
        将文字复制到设备剪贴板。
        优先使用 Clipper 广播；不可用时退回 ADB input text 方式。
        """
        # 方案1: 使用 Clipper App 广播（需预装 Clipper）
        escaped = text.replace('"', '\\"')
        rc, _ = self._adb(
            f'shell am broadcast -a clipper.set -e text "{escaped}"'
        )
        if rc == 0:
            log.debug("文字已写入剪贴板 (Clipper)")
            return

        # 方案2: 通过 input text 直接输入（不经剪贴板）
        log.debug("Clipper 不可用，改用 input text 输入")
        self._type_text(text)

    # ─────────────────────────────────────────────
    # 抽象方法 — 子类必须实现
    # ─────────────────────────────────────────────

    @abstractmethod
    def get_package_name(self) -> str:
        """返回目标 App 的 Android 包名"""
        ...

    @abstractmethod
    def navigate_to_upload(self) -> bool:
        """从 App 主界面导航到上传/发布入口"""
        ...

    @abstractmethod
    def select_video_file(self, remote_path: str) -> bool:
        """在 App 内从相册/文件选择器中选中指定视频"""
        ...

    @abstractmethod
    def fill_post_details(self, caption: str, hashtags: List[str]) -> bool:
        """填写帖子文案和话题标签"""
        ...

    @abstractmethod
    def confirm_publish(self) -> bool:
        """点击最终发布/分享按钮"""
        ...

    @abstractmethod
    def verify_published(self) -> Optional[str]:
        """
        等待并验证发布成功。

        Returns:
            成功时返回 post_id（字符串），失败返回 None
        """
        ...

    # ─────────────────────────────────────────────
    # 主流程编排（子类通常无需重写）
    # ─────────────────────────────────────────────

    def publish(
        self,
        video_path: str,
        caption: str,
        hashtags: List[str] = [],
        schedule_time: Optional[datetime] = None,
    ) -> PublishResult:
        """
        完整发布流程编排。

        流程：
          1. 检查设备连接
          2. 推送视频到设备
          3. 启动 App
          4. 等待 App 加载（3s）
          5. 导航到上传入口
          6. 选择视频文件
          7. 填写帖子详情
          8. 确认发布
          9. 验证发布结果
         10. 关闭 App

        Args:
            video_path: 本地视频文件路径
            caption:    帖子文案
            hashtags:   话题标签列表（不含 #）
            schedule_time: 预约发布时间（暂留接口，各平台自行实现）

        Returns:
            PublishResult
        """
        platform = self.__class__.__name__.replace("Publisher", "").lower()
        start_ts = time.time()
        screenshot_path: Optional[str] = None

        def _fail(reason: str) -> PublishResult:
            # 失败时自动截图
            try:
                sp = self._screenshot()
                nonlocal screenshot_path
                screenshot_path = sp
            except Exception:
                pass
            return PublishResult(
                success=False,
                platform=platform,
                error=reason,
                screenshot_path=screenshot_path,
                duration_sec=time.time() - start_ts,
            )

        # 步骤 1：检查设备连接
        log.info("[%s] 步骤1/10: 检查设备连接", platform)
        if not self._is_connected():
            return _fail("ADB 设备未连接")

        # 步骤 1.5：代理健康熔断检查（Phase 7 P1）
        # 如果设备的代理出口IP泄漏熔断已打开，拒绝发布
        # 避免在IP泄漏状态下留下 TikTok 发布记录（被识别IP封号风险极高）
        circuit_check = _check_proxy_circuit_breaker(self.device_id)
        if circuit_check.get("blocked"):
            reason = circuit_check.get("reason", "代理熔断中")
            log.warning("[%s] 步骤1.5/10: 代理熔断拒绝发布 (%s): %s",
                        platform, self.device_id[:12], reason)
            return _fail(f"代理安全检查失败: {reason}")

        # 步骤 2：推送视频到设备
        log.info("[%s] 步骤2/10: 推送视频 %s", platform, video_path)
        filename = os.path.basename(video_path)
        remote_path = self.REMOTE_VIDEO_DIR + filename
        if not self._push_file(video_path, remote_path):
            return _fail(f"视频推送失败: {video_path}")

        # 让媒体库扫描新文件
        self._adb(
            f"shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE"
            f" -d file://{remote_path}"
        )
        self._wait(1)

        # 步骤 3：启动 App
        log.info("[%s] 步骤3/10: 启动 App %s", platform, self.get_package_name())
        if not self._open_app(self.get_package_name()):
            return _fail("App 启动失败")

        # 步骤 4：等待 App 加载
        log.info("[%s] 步骤4/10: 等待 App 加载 (3s)", platform)
        self._wait(3)

        # 人类行为：打开App后先随机浏览几秒再去上传
        import random
        import time as _time
        _browse_time = random.randint(15, 45)  # 随机浏览 15-45 秒
        log.info("模拟发布前浏览 %s 秒...", _browse_time)
        _scroll_count = random.randint(3, 8)
        self._human_scroll("down", _scroll_count, self.device_id)
        _time.sleep(random.uniform(2, 5))

        # 步骤 5：导航到上传入口
        log.info("[%s] 步骤5/10: 导航到上传入口", platform)
        if not self.navigate_to_upload():
            return _fail("导航到上传页失败")

        # 步骤 6：选择视频文件
        log.info("[%s] 步骤6/10: 选择视频文件 %s", platform, remote_path)
        if not self.select_video_file(remote_path):
            return _fail("选择视频失败")

        # 步骤 7：填写帖子详情
        log.info("[%s] 步骤7/10: 填写文案和标签", platform)
        if not self.fill_post_details(caption, hashtags):
            return _fail("填写帖子详情失败")

        # 步骤 8：确认发布
        log.info("[%s] 步骤8/10: 确认发布", platform)
        if not self.confirm_publish():
            return _fail("点击发布按钮失败")

        # 步骤 9：验证发布结果
        log.info("[%s] 步骤9/10: 验证发布结果", platform)
        post_id = self.verify_published()
        if post_id is None:
            return _fail("发布验证失败（未检测到成功信号）")

        # 步骤 10：关闭 App
        log.info("[%s] 步骤10/10: 关闭 App", platform)
        self._close_app(self.get_package_name())

        duration = time.time() - start_ts
        log.info("[%s] 发布完成 post_id=%s 耗时=%.1fs", platform, post_id, duration)

        return PublishResult(
            success=True,
            platform=platform,
            post_id=post_id,
            duration_sec=duration,
        )


# ─────────────────────────────────────────────
# Phase 7 P1: 代理熔断检查（设备状态联动）
# ─────────────────────────────────────────────

def _check_proxy_circuit_breaker(device_id: str) -> dict:
    """检查设备的代理熔断状态，决定是否允许发布。

    设计思路：
      - 熔断（circuit_open=True）意味着设备出口IP泄漏或无法获取
      - 在此状态下发布内容，TikTok会关联到泄漏的真实IP，风险极高
      - 因此熔断中的设备应暂停所有内容发布，等代理恢复后再继续
      - unverified（代理IP未知）属于「无法确认但可能安全」，允许发布（降级策略）
      - no_ip / leak 状态是明确有问题的，阻止发布

    Args:
        device_id: ADB 设备序列号

    Returns:
        {
          blocked: bool,       # True = 禁止发布
          reason: str,         # 阻止原因（blocked=True时）
          state: str,          # 4态状态机状态
          circuit_open: bool,  # 熔断器是否打开
          cooldown_remaining: int,  # 冷却剩余秒数
        }
    """
    try:
        from src.behavior.proxy_health import get_proxy_health_monitor
        monitor = get_proxy_health_monitor()

        with monitor._status_lock:
            status = monitor._status_cache.get(device_id)

        if status is None:
            # 设备未注册到监控系统，允许发布（监控可能未启动）
            return {"blocked": False, "reason": "", "state": "unknown",
                    "circuit_open": False, "cooldown_remaining": 0}

        state = status.state
        circuit_open = status.circuit_open

        if circuit_open:
            import time as _time
            cooldown = max(0, int(900 - (_time.time() - status.circuit_open_time)))
            return {
                "blocked": True,
                "reason": (
                    f"代理熔断保护中（连续{status.consecutive_fails}次IP异常）"
                    f"，冷却剩余{cooldown}s，请等待代理恢复或手动重置熔断"
                ),
                "state": state,
                "circuit_open": True,
                "cooldown_remaining": cooldown,
            }

        if state == "leak":
            return {
                "blocked": True,
                "reason": (
                    f"检测到IP泄漏（实际IP={status.actual_ip}，"
                    f"期望IP={status.expected_ip}），禁止发布以保护账号安全"
                ),
                "state": state,
                "circuit_open": False,
                "cooldown_remaining": 0,
            }

        if state == "no_ip":
            return {
                "blocked": True,
                "reason": "无法获取设备出口IP（ADB或网络故障），禁止发布以防止风险",
                "state": state,
                "circuit_open": False,
                "cooldown_remaining": 0,
            }

        # ok 或 unverified — 允许发布
        return {
            "blocked": False,
            "reason": "",
            "state": state,
            "circuit_open": False,
            "cooldown_remaining": 0,
        }

    except ImportError:
        # proxy_health 模块不可用（测试环境等），允许发布
        log.debug("[Publisher] proxy_health 不可用，跳过熔断检查")
        return {"blocked": False, "reason": "", "state": "unknown",
                "circuit_open": False, "cooldown_remaining": 0}
    except Exception as e:
        # 任何异常都不阻止发布（安全降级）
        log.warning("[Publisher] 熔断检查异常（允许发布）: %s", e)
        return {"blocked": False, "reason": "", "state": "error",
                "circuit_open": False, "cooldown_remaining": 0}
