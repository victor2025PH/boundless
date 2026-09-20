# -*- coding: utf-8 -*-
"""「相册无命中」会话琥珀 note（Q-35 #306 BPMEWX，2026-09-12）。

背景：客户「发张自拍」→ ``image_autosend.pick_registered_media`` 查注册相册
``[album_match] … candidates=0`` → **AI 侧**已在库（``note_album_miss`` → 生成侧 consume →
``album_miss_addendum`` 让 AI 按场景改口，不空头承诺）——但**坐席侧**一字不见：状态带 /
草稿条都没说「相册里没有能匹配的图」，用户只会问「后台传了很多图为什么不发」。

本模块只让它**可见**：无命中处写一条 ``album_no_match:<cid>={ts, query, scene, persona_id, n}``，
``conv_state.compute`` 作为第七个只读源读它 → 状态带下一行琥珀
「相册没有匹配『自拍』的图 · AI 已改口 · 去相册补标签」+ 深链
``/personas?pid=<pid>&pma=1&filter=notrg``；同会话下一次相册**命中**即清。

存储：复用 ``InboxStore`` 通用 KV ``app_settings``（与 ``ai_fail_marker`` 同款，**不动 store.py /
不建表**）。``image_autosend`` 没有 store 句柄 → 缺省经 ``protocol_bridge.get_inbox_store()``
惰性取（web 层启动时注入；测试 / CLI 场景拿不到 → 静默 no-op）。
红线：不改 Q-6 匹配打分、不改 ``note_album_miss`` / ``album_miss_addendum`` 行为——本模块只是旁路记账。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

KEY_PREFIX = "album_no_match:"
#: 超过这个时长的 note 视为陈旧，状态带不再显示（相册补货往往当天完成；隔天旧账不该还挂着）
DEFAULT_TTL_SEC = 24 * 3600
#: 同会话连续无命中在此窗口内只累计 n、不刷新首见时刻（状态带「今天已 n 次」）
_REPEAT_WINDOW_SEC = 6 * 3600


def _key(cid: str) -> str:
    return KEY_PREFIX + str(cid or "").strip()


def _default_store() -> Any:
    try:
        from src.integrations.protocol_bridge import get_inbox_store
        return get_inbox_store()
    except Exception:
        return None


def deep_link(persona_id: str) -> str:
    """相册「缺触发词」筛选的深链（personas.html 消费 ``?pid=&pma=1&filter=notrg``）。"""
    pid = str(persona_id or "").strip()
    return ("/personas?pid=" + quote(pid, safe="") + "&pma=1&filter=notrg") if pid \
        else "/personas?pma=1&filter=notrg"


def _parse(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        got = json.loads(str(raw or "") or "{}")
        if isinstance(got, dict) and got.get("ts"):
            return got
    except Exception:
        pass
    return None


def mark(cid: str, *, query: str = "", scene: str = "", persona_id: str = "",
         store: Any = None, ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """写 / 累计 note。绝不抛；拿不到 store → None。"""
    cid = str(cid or "").strip()
    if not cid:
        return None
    st = store if store is not None else _default_store()
    if st is None or not hasattr(st, "set_app_setting"):
        return None
    now = float(ts or time.time())
    prev = get(cid, store=st, now=now, ttl_sec=_REPEAT_WINDOW_SEC)
    rec = {
        "ts": now,
        "first_ts": float((prev or {}).get("first_ts") or now),
        "n": int((prev or {}).get("n") or 0) + 1,
        "query": str(query or "").replace("\n", " ").strip()[:80],
        "scene": str(scene or "")[:40],
        "persona_id": str(persona_id or "")[:64],
    }
    try:
        st.set_app_setting(_key(cid), json.dumps(rec, ensure_ascii=False), updated_by="album_miss")
        return rec
    except Exception:
        logger.debug("[album_miss_marker] 写入失败（忽略）", exc_info=True)
        return None


def clear(cid: str, *, store: Any = None) -> bool:
    """相册命中 / 补标后清 note（值空串 = 删键）。没有 note 时零写。绝不抛。"""
    cid = str(cid or "").strip()
    if not cid:
        return False
    st = store if store is not None else _default_store()
    if st is None or not hasattr(st, "set_app_setting") or not hasattr(st, "get_app_setting"):
        return False
    try:
        if not str(st.get_app_setting(_key(cid), "") or ""):
            return False
        st.set_app_setting(_key(cid), "")
        return True
    except Exception:
        logger.debug("[album_miss_marker] 清除失败（忽略）", exc_info=True)
        return False


def get(cid: str, *, store: Any = None, now: Optional[float] = None,
        ttl_sec: float = DEFAULT_TTL_SEC) -> Optional[Dict[str, Any]]:
    """读某会话 note；无 / 脏 / 超 ``ttl_sec`` → None（陈旧不删，只是不显示）。"""
    cid = str(cid or "").strip()
    st = store if store is not None else _default_store()
    if not cid or st is None or not hasattr(st, "get_app_setting"):
        return None
    try:
        rec = _parse(st.get_app_setting(_key(cid), ""))
    except Exception:
        return None
    if not rec:
        return None
    try:
        age = float(now if now is not None else time.time()) - float(rec.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if ttl_sec and age > float(ttl_sec):
        return None
    rec["age_sec"] = round(max(0.0, age), 0)
    return rec


def hhmm(ts: Any) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return "--:--"


__all__ = ["KEY_PREFIX", "DEFAULT_TTL_SEC", "mark", "clear", "get", "deep_link", "hhmm"]
