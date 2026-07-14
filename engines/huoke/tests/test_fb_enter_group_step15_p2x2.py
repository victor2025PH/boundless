"""P2.X-2 (2026-04-30): enter_group Step 1.5 硬断言回归测试.

历史 bug: Step 1 三层 fallback 全失败时仍 silent 继续, type_text 在 Feed 的
"What's on your mind?" 输入框输入 group_name → 进入发帖编辑器 → 后续步骤全错位。

修复后必须做到:
  - Step 1 后未在搜索页 → 立即 return False
  - 不调用 type_text / press_enter / step 3-5
  - capture_immediate 留证 (step_name=enter_group_search_page_not_opened)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ────────── hierarchy_looks_like_fb_search_surface 契约 ──────────

def test_hierarchy_helper_rejects_feed_with_composer():
    """Feed 页含 'What's on your mind?' → 不是搜索页"""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_surface,
    )
    feed_xml = (
        "<hierarchy>"
        "<node text='Home, tab 1 of 6'/>"
        "<node text=\"What's on your mind?\"/>"
        '<node class="android.widget.EditText"/>'  # 即使有 EditText 也应拒绝
        "</hierarchy>"
    )
    assert hierarchy_looks_like_fb_search_surface(feed_xml) is False


def test_hierarchy_helper_accepts_real_search_page():
    """真搜索页: 没有 What's on your mind, 但有 EditText"""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_surface,
    )
    search_xml = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '<node text="Recent searches"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_search_surface(search_xml) is True


def test_hierarchy_helper_rejects_profile_page():
    """Profile 页没有 EditText → 不是搜索页"""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_surface,
    )
    profile_xml = (
        "<hierarchy>"
        "<node text='Yasushi Kaneko'/>"
        "<node text='411 friends'/>"
        "<node text='Add Friend'/>"
        "</hierarchy>"
    )
    assert hierarchy_looks_like_fb_search_surface(profile_xml) is False


def test_hierarchy_helper_rejects_empty():
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_surface,
    )
    assert hierarchy_looks_like_fb_search_surface("") is False
    assert hierarchy_looks_like_fb_search_surface(None) is False


# ────────── enter_group 修复路径回归 ──────────

def test_enter_group_returns_false_when_step1_failed_silently():
    """模拟 Step 1 三层 fallback 全失败但代码继续, 新加的 Step 1.5 断言必须拦住。"""
    from src.app_automation import facebook as fb_mod

    # mock FacebookAutomation 实例所需的 attributes
    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    # _did 返回输入 device_id, _u2 返回 mock device
    inst._did.return_value = "dev1"
    mock_d = MagicMock()
    # dump_hierarchy 返回 Feed 页面 XML (含 What's on your mind, 不是搜索页)
    mock_d.dump_hierarchy.return_value = (
        "<hierarchy>"
        "<node text=\"What's on your mind?\"/>"
        "<node text='Active stories'/>"
        "</hierarchy>"
    )
    inst._u2.return_value = mock_d

    # Step 1 三层 fallback 全失败
    inst._tap_search_bar_preferred.return_value = False
    inst.smart_tap.return_value = False
    inst._fallback_search_tap.return_value = None  # 无返回值, 历史 bug 来源

    # guarded 是 contextmanager, mock 成 no-op
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None

    # hb mock — 用来检测 type_text 是否被错误调用
    inst.hb = MagicMock()

    # 执行 enter_group (用 unbound method + inst 作为 self)
    with patch("src.host.task_forensics.capture_immediate") as mock_capture:
        result = fb_mod.FacebookAutomation.enter_group(
            inst, "潮味", device_id="dev1"
        )

    # 关键断言 1: enter_group 返回 False
    assert result is False, "Step 1.5 硬断言必须返 False, 不能 silent 通过"

    # 关键断言 2: type_text 没被调用 (核心修复 — 之前会在 Feed 输入框误输)
    inst.hb.type_text.assert_not_called()

    # 关键断言 3: press("enter") 没被调用
    enter_calls = [c for c in mock_d.press.call_args_list
                   if c.args and c.args[0] == "enter"]
    assert len(enter_calls) == 0, "Step 1.5 失败后不应调 press(enter)"

    # 关键断言 4: capture_immediate 被调用 (留证)
    assert mock_capture.called, "应该 capture_immediate 留证"
    # 验证 step_name 是新的精确标识
    call_kwargs = mock_capture.call_args.kwargs
    assert call_kwargs.get("step_name") == "enter_group_search_page_not_opened"


def test_enter_group_proceeds_when_step1_succeeded():
    """正向路径: 真在搜索页时, 应该继续走 Step 2~5, 不被新断言误拦"""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()
    # dump_hierarchy 返回真搜索页 XML
    mock_d.dump_hierarchy.return_value = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '<node text="Recent"/>'
        '</hierarchy>'
    )
    inst._u2.return_value = mock_d
    inst._tap_search_bar_preferred.return_value = True   # Step 1 成功
    # precheck 必须返 (False,...) 否则 enter_group 在 line 7141 短路 return True,
    # 根本不进 Step 1.5 / Step 2.
    inst._assert_on_specific_group_page = MagicMock(return_value=(False, "not_on_group"))
    # _type_fb_search_query 是新代码包 type_text 的外层方法 (Step 2 入口)
    inst._type_fb_search_query = MagicMock(return_value=True)
    # _submit_fb_search_with_verify 直接成功, 跳过 self-heal
    inst._submit_fb_search_with_verify = MagicMock(return_value=True)

    # 后续步骤 mock 失败, 让 enter_group 在 Step 3 (Groups filter) 退出
    inst._tap_search_results_groups_filter.return_value = False
    inst.smart_tap.return_value = False
    inst._tap_first_search_result_group.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    result = fb_mod.FacebookAutomation.enter_group(
        inst, "ママ友サークル", device_id="dev1"
    )

    # 应该走到 Step 2 (_type_fb_search_query 被调用 — 新代码把 type_text + 多路提交
    # 包到这层, 不再直接调 hb.type_text 在 enter_group 主线里).
    inst._type_fb_search_query.assert_called_once_with(mock_d, "ママ友サークル", "dev1")
    # 因为 Step 3 失败 (Groups filter chip 找不到), 最终仍返 False
    # (但走了正确的失败路径, 不是被 Step 1.5 误拦)
    assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
