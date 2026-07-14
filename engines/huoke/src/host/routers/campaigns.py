# -*- coding: utf-8 -*-
"""Campaign（引流活动）管理路由。"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request, Security
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/campaigns", tags=["campaigns"])


async def _verify_api_key(request: Request,
                          key: Optional[str] = Security(
                              APIKeyHeader(name="X-API-Key", auto_error=False))):
    from ..api import verify_api_key
    await verify_api_key(request, key)

_auth = [Depends(_verify_api_key)]


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("target_accounts", "device_ids", "task_sequence", "params"):
        try:
            d[field] = json.loads(d.get(field) or "[]" if field != "params" else d.get(field) or "{}")
        except Exception:
            d[field] = [] if field != "params" else {}
    return d


@router.get("", dependencies=_auth)
def list_campaigns(status: Optional[str] = None):
    from ..database import get_conn
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM campaigns WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM campaigns ORDER BY created_at DESC"
            ).fetchall()
    campaigns = [_row_to_dict(r) for r in rows]
    # 为每个 campaign 附加实时统计
    for c in campaigns:
        if c.get("batch_id"):
            c["stats"] = _get_batch_stats(c["batch_id"])
        else:
            c["stats"] = {"total": 0, "completed": 0, "failed": 0, "running": 0, "pending": 0, "progress": 0}
    return campaigns


@router.post("", dependencies=_auth)
def create_campaign(body: dict):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 必填")
    campaign_id = str(uuid.uuid4())[:12]
    now = _now()
    from ..database import get_conn
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO campaigns
               (campaign_id, name, description, status, target_accounts, device_ids,
                task_sequence, params, message_template, ai_rewrite, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                campaign_id,
                name,
                body.get("description", ""),
                "draft",
                json.dumps(body.get("target_accounts", [])),
                json.dumps(body.get("device_ids", [])),
                json.dumps(body.get("task_sequence", ["tiktok_follow", "tiktok_check_and_chat_followbacks", "tiktok_check_inbox"])),
                json.dumps(body.get("params", {})),
                body.get("message_template", ""),
                1 if body.get("ai_rewrite", True) else 0,
                now, now,
            )
        )
    return {"campaign_id": campaign_id, "ok": True}


@router.get("/{campaign_id}", dependencies=_auth)
def get_campaign(campaign_id: str):
    from ..database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign 不存在")
    c = _row_to_dict(row)
    if c.get("batch_id"):
        c["stats"] = _get_batch_stats(c["batch_id"])
    else:
        c["stats"] = {"total": 0, "completed": 0, "failed": 0, "running": 0, "pending": 0, "progress": 0}
    return c


@router.put("/{campaign_id}", dependencies=_auth)
def update_campaign(campaign_id: str, body: dict):
    from ..database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Campaign 不存在")
        c = _row_to_dict(row)
        if c["status"] == "active":
            raise HTTPException(status_code=400, detail="活动进行中，请先暂停再修改")
        updates = {}
        for field in ("name", "description", "message_template"):
            if field in body:
                updates[field] = body[field]
        for field in ("target_accounts", "device_ids", "task_sequence", "params"):
            if field in body:
                updates[field] = json.dumps(body[field])
        if "ai_rewrite" in body:
            updates["ai_rewrite"] = 1 if body["ai_rewrite"] else 0
        if not updates:
            return {"ok": True, "message": "无变更"}
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE campaigns SET {set_clause} WHERE campaign_id=?",
            list(updates.values()) + [campaign_id]
        )
    return {"ok": True}


@router.post("/{campaign_id}/start", dependencies=_auth)
def start_campaign(campaign_id: str):
    """启动 Campaign：为每个设备创建第一阶段任务。"""
    from ..database import get_conn
    from ..api import task_store, get_worker_pool, run_task, _config_path
    from src.device_control.device_manager import get_device_manager
    from ..executor import _get_device_id

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign 不存在")
    c = _row_to_dict(row)
    if c["status"] == "active":
        return {"ok": False, "message": "Campaign 已在运行中"}

    device_ids = c.get("device_ids", [])
    if not device_ids:
        raise HTTPException(status_code=400, detail="请先配置设备")
    task_sequence = c.get("task_sequence", [])
    if not task_sequence:
        raise HTTPException(status_code=400, detail="请先配置任务序列")

    # 第一个任务类型
    first_task_type = task_sequence[0]
    from src.host.task_origin import with_origin

    params = dict(c.get("params") or {})
    if c.get("message_template"):
        params["message_template"] = c["message_template"]
    if c.get("ai_rewrite"):
        params["ai_rewrite"] = True
    if c.get("target_accounts"):
        params["target_accounts"] = c["target_accounts"]
    params = with_origin(params, "campaign")

    batch_id = str(uuid.uuid4())[:8]
    task_ids = []
    manager = get_device_manager(_config_path)
    manager.discover_devices()
    pool = get_worker_pool()

    for did in device_ids:
        resolved = _get_device_id(manager, did, _config_path)
        device_for_lock = resolved or did
        tid = task_store.create_task(
            task_type=first_task_type,
            device_id=device_for_lock,
            params=params,
            batch_id=batch_id,
        )
        pool.submit(tid, device_for_lock, run_task, tid, _config_path)
        task_ids.append(tid)

    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET status='active', batch_id=?, started_at=?, updated_at=? WHERE campaign_id=?",
            (batch_id, now, now, campaign_id)
        )

    from ..event_stream import push_event
    push_event("campaign.started", {
        "campaign_id": campaign_id,
        "name": c["name"],
        "batch_id": batch_id,
        "task_count": len(task_ids),
        "first_task": first_task_type,
    })
    logger.info("Campaign %s 启动: batch=%s, tasks=%d, type=%s",
                campaign_id, batch_id, len(task_ids), first_task_type)
    return {"ok": True, "batch_id": batch_id, "task_ids": task_ids, "count": len(task_ids)}


@router.post("/{campaign_id}/pause", dependencies=_auth)
def pause_campaign(campaign_id: str):
    from ..database import get_conn
    from ..api import task_store, get_worker_pool
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign 不存在")
    c = _row_to_dict(row)
    if c["status"] != "active":
        return {"ok": False, "message": f"Campaign 状态为 {c['status']}，无需暂停"}

    # 取消 pending 任务
    pool = get_worker_pool()
    pending = task_store.list_tasks(status="pending")
    batch_id = c.get("batch_id", "")
    cancelled = 0
    for t in pending:
        if batch_id and t.get("batch_id") == batch_id:
            pool.cancel_task(t["task_id"])
            task_store.set_task_cancelled(t["task_id"])
            cancelled += 1

    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET status='paused', updated_at=? WHERE campaign_id=?",
            (_now(), campaign_id)
        )
    return {"ok": True, "cancelled_pending": cancelled}


@router.post("/{campaign_id}/stop", dependencies=_auth)
def stop_campaign(campaign_id: str):
    from ..database import get_conn
    from ..api import task_store, get_worker_pool
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign 不存在")
    c = _row_to_dict(row)

    pool = get_worker_pool()
    batch_id = c.get("batch_id", "")
    cancelled = 0
    if batch_id:
        for t in task_store.list_tasks():
            if t.get("batch_id") == batch_id and t.get("status") in ("pending", "running"):
                pool.cancel_task(t["task_id"])
                task_store.set_task_cancelled(t["task_id"])
                cancelled += 1

    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET status='completed', completed_at=?, updated_at=? WHERE campaign_id=?",
            (now, now, campaign_id)
        )
    return {"ok": True, "cancelled": cancelled}


@router.delete("/{campaign_id}", dependencies=_auth)
def delete_campaign(campaign_id: str):
    from ..database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Campaign 不存在")
        if row["status"] == "active":
            raise HTTPException(status_code=400, detail="请先停止活动再删除")
        conn.execute("DELETE FROM campaigns WHERE campaign_id=?", (campaign_id,))
    return {"ok": True}


@router.get("/{campaign_id}/stats", dependencies=_auth)
def campaign_stats(campaign_id: str):
    from ..database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign 不存在")
    c = _row_to_dict(row)
    stats = _get_batch_stats(c.get("batch_id", "")) if c.get("batch_id") else {}
    # 附加 leads 统计
    try:
        from src.leads.store import get_leads_store
        ls = get_leads_store()
        campaign_leads = ls.list_leads(limit=1000)
        stats["leads_generated"] = len(campaign_leads)
        stats["leads_converted"] = sum(1 for l in campaign_leads if l.get("status") == "converted")
    except Exception:
        pass
    return {"campaign_id": campaign_id, "name": c["name"], "status": c["status"], **stats}


def _get_batch_stats(batch_id: str) -> dict:
    if not batch_id:
        return {"total": 0, "completed": 0, "failed": 0, "running": 0, "pending": 0, "cancelled": 0, "progress": 0}
    try:
        from ..api import task_store
        return task_store.get_batch_progress(batch_id)
    except Exception:
        return {"total": 0, "completed": 0, "failed": 0, "running": 0, "pending": 0, "cancelled": 0, "progress": 0}
