"""入站表情回应（Reaction）轻量跟进（Phase4）。

对端给**我方出站消息**点正向表情（❤️👍…）→ 短延迟经多平台 deferred 队列回一句
轻量确认（像真人看到回应后顺口接一句），带会话冷却 + 主动情绪护栏，默认关。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.integrations.companion_proactive import JsonCooldownStore
from src.integrations.shared.deferred_outbox import shift_out_of_quiet_hours
from src.utils.wellbeing_guard import proactive_emotion_gate

logger = logging.getLogger(__name__)

_DEFAULT_POSITIVE = (
    "❤", "❤️", "♥️", "👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿",
    "😂", "🤣", "🙏", "😍", "🥰", "💕", "💖", "💗", "💓", "💞",
    "✨", "🎉", "👏", "🤗", "😊", "🙂", "☺️", "😘", "💋", "🔥",
)

_FOLLOWUP_BY_LANG: Dict[str, Dict[str, List[str]]] = {
    "zh": {
        "❤": ["看到你回应了，心里暖暖的～", "嘿嘿，收到你的心意啦"],
        "👍": ["收到你的赞啦～", "谢谢认可呀，开心"],
        "😂": ["哈哈，看来戳中笑点了", "你笑我也跟着开心～"],
        "default": ["看到你的回应啦～", "嘿嘿，收到～"],
    },
    "en": {
        "❤": ["Aww, thanks for the love!", "That means a lot — glad you liked it"],
        "👍": ["Got your thumbs-up — thanks!", "Appreciate that!"],
        "😂": ["Haha glad that landed!", "Your laugh made my day"],
        "default": ["Saw your reaction — thanks!", "Hehe, noted!"],
    },
}


def parse_reaction_followup_cfg(companion_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``companion.reaction_followup`` 配置块。"""
    comp = companion_cfg or {}
    rf = comp.get("reaction_followup") if isinstance(comp.get("reaction_followup"), dict) else {}
    rf = rf or {}
    emojis = rf.get("positive_emojis")
    if not isinstance(emojis, list) or not emojis:
        emojis = list(_DEFAULT_POSITIVE)
    platforms = rf.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        platforms = ["whatsapp"]
    return {
        "enabled": bool(rf.get("enabled", False)),
        "platforms": [str(p).lower() for p in platforms],
        "positive_emojis": frozenset(str(e) for e in emojis if str(e).strip()),
        "cooldown_hours": float(rf.get("cooldown_hours", 6)),
        "defer_sec": float(rf.get("defer_sec", 45)),
        "staleness_sec": float(rf.get("staleness_sec", 3600)),
        "quiet_start_hour": float(rf.get("quiet_start_hour", 23)),
        "quiet_end_hour": float(rf.get("quiet_end_hour", 8)),
        "cooldown_path": str(rf.get("cooldown_path") or "reaction_followup_cooldown.json"),
    }


def is_positive_reaction(emoji: str, *, positive_set: frozenset) -> bool:
    e = str(emoji or "").strip()
    if not e:
        return False
    if e in positive_set:
        return True
    # 肤色变体：剥 skin tone 再比基码
    base = e
    for mod in ("\U0001f3fb", "\U0001f3fc", "\U0001f3fd", "\U0001f3fe", "\U0001f3ff"):
        base = base.replace(mod, "")
    return base in positive_set


def _emoji_bucket(emoji: str) -> str:
    e = str(emoji or "").strip()
    for mod in ("\U0001f3fb", "\U0001f3fc", "\U0001f3fd", "\U0001f3fe", "\U0001f3ff"):
        e = e.replace(mod, "")
    if e.startswith("❤") or e in ("♥", "♥️"):
        return "❤"
    if e.startswith("👍"):
        return "👍"
    if e in ("😂", "🤣"):
        return "😂"
    return "default"


def build_followup_text(emoji: str, *, lang: str = "zh", seed: str = "") -> str:
    """确定性选一句轻量跟进（crc32 轮换，同会话同 emoji 稳定）。"""
    lang_key = str(lang or "zh").lower()[:2]
    bucket = _emoji_bucket(emoji)
    pool = (_FOLLOWUP_BY_LANG.get(lang_key) or _FOLLOWUP_BY_LANG["zh"])
    variants = pool.get(bucket) or pool.get("default") or _FOLLOWUP_BY_LANG["zh"]["default"]
    if len(variants) == 1:
        return variants[0]
    import zlib
    key = f"{seed}:{emoji}:{lang_key}"
    idx = zlib.crc32(key.encode("utf-8")) % len(variants)
    return variants[idx]


def should_schedule_reaction_followup(
    *,
    sender: str,
    emoji: str,
    direction: str,
    platform: str,
    chat_type: str,
    cfg: Dict[str, Any],
    cooldown_ts: float,
    now: float,
    emotion_gate: str = "",
) -> str:
    """判断是否应跟进；返回空串=可发，非空=跳过原因（可观测）。"""
    if not cfg.get("enabled"):
        return "disabled"
    plat = str(platform or "").lower()
    if plat not in cfg.get("platforms", []):
        return "platform"
    if str(sender or "").strip().lower() in ("me", "self", ""):
        return "self_reaction"
    if str(chat_type or "").strip().lower() == "group":
        return "group_chat"
    if str(direction or "").lower() != "out":
        return "not_our_message"
    if not is_positive_reaction(emoji, positive_set=cfg["positive_emojis"]):
        return "not_positive"
    if emotion_gate == "block":
        return "emotion_block"
    cd_h = float(cfg.get("cooldown_hours", 6))
    if cooldown_ts > 0 and (float(now) - cooldown_ts) < cd_h * 3600.0:
        return "cooldown"
    return ""


def _resolve_lang(store: Any, conversation_id: str) -> str:
    try:
        conv = store.get_conversation(conversation_id) if store else None
        lang = str((conv or {}).get("peer_language") or (conv or {}).get("language") or "")
        if lang:
            return lang[:2].lower()
    except Exception:
        pass
    return "zh"


def _emotion_gate_for_conv(store: Any, conversation_id: str, *, now: float) -> str:
    try:
        meta = store.get_conv_meta(conversation_id) if store else None
    except Exception:
        meta = None
    last_emotion = str((meta or {}).get("last_emotion") or "")
    intensity = (meta or {}).get("last_emotion_intensity")
    try:
        intensity_f = float(intensity) if intensity is not None else None
    except (TypeError, ValueError):
        intensity_f = None
    return proactive_emotion_gate(
        None,
        now=now,
        last_emotion=last_emotion,
        last_emotion_intensity=intensity_f,
    )


def schedule_reaction_followup(
    *,
    store: Any,
    deferred_dispatcher: Any,
    config: Dict[str, Any],
    config_dir: Path,
    platform: str,
    account_id: str,
    chat_key: str,
    target_id: str,
    emoji: str,
    sender: str,
    chat_type: str = "",
    now: Optional[float] = None,
) -> int:
    """尝试把 reaction 跟进入 deferred 队列。返回 row_id（0=未入队）。"""
    ts = float(now if now is not None else time.time())
    companion = (config or {}).get("companion") or {}
    cfg = parse_reaction_followup_cfg(companion)
    cid = f"{str(platform).lower()}:{account_id}:{chat_key}"

    direction = ""
    try:
        direction = store.get_message_direction(cid, str(target_id)) if store else ""
    except Exception:
        logger.debug("[reaction_followup] direction 查询失败", exc_info=True)

    cd_path = config_dir / str(cfg.get("cooldown_path") or "reaction_followup_cooldown.json")
    cd_store = JsonCooldownStore(cd_path)
    last_cd = float(cd_store.snapshot().get(cid) or 0)
    gate = _emotion_gate_for_conv(store, cid, now=ts)

    skip = should_schedule_reaction_followup(
        sender=sender, emoji=emoji, direction=direction, platform=platform,
        chat_type=chat_type, cfg=cfg, cooldown_ts=last_cd, now=ts,
        emotion_gate=gate,
    )
    if skip:
        logger.debug(
            "[reaction_followup] skip=%s plat=%s ck=%s emoji=%r",
            skip, platform, chat_key, emoji,
        )
        return 0

    if deferred_dispatcher is None:
        return 0
    outbox = getattr(deferred_dispatcher, "_store", None)
    if outbox is None:
        return 0

    lang = _resolve_lang(store, cid)
    text = build_followup_text(emoji, lang=lang, seed=cid)
    defer_until = ts + float(cfg.get("defer_sec", 45))
    defer_until = shift_out_of_quiet_hours(
        defer_until,
        start_hour=float(cfg.get("quiet_start_hour", 23)),
        end_hour=float(cfg.get("quiet_end_hour", 8)),
    )

    try:
        row_id = outbox.enqueue(
            platform=str(platform).lower(),
            account_id=str(account_id or "default"),
            chat_key=str(chat_key),
            reply_text=text,
            defer_until=defer_until,
            reason=f"reaction_followup:{emoji}",
            staleness_sec=float(cfg.get("staleness_sec", 3600)),
            extra={"target_id": str(target_id), "emoji": str(emoji)},
        )
    except Exception:
        logger.debug("[reaction_followup] enqueue 失败", exc_info=True)
        return 0

    if row_id:
        cd_store.mark(cid, ts)
        logger.info(
            "[reaction_followup] 已入队 id=%s plat=%s ck=%s emoji=%r defer=%.0fs",
            row_id, platform, chat_key, emoji, defer_until - ts,
        )
    return int(row_id or 0)


__all__ = [
    "parse_reaction_followup_cfg",
    "is_positive_reaction",
    "build_followup_text",
    "should_schedule_reaction_followup",
    "schedule_reaction_followup",
]
