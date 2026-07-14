# -*- coding: utf-8 -*-
"""系统管理路由 — 路由列表、配置历史/回滚、话术、脚本、定时任务、数据导出、系统配置、模板市场、插件系统。"""

import logging
import subprocess
from fastapi import APIRouter, HTTPException, Depends, Request
from typing import Optional

from src.utils.subprocess_text import run as _sp_run_text
from src.utils.subprocess_text import run_shell
from src.host.device_registry import PROJECT_ROOT, config_file, scripts_dir, templates_dir

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])


@router.get("/system/cluster/probe-contacts-enriched")
def system_probe_contacts_enriched():
    """探测本机与各 Worker 的 OpenAPI 是否声明 ``contacts/enriched``（排障用，需鉴权）。"""
    from ..cluster_probe import run_contacts_enriched_probe

    return run_contacts_enriched_probe()


@router.get("/system/git-branch")
def system_git_branch():
    """当前 service 跑的 git 分支信息 — 用于 dashboard 顶栏 chip 显示.

    返回::

        {"branch": "feat-a-resume-2026-04-27", "ahead_of_main": 4, "is_main": false}

    用途: P3 sibling 协同护栏 L3 — user 任何时候打开 dashboard 都能一眼看到
    service 当前跑哪个分支, 防止"sibling 切了 main 重启 → 我看到没开发好的样子"
    这种事故复发.
    """
    try:
        proc1 = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=2,
            cwd=str(PROJECT_ROOT),
        )
        branch = (proc1.stdout or "").strip()
        if not branch:
            return {"branch": "(detached)", "ahead_of_main": 0, "is_main": False}
        proc2 = subprocess.run(
            ["git", "rev-list", "--count", "main..HEAD"],
            capture_output=True, text=True, timeout=2,
            cwd=str(PROJECT_ROOT),
        )
        ahead_str = (proc2.stdout or "").strip()
        ahead = int(ahead_str) if ahead_str.isdigit() else 0
        return {
            "branch": branch,
            "ahead_of_main": ahead,
            "is_main": branch == "main",
        }
    except Exception as e:
        return {"branch": "(unknown)", "ahead_of_main": 0, "is_main": False, "error": str(e)}


@router.post("/system/force-restart")
def force_restart():
    """强制重启当前 Worker/Coordinator 进程。"""
    import threading, os, sys, signal, logging
    log = logging.getLogger(__name__)

    def _kill():
        import time
        time.sleep(2)
        log.info("[force-restart] 正在终止进程...")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_kill, daemon=True).start()
    return {"ok": True, "message": "进程将在 2 秒后终止，service_wrapper 将自动重启"}


@router.post("/system/exec-command")
def exec_system_command(body: dict):
    """在 Worker 机器上执行系统命令（用于远程管理）。"""
    command = body.get("command", "")
    if not command:
        from fastapi import HTTPException
        raise HTTPException(400, "command required")
    # 安全限制
    blocked = ["format", "del /", "rm -rf /", "shutdown", "reboot"]
    for b in blocked:
        if b in command.lower():
            from fastapi import HTTPException
            raise HTTPException(403, f"Blocked: {b}")
    try:
        r = run_shell(command, capture_output=True, timeout=30)
        return {"ok": r.returncode == 0, "output": r.stdout[:5000], "error": r.stderr[:2000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ══════════════════════════════════════════════
# GET /api/routes — 路由列表
# ══════════════════════════════════════════════

@router.get("/api/routes")
def list_routes():
    """List all API routes grouped by domain."""
    from ..api import app  # lazy import inside function
    groups: dict = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set())
        if not path or path in ("/docs", "/redoc", "/openapi.json"):
            continue
        parts = path.strip("/").split("/")
        domain = parts[0] if parts else "root"
        groups.setdefault(domain, []).append({
            "path": path,
            "methods": sorted(methods) if methods else [],
            "name": getattr(route, "name", ""),
        })
    return {"total": sum(len(v) for v in groups.values()),
            "domains": {k: len(v) for k, v in sorted(groups.items())},
            "routes": groups}


# ══════════════════════════════════════════════
# 配置历史 / 回滚
# ══════════════════════════════════════════════

@router.get("/config/history")
def get_config_history(config_type: str = ""):
    """Get configuration change history for rollback."""
    from ..api import _config_history, _config_history_lock
    with _config_history_lock:
        history = list(_config_history)
    if config_type:
        history = [h for h in history if h["type"] == config_type]
    return {"history": history[-20:]}


@router.post("/config/rollback/{snapshot_id}")
def rollback_config(snapshot_id: int):
    """Rollback to a previous configuration snapshot."""
    from ..api import _config_history, _config_history_lock, _project_root, _audit
    with _config_history_lock:
        snap = next(
            (h for h in _config_history if h["id"] == snapshot_id), None)
    if not snap:
        raise HTTPException(404, "Snapshot not found")

    if snap["type"] == "notifications":
        import yaml
        from ..alert_notifier import AlertNotifier
        AlertNotifier.get().configure(snap["data"])
        notif_path = _project_root / "config" / "notifications.yaml"
        try:
            with open(notif_path, "w", encoding="utf-8") as f:
                yaml.dump({"notifications": snap["data"]}, f,
                          allow_unicode=True, default_flow_style=False)
        except Exception:
            pass
    _audit("config_rollback", str(snapshot_id),
           f"type={snap['type']}")
    return {"ok": True, "restored": snap["type"],
            "timestamp": snap["timestamp"]}


# ══════════════════════════════════════════════
# 话术管理 (Phrases)
# ══════════════════════════════════════════════

def _load_phrases() -> list:
    import json
    phrases_path = config_file("phrases.json")
    if phrases_path.exists():
        with open(phrases_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_phrases(data: list):
    import json
    phrases_path = config_file("phrases.json")
    phrases_path.parent.mkdir(parents=True, exist_ok=True)
    with open(phrases_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@router.get("/phrases")
def get_phrases():
    return _load_phrases()


@router.post("/phrases")
def create_phrase_group(body: dict):
    """Create a phrase group. body: {name, color?, items: [str]}"""
    import uuid
    groups = _load_phrases()
    g = {
        "id": str(uuid.uuid4())[:8],
        "name": body.get("name", "未命名"),
        "color": body.get("color", "#60a5fa"),
        "items": body.get("items", []),
    }
    groups.append(g)
    _save_phrases(groups)
    return g


@router.put("/phrases/{group_id}")
def update_phrase_group(group_id: str, body: dict):
    groups = _load_phrases()
    for g in groups:
        if g["id"] == group_id:
            if "name" in body:
                g["name"] = body["name"]
            if "color" in body:
                g["color"] = body["color"]
            if "items" in body:
                g["items"] = body["items"]
            _save_phrases(groups)
            return g
    raise HTTPException(404, "Group not found")


@router.delete("/phrases/{group_id}")
def delete_phrase_group(group_id: str):
    groups = _load_phrases()
    groups = [g for g in groups if g["id"] != group_id]
    _save_phrases(groups)
    return {"ok": True}


# ══════════════════════════════════════════════
# 脚本管理 (Scripts)
# ══════════════════════════════════════════════

def _get_scripts_dir():
    return scripts_dir()


@router.get("/scripts")
def list_scripts():
    """List uploaded scripts."""
    scripts_dir = _get_scripts_dir()
    scripts_dir.mkdir(exist_ok=True)
    files = sorted(scripts_dir.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
    return {"scripts": [
        {"name": f.name, "size": f.stat().st_size, "ext": f.suffix}
        for f in files if f.is_file()
    ]}


@router.post("/scripts/upload")
async def upload_script(request: Request):
    """Upload a script file. body: {filename, content}"""
    from ..api import _audit
    scripts_dir = _get_scripts_dir()
    body = await request.json()
    filename = body.get("filename", "script.sh")
    content = body.get("content", "")
    if not content:
        raise HTTPException(400, "Empty script")
    scripts_dir.mkdir(exist_ok=True)
    path = scripts_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    _audit("upload_script", detail=str({"filename": filename}))
    return {"ok": True, "filename": filename}


@router.delete("/scripts/{filename}")
def delete_script(filename: str):
    scripts_dir = _get_scripts_dir()
    path = scripts_dir / filename
    if path.exists():
        path.unlink()
    return {"ok": True}


def _expand_template_vars(content: str, device_id: str, device_index: int = 0,
                          group_name: str = "", custom_vars: dict = None) -> str:
    """Replace {{var}} placeholders in script templates."""
    import re
    import time
    alias = ""
    try:
        from ..api import _load_aliases as _api_load_aliases
        aliases = _api_load_aliases()
        alias = aliases.get(device_id, {}).get("alias", "")
    except Exception:
        pass
    mapping = {
        "device_id": device_id,
        "device_serial": device_id,
        "device_alias": alias or device_id[:8],
        "device_index": str(device_index),
        "group_name": group_name,
        "timestamp": time.strftime("%Y%m%d_%H%M%S"),
        "date": time.strftime("%Y-%m-%d"),
    }
    if custom_vars:
        mapping.update(custom_vars)

    def _repl(m):
        key = m.group(1).strip()
        return mapping.get(key, m.group(0))
    return re.sub(r'\{\{(\s*\w+\s*)\}\}', _repl, content)


@router.post("/scripts/execute")
def execute_script(body: dict):
    """Execute a script on devices with template variable expansion.
    body: {filename, device_ids?, group_id?, type, variables?}"""
    from ..api import _config_path, _audit
    from src.device_control.device_manager import get_device_manager

    scripts_dir = _get_scripts_dir()
    filename = body.get("filename", "")
    device_ids = body.get("device_ids", [])
    group_id = body.get("group_id", "")
    script_type = body.get("type", "shell")
    custom_vars = body.get("variables", {})
    path = scripts_dir / filename
    if not path.exists():
        raise HTTPException(404, "Script not found")
    raw_content = path.read_text(encoding="utf-8")
    manager = get_device_manager(_config_path)

    group_name = ""
    if group_id and not device_ids:
        try:
            from ..database import get_conn
            with get_conn() as conn:
                g = conn.execute("SELECT name FROM device_groups WHERE group_id=?", (group_id,)).fetchone()
                if g:
                    group_name = g["name"]
                members = conn.execute(
                    "SELECT device_id FROM device_group_members WHERE group_id=?",
                    (group_id,)).fetchall()
                device_ids = [m["device_id"] for m in members]
        except Exception:
            pass

    if not device_ids:
        device_ids = [d.device_id for d in manager.get_all_devices() if d.is_online]

    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _run(did, idx):
        content = _expand_template_vars(raw_content, did, idx, group_name, custom_vars)
        try:
            if script_type == "adb":
                lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                outputs = []
                for cmd in lines:
                    r = _sp_run_text(
                        ["adb", "-s", did, "shell", cmd],
                        capture_output=True,
                        timeout=30,
                    )
                    outputs.append(f"$ {cmd}\n{r.stdout}{r.stderr}".strip())
                return True, "\n".join(outputs)[:2000]
            else:
                r = _sp_run_text(
                    ["adb", "-s", did, "shell", "sh", "-c", content],
                    capture_output=True,
                    timeout=60,
                )
                return r.returncode == 0, (r.stdout + r.stderr).strip()[:2000]
        except Exception as e:
            return False, str(e)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_run, did, i): did for i, did in enumerate(device_ids)}
        for fut in futs:
            did = futs[fut]
            ok, out = fut.result()
            results[did] = {"success": ok, "output": out}
    _audit("execute_script", detail=str({"filename": filename, "devices": len(device_ids),
                                        "group": group_id or None}))
    return {"total": len(results), "results": results}


@router.get("/scripts/templates")
def list_script_templates():
    """List built-in script templates with variable placeholders."""
    templates = [
        {"name": "获取设备信息", "filename": "tpl_device_info.sh",
         "content": "echo 'Device: {{device_alias}} ({{device_id}})'\ngetprop ro.product.model\ngetprop ro.build.version.release\ncat /proc/meminfo | head -3\ndumpsys battery | grep level",
         "type": "adb", "vars": ["device_id", "device_alias"]},
        {"name": "清理应用缓存", "filename": "tpl_clear_cache.sh",
         "content": "echo 'Clearing cache for {{device_alias}}'\npm list packages -3 | cut -d: -f2 | while read pkg; do pm clear $pkg 2>/dev/null; done\necho 'Done'",
         "type": "adb", "vars": ["device_alias"]},
        {"name": "设备编号壁纸", "filename": "tpl_set_number.sh",
         "content": "echo 'Setting number {{device_index}} for {{device_alias}}'",
         "type": "adb", "vars": ["device_index", "device_alias"]},
        {"name": "批量安装APK", "filename": "tpl_install_apk.sh",
         "content": "pm install -r /sdcard/Download/{{apk_name}}\necho 'Installed {{apk_name}} on {{device_alias}}'",
         "type": "adb", "vars": ["apk_name", "device_alias"]},
        {"name": "网络诊断", "filename": "tpl_network_diag.sh",
         "content": "echo '=== {{device_alias}} 网络诊断 ==='\nping -c 3 8.8.8.8\nip addr show wlan0 | grep inet\nsettings get global http_proxy",
         "type": "adb", "vars": ["device_alias"]},
        {"name": "自定义分组脚本", "filename": "tpl_group_custom.sh",
         "content": "echo 'Group: {{group_name}} | Device #{{device_index}}: {{device_alias}}'\n# 在此添加自定义命令",
         "type": "adb", "vars": ["group_name", "device_index", "device_alias"]},
    ]
    return {"templates": templates}


@router.post("/scripts/from-template")
def create_script_from_template(body: dict):
    """Create a script from a template. body: {template_name, custom_name?}"""
    scripts_dir = _get_scripts_dir()
    template_name = body.get("template_name", "")
    custom_name = body.get("custom_name", "")
    templates_resp = list_script_templates()
    tpl = next((t for t in templates_resp["templates"] if t["name"] == template_name), None)
    if not tpl:
        raise HTTPException(404, "Template not found")
    fname = custom_name or tpl["filename"]
    scripts_dir.mkdir(exist_ok=True)
    with open(scripts_dir / fname, "w", encoding="utf-8") as f:
        f.write(tpl["content"])
    return {"ok": True, "filename": fname, "content": tpl["content"]}


# ══════════════════════════════════════════════
# 定时任务 (Scheduled Jobs)
# ══════════════════════════════════════════════

def _load_scheduled_jobs() -> list:
    from ..api import _load_scheduled_jobs as _impl
    return _impl()


def _save_scheduled_jobs(data: list):
    from ..api import _save_scheduled_jobs as _impl
    _impl(data)


@router.get("/scheduled-jobs")
def get_scheduled_jobs():
    return _load_scheduled_jobs()


@router.post("/scheduled-jobs")
def create_scheduled_job(body: dict):
    """Create a scheduled job. body: {name, cron, action, params}"""
    import uuid
    from ..api import _audit
    jobs = _load_scheduled_jobs()
    job = {
        "id": str(uuid.uuid4())[:8],
        "name": body.get("name", "未命名"),
        "cron": body.get("cron", ""),
        "action": body.get("action", ""),
        "params": body.get("params", {}),
        "enabled": True,
        "last_run": None,
    }
    jobs.append(job)
    _save_scheduled_jobs(jobs)
    _audit("create_scheduled_job", detail=str(job))
    return job


@router.put("/scheduled-jobs/{job_id}")
def update_scheduled_job(job_id: str, body: dict):
    jobs = _load_scheduled_jobs()
    for j in jobs:
        if j["id"] == job_id:
            for k in ("name", "cron", "action", "params", "enabled"):
                if k in body:
                    j[k] = body[k]
            _save_scheduled_jobs(jobs)
            return j
    raise HTTPException(404, "Job not found")


@router.delete("/scheduled-jobs/{job_id}")
def delete_scheduled_job(job_id: str):
    jobs = _load_scheduled_jobs()
    jobs = [j for j in jobs if j["id"] != job_id]
    _save_scheduled_jobs(jobs)
    return {"ok": True}


@router.post("/scheduled-jobs/{job_id}/run-now")
def run_scheduled_job_now(job_id: str):
    """Immediately execute a scheduled job."""
    import time
    from ..api import _execute_scheduled_action
    jobs = _load_scheduled_jobs()
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        raise HTTPException(404, "Job not found")
    result = _execute_scheduled_action(job)
    job["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_scheduled_jobs(jobs)
    return {"ok": True, "result": result}


# ══════════════════════════════════════════════
# 数据导出 (Export)
# ══════════════════════════════════════════════

@router.get("/export/tasks")
def export_tasks(fmt: str = "csv", days: int = 7):
    """Export task data as CSV."""
    import time
    from fastapi.responses import PlainTextResponse
    cutoff = time.time() - days * 86400
    try:
        from src.host.executor import get_task_store
        store = get_task_store()
        tasks = store.list_tasks(limit=5000)
    except Exception:
        tasks = []
    rows = [t for t in tasks if t.get("created_at", 0) > cutoff or not t.get("created_at")]
    if fmt == "csv":
        import io, csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["task_id", "device_id", "task_type", "status", "created_at", "duration"])
        for t in rows:
            w.writerow([t.get("task_id", ""), t.get("device_id", ""),
                        t.get("task_type", ""), t.get("status", ""),
                        t.get("created_at", ""), t.get("duration", "")])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename=tasks_{days}d.csv"})
    return rows


@router.get("/export/devices")
def export_devices(fmt: str = "csv"):
    """Export device info as CSV."""
    import json
    from fastapi.responses import PlainTextResponse
    from ..api import _config_path
    from src.device_control.device_manager import get_device_manager
    manager = get_device_manager(_config_path)
    devices = manager.get_all_devices()
    aliases_path = config_file("device_aliases.json")
    aliases = {}
    if aliases_path.exists():
        with open(aliases_path, "r", encoding="utf-8") as f:
            aliases = json.load(f)
    if fmt == "csv":
        import io, csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["device_id", "alias", "display_name", "status", "model", "android_version"])
        for d in devices:
            a = aliases.get(d.device_id, {})
            w.writerow([d.device_id, a.get("alias", ""), d.display_name,
                        d.status, getattr(d, "model", ""), getattr(d, "android_version", "")])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=devices.csv"})
    return [{"device_id": d.device_id, "status": d.status, "display_name": d.display_name} for d in devices]


@router.get("/export/performance")
def export_performance(fmt: str = "csv"):
    """Export performance data as CSV."""
    from fastapi.responses import PlainTextResponse
    import requests as _req
    try:
        from src.openclaw_env import local_api_base

        r = _req.get(f"{local_api_base()}/devices/performance/all", timeout=30)
        data = r.json().get("devices", {})
    except Exception:
        data = {}
    if fmt == "csv":
        import io, csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["device_id", "mem_usage%", "battery%", "battery_temp", "storage_usage%", "charging"])
        for did, d in data.items():
            w.writerow([did, d.get("mem_usage", ""), d.get("battery_level", ""),
                        d.get("battery_temp", ""), d.get("storage_usage", ""),
                        d.get("charging", "")])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=performance.csv"})
    return data


# ══════════════════════════════════════════════
# 系统配置导出/导入
# ══════════════════════════════════════════════

@router.get("/system/export-config")
def export_config():
    """Export all config files as a ZIP archive."""
    import time
    import zipfile
    from io import BytesIO
    from fastapi.responses import Response
    from ..api import _project_root, _CONFIG_FILES

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in _CONFIG_FILES:
            fp = _project_root / rel
            if fp.exists():
                zf.write(str(fp), rel)
        scripts_dir = _project_root / "scripts"
        if scripts_dir.exists():
            for f in scripts_dir.iterdir():
                if f.is_file():
                    zf.write(str(f), f"scripts/{f.name}")
        templates_dir = _project_root / "templates"
        if templates_dir.exists():
            for f in templates_dir.iterdir():
                if f.is_file():
                    zf.write(str(f), f"templates/{f.name}")
    buf.seek(0)
    filename = f"openclaw_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/system/import-config")
async def import_config(request: Request):
    """Import config from uploaded ZIP. Expects base64 encoded ZIP in body."""
    import zipfile, base64
    from io import BytesIO
    from ..api import _project_root

    body = await request.json()
    zip_b64 = body.get("data", "")
    if not zip_b64:
        raise HTTPException(400, "No data provided")

    try:
        zip_data = base64.b64decode(zip_b64)
    except Exception:
        raise HTTPException(400, "Invalid base64 data")

    restored = []
    skipped = []
    try:
        with zipfile.ZipFile(BytesIO(zip_data), "r") as zf:
            for name in zf.namelist():
                if name.startswith("config/") or name.startswith("scripts/") or name.startswith("templates/"):
                    target = _project_root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(str(target), "wb") as f:
                        f.write(zf.read(name))
                    restored.append(name)
                else:
                    skipped.append(name)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid ZIP file")

    return {"ok": True, "restored": restored, "skipped": skipped}


@router.get("/system/config-list")
def config_list():
    """List all config files with sizes."""
    import time
    from ..api import _project_root, _CONFIG_FILES
    files = []
    for rel in _CONFIG_FILES:
        fp = _project_root / rel
        if fp.exists():
            files.append({"path": rel, "size": fp.stat().st_size,
                          "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(fp.stat().st_mtime))})
    return files


# ══════════════════════════════════════════════
# 模板市场 (Templates)
# ══════════════════════════════════════════════

def _get_templates_dir():
    return templates_dir()


@router.get("/templates")
def list_templates():
    """List available templates (scripts + workflows)."""
    import json
    templates_dir = _get_templates_dir()
    templates_dir.mkdir(exist_ok=True)
    templates = []
    for f in sorted(templates_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            templates.append({
                "filename": f.name,
                "name": data.get("name", f.stem),
                "type": data.get("type", "script"),
                "description": data.get("description", ""),
                "author": data.get("author", ""),
                "tags": data.get("tags", []),
            })
        except Exception:
            continue
    return {"templates": templates}


@router.post("/templates")
def create_template(body: dict):
    """Create/share a template. body: {name, type, description, content, tags?, author?}"""
    import json, uuid
    from ..api import _audit
    templates_dir = _get_templates_dir()
    templates_dir.mkdir(exist_ok=True)
    tpl = {
        "id": str(uuid.uuid4())[:8],
        "name": body.get("name", "未命名"),
        "type": body.get("type", "script"),
        "description": body.get("description", ""),
        "content": body.get("content", ""),
        "author": body.get("author", ""),
        "tags": body.get("tags", []),
    }
    fname = f"{tpl['id']}_{tpl['name']}.json"
    with open(templates_dir / fname, "w", encoding="utf-8") as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)
    _audit("create_template", detail=str({"name": tpl["name"]}))
    return tpl


@router.get("/templates/{filename}")
def get_template(filename: str):
    """Get template content."""
    import json
    templates_dir = _get_templates_dir()
    path = templates_dir / filename
    if not path.exists():
        raise HTTPException(404, "Template not found")
    return json.loads(path.read_text(encoding="utf-8"))


@router.delete("/templates/{filename}")
def delete_template(filename: str):
    templates_dir = _get_templates_dir()
    path = templates_dir / filename
    if path.exists():
        path.unlink()
    return {"ok": True}


@router.post("/templates/{filename}/import")
def import_template(filename: str):
    """Import template into scripts or workflows."""
    import json
    templates_dir = _get_templates_dir()
    scripts_dir = _get_scripts_dir()
    path = templates_dir / filename
    if not path.exists():
        raise HTTPException(404, "Template not found")
    tpl = json.loads(path.read_text(encoding="utf-8"))
    if tpl.get("type") == "script":
        script_name = tpl["name"] + ".sh"
        script_path = scripts_dir / script_name
        scripts_dir.mkdir(exist_ok=True)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(tpl.get("content", ""))
        return {"ok": True, "imported_as": script_name, "type": "script"}
    return {"ok": True, "type": tpl.get("type", "unknown")}


# ══════════════════════════════════════════════
# 插件系统 (Plugins)
# ══════════════════════════════════════════════

@router.get("/plugins")
def list_plugins():
    from ..plugin_manager import get_plugin_manager
    pm = get_plugin_manager()
    discovered = pm.discover()
    loaded = pm.list_all()
    loaded_names = {p["name"] for p in loaded}
    for name in discovered:
        if name not in loaded_names:
            loaded.append({"name": name, "version": "?", "author": "", "description": "",
                           "enabled": False, "loaded_at": 0, "hooks": [], "error": "未加载"})
    return loaded


@router.post("/plugins/{name}/load")
def load_plugin(name: str):
    from ..plugin_manager import get_plugin_manager
    pm = get_plugin_manager()
    meta = pm.load(name)
    return meta.to_dict()


@router.post("/plugins/{name}/enable")
def enable_plugin(name: str):
    from ..plugin_manager import get_plugin_manager
    pm = get_plugin_manager()
    if name not in pm.plugins:
        pm.load(name)
    ok = pm.enable(name)
    return {"ok": ok, "plugin": pm.plugins[name].to_dict() if name in pm.plugins else {}}


@router.post("/plugins/{name}/disable")
def disable_plugin(name: str):
    from ..plugin_manager import get_plugin_manager
    pm = get_plugin_manager()
    ok = pm.disable(name)
    return {"ok": ok}


@router.delete("/plugins/{name}")
def unload_plugin(name: str):
    from ..plugin_manager import get_plugin_manager
    pm = get_plugin_manager()
    pm.unload(name)
    return {"ok": True}


@router.post("/plugins/{name}/reload")
def reload_plugin(name: str):
    from ..plugin_manager import get_plugin_manager
    pm = get_plugin_manager()
    meta = pm.reload(name)
    return meta.to_dict()


@router.post("/plugins/reload-all")
def reload_all_plugins():
    from ..plugin_manager import get_plugin_manager
    pm = get_plugin_manager()
    pm.reload_all()
    return {"ok": True, "plugins": pm.list_all()}


@router.get("/plugins/events")
def plugin_events():
    from ..plugin_manager import get_plugin_manager
    pm = get_plugin_manager()
    return pm.get_event_log()


# ══════════════════════════════════════════════
# 定时任务管理 (System Jobs API)
# ══════════════════════════════════════════════

@router.post("/system/jobs")
def upsert_scheduled_job(body: dict):
    """添加或更新一个定时任务。

    Body: {
        "id": "my_job_id",  # 任务唯一ID，相同ID会覆盖
        "name": "任务名称",
        "cron": "0 7 * * *",  # cron表达式
        "action": "tiktok_daily_campaign",
        "params": {"country": "italy"},
        "enabled": true
    }
    """
    from src.host.job_scheduler import load_scheduled_jobs, save_scheduled_jobs

    action = body.get("action")
    if not action:
        raise HTTPException(400, "action required")
    cron = body.get("cron")
    if not cron:
        raise HTTPException(400, "cron required")

    jobs = load_scheduled_jobs()
    job_id = body.get("id") or action

    # Remove existing job with same id
    jobs = [j for j in jobs if j.get("id") != job_id]

    new_job = {
        "id": job_id,
        "name": body.get("name", action),
        "cron": cron,
        "action": action,
        "params": body.get("params", {}),
        "enabled": bool(body.get("enabled", True)),
    }
    jobs.append(new_job)
    save_scheduled_jobs(jobs)
    return {"ok": True, "job": new_job}


@router.delete("/system/jobs/{job_id}")
def delete_scheduled_job(job_id: str):
    """删除一个定时任务。"""
    from src.host.job_scheduler import load_scheduled_jobs, save_scheduled_jobs
    jobs = load_scheduled_jobs()
    before = len(jobs)
    jobs = [j for j in jobs if j.get("id") != job_id]
    save_scheduled_jobs(jobs)
    return {"ok": True, "deleted": before - len(jobs)}


@router.get("/system/jobs")
def list_scheduled_jobs():
    """列出所有定时任务。"""
    from src.host.job_scheduler import load_scheduled_jobs
    return {"jobs": load_scheduled_jobs()}


# ── 日报推送（配置占位 + 手动触发钩子）────────────────────────────

@router.get("/system/daily-report-config")
def daily_report_config():
    """读取 config/daily_report.yaml（若存在）。用于 Telegram 等渠道定时推送。"""
    import yaml

    p = config_file("daily_report.yaml")
    if not p.exists():
        return {"configured": False, "path": str(p), "hint": "复制 config/daily_report.example.yaml 为 daily_report.yaml"}
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return {"configured": True, "config": cfg}
    except Exception as e:
        return {"configured": False, "error": str(e)[:200]}


@router.post("/system/daily-report/trigger")
def daily_report_trigger(body: dict = None):
    """
    手动触发「综合日报」生成（不写真实 Telegram，仅返回可投递的摘要文本）。
    与前端 _ttExportDailyCSV 数据源对齐，便于后续接入 Bot。
    """
    body = body or {}
    lines = []
    try:
        import urllib.request as _ur
        from src.openclaw_env import local_api_base

        base = body.get("base_url", local_api_base())
        for path, title in (
            ("/tiktok/funnel", "Funnel"),
            ("/tiktok/chat/active", "Chat KPI"),
            ("/devices/health-scores", "Health"),
        ):
            try:
                req = _ur.Request(f"{base}{path}", method="GET")
                req.add_header("X-API-Key", body.get("api_key", ""))
                r = _ur.urlopen(req, timeout=8)
                raw = r.read().decode()[:4000]
                lines.append(f"=== {title} ===\n{raw}\n")
            except Exception as ex:
                lines.append(f"=== {title} === (skip: {ex})\n")
    except Exception as e:
        lines.append(str(e))
    text = "\n".join(lines)
    return {"ok": True, "preview_length": len(text), "preview": text[:8000]}
