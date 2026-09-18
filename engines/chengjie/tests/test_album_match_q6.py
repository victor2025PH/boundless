# -*- coding: utf-8 -*-
"""Q-6 B/E（#263 #266）：[album_match] / 建议词 0.6 / 随机关 / 索风景不抓自拍。"""
from __future__ import annotations

import logging
import random

from src.companion.persona_media import (
    album_trigger_counts,
    requested_scene_kind,
    scene_kind_of,
    select_media,
)


def _row(i, **kw):
    r = {
        "id": str(i), "enabled": True, "media_type": "photo",
        "triggers": kw.get("triggers", []), "weight": kw.get("weight", 1),
        "tags": kw.get("tags", []), "auto_meta": kw.get("auto_meta", {}),
        "min_bond_level": 0,
    }
    r.update(kw)
    return r


def test_suggest_triggers_match_at_low_weight():
    rows = [
        _row(1, triggers=[], auto_meta={"triggers_suggest": ["海边", "沙滩"]}),
        _row(2, triggers=["跳舞"]),
    ]
    got = select_media(rows, "来张海边的", generic_ok=True, allow_random=False,
                       rng=random.Random(1))
    assert got and got["id"] == "1"
    assert float(got.get("_match_weight") or 0) == 0.6


def test_confirmed_keyword_beats_suggest():
    rows = [
        _row(1, triggers=[], auto_meta={"triggers_suggest": ["海边"]}),
        _row(2, triggers=["海边"]),
    ]
    got = select_media(rows, "海边", generic_ok=True, allow_random=False)
    assert got and got["id"] == "2"
    assert got.get("_match_weight") in (None, 1, 1.0)


def test_random_off_on_photo_ask_even_if_generic_ok():
    rows = [_row(1, triggers=[]), _row(2, triggers=[])]
    assert select_media(rows, "发张自拍", generic_ok=True, allow_random=False) is None


def test_scenery_ask_picks_outdoor_not_selfie():
    rows = [
        _row(1, triggers=[], tags=["kind:selfie", "scene:home"],
             auto_meta={"scene_kind": "selfie", "indoor": "indoor", "people_count": 1}),
        _row(2, triggers=[], tags=["kind:outdoor", "scene:park"],
             auto_meta={"scene_kind": "outdoor", "indoor": "outdoor"}),
    ]
    assert requested_scene_kind("发张风景照给我") == "outdoor"
    got = select_media(
        rows, "发张风景照给我", generic_ok=True, allow_random=False,
        required_scene_kind="outdoor", rng=random.Random(1))
    assert got and got["id"] == "2"
    assert scene_kind_of(got) == "outdoor"


def test_trigger_counts_three_buckets():
    rows = [
        _row(1, triggers=["已确认"]),
        _row(2, triggers=[], auto_meta={"triggers_suggest": ["海边"]}),
        _row(3, triggers=[], auto_meta={}),
        _row(4, triggers=["x"], auto_meta={"triggers_suggest": ["y"]}),
    ]
    c = album_trigger_counts(rows)
    assert c == {"confirmed": 2, "ai_suggest": 1, "none": 1}


def test_pick_registered_media_logs_album_match(caplog, monkeypatch):
    from src.inbox import image_autosend as ia

    class _St:
        def list(self, *a, **k):
            return [_row(1, triggers=["海边"], tags=["kind:outdoor"])]

        def sent_history(self, *a, **k):
            return {"ids": set(), "series": set(), "file_keys": set(), "items": []}

    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: _St())
    cfg = {"companion": {"selfie": {"enabled": True, "album_ai": {"apply": "auto"}}}}
    with caplog.at_level(logging.INFO, logger="src.inbox.image_autosend"):
        # logger name may be ai_chat... module logger is __name__
        pass
    caplog.set_level(logging.INFO)
    row = ia.pick_registered_media(cfg, "nori", "海边看看", conv_key="wa:a:c")
    assert row and row["id"] == "1"
    joined = "\n".join(r.message for r in caplog.records)
    assert "[album_match]" in joined
    assert "picked=1" in joined and "fallback=none" in joined


def test_miss_notes_and_consume(monkeypatch):
    from src.inbox import image_autosend as ia

    class _St:
        def list(self, *a, **k):
            return [_row(1, triggers=["跳舞"], tags=["kind:selfie"])]

        def sent_history(self, *a, **k):
            return {"ids": set(), "series": set(), "file_keys": set(), "items": []}

    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: _St())
    cfg = {"companion": {"selfie": {"enabled": True}}}
    ia.consume_album_miss("wa:a:miss")
    row = ia.pick_registered_media(
        cfg, "nori", "发张风景照", conv_key="wa:a:miss")
    assert row is None
    rec = ia.consume_album_miss("wa:a:miss")
    assert rec and rec.get("scene") == "outdoor"
    assert ia.consume_album_miss("wa:a:miss") == {}


def test_picture_of_you_falls_back_to_enabled_selfie(monkeypatch, caplog):
    from src.inbox import image_autosend as ia

    class _St:
        def list(self, *a, **k):
            return [_row(1, triggers=[], tags=["kind:selfie"])]

        def sent_history(self, *a, **k):
            return {"ids": set(), "series": set(), "file_keys": set(), "items": []}

    monkeypatch.setattr("src.companion.persona_media_store.get_persona_media_store", lambda: _St())
    cfg = {"companion": {"selfie": {"enabled": True, "consistency": {"enabled": False}}}}
    caplog.set_level(logging.INFO)
    row = ia.pick_registered_media(
        cfg, "nori", "can I see a picture of you", conv_key="wa:a:picyou")
    assert row and row["id"] == "1"
    joined = "\n".join(r.message for r in caplog.records)
    assert "selfie_fallback=1" in joined
