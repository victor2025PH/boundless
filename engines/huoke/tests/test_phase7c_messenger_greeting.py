# -*- coding: utf-8 -*-
"""Phase 7c: _send_messenger_greeting_to_peer 单测 (2026-04-24).

逻辑分支覆盖 (UI 用 FakeDevice 模拟, 不真碰设备):
  * Messenger app 启动失败 → messenger_unavailable
  * 搜索入口找不到 → search_ui_missing
  * 搜索无候选 → recipient_not_found
  * 对话页无输入框 → send_fail
  * Send 按钮找不到 → send_button_missing
  * 全 happy path → (True, "")
"""
from __future__ import annotations

import pytest


def _stub_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._messenger_installed_cache = {}
    return fb


class FakeEl:
    """FakeEl — 用于 mock u2 UiObject.

    get_text() 固定返回 get_text_value (默认空), 模拟 "Messenger 发送后输入框
    自动 clear" 的真机行为. set_text 不改这个, 因为我们要测的是发送后 UI
    验证, 而不是 set_text 自身; 要测 set_text 需要分别注入不同 FakeEl.
    """
    def __init__(self, exists_ret: bool = True, get_text_value: str = ""):
        self._exists = exists_ret
        self._get_text_value = get_text_value

    def exists(self, timeout=0):
        return self._exists

    def click(self):
        return True

    def clear_text(self):
        return True

    def set_text(self, t):
        return True

    def get_text(self):
        return self._get_text_value


class FakeDevice:
    """模拟 u2 Device. 通过 query_map 控制每种 selector 是否存在."""

    def __init__(self, *, cur_pkg_seq=None, xml_seq=None, el_map=None):
        self._pkgs = cur_pkg_seq or ["com.facebook.orca"]
        self._xml_seq = xml_seq or [""]
        self._el_map = el_map or {}
        self._xml_idx = 0
        self._pkg_idx = 0

    def app_stop(self, pkg):
        pass

    def app_start(self, pkg):
        pass

    def app_current(self):
        p = self._pkgs[min(self._pkg_idx, len(self._pkgs) - 1)]
        self._pkg_idx += 1
        return {"package": p}

    def dump_hierarchy(self):
        x = self._xml_seq[min(self._xml_idx, len(self._xml_seq) - 1)]
        self._xml_idx += 1
        return x

    def click(self, x, y):
        pass

    def __call__(self, **kwargs):
        key = tuple(sorted(kwargs.items()))
        if key in self._el_map:
            return self._el_map[key]
        # 默认 exists False
        return FakeEl(exists_ret=False)


# Happy-path xml: Messenger 搜索页含 EditText, 搜索后 4 候选
_HAPPY_SEARCH_XML = """<?xml version='1.0'?>
<hierarchy>
  <node index="0" class="android.widget.ImageView" text="" content-desc="山田花子" bounds="[0,388][720,527]" clickable="false" />
</hierarchy>"""


class TestMessengerFallbackPathways:

    def test_messenger_unavailable_when_app_not_foreground(self):
        fb = _stub_fb()
        fake = FakeDevice(cur_pkg_seq=["com.miui.home"])
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is False
        assert code == "messenger_unavailable"

    def test_search_ui_missing_when_no_search_entry(self):
        fb = _stub_fb()
        fake = FakeDevice(cur_pkg_seq=["com.facebook.orca"])
        # 不注册任何搜索入口 selector → 全 exists=False
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is False
        assert code == "search_ui_missing"

    def test_recipient_not_found_when_no_candidates(self):
        fb = _stub_fb()
        fake = FakeDevice(
            cur_pkg_seq=["com.facebook.orca"],
            xml_seq=["<?xml version='1.0'?><hierarchy></hierarchy>"],
            el_map={
                (("descriptionContains", "search"),): FakeEl(True),
                (("className", "android.widget.EditText"),): FakeEl(True),
            },
        )
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is False
        assert code == "recipient_not_found"

    def test_happy_path_returns_true(self):
        fb = _stub_fb()
        fake = FakeDevice(
            cur_pkg_seq=["com.facebook.orca"],
            xml_seq=[_HAPPY_SEARCH_XML],
            el_map={
                (("descriptionContains", "search"),): FakeEl(True),
                # 搜索输入框 + 对话输入框 (都被映射到同 selector)
                (("className", "android.widget.EditText"),): FakeEl(True),
                (("description", "Send"),): FakeEl(True),
            },
        )
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is True
        assert code == ""

    def test_peer_exact_match_wins_over_prefix_partial(self):
        """搜"山田花子"遇到同姓不同名 "山田太郎" (片段匹 50 分) 和
        "山田花子" (精确匹 100 分) — 应选精确匹配, 不管顺序."""
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb._messenger_installed_cache = {}

        # 第 1 行是"山田太郎"(片段匹, score 50, y1=388 最顶),
        # 第 2 行是"山田花子"(精确匹, score 100, y1=527)
        xml_mixed = """<?xml version='1.0'?>
<hierarchy>
  <node index="0" class="android.widget.ImageView" text="" content-desc="山田太郎" bounds="[0,388][720,527]" clickable="false" />
  <node index="1" class="android.widget.ImageView" text="" content-desc="山田花子" bounds="[0,527][720,639]" clickable="false" />
</hierarchy>"""

        clicked_points = []

        class FakeDev(FakeDevice):
            def click(self2, x, y):  # noqa: N805
                clicked_points.append((x, y))

        fake = FakeDev(
            cur_pkg_seq=["com.facebook.orca"],
            xml_seq=[xml_mixed],
            el_map={
                (("descriptionContains", "search"),): FakeEl(True),
                (("className", "android.widget.EditText"),): FakeEl(True),
                (("description", "Send"),): FakeEl(True),
            },
        )
        fb._u2 = lambda did=None: fake
        # 只要求 peer 目标点击对应 "山田花子"(第 2 行 y1=527, 中心 y=(527+639)/2=583)
        # 而不是第 1 行"山田太郎" (y1=388, 中心 y=(388+527)/2=457).
        ok, _ = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="hi")
        assert ok is True
        # 找第一次人名 click (不是 send btn, 也不是 search 入口)
        assert clicked_points, "没 click 任何点"
        # 点的 y 应在 (527, 639) 区间, 而不是 (388, 527)
        first_click_y = clicked_points[0][1]
        assert 527 <= first_click_y <= 639, (
            f"预期点击 '山田花子' y 在 [527, 639], 实际 {first_click_y}")

    def test_send_button_missing(self):
        fb = _stub_fb()
        fake = FakeDevice(
            cur_pkg_seq=["com.facebook.orca"],
            xml_seq=[_HAPPY_SEARCH_XML],
            el_map={
                (("descriptionContains", "search"),): FakeEl(True),
                (("className", "android.widget.EditText"),): FakeEl(True),
                # 没注册 Send 按钮 → 全 exists=False
            },
        )
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is False
        assert code == "send_button_missing"
