# -*- coding: utf-8 -*-
"""WebSocket 群控通道路由。"""
import asyncio
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import APIRouter, WebSocket, Depends
from .auth import verify_api_key
from src.device_control.device_manager import get_device_manager
from src.host.device_registry import DEFAULT_DEVICES_YAML

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)
_config_path = DEFAULT_DEVICES_YAML


# ── 辅助函数 ──


def _try_scrcpy_broadcast(device_ids, action, **kwargs):
    """Try scrcpy broadcast first (sync <50ms), return None if unavailable."""
    try:
        from ..scrcpy_manager import get_scrcpy_manager
        mgr = get_scrcpy_manager()
        has_ctrl = [did for did in device_ids if
                    mgr.get_session(did) and mgr.get_session(did).has_control]
        if len(has_ctrl) < len(device_ids) * 0.5:
            return None
        if action == "tap":
            return mgr.broadcast_tap(has_ctrl, kwargs["x"], kwargs["y"])
        elif action == "key":
            return mgr.broadcast_key(has_ctrl, kwargs["keycode"])
        elif action == "swipe":
            return mgr.broadcast_swipe(
                has_ctrl, kwargs["x1"], kwargs["y1"],
                kwargs["x2"], kwargs["y2"], kwargs.get("duration", 300))
    except Exception:
        pass
    return None


def _try_scrcpy_broadcast_kw(device_ids, action, kwargs):
    """Wrapper for _try_scrcpy_broadcast with keyword args dict."""
    return _try_scrcpy_broadcast(device_ids, action, **kwargs)


def _group_exec(device_ids: list, fn, *args) -> dict:
    """Execute fn(manager, device_id, *args) on all devices concurrently."""
    manager = get_device_manager(_config_path)
    results = {}

    def _run(did):
        try:
            ok = fn(manager, did, *args)
            return did[:12], "ok" if ok else "fail"
        except Exception as e:
            return did[:12], str(e)

    with ThreadPoolExecutor(max_workers=min(len(device_ids), 10)) as pool:
        futures = {pool.submit(_run, did): did for did in device_ids}
        for f in as_completed(futures):
            k, v = f.result()
            results[k] = v
    return results


# ── WebSocket 端点 ──


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Real-time event stream via WebSocket."""
    from src.host.websocket_hub import get_ws_hub
    await get_ws_hub().handle(ws)


@router.websocket("/ws/group-control")
async def ws_group_control(ws: WebSocket):
    """
    WebSocket 群控通道 — 持久连接，<20ms 延迟。

    Client -> Server:
      {"cmd":"select", "devices":["id1","id2"]}     # 设置选中设备
      {"cmd":"tap", "x":540, "y":1200}               # 群控点击
      {"cmd":"swipe", "x1":..., "y1":..., "x2":..., "y2":..., "dur":300}
      {"cmd":"key", "code":3}                         # 按键
      {"cmd":"text", "text":"hello"}                  # 文本输入
      {"cmd":"home"} / {"cmd":"back"} / {"cmd":"recent"}  # 快捷按键

    Server -> Client:
      {"type":"ack", "cmd":"tap", "method":"scrcpy_sync", "latency_ms":25, "results":{...}}
      {"type":"status", "connected":N, "selected":M}  # 定期状态推送
    """
    await ws.accept()
    selected_devices: list = []
    manager = get_device_manager(_config_path)

    async def _send_ack(cmd: str, method: str, results: dict,
                        latency_ms: float):
        await ws.send_json({
            "type": "ack", "cmd": cmd, "method": method,
            "latency_ms": round(latency_ms, 1), "results": results,
        })

    async def _send_status():
        connected = manager.get_connected_devices()
        connected_ids = {d.device_id for d in connected}
        await ws.send_json({
            "type": "status",
            "connected": len(connected_ids),
            "selected": len(selected_devices),
            "devices": {
                did[:12]: ("online" if did in connected_ids else "offline")
                for did in selected_devices
            },
        })

    async def _status_loop():
        while True:
            try:
                await asyncio.sleep(5)
                await _send_status()
            except Exception:
                break

    loop = asyncio.get_running_loop()

    async def _handle_messages():
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            cmd = msg.get("cmd", "")
            t0 = time.time()

            if cmd == "select":
                selected_devices.clear()
                selected_devices.extend(msg.get("devices", []))
                await _send_status()
                continue

            if not selected_devices:
                await ws.send_json({"type": "error", "msg": "no devices selected"})
                continue

            key_shortcuts = {"home": 3, "back": 4, "recent": 187}
            if cmd in key_shortcuts:
                cmd = "key"
                msg["code"] = key_shortcuts[msg["cmd"]]

            method = "adb"
            results = {}

            if cmd == "tap":
                x, y = int(msg.get("x", 0)), int(msg.get("y", 0))
                sync = await loop.run_in_executor(
                    None, _try_scrcpy_broadcast, selected_devices, "tap",
                )
                if sync is None:
                    sync = await loop.run_in_executor(
                        None, _try_scrcpy_broadcast_kw,
                        selected_devices, "tap", {"x": x, "y": y},
                    )
                if sync is not None:
                    results, method = sync, "scrcpy_sync"
                else:
                    results = await loop.run_in_executor(
                        None, _group_exec, selected_devices,
                        lambda m, d, _x, _y: m.input_tap(d, _x, _y), x, y,
                    )

            elif cmd == "swipe":
                x1 = int(msg.get("x1", 0))
                y1 = int(msg.get("y1", 0))
                x2 = int(msg.get("x2", 0))
                y2 = int(msg.get("y2", 0))
                dur = int(msg.get("dur", 300))
                sync = await loop.run_in_executor(
                    None, _try_scrcpy_broadcast_kw,
                    selected_devices, "swipe",
                    {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": dur},
                )
                if sync is not None:
                    results, method = sync, "scrcpy_sync"
                else:
                    results = await loop.run_in_executor(
                        None, _group_exec, selected_devices,
                        lambda m, d, a, b, c, e, f: m.input_swipe(d, a, b, c, e, f),
                        x1, y1, x2, y2, dur,
                    )

            elif cmd == "key":
                code = int(msg.get("code", 0))
                sync = await loop.run_in_executor(
                    None, _try_scrcpy_broadcast_kw,
                    selected_devices, "key", {"keycode": code},
                )
                if sync is not None:
                    results, method = sync, "scrcpy_sync"
                else:
                    results = await loop.run_in_executor(
                        None, _group_exec, selected_devices,
                        lambda m, d, k: m.input_key(d, k), code,
                    )

            elif cmd == "text":
                text = msg.get("text", "")
                results = await loop.run_in_executor(
                    None, _group_exec, selected_devices,
                    lambda m, d, t: m.input_text(d, t), text,
                )

            latency = (time.time() - t0) * 1000
            await _send_ack(cmd, method, results, latency)

    try:
        status_task = asyncio.create_task(_status_loop())
        await _handle_messages()
    except Exception:
        pass
    finally:
        status_task.cancel()


@router.get("/ws/stats", dependencies=[Depends(verify_api_key)])
def ws_stats():
    from src.host.websocket_hub import get_ws_hub
    return get_ws_hub().stats()
