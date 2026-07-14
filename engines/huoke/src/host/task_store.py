# -*- coding: utf-8 -*-
"""任务存储 — SQLite 持久化版。重启不丢数据。"""

import json
import time
import uuid
import logging
from typing import Any, Dict, List, Optional

from .database import get_conn

logger = logging.getLogger(__name__)

# 连续失败追踪: device_id → consecutive_failure_count
_device_fail_streak: dict = {}
_FAIL_STREAK_THRESHOLD = 3  # 连续失败N次触发Telegram告警


def _push(event_type: str, **kw):
    try:
        from .event_stream import push_event
        push_event(event_type, kw)
    except Exception as e:
        logger.debug("事件推送失败: %s", e)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_task(task_type: str, device_id: Optional[str],
                params: dict, policy_id: Optional[str] = None,
                batch_id: str = "", priority: int = 50,
                max_retries: int = 0) -> str:
    """
    创建任务。priority: 0=最低, 50=默认, 100=高意向紧急回复。
    调度器按 priority DESC, created_at ASC 排序取任务。
    max_retries: 失败后最大重试次数，0 表示不重试。
    """
    task_id = str(uuid.uuid4())
    now = _now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, type, device_id, status, params, policy_id, batch_id, priority, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
            (task_id, task_type, device_id, json.dumps(params, ensure_ascii=False),
             policy_id, batch_id, priority, max_retries, now, now),
        )
    _push("task.created", task_id=task_id, task_type=task_type,
          device_id=device_id or "", batch_id=batch_id, priority=priority)
    return task_id


def _alive_sql() -> str:
    """未进回收站的记录（deleted_at 为空）。"""
    return "(deleted_at IS NULL OR deleted_at = '')"


def get_batch_progress(batch_id: str) -> dict:
    """Get aggregated progress for a batch of tasks."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks "
            f"WHERE batch_id = ? AND {_alive_sql()} GROUP BY status",
            (batch_id,),
        ).fetchall()
    stats = {r["status"]: r["cnt"] for r in rows}
    total = sum(stats.values())
    completed = stats.get("completed", 0)
    failed = stats.get("failed", 0)
    done = completed + failed + stats.get("cancelled", 0)
    return {
        "batch_id": batch_id,
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": stats.get("running", 0),
        "pending": stats.get("pending", 0),
        "cancelled": stats.get("cancelled", 0),
        "progress": int(done / total * 100) if total else 0,
    }


def get_task(task_id: str, include_deleted: bool = False) -> Optional[dict]:
    """include_deleted=True 时回收站记录也可取回（恢复接口用）。"""
    with get_conn() as conn:
        if include_deleted:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT * FROM tasks WHERE task_id = ? AND {_alive_sql()}",
                (task_id,),
            ).fetchone()
    return _row_to_dict(row) if row else None


def delete_task(task_id: str) -> bool:
    """软删除：写入 deleted_at（回收站）。"""
    now = _now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE task_id = ? AND {_alive_sql()} "
            "AND status NOT IN ('running', 'pending')",
            (now, now, task_id),
        )
    return cur.rowcount > 0


BATCH_DELETE_MAX = 100


def delete_tasks_batch(task_ids: List[str]) -> Dict[str, Any]:
    """批量删除任务。跳过不存在、running、pending。单次最多 BATCH_DELETE_MAX 条。"""
    if not task_ids:
        return {"ok": True, "deleted": 0, "deleted_ids": [], "skipped": []}
    seen = set()
    uniq: List[str] = []
    for x in task_ids:
        if not x or not isinstance(x, str):
            continue
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    if len(uniq) > BATCH_DELETE_MAX:
        raise ValueError(f"单次最多删除 {BATCH_DELETE_MAX} 条")

    deleted_ids: List[str] = []
    skipped: List[Dict[str, str]] = []
    now = _now_iso()
    with get_conn() as conn:
        for tid in uniq:
            row = conn.execute(
                "SELECT task_id, status, deleted_at FROM tasks WHERE task_id = ?",
                (tid,),
            ).fetchone()
            if not row:
                skipped.append({"task_id": tid, "reason": "not_found"})
                continue
            if row["deleted_at"]:
                skipped.append({"task_id": tid, "reason": "already_in_trash"})
                continue
            st = row["status"]
            if st in ("running", "pending"):
                skipped.append({"task_id": tid, "reason": "running_or_pending"})
                continue
            cur = conn.execute(
                f"UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE task_id = ? AND {_alive_sql()}",
                (now, now, tid),
            )
            if cur.rowcount:
                deleted_ids.append(tid)
    return {
        "ok": True,
        "deleted": len(deleted_ids),
        "deleted_ids": deleted_ids,
        "skipped": skipped,
    }


_TRASH_ALL_STATUSES = frozenset({"failed", "completed", "cancelled"})


def soft_delete_all_by_status(status: str) -> int:
    """
    将指定终态且未在回收站的任务一次性软删除（SET deleted_at）。
    仅允许 failed / completed / cancelled（不含 running/pending）。
    返回受影响行数。
    """
    st = (status or "").strip().lower()
    if st not in _TRASH_ALL_STATUSES:
        raise ValueError("status 必须是 failed、completed 或 cancelled")
    now = _now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET deleted_at = ?, updated_at = ? "
            f"WHERE {_alive_sql()} AND status = ?",
            (now, now, st),
        )
    return int(cur.rowcount or 0)


def restore_tasks_batch(task_ids: List[str]) -> Dict[str, Any]:
    """从回收站恢复（清空 deleted_at）。单次最多 BATCH_DELETE_MAX 条。"""
    if not task_ids:
        return {"ok": True, "restored": 0, "restored_ids": [], "skipped": []}
    seen = set()
    uniq: List[str] = []
    for x in task_ids:
        if not x or not isinstance(x, str) or x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    if len(uniq) > BATCH_DELETE_MAX:
        raise ValueError(f"单次最多处理 {BATCH_DELETE_MAX} 条")

    restored_ids: List[str] = []
    skipped: List[Dict[str, str]] = []
    now = _now_iso()
    with get_conn() as conn:
        for tid in uniq:
            row = conn.execute(
                "SELECT task_id, deleted_at FROM tasks WHERE task_id = ?",
                (tid,),
            ).fetchone()
            if not row:
                skipped.append({"task_id": tid, "reason": "not_found"})
                continue
            if not row["deleted_at"]:
                skipped.append({"task_id": tid, "reason": "not_in_trash"})
                continue
            cur = conn.execute(
                "UPDATE tasks SET deleted_at = NULL, updated_at = ? WHERE task_id = ?",
                (now, tid),
            )
            if cur.rowcount:
                restored_ids.append(tid)
    return {
        "ok": True,
        "restored": len(restored_ids),
        "restored_ids": restored_ids,
        "skipped": skipped,
    }


def erase_tasks_batch(task_ids: List[str]) -> Dict[str, Any]:
    """永久删除（仅回收站中记录）。物理 DELETE。"""
    if not task_ids:
        return {"ok": True, "erased": 0, "erased_ids": [], "skipped": []}
    seen = set()
    uniq: List[str] = []
    for x in task_ids:
        if not x or not isinstance(x, str) or x in seen:
            continue
        seen.add(x)
        uniq.append(x)
    if len(uniq) > BATCH_DELETE_MAX:
        raise ValueError(f"单次最多处理 {BATCH_DELETE_MAX} 条")

    erased_ids: List[str] = []
    skipped: List[Dict[str, str]] = []
    with get_conn() as conn:
        for tid in uniq:
            row = conn.execute(
                "SELECT task_id, deleted_at FROM tasks WHERE task_id = ?",
                (tid,),
            ).fetchone()
            if not row:
                skipped.append({"task_id": tid, "reason": "not_found"})
                continue
            if not row["deleted_at"]:
                skipped.append({"task_id": tid, "reason": "not_in_trash"})
                continue
            cur = conn.execute("DELETE FROM tasks WHERE task_id = ?", (tid,))
            if cur.rowcount:
                erased_ids.append(tid)
    return {
        "ok": True,
        "erased": len(erased_ids),
        "erased_ids": erased_ids,
        "skipped": skipped,
    }


def list_tasks(device_id: Optional[str] = None,
               status: Optional[str] = None,
               limit: int = 50,
               offset: int = 0,
               trash_only: bool = False) -> List[dict]:
    clauses, params = [], []
    if trash_only:
        clauses.append("deleted_at IS NOT NULL AND deleted_at != ''")
    else:
        clauses.append(_alive_sql())
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses)
    sql = f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.append(limit)
    params.append(offset)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_task_count(device_id: Optional[str] = None, status: Optional[str] = None,
                   trash_only: bool = False) -> int:
    """Get total count of tasks matching filters."""
    clauses, params = [], []
    if trash_only:
        clauses.append("deleted_at IS NOT NULL AND deleted_at != ''")
    else:
        clauses.append(_alive_sql())
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses)
    with get_conn() as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM tasks {where}", params).fetchone()
    return row[0] if row else 0


def count_today_by_preset() -> Dict[str, int]:
    """统计今日各 preset_key 的任务创建次数（从 params JSON 提取 _preset_key）。"""
    today_start = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    sql = (
        "SELECT json_extract(params, '$._preset_key') AS pk, COUNT(*) AS cnt "
        f"FROM tasks WHERE created_at >= ? AND {_alive_sql()} "
        "AND pk IS NOT NULL GROUP BY pk"
    )
    with get_conn() as conn:
        rows = conn.execute(sql, (today_start,)).fetchall()
    return {r[0]: r[1] for r in rows if r[0]}


def purge_old_tasks(days: int = 7) -> int:
    """物理删除：终态且超过 N 天，且未在回收站（deleted_at 为空）。"""
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM tasks WHERE {_alive_sql()} "
            "AND status IN ('completed', 'failed', 'cancelled', 'blocked') "
            "AND created_at < datetime('now', ?)",
            (f'-{days} days',)
        )
    count = cur.rowcount
    if count:
        logger.info("Auto-purged %d old tasks (>%d days)", count, days)
    return count


def set_task_running(task_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'running', updated_at = ? WHERE task_id = ?",
            (_now_iso(), task_id),
        )
    _push("task.running", task_id=task_id)


def set_task_step(step: str, sub_step: str = "",
                  task_id: str = "") -> bool:
    """更新 running task 的当前业务步骤 — dashboard 实时可见 + Phase 2 P0 #2.

    Args:
        step: 主步骤 (e.g. "搜索群组" / "群成员打招呼" / "添加好友")
        sub_step: 副步骤 (e.g. "ママ友" / "第 5/30 人" / "@john")
        task_id: 任务 ID; 默认从 thread-local task_context 隐式取
                 (executor.run_task 已 set_task_context → 业务方法不需显式传)

    设计:
        - 写到 checkpoint.current_step / current_sub_step / current_step_at
        - 用 SQLite json_patch 原子合并 (避免 read-modify-write 竞态)
        - 仅 status='running' 时写, 防止 SLA abort 后业务方法的 trailing 调用
          污染 fail 后的 task 状态
        - 同时刷 updated_at — 给 SLA tasks/orphan reaper 提供精确进展信号
        - 失败静默 (写 step 不该影响业务) → 返回 False, 调用方可忽略

    Returns:
        True 写入生效 (1 行); False 没写 (任务不存在 / 不在 running / 异常)

    2026-04-27 Phase 2 P0 #2 加: 用户 5h 死循环时手抓 9 张截图排查 — dashboard
    任务详情应该实时显示"现在做到第几步".
    """
    if not step:
        return False
    if not task_id:
        try:
            from src.utils.log_config import _task_context
            task_id = getattr(_task_context, "task_id", "") or ""
        except Exception:
            task_id = ""
    if not task_id:
        return False
    now = _now_iso()
    payload = json.dumps({
        "current_step": step,
        "current_sub_step": sub_step or "",
        "current_step_at": now,
    }, ensure_ascii=False)
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET "
                " checkpoint = json_patch(COALESCE(NULLIF(checkpoint, ''), '{}'), ?), "
                " updated_at = ? "
                "WHERE task_id = ? AND status = 'running'",
                (payload, now, task_id),
            )
        return cur.rowcount > 0
    except Exception:
        return False


def set_task_result(task_id: str, success: bool, error: str = "",
                    screenshot_path: str = "", extra: Optional[dict] = None) -> None:
    result = {"success": success, "error": error,
              "screenshot_path": screenshot_path, **(extra or {})}
    try:
        from src.host.task_dispatch_gate import result_dict_with_gate_hints

        merged = result_dict_with_gate_hints(result)
        if merged is not result:
            result = merged
    except Exception:
        pass
    status = "completed" if success else "failed"
    device_id = extra.get("device_id", "") if extra else ""

    with get_conn() as conn:
        row = conn.execute(
            "SELECT retry_count, max_retries FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        retry_count = row[0] if row else 0
        max_retries = row[1] if row else 0

        if not success and retry_count < max_retries:
            # 安排重试，使用指数退避：30s, 60s, 120s...
            delay = 30 * (2 ** retry_count)
            next_retry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delay))

            # 将 checkpoint 注入到 params，以便重试时从断点继续
            params_row = conn.execute(
                "SELECT params, checkpoint FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            retry_params_json = None
            if params_row:
                raw_params = params_row[0]
                raw_checkpoint = params_row[1]
                if raw_checkpoint:
                    try:
                        checkpoint_data = json.loads(raw_checkpoint)
                        existing_params = json.loads(raw_params) if raw_params else {}
                        existing_params["_checkpoint"] = checkpoint_data
                        retry_params_json = json.dumps(existing_params, ensure_ascii=False)
                        logger.info("重试任务 %s 携带 checkpoint (phase=%s)",
                                    task_id[:8],
                                    checkpoint_data.get("phase", "?"))
                    except (json.JSONDecodeError, TypeError):
                        pass

            if retry_params_json is not None:
                conn.execute(
                    "UPDATE tasks SET status='pending', result=?, params=?, "
                    "retry_count=retry_count+1, next_retry_at=?, updated_at=? WHERE task_id=?",
                    (json.dumps(result, ensure_ascii=False), retry_params_json,
                     next_retry, _now_iso(), task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status='pending', result=?, retry_count=retry_count+1, "
                    "next_retry_at=?, updated_at=? WHERE task_id=?",
                    (json.dumps(result, ensure_ascii=False), next_retry, _now_iso(), task_id),
                )
            _push("task.retry_scheduled", task_id=task_id,
                  retry_count=retry_count + 1, next_retry_at=next_retry)
            return

        conn.execute(
            "UPDATE tasks SET status = ?, result = ?, updated_at = ? WHERE task_id = ?",
            (status, json.dumps(result, ensure_ascii=False), _now_iso(), task_id),
        )
    _push(f"task.{status}", task_id=task_id, success=success,
          error=error, device_id=device_id)
    # P2-② 失败任务自动留证据 (异步 fail-safe, 不阻塞当前调用)
    # P1.5 (2026-04-30): 扩展触发条件 —— task.status='success' 但内部含
    # automation_* outcome 或 steps_failed 时, 仍触发取证。原因：FB campaign_run
    # 即便有 step 失败, 整任务返回 True (partial 也算 done), 导致历来失败现场零证据。
    _need_forensics = (status == "failed")
    if not _need_forensics and isinstance(result, dict):
        _oc = str(result.get("outcome") or "")
        if _oc.startswith("automation_") or _oc.startswith("missing_param"):
            _need_forensics = True
        elif result.get("steps_failed"):
            # 任一子步骤标记失败 → 也取证（campaign_run 多步剧本场景）
            _need_forensics = True
    if _need_forensics and device_id:
        try:
            from .task_forensics import capture_forensics
            _err_for_forensics = error or (
                isinstance(result, dict)
                and (result.get("outcome") or
                     (result.get("steps_failed") or [{}])[0].get("error", ""))
                or ""
            )
            capture_forensics(task_id=task_id, device_id=device_id,
                              error_text=str(_err_for_forensics))
        except Exception as _fe:
            logger.debug("forensics 触发异常 (已忽略): %s", _fe)
    # 连续失败追踪 → Telegram 告警
    if device_id:
        if success:
            _device_fail_streak.pop(device_id, None)
        else:
            streak = _device_fail_streak.get(device_id, 0) + 1
            _device_fail_streak[device_id] = streak
            if streak >= _FAIL_STREAK_THRESHOLD:
                _device_fail_streak[device_id] = 0  # reset after alert
                try:
                    from .alert_notifier import AlertNotifier
                    short_id = device_id[:10]
                    AlertNotifier.get().notify(
                        "error", device_id,
                        f"连续 {streak} 次任务失败 (设备: {short_id}) — {error[:80] if error else '未知错误'}"
                    )
                except Exception:
                    pass


def update_task_progress(task_id: str, progress: int, message: str = "") -> None:
    """Update real-time progress (0-100) for a running task."""
    result_json = json.dumps({"progress": progress, "progress_msg": message},
                             ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET result = ?, updated_at = ? "
            "WHERE task_id = ? AND status = 'running'",
            (result_json, _now_iso(), task_id),
        )
    if cur.rowcount:  # Only push if task was actually updated (still running)
        _push("task.progress", task_id=task_id, progress=progress, message=message)


def save_checkpoint(task_id: str, checkpoint: dict) -> None:
    """Save progress checkpoint for resumable tasks."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET checkpoint = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(checkpoint, ensure_ascii=False), _now_iso(), task_id),
        )


def get_checkpoint(task_id: str) -> Optional[dict]:
    """Load saved checkpoint from a (possibly failed) task."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT checkpoint FROM tasks WHERE task_id = ?", (task_id,),
        ).fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def set_task_cancelled(task_id: str) -> None:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'cancelled', updated_at = ? "
            "WHERE task_id = ? AND status IN ('pending', 'running')",
            (_now_iso(), task_id),
        )
    if cur.rowcount > 0:
        _push("task.cancelled", task_id=task_id)


def set_task_blocked(task_id: str, reason: str, step: str = "") -> None:
    """将任务标记为 blocked（预检失败，等待人工处理后重试）。"""
    result = {"blocked": True, "blocked_reason": reason, "blocked_step": step}
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = 'blocked', result = ?, updated_at = ? "
            "WHERE task_id = ? AND status IN ('pending', 'running')",
            (json.dumps(result, ensure_ascii=False), _now_iso(), task_id),
        )
    if cur.rowcount > 0:
        _push("task.blocked", task_id=task_id, reason=reason, step=step)


def unblock_task(task_id: str) -> None:
    """将 blocked 任务重置为 pending（人工确认修复后重试）。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'pending', result = NULL, updated_at = ? WHERE task_id = ?",
            (_now_iso(), task_id),
        )
    _push("task.unblocked", task_id=task_id)


def get_retry_ready_tasks(limit: int = 20) -> list:
    """获取已到重试时间、等待执行的任务列表。"""
    now = _now_iso()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT task_id, type, device_id, params, priority, max_retries, retry_count "
            f"FROM tasks WHERE status='pending' AND next_retry_at IS NOT NULL AND next_retry_at <= ? "
            f"AND {_alive_sql()} "
            "ORDER BY priority DESC, next_retry_at ASC LIMIT ?",
            (now, limit),
        ).fetchall()
    return [{"task_id": r[0], "type": r[1], "device_id": r[2],
             "params": json.loads(r[3] or "{}"), "priority": r[4],
             "max_retries": r[5], "retry_count": r[6]} for r in rows]


def is_current_task_cancelled() -> bool:
    """Check if the current thread's task has been cancelled."""
    try:
        from src.utils.log_config import _task_context
        tid = getattr(_task_context, "task_id", "")
        if not tid:
            return False
        from .worker_pool import get_worker_pool
        return get_worker_pool().is_cancelled(tid)
    except Exception:
        return False


_ERROR_CATEGORIES = [
    ("sla_timeout",      ["sla timeout"]),
    ("task_timeout",     ["超时", "timed out", "tasktime"]),
    ("gate_blocked",     ["gate_blocked", "gate 评估", "gate]", "[gate]"]),
    ("account_risk",     ["风控", "risk", "banned", "account block", "checkpoint", "登录受限"]),
    ("network",          ["network", "网络", "connection", "connect timeout", "errno", "socket"]),
    ("llm_unavailable",  ["llm", "circuit breaker", "llmunavailable", "ai 服务"]),
    ("ui_not_found",     ["element", "xpath", "找不到", "not found", "no element", "no such"]),
    ("device_error",     ["adb", "device offline", "设备断开", "screen capture", "uiautomator"]),
]


def _classify_error(error: str) -> str:
    """将 error 字符串归类到最匹配的错误类别。"""
    s = (error or "").lower()
    if not s:
        return "unknown"
    for category, keywords in _ERROR_CATEGORIES:
        if any(k in s for k in keywords):
            return category
    return "unknown"


def get_task_analysis(hours: int = 24) -> dict:
    """按任务类型/设备/错误类别分析最近 N 小时的执行结果。

    Returns:
        {
          "window_hours": int,
          "generated_at": str,
          "totals": {"total": N, "completed": N, "failed": N, "success_rate": float},
          "by_type": {task_type: {"total": N, "completed": N, "failed": N, "success_rate": float}},
          "by_device": {device_id: {"total": N, "completed": N, "failed": N, "success_rate": float}},
          "error_categories": {category: count},
          "top_errors": [{"error": str, "task_type": str, "category": str, "count": N}],
          "fail_history": {key: [{"ts": float, "err": str}]},  # 来自 smart_scheduler
        }
    """
    import time as _time
    hours = max(1, min(hours, 168))
    cutoff = _time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        _time.gmtime(_time.time() - hours * 3600)
    )

    by_type: dict = {}
    by_device: dict = {}
    error_categories: dict = {}
    top_errors_raw: dict = {}   # (error_text, task_type) → count

    with get_conn() as conn:
        # 1. 按 type×status 聚合
        rows = conn.execute(
            f"SELECT type, status, COUNT(*) as cnt FROM tasks "
            f"WHERE {_alive_sql()} AND updated_at >= ? GROUP BY type, status",
            (cutoff,),
        ).fetchall()
        for r in rows:
            t = r["type"]
            s = r["status"]
            cnt = r["cnt"]
            bucket = by_type.setdefault(t, {"total": 0, "completed": 0, "failed": 0})
            bucket["total"] += cnt
            if s == "completed":
                bucket["completed"] += cnt
            elif s == "failed":
                bucket["failed"] += cnt

        # 2. 按 device_id×status 聚合
        rows2 = conn.execute(
            f"SELECT device_id, status, COUNT(*) as cnt FROM tasks "
            f"WHERE {_alive_sql()} AND updated_at >= ? AND device_id IS NOT NULL AND device_id != '' "
            f"GROUP BY device_id, status",
            (cutoff,),
        ).fetchall()
        for r in rows2:
            d = r["device_id"]
            s = r["status"]
            cnt = r["cnt"]
            bucket = by_device.setdefault(d, {"total": 0, "completed": 0, "failed": 0})
            bucket["total"] += cnt
            if s == "completed":
                bucket["completed"] += cnt
            elif s == "failed":
                bucket["failed"] += cnt

        # 3. 失败任务 error 字段统计（json_extract + Python 分类）
        rows3 = conn.execute(
            f"SELECT type, json_extract(result, '$.error') as err FROM tasks "
            f"WHERE status='failed' AND {_alive_sql()} AND updated_at >= ? "
            f"AND result IS NOT NULL",
            (cutoff,),
        ).fetchall()
        for r in rows3:
            err = (r["err"] or "").strip()
            cat = _classify_error(err)
            error_categories[cat] = error_categories.get(cat, 0) + 1
            key = (err[:120], r["type"])
            top_errors_raw[key] = top_errors_raw.get(key, 0) + 1

    # 计算成功率
    for bucket in list(by_type.values()) + list(by_device.values()):
        t = bucket["total"]
        bucket["success_rate"] = round(bucket["completed"] / t, 4) if t else 0.0

    # 全局 totals
    total = completed = failed = 0
    for b in by_type.values():
        total += b["total"]
        completed += b["completed"]
        failed += b["failed"]
    totals = {
        "total": total,
        "completed": completed,
        "failed": failed,
        "success_rate": round(completed / total, 4) if total else 0.0,
    }

    # top errors 排序，取前 20
    top_errors = sorted(
        [{"error": k[0], "task_type": k[1], "category": _classify_error(k[0]), "count": v}
         for k, v in top_errors_raw.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:20]

    # 从 smart_scheduler 拉取近期失败快照（含详细 error 片段）
    fail_history: dict = {}
    try:
        from .smart_scheduler import get_smart_scheduler
        sched = get_smart_scheduler()
        with sched._lock:
            for k, dq in sched._fail_history.items():
                entries = list(dq)
                if entries:
                    fail_history[k] = [
                        {"ts": round(e["ts"], 1), "err": e.get("err", "")}
                        for e in entries[-5:]
                    ]
    except Exception:
        pass

    from datetime import datetime, timezone
    return {
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "by_type": dict(sorted(by_type.items(), key=lambda x: x[1]["failed"], reverse=True)),
        "by_device": dict(sorted(by_device.items(), key=lambda x: x[1]["failed"], reverse=True)),
        "error_categories": dict(sorted(error_categories.items(), key=lambda x: x[1], reverse=True)),
        "top_errors": top_errors,
        "fail_history": fail_history,
    }


def get_stats() -> dict:
    """任务统计（不含回收站）。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) as cnt FROM tasks WHERE {_alive_sql()} GROUP BY status"
        ).fetchall()
    stats = {r["status"]: r["cnt"] for r in rows}
    stats["total"] = sum(stats.values())
    return stats


def _row_to_dict(row: "sqlite3.Row") -> dict:
    d = dict(row)
    for key in ("params", "result"):
        val = d.get(key)
        if isinstance(val, str):
            try:
                d[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return d
