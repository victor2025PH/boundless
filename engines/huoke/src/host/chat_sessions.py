# -*- coding: utf-8 -*-
"""
AI 指令多轮会话 — 内存存储 ChatController 与待确认执行计划（TTL 清理）。

P1：同 session_id 复用解析上下文；dry_run 写入 pending_plan；confirm 后执行。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_TTL_SEC = 86400  # 24h
_MAX_SESSIONS = 2000


class ChatSessionStore:
    """单例会话表。"""

    _instance: Optional["ChatSessionStore"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def instance(cls) -> "ChatSessionStore":
        with cls._lock:
            if cls._instance is None:
                cls._instance = ChatSessionStore()
            return cls._instance

    def _prune(self) -> None:
        now = time.time()
        dead = [sid for sid, s in self._sessions.items() if now - s.get("ts", 0) > _TTL_SEC]
        for sid in dead:
            self._sessions.pop(sid, None)
        if len(self._sessions) > _MAX_SESSIONS:
            # 删最旧的一半
            items = sorted(self._sessions.items(), key=lambda x: x[1].get("ts", 0))
            for sid, _ in items[: len(items) // 2]:
                self._sessions.pop(sid, None)

    def get_or_create(self, session_id: Optional[str]) -> Tuple[str, Any]:
        """返回 (session_id, ChatController)。"""
        from src.chat.controller import ChatController

        self._prune()
        sid = (session_id or "").strip()
        if not sid or sid not in self._sessions:
            sid = str(uuid.uuid4())
            self._sessions[sid] = {"ts": time.time(), "ctrl": ChatController()}
        else:
            self._sessions[sid]["ts"] = time.time()
        return sid, self._sessions[sid]["ctrl"]

    def touch_controller(self, session_id: str) -> Optional[Any]:
        """仅刷新已存在会话；不存在返回 None（用于 confirm，避免误建新会话）。"""
        sid = (session_id or "").strip()
        if not sid or sid not in self._sessions:
            return None
        self._sessions[sid]["ts"] = time.time()
        return self._sessions[sid].get("ctrl")

    def set_pending_plan(self, session_id: str, plan: Dict[str, Any]) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is not None:
                s["pending_plan"] = plan
                s["ts"] = time.time()

    def pop_pending_plan(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            s = self._sessions.get(session_id)
            if not s:
                return None
            p = s.pop("pending_plan", None)
            s["ts"] = time.time()
            return p

    def clear_session(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None


def get_session_store() -> ChatSessionStore:
    return ChatSessionStore.instance()
