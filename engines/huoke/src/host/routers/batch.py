# -*- coding: utf-8 -*-
"""批量操作路由 — 批量安装/输入/操作/上传/截图/快捷操作、同步镜像、分组控制。"""

import logging
from fastapi import APIRouter, HTTPException, Depends, Request

from src.utils.subprocess_text import run as _sp_run_text

logger = logging.getLogger(__name__)
router = APIRouter(tags=["batch"])


# ══════════════════════════════════════════════
# POST /batch/install-apk
# ══════════════════════════════════════════════

@router.post("/batch/install-apk")
async def batch_install_apk(request: Request):
    """Upload APK and install to multiple devices."""
    import tempfile, shutil, base64
    from pathlib import Path
    from ..api import _config_path, _audit
    from src.device_control.device_manager import get_device_manager

    body = await request.json()
    apk_b64 = body.get("apk_data", "")
    filename = body.get("filename", "app.apk")
    target = body.get("target", "all")
    device_ids_body = body.get("device_ids")
    if not apk_b64:
        raise HTTPException(400, "No APK data")
    apk_bytes = base64.b64decode(apk_b64)
    tmp = Path(tempfile.mkdtemp())
    apk_path = tmp / filename
    with open(apk_path, "wb") as f:
        f.write(apk_bytes)
    manager = get_device_manager(_config_path)
    online_ids = {d.device_id for d in manager.get_all_devices() if d.is_online}
    if device_ids_body is not None and isinstance(device_ids_body, list):
        device_ids = [str(x).strip() for x in device_ids_body if x]
        device_ids = [d for d in device_ids if d in online_ids]
        if not device_ids:
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass
            raise HTTPException(400, "No online devices match device_ids")
    elif target == "all":
        device_ids = list(online_ids)
    else:
        device_ids = [target] if isinstance(target, str) else list(target)
        device_ids = [d for d in device_ids if d in online_ids]
        if not device_ids:
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass
            raise HTTPException(400, "No matching online devices for target")
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _install(did):
        # 用 push + pm install 绕开 MIUI 14+ 的 securitycenter/AdbInstallActivity 拦截
        from src.utils.safe_apk_install import safe_install_apk
        try:
            success, msg = safe_install_apk(
                "adb", did, str(apk_path), replace=True, timeout=300)
            return success, msg
        except Exception as e:
            return False, str(e)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_install, did): did for did in device_ids}
        for fut in futs:
            did = futs[fut]
            ok, msg = fut.result()
            results[did] = {"success": ok, "message": msg.strip()[:200]}
    try:
        shutil.rmtree(tmp)
    except Exception:
        pass
    _audit("batch_install_apk", detail=f"filename={filename}, devices={len(device_ids)}")
    return {
        "total": len(results),
        "success": sum(1 for v in results.values() if v["success"]),
        "results": results,
    }


# ══════════════════════════════════════════════
# POST /batch/text-input
# ══════════════════════════════════════════════

@router.post("/batch/text-input")
def batch_text_input(body: dict):
    """Send different text to different devices. body: {entries: [{device_id, text}], mode: type|clipboard|broadcast}"""
    from ..api import _config_path
    from src.device_control.device_manager import get_device_manager

    entries = body.get("entries", [])
    mode = body.get("mode", "type")
    broadcast_text = body.get("broadcast_text", "")
    manager = get_device_manager(_config_path)
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _send(did, text):
        try:
            if mode == "clipboard":
                import subprocess
                subprocess.run(["adb", "-s", did, "shell", "am", "broadcast",
                                "-a", "clipper.set", "-e", "text", text],
                               capture_output=True, timeout=10)
                return True, "已复制"
            else:
                escaped = text.replace(" ", "%s").replace("&", "\\&").replace("'", "\\'")
                r = _sp_run_text(["adb", "-s", did, "shell", "input", "text", escaped],
                                 capture_output=True, timeout=15)
                return r.returncode == 0, "已输入"
        except Exception as e:
            return False, str(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        if mode == "broadcast" and broadcast_text:
            online = [d.device_id for d in manager.get_all_devices() if d.is_online]
            futs = {pool.submit(_send, did, broadcast_text): did for did in online}
        else:
            futs = {pool.submit(_send, e["device_id"], e["text"]): e["device_id"]
                    for e in entries if e.get("device_id") and e.get("text")}
        for fut in futs:
            did = futs[fut]
            ok, msg = fut.result()
            results[did] = {"success": ok, "message": msg}
    return {"total": len(results), "results": results}


# ══════════════════════════════════════════════
# POST /batch/app-action
# ══════════════════════════════════════════════

@router.post("/batch/app-action")
def batch_app_action(body: dict):
    """Batch app action across multiple devices. body: {action, package, device_ids?}"""
    from ..api import _config_path, _audit
    from src.device_control.device_manager import get_device_manager

    action = body.get("action", "")
    package = body.get("package", "")
    device_ids = body.get("device_ids", [])
    if not package or not action:
        raise HTTPException(400, "action and package required")
    manager = get_device_manager(_config_path)
    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _do(did):
        cmds = {
            "start": ["adb", "-s", did, "shell", "monkey", "-p", package, "-c",
                       "android.intent.category.LAUNCHER", "1"],
            "stop": ["adb", "-s", did, "shell", "am", "force-stop", package],
            "clear": ["adb", "-s", did, "shell", "pm", "clear", package],
            "uninstall": ["adb", "-s", did, "shell", "pm", "uninstall", package],
        }
        try:
            r = _sp_run_text(cmds[action], capture_output=True, timeout=15)
            return r.returncode == 0, (r.stdout + r.stderr).strip()[:200]
        except Exception as e:
            return False, str(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_do, did): did for did in device_ids}
        for fut in futs:
            did = futs[fut]
            ok, msg = fut.result()
            results[did] = {"success": ok, "message": msg}
    _audit("batch_app_action", detail=f"action={action}, package={package}, count={len(results)}")
    return {"total": len(results), "results": results}


# ══════════════════════════════════════════════
# POST /batch/upload-file
# ══════════════════════════════════════════════

@router.post("/batch/upload-file")
async def batch_upload_file(request: Request):
    """Upload a file to multiple devices. body: {data_b64, filename, dest_dir?, device_ids?}"""
    import base64, tempfile, shutil
    from pathlib import Path
    from ..api import _config_path, _audit
    from src.device_control.device_manager import get_device_manager

    body = await request.json()
    data_b64 = body.get("data_b64", "")
    filename = body.get("filename", "file")
    dest_dir = body.get("dest_dir", "/sdcard/Download/")
    device_ids = body.get("device_ids", [])
    if not data_b64:
        raise HTTPException(400, "No file data")
    file_bytes = base64.b64decode(data_b64)
    tmp = Path(tempfile.mkdtemp())
    local_path = tmp / filename
    with open(local_path, "wb") as f:
        f.write(file_bytes)
    manager = get_device_manager(_config_path)
    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _push(did):
        try:
            remote = dest_dir.rstrip("/") + "/" + filename
            r = _sp_run_text(["adb", "-s", did, "push", str(local_path), remote],
                             capture_output=True, timeout=120)
            return r.returncode == 0, (r.stdout + r.stderr).strip()[:200]
        except Exception as e:
            return False, str(e)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_push, did): did for did in device_ids}
        for fut in futs:
            did = futs[fut]
            ok, msg = fut.result()
            results[did] = {"success": ok, "message": msg}
    try:
        shutil.rmtree(tmp)
    except Exception:
        pass
    _audit("batch_upload_file", detail=f"file={filename}, devices={len(device_ids)}")
    return {"total": len(results), "success": sum(1 for v in results.values() if v["success"]), "results": results}


# ══════════════════════════════════════════════
# POST /batch/screenshot
# ══════════════════════════════════════════════

@router.post("/batch/screenshot")
def batch_screenshot(body: dict = None):
    """Take screenshots from multiple devices and return as base64."""
    from ..api import _config_path
    from src.device_control.device_manager import get_device_manager

    body = body or {}
    device_ids = body.get("device_ids", [])
    manager = get_device_manager(_config_path)
    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]
    from concurrent.futures import ThreadPoolExecutor
    import subprocess, base64
    results = {}

    def _shot(did):
        try:
            r = subprocess.run(["adb", "-s", did, "exec-out", "screencap", "-p"],
                               capture_output=True, timeout=10)
            if r.returncode == 0 and len(r.stdout) > 100:
                return True, base64.b64encode(r.stdout).decode()[:500000]
            return False, ""
        except Exception:
            return False, ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_shot, did): did for did in device_ids}
        for fut in futs:
            did = futs[fut]
            ok, data = fut.result()
            results[did] = {"success": ok, "image_b64": data if ok else ""}
    return {"total": len(results), "results": results}


# ══════════════════════════════════════════════
# POST /batch/quick-action
# ══════════════════════════════════════════════

@router.post("/batch/quick-action")
def batch_quick_action(body: dict):
    """Execute a quick action on multiple devices.
    body: {action: reboot|screenshot|volume_up|volume_down|brightness|lock|unlock, device_ids?, value?}"""
    from ..api import _config_path, _audit
    from src.device_control.device_manager import get_device_manager

    action = body.get("action", "")
    device_ids = body.get("device_ids", [])
    value = body.get("value", "")
    manager = get_device_manager(_config_path)
    if not device_ids:
        all_devs = manager.get_all_devices()
        device_ids = [
            (d.device_id if hasattr(d, 'device_id') else d.get('device_id', ''))
            for d in all_devs
            if (getattr(d, 'is_online', False) if hasattr(d, 'is_online')
                else (d.get('status') in ('connected', 'online') if isinstance(d, dict) else False))
        ]
    action_cmds = {
        "reboot": "reboot",
        "volume_up": "input keyevent 24",
        "volume_down": "input keyevent 25",
        "lock": "input keyevent 26",
        "unlock": "input keyevent 26 && sleep 1 && input swipe 540 1800 540 800 300",
        "wifi_on": "svc wifi enable",
        "wifi_off": "svc wifi disable",
        "airplane_on": "settings put global airplane_mode_on 1",
        "airplane_off": "settings put global airplane_mode_on 0",
        "screenshot": "screencap -p /sdcard/openclaw_screenshot.png",
    }
    if action == "brightness" and value:
        cmd = f"settings put system screen_brightness {value}"
    else:
        cmd = action_cmds.get(action)
    if not cmd:
        raise HTTPException(400, f"Unknown action: {action}")
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _do(did):
        try:
            if action == "reboot":
                r = _sp_run_text(["adb", "-s", did, action_cmds["reboot"]],
                                 capture_output=True, timeout=10)
            else:
                r = _sp_run_text(["adb", "-s", did, "shell", cmd],
                                 capture_output=True, timeout=10)
            return r.returncode == 0, (r.stdout + r.stderr).strip()[:100]
        except Exception as e:
            return False, str(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_do, did): did for did in device_ids}
        for fut in futs:
            did = futs[fut]
            ok, msg = fut.result()
            results[did] = {"success": ok, "message": msg}
    _audit("batch_quick_action", detail=f"action={action}, count={len(results)}")
    return {"total": len(results), "results": results}


# ══════════════════════════════════════════════
# Sync Mirror Operations — /sync/touch, /sync/key, /sync/text
# ══════════════════════════════════════════════

@router.post("/sync/touch")
def sync_touch(body: dict):
    """Mirror a touch event to multiple devices.
    body: {x_pct, y_pct, action: tap|swipe|long_press, device_ids?, end_x_pct?, end_y_pct?, duration?}"""
    from ..api import _config_path
    from src.device_control.device_manager import get_device_manager

    x_pct = float(body.get("x_pct", 50))
    y_pct = float(body.get("y_pct", 50))
    action = body.get("action", "tap")
    device_ids = body.get("device_ids", [])
    end_x_pct = float(body.get("end_x_pct", x_pct))
    end_y_pct = float(body.get("end_y_pct", y_pct))
    duration = int(body.get("duration", 300))
    manager = get_device_manager(_config_path)
    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _touch(did):
        try:
            info = manager.get_device_info(did)
            w = getattr(info, 'resolution', {}).get('width', 720) if info else 720
            h = getattr(info, 'resolution', {}).get('height', 1600) if info else 1600
            x = int(w * x_pct / 100)
            y = int(h * y_pct / 100)
            if action == "tap":
                cmd = f"input tap {x} {y}"
            elif action == "swipe":
                ex = int(w * end_x_pct / 100)
                ey = int(h * end_y_pct / 100)
                cmd = f"input swipe {x} {y} {ex} {ey} {duration}"
            elif action == "long_press":
                cmd = f"input swipe {x} {y} {x} {y} {duration}"
            else:
                cmd = f"input tap {x} {y}"
            r = _sp_run_text(["adb", "-s", did, "shell", cmd],
                             capture_output=True, timeout=10)
            return r.returncode == 0
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_touch, did): did for did in device_ids}
        for fut in futs:
            did = futs[fut]
            results[did] = fut.result()
    return {"total": len(results), "success": sum(1 for v in results.values() if v)}


@router.post("/sync/key")
def sync_key(body: dict):
    """Mirror a key event to multiple devices. body: {keycode, device_ids?}"""
    from ..api import _config_path
    from src.device_control.device_manager import get_device_manager

    keycode = int(body.get("keycode", 0))
    device_ids = body.get("device_ids", [])
    manager = get_device_manager(_config_path)
    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _key(did):
        try:
            r = _sp_run_text(["adb", "-s", did, "shell", "input", "keyevent", str(keycode)],
                             capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_key, did): did for did in device_ids}
        for fut in futs:
            results[futs[fut]] = fut.result()
    return {"total": len(results), "success": sum(1 for v in results.values() if v)}


@router.post("/sync/text")
def sync_text(body: dict):
    """Mirror text input to multiple devices. body: {text, device_ids?}"""
    from ..api import _config_path
    from src.device_control.device_manager import get_device_manager

    text = body.get("text", "")
    device_ids = body.get("device_ids", [])
    if not text:
        raise HTTPException(400, "text required")
    manager = get_device_manager(_config_path)
    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]
    from concurrent.futures import ThreadPoolExecutor
    escaped = text.replace(" ", "%s").replace("&", "\\&")
    results = {}

    def _text(did):
        try:
            r = _sp_run_text(["adb", "-s", did, "shell", "input", "text", escaped],
                             capture_output=True, timeout=10)
            return r.returncode == 0
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_text, did): did for did in device_ids}
        for fut in futs:
            results[futs[fut]] = fut.result()
    return {"total": len(results), "success": sum(1 for v in results.values() if v)}


# ══════════════════════════════════════════════
# Group Control — /devices/group/*
# ══════════════════════════════════════════════

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


def _group_exec(device_ids: list, fn, *args) -> dict:
    """Execute fn(manager, device_id, *args) on all devices concurrently."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..api import _config_path
    from src.device_control.device_manager import get_device_manager
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


@router.post("/devices/group/tap")
def group_tap(body: dict):
    """Tap at (x, y) -- scrcpy sync broadcast (~30ms) with ADB fallback."""
    device_ids = body.get("device_ids", [])
    x, y = int(body.get("x", 0)), int(body.get("y", 0))
    sync = _try_scrcpy_broadcast(device_ids, "tap", x=x, y=y)
    if sync is not None:
        return {"action": "tap", "x": x, "y": y, "method": "scrcpy_sync", "results": sync}
    results = _group_exec(device_ids, lambda m, d, _x, _y: m.input_tap(d, _x, _y), x, y)
    return {"action": "tap", "x": x, "y": y, "method": "adb", "results": results}


@router.post("/devices/group/swipe")
def group_swipe(body: dict):
    """Swipe -- scrcpy sync broadcast with ADB fallback."""
    device_ids = body.get("device_ids", [])
    x1, y1 = int(body.get("x1", 0)), int(body.get("y1", 0))
    x2, y2 = int(body.get("x2", 0)), int(body.get("y2", 0))
    dur = int(body.get("duration", 300))
    sync = _try_scrcpy_broadcast(device_ids, "swipe",
                                  x1=x1, y1=y1, x2=x2, y2=y2, duration=dur)
    if sync is not None:
        return {"action": "swipe", "method": "scrcpy_sync", "results": sync}
    results = _group_exec(
        device_ids,
        lambda m, d, a, b, c, e, f: m.input_swipe(d, a, b, c, e, f),
        x1, y1, x2, y2, dur)
    return {"action": "swipe", "method": "adb", "results": results}


@router.post("/devices/group/key")
def group_key(body: dict):
    """Send keycode -- scrcpy sync broadcast with ADB fallback."""
    device_ids = body.get("device_ids", [])
    keycode = int(body.get("keycode", 0))
    sync = _try_scrcpy_broadcast(device_ids, "key", keycode=keycode)
    if sync is not None:
        return {"action": "key", "keycode": keycode, "method": "scrcpy_sync", "results": sync}
    results = _group_exec(device_ids, lambda m, d, k: m.input_key(d, k), keycode)
    return {"action": "key", "keycode": keycode, "method": "adb", "results": results}


@router.post("/devices/group/text")
def group_text(body: dict):
    """Input text on multiple devices (concurrent)."""
    device_ids = body.get("device_ids", [])
    text = body.get("text", "")
    results = _group_exec(device_ids, lambda m, d, t: m.input_text(d, t), text)
    return {"action": "text", "results": results}


@router.post("/devices/group/task")
def group_task(body: dict):
    """Launch a task on multiple devices."""
    device_ids = body.get("device_ids", [])
    from src.host.task_origin import with_origin

    task_type = body.get("task_type", "")
    task_params = with_origin(body.get("params", {}), "group_grid")
    results = {}
    from ..api import task_store, get_worker_pool, run_task, _config_path
    pool = get_worker_pool()
    for did in device_ids:
        try:
            task_id = task_store.create_task(
                task_type=task_type, device_id=did, params=task_params)
            pool.submit(task_id, did, run_task, task_id, _config_path)
            results[did[:12]] = task_id
        except Exception as e:
            results[did[:12]] = str(e)
    return {"action": "task", "task_type": task_type, "results": results}


# ══════════════════════════════════════════════
# POST /batch/install-apk-cluster  （与 /cluster/batch/install-apk 相同，便于反代与 /batch/* 同源）
# ══════════════════════════════════════════════


@router.post("/batch/install-apk-cluster")
async def batch_install_apk_cluster(request: Request):
    """主控向各 Worker 转发 APK；实现见 cluster.run_cluster_batch_install_apk。"""
    from .cluster import run_cluster_batch_install_apk

    return await run_cluster_batch_install_apk(request)
