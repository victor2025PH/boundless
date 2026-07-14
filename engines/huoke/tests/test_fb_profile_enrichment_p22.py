"""P2.2 Sprint G-0: fb_profile_enrichment 单元测试.

mock fb_automation 验证调用流程:
  - view_profile 失败 → 返回 enriched=False, error_step
  - view_profile 成功 + about 提取成功 → bio/work/lives_in 字段填充
  - extract_posts=False 不调 _extract_recent_posts
  - posts 提取后正确过滤 noise
  - press_back_on_done 控制退出
  - enrich_top_members 按 score 排序只 enrich 前 N
"""
from __future__ import annotations

import pytest

from src.app_automation import fb_profile_enrichment as enr


# ────────── _is_likely_post_text ──────────

def test_is_likely_post_text_accepts_normal_text():
    assert enr._is_likely_post_text(
        "今日は子供と公園でお弁当を食べました🍱 桜が綺麗でした"
    ) is True


def test_is_likely_post_text_rejects_button_labels():
    assert enr._is_likely_post_text("Like") is False
    assert enr._is_likely_post_text("Comment") is False
    assert enr._is_likely_post_text("赞") is False
    assert enr._is_likely_post_text("いいね") is False


def test_is_likely_post_text_rejects_too_short():
    assert enr._is_likely_post_text("hi") is False
    assert enr._is_likely_post_text("") is False


def test_is_likely_post_text_rejects_too_long():
    assert enr._is_likely_post_text("a" * 600) is False


def test_is_likely_post_text_rejects_repeated_chars():
    assert enr._is_likely_post_text("aaaaaaaaaaa") is False
    assert enr._is_likely_post_text("ababababab") is False


def test_is_likely_post_text_rejects_close_to_noise():
    """文本是 noise + 少许字符 (FB 时间戳如 '2 h ago' 这种)"""
    assert enr._is_likely_post_text("3 h ago") is False
    assert enr._is_likely_post_text("5 d ago") is False


# ────────── _extract_recent_posts ──────────

class _FakeElement:
    def __init__(self, text: str):
        self.text = text


class _FakeDevice:
    """模拟 u2 device — 每次 dump 返回不同的 elements (模拟滚动后内容变化)"""
    def __init__(self, post_lists):
        self._post_lists = post_lists
        self._call = 0

    def dump_hierarchy(self):
        # 返回简单 XML, 实际 elements 由 monkeypatch screen_parser 注入
        return f"<dump call={self._call}/>"

    def swipe_ext(self, direction, scale=0.6):
        self._call += 1

    def press(self, key):
        self._call += 1


def _patch_xml_parser(monkeypatch, post_lists):
    """让 XMLParser.parse 按 _FakeDevice 当前 call 索引返不同 elements"""
    state = {"call": 0}

    def fake_parse(xml):
        idx = state["call"]
        state["call"] += 1
        elements = post_lists[idx] if idx < len(post_lists) else []
        return [_FakeElement(t) for t in elements]

    import src.vision.screen_parser as sp
    monkeypatch.setattr(sp.XMLParser, "parse", staticmethod(fake_parse))


def test_extract_recent_posts_collects_unique(monkeypatch):
    posts_per_scroll = [
        ["桜が綺麗でした🌸 公園でお弁当", "Like", "Comment", "Share"],
        ["子育て大変だけど楽しい毎日です", "Like", "3 h ago"],
        ["スキンケアの新商品をレビュー", "Comment", "Reply"],
    ]
    _patch_xml_parser(monkeypatch, posts_per_scroll)
    d = _FakeDevice(posts_per_scroll)
    posts = enr._extract_recent_posts(d, max_posts=3)
    assert len(posts) == 3
    assert "桜が綺麗" in posts[0]
    assert "子育て" in posts[1]
    assert "スキンケア" in posts[2]
    # noise 全被过滤
    assert all("Like" not in p and "Comment" not in p for p in posts)


def test_extract_recent_posts_dedup_across_scrolls(monkeypatch):
    """同一条 post 在多次 dump 中重复出现, 应只收一次"""
    posts_per_scroll = [
        ["桜が綺麗でした🌸 公園でお弁当", "Like"],
        ["桜が綺麗でした🌸 公園でお弁当", "子育て大変だけど楽しい毎日です"],  # 第一条重复
    ]
    _patch_xml_parser(monkeypatch, posts_per_scroll)
    d = _FakeDevice(posts_per_scroll)
    posts = enr._extract_recent_posts(d, max_posts=5)
    assert len(posts) == 2  # 去重后只有 2 条


def test_extract_recent_posts_handles_dump_failure(monkeypatch):
    class _BrokenDevice:
        def dump_hierarchy(self): raise RuntimeError("dump failed")
        def swipe_ext(self, *a, **kw): pass
        def press(self, *a): pass
    posts = enr._extract_recent_posts(_BrokenDevice(), max_posts=3)
    assert posts == []  # 容错, 不抛异常


# ────────── enrich_member_profile ──────────

class _FakeFB:
    """mock FacebookAutomation"""
    def __init__(self, view_ok=True, about_data=None, posts_per_scroll=None):
        self.view_ok = view_ok
        self.about_data = about_data or {}
        self.posts_per_scroll = posts_per_scroll or []
        self.calls = {"view_profile": 0, "read_about": 0,
                       "smart_tap": 0, "press_back": 0}

    def view_profile(self, name, read_seconds=10.0, device_id=None):
        self.calls["view_profile"] += 1
        return self.view_ok

    def read_profile_about(self, device_id=None):
        self.calls["read_about"] += 1
        return self.about_data

    def smart_tap(self, desc, device_id=None):
        self.calls["smart_tap"] += 1
        return True

    def _u2(self, device_id):
        # 返回简易 mock device, press 计数
        fb_self = self
        class _D:
            def dump_hierarchy(_self): return "<dump/>"
            def swipe_ext(_self, *a, **kw): pass
            def press(_self, key):
                fb_self.calls["press_back"] += 1
        return _D()


def test_enrich_no_name_returns_error():
    em = enr.enrich_member_profile(_FakeFB(), {"name": ""}, "dev1")
    assert em["enriched"] is False
    assert em["enrich_error"] == "no_name"


def test_enrich_no_device_id_returns_error():
    em = enr.enrich_member_profile(_FakeFB(), {"name": "x"}, "")
    assert em["enriched"] is False
    assert em["enrich_error"] == "no_device_id"


def test_enrich_view_profile_failed():
    fb = _FakeFB(view_ok=False)
    em = enr.enrich_member_profile(fb, {"name": "山田美穂"}, "dev1")
    assert em["enriched"] is False
    assert em["enrich_error"] == "view_profile_failed"
    assert em["bio"] == ""
    # 没必要调 read_about 因为 view 都失败了
    assert fb.calls["read_about"] == 0


def test_enrich_success_populates_fields(monkeypatch):
    fb = _FakeFB(view_ok=True,
                  about_data={
                      "raw_about": "Lives in Tokyo, Japan. Loves cooking and yoga.",
                      "work": "Yoga Studio Tokyo",
                      "lives_in": "Tokyo",
                  },
                  posts_per_scroll=[["今日のヨガレッスン気持ち良かった😊"]])
    _patch_xml_parser(monkeypatch, fb.posts_per_scroll)
    em = enr.enrich_member_profile(fb, {"name": "山田美穂"}, "dev1")
    assert em["enriched"] is True
    assert em["enrich_error"] == ""
    assert "Tokyo" in em["bio"]
    assert em["work"] == "Yoga Studio Tokyo"
    assert em["lives_in"] == "Tokyo"
    assert len(em["recent_posts"]) == 1
    assert "ヨガ" in em["recent_posts"][0]


def test_enrich_skip_posts_when_disabled(monkeypatch):
    fb = _FakeFB(view_ok=True,
                  about_data={"raw_about": "bio text", "work": "Cafe"})
    em = enr.enrich_member_profile(fb, {"name": "x"}, "dev1",
                                    extract_posts=False)
    assert em["enriched"] is True
    assert em["recent_posts"] == []
    # smart_tap 不应被调 (因为 extract_posts=False)
    assert fb.calls["smart_tap"] == 0


def test_enrich_press_back_on_done():
    fb = _FakeFB(view_ok=True, about_data={"raw_about": "bio"})
    enr.enrich_member_profile(fb, {"name": "x"}, "dev1",
                               extract_posts=False,
                               press_back_on_done=True)
    # ENRICH_BACK_PRESS_COUNT = 2
    assert fb.calls["press_back"] == enr.ENRICH_BACK_PRESS_COUNT


def test_enrich_no_back_when_disabled():
    fb = _FakeFB(view_ok=True, about_data={"raw_about": "bio"})
    enr.enrich_member_profile(fb, {"name": "x"}, "dev1",
                               extract_posts=False,
                               press_back_on_done=False)
    assert fb.calls["press_back"] == 0


def test_enrich_does_not_mutate_input():
    """关键: enrich 返回新 dict, 不改原输入 (防止意外副作用)"""
    fb = _FakeFB(view_ok=False)
    original = {"name": "山田", "score": 88.5, "tier": "A"}
    em = enr.enrich_member_profile(fb, original, "dev1")
    assert original == {"name": "山田", "score": 88.5, "tier": "A"}
    assert em is not original
    assert em["score"] == 88.5  # 浅拷贝保留原字段


# ────────── enrich_top_members ──────────

def test_enrich_top_members_only_top_n(monkeypatch):
    fb = _FakeFB(view_ok=True, about_data={"raw_about": "bio"})
    members = [
        {"name": "A", "score": 90},
        {"name": "B", "score": 50},
        {"name": "C", "score": 80},
        {"name": "D", "score": 30},
        {"name": "E", "score": 70},
    ]
    out = enr.enrich_top_members(fb, members, "dev1", top_n=2,
                                  sort_key=lambda m: m["score"])
    assert len(out) == 5  # 全部返回
    # A (90), C (80) 应被 enrich
    a = next(m for m in out if m["name"] == "A")
    c = next(m for m in out if m["name"] == "C")
    e = next(m for m in out if m["name"] == "E")  # 70 不在 top-2
    assert a.get("enriched") is True
    assert c.get("enriched") is True
    # E 没有被 enrich, 没 enriched 字段或为 False
    assert not e.get("enriched")


def test_enrich_top_members_handles_zero_top_n():
    fb = _FakeFB(view_ok=True)
    members = [{"name": "A", "score": 90}]
    out = enr.enrich_top_members(fb, members, "dev1", top_n=0)
    # 不 enrich 任何
    assert out == members
    assert fb.calls["view_profile"] == 0


def test_enrich_top_members_empty_input():
    fb = _FakeFB()
    assert enr.enrich_top_members(fb, [], "dev1", top_n=5) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
