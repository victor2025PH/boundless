# -*- coding: utf-8 -*-
"""工作流 & 调度路由 — /workflows, /schedules, /visual-workflows 端点。"""

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request, Security
from fastapi.security import APIKeyHeader

from src.host.device_registry import config_dir, config_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["workflows"])


# ---------------------------------------------------------------------------
# 延迟鉴权依赖（避免循环导入 api.py）
# ---------------------------------------------------------------------------

async def _verify_api_key(request: Request,
                          key: Optional[str] = Security(
                              APIKeyHeader(name="X-API-Key", auto_error=False))):
    from ..api import verify_api_key
    await verify_api_key(request, key)


_auth = [Depends(_verify_api_key)]


# ---------------------------------------------------------------------------
# visual-workflows 辅助
# ---------------------------------------------------------------------------

_visual_workflows_path = config_file("visual_workflows.json")


def _load_visual_workflows() -> list:
    if _visual_workflows_path.exists():
        with open(_visual_workflows_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_visual_workflows(data: list):
    _visual_workflows_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_visual_workflows_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ===========================================================================
# 调度 (Schedules)
# ===========================================================================

@router.get("/schedules", dependencies=_auth)
def list_schedules():
    from .. import scheduler
    return scheduler.list_schedules()


@router.post("/schedules", dependencies=_auth)
def create_schedule(body: dict):
    from .. import scheduler
    try:
        sid = scheduler.create_schedule(
            name=body["name"],
            cron_expr=body["cron_expr"],
            task_type=body["task_type"],
            device_id=body.get("device_id"),
            params=body.get("params", {}),
        )
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return scheduler.get_schedule(sid)


@router.post("/schedules/workflow", dependencies=_auth)
def create_workflow_schedule(body: dict):
    """Create a schedule that triggers a workflow."""
    from .. import scheduler
    workflow_name = body.get("workflow", "")
    cron_expr = body.get("cron_expr", "")
    name = body.get("name", f"定时-{workflow_name}")
    variables = body.get("variables", {})
    if not workflow_name or not cron_expr:
        raise HTTPException(status_code=400,
                            detail="workflow and cron_expr required")
    try:
        sid = scheduler.create_schedule(
            name=name,
            cron_expr=cron_expr,
            task_type="workflow",
            params={"workflow": workflow_name, "variables": variables},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return scheduler.get_schedule(sid)


@router.delete("/schedules/{schedule_id}", dependencies=_auth)
def delete_schedule(schedule_id: str):
    from .. import scheduler
    if not scheduler.delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="调度不存在")
    return {"ok": True}


@router.post("/schedules/{schedule_id}/toggle", dependencies=_auth)
def toggle_schedule(schedule_id: str, body: dict):
    from .. import scheduler
    enabled = body.get("enabled", True)
    if not scheduler.toggle_schedule(schedule_id, enabled):
        raise HTTPException(status_code=404, detail="调度不存在")
    return scheduler.get_schedule(schedule_id)


# ===========================================================================
# Workflow YAML API
# ===========================================================================

@router.get("/workflows", dependencies=_auth)
def list_workflows():
    """List available workflow YAML files."""
    workflow_dir = config_dir() / "workflows"
    if not workflow_dir.exists():
        return []
    files = sorted(workflow_dir.glob("*.yaml")) + sorted(workflow_dir.glob("*.yml"))
    results = []
    for f in files:
        try:
            import yaml
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            results.append({
                "file": f.name,
                "name": data.get("name", f.stem),
                "description": data.get("description", ""),
                "steps": len(data.get("steps", [])),
                "variables": list(data.get("variables", {}).keys()),
            })
        except Exception as e:
            results.append({"file": f.name, "error": str(e)})
    return results


@router.post("/workflows/run", dependencies=_auth)
def run_workflow(body: dict):
    """Run a workflow by name or file path."""
    workflow_name = body.get("workflow", "")
    variables = body.get("variables", {})
    if not workflow_name:
        raise HTTPException(status_code=400, detail="workflow name required")

    workflow_dir = config_dir() / "workflows"
    candidates = [
        workflow_dir / f"{workflow_name}.yaml",
        workflow_dir / f"{workflow_name}.yml",
        workflow_dir / workflow_name,
    ]
    workflow_path = None
    for c in candidates:
        if c.exists():
            workflow_path = c
            break
    if not workflow_path:
        raise HTTPException(status_code=404, detail=f"Workflow not found: {workflow_name}")

    try:
        from src.workflow.engine import WorkflowDef, WorkflowExecutor
        wf = WorkflowDef.from_yaml(str(workflow_path))
        executor = WorkflowExecutor()
        result = executor.run(wf, initial_vars=variables)
        return result.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflows/{workflow_name}", dependencies=_auth)
def get_workflow_yaml(workflow_name: str):
    """Get workflow YAML content for editing."""
    workflow_dir = config_dir() / "workflows"
    for ext in (".yaml", ".yml"):
        p = workflow_dir / f"{workflow_name}{ext}"
        if p.exists():
            return {"name": workflow_name, "content": p.read_text(encoding="utf-8")}
    raise HTTPException(status_code=404, detail=f"Workflow not found: {workflow_name}")


@router.put("/workflows/{workflow_name}", dependencies=_auth)
def save_workflow_yaml(workflow_name: str, body: dict):
    """Create or update a workflow YAML file."""
    content = body.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="content required")

    import yaml
    try:
        yaml.safe_load(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    workflow_dir = config_dir() / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    path = workflow_dir / f"{workflow_name}.yaml"
    path.write_text(content, encoding="utf-8")
    return {"status": "saved", "file": path.name}


@router.delete("/workflows/{workflow_name}", dependencies=_auth)
def delete_workflow(workflow_name: str):
    """Delete a workflow YAML file."""
    workflow_dir = config_dir() / "workflows"
    for ext in (".yaml", ".yml"):
        p = workflow_dir / f"{workflow_name}{ext}"
        if p.exists():
            p.unlink()
            return {"status": "deleted", "file": p.name}
    raise HTTPException(status_code=404, detail="Workflow not found")


@router.get("/workflows/runs/active", dependencies=_auth)
def workflow_active_runs():
    """List currently running workflows."""
    from src.workflow.engine import get_workflow_tracker
    return {"runs": get_workflow_tracker().get_active_runs()}


@router.get("/workflows/runs/history", dependencies=_auth)
def workflow_run_history(limit: int = 20):
    """Get recent workflow execution history."""
    from src.workflow.engine import get_workflow_tracker
    return {"runs": get_workflow_tracker().get_history(limit)}


@router.get("/workflows/runs/{run_id}", dependencies=_auth)
def workflow_run_detail(run_id: str):
    """Get details of a specific workflow run."""
    from src.workflow.engine import get_workflow_tracker
    run = get_workflow_tracker().get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/workflows/actions", dependencies=_auth)
def list_workflow_actions():
    """List all registered workflow actions."""
    from src.workflow.actions import get_action_registry
    registry = get_action_registry()
    return registry.list_by_platform()


# ===========================================================================
# Visual Workflows
# ===========================================================================

@router.get("/visual-workflows", dependencies=_auth)
def list_visual_workflows():
    return _load_visual_workflows()


@router.post("/visual-workflows", dependencies=_auth)
def save_visual_workflow(body: dict):
    import uuid
    workflows = _load_visual_workflows()
    wf = {
        "id": str(uuid.uuid4())[:8],
        "name": body.get("name", "Untitled"),
        "nodes": body.get("nodes", []),
        "edges": body.get("edges", []),
        "steps": body.get("steps", []),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    workflows.append(wf)
    _save_visual_workflows(workflows)
    return wf


@router.get("/visual-workflows/{wf_id}", dependencies=_auth)
def get_visual_workflow(wf_id: str):
    workflows = _load_visual_workflows()
    wf = next((w for w in workflows if w["id"] == wf_id), None)
    if not wf:
        raise HTTPException(404, "工作流不存在")
    return wf


@router.delete("/visual-workflows/{wf_id}", dependencies=_auth)
def delete_visual_workflow(wf_id: str):
    workflows = _load_visual_workflows()
    workflows = [w for w in workflows if w["id"] != wf_id]
    _save_visual_workflows(workflows)
    return {"ok": True}


@router.post("/visual-workflows/execute", dependencies=_auth)
def execute_visual_workflow(body: dict):
    from ..api import _config_path
    from src.device_control.device_manager import get_device_manager

    steps = body.get("steps", [])
    device_ids = body.get("device_ids", [])
    if not steps:
        raise HTTPException(400, "空工作流")
    manager = get_device_manager(_config_path)
    results = []

    def _run_on_device(did: str):
        device = manager.get_device(did)
        if not device:
            return {"device_id": did, "status": "not_found"}
        step_results = []
        for step in steps:
            stype = step.get("type", "")
            try:
                if stype == "adb_cmd":
                    cmd = step.get("command", "")
                    out = device.shell(cmd)
                    step_results.append({"step": stype, "command": cmd, "output": str(out)[:200], "ok": True})
                elif stype == "delay":
                    import time as t
                    t.sleep(float(step.get("seconds", 1)))
                    step_results.append({"step": "delay", "seconds": step.get("seconds"), "ok": True})
                elif stype == "loop":
                    count = int(step.get("count", 1))
                    sub_steps = step.get("steps", [])
                    for i in range(count):
                        for sub in sub_steps:
                            if sub.get("type") == "adb_cmd":
                                device.shell(sub.get("command", ""))
                            elif sub.get("type") == "delay":
                                import time as t
                                t.sleep(float(sub.get("seconds", 1)))
                    step_results.append({"step": "loop", "count": count, "ok": True})
                else:
                    step_results.append({"step": stype, "ok": False, "error": "未知步骤类型"})
            except Exception as ex:
                step_results.append({"step": stype, "ok": False, "error": str(ex)[:200]})
        return {"device_id": did, "status": "done", "steps": step_results}

    from concurrent.futures import ThreadPoolExecutor as _TPE2
    with _TPE2(max_workers=min(len(device_ids), 8)) as pool:
        results = list(pool.map(_run_on_device, device_ids))
    return {"task_count": len(results), "results": results}
