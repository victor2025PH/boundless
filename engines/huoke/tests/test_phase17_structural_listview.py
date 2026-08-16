# -*- coding: utf-8 -*-
"""Phase 17 (2026-04-25): XMLParser parent_index/parent_class + ListView 行级匹配."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestXmlParserParentInfo:
    """XMLParser._walk 给 element 加 parent_index/parent_class."""

    def test_root_element_has_no_parent(self):
        from src.vision.screen_parser import XMLParser
        xml = '<hierarchy><root bounds="[0,0][100,100]" class="A"/></hierarchy>'
        elements = XMLParser.parse(xml)
        # root has no parent
        assert len(elements) >= 1
        assert elements[0].parent_index == -1
        assert elements[0].parent_class == ""

    def test_child_links_to_parent(self):
        from src.vision.screen_parser import XMLParser
        xml = '''<hierarchy>
          <root bounds="[0,0][100,100]" class="LinearLayout">
            <child bounds="[10,10][90,90]" class="TextView" text="hello"/>
          </root>
        </hierarchy>'''
        elements = XMLParser.parse(xml)
        # root + child
        assert len(elements) == 2
        # child.parent_index 指向 root
        child = elements[1]
        assert child.parent_index == 0
        assert child.parent_class == "LinearLayout"

    def test_skip_node_without_bounds_propagates_grandparent(self):
        """没 bounds 的 node 不入 list, 但 children 的 parent 跳到祖父."""
        from src.vision.screen_parser import XMLParser
        xml = '''<hierarchy>
          <root bounds="[0,0][100,100]" class="A">
            <middle class="B">
              <leaf bounds="[20,20][30,30]" class="C" text="x"/>
            </middle>
          </root>
        </hierarchy>'''
        elements = XMLParser.parse(xml)
        # root + leaf (middle 没 bounds 不入)
        assert len(elements) == 2
        leaf = elements[1]
        # leaf 的 parent 应该是 root (跳过 middle)
        assert leaf.parent_index == 0
        assert leaf.parent_class == "A"

    def test_existing_parse_signature_unchanged(self):
        """老 caller XMLParser.parse(xml) 用法不变, 返 list."""
        from src.vision.screen_parser import XMLParser
        elements = XMLParser.parse(
            '<hierarchy><x bounds="[0,0][10,10]" class="A"/></hierarchy>')
        assert isinstance(elements, list)
        assert hasattr(elements[0], "parent_index")
        assert hasattr(elements[0], "parent_class")


class TestListMessengerConversationsStructural:
    """Phase 17: 结构敏感 ListView 行级过滤."""
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        return FacebookAutomation.__new__(FacebookAutomation)

    def test_list_row_kept_toolbar_button_filtered(self, monkeypatch):
        """父容器是 RecyclerView 的 row 通过, 父容器是 Toolbar 的不通过."""
        from src.app_automation.facebook import FacebookAutomation
        fb = self._make_fb()
        class _El:
            def __init__(self, text, parent_class, clickable=True):
                self.text = text
                self.clickable = clickable
                self.parent_class = parent_class
                self.bounds = (0, 0, 100, 100)
        # 模拟: 工具栏按钮 (parent=Toolbar) + 列表行 (parent=RecyclerView)
        fake = [
            _El("查看翻译", "androidx.appcompat.widget.Toolbar"),  # toolbar
            _El("Reply", "androidx.appcompat.widget.Toolbar"),     # toolbar
            _El("山田花子", "androidx.recyclerview.widget.RecyclerView"),
            _El("佐藤美咲", "androidx.recyclerview.widget.RecyclerView"),
        ]
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp_mod
        monkeypatch.setattr(sp_mod.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = fb._list_messenger_conversations(d, max_n=10)
        names = [it["name"] for it in items]
        # 工具栏按钮被结构层 filter 跳过 (即使没在黑名单)
        assert "查看翻译" not in names  # 既被 sanitize 又被结构过滤
        assert "山田花子" in names
        assert "佐藤美咲" in names

    def test_old_parser_no_parent_class_falls_back_to_sanitize(self,
                                                                  monkeypatch):
        """老 parser (parent_class='') 退化用 sanitize 单层防御."""
        from src.app_automation.facebook import FacebookAutomation
        fb = self._make_fb()
        class _OldEl:
            def __init__(self, text):
                self.text = text
                self.clickable = True
                self.parent_class = ""  # 老 parser 没有 parent info
                self.bounds = (0, 0, 100, 100)
        fake = [
            _OldEl("查看翻译"),  # 黑名单 ban
            _OldEl("山田花子"),  # 通过
        ]
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp_mod
        monkeypatch.setattr(sp_mod.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = fb._list_messenger_conversations(d, max_n=10)
        names = [it["name"] for it in items]
        assert names == ["山田花子"]
        # "查看翻译" 仍被 sanitize 拦掉 (与 Phase 15 行为一致)

    def test_real_xml_end_to_end(self, monkeypatch):
        """端到端: 用真实 Android XML, 验证 parser+filter 配合."""
        from src.app_automation.facebook import FacebookAutomation
        fb = self._make_fb()
        xml = '''<hierarchy>
          <main bounds="[0,0][1080,2400]" class="FrameLayout">
            <toolbar bounds="[0,0][1080,200]"
                     class="androidx.appcompat.widget.Toolbar">
              <btn bounds="[20,50][120,150]" class="android.widget.Button"
                   text="Reply" clickable="true"/>
              <btn2 bounds="[140,50][240,150]" class="android.widget.Button"
                    text="More" clickable="true"/>
            </toolbar>
            <recycler bounds="[0,200][1080,2200]"
                     class="androidx.recyclerview.widget.RecyclerView">
              <row1 bounds="[0,200][1080,400]"
                    class="android.widget.LinearLayout"
                    text="山田花子" clickable="true"/>
              <row2 bounds="[0,400][1080,600]"
                    class="android.widget.LinearLayout"
                    text="佐藤美咲" clickable="true"/>
            </recycler>
          </main>
        </hierarchy>'''
        d = MagicMock()
        d.dump_hierarchy.return_value = xml
        items = fb._list_messenger_conversations(d, max_n=10)
        names = [it["name"] for it in items]
        # 真实结构: Reply/More 在 Toolbar 父容器 → struct 过滤跳; row1/row2
        # 在 RecyclerView 父容器 → 通过.
        assert "山田花子" in names
        assert "佐藤美咲" in names
        assert "Reply" not in names
        assert "More" not in names


# ─── P0 (2026-08-15): 综合未读判定 + 增量滚动收集 ─────────────────────────
class TestUnreadSignalIntegration:
    """未读判定改走 detect_unread 综合信号 (selected 修复 + content-desc 关键词)."""

    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        return FacebookAutomation.__new__(FacebookAutomation)

    def _el(self, text, desc="", selected=False):
        class _E:
            pass
        e = _E()
        e.text = text
        e.content_desc = desc
        e.selected = selected
        e.clickable = True
        e.parent_class = "androidx.recyclerview.widget.RecyclerView"
        e.bounds = (0, 0, 1080, 200)
        return e

    def test_content_desc_unread_keyword_detected(self, monkeypatch):
        """content-desc 含 "2 unread messages" → unread=True (原实现只看 name 里的 •)."""
        fb = self._make_fb()
        fake = [
            self._el("Alice", desc="Alice, 2 unread messages, 5m"),
            self._el("Bob", desc="Bob, 昨天, 你: 好的"),  # 已读
        ]
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp
        monkeypatch.setattr(sp.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = {it["name"]: it for it in
                 fb._list_messenger_conversations(d, max_n=10, max_scrolls=0)}
        assert items["Alice"]["unread"] is True
        assert items["Bob"]["unread"] is False

    def test_selected_attribute_now_respected(self, monkeypatch):
        """selected=True → unread (修复前 XMLElement 从不解析 selected → 恒 False)."""
        fb = self._make_fb()
        fake = [self._el("Carol", selected=True)]
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp
        monkeypatch.setattr(sp.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = fb._list_messenger_conversations(d, max_n=10, max_scrolls=0)
        assert items[0]["unread"] is True
        assert items[0]["unread_reason"] == "selected"

    def test_middle_dot_no_longer_false_unread(self, monkeypatch):
        """'名字 • 时间' 的分隔符 • 不再被误判为未读 (原实现的坑)."""
        fb = self._make_fb()
        fake = [self._el("山田花子 • 5分钟前")]
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp
        monkeypatch.setattr(sp.XMLParser, "parse",
                            staticmethod(lambda xml: fake))
        items = fb._list_messenger_conversations(d, max_n=10, max_scrolls=0)
        # 名字合法即收 (sanitize 允许), 但不该判未读
        assert items and items[0]["unread"] is False

    def test_selected_parsed_from_real_xml(self):
        """screen_parser 端: selected="true" 真被解析进 XMLElement."""
        from src.vision.screen_parser import XMLParser
        xml = ('<hierarchy><n bounds="[0,0][10,10]" class="A" '
               'selected="true" checked="true"/></hierarchy>')
        els = XMLParser.parse(xml)
        assert els[0].selected is True
        assert els[0].checked is True


class TestScrollCollection:
    """P0: 增量滚动收集屏外对话 (原实现只 dump 首屏 → 屏外未读永不可见)."""

    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()  # scroll_down no-op
        return fb

    def _el(self, text):
        class _E:
            pass
        e = _E()
        e.text = text
        e.content_desc = ""
        e.selected = False
        e.clickable = True
        e.parent_class = "androidx.recyclerview.widget.RecyclerView"
        e.bounds = (0, 0, 1080, 200)
        return e

    def test_scroll_collects_offscreen_rows(self, monkeypatch):
        fb = self._make_fb()
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        d.window_size.return_value = (1080, 2400)  # 可 int 化 → 滚动生效
        screen1 = [self._el("Alice"), self._el("Bob")]
        screen2 = [self._el("Carol"), self._el("David")]
        screens = iter([screen1, screen2, screen2])  # 第 3 屏重复 → added=0 停
        import src.vision.screen_parser as sp
        monkeypatch.setattr(sp.XMLParser, "parse",
                            staticmethod(lambda xml: next(screens)))
        items = fb._list_messenger_conversations(d, max_n=10, max_scrolls=3)
        names = [it["name"] for it in items]
        assert names == ["Alice", "Bob", "Carol", "David"]
        assert fb.hb.scroll_down.called

    def test_no_scroll_when_max_scrolls_zero(self, monkeypatch):
        fb = self._make_fb()
        d = MagicMock()
        d.dump_hierarchy.return_value = "<x/>"
        d.window_size.return_value = (1080, 2400)
        calls = {"n": 0}

        def _parse(xml):
            calls["n"] += 1
            return [self._el("Alice")]
        import src.vision.screen_parser as sp
        monkeypatch.setattr(sp.XMLParser, "parse", staticmethod(_parse))
        items = fb._list_messenger_conversations(d, max_n=10, max_scrolls=0)
        assert [it["name"] for it in items] == ["Alice"]
        assert calls["n"] == 1  # 只 dump 一屏
        assert not fb.hb.scroll_down.called

    def test_scroll_degrades_gracefully_without_window_size(self, monkeypatch):
        """window_size 不可解析 (MagicMock) → 退化单屏, 不抛."""
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)  # 无 hb
        d = MagicMock()  # window_size() 返回 MagicMock → int() 抛 → 退化
        d.dump_hierarchy.return_value = "<x/>"
        import src.vision.screen_parser as sp
        monkeypatch.setattr(sp.XMLParser, "parse",
                            staticmethod(lambda xml: [self._el("Alice")]))
        items = fb._list_messenger_conversations(d, max_n=10, max_scrolls=3)
        assert [it["name"] for it in items] == ["Alice"]


class TestTitleVerificationAndDedup:
    """P0: 开会话标题回读校验 (peer_name 自我纠正) + 入站去重."""

    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.hb = MagicMock()
        fb.hb.tap = MagicMock()
        return fb

    def test_title_mismatch_corrects_peer_name(self, monkeypatch):
        """列表名与会话标题不符 → 以会话标题为权威 + 置 title_mismatch."""
        fb = self._make_fb()
        d = MagicMock()
        conv = {"name": "柳原慧", "bounds": (0, 300, 1080, 500)}
        recorded = {}

        import src.host.fb_store as store
        monkeypatch.setattr(store, "record_inbox_message",
                            lambda did, peer, **kw: recorded.update(
                                {"peer": peer, "kw": kw}) or 1)
        monkeypatch.setattr(store, "inbox_message_seen_recently",
                            lambda *a, **k: False, raising=False)
        monkeypatch.setattr(store, "mark_greeting_replied_back",
                            lambda *a, **k: 0, raising=False)
        monkeypatch.setattr(fb, "_detect_risk_dialog",
                            lambda d: (False, ""))
        # 点进去后读到的其实是「萧雅云」(点错了行)
        monkeypatch.setattr(fb, "_read_thread_title", lambda d: "萧雅云")
        monkeypatch.setattr(fb, "_extract_latest_incoming_message",
                            lambda d: "你好呀")
        monkeypatch.setattr("src.app_automation.facebook.time.sleep",
                            lambda *a: None)
        monkeypatch.setattr("src.app_automation.facebook.random.uniform",
                            lambda *a: 0.0)
        detail = fb._open_and_read_conversation(d, conv, "devA")
        assert detail["title_mismatch"] is True
        assert detail["peer_name"] == "萧雅云"      # 以会话标题为准
        assert recorded["peer"] == "萧雅云"          # 入库用纠正后的名字

    def test_dedup_skips_repeat(self, monkeypatch):
        """近窗口已入库同一条 → deduped, 不重复写库."""
        fb = self._make_fb()
        d = MagicMock()
        conv = {"name": "Alice", "bounds": (0, 300, 1080, 500)}
        writes = []

        import src.host.fb_store as store
        monkeypatch.setattr(store, "record_inbox_message",
                            lambda *a, **k: writes.append(1) or 1)
        monkeypatch.setattr(store, "inbox_message_seen_recently",
                            lambda *a, **k: True, raising=False)  # 已见过
        monkeypatch.setattr(fb, "_detect_risk_dialog",
                            lambda d: (False, ""))
        monkeypatch.setattr(fb, "_read_thread_title", lambda d: "")
        monkeypatch.setattr(fb, "_extract_latest_incoming_message",
                            lambda d: "重复消息")
        monkeypatch.setattr("src.app_automation.facebook.time.sleep",
                            lambda *a: None)
        monkeypatch.setattr("src.app_automation.facebook.random.uniform",
                            lambda *a: 0.0)
        detail = fb._open_and_read_conversation(d, conv, "devA")
        assert detail["deduped"] is True
        assert writes == []  # 没写库
        assert "incoming_text" not in detail  # 不触发后续回复
