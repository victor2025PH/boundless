# -*- coding: utf-8 -*-
"""
Facebook 搜索路径 — hierarchy 子串与启动弹窗文案（单源）。

供 ``FacebookAutomation._tap_search_bar_preferred`` / ``enter_group`` 使用，
避免 Home / 搜索页 / 搜索结果页 / Messenger 误判字符串散落在 ``facebook.py``
内难以同步。
"""

from __future__ import annotations

import re
from typing import Final, Tuple

# ── Home Feed（出现任一即视为在 Home）────────────────────────────────
FB_WHATS_ON_MIND: Final[str] = "What's on your mind?"
FB_HOME_TAB_MARKER: Final[str] = "Home, tab 1 of"
FB_FEED_LIST_MARKER: Final[str] = "id/feed_list"
# 2026-04-24 中文 katana bottom tab content-desc: '首页，第1/6个选项卡'
FB_HOME_TAB_MARKER_ZH: Final[str] = "首页，第1"

# 发帖框提示语（多语言 katana 常见文案，与英文逻辑并列；按需再扩）
FB_HOME_COMPOSER_I18N_SUBSTRINGS: Tuple[str, ...] = (
    "A cosa stai pensando?",       # IT
    "\u00bfEn qu\u00e9 est\u00e1s pensando?",  # ES: ¿En qué estás pensando?
    "\u4eca\u306a\u306b\u3057\u3066\u308b\uff1f",  # JP: 今なにしてる？
    "\u4f60\u5728\u60f3\u4ec0\u4e48\uff1f",       # zh-Hans: 你在想什么？
    "\u5728\u60f3\u4e9b\u4ec0\u4e48\uff1f",        # zh-Hans alt
)

# ── 搜索页（顶栏搜索：无发帖框 + 存在 EditText）────────────────────────
FB_EDITTEXT_CLASS_IN_XML: Final[str] = 'class="android.widget.EditText"'

# ── Group Members 内搜索页（不是 Facebook 全局搜索）────────────────────
#
# 8 号英文版真机复现：任务从群组 Members 页残留状态开始时，页面也有一个
# EditText（hint="Search members"），旧 search_surface 判据会把它误认为全局
# FB 搜索页，导致宽关键词被输入到“群成员搜索”里，结果一直搜不到小组。
FB_GROUP_MEMBERS_SEARCH_MARKERS: Tuple[str, ...] = (
    'text="Members"',
    'hint="Search members"',
    "New people and Pages that join this group will appear here",
    "Admins and moderators",
    "Group contributors",
)

# ── Messenger / Chats 误点检测 ─────────────────────────────────────────
FB_GET_MESSENGER_BANNER: Final[str] = "Get Messenger"
FB_CHATS_FRAGMENT_ID: Final[str] = "id/chats_fragment"
FB_MESSAGES_CALLS_SNIPPET: Final[str] = "Messages and calls are"

# ── 冷启动后 ``_force_back_to_home`` 内快速点掉的常见按钮文案 ─────────
FB_STARTUP_DISMISS_TARGET_TEXTS: Tuple[str, ...] = (
    "Not Now", "Skip", "Maybe Later", "OK", "Got it",
    "Continue", "Close", "Dismiss", "Cancel",
    "Allow", "While using the app", "Later",
)


def hierarchy_looks_like_fb_home(xml: str) -> bool:
    """当前 dump 是否像 FB Home Feed（宽松启发式）。"""
    if not xml:
        return False
    if (
        FB_WHATS_ON_MIND in xml
        or FB_HOME_TAB_MARKER in xml
        or FB_HOME_TAB_MARKER_ZH in xml
        or FB_FEED_LIST_MARKER in xml
    ):
        return True
    return any(s in xml for s in FB_HOME_COMPOSER_I18N_SUBSTRINGS)


def hierarchy_looks_like_fb_search_surface(xml: str) -> bool:
    """是否像「已进入搜索输入界面」（非 Feed）。与 katana 新版 placeholder 对齐。"""
    if not xml:
        return False
    if FB_WHATS_ON_MIND in xml:
        return False
    if FB_EDITTEXT_CLASS_IN_XML not in xml:
        return False
    if hierarchy_looks_like_fb_group_members_search(xml):
        return False
    return True


def hierarchy_looks_like_fb_group_members_search(xml: str) -> bool:
    """是否像群组 Members 页内搜索，而不是 Facebook 全局搜索。"""
    if not xml:
        return False
    if 'hint="Search members"' in xml:
        return True
    marker_hits = sum(1 for marker in FB_GROUP_MEMBERS_SEARCH_MARKERS
                      if marker in xml)
    return marker_hits >= 2


# ── 搜索结果页 filter chip 行（真实落在 results 页才会出现） ──────────────
#
# 核心区分：typeahead overlay 只是搜索建议下拉, **不会** 出现以下任何 2 个
# 作为 ``text="..."`` 的可点击 chip。一旦看到 ≥2 个 chip text，必是结果页。
# 历史 bug (caefd0e0 2026-04-30): 旧 ``hierarchy_looks_like_fb_search_surface``
# 只查 EditText 存在 + 无 'What's on your mind', 导致 typeahead overlay
# 也被误判为搜索页, Step 2 send_action('search') 静默失败也察觉不到。
#
# 注意：必须用 ``text="<词>"`` 完整匹配（XML 属性值），而不是子串包含——因为
# typeahead 行的 content-desc 形如 ``"搜索建议: 潮味, 第 1 项, 共 6 项"`` 也含
# 单字 "All" / "用户" 等子串, 子串包含会再次误命中。
FB_SEARCH_RESULTS_CHIP_TEXTS: Tuple[str, ...] = (
    # 英
    "All", "Posts", "People", "Groups", "Pages", "Photos", "Videos",
    "Marketplace", "Reels", "Events", "Places",
    # zh-Hans
    "全部", "帖子", "用户", "小组", "公共主页", "照片", "视频", "商城",
    "活动",
    # zh-Hant
    "貼文", "用戶", "小組", "粉絲專頁", "相片", "影片", "活動",
    # 日
    "すべて", "投稿", "ユーザー", "グループ", "ページ", "写真", "動画",
    # 意 / 西
    "Tutto", "Tutti", "Persone", "Gruppi", "Pagine",
    "Todo", "Personas", "Páginas",
)

FB_SEARCH_RESULTS_CHIP_DESC_SUFFIXES: Tuple[str, ...] = (
    "个搜索结果",
    "個搜尋結果",
    "の検索結果",
    "search results",
)


def _count_chip_texts_in_xml(xml: str) -> int:
    """统计 ``text="<chip>"`` 形式的 filter chip 数量（区分大小写完整匹配）。"""
    if not xml:
        return 0
    n = 0
    for chip in FB_SEARCH_RESULTS_CHIP_TEXTS:
        # XML 属性值的精确闭合匹配，避免 typeahead desc 的子串干扰
        needle = f'text="{chip}"'
        if needle in xml:
            n += 1
            continue
        # 新版 katana 的 chip 可能 text 为空, label 落在 content-desc:
        #   content-desc="小组个搜索结果, 第3项，共7项"
        # typeahead 建议行没有这些完整后缀, 因此仍可区分。
        if any(f"{chip}{suffix}" in xml or f"{chip} {suffix}" in xml
               for suffix in FB_SEARCH_RESULTS_CHIP_DESC_SUFFIXES):
            n += 1
    return n


def hierarchy_looks_like_fb_search_results_page(xml: str) -> bool:
    """是否真正进入了搜索 *结果* 页（而非 typeahead overlay）。

    判据：filter-chip 行至少出现 **2 个** 不同的 chip text 完整匹配。
    单个匹配可能是输入框 placeholder / 弹窗按钮巧合命中，提两个互不相邻的
    chip 才能稳定区分。
    """
    return _count_chip_texts_in_xml(xml) >= 2


def hierarchy_looks_like_fb_search_typeahead(xml: str) -> bool:
    """启发式：还停留在搜索 typeahead overlay（输入页+下拉建议）。

    判据：是搜索页 (``hierarchy_looks_like_fb_search_surface`` 通过) 但
    **不是** 搜索结果页（chip 行 < 2）。
    """
    if not hierarchy_looks_like_fb_search_surface(xml):
        return False
    return not hierarchy_looks_like_fb_search_results_page(xml)


# ── Groups-filtered 结果页特征（Step 3 之后的强校验） ─────────────────────
#
# 历史 bug (caefd0e0 2026-04-30): 旧版 markers 用 ('members'|'小组'|'公开小组'|...)
# 子串包含, ``'小组'`` 单字在 typeahead 任何 desc/text 一旦命中就误放行。
# 加严：必须出现以下 *完整短语* 之一（大小写敏感），typeahead overlay 不会有。
FB_GROUPS_FILTERED_PAGE_STRICT_PHRASES: Tuple[str, ...] = (
    # 英文（含/不含大小写）
    "Public group", "Private group", "Public Group", "Private Group",
    "Visible group", "Hidden group",
    # zh-Hans
    "公开小组", "私密小组", "公开群组", "私密群组",
    # zh-Hant
    "公開社團", "私密社團", "公開小組", "私密小組",
    # 日
    "公開グループ", "非公開グループ",
    "公開のグループ", "非公開のグループ",
    # 意 / 西 / 韩 (按需扩展)
    "Gruppo pubblico", "Gruppo privato",
    "Grupo público", "Grupo privado",
)

# "<N> members" / "<N> 名のメンバー" / "<N> 名成员" / "<N>位成員" 等 — 群组列表行
# 上常见的成员数标签。typeahead 的 desc 里不会出现这种"数字+量词+成员"模式。
_FB_GROUPS_MEMBER_COUNT_RE: Final[re.Pattern] = re.compile(
    # 数字部分：纯数字 / 千分位 / "1.2K" / "8.5 万" 这类缩写
    r"(?:\d{1,3}(?:[,，]\d{3})+|\d+(?:[.,]\d+)?(?:\s*[KkMm万萬])?)\s*"
    # 量词 + 成员
    r"(?:members?|名のメンバー|名(?:成员|成員|メンバー)|"
    r"位(?:成员|成員|会员|會員)|成员|成員)",
)


def hierarchy_looks_like_fb_groups_filtered_results_page(xml: str) -> bool:
    """Step 3 后强校验：是否在 Groups-filtered 结果列表页。

    必须同时:
      1. 是 results 页 (`hierarchy_looks_like_fb_search_results_page` 为 True)
      2. 出现 *完整短语* group 标识或 ``\\d+ members`` 行模式

    typeahead overlay 上即使 desc 含 ``'小组'`` 子串, chip 行 <2 也会被
    步骤 1 拒掉, 双闸再保险。
    """
    if not xml:
        return False
    if not hierarchy_looks_like_fb_search_results_page(xml):
        return False
    if any(phrase in xml for phrase in FB_GROUPS_FILTERED_PAGE_STRICT_PHRASES):
        return True
    if _FB_GROUPS_MEMBER_COUNT_RE.search(xml):
        return True
    return False


# ── Typeahead 中 person-profile 识别 ────────────────────────────────────────
#
# 背景：keyevent 66 (ENTER) 在 FB 搜索 typeahead overlay 开着时, 会选中
# 第一条 typeahead 建议。若第一条是人物主页建议 (content-desc 含 "N friends" /
# "好友" 等人际词), ENTER 会导航到该 profile 而不是搜索结果页。
#
# 识别策略：
#   person-suggestion desc 形如 "桐島青大, 4 mutual friends"  / "Name · 好友"
#   group-suggestion  desc 形如 "潮味, 1.2K members"         / "Name · 小组"
#   search-row        desc 形如 "Search for 潮味"             (SAFE: ENTER→结果)
#
# 这里用 **存在 person-marker 而不存在 group-marker** 的组合判断。
# 保守策略：如果 person/group 同时出现, 认为「group 存在」—— 不跳过 ENTER。
# 如果只有 person-marker, 跳过 ENTER 改用 keyevent 84。
_FB_TYPEAHEAD_PERSON_DESC_MARKERS: Tuple[str, ...] = (
    # 英文 FB ("4 mutual friends" / "Add friend" suggestion)
    "mutual friend", "mutual friends",
    "Add Friend", "Add friend",
    "Friends", "friends",
    # zh-Hans
    "好友", "加为好友", "加为朋友", "共同好友",
    # zh-Hant
    "好友", "加為好友", "共同朋友",
    # 日
    "友達", "共通の友達", "友達リクエスト",
)
_FB_TYPEAHEAD_GROUP_DESC_MARKERS: Tuple[str, ...] = (
    # 英
    "members", "member", "Public group", "Private group",
    # zh-Hans
    "成员", "小组", "公开小组", "私密小组", "群组",
    # zh-Hant
    "成員", "小組", "群組",
    # 日
    "メンバー", "グループ",
)


def typeahead_has_person_but_no_group_suggestions(xml: str) -> bool:
    """typeahead overlay 中出现了 person-profile 建议但没有 group 建议。

    用于在发送 KEYCODE_ENTER 前判断是否跳过——如果 typeahead 首位是人物主页,
    ENTER 会错进 profile 而不是搜索结果页。

    True = 有人物建议且无群组建议  → **跳过 ENTER**
    False = 安全 / 含群组建议     → **允许 ENTER**
    """
    if not xml:
        return False
    has_person = any(m in xml for m in _FB_TYPEAHEAD_PERSON_DESC_MARKERS)
    has_group = any(m in xml for m in _FB_TYPEAHEAD_GROUP_DESC_MARKERS)
    return has_person and not has_group


def hierarchy_looks_like_messenger_or_chats(xml: str) -> bool:
    """是否误在 Messenger / Chats 等需先 back 的界面。"""
    if not xml:
        return False
    return (
        FB_GET_MESSENGER_BANNER in xml
        or FB_CHATS_FRAGMENT_ID in xml
        or (xml.count(FB_MESSAGES_CALLS_SNIPPET) >= 1)
    )
