"""P2 Phase-2 (2026-04-30): 五路提交 v2 + person detection + self-heal 回归测试.

新增功能:
  1. send_action_go 第二路 (IME_ACTION_GO fallback for MIUI)
  2. tap_typeahead_group: typeahead 里直接 tap 群组建议行
  3. pre-ENTER person 检测: typeahead 含 person-only 建议时跳过 keyevent_enter
  4. enter_group 自愈循环: 五路失败后 BACK + 重输 + 重试一次
  5. typeahead_has_person_but_no_group_suggestions helper 本身的行为
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# 一. typeahead_has_person_but_no_group_suggestions helper
# ═══════════════════════════════════════════════════════════════════════════════

def test_person_detection_returns_false_for_empty():
    from src.app_automation.fb_search_markers import (
        typeahead_has_person_but_no_group_suggestions,
    )
    assert typeahead_has_person_but_no_group_suggestions("") is False
    assert typeahead_has_person_but_no_group_suggestions(None) is False  # type: ignore[arg-type]


def test_person_detection_returns_false_for_group_only():
    """只有群组建议行 (members) — ENTER 安全, 返 False."""
    from src.app_automation.fb_search_markers import (
        typeahead_has_person_but_no_group_suggestions,
    )
    group_typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="潮味"/>'
        '<node content-desc="潮味爱好者, 1,234 members" clickable="true"/>'
        '</hierarchy>'
    )
    assert typeahead_has_person_but_no_group_suggestions(group_typeahead_xml) is False


def test_person_detection_returns_true_for_person_only():
    """只有人物建议行 (mutual friends / Add Friend) — ENTER 不安全, 返 True."""
    from src.app_automation.fb_search_markers import (
        typeahead_has_person_but_no_group_suggestions,
    )
    person_typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="桐島"/>'
        '<node content-desc="桐島青大, 4 mutual friends" clickable="true"/>'
        '<node content-desc="桐島真奈美, Add friend" clickable="true"/>'
        '</hierarchy>'
    )
    assert typeahead_has_person_but_no_group_suggestions(person_typeahead_xml) is True


def test_person_detection_returns_false_for_mixed_person_and_group():
    """同时存在 person 和 group 建议 — 保守策略, 允许 ENTER (含 group = 安全), 返 False."""
    from src.app_automation.fb_search_markers import (
        typeahead_has_person_but_no_group_suggestions,
    )
    mixed_xml = (
        '<hierarchy>'
        '<node content-desc="Takeshi Yoshida, 12 mutual friends" clickable="true"/>'
        '<node content-desc="潮味グループ, 567 members" clickable="true"/>'
        '</hierarchy>'
    )
    assert typeahead_has_person_but_no_group_suggestions(mixed_xml) is False


def test_person_detection_zh_hans_friend_marker():
    """中文 '好友' 标记 (zh-Hans FB) → person-only 时返 True."""
    from src.app_automation.fb_search_markers import (
        typeahead_has_person_but_no_group_suggestions,
    )
    zh_person_xml = (
        '<hierarchy>'
        '<node content-desc="张伟, 好友" clickable="true"/>'
        '</hierarchy>'
    )
    assert typeahead_has_person_but_no_group_suggestions(zh_person_xml) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 二. send_action_go 路径 (第二路)
# ═══════════════════════════════════════════════════════════════════════════════

def test_send_action_go_succeeds_returns_true():
    """send_action_search silent no-op, send_action_go 把页面带到 results."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_SUBMIT_PATHS = fb_mod.FacebookAutomation._FB_SEARCH_SUBMIT_PATHS
    inst._FB_PROFILE_PAGE_MARKERS = fb_mod.FacebookAutomation._FB_PROFILE_PAGE_MARKERS
    inst._adb = MagicMock()
    inst._tap_typeahead_group_row = MagicMock(return_value=False)

    typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="潮味"/>'
        '<node text="潮味决"/>'
        '</hierarchy>'
    )
    results_xml = (
        '<hierarchy>'
        '<node text="All"/>'
        '<node text="Groups"/>'
        '<node text="People"/>'
        '</hierarchy>'
    )
    mock_d = MagicMock()
    # path1 send_action_search → typeahead; path2 send_action_go → results
    mock_d.dump_hierarchy.side_effect = [typeahead_xml, results_xml]

    result = fb_mod.FacebookAutomation._submit_fb_search_with_verify(
        inst, mock_d, "dev1", "潮味",
    )

    assert result is True
    # send_action 必须被调用两次: "search" 然后 "go"
    calls = [c.args[0] for c in mock_d.send_action.call_args_list]
    assert calls == ["search", "go"], (
        f"期望 [search, go] 两次 send_action 调用, 实际: {calls}"
    )
    # 不应发出任何 keyevent (在 path2 就成功了)
    inst._adb.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 三. tap_typeahead_group 路径 (第三路)
# ═══════════════════════════════════════════════════════════════════════════════

def test_tap_typeahead_group_path_succeeds():
    """tap_typeahead_group 成功 tap 到群组建议 → 页面进入 results."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_SUBMIT_PATHS = fb_mod.FacebookAutomation._FB_SEARCH_SUBMIT_PATHS
    inst._FB_PROFILE_PAGE_MARKERS = fb_mod.FacebookAutomation._FB_PROFILE_PAGE_MARKERS
    inst._adb = MagicMock()
    # path3 tap_typeahead_group: 模拟成功找到群组建议行并 tap
    inst._tap_typeahead_group_row = MagicMock(return_value=True)

    typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="潮味"/>'
        '<node text="潮味决"/>'
        '</hierarchy>'
    )
    results_xml = (
        '<hierarchy>'
        '<node text="全部"/>'
        '<node text="小组"/>'
        '<node text="用户"/>'
        '</hierarchy>'
    )
    mock_d = MagicMock()
    # path1 → typeahead, path2 → typeahead, path3 → tap succeeds → results
    mock_d.dump_hierarchy.side_effect = [typeahead_xml, typeahead_xml, results_xml]

    result = fb_mod.FacebookAutomation._submit_fb_search_with_verify(
        inst, mock_d, "dev1", "潮味",
    )

    assert result is True
    inst._tap_typeahead_group_row.assert_called_once_with(mock_d, "潮味")
    # 不应发出任何 keyevent (path3 成功)
    inst._adb.assert_not_called()


def test_tap_typeahead_group_row_unit_no_group_suggestion():
    """``_tap_typeahead_group_row``: typeahead 没有群组建议行 → 返 False."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst.hb = MagicMock()
    inst._el_center = MagicMock(return_value=(100, 200))

    mock_d = MagicMock()
    # d(descriptionContains=group_name) 返回没有 exists 的 mock
    no_match_el = MagicMock()
    no_match_el.exists.return_value = False
    mock_d.return_value = no_match_el

    result = fb_mod.FacebookAutomation._tap_typeahead_group_row(
        inst, mock_d, "潮味",
    )
    assert result is False
    inst.hb.tap.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 四. pre-ENTER person 检测 → 跳过 keyevent_enter
# ═══════════════════════════════════════════════════════════════════════════════

def test_skip_keyevent_enter_when_person_only_typeahead():
    """typeahead 含 person-only 建议 → 跳过 keyevent_enter, 直接用 keyevent_search."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_SUBMIT_PATHS = fb_mod.FacebookAutomation._FB_SEARCH_SUBMIT_PATHS
    inst._FB_PROFILE_PAGE_MARKERS = fb_mod.FacebookAutomation._FB_PROFILE_PAGE_MARKERS
    inst._adb = MagicMock()
    inst._tap_typeahead_group_row = MagicMock(return_value=False)

    # typeahead 里只有人物建议, 无群组 → person-only = True → skip ENTER
    # 注意: 不能用 'Add Friend' / 'Add friend' — 它们在 _FB_PROFILE_PAGE_MARKERS 里,
    # helper 会在 dump 后直接返 False (profile 误入), 而不是进行 per-ENTER 跳过。
    # 用 'mutual friends' 只运发 person detection 而不运发 profile abort.
    person_only_typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="Taka"/>'
        '<node content-desc="Takeshi Yoshida, 8 mutual friends" clickable="true"/>'
        '<node content-desc="Takashi Morita, 3 mutual friends" clickable="true"/>'
        '</hierarchy>'
    )
    results_xml = (
        '<hierarchy>'
        '<node text="All"/>'
        '<node text="Groups"/>'
        '<node text="People"/>'
        '</hierarchy>'
    )

    mock_d = MagicMock()
    # path1 send_action_search → person typeahead
    # path2 send_action_go → person typeahead
    # path3 tap_typeahead_group → False → skip (no dump)
    # path4 keyevent_enter → person-only detected → skip (no dump)
    # path5 keyevent_search → results
    mock_d.dump_hierarchy.side_effect = [
        person_only_typeahead_xml,   # after send_action_search
        person_only_typeahead_xml,   # after send_action_go
        results_xml,                 # after keyevent_search
    ]

    result = fb_mod.FacebookAutomation._submit_fb_search_with_verify(
        inst, mock_d, "dev1", "Taka",
    )

    assert result is True
    keyevent_calls = [
        c.args[0] if c.args else c.kwargs.get("cmd", "")
        for c in inst._adb.call_args_list
    ]
    # keyevent 66 (ENTER) 绝对不能被调用 (person-only 跳过)
    assert not any("keyevent 66" in c for c in keyevent_calls), (
        f"person-only typeahead 时 ENTER 必须被跳过, 实际 _adb 调用: {keyevent_calls}"
    )
    # keyevent 84 (SEARCH) 必须被调用 (兜底路径)
    assert any("keyevent 84" in c for c in keyevent_calls), (
        f"person-only 时应走 keyevent_search (84) 兜底, 实际: {keyevent_calls}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 五. enter_group 自愈循环
# ═══════════════════════════════════════════════════════════════════════════════

def _make_enter_group_inst(mock_d):
    """构建 enter_group 所需的最小 FacebookAutomation mock (pattern 参考 p2x3v3).
    
    enter_group 通过 self._did() + self._u2() 获取 d, 而不是参数传入.
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    inst._u2.return_value = mock_d
    inst.hb = MagicMock()
    inst._adb = MagicMock()
    inst._tap_search_bar_preferred = MagicMock(return_value=True)
    inst._submit_fb_search_with_verify = MagicMock(return_value=True)
    inst._tap_search_results_groups_filter = MagicMock(return_value=True)
    inst._tap_first_search_result_group = MagicMock(return_value=True)
    # precheck 默认设为 (False, "not_on_group") — 否则 enter_group 在
    # current-page precheck 阶段直接 return True, 根本不进 Step 1+ 主流程,
    # self-heal 路径无法触发. self-heal 测试要走完整路径, 所以默认让 precheck 失败.
    inst._assert_on_specific_group_page = MagicMock(return_value=(False, "not_on_group"))
    inst.smart_tap = MagicMock(return_value=False)
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    return inst


def test_selfheal_triggered_then_succeeds():
    """五路失败后 self-heal: BACK → 仍在搜索页 → 重输 → 第二次提交成功 → 整体返 True."""
    from src.app_automation import facebook as fb_mod

    # dump 调用序列:
    #   (1) Step 1.5 搜索页确认    → search_surface_xml
    #   (2) self-heal 检查 BACK 后   → search_surface_xml (仍在搜索页)
    search_surface_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text=""/>'
        '<node resource-id="com.facebook.katana:id/search_box_input"/>'
        '</hierarchy>'
    )

    # Step 3.5 需要看到一个 groups-filtered 结果页 XML
    groups_filtered_xml = (
        '<hierarchy>'
        '<node text="All"/>'
        '<node text="Groups"/>'
        '<node text="People"/>'
        '<node text="潮味爱好者"/>'
        '<node text="Public group · 1,234 members"/>'
        '</hierarchy>'
    )

    mock_d = MagicMock()
    # dump 调用序列:
    #   call 1 (Step 1.5):     search_surface_xml  — 确认在搜索页
    #   call 2 (self-heal):    search_surface_xml  — BACK 后仍在搜索页
    #   call 3 (Step 3.5):     groups_filtered_xml — 验证 Groups-filtered 结果页
    mock_d.dump_hierarchy.side_effect = [
        search_surface_xml,
        search_surface_xml,
        groups_filtered_xml,
    ]
    # d(className="android.widget.EditText") 返回一个 exists=True 的 mock
    et_mock = MagicMock()
    et_mock.exists.return_value = True
    mock_d.return_value = et_mock

    inst = _make_enter_group_inst(mock_d)
    # 首次 _submit 失败, 第二次成功
    inst._submit_fb_search_with_verify = MagicMock(side_effect=[False, True])
    # _assert_on_specific_group_page 被 enter_group 调 2 次:
    #   第 1 次 (line 7141 precheck): 必须 (False,...) 否则 enter_group 短路 return True
    #   第 2 次 (line 7344 Step 5): 必须 (True,...) 否则最后断言失败 return False
    inst._assert_on_specific_group_page = MagicMock(side_effect=[
        (False, "precheck_not_on_group"),
        (True, "step5_ok"),
    ])

    result = fb_mod.FacebookAutomation.enter_group(
        inst, "潮味", device_id="dev1",
    )

    assert result is True
    # 确认 _submit_fb_search_with_verify 被调用了两次 (首次失败 + self-heal 重试)
    assert inst._submit_fb_search_with_verify.call_count == 2, (
        f"self-heal 应触发第二次提交, 实际调用次数: "
        f"{inst._submit_fb_search_with_verify.call_count}"
    )
    # 确认 BACK (keyevent 4) 被发出
    adb_args = [
        c.args[0] if c.args else c.kwargs.get("cmd", "")
        for c in inst._adb.call_args_list
    ]
    assert any("keyevent 4" in a for a in adb_args), (
        f"self-heal 必须先发 BACK (keyevent 4), 实际 _adb 调用: {adb_args}"
    )


def test_selfheal_aborts_when_back_exits_search_surface():
    """self-heal: BACK 后已离开搜索页 → 自愈放弃 → 整体返 False."""
    from src.app_automation import facebook as fb_mod

    search_surface_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text=""/>'
        '<node resource-id="com.facebook.katana:id/search_box_input"/>'
        '</hierarchy>'
    )
    home_xml = (
        '<hierarchy>'
        '<node content-desc="Home, tab 1 of 6"/>'
        '<node text="What\'s on your mind?"/>'
        '</hierarchy>'
    )

    mock_d = MagicMock()
    # dump 调用序列:
    #   (1) Step 1.5 搜索页确认 → search_surface_xml
    #   (2) self-heal 检查 BACK 后 → home_xml (BACK 已回到首页)
    mock_d.dump_hierarchy.side_effect = [search_surface_xml, home_xml]

    inst = _make_enter_group_inst(mock_d)
    # 五路全失败
    inst._submit_fb_search_with_verify = MagicMock(return_value=False)

    result = fb_mod.FacebookAutomation.enter_group(
        inst, "潮味", device_id="dev1",
    )

    assert result is False
    # BACK 移开搜索页后不应再发起第二次提交
    assert inst._submit_fb_search_with_verify.call_count == 1, (
        "BACK 移开搜索页后不应再发起第二次提交"
    )
