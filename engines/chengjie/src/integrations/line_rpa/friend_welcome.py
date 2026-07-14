"""LINE 好友接受后主动欢迎（Phase16）。

接受好友申请 → 入队 companion 风格问候（复用 send_queue 投递链），
与 inbox bootstrap（auto_ai）互补：此处是「我方先发第一句」。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from src.inbox.greeting import get_time_slot, select_greeting_text

logger = logging.getLogger(__name__)

# 陪伴口吻（非客服）；可被 config 覆写
_COMPANION_WELCOME: Dict[str, Dict[str, str]] = {
    "zh": {
        "morning": "嗨～终于加上好友啦，早上好呀 ☀️",
        "afternoon": "嗨～加上好友啦，下午好呀",
        "evening": "嗨～加上好友啦，晚上好呀",
        "night": "嗨～加上好友啦，这么晚还没睡呀",
    },
    "en": {
        "morning": "Hey! So glad we're connected now ☀️ How's your morning?",
        "afternoon": "Hey! Glad we're friends now — how's your day going?",
        "evening": "Hey! Nice to connect — how's your evening?",
        "night": "Hey! Glad we connected — still up?",
    },
    "ja": {
        "morning": "やっと友だちになれたね！おはよう ☀️",
        "afternoon": "やっと友だちになれたね！こんにちは",
        "evening": "やっと友だちになれたね！こんばんは",
        "night": "やっと友だちになれたね！まだ起きてる？",
    },
}


def parse_welcome_cfg(auto_accept_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    aa = auto_accept_cfg or {}
    w = aa.get("welcome") if isinstance(aa.get("welcome"), dict) else {}
    return {
        "enabled": bool((w or {}).get("enabled", False)),
        "lang": str((w or {}).get("lang") or "").strip().lower(),
        "scene": str((w or {}).get("scene") or "companion_line_welcome"),
        "texts": dict((w or {}).get("texts") or {}),
    }


def build_welcome_text(
    *,
    lang: str,
    welcome_cfg: Optional[Dict[str, Any]] = None,
    templates_store=None,
) -> str:
    """生成 LINE 新好友欢迎语。"""
    wc = welcome_cfg or {}
    slot = get_time_slot()
    lang_key = str(lang or "zh").lower()[:2]
    overrides = wc.get("texts") if isinstance(wc.get("texts"), dict) else {}
    if isinstance(overrides.get(lang_key), str) and overrides[lang_key].strip():
        return str(overrides[lang_key]).strip()
    if isinstance(overrides.get(lang), str) and overrides[lang].strip():
        return str(overrides[lang]).strip()

    scene = str(wc.get("scene") or "companion_line_welcome")
    if templates_store is not None:
        try:
            tpl = select_greeting_text(
                lang_key, slot, templates_store, custom_scene=scene)
            if tpl and "客服" not in tpl and "assist" not in tpl.lower():
                return tpl
        except Exception:
            pass

    slot_map = _COMPANION_WELCOME.get(lang_key) or _COMPANION_WELCOME["zh"]
    return slot_map.get(slot, slot_map.get("night", "嗨～加上好友啦"))


def _meta_key(peer_name: str) -> str:
    return f"friend_welcome:{str(peer_name or '').strip()[:80]}"


def already_welcomed(state_store: Any, peer_name: str) -> bool:
    if state_store is None or not peer_name:
        return True
    try:
        meta = state_store.get_meta(_meta_key(peer_name))
        return bool(meta)
    except Exception:
        return False


def mark_welcomed(state_store: Any, peer_name: str, *, queue_id: int = 0) -> None:
    if state_store is None or not peer_name:
        return
    try:
        state_store.set_meta(_meta_key(peer_name), {
            "ts": time.time(),
            "queue_id": int(queue_id or 0),
        })
    except Exception:
        logger.debug("mark_welcomed failed", exc_info=True)


def enqueue_friend_welcome(
    state_store: Any,
    *,
    peer_name: str,
    text: str,
) -> int:
    """入队欢迎语；返回 queue id，0 表示未入队。"""
    if state_store is None or not peer_name or not str(text or "").strip():
        return 0
    if already_welcomed(state_store, peer_name):
        return 0
    try:
        qid = state_store.enqueue_send(
            chat_key=str(peer_name),
            peer_name=str(peer_name),
            text=str(text).strip(),
            created_by="friend_welcome",
        )
        mark_welcomed(state_store, peer_name, queue_id=qid)
        return int(qid or 0)
    except Exception:
        logger.debug("enqueue_friend_welcome failed", exc_info=True)
        return 0


__all__ = [
    "parse_welcome_cfg",
    "build_welcome_text",
    "enqueue_friend_welcome",
    "already_welcomed",
]
