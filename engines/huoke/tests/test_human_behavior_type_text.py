# -*- coding: utf-8 -*-
"""`_type_text_unicode_safe` — 2 级 fallback 修 2026-04-24 真机 blocker。

覆盖:
  * Path 1 focused.set_text Unicode 安全 (zh/ja/en)
  * Path 2 adb input text ASCII 兜底
  * Path 1 miss + text 含非 ASCII → 拒绝 fallback 返 False (避免字符丢失)
  * Path 1 异常 + ASCII → 降级 Path 2 仍可用
  * Path 1 异常 + Unicode → False
  * 两路都失败 → False
  * shell 调用参数 escape (空格 → %s, & → \\&)
  * 无 d.shell attr → subprocess 降级
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ─── Path 1: focused.set_text (Unicode 主路径) ───────────────────────────────


class TestPath1FocusedSetText:

    def test_unicode_zh_success(self):
        """中文 — Path 1 focused.set_text 成功。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=True)
        focused.set_text = MagicMock()
        d.return_value = focused
        assert _type_text_unicode_safe(d, "你好,测试") is True
        focused.set_text.assert_called_once_with("你好,测试")
        # shell 不应被调 (Path 2 无需走)
        if hasattr(d, "shell"):
            d.shell.assert_not_called()

    def test_unicode_ja_success(self):
        """日文 — hiragana/katakana/kanji 混合。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=True)
        d.return_value = focused
        assert _type_text_unicode_safe(
            d, "こんにちは、お元気ですか?素敵な一日を。") is True
        focused.set_text.assert_called_once()

    def test_ascii_en_success(self):
        """英文 — Path 1 也 work, 不退到 Path 2。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=True)
        d.return_value = focused
        assert _type_text_unicode_safe(d, "Hello world") is True
        focused.set_text.assert_called_once_with("Hello world")

    def test_focused_miss_ascii_falls_to_path2(self):
        """focused 不存在, ASCII → Path 2 adb input 兜底。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=False)
        d.return_value = focused
        assert _type_text_unicode_safe(d, "Hello") is True
        focused.set_text.assert_not_called()
        d.shell.assert_called_once()
        # 参数应是 ['input', 'text', 'Hello']
        args = d.shell.call_args[0][0]
        assert args[0] == 'input' and args[1] == 'text' and args[2] == 'Hello'

    def test_focused_miss_unicode_returns_false(self):
        """focused 不存在 + text 含非 ASCII → 拒绝 Path 2, 返 False。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=False)
        d.return_value = focused
        assert _type_text_unicode_safe(d, "你好") is False
        focused.set_text.assert_not_called()
        d.shell.assert_not_called()  # 避免字符丢失, 不 fallback

    def test_focused_set_text_exception_ascii_falls_to_path2(self):
        """Path 1 set_text 抛异常 + text ASCII → Path 2 兜底。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=True)
        focused.set_text = MagicMock(side_effect=RuntimeError("widget gone"))
        d.return_value = focused
        assert _type_text_unicode_safe(d, "Hello") is True
        d.shell.assert_called_once()

    def test_focused_set_text_exception_unicode_returns_false(self):
        """Path 1 抛异常 + Unicode → False, 不 fallback 避免乱码。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=True)
        focused.set_text = MagicMock(side_effect=RuntimeError("boom"))
        d.return_value = focused
        assert _type_text_unicode_safe(d, "你好世界") is False


# ─── Path 2: adb shell input text (ASCII fallback) ───────────────────────────


class TestPath2AdbInput:

    def test_ascii_space_escaped(self):
        """空格 → '%s' (adb input text 要求)。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=False)
        d.return_value = focused
        assert _type_text_unicode_safe(d, "Hello world foo") is True
        args = d.shell.call_args[0][0]
        assert args[2] == "Hello%sworld%sfoo"

    def test_ascii_ampersand_escaped(self):
        """& → \\& (shell escape)。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=False)
        d.return_value = focused
        assert _type_text_unicode_safe(d, "a & b") is True
        args = d.shell.call_args[0][0]
        assert args[2] == r"a%s\&%sb"

    def test_shell_exception_returns_false(self):
        """d.shell 抛异常 + Path 1 miss → False。"""
        from src.behavior.human_behavior import _type_text_unicode_safe
        d = MagicMock()
        focused = MagicMock()
        focused.exists = MagicMock(return_value=False)
        d.return_value = focused
        d.shell = MagicMock(side_effect=RuntimeError("adb disconnected"))
        assert _type_text_unicode_safe(d, "Hello") is False

    def test_subprocess_fallback_when_no_shell_attr(self):
        """d 无 shell attr → 走 subprocess adb 兜底。"""
        from src.behavior.human_behavior import _type_text_unicode_safe

        # 用 type() 动态生成没 shell 属性的简单对象
        class _NoShellDevice:
            serial = "IJ8HZLOR"

            def __init__(self):
                self.focused_exists = False

            def __call__(self, **kw):
                """模拟 d(focused=True) → Mock with .exists()=False."""
                m = MagicMock()
                m.exists = MagicMock(return_value=False)
                return m

        d = _NoShellDevice()
        with patch("subprocess.run") as mrun:
            mrun.return_value = MagicMock(returncode=0)
            assert _type_text_unicode_safe(d, "Hello") is True
            assert mrun.call_count == 1
            cmd = mrun.call_args[0][0]
            assert cmd[0] == "adb" and "-s" in cmd and "IJ8HZLOR" in cmd
            assert cmd[-3:] == ["shell", "input", "text"] or \
                   ("input" in cmd and "text" in cmd and "Hello" in cmd)


# ─── HumanBehavior.type_text 集成 ─────────────────────────────────────────────


class TestTypeTextIntegration:
    """验证 type_text(method) 正确调用 _type_text_unicode_safe。"""

    def _make_hb(self):
        from src.behavior.human_behavior import HumanBehavior
        return HumanBehavior()

    def test_type_text_routes_to_unicode_safe(self):
        """HumanBehavior.type_text 最终调 _type_text_unicode_safe。"""
        from src.behavior import human_behavior as hb_mod
        hb = self._make_hb()
        d = MagicMock()
        d.clear_text = MagicMock()

        with patch.object(hb_mod, "_type_text_unicode_safe",
                          return_value=True) as m_safe, \
             patch("src.behavior.human_behavior.time.sleep"), \
             patch("src.behavior.human_behavior.random.uniform",
                   return_value=0.1), \
             patch("src.behavior.human_behavior.random.gauss",
                   return_value=50.0), \
             patch("src.behavior.human_behavior.random.random",
                   return_value=0.99), \
             patch("src.behavior.human_behavior.random.randint",
                   return_value=0):
            hb.type_text(d, "你好 hello こんにちは")

        m_safe.assert_called_once_with(d, "你好 hello こんにちは")
        d.clear_text.assert_called_once()  # clear_first=True 默认

    def test_type_text_clear_text_exception_does_not_block(self):
        """d.clear_text 抛异常 → 继续, 不影响主流程。"""
        from src.behavior import human_behavior as hb_mod
        hb = self._make_hb()
        d = MagicMock()
        d.clear_text = MagicMock(side_effect=RuntimeError("not focused"))
        with patch.object(hb_mod, "_type_text_unicode_safe",
                          return_value=True) as m_safe, \
             patch("src.behavior.human_behavior.time.sleep"), \
             patch("src.behavior.human_behavior.random.uniform",
                   return_value=0.1), \
             patch("src.behavior.human_behavior.random.gauss",
                   return_value=50.0), \
             patch("src.behavior.human_behavior.random.random",
                   return_value=0.99), \
             patch("src.behavior.human_behavior.random.randint",
                   return_value=0):
            hb.type_text(d, "hello")
        m_safe.assert_called_once()

    def test_type_text_clear_first_false(self):
        """clear_first=False → 不调 clear_text。"""
        from src.behavior import human_behavior as hb_mod
        hb = self._make_hb()
        d = MagicMock()
        with patch.object(hb_mod, "_type_text_unicode_safe",
                          return_value=True), \
             patch("src.behavior.human_behavior.time.sleep"), \
             patch("src.behavior.human_behavior.random.uniform",
                   return_value=0.1), \
             patch("src.behavior.human_behavior.random.gauss",
                   return_value=50.0), \
             patch("src.behavior.human_behavior.random.random",
                   return_value=0.99), \
             patch("src.behavior.human_behavior.random.randint",
                   return_value=0):
            hb.type_text(d, "hi", clear_first=False)
        d.clear_text.assert_not_called()
