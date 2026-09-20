"""已发媒体事实账本（B 线，2026-09-18 「从没给你发过照片」事故沉淀）。

事故：inbox 里明明有我方出站图片行，AI 却对客户说「我从来没给你发过照片」——
A 线的 ``_media_sent_log`` 只记 A 线自己发的图，B 线持久 user_context 里它是空的，
于是「你最近发过的照片」块根本不注入，LLM 只能凭窗口里剩下的几条历史瞎猜。
inbox 才是两条线合流后的**唯一真相**：出站行 ``direction=out`` + ``media_type``。

本模块：
- :func:`wants_media_history`：客户这条是不是在聊「你发过/没发过照片」这件事（保守
  词表；不相关就不扫库、零 token）；
- :func:`summarize_outbound_media`：出站行 → {image, voice, video 计数, first_ts, last_ts,
  last_caption}；
- :func:`build_media_ledger_block`：摘要 → 注入块（发过就别否认；没发过就别认领）。
纯函数 + 一处 store 读；异常返回 ""。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

_IMAGE_KINDS = frozenset({"image", "photo", "picture", "sticker", "selfie"})
_VOICE_KINDS = frozenset({"voice", "audio"})
_VIDEO_KINDS = frozenset({"video", "video_note", "animation", "gif"})

_WANTS_RE = re.compile(
    r"(照片|相片|图片|自拍|发过.{0,4}(图|照|片)|没(有)?发过|发张|发个.{0,2}(图|照)|"
    r"看看你|长什么样|長什麼樣|你的样子|上次(那|发的)(张|图|照)|"
    r"photo|picture|pic\b|selfie|send\s+me\s+(a|your)|what\s+you\s+look\s+like|"
    r"语音|語音|voice)",
    re.IGNORECASE,
)
_LABEL_RE = re.compile(r"^\s*\[[^\]\n]{1,30}\]\s*")


def wants_media_history(text: str) -> bool:
    t = str(text or "").strip()
    return bool(t) and len(t) <= 300 and bool(_WANTS_RE.search(t))


def _is_out(r: Dict[str, Any]) -> bool:
    role = str(r.get("role") or "")
    if role:
        return role == "assistant"
    return str(r.get("direction") or "") in ("out", "outbound", "outgoing")


def _ts(r: Dict[str, Any]) -> float:
    try:
        return float(r.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def summarize_outbound_media(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"image": 0, "voice": 0, "video": 0, "first_ts": 0.0,
                           "last_ts": 0.0, "last_caption": "", "last_kind": ""}
    for r in rows or []:
        if not isinstance(r, dict) or not _is_out(r):
            continue
        mt = str(r.get("media_type") or "").strip().lower()
        if not mt:
            continue
        if mt in _IMAGE_KINDS:
            kind = "image"
        elif mt in _VOICE_KINDS:
            kind = "voice"
        elif mt in _VIDEO_KINDS:
            kind = "video"
        else:
            continue
        out[kind] += 1
        ts = _ts(r)
        if ts > 0:
            if not out["first_ts"] or ts < out["first_ts"]:
                out["first_ts"] = ts
            if ts >= out["last_ts"]:
                out["last_ts"] = ts
                out["last_kind"] = kind
                cap = _LABEL_RE.sub("", str(r.get("text") or "")).strip()
                out["last_caption"] = cap[:40]
    return out


def build_media_ledger_block(summary: Dict[str, Any], *, now: Optional[float] = None) -> str:
    s = summary or {}
    n_img, n_voice, n_video = int(s.get("image") or 0), int(s.get("voice") or 0), int(s.get("video") or 0)
    if n_img + n_voice + n_video == 0:
        return (
            "【已发媒体事实】在这个会话里你**还没有**发过任何照片/语音/视频。"
            "别说「上次发给你那张」「我发过呀」这类话；对方问起就按没发过来接。"
        )
    parts: List[str] = []
    if n_img:
        parts.append(f"照片 {n_img} 张")
    if n_voice:
        parts.append(f"语音 {n_voice} 条")
    if n_video:
        parts.append(f"视频 {n_video} 个")
    now_ts = float(now or time.time())
    when = ""
    try:
        from src.inbox.time_context import describe_age
        if s.get("last_ts"):
            when = describe_age(max(0.0, now_ts - float(s["last_ts"])))
    except Exception:
        when = ""
    kind_word = {"image": "照片", "voice": "语音", "video": "视频"}.get(str(s.get("last_kind") or ""), "")
    tail = f"，最近一次是约 {when}前发的{kind_word}" if when and when != "不到 1 小时" else ""
    cap = str(s.get("last_caption") or "").strip()
    if cap:
        tail += f"（当时配文「{cap}」）"
    return (
        f"【已发媒体事实】在这个会话里你一共发过：{'、'.join(parts)}{tail}。"
        "对方问「你发过照片没 / 上次那张」时**不要否认**发过；也别把之前发的说成刚拍的、"
        "别编造画面里没有的细节（不知道画面就含糊带过）。"
    )


def media_ledger_from_store(
    store: Any, conversation_id: str, text: str, *, scan_limit: int = 500,
    now: Optional[float] = None,
) -> str:
    """接线：相关问句 → 扫 inbox 出站媒体行 → 注入块；不相关 / 无 store / 异常 → ""。"""
    if store is None or not str(conversation_id or "").strip() or not wants_media_history(text):
        return ""
    try:
        cid = str(conversation_id).strip()
        try:
            rows = list(store.list_recent_messages(cid, limit=int(scan_limit), include_deleted=False) or [])
        except TypeError:
            rows = list(store.list_recent_messages(cid, limit=int(scan_limit)) or [])
        return build_media_ledger_block(summarize_outbound_media(rows), now=now)
    except Exception:
        return ""


def apply_media_ledger_hint(
    user_context: Optional[Dict[str, Any]],
    store: Any,
    conversation_id: str,
    text: str,
    *,
    now: Optional[float] = None,
) -> str:
    """把 inbox 已发媒体事实写入 ``_topic_switch_hint``（A/B 线共用）。

    2026-09-18 单源：不再看 ``_media_sent_log`` 空不空——那本账只记本进程自己发的，
    坐席手发 / B 线自动发都在 inbox 里。相关问句一律以 inbox 为准；块头已在 hint
    里则不叠。返回写入的块（未写 → ""）。
    """
    if not isinstance(user_context, dict):
        return ""
    prev = str(user_context.get("_topic_switch_hint") or "")
    if "【已发媒体事实】" in prev:
        return ""
    block = media_ledger_from_store(store, conversation_id, text, now=now)
    if not block:
        return ""
    user_context["_topic_switch_hint"] = (
        f"{prev.strip()}\n{block}" if prev.strip() else block)
    return block


__all__ = [
    "wants_media_history", "summarize_outbound_media", "build_media_ledger_block",
    "media_ledger_from_store", "apply_media_ledger_hint",
]
