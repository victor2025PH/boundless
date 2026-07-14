"""P2.X-3 v3 (2026-04-30): Step 2 提交搜索使用 IME action=search 而非 KEYCODE_ENTER.

P2.X-4 (2026-04-30): 提交逻辑已搬入 ``_submit_fb_search_with_verify`` helper,
本文件原有针对 ``enter_group`` 全流程断言 ``send_action`` / ``press('enter')``
的用例不再适用 (``MagicMock(spec=...)`` 会自动 mock helper, 内部 d.send_action
不再被真实调用)。已改写为：

- ``_submit_fb_search_with_verify`` 内部仍优先 send_action('search')
  → 详见 ``test_fb_enter_group_p2x4_caefd0e0.py::test_submit_first_path_succeeds_returns_early``
- send_action 抛异常 → 走 ADB keyevent fallback
  → 详见 ``test_fb_enter_group_p2x4_caefd0e0.py::test_submit_falls_through_to_keyevent_when_send_action_silent_noop``
- 提交后落到 profile → 留证 ``enter_group_submit_landed_on_profile``
  → 详见 ``test_fb_enter_group_p2x4_caefd0e0.py::test_submit_aborts_on_profile_landing_with_proper_step_name``

本文件仅保留：
1. helper 返 False 时上层 enter_group 不再进 Step 3 (端到端契约)
2. helper 返 True 时上层进 Step 3 (端到端契约 — 仍由原 v3 用例覆盖)

历史背景 (任务 fd7b0909):
  d.press('enter') 在 FB Android 的搜索 EditText 上被解释为
  "选中 typeahead 第一项建议" → 通常是同名人物 → 进 profile 页, 而非
  search results 页. 用户手动用键盘"搜索/Go"按钮 (IME action=search)
  能正确进 results 页.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest


def test_step2_uses_send_action_search_not_press_enter():
    """端到端契约: enter_group 必须委托给 ``_submit_fb_search_with_verify`` 提交,
    不再在外层直接 ``d.send_action`` / ``d.press('enter')``。

    helper 内部的优先 send_action 行为, 由 P2.X-4 单测
    (test_submit_first_path_succeeds_returns_early) 直接验证。
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()
    mock_d.dump_hierarchy.return_value = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '<node text="All"/>'
        '<node text="Groups"/>'
        '<node text="People"/>'
        '<node text="Public group"/>'
        '</hierarchy>'
    )
    inst._u2.return_value = mock_d
    inst._tap_search_bar_preferred.return_value = True
    inst._submit_fb_search_with_verify.return_value = True  # helper 假设成功
    inst._tap_search_results_groups_filter.return_value = True
    inst._tap_first_search_result_group.return_value = False
    inst.smart_tap.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    fb_mod.FacebookAutomation.enter_group(inst, "潮味", device_id="dev1")

    # 关键: 提交委托给 helper
    inst._submit_fb_search_with_verify.assert_called_once()
    # 外层不再直接调 d.press('enter') 作为主提交路径
    enter_calls = [
        c for c in mock_d.press.call_args_list
        if c.args and c.args[0] in ("enter", 66)
    ]
    assert not enter_calls, (
        f"外层不应再直接 press('enter') (已搬入 helper). 发生: {enter_calls}"
    )


def test_step2_falls_back_to_press_enter_when_send_action_raises():
    """端到端契约: helper 内部异常被吞掉 (返 False), 上层 enter_group 不应崩。

    helper 内部的多路 fallback (send_action → keyevent 66 → keyevent 84)
    由 P2.X-4 单测 (test_submit_falls_through_to_keyevent_when_send_action_silent_noop)
    验证。
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()
    mock_d.dump_hierarchy.return_value = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '</hierarchy>'
    )
    inst._u2.return_value = mock_d
    inst._tap_search_bar_preferred.return_value = True
    # helper 在三路全失败时返 False, 上层应平滑 return False, 不抛异常
    inst._submit_fb_search_with_verify.return_value = False
    inst.smart_tap.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    # 关键: enter_group 在 helper 失败时优雅 return False, 不抛异常
    result = fb_mod.FacebookAutomation.enter_group(
        inst, "潮味", device_id="dev1",
    )
    assert result is False
    # Step 3 不应被调用 (helper 已挡住)
    inst._tap_search_results_groups_filter.assert_not_called()


def test_step2_5_returns_false_when_landed_on_profile_after_submit():
    """端到端契约: helper 返 False (例如内部检测到 profile 误入) 时,
    上层 enter_group 立即 return False, 不进 Step 3。

    profile 误入的留证 (``enter_group_submit_landed_on_profile``) 由 P2.X-4
    单测 (test_submit_aborts_on_profile_landing_with_proper_step_name) 验证。
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()

    search_xml = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '</hierarchy>'
    )
    mock_d.dump_hierarchy.return_value = search_xml
    inst._u2.return_value = mock_d
    inst._tap_search_bar_preferred.return_value = True
    # helper 检测到 profile 后内部已 capture_immediate 并返 False
    inst._submit_fb_search_with_verify.return_value = False
    inst.smart_tap.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    result = fb_mod.FacebookAutomation.enter_group(
        inst, "潮味", device_id="dev1",
    )

    assert result is False, "helper 返 False 时上层必须 return False"
    # Step 3 / 4 都不应被调用
    inst._tap_search_results_groups_filter.assert_not_called()
    inst._tap_first_search_result_group.assert_not_called()


def test_step2_5_passes_when_on_search_results_page():
    """正向: 提交后在搜索结果页 (含 chip / 含 '位成员' 等), Step 2.5 通过, 继续 Step 3."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()

    search_xml = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '</hierarchy>'
    )
    results_xml = (
        '<hierarchy>'
        '<node text="" content-desc="小组个搜索结果, 第3项，共7项" clickable="true"/>'
        '<node text="潮味决士林中正店"/>'
        '<node text="公开 · 385 位成员"/>'
        '</hierarchy>'
    )
    mock_d.dump_hierarchy.side_effect = [
        search_xml, results_xml, results_xml, results_xml,
    ]
    inst._u2.return_value = mock_d
    inst._tap_search_bar_preferred.return_value = True
    inst._tap_search_results_groups_filter.return_value = True
    inst._tap_first_search_result_group.return_value = False  # 让流程退出
    inst.smart_tap.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    fb_mod.FacebookAutomation.enter_group(inst, "潮味", device_id="dev1")

    # Step 3 应被调用 (Step 2.5 验证通过)
    inst._tap_search_results_groups_filter.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
