"""P2.X-3 (2026-04-30): Step 3/4 防 Takeshi 误点回归测试.

历史 bug (real device 2026-04-30 task c20c41e7):
  Step 3 _tap_search_results_groups_filter 失败 → 降级 smart_tap("Groups
  tab or filter") 也没效 → 但 enter_group 没拦住 → Step 4 在 All-tab 跑 →
  _tap_first_search_result_group 3 路严格匹配 group_name='潮味' 全 miss →
  fallback smart_tap("First matching group") 被 AutoSelector 训练成"点
  列表第一项" → 点中同名人物 Takeshi Yoshida 的 profile → 双断言救场
  返 False, outcome=enter_group_failed.

修复后必须:
  (A) Step 4 移除 smart_tap("First matching group") fallback —— 严格 3
      路 miss 即返 False, 绝不"瞎点首位".
  (B) Step 3 后增加 hierarchy 验证 —— 若 chip 没真的切成 Groups (页面无
      group-typed markers), 立即返 False, 不进 Step 4.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ────────── (A) Step 4 严格匹配失败时绝不调 smart_tap("First matching group") ──────────

def test_step4_no_smart_tap_fallback_when_strict_match_fails():
    """
    场景: Step 1-3 全成功, 来到 Groups-filtered 结果页, 但页面上没有
    text=group_name 的 clickable row (例如 group_name 拼写错 / 群已删).
    修复后: 严格 3 路 miss → 直接返 False, 不调 smart_tap("First matching
    group") (历史漏洞 — 会误点列表首位人物).
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()
    # Step 1.5 hierarchy 验证: 真搜索页
    mock_d.dump_hierarchy.return_value = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '<node text="members"/>'  # Step 3 验证用
        '<node text="Public group"/>'
        '</hierarchy>'
    )
    inst._u2.return_value = mock_d

    # Step 1 成功
    inst._tap_search_bar_preferred.return_value = True
    # Step 3 切 Groups 成功
    inst._tap_search_results_groups_filter.return_value = True
    # Step 4 严格匹配全失败
    inst._tap_first_search_result_group.return_value = False

    # smart_tap mock — 关键: 必须验证 "First matching group" 不被调用
    inst.smart_tap.return_value = False

    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    result = fb_mod.FacebookAutomation.enter_group(
        inst, "潮味", device_id="dev1"
    )

    assert result is False, "Step 4 严格 miss 必须返 False"

    # 关键断言: smart_tap 没被以 'First matching group' 调用过
    smart_tap_intents = [
        c.args[0] if c.args else c.kwargs.get("intent")
        for c in inst.smart_tap.call_args_list
    ]
    assert "First matching group" not in smart_tap_intents, (
        f"Step 4 不应再 fallback smart_tap('First matching group'), "
        f"但发生了: smart_tap 调用列表 = {smart_tap_intents}"
    )


# ────────── (B) Step 3 切 filter 后必须验证页面真的切到了 Groups ──────────

def test_step3_5_returns_false_when_chip_tap_didnt_switch_page():
    """
    场景 (P2.X-3 v2): Step 3 硬编码 _tap_search_results_groups_filter 声称
    点中了 chip (returned True), 但实际上 FB 把 chip-tap 误识别 / chip 是
    overlap 状态 / 点中了下方人物 → 页面跳到 profile, 不在 Groups 结果页.
    Step 3.5 后置 hierarchy 验证发现无 group markers → return False.
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()

    # Step 1.5: 搜索页 ✅
    # Step 2.5: All-tab 结果页 (无 profile signature, 通过)
    # Step 3.5 后置: 仍是 All-tab (chip-tap 没切到 Groups), 无 group markers
    search_page_xml = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '<node text="Recent"/>'
        '</hierarchy>'
    )
    all_tab_results_xml = (
        '<hierarchy>'
        '<node text="全部"/>'
        '<node text="Youngjo Song"/>'
        '<node text="128 friends"/>'
        '<node text="People you may know"/>'
        '</hierarchy>'
    )
    mock_d.dump_hierarchy.side_effect = [
        search_page_xml,        # Step 1.5
        all_tab_results_xml,    # Step 2.5 (新增, 无 profile markers → 通过)
        all_tab_results_xml,    # Step 3.5 (无 group markers → 拦下)
        all_tab_results_xml,    # 兜底
    ]
    inst._u2.return_value = mock_d

    inst._tap_search_bar_preferred.return_value = True
    # 关键: 硬编码 _tap_search_results_groups_filter "点中" (返 True), 但实际
    # FB 把 tap 路由到了首位人物 row → 进了 profile.
    inst._tap_search_results_groups_filter.return_value = True
    inst.smart_tap.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    with patch("src.host.task_forensics.capture_immediate") as mock_capture:
        result = fb_mod.FacebookAutomation.enter_group(
            inst, "潮味", device_id="dev1"
        )

    assert result is False, "Step 3.5 后置验证未通过必须返 False"

    # Step 4 不应被调用 (Step 3.5 验证拦住了)
    inst._tap_first_search_result_group.assert_not_called()

    # capture_immediate 留证 step_name
    assert mock_capture.called, "Step 3.5 验证失败必须 capture_immediate"
    step_names = [c.kwargs.get("step_name") for c in mock_capture.call_args_list]
    assert "enter_group_groups_filter_not_applied" in step_names, (
        f"应有 step_name='enter_group_groups_filter_not_applied', got {step_names}"
    )


def test_step3_passes_when_groups_markers_present():
    """正向: Step 3 后页面含 'members' / 'group' 等 markers → 视为切到 Groups, 继续 Step 4."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()

    search_page_xml = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '</hierarchy>'
    )
    # P2.X-4 (2026-04-30): 新 strict 校验要求 *双闸* ——
    # 需同时含♥2 个 chip text 完整匹配 (results 页) +
    # 完整短语 group marker (Public group / 公开小组 等) 。
    groups_filtered_xml = (
        '<hierarchy>'
        '<node text="All"/>'           # chip 1 (results 页验证)
        '<node text="Groups"/>'        # chip 2
        '<node text="People"/>'        # chip 3
        '<node text="潮味"/>'
        '<node text="3.2K members"/>'  # 补充群组 marker (可选)
        '<node text="Public group"/>' # group marker 完整短语
        '</hierarchy>'
    )
    mock_d.dump_hierarchy.side_effect = [
        search_page_xml,
        groups_filtered_xml,
        groups_filtered_xml,
    ]
    inst._u2.return_value = mock_d

    inst._tap_search_bar_preferred.return_value = True
    inst._tap_search_results_groups_filter.return_value = True
    inst._tap_first_search_result_group.return_value = False  # Step 4 让它 miss 让流程在此退出
    inst.smart_tap.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    result = fb_mod.FacebookAutomation.enter_group(
        inst, "潮味", device_id="dev1"
    )

    # Step 4 应被调用 (Step 3 验证通过了)
    inst._tap_first_search_result_group.assert_called_once()
    # 因 Step 4 让它 miss, 最终仍 False (但走了正确路径)
    assert result is False


def test_step3_chinese_markers_recognized():
    """中文 FB: '成员' / '公开小组' 也应被识别为群组 markers."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()

    search_xml = '<hierarchy><node class="android.widget.EditText"/></hierarchy>'
    cn_groups_xml = (
        '<hierarchy>'
        '<node text="全部"/>'         # chip 1 (zh-Hans results)
        '<node text="小组"/>'         # chip 2
        '<node text="用户"/>'         # chip 3
        '<node text="潮味"/>'
        '<node text="3.2万 成员"/>'
        '<node text="公开小组"/>'   # group marker 完整短语
        '</hierarchy>'
    )
    mock_d.dump_hierarchy.side_effect = [search_xml, cn_groups_xml, cn_groups_xml]
    inst._u2.return_value = mock_d

    inst._tap_search_bar_preferred.return_value = True
    inst._tap_search_results_groups_filter.return_value = True
    inst._tap_first_search_result_group.return_value = False
    inst.smart_tap.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    fb_mod.FacebookAutomation.enter_group(inst, "潮味", device_id="dev1")
    # 中文 markers 也应让 Step 3 验证通过 → Step 4 被调用
    inst._tap_first_search_result_group.assert_called_once()


# ────────── (C) Step 3 硬编码 chip miss 时绝不调 smart_tap('Groups tab or filter') ──────────

def test_step3_no_smart_tap_fallback_when_chip_not_found():
    """
    场景: Step 1-2 成功, Step 3 硬编码 _tap_search_results_groups_filter 找不到
    chip (例如 chip 文案变了或被 lazy-render).
    历史 bug (real device 2026-04-30 task f5f2941a):
      此时 fallback smart_tap('Groups tab or filter') → AutoSelector 把它
      学成"点搜索结果首位" → 误点首位人物 (Youngjo Song) → 进 profile.
      Step 3.5 后置验证虽能拦下, 但设备已经被错误带到 profile 页, 浪费一轮.
    修复后: 硬编码 miss 即 return False, 不再允许 smart_tap 介入 Step 3.
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._did.return_value = "dev1"
    mock_d = MagicMock()
    # Step 1.5 hierarchy 验证: 真搜索页 (含 EditText, 无 What's on your mind)
    mock_d.dump_hierarchy.return_value = (
        '<hierarchy>'
        '<node text="Search Facebook" class="android.widget.EditText"/>'
        '<node text="Recent"/>'
        '</hierarchy>'
    )
    inst._u2.return_value = mock_d

    inst._tap_search_bar_preferred.return_value = True
    inst._tap_search_results_groups_filter.return_value = False  # chip miss
    inst.smart_tap.return_value = False
    inst.guarded.return_value.__enter__ = lambda *a: None
    inst.guarded.return_value.__exit__ = lambda *a: None
    inst.hb = MagicMock()

    with patch("src.host.task_forensics.capture_immediate") as mock_capture:
        result = fb_mod.FacebookAutomation.enter_group(
            inst, "潮味", device_id="dev1"
        )

    assert result is False, "Step 3 硬编码 miss 必须直接 return False"

    # 关键断言: smart_tap('Groups tab or filter') 不应被调用
    smart_tap_intents = [
        c.args[0] if c.args else c.kwargs.get("intent")
        for c in inst.smart_tap.call_args_list
    ]
    assert "Groups tab or filter" not in smart_tap_intents, (
        f"Step 3 不应再 fallback smart_tap('Groups tab or filter'), "
        f"但发生了: {smart_tap_intents}"
    )

    # Step 4 不应被调用
    inst._tap_first_search_result_group.assert_not_called()

    # capture_immediate 留证用更精确的 step_name
    step_names = [c.kwargs.get("step_name") for c in mock_capture.call_args_list]
    assert "enter_group_groups_filter_chip_not_found" in step_names, (
        f"应有 step_name='enter_group_groups_filter_chip_not_found', got {step_names}"
    )


# ────────── (D) chip selector 中文版 — text 空 + content-desc 部分匹配 ──────────

def test_chip_selector_matches_chinese_fb_via_description_contains():
    """
    真机证据 (debug_dump.xml 2026-04-30):
      中文版 FB chip 实际属性:
        class="android.widget.Button"
        text=""
        content-desc="小组个搜索结果, 第3项，共7项"
        clickable="true"
      历史 4 路 selector ('Groups'/'GROUPS'/'群组'/'群組'/'グループ'/'Gruppi')
      用 text= 全等 + description= 全等都 miss → enter_group 失败.
      修复后必须支持 '小组' + descriptionContains= *完整短语* 匹配
      ('<lang_word>个搜索结果' 后缀, 而非裸子串 '小组' — 后者是 caefd0e0 FP 根因).
    """
    from src.app_automation.facebook import FacebookAutomation

    inst = MagicMock(spec=FacebookAutomation)
    inst._FB_SEARCH_GROUPS_FILTER_TEXTS = FacebookAutomation._FB_SEARCH_GROUPS_FILTER_TEXTS
    inst._FB_SEARCH_GROUPS_CHIP_DESC_SUFFIXES = (
        FacebookAutomation._FB_SEARCH_GROUPS_CHIP_DESC_SUFFIXES
    )
    inst._el_center.return_value = (300, 200)
    inst.hb = MagicMock()

    # 真机 chip content-desc 实际值 (debug_dump.xml 2026-04-30)
    REAL_CHIP_DESC = "小组个搜索结果, 第3项，共7项"

    mock_d = MagicMock()
    # 模拟 d(text=...) / d(descriptionContains=...) 行为 — 与真机 selector 语义一致:
    # descContains 命中条件 = needle 是真实 content-desc 的子串
    def d_call(**kwargs):
        sel = MagicMock()
        if kwargs.get("text") and kwargs.get("clickable"):
            sel.exists.return_value = False  # 中文版 chip text 为空
        elif (
            kwargs.get("descriptionContains")
            and kwargs.get("clickable")
            and kwargs["descriptionContains"] in REAL_CHIP_DESC
        ):
            sel.exists.return_value = True
        else:
            sel.exists.return_value = False
        return sel
    mock_d.side_effect = d_call

    result = FacebookAutomation._tap_search_results_groups_filter(
        inst, mock_d, "dev1"
    )
    assert result is True, "中文 FB chip 应通过 descriptionContains='小组' 匹配上"
    inst.hb.tap.assert_called_once()


def test_chip_selector_still_works_for_english_text_match():
    """正向: 英文版 FB chip text='Groups' 全等仍能匹配."""
    from src.app_automation.facebook import FacebookAutomation

    inst = MagicMock(spec=FacebookAutomation)
    inst._FB_SEARCH_GROUPS_FILTER_TEXTS = FacebookAutomation._FB_SEARCH_GROUPS_FILTER_TEXTS
    inst._el_center.return_value = (300, 200)
    inst.hb = MagicMock()

    mock_d = MagicMock()
    def d_call(**kwargs):
        sel = MagicMock()
        if kwargs.get("text") == "Groups" and kwargs.get("clickable"):
            sel.exists.return_value = True
        else:
            sel.exists.return_value = False
        return sel
    mock_d.side_effect = d_call

    result = FacebookAutomation._tap_search_results_groups_filter(
        inst, mock_d, "dev1"
    )
    assert result is True


def test_chip_selector_returns_false_when_no_chip_at_all():
    """所有语种全 miss → return False, 让 enter_group 上层 capture chip_not_found."""
    from src.app_automation.facebook import FacebookAutomation

    inst = MagicMock(spec=FacebookAutomation)
    inst._FB_SEARCH_GROUPS_FILTER_TEXTS = FacebookAutomation._FB_SEARCH_GROUPS_FILTER_TEXTS
    inst._el_center.return_value = (300, 200)
    inst.hb = MagicMock()

    mock_d = MagicMock()
    def d_call(**kwargs):
        sel = MagicMock()
        sel.exists.return_value = False
        return sel
    mock_d.side_effect = d_call

    result = FacebookAutomation._tap_search_results_groups_filter(
        inst, mock_d, "dev1"
    )
    assert result is False
    inst.hb.tap.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
