# -*- coding: utf-8 -*-
"""P0-2（2026-09-21，#339 / #332-A）：谈照片 ≠ 要照片。

出图闸在宽口径 ``detect_selfie_request`` 之上再要求「索要句形」：客户说
「love your picture」「that's a cute selfie」「你的照片真好看」「I just took a selfie」
→ 不匹配相册、不记 miss、不刷红条、不跟发；真索要（动词 / 求图句式 / 问号）照旧放行。
``pick_registered_media`` 的通用池与 selfie_fallback 同口径。
"""

from __future__ import annotations

import pytest

from src.inbox import image_send_gate as isg


@pytest.mark.parametrize("text", [
    "love your picture", "that's a cute selfie", "selfie time for me lol",
    "I just took a selfie of myself", "你的照片真好看", "自拍",
])
def test_mention_only_is_not_an_ask(text):
    assert isg.explicit_photo_ask(text) is False
    g = isg.compute_image_intent(text, [])
    assert g.intent is False and g.reason == isg.REASON_MENTION_ONLY
    assert g.trigger == isg.TRIGGER_NONE


@pytest.mark.parametrize("text", [
    "can I see a picture of you", "send me a photo", "May I see a pic?", "got any pics?",
    "what do you look like", "发张照片", "發個照片給我看看嘛", "你长什么样", "照片呢",
    "your selfie?",
])
def test_real_ask_still_passes(text):
    assert isg.explicit_photo_ask(text) is True
    g = isg.compute_image_intent(text, [])
    assert g.intent is True and g.trigger == isg.TRIGGER_ASK


def test_strict_scene_kind_needs_ask_shape_too():
    assert isg.strict_requested_scene_kind("that's a cute selfie") == ""
    assert isg.strict_requested_scene_kind("send me a landscape pic") == "outdoor"


def test_mention_only_with_commitment_or_offer_unchanged():
    # 承诺兑现 / 运营触发词与本判定无关，照旧
    g = isg.compute_image_intent("love your picture", [], assume_intent="selfie")
    assert g.intent is True and g.trigger == isg.TRIGGER_COMMITMENT
    g = isg.compute_image_intent("love your dance 跳舞", [], trigger_terms=["跳舞"])
    assert g.intent is True and g.trigger == isg.TRIGGER_KEYWORD
    # 闲聊仍是 no_intent（与 mention_only 区分，便于日志归因）
    assert isg.compute_image_intent("今天心情不错", []).reason == isg.REASON_NO_INTENT


def test_pick_registered_media_skips_match_on_mention_only(monkeypatch):
    from src.inbox import image_autosend as ia

    called = {"pick": 0, "miss": 0}

    class _Store:
        def list(self, pid, enabled_only=True):
            return [{"id": "m1", "enabled": 1}]

    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: _Store())

    def _pick(*a, **k):
        called["pick"] += 1
        return None

    monkeypatch.setattr("src.companion.persona_media.pick_media", _pick)
    monkeypatch.setattr(ia, "note_album_miss", lambda *a, **k: called.__setitem__("miss", called["miss"] + 1))
    cfg = {"companion": {"selfie": {"enabled": True}}}
    row = ia.pick_registered_media(cfg, "p1", "that's a cute selfie", conv_key="c1")
    assert row is None and called["pick"] == 0 and called["miss"] == 0
