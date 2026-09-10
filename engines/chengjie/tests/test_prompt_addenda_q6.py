# -*- coding: utf-8 -*-
"""Q-6 F（#263）：三个 addenda 各出现一次、为空不出现；单一接线点 _prompt_addenda。"""
from __future__ import annotations

from src.inbox.prompt_addenda import (
    album_miss_addendum,
    album_scene_addendum,
    identity_addendum,
    sent_media_addendum,
)


def test_album_scene_empty_omitted():
    assert album_scene_addendum({}) == ""
    assert album_scene_addendum({"selfie": 0, "other": 0}) == ""


def test_album_scene_lists_kinds_once():
    s = album_scene_addendum({"selfie": 3, "outdoor": 8, "food": 1})
    assert s.count("【相册可展示场景】") == 1
    assert "自拍 3" in s and "室外风景 8" in s and "美食 1" in s
    assert "只能承诺清单内" in s


def test_sent_media_unknown_conv_omitted_known_empty_is_never():
    assert sent_media_addendum([], conv_known=False) == ""
    never = sent_media_addendum([], conv_known=True)
    assert never.count("【本会话已发媒体】") == 1
    assert "从未给 TA 发过照片" in never
    listed = sent_media_addendum(
        [{"id": "m1", "scene": "outdoor", "when": "09-10 23:19"}], conv_known=True)
    assert "m1/outdoor" in listed and "从未" not in listed


def test_identity_still_omitted_when_names_match():
    assert identity_addendum({"name": "Nori"}, {"self_name": "Nori"}) == ""
    one = identity_addendum({"name": "Nori"}, {"self_name": "MikeNick"})
    assert one.count("MikeNick") == 1


def test_prompt_addenda_wires_three_once(monkeypatch):
    from src.inbox import persona_reply as pr

    class _St:
        def list(self, *a, **k):
            return [{"id": "x", "enabled": True, "tags": ["kind:selfie"], "auto_meta": {}}]

        def sent_history(self, *a, **k):
            return {"items": []}

    monkeypatch.setattr(
        "src.companion.persona_media_store.get_persona_media_store", lambda: _St())
    monkeypatch.setattr(
        "src.inbox.image_autosend.pick_registered_media", lambda *a, **k: {"id": "hit"})
    monkeypatch.setattr(
        "src.inbox.image_autosend.consume_album_miss", lambda *a, **k: {})
    monkeypatch.setattr(
        "src.ai.companion_selfie.detect_selfie_request", lambda *a, **k: False)
    monkeypatch.setattr(
        "src.ai.companion_selfie.extract_requested_scene", lambda *a, **k: "")
    monkeypatch.setattr(
        "src.companion.persona_media.requested_scene_kind", lambda *a, **k: "")
    monkeypatch.setattr(
        "src.inbox.media_claim_block.blocked_addendum", lambda *a, **k: "")

    text = pr._prompt_addenda(
        {"name": "Nori", "id": "nori"},
        {"self_name": "DisplayNick"},
        "wa:acct:chat",
        persona_id="nori",
        inbound="hi",
        config={"companion": {"selfie": {"enabled": True}}},
    )
    assert text.count("【相册可展示场景】") == 1
    assert text.count("【本会话已发媒体】") == 1
    assert "从未给 TA 发过照片" in text
    assert text.count("DisplayNick") == 1
    assert "【无可用照片" not in text


def test_album_miss_addendum_forbids_delay_and_lies():
    s = album_miss_addendum("风景")
    assert "无可用照片（场景 风景）" in s
    assert "稍后发" in s and "加载中" in s
