"""automation_mode bootstrap / 接管 / 自动接回 观测（进程内计数）。"""
from __future__ import annotations

import threading
from typing import Any, Dict

_lock = threading.Lock()
_bootstrap_total = 0
_last_bootstrap: Dict[str, Any] = {}
_takeover_total = 0
_rearm_total = 0
_last_takeover_cid = ""


def record_bootstrap(*, platform: str = "", conversation_id: str = "") -> None:
    global _bootstrap_total, _last_bootstrap
    with _lock:
        _bootstrap_total += 1
        _last_bootstrap = {
            "platform": str(platform or ""),
            "conversation_id": str(conversation_id or ""),
        }


def record_takeover(*, conversation_id: str = "") -> None:
    """坐席手动出站触发「接管即静音」一次（takeover_rearm.record_agent_takeover）。"""
    global _takeover_total, _last_takeover_cid
    with _lock:
        _takeover_total += 1
        _last_takeover_cid = str(conversation_id or "")


def record_rearm(*, count: int = 1) -> None:
    """自动接回恢复了 N 个会话（takeover_rearm.sweep_takeover_rearm）。"""
    global _rearm_total
    with _lock:
        _rearm_total += max(0, int(count or 0))


_handoff_total = 0
_handoff_with_media = 0
_last_handoff: Dict[str, Any] = {}


def record_handoff_note(*, conversation_id: str = "", out_n: int = 0, in_n: int = 0,
                        media_n: int = 0) -> None:
    """manual → AI 切换时成功构建了一份接力摘要（handoff_memory.record_handoff）。"""
    global _handoff_total, _handoff_with_media, _last_handoff
    with _lock:
        _handoff_total += 1
        if int(media_n or 0) > 0:
            _handoff_with_media += 1
        _last_handoff = {
            "conversation_id": str(conversation_id or ""),
            "out_n": int(out_n or 0), "in_n": int(in_n or 0), "media_n": int(media_n or 0),
        }


def metrics_snapshot() -> Dict[str, Any]:
    with _lock:
        return {
            "bootstrap_total": _bootstrap_total,
            "last": dict(_last_bootstrap),
            "takeover_total": _takeover_total,
            "rearm_total": _rearm_total,
            "last_takeover_cid": _last_takeover_cid,
            # 接力记忆 P0-3：切回 AI 时构建的接力摘要数 / 含媒体的份数
            "handoff_total": _handoff_total,
            "handoff_with_media": _handoff_with_media,
            "last_handoff": dict(_last_handoff),
        }


__all__ = [
    "record_bootstrap",
    "record_takeover",
    "record_rearm",
    "record_handoff_note",
    "metrics_snapshot",
]
