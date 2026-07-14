# -*- coding: utf-8 -*-
"""任务管理路由 — /tasks, /stats, /pool 端点。"""

import logging
import os
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Request, Security
from fastapi.security import APIKeyHeader

from ..schemas import TaskBatchDelete, TaskCreate, TaskResponse, TaskResultReport

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["tasks"])

# P2-④ smart_retry 风暴防护:
# - 同 device_id 5 分钟内最多 3 次手动 smart_retry（防运营连点）
# - retry 链路总深度（系统 retry_count + 手动 _retry_of 链）≥ 5 时拒绝
_RETRY_THROTTLE_LOCK = threading.Lock()
_RETRY_THROTTLE: dict = {}  # device_id → list[timestamp]
_RETRY_THROTTLE_WINDOW_S = 300
_RETRY_THROTTLE_MAX = 3
_RETRY_DEPTH_MAX = 5


def _retry_throttle_check(device_id: str) -> tuple:
    """返回 (allowed, recent_count). 不允许时 recent_count 用于 UI 提示."""
    if not device_id:
        return True, 0
    now = time.time()
    cutoff = now - _RETRY_THROTTLE_WINDOW_S
    with _RETRY_THROTTLE_LOCK:
        bucket = _RETRY_THROTTLE.setdefault(device_id, [])
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= _RETRY_THROTTLE_MAX:
            return False, len(bucket)
        bucket.append(now)
        return True, len(bucket)


def _compute_retry_depth(task: dict) -> int:
    """估算 retry 链路深度: 系统 retry_count + 手动 _retry_of 链长度."""
    depth = int(task.get("retry_count") or 0)
    params = task.get("params") or {}
    if isinstance(params, dict) and params.get("_retry_of"):
        depth += 1
    return depth


# ---------------------------------------------------------------------------
# 延迟鉴权依赖（避免循环导入 api.py）
# ---------------------------------------------------------------------------

async def _verify_api_key(request: Request,
                          key: Optional[str] = Security(
                              APIKeyHeader(name="X-API-Key", auto_error=False))):
    from ..api import verify_api_key
    await verify_api_key(request, key)


_auth = [Depends(_verify_api_key)]


def _proxy_delete_task_to_worker(task_id: str) -> Optional[str]:
    """在集群 Worker 上执行软删（DELETE /tasks/{id}）。成功返回节点 IP，失败返回 None。"""
    import urllib.error as _ue
    import urllib.request as _ur

    _key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
    try:
        from ..multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        for h in coord._hosts.values():
            if not getattr(h, "online", False) or not getattr(h, "host_ip", ""):
                continue
            url = f"http://{h.host_ip}:{h.port}/tasks/{task_id}"
            hdrs = {}
            if _key:
                hdrs["X-API-Key"] = _key
            try:
                _req = _ur.Request(url, method="DELETE", headers=hdrs)
                _resp = _ur.urlopen(_req, timeout=10)
                _resp.read()
                return h.host_ip
            except _ue.HTTPError as _he:
                if _he.code == 404:
                    continue
            except Exception:
                continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.post("/tasks", response_model=TaskResponse, dependencies=_auth)
def create_task_endpoint(body: TaskCreate):
    from ..api import task_store, _audit, _to_response
    from ..task_dispatcher import dispatch_after_create

    task_type = body.type.value if hasattr(body.type, "value") else body.type
    task_priority = getattr(body, "priority", 50)
    from src.host.task_param_rules import maybe_normalize_for_task

    params = maybe_normalize_for_task(task_type, body.params or {})
    cv = getattr(body, "created_via", None)
    if cv:
        params = {**params, "_created_via": str(cv).strip()}
    elif isinstance(params, dict) and "_created_via" not in params:
        params = {**params, "_created_via": "api"}
    # P3-2 2026-04-23: 统一用 gate_registry 查 gate,避免 if-elif 膨胀。
    # A 已注册 facebook_add_friend / facebook_add_friend_and_greet /
    # facebook_send_greeting 三个 task_type 的 gate;
    # B 只需在自己的 gate 模块末尾调 register_task_gate(...) 即可加入, 无需改本文件。
    # 2026-05-07 fix: 主控 fb_device_launch 转发到 Worker 时已做过 gate 校验，
    # Worker 收到 _coordinator_forwarded=True 的任务跳过重复的 gate（含 GEO/preflight），
    # 避免 Worker 因本地无法 curl 公网 IP 导致 GEO 检查失败。
    _skip_gate = bool(params.get("_coordinator_forwarded"))
    if body.device_id and not _skip_gate:
        from src.host.gate_registry import check_gate_for_task
        _gerr, _gmeta = check_gate_for_task(task_type, str(body.device_id), params)
        if _gerr:
            raise HTTPException(
                status_code=400,
                detail={"error": _gerr, "meta": _gmeta, "task_type": task_type},
            )
    # campaign 含 gated step(add_friends / send_greeting / 未来 B 加的 reply_flood 等)时,
    # 前置闸统一走 check_gates_for_campaign_steps, 任何 step gate 失败整单拒绝入队。
    if task_type in ("facebook_campaign_run", "facebook_group_member_greet") \
            and body.device_id and not _skip_gate:
        from src.host.fb_add_friend_gate import campaign_step_names
        from src.host.gate_registry import check_gates_for_campaign_steps

        _step_names = campaign_step_names(params)
        _gerr_c, _gmeta_c = check_gates_for_campaign_steps(
            _step_names, str(body.device_id), params)
        if _gerr_c:
            raise HTTPException(
                status_code=400,
                detail={"error": _gerr_c, "meta": _gmeta_c, "task_type": task_type},
            )
    task_id = task_store.create_task(
        task_type=task_type,
        device_id=body.device_id,
        params=params,
        policy_id=body.policy_id,
        batch_id=getattr(body, "batch_id", "") or "",
        priority=task_priority,
    )

    if getattr(body, "run_on_host", True):
        dispatch_after_create(
            task_id=task_id,
            device_id=body.device_id,
            task_type=task_type,
            params=params,
            priority=task_priority,
        )

    t = task_store.get_task(task_id)
    _audit("create_task", body.device_id or "",
           f"type={task_type} id={task_id[:8]}")
    return _to_response(t)


@router.get(
    "/tasks/today-funnel",
    dependencies=_auth,
    summary="今日任务漏斗",
    description=(
        "返回当天 UTC 0 点至今的任务状态漏斗：created / pending / running / "
        "completed / failed / cancelled，以及三个健康度派生指标："
        "success_rate（完成/终态）、in_flight（进行中）、stuck_pending_over_5min"
        "（pending 超过 5 分钟未动的数量，一般就是需要救援的任务）。"
    ),
)
def today_funnel():
    from datetime import datetime, timezone
    from ..database import get_conn
    from ..task_store import _alive_sql

    today_utc_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    stuck_cutoff = datetime.now(timezone.utc).timestamp() - 300
    alive = _alive_sql()
    out = {
        "created": 0, "pending": 0, "running": 0,
        "completed": 0, "failed": 0, "cancelled": 0,
        "stuck_pending_over_5min": 0,
    }
    with get_conn() as conn:
        row_total = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE created_at >= ? AND {alive}",
            (today_utc_start,),
        ).fetchone()
        out["created"] = (row_total[0] if row_total else 0) or 0

        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM tasks "
            f"WHERE created_at >= ? AND {alive} GROUP BY status",
            (today_utc_start,),
        ).fetchall()
        for r in status_rows:
            st = r["status"]
            if st in out:
                out[st] = r["cnt"]

        stuck_rows = conn.execute(
            "SELECT updated_at FROM tasks "
            f"WHERE status='pending' AND {alive}",
        ).fetchall()
        for r in stuck_rows:
            ua = (r["updated_at"] or "").strip()
            if not ua:
                continue
            try:
                from datetime import datetime as _dt
                dt = _dt.fromisoformat(ua.replace("Z", "+00:00"))
                if dt.timestamp() <= stuck_cutoff:
                    out["stuck_pending_over_5min"] += 1
            except Exception:
                pass

    final = out["completed"] + out["failed"] + out["cancelled"]
    out["success_rate"] = (
        round(out["completed"] / final * 100, 1) if final else 0.0
    )
    out["in_flight"] = out["pending"] + out["running"]
    out["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


@router.get(
    "/tasks/meta/labels",
    summary="任务类型中文标签字典（单一真源）",
    description=(
        "返回 task_type → 中文展示名的全量映射，前端可一次性拉取并覆盖本地字典。"
        "优先级：本接口 > platforms.PLATFORMS[*].label > 硬编码 TASK_NAMES。"
        "不走鉴权，便于未登录页面（如登录页、PWA）直接消费。"
    ),
)
def task_labels_dict():
    from src.host.task_labels_zh import export_frontend_dict
    return {"labels": export_frontend_dict()}


@router.get("/tasks/meta/execution-policy", dependencies=_auth)
def task_execution_policy_summary():
    """只读：当前节点 task_execution_policy 关键开关（便于与主控/Worker 对照）。"""
    from src.host.task_policy import load_task_execution_policy

    p = load_task_execution_policy()
    return {
        "manual_execution_only": bool(p.get("manual_execution_only")),
        "disable_db_scheduler": bool(p.get("disable_db_scheduler", True)),
        "disable_json_scheduled_jobs": bool(p.get("disable_json_scheduled_jobs", True)),
        "disable_reconnect_task_recovery": bool(p.get("disable_reconnect_task_recovery", True)),
        "disable_auto_tiktok_check_inbox": bool(p.get("disable_auto_tiktok_check_inbox")),
        "disable_executor_inbox_followup": bool(p.get("disable_executor_inbox_followup")),
        "disable_strategy_optimizer": bool(p.get("disable_strategy_optimizer")),
        "disable_event_driven_auto_tasks": bool(p.get("disable_event_driven_auto_tasks")),
    }


@router.get(
    "/tasks",
    response_model=list,
    dependencies=_auth,
    summary="任务列表",
    description=(
        "默认返回未删除任务；`trash_only=true` 时仅返回回收站（`deleted_at` 非空）记录，"
        "且不与集群 Worker 合并。`offset>0` 时不做 Worker 聚合。"
    ),
)
def list_tasks(
    device_id: Optional[str] = Query(None, description="按设备序列号筛选"),
    status: Optional[str] = Query(
        None,
        description="按状态筛选：pending / running / completed / failed / cancelled 等",
    ),
    limit: int = Query(50, ge=1, le=2000, description="返回条数上限"),
    offset: int = Query(0, ge=0, description="分页偏移"),
    trash_only: bool = Query(
        False,
        description="为 true 时仅列出软删除（回收站）任务，仅本机 SQLite，不含 Worker 任务",
    ),
):
    from ..api import task_store, _to_response
    import urllib.request as _ur, json as _json
    import concurrent.futures

    local_items = task_store.list_tasks(
        device_id=device_id,
        status=status,
        limit=limit,
        offset=offset,
        trash_only=trash_only,
    )
    local_responses = [_to_response(t) for t in local_items]

    # 仅当 offset=0 且有注册 Worker 时才聚合（避免在 Worker 节点上递归调用）
    if offset > 0:
        return local_responses
    if trash_only:
        return local_responses

    worker_results = []
    try:
        from ..multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]
        if online_hosts:
            def _fetch_worker(h):
                url = f"http://{h.host_ip}:{h.port}/tasks?limit={limit}"
                if device_id:
                    url += f"&device_id={device_id}"
                if status:
                    url += f"&status={status}"
                resp = _ur.urlopen(_ur.Request(url), timeout=3)
                results = _json.loads(resp.read().decode())
                for t in results:
                    if isinstance(t, dict):
                        t["_worker"] = h.host_ip
                return results

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                futures = {ex.submit(_fetch_worker, h): h for h in online_hosts}
                for f in concurrent.futures.as_completed(futures, timeout=4):
                    try:
                        worker_results.extend(f.result())
                    except Exception as e:
                        logger.debug("Worker fetch failed: %s", e)
    except Exception as e:
        logger.debug("集群聚合失败: %s", e)

    if not worker_results:
        return local_responses

    # 合并：本地任务转为 dict，再和 worker 任务合并去重排序
    local_dicts = [t.model_dump() if hasattr(t, "model_dump") else dict(t) for t in local_responses]
    seen = {t.get("task_id") for t in local_dicts}
    merged = list(local_dicts)
    for t in worker_results:
        if t.get("task_id") not in seen:
            seen.add(t.get("task_id"))
            merged.append(t)
    # 优先保留运行中/等待中任务，不受 limit 截断；其余按时间倒序填满
    _active = [t for t in merged if t.get("status") in ("running", "pending")]
    _inactive = [t for t in merged if t.get("status") not in ("running", "pending")]
    _inactive.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    result = _active + _inactive
    from src.host.task_dispatch_gate import enrich_task_payload_row

    result = [enrich_task_payload_row(x) if isinstance(x, dict) else x for x in result]
    # deduplicate active tasks that might appear in inactive too (shouldn't happen but safety)
    return result[:max(limit, len(_active) + min(limit, len(_inactive)))]


@router.get(
    "/tasks/count",
    dependencies=_auth,
    summary="任务数量",
    description="与 `GET /tasks` 相同的筛选维度；`trash_only=true` 时统计回收站条数（仅本机）。",
)
def get_tasks_count(
    device_id: Optional[str] = Query(None, description="按设备序列号筛选"),
    status: Optional[str] = Query(None, description="按状态筛选"),
    trash_only: bool = Query(
        False,
        description="为 true 时只统计软删除（回收站）中的任务",
    ),
):
    from ..api import task_store
    return {
        "count": task_store.get_task_count(
            device_id=device_id, status=status, trash_only=trash_only
        ),
    }


@router.post("/tasks/purge", dependencies=_auth)
def purge_tasks(days: int = 7):
    """Delete completed/failed/cancelled tasks older than N days."""
    from ..api import task_store, _audit
    count = task_store.purge_old_tasks(days=days)
    _audit("purge_tasks", "", f"deleted={count} days={days}")
    return {"ok": True, "deleted": count}


@router.get("/tasks/active-by-device", dependencies=_auth)
def active_tasks_by_device():
    """返回每台设备当前活跃任务（pending/running）及30秒内完成任务，供设备卡片状态显示。"""
    from ..api import task_store
    import time as _t
    result = {}
    priority = {"running": 0, "pending": 1, "completed": 2, "failed": 2, "cancelled": 3}
    cutoff = _t.time() - 30  # 30秒内完成的任务也展示（完成闪烁动画用）

    for status in ("running", "pending", "completed", "failed"):
        for t in task_store.list_tasks(status=status, limit=300):
            did = t.get("device_id")
            if not did:
                continue
            # 已完成任务：仅包含30秒内完成的
            if status in ("completed", "failed"):
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat((t.get("updated_at") or "").replace("Z", "+00:00"))
                    if dt.timestamp() < cutoff:
                        continue
                except Exception:
                    continue
            existing = result.get(did)
            cur_rank = priority.get(status, 9)
            ex_rank = priority.get(existing["status"], 9) if existing else 99
            if cur_rank < ex_rank:
                result[did] = {
                    "status": status,
                    "type": t.get("type") or "",
                    "progress": (t.get("result") or {}).get("progress", 0),
                    "updated_at": t.get("updated_at"),
                }
    return result



# ── 错误类型定义（供前端 i18n） ──
_ERROR_CATS = [
    "vpn_failure", "network_timeout", "ui_not_found",
    "account_limited", "device_offline", "geo_mismatch",
    "task_timeout", "unknown",
]

import re as _re_ea


def _classify_error(error_text: str) -> str:
    """将错误消息分类到7大类型 + unknown。"""
    if not error_text:
        return "unknown"
    _EA_PATTERNS = [
        ("vpn_failure",     r"VPN|vpn|v2ray|V2Ray|重连失败|未连接|not.*connected"),
        ("task_timeout",    r"执行超时|任务.*超时|\d+s\)"),
        ("device_offline",  r"无可用设备|ADB.*error|adb.*error|掉线|offline|device.*not.*found"),
        ("geo_mismatch",    r"IP.*不在|不匹配|wrong.*country|geo|Italy.*出口"),
        ("account_limited", r"封禁|限流|limit.*exceed|banned|restricted|账号.*异常"),
        ("ui_not_found",    r"not found|未找到|element.*not|XPath|uiautomator|selector.*fail|UI.*fail"),
        ("network_timeout", r"超时|[Tt]imeout|connection.*refused|网络.*不通|无法访问"),
    ]
    for cat, pat in _EA_PATTERNS:
        if _re_ea.search(pat, error_text):
            return cat
    return "unknown"


@router.get("/tasks/error-analysis", dependencies=_auth)
def error_analysis(hours: int = 24, include_samples: bool = False):
    """分析最近 N 小时失败任务：错误分类、趋势、修复建议，并聚合集群 Worker 数据。"""
    import time as _t, json as _j, concurrent.futures as _cf
    import urllib.request as _ur
    from ..database import get_conn
    from ..task_store import _alive_sql

    _aq = _alive_sql()
    cutoff = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - hours * 3600))

    # ── 查询失败任务 ──
    with get_conn() as conn:
        failed_rows = conn.execute(
            f"SELECT type, device_id, result FROM tasks "
            f"WHERE status='failed' AND updated_at > ? AND {_aq} ORDER BY updated_at DESC LIMIT 500",
            (cutoff,),
        ).fetchall()
        total_row = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE updated_at > ? AND {_aq}", (cutoff,)
        ).fetchone()
        hourly_rows = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:00:00Z', updated_at) as hr, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed, "
            "COUNT(*) as total "
            f"FROM tasks WHERE updated_at > ? AND {_aq} "
            "GROUP BY strftime('%Y-%m-%dT%H', updated_at) ORDER BY hr",
            (cutoff,),
        ).fetchall()

    total_tasks = (total_row[0] if total_row else 0) or 0
    total_failed = len(failed_rows)
    failure_rate = round(total_failed / total_tasks * 100, 1) if total_tasks else 0.0

    # ── 分类统计 ──
    categories = {c: 0 for c in _ERROR_CATS}
    by_device: dict = {}

    for row in failed_rows:
        result_raw = row[2]
        result = {}
        if isinstance(result_raw, str):
            try:
                result = _j.loads(result_raw)
            except Exception:
                result = {}
        elif isinstance(result_raw, dict):
            result = result_raw
        err = result.get("error", "") or ""
        cat = _classify_error(err)
        categories[cat] += 1
        did = row[1] or "unknown"
        if did not in by_device:
            by_device[did] = {"total_failed": 0, "top_category": "unknown", "_c": {}}
        by_device[did]["total_failed"] += 1
        by_device[did]["_c"][cat] = by_device[did]["_c"].get(cat, 0) + 1

    for did, ddata in by_device.items():
        cats = ddata.pop("_c", {})
        if cats:
            ddata["top_category"] = max(cats, key=lambda k: cats[k])

    hourly_trend = [
        {"hour": r[0], "failed": r[1], "total": r[2],
         "rate": round(r[1] / r[2] * 100, 1) if r[2] else 0}
        for r in hourly_rows
    ]

    # ── 错误样本（仅 include_samples=True 时返回）──
    samples_by_cat: dict = {}
    if include_samples:
        for row in failed_rows:
            result_raw = row[2]
            result_s = {}
            if isinstance(result_raw, str):
                try:
                    result_s = _j.loads(result_raw)
                except Exception:
                    result_s = {}
            elif isinstance(result_raw, dict):
                result_s = result_raw
            err = result_s.get("error", "") or ""
            cat = _classify_error(err)
            if err and len(samples_by_cat.get(cat, [])) < 3:
                samples_by_cat.setdefault(cat, []).append(err[:120])

    # ── 生成告警 ──
    alerts = []
    if failure_rate > 50:
        alerts.append({"level": "critical",
                        "message": f"失败率严重偏高 {failure_rate}%（过去{hours}小时）",
                        "category": "high_failure_rate"})
    elif failure_rate > 25:
        alerts.append({"level": "warning",
                        "message": f"失败率偏高 {failure_rate}%",
                        "category": "high_failure_rate"})
    if categories["vpn_failure"] >= 3:
        alerts.append({"level": "critical",
                        "message": f"VPN 连续失败 {categories['vpn_failure']} 次（可能影响所有设备）",
                        "category": "vpn_failure"})
    if categories["ui_not_found"] >= 3:
        alerts.append({"level": "warning",
                        "message": f"UI 元素未找到 {categories['ui_not_found']} 次（应用可能已更新）",
                        "category": "ui_not_found"})
    if categories["device_offline"] >= 2:
        alerts.append({"level": "warning",
                        "message": f"设备离线错误 {categories['device_offline']} 次",
                        "category": "device_offline"})
    if categories["account_limited"] > 0:
        alerts.append({"level": "critical",
                        "message": f"检测到 {categories['account_limited']} 次账号限流/封禁",
                        "category": "account_limited"})

    # ── 生成建议 ──
    suggestions = []
    if categories["vpn_failure"] > 0:
        suggestions.append({
            "priority": "high" if categories["vpn_failure"] >= 3 else "medium",
            "category": "vpn_failure", "icon": "🔐",
            "action": "检查 V2RayNG 配置，点击一键重连所有设备 VPN",
            "endpoint": "/vpn/reconnect-all", "method": "POST",
        })
    if categories["ui_not_found"] > 0:
        suggestions.append({
            "priority": "high" if categories["ui_not_found"] >= 3 else "medium",
            "category": "ui_not_found", "icon": "📱",
            "action": "TikTok 界面可能已更新，建议重启所有设备 TikTok 应用",
            "endpoint": None, "method": None,
        })
    if categories["device_offline"] > 0:
        suggestions.append({
            "priority": "high",
            "category": "device_offline", "icon": "📡",
            "action": "批量重连离线设备",
            "endpoint": "/devices/batch-reconnect", "method": "POST",
        })
    if categories["account_limited"] > 0:
        suggestions.append({
            "priority": "critical",
            "category": "account_limited", "icon": "⛔",
            "action": "立即暂停账号敏感操作，等待24小时解除限流",
            "endpoint": "/tasks/cancel-all", "method": "POST",
        })
    if categories["network_timeout"] >= 3:
        suggestions.append({
            "priority": "medium",
            "category": "network_timeout", "icon": "🌐",
            "action": "网络超时频繁，建议检查 WiFi/SIM 信号",
            "endpoint": None, "method": None,
        })
    if categories["geo_mismatch"] > 0:
        suggestions.append({
            "priority": "high",
            "category": "geo_mismatch", "icon": "🗺️",
            "action": "IP 地理位置不符，检查 VPN 节点是否为意大利出口",
            "endpoint": None, "method": None,
        })

    # ── 聚合集群 Worker 数据 ──
    cluster_summary: dict = {}
    try:
        from ..multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        online_hosts = [h for h in coord._hosts.values()
                        if getattr(h, "online", False) and getattr(h, "host_ip", "")]

        def _fetch_worker_ea(h):
            url = f"http://{h.host_ip}:{h.port}/tasks/error-analysis?hours={hours}"
            resp = _ur.urlopen(_ur.Request(url), timeout=4)
            return h.host_ip, _j.loads(resp.read().decode())

        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_fetch_worker_ea, h): h for h in online_hosts}
            for fut in _cf.as_completed(futs, timeout=5):
                try:
                    ip, data = fut.result()
                    w_cats = data.get("categories", {})
                    top_cat = max(w_cats, key=lambda k: w_cats[k]) if w_cats else "unknown"
                    cluster_summary[ip] = {
                        "total_failed": data.get("total_failed", 0),
                        "failure_rate": data.get("failure_rate", 0),
                        "top_category": top_cat,
                    }
                    # 合并分类计数到本地
                    for cat, cnt in w_cats.items():
                        if cat in categories:
                            categories[cat] += cnt
                except Exception:
                    pass
    except Exception:
        pass

    # ── 确定最多的错误类别 ──
    top_category = max(categories, key=lambda k: categories[k]) if any(categories.values()) else "unknown"

    return {
        "period_hours": hours,
        "total_failed": total_failed,
        "total_tasks": total_tasks,
        "failure_rate": failure_rate,
        "top_category": top_category,
        "categories": categories,
        "hourly_trend": hourly_trend,
        "by_device": by_device,
        "alerts": alerts,
        "suggestions": suggestions,
        "cluster_summary": cluster_summary,
        "samples": samples_by_cat if include_samples else {},
    }

@router.get(
    "/tasks/{task_id}",
    dependencies=_auth,
    summary="单条任务详情",
    description=(
        "默认不返回已软删记录（404）。`include_deleted=true` 时可读取回收站中的任务。"
        "集群场景下若本地无记录，会依次请求各 Worker 的同名接口（可带 `include_deleted`）。"
    ),
)
def get_task(
    task_id: str,
    include_deleted: bool = Query(
        False,
        description="为 true 时包含已软删除（回收站）任务，并返回 `deleted_at`",
    ),
):
    from ..api import task_store, _to_response
    t = task_store.get_task(task_id, include_deleted=include_deleted)
    if t:
        return _to_response(t)
    # 本地不存在 — 自动代理到集群 Worker 节点查询（双保险）
    import urllib.request as _ur, json as _json
    try:
        from ..multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        for h in coord._hosts.values():
            if not getattr(h, "online", False):
                continue
            try:
                q = "include_deleted=true" if include_deleted else ""
                url = f"http://{h.host_ip}:{h.port}/tasks/{task_id}"
                if q:
                    url = f"{url}?{q}"
                resp = _ur.urlopen(_ur.Request(url), timeout=3)
                remote = _json.loads(resp.read().decode())
                if isinstance(remote, dict) and "task_id" in remote:
                    remote["_worker"] = h.host_ip
                    from src.host.task_dispatch_gate import enrich_task_payload_row

                    return enrich_task_payload_row(remote)
            except Exception:
                continue
    except Exception:
        pass
    raise HTTPException(status_code=404, detail="任务不存在")


@router.post("/tasks/{task_id}/cancel", dependencies=_auth)
def cancel_task(task_id: str):
    """Cancel a running or pending task. If not local, proxy to owning worker node."""
    from ..api import task_store, get_worker_pool
    import urllib.request as _ur, json as _jj, urllib.error as _ue

    t = task_store.get_task(task_id)

    if not t:
        # 本地不存在 — 尝试代理到集群 Worker 节点
        _proxied = False
        try:
            from ..multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            for h in coord._hosts.values():
                if not h.online or not h.host_ip:
                    continue
                try:
                    _req = _ur.Request(
                        f"http://{h.host_ip}:{h.port}/tasks/{task_id}/cancel",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    _resp = _ur.urlopen(_req, timeout=5)
                    _result = _jj.loads(_resp.read().decode())
                    _proxied = True
                    return {**_result, "_proxied_to": h.host_ip}
                except _ue.HTTPError as _he:
                    if _he.code == 404:
                        continue  # 该节点也没有，继续找下一个
                    raise
                except Exception:
                    continue
        except Exception:
            pass
        if not _proxied:
            raise HTTPException(status_code=404, detail="任务不存在（本地及所有节点均未找到）")

    if t.get("status") in ("completed", "failed", "cancelled"):
        return {"ok": False, "message": f"任务已是 {t['status']} 状态，无需取消"}

    pool = get_worker_pool()
    pool.cancel_task(task_id)
    task_store.set_task_cancelled(task_id)
    return {"ok": True, "task_id": task_id, "message": "取消信号已发送"}


# P1-A fix_action: smart_retry — 复制原 task params 重派 + 失效预检缓存
@router.post("/tasks/{task_id}/retry", dependencies=_auth)
def retry_task_endpoint(task_id: str):
    """前端「🔁 智能重试」按钮端点 (fix_action=smart_retry)。

    与「重派」语义不同：retry 是 **基于失败任务的快照** 复制一份新 task，
    并主动失效该设备的预检缓存（让兜底逻辑重跑而不是读 90s 的 fail 缓存）。

    跨集群：若 task_id 不在本地，按 cancel_task 的模式 proxy 到所属 worker。

    返回 {ok, new_task_id, original_task_id, preflight_invalidated}
    """
    from ..api import task_store, _audit
    from ..task_dispatcher import dispatch_after_create
    import urllib.request as _ur, json as _jj, urllib.error as _ue

    t = task_store.get_task(task_id, include_deleted=True)

    if not t:
        # 跨集群代理（同 cancel_task 模式）
        try:
            from ..multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            for h in coord._hosts.values():
                if not h.online or not h.host_ip:
                    continue
                try:
                    _req = _ur.Request(
                        f"http://{h.host_ip}:{h.port}/tasks/{task_id}/retry",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    _resp = _ur.urlopen(_req, timeout=8)
                    return {**_jj.loads(_resp.read().decode()), "_proxied_to": h.host_ip}
                except _ue.HTTPError as _he:
                    if _he.code == 404:
                        continue
                    raise
                except Exception:
                    continue
        except Exception:
            pass
        raise HTTPException(status_code=404, detail="任务不存在（本地及所有节点均未找到）")

    device_id = t.get("device_id")
    task_type = t.get("type") or ""
    params = dict(t.get("params") or {})

    # P2-④ 风暴防护: 链路深度 + 设备 throttle 双闸
    depth = _compute_retry_depth(t)
    if depth >= _RETRY_DEPTH_MAX:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "retry_depth_exceeded",
                "depth": depth,
                "max": _RETRY_DEPTH_MAX,
                "hint": f"该任务已重试 {depth} 次（系统自动 + 手动）。请先用「🩺 诊断」排查根因，避免无效风暴。",
            },
        )
    allowed, recent = _retry_throttle_check(str(device_id or ""))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "retry_throttled",
                "recent_count": recent,
                "window_seconds": _RETRY_THROTTLE_WINDOW_S,
                "hint": f"该设备 {_RETRY_THROTTLE_WINDOW_S//60} 分钟内已 {recent} 次手动重试，请等待或换设备。",
            },
        )

    # 标记 retry 来源便于审计 + dashboard 区分原始/重试
    params["_retry_of"] = task_id
    params["_created_via"] = "smart_retry"
    params["_retry_depth"] = depth + 1  # 让下游知道这是第几跳，便于看板分析

    invalidated = False
    if device_id:
        try:
            from src.host.preflight import invalidate_cache
            invalidate_cache(device_id)
            invalidated = True
        except Exception as e:
            logger.warning("[tasks/retry] %s preflight invalidate_cache failed: %s", str(device_id)[:8], e)

    new_task_id = task_store.create_task(
        task_type=task_type,
        device_id=device_id,
        params=params,
        policy_id=t.get("policy_id"),
        batch_id=t.get("batch_id") or "",
        priority=t.get("priority", 50),
    )
    dispatch_after_create(
        task_id=new_task_id,
        device_id=device_id,
        task_type=task_type,
        params=params,
        priority=t.get("priority", 50),
    )
    _audit("retry_task", str(device_id or ""), f"orig={task_id} new={new_task_id}")
    return {
        "ok": True,
        "new_task_id": new_task_id,
        "original_task_id": task_id,
        "preflight_invalidated": invalidated,
    }


# P2-② 失败任务证据查询 (列表 + 单文件下载 + 清理)
@router.get("/tasks/{task_id}/forensics", dependencies=_auth)
def list_task_forensics(task_id: str):
    """返回该 task_id 的所有失败现场快照清单 (按时间倒序).

    输出每项含 ts (UTC 时间戳目录名) / files (含 size) / meta (从 meta.json).
    供前端任务详情对话框 "📸 失败证据" 折叠面板消费.
    """
    from src.host.task_forensics import list_forensics
    return {"task_id": task_id, "snapshots": list_forensics(task_id)}


@router.get("/tasks/{task_id}/forensics/{ts}/{filename}", dependencies=_auth)
def download_forensics_file(task_id: str, ts: str, filename: str):
    """下载/预览单个证据文件 (screencap.png / logcat.txt / meta.json).

    含路径穿越防护 (forensics_path 内部校验). 任何失败都返 404 而非 500.
    """
    from fastapi.responses import FileResponse
    from src.host.task_forensics import forensics_path
    p = forensics_path(task_id, ts, filename)
    if not p:
        raise HTTPException(status_code=404, detail="forensics file not found or unsafe path")
    media_type = "image/png" if filename.endswith(".png") else (
        "application/json" if filename.endswith(".json") else "text/plain"
    )
    return FileResponse(p, media_type=media_type, filename=filename)


@router.post("/admin/forensics/cleanup", dependencies=_auth)
def admin_forensics_cleanup(retention_days: int = 7):
    """运维清理 > N 天的失败证据 (启动时 + 定时调一次)."""
    from src.host.task_forensics import startup_cleanup
    n = startup_cleanup(retention_days=retention_days)
    return {"ok": True, "removed": n, "retention_days": retention_days}


@router.post("/tasks/cancel-all", dependencies=_auth)
def cancel_all_tasks():
    """Cancel all running and pending tasks (local + cluster workers)."""
    from ..api import task_store, get_worker_pool, _audit
    tasks = task_store.list_tasks(status="running") + task_store.list_tasks(status="pending")
    pool = get_worker_pool()
    cancelled = 0
    for t in tasks:
        tid = t.get("task_id", "")
        pool.cancel_task(tid)
        task_store.set_task_cancelled(tid)
        cancelled += 1

    # 同步取消集群 Worker 节点上的任务
    worker_cancelled = 0
    try:
        from ..multi_host import get_cluster_coordinator
        import urllib.request as _ur, json as _json
        coord = get_cluster_coordinator()
        for h in coord._hosts.values():
            if not h.online or not h.host_ip:
                continue
            try:
                req = _ur.Request(
                    f"http://{h.host_ip}:{h.port}/tasks/cancel-all",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = _ur.urlopen(req, timeout=5)
                result = _json.loads(resp.read().decode())
                worker_cancelled += result.get("cancelled", 0)
            except Exception:
                pass
    except Exception:
        pass

    _audit("cancel_all_tasks", "", f"cancelled={cancelled} worker_cancelled={worker_cancelled}")
    return {"ok": True, "cancelled": cancelled + worker_cancelled,
            "local": cancelled, "workers": worker_cancelled}


@router.post("/tasks/cancel-batch/{batch_id}", dependencies=_auth)
def cancel_batch_tasks(batch_id: str):
    """Cancel all tasks belonging to a batch."""
    from ..api import task_store, get_worker_pool, _audit
    all_tasks = task_store.list_tasks()
    pool = get_worker_pool()
    cancelled = 0
    for t in all_tasks:
        if t.get("batch_id") == batch_id and t.get("status") in (
                "pending", "running"):
            pool.cancel_task(t["task_id"])
            task_store.set_task_cancelled(t["task_id"])
            cancelled += 1
    _audit("cancel_batch", batch_id, f"cancelled={cancelled}")
    return {"ok": True, "batch_id": batch_id, "cancelled": cancelled}


@router.put("/tasks/{task_id}/result", dependencies=_auth)
def report_task_result(task_id: str, body: TaskResultReport):
    from ..api import task_store
    t = task_store.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    task_store.set_task_result(
        task_id,
        success=body.success,
        error=body.error or "",
        screenshot_path=body.screenshot_path or "",
    )
    return {"ok": True}


def run_batch_tasks_core(body: dict) -> dict:
    """
    与 POST /tasks/batch 完全相同的派发逻辑，供定时器 / job_scheduler 等进程内调用，
    避免重复实现；语义上等价于经 HTTP 调用 /tasks/batch。
    """
    from ..api import task_store, get_worker_pool, run_task, _config_path
    from src.device_control.device_manager import get_device_manager
    from ..executor import _get_device_id
    from src.host.task_param_rules import maybe_normalize_for_task

    task_type = body.get("type", "")
    device_ids = body.get("device_ids", [])
    params = maybe_normalize_for_task(task_type, body.get("params") or {})
    if isinstance(params, dict) and "_created_via" not in params:
        cv = (body.get("created_via") or "").strip() or "batch_api"
        params = {**params, "_created_via": cv}
    if not task_type or not device_ids:
        raise ValueError("type 和 device_ids 必填")

    import uuid as _uuid
    batch_id = str(_uuid.uuid4())[:8]
    task_ids = []

    manager = get_device_manager(_config_path)
    manager.discover_devices()
    pool = get_worker_pool()

    for did in device_ids:
        resolved = _get_device_id(manager, did, _config_path)
        device_for_lock = resolved or did
        tid = task_store.create_task(task_type, device_for_lock, params,
                                     batch_id=batch_id)
        pool.submit(tid, device_for_lock, run_task, tid, _config_path)
        task_ids.append(tid)

    from ..event_stream import push_event
    push_event("batch.created", {"batch_id": batch_id, "count": len(task_ids),
                                  "type": task_type})
    return {"batch_id": batch_id, "task_ids": task_ids, "count": len(task_ids)}


@router.post("/tasks/batch", dependencies=_auth)
def create_batch_tasks(body: dict):
    """Create a batch of tasks for multiple devices at once."""
    try:
        return run_batch_tasks_core(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tasks/batch/{batch_id}", dependencies=_auth)
def batch_progress(batch_id: str):
    from ..api import task_store
    return task_store.get_batch_progress(batch_id)


# ── 2026-05-08 P2-2: 任务链编排 API ──────────────────────────────────

@router.get("/tasks/chains", dependencies=_auth)
def list_task_chains():
    """列出所有可用的任务链配置。"""
    from ..task_chain import list_chains
    return {"chains": list_chains()}


@router.post("/tasks/chain", dependencies=_auth)
def create_task_chain(body: dict):
    """启动一条任务链。

    body: {"chain_id": "fb_full_funnel", "device_id": "xxx", "params": {...}}
    或批量: {"chain_id": "fb_full_funnel", "device_ids": ["a","b"], "params": {...}}
    """
    from ..task_chain import create_chain, create_chain_batch

    chain_id = body.get("chain_id", "")
    if not chain_id:
        raise HTTPException(status_code=400, detail="chain_id 必填")

    params = body.get("params") or {}
    device_ids = body.get("device_ids") or []
    device_id = body.get("device_id", "")

    if device_ids:
        return create_chain_batch(chain_id, device_ids, params)
    elif device_id:
        return create_chain(chain_id, device_id, params)
    else:
        raise HTTPException(status_code=400, detail="device_id 或 device_ids 必填")


@router.get("/tasks/chain-runs", dependencies=_auth)
def list_chain_runs(device_id: str = "", limit: int = 20):
    """查询最近的链执行记录。"""
    from ..task_chain import get_chain_runs
    return {"runs": get_chain_runs(device_id=device_id, limit=limit)}


# ── 2026-05-09 P3-3: 链模板 CRUD ─────────────────────────────────────

@router.get("/tasks/chains/{chain_id}", dependencies=_auth)
def get_chain_detail_endpoint(chain_id: str):
    """获取单条链完整配置。"""
    from ..task_chain import get_chain_detail
    detail = get_chain_detail(chain_id)
    if not detail:
        raise HTTPException(status_code=404, detail="链定义不存在")
    return detail


@router.put("/tasks/chains/{chain_id}", dependencies=_auth)
def save_chain_endpoint(chain_id: str, body: dict):
    """新增或更新一条链定义。

    body: {"name": "...", "description": "...", "platform": "facebook",
           "steps": [{"task_type": "...", "params": {...}, "on_fail": "skip"}]}
    """
    from ..task_chain import save_chain
    try:
        save_chain(chain_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "chain_id": chain_id}


@router.delete("/tasks/chains/{chain_id}", dependencies=_auth)
def delete_chain_endpoint(chain_id: str):
    """删除一条链定义。"""
    from ..task_chain import delete_chain
    if not delete_chain(chain_id):
        raise HTTPException(status_code=404, detail="链定义不存在")
    return {"ok": True, "chain_id": chain_id}


@router.get("/stats", dependencies=_auth)
def stats():
    from ..api import task_store
    return task_store.get_stats()


# ── 2026-05-09 P3-2: 自愈仪表盘 API ──────────────────────────────────

@router.get("/tasks/auto-heal/stats", dependencies=_auth)
def auto_heal_stats():
    """自动自愈统计（P3-2 自愈仪表盘）。"""
    try:
        from ..task_failure_healer import get_task_failure_healer
        return get_task_failure_healer().get_stats()
    except Exception:
        return {"total": 0, "healed": 0, "skipped": 0}


# ── 2026-05-09 P4-1: 智能链推荐 API ──────────────────────────────────

@router.get("/tasks/chains/recommend/{platform}", dependencies=_auth)
def recommend_chains_endpoint(platform: str):
    """基于历史成功率为平台推荐最优链模板。"""
    from ..chain_advisor import recommend_chains
    return {"recommendations": recommend_chains(platform)}


# ── 2026-05-09 P6-2: 自愈策略热配置 API ───────────────────────────────

@router.get("/tasks/auto-heal/config", dependencies=_auth)
def auto_heal_config():
    """获取自愈运行时配置。"""
    from ..task_failure_healer import _RUNTIME_CONFIG
    return dict(_RUNTIME_CONFIG)


@router.post("/tasks/auto-heal/config", dependencies=_auth)
def update_auto_heal_config(body: dict):
    """热更新自愈配置，无需重启。"""
    from ..task_failure_healer import _RUNTIME_CONFIG
    allowed = {"max_heals_per_hour", "min_recovery_rate", "enabled"}
    updated = {}
    for k, v in body.items():
        if k in allowed:
            if k == "enabled":
                _RUNTIME_CONFIG[k] = bool(v)
            else:
                _RUNTIME_CONFIG[k] = max(1, int(v))
            updated[k] = _RUNTIME_CONFIG[k]
    if not updated:
        raise HTTPException(status_code=400,
                            detail=f"允许的配置项: {allowed}")
    return {"ok": True, "config": dict(_RUNTIME_CONFIG)}


# ── 2026-05-09 P4-2: 自愈效果分析 API ────────────────────────────────

@router.get("/tasks/auto-heal/effectiveness", dependencies=_auth)
def auto_heal_effectiveness():
    """统计每种自愈动作的真实恢复率。"""
    from ..chain_advisor import get_heal_effectiveness
    return get_heal_effectiveness()


# ── 2026-05-09 P5-4: 链执行趋势 API ─────────────────────────────────

@router.get("/tasks/chain-trend", dependencies=_auth)
def chain_trend(days: int = 7):
    """返回最近 N 天每天的链执行统计。"""
    from ..chain_advisor import get_chain_trend
    return {"trend": get_chain_trend(days=days)}


# ── 2026-05-09 P6-4: 链模板导入/导出 API ─────────────────────────────

@router.get("/tasks/chains/export", dependencies=_auth)
def export_chains():
    """导出所有链模板为 YAML。"""
    from ..task_chain import export_chains_yaml
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(export_chains_yaml(),
                             media_type="text/yaml",
                             headers={"Content-Disposition":
                                      "attachment; filename=task_chains.yaml"})


@router.post("/tasks/chains/import", dependencies=_auth)
def import_chains(body: dict):
    """导入链模板 YAML。body: {yaml_text, overwrite?}"""
    from ..task_chain import import_chains_yaml
    yaml_text = body.get("yaml_text", "")
    if not yaml_text:
        raise HTTPException(status_code=400, detail="yaml_text required")
    overwrite = body.get("overwrite", False)
    return import_chains_yaml(yaml_text, overwrite=overwrite)


# ── 2026-05-09 P6-1: 链执行回放 API ──────────────────────────────────

@router.get("/tasks/chain-runs/{run_id}/detail", dependencies=_auth)
def chain_run_detail(run_id: str):
    """获取链运行详情(含每步时间线)。"""
    from ..task_chain import get_chain_run_detail
    detail = get_chain_run_detail(run_id)
    if not detail:
        raise HTTPException(status_code=404, detail="chain run not found")
    return detail


@router.post("/tasks/chain-runs/{run_id}/rerun", dependencies=_auth)
def chain_run_rerun(run_id: str):
    """重新执行链中失败的步骤。"""
    from ..task_chain import rerun_failed_steps
    try:
        return rerun_failed_steps(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/pool", dependencies=_auth)
def pool_status():
    """工作池运行状态"""
    from ..api import get_worker_pool
    return get_worker_pool().get_status()


@router.delete(
    "/tasks/{task_id}",
    dependencies=_auth,
    summary="软删除单条任务",
    description="写入 `deleted_at` 移入回收站；`running`/`pending` 需先取消。已在回收站则失败。",
)
def delete_task(task_id: str):
    """软删除一个任务到回收站（仅限非 running/pending）。本地无记录时转发集群 Worker（与 cancel 一致）。"""
    from ..api import _audit, task_store

    t = task_store.get_task(task_id)
    if not t:
        w = _proxy_delete_task_to_worker(task_id)
        if w:
            _audit(
                "delete_task",
                task_id[:14],
                f"proxied_to={w} type=worker",
            )
            return {"ok": True, "_proxied_to": w}
        raise HTTPException(
            status_code=404,
            detail="任务不存在（本地及所有节点均未找到）",
        )
    if t.get("status") in ("running", "pending"):
        raise HTTPException(status_code=400, detail="运行中或等待中的任务请先取消")
    ok = task_store.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="删除失败（可能已在回收站）")
    _audit(
        "delete_task",
        task_id[:14],
        f"status={t.get('status')} type={t.get('type', '')[:32]}",
    )
    return {"ok": True}


@router.post(
    "/tasks/delete-batch",
    dependencies=_auth,
    summary="批量软删除",
    description="移入回收站；running/pending/已在回收站/不存在会列入 skipped。",
)
def delete_tasks_batch_endpoint(body: TaskBatchDelete):
    """批量软删除；running/pending/不存在会出现在 skipped 中。本地 not_found 会尝试各 Worker DELETE。"""
    from ..api import _audit, task_store

    try:
        out = task_store.delete_tasks_batch(body.task_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    new_skipped: List[Dict[str, str]] = []
    extra = 0
    for sk in out["skipped"]:
        if sk.get("reason") != "not_found":
            new_skipped.append(sk)
            continue
        tid = (sk.get("task_id") or "").strip()
        if not tid:
            new_skipped.append(sk)
            continue
        if _proxy_delete_task_to_worker(tid):
            extra += 1
            out["deleted_ids"].append(tid)
        else:
            new_skipped.append(sk)
    if extra:
        out["deleted"] = out["deleted"] + extra
        out["skipped"] = new_skipped
    _hint = (body.task_ids[0][:14] if body.task_ids else "") or ""
    _audit(
        "delete_tasks_batch",
        _hint,
        f"deleted={out['deleted']} skipped={len(out['skipped'])} ids={len(body.task_ids or [])}",
    )
    return out


@router.post(
    "/tasks/trash-all-by-status",
    dependencies=_auth,
    summary="按状态一次性软删（本机 SQL + 可选集群转发）",
    description=(
        "将本机 SQLite 中指定终态且未回收的任务批量移入回收站；"
        "`forward_cluster=true`（默认）时再请求各在线 Worker 的同一接口（`forward_cluster=false`）以免递归。"
        "用于「清空失败」等场景，避免前端成百上千次 delete-batch。"
    ),
)
def trash_all_tasks_by_status_endpoint(
    status: str = Query(
        ...,
        description="failed / completed / cancelled",
        pattern="^(failed|completed|cancelled)$",
    ),
    forward_cluster: bool = Query(
        True,
        description="为 true 时向集群内其他节点转发（各节点只清本库）",
    ),
):
    from ..api import _audit, task_store

    try:
        n_local = task_store.soft_delete_all_by_status(status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    worker_total = 0
    worker_details: List[Dict[str, Any]] = []
    if forward_cluster:
        import json as _json
        import urllib.error as _ue
        import urllib.request as _ur

        _key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
        try:
            from ..multi_host import get_cluster_coordinator

            coord = get_cluster_coordinator()
            st_enc = urllib.parse.quote(status, safe="")
            for h in coord._hosts.values():
                if not getattr(h, "online", False) or not getattr(h, "host_ip", ""):
                    continue
                port = int(getattr(h, "port", 8000) or 8000)
                url = (
                    f"http://{h.host_ip}:{port}/tasks/trash-all-by-status"
                    f"?status={st_enc}&forward_cluster=false"
                )
                hdrs: Dict[str, str] = {}
                if _key:
                    hdrs["X-API-Key"] = _key
                try:
                    req = _ur.Request(url, method="POST", headers=hdrs)
                    resp = _ur.urlopen(req, timeout=45)
                    body = _json.loads(resp.read().decode())
                    wn = int(body.get("deleted_local") or 0)
                    worker_total += wn
                    worker_details.append({"host_ip": h.host_ip, "deleted_local": wn})
                except _ue.HTTPError as he:
                    logger.warning(
                        "trash-all-by-status forward HTTP %s %s: %s",
                        h.host_ip,
                        getattr(he, "code", "?"),
                        he,
                    )
                except Exception as e:
                    logger.warning("trash-all-by-status forward failed %s: %s", h.host_ip, e)
        except Exception as e:
            logger.debug("cluster forward trash-all: %s", e)

    _audit(
        "trash_all_by_status",
        status[:12],
        f"local={n_local} workers={worker_total} forward={forward_cluster}",
    )
    deleted_total = n_local + worker_total
    return {
        "ok": True,
        "status": status,
        "deleted_local": n_local,
        "deleted_on_workers": worker_total,
        "deleted_total": deleted_total,
        "worker_details": worker_details,
    }


@router.post(
    "/tasks/restore-batch",
    dependencies=_auth,
    summary="批量恢复（回收站）",
    description="清空 `deleted_at`；不在回收站/不存在等记入 `skipped`。单次最多 100 条。",
)
def restore_tasks_batch_endpoint(body: TaskBatchDelete):
    """从回收站恢复任务。"""
    from ..api import _audit, task_store

    try:
        out = task_store.restore_tasks_batch(body.task_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _hint = (body.task_ids[0][:14] if body.task_ids else "") or ""
    _audit(
        "restore_tasks_batch",
        _hint,
        f"restored={out['restored']} skipped={len(out['skipped'])} ids={len(body.task_ids or [])}",
    )
    return out


@router.post(
    "/tasks/erase-batch",
    dependencies=_auth,
    summary="批量永久删除",
    description="仅删除 `deleted_at` 已设置的行（物理 DELETE）；未在回收站则 `skipped`。单次最多 100 条。",
)
def erase_tasks_batch_endpoint(body: TaskBatchDelete):
    """永久删除回收站中的任务（物理删库）。"""
    from ..api import _audit, task_store

    try:
        out = task_store.erase_tasks_batch(body.task_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _hint = (body.task_ids[0][:14] if body.task_ids else "") or ""
    _audit(
        "erase_tasks_batch",
        _hint,
        f"erased={out['erased']} skipped={len(out['skipped'])} ids={len(body.task_ids or [])}",
    )
    return out


