# -*- coding: utf-8 -*-
"""Phase 15 (2026-04-25): _is_valid_peer_name + _list_messenger_conversations 单测.

修 Messenger inbox 抓取 root bug — "查看翻译" 等 UI 文本被当 peer_name 写
进 fb_contact_events 污染 dispatcher 数据流.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestIsValidPeerName:
    def setup_method(self):
        from src.app_automation.facebook import FacebookAutomation
        self.fn = FacebookAutomation._is_valid_peer_name

    # ── 应该被 ban 的 ─────────────────────────────────────────────

    def test_translate_zh_rejected(self):
        assert self.fn("查看翻译") is False
        assert self.fn("点击翻译") is False
        assert self.fn("显示原文") is False

    def test_translate_en_rejected(self):
        assert self.fn("See translation") is False
        assert self.fn("Tap to translate") is False
        assert self.fn("Translate") is False

    def test_translate_jp_rejected(self):
        assert self.fn("翻訳を表示") is False
        assert self.fn("原文") is False

    def test_buttons_rejected(self):
        for btn in ["Reply", "Send", "More", "Edit", "Delete", "Block"]:
            assert self.fn(btn) is False, f"{btn!r} 应被 ban"

    def test_status_indicators_rejected(self):
        for s in ["Active now", "Online", "Typing", "Seen",
                   "在线", "已读", "オンライン"]:
            assert self.fn(s) is False, f"{s!r} 应被 ban"

    def test_message_preview_with_colon_rejected(self):
        assert self.fn("Alice: hello") is False
        assert self.fn("山田: ありがとう") is False

    def test_message_preview_ellipsis_rejected(self):
        assert self.fn("ありがとう...") is False
        assert self.fn("Hi there…") is False

    def test_too_short_rejected(self):
        assert self.fn("") is False
        assert self.fn("a") is False
        assert self.fn(" ") is False

    def test_too_long_rejected(self):
        # > 30 字符
        assert self.fn("a" * 31) is False

    def test_only_digits_rejected(self):
        assert self.fn("12345") is False

    def test_only_punct_rejected(self):
        assert self.fn("...") is False
        assert self.fn("•") is False

    def test_sentence_ending_punct_rejected(self):
        assert self.fn("Hello!") is False
        assert self.fn("Yes?") is False
        assert self.fn("こんにちは。") is False

    def test_ascii_single_word_capitalized_rejected(self):
        """单词 ASCII + 首大写 + 后小写 = 按钮启发式."""
        assert self.fn("Translate") is False
        assert self.fn("Reply") is False
        assert self.fn("Send") is False

    # ── 应该通过的 ─────────────────────────────────────────────

    def test_japanese_real_name_passes(self):
        for name in ["山田花子", "佐藤美咲", "中村恵", "田中由紀子"]:
            assert self.fn(name) is True, f"{name!r} 应通过"

    def test_chinese_real_name_passes(self):
        assert self.fn("张小明") is True
        assert self.fn("李四") is True

    def test_english_full_name_passes(self):
        """空格分隔的英文全名应通过 (绕过 'ASCII 单词按钮' 启发式)."""
        assert self.fn("John Smith") is True
        assert self.fn("Maria Rossi") is True

    def test_italian_name_passes(self):
        assert self.fn("Giulia Bianchi") is True

    def test_lower_case_ascii_word_passes(self):
        """全小写不触发"按钮启发式" (按钮是首字母大写)."""
        # 真名通常不全小写, 但允许 (避免误杀 username-style)
        assert self.fn("alice") is True

    def test_long_japanese_name_passes(self):
        """30 字符以内的长日文 / 韩文名应通过."""
        assert self.fn("田中由紀子と山田花子") is True  # 10 字符
        assert self.fn("a" * 30) is True  # 边界


class TestListMessengerConversationsSanitize:
    """端到端: _list_messenger_conversations 在 mock 设备上不抓 UI 文本."""
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        return FacebookAutomation.__new__(FacebookAutomation)

    def test_real_jp_name_kept_ui_filtered(self, monkeypatch):
        """混合元素列表: 真名通过, "查看翻译" / "Reply" 被过滤."""
        fb = self._make_fb()
        # 构造 fake elements
        class _El:
            def __init__(self, text, clickable=True, selected=False, bounds=None):
                self.text = text
                self.clickable = clickable
                self.selected = selected
                self.bounds = bounds or (0, 0, 100, 100)
        fake_elements = [
            _El("山田花子"),    # 真用户
            _El("查看翻译"),    # ban
            _El("Reply"),       # ban
            _El("More"),        # ban
            _El("ありがとう..."),  # 消息预览 ban
            _El("佐藤美咲"),    # 真用户
            _El("Active now"),  # ban
        ]

        d = MagicMock()
        d.dump_hierarchy.return_value = "<dummy/>"

        # patch XMLParser.parse 返 fake elements
        import src.vision.screen_parser as sp_mod
        monkeypatch.setattr(sp_mod.XMLParser, "parse",
                            staticmethod(lambda xml: fake_elements))

        items = fb._list_messenger_conversations(d, max_n=10)
        names = [it["name"] for it in items]
        assert names == ["山田花子", "佐藤美咲"]

    def test_non_clickable_filtered(self, monkeypatch):
        fb = self._make_fb()
        class _El:
            def __init__(self, text, clickable):
                self.text = text
                self.clickable = clickable
                self.selected = False
                self.bounds = (0, 0, 100, 100)
        fake = [_El("山田花子", clickable=False),  # 即使是真名, 不可点击 skip
                _El("佐藤美咲", clickable=True)]
        d = MagicMock(); d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp_mod
        monkeypatch.setattr(sp_mod.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = fb._list_messenger_conversations(d, max_n=10)
        assert [it["name"] for it in items] == ["佐藤美咲"]

    def test_max_n_caps(self, monkeypatch):
        fb = self._make_fb()
        class _El:
            def __init__(self, text):
                self.text = text
                self.clickable = True
                self.selected = False
                self.bounds = (0, 0, 100, 100)
        fake = [_El(f"用户{i}") for i in range(20)]
        d = MagicMock(); d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp_mod
        monkeypatch.setattr(sp_mod.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = fb._list_messenger_conversations(d, max_n=5)
        assert len(items) == 5
