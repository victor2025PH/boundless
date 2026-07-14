# -*- coding: utf-8 -*-
"""AI 接管状态 — 真人接管期间, worker 不再 AI 自动回复.

当真人在后台按"我接手"时, 调 ``mark_taken_over(peer_name, device_id, by_username)``
让 worker 端 ``_ai_reply_and_send`` 入口短路. 真人完成 / 释放时调 ``release``.

存储: 内存 dict + threading.Lock + TTL (避免遗忘 release 永久 stuck).
PR-6 接 agent_mesh listener 后, mark/release 也会被远端命令触发.

边界:
- 进程内单例, 单 worker 进程范围有效
- 真人在主电脑后台按按钮 → 主电脑 agent_mesh.send_message(to=worker_id, cmd=...)
  → worker poll → mark_taken_over (PR-6 实施)
- TTL 默认 1h, 防遗忘释放. 真人长聊可手动 extend.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

DEFAULT_TTL_SEC = 3600.0  # 1h

_state: Dict[Tuple[str, str], Dict[str, float]] = {}
_lock = threading.Lock()


def _key(peer_name: str, device_id: str) -> Tuple[str, str]:
    return (device_id or "", peer_name or "")


def mark_taken_over(
    peer_name: str,
    device_id: str,
    by_username: str,
    ttl_sec: float = DEFAULT_TTL_SEC,
) -> None:
    """真人接管开始. ttl_sec 后自动失效 (防遗忘 release 永久 stuck)."""
    if not peer_name or not device_id:
        return
    now = time.time()
    with _lock:
        _state[_key(peer_name, device_id)] = {
            "by": by_username,
            "started_at": now,
            "expires_at": now + max(60.0, ttl_sec),
        }


def is_taken_over(peer_name: str, device_id: str) -> bool:
    """该 peer 当前是否被真人接管 (TTL 未过期)."""
    if not peer_name or not device_id:
        return False
    with _lock:
        rec = _state.get(_key(peer_name, device_id))
        if not rec:
            return False
        if time.time() > rec.get("expires_at", 0):
            # 过期, 顺手清理
            _state.pop(_key(peer_name, device_id), None)
            return False
    return True


def release(peer_name: str, device_id: str) -> bool:
    """真人完成接管, 释放该 peer 让 AI 重新接管. 返回是否真清理了."""
    if not peer_name or not device_id:
        return False
    with _lock:
        return _state.pop(_key(peer_name, device_id), None) is not None


def get_takeover_info(peer_name: str, device_id: str) -> Optional[Dict[str, float]]:
    """返回 {by, started_at, expires_at} 或 None."""
    if not peer_name or not device_id:
        return None
    with _lock:
        rec = _state.get(_key(peer_name, device_id))
        if not rec:
            return None
        if time.time() > rec.get("expires_at", 0):
            _state.pop(_key(peer_name, device_id), None)
            return None
        return dict(rec)


def list_active() -> Dict[str, Dict[str, float]]:
    """列出当前所有接管中的 peer (TTL 未过的). 给 /cluster/stats 暴露用."""
    out: Dict[str, Dict[str, float]] = {}
    now = time.time()
    with _lock:
        expired: list = []
        for (did, peer), rec in _state.items():
            if now > rec.get("expires_at", 0):
                expired.append((did, peer))
                continue
            out[f"{did}::{peer}"] = dict(rec)
        for k in expired:
            _state.pop(k, None)
    return out


def clear_for_tests() -> None:
    """仅测试用. 清掉全部接管标记."""
    with _lock:
        _state.clear()
