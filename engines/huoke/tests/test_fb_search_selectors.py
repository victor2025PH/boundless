# -*- coding: utf-8 -*-
"""fb_search_selectors — 结构契约单测（无设备、无 u2）。

保证元组不可变、selector 字典键合法、resourceId 归属 katana，
避免手滑把 list 或非法 kwargs 写进模块。

Run: pytest tests/test_fb_search_selectors.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.fb_contract

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app_automation import fb_search_selectors as sel


# u2 常见 kwargs；若需更多键，在此白名单扩展并同步文档
_ALLOWED_SELECTOR_KEYS = frozenset({
    "resourceId",
    "description",
    "descriptionContains",
    "text",
    "textContains",
    "className",
    "clickable",
})

_TUPLE_NAMES = (
    "FB_HOME_SEARCH_BUTTON_SELECTORS",
    "FB_FALLBACK_SEARCH_TAP_SELECTORS",
    "FB_SEARCH_SURFACE_EXTRA_SELECTORS",
    "FB_SEARCH_QUERY_EDITOR_SELECTORS",
    "FB_PEOPLE_TAB_SELECTORS",
)


def _assert_selector_dict(d: dict, *, ctx: str) -> None:
    assert isinstance(d, dict), f"{ctx}: not dict {type(d)}"
    assert d, f"{ctx}: empty dict"
    extra = set(d) - _ALLOWED_SELECTOR_KEYS
    assert not extra, f"{ctx}: unknown keys {extra}"
    rid = d.get("resourceId")
    if rid is not None:
        assert isinstance(rid, str) and rid.startswith("com.facebook.katana"), (
            f"{ctx}: resourceId must be katana, got {rid!r}"
        )


def _tuple_unique_selectors(tup: tuple, *, name: str) -> None:
    seen = set()
    for i, item in enumerate(tup):
        ctx = f"{name}[{i}]"
        _assert_selector_dict(item, ctx=ctx)
        key = tuple(sorted(item.items()))
        assert key not in seen, f"{name}: duplicate selector {item}"
        seen.add(key)


@pytest.mark.parametrize("name", _TUPLE_NAMES)
def test_each_export_is_tuple(name: str):
    tup = getattr(sel, name)
    assert isinstance(tup, tuple), f"{name} must be tuple, got {type(tup)}"
    assert len(tup) >= 1, f"{name} must be non-empty"


@pytest.mark.parametrize("name", _TUPLE_NAMES)
def test_selectors_shape_and_uniqueness(name: str):
    _tuple_unique_selectors(getattr(sel, name), name=name)


def test_home_search_has_button_then_clickable_search():
    """与 katana Home 顶栏策略一致: 先 Button+desc, 再泛化 clickable.
    2026-04-24 v2: zh-CN 优先 (实测当前 FB 全中文), 英文作 fallback."""
    t = sel.FB_HOME_SEARCH_BUTTON_SELECTORS
    assert t[0].get("className") == "android.widget.Button"
    # zh-CN 优先
    assert t[0].get("description") == "搜索"
    assert t[1].get("description") == "搜索"
    assert t[1].get("clickable") is True
    # 英文 fallback 必须仍存在
    en_descs = [d.get("description") for d in t]
    assert "Search" in en_descs, "应包含英文 'Search' fallback"


def test_people_tab_has_english_and_zh():
    """People 筛选条：英文 'People' + 中文 '用户' 本地化必须共存。"""
    t = sel.FB_PEOPLE_TAB_SELECTORS
    assert len(t) >= 3
    # 两条 descriptionContains 键型相同但取值不同，必须用完整 dict 判重
    assert len({tuple(sorted(d.items())) for d in t}) == len(t), \
        "People tab selectors 不能重复"
    texts = [d.get("text") for d in t if d.get("text")]
    descs = [d.get("descriptionContains") for d in t if d.get("descriptionContains")]
    assert "People" in texts or "People" in [d.get("description") for d in t]
    assert "用户" in texts, "应包含中文 '用户' 精确 text 变体"
    assert any("用户" in (c or "") for c in descs), \
        "应包含 descriptionContains '用户' 或 '用户搜索结果'"


def test_query_editor_only_className_editText():
    """2026-04-24 简化: 其他 selector 在新版 FB 都是 0 candidate 或假阳性,
    只保留 {className: EditText} 最稳; search 页顶部只有 1 个 EditText."""
    t = sel.FB_SEARCH_QUERY_EDITOR_SELECTORS
    assert len(t) == 1
    assert t[0] == {"className": "android.widget.EditText"}


def test_home_search_tap_skips_search_facebook_outside_top_slot(monkeypatch):
    """Search Facebook 也必须做坐标保护，避免点到右上 Messenger/聊天入口。"""
    from unittest.mock import MagicMock

    from src.app_automation import facebook as fb_mod
    from src.app_automation.facebook import FacebookAutomation

    class FakeEl:
        def __init__(self, exists, center=(0, 0), desc=""):
            self._exists = exists
            self.center = center
            self.info = {"contentDescription": desc, "text": ""}

        def exists(self, timeout=0):
            return self._exists

    class FakeDevice:
        def __init__(self):
            self.search_opened = False
            self.pkg = fb_mod.PACKAGE

        def __call__(self, **sel):
            desc = sel.get("description") or ""
            if desc == "Search Facebook":
                # Right edge, where Messenger/chat shortcuts commonly live.
                return FakeEl(True, center=(690, 112), desc=desc)
            if desc == "Search":
                return FakeEl(True, center=(584, 112), desc=desc)
            return FakeEl(False)

        def dump_hierarchy(self):
            return "<search/>" if self.search_opened else "<home/>"

        def app_current(self):
            return {"package": self.pkg}

        def app_stop(self, pkg):
            pass

        def window_size(self):
            return (720, 1600)

    d = FakeDevice()
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._did = MagicMock(return_value="dev1")
    fb._u2 = MagicMock(return_value=d)
    fb._adb_start_main_user = MagicMock()
    fb._el_center = lambda el: el.center

    def tap(_d, x, y):
        assert (x, y) != (690, 112)
        if (x, y) == (584, 112):
            d.search_opened = True

    fb.hb = MagicMock()
    fb.hb.tap.side_effect = tap

    monkeypatch.setattr(fb_mod.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        fb_mod, "hierarchy_looks_like_fb_home",
        lambda _xml: not d.search_opened,
    )
    monkeypatch.setattr(
        fb_mod, "hierarchy_looks_like_fb_search_surface",
        lambda _xml: d.search_opened,
    )

    assert fb._tap_search_bar_preferred(d, "dev1") is True
    fb.hb.tap.assert_called_once()
