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
