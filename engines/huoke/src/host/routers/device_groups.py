# -*- coding: utf-8 -*-
"""设备分组管理路由。"""

import uuid
from datetime import datetime
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/device-groups", tags=["device-groups"])


@router.get("")
def list_groups():
    """List all device groups with their members."""
    from ..database import get_conn
    with get_conn() as conn:
        groups = conn.execute(
            "SELECT group_id, name, color, created_at "
            "FROM device_groups ORDER BY created_at").fetchall()
        result = []
        for g in groups:
            members = conn.execute(
                "SELECT device_id FROM device_group_members "
                "WHERE group_id = ?", (g["group_id"],)).fetchall()
            result.append({
                "id": g["group_id"],
                "name": g["name"],
                "color": g["color"],
                "created_at": g["created_at"],
                "devices": [m["device_id"] for m in members],
            })
    return {"groups": result}


@router.post("")
def create_group(body: dict):
    """Create a new device group."""
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name required")
    color = body.get("color", "#60a5fa")
    gid = uuid.uuid4().hex[:12]
    from ..database import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO device_groups (group_id, name, color, created_at) "
            "VALUES (?, ?, ?, ?)",
            (gid, name, color, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    try:
        from ..api import _audit
        _audit("create_group", gid, f"name={name}")
    except Exception:
        pass
    return {"ok": True, "id": gid, "name": name}


@router.delete("/{group_id}")
def delete_group(group_id: str):
    """Delete a device group and its memberships."""
    from ..database import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM device_group_members WHERE group_id = ?",
                     (group_id,))
        conn.execute("DELETE FROM device_groups WHERE group_id = ?",
                     (group_id,))
    try:
        from ..api import _audit
        _audit("delete_group", group_id)
    except Exception:
        pass
    return {"ok": True}


@router.post("/{group_id}/devices")
def add_device_to_group(group_id: str, body: dict):
    """Add a device to a group."""
    device_id = body.get("device_id", "").strip()
    if not device_id:
        raise HTTPException(400, "device_id required")
    from ..database import get_conn
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO device_group_members (group_id, device_id) "
                "VALUES (?, ?)", (group_id, device_id))
        except Exception:
            raise HTTPException(400, "Device already in group")
    return {"ok": True}


@router.delete("/{group_id}/devices/{device_id}")
def remove_device_from_group(group_id: str, device_id: str):
    """Remove a device from a group."""
    from ..database import get_conn
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM device_group_members "
            "WHERE group_id = ? AND device_id = ?",
            (group_id, device_id))
    return {"ok": True}


@router.put("/{group_id}")
def update_group(group_id: str, body: dict):
    """Update group name or color."""
    from ..database import get_conn
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT group_id FROM device_groups WHERE group_id = ?",
            (group_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Group not found")
        updates = []
        params = []
        if "name" in body:
            updates.append("name = ?")
            params.append(body["name"])
        if "color" in body:
            updates.append("color = ?")
            params.append(body["color"])
        if updates:
            params.append(group_id)
            conn.execute(
                f"UPDATE device_groups SET {', '.join(updates)} WHERE group_id = ?",
                params)
    return {"ok": True}


@router.post("/{group_id}/batch-add")
def batch_add_devices(group_id: str, body: dict):
    """Add multiple devices to a group at once."""
    device_ids = body.get("device_ids", [])
    if not device_ids:
        raise HTTPException(400, "device_ids required")
    from ..database import get_conn
    added = 0
    with get_conn() as conn:
        for did in device_ids:
            try:
                conn.execute(
                    "INSERT INTO device_group_members (group_id, device_id) VALUES (?, ?)",
                    (group_id, did))
                added += 1
            except Exception:
                pass
    return {"ok": True, "added": added}


@router.post("/{group_id}/batch-task")
def batch_task_for_group(group_id: str, body: dict):
    """Create a task for every device in this group."""
    from src.host.task_origin import with_origin

    task_type = body.get("task_type", "warmup")
    params = with_origin(body.get("params", {}), "device_group")
    from ..database import get_conn
    with get_conn() as conn:
        members = conn.execute(
            "SELECT device_id FROM device_group_members "
            "WHERE group_id = ?", (group_id,)).fetchall()
    if not members:
        raise HTTPException(400, "Group has no devices")

    from ..api import task_store, get_worker_pool, run_task, _config_path
    created = 0
    batch_id = uuid.uuid4().hex[:10]
    for m in members:
        did = m["device_id"]
        task_id = task_store.create_task(
            task_type=task_type, device_id=did,
            params=params)
        pool = get_worker_pool()
        pool.submit(task_id, did, run_task, task_id, _config_path)
        created += 1
    try:
        from ..api import _audit
        _audit("batch_group_task", group_id,
               f"type={task_type} count={created} batch={batch_id}")
    except Exception:
        pass
    return {"ok": True, "created": created, "batch_id": batch_id}
