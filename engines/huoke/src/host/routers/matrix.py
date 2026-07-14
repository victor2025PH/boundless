# -*- coding: utf-8 -*-
"""设备矩阵任务队列路由。"""
from fastapi import APIRouter, HTTPException, Depends
from .auth import verify_api_key

router = APIRouter(prefix="/matrix", tags=["matrix"], dependencies=[Depends(verify_api_key)])


@router.get("/status")
def matrix_status():
    """Get device matrix queue and worker status."""
    from src.device_control.device_matrix import get_device_matrix
    return get_device_matrix().queue_stats()


@router.post("/register")
def matrix_register(body: dict):
    """Register a device in the matrix."""
    device_id = body.get("device_id", "")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")
    from src.device_control.device_matrix import get_device_matrix
    profile = get_device_matrix().register_device(
        device_id, display_name=body.get("display_name", ""),
        platforms=body.get("platforms", []),
        max_concurrent=body.get("max_concurrent", 1),
    )
    return {"device_id": device_id, "platforms": profile.platforms}


@router.post("/submit")
def matrix_submit(body: dict):
    """Submit a task to the matrix queue."""
    platform = body.get("platform", "")
    action = body.get("action", "")
    if not platform or not action:
        raise HTTPException(status_code=400, detail="platform and action required")
    from src.device_control.device_matrix import get_device_matrix
    task_id = get_device_matrix().submit(
        platform=platform, action=action,
        params=body.get("params"), priority=body.get("priority", 5),
    )
    return {"task_id": task_id}


@router.post("/submit_batch")
def matrix_submit_batch(body: dict):
    """Submit multiple tasks at once."""
    tasks = body.get("tasks", [])
    if not tasks:
        raise HTTPException(status_code=400, detail="tasks list required")
    from src.device_control.device_matrix import get_device_matrix
    ids = get_device_matrix().submit_batch(tasks)
    return {"task_ids": ids}


@router.get("/tasks")
def matrix_list_tasks(status: str = "", platform: str = "",
                      device_id: str = "", limit: int = 50):
    from src.device_control.device_matrix import get_device_matrix
    tasks = get_device_matrix().list_tasks(status, platform, device_id, limit)
    return {"tasks": [t.to_dict() for t in tasks]}


@router.post("/start")
def matrix_start_workers():
    from src.device_control.device_matrix import get_device_matrix
    get_device_matrix().start_workers()
    return {"ok": True}


@router.post("/stop")
def matrix_stop_workers():
    from src.device_control.device_matrix import get_device_matrix
    get_device_matrix().stop_workers()
    return {"ok": True}


@router.post("/recover_stale")
def matrix_recover_stale(minutes: int = 15):
    from src.device_control.device_matrix import get_device_matrix
    count = get_device_matrix().recover_stale_tasks(minutes)
    return {"recovered": count}
