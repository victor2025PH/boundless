# -*- coding: utf-8 -*-
"""设备控制和交互路由：截图、MJPEG、输入控制、Shell、设备设置、应用、文件、剪贴板、通讯录"""
import json
import logging
import time
from fastapi import APIRouter, HTTPException, Depends

from src.utils.subprocess_text import run as _sp_run_text
from src.host.device_registry import DEFAULT_DEVICES_YAML, data_file

router = APIRouter(prefix="", tags=["devices-control"])
logger = logging.getLogger(__name__)
_config_path = DEFAULT_DEVICES_YAML

# ── Screenshot cache (shared state) ──

_screenshot_cache: dict = {}  # device_id -> (timestamp, jpeg_bytes)
_screenshot_locks: dict = {}  # device_id -> threading.Lock
_screenshot_guard = __import__("threading").Lock()
_SCREENSHOT_TTL = 2.0


@router.get("/devices/{device_id}/screenshot")
def device_screenshot(device_id: str, mode: str = "grid",
                      max_h: int = 0, quality: int = 0):
    """Capture and return a device screenshot as JPEG."""
    import time as _time
    from fastapi.responses import Response
    from src.device_control.device_manager import get_device_manager
    from ..executor import _resolve_serial_from_config

    manager = get_device_manager(_config_path)
    info = manager.get_device_info(device_id)
    if not info:
        serial = _resolve_serial_from_config(_config_path, device_id)
        info = manager.get_device_info(serial)
        if not info:
            raise HTTPException(status_code=404, detail="设备不存在")
        device_id = serial

    is_control = mode == "control"
    ttl = 0.4 if is_control else 1.5
    if not max_h:
        max_h = 900 if is_control else 600
    if not quality:
        quality = 75 if is_control else 60

    now = _time.time()
    cached = _screenshot_cache.get(device_id)
    if cached and (now - cached[0]) < ttl:
        return Response(content=cached[1], media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})

    if device_id not in _screenshot_locks:
        with _screenshot_guard:
            if device_id not in _screenshot_locks:
                _screenshot_locks[device_id] = __import__("threading").Lock()
    lock = _screenshot_locks[device_id]

    if not lock.acquire(timeout=5):
        cached = _screenshot_cache.get(device_id)
        if cached:
            return Response(content=cached[1], media_type="image/jpeg",
                            headers={"Cache-Control": "no-cache"})
        raise HTTPException(status_code=503, detail="截屏繁忙，请稍后重试")

    try:
        cached = _screenshot_cache.get(device_id)
        if cached and (_time.time() - cached[0]) < ttl:
            return Response(content=cached[1], media_type="image/jpeg",
                            headers={"Cache-Control": "no-cache"})

        png_data = manager.capture_screen(device_id)
        if not png_data:
            raise HTTPException(status_code=500, detail="截屏失败")
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(png_data))
            if img.height > max_h:
                ratio = max_h / img.height
                img = img.resize((int(img.width * ratio), max_h), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format='JPEG', quality=quality)
            jpeg_data = buf.getvalue()
        except ImportError:
            jpeg_data = png_data

        _screenshot_cache[device_id] = (_time.time(), jpeg_data)
        return Response(content=jpeg_data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})
    finally:
        lock.release()


# ── MJPEG stream ──

_active_mjpeg_streams: dict = {}


@router.get("/devices/{device_id}/mjpeg")
def device_mjpeg_stream(device_id: str, fps: int = 5, quality: int = 50,
                        max_h: int = 480, adaptive: int = 1):
    """MJPEG stream with adaptive bitrate based on concurrent viewers."""
    from fastapi.responses import StreamingResponse
    import time as _time
    from io import BytesIO
    from src.device_control.device_manager import get_device_manager
    from ..executor import _resolve_serial_from_config

    manager = get_device_manager(_config_path)
    info = manager.get_device_info(device_id)
    if not info:
        serial = _resolve_serial_from_config(_config_path, device_id)
        info = manager.get_device_info(serial)
        if not info:
            raise HTTPException(status_code=404, detail="设备不存在")
        device_id = serial

    fps = max(1, min(fps, 15))

    def _adaptive_params():
        total_streams = sum(_active_mjpeg_streams.values())
        if not adaptive or total_streams <= 2:
            return fps, quality, max_h
        if total_streams <= 4:
            return max(2, fps - 1), max(30, quality - 10), min(max_h, 400)
        if total_streams <= 8:
            return max(1, fps // 2), max(20, quality - 20), min(max_h, 320)
        return 1, 20, 240

    def _generate():
        _active_mjpeg_streams[device_id] = _active_mjpeg_streams.get(device_id, 0) + 1
        boundary = b"--openclaw_mjpeg\r\n"
        try:
            while True:
                a_fps, a_quality, a_max_h = _adaptive_params()
                interval = 1.0 / a_fps
                try:
                    cached = _screenshot_cache.get(device_id)
                    if cached and (_time.time() - cached[0]) < 1.0:
                        jpeg_data = cached[1]
                    else:
                        png_data = manager.capture_screen(device_id)
                        if not png_data:
                            _time.sleep(interval)
                            continue
                        try:
                            from PIL import Image
                            img = Image.open(BytesIO(png_data))
                            if img.height > a_max_h:
                                ratio = a_max_h / img.height
                                img = img.resize((int(img.width * ratio), a_max_h), Image.LANCZOS)
                            buf = BytesIO()
                            img.save(buf, format='JPEG', quality=a_quality)
                            jpeg_data = buf.getvalue()
                        except ImportError:
                            jpeg_data = png_data
                        _screenshot_cache[device_id] = (_time.time(), jpeg_data)

                    yield (boundary +
                           b"Content-Type: image/jpeg\r\n" +
                           b"Content-Length: " + str(len(jpeg_data)).encode() + b"\r\n\r\n" +
                           jpeg_data + b"\r\n")
                except GeneratorExit:
                    return
                except Exception:
                    pass
                _time.sleep(interval)
        finally:
            _active_mjpeg_streams[device_id] = max(0, _active_mjpeg_streams.get(device_id, 1) - 1)

    return StreamingResponse(
        _generate(),
        media_type="multipart/x-mixed-replace; boundary=openclaw_mjpeg",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/mjpeg/stats")
def mjpeg_stats():
    return {
        "active_streams": dict(_active_mjpeg_streams),
        "total": sum(_active_mjpeg_streams.values()),
    }


# ── Screen size ──

@router.get("/devices/{device_id}/screen-size")
def device_screen_size(device_id: str):
    """Get device screen resolution."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    size = manager.get_screen_size(did)
    if not size:
        raise HTTPException(status_code=500, detail="无法获取屏幕尺寸")
    return {"width": size[0], "height": size[1], "device_id": did}


# ── Input control ──

def _record_input_event(device_id: str, action: str, params: dict):
    """If a recording session is active for this device, log the event."""
    from ..api import _op_recordings
    for sid, rec in _op_recordings.items():
        if rec["device_id"] == device_id and rec.get("active"):
            rec["events"].append({
                "action": action,
                "params": params,
                "ts": time.time(),
                "offset": round(time.time() - rec["started_at"], 3),
            })
            break


@router.post("/devices/{device_id}/input/tap")
def device_input_tap(device_id: str, body: dict):
    """Tap at coordinates {x, y} on device screen."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    x, y = int(body.get("x", 0)), int(body.get("y", 0))
    ok = manager.input_tap(did, x, y)
    if not ok:
        raise HTTPException(status_code=500, detail="点击失败")
    _record_input_event(did, "tap", {"x": x, "y": y})
    return {"ok": True, "action": "tap", "x": x, "y": y}


@router.post("/devices/{device_id}/input/swipe")
def device_input_swipe(device_id: str, body: dict):
    """Swipe from (x1,y1) to (x2,y2) with optional duration."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    x1, y1 = int(body.get("x1", 0)), int(body.get("y1", 0))
    x2, y2 = int(body.get("x2", 0)), int(body.get("y2", 0))
    dur = int(body.get("duration", 300))
    ok = manager.input_swipe(did, x1, y1, x2, y2, dur)
    if not ok:
        raise HTTPException(status_code=500, detail="滑动失败")
    _record_input_event(did, "swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": dur})
    return {"ok": True, "action": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2}


@router.post("/devices/{device_id}/input/key")
def device_input_key(device_id: str, body: dict):
    """Send keyevent to device."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    keycode = int(body.get("keycode", 0))
    ok = manager.input_keyevent(did, keycode)
    if not ok:
        raise HTTPException(status_code=500, detail="按键失败")
    _record_input_event(did, "key", {"keycode": keycode})
    return {"ok": True, "action": "key", "keycode": keycode}


@router.post("/devices/{device_id}/input/text")
def device_input_text(device_id: str, body: dict):
    """Type text into the currently focused field on device."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="text 参数不能为空")
    ok = manager.input_text(did, text)
    if not ok:
        raise HTTPException(status_code=500, detail="输入文本失败")
    _record_input_event(did, "text", {"text": text})
    return {"ok": True, "action": "text", "length": len(text)}


# ── Shell command ──

@router.post("/devices/{device_id}/shell")
def device_shell_command(device_id: str, body: dict):
    """Execute an ADB shell command on device and return output."""
    from ..api import _resolve_device_with_manager, _audit

    did, manager = _resolve_device_with_manager(device_id)
    command = body.get("command", "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="command required")
    blocked = ["rm -rf /", "factory_reset", "wipe", "format"]
    for b in blocked:
        if b in command:
            raise HTTPException(status_code=403,
                                detail=f"Blocked command: {b}")
    ok, output = manager.execute_adb_command(f"shell {command}", did)
    _audit("shell_command", did, command[:80])
    return {"ok": ok, "command": command, "output": output,
            "device_id": did}


# ── Device settings: rotate, brightness, screen-toggle, open-app ──

@router.post("/devices/{device_id}/rotate")
def device_rotate(device_id: str, body: dict):
    """Rotate device screen."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    orientation = int(body.get("orientation", 1))
    manager.execute_adb_command(
        "shell settings put system accelerometer_rotation 0", did)
    ok, out = manager.execute_adb_command(
        f"shell settings put system user_rotation {orientation}", did)
    labels = {0: "竖屏", 1: "横屏(左)", 2: "倒置", 3: "横屏(右)"}
    return {"ok": ok, "orientation": orientation,
            "label": labels.get(orientation, str(orientation))}


@router.post("/devices/{device_id}/brightness")
def device_brightness(device_id: str, body: dict):
    """Set device screen brightness (0-255)."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    level = max(0, min(255, int(body.get("level", 128))))
    manager.execute_adb_command(
        "shell settings put system screen_brightness_mode 0", did)
    ok, out = manager.execute_adb_command(
        f"shell settings put system screen_brightness {level}", did)
    return {"ok": ok, "brightness": level}


@router.post("/devices/{device_id}/screen-toggle")
def device_screen_toggle(device_id: str):
    """Toggle screen on/off (power button)."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    ok, _ = manager.execute_adb_command("shell input keyevent 26", did)
    return {"ok": ok, "action": "screen_toggle"}


@router.post("/devices/{device_id}/open-app")
def device_open_app(device_id: str, body: dict):
    """Open an app by package name."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    package = body.get("package", "")
    if not package:
        raise HTTPException(status_code=400, detail="package required")
    ok, out = manager.execute_adb_command(
        f"shell monkey -p {package} -c android.intent.category.LAUNCHER 1",
        did)
    return {"ok": ok, "package": package}


# ── Installed apps & battery ──

@router.get("/devices/{device_id}/installed-apps")
def device_installed_apps(device_id: str):
    """List installed apps (third-party only)."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    ok, output = manager.execute_adb_command("shell pm list packages -3", did)
    if not ok:
        return {"apps": [], "error": output}
    apps = [line.replace("package:", "").strip()
            for line in output.split("\n") if line.strip()]
    return {"apps": sorted(apps), "count": len(apps)}


@router.get("/devices/{device_id}/battery")
def device_battery(device_id: str):
    """Get device battery info."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    ok, output = manager.execute_adb_command("shell dumpsys battery", did)
    if not ok:
        return {"error": output}
    info = {}
    for line in output.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip().lower().replace(" ", "_")] = v.strip()
    return info


# ── File management ──

@router.get("/devices/{device_id}/files")
def device_list_files(device_id: str, path: str = "/sdcard"):
    """List files and directories at the given path on device."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    ok, output = manager.execute_adb_command(
        f"shell ls -la {path}", did)
    if not ok:
        raise HTTPException(status_code=500, detail=output)
    items = []
    for line in output.split("\n"):
        line = line.strip()
        if not line or line.startswith("total"):
            continue
        parts = line.split(None, 7)
        if len(parts) < 7:
            continue
        perms = parts[0]
        is_dir = perms.startswith("d")
        is_link = perms.startswith("l")
        size_str = parts[3] if not is_dir else "0"
        name = parts[-1] if len(parts) >= 8 else parts[-1]
        if name in (".", ".."):
            continue
        if is_link and " -> " in name:
            name = name.split(" -> ")[0]
        try:
            size = int(size_str)
        except ValueError:
            size = 0
        items.append({
            "name": name,
            "is_dir": is_dir,
            "is_link": is_link,
            "size": size,
            "perms": perms,
            "date": f"{parts[4]} {parts[5]}" if len(parts) >= 7 else "",
        })
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"path": path, "items": items, "count": len(items)}


@router.get("/devices/{device_id}/files/download")
def device_download_file(device_id: str, path: str = ""):
    """Download (pull) a file from device."""
    import os
    from ..api import _resolve_device_with_manager

    if not path:
        raise HTTPException(status_code=400, detail="path required")
    did, manager = _resolve_device_with_manager(device_id)
    import tempfile
    tmp = tempfile.mktemp(suffix="_" + os.path.basename(path))
    ok, out = manager.execute_adb_command(f"pull {path} {tmp}", did)
    if not ok or not os.path.exists(tmp):
        raise HTTPException(status_code=500, detail=f"下载失败: {out}")
    from fastapi.responses import FileResponse
    return FileResponse(tmp, filename=os.path.basename(path),
                        media_type="application/octet-stream")


@router.post("/devices/{device_id}/files/upload")
def device_upload_file(device_id: str, dest_path: str = "/sdcard/",
                       file: bytes = None):
    """Upload a file to device via ADB push."""
    raise HTTPException(status_code=501,
                        detail="Use /devices/{id}/files/push with multipart")


@router.post("/devices/{device_id}/files/push")
async def device_push_file(device_id: str, request):
    """Push file content to device. Body: JSON {dest_path, content_base64}."""
    import base64
    import tempfile
    import os
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    body = await request.json()
    dest = body.get("dest_path", "/sdcard/upload.txt")
    content_b64 = body.get("content_base64", "")
    if not content_b64:
        raise HTTPException(status_code=400, detail="content_base64 required")
    raw = base64.b64decode(content_b64)
    tmp = tempfile.mktemp(suffix="_upload")
    with open(tmp, "wb") as f:
        f.write(raw)
    ok, out = manager.execute_adb_command(f"push {tmp} {dest}", did)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    if not ok:
        raise HTTPException(status_code=500, detail=f"上传失败: {out}")
    return {"ok": True, "dest": dest, "size": len(raw)}


@router.delete("/devices/{device_id}/files")
def device_delete_file(device_id: str, path: str = ""):
    """Delete a file or directory on device."""
    from ..api import _resolve_device_with_manager

    if not path:
        raise HTTPException(status_code=400, detail="path required")
    blocked = ["/", "/system", "/data", "/vendor", "/proc"]
    if path.rstrip("/") in blocked:
        raise HTTPException(status_code=403, detail="Cannot delete system path")
    did, manager = _resolve_device_with_manager(device_id)
    ok, out = manager.execute_adb_command(f"shell rm -rf {path}", did)
    if not ok:
        raise HTTPException(status_code=500, detail=f"删除失败: {out}")
    return {"ok": True, "deleted": path}


@router.post("/devices/{device_id}/files/mkdir")
def device_mkdir(device_id: str, body: dict):
    """Create a directory on device."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    path = body.get("path", "")
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    ok, out = manager.execute_adb_command(f"shell mkdir -p {path}", did)
    if not ok:
        raise HTTPException(status_code=500, detail=f"创建目录失败: {out}")
    return {"ok": True, "path": path}


# ── Clipboard sync ──

@router.get("/devices/{device_id}/clipboard")
def device_get_clipboard(device_id: str):
    """Get device clipboard content."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    ok, output = manager.execute_adb_command(
        "shell am broadcast -a clipper.get --es return stdout 2>/dev/null "
        "|| service call clipboard 2 s16 com.android.shell", did)
    if ok:
        for line in output.split("\n"):
            if "data=" in line:
                return {"ok": True, "text": line.split("data=", 1)[1].strip('"')}
            if "String16" in line:
                return {"ok": True, "text": line}
    ok2, out2 = manager.execute_adb_command(
        "shell dumpsys clipboard_service 2>/dev/null || echo ''", did)
    return {"ok": True, "text": output.strip(), "raw": out2.strip()[:500]}


@router.post("/devices/{device_id}/clipboard")
def device_set_clipboard(device_id: str, body: dict):
    """Set device clipboard content."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    safe_text = text.replace("'", "'\\''")
    ok, out = manager.execute_adb_command(
        f"shell am broadcast -a clipper.set -e text '{safe_text}'", did)
    if not ok:
        ok, out = manager.execute_adb_command(
            f"shell input text '{safe_text}'", did)
    return {"ok": ok, "text": text, "output": out}


# ── Contacts ──

@router.get("/devices/{device_id}/contacts")
def get_device_contacts(device_id: str, limit: int = 100):
    """List contacts from device."""
    from ..api import _resolve_device_with_manager
    import re

    did, _ = _resolve_device_with_manager(device_id)
    r = _sp_run_text(
        ["adb", "-s", did, "shell", "content", "query", "--uri", "content://contacts/phones/",
         "--projection", "display_name:number"],
        capture_output=True, timeout=15
    )
    contacts = []
    for line in r.stdout.splitlines():
        name_m = re.search(r'display_name=([^,]+)', line)
        num_m = re.search(r'number=([^,\s]+)', line)
        if name_m:
            contacts.append({
                "name": name_m.group(1).strip(),
                "number": num_m.group(1).strip() if num_m else ""
            })
    return {"device_id": did, "count": len(contacts), "contacts": contacts[:limit]}


@router.post("/devices/{device_id}/contacts/add")
def add_device_contact(device_id: str, body: dict):
    """Add a contact to device. body: {name, number}"""
    from ..api import _resolve_device_with_manager
    import re

    did, _ = _resolve_device_with_manager(device_id)
    name = body.get("name", "")
    number = body.get("number", "")
    if not name or not number:
        raise HTTPException(400, "name and number required")
    cmds = [
        f'content insert --uri content://com.android.contacts/raw_contacts --bind account_type:s: --bind account_name:s:',
        f'content insert --uri content://com.android.contacts/data --bind raw_contact_id:i:{{RID}} --bind mimetype:s:vnd.android.cursor.item/name --bind data1:s:{name}',
        f'content insert --uri content://com.android.contacts/data --bind raw_contact_id:i:{{RID}} --bind mimetype:s:vnd.android.cursor.item/phone_v2 --bind data1:s:{number} --bind data2:i:1',
    ]
    r0 = _sp_run_text(["adb", "-s", did, "shell", cmds[0]],
                        capture_output=True, timeout=10)
    rid_m = re.search(r'uri.*?(\d+)', r0.stdout + r0.stderr)
    rid = rid_m.group(1) if rid_m else "1"
    for cmd in cmds[1:]:
        _sp_run_text(["adb", "-s", did, "shell", cmd.replace("{RID}", rid)],
                       capture_output=True, timeout=10)
    return {"ok": True, "name": name, "number": number}


@router.post("/devices/{device_id}/contacts/batch")
def batch_inject_contacts(device_id: str, body: dict):
    """批量注入联系人。body: {contacts: [{name, number}, ...]}"""
    from ..api import _resolve_device_with_manager
    from src.app_automation.contacts_manager import ContactsManager

    did, _ = _resolve_device_with_manager(device_id)
    contacts = body.get("contacts", [])
    if not contacts:
        raise HTTPException(400, "contacts list is empty")

    mgr = ContactsManager(did)
    result = mgr.inject_contacts(contacts)
    try:
        from ..event_stream import push_event
        push_event("task.completed", {
            "action": "contacts_batch", "success": result.get("success", 0),
            "total": result.get("total", 0),
        }, device_id=device_id)
    except Exception:
        pass
    return {"ok": True, **result}


@router.post("/devices/{device_id}/contacts/import-csv")
def import_contacts_csv(device_id: str, body: dict):
    """从 CSV 文件导入联系人。body: {csv_path: "path/to/file.csv"}"""
    from ..api import _resolve_device_with_manager
    from src.app_automation.contacts_manager import ContactsManager

    did, _ = _resolve_device_with_manager(device_id)
    csv_path = body.get("csv_path", "")
    if not csv_path:
        raise HTTPException(400, "csv_path required")

    contacts = ContactsManager.parse_csv(csv_path)
    if not contacts:
        raise HTTPException(400, f"No contacts found in {csv_path}")

    mgr = ContactsManager(did)
    result = mgr.inject_contacts(contacts)
    return {"ok": True, "file": csv_path, **result}


@router.delete("/devices/{device_id}/contacts/clean")
def clean_injected_contacts(device_id: str):
    """清理所有 OC_ 前缀的已注入联系人"""
    from ..api import _resolve_device_with_manager
    from src.app_automation.contacts_manager import ContactsManager

    did, _ = _resolve_device_with_manager(device_id)
    mgr = ContactsManager(did)
    removed = mgr.clean_injected()
    return {"ok": True, "removed": removed}


@router.get("/devices/{device_id}/contacts/export")
def export_device_contacts(device_id: str, only_injected: bool = False):
    """导出设备通讯录为 JSON（用于跨设备同步）"""
    from ..api import _resolve_device_with_manager
    from src.app_automation.contacts_manager import ContactsManager

    did, _ = _resolve_device_with_manager(device_id)
    mgr = ContactsManager(did)
    contacts = mgr.list_contacts()
    if only_injected:
        contacts = [c for c in contacts if c["name"].startswith("OC_")]
    return {"device_id": did, "count": len(contacts), "contacts": contacts}


@router.get("/devices/{device_id}/contacts/enriched")
def enriched_contacts(device_id: str):
    """通讯录 + 线索/会话数据交叉匹配（一次返回完整状态）"""
    from ..api import _resolve_device_with_manager
    from src.app_automation.contacts_manager import ContactsManager
    import re
    import sqlite3

    did, _ = _resolve_device_with_manager(device_id)
    mgr = ContactsManager(did)
    contacts = mgr.list_contacts()

    # 从会话数据库加载所有已聊天的 lead_id
    chatted_leads: dict = {}
    try:
        db_path = str(data_file("conversations.db"))
        conn = sqlite3.connect(db_path, timeout=5)
        rows = conn.execute(
            "SELECT lead_id, COUNT(*) as cnt, MAX(timestamp) as last_ts "
            "FROM conversations GROUP BY lead_id"
        ).fetchall()
        conn.close()
        for r in rows:
            chatted_leads[r[0].lower()] = {"messages": r[1], "last_time": r[2]}
    except Exception:
        pass

    # 从线索库加载所有线索用户名
    lead_usernames: dict = {}
    try:
        db_path2 = str(data_file("leads.db"))
        conn2 = sqlite3.connect(db_path2, timeout=5)
        rows2 = conn2.execute(
            "SELECT name, score, status, platform FROM leads LIMIT 2000"
        ).fetchall()
        conn2.close()
        for r in rows2:
            lead_usernames[r[0].lower()] = {
                "score": r[1], "status": r[2], "platform": r[3]
            }
    except Exception:
        pass

    # 从好友发现数据库加载匹配结果
    discovered: dict = {}
    try:
        from src.app_automation.contacts_manager import get_discovery_results
        disc_rows = get_discovery_results(device_id)
        for dr in disc_rows:
            discovered[dr["contact_name"].lower()] = dr
    except Exception:
        pass

    enriched = []
    for c in contacts:
        name = c.get("name", "")
        is_injected = name.startswith("OC_")
        clean_name = name[3:] if is_injected else name
        lower_name = clean_name.lower()

        # 多源匹配：leads库 + 好友发现库
        in_leads = lower_name in lead_usernames
        in_discovery = lower_name in discovered
        lead_info = lead_usernames.get(lower_name, {})
        chat_info = chatted_leads.get(lower_name, {})
        disc_info = discovered.get(lower_name, {})
        greeted = chat_info.get("messages", 0) > 0 or disc_info.get("messaged", False)
        matched = in_leads or in_discovery

        enriched.append({
            "name": clean_name,
            "number": c.get("number", ""),
            "source": "injected" if is_injected else "original",
            "matched_app": matched,
            "platform": lead_info.get("platform", "") or disc_info.get("platform", ""),
            "lead_score": lead_info.get("score", 0) if in_leads else 0,
            "lead_status": lead_info.get("status", "") if in_leads else "",
            "greeted": greeted,
            "messages": chat_info.get("messages", 0),
            "last_chat": chat_info.get("last_time", "") or disc_info.get("matched_at", ""),
            "followed": disc_info.get("followed", False),
            "discovery_time": disc_info.get("matched_at", ""),
        })

    stats = {
        "total": len(enriched),
        "injected": sum(1 for e in enriched if e["source"] == "injected"),
        "matched": sum(1 for e in enriched if e["matched_app"]),
        "greeted": sum(1 for e in enriched if e["greeted"]),
        "followed": sum(1 for e in enriched if e.get("followed")),
    }

    return {"device_id": did, "stats": stats, "contacts": enriched}


@router.get("/contacts/dedup-check")
def contacts_dedup_check():
    """跨设备通讯录去重检测 — 找出分布在多台设备上的重复号码"""
    import re

    # 获取所有在线设备
    try:
        from ..api import _resolve_device_with_manager
        r = _sp_run_text(
            ["adb", "devices"], capture_output=True, timeout=10
        )
        serials = []
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
    except Exception:
        serials = []

    # 每台设备的联系人
    device_contacts: dict = {}
    number_to_devices: dict = {}

    for serial in serials:
        try:
            out = _sp_run_text(
                ["adb", "-s", serial, "shell", "content", "query",
                 "--uri", "content://contacts/phones/",
                 "--projection", "display_name:number"],
                capture_output=True, timeout=15
            )
            contacts = []
            for line in out.stdout.splitlines():
                name_m = re.search(r'display_name=([^,]+)', line)
                num_m = re.search(r'number=([^,\s]+)', line)
                if name_m and num_m:
                    num = re.sub(r'[\s\-\(\)\.]+', '', num_m.group(1).strip())
                    name = name_m.group(1).strip()
                    contacts.append({"name": name, "number": num})
                    if num not in number_to_devices:
                        number_to_devices[num] = []
                    number_to_devices[num].append(serial)
            device_contacts[serial] = len(contacts)
        except Exception:
            device_contacts[serial] = 0

    # 过滤出重复项（出现在 2+ 设备上）
    duplicates = []
    for num, devices in number_to_devices.items():
        if len(devices) > 1:
            duplicates.append({
                "number": num,
                "device_count": len(devices),
                "devices": devices,
            })

    duplicates.sort(key=lambda d: d["device_count"], reverse=True)

    return {
        "total_devices": len(serials),
        "device_contacts": device_contacts,
        "duplicates": duplicates[:100],
        "duplicate_count": len(duplicates),
    }


# ── App manager ──

@router.get("/devices/{device_id}/apps")
def list_device_apps(device_id: str, third_party: bool = True):
    """List installed apps on device."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    flag = "-3" if third_party else ""
    r = _sp_run_text(["adb", "-s", did, "shell", "pm", "list", "packages", flag],
                       capture_output=True, timeout=15)
    if r.returncode != 0:
        raise HTTPException(500, r.stderr[:200])
    packages = [line.replace("package:", "").strip() for line in r.stdout.splitlines() if line.startswith("package:")]
    return {"device_id": did, "count": len(packages), "packages": sorted(packages)}


@router.post("/devices/{device_id}/apps/action")
def device_app_action(device_id: str, body: dict):
    """Perform action on app: start, stop, clear, uninstall."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    action = body.get("action", "")
    package = body.get("package", "")
    if not package:
        raise HTTPException(400, "package required")
    cmds = {
        "start": ["adb", "-s", did, "shell", "monkey", "-p", package, "-c",
                   "android.intent.category.LAUNCHER", "1"],
        "stop": ["adb", "-s", did, "shell", "am", "force-stop", package],
        "clear": ["adb", "-s", did, "shell", "pm", "clear", package],
        "uninstall": ["adb", "-s", did, "shell", "pm", "uninstall", package],
    }
    cmd = cmds.get(action)
    if not cmd:
        raise HTTPException(400, f"Unknown action: {action}")
    r = _sp_run_text(cmd, capture_output=True, timeout=15)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()[:300]}


# ════════════════════════════════════════════════════════
# 操作日志归档 — localStorage → SQLite 持久化
# ════════════════════════════════════════════════════════

_OPS_DB = str(data_file("operation_logs.db"))


def _ensure_ops_db():
    """按需创建操作日志表"""
    import sqlite3
    conn = sqlite3.connect(_OPS_DB, timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS ops_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT, devices INTEGER, success INTEGER,
        contacts INTEGER, file TEXT, ts TEXT,
        synced_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()


@router.post("/ops-logs/sync")
def sync_ops_logs(body: dict):
    """前端 localStorage 批量同步到后端"""
    import sqlite3
    logs = body.get("logs", [])
    if not logs:
        return {"synced": 0}
    _ensure_ops_db()
    conn = sqlite3.connect(_OPS_DB, timeout=5)
    inserted = 0
    for entry in logs[:200]:
        ts = entry.get("ts", "")
        exists = conn.execute("SELECT 1 FROM ops_logs WHERE ts=? LIMIT 1", (ts,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO ops_logs(action,devices,success,contacts,file,ts) VALUES(?,?,?,?,?,?)",
            (entry.get("action", ""), entry.get("devices", 0),
             entry.get("success", 0), entry.get("contacts", 0),
             entry.get("file", ""), ts)
        )
        inserted += 1
    conn.commit()
    conn.close()
    return {"synced": inserted, "total_received": len(logs)}


@router.get("/ops-logs")
def get_ops_logs(limit: int = 100, offset: int = 0):
    """获取归档操作日志（支持分页）"""
    import sqlite3
    _ensure_ops_db()
    conn = sqlite3.connect(_OPS_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ops_logs ORDER BY ts DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM ops_logs").fetchone()[0]
    conn.close()
    return {"logs": [dict(r) for r in rows], "total": total}


@router.get("/ops-logs/stats")
def ops_logs_stats():
    """操作日志统计摘要"""
    import sqlite3
    _ensure_ops_db()
    conn = sqlite3.connect(_OPS_DB, timeout=5)
    total = conn.execute("SELECT COUNT(*) FROM ops_logs").fetchone()[0]
    total_contacts = conn.execute("SELECT COALESCE(SUM(contacts),0) FROM ops_logs").fetchone()[0]
    total_devices = conn.execute("SELECT COALESCE(SUM(devices),0) FROM ops_logs").fetchone()[0]
    recent = conn.execute("SELECT action, ts FROM ops_logs ORDER BY ts DESC LIMIT 1").fetchone()
    conn.close()
    return {
        "total_operations": total,
        "total_contacts_imported": total_contacts,
        "total_device_operations": total_devices,
        "last_operation": {"action": recent[0], "ts": recent[1]} if recent else None,
    }


# ════════════════════════════════════════════════════════
# 设备标签（轻量 JSON，便于筛选/分组）
# ════════════════════════════════════════════════════════

_TAGS_FILE = data_file("device_tags.json")
_tags_lock = __import__("threading").Lock()


def _read_device_tags() -> dict:
    with _tags_lock:
        if not _TAGS_FILE.exists():
            return {}
        try:
            return json.loads(_TAGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


def _write_device_tags(data: dict) -> None:
    _TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _tags_lock:
        _TAGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


@router.get("/devices/tags/all")
def list_all_device_tags():
    """返回 device_id -> [tag, ...]"""
    return {"tags_by_device": _read_device_tags()}


@router.get("/devices/{device_id}/tags")
def get_device_tags(device_id: str):
    from ..api import _resolve_device_with_manager
    did, _ = _resolve_device_with_manager(device_id)
    m = _read_device_tags()
    return {"device_id": did, "tags": m.get(did, [])}


@router.put("/devices/{device_id}/tags")
def put_device_tags(device_id: str, body: dict):
    from ..api import _resolve_device_with_manager
    did, _ = _resolve_device_with_manager(device_id)
    tags = body.get("tags")
    if not isinstance(tags, list):
        raise HTTPException(400, "tags must be a list of strings")
    tags = [str(t).strip() for t in tags if str(t).strip()][:32]
    m = _read_device_tags()
    m[did] = tags
    _write_device_tags(m)
    return {"ok": True, "device_id": did, "tags": tags}


# ════════════════════════════════════════════════════════
# 对话质检标注（按 lead 字符串键）
# ════════════════════════════════════════════════════════

_QA_FILE = data_file("conversation_qa.json")


def _read_qa() -> dict:
    if not _QA_FILE.exists():
        return {}
    try:
        return json.loads(_QA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_qa(data: dict) -> None:
    _QA_FILE.parent.mkdir(parents=True, exist_ok=True)
    _QA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/conversations/qa/{lead_key:path}")
def get_conversation_qa(lead_key: str):
    """lead_key 可为 TikTok 用户名等 URL 安全字符串"""
    k = lead_key.strip()
    m = _read_qa()
    return {"lead_key": k, "qa": m.get(k)}


@router.post("/conversations/qa")
def post_conversation_qa(body: dict):
    """标注一条对话质量：good / bad / neutral"""
    k = (body.get("lead_key") or body.get("lead_id") or "").strip()
    if not k:
        raise HTTPException(400, "lead_key required")
    label = (body.get("label") or "neutral").lower()
    if label not in ("good", "bad", "neutral"):
        raise HTTPException(400, "label must be good|bad|neutral")
    note = str(body.get("note") or "")[:2000]
    import datetime as _dt
    m = _read_qa()
    m[k] = {
        "label": label,
        "note": note,
        "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    _write_qa(m)
    return {"ok": True, "lead_key": k, "qa": m[k]}
