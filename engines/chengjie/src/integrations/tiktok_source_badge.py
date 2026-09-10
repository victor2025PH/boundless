"""TikTok 会话来源徽标（TK-3 E3，2026-09-11）。

同一 ``tiktok:user:<username>`` 会话键刻意与官方私信 worker 同键（两路并存时合并成一条会话），
所以列表分不出「这条是官方 API 进来的、还是真机桥 / 网页边车读来的」——坐席看到发送被拦
（消息请求未回 / 真机离线）时不知道该去修哪条路。本模块按**账号 mode** 给 tiktok 会话行打
``tiktok_source``：``official`` / ``personal_rpa`` / ``web``；评论线索键 ``tiktok:comment:*`` 恒为真机。

个人号两路另打 ``peer_never_replied``：会话从未有对方入站（``conversations.last_in_ts == 0``）——
这正是桥闸门 ``policy_message_request_pending`` 拒发的条件，列表提前挂「消息请求 · 对方未回」，
坐席不必点进去按发送才知道发不出。官方号不打（官方私信有 48h 窗口语义，另有一套提示）。

只对 ``platform == tiktok`` 的行工作；其它平台零改动零开销。注册表 / 会话库任何异常都吞掉，
徽标缺席不影响列表。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

PLATFORM = "tiktok"
SOURCE_OFFICIAL = "official"
SOURCE_RPA = "personal_rpa"
SOURCE_WEB = "web"
SOURCES = (SOURCE_OFFICIAL, SOURCE_RPA, SOURCE_WEB)
COMMENT_PREFIX = "tiktok:comment:"
PERSONAL_SOURCES = (SOURCE_RPA, SOURCE_WEB)


def source_for(chat_key: str, mode: str) -> str:
    """会话键 + 账号 mode → 来源；评论线索恒真机；未知 mode → 空（不打徽标，不猜）。"""
    if str(chat_key or "").startswith(COMMENT_PREFIX):
        return SOURCE_RPA
    m = str(mode or "").strip().lower()
    return m if m in SOURCES else ""


def annotate_tiktok_sources(chats: Iterable[Dict[str, Any]], *, registry: Any = None, store: Any = None) -> int:
    """就地给 tiktok 会话行加 ``tiktok_source`` / ``peer_never_replied``；返回打了来源的行数。

    ``registry``：``get(platform, account_id) -> row|None``（缺省进程单例）；``store``：``get_conversation(cid)``
    （缺省不查 → 不打 peer_never_replied）。每账号只查注册表一次。
    """
    rows = [c for c in chats if isinstance(c, dict) and str(c.get("platform") or "").lower() == PLATFORM]
    if not rows:
        return 0
    reg = registry
    if reg is None:
        try:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
        except Exception:
            logger.debug("[tiktok-badge] 注册表不可用", exc_info=True)
            reg = None
    modes: Dict[str, str] = {}
    n = 0
    for c in rows:
        aid = str(c.get("account_id") or "")
        if aid not in modes:
            mode = ""
            if reg is not None:
                try:
                    row = reg.get(PLATFORM, aid) or {}
                    mode = str(row.get("mode") or "")
                except Exception:
                    logger.debug("[tiktok-badge] 读账号 mode 失败 %s", aid, exc_info=True)
            modes[aid] = mode
        src = source_for(str(c.get("chat_key") or ""), modes[aid])
        if not src:
            continue
        c["tiktok_source"] = src
        n += 1
        if src in PERSONAL_SOURCES and store is not None and c.get("conversation_id"):
            try:
                conv = store.get_conversation(str(c["conversation_id"])) or {}
                if float(conv.get("last_in_ts") or 0) <= 0:
                    c["peer_never_replied"] = True
            except Exception:
                logger.debug("[tiktok-badge] 读会话 last_in_ts 失败", exc_info=True)
    return n


__all__ = ["SOURCE_OFFICIAL", "SOURCE_RPA", "SOURCE_WEB", "SOURCES", "PERSONAL_SOURCES", "source_for",
           "annotate_tiktok_sources"]
