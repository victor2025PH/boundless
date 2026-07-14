# -*- coding: utf-8 -*-
"""Worker 端 agent_mesh 消息监听 (PR-6.6).

worker 启动后起后台线程, 每 ``interval`` 秒 poll 主控 agent_messages 队列,
处理 message_type=command 的指令:

* ``cmd=manual_reply``   payload {device_id, peer_name, text}
    主控真人客服在后台输入了一句话, worker 用对应物理手机发出去.
* ``cmd=pause_ai``       payload {device_id, peer_name, by_username, ttl_sec?}
    真人按"我接手", worker 把这个 peer 加入 ai_paused 集合,
    后续 _ai_reply_and_send 入口短路.
* ``cmd=resume_ai``      payload {device_id, peer_name}
    真人标"成交/流失" outcome, worker 释放 ai_paused.

复用 PR #87 _HeartbeatThread 模式 (threading.Event + wait + 优雅停止).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional
from urllib import error as _uerr, request as _ureq

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 30.0
DEFAULT_POLL_LIMIT = 50
DEFAULT_HTTP_TIMEOUT = 8.0
DEFAULT_STARTUP_DELAY_SEC = 8.0  # 让 worker 主循环先启起来


def _coordinator_url() -> str:
    try:
        from src.host.cluster_lock_client import get_coordinator_url
        return get_coordinator_url()
    except Exception:
        import os
        return os.environ.get("OPENCLAW_COORDINATOR_URL",
                              "http://192.168.0.118:8000")


def _api_key_header() -> Dict[str, str]:
    import os
    key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
    return {"X-API-Key": key} if key else {}


def _http_get(path: str, timeout: float = DEFAULT_HTTP_TIMEOUT) -> Optional[Dict[str, Any]]:
    url = _coordinator_url().rstrip("/") + path
    headers = _api_key_header()
    try:
        req = _ureq.Request(url, method="GET", headers=headers)
        with _ureq.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[mesh_listener] GET %s failed: %s", path, exc)
        return None


def _http_post(path: str, body: Optional[Dict[str, Any]] = None,
               timeout: float = DEFAULT_HTTP_TIMEOUT) -> Optional[Dict[str, Any]]:
    url = _coordinator_url().rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    headers.update(_api_key_header())
    data = json.dumps(body or {}).encode()
    try:
        req = _ureq.Request(url, data=data, method="POST", headers=headers)
        with _ureq.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[mesh_listener] POST %s failed: %s", path, exc)
        return None


# ── 命令处理器 ───────────────────────────────────────────────────────
def _handle_pause_ai(payload: Dict[str, Any]) -> Optional[str]:
    device_id = (payload.get("device_id") or "").strip()
    peer_name = (payload.get("peer_name") or "").strip()
    by_username = (payload.get("by_username") or "").strip() or "unknown"
    ttl_sec = float(payload.get("ttl_sec") or 3600.0)
    if not device_id or not peer_name:
        return "device_id / peer_name 必填"
    try:
        from src.host.ai_takeover_state import mark_taken_over
        mark_taken_over(peer_name=peer_name, device_id=device_id,
                        by_username=by_username, ttl_sec=ttl_sec)
        logger.info("[mesh_listener] AI paused for peer=%s device=%s by=%s",
                    peer_name, device_id, by_username)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"mark_taken_over failed: {exc}"


def _handle_resume_ai(payload: Dict[str, Any]) -> Optional[str]:
    device_id = (payload.get("device_id") or "").strip()
    peer_name = (payload.get("peer_name") or "").strip()
    if not device_id or not peer_name:
        return "device_id / peer_name 必填"
    try:
        from src.host.ai_takeover_state import release
        release(peer_name=peer_name, device_id=device_id)
        logger.info("[mesh_listener] AI resumed for peer=%s device=%s",
                    peer_name, device_id)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"release failed: {exc}"


def _handle_manual_reply(payload: Dict[str, Any]) -> Optional[str]:
    """真人客服在后台打字 → worker 用对应手机发出.

    Args (payload):
        device_id: 物理手机 serial
        peer_name: 收件方 FB 名字
        text: 要发送的内容
    """
    device_id = (payload.get("device_id") or "").strip()
    peer_name = (payload.get("peer_name") or "").strip()
    text = payload.get("text") or ""
    if not device_id or not peer_name or not text:
        return "device_id / peer_name / text 必填"
    try:
        from src.app_automation.facebook import FacebookAutomation
        from src.device_control.device_manager import get_device_manager
        dm = get_device_manager()
        # FacebookAutomation 内部用 _did/_u2 自动路由, 这里只要有实例就行
        fa = FacebookAutomation(device_manager=dm)
        ok = fa.send_message(target_name=peer_name, message=text,
                             device_id=device_id)
        if ok:
            logger.info("[mesh_listener] manual_reply 发出 device=%s peer=%s len=%d",
                        device_id, peer_name, len(text))
            return None
        return "facebook.send_message returned False"
    except Exception as exc:  # noqa: BLE001
        return f"manual_reply 异常: {exc}"


_COMMAND_HANDLERS = {
    "pause_ai": _handle_pause_ai,
    "resume_ai": _handle_resume_ai,
    "manual_reply": _handle_manual_reply,
}


# ── _ListenerThread ──────────────────────────────────────────────────
class _ListenerThread(threading.Thread):
    """周期 poll 主控 agent_messages, 处理 cmd 指令."""

    def __init__(
        self,
        worker_id: str,
        interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        limit: int = DEFAULT_POLL_LIMIT,
        startup_delay_sec: float = DEFAULT_STARTUP_DELAY_SEC,
    ):
        super().__init__(daemon=True, name=f"mesh-listener-{worker_id[:8]}")
        self._worker_id = worker_id
        # interval 不在这里 clamp; caller 默认 30s, 测试可传更小
        self._interval = max(0.01, interval_sec)
        self._limit = max(1, limit)
        self._startup_delay = max(0.0, startup_delay_sec)
        self._stop_event = threading.Event()
        self._iterations = 0
        self._processed = 0
        self._failed = 0
        self._last_run_at: Optional[float] = None

    def stop(self) -> None:
        self._stop_event.set()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.is_alive() and not self._stop_event.is_set(),
            "worker_id": self._worker_id,
            "iterations": self._iterations,
            "processed_total": self._processed,
            "failed_total": self._failed,
            "last_run_at": self._last_run_at,
            "interval_sec": self._interval,
        }

    def run(self) -> None:
        if self._startup_delay > 0:
            self._stop_event.wait(self._startup_delay)
            if self._stop_event.is_set():
                return
        logger.info("[mesh_listener] thread started worker_id=%s interval=%.0fs",
                    self._worker_id, self._interval)
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(self._interval)
        logger.info("[mesh_listener] thread stopped, processed=%d failed=%d",
                    self._processed, self._failed)

    def _tick(self) -> None:
        self._iterations += 1
        self._last_run_at = time.time()
        try:
            messages = self._poll()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[mesh_listener] poll failed: %s", exc)
            return
        for m in messages:
            self._dispatch(m)

    def _poll(self) -> List[Dict[str, Any]]:
        path = (f"/lead-mesh/agents/messages"
                f"?to_agent={self._worker_id}"
                f"&message_type=command"
                f"&status=pending"
                f"&limit={self._limit}")
        result = _http_get(path)
        if not result:
            return []
        return result.get("messages") or result if isinstance(result, list) else result.get("messages") or []

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        msg_id = msg.get("id")
        payload = msg.get("payload") or {}
        cmd = (payload.get("cmd") or "").strip()
        # 标 delivered (拿到了, 还没处理完)
        _http_post(f"/lead-mesh/agents/messages/{msg_id}/deliver")

        handler = _COMMAND_HANDLERS.get(cmd)
        error: Optional[str] = None
        if not handler:
            error = f"unknown cmd '{cmd}'"
            logger.warning("[mesh_listener] %s msg_id=%s", error, msg_id)
        else:
            try:
                error = handler(payload)
            except Exception as exc:  # noqa: BLE001
                error = f"handler exception: {exc}"
                logger.exception("[mesh_listener] handler failed msg_id=%s", msg_id)

        # 标 ack (处理完, 带 error 文字反馈)
        _http_post(f"/lead-mesh/agents/messages/{msg_id}/ack",
                   {"error": error or ""})
        if error:
            self._failed += 1
        else:
            self._processed += 1


# ── 单例 ──────────────────────────────────────────────────────────────
_listener: Optional[_ListenerThread] = None
_listener_lock = threading.Lock()


def start_worker_listener(
    worker_id: Optional[str] = None,
    interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    limit: int = DEFAULT_POLL_LIMIT,
    startup_delay_sec: float = DEFAULT_STARTUP_DELAY_SEC,
) -> _ListenerThread:
    """启动 worker listener (idempotent). worker 启动钩子调."""
    global _listener
    with _listener_lock:
        if _listener is not None and _listener.is_alive():
            return _listener
        if not worker_id:
            try:
                from src.host.cluster_lock_client import get_worker_id
                worker_id = get_worker_id()
            except Exception:
                import socket
                worker_id = socket.gethostname()
        t = _ListenerThread(
            worker_id=worker_id,
            interval_sec=interval_sec, limit=limit,
            startup_delay_sec=startup_delay_sec,
        )
        t.start()
        _listener = t
        return t


def stop_worker_listener(timeout_sec: float = 10.0) -> bool:
    global _listener
    with _listener_lock:
        t = _listener
        _listener = None
    if t is None:
        return True
    t.stop()
    t.join(timeout=timeout_sec)
    return not t.is_alive()


def get_listener_status() -> Dict[str, Any]:
    with _listener_lock:
        t = _listener
    if t is None:
        return {"running": False, "reason": "not started"}
    return t.status()


def reset_for_tests() -> None:
    global _listener
    with _listener_lock:
        _listener = None
