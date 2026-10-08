# -*- coding: utf-8 -*-
"""客户指标里不算数的对端。

合成演练号段永远剔除。开着的双向演练对、登记在 ``chatx_peers`` 里的跨实例智聊号，
由 :func:`omit_internal_peers` 在读报表的这一次调用里额外剔除。

不从收件箱列表和未读徽标里拿掉这两类：演示要看着那场聊，坐席也要打得开审稿。
未读徽标和「点数字看到的清单」必须同口径，拆开就是幽灵未读。合成号段没有这场
演示，徽标和清单可以一起跳过。
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Any, List, Optional, Tuple

from src.ai.llm_purpose import is_drill_id

_OMIT: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "omit_internal_peers", default=frozenset())
_OMIT_CIDS: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "omit_handshake_cids", default=frozenset())
_HANDSHAKES: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "omit_handshake_holds", default=())


def is_synthetic_drill(chat_key: Any) -> bool:
    """``990001000``–``990001999``。与成本归因的演练号段同一段。"""
    return is_drill_id(chat_key)


def exclude_peer_ids(config: Optional[dict]) -> List[str]:
    """报表要跳过的对端 id。演练号段不在这里，SQL 用号段判断。"""
    ids = set()
    try:
        from src.inbox.duplex_drill import parse_cfg
        cfg = parse_cfg(config if isinstance(config, dict) else {})
        if cfg.get("enabled"):
            for a, b in cfg.get("pairs") or []:
                if a:
                    ids.add(str(a))
                if b:
                    ids.add(str(b))
    except Exception:
        pass
    try:
        from src.inbox.peer_bot_guard import chatx_peer_ids
        ids |= set(chatx_peer_ids(config if isinstance(config, dict) else {}))
    except Exception:
        pass
    return sorted(i for i in ids if i and not is_synthetic_drill(i))


def handshake_holds(inbox_store: Any) -> List[dict]:
    """当前粘住握手指纹的私聊。回声不在这里：两个真人也可能互相改写。"""
    fn = getattr(inbox_store, "list_app_settings", None)
    if not callable(fn):
        return []
    prefix = "chatx_peer_hold:"
    try:
        rows = fn(prefix) or []
    except Exception:
        return []
    out: List[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("value") or "") != "chatx_handshake":
            continue
        key = str(row.get("key") or "")
        cid = key[len(prefix):] if key.startswith(prefix) else ""
        parts = cid.split(":", 2)
        ck = parts[2] if len(parts) == 3 else ""
        if not cid or not ck or is_synthetic_drill(ck):
            continue
        out.append({
            "conversation_id": cid,
            "chat_key": ck,
            "reason": "chatx_handshake",
        })
    return out


@contextlib.contextmanager
def omit_internal_peers(config: Optional[dict], inbox_store: Any = None):
    """这一次报表读取跳过演练对、登记的智聊号，以及当前握手指纹的这一场。

    握手指纹按会话剔除，不按对端。同一个人在销售号上的目标仍计入完成率。
    """
    ids = set(exclude_peer_ids(config))
    holds = handshake_holds(inbox_store)
    cids = {str(h.get("conversation_id") or "") for h in holds}
    token = _OMIT.set(frozenset(i for i in ids if i))
    token_c = _OMIT_CIDS.set(frozenset(c for c in cids if c))
    token_h = _HANDSHAKES.set(tuple(holds))
    try:
        yield
    finally:
        _HANDSHAKES.reset(token_h)
        _OMIT_CIDS.reset(token_c)
        _OMIT.reset(token)


def excluded_internal_view(goal_store: Any, since_ts: float) -> dict:
    """完成率剔除了哪些会话。握手粘住但没有终态目标的也算一场，点开能看见。"""
    holds = [h for h in _HANDSHAKES.get() if isinstance(h, dict)]
    hold_keys = {str(h.get("chat_key") or "") for h in holds}
    try:
        terminal = goal_store.excluded_terminal(since_ts, sorted(_OMIT.get()))
    except Exception:
        terminal = []
    seen = set()
    rows: List[dict] = []
    for h in holds:
        cid = str(h.get("conversation_id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        rows.append({
            "conversation_id": cid,
            "chat_key": str(h.get("chat_key") or ""),
            "reason": "chatx_handshake",
        })
    for r in terminal or []:
        cid = str(r.get("conversation_id") or "")
        ck = str(r.get("chat_key") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        if is_synthetic_drill(ck):
            reason = "synthetic"
        elif ck in hold_keys:
            reason = "chatx_handshake"
        else:
            reason = "internal_peer"
        rows.append({
            "conversation_id": cid, "chat_key": ck, "reason": reason,
        })
    return {"n": len(rows), "rows": rows[:20]}


def internal_peer_predicate(alias: str = "") -> Tuple[str, List[str]]:
    """可拼进 WHERE 的条件（不含前导 AND）和对应参数。

    合成号段始终在。上下文里还有对端 id 时再加 ``NOT IN``，最多 200 个。
    """
    col = f"{alias}.chat_key" if alias else "chat_key"
    cid_col = f"{alias}.conversation_id" if alias else "conversation_id"
    pred = f"NOT ({col} GLOB '990001[0-9][0-9][0-9]')"
    params: List[str] = []
    ids = [i for i in _OMIT.get() if i][:200]
    if ids:
        pred += f" AND {col} NOT IN (" + ",".join("?" * len(ids)) + ")"
        params.extend(ids)
    cids = [c for c in _OMIT_CIDS.get() if c][:200]
    if cids:
        pred += f" AND {cid_col} NOT IN (" + ",".join("?" * len(cids)) + ")"
        params.extend(cids)
    return pred, params
