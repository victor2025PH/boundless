#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 应用自动化模块 (v3 — u2 原子操作优先)

核心设计:
  所有UI交互优先通过 uiautomator2 在设备端原子完成。
  "查找元素 + 点击" 不再分两步跨主机与设备，而是在设备上一步完成。
  这从根本上解决了：
    - 发送按钮位置随文字多少变化的问题
    - 不同输入法高度不同导致的布局偏移
    - dump → 解析 → tap 之间的时间差导致的位置不准
    - Unicode/中文文本输入（u2.send_keys 原生支持）

  当 u2 不可用时，自动降级为 ADB dump + input tap 方案。
"""

import time
import logging
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path
from dataclasses import dataclass
from enum import Enum

from ..device_control.device_manager import DeviceManager, UIElement, get_device_manager
from ..behavior.human_behavior import HumanBehavior, get_profile
from ..behavior.compliance_guard import get_compliance_guard, QuotaExceeded


# =============================================================================
# u2 选择器定义（传给 uiautomator2 的参数）
# 每个UI元素定义多组选择器，按优先级排列
# =============================================================================

class TG:
    """
    Telegram UI 元素的 u2 选择器策略。
    基于 2026-03-12 在 Redmi 13C (Android 13) 上实测数据。
    实测发现: Telegram 几乎不用 resource-id，全靠 content-desc (description)。
    """

    SEARCH_ICON = [
        {"description": "Search"},
        {"description": "搜索"},
    ]

    SEARCH_INPUT = [
        {"className": "android.widget.EditText", "packageName": "org.telegram.messenger"},
    ]

    MESSAGE_INPUT = [
        {"className": "android.widget.EditText", "packageName": "org.telegram.messenger"},
    ]

    SEND_BUTTON = [
        {"description": "Send"},
        {"description": "发送"},
    ]

    VOICE_BUTTON = [
        {"description": "Record voice message"},
        {"description": "Record video message"},
    ]

    ATTACH_BUTTON = [
        {"description": "Attach media"},
        {"description": "附件"},
    ]

    BACK_BUTTON = [
        {"description": "Go back"},
        {"description": "返回"},
        {"description": "Navigate up"},
    ]

    EMOJI_BUTTON = [
        {"description": "Emoji, stickers, and GIFs"},
    ]


# Legacy dump 方案用的选择器（字段名与 u2 不同）
class TGLegacy:
    """Legacy ADB dump 方案的搜索条件（基于实测数据）"""

    SEARCH_ICON = [
        {"content_desc_contains": "Search"},
        {"content_desc_contains": "搜索"},
    ]
    SEARCH_INPUT = [
        {"class_name": "EditText", "package": "org.telegram.messenger"},
    ]
    MESSAGE_INPUT = [
        {"class_name": "EditText", "package": "org.telegram.messenger"},
    ]
    SEND_BUTTON = [
        {"content_desc_contains": "Send"},
        {"content_desc_contains": "发送"},
    ]
    VOICE_BUTTON = [
        {"content_desc_contains": "Record voice"},
        {"content_desc_contains": "Record video"},
    ]


class TelegramAction(Enum):
    START_APP = "start_app"
    SEARCH_USER = "search_user"
    SEND_TEXT = "send_text"
    SEND_IMAGE = "send_image"
    TAKE_SCREENSHOT = "take_screenshot"


@dataclass
class TelegramConfig:
    package_name: str = "org.telegram.messenger"
    main_activity: str = "org.telegram.ui.LaunchActivity"
    element_timeout: float = 15.0
    action_delay: float = 0.5
    max_retries: int = 3


class TelegramAutomation:
    """基于 u2 原子操作的 Telegram 自动化（带 ADB dump fallback）"""

    PLATFORM = "telegram"

    def __init__(self, device_manager: DeviceManager,
                 config: Optional[TelegramConfig] = None):
        self.logger = logging.getLogger(__name__)
        self.dm = device_manager
        self.cfg = config or TelegramConfig()
        self.current_device_id: Optional[str] = None
        self.hb = HumanBehavior(profile=get_profile("telegram"))
        self.guard = get_compliance_guard()
        self._current_account: str = ""

    # =========================================================================
    # 设备管理
    # =========================================================================

    def set_current_device(self, device_id: str) -> None:
        info = self.dm.get_device_info(device_id)
        if not info:
            raise ValueError(f"设备不存在: {device_id}")
        if info.status.value != "connected":
            raise ValueError(f"设备未连接: {device_id}")
        self.current_device_id = device_id
        self.logger.info(f"设置当前设备: {info.display_name} (u2={'可用' if self.dm.get_u2(device_id) else '不可用'})")

    def _did(self, device_id: Optional[str] = None) -> str:
        did = device_id or self.current_device_id
        if not did:
            raise ValueError("未指定设备ID")
        return did

    def _use_u2(self, device_id: str) -> bool:
        """判断当前设备是否可用 u2"""
        return self.dm.get_u2(device_id) is not None

    # =========================================================================
    # 核心交互：u2 优先，legacy fallback
    # =========================================================================

    def _click_element(self, device_id: str,
                       u2_strategies: List[Dict],
                       legacy_strategies: List[Dict],
                       label: str = "元素",
                       timeout: Optional[float] = None) -> bool:
        """
        统一的元素点击方法。
        u2可用时用原子操作，否则降级为 dump+tap。
        """
        t = timeout or self.cfg.element_timeout

        if self._use_u2(device_id):
            ok = self.dm.u2_click_multi(device_id, u2_strategies, timeout=t)
            if ok:
                return True
            self.logger.debug(f"u2 点击{label}失败，尝试legacy")

        ok = self.dm.tap_element_multi(device_id, legacy_strategies, timeout=t)
        if ok:
            return True

        self.logger.error(f"未找到{label}（u2 和 legacy 均失败）")
        return False

    def _find_element(self, device_id: str,
                      u2_strategies: List[Dict],
                      legacy_strategies: List[Dict],
                      timeout: Optional[float] = None) -> bool:
        """检查元素是否存在"""
        t = timeout or self.cfg.element_timeout

        if self._use_u2(device_id):
            result = self.dm.u2_find_multi(device_id, u2_strategies, timeout=t)
            if result:
                return True

        elem = self.dm.find_element_multi(device_id, legacy_strategies, timeout=t)
        return elem is not None

    def _input_to_element(self, device_id: str, text: str,
                          u2_strategies: List[Dict],
                          legacy_strategies: List[Dict],
                          label: str = "输入框",
                          timeout: Optional[float] = None) -> bool:
        """找到输入框，点击聚焦，然后输入文本"""
        t = timeout or self.cfg.element_timeout

        if self._use_u2(device_id):
            for sel in u2_strategies:
                try:
                    ok = self.dm.u2_set_text(device_id, text, timeout=t, **sel)
                    if ok:
                        self.logger.info(f"u2 文本输入到{label}成功")
                        return True
                except Exception:
                    continue
            self.logger.debug(f"u2 输入到{label}失败，尝试legacy")

        # Legacy: 先点击输入框，再 input_text
        elem = self.dm.find_element_multi(device_id, legacy_strategies, timeout=t)
        if not elem:
            self.logger.error(f"未找到{label}")
            return False

        self.dm.input_tap(device_id, *elem.center)
        time.sleep(0.3)
        return self.dm.input_text(device_id, text)

    # =========================================================================
    # Telegram 操作
    # =========================================================================

    def start_telegram(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        self.logger.info(f"在设备 {did} 上启动Telegram")

        cmd = f"shell am start -n {self.cfg.package_name}/{self.cfg.main_activity}"
        ok, out = self.dm.execute_adb_command(cmd, did)
        if not ok:
            self.logger.error(f"Telegram启动失败: {out}")
            return False

        self.logger.info("Telegram启动成功，等待界面加载...")
        time.sleep(2)
        return True

    def search_and_open_user(self, username: str,
                             device_id: Optional[str] = None) -> bool:
        """搜索并打开用户聊天"""
        did = self._did(device_id)
        self.logger.info(f"在设备 {did} 上搜索用户: {username}")

        if not self._is_telegram_running(did):
            self.logger.info("Telegram未运行，启动中...")
            if not self.start_telegram(did):
                return False

        # 1) 点击搜索图标
        self.logger.info("步骤1: 点击搜索按钮")
        if not self._click_element(did, TG.SEARCH_ICON, TGLegacy.SEARCH_ICON, "搜索按钮"):
            return False
        time.sleep(self.cfg.action_delay)

        # 2) 等待搜索输入框并输入用户名
        self.logger.info(f"步骤2: 输入用户名 {username}")
        if not self._input_to_element(did, username,
                                      TG.SEARCH_INPUT, TGLegacy.SEARCH_INPUT,
                                      "搜索输入框", timeout=10.0):
            return False
        time.sleep(2)

        # 3) 点击搜索结果
        self.logger.info("步骤3: 点击搜索结果")
        if not self._click_search_result(did, username):
            self.logger.error("点击搜索结果失败")
            return False
        time.sleep(1.5)

        # 4) 验证进入聊天界面
        self.logger.info("步骤4: 验证聊天界面")
        if not self._find_element(did, TG.MESSAGE_INPUT, TGLegacy.MESSAGE_INPUT, timeout=8.0):
            self.logger.warning("未检测到消息输入框，可能是频道或需要加入的群组")
            return False

        # 5) 核对收件人——防止发错人
        self.logger.info("步骤5: 核对收件人")
        if not self._verify_recipient(did, username):
            self.logger.error(f"收件人核对失败！目标: {username}。中止操作并返回。")
            self.go_back(did)
            time.sleep(0.5)
            self.go_back(did)
            return False

        self.logger.info("成功进入聊天界面，收件人已核对")
        return True

    @staticmethod
    def _extract_usernames(text: str) -> List[str]:
        """从搜索结果文本中提取所有 @username（小写）"""
        import re
        return [m.lower() for m in re.findall(r'@(\w+)', text)]

    @staticmethod
    def _username_exact_match(text: str, target: str) -> bool:
        """
        严格匹配: 搜索结果文本中是否包含精确的 @username。
        例: target='ykj123' 会匹配 '@ykj123' 但不匹配 '@ykj123456'。
        """
        usernames = TelegramAutomation._extract_usernames(text)
        return target.lower() in usernames

    def _parse_search_results(self, xml: str) -> Tuple[List[Tuple[str, int, int]],
                                                        List[Tuple[str, int, int]]]:
        """
        解析搜索结果 XML，返回 (recent_items, global_items)。

        Telegram 搜索结果结构 (实测):
          - "Recent" 区域: ViewGroup, 文本只有显示名，不含 @username
          - "Global search" 标题之后: ViewGroup, 文本含 "@username"

        返回: 每个 item 是 (text, center_x, center_y)
        """
        from lxml import etree
        import re
        bounds_re = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')

        root = etree.fromstring(xml.encode('utf-8'))

        recent_items = []
        global_items = []
        in_global = False

        for node in root.iter('node'):
            pkg = node.get('package', '')
            text = node.get('text', '')
            cls = node.get('class', '')
            if 'telegram' not in pkg:
                continue

            if text == 'Global search' and 'TextView' in cls:
                in_global = True
                continue
            if text == 'Messages' and 'TextView' in cls:
                break

            if 'ViewGroup' in cls and text:
                bs = node.get('bounds', '')
                m = bounds_re.match(bs)
                if not m:
                    continue
                cx = (int(m.group(1)) + int(m.group(3))) // 2
                cy = (int(m.group(2)) + int(m.group(4))) // 2
                if in_global:
                    global_items.append((text, cx, cy))
                else:
                    recent_items.append((text, cx, cy))

        return recent_items, global_items

    def _verify_username_via_profile(self, device_id: str, target_username: str) -> bool:
        """
        点击进入聊天后，打开 Profile 页面核实 @username。
        如果匹配返回 True（停留在聊天中），否则返回 False（自动退回搜索）。
        """
        clean = target_username.lstrip('@').lower()
        d = self.dm.get_u2(device_id)
        if not d:
            return False

        try:
            from lxml import etree
            import re
            bounds_re = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')

            # 等待聊天界面加载
            time.sleep(1.5)
            xml = d.dump_hierarchy()
            root = etree.fromstring(xml.encode('utf-8'))

            # 查找标题栏可点击的 FrameLayout（包含用户名和在线状态）
            header = None
            for node in root.iter('node'):
                pkg = node.get('package', '')
                desc = node.get('content-desc', '')
                cls = node.get('class', '')
                click = node.get('clickable', '')
                if ('telegram' in pkg and 'FrameLayout' in cls
                        and click == 'true' and desc):
                    bs = node.get('bounds', '')
                    m = bounds_re.match(bs)
                    if m and int(m.group(4)) <= 200:
                        header = node
                        break

            if header is None:
                self.logger.warning("验证: 未找到聊天标题栏")
                d.press('back')
                time.sleep(0.5)
                return False

            # 点击标题进入 profile
            bs = header.get('bounds', '')
            m = bounds_re.match(bs)
            hx = (int(m.group(1)) + int(m.group(3))) // 2
            hy = (int(m.group(2)) + int(m.group(4))) // 2
            d.click(hx, hy)
            time.sleep(1.5)

            # 在 profile 页面搜索 @username
            profile_xml = d.dump_hierarchy()
            profile_root = etree.fromstring(profile_xml.encode('utf-8'))

            for node in profile_root.iter('node'):
                text = node.get('text', '')
                if text.startswith('@') and text.lstrip('@').lower() == clean:
                    self.logger.info(f"Profile 验证通过: {text}")
                    d.press('back')
                    time.sleep(0.5)
                    return True

            # 验证失败 — 收集 profile 中的 @username 用于日志
            found_ats = [n.get('text', '') for n in profile_root.iter('node')
                         if n.get('text', '').startswith('@')]
            self.logger.info(f"Profile 验证失败: 目标 @{clean}, 找到 {found_ats}")
            d.press('back')
            time.sleep(0.5)
            d.press('back')
            time.sleep(0.5)
            return False

        except Exception as e:
            self.logger.error(f"Profile 验证异常: {e}")
            try:
                d.press('back')
                time.sleep(0.3)
            except Exception:
                pass
            return False

    def _click_search_result(self, device_id: str, username: str) -> bool:
        """
        智能点击搜索结果（多级策略 + Profile 验证）。

        Telegram 搜索行为 (实测):
          - "Recent" 区域: 只显示显示名，不显示 @username
          - "Global search": 显示 "显示名, @username" 格式

        搜索策略:
          1. 在 Global search 中精确匹配 @username
          2. 点击 "Show more" 展开更多 Global 结果，再次精确匹配
          3. 对 Recent 区域的每个用户: 点击进入 → Profile 核实 @username
             匹配则停留，不匹配则退回继续下一个
          4. 非 @username 搜索: 在显示名中做包含匹配
        """
        clean = username.lstrip('@')
        has_at = username.startswith('@')

        if not self._use_u2(device_id):
            return self._click_search_result_legacy(device_id, username)

        d = self.dm.get_u2(device_id)
        if not d:
            return False

        try:
            from lxml import etree
            import re
            bounds_re = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')

            # 轮询等待搜索结果加载（最多 6 秒）
            recent, global_items = [], []
            for attempt in range(6):
                xml = d.dump_hierarchy()
                recent, global_items = self._parse_search_results(xml)
                if recent or global_items:
                    self.logger.info(f"搜索结果已加载: recent={len(recent)}, global={len(global_items)}")
                    break
                self.logger.info(f"等待搜索结果加载... ({attempt + 1}/6)")
                time.sleep(1)
            else:
                self.logger.error("搜索结果加载超时")
                return False

            # === 策略 1: Global search 精确 @username 匹配 ===
            if has_at:
                for text, cx, cy in global_items:
                    if self._username_exact_match(text, clean):
                        self.logger.info(f"Global 精确匹配: '{text[:50]}' -> ({cx},{cy})")
                        d.click(cx, cy)
                        return True

                # === 策略 2: 点击 "Show more" 展开更多 Global 结果 ===
                show_more = d(text='Show more')
                if show_more.exists:
                    self.logger.info("点击 'Show more' 展开更多结果")
                    show_more.click()
                    time.sleep(2)

                    xml2 = d.dump_hierarchy()
                    _, global_expanded = self._parse_search_results(xml2)

                    for text, cx, cy in global_expanded:
                        if self._username_exact_match(text, clean):
                            self.logger.info(f"Global(展开) 精确匹配: '{text[:50]}' -> ({cx},{cy})")
                            d.click(cx, cy)
                            return True

                    self.logger.info(f"Global search 展开后仍无 @{clean} 的精确匹配")

                # === 策略 3: Recent 逐个验证 @username ===
                if recent:
                    self.logger.info(f"尝试 Recent 区域 ({len(recent)} 个用户) 逐个 Profile 验证")
                    for text, cx, cy in recent:
                        self.logger.info(f"验证 Recent 用户: '{text[:40]}' -> ({cx},{cy})")
                        d.click(cx, cy)

                        if self._verify_username_via_profile(device_id, username):
                            self.logger.info(f"Recent 用户 '{text[:30]}' 验证为 @{clean}")
                            return True

                        # 验证失败，已自动退回搜索页，继续下一个
                        self.logger.info(f"'{text[:30]}' 不是 @{clean}，继续")
                        time.sleep(0.5)

                self.logger.error(f"所有搜索结果均无 @{clean} 的精确匹配")
                all_results = [t for t, _, _ in recent + global_items]
                if all_results:
                    self.logger.error(f"可用结果: {all_results[:8]}")
                return False

            else:
                # 非 @username 搜索: 显示名匹配
                for text, cx, cy in recent + global_items:
                    if clean.lower() in text.lower().split(',')[0]:
                        self.logger.info(f"显示名匹配: '{text[:50]}' -> ({cx},{cy})")
                        d.click(cx, cy)
                        return True

                self.logger.error(f"搜索结果中无 '{username}' 的显示名匹配")
                return False

        except Exception as e:
            self.logger.error(f"搜索结果点击异常: {e}")
            return False

    def _click_search_result_legacy(self, device_id: str, username: str) -> bool:
        """Legacy ADB dump 路径的搜索结果点击"""
        clean = username.lstrip('@')
        has_at = username.startswith('@')

        xml = self.dm.dump_ui_hierarchy(device_id)
        if not xml:
            return False

        all_elems = self.dm._parse_ui_elements(xml)

        if has_at:
            for e in all_elems:
                if self._username_exact_match(e.text, clean) and e.height > 40:
                    self.logger.info(f"Legacy 精确匹配: {e.text[:50]}")
                    return self.dm.input_tap(device_id, *e.center)
        else:
            for e in all_elems:
                if clean.lower() in e.text.lower().split(',')[0] and e.height > 40:
                    self.logger.info(f"Legacy 显示名匹配: {e.text[:50]}")
                    return self.dm.input_tap(device_id, *e.center)

        self.logger.error(f"Legacy 搜索结果中无 '{username}' 的匹配")
        return False

    def _verify_recipient(self, device_id: str, expected_username: str) -> bool:
        """
        进入聊天后的最终收件人核对。

        对于 @username 搜索:
          _click_search_result 已在搜索阶段通过 Profile 验证了身份。
          此处再做一次 Profile 验证作为最后防线。

        对于显示名搜索:
          验证聊天标题包含搜索词。
        """
        clean = expected_username.lstrip('@')
        has_at = expected_username.startswith('@')

        if not self._use_u2(device_id):
            self.logger.warning("Legacy 模式: 跳过收件人深度验证")
            return True

        d = self.dm.get_u2(device_id)
        if not d:
            return True

        try:
            from lxml import etree
            import re
            bounds_re = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')

            xml = d.dump_hierarchy()
            root = etree.fromstring(xml.encode('utf-8'))

            # 读取聊天标题 (顶部 y < 200 的 TextView)
            chat_title = ""
            for node in root.iter('node'):
                pkg = node.get('package', '')
                text = node.get('text', '')
                cls = node.get('class', '')
                if 'telegram' not in pkg or 'TextView' not in cls or not text:
                    continue
                bs = node.get('bounds', '')
                m = bounds_re.match(bs)
                if m and int(m.group(4)) <= 200:
                    chat_title = text
                    break

            if not chat_title:
                self.logger.warning("收件人验证: 未找到聊天标题")
                return False

            self.logger.info(f"收件人验证: 聊天标题 = '{chat_title}'")

            if has_at:
                # @username 搜索: 点击标题进 Profile 做最终确认
                header = d(className='android.widget.FrameLayout',
                           clickable=True,
                           packageName='org.telegram.messenger')
                # 找顶部区域的 FrameLayout
                for i in range(header.count):
                    info = header[i].info
                    b = info.get('bounds', {})
                    desc = info.get('contentDescription', '')
                    if b.get('bottom', 999) <= 200 and desc:
                        header[i].click()
                        time.sleep(1.5)

                        profile_xml = d.dump_hierarchy()
                        profile_root = etree.fromstring(profile_xml.encode('utf-8'))

                        for node in profile_root.iter('node'):
                            t = node.get('text', '')
                            if t.startswith('@') and t.lstrip('@').lower() == clean:
                                self.logger.info(f"收件人最终确认: {t}")
                                d.press('back')
                                time.sleep(0.5)
                                return True

                        found_ats = [n.get('text', '') for n in profile_root.iter('node')
                                     if n.get('text', '').startswith('@')]
                        self.logger.error(f"收件人验证失败！目标 @{clean}, 找到 {found_ats}")
                        d.press('back')
                        time.sleep(0.5)
                        return False

                self.logger.warning("收件人验证: 无法进入 Profile，信任搜索阶段验证")
                return True
            else:
                if clean.lower() in chat_title.lower():
                    self.logger.info(f"收件人验证通过: 标题包含 '{clean}'")
                    return True
                self.logger.error(f"收件人验证失败: 标题 '{chat_title}' 不含 '{clean}'")
                return False

        except Exception as e:
            self.logger.error(f"收件人验证异常: {e}")
            return False

    def send_text_message(self, message: str,
                          device_id: Optional[str] = None) -> bool:
        """
        发送文本消息。
        核心流程: 点击输入框 → 输入文本 → 点击发送。
        u2 方案下，每步都是设备端原子操作，不受键盘/布局变化影响。
        """
        did = self._did(device_id)
        self.logger.info(f"在设备 {did} 上发送消息: {message[:50]}...")

        # 1) 输入消息到输入框
        self.logger.info("步骤1: 输入消息到输入框")
        if not self._input_to_element(did, message,
                                      TG.MESSAGE_INPUT, TGLegacy.MESSAGE_INPUT,
                                      "消息输入框", timeout=10.0):
            self.logger.error("输入消息失败")
            return False
        time.sleep(self.cfg.action_delay)

        # 2) 点击发送按钮
        # 关键发现 (2026-03-12 实测): Telegram 的 Send 按钮区域与 EditText 有重叠，
        # 点击按钮中心可能会打到 EditText。必须点击按钮的右侧区域。
        self.logger.info("步骤2: 等待并点击发送按钮")
        if not self._click_send_button(did):
            self.logger.error("未找到发送按钮")
            return False
        time.sleep(0.5)

        # 3) 验证发送
        self.logger.info("步骤3: 验证发送结果")
        verified = self._verify_send(did, message)
        if verified:
            self.logger.info("消息发送成功（已验证）")
        else:
            self.logger.warning("消息可能已发送，但未完全验证")
        return True

    def _click_send_button(self, device_id: str, timeout: float = 10.0) -> bool:
        """
        点击发送按钮（特殊处理）。

        实测发现: Telegram 的 Send 按钮 bounds 与 EditText 存在重叠。
        Send 按钮 [520,1327][720,1423]，EditText [100,1332][620,1420]，
        X=520~620 区域两者重叠，点击按钮中心 (620,1375) 实际上打在 EditText 上。
        必须点击按钮的右侧区域（X > EditText.right）才能命中真正的发送图标。
        """
        if self._use_u2(device_id):
            d = self.dm.get_u2(device_id)
            if d:
                start = time.time()
                while time.time() - start < timeout:
                    for sel in TG.SEND_BUTTON:
                        elem = d(**sel)
                        if elem.exists:
                            info = elem.info
                            bounds = info.get('bounds', {})
                            right = bounds.get('right', 0)
                            top = bounds.get('top', 0)
                            bottom = bounds.get('bottom', 0)
                            # 点击按钮右侧 1/3 区域，避开与 EditText 的重叠
                            click_x = right - 40
                            click_y = (top + bottom) // 2
                            self.logger.info(f"点击发送按钮右侧 ({click_x}, {click_y}), bounds={bounds}")
                            d.click(click_x, click_y)
                            return True
                    time.sleep(0.5)

        # Legacy fallback
        return self._click_element(
            device_id, TG.SEND_BUTTON, TGLegacy.SEND_BUTTON,
            "发送按钮", timeout=timeout
        )

    def _verify_send(self, device_id: str, message: str) -> bool:
        """验证发送成功: 输入框清空 或 语音按钮重新出现"""
        time.sleep(1.0)

        if self._use_u2(device_id):
            d = self.dm.get_u2(device_id)
            if d:
                try:
                    # 检查输入框文本是否已清空
                    for sel in TG.MESSAGE_INPUT:
                        elem = d(**sel)
                        if elem.exists:
                            txt = elem.get_text() or ""
                            if message[:20] in txt:
                                return False
                            return True

                    # 检查语音按钮是否回归
                    for sel in TG.VOICE_BUTTON:
                        if d(**sel).exists:
                            return True
                except Exception:
                    pass

        # Legacy 验证
        xml = self.dm.dump_ui_hierarchy(device_id)
        if not xml:
            return False
        elems = self.dm._parse_ui_elements(xml)
        for s in TGLegacy.SEND_BUTTON:
            if self.dm._filter_elements(elems, **s):
                return False
        return True

    def send_screenshot(self, device_id: Optional[str] = None,
                        save_path: Optional[str] = None) -> bool:
        did = self._did(device_id)
        data = self.dm.capture_screen(did, save_path)
        if data:
            self.logger.info(f"截图成功: {len(data)} bytes")
            return True
        self.logger.error("截图失败")
        return False

    def send_file(self, file_path: str, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        device_path = f"/sdcard/{Path(file_path).name}"
        ok, out = self.dm.execute_adb_command(f"push {file_path} {device_path}", did)
        if not ok:
            self.logger.error(f"文件推送失败: {out}")
            return False
        if not self._click_element(did, TG.ATTACH_BUTTON, [{"resource_id": "chat_attach_button"}],
                                   "附件按钮"):
            return False
        time.sleep(1.5)
        self.logger.warning("文件发送UI交互待完善")
        return True

    def complete_workflow(self, username: str, message: str,
                         include_screenshot: bool = True) -> bool:
        self.logger.info(f"开始工作流: {username} → {message[:30]}...")

        if not self.search_and_open_user(username):
            return False

        if not self.send_text_message(message):
            return False

        if include_screenshot:
            ts = int(time.time())
            self.send_screenshot(save_path=f"logs/screenshots/workflow_{ts}.png")

        self.logger.info("工作流执行成功")
        return True

    # =========================================================================
    # 辅助方法
    # =========================================================================

    def _is_telegram_running(self, device_id: str) -> bool:
        ok, out = self.dm.execute_adb_command(
            f"shell pidof {self.cfg.package_name}", device_id
        )
        return ok and out.strip().isdigit()

    # =========================================================================
    # 消息读取
    # =========================================================================

    def read_messages(self, device_id: Optional[str] = None,
                      count: int = 20, scroll_pages: int = 0) -> List[dict]:
        """
        读取当前聊天中的消息。调用前需已进入聊天界面。

        返回 [{text, direction, time, status, raw}] 按时间正序。
        scroll_pages: 向上滚动页数以加载更多历史消息。
        """
        did = self._did(device_id)
        import re

        all_msgs = []
        seen_raw = set()

        def _parse_page():
            d = self.dm.get_u2(did)
            if not d:
                return []
            xml = d.dump_hierarchy()
            from lxml import etree
            root = etree.fromstring(xml.encode("utf-8"))
            page = []
            for node in root.iter("node"):
                if "telegram" not in node.get("package", ""):
                    continue
                cls = node.get("class", "")
                if "ViewGroup" not in cls:
                    continue
                text = node.get("text", "").strip()
                if not text or len(text) < 2:
                    continue
                # 过滤 UI 元素（非消息）
                if text in ("Message", "Add to contacts"):
                    continue
                if text.startswith("ADD ") and "TO CONTACTS" in text:
                    continue

                msg = self._parse_message_text(text)
                if msg and msg["raw"] not in seen_raw:
                    seen_raw.add(msg["raw"])
                    page.append(msg)
            return page

        # 先滚动加载历史（如果需要）
        if scroll_pages > 0:
            d = self.dm.get_u2(did)
            if d:
                for _ in range(scroll_pages):
                    d.swipe(360, 400, 360, 1200, duration=0.3)
                    time.sleep(1)
                    all_msgs.extend(_parse_page())

        # 解析当前页
        all_msgs.extend(_parse_page())

        # 去重 + 按位置排序（bounds y 坐标隐含在添加顺序中）
        unique = list({m["raw"]: m for m in all_msgs}.values())
        return unique[-count:]

    @staticmethod
    def _parse_message_text(raw: str) -> Optional[dict]:
        """
        解析单条消息的 ViewGroup text。

        格式实测:
          收到: "你好\\nReceived at 2:18 AM"
          发出: "Hello\\nSent at 8:28 AM, Seen"
          日期: "March 8"
        """
        import re
        lines = raw.strip().split("\n")
        if not lines:
            return None

        last = lines[-1].strip()

        # 发出消息
        m = re.match(r"Sent at (.+?)(?:,\s*(.+))?$", last)
        if m:
            text = "\n".join(lines[:-1]).strip()
            if not text:
                return None
            return {
                "text": text,
                "direction": "sent",
                "time": m.group(1).strip(),
                "status": (m.group(2) or "").strip(),
                "raw": raw,
            }

        # 收到消息
        m = re.match(r"Received at (.+)$", last)
        if m:
            text = "\n".join(lines[:-1]).strip()
            if not text:
                return None
            return {
                "text": text,
                "direction": "received",
                "time": m.group(1).strip(),
                "status": "",
                "raw": raw,
            }

        # 日期分隔符（不返回）
        if re.match(r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d+", raw.strip()):
            return None

        # 其他不可识别的 ViewGroup text（跳过）
        return None

    def read_chat_messages(self, username: str, device_id: Optional[str] = None,
                           count: int = 20) -> Optional[List[dict]]:
        """
        打开指定用户的聊天并读取消息。完整流程。
        """
        did = self._did(device_id)
        if not self.search_and_open_user(username, did):
            return None
        return self.read_messages(did, count=count)

    # =========================================================================
    # 文件 / 图片发送
    # =========================================================================

    def send_file(self, local_path: str, device_id: Optional[str] = None,
                  caption: str = "") -> bool:
        """
        在当前聊天中发送文件。调用前需已进入聊天界面。

        流程: ADB push → media scan → 点击附件 → 选最新文件 → 发送。
        支持图片和一般文件。
        """
        did = self._did(device_id)
        import os
        local = Path(local_path)
        if not local.exists():
            self.logger.error(f"本地文件不存在: {local_path}")
            return False

        ext = local.suffix.lower()
        is_image = ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
        filename = local.name

        # 1) Push 文件到设备
        remote_dir = "/sdcard/Download"
        remote_path = f"{remote_dir}/{filename}"
        self.logger.info(f"推送文件到设备: {local_path} → {remote_path}")
        ok, out = self.dm._run_adb(["push", str(local), remote_path], did)
        if not ok:
            self.logger.error(f"ADB push 失败: {out}")
            return False

        # 2) 触发媒体扫描
        self.dm._run_adb([
            "shell", "am", "broadcast",
            "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d", f"file://{remote_path}"
        ], did)
        time.sleep(1.5)

        d = self.dm.get_u2(did)
        if not d:
            self.logger.error("u2 不可用，文件发送需要 u2")
            return False

        # 3) 点击附件按钮
        self.logger.info("点击附件按钮")
        if not self._click_element(did, TG.ATTACH_BUTTON, [], "附件按钮"):
            return False
        time.sleep(1.5)

        if is_image:
            return self._send_from_gallery(d, did, caption)
        else:
            return self._send_from_file_picker(d, did, filename, caption)

    def _send_from_gallery(self, d, device_id: str, caption: str = "") -> bool:
        """从 Gallery 选择最新的图片并发送"""
        # 选第一个（最近的）图片
        xml = d.dump_hierarchy()
        from lxml import etree
        import re
        root = etree.fromstring(xml.encode("utf-8"))
        bounds_re = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')

        first_photo = None
        for node in root.iter("node"):
            if "telegram" not in node.get("package", ""):
                continue
            text = node.get("text", "")
            cls = node.get("class", "")
            if "FrameLayout" in cls and text.startswith("Photo."):
                bs = node.get("bounds", "")
                m = bounds_re.match(bs)
                if m:
                    cx = (int(m.group(1)) + int(m.group(3))) // 2
                    cy = (int(m.group(2)) + int(m.group(4))) // 2
                    first_photo = (cx, cy, text)
                    break

        if not first_photo:
            self.logger.error("Gallery 中未找到图片")
            d.press("back")
            return False

        cx, cy, label = first_photo
        self.logger.info(f"选择图片: '{label[:50]}' → ({cx},{cy})")
        d.click(cx, cy)
        time.sleep(1)

        # 添加 caption
        if caption:
            cap_input = d(className="android.widget.EditText", packageName="org.telegram.messenger")
            if cap_input.exists(timeout=3):
                cap_input.set_text(caption)
                time.sleep(0.5)

        # 点击发送
        send = d(description="Send")
        if not send.exists(timeout=5):
            send = d(description="发送")
        if send.exists(timeout=3):
            send.click()
            self.logger.info("图片发送成功")
            time.sleep(1)
            return True

        self.logger.error("发送按钮未找到")
        d.press("back")
        return False

    def _send_from_file_picker(self, d, device_id: str, filename: str,
                               caption: str = "") -> bool:
        """从 File tab 选择指定文件并发送"""
        # 点击 File tab
        file_tab = d(text="File")
        if not file_tab.exists(timeout=3):
            self.logger.error("File tab 未找到")
            d.press("back")
            return False
        file_tab.click()
        time.sleep(1.5)

        # 尝试在列表中找到文件
        target = d(textContains=filename)
        if not target.exists(timeout=5):
            # 尝试 Internal Storage → Download
            dl = d(text="Download")
            if dl.exists(timeout=3):
                dl.click()
                time.sleep(1)
                target = d(textContains=filename)

        if not target.exists(timeout=3):
            self.logger.error(f"文件 '{filename}' 未在文件列表中找到")
            d.press("back")
            d.press("back")
            return False

        target.click()
        time.sleep(1)

        if caption:
            cap_input = d(className="android.widget.EditText", packageName="org.telegram.messenger")
            if cap_input.exists(timeout=3):
                cap_input.set_text(caption)
                time.sleep(0.5)

        send = d(description="Send")
        if not send.exists(timeout=5):
            send = d(description="发送")
        if send.exists(timeout=3):
            send.click()
            self.logger.info(f"文件 '{filename}' 发送成功")
            time.sleep(1)
            return True

        self.logger.error("发送按钮未找到")
        d.press("back")
        return False

    def send_file_to_user(self, username: str, local_path: str,
                          device_id: Optional[str] = None, caption: str = "") -> bool:
        """完整流程: 搜索用户 → 进入聊天 → 发送文件"""
        did = self._did(device_id)
        if not self.search_and_open_user(username, did):
            return False
        return self.send_file(local_path, did, caption=caption)

    # =========================================================================
    # 多账号管理
    # =========================================================================

    def list_accounts(self, device_id: Optional[str] = None) -> List[Dict[str, str]]:
        """
        列出设备上所有已登录的 Telegram 账号。
        返回 [{"name": "...", "active": True/False}]
        """
        did = self._did(device_id)
        d = self.dm.get_u2(did)
        if not d:
            return []

        from lxml import etree

        if not self._is_telegram_running(did):
            self.start_telegram(did)
            time.sleep(3)

        accounts = self._try_list_accounts_sidebar(d)
        if not accounts:
            accounts = self._try_list_accounts_longpress(d)
        return accounts

    def _try_list_accounts_sidebar(self, d) -> List[Dict[str, str]]:
        """通过侧边栏列出账号（传统 Telegram UI）"""
        from lxml import etree

        # 打开侧边栏
        for desc in ["Open navigation menu", "打开导航菜单"]:
            menu = d(description=desc)
            if menu.exists(timeout=3):
                menu.click()
                time.sleep(2)
                break
        else:
            return []

        # 读取当前账号名
        xml = d.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))
        current_name = None
        current_phone = None
        for el in root.iter():
            text = el.get("text", "")
            rid = el.get("resource-id", "")
            if "systemui" in rid:
                continue
            if text.startswith("+") and any(c.isdigit() for c in text) and len(text) > 8:
                current_phone = text
            elif (text and len(text) > 1 and len(text) < 40
                  and not text.startswith("+")
                  and text not in ["My Profile", "New Group", "Contacts", "Calls",
                                   "Saved Messages", "Settings", "Invite Friends",
                                   "Telegram Features", "Telegram", "Add Account"]):
                desc = el.get("content-desc", "")
                if "night" not in text.lower() and "notification" not in desc.lower():
                    if current_name is None:
                        current_name = text

        # 展开账号列表
        arrow = d(description="Show accounts")
        if not arrow.exists(timeout=2):
            arrow = d(descriptionContains="account")
        if not arrow.exists(timeout=2):
            arrow = d(descriptionContains="帐号")
        if arrow.exists(timeout=2):
            arrow.click()
            time.sleep(2)
        else:
            d.press("back")
            return []

        xml = d.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))

        accounts = []
        skip_texts = {"My Profile", "New Group", "Contacts", "Calls",
                      "Saved Messages", "Settings", "Invite Friends",
                      "Telegram Features", "Telegram", "Add Account",
                      "添加帐号", "Switch to night theme"}

        # 账号区在 "Hide accounts" 和 "My Profile" 之间
        in_account_zone = False
        for el in root.iter():
            desc = el.get("content-desc", "")
            text = el.get("text", "")
            rid = el.get("resource-id", "")

            if desc == "Hide accounts":
                in_account_zone = True
                continue
            if text in ("My Profile", "New Group"):
                in_account_zone = False
                continue

            if in_account_zone and text and text not in skip_texts:
                if "systemui" not in rid and "notification" not in desc.lower():
                    if not text.startswith("+") and len(text) > 1 and len(text) < 40:
                        is_active = (text == current_name)
                        accounts.append({"name": text, "active": is_active})

        d.press("back")
        time.sleep(1)
        return accounts

    def _try_list_accounts_longpress(self, d) -> List[Dict[str, str]]:
        """通过长按 Profile tab 列出账号（新版 Telegram UI）"""
        profile_tab = d(text="Profile")
        if not profile_tab.exists(timeout=3):
            return []

        profile_tab.long_click(duration=1.5)
        time.sleep(2)

        from lxml import etree
        xml = d.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))

        accounts = []
        skip_texts = {"Add Account", "添加帐号"}
        for el in root.iter():
            text = el.get("text", "")
            rid = el.get("resource-id", "")
            if (text and text not in skip_texts
                    and len(text) > 1 and len(text) < 40
                    and "systemui" not in rid
                    and not text.startswith("+")
                    and ":" not in text  # skip timestamps
                    and not text.isdigit()):  # skip battery
                accounts.append({"name": text, "active": len(accounts) == 0})

        d.press("back")
        time.sleep(1)
        return accounts

    def switch_account(self, account_name: str,
                       device_id: Optional[str] = None) -> bool:
        """
        切换到指定名字的 Telegram 账号。
        支持侧边栏和底部 tab (长按 Profile) 两种 UI。
        返回 True=成功切换, False=失败。
        """
        did = self._did(device_id)
        d = self.dm.get_u2(did)
        if not d:
            return False

        if not self._is_telegram_running(did):
            self.start_telegram(did)
            time.sleep(3)

        # 尝试侧边栏方式
        if self._switch_via_sidebar(d, account_name):
            self.logger.info(f"账号切换成功(侧边栏): {account_name}")
            return True

        # 尝试长按 Profile 方式
        if self._switch_via_longpress(d, account_name):
            self.logger.info(f"账号切换成功(长按Profile): {account_name}")
            return True

        self.logger.error(f"账号切换失败: 未找到 '{account_name}'")
        return False

    def _switch_via_sidebar(self, d, target_name: str) -> bool:
        """通过侧边栏切换账号"""
        for desc in ["Open navigation menu", "打开导航菜单"]:
            menu = d(description=desc)
            if menu.exists(timeout=3):
                menu.click()
                time.sleep(2)
                break
        else:
            return False

        # 展开账号列表
        arrow = d(description="Show accounts")
        if not arrow.exists(timeout=2):
            arrow = d(descriptionContains="account")
        if arrow.exists(timeout=2):
            arrow.click()
            time.sleep(2)
        else:
            d.press("back")
            return False

        # 点击目标账号 — 需要跳过当前同名账号（当前在顶部显示）
        # 账号列表在 "Hide accounts" 下方
        from lxml import etree
        import re
        bounds_re = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')

        xml = d.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))

        in_account_zone = False
        for el in root.iter():
            desc_attr = el.get("content-desc", "")
            text = el.get("text", "")

            if desc_attr == "Hide accounts":
                in_account_zone = True
                continue
            if text in ("My Profile", "New Group"):
                break

            if in_account_zone and text == target_name:
                bs = el.get("bounds", "")
                m = bounds_re.match(bs)
                if m:
                    cx = (int(m.group(1)) + int(m.group(3))) // 2
                    cy = (int(m.group(2)) + int(m.group(4))) // 2
                    d.click(cx, cy)
                    time.sleep(5)
                    return True

        d.press("back")
        time.sleep(1)
        return False

    def _switch_via_longpress(self, d, target_name: str) -> bool:
        """通过长按 Profile tab 切换账号"""
        profile_tab = d(text="Profile")
        if not profile_tab.exists(timeout=3):
            return False

        profile_tab.long_click(duration=1.5)
        time.sleep(2)

        target = d(text=target_name)
        if target.exists(timeout=3):
            target.click()
            time.sleep(5)
            return True

        d.press("back")
        time.sleep(1)
        return False

    def get_current_account(self, device_id: Optional[str] = None) -> Optional[Dict[str, str]]:
        """获取当前活跃账号的名字、电话、用户名"""
        did = self._did(device_id)
        d = self.dm.get_u2(did)
        if not d:
            return None

        from lxml import etree

        if not self._is_telegram_running(did):
            self.start_telegram(did)
            time.sleep(3)

        # 尝试侧边栏
        for desc in ["Open navigation menu", "打开导航菜单"]:
            menu = d(description=desc)
            if menu.exists(timeout=3):
                menu.click()
                time.sleep(2)

                xml = d.dump_hierarchy()
                root = etree.fromstring(xml.encode("utf-8"))
                name, phone = None, None
                skip = {"My Profile", "New Group", "Contacts", "Calls",
                        "Saved Messages", "Settings", "Invite Friends",
                        "Telegram Features", "Telegram", "Add Account"}
                for el in root.iter():
                    text = el.get("text", "")
                    rid = el.get("resource-id", "")
                    if "systemui" in rid:
                        continue
                    if text.startswith("+") and any(c.isdigit() for c in text):
                        phone = text
                    elif (text and text not in skip and len(text) > 1
                          and len(text) < 40 and not text.startswith("+")):
                        edesc = el.get("content-desc", "")
                        if "night" not in text.lower() and "notification" not in edesc.lower():
                            if name is None:
                                name = text

                # 进 Settings 获取 username
                username = None
                settings_btn = d(text="Settings")
                if settings_btn.exists(timeout=2):
                    settings_btn.click()
                    time.sleep(3)
                    xml2 = d.dump_hierarchy()
                    root2 = etree.fromstring(xml2.encode("utf-8"))
                    for el in root2.iter():
                        t = el.get("text", "")
                        if t.startswith("@"):
                            username = t
                            break
                    d.press("back")
                    time.sleep(1)

                d.press("back")
                time.sleep(1)
                return {"name": name, "phone": phone, "username": username}

        # 底部 tab UI — 从 Settings tab 获取
        settings_tab = d(text="Settings")
        if settings_tab.exists(timeout=2):
            settings_tab.click()
            time.sleep(3)
            xml = d.dump_hierarchy()
            root = etree.fromstring(xml.encode("utf-8"))
            name, phone, username = None, None, None
            for el in root.iter():
                text = el.get("text", "")
                if text.startswith("@"):
                    username = text
                elif text.startswith("+") and any(c.isdigit() for c in text):
                    if "•" in text:
                        parts = text.split("•")
                        phone = parts[0].strip()
                        username = parts[1].strip() if len(parts) > 1 else username
                    else:
                        phone = text
                elif (text and len(text) > 1 and len(text) < 40
                      and text not in ["Account", "Chat Settings", "Privacy & Security",
                                       "Sounds, Calls, Badges", "Data and Storage",
                                       "Chat Folders", "Devices", "Power Saving",
                                       "Language", "Chats", "Contacts", "Settings",
                                       "Profile"]):
                    if name is None and not text.startswith("Number"):
                        name = text
            d.press("back")
            time.sleep(1)
            return {"name": name, "phone": phone, "username": username}

        return None

    def go_back(self, device_id: Optional[str] = None) -> bool:
        return self.dm.input_keyevent(self._did(device_id), 4)

    def go_home(self, device_id: Optional[str] = None) -> bool:
        return self.dm.input_keyevent(self._did(device_id), 3)

    def debug_dump_ui(self, device_id: Optional[str] = None,
                      save_path: Optional[str] = None) -> Optional[str]:
        """调试: dump 全部 UI 元素"""
        did = self._did(device_id)

        if self._use_u2(did):
            d = self.dm.get_u2(did)
            if d:
                try:
                    hierarchy = d.dump_hierarchy()
                    if save_path:
                        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                        with open(save_path, 'w', encoding='utf-8') as f:
                            f.write(hierarchy)
                    return hierarchy
                except Exception as e:
                    self.logger.debug(f"u2 dump_hierarchy 失败: {e}")

        xml = self.dm.dump_ui_hierarchy(did)
        if xml and save_path:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(xml)
        return xml


    # =========================================================================
    # 消息监听（智能间隔轮询）
    # =========================================================================

    def monitor_chat(self, username: str, device_id: Optional[str] = None,
                     callback=None, duration_sec: int = 300,
                     base_interval: float = 30.0) -> List[dict]:
        """
        监听指定聊天的新消息。

        智能间隔策略:
        - 有新消息: 加速到 base_interval * 0.3 (约10s)
        - 连续无新消息: 逐步退到 base_interval * 2 (约60s)
        - 降低检测风险 + 省电

        callback(msg_list): 收到新消息时调用
        duration_sec: 监听总时长
        返回: 监听期间所有新消息
        """
        did = self._did(device_id)
        self.logger.info("开始监听 %s (持续 %ds)", username, duration_sec)

        if not self.search_and_open_user(username, did):
            self.logger.error("无法打开 %s 的聊天", username)
            return []

        all_new = []
        known_texts = set()

        initial = self.read_messages(did, count=50)
        for m in initial:
            known_texts.add(m.get("raw", m.get("text", "")))

        start = time.time()
        interval = base_interval
        consecutive_empty = 0

        while time.time() - start < duration_sec:
            time.sleep(interval)

            current = self.read_messages(did, count=30)
            new_msgs = []
            for m in current:
                key = m.get("raw", m.get("text", ""))
                if key and key not in known_texts:
                    known_texts.add(key)
                    new_msgs.append(m)

            if new_msgs:
                all_new.extend(new_msgs)
                consecutive_empty = 0
                interval = max(base_interval * 0.3, 8.0)
                self.logger.info("监听到 %d 条新消息, 间隔缩短至 %.0fs", len(new_msgs), interval)
                if callback:
                    try:
                        callback(new_msgs)
                    except Exception as e:
                        self.logger.warning("Monitor callback error: %s", e)
            else:
                consecutive_empty += 1
                interval = min(base_interval * (1.2 ** consecutive_empty), base_interval * 2.5)
                interval = min(interval, 90.0)

        self.logger.info("监听结束, 共收到 %d 条新消息", len(all_new))
        return all_new

    # =========================================================================
    # 消息转发
    # =========================================================================

    def forward_last_message(self, from_user: str, to_user: str,
                             device_id: Optional[str] = None) -> bool:
        """
        转发最近一条收到的消息: from_user -> to_user.
        使用 Telegram 原生转发功能（长按 → Forward）。
        """
        did = self._did(device_id)
        d = self.dm.get_u2(did)
        if not d:
            return False

        # 打开源聊天
        if not self.search_and_open_user(from_user, did):
            return False
        time.sleep(2)

        # 长按最后一条消息触发选择
        xml = d.dump_hierarchy()
        last_msg_coords = self._find_last_received_message(xml)
        if not last_msg_coords:
            self.logger.error("未找到可转发的消息")
            return False

        cx, cy = last_msg_coords
        d.long_click(cx, cy, duration=0.8)
        time.sleep(1.5)

        # 点 Forward
        fwd_btn = d(description="Forward")
        if not fwd_btn.exists(timeout=3):
            fwd_btn = d(description="转发")
        if not fwd_btn.exists(timeout=3):
            self.logger.error("未找到转发按钮")
            d.press("back")
            return False

        fwd_btn.click()
        time.sleep(2)

        # 搜索目标用户
        search = d(className="android.widget.EditText")
        if search.exists(timeout=5):
            search.set_text(to_user)
            time.sleep(2)

            # 点击搜索结果
            result = d(textContains=to_user.lstrip("@"))
            if result.exists(timeout=5):
                result.click()
                time.sleep(1)

                # 确认发送
                send_btn = d(description="Send")
                if not send_btn.exists(timeout=3):
                    send_btn = d(description="发送")
                if send_btn.exists(timeout=3):
                    send_btn.click()
                    time.sleep(2)
                    self.logger.info("消息转发成功: %s → %s", from_user, to_user)
                    return True

        self.logger.error("转发流程失败")
        d.press("back")
        d.press("back")
        return False

    def _find_last_received_message(self, xml: str) -> Optional[Tuple[int, int]]:
        """在聊天 XML 中找到最后一条收到的消息的坐标"""
        from lxml import etree
        import re
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return None

        last_coords = None
        for node in root.iter():
            text = node.get("text", "")
            if "Received at" in text:
                bounds = node.get("bounds", "")
                m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                if m:
                    x1, y1, x2, y2 = int(m[1]), int(m[2]), int(m[3]), int(m[4])
                    last_coords = ((x1 + x2) // 2, (y1 + y2) // 2)

        return last_coords

    # =========================================================================
    # 群组操作
    # =========================================================================

    def join_group(self, invite_link: str, device_id: Optional[str] = None) -> bool:
        """通过邀请链接加入群组/频道"""
        did = self._did(device_id)
        d = self.dm.get_u2(did)
        if not d:
            return False

        self.dm.execute_adb_command(
            f'shell am start -a android.intent.action.VIEW -d "{invite_link}"', did)
        time.sleep(5)

        # 点 JOIN GROUP / 加入群组
        join_btn = d(textContains="JOIN")
        if not join_btn.exists(timeout=5):
            join_btn = d(textContains="加入")
        if join_btn.exists(timeout=3):
            join_btn.click()
            time.sleep(3)
            self.logger.info("已加入群组: %s", invite_link)
            return True

        self.logger.warning("未找到加入按钮 (可能已在群中或链接失效)")
        return False

    def send_group_message(self, group_name: str, message: str,
                           device_id: Optional[str] = None) -> bool:
        """给群组发送消息"""
        did = self._did(device_id)
        if not self.search_and_open_user(group_name, did):
            return False
        return self.send_text_message(message, did)

    def read_group_messages(self, group_name: str,
                            device_id: Optional[str] = None,
                            count: int = 20) -> List[dict]:
        """读取群组消息"""
        did = self._did(device_id)
        if not self.search_and_open_user(group_name, did):
            return []
        msgs = self.read_messages(did, count=count)
        self.go_back(did)
        return msgs

    # =========================================================================
    # AI 增强功能
    # =========================================================================

    def send_rewritten_message(self, message: str,
                               context: Optional[Dict[str, str]] = None,
                               device_id: Optional[str] = None) -> bool:
        """
        发送经 LLM 改写后的独特消息（防检测）。
        Falls back to original if rewriter unavailable.
        """
        try:
            from ..ai.message_rewriter import get_rewriter
            rw = get_rewriter()
            unique_msg = rw.rewrite(message, context, "telegram")
            self.logger.info("消息已改写: '%s' → '%s'", message[:30], unique_msg[:30])
        except Exception as e:
            self.logger.debug("MessageRewriter unavailable: %s", e)
            unique_msg = message
        return self.send_text_message(unique_msg, device_id)

    def auto_reply_monitor(self, username: str, device_id: Optional[str] = None,
                           persona: str = "casual", duration_sec: int = 300,
                           base_interval: float = 30.0) -> List[dict]:
        """
        监听聊天 + 自动回复。

        收到新消息 → 意图分类 → 生成回复 → 模拟延迟 → 发送。
        结合 monitor_chat 的智能间隔 + AutoReply 的意图过滤。
        """
        did = self._did(device_id)

        try:
            from ..ai.auto_reply import AutoReply
            ar = AutoReply()
        except Exception as e:
            self.logger.error("AutoReply unavailable: %s", e)
            return []

        replies_sent = []

        def on_new_messages(msgs):
            for msg in msgs:
                if msg.get("direction") != "received":
                    continue
                text = msg.get("text", "")
                if not text:
                    continue

                result = ar.generate_reply(
                    message=text,
                    sender=username,
                    platform="telegram",
                    persona=persona,
                    conversation_id=f"telegram:{username}",
                )
                if result:
                    self.logger.info(
                        "AutoReply [%s] delay=%.1fs: %s",
                        result.intent, result.delay_sec, result.text[:50],
                    )
                    time.sleep(result.delay_sec)
                    if self.send_text_message(result.text, did):
                        replies_sent.append({
                            "incoming": text,
                            "reply": result.text,
                            "intent": result.intent,
                            "delay": result.delay_sec,
                        })

        self.monitor_chat(
            username, did,
            callback=on_new_messages,
            duration_sec=duration_sec,
            base_interval=base_interval,
        )

        self.logger.info("AutoReply monitor 结束, 发送 %d 条回复", len(replies_sent))
        return replies_sent

    def smart_send(self, username: str, template: str,
                   context: Optional[Dict[str, str]] = None,
                   device_id: Optional[str] = None) -> bool:
        """
        智能发送: 搜索用户 → 改写消息 → 发送。
        一站式 API，适合批量外发。
        """
        did = self._did(device_id)
        if not self.search_and_open_user(username, did):
            return False
        return self.send_rewritten_message(template, context, did)


# =============================================================================
# 工厂函数
# =============================================================================

def create_telegram_automation(config_path: Optional[str] = None) -> TelegramAutomation:
    return TelegramAutomation(get_device_manager(config_path))


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    tg = create_telegram_automation("config/devices.yaml")
    mgr = get_device_manager("config/devices.yaml")
    devices = mgr.get_all_devices()

    if not devices:
        print("无设备")
        sys.exit(1)

    dev = devices[0]
    print(f"设备: {dev.display_name} ({dev.device_id})")
    tg.set_current_device(dev.device_id)

    print(f"u2 可用: {tg._use_u2(dev.device_id)}")
    print("\nDump UI...")
    tg.debug_dump_ui(save_path="logs/debug_ui_dump.txt")
    print("Done. 检查 logs/debug_ui_dump.txt")
