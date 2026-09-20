"""B 线语音会话守卫 —— 语种错配 + 语音后孤儿二稿抑制（纯函数为主）。

背景（2026-08-04 智拓 .198）
============================
同会话短窗内：分条语音已达 → 另一条草稿又拆成 reply_bubbles；稍后又发中文语音
「你在干嘛呢？」进英语会话。同调用内 ``delivered_as: voice`` 已 early-return，
管不住的是**跨草稿**竞态与**语种闸只认「译文≠原文」**的盲区（翻译关/失败时
中文人设原文仍可进 TTS）。

本模块两道闸（默认开，安全不变量）：
1. **peer_lang**：客户语种非 CJK 且语音念稿实质性含 CJK → 拒语音回落文字
2. **voice_quiet**：最近出站已是语音/音频，且其后无新入站 → 抑制本轮再发
   （语音或文本/气泡）——「无新客户消息的孤儿二稿」静默消化，不算投递故障

进程内 ``mark_voice_delivered`` 补 DB 镜像写入时差（刚发完、inbox 行尚未可见）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_QUIET_AFTER_SEC = 90.0
_MAX_MARKS = 512
_VOICE_MEDIA = frozenset({"voice", "audio"})

_MARKS: "OrderedDict[str, float]" = OrderedDict()
_MARKS_LOCK = threading.Lock()


def resolve_voice_session_cfg(voice_block: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``inbox.l2_autosend.voice`` 会话守卫块。纯函数。

    - ``peer_lang_gate`` 默认 True（外语客户禁中文语音）
    - ``quiet_after_sec`` 默认 90；``0`` / 负数 = 关静默窗
    """
    vb = voice_block if isinstance(voice_block, dict) else {}
    try:
        quiet = float(vb.get("quiet_after_sec", DEFAULT_QUIET_AFTER_SEC))
    except (TypeError, ValueError):
        quiet = DEFAULT_QUIET_AFTER_SEC
    return {
        "peer_lang_gate": bool(vb.get("peer_lang_gate", True)),
        "quiet_after_sec": quiet,
    }


def voice_peer_lang_conflict(voice_text: str, peer_lang: str) -> str:
    """客户语种 vs 语音念稿冲突 → 短 reason；合规 → ``""``。

    只拦高置信 CJK↔非 CJK（与出站 ``lang_gate`` / ``cjk_substantial`` 同口径），
    不碰 en/es 低置信差异。
    """
    try:
        from src.inbox.outbound_translate import (
            cjk_substantial, lang_is_cjk, normalize_target,
        )
    except Exception:
        return ""
    peer = normalize_target(peer_lang)
    if not peer or lang_is_cjk(peer):
        return ""
    if cjk_substantial(voice_text):
        return "peer_lang_cjk"
    return ""


def _msg_ts(row: Dict[str, Any]) -> float:
    """本条消息**我们何时看见**它；``ingested_at`` 优先于 ``ts``。

    ``ts`` 是展示时间，各平台精度不一，个别平台还会**比真实到达早很多**：
    电脑微信的气泡自己不带时间，驱动只能取气泡上方那条时间分隔条
    （``ts_hint``），于是 13:21 到达的消息被打上 13:19。拿它和出站语音的
    真实投递时刻比，``in_ts > voice_ts`` 恒为假 → 明明是新来信，却被判成
    「语音后的孤儿二稿」整轮抑制（2026-09-20 实锤：客户 13:21 来信，
    13:21:45 英文草稿已生成，13:22:09 被 voice_quiet 吞掉，客户什么也没收到）。

    ``ingested_at`` 是入库墙钟，与 :func:`mark_voice_delivered` 的墙钟同量纲，
    也正是本守卫真正要问的「客户是不是在我们发完语音之后又说话了」。
    """
    for k in ("ingested_at", "ts", "created_ts", "created_at", "timestamp"):
        try:
            v = float(row.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def last_outbound_voice_ts(recent: List[Dict[str, Any]]) -> float:
    """最近出站语音/音频时间戳；无 → 0。纯函数。"""
    best = 0.0
    for row in recent or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("direction") or "").lower() not in ("out", "outbound"):
            continue
        mt = str(row.get("media_type") or "").lower()
        if mt not in _VOICE_MEDIA:
            continue
        best = max(best, _msg_ts(row))
    return best


def last_inbound_ts(recent: List[Dict[str, Any]]) -> float:
    best = 0.0
    for row in recent or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("direction") or "in").lower() not in ("in", "inbound"):
            continue
        best = max(best, _msg_ts(row))
    return best


def mark_voice_delivered(conv_key: str, *, now: Optional[float] = None) -> None:
    """投递成功后登记（补镜像时差）。绝不抛。"""
    key = str(conv_key or "").strip()
    if not key:
        return
    ts = float(now if now is not None else time.time())
    try:
        with _MARKS_LOCK:
            _MARKS[key] = ts
            _MARKS.move_to_end(key)
            while len(_MARKS) > _MAX_MARKS:
                _MARKS.popitem(last=False)
    except Exception:
        pass


def marked_voice_ts(conv_key: str) -> float:
    key = str(conv_key or "").strip()
    if not key:
        return 0.0
    with _MARKS_LOCK:
        return float(_MARKS.get(key) or 0.0)


def clear_voice_marks_for_tests() -> None:
    with _MARKS_LOCK:
        _MARKS.clear()


def should_quiet_after_voice(
    *,
    conv_key: str = "",
    recent_messages: Optional[List[Dict[str, Any]]] = None,
    quiet_after_sec: float = DEFAULT_QUIET_AFTER_SEC,
    now: Optional[float] = None,
) -> str:
    """语音已达且无新入站 → ``voice_quiet``；否则 ``""``。

    判据（任一来源有语音戳即可）：
    - inbox 最近出站语音 ts，或进程内 ``mark_voice_delivered``
    - 该语音之后**没有**更新的入站（客户没再说新话）
    - 距语音发出仍在 ``quiet_after_sec`` 窗内
    """
    try:
        window = float(quiet_after_sec)
    except (TypeError, ValueError):
        window = DEFAULT_QUIET_AFTER_SEC
    if window <= 0:
        return ""
    ts_now = float(now if now is not None else time.time())
    # 进程内投递登记是**本进程真的发出去了**的一手事实，优先于翻库。
    # 取 max 会被镜像行污染：电脑微信屏上那颗「语音5″秒」占位在重扫时可能
    # 被当成新的出站语音行入库（2026-09-20 实锤：12:58 发的两条语音，占位
    # 直到 13:21:28 才扫进来），于是 max 把「刚发过语音」推到了新来信之后，
    # 真来信被判成孤儿二稿吞掉。翻库只在没有登记时兜底（进程刚重启）。
    voice_ts = marked_voice_ts(conv_key)
    if voice_ts <= 0:
        voice_ts = last_outbound_voice_ts(list(recent_messages or []))
    if voice_ts <= 0:
        return ""
    if ts_now - voice_ts > window:
        return ""
    in_ts = last_inbound_ts(list(recent_messages or []))
    # 语音之后又有入站 → 新一轮对话，放行
    if in_ts > voice_ts + 0.05:
        return ""
    return "voice_quiet"


__all__ = [
    "DEFAULT_QUIET_AFTER_SEC",
    "resolve_voice_session_cfg",
    "voice_peer_lang_conflict",
    "last_outbound_voice_ts",
    "last_inbound_ts",
    "mark_voice_delivered",
    "marked_voice_ts",
    "clear_voice_marks_for_tests",
    "should_quiet_after_voice",
]
