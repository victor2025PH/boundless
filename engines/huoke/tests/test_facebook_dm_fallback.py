# -*- coding: utf-8 -*-
"""方案 A profile DM fallback (_send_msg_from_current_profile) 单测.

测试契约: (ok, reason) 矩阵 — 每个失败 reason 都应被准确返回.
不依赖真机 / atx-agent / Android, 全 mock.

未覆盖 (留下阶段):
  - chat_input_position_unexpected (bounds 半屏检查)
  - left_chat_after_input / left_chat_after_send (中间态 pkg 验证)
  - send via IME action / geometry fallback (策略分支)
"""
from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest


# ---- 测试 fixture: 构造 minimal FacebookAutomation 跳过 __init__ ----

@pytest.fixture
def fb(monkeypatch):
    """跳过 FacebookAutomation.__init__ 的硬依赖, 只挂上 mock 用 attr.

    monkeypatch 同时把 _capture_immediate_async 短路, 防 forensics 真起
    daemon thread 写盘.
    """
    from src.app_automation import facebook as fb_mod
    monkeypatch.setattr(
        fb_mod, "_capture_immediate_async",
        lambda *a, **k: None,
    )
    inst = fb_mod.FacebookAutomation.__new__(fb_mod.FacebookAutomation)
    inst.hb = MagicMock()
    inst._el_center = MagicMock(return_value=(540, 1200))
    return inst


def _make_device(
    pkgs: list,
    msg_btn_visible: bool = False,
    edit_text_visible: bool = False,
    edit_text_bounds: tuple = (50, 1300, 900, 1400),
    edit_text_after_send_text: str = "",
    send_btn_visible: bool = False,
    send_action_works: bool = False,
    display_height: int = 1440,
):
    """构造一个 mock d 模拟 atx-agent uiautomator2.Device.

    pkgs: 按顺序消费的 app_current 包名列表 (precheck/post-msg-tap/post-input/post-send).
          序列耗尽后保持最后一个值.
    """
    d = MagicMock()

    # app_current — 按 pkg 列表顺序消费
    pkg_iter = iter(pkgs)
    _last = ["com.facebook.katana"]

    def _app_current():
        try:
            v = next(pkg_iter)
            _last[0] = v
        except StopIteration:
            v = _last[0]
        return {"package": v}
    d.app_current.side_effect = _app_current

    d.info = {"displayHeight": display_height}

    # selector dispatch — 直接查 module-level _PROFILE_*_SELECTORS,
    # sibling 加新 selector 测试不破.
    # overlap selector (同时在 MSG/SEND list, 如繁体 '傳送訊息') 按 send 视图,
    # 因 msg loop 永远先命中非 overlap selector ({'text':'Message'}) break.
    from src.app_automation import facebook as _fb_mod
    msg_sel_set = list(_fb_mod._PROFILE_MSG_BTN_SELECTORS)
    send_sel_set = list(_fb_mod._PROFILE_SEND_BTN_SELECTORS)

    def _dispatch(**kwargs):
        obj = MagicMock()
        kw = dict(kwargs)
        in_send = kw in send_sel_set
        in_msg = kw in msg_sel_set
        if in_send:
            obj.exists.return_value = send_btn_visible
            obj.wait.return_value = send_btn_visible
            obj.click.return_value = None
            return obj
        if in_msg:
            obj.exists.return_value = msg_btn_visible
            obj.wait.return_value = msg_btn_visible
            obj.click.return_value = None
            return obj
        if kw.get("className") == "android.widget.EditText":
            obj.exists.return_value = edit_text_visible
            obj.wait.return_value = edit_text_visible
            obj.bounds.return_value = edit_text_bounds
            obj.get_text.return_value = edit_text_after_send_text
            obj.click.return_value = None
            return obj
        # default: not exist
        obj.exists.return_value = False
        obj.wait.return_value = False
        return obj
    d.side_effect = _dispatch

    # IME send_action
    if send_action_works:
        d.send_action = MagicMock(return_value=None)
    else:
        d.send_action = MagicMock(side_effect=Exception("no IME"))

    # geometry 兜底用的 dump_hierarchy: 返一个 minimal XML 不命中任何 candidate
    d.dump_hierarchy.return_value = '<hierarchy/>'

    # click for geometry fallback
    d.click = MagicMock(return_value=None)

    return d


# ---- 测试用例 ----

def test_empty_message_returns_empty_message(fb):
    d = _make_device(["com.facebook.katana"])
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "")
    assert ok is False
    assert reason == "empty_message"


def test_precheck_wrong_pkg_aborts(fb):
    d = _make_device(["com.evil.thirdparty"])
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hi")
    assert ok is False
    assert reason.startswith("not_in_fb_or_orca:")
    assert "com.evil.thirdparty" in reason


def test_no_message_button_when_all_selectors_miss(fb):
    d = _make_device(
        ["com.facebook.katana"],
        msg_btn_visible=False,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hi")
    assert ok is False
    assert reason == "no_message_button"


def test_chat_did_not_open_when_pkg_drifts_after_msg_tap(fb):
    # precheck katana → tap Message → pkg becomes 系统 launcher
    d = _make_device(
        ["com.facebook.katana", "com.android.launcher"],
        msg_btn_visible=True,
        edit_text_visible=False,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hi")
    assert ok is False
    assert reason.startswith("chat_did_not_open:")
    assert "com.android.launcher" in reason


def test_chat_input_missing_when_no_edit_text(fb):
    # precheck → tap Message → orca → 但 EditText 一直不出现
    d = _make_device(
        ["com.facebook.katana", "com.facebook.orca"],
        msg_btn_visible=True,
        edit_text_visible=False,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hi")
    assert ok is False
    assert reason == "chat_input_missing"


def test_send_via_selector_happy_path(fb):
    # 精确控制 4 次 app_current 都返 orca: precheck / post-msg-tap /
    # post-input / final-send-verify
    d = _make_device(
        ["com.facebook.orca"] * 4,
        msg_btn_visible=True,
        edit_text_visible=True,
        edit_text_bounds=(50, 1300, 900, 1400),
        edit_text_after_send_text="",  # 已发送 → input 清空
        send_btn_visible=True,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hello")
    assert ok is True
    assert reason == "sent"


def test_send_failed_when_all_strategies_miss(fb):
    # orca pkg 全程 + EditText 可见 + Send btn 不可见 + IME 不工作 +
    # dump_hierarchy 无 geometry candidate → 三层全失败
    d = _make_device(
        ["com.facebook.orca"] * 5,
        msg_btn_visible=True,
        edit_text_visible=True,
        edit_text_after_send_text="hello",  # 没发出去 → 仍有文字
        send_btn_visible=False,
        send_action_works=False,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hello")
    assert ok is False
    assert reason == "send_failed_no_button"


def test_message_truncated_to_500_chars(fb):
    """type_text 调用应只传前 500 字符 (防 atx-agent IME 异步失败)."""
    long_msg = "あ" * 1000
    d = _make_device(
        ["com.facebook.orca"] * 4,
        msg_btn_visible=True,
        edit_text_visible=True,
        edit_text_after_send_text="",
        send_btn_visible=True,
    )
    fb._send_msg_from_current_profile(d, "DEV", long_msg)
    # 校验 hb.type_text 收到的字符串长度 ≤ 500
    fb.hb.type_text.assert_called_once()
    args, _ = fb.hb.type_text.call_args
    assert len(args[1]) == 500
    assert all(c == "あ" for c in args[1])


def test_lite_pkg_also_accepted_as_fb_family(fb):
    """com.facebook.lite 应该跟 katana/orca 一起被允许."""
    d = _make_device(
        ["com.facebook.lite"] * 4,
        msg_btn_visible=True,
        edit_text_visible=True,
        edit_text_after_send_text="",
        send_btn_visible=True,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hi")
    assert ok is True
    assert reason == "sent"


def test_chat_input_position_unexpected_when_top_half(fb):
    """EditText bounds 顶部 < 屏幕高 * 0.4 → 异常位置 (chat 输入栏不应在
    上半屏). 防 katana 内嵌 chat header 误识别为 input.
    """
    # bounds[1] = 200, displayHeight = 1440, 200/1440 = 0.14 < 0.4
    d = _make_device(
        ["com.facebook.orca", "com.facebook.orca"],
        msg_btn_visible=True,
        edit_text_visible=True,
        edit_text_bounds=(50, 200, 900, 320),  # 上半屏
        display_height=1440,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hi")
    assert ok is False
    assert reason == "chat_input_position_unexpected"


def test_left_chat_after_input_when_pkg_changes(fb):
    """type_text 后 pkg 离开 fb 系 — 通常是 IME 切换或弹窗顶上."""
    # pkg 序列: precheck katana / post-msg-tap orca / post-input launcher
    d = _make_device(
        ["com.facebook.katana", "com.facebook.orca", "com.android.launcher"],
        msg_btn_visible=True,
        edit_text_visible=True,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hi")
    assert ok is False
    assert reason.startswith("left_chat_after_input:")
    assert "com.android.launcher" in reason


def test_left_chat_after_send_when_pkg_changes(fb):
    """send 触发跳转到第三方 app (e.g. 短链拉到浏览器). 防误判 sent=True
    实际消息没真发出去."""
    # pkg sequence: precheck/post-msg-tap/post-input/post-send-final
    # 前 3 次都 orca, send 后变 chrome
    d = _make_device(
        ["com.facebook.orca", "com.facebook.orca",
         "com.facebook.orca", "com.android.chrome"],
        msg_btn_visible=True,
        edit_text_visible=True,
        edit_text_after_send_text="",  # 看上去 send 了
        send_btn_visible=True,
    )
    ok, reason = fb._send_msg_from_current_profile(d, "DEV", "hi")
    assert ok is False
    assert reason.startswith("left_chat_after_send:")
    assert "com.android.chrome" in reason
