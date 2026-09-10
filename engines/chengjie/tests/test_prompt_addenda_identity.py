# -*- coding: utf-8 -*-
"""Q-1 F（#269）：prompt_addenda.identity_addendum——账号昵称 ≠ 人设名时给生成链一句「别否认」。"""
from __future__ import annotations

from types import SimpleNamespace

from src.inbox.prompt_addenda import account_display_name, identity_addendum, persona_display_name


def test_mismatch_returns_addendum_with_account_name():
    s = identity_addendum({"name": "Mizuki"}, {"meta": {"self_name": "Vanessa"}})
    assert s and "「Vanessa」" in s and "不得否认" in s and "不必解释" in s
    assert "Mizuki" not in s   # 附加段只讲昵称，不复述人设名


def test_same_or_contained_name_returns_empty():
    assert identity_addendum({"name": "Mizuki"}, {"meta": {"self_name": "Mizuki"}}) == ""
    assert identity_addendum({"name": "Mizuki"}, {"meta": {"self_name": "Mizuki 🌸"}}) == ""
    assert identity_addendum({"name": "Mizuki Tanaka"}, {"self_name": "Mizuki"}) == ""
    assert identity_addendum("mizuki", "MIZUKI") == ""


def test_unknown_either_side_returns_empty():
    assert identity_addendum({"name": "Mizuki"}, {}) == ""
    assert identity_addendum({}, {"meta": {"self_name": "Vanessa"}}) == ""
    assert identity_addendum(None, None) == ""
    assert identity_addendum({"name": ""}, {"self_name": "   "}) == ""
    assert identity_addendum(object(), 123) == ""    # 怪对象也不抛


def test_shapes_and_langs():
    assert persona_display_name({"profile": {"name": "Nori"}}) == "Nori"
    assert persona_display_name(SimpleNamespace(name="Nori")) == "Nori"
    assert account_display_name({"name": "Vanessa"}) == "Vanessa"
    assert account_display_name(SimpleNamespace(meta={"self_name": "Vanessa"})) == "Vanessa"
    en = identity_addendum("Mizuki", "Vanessa", lang="en")
    assert '"Vanessa"' in en and "Never deny" in en
    ja = identity_addendum("Mizuki", "Vanessa", lang="ja")
    assert "「Vanessa」" in ja and "否定せず" in ja


def test_pure_function_no_persona_reply_touch():
    import inspect
    import src.inbox.prompt_addenda as m
    src = inspect.getsource(m)
    assert "from src.inbox.persona_reply" not in src and "import persona_reply" not in src
