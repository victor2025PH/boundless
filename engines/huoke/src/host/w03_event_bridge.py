# -*- coding: utf-8 -*-
"""W03 事件桥接 — 从 Worker-03 轮询 SSE Hub 快照并转发到本地 EventStreamHub。

架构:
  - 后台守护线程，每 10s 轮询 W03 的 /events/hub-snapshot?since_id=N
  - 新事件写入本地 EventStreamHub，前端通过 oc:event 立即收到
  - 标记 data._from_w03=True，让前端知道事件来自 W03
  - W03 离线时静默跳过，不影响主面板
"""

import json
import logging
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

_W03_BASE = "http://192.168.0.103:8000"
_POLL_INTERVAL = 10       # 正常轮询间隔（秒）
_RETRY_INTERVAL = 30      # W03 离线时回退间隔（秒）
_MAX_FORWARD_PER_CYCLE = 30  # 单次最多转发条数，防止大量历史事件刷屏

_last_event_id = 0
_bridge_started = False
_bridge_lock = threading.Lock()
_consecutive_failures = 0
# 2026-05-05 H.2-followup: 停 thread 用 stop_event + thread ref
_bridge_stop_event = threading.Event()
_bridge_thread = None


def start_w03_bridge():
    """启动 W03 事件桥接后台线程（幂等）。"""
    global _bridge_started, _bridge_thread
    with _bridge_lock:
        if _bridge_started:
            return
        _bridge_started = True
    _bridge_stop_event.clear()
    t = threading.Thread(target=_bridge_loop, daemon=True, name="w03-event-bridge")
    t.start()
    _bridge_thread = t
    logger.info("[W03Bridge] 事件桥接线程已启动，轮询间隔 %ds", _POLL_INTERVAL)


def stop_w03_bridge(timeout_sec: float = 5.0) -> bool:
    """优雅停止 W03 事件桥接 (Stage H.2-followup).

    设 _stop_event 让 _bridge_loop 下次 wait 立即唤醒退出. join thread.
    Returns True 如果在 timeout 内退出.
    """
    global _bridge_started, _bridge_thread
    _bridge_stop_event.set()
    t = _bridge_thread
    _bridge_thread = None
    with _bridge_lock:
        _bridge_started = False
    if t is None:
        return True
    t.join(timeout=timeout_sec)
    return not t.is_alive()


def _bridge_loop():
    global _last_event_id, _consecutive_failures

    # 首次启动：先获取当前 max_id，避免转发大量历史事件
    _init_since_id()

    # 2026-05-05 H.2-followup: while True → stop_event 可控
    while not _bridge_stop_event.is_set():
        sleep_time = _POLL_INTERVAL
        try:
            from .event_stream import EventStreamHub
            hub = EventStreamHub.get()

            url = (f"{_W03_BASE}/events/hub-snapshot"
                   f"?since_id={_last_event_id}&limit={_MAX_FORWARD_PER_CYCLE}")
            req = urllib.request.Request(url, method="GET",
                                         headers={"Connection": "close"})
            resp = urllib.request.urlopen(req, timeout=5)
            try:
                data = json.loads(resp.read().decode())
            finally:
                resp.close()
            events = data.get("events", [])

            forwarded = 0
            for event in events:
                eid = event.get("id", 0)
                if eid <= _last_event_id:
                    continue
                _last_event_id = eid

                # 转发到本地 Hub，标记 _from_w03=True
                evt_data = dict(event.get("data") or {})
                evt_data["_from_w03"] = True

                # 部分事件类型在 W03 idle 时会刷屏，过滤掉低价值心跳
                evt_type = event.get("type", "")
                if evt_type in ("system.log",):
                    continue

                hub.push_event(
                    evt_type,
                    evt_data,
                    device_id=event.get("device_id", ""),
                )
                forwarded += 1

            if forwarded:
                logger.debug("[W03Bridge] 转发 %d 个事件，max_id=%d",
                             forwarded, _last_event_id)

            _consecutive_failures = 0  # 成功则重置

        except Exception as e:
            _consecutive_failures += 1
            if _consecutive_failures <= 3 or _consecutive_failures % 20 == 0:
                logger.debug("[W03Bridge] 轮询失败 (连续%d次): %s",
                             _consecutive_failures, e)
            # 离线时拉长间隔，减少无用请求
            sleep_time = _RETRY_INTERVAL if _consecutive_failures >= 3 else _POLL_INTERVAL

        # 2026-05-05 H.2-followup: time.sleep → stop_event.wait 让 stop 立即唤醒
        _bridge_stop_event.wait(sleep_time)


def _init_since_id():
    """启动时先获取 W03 当前最大 event_id，避免回放历史。"""
    global _last_event_id
    try:
        url = f"{_W03_BASE}/events/hub-snapshot?since_id=0&limit=1"
        req = urllib.request.Request(url, method="GET",
                                     headers={"Connection": "close"})
        resp = urllib.request.urlopen(req, timeout=5)
        try:
            data = json.loads(resp.read().decode())
        finally:
            resp.close()
        # 取 max_id 字段或从 events 中推算
        max_id = data.get("max_id", 0)
        if not max_id:
            evts = data.get("events", [])
            max_id = max((e.get("id", 0) for e in evts), default=0)
        _last_event_id = max_id
        logger.info("[W03Bridge] 初始化 since_id=%d", _last_event_id)
    except Exception as e:
        logger.debug("[W03Bridge] 初始化 since_id 失败: %s", e)
