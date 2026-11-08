"""把收件箱里已经落库的往来，收成 decide 用的历史。

电脑上的 messages 表是对话记录。decide 以前固定传 ``history=[]``，
多轮只能靠调用方自己记得。这里按会话 id 读最近若干条，转成
``{role, content}``，并拿掉末尾那条和「当前待回复」重复的入站，
避免同一句话在历史和 last_inbound 里各出现一次。

读不到库、会话还没有消息、查询失败，都返回空列表，调用方行为与
「没有历史」时一致。
"""

from __future__ import annotations

from typing import Any, Dict, List


def load_reply_history(
    store: Any,
    conversation_id: str,
    last_inbound: str = "",
    *,
    limit: int = 30,
) -> List[Dict[str, str]]:
    cid = str(conversation_id or "").strip()
    if store is None or not cid or not hasattr(store, "list_recent_messages"):
        return []
    try:
        rows = store.list_recent_messages(
            cid, limit=max(1, int(limit or 30)), include_deleted=False,
        )
    except TypeError:
        try:
            rows = store.list_recent_messages(cid, limit=max(1, int(limit or 30)))
        except Exception:
            return []
    except Exception:
        return []
    try:
        from src.inbox.persona_reply import normalize_history
        history, _last = normalize_history(rows or [])
    except Exception:
        return []
    inbound = str(last_inbound or "").strip()
    if (
        history
        and str(history[-1].get("role") or "") == "user"
        and str(history[-1].get("content") or "") == inbound
    ):
        history = history[:-1]
    out: List[Dict[str, str]] = []
    for row in history:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out
