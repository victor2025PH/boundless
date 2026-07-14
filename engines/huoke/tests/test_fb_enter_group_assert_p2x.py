"""P2.X (2026-04-30): 测试 _assert_on_specific_group_page 双重断言修复。

修复的历史 bug:
  textContains(group_name) 在整屏匹配, FB Feed 上的搜索建议/推荐卡片/过往帖子里
  含 group_name 字样会误判通过 → enter_group 静默成功 → 0 提取。

测试场景:
  1. 真群组页 (含 group_name + Members tab) → 通过
  2. Feed 页 (含 group_name 但无群 tab — 推荐卡片误命中) → 拒绝
  3. Messenger 页 (无 group_name 也无群 tab) → 拒绝
  4. 异常 / dump 失败 → 拒绝
"""
from __future__ import annotations

import pytest


class _FakeElement:
    def __init__(self, exists: bool):
        self._exists = exists

    def exists(self, timeout: float = 1.0) -> bool:
        return self._exists


class _FakeDevice:
    """模拟 u2 device 对象, 仅实现 textContains 查询 + dump_hierarchy."""
    def __init__(self, screen_text: str = "", xml: str = ""):
        self._screen_text = screen_text
        self._xml = xml

    def __call__(self, **kwargs):
        # textContains 查询: 走我们的 mock 屏幕文本
        target = kwargs.get("textContains") or kwargs.get("text") or ""
        return _FakeElement(target in self._screen_text)

    def dump_hierarchy(self) -> str:
        return self._xml


# ────────── 加载 facebook.FacebookAutomation 时不实例化 ──────────
# 直接拿 unbound method 测试, 不走 __init__
def _get_assert_method():
    from src.app_automation.facebook import FacebookAutomation
    return FacebookAutomation._assert_on_specific_group_page, FacebookAutomation


# ────────── 1. 真群组页 ──────────

def test_assert_passes_on_real_group_page_japanese():
    method, cls = _get_assert_method()
    fb_self = type("S", (), {
        "_GROUP_PAGE_SIGNATURE_TOKENS": cls._GROUP_PAGE_SIGNATURE_TOKENS,
    })()
    # 真群组页: 顶部含 group_name + メンバー (日文 Members) tab
    xml = ("<hierarchy><node text='ママ友サークル' bounds='[0,100][800,200]'/>"
           "<node text='ディスカッション'/><node text='メンバー'/><node text='概要'/></hierarchy>")
    d = _FakeDevice(screen_text="ママ友サークル メンバー", xml=xml)
    ok, reason = method(fb_self, d, "ママ友サークル")
    assert ok is True
    assert "name_hit" in reason
    assert "メンバー" in reason or "ディスカッション" in reason


def test_assert_passes_on_real_group_page_english():
    method, cls = _get_assert_method()
    fb_self = type("S", (), {
        "_GROUP_PAGE_SIGNATURE_TOKENS": cls._GROUP_PAGE_SIGNATURE_TOKENS,
    })()
    xml = ("<hierarchy><node text='Active Beauty Group'/>"
           "<node text='Discussion'/><node text='Members'/><node text='About'/></hierarchy>")
    d = _FakeDevice(screen_text="Active Beauty Group Discussion Members",
                     xml=xml)
    ok, reason = method(fb_self, d, "Active Beauty Group")
    assert ok is True


# ────────── 2. Feed 页误命中(历史 bug 重现) ──────────

def test_assert_rejects_feed_with_name_in_recommendation_card():
    """关键回归: FB Feed 顶部有"What's on your mind" + 推荐群卡片含 group_name,
    但没有任何群组结构 tab. 旧版本会误判通过, 新版本必须拒绝。"""
    method, cls = _get_assert_method()
    fb_self = type("S", (), {
        "_GROUP_PAGE_SIGNATURE_TOKENS": cls._GROUP_PAGE_SIGNATURE_TOKENS,
    })()
    # Feed 上某个推荐群卡片含 "潮味" 字样, 但屏幕没有 Discussion/Members 等群组 tab
    xml = ("<hierarchy><node text='Home, tab 1 of 6'/>"
           "<node text=\"What's on your mind?\"/>"
           "<node text='Suggested group: 潮味爱好者'/>"
           "<node text='Like'/><node text='Comment'/><node text='Share'/></hierarchy>")
    d = _FakeDevice(screen_text="What's on your mind? 潮味爱好者", xml=xml)
    ok, reason = method(fb_self, d, "潮味")
    assert ok is False, "Feed 推荐卡片含 group_name 但无群 tab, 必须拒绝"
    assert reason == "name_present_but_no_group_tab"


def test_assert_rejects_feed_with_name_in_search_history():
    """搜索建议下拉里残留的 group_name (历史搜索) 也不应通过"""
    method, cls = _get_assert_method()
    fb_self = type("S", (), {
        "_GROUP_PAGE_SIGNATURE_TOKENS": cls._GROUP_PAGE_SIGNATURE_TOKENS,
    })()
    xml = ("<hierarchy><node text='Recent searches'/>"
           "<node text='ママ友サークル (recent)'/>"
           "<node text='Trending'/></hierarchy>")
    d = _FakeDevice(screen_text="Recent searches ママ友サークル (recent)", xml=xml)
    ok, reason = method(fb_self, d, "ママ友サークル")
    assert ok is False
    assert reason == "name_present_but_no_group_tab"


# ────────── 3. Messenger / Profile 误入(无 name) ──────────

def test_assert_rejects_messenger_page():
    method, cls = _get_assert_method()
    fb_self = type("S", (), {
        "_GROUP_PAGE_SIGNATURE_TOKENS": cls._GROUP_PAGE_SIGNATURE_TOKENS,
    })()
    xml = ("<hierarchy><node text='New message'/>"
           "<node text='To:'/><node text='Search people'/></hierarchy>")
    d = _FakeDevice(screen_text="New message To: Search people", xml=xml)
    ok, reason = method(fb_self, d, "ママ友サークル")
    assert ok is False
    assert reason == "name_not_found"


def test_assert_rejects_profile_page():
    """自己的 Profile 页 — group_name 不在屏幕"""
    method, cls = _get_assert_method()
    fb_self = type("S", (), {
        "_GROUP_PAGE_SIGNATURE_TOKENS": cls._GROUP_PAGE_SIGNATURE_TOKENS,
    })()
    xml = ("<hierarchy><node text='Posts'/>"
           "<node text='My Profile'/><node text='Edit profile'/></hierarchy>")
    d = _FakeDevice(screen_text="Posts My Profile Edit profile", xml=xml)
    ok, reason = method(fb_self, d, "ママ友サークル")
    assert ok is False
    assert reason == "name_not_found"


# ────────── 4. 异常容错 ──────────

def test_assert_handles_dump_failure():
    """dump_hierarchy 抛异常时, 回退到 textContains 即可"""
    method, cls = _get_assert_method()
    fb_self = type("S", (), {
        "_GROUP_PAGE_SIGNATURE_TOKENS": cls._GROUP_PAGE_SIGNATURE_TOKENS,
    })()

    class _BrokenDevice:
        def __call__(self, **kwargs):
            t = kwargs.get("textContains") or ""
            # 第一步 group_name 匹配 + 第二步特征 tab 匹配
            return _FakeElement(t in {"ママ友サークル", "メンバー"})
        def dump_hierarchy(self):
            raise RuntimeError("dump failed")

    ok, reason = method(fb_self, _BrokenDevice(), "ママ友サークル")
    # textContains 找到 group_name, dump 失败但 fallback 路径找到了 メンバー → 通过
    assert ok is True


def test_assert_signature_tokens_cover_jp_zh_en():
    """断言: signature 词列表至少含中日英 3 语种"""
    from src.app_automation.facebook import FacebookAutomation
    tokens = FacebookAutomation._GROUP_PAGE_SIGNATURE_TOKENS
    assert "Members" in tokens, "需要英文 Members"
    assert "メンバー" in tokens, "需要日文 メンバー"
    assert "成员" in tokens, "需要中文 成员"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
