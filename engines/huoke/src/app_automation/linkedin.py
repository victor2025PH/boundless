# -*- coding: utf-8 -*-
"""
LinkedIn 自动化模块。

基于 Redmi 13C 真机 UI dump 构建的选择器。
所有操作通过 uiautomator2 在设备端原子执行。
集成 HumanBehavior (Phase 1) 实现自然操作节奏。
"""

import logging
import random
import time
from typing import Optional, List, Dict, Any

from lxml import etree

from .base_automation import BaseAutomation

logger = logging.getLogger(__name__)

PACKAGE = "com.linkedin.android"


class LI:
    """LinkedIn u2 选择器 — 基于实测 resource-id / content-desc"""

    # ── 底部导航栏 ──
    TAB_HOME = {"resourceId": "com.linkedin.android:id/tab_feed"}
    TAB_NETWORK = {"resourceId": "com.linkedin.android:id/tab_relationships"}
    TAB_POST = {"resourceId": "com.linkedin.android:id/tab_post"}
    TAB_NOTIFICATIONS = {"resourceId": "com.linkedin.android:id/tab_notifications"}
    TAB_JOBS = {"resourceId": "com.linkedin.android:id/tab_jobs"}

    # ── 顶部栏 ──
    SEARCH_BAR = {"resourceId": "com.linkedin.android:id/search_open_bar_box"}
    SEARCH_INPUT = {"resourceId": "com.linkedin.android:id/search_bar_text"}
    MESSAGING_ICON = [
        {"description": "messaging"},
        {"resourceId": "com.linkedin.android:id/home_messaging"},
    ]

    # ── 消息页 ──
    MSG_SEARCH_BOX = {"resourceId": "com.linkedin.android:id/pill_inbox_search_box_container"}
    MSG_COMPOSE_FAB = [
        {"description": "写新消息"},
        {"resourceId": "com.linkedin.android:id/focused_Inbox_compose_fab_view"},
    ]
    MSG_CONVERSATION_LIST = {"resourceId": "com.linkedin.android:id/conversation_list"}

    # ── 消息撰写 ──
    COMPOSE_RECIPIENT_INPUT = {"resourceId": "com.linkedin.android:id/msglib_recipient_input"}
    COMPOSE_SEARCH_RESULTS = {"resourceId": "com.linkedin.android:id/msglib_compose_search_results"}
    COMPOSE_RESULT_CONTAINER = {"resourceId": "com.linkedin.android:id/people_result_container"}
    COMPOSE_RESULT_NAME = {"resourceId": "com.linkedin.android:id/people_result_name"}
    COMPOSE_CLOSE = {"description": "关闭"}

    # ── 聊天对话 ──
    CHAT_INPUT = [
        {"resourceId": "com.linkedin.android:id/msg_edit_text"},
        {"resourceId": "com.linkedin.android:id/msglib_compose_message_input"},
        {"className": "android.widget.EditText", "description": "写消息…"},
        {"className": "android.widget.EditText", "textContains": "消息"},
    ]
    CHAT_SEND = [
        {"resourceId": "com.linkedin.android:id/msg_send_button"},
        {"description": "发送"},
        {"description": "Send"},
    ]

    # ── 发布动态 ──
    POST_TEXT_INPUT = [
        {"resourceId": "com.linkedin.android:id/share_compose_text_input_entities"},
        {"description": "分享您的看法…"},
    ]
    POST_BUTTON = [
        {"resourceId": "com.linkedin.android:id/share_compose_post_button"},
        {"text": "发布"},
    ]
    POST_CLOSE = {"resourceId": "com.linkedin.android:id/share_compose_close_button"}
    POST_VISIBILITY = {"resourceId": "com.linkedin.android:id/share_compose_visibility_toggle"}
    POST_PHOTO = {"description": "照片"}

    # ── Premium 弹窗 ──
    PREMIUM_DISMISS = {"resourceId": "com.linkedin.android:id/touch_outside"}
    PREMIUM_INDICATOR = {"textContains": "试用高级帐号"}

    # ── 通用 ──
    BACK_BUTTON = {"description": "返回"}


class LinkedInAutomation(BaseAutomation):
    """LinkedIn 自动化操作 — 继承 BaseAutomation 获得 HumanBehavior + ComplianceGuard"""

    PLATFORM = "linkedin"
    PACKAGE = PACKAGE
    MAIN_ACTIVITY = ""

    def __init__(self, device_manager, **kwargs):
        super().__init__(device_manager, **kwargs)
        self.hb.session_start()

    def _d(self, device_id: Optional[str] = None):
        """获取 u2 设备连接"""
        return self._u2(device_id)

    def _find_and_click(self, d, selectors, timeout=5) -> bool:
        """尝试多个选择器，找到第一个存在的并点击（使用 HumanBehavior tap）"""
        if isinstance(selectors, dict):
            selectors = [selectors]
        for sel in selectors:
            el = d(**sel)
            if el.exists(timeout=timeout):
                info = el.info
                cx = (info["bounds"]["left"] + info["bounds"]["right"]) // 2
                cy = (info["bounds"]["top"] + info["bounds"]["bottom"]) // 2
                self.hb.tap(d, cx, cy)
                return True
        return False

    def _find_element(self, d, selectors, timeout=5):
        """尝试多个选择器，返回第一个存在的元素"""
        if isinstance(selectors, dict):
            selectors = [selectors]
        for sel in selectors:
            el = d(**sel)
            if el.exists(timeout=timeout):
                return el
        return None

    def _dismiss_premium_popup(self, d):
        """检测并关闭 Premium 付费弹窗"""
        premium = d(**LI.PREMIUM_INDICATOR)
        if premium.exists(timeout=1):
            logger.info("检测到 Premium 弹窗，关闭中...")
            d(**LI.PREMIUM_DISMISS).click()
            time.sleep(1)
            return True
        return False

    # ── 导航 ──

    def start_app(self, device_id: Optional[str] = None):
        """启动 LinkedIn（冷启动）"""
        d = self._d(device_id)
        d.app_stop(PACKAGE)
        time.sleep(1)
        d.app_start(PACKAGE)
        time.sleep(4)
        logger.info("LinkedIn 已启动")

    def go_home(self, device_id: Optional[str] = None) -> bool:
        d = self._d(device_id)
        return self._find_and_click(d, LI.TAB_HOME, timeout=3)

    def go_messaging(self, device_id: Optional[str] = None) -> bool:
        d = self._d(device_id)
        return self._find_and_click(d, LI.MESSAGING_ICON, timeout=3)

    def go_network(self, device_id: Optional[str] = None) -> bool:
        d = self._d(device_id)
        return self._find_and_click(d, LI.TAB_NETWORK, timeout=3)

    # ── 发布动态 ──

    def post_update(self, content: str, device_id: Optional[str] = None) -> bool:
        """
        发布文字动态。
        返回 True=成功, False=失败。
        """
        d = self._d(device_id)
        did = device_id or self._current_device

        # 点发布 tab
        if not self._find_and_click(d, LI.TAB_POST, timeout=5):
            logger.error("未找到发布 tab")
            return False
        time.sleep(3)

        # 输入文本
        text_input = self._find_element(d, LI.POST_TEXT_INPUT, timeout=5)
        if not text_input:
            logger.error("未找到发布文本输入框")
            return False

        text_input.click()
        self.hb.wait_think(0.5)
        self.hb.type_text(d, content)
        self.hb.wait_between_actions()

        # 点发布按钮
        if not self._find_and_click(d, LI.POST_BUTTON, timeout=5):
            logger.error("未找到发布按钮")
            return False

        time.sleep(3)
        logger.info("动态发布成功: %s...", content[:30])
        return True

    # ── 搜索用户 ──

    def _open_search_and_type(self, d, query: str) -> bool:
        """打开搜索栏并输入关键词，回车搜索"""
        self._find_and_click(d, LI.TAB_HOME, timeout=3)
        time.sleep(2)

        if not self._find_and_click(d, LI.SEARCH_BAR, timeout=5):
            logger.error("未找到搜索栏")
            return False
        time.sleep(2)

        # 搜索页面的 EditText 无 resource-id，用 className 定位
        edit = d(className="android.widget.EditText")
        if not edit.exists(timeout=5):
            logger.error("未找到搜索输入框")
            return False

        self.hb.type_text(d, query)
        self.hb.wait_think()
        d.press("enter")
        time.sleep(4)
        return True

    def _switch_to_people_tab(self, d) -> bool:
        """搜索结果默认显示职位，切换到"会员"(People) tab"""
        for label in ["会员", "People"]:
            btn = d(text=label)
            if btn.exists(timeout=3):
                btn.click()
                time.sleep(3)
                return True
        logger.warning("未找到'会员'筛选 tab")
        return False

    def search_profiles(self, query: str, device_id: Optional[str] = None,
                        max_results: int = 10) -> List[Dict[str, str]]:
        """
        搜索用户并返回资料列表。
        返回: [{"name": "...", "title": "...", "location": "...", "degree": "..."}]
        """
        d = self._d(device_id)

        if not self._open_search_and_type(d, query):
            return []

        self._switch_to_people_tab(d)

        xml = d.dump_hierarchy()
        profiles = self._parse_search_results(xml, max_results)
        logger.info("搜索 '%s' 找到 %d 个结果", query, len(profiles))

        d.press("back")
        d.press("back")
        return profiles

    def _parse_search_results(self, xml: str, max_results: int) -> List[Dict[str, str]]:
        """
        从会员搜索结果 XML 中解析用户信息。
        LinkedIn 搜索结果结构:
          - content-desc="姓名 • N 度+" (TextView) — 姓名+度数
          - 下方 text="职位 at 公司"
          - 再下方 text="地点"
          - 再下方 text="目前就职: ..." 或 "曾经就职: ..."
          - 加好友: content-desc="邀请{name}加为好友"
        """
        results = []
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return results

        elements = list(root.iter())
        i = 0
        while i < len(elements) and len(results) < max_results:
            el = elements[i]
            text = el.get("text", "")
            # 匹配 "姓名 • N 度+" 模式
            if "•" in text and ("度" in text or "1st" in text or "2nd" in text or "3rd" in text):
                parts = text.split("•")
                name = parts[0].strip().rstrip(" \u00a0\u200b")
                degree = parts[1].strip() if len(parts) > 1 else ""

                profile: Dict[str, str] = {"name": name, "degree": degree}

                # 往下找同一组的职位和地点（紧跟着的 TextView）
                for j in range(i + 1, min(i + 8, len(elements))):
                    next_text = elements[j].get("text", "")
                    if not next_text or len(next_text) < 3:
                        continue
                    if "•" in next_text and "度" in next_text:
                        break  # 下一个人了
                    if "title" not in profile:
                        profile["title"] = next_text
                    elif "location" not in profile and any(
                        c in next_text for c in ["菲律宾", "Philippines", "市", "City", "Area"]
                    ):
                        profile["location"] = next_text
                    elif next_text.startswith("目前就职") or next_text.startswith("曾经就职"):
                        profile["current_role"] = next_text

                results.append(profile)
            i += 1

        return results

    # ── 发送连接请求 ──

    def send_connection_request(self, name: str, device_id: Optional[str] = None,
                                note: str = "") -> bool:
        """
        搜索用户并发送连接请求。
        name: 要搜索的用户名
        note: 可选的连接备注
        返回 True=成功发送, False=失败
        """
        d = self._d(device_id)

        if not self._open_search_and_type(d, name):
            return False
        self._switch_to_people_tab(d)

        # 找 "邀请...加为好友" 按钮（content-desc 格式）
        connect_btn = d(descriptionContains="加为好友")
        if not connect_btn.exists(timeout=3):
            connect_btn = d(textContains="加为好友")
        if not connect_btn.exists(timeout=3):
            connect_btn = d(descriptionContains="Connect")
        if not connect_btn.exists(timeout=3):
            connect_btn = d(textContains="Connect")

        if not connect_btn.exists(timeout=3):
            logger.warning("未找到加好友按钮（可能已经是好友）")
            d.press("back")
            d.press("back")
            return False

        connect_btn.click()
        self.hb.wait_between_actions(context_weight=1.5)

        # 检查是否弹出"添加备注"对话框
        if note:
            add_note_btn = d(textContains="添加备注")
            if not add_note_btn.exists(timeout=2):
                add_note_btn = d(textContains="Add a note")
            if add_note_btn.exists(timeout=2):
                add_note_btn.click()
                time.sleep(2)
                # 找备注输入框
                note_input = d(className="android.widget.EditText")
                if note_input.exists(timeout=3):
                    self.hb.type_text(d, note)
                    self.hb.wait_between_actions()
                    # 发送
                    send_btn = d(textContains="发送")
                    if not send_btn.exists(timeout=2):
                        send_btn = d(textContains="Send")
                    if send_btn.exists(timeout=2):
                        send_btn.click()
                        time.sleep(2)
                        logger.info("连接请求已发送（带备注）: %s", name)
                        d.press("back")
                        d.press("back")
                        return True

        # 没有备注或直接发送
        time.sleep(2)

        # 检查是否发送成功（按钮变为 "Pending" / "已发送"）
        pending = d(textContains="待处理")
        if not pending.exists(timeout=2):
            pending = d(textContains="Pending")
        if not pending.exists(timeout=2):
            pending = d(textContains="已发送")

        sent = pending.exists(timeout=2)
        if sent:
            logger.info("连接请求已发送: %s", name)
        else:
            logger.info("连接请求可能已发送: %s（无法确认状态）", name)

        d.press("back")
        d.press("back")
        return True

    # ── 发送消息（仅限已连接联系人）──

    def send_message(self, recipient: str, message: str,
                     device_id: Optional[str] = None) -> bool:
        """
        给已连接的联系人发消息。
        recipient: 联系人名字
        message: 消息内容
        返回 True=成功, False=失败
        """
        d = self._d(device_id)

        # 进入 Messaging
        if not self.go_messaging(device_id):
            logger.error("未找到消息入口")
            return False
        time.sleep(3)

        # 点写新消息
        if not self._find_and_click(d, LI.MSG_COMPOSE_FAB, timeout=5):
            logger.error("未找到写新消息按钮")
            return False
        time.sleep(3)

        # 输入收件人
        recipient_input = d(**LI.COMPOSE_RECIPIENT_INPUT)
        if not recipient_input.exists(timeout=5):
            logger.error("未找到收件人输入框")
            return False

        recipient_input.set_text(recipient)
        time.sleep(3)

        # 等搜索结果
        result_name = d(**LI.COMPOSE_RESULT_NAME)
        if not result_name.exists(timeout=5):
            logger.warning("未找到匹配的联系人: %s", recipient)
            d.press("back")
            return False

        # 检查结果中是否有 1 度连接
        xml = d.dump_hierarchy()
        first_degree = self._find_first_degree_result(xml, recipient)

        if first_degree:
            # 点击该结果
            target = d(textContains=first_degree["name"])
            if target.exists(timeout=3):
                target.click()
            else:
                d(**LI.COMPOSE_RESULT_CONTAINER).click()
        else:
            # 点第一个结果（可能触发 Premium 弹窗）
            d(**LI.COMPOSE_RESULT_CONTAINER).click()

        time.sleep(3)

        # 检查 Premium 弹窗
        if self._dismiss_premium_popup(d):
            logger.warning("对方不是 1 度连接，需要 Premium 才能发消息")
            d.press("back")
            return False

        # 输入消息
        msg_input = self._find_element(d, LI.CHAT_INPUT, timeout=5)
        if not msg_input:
            logger.error("未检测到消息输入框")
            d.press("back")
            return False

        msg_input.click()
        self.hb.wait_think(0.5)
        self.hb.type_text(d, message)
        self.hb.wait_between_actions()

        # 发送
        if not self._find_and_click(d, LI.CHAT_SEND, timeout=5):
            logger.error("未找到发送按钮")
            d.press("back")
            return False

        time.sleep(2)
        logger.info("消息已发送给 %s: %s...", recipient, message[:30])
        return True

    def _find_first_degree_result(self, xml: str, target_name: str) -> Optional[Dict]:
        """在消息搜索结果中找 1 度连接"""
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return None

        for el in root.iter():
            rid = el.get("resource-id", "")
            if rid == "com.linkedin.android:id/people_result_name":
                name_text = el.get("text", "")
                # 1 度连接通常显示 "姓名 • 1 度" / "1st"
                if "1" in name_text and target_name.lower() in name_text.lower():
                    return {"name": name_text}

        return None

    # ── 读取消息 ──

    def read_messages(self, device_id: Optional[str] = None,
                      count: int = 20) -> List[Dict[str, str]]:
        """
        读取消息列表（收件箱概览）。
        返回: [{"sender": "...", "preview": "...", "time": "..."}]
        """
        d = self._d(device_id)

        if not self.go_messaging(device_id):
            return []
        time.sleep(3)

        xml = d.dump_hierarchy()
        messages = self._parse_message_list(xml, count)
        logger.info("读取到 %d 条消息", len(messages))

        d.press("back")
        return messages

    def _parse_message_list(self, xml: str, max_count: int) -> List[Dict[str, str]]:
        """解析消息列表"""
        results = []
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return results

        for el in root.iter():
            desc = el.get("content-desc", "")
            if not desc:
                continue
            # 消息列表项通常有 content-desc 包含发件人和预览
            if len(desc) > 20 and ("消息" in desc or "message" in desc.lower()):
                results.append({
                    "raw": desc[:200],
                    "sender": "",
                    "preview": desc[:100],
                })
                if len(results) >= max_count:
                    break

        return results

    # ── 接受连接请求 ──

    def accept_connections(self, device_id: Optional[str] = None,
                           max_accept: int = 10) -> int:
        """
        接受待处理的连接请求。
        返回: 接受的数量。
        """
        d = self._d(device_id)

        # 去人脉 tab
        if not self.go_network(device_id):
            logger.error("未找到人脉 tab")
            return 0
        time.sleep(3)

        # 点"已收到邀请"
        invitations = d(textContains="已收到邀请")
        if not invitations.exists(timeout=3):
            invitations = d(textContains="Invitations")
        if not invitations.exists(timeout=3):
            logger.info("未找到邀请入口（可能没有待处理邀请）")
            return 0

        invitations.click()
        time.sleep(3)

        accepted = 0
        for i in range(max_accept):
            # 找接受按钮
            accept_btn = d(textContains="接受")
            if not accept_btn.exists(timeout=2):
                accept_btn = d(textContains="Accept")
            if not accept_btn.exists(timeout=2):
                break

            accept_btn.click()
            accepted += 1
            self.hb.wait_between_actions(context_weight=1.2)
            logger.info("已接受第 %d 个连接请求", accepted)

        logger.info("共接受 %d 个连接请求", accepted)
        d.press("back")
        return accepted

    # ── 查看个人资料 ──

    def view_profile(self, name: str, device_id: Optional[str] = None) -> Optional[Dict]:
        """
        搜索并查看用户资料，提取关键信息。
        返回: {"name": "...", "title": "...", "company": "...", "location": "..."}
        """
        d = self._d(device_id)

        if not self._open_search_and_type(d, name):
            return None
        self._switch_to_people_tab(d)

        # 点第一个结果进入资料页
        first_result = d(textContains=name)
        if not first_result.exists(timeout=5):
            logger.warning("搜索结果中未找到 %s", name)
            d.press("back")
            d.press("back")
            return None

        first_result.click()
        time.sleep(5)

        # dump 资料页
        xml = d.dump_hierarchy()
        profile = self._parse_profile_page(xml)

        d.press("back")
        d.press("back")
        d.press("back")

        return profile

    def _parse_profile_page(self, xml: str) -> Dict[str, str]:
        """从资料页 XML 提取信息"""
        profile: Dict[str, str] = {}
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return profile

        texts = []
        for el in root.iter():
            text = el.get("text", "")
            if text and len(text) < 200:
                texts.append(text)

        # 资料页通常: 第一个大文本是名字，第二个是标题/职位
        if len(texts) >= 2:
            profile["name"] = texts[0] if len(texts[0]) < 50 else ""
            profile["title"] = texts[1] if len(texts) > 1 and len(texts[1]) < 100 else ""

        # 找包含公司、地点等关键信息的文本
        for t in texts:
            if "at " in t.lower() or "@ " in t:
                profile.setdefault("company", t)
            if any(loc in t.lower() for loc in ["city", "area", "region", "manila", "philippines"]):
                profile.setdefault("location", t)

        profile.setdefault("raw_texts", "|".join(texts[:10]))
        return profile

    # ── 资料页增强: 带阅读模拟 ──

    def view_profile_natural(self, name: str, device_id: Optional[str] = None) -> Optional[Dict]:
        """
        搜索并查看用户资料（模拟自然浏览路径）。
        与 view_profile 的区别: 模拟阅读时间 + 滚动 + ComplianceGuard。
        """
        d = self._d(device_id)
        did = device_id or self._current_device

        with self.guarded("view_profile", device_id=did):
            if not self._open_search_and_type(d, name):
                return None
            self._switch_to_people_tab(d)

            # 自然浏览: 先看几个搜索结果再点目标
            self.hb.wait_read(200)

            first_result = d(textContains=name)
            if not first_result.exists(timeout=5):
                logger.warning("搜索结果中未找到 %s", name)
                d.press("back")
                d.press("back")
                return None

            first_result.click()
            time.sleep(3)

            # 模拟阅读资料页: 滚动 + 等待
            xml = d.dump_hierarchy()
            self.hb.wait_read(len(xml) // 10)
            self.hb.scroll_down(d, screen_height=1600, fraction=0.3)
            self.hb.wait_read(150)

            xml = d.dump_hierarchy()
            profile = self._parse_profile_page(xml)

            d.press("back")
            d.press("back")
            d.press("back")

            return profile

    # ── Feed 互动: 点赞 ──

    def like_feed_post(self, scroll_to_find: int = 3,
                       device_id: Optional[str] = None) -> bool:
        """
        在 Feed 中找到一条帖子并点赞。
        scroll_to_find: 最多滚动几次寻找可点赞的帖子。
        返回 True=成功点赞, False=未找到或失败。
        """
        d = self._d(device_id)
        did = device_id or self._current_device

        with self.guarded("like_post", device_id=did):
            self._find_and_click(d, LI.TAB_HOME, timeout=3)
            time.sleep(3)

            for attempt in range(scroll_to_find):
                xml = d.dump_hierarchy()
                like_btn = self._find_like_button(xml, d)
                if like_btn:
                    self.hb.wait_read(300)
                    cx, cy = like_btn
                    self.hb.tap(d, cx, cy)
                    time.sleep(1)
                    logger.info("帖子点赞成功 (scroll attempt %d)", attempt)
                    return True

                self.hb.scroll_down(d, screen_height=1600, fraction=0.5)
                self.hb.wait_read(200)

            logger.warning("未找到可点赞的帖子")
            return False

    def _find_like_button(self, xml: str, d) -> Optional[tuple]:
        """从 XML 中找到未点赞的 Like 按钮坐标。"""
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return None

        for node in root.iter():
            desc = node.get("content-desc", "")
            if not desc:
                continue
            desc_lower = desc.lower()
            if ("like" in desc_lower or "赞" in desc_lower) and \
               "unlike" not in desc_lower and "已赞" not in desc_lower:
                bounds = node.get("bounds", "")
                if bounds:
                    import re
                    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                    if m:
                        x1, y1, x2, y2 = int(m[1]), int(m[2]), int(m[3]), int(m[4])
                        return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    # ── Feed 互动: 评论 ──

    def comment_feed_post(self, comment_text: str,
                          scroll_to_find: int = 3,
                          device_id: Optional[str] = None) -> bool:
        """
        在 Feed 中找到一条帖子并评论。
        """
        d = self._d(device_id)
        did = device_id or self._current_device

        with self.guarded("comment_post", device_id=did, weight=2.0):
            self._find_and_click(d, LI.TAB_HOME, timeout=3)
            time.sleep(3)

            for attempt in range(scroll_to_find):
                xml = d.dump_hierarchy()
                comment_btn = self._find_comment_button(xml)
                if comment_btn:
                    self.hb.wait_read(400)
                    cx, cy = comment_btn
                    self.hb.tap(d, cx, cy)
                    time.sleep(3)

                    # 找评论输入框
                    comment_input = d(className="android.widget.EditText")
                    if comment_input.exists(timeout=5):
                        comment_input.click()
                        self.hb.wait_think(1.5)
                        self.hb.type_text(d, comment_text)
                        self.hb.wait_between_actions()

                        # 发送评论
                        post_btn = d(text="发布")
                        if not post_btn.exists(timeout=2):
                            post_btn = d(text="Post")
                        if post_btn.exists(timeout=3):
                            post_btn.click()
                            time.sleep(2)
                            logger.info("评论发布成功")
                            d.press("back")
                            return True

                    d.press("back")
                    return False

                self.hb.scroll_down(d, screen_height=1600, fraction=0.5)
                self.hb.wait_read(200)

            logger.warning("未找到可评论的帖子")
            return False

    def _find_comment_button(self, xml: str) -> Optional[tuple]:
        """从 XML 中找到评论按钮坐标。"""
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return None

        for node in root.iter():
            desc = node.get("content-desc", "")
            if not desc:
                continue
            desc_lower = desc.lower()
            if "comment" in desc_lower or "评论" in desc_lower:
                bounds = node.get("bounds", "")
                if bounds:
                    import re
                    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                    if m:
                        x1, y1, x2, y2 = int(m[1]), int(m[2]), int(m[3]), int(m[4])
                        return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    # ── 技能认可 ──

    def endorse_skill(self, name: str, skill_keyword: str = "",
                      device_id: Optional[str] = None) -> bool:
        """
        搜索用户并认可其技能。
        skill_keyword 可选 — 空则认可第一个可用技能。
        """
        d = self._d(device_id)
        did = device_id or self._current_device

        with self.guarded("endorse_skill", device_id=did, weight=1.5):
            if not self._open_search_and_type(d, name):
                return False
            self._switch_to_people_tab(d)

            first_result = d(textContains=name)
            if not first_result.exists(timeout=5):
                d.press("back")
                d.press("back")
                return False

            first_result.click()
            time.sleep(4)

            # 滚动到技能区域
            self.hb.wait_read(300)
            for _ in range(5):
                self.hb.scroll_down(d, screen_height=1600, fraction=0.4)
                time.sleep(1)

                xml = d.dump_hierarchy()
                endorse_btn = self._find_endorse_button(xml, skill_keyword)
                if endorse_btn:
                    cx, cy = endorse_btn
                    self.hb.tap(d, cx, cy)
                    time.sleep(2)
                    logger.info("技能认可成功: %s -> %s", name, skill_keyword or "first skill")
                    d.press("back")
                    d.press("back")
                    d.press("back")
                    return True

            logger.warning("未找到可认可的技能按钮")
            d.press("back")
            d.press("back")
            d.press("back")
            return False

    def _find_endorse_button(self, xml: str, skill_keyword: str) -> Optional[tuple]:
        """找到 + 认可按钮"""
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return None

        for node in root.iter():
            desc = (node.get("content-desc", "") or "").lower()
            text = (node.get("text", "") or "").lower()
            combined = desc + " " + text

            if ("endorse" in combined or "认可" in combined or "+1" in combined):
                if skill_keyword and skill_keyword.lower() not in combined:
                    continue
                bounds = node.get("bounds", "")
                if bounds:
                    import re
                    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                    if m:
                        x1, y1, x2, y2 = int(m[1]), int(m[2]), int(m[3]), int(m[4])
                        return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    # ── AI 增强功能 ──

    def send_rewritten_connection_note(self, name: str,
                                       note_template: str,
                                       context: Optional[Dict[str, str]] = None,
                                       device_id: Optional[str] = None) -> bool:
        """
        发送经 LLM 改写的连接请求备注。
        LinkedIn 对重复备注文本检测严格 — 每条必须独特。
        """
        unique_note = self.rewrite_message(note_template, context)
        logger.info("连接备注已改写: '%s' → '%s'", note_template[:30], unique_note[:30])
        return self.send_connection_request(name, device_id, note=unique_note)

    def send_rewritten_message(self, recipient: str, template: str,
                               context: Optional[Dict[str, str]] = None,
                               device_id: Optional[str] = None) -> bool:
        """发送经 LLM 改写的消息"""
        unique_msg = self.rewrite_message(template, context)
        logger.info("消息已改写: '%s' → '%s'", template[:30], unique_msg[:30])
        return self.send_message(recipient, unique_msg, device_id)

    def smart_outreach(self, name: str, message_template: str,
                       note_template: str = "",
                       context: Optional[Dict[str, str]] = None,
                       device_id: Optional[str] = None) -> Dict[str, Any]:
        """
        智能外发: 搜索 → 查看资料(自然) → 发连接请求(改写) → 发消息(改写)。
        模拟真人浏览路径 + 确保每条消息独特。

        返回操作结果 dict。
        """
        result = {"profile_viewed": False, "connection_sent": False, "message_sent": False}

        profile = self.view_profile_natural(name, device_id)
        result["profile_viewed"] = profile is not None
        if profile:
            result["profile"] = profile

        if note_template:
            result["connection_sent"] = self.send_rewritten_connection_note(
                name, note_template, context, device_id
            )

        if message_template:
            result["message_sent"] = self.send_rewritten_message(
                name, message_template, context, device_id
            )

        return result
