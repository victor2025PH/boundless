# -*- coding: utf-8 -*-
"""入站图 vs 本会话已发 / 人设相册的指纹（#333 P0-2 / P2 pHash，2026-09-17）。

人脸边车默认关、截图/回传也比 ArcFace 更该先认「这就是我刚发出去的那张」。
分层：先 **sha256**（字节级）；对不上再 **pHash**（重编码 / 缩放 / 轻压缩仍近重复）。
命中相册或本会话最近真发回执 → ``persona``。对不上 → 空串。
任何 IO 失败静默，拟稿零阻断。门禁 ``tests/test_media_ownership.py``。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_IMAGE_TYPES = frozenset({
    "image", "photo", "picture", "sticker", "sticker_image", "gif", "animation",
})


def sha256_path(path: Any) -> str:
    p = Path(str(path or ""))
    if not p.is_file():
        return ""
    try:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def classify_sha(*, inbound_sha: str, album_sha: str = "", sent_sha: str = "") -> str:
    """纯函数：入站 sha 与相册/已发指纹比对 → ``persona`` / ``unknown``。"""
    s = str(inbound_sha or "").strip().lower()
    if not s:
        return "unknown"
    if s == str(album_sha or "").strip().lower() and s:
        return "persona"
    if s == str(sent_sha or "").strip().lower() and s:
        return "persona"
    return "unknown"


def classify_near(*, inbound_phash: str, album_phash: str = "",
                  sent_phash: str = "") -> str:
    """sha 对不上之后：pHash 近重复 → ``persona``。空指纹 / 依赖缺失 → ``unknown``。"""
    h = str(inbound_phash or "").strip().lower()
    if not h:
        return "unknown"
    try:
        from src.companion.image_phash import is_near_dup
    except Exception:
        return "unknown"
    if album_phash and is_near_dup(h, album_phash):
        return "persona"
    if sent_phash and is_near_dup(h, sent_phash):
        return "persona"
    return "unknown"


def _resolve_path(media_ref: str) -> str:
    ref = str(media_ref or "").strip()
    if not ref:
        return ""
    try:
        from src.integrations.protocol_bridge import static_media_ref_to_path
        p = static_media_ref_to_path(ref)
        if p and Path(p).is_file():
            return str(p)
    except Exception:
        pass
    try:
        if Path(ref).is_file():
            return ref
    except Exception:
        pass
    return ""


def _album_phash_map(store: Any, pid: str, *, max_fill: int = 40) -> Dict[str, str]:
    """相册指纹：已存 phash 优先；缺列的旧图按 file_path 现算并 ``set_auto_tag`` 写回（上限 max_fill）。"""
    got: Dict[str, str] = {}
    if store is None or not pid:
        return got
    try:
        if hasattr(store, "phashes"):
            got.update(store.phashes(pid) or {})
    except Exception:
        pass
    if not hasattr(store, "list"):
        return got
    try:
        from src.companion.image_phash import phash_file
        filled = 0
        for row in store.list(pid) or []:
            if filled >= int(max_fill):
                break
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "")
            if not rid or rid in got:
                continue
            h = str(row.get("phash") or "").strip()
            if not h:
                h = phash_file(row.get("file_path") or "")
                if h and hasattr(store, "set_auto_tag"):
                    try:
                        store.set_auto_tag(rid, phash=h)
                    except Exception:
                        logger.debug("[media_ownership] pHash 写回失败", exc_info=True)
            if h:
                got[rid] = h
                filled += 1
    except Exception:
        logger.debug("[media_ownership] album pHash 现算失败", exc_info=True)
    return got


def _sent_phash(rec: Any) -> str:
    if not isinstance(rec, dict):
        return ""
    h = str(rec.get("phash") or "").strip()
    if h:
        return h
    try:
        from src.companion.image_phash import phash_file
        return phash_file(rec.get("path") or "")
    except Exception:
        return ""


def annotate_hash_ownership(
    *, conversation_id: str = "", persona_id: str = "",
    media_type: str = "", media_ref: str = "",
) -> str:
    """入站媒体指纹命中人设相册或本会话已发 → identity_note；否则 ''。"""
    try:
        mt = str(media_type or "").strip().lower()
        if mt and mt not in _IMAGE_TYPES:
            return ""
        path = _resolve_path(media_ref)
        if not path:
            return ""
        sha = sha256_path(path)
        if not sha:
            return ""
        album_sha = ""
        album_store = None
        pid = str(persona_id or "").strip()
        if pid:
            try:
                from src.companion.persona_media_store import get_persona_media_store
                album_store = get_persona_media_store()
                row = album_store.find_by_sha(pid, sha) if album_store is not None else None
                if row:
                    album_sha = sha
            except Exception:
                album_sha = ""
                album_store = None
        sent_sha = ""
        sent_rec: Any = None
        ck = str(conversation_id or "").strip()
        if ck:
            try:
                from src.inbox.image_autosend import resolve_last_sent_media
                sent_rec = resolve_last_sent_media(ck) or {}
                sent_sha = str(sent_rec.get("sha256") or "").strip()
                if not sent_sha and sent_rec.get("path"):
                    sent_sha = sha256_path(sent_rec.get("path"))
            except Exception:
                sent_sha = ""
                sent_rec = None
        label = classify_sha(inbound_sha=sha, album_sha=album_sha, sent_sha=sent_sha)
        if label != "persona":
            try:
                from src.companion.image_phash import NEAR_DUP_MAX_HAMMING, nearest, phash_file
                inbound_h = phash_file(path)
                album_h = ""
                if inbound_h and pid and album_store is not None:
                    _nid, dist = nearest(inbound_h, _album_phash_map(album_store, pid))
                    if _nid and dist <= NEAR_DUP_MAX_HAMMING:
                        album_h = inbound_h
                sent_h = _sent_phash(sent_rec) if sent_rec else ""
                label = classify_near(
                    inbound_phash=inbound_h, album_phash=album_h, sent_phash=sent_h)
            except Exception:
                logger.debug("[media_ownership] pHash 回退失败", exc_info=True)
        if label != "persona":
            return ""
        from src.companion.visual_identity import identity_note
        return identity_note("persona")
    except Exception:
        logger.debug("[media_ownership] annotate failed", exc_info=True)
        return ""


__all__ = [
    "sha256_path", "classify_sha", "classify_near", "annotate_hash_ownership",
]
