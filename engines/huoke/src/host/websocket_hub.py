# -*- coding: utf-8 -*-
"""
WebSocket Hub v2 — 统一双向实时通道。

替代 SSE + 6 个轮询定时器，通过单一 WS 连接实现:

Server → Client (自动推送):
  {"type": "push.devices",   "data": {...}, "ts": ...}  # 每 10s
  {"type": "push.health",    "data": {...}, "ts": ...}  # 每 10s
  {"type": "push.tasks",     "data": {...}, "ts": ...}  # 每 15s
  {"type": "push.recovery",  "data": {...}, "ts": ...}  # 每 10s (仅掉线时)
  {"type": "push.logs",      "data": {...}, "ts": ...}  # 每 8s
  + EventBus 实时事件 (task.*, device.*, lead.*, etc.)

Client → Server:
  {"subscribe": "device.*"}           # 过滤订阅
  {"patterns": ["task.*", "lead.*"]}  # 多模式订阅
  {"cmd": "refresh", "target": "devices"}  # 立即请求推送
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import fnmatch
import threading
from typing import Dict, List, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from src.utils.subprocess_text import run as _sp_run_text

log = logging.getLogger(__name__)

_PUSH_INTERVAL_DEVICES = 10
_PUSH_INTERVAL_TASKS = 15
_PUSH_INTERVAL_LOGS = 8
_PUSH_INTERVAL_SCREENSHOTS = 1.5
_PUSH_INTERVAL_PERF = 30
_PUSH_INTERVAL_CHARTS = 120


class WebSocketClient:
    __slots__ = ("ws", "patterns", "connected_at", "messages_sent")

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.patterns: Set[str] = {"*"}
        self.connected_at = time.time()
        self.messages_sent = 0

    def matches(self, event_type: str) -> bool:
        return any(fnmatch.fnmatch(event_type, p) for p in self.patterns)


class WebSocketHub:
    def __init__(self):
        self._clients: List[WebSocketClient] = []
        self._lock = threading.Lock()
        self._bus_sub_id: str = ""
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._push_thread: Optional[threading.Thread] = None
        self._push_stop = threading.Event()
        self._screenshot_push_enabled = True
        self._visible_devices: Set[str] = set()
        self._last_push_hash: Dict[str, int] = {}
        self._last_perf_data: Dict[str, dict] = {}  # device_id → {battery_level, mem_usage, ...}

    def _ensure_bus_subscription(self):
        if self._bus_sub_id:
            return
        try:
            from ..workflow.event_bus import get_event_bus
            bus = get_event_bus()
            self._bus_sub_id = bus.on("*", self._on_event)
            log.info("WebSocketHub: subscribed to EventBus")
        except Exception as e:
            log.warning("WebSocketHub: cannot subscribe to EventBus: %s", e)

    def _ensure_push_thread(self):
        if self._push_thread and self._push_thread.is_alive():
            return
        self._push_stop.clear()
        self._push_thread = threading.Thread(
            target=self._push_loop, daemon=True, name="ws-push")
        self._push_thread.start()
        log.info("WebSocketHub: push thread started")

    def _push_loop(self):
        """Background thread that periodically pushes data to all clients."""
        last_devices = 0
        last_tasks = 0
        last_logs = 0
        last_screenshots = 0
        last_perf = 0
        last_charts = 0

        while not self._push_stop.is_set():
            now = time.time()
            with self._lock:
                has_clients = len(self._clients) > 0

            if not has_clients:
                self._push_stop.wait(2)
                continue

            if now - last_devices >= _PUSH_INTERVAL_DEVICES:
                last_devices = now
                self._push_device_data()

            if now - last_tasks >= _PUSH_INTERVAL_TASKS:
                last_tasks = now
                self._push_task_data()

            if now - last_logs >= _PUSH_INTERVAL_LOGS:
                last_logs = now
                self._push_log_data()

            if self._screenshot_push_enabled and now - last_screenshots >= _PUSH_INTERVAL_SCREENSHOTS:
                last_screenshots = now
                self._push_screenshot_data()

            if now - last_perf >= _PUSH_INTERVAL_PERF:
                last_perf = now
                self._push_performance_data()

            if now - last_charts >= _PUSH_INTERVAL_CHARTS:
                last_charts = now
                self._push_chart_data()

            if now - getattr(self, '_last_notif_poll', 0) >= 10:
                self._last_notif_poll = now
                self._push_notification_data()

            self._push_stop.wait(2)

    def _push_device_data(self):
        """Push device status + health scores + recovery state."""
        data = {}
        try:
            from .health_monitor import metrics
            data["devices"] = metrics.device_status
            data["health_scores"] = metrics.all_health_scores()
            data["reconnects"] = metrics.device_reconnects
        except Exception as e:
            log.debug("ws推送: 获取设备指标失败: %s", e)

        try:
            from .health_monitor import get_recovery_summary
            data["recovery"] = get_recovery_summary()
        except Exception as e:
            log.debug("ws推送: 获取恢复摘要失败: %s", e)

        if data:
            self.broadcast("push.devices", data, deduplicate=True)

    def _push_task_data(self):
        """Push recent task status."""
        try:
            from .task_store import list_tasks, get_stats
            recent = list_tasks(limit=20)
            stats = get_stats()
            self.broadcast("push.tasks", {
                "recent": recent, "stats": stats,
            })
        except Exception as e:
            log.debug("ws推送: 获取任务数据失败: %s", e)

    def _push_log_data(self):
        """Push recent log entries."""
        try:
            from .event_stream import EventStreamHub
            hub = EventStreamHub.get()
            recent = hub.get_recent(30)
            self.broadcast("push.logs", {"entries": recent})
        except Exception as e:
            log.debug("ws推送: 获取日志数据失败: %s", e)

    def _push_screenshot_data(self):
        """Capture thumbnails for visible devices in parallel and push."""
        import base64
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            from .api import get_device_manager, _config_path
            manager = get_device_manager(_config_path)
            devices = manager.get_all_devices()
            visible = self._visible_devices
            targets = [d for d in devices
                       if d.status in ("connected", "online")
                       and (not visible or d.device_id in visible)]
            if not targets:
                return

            def _capture_one(device_id):
                try:
                    jpeg = self._fast_capture(manager, device_id)
                    if jpeg:
                        return device_id, base64.b64encode(jpeg).decode("ascii")
                except Exception as e:
                    log.debug("ws推送: 截图采集失败 %s: %s", device_id, e)
                return None

            screenshots = {}
            max_w = min(len(targets), 6)
            with ThreadPoolExecutor(max_workers=max_w,
                                    thread_name_prefix="scr") as pool:
                futs = {pool.submit(_capture_one, d.device_id): d
                        for d in targets[:30]}
                for f in as_completed(futs, timeout=4):
                    try:
                        result = f.result(timeout=0.1)
                        if result:
                            screenshots[result[0]] = result[1]
                    except Exception as e:
                        log.debug("ws推送: 截图future获取失败: %s", e)
                        continue
            if screenshots:
                self.broadcast("push.screenshots",
                               {"screenshots": screenshots})
        except Exception as e:
            log.debug("Screenshot push error: %s", e)

    @staticmethod
    def _fast_capture(manager, device_id):
        """Fast screenshot: adb exec-out screencap → JPEG thumbnail."""
        import subprocess
        from io import BytesIO
        try:
            adb = getattr(manager, 'adb_path', 'adb')
            r = subprocess.run(
                [adb, "-s", device_id, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode != 0 or len(r.stdout) < 100:
                return None
            from PIL import Image
            img = Image.open(BytesIO(r.stdout))
            if img.height > 240:
                ratio = 240 / img.height
                img = img.resize(
                    (int(img.width * ratio), 240), Image.BILINEAR)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=35)
            return buf.getvalue()
        except Exception as e:
            log.debug("ws推送: 快速截图失败 %s: %s", device_id, e)
            return None

    def _push_performance_data(self):
        """Push aggregated device performance metrics."""
        try:
            from .api import get_device_manager, _config_path
            manager = get_device_manager(_config_path)
            devices = manager.get_all_devices()
            online = [d for d in devices if d.status in ("connected", "online")]
            perf_data = {}
            for d in online[:20]:
                try:
                    r = _sp_run_text(
                        ["adb", "-s", d.device_id, "shell",
                         "dumpsys battery | grep level && "
                         "cat /proc/meminfo | head -3 && "
                         "dumpsys battery | grep temperature"],
                        capture_output=True, timeout=5, shell=False,
                    )
                    if r.returncode == 0:
                        import re
                        bat = re.search(r'level:\s*(\d+)', r.stdout)
                        temp = re.search(r'temperature:\s*(\d+)', r.stdout)
                        mem_total = re.search(r'MemTotal:\s*(\d+)', r.stdout)
                        mem_avail = re.search(r'MemAvailable:\s*(\d+)', r.stdout)
                        entry = {}
                        if bat:
                            entry["battery_level"] = int(bat.group(1))
                        if temp:
                            entry["battery_temp"] = round(int(temp.group(1)) / 10, 1)
                        if mem_total and mem_avail:
                            mt = int(mem_total.group(1))
                            ma = int(mem_avail.group(1))
                            entry["mem_usage"] = round((mt - ma) / mt * 100, 1) if mt else 0
                        if entry:
                            perf_data[d.device_id] = entry
                except Exception as e:
                    log.debug("ws推送: 性能数据采集失败 %s: %s", d.device_id, e)
                    continue
            if perf_data:
                self._last_perf_data.update(perf_data)  # cache for /analytics/device-perf
                self.broadcast("push.performance", {"devices": perf_data}, deduplicate=True)
                for did, pdata in perf_data.items():
                    try:
                        from .routers.monitoring import check_perf_alerts
                        check_perf_alerts(did, pdata)
                    except Exception as e:
                        log.debug("ws推送: 性能告警检查失败 %s: %s", did, e)
        except Exception as e:
            log.debug("ws推送: 性能数据推送失败: %s", e)

    def _push_chart_data(self):
        """Push updated chart trend data."""
        try:
            from .api import _analytics_cache, _load_analytics_history, _record_analytics_snapshot
            _record_analytics_snapshot()
            snaps_dev = _analytics_cache.get("device_snapshots", [])[-24:]
            snaps_task = _analytics_cache.get("task_snapshots", [])[-24:]
            self.broadcast("push.charts", {
                "device_trend": {
                    "labels": [s["ts"].split(" ")[-1] if " " in s["ts"] else s["ts"] for s in snaps_dev],
                    "online": [s.get("online", 0) for s in snaps_dev],
                    "total": [s.get("total", 0) for s in snaps_dev],
                },
                "task_trend": {
                    "labels": [s["ts"].split(" ")[-1] if " " in s["ts"] else s["ts"] for s in snaps_task],
                    "success": [s.get("success", 0) for s in snaps_task],
                    "failed": [s.get("failed", 0) for s in snaps_task],
                    "total": [s.get("total", 0) for s in snaps_task],
                },
            })
        except Exception as e:
            log.debug("ws推送: 图表数据推送失败: %s", e)

    def _push_notification_data(self):
        """Poll notifications from online devices and push new ones."""
        try:
            from .api import get_device_manager, _config_path, _notification_store
            manager = get_device_manager(_config_path)
            devices = manager.get_all_devices()
            online = [d for d in devices if d.status in ("connected", "online")]
            import re, time
            new_notifs = []
            for d in online[:10]:
                try:
                    r = _sp_run_text(
                        ["adb", "-s", d.device_id, "shell", "dumpsys", "notification", "--noredact"],
                        capture_output=True, timeout=5,
                    )
                    if r.returncode != 0:
                        continue
                    for m in re.finditer(r'pkg=(\S+).*?android\.title=\[?([^\]\n]+)', r.stdout, re.DOTALL):
                        n = {"device_id": d.device_id, "package": m.group(1),
                             "title": m.group(2).strip(), "time": time.strftime("%H:%M:%S")}
                        new_notifs.append(n)
                        _notification_store.append(n)
                        if len(_notification_store) > 200:
                            _notification_store.pop(0)
                except Exception as e:
                    log.debug("ws推送: 通知采集失败 %s: %s", d.device_id, e)
                    continue
            if new_notifs:
                self.broadcast("push.notifications", {"notifications": new_notifs[-20:]})
        except Exception as e:
            log.debug("ws推送: 通知推送失败: %s", e)

    def set_visible_devices(self, device_ids: Set[str]):
        """Client tells server which devices are visible on screen."""
        self._visible_devices = device_ids

    _NOTIFY_EVENT_TYPES = {
        "device.disconnected", "device.alert", "task.failed",
        "watchdog.captcha_detected", "workflow.finished",
    }

    def _on_event(self, event):
        msg = {
            "type": event.type,
            "data": event.data,
            "source": event.source,
            "ts": event.timestamp,
        }
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            if client.matches(event.type):
                try:
                    self._send_to_client(client, msg)
                except Exception as e:
                    log.debug("ws事件: 发送事件到客户端失败: %s", e)

        if event.type in self._NOTIFY_EVENT_TYPES:
            try:
                from .api import send_notification
                data = event.data if isinstance(event.data, dict) else {}
                title = data.get("message", event.type)
                detail = f"设备: {data.get('device_id', 'N/A')}" if "device_id" in data else str(data)[:120]
                level = data.get("level", "warning")
                send_notification(event.type, title, detail, level)
            except Exception as e:
                log.debug("ws事件: 发送通知失败: %s", e)

    def _send_to_client(self, client: WebSocketClient, msg: dict):
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._async_send(client, msg), self._loop
            )

    async def _async_send(self, client: WebSocketClient, msg: dict):
        try:
            await client.ws.send_json(msg)
            client.messages_sent += 1
        except Exception as e:
            log.debug("ws异步发送失败, 移除客户端: %s", e)
            self._remove_client(client)

    async def handle(self, ws: WebSocket):
        await ws.accept()
        client = WebSocketClient(ws)
        self._loop = asyncio.get_running_loop()
        self._ensure_bus_subscription()
        self._ensure_push_thread()

        with self._lock:
            self._clients.append(client)
        log.info("WebSocket client connected (%d total)", len(self._clients))

        await self._send_snapshot(client)

        try:
            while True:
                data = await ws.receive_text()
                try:
                    msg = json.loads(data)
                    if "subscribe" in msg:
                        client.patterns = {msg["subscribe"]}
                    elif "patterns" in msg:
                        client.patterns = set(msg["patterns"])
                    elif msg.get("cmd") == "refresh":
                        await self._handle_refresh(client, msg)
                    elif msg.get("cmd") == "set_visible_devices":
                        ids = msg.get("ids", [])
                        self._visible_devices = set(ids) if ids else set()
                except (json.JSONDecodeError, TypeError):
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            self._remove_client(client)
            log.info("WebSocket client disconnected (%d remaining)",
                     len(self._clients))

    async def _handle_refresh(self, client: WebSocketClient, msg: dict):
        """Handle on-demand refresh request from client."""
        target = msg.get("target", "")
        data = {}
        if target in ("devices", "all"):
            try:
                from .health_monitor import metrics
                data["devices"] = metrics.device_status
                data["health_scores"] = metrics.all_health_scores()
            except Exception as e:
                log.debug("ws刷新: 获取设备数据失败: %s", e)
        if target in ("tasks", "all"):
            try:
                from .task_store import list_tasks, get_stats
                data["recent_tasks"] = list_tasks(limit=20)
                data["task_stats"] = get_stats()
            except Exception as e:
                log.debug("ws刷新: 获取任务数据失败: %s", e)
        await client.ws.send_json({
            "type": f"push.{target}", "data": data, "ts": time.time(),
        })

    async def _send_snapshot(self, client: WebSocketClient):
        snapshot = {"type": "snapshot", "data": {}, "ts": time.time()}
        try:
            from .health_monitor import metrics
            snapshot["data"]["devices"] = metrics.device_status
            snapshot["data"]["health_scores"] = metrics.all_health_scores()
        except Exception as e:
            log.debug("ws快照: 获取设备指标失败: %s", e)
        try:
            from ..device_control.device_matrix import get_device_matrix
            snapshot["data"]["matrix"] = get_device_matrix().queue_stats()
        except Exception as e:
            log.debug("ws快照: 获取矩阵数据失败: %s", e)
        try:
            from ..leads.store import get_leads_store
            snapshot["data"]["leads"] = get_leads_store().pipeline_stats()
        except Exception as e:
            log.debug("ws快照: 获取线索数据失败: %s", e)
        try:
            from ..behavior.compliance_guard import get_compliance_guard
            guard = get_compliance_guard()
            snapshot["data"]["compliance"] = {
                "total_actions": guard.total_actions_today(),
            }
        except Exception as e:
            log.debug("ws快照: 获取合规数据失败: %s", e)
        try:
            from .task_store import get_stats
            snapshot["data"]["task_stats"] = get_stats()
        except Exception as e:
            log.debug("ws快照: 获取任务统计失败: %s", e)
        await client.ws.send_json(snapshot)

    def _remove_client(self, client: WebSocketClient):
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def broadcast(self, event_type: str, data: dict,
                  deduplicate: bool = False):
        """Broadcast to all matching clients. deduplicate=True skips if
        data hash matches the last push for this event_type."""
        if deduplicate:
            h = hash(json.dumps(data, sort_keys=True, default=str))
            if self._last_push_hash.get(event_type) == h:
                return
            self._last_push_hash[event_type] = h

        msg = {"type": event_type, "data": data, "ts": time.time()}
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            if client.matches(event_type):
                try:
                    self._send_to_client(client, msg)
                except Exception as e:
                    log.debug("ws广播: 发送失败: %s", e)

    def stats(self) -> dict:
        with self._lock:
            clients = list(self._clients)
        return {
            "connected_clients": len(clients),
            "push_thread_alive": (self._push_thread.is_alive()
                                  if self._push_thread else False),
            "clients": [
                {
                    "patterns": list(c.patterns),
                    "connected_for_sec": round(
                        time.time() - c.connected_at, 0),
                    "messages_sent": c.messages_sent,
                }
                for c in clients
            ],
        }


_hub: Optional[WebSocketHub] = None
_hub_lock = threading.Lock()


def get_ws_hub() -> WebSocketHub:
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = WebSocketHub()
    return _hub
