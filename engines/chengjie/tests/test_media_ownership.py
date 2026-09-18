# -*- coding: utf-8 -*-
"""#333 P0-2：入站图 sha256 vs 相册 / 本会话已发。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.companion.media_ownership import (
    classify_near, classify_sha, sha256_path, annotate_hash_ownership,
)
from src.companion.visual_identity import identity_note


def test_classify_sha_persona_vs_unknown():
    assert classify_sha(inbound_sha="abc", album_sha="abc") == "persona"
    assert classify_sha(inbound_sha="abc", sent_sha="ABC") == "persona"
    assert classify_sha(inbound_sha="abc", album_sha="zzz", sent_sha="yyy") == "unknown"
    assert classify_sha(inbound_sha="") == "unknown"


def test_sha256_path_roundtrip(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello-media")
    d = sha256_path(p)
    assert len(d) == 64 and d == sha256_path(str(p))
    assert sha256_path(tmp_path / "missing.jpg") == ""


def test_annotate_hash_hits_last_sent(tmp_path, monkeypatch):
    blob = b"\x89PNG outbound-same"
    sent = tmp_path / "sent.png"
    inbound = tmp_path / "back.png"
    sent.write_bytes(blob)
    inbound.write_bytes(blob)

    def _last(_ck):
        return {"path": str(sent), "sha256": sha256_path(sent)}

    monkeypatch.setattr(
        "src.inbox.image_autosend.resolve_last_sent_media", _last, raising=False)
    monkeypatch.setattr(
        "src.companion.persona_media_store.get_persona_media_store",
        lambda: None, raising=False)
    note = annotate_hash_ownership(
        conversation_id="wa:1:2", persona_id="", media_type="image",
        media_ref=str(inbound))
    assert "自己的照片" in note or "此前发过" in note
    assert note == identity_note("persona")


def test_annotate_hash_miss_is_empty(tmp_path, monkeypatch):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    monkeypatch.setattr(
        "src.inbox.image_autosend.resolve_last_sent_media",
        lambda _ck: {"path": str(a), "sha256": sha256_path(a)}, raising=False)
    monkeypatch.setattr(
        "src.companion.persona_media_store.get_persona_media_store",
        lambda: None, raising=False)
    assert annotate_hash_ownership(
        conversation_id="wa:1:2", media_type="image", media_ref=str(b)) == ""


def test_wiring_persona_reply_and_skill_manager():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    pr = (root / "src" / "inbox" / "persona_reply.py").read_text(encoding="utf-8")
    sm = (root / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    assert "annotate_hash_ownership" in pr and "not _own" in pr
    assert "annotate_hash_ownership" in sm and "_media_ownership" in sm


def test_classify_near_persona_vs_unknown():
    same = "0123456789abcdef"
    assert classify_near(inbound_phash=same, sent_phash=same) == "persona"
    assert classify_near(inbound_phash=same, album_phash="fedcba9876543210") == "unknown"
    assert classify_near(inbound_phash="") == "unknown"


def test_annotate_phash_hits_reencoded_last_sent(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    import io
    from PIL import Image, ImageDraw
    from src.companion.image_phash import phash_bytes

    def _photo():
        img = Image.new("RGB", (320, 240), (30, 80, 140))
        d = ImageDraw.Draw(img)
        d.ellipse([40, 30, 180, 170], fill=(220, 180, 140))
        d.rectangle([200, 80, 300, 200], fill=(200, 40, 40))
        return img

    png = io.BytesIO(); _photo().save(png, "PNG")
    jpg = io.BytesIO(); _photo().save(jpg, "JPEG", quality=55)
    sent = tmp_path / "sent.png"
    inbound = tmp_path / "back.jpg"
    sent.write_bytes(png.getvalue())
    inbound.write_bytes(jpg.getvalue())
    assert sha256_path(sent) != sha256_path(inbound)
    assert phash_bytes(png.getvalue())

    monkeypatch.setattr(
        "src.inbox.image_autosend.resolve_last_sent_media",
        lambda _ck: {"path": str(sent), "sha256": sha256_path(sent)}, raising=False)
    monkeypatch.setattr(
        "src.companion.persona_media_store.get_persona_media_store",
        lambda: None, raising=False)
    note = annotate_hash_ownership(
        conversation_id="wa:1:2", persona_id="", media_type="image",
        media_ref=str(inbound))
    assert note == identity_note("persona")


def test_annotate_phash_fills_album_row_without_stored_hash(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    import io
    from PIL import Image, ImageDraw

    def _photo():
        img = Image.new("RGB", (320, 240), (10, 90, 40))
        d = ImageDraw.Draw(img)
        d.ellipse([20, 20, 160, 160], fill=(240, 200, 80))
        return img

    png = io.BytesIO(); _photo().save(png, "PNG")
    jpg = io.BytesIO(); _photo().save(jpg, "JPEG", quality=50)
    album = tmp_path / "album.png"
    inbound = tmp_path / "shot.jpg"
    album.write_bytes(png.getvalue())
    inbound.write_bytes(jpg.getvalue())
    assert sha256_path(album) != sha256_path(inbound)

    class _Album:
        def __init__(self):
            self.written = []

        def find_by_sha(self, pid, sha):
            return None

        def phashes(self, pid):
            return {}

        def list(self, pid):
            return [{"id": "old1", "phash": "", "file_path": str(album)}]

        def set_auto_tag(self, media_id, **kw):
            self.written.append((media_id, kw.get("phash")))
            return {"id": media_id, "phash": kw.get("phash")}

    album_st = _Album()
    monkeypatch.setattr(
        "src.companion.persona_media_store.get_persona_media_store",
        lambda: album_st, raising=False)
    monkeypatch.setattr(
        "src.inbox.image_autosend.resolve_last_sent_media",
        lambda _ck: {}, raising=False)
    note = annotate_hash_ownership(
        conversation_id="wa:1:2", persona_id="nori", media_type="image",
        media_ref=str(inbound))
    assert note == identity_note("persona")
    assert album_st.written and album_st.written[0][0] == "old1" and album_st.written[0][1]
