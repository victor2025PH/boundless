#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WhatsApp 应用自动化模块

基于 2026-03-12 Redmi 13C (Android 13) 实测 UI dump 建立。
与 Telegram 模块的关键差异:
  - WhatsApp 大量使用 resource-id（Telegram 主要用 content-desc）
  - 搜索入口是顶部搜索栏（非独立按钮）
  - 联系人用手机号或显示名搜索（无 @username）

集成 HumanBehavior + ComplianceGuard (Phase 1).
"""

import time
import logging
from typing import Optional, List, Dict
from pathlib import Path

from ..device_control.device_manager import DeviceManager, get_device_manager
from .base_automation import BaseAutomation

PKG = "com.whatsapp"


class WA:
    """
    WhatsApp u2 选择器 — 基于 2026-03-13 两台 Redmi 13C 真机校准。
    Phone1 (v2.26.8.72) 中文界面, Phone2 (v2.26.9.72) 英文界面。
    """

    # ── 搜索 ──
    SEARCH_BAR = [
        {"resourceId": "com.whatsapp:id/my_search_bar"},
        {"description": "搜索"},
        {"description": "Search"},
    ]
    SEARCH_INPUT = [
        {"resourceId": "com.whatsapp:id/search_src_text"},
        {"className": "android.widget.EditText", "packageName": PKG},
    ]

    # ── 聊天列表 ──
    CONTACT_NAME = "com.whatsapp:id/conversations_row_contact_name"
    CONTACT_ROW = "com.whatsapp:id/contact_row_container"
    SUGGESTED_ITEM = "com.whatsapp:id/suggested_contacts_list_item_container"
    SUGGESTED_NAME = "com.whatsapp:id/suggested_contacts_list_item_name"
    SUGGESTED_CHAT_BTN = "com.whatsapp:id/suggested_contacts_list_item_chat_button"

    # ── 对话界面 ──
    MESSAGE_INPUT = [
        {"resourceId": "com.whatsapp:id/entry"},
        {"className": "android.widget.EditText", "packageName": PKG},
    ]
    SEND_BUTTON = [
        {"resourceId": "com.whatsapp:id/send"},
        {"description": "发送"},
        {"description": "Send"},
    ]
    ATTACH_BUTTON = [
        {"description": "附加"},
        {"description": "Attach"},
        {"resourceId": "com.whatsapp:id/input_attach_button"},
    ]
    HEADER_TITLE = "com.whatsapp:id/conversation_contact_name"
    VOICE_BUTTON = [
        {"resourceId": "com.whatsapp:id/voice_btn"},
        {"description": "语音消息"},
        {"description": "Voice message"},
    ]

    # ── 新聊天/导航 ──
    NEW_CHAT_FAB = "com.whatsapp:id/fab"
    META_AI_FAB = "com.whatsapp:id/extended_mini_fab"

    # ── 底部导航 (2026 版本用 BottomNavigationView) ──
    TAB_CHATS = [{"text": "聊天"}, {"text": "Chats"}]
    TAB_UPDATES = [{"text": "更新"}, {"text": "Updates"}]
    TAB_COMMUNITIES = [{"text": "社群"}, {"text": "Communities"}]
    TAB_CALLS = [{"text": "通话"}, {"text": "Calls"}]

    # ── 工具栏 ──
    CAMERA_BTN = {"resourceId": "com.whatsapp:id/menuitem_camera"}
    OVERFLOW_BTN = {"resourceId": "com.whatsapp:id/menuitem_overflow"}

    # ── 常见弹窗 ──
    BACKUP_NOT_NOW = [{"text": "NOT NOW"}, {"text": "以后再说"}]
    BACKUP_DONE = [{"text": "DONE"}, {"text": "完成"}]


class WhatsAppAutomation(BaseAutomation):
    """WhatsApp 自动化 — 继承 BaseAutomation 获得 HumanBehavior + ComplianceGuard"""

    PLATFORM = "whatsapp"
    PACKAGE = PKG
    MAIN_ACTIVITY = ".HomeActivity"

    def __init__(self, device_manager: DeviceManager, **kwargs):
        super().__init__(device_manager, **kwargs)

    def _use_u2(self, device_id: str) -> bool:
        return self.dm.get_u2(device_id) is not None

    # =========================================================================
    # App 管理
    # =========================================================================

    def start_whatsapp(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        self.logger.info("Starting WhatsApp on %s", did[:8])
        ok = self.start_app(did)
        if ok:
            self._dismiss_startup_dialogs(did)
        return ok

    def _dismiss_startup_dialogs(self, device_id: Optional[str] = None):
        """Handle backup prompts and other startup dialogs."""
        d = self._u2_optional(device_id)
        if not d:
            return
        for sel_list in [WA.BACKUP_NOT_NOW, WA.BACKUP_DONE]:
            for sel in sel_list:
                btn = d(**sel)
                if btn.exists(timeout=1):
                    btn.click()
                    self.logger.info("Dismissed startup dialog: %s", sel)
                    time.sleep(2)
                    return

    def _is_running(self, device_id: str) -> bool:
        return self.is_foreground(device_id)

    # =========================================================================
    # 搜索 & 打开聊天
    # =========================================================================

    def search_and_open_user(self, contact: str,
                             device_id: Optional[str] = None) -> bool:
        """
        搜索联系人并打开聊天。contact 可以是显示名或手机号。
        """
        did = self._did(device_id)
        self.logger.info(f"搜索 WhatsApp 联系人: {contact}")

        if not self._is_running(did):
            if not self.start_whatsapp(did):
                return False

        d = self.dm.get_u2(did)
        if not d:
            self.logger.error("u2 不可用")
            return False

        # 1) 点击搜索栏
        self.logger.info("步骤1: 点击搜索栏")
        clicked = False
        for sel in WA.SEARCH_BAR:
            elem = d(**sel)
            if elem.exists(timeout=5):
                elem.click()
                clicked = True
                break
        if not clicked:
            self.logger.error("搜索栏未找到")
            return False
        time.sleep(1)

        # 2) 输入联系人
        self.logger.info(f"步骤2: 输入 {contact}")
        for sel in WA.SEARCH_INPUT:
            elem = d(**sel)
            if elem.exists(timeout=5):
                elem.set_text(contact)
                break
        else:
            self.logger.error("搜索输入框未找到")
            return False
        time.sleep(2)

        # 3) 点击搜索结果
        self.logger.info("步骤3: 点击搜索结果")
        if not self._click_search_result(d, contact):
            self.logger.error("搜索结果匹配失败")
            d.press("back")
            d.press("back")
            return False
        time.sleep(1.5)

        # 4) 验证聊天界面
        for sel in WA.MESSAGE_INPUT:
            if d(**sel).exists(timeout=5):
                self.logger.info("成功进入聊天界面")
                return True

        self.logger.error("未检测到消息输入框")
        return False

    def _click_search_result(self, d, contact: str) -> bool:
        """在搜索结果中找到匹配的联系人并点击"""
        clean = contact.strip().lower()

        # 方案1: 通过 resource-id 找联系人名字
        names = d(resourceId=WA.CONTACT_NAME)
        if names.exists(timeout=3):
            for i in range(names.count):
                try:
                    name_elem = names[i]
                    name_text = name_elem.get_text() or ""
                    if clean in name_text.lower():
                        self.logger.info(f"匹配联系人: '{name_text}'")
                        # 点击父容器（整行可点击）
                        name_elem.click()
                        return True
                except Exception:
                    continue

        # 方案2: 通过 contact_row_container 匹配
        rows = d(resourceId=WA.CONTACT_ROW)
        if rows.exists(timeout=3):
            for i in range(rows.count):
                try:
                    row = rows[i]
                    info = row.info
                    text = info.get("text", "") or info.get("contentDescription", "")
                    if clean in text.lower():
                        row.click()
                        return True
                except Exception:
                    continue

        # 方案3: 最后尝试文本包含匹配
        match = d(textContains=contact)
        if match.exists(timeout=2):
            match.click()
            return True

        return False

    # =========================================================================
    # 消息发送
    # =========================================================================

    def send_text_message(self, message: str,
                          device_id: Optional[str] = None) -> bool:
        """在当前聊天中发送文本消息（带 HumanBehavior + ComplianceGuard）"""
        did = self._did(device_id)
        d = self.dm.get_u2(did)
        if not d:
            return False

        self.logger.info(f"发送 WhatsApp 消息: {message[:50]}...")

        with self.guarded("send_message", device_id=did):
            # 输入消息
            for sel in WA.MESSAGE_INPUT:
                elem = d(**sel)
                if elem.exists(timeout=5):
                    elem.click()
                    self.hb.wait_think(0.3)
                    self.hb.type_text(d, message)
                    break
            else:
                self.logger.error("消息输入框未找到")
                return False

            self.hb.wait_think(0.5)

            # 点击发送
            for sel in WA.SEND_BUTTON:
                elem = d(**sel)
                if elem.exists(timeout=5):
                    info = elem.info
                    cx = (info["bounds"]["left"] + info["bounds"]["right"]) // 2
                    cy = (info["bounds"]["top"] + info["bounds"]["bottom"]) // 2
                    self.hb.tap(d, cx, cy)
                    self.logger.info("消息已发送")
                    return True

            self.logger.error("发送按钮未找到")
            return False

    # =========================================================================
    # 消息读取
    # =========================================================================

    def read_messages(self, device_id: Optional[str] = None,
                      count: int = 20) -> List[dict]:
        """读取当前聊天中的消息"""
        did = self._did(device_id)
        d = self.dm.get_u2(did)
        if not d:
            return []

        xml = d.dump_hierarchy()
        from lxml import etree
        root = etree.fromstring(xml.encode("utf-8"))

        msgs = []
        for node in root.iter("node"):
            if PKG not in node.get("package", ""):
                continue
            cls = node.get("class", "")
            rid = node.get("resource-id", "")

            # WhatsApp 消息使用 content-desc 描述完整内容
            desc = node.get("content-desc", "")
            if not desc:
                continue

            # 识别消息模式（实测 WhatsApp content-desc 格式多样）
            msg = self._parse_wa_message(desc)
            if msg:
                msgs.append(msg)

        unique = list({m["text"]: m for m in msgs}.values())
        return unique[-count:]

    @staticmethod
    def _parse_wa_message(desc: str) -> Optional[dict]:
        """解析 WhatsApp 消息的 content-desc"""
        import re

        # 跳过 UI 元素描述
        skip_prefixes = ("搜索", "Search", "更多", "新聊天", "相机",
                         "附加", "发送", "语音", "WhatsApp的照片",
                         "聊天,", "更新", "社群", "通话", "关闭",
                         "Go back", "返回", "‎WhatsApp")
        for p in skip_prefixes:
            if desc.startswith(p):
                return None

        # WhatsApp 消息格式多种（根据语言和版本不同）
        # 常见: "你: 消息内容, 时间"  或 "联系人名: 消息内容, 时间"
        if len(desc) < 5:
            return None

        return {
            "text": desc,
            "direction": "unknown",
            "time": "",
            "status": "",
        }

    # =========================================================================
    # 导航
    # =========================================================================

    def go_chats(self, device_id: Optional[str] = None) -> bool:
        d = self._u2_optional(self._did(device_id))
        if not d:
            return False
        for sel in WA.TAB_CHATS:
            if d(**sel).exists(timeout=2):
                d(**sel).click()
                time.sleep(1)
                return True
        return False

    def go_calls(self, device_id: Optional[str] = None) -> bool:
        d = self._u2_optional(self._did(device_id))
        if not d:
            return False
        for sel in WA.TAB_CALLS:
            if d(**sel).exists(timeout=2):
                d(**sel).click()
                time.sleep(1)
                return True
        return False

    # =========================================================================
    # 媒体发送
    # =========================================================================

    def send_media(self, contact: str, file_path: str,
                   caption: str = "", device_id: Optional[str] = None) -> bool:
        """发送图片/视频/文件给联系人。先 push 到设备再通过 attach 发送。"""
        did = self._did(device_id)
        d = self._u2_optional(did)
        if not d:
            return False

        with self.guarded("send_media", device_id=did):
            if not self.search_and_open_user(contact, did):
                return False

            remote = f"/sdcard/Download/{Path(file_path).name}"
            self.dm.execute_adb_command(f"push {file_path} {remote}", did)
            time.sleep(1)

            # Click attach
            for sel in WA.ATTACH_BUTTON:
                if d(**sel).exists(timeout=3):
                    d(**sel).click()
                    break
            else:
                self.logger.error("Attach button not found")
                return False

            time.sleep(2)

            doc_btn = d(text="文档") if d(text="文档").exists(timeout=2) else d(text="Document")
            if doc_btn.exists(timeout=2):
                doc_btn.click()
                time.sleep(2)

            self.logger.info("Media send flow initiated for %s", Path(file_path).name)
            return True

    # =========================================================================
    # 群聊操作
    # =========================================================================

    def read_group_messages(self, group_name: str,
                            device_id: Optional[str] = None,
                            count: int = 20) -> List[dict]:
        """打开群聊并读取消息"""
        did = self._did(device_id)
        if not self.search_and_open_user(group_name, did):
            return []
        msgs = self.read_messages(did, count)
        self.go_back(did)
        return msgs

    def send_group_message(self, group_name: str, message: str,
                           device_id: Optional[str] = None) -> bool:
        """给群聊发送消息"""
        did = self._did(device_id)
        with self.guarded("send_group_message", device_id=did):
            if not self.search_and_open_user(group_name, did):
                return False
            return self.send_text_message(message, did)

    # =========================================================================
    # 联系人搜索增强
    # =========================================================================

    def list_chats(self, device_id: Optional[str] = None,
                   max_count: int = 20) -> List[Dict]:
        """列出主界面可见的聊天列表"""
        did = self._did(device_id)
        d = self._u2_optional(did)
        if not d:
            return []

        if not self._is_running(did):
            self.start_whatsapp(did)

        self.go_chats(did)
        time.sleep(1)

        from lxml import etree
        xml = d.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))
        chats = []

        for node in root.iter():
            rid = node.get("resource-id", "")
            if rid == WA.CONTACT_NAME:
                name = node.get("text", "")
                if name and name != "WhatsApp":
                    chats.append({"name": name, "type": "chat"})

        for node in root.iter():
            rid = node.get("resource-id", "")
            if rid == WA.SUGGESTED_NAME:
                name = node.get("text", "")
                if name:
                    chats.append({"name": name, "type": "suggested"})

        return chats[:max_count]


    # =========================================================================
    # AI 增强功能
    # =========================================================================

    def send_rewritten_message(self, contact: str, template: str,
                               context: Optional[Dict[str, str]] = None,
                               device_id: Optional[str] = None) -> bool:
        """搜索联系人 → 改写消息 → 发送"""
        did = self._did(device_id)
        unique_msg = self.rewrite_message(template, context)
        self.logger.info("消息已改写: '%s' → '%s'", template[:30], unique_msg[:30])
        if not self.search_and_open_user(contact, did):
            return False
        return self.send_text_message(unique_msg, did)

    def auto_reply_chat(self, contact: str, device_id: Optional[str] = None,
                        persona: str = "casual", duration_sec: int = 300,
                        check_interval: float = 30.0) -> List[dict]:
        """
        监听 WhatsApp 聊天并自动回复。

        与 Telegram 的 auto_reply_monitor 类似:
        读消息 → 意图分类 → 生成回复 → 模拟延迟 → 发送。
        """
        import time as _time
        did = self._did(device_id)

        try:
            from ..ai.auto_reply import AutoReply
            ar = AutoReply()
        except Exception as e:
            self.logger.error("AutoReply unavailable: %s", e)
            return []

        if not self.search_and_open_user(contact, did):
            return []

        known_texts = set()
        initial_msgs = self.read_messages(did, count=30)
        for m in initial_msgs:
            known_texts.add(m.get("text", ""))

        replies = []
        start = _time.time()

        while _time.time() - start < duration_sec:
            _time.sleep(check_interval)
            current_msgs = self.read_messages(did, count=20)

            for m in current_msgs:
                text = m.get("text", "")
                if not text or text in known_texts:
                    continue
                known_texts.add(text)

                result = ar.generate_reply(
                    message=text, sender=contact,
                    platform="whatsapp", persona=persona,
                    conversation_id=f"whatsapp:{contact}",
                )
                if result:
                    self.logger.info("WA AutoReply: delay=%.1fs, text=%s",
                                     result.delay_sec, result.text[:50])
                    _time.sleep(result.delay_sec)
                    if self.send_text_message(result.text, did):
                        replies.append({
                            "incoming": text, "reply": result.text,
                            "intent": result.intent, "delay": result.delay_sec,
                        })

        self.logger.info("WA AutoReply monitor done, sent %d replies", len(replies))
        return replies


def create_whatsapp_automation(config_path: Optional[str] = None) -> WhatsAppAutomation:
    return WhatsAppAutomation(get_device_manager(config_path))
