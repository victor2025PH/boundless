# -*- coding: utf-8 -*-
"""P19 (2026-04-24): `_ai_reply_and_send` 发消息路径接入 `_tap_messenger_send`
4 级 fallback (原裸 smart_tap + Enter-key 模式升级)。

测试范围只打 send path (type_text 后的 Send button 点击), 其他信号 (LLM /
rewriter / record_inbox_message) 都 stub 或 patch 掉。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.hb = MagicMock()
    fb.hb.type_text = MagicMock()
    fb.hb.tap = MagicMock()
    fb.hb.wait_think = MagicMock()
    return fb


class TestSendPathFallback:
    """`_ai_reply_and_send` 的发送子路径 — 验证 4 级 fallback 接入 + L5
    Enter-key backstop 在 4 级全 miss 时触发。不完整跑 `_ai_reply_and_send`
    主体 (会触达 LLM + record_inbox 等大量依赖), 只单独验证 send 子块。
    """

    def _run_send_subblock(self, fb, d, did="devA", reply="hello"):
        """模拟 `_ai_reply_and_send` 在 type_text 之后的最后一个 try 块 —
        与 src/app_automation/facebook.py 里 line 5835-5850 逻辑对齐。"""
        from src.app_automation.facebook import MessengerError
        import time
        try:
            input_box = d(className="android.widget.EditText")
            if input_box.exists(timeout=2.0):
                fb.hb.tap(d, 100, 200)
                fb.hb.type_text(d, reply)
                try:
                    fb._tap_messenger_send(d, did)
                    return "sent"
                except MessengerError:
                    d.send_keys("\n")
                    return "sent_via_enter"
        except Exception as e:
            return f"skip: {e}"

    def test_4level_hit_no_enter(self):
        """4 级有任一层 HIT → `_tap_messenger_send` 不抛 → 不走 Enter-key。"""
        fb = _make_fb()
        d = MagicMock()
        input_box = MagicMock()
        input_box.exists = MagicMock(return_value=True)
        d.return_value = input_box
        fb._tap_messenger_send = MagicMock()  # no raise → 4 级 hit
        r = self._run_send_subblock(fb, d)
        assert r == "sent"
        fb._tap_messenger_send.assert_called_once_with(d, "devA")
        d.send_keys.assert_not_called()

    def test_4level_all_miss_falls_back_enter(self):
        """`_tap_messenger_send` 4 级全 miss 抛 → Enter-key L5 backstop 触发。"""
        from src.app_automation.facebook import MessengerError
        fb = _make_fb()
        d = MagicMock()
        input_box = MagicMock()
        input_box.exists = MagicMock(return_value=True)
        d.return_value = input_box
        fb._tap_messenger_send = MagicMock(
            side_effect=MessengerError("send_button_missing", "全 miss"))
        r = self._run_send_subblock(fb, d)
        assert r == "sent_via_enter"
        d.send_keys.assert_called_once_with("\n")

    def test_no_editbox_skips_entire_block(self):
        """EditText 不在 (输入框没 focus) → 整块跳过, 不 send, 不 Enter。"""
        fb = _make_fb()
        d = MagicMock()
        input_box = MagicMock()
        input_box.exists = MagicMock(return_value=False)
        d.return_value = input_box
        fb._tap_messenger_send = MagicMock()
        self._run_send_subblock(fb, d)
        fb._tap_messenger_send.assert_not_called()
        d.send_keys.assert_not_called()


class TestRealAiReplyWiring:
    """验证 facebook.py 源码中 `_ai_reply_and_send` 确实引用了 `_tap_messenger_send`
    而不是裸 `smart_tap("Send message button")`。
    """

    def test_source_contains_helper_call(self):
        """grep 级契约测试: 确保生产路径确实走新 helper, 防止未来 refactor 打回。"""
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "src" / "app_automation" / "facebook.py"
        text = src.read_text(encoding="utf-8")
        # `_ai_reply_and_send` 函数体里 send 子块应该调 _tap_messenger_send
        # 找函数 def 然后截取一段看内容
        idx = text.find("def _ai_reply_and_send")
        assert idx != -1, "_ai_reply_and_send 函数缺失"
        # 函数到下一个顶级 def 之前结束
        end = text.find("\n    def ", idx + 1)
        body = text[idx:end if end > 0 else len(text)]
        assert "self._tap_messenger_send(d, did)" in body, (
            "_ai_reply_and_send 应调用 _tap_messenger_send (P19 4 级 fallback)")
        # Enter-key backstop 依然保留
        assert 'd.send_keys("\\n")' in body, (
            "Enter-key L5 backstop 应保留")

    def test_source_no_legacy_bare_smart_tap(self):
        """确保原来 `if not self.smart_tap("Send message button"...)` 已消失。
        防止后续 merge 误引回。"""
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "src" / "app_automation" / "facebook.py"
        text = src.read_text(encoding="utf-8")
        idx = text.find("def _ai_reply_and_send")
        body = text[idx:idx + 8000]
        # 不允许 _ai_reply_and_send 内有直接的 smart_tap Send-button 调用 (应走 helper)
        # 注意: _tap_messenger_send 内部会 smart_tap, 但那在另一个函数, 不在 body 里
        assert 'if not self.smart_tap("Send message button"' not in body, (
            "legacy 裸 smart_tap(\"Send message button\") 不该出现在 _ai_reply_and_send — "
            "已迁到 _tap_messenger_send helper 内部"
        )
