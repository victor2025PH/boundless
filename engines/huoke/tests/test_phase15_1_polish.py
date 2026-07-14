# -*- coding: utf-8 -*-
"""Phase 15.1 (2026-04-25) 打磨: emoji range / friend_requests sanitize /
ASCII 短串数字 ban / cleanup --since-days."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestEmojiRangeBan:
    def setup_method(self):
        from src.app_automation.facebook import FacebookAutomation
        self.fn = FacebookAutomation._is_valid_peer_name

    def test_pure_emoji_string_rejected(self):
        """Phase 15.1: 全 emoji 串 (Unicode 类 So) ban."""
        assert self.fn("👋🌸✨") is False
        assert self.fn("❤❤❤") is False
        assert self.fn("🎉🎊") is False

    def test_emoji_with_zwj_rejected(self):
        """ZWJ 复合 emoji 也算 emoji 串 ban."""
        assert self.fn("👨‍👩‍👧") is False  # 家庭 emoji ZWJ 合成

    def test_name_with_trailing_emoji_passes(self):
        """名字 + 末尾装饰 emoji = 真用户常见 (花子🌸), 应通过."""
        assert self.fn("花子🌸") is True


class TestAsciiShortDigitBan:
    def setup_method(self):
        from src.app_automation.facebook import FacebookAutomation
        self.fn = FacebookAutomation._is_valid_peer_name

    def test_p0_p1_p2_rejected(self):
        """Phase 15.1: ASCII <= 4 字符含数字 = 测试残留 / 编号."""
        assert self.fn("p0") is False
        assert self.fn("p1") is False
        assert self.fn("p2") is False
        assert self.fn("a1") is False
        assert self.fn("X3") is False

    def test_pure_letters_short_passes(self):
        """纯字母 alice/bob 不 ban (避免误杀真昵称)."""
        # 注意: "Alice" 因首大写后小写 + 5 字符 + ASCII 已被旧规则 ban
        # 本规则只关心 含数字 的短 ASCII
        assert self.fn("alice") is True
        assert self.fn("bob") is True

    def test_jp_short_name_passes(self):
        """2 字符日文不受影响 (非 ASCII)."""
        assert self.fn("山田") is True
        assert self.fn("佐藤") is True

    def test_long_ascii_with_digits_passes(self):
        """长 ASCII 含数字 (>4) 不被本条 ban — 留给其它规则."""
        # 注意: 这种全数字混字母可能是 user123 username, 不 ban
        assert self.fn("user123") is True


class TestFriendRequestsSanitize:
    """_list_friend_requests 接入 sanitize."""
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        return FacebookAutomation.__new__(FacebookAutomation)

    def test_ui_text_filtered_in_friend_requests(self, monkeypatch):
        """friend_requests inbox 抓取也用 _is_valid_peer_name."""
        fb = self._make_fb()

        class _El:
            def __init__(self, text, clickable=True):
                self.text = text
                self.clickable = clickable
                self.bounds = (0, 0, 100, 100)

        # 混合: 真名 + UI 按钮 + 测试残留
        fake = [
            _El("山田花子"),
            _El("查看翻译"),
            _El("Confirm"),  # FB 好友请求 confirm 按钮
            _El("p0"),
            _El("Mark as read"),
            _El("佐藤美咲"),
        ]
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp_mod
        monkeypatch.setattr(sp_mod.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = fb._list_friend_requests(d, max_n=10)
        names = [it["name"] for it in items]
        # "Confirm" 是 ASCII 单词首大写 + 7 字符 — 旧规则就 ban
        # "查看翻译" / "Mark as read" 黑名单 ban
        # "p0" 新规则 ban
        assert "山田花子" in names
        assert "佐藤美咲" in names
        assert "查看翻译" not in names
        assert "p0" not in names
        assert "Confirm" not in names

    def test_mutual_friend_pattern_kept(self, monkeypatch):
        """带 mutual friends 模式且 name 部分合法的应保留."""
        fb = self._make_fb()

        class _El:
            def __init__(self, text):
                self.text = text
                self.clickable = True
                self.bounds = (0, 0, 100, 100)
        fake = [_El("山田花子\n3 mutual friends")]
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp_mod
        monkeypatch.setattr(sp_mod.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = fb._list_friend_requests(d, max_n=10)
        # 提取 name 后是"山田花子" 应通过
        # 但实际 mutual matcher 是 lowercase 比较, 该 element text 含 mutual
        # → 走 mutual 分支, name=text.split("\n")[0]="山田花子"
        assert len(items) == 1
        assert items[0]["name"] == "山田花子"
        assert items[0]["mutual_friends"] == 3


class TestCleanupSinceDays:
    """cleanup_dirty_peer_names.py --since-days 过滤."""
    def test_since_days_arg_parses(self):
        """smoke: --since-days=7 不报错."""
        import sys
        from pathlib import Path
        REPO = Path(__file__).resolve().parent.parent
        SCRIPT = REPO / "scripts" / "cleanup_dirty_peer_names.py"
        # 用 argparse --help 验证参数注册
        import subprocess
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        assert r.returncode == 0
        assert "--since-days" in r.stdout
