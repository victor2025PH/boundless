# -*- coding: utf-8 -*-
"""宏录制与回放路由。"""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Depends
from .auth import verify_api_key
from src.device_control.device_manager import get_device_manager
from src.host.device_registry import DEFAULT_DEVICES_YAML, PROJECT_ROOT

router = APIRouter(tags=["macros"])
_project_root = PROJECT_ROOT
_config_path = DEFAULT_DEVICES_YAML

# ── Operation Recording state ──
_op_recordings: dict = {}  # session_id -> {device_id, events: [], started_at, ...}
_op_saved_dir = _project_root / "recordings"


# ── Recording Session API ──

@router.post("/recording/start", dependencies=[Depends(verify_api_key)])
def start_recording(body: dict):
    """Start recording input operations on a device."""
    device_id = body.get("device_id", "")
    if not device_id:
        raise HTTPException(400, "device_id required")
    sid = uuid.uuid4().hex[:10]
    _op_recordings[sid] = {
        "device_id": device_id,
        "events": [],
        "started_at": time.time(),
        "active": True,
        "name": body.get("name", f"Recording {time.strftime('%H:%M:%S')}"),
    }
    return {"ok": True, "session_id": sid}


@router.post("/recording/stop", dependencies=[Depends(verify_api_key)])
def stop_recording(body: dict):
    """Stop a recording session and optionally save it."""
    sid = body.get("session_id", "")
    rec = _op_recordings.get(sid)
    if not rec:
        raise HTTPException(404, "Recording session not found")
    rec["active"] = False
    rec["stopped_at"] = time.time()
    rec["duration"] = round(rec["stopped_at"] - rec["started_at"], 2)

    _op_saved_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{sid}_{rec['device_id'][:8]}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(_op_saved_dir / filename, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)

    return {"ok": True, "events": len(rec["events"]), "duration": rec["duration"],
            "filename": filename}


@router.get("/recording/list", dependencies=[Depends(verify_api_key)])
def list_recordings():
    """List saved recording files."""
    _op_saved_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(_op_saved_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    result = []
    for f in files[:50]:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            result.append({
                "filename": f.name,
                "name": data.get("name", f.name),
                "device_id": data.get("device_id", ""),
                "events": len(data.get("events", [])),
                "duration": data.get("duration", 0),
                "started_at": data.get("started_at", 0),
            })
        except Exception:
            pass
    return {"recordings": result}


@router.post("/recording/replay", dependencies=[Depends(verify_api_key)])
def replay_recording(body: dict):
    """Replay a recorded operation on a device (same or different).
    body: {filename, target_device_id?, speed?}"""
    filename = body.get("filename", "")
    target_device = body.get("target_device_id", "")
    speed = body.get("speed", 1.0)

    filepath = _op_saved_dir / filename
    if not filepath.exists():
        raise HTTPException(404, "Recording file not found")

    with open(filepath, "r", encoding="utf-8") as f:
        rec = json.load(f)

    device_id = target_device or rec.get("device_id", "")
    if not device_id:
        raise HTTPException(400, "No target device")

    events = rec.get("events", [])
    if not events:
        return {"ok": True, "replayed": 0}

    import threading
    def _do_replay():
        manager = get_device_manager(_config_path)
        for i, ev in enumerate(events):
            if i > 0 and speed > 0:
                delay = (ev["offset"] - events[i - 1]["offset"]) / speed
                if delay > 0:
                    time.sleep(min(delay, 5.0))
            action = ev["action"]
            p = ev["params"]
            try:
                if action == "tap":
                    manager.input_tap(device_id, p["x"], p["y"])
                elif action == "swipe":
                    manager.input_swipe(device_id, p["x1"], p["y1"], p["x2"], p["y2"],
                                        p.get("duration", 300))
                elif action == "key":
                    manager.input_keyevent(device_id, p["keycode"])
                elif action == "text":
                    manager.input_text(device_id, p.get("text", ""))
            except Exception:
                pass

    threading.Thread(target=_do_replay, daemon=True).start()
    return {"ok": True, "replaying": len(events), "device": device_id, "speed": speed}


@router.delete("/recording/{filename}", dependencies=[Depends(verify_api_key)])
def delete_recording(filename: str):
    filepath = _op_saved_dir / filename
    if filepath.exists():
        filepath.unlink()
    return {"ok": True}


# ── Macro Recording & Playback API ──

@router.get("/macros", dependencies=[Depends(verify_api_key)])
def list_macros():
    """List all saved macros."""
    from src.utils.macro_recorder import get_macro_store
    return {"macros": get_macro_store().list_macros()}


@router.post("/macros", dependencies=[Depends(verify_api_key)])
def save_macro(body: dict):
    """Save a recorded macro."""
    from src.utils.macro_recorder import get_macro_store
    store = get_macro_store()
    filename = store.save(body)
    return {"ok": True, "filename": filename}


@router.get("/macros/{filename}")
def get_macro(filename: str):
    """Get a macro by filename."""
    from src.utils.macro_recorder import get_macro_store
    macro = get_macro_store().load(filename)
    if not macro:
        raise HTTPException(status_code=404, detail="宏不存在")
    return macro


@router.delete("/macros/{filename}", dependencies=[Depends(verify_api_key)])
def delete_macro(filename: str):
    """Delete a macro."""
    from src.utils.macro_recorder import get_macro_store
    ok = get_macro_store().delete(filename)
    return {"ok": ok}


@router.post("/macros/{filename}/play", dependencies=[Depends(verify_api_key)])
def play_macro(filename: str, body: dict = None):
    """Play a macro on specified devices."""
    body = body or {}
    device_ids = body.get("device_ids", [])
    speed = float(body.get("speed", 1.0))
    repeat = int(body.get("repeat", 1))

    if not device_ids:
        raise HTTPException(status_code=400, detail="需要指定设备")

    from src.utils.macro_recorder import get_macro_store, get_macro_player
    store = get_macro_store()
    macro = store.load(filename)
    if not macro:
        raise HTTPException(status_code=404, detail="宏不存在")

    manager = get_device_manager(_config_path)
    player = get_macro_player(manager)
    results = player.play_group(macro, device_ids, speed, repeat)
    return {
        "ok": True,
        "macro": filename,
        "speed": speed,
        "repeat": repeat,
        "results": results,
    }


@router.post("/macros/stop/{device_id}", dependencies=[Depends(verify_api_key)])
def stop_macro(device_id: str):
    """Stop macro playback on a device."""
    from src.utils.macro_recorder import get_macro_player
    player = get_macro_player()
    player.stop(device_id)
    return {"ok": True}


@router.post("/macros/pause/{device_id}", dependencies=[Depends(verify_api_key)])
def pause_macro(device_id: str):
    """Pause macro playback."""
    from src.utils.macro_recorder import get_macro_player
    player = get_macro_player()
    player.pause(device_id)
    return {"ok": True, "paused": True}


@router.post("/macros/resume/{device_id}", dependencies=[Depends(verify_api_key)])
def resume_macro(device_id: str):
    """Resume paused macro playback."""
    from src.utils.macro_recorder import get_macro_player
    player = get_macro_player()
    player.resume(device_id)
    return {"ok": True, "paused": False}


@router.get("/macros/progress/{device_id}")
def macro_progress(device_id: str):
    """Get macro playback progress."""
    from src.utils.macro_recorder import get_macro_player
    player = get_macro_player()
    progress = player.get_progress(device_id)
    if not progress:
        return {"playing": False}
    return {"playing": True, **progress}


@router.get("/macros/progress")
def all_macro_progress():
    """Get all active macro playback progress."""
    from src.utils.macro_recorder import get_macro_player
    player = get_macro_player()
    return {"progress": player.all_progress()}


# ── Batch Macro Play API ──

@router.post("/macros/batch-play", dependencies=[Depends(verify_api_key)])
def batch_play_macro(body: dict):
    """Play a macro on multiple devices simultaneously."""
    filename = body.get("filename", "")
    device_ids = body.get("device_ids", [])
    speed = float(body.get("speed", 1.0))
    repeat = int(body.get("repeat", 1))
    if not filename:
        raise HTTPException(400, "filename required")
    manager = get_device_manager(_config_path)
    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]
    from src.utils.macro_recorder import get_macro_store, get_macro_player
    store = get_macro_store()
    macro = store.load(filename)
    if not macro:
        raise HTTPException(404, "Macro not found")
    player = get_macro_player()
    results = {}
    def _play(did):
        try:
            player.play(did, macro, manager, speed=speed, repeat=repeat)
            return True
        except Exception as e:
            return str(e)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_play, did): did for did in device_ids}
        for fut in futs:
            did = futs[fut]
            r = fut.result()
            results[did] = {"ok": r is True, "error": "" if r is True else r}
    try:
        from ..api import _audit
        _audit("batch_play_macro", detail=f"filename={filename}, devices={len(device_ids)}")
    except Exception:
        pass
    return {"total": len(results), "results": results}
