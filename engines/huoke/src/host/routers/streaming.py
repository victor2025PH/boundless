# -*- coding: utf-8 -*-
"""Scrcpy 流媒体与屏幕录制路由。"""
import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, HTTPException, WebSocket, Depends

from .auth import verify_api_key
from src.device_control.device_manager import get_device_manager
from src.host.executor import _resolve_serial_from_config
from src.host.device_registry import DEFAULT_DEVICES_YAML

router = APIRouter(tags=["streaming"])
logger = logging.getLogger(__name__)

# 曾误用 parent×3 指向 src/config；须与 device_registry 主控路径一致
_config_path = DEFAULT_DEVICES_YAML


def _resolve_device_with_manager(device_id: str):
    """Resolve device_id (alias or serial) to actual serial and return (serial, manager)."""
    manager = get_device_manager(_config_path)
    info = manager.get_device_info(device_id)
    if info:
        return device_id, manager
    serial = _resolve_serial_from_config(_config_path, device_id)
    info = manager.get_device_info(serial)
    if not info:
        raise HTTPException(status_code=404, detail="设备不存在")
    return serial, manager


# ── Scrcpy Streaming API ──

@router.post("/devices/{device_id}/stream/start")
def start_stream(device_id: str, body: dict = None):
    """Start scrcpy H.264 stream for a device."""
    body = body or {}
    did, _ = _resolve_device_with_manager(device_id)
    from src.host.scrcpy_manager import get_scrcpy_manager
    mgr = get_scrcpy_manager()
    session = mgr.start_session(
        did,
        max_size=int(body.get("max_size", 800)),
        bitrate=int(body.get("bitrate", 2_000_000)),
        max_fps=int(body.get("max_fps", 30)),
    )
    if not session:
        raise HTTPException(status_code=500, detail="无法启动 scrcpy 流")
    return {
        "ok": True, "device_id": did, "port": session.port,
        "width": session.screen_width, "height": session.screen_height,
    }


@router.post("/devices/{device_id}/stream/stop")
def stop_stream(device_id: str):
    """Stop scrcpy stream for a device."""
    did, _ = _resolve_device_with_manager(device_id)
    from src.host.scrcpy_manager import get_scrcpy_manager
    mgr = get_scrcpy_manager()
    mgr.stop_session(did)
    return {"ok": True, "device_id": did}


@router.post("/devices/{device_id}/stream/reconnect-control",
             dependencies=[Depends(verify_api_key)])
def reconnect_stream_control(device_id: str):
    """P2-③ 仅重连 control 通道, 保持 video stream.

    当用户看到投屏只读 banner (control socket 未建立但 video 正常) 时调用,
    比 stop+start 快 5-10s, 不影响视频画面.

    返回 {ok, device_id, has_control, message}.
    若 session 不存在或 video 未启动则 404.
    """
    did, _ = _resolve_device_with_manager(device_id)
    from src.host.scrcpy_manager import get_scrcpy_manager
    mgr = get_scrcpy_manager()
    session = mgr.get_session(did) if hasattr(mgr, "get_session") else mgr._sessions.get(did)
    if not session or not session.is_running:
        raise HTTPException(
            status_code=404,
            detail="No active stream session — please start the stream first",
        )
    try:
        ok = session.reconnect_control()
        return {
            "ok": ok,
            "device_id": did,
            "has_control": session.has_control,
            "message": "control reconnected" if ok else "still video-only after retries",
        }
    except Exception as e:
        logger.error("[reconnect-control] %s failed: %s", did[:8], e)
        raise HTTPException(status_code=500, detail=f"reconnect failed: {e}")


@router.get("/streams")
def list_streams():
    """List active scrcpy streaming sessions with stats."""
    from src.host.scrcpy_manager import get_scrcpy_manager
    mgr = get_scrcpy_manager()
    return {"sessions": mgr.active_sessions()}


@router.get("/streams/quality-presets")
def stream_quality_presets():
    """List available quality presets."""
    from src.host.scrcpy_manager import QUALITY_PRESETS
    return {"presets": {k: {"max_size": v[0], "bitrate": v[1],
                            "max_fps": v[2], "label": v[3]}
                       for k, v in QUALITY_PRESETS.items()}}


@router.post("/devices/{device_id}/stream/quality", dependencies=[Depends(verify_api_key)])
def change_stream_quality(device_id: str, body: dict):
    """Change stream quality (restarts session with new params).

    Returns ok=True with actual_quality indicating the quality that was applied.
    If the requested quality fails, scrcpy_manager will try to restore the
    previous quality and the response will have ok=True with a 'degraded' flag.
    """
    did, _ = _resolve_device_with_manager(device_id)
    quality = body.get("quality", "medium")
    from src.host.scrcpy_manager import get_scrcpy_manager, QUALITY_PRESETS
    if quality not in QUALITY_PRESETS:
        raise HTTPException(status_code=400,
                            detail=f"未知画质 '{quality}'，可用选项: {list(QUALITY_PRESETS.keys())}")
    mgr = get_scrcpy_manager()
    # 记录切换前的实际画质，用于响应中标注是否降级
    old_session = mgr.get_session(did)
    prev_quality = old_session.quality if old_session else "medium"

    session = mgr.change_quality(did, quality)
    if not session:
        logger.error("[stream] change_quality failed for %s: %s→%s, even fallback failed",
                     did[:8], prev_quality, quality)
        raise HTTPException(
            status_code=500,
            detail=f"画质切换失败 ({quality})，设备连接可能中断，请检查 ADB 连接后重试")

    actual_quality = session.quality
    degraded = actual_quality != quality
    if degraded:
        logger.warning("[stream] quality degraded for %s: %s→%s (requested %s)",
                       did[:8], prev_quality, actual_quality, quality)
    return {
        "ok": True,
        "quality": actual_quality,
        "requested_quality": quality,
        "degraded": degraded,
        "label": QUALITY_PRESETS[actual_quality][3],
        **session.get_stream_stats(),
    }


@router.get("/devices/{device_id}/stream/stats")
@router.get("/cluster/devices/{device_id}/stream/stats")  # 兼容前端 cluster 前缀（修复 49 个 404）
def stream_stats(device_id: str):
    """Get real-time stream statistics. Proxies to owning worker for cluster devices.

    Registered under both `/devices/...` and `/cluster/devices/...` because
    the frontend (devices.js + video-stream.js) prepends `/cluster` for
    cluster-resolved devices, but the proxy fallback already handles both cases.
    """
    import urllib.request as _ur, json as _jj
    try:
        did, _ = _resolve_device_with_manager(device_id)
    except HTTPException as e:
        if e.status_code != 404:
            raise
        # Cluster device - proxy to owning worker
        try:
            from src.host.multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            if coord:
                for hid, hinfo in coord._hosts.items():
                    if not getattr(hinfo, 'online', False):
                        continue
                    host_devs = {d.get('device_id') for d in (getattr(hinfo, 'devices', None) or [])}
                    if device_id not in host_devs:
                        continue
                    url = f"http://{hinfo.host_ip}:{hinfo.port or 8000}/devices/{device_id}/stream/stats"
                    req = _ur.Request(url, method="GET", headers={"Connection": "close"})
                    resp = _ur.urlopen(req, timeout=5)
                    return _jj.loads(resp.read().decode())
        except Exception:
            pass
        return {"active": False}
    from src.host.scrcpy_manager import get_scrcpy_manager
    mgr = get_scrcpy_manager()
    session = mgr.get_session(did)
    if not session or not session.is_running:
        return {"active": False}
    return {"active": True, **session.get_stream_stats()}


# ── Screen Recording API ──

@router.post("/devices/{device_id}/record/start", dependencies=[Depends(verify_api_key)])
def start_recording(device_id: str, body: dict = None):
    """Start screen recording for a device."""
    did, _ = _resolve_device_with_manager(device_id)
    from src.utils.screen_recorder import get_screen_recorder
    rec = get_screen_recorder()
    body = body or {}
    ok = rec.start_recording(did, max_segment_sec=int(body.get("max_sec", 180)))
    if not ok:
        raise HTTPException(status_code=500, detail="录屏启动失败")
    return {"ok": True, "device_id": did, "action": "recording_started"}


@router.post("/devices/{device_id}/record/stop", dependencies=[Depends(verify_api_key)])
def stop_recording(device_id: str):
    """Stop recording and pull files."""
    did, _ = _resolve_device_with_manager(device_id)
    from src.utils.screen_recorder import get_screen_recorder
    rec = get_screen_recorder()
    files = rec.stop_recording(did)
    return {"ok": True, "device_id": did, "files": files, "count": len(files)}


@router.get("/devices/{device_id}/record/status", dependencies=[Depends(verify_api_key)])
def recording_status(device_id: str):
    """Check recording status for a device."""
    did, _ = _resolve_device_with_manager(device_id)
    from src.utils.screen_recorder import get_screen_recorder
    rec = get_screen_recorder()
    status = rec.get_status(did)
    return status or {"device_id": did, "recording": False}


@router.get("/recordings", dependencies=[Depends(verify_api_key)])
def list_recordings():
    """List all saved recording files."""
    from src.utils.screen_recorder import get_screen_recorder
    rec = get_screen_recorder()
    return {"recordings": rec.list_recordings()}


@router.get("/recordings/{filename}")
def download_recording(filename: str):
    """Download a recording file."""
    from src.utils.screen_recorder import _RECORDINGS_DIR
    from fastapi.responses import FileResponse
    filepath = os.path.join(_RECORDINGS_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(filepath, media_type="video/mp4", filename=filename)


# ── WebSocket Stream ──

@router.websocket("/devices/{device_id}/stream/ws")
async def stream_ws(websocket: WebSocket, device_id: str):
    """WebSocket: bidirectional H.264 video + scrcpy control.

    Server->Client: binary H.264 frames, JSON config
    Client->Server: JSON control messages:
      {"type":"touch", "action":0, "x":100, "y":200}        # 0=down,1=up,2=move
      {"type":"tap", "x":100, "y":200}
      {"type":"swipe", "x1":100, "y1":200, "x2":300, "y2":400, "duration":300}
      {"type":"key", "keycode":4}
      {"type":"text", "text":"hello"}
      {"type":"scroll", "x":100, "y":200, "vscroll":-1}
    """
    import json as _json
    import asyncio as _asyncio

    # 先接受连接，避免同步操作阻塞事件循环导致握手超时
    await websocket.accept()

    # 在线程池中执行同步的设备解析，不阻塞事件循环
    try:
        did, _ = await _asyncio.to_thread(_resolve_device_with_manager, device_id)
    except HTTPException:
        logger.warning("[stream_ws] Device not found: %s", device_id)
        try:
            await websocket.send_json({"error": f"device {device_id} not found"})
            await websocket.close(code=4004, reason="device not found")
        except Exception:
            pass
        return
    except Exception as e:
        logger.error("[stream_ws] resolve error: %s", e)
        try:
            await websocket.send_json({"error": f"resolve error: {e}"})
            await websocket.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass
        return

    try:
        from src.host.scrcpy_manager import get_scrcpy_manager
        mgr = get_scrcpy_manager()
    except Exception as e:
        logger.error("[stream_ws] scrcpy init error: %s", e)
        try:
            await websocket.send_json({"error": f"scrcpy init error: {e}"})
            await websocket.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass
        return

    session = await _asyncio.to_thread(mgr.acquire_session, did)
    replay_frames = session.get_init_frames() if session else []
    if not session:
        logger.error("[stream_ws] scrcpy start failed for %s", did[:8])
        try:
            await websocket.send_json({"error": "scrcpy start failed, check device connection"})
            await websocket.close(code=1011, reason="scrcpy start failed")
        except Exception:
            pass
        return

    logger.info("[stream_ws] Stream connected: %s (%dx%d)",
                did[:8], session.screen_width, session.screen_height)

    try:
        await websocket.send_json({
            "type": "config",
            "width": session.screen_width,
            "height": session.screen_height,
            "has_control": session.has_control,
            "quality": session.quality,
        })

        # 重放缓存的初始帧(SPS+PPS+IDR)，确保新客户端能解码
        if replay_frames:
            for frame in replay_frames:
                await websocket.send_bytes(frame)
            logger.info("[stream_ws] Replayed %d init frames to new client", len(replay_frames))

        loop = asyncio.get_running_loop()

        async def _read_controls():
            """Listen for control messages (binary or JSON) from browser."""
            while session.is_running:
                try:
                    ws_msg = await websocket.receive()
                    if "bytes" in ws_msg and ws_msg["bytes"]:
                        raw_bytes = ws_msg["bytes"]
                        if len(raw_bytes) >= 10:
                            cmd = raw_bytes[0]
                            if cmd == 0x80:  # touch
                                action = raw_bytes[1]
                                x = int.from_bytes(raw_bytes[2:6], 'big', signed=True)
                                y = int.from_bytes(raw_bytes[6:10], 'big', signed=True)
                                pid = int.from_bytes(raw_bytes[10:12], 'big', signed=True) if len(raw_bytes) >= 12 else -1
                                session.inject_touch(action, x, y, pointer_id=pid)
                            elif cmd == 0x83:  # scroll
                                x = int.from_bytes(raw_bytes[2:6], 'big', signed=True)
                                y = int.from_bytes(raw_bytes[6:10], 'big', signed=True)
                                hs = int.from_bytes(raw_bytes[10:12], 'big', signed=True) if len(raw_bytes) >= 12 else 0
                                vs = int.from_bytes(raw_bytes[12:14], 'big', signed=True) if len(raw_bytes) >= 14 else 0
                                session.inject_scroll(x, y, hs, vs)
                        continue
                    if "text" in ws_msg and ws_msg["text"]:
                        msg = _json.loads(ws_msg["text"])
                    else:
                        continue
                    mt = msg.get("type", "")
                    if mt == "touch":
                        session.inject_touch(
                            msg.get("action", 0),
                            int(msg["x"]), int(msg["y"]),
                            pointer_id=int(msg.get("pointerId", -1)))
                    elif mt == "tap":
                        await loop.run_in_executor(None,
                            session.tap, int(msg["x"]), int(msg["y"]))
                    elif mt == "swipe":
                        await loop.run_in_executor(None,
                            session.swipe,
                            int(msg["x1"]), int(msg["y1"]),
                            int(msg["x2"]), int(msg["y2"]),
                            int(msg.get("duration", 300)))
                    elif mt == "key":
                        await loop.run_in_executor(None,
                            session.press_key, int(msg["keycode"]))
                    elif mt == "text":
                        session.inject_text(msg.get("text", ""))
                    elif mt == "scroll":
                        session.inject_scroll(
                            int(msg.get("x", 0)), int(msg.get("y", 0)),
                            int(msg.get("hscroll", 0)),
                            int(msg.get("vscroll", 0)))
                except Exception:
                    break

        async def _send_video():
            """Stream H.264 frames via a bounded queue.

            The reader thread tolerates transient socket timeouts (up to 3
            consecutive None returns ~ 24 s) instead of treating them as
            stream-end.  When the reader truly stops, _send_video returns
            and the caller cancels _read_controls so the WebSocket closes.
            """
            import threading as _th
            import collections as _col
            _q: _col.deque = _col.deque(maxlen=60)
            _stop = [False]
            _evt = _th.Event()

            def _reader():
                frame_n = 0
                consecutive_none = 0
                logger.info("[stream_ws] reader started for %s", did[:8])
                try:
                    while session.is_running and not _stop[0]:
                        f = session.read_video_frame()
                        if f is None:
                            consecutive_none += 1
                            if not session.is_running or consecutive_none > 3:
                                logger.info(
                                    "[stream_ws] reader stop %s: running=%s "
                                    "none_count=%d frames=%d",
                                    did[:8], session.is_running,
                                    consecutive_none, frame_n)
                                break
                            continue
                        consecutive_none = 0
                        frame_n += 1
                        if frame_n <= 5 or frame_n % 300 == 0:
                            logger.info(
                                "[stream_ws] frame #%d for %s (%d B)",
                                frame_n, did[:8], len(f))
                        _q.append(f)
                        _evt.set()
                except Exception as exc:
                    logger.warning("[stream_ws] reader exception %s: %s",
                                   did[:8], exc)
                finally:
                    logger.info("[stream_ws] reader exiting %s (frames=%d)",
                                did[:8], frame_n)
                    _stop[0] = True
                    _evt.set()

            rt = _th.Thread(target=_reader, daemon=True)
            rt.start()
            try:
                while not _stop[0]:
                    await loop.run_in_executor(None,
                                               lambda: _evt.wait(timeout=2))
                    _evt.clear()
                    while _q:
                        frame = _q.popleft()
                        try:
                            await websocket.send_bytes(frame)
                        except Exception:
                            _stop[0] = True
                            break
            finally:
                _stop[0] = True
                rt.join(timeout=3)

        video_task = asyncio.ensure_future(_send_video())
        ctrl_task = asyncio.ensure_future(_read_controls())
        try:
            done, pending = await asyncio.wait(
                [video_task, ctrl_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        except Exception:
            video_task.cancel()
            ctrl_task.cancel()
    except Exception:
        pass
    finally:
        # 仅减少引用计数，引用为 0 时才真正停止（支持多客户端共享同一 session）
        mgr.release_session(did)
