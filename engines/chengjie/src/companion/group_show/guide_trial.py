"""记下客户做了向导点名的那一项功能。

只写事件账，不写进聊天正文，也不发私聊。漏斗从这本账读，口径仍在
:mod:`src.companion.group_show.funnel`。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def note_feature_used(
    store: Any,
    *,
    account_id: str,
    user_id: str,
    feature: str,
    ts: Optional[float] = None,
    platform: str = "",
) -> bool:
    """客户对这个号做了 ``feature``。没先私聊过来、或功能 id 不合法 → 不记。"""
    fn = getattr(store, "record_guide_trial", None)
    if not callable(fn):
        return False
    return bool(fn(
        account_id=account_id,
        user_id=user_id,
        feature=feature,
        ts=ts,
        platform=platform,
    ))


def note_feature_for_conversation(
    store: Any,
    app_config: Any,
    *,
    conversation_id: str,
    feature: str,
    ts: Optional[float] = None,
) -> bool:
    """落地页回传「这个会话的人做了这项功能」。

    只认配置里的向导号。没配向导号、号对不上、或钉死的功能对不上，都不记。
    不改聊天记录。对方还没先私聊过来时，底层同样不记。
    """
    try:
        from src.companion.group_show.funnel import (
            configured_guide_account,
            configured_guide_feature,
            trial_marker,
        )
        marker = trial_marker(feature)
        if not marker:
            return False
        feat = marker.split(":", 1)[1]
        guide = configured_guide_account(app_config)
        if not guide:
            return False
        pinned = configured_guide_feature(app_config)
        if pinned and pinned != feat:
            return False
        cid = str(conversation_id or "").strip()
        account = ""
        user = ""
        platform = ""
        getter = getattr(store, "get_conversation", None)
        row = getter(cid) if callable(getter) and cid else None
        if isinstance(row, dict):
            account = str(row.get("account_id") or "").strip()
            user = str(row.get("chat_key") or "").strip()
            platform = str(row.get("platform") or "").strip()
        parts = cid.split(":")
        if len(parts) >= 3:
            platform = platform or parts[0].strip()
            account = account or parts[1].strip()
            user = user or ":".join(parts[2:]).strip()
        if account != guide or not user:
            return False
        return note_feature_used(
            store, account_id=account, user_id=user, feature=feat,
            ts=ts, platform=platform)
    except Exception:
        logger.debug("[guide_trial] 按会话记功能失败", exc_info=True)
        return False


def note_feature_for_handle(
    store: Any,
    app_config: Any,
    *,
    username: str,
    feature: str,
    ts: Optional[float] = None,
) -> bool:
    """对方用 Telegram 用户名做完了这项功能。只在向导号的私聊里对得上才记。"""
    try:
        from src.companion.group_show.funnel import configured_guide_account
        guide = configured_guide_account(app_config)
        finder = getattr(store, "find_private_conversation_id", None)
        if not guide or not callable(finder):
            return False
        cid = str(finder(account_id=guide, username=username) or "")
        if not cid:
            return False
        return note_feature_for_conversation(
            store, app_config, conversation_id=cid, feature=feature, ts=ts)
    except Exception:
        logger.debug("[guide_trial] 按用户名记功能失败", exc_info=True)
        return False
