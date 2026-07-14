"""P2.X-4 (2026-04-30): caefd0e0 bug 回归 — typeahead overlay 误判 + silent
``send_action`` no-op 的纵深修复.

历史 bug (real device task caefd0e0):
  Step 1 真进搜索页 ✅
  Step 2 ``d.send_action("search")`` 在该机 MIUI 自带拼音输入法上 silent no-op
        (不抛异常但 ACTION_SEARCH 没派发) → 屏幕停留在 typeahead overlay
  Step 3 旧版 ``descriptionContains='小组'`` 在 typeahead 联想项 desc 中误命中
        → ``_tap_search_results_groups_filter`` 假"成功" 返 True
  Step 3.5 旧版 markers ('members'|'小组'|'公开小组') 用子串包含, typeahead
        desc 含 ``'小组'`` 子串就放行
  Step 4 在 typeahead 上点 ``text='潮味'`` 联想项 → 仍是 typeahead
  Step 5 才捕获 ``name_present_but_no_group_tab`` 失败

修复后必须:
  (A) ``hierarchy_looks_like_fb_search_results_page`` 对 typeahead overlay 返 False
  (B) ``hierarchy_looks_like_fb_groups_filtered_results_page`` 仅在 results 页 +
      完整短语 group marker / ``\\d+ members`` 行模式时返 True
  (C) ``_tap_search_results_groups_filter`` 不再用裸 descContains '小组' (改要求
      "<lang_word>个搜索结果" 类后缀)
  (D) ``_submit_fb_search_with_verify`` 三路提交后真校验 results 页 / profile 页
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ────────── (A) results-page detector 不被 typeahead overlay 误判 ──────────

def test_results_page_helper_rejects_typeahead_overlay():
    """caefd0e0 真机抓到的 typeahead overlay XML — 必须返 False."""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_results_page,
        hierarchy_looks_like_fb_search_typeahead,
    )
    # 输入框还在 + 6 条联想行 (典型 typeahead overlay)
    typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="潮味"/>'
        '<node text="潮味决"/>'
        '<node text="潮味决 金門金城店"/>'
        '<node text="潮味决 湯滷專門店"/>'
        '<node text="潮味"/>'
        '<node text="潮味决 湯滷專門店善化"/>'
        '<node text="潮味时光"/>'
        # 关键: typeahead 联想行 desc 含 '小组' 子串 (历史 FP 来源)
        '<node content-desc="搜索建议: 小组建议, 第 1 项, 共 6 项"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_search_results_page(typeahead_xml) is False
    assert hierarchy_looks_like_fb_search_typeahead(typeahead_xml) is True


def test_results_page_helper_accepts_real_results_page():
    """搜索结果页: filter chip 行 [全部] [帖子] [用户] [小组] 至少 2 个 text 命中."""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_results_page,
    )
    results_xml = (
        '<hierarchy>'
        '<node text="全部"/>'
        '<node text="帖子"/>'
        '<node text="用户"/>'
        '<node text="小组"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_search_results_page(results_xml) is True


def test_results_page_helper_accepts_chip_content_desc():
    """新版中文 FB chip label 可能只在 content-desc, text 为空。"""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_results_page,
    )
    results_xml = (
        '<hierarchy>'
        '<node text="" content-desc="全部个搜索结果, 第1项，共5项"/>'
        '<node text="" content-desc="小组个搜索结果, 第3项，共5项"/>'
        '<node text="" content-desc="公共主页个搜索结果, 第5项，共5项"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_search_results_page(results_xml) is True


def test_results_page_helper_rejects_single_chip_match():
    """只有 1 个 chip text — 可能是 typeahead/profile/feed 巧合, 不算 results 页."""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_results_page,
    )
    only_one_chip_xml = (
        '<hierarchy>'
        '<node text="全部"/>'
        '<node text="Youngjo Song"/>'
        '<node text="128 friends"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_search_results_page(only_one_chip_xml) is False


def test_results_page_helper_substring_does_not_count():
    """typeahead desc 含 ``'用户'`` 子串不应触发 chip 匹配 (必须 ``text="用户"`` 完整)."""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_search_results_page,
    )
    desc_only_xml = (
        '<hierarchy>'
        '<node content-desc="用户搜索建议"/>'
        '<node content-desc="小组搜索建议"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_search_results_page(desc_only_xml) is False


# ────────── (B) groups-filtered detector 双闸 ──────────

def test_groups_filtered_helper_rejects_typeahead_with_substring():
    """typeahead overlay 即使含 '小组' 子串也必须被拒 (历史 caefd0e0 root cause)."""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_groups_filtered_results_page,
    )
    typeahead_with_substring = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="潮味"/>'
        '<node text="潮味决"/>'
        '<node content-desc="搜索小组建议: 潮味"/>'
        # 含子串 '小组' / '成员' 但没有完整短语 'Public group' / '<N> members'
        '<node text="小组成员推荐"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_groups_filtered_results_page(
        typeahead_with_substring
    ) is False


def test_groups_filtered_helper_accepts_real_groups_page():
    """真 Groups-filtered 结果页: chip 行 + 'Public group' 完整短语."""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_groups_filtered_results_page,
    )
    real_groups_results_xml = (
        '<hierarchy>'
        '<node text="All"/>'
        '<node text="Groups"/>'
        '<node text="People"/>'
        '<node text="潮味爱好者"/>'
        '<node text="Public group · 1,234 members"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_groups_filtered_results_page(
        real_groups_results_xml
    ) is True


def test_groups_filtered_helper_accepts_member_count_pattern():
    """``\\d+ members`` / ``\\d+ 名のメンバー`` 行模式也算 groups marker."""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_groups_filtered_results_page,
    )
    # 日文 FB groups-filtered 结果页
    ja_results_xml = (
        '<hierarchy>'
        '<node text="すべて"/>'
        '<node text="グループ"/>'
        '<node text="ユーザー"/>'
        '<node text="関西お笑いサークル"/>'
        '<node text="3,456 名のメンバー"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_groups_filtered_results_page(
        ja_results_xml
    ) is True


def test_groups_filtered_helper_accepts_chinese_wan_member_count():
    """真实中文 FB: ``公开 · 8.5 万位成员`` 也应算群组结果页。"""
    from src.app_automation.fb_search_markers import (
        hierarchy_looks_like_fb_groups_filtered_results_page,
    )
    zh_results_xml = (
        '<hierarchy>'
        '<node text="" content-desc="全部个搜索结果, 第1项，共5项"/>'
        '<node text="" content-desc="小组个搜索结果, 第3项，共5项"/>'
        '<node text="美味しいカレーの会 · 加入"/>'
        '<node text="公开 · 8.5 万位成员 · 30+ 篇帖子/天"/>'
        '</hierarchy>'
    )
    assert hierarchy_looks_like_fb_groups_filtered_results_page(
        zh_results_xml
    ) is True


def test_group_candidate_name_rejects_truncated_meta_rows():
    """搜索结果分行时 ``公开 · 9`` 这类元信息残片不能成为群名。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    assert fb._is_valid_group_candidate_name("我がペットの日常", "ペット") is True
    assert fb._is_valid_group_candidate_name("公开 · 9", "ペット") is False
    assert fb._is_valid_group_candidate_name("公开 · 9,258 位成员", "ペット") is False


def test_members_list_detector_rejects_group_info_preview():
    """群详情页的成员预览不能被误判成完整成员列表页。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    info_preview_xml = (
        '<hierarchy>'
        '<node text="成员"/>'
        '<node content-desc="美味しいカレーの会, 公开小组 · 8.5万位成员"/>'
        '<node text="松田壮史是管理员。Kagoshima Kiyoshi 和其他 12 位成员是版主。"/>'
        '</hierarchy>'
    )
    assert fb._looks_like_group_members_list_xml(info_preview_xml) is False


def test_members_list_detector_accepts_real_members_page():
    """真实成员页有搜索成员和成员分区等强特征。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    members_page_xml = (
        '<hierarchy>'
        '<node text="成员"/>'
        '<node text="搜索成员"/>'
        '<node text="新加入这个小组的用户和公共主页会显示在这里。"/>'
        '<node text="管理员和版主"/>'
        '<node text="田中恵一"/>'
        '<node text="添加"/>'
        '</hierarchy>'
    )
    assert fb._looks_like_group_members_list_xml(members_page_xml) is True


def test_members_desc_filter_rejects_group_member_count_metadata():
    """``公开小组 · 8.5万位成员`` 是群元信息, 不是 Members tab。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    assert fb._members_desc_looks_like_group_metadata(
        "美味しいカレーの会, 公开小组 · 8.5万位成员"
    ) is True
    assert fb._members_desc_looks_like_group_metadata("成员") is False


class _MissingSelector:
    def exists(self, timeout=0):
        return False


class _XmlOnlyDevice:
    def __init__(self, xml: str):
        self._xml = xml

    def __call__(self, **_selector):
        return _MissingSelector()

    def dump_hierarchy(self):
        return self._xml


def test_current_group_page_requires_join_detects_bottom_join_button():
    """未入群页面底部 ``加入小组`` 按钮应快速识别，避免继续找 Members tab。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    d = _XmlOnlyDevice(
        '<hierarchy>'
        '<node text="ペットの時間" bounds="[48,180][520,260]" />'
        '<node text="公开 · 7,383 位成员" bounds="[48,270][640,330]" />'
        '<node text="加入小组" bounds="[72,1220][648,1300]" />'
        '</hierarchy>'
    )
    assert fb._current_group_page_requires_join(d) is True


def test_current_group_page_requires_join_ignores_member_count_metadata():
    """成员数量元信息不能被误判为入群按钮。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    d = _XmlOnlyDevice(
        '<hierarchy>'
        '<node text="美味しいカレーの会" bounds="[48,180][520,260]" />'
        '<node text="公开小组 · 8.5万位成员" bounds="[48,270][640,330]" />'
        '<node text="成员" bounds="[88,460][160,520]" />'
        '</hierarchy>'
    )
    assert fb._current_group_page_requires_join(d) is False


def test_current_group_page_requires_join_ignores_search_results_join_buttons():
    """搜索结果页上的其它群 ``加入`` 不能当成当前群主页需入群。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    d = _XmlOnlyDevice(
        '<hierarchy>'
        '<node text="全部" bounds="[40,170][120,230]" />'
        '<node text="用户" bounds="[180,170][260,230]" />'
        '<node text="小组" bounds="[320,170][420,230]" />'
        '<node text="我がペットの日常 · 访问" bounds="[166,304][620,350]" />'
        '<node text="公开 · 9,259 位成员" bounds="[166,356][620,396]" />'
        '<node text="ペット等動物の飼い主 · 加入" bounds="[166,470][620,540]" />'
        '<node text="公开 · 1.2 万位成员" bounds="[166,550][620,590]" />'
        '</hierarchy>'
    )
    assert fb._current_group_page_requires_join(d) is False


def test_assert_on_specific_group_page_rejects_search_results_page():
    """搜索结果页含目标群名和“成员”元信息，也不能通过群主页自检。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    d = _XmlOnlyDevice(
        '<hierarchy>'
        '<node text="全部" bounds="[40,170][120,230]" />'
        '<node text="用户" bounds="[180,170][260,230]" />'
        '<node text="小组" bounds="[320,170][420,230]" />'
        '<node text="我がペットの日常 · 访问" bounds="[166,304][620,350]" />'
        '<node text="公开 · 9,259 位成员" bounds="[166,356][620,396]" />'
        '</hierarchy>'
    )
    ok, reason = fb._assert_on_specific_group_page(d, "我がペットの日常")
    assert ok is False
    assert reason == "still_on_search_results_page"


def test_join_button_matching_does_not_treat_joined_as_join_button():
    """``已加入`` 不能因为包含泛词 ``加入`` 再被当成 Join 按钮点击。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    assert fb._join_button_label_matches("加入小组", "加入小组") is True
    assert fb._join_button_label_matches("已加入", "加入") is False
    d = _XmlOnlyDevice(
        '<hierarchy>'
        '<node text="ペットの時間" bounds="[48,180][520,260]" />'
        '<node text="已加入" bounds="[24,650][352,730]" />'
        '</hierarchy>'
    )
    assert fb._current_group_page_requires_join(d) is False


def test_join_status_classifier_accepts_joined_bottom_sheet():
    """已加入按钮点开后的菜单包含退出/取关，也应归类为 joined。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="取关小组" bounds="[128,990][500,1070]" />'
        '<node text="管理通知" bounds="[128,1140][500,1220]" />'
        '<node text="退出小组" bounds="[128,1290][500,1370]" />'
        '</hierarchy>'
    )
    assert fb._classify_join_group_page(xml, "ペットの時間") == "joined"


def test_reaction_user_list_is_not_members_list():
    """帖子反应用户列表不能当成群成员列表。"""
    from src.app_automation.facebook import FacebookAutomation

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="留下心情的用户" bounds="[96,68][520,156]" />'
        '<node text="Kazuo Nagaoka" bounds="[144,280][520,340]" />'
        '<node text="小西正之" bounds="[144,400][520,460]" />'
        '</hierarchy>'
    )
    assert fb._looks_like_reaction_user_list_xml(xml) is True
    assert fb._looks_like_group_members_list_xml(xml) is False


def test_member_candidates_parse_button_rows_with_add_action():
    """新版 FB 成员页: 用户行是 Button content-desc, 姓名 ViewGroup 不可点击。"""
    from src.app_automation.facebook import FacebookAutomation
    from src.vision.screen_parser import XMLParser

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="成员" class="android.widget.TextView" bounds="[96,68][168,156]" />'
        '<node text="搜索成员" class="android.widget.EditText" bounds="[0,156][720,248]" />'
        '<node text="" content-desc="Kazuhiro Takamiya, 38,469分" '
        'class="android.widget.Button" clickable="true" bounds="[0,249][720,374]" />'
        '<node text="Kazuhiro Takamiya" content-desc="Kazuhiro Takamiya" '
        'class="android.view.ViewGroup" clickable="false" bounds="[168,256][520,296]" />'
        '<node text="加为好友" content-desc="加为好友" class="android.widget.Button" '
        'clickable="true" bounds="[544,262][696,334]" />'
        '<node text="" content-desc="清田, 0分" class="android.widget.Button" '
        'clickable="true" bounds="[0,526][720,678]" />'
        '<node text="查看所有成员" content-desc="查看所有成员" '
        'class="android.widget.Button" clickable="true" bounds="[24,718][696,798]" />'
        '<node text="这份名单包含小组中贡献积分排在前列的成员。" '
        'content-desc="这份名单包含小组中贡献积分排在前列的成员。" '
        'class="android.view.ViewGroup" clickable="false" bounds="[168,806][680,860]" />'
        '<node text="" content-desc="Jyoji Hozumi, 1 位共同好友：JinSeung Kim, 目前就职：Panasonic" '
        'class="android.widget.Button" clickable="true" bounds="[0,921][720,1170]" />'
        '<node text="加为好友" content-desc="加为好友" class="android.widget.Button" '
        'clickable="true" bounds="[544,1010][696,1082]" />'
        '</hierarchy>'
    )
    names = [
        m["name"]
        for m in fb._extract_group_member_candidates(XMLParser.parse(xml))
    ]
    assert names == ["Kazuhiro Takamiya", "Jyoji Hozumi"]


def test_member_candidates_require_add_friend_action():
    """没有同屏加好友动作时不抽取，避免把帖子控件/图片/按钮写成客户线索。"""
    from src.app_automation.facebook import FacebookAutomation
    from src.vision.screen_parser import XMLParser

    fb = FacebookAutomation.__new__(FacebookAutomation)
    xml = (
        '<hierarchy>'
        '<node text="赞按钮" content-desc="赞按钮，双击并长按即可给评论留下心情。" '
        'bounds="[32,360][220,430]" />'
        '<node text="评论" bounds="[230,360][420,430]" />'
        '<node text="加入小组" bounds="[72,1220][648,1300]" />'
        '</hierarchy>'
    )
    assert fb._extract_group_member_candidates(XMLParser.parse(xml)) == []


# ────────── (C) chip matcher 拒绝裸 descContains '小组' ──────────

def test_groups_chip_matcher_rejects_bare_substring_desc():
    """``_tap_search_results_groups_filter`` 不再用 ``descContains='小组'`` 裸子串.

    模拟 typeahead overlay 上 desc 含 '小组' 子串的元素只能被 *新的* 完整短语
    ``descContains='小组个搜索结果'`` 否定, 不会被误命中。
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_GROUPS_FILTER_TEXTS = (
        fb_mod.FacebookAutomation._FB_SEARCH_GROUPS_FILTER_TEXTS
    )
    inst._FB_SEARCH_GROUPS_CHIP_DESC_SUFFIXES = (
        fb_mod.FacebookAutomation._FB_SEARCH_GROUPS_CHIP_DESC_SUFFIXES
    )

    # 关键: 任意 text=lang_word 全等 都不命中 (typeahead 状态),
    # 任何 descContains='<lang_word><suffix>' 也不命中 (typeahead desc 不含完整后缀);
    # 但若旧版还在用 descContains='<lang_word>' 裸子串, 下面这个 element 会假命中。
    typeahead_desc_substr_el = MagicMock()
    typeahead_desc_substr_el.exists.return_value = False  # 任何 selector 都没真命中

    mock_d = MagicMock()
    mock_d.return_value = typeahead_desc_substr_el
    # 让所有 d(...) 调用都返回同一个 mock element (exists=False)
    mock_d.side_effect = None

    result = fb_mod.FacebookAutomation._tap_search_results_groups_filter(
        inst, mock_d, "dev1",
    )

    assert result is False, (
        "typeahead overlay 上 chip selector 必须全 miss, 不能假成功. "
        "若返 True 说明旧版裸 descContains 又被引入了."
    )

    # 验证: 至少试过 descContains='<lang_word><suffix>' 形式 (例如 '小组个搜索结果')
    desc_calls = [
        c.kwargs.get("descriptionContains")
        for c in mock_d.call_args_list
        if c.kwargs.get("descriptionContains")
    ]
    has_strict_suffix_call = any(
        "个搜索结果" in c or "个搜尋結果" in c or "の検索結果" in c
        or "search results" in c
        for c in desc_calls
    )
    assert has_strict_suffix_call, (
        f"chip matcher 必须用 '<lang>+<suffix>' 而不是裸子串. "
        f"实际 descContains 调用: {desc_calls}"
    )

    # 关键反向断言: 不能再有裸 descContains='小组' / 'Groups' / 'グループ' (无后缀)
    bare_lang_word_calls = [
        c for c in desc_calls
        if c in ("小组", "小組", "Groups", "GROUPS", "群组", "群組",
                 "グループ", "Gruppi")
    ]
    assert not bare_lang_word_calls, (
        f"不能再用裸 descContains='<lang_word>' (历史 caefd0e0 FP 根因). "
        f"误用列表: {bare_lang_word_calls}"
    )


# ────────── (D) Step 2 多路提交 + 校验 ──────────

def test_submit_first_path_succeeds_returns_early():
    """``send_action('search')`` 一次性把页面带到 results, 后两路不应再触发."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_SUBMIT_PATHS = (
        fb_mod.FacebookAutomation._FB_SEARCH_SUBMIT_PATHS
    )
    inst._FB_PROFILE_PAGE_MARKERS = (
        fb_mod.FacebookAutomation._FB_PROFILE_PAGE_MARKERS
    )
    inst._adb = MagicMock()

    mock_d = MagicMock()
    # 第一次 dump 就是 results 页 (chip 行 ≥2)
    mock_d.dump_hierarchy.return_value = (
        '<hierarchy>'
        '<node text="All"/>'
        '<node text="Groups"/>'
        '<node text="People"/>'
        '</hierarchy>'
    )

    result = fb_mod.FacebookAutomation._submit_fb_search_with_verify(
        inst, mock_d, "dev1", "潮味",
    )

    assert result is True
    mock_d.send_action.assert_called_once_with("search")
    # 后两路 keyevent 66 / 84 不应被调用
    inst._adb.assert_not_called()


def test_submit_falls_through_to_keyevent_when_send_action_silent_noop():
    """send_action 不抛异常但 silent no-op (caefd0e0 现场), 必须 fallback ENTER.

    五路设计下顺序:
      path1 send_action_search → typeahead → 继续
      path2 send_action_go     → typeahead → 继续
      path3 tap_typeahead_group → mock False → 跳过 (no dump)
      path4 keyevent_enter     → results  → 返 True
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_SUBMIT_PATHS = (
        fb_mod.FacebookAutomation._FB_SEARCH_SUBMIT_PATHS
    )
    inst._FB_PROFILE_PAGE_MARKERS = (
        fb_mod.FacebookAutomation._FB_PROFILE_PAGE_MARKERS
    )
    inst._adb = MagicMock()
    # tap_typeahead_group 路径: 模拟 typeahead 里没有群组建议行 → 跳过该路径
    inst._tap_typeahead_group_row = MagicMock(return_value=False)

    typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="潮味"/>'
        '<node text="潮味决"/>'
        '<node text="潮味决 金門金城店"/>'
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
    # dump 1 (send_action_search 后): typeahead
    # dump 2 (send_action_go 后):     typeahead
    # dump 3 (keyevent_enter 后):     results 页
    mock_d.dump_hierarchy.side_effect = [typeahead_xml, typeahead_xml, results_xml]

    result = fb_mod.FacebookAutomation._submit_fb_search_with_verify(
        inst, mock_d, "dev1", "潮味",
    )

    assert result is True
    # 五路设计下 send_action 被调用两次: search + go
    assert mock_d.send_action.call_count == 2, (
        f"期望 send_action 被调用 2 次 (search + go), 实际: {mock_d.send_action.call_count}"
    )
    # keyevent 66 (ENTER) 必须被发出
    keyevent_calls = [
        c.args[0] if c.args else c.kwargs.get("cmd", "")
        for c in inst._adb.call_args_list
    ]
    assert any("keyevent 66" in c for c in keyevent_calls), (
        f"silent no-op 后必须 fallback KEYCODE_ENTER, 实际 _adb 调用: {keyevent_calls}"
    )


def test_submit_aborts_on_profile_landing_with_proper_step_name():
    """ENTER 选中 typeahead 首位人物 → profile 页, 立即返 False, 不再继续 keyevent.

    步骤名必须是 ``enter_group_submit_landed_on_profile`` (而不是误导性的
    ``enter_group_search_not_submitted``).
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_SUBMIT_PATHS = (
        fb_mod.FacebookAutomation._FB_SEARCH_SUBMIT_PATHS
    )
    inst._FB_PROFILE_PAGE_MARKERS = (
        fb_mod.FacebookAutomation._FB_PROFILE_PAGE_MARKERS
    )
    inst._adb = MagicMock()

    typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="潮味"/>'
        '<node text="潮味决"/>'
        '</hierarchy>'
    )
    profile_xml = (
        '<hierarchy>'
        '<node text="Takeshi Yoshida"/>'
        '<node text="Add Friend" clickable="true"/>'
        '<node text="Message" clickable="true"/>'
        '<node text="411 friends"/>'
        '</hierarchy>'
    )

    mock_d = MagicMock()
    mock_d.dump_hierarchy.side_effect = [typeahead_xml, profile_xml]

    with patch("src.host.task_forensics.capture_immediate") as mock_cap:
        result = fb_mod.FacebookAutomation._submit_fb_search_with_verify(
            inst, mock_d, "dev1", "潮味",
        )

    assert result is False
    # 第 3 路 KEYCODE_SEARCH 不应再发 (避免在 profile 页继续制造副作用)
    keyevent_calls = [
        c.args[0] if c.args else c.kwargs.get("cmd", "")
        for c in inst._adb.call_args_list
    ]
    assert not any("keyevent 84" in c for c in keyevent_calls), (
        f"profile 命中后必须停止后续 keyevent, 实际: {keyevent_calls}"
    )
    # forensics step_name 必须是 profile 落地, 不是 not_submitted
    assert mock_cap.called
    step_names = [c.kwargs.get("step_name") for c in mock_cap.call_args_list]
    assert "enter_group_submit_landed_on_profile" in step_names, (
        f"步骤名应区分 profile 误入与未提交, 实际: {step_names}"
    )
    assert "enter_group_search_not_submitted" not in step_names


def test_submit_all_three_paths_fail_records_not_submitted():
    """五路提交全失败 (始终 typeahead) → 留证 ``enter_group_search_not_submitted``."""
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_SUBMIT_PATHS = (
        fb_mod.FacebookAutomation._FB_SEARCH_SUBMIT_PATHS
    )
    inst._FB_PROFILE_PAGE_MARKERS = (
        fb_mod.FacebookAutomation._FB_PROFILE_PAGE_MARKERS
    )
    inst._adb = MagicMock()

    typeahead_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text="潮味"/>'
        '<node text="潮味决"/>'
        '</hierarchy>'
    )
    mock_d = MagicMock()
    mock_d.dump_hierarchy.return_value = typeahead_xml  # 所有路径都没用

    with patch("src.host.task_forensics.capture_immediate") as mock_cap:
        result = fb_mod.FacebookAutomation._submit_fb_search_with_verify(
            inst, mock_d, "dev1", "潮味",
        )

    assert result is False
    # 三路都尝试过
    assert mock_d.send_action.called
    keyevent_calls = [
        c.args[0] if c.args else c.kwargs.get("cmd", "")
        for c in inst._adb.call_args_list
    ]
    assert any("keyevent 66" in c for c in keyevent_calls)
    assert any("keyevent 84" in c for c in keyevent_calls)
    # 留证步骤名 not_submitted
    step_names = [c.kwargs.get("step_name") for c in mock_cap.call_args_list]
    assert "enter_group_search_not_submitted" in step_names


def test_submit_no_false_positive_on_people_you_may_know():
    """回归: 搜索主页"可能认识"卡片含 '加为好友' 但无 '发消息' → 不应误判为 profile 落地。

    真实 device bug (3d938c25, 2026-04-30): send_action_search 后仍在搜索主页
    ("可能认识" 区块有 '加为好友'), profile 误判 → 两次都 abort → enter_group 失败。

    修复后: 只有 '加为好友' 无 '发消息' 时跳过 profile 判定, 继续后续路径,
    最终通过 keyevent_enter 进入 results 页。
    """
    from src.app_automation import facebook as fb_mod

    inst = MagicMock(spec=fb_mod.FacebookAutomation)
    inst._FB_SEARCH_SUBMIT_PATHS = fb_mod.FacebookAutomation._FB_SEARCH_SUBMIT_PATHS
    inst._FB_PROFILE_PAGE_MARKERS = fb_mod.FacebookAutomation._FB_PROFILE_PAGE_MARKERS
    inst._adb = MagicMock()
    inst._tap_typeahead_group_row = MagicMock(return_value=False)

    search_home_xml = (
        '<hierarchy>'
        '<node class="android.widget.EditText" text=""/>'
        '<node text="\u52a0\u4e3a\u597d\u53cb" clickable="true"/>'
        '<node text="\u79fb\u9664" clickable="true"/>'
        '<node text="\u5927\u91ce\u967d\u5e73"/>'
        '</hierarchy>'
    )
    results_xml = (
        '<hierarchy>'
        '<node text="\u5168\u90e8"/>'
        '<node text="\u5c0f\u7ec4"/>'
        '<node text="\u7528\u6237"/>'
        '</hierarchy>'
    )

    mock_d = MagicMock()
    # Path 1 (send_action_search): search_home_xml — profile guarded, continue
    # Path 2 (send_action_go):     search_home_xml — profile guarded, continue
    # Path 3 (tap_typeahead_group_row): returns False → skipped, no dump
    # Path 4 (keyevent_enter): "好友" in search_home_xml (from "加为好友") triggers
    #   typeahead_has_person_but_no_group_suggestions → also skipped, no dump
    # Path 5 (keyevent_search):   results_xml — success
    mock_d.dump_hierarchy.side_effect = [
        search_home_xml,  # path 1
        search_home_xml,  # path 2
        results_xml,      # path 5
    ]

    with patch("src.host.task_forensics.capture_immediate") as mock_cap:
        result = fb_mod.FacebookAutomation._submit_fb_search_with_verify(
            inst, mock_d, "dev1", "\u6f6e\u5473",
        )

    assert result is True, (
        "搜索主页 '可能认识' 不应误判为 profile, 最终应通过 keyevent_search 进入 results 页"
    )
    assert not mock_cap.called or all(
        c.kwargs.get("step_name") != "enter_group_submit_landed_on_profile"
        for c in mock_cap.call_args_list
    ), "不应留证 profile 误入"
