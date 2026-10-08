# -*- coding: utf-8 -*-
"""待人工页的两笔小账：先不回、今天清掉几条。

先不回只对「客户在等」。记下当时客户最后那句话的指纹；这句话没变就从主列表
拿开，换浏览器也还在。对方又说了新的一句，指纹对不上，自动回到列表。
不是「已处理」——标签不动，AI 该回还回。

今天清掉＝本机自然日里，在这页摘掉「需人工」、把接管交还、页内回一句，
或在完整对话里回了正在等的客户（同一轮等待只算一次）。不把「先不回」算进去。

落盘 ``<config_dir>/cockpit_hold.json``。文件坏了按空账，不挡队列。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("ai_chat_assistant.cockpit_hold")

_FILE = "cockpit_hold.json"
_CLEARED_KEEP_SEC = 48 * 3600.0
_lock = threading.Lock()


def item_fingerprint(it: Dict[str, Any]) -> str:
    """和页面「这句话变了就回来」同一口径。"""
    return "|".join(
        str((it or {}).get(k) or "")
        for k in ("kind", "detail", "last_text", "quote_media")
    )


def _path():
    from src.licensing.data_paths import config_dir
    return config_dir() / _FILE


def _empty() -> Dict[str, Any]:
    return {"snooze": {}, "cleared": []}


def _load() -> Dict[str, Any]:
    data = _empty()
    try:
        with open(str(_path()), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data
    snooze = raw.get("snooze")
    cleared = raw.get("cleared")
    if isinstance(snooze, dict):
        data["snooze"] = snooze
    if isinstance(cleared, list):
        data["cleared"] = [c for c in cleared if isinstance(c, dict)]
    return data


def _save(data: Dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    data["cleared"] = [
        c for c in (data.get("cleared") or [])
        if now - float(c.get("ts") or 0) < _CLEARED_KEEP_SEC
    ][-500:]
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, str(path))


def _day(ts: float):
    t = time.localtime(ts)
    return (t.tm_year, t.tm_yday)


def apply_snooze(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把仍对得上指纹的「客户在等」分到 held。指纹变了或已经不是在等，删掉这笔并留在主列表。"""
    with _lock:
        data = _load()
        snooze = data["snooze"]
        visible: List[Dict[str, Any]] = []
        held: List[Dict[str, Any]] = []
        changed = False
        for it in items or []:
            cid = str(it.get("conversation_id") or "")
            rec = snooze.get(cid) if cid else None
            if not isinstance(rec, dict):
                visible.append(it)
                continue
            same = (it.get("kind") == "waiting"
                    and str(rec.get("fp") or "") == item_fingerprint(it))
            if same:
                held.append(it)
            else:
                snooze.pop(cid, None)
                changed = True
                visible.append(it)
        if changed:
            try:
                _save(data)
            except Exception:
                logger.debug("[cockpit] 先不回账本写失败（忽略）", exc_info=True)
        return visible, held


def remember_snooze(cid: str, fp: str, now: float = None) -> None:
    cid = str(cid or "").strip()
    if not cid:
        return
    ts = time.time() if now is None else float(now)
    with _lock:
        data = _load()
        data["snooze"][cid] = {"fp": str(fp or ""), "at": ts}
        _save(data)


def forget_snooze(cid: str) -> bool:
    cid = str(cid or "").strip()
    if not cid:
        return False
    with _lock:
        data = _load()
        existed = cid in data["snooze"]
        if existed:
            data["snooze"].pop(cid, None)
            _save(data)
        return existed


def note_cleared(cid: str, how: str, now: float = None, by: str = "") -> None:
    ts = time.time() if now is None else float(now)
    rec = {"ts": ts, "cid": str(cid or ""), "how": str(how or "")}
    if by:
        rec["by"] = str(by)[:64]
    with _lock:
        data = _load()
        data["cleared"].append(rec)
        _save(data)


def cleared_recent(cid: str, how: str, within_sec: float,
                   now: float = None) -> bool:
    """同一会话同一种清法在 ``within_sec`` 秒内已经记过（防重放/连点重复记）。"""
    cid = str(cid or "").strip()
    if not cid:
        return False
    ts = time.time() if now is None else float(now)
    for c in _load()["cleared"]:
        if str(c.get("cid") or "") != cid or str(c.get("how") or "") != str(how or ""):
            continue
        try:
            if ts - float(c.get("ts") or 0) <= float(within_sec):
                return True
        except (TypeError, ValueError):
            continue
    return False


def cleared_since(cid: str, since: float) -> bool:
    """这个会话从 ``since`` 起是否已经记过一笔清掉（同一轮等待不重复记）。"""
    cid = str(cid or "").strip()
    if not cid:
        return False
    try:
        floor = float(since)
    except (TypeError, ValueError):
        return False
    for c in _load()["cleared"]:
        if str(c.get("cid") or "") != cid:
            continue
        try:
            if float(c.get("ts") or 0) >= floor:
                return True
        except (TypeError, ValueError):
            continue
    return False


def cleared_today(now: float = None) -> int:
    ts = time.time() if now is None else float(now)
    day = _day(ts)
    data = _load()
    n = 0
    for c in data["cleared"]:
        try:
            if _day(float(c.get("ts") or 0)) == day:
                n += 1
        except (TypeError, ValueError, OSError):
            continue
    return n
