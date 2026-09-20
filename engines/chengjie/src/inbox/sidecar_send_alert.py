# -*- coding: utf-8 -*-
"""Messenger 边车「发送卡住」铃铛（Q-24 A 段，#298，2026-09-12）。

DXAPAX 实锤：同会话连败 4 次 → 边车静默 backoff 40s → 坐席零感知、客户被晾。现在边车
连败 ≥2 即把该条进待重试队列，并经 ``/api/internal/protocol/session-status`` 推
``status=send_stuck|send_recovered|send_stuck_final``（detail=``<code>|jid=..|streak=..|preview=..``）。
本模块把它翻成工作台通知中心 ``sys_status`` 条目（``app.state.notif_queue``，与
health_watchdog / profile_fill.notify_conflict 同语义，按 id 合并），不进
``PlatformSessionHealth``（那是登录态状态机，塞发送态会污染健康判定）。

红线：不动 autosend_worker / autosend_policy；不自动换文案；只通知。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SEND_STUCK_STATUSES = frozenset(("send_stuck", "send_recovered", "send_stuck_final"))

_BELL_KEY = {
    "send_stuck": "inbox.ms.bell.send_stuck",
    "send_recovered": "inbox.ms.bell.send_recovered",
    "send_stuck_final": "inbox.ms.bell.send_stuck_final",
}


def is_send_stuck_status(status: str) -> bool:
    return str(status or "").lower() in SEND_STUCK_STATUSES


def parse_send_stuck_detail(detail: str) -> Dict[str, str]:
    """``composer_detached|jid=1000123|streak=2|preview=hello`` → dict（缺字段给空串）。"""
    out = {"code": "", "jid": "", "streak": "", "preview": "", "tries": ""}
    for i, p in enumerate(str(detail or "").split("|")):
        if "=" not in p:
            if i == 0:
                out["code"] = p.strip()[:40]
            continue
        k, v = p.split("=", 1)
        k = k.strip()
        if k in out:
            out[k] = v.strip()[:120]
    return out


def _t(key: str, lang: str = "zh", **fmt: Any) -> str:
    try:
        from src.web.web_i18n import t as _tt
        s = _tt(key, lang)
    except Exception:
        s = key
    if fmt:
        try:
            s = s.format(**fmt)
        except Exception:
            pass
    return s


def _peer_name(platform: str, account_id: str, jid: str, app: Any) -> str:
    """会话显示名（联系人名 > 会话 peer_name > jid 尾号）。best-effort。"""
    try:
        store = getattr(getattr(app, "state", None), "inbox_store", None)
        if store is not None and jid:
            from src.inbox.normalizer import conv_id
            cid = conv_id(platform, account_id, jid)
            fn = getattr(store, "get_conversation", None)
            conv = fn(cid) if fn else None
            name = ""
            if isinstance(conv, dict):
                name = str(conv.get("peer_name") or conv.get("name") or "")
            elif conv is not None:
                name = str(getattr(conv, "peer_name", "") or getattr(conv, "name", "") or "")
            if name:
                return name[:30]
    except Exception:
        pass
    j = str(jid or "")
    return f"…{j[-4:]}" if len(j) > 4 else (j or "?")


def push_send_stuck_bell(app: Any, *, platform: str, account_id: str, status: str,
                         detail: str, lang: str = "zh") -> Optional[Dict[str, Any]]:
    """推一条 sys_status 到通知中心；返回写入的条目（无队列/异常 → None）。"""
    st = str(status or "").lower()
    key = _BELL_KEY.get(st)
    if not key or app is None:
        return None
    try:
        state = getattr(app, "state", app)
        nq = getattr(state, "notif_queue", None)
        if nq is None:
            nq = []
            state.notif_queue = nq
        d = parse_send_stuck_detail(detail)
        name = _peer_name(platform, account_id, d["jid"], app)
        sid = f"ms_send_stuck:{platform}:{account_id}:{d['jid']}"[:64]
        text = _t(key, lang, name=name)
        conv_id = ""
        try:
            from src.inbox.normalizer import conv_id as _cid
            conv_id = _cid(platform, account_id, d["jid"]) if d["jid"] else ""
        except Exception:
            conv_id = ""
        # 同会话只留最新一条（stuck → recovered 覆盖，铃铛不堆叠）
        nq[:] = [n for n in nq
                 if not ((n or {}).get("type") == "sys_status"
                         and str(((n or {}).get("data") or {}).get("id") or "") == sid)]
        item = {"type": "sys_status",
                "data": {"id": sid, "text": text[:300], "source": "messenger_sidecar",
                         "level": "warn" if st != "send_recovered" else "info",
                         "sidecar_code": d["code"], "streak": d["streak"],
                         "conversation_id": conv_id, "platform": platform,
                         "account_id": account_id, "chat_key": d["jid"]},
                "_notif_ts": int(time.time() * 1000)}
        nq.append(item)
        if len(nq) > 200:
            del nq[:-200]
        logger.warning("[messenger] 发送卡住铃铛 status=%s account=%s jid=%s code=%s streak=%s",
                       st, account_id, d["jid"], d["code"], d["streak"])
        return item
    except Exception:
        logger.debug("push_send_stuck_bell failed", exc_info=True)
        return None


__all__ = ["SEND_STUCK_STATUSES", "is_send_stuck_status", "parse_send_stuck_detail",
           "push_send_stuck_bell"]
