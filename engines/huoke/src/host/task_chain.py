# -*- coding: utf-8 -*-
"""通用任务链编排器 — 2026-05-08 P2-2。

设计思路:
  不在 executor 里串行跑所有步骤(campaign_run 模式的缺点: 单步卡住 = 整个链超时)，
  而是利用 EventBus 的 task.completed / task.failed 事件驱动:
    1. create_chain() 在 DB 建一条 chain 记录 + 第一步 task
    2. task 完成时 _on_task_done() 被 EventBus 触发
    3. 如果该 task 属于某条 chain → 自动创建下一步 task
    4. 全部步骤完成 → 标记 chain 完成，推送汇总

优点:
  - 每步独立超时控制(用 _TASK_TYPE_TIMEOUTS)
  - 每步有独立 screenshot / forensics
  - 步骤之间天然有 2-5s 间隔(调度器 dispatch)，模拟真人节奏
  - chain YAML 可热加载，不需要改代码
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.host.device_registry import config_dir

logger = logging.getLogger(__name__)

_CFG_PATH = config_dir() / "task_chains.yaml"
_chains_cache: Optional[Dict[str, Any]] = None
_chains_cache_mtime: float = 0


def _load_chains() -> Dict[str, Any]:
    """热加载 task_chains.yaml，文件变更时自动刷新。"""
    global _chains_cache, _chains_cache_mtime
    try:
        mtime = _CFG_PATH.stat().st_mtime
    except OSError:
        return _chains_cache or {}
    if _chains_cache is not None and mtime == _chains_cache_mtime:
        return _chains_cache
    try:
        with open(_CFG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _chains_cache = data.get("chains") or {}
        _chains_cache_mtime = mtime
        logger.info("[chain] 加载 %d 条任务链配置", len(_chains_cache))
    except Exception as e:
        logger.warning("[chain] 加载 task_chains.yaml 失败: %s", e)
        _chains_cache = _chains_cache or {}
    return _chains_cache


def list_chains() -> List[Dict[str, Any]]:
    """列出所有可用的任务链（供前端下拉选择）。"""
    chains = _load_chains()
    out = []
    for chain_id, cfg in chains.items():
        out.append({
            "chain_id": chain_id,
            "name": cfg.get("name", chain_id),
            "description": cfg.get("description", ""),
            "platform": cfg.get("platform", ""),
            "steps": cfg.get("steps") or [],
        })
    return out


def get_chain_detail(chain_id: str) -> Optional[Dict[str, Any]]:
    """获取单条链完整配置。"""
    chains = _load_chains()
    cfg = chains.get(chain_id)
    if not cfg:
        return None
    return {"chain_id": chain_id, **cfg}


def save_chain(chain_id: str, cfg: Dict[str, Any]):
    """新增或更新一条链定义，写入 YAML 文件。"""
    global _chains_cache, _chains_cache_mtime
    if not chain_id or not chain_id.strip():
        raise ValueError("chain_id 不能为空")
    steps = cfg.get("steps") or []
    if not steps:
        raise ValueError("至少需要一个步骤")
    for i, s in enumerate(steps):
        if not s.get("task_type"):
            raise ValueError(f"步骤 {i+1} 缺少 task_type")

    # 读取现有文件
    data: Dict[str, Any] = {}
    if _CFG_PATH.exists():
        try:
            with open(_CFG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            pass
    chains = data.get("chains") or {}
    chains[chain_id] = {
        "name": cfg.get("name", chain_id),
        "description": cfg.get("description", ""),
        "platform": cfg.get("platform", ""),
        "steps": steps,
    }
    data["chains"] = chains
    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CFG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    # 刷新缓存
    _chains_cache = chains
    _chains_cache_mtime = _CFG_PATH.stat().st_mtime
    logger.info("[chain] 已保存链 %s (%d 步)", chain_id, len(steps))


def export_chains_yaml() -> str:
    """P6-4: 导出全部链模板为 YAML 字符串。"""
    chains = _load_chains()
    data = {"chains": dict(chains)}
    return yaml.dump(data, allow_unicode=True, default_flow_style=False,
                     sort_keys=False)


def import_chains_yaml(yaml_text: str, overwrite: bool = False) -> dict:
    """P6-4: 从 YAML 文本导入链模板。

    overwrite=False 时仅导入新链(跳过已有的)；
    overwrite=True 时覆盖同名链。
    返回 {imported: int, skipped: int, errors: [str]}
    """
    try:
        data = yaml.safe_load(yaml_text) or {}
    except Exception as e:
        return {"imported": 0, "skipped": 0, "errors": [f"YAML 解析失败: {e}"]}

    incoming = data.get("chains") or {}
    if not incoming:
        return {"imported": 0, "skipped": 0, "errors": ["未找到 chains 节点"]}

    existing = _load_chains()
    imported = 0
    skipped = 0
    errors = []

    for chain_id, cfg in incoming.items():
        if not overwrite and chain_id in existing:
            skipped += 1
            continue
        try:
            save_chain(chain_id, cfg)
            imported += 1
        except Exception as e:
            errors.append(f"{chain_id}: {e}")

    return {"imported": imported, "skipped": skipped, "errors": errors}


def delete_chain(chain_id: str) -> bool:
    """删除一条链定义。"""
    global _chains_cache, _chains_cache_mtime
    data: Dict[str, Any] = {}
    if _CFG_PATH.exists():
        try:
            with open(_CFG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            pass
    chains = data.get("chains") or {}
    if chain_id not in chains:
        return False
    del chains[chain_id]
    data["chains"] = chains
    with open(_CFG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    _chains_cache = chains
    _chains_cache_mtime = _CFG_PATH.stat().st_mtime
    logger.info("[chain] 已删除链 %s", chain_id)
    return True


def create_chain(chain_id: str, device_id: str,
                 params_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """为一台设备启动一条任务链。返回 chain run 信息。"""
    chains = _load_chains()
    cfg = chains.get(chain_id)
    if not cfg:
        raise ValueError(f"未知任务链: {chain_id}")

    steps = cfg.get("steps") or []
    if not steps:
        raise ValueError(f"任务链 {chain_id} 没有定义任何步骤")

    run_id = str(uuid.uuid4())
    chain_run = {
        "run_id": run_id,
        "chain_id": chain_id,
        "device_id": device_id,
        "steps": steps,
        "current_step": 0,
        "params_override": params_override or {},
        "results": [],
        "status": "running",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # 存入 DB
    _save_chain_run(chain_run)

    # 创建第一步任务
    _dispatch_step(chain_run, step_index=0)

    return {
        "run_id": run_id,
        "chain_id": chain_id,
        "device_id": device_id,
        "total_steps": len(steps),
        "first_task_type": steps[0].get("task_type", ""),
    }


def create_chain_batch(chain_id: str, device_ids: List[str],
                       params_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """为多台设备启动同一条任务链。"""
    results = []
    for did in device_ids:
        try:
            r = create_chain(chain_id, did, params_override)
            results.append({"device_id": did, "ok": True, **r})
        except Exception as e:
            results.append({"device_id": did, "ok": False, "error": str(e)})
    return {
        "chain_id": chain_id,
        "total": len(device_ids),
        "created": sum(1 for r in results if r.get("ok")),
        "results": results,
    }


def _get_parallel_group(steps: list, start_index: int) -> List[int]:
    """P5-2: 从 start_index 开始，找出连续 parallel=true 的步骤组。

    如果当前步骤是 parallel，则收集连续的 parallel 步骤；
    否则只返回单步 [start_index]。
    """
    if start_index >= len(steps):
        return []
    first = steps[start_index]
    if not (isinstance(first, dict) and first.get("parallel")):
        return [start_index]
    group = [start_index]
    for i in range(start_index + 1, len(steps)):
        s = steps[i]
        if isinstance(s, dict) and s.get("parallel"):
            group.append(i)
        else:
            break
    return group


def _dispatch_step(chain_run: dict, step_index: int):
    """创建链中第 step_index 步的任务（支持并行步骤组）。"""
    from .task_store import create_task

    steps = chain_run["steps"]
    if step_index >= len(steps):
        return

    # P5-2: 检测并行组
    group = _get_parallel_group(steps, step_index)

    for idx in group:
        step = steps[idx]
        task_type = step.get("task_type", "")
        # 合并参数: step 默认 → chain override
        merged_params = dict(step.get("params") or {})
        override = chain_run.get("params_override") or {}
        for k, v in override.items():
            if v is not None and v != "":
                merged_params[k] = v

        # 2026-05-10 Fix G: 为 FB 群相关任务注入 target_groups（远程 Worker
        # 没有 persona seed 兜底逻辑，需在协调器侧提前填充）
        if (task_type in ("facebook_group_member_greet", "facebook_campaign_run")
                and not merged_params.get("target_groups")):
            try:
                from .fb_target_personas import get_persona_display, get_default_persona_key
                _pk = merged_params.get("persona_key") or get_default_persona_key()
                _pd = get_persona_display(_pk) or {}
                _seeds = _pd.get("seed_group_keywords") or []
                if _seeds:
                    merged_params["target_groups"] = list(_seeds)[:3]
            except Exception:
                pass

        # 标记任务归属哪条链
        merged_params["_chain_run_id"] = chain_run["run_id"]
        merged_params["_chain_step_index"] = idx
        # P5-2: 标记并行组信息
        if len(group) > 1:
            merged_params["_chain_parallel_group"] = group

        task_id = create_task(
            task_type=task_type,
            device_id=chain_run["device_id"],
            params=merged_params,
            batch_id=f"chain:{chain_run['run_id']}",
            priority=55,
        )

        logger.info("[chain] %s step %d/%d → task=%s type=%s device=%s%s",
                    chain_run["run_id"][:8], idx + 1, len(steps),
                    task_id[:8], task_type, chain_run["device_id"][:8],
                    f" [parallel {len(group)}]" if len(group) > 1 else "")

        # 2026-05-10: 立刻派发，不等 rescue loop（2+ min 延迟 + 设备可能暂时不可达）
        try:
            from .task_dispatcher import dispatch_after_create
            dispatch_after_create(
                task_id=task_id,
                device_id=chain_run["device_id"],
                task_type=task_type,
                params=merged_params,
                priority=55,
            )
        except Exception as _disp_e:
            logger.debug("[chain] dispatch_after_create failed (rescue loop 兜底): %s", _disp_e)


def on_task_done(task_id: str, task_type: str, success: bool,
                 error: str = "", device_id: str = "",
                 params: Optional[dict] = None):
    """EventBus 回调 — 任务完成时检查是否是链中的一步，推进下一步。

    由 set_task_result → EventBus task.completed/task.failed 触发。
    """
    if not params:
        return
    run_id = params.get("_chain_run_id")
    if not run_id:
        return

    step_index = int(params.get("_chain_step_index", -1))
    if step_index < 0:
        return

    chain_run = _load_chain_run(run_id)
    if not chain_run:
        logger.warning("[chain] run_id=%s 未找到，忽略", run_id[:8])
        return

    steps = chain_run.get("steps") or []
    current_step = steps[step_index] if step_index < len(steps) else {}
    on_fail = current_step.get("on_fail", "skip")

    # 记录步骤结果
    chain_run.setdefault("results", []).append({
        "step": step_index,
        "task_type": task_type,
        "task_id": task_id,
        "success": success,
        "error": error,
    })

    # P5-2: 并行组处理 — 等待组内所有任务完成才推进
    parallel_group = params.get("_chain_parallel_group")
    if parallel_group and len(parallel_group) > 1:
        # 检查组内所有步骤是否都有结果
        results = chain_run.get("results") or []
        completed_indices = {r["step"] for r in results}
        pending = [i for i in parallel_group if i not in completed_indices]
        if pending:
            # 组内还有未完成的 — 保存当前结果，等待其余
            _save_chain_run(chain_run)
            logger.debug("[chain] %s parallel group 等待: %d/%d 完成",
                         run_id[:8], len(parallel_group) - len(pending),
                         len(parallel_group))
            return
        # 组全部完成 — 检查是否有 abort
        group_results = [r for r in results if r["step"] in parallel_group]
        group_failed = [r for r in group_results if not r["success"]]
        abort_steps = [i for i in parallel_group
                       if steps[i].get("on_fail") == "abort"]
        aborted = any(r["step"] in abort_steps for r in group_failed)
        if aborted:
            logger.warning("[chain] %s parallel group has abort failure",
                           run_id[:8])
            chain_run["status"] = "aborted"
            chain_run["finished_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_chain_run(chain_run)
            _push_chain_summary(chain_run)
            return
        # 推进到组后第一个非并行步骤
        next_index = max(parallel_group) + 1
    else:
        next_index = step_index + 1

        if not success and on_fail == "abort":
            # 2026-05-10: cold_start / playbook 策略阻断不是真正的执行错误，
            # 不应 abort 整条链。远程 Worker 可能未部署 soft-skip，此处兜底。
            _err = error or ""
            _soft = ("cold_start" in _err or
                     "playbook" in _err.lower() or
                     "无可用设备" in _err or
                     "quota exceeded" in _err.lower() or
                     "extract_zero" in _err or
                     "未能切回前台" in _err)
            if _soft:
                logger.info("[chain] %s step %d soft-fail (不 abort): %s",
                            run_id[:8], step_index, (error or "")[:80])
                chain_run.setdefault("soft_failures", []).append({
                    "step": step_index, "error": error})
                _save_chain_run(chain_run)
                # 继续推进下一步（不 return）
            else:
                logger.warning("[chain] %s step %d failed (on_fail=abort), 链终止: %s",
                               run_id[:8], step_index, error)
                chain_run["status"] = "aborted"
                chain_run["finished_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _save_chain_run(chain_run)
                _push_chain_summary(chain_run)
                return

    if next_index >= len(steps):
        # 全部完成
        chain_run["status"] = "completed"
        chain_run["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_chain_run(chain_run)
        _push_chain_summary(chain_run)
        logger.info("[chain] %s 全部 %d 步完成 (device=%s)",
                    run_id[:8], len(steps), chain_run.get("device_id", "")[:8])
        return

    # 推进下一步
    chain_run["current_step"] = next_index
    _save_chain_run(chain_run)
    _dispatch_step(chain_run, next_index)


def _push_chain_summary(chain_run: dict):
    """推送链完成事件到 EventBus + P4-4 聚合告警通知。"""
    results = chain_run.get("results") or []
    ok = sum(1 for r in results if r.get("success"))
    total_steps = len(chain_run.get("steps") or [])
    fail_results = [r for r in results if not r.get("success")]
    device_id = chain_run.get("device_id", "")
    status = chain_run.get("status", "")
    chain_id = chain_run.get("chain_id", "")

    # 1) WS 推送（前端即时刷新）
    try:
        from src.host.event_stream import EventStreamHub
        hub = EventStreamHub.get()

        # P4-4: 聚合失败错误分类
        error_summary = {}
        for r in fail_results:
            err = r.get("error", "")[:80] or "unknown"
            error_summary[err] = error_summary.get(err, 0) + 1

        hub.push_event("chain.completed", {
            "run_id": chain_run.get("run_id"),
            "chain_id": chain_id,
            "device_id": device_id,
            "status": status,
            "steps_ok": ok,
            "steps_total": total_steps,
            "failed_steps": len(fail_results),
            "error_summary": error_summary,
        }, device_id=device_id)
    except Exception as e:
        logger.debug("[chain] 推送完成事件失败: %s", e)

    # 2) P4-4: 链级别外部告警 — 失败率 ≥50% 或 abort 时通知
    fail_count = len(fail_results)
    if fail_count > 0 and (status == "aborted" or fail_count >= total_steps * 0.5):
        try:
            from .alert_notifier import AlertNotifier
            notifier = AlertNotifier.get()
            if notifier:
                err_text = ", ".join(
                    f"{e}(x{n})" for e, n in (error_summary if error_summary
                                               else {}).items()
                )[:200]
                notifier.notify(
                    level="warning",
                    device_id=device_id,
                    message=(
                        f"链 {chain_id} {'中止' if status == 'aborted' else '完成'}: "
                        f"成功 {ok}/{total_steps}, 失败 {fail_count}。"
                        f"{' 错误: ' + err_text if err_text else ''}"
                    ),
                    alert_code="CHAIN_COMPLETION",
                    params={},
                )
        except Exception:
            pass


# ── 持久化 (SQLite) ──────────────────────────────────────────────────

def _ensure_table():
    """确保 chain_runs 表存在。"""
    try:
        from .database import get_conn
        with get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chain_runs (
                    run_id TEXT PRIMARY KEY,
                    chain_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    data TEXT,
                    created_at TEXT,
                    finished_at TEXT
                )
            """)
    except Exception as e:
        logger.debug("[chain] 创建 chain_runs 表失败: %s", e)


_table_ensured = False


def _save_chain_run(chain_run: dict):
    global _table_ensured
    if not _table_ensured:
        _ensure_table()
        _table_ensured = True

    try:
        from .database import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chain_runs "
                "(run_id, chain_id, device_id, status, data, created_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chain_run["run_id"], chain_run["chain_id"],
                 chain_run["device_id"], chain_run["status"],
                 json.dumps(chain_run, ensure_ascii=False),
                 chain_run.get("created_at", ""),
                 chain_run.get("finished_at", "")),
            )
    except Exception as e:
        logger.warning("[chain] 保存 chain_run 失败: %s", e)


def _load_chain_run(run_id: str) -> Optional[dict]:
    global _table_ensured
    if not _table_ensured:
        _ensure_table()
        _table_ensured = True

    try:
        from .database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT data FROM chain_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row and row["data"]:
            return json.loads(row["data"])
    except Exception as e:
        logger.debug("[chain] 读取 chain_run %s 失败: %s", run_id[:8], e)
    return None


def get_chain_run_detail(run_id: str) -> Optional[dict]:
    """P6-1: 获取链运行详情，包含每步的时间线信息。"""
    chain_run = _load_chain_run(run_id)
    if not chain_run:
        return None

    # 用 task_id 查询每步的 created_at / updated_at 用于计算耗时
    results = chain_run.get("results") or []
    task_ids = [r.get("task_id") for r in results if r.get("task_id")]
    if task_ids:
        try:
            from .database import get_conn
            placeholders = ",".join("?" * len(task_ids))
            with get_conn() as conn:
                rows = conn.execute(
                    f"SELECT task_id, created_at, updated_at, status "
                    f"FROM tasks WHERE task_id IN ({placeholders})",
                    task_ids,
                ).fetchall()
            timing_map = {r["task_id"]: dict(r) for r in rows}
            for r in results:
                tid = r.get("task_id")
                if tid and tid in timing_map:
                    t = timing_map[tid]
                    r["started_at"] = t.get("created_at", "")
                    r["finished_at"] = t.get("updated_at", "")
                    # 计算耗时
                    try:
                        from datetime import datetime
                        fmt = "%Y-%m-%dT%H:%M:%SZ"
                        s = datetime.strptime(t["created_at"], fmt)
                        e = datetime.strptime(t["updated_at"], fmt)
                        r["duration_sec"] = round((e - s).total_seconds(), 1)
                    except Exception:
                        r["duration_sec"] = 0
        except Exception:
            pass

    chain_run["results"] = results
    return chain_run


def rerun_failed_steps(run_id: str) -> dict:
    """P6-1: 重新执行链中失败的步骤。创建新的 chain run。"""
    chain_run = _load_chain_run(run_id)
    if not chain_run:
        raise ValueError(f"链运行 {run_id} 不存在")

    results = chain_run.get("results") or []
    failed_indices = [r["step"] for r in results if not r.get("success")]
    if not failed_indices:
        return {"rerun": False, "reason": "无失败步骤"}

    steps = chain_run.get("steps") or []
    device_id = chain_run.get("device_id", "")
    chain_id = chain_run.get("chain_id", "")

    # 创建新链 run，仅包含失败步骤
    import uuid
    new_run_id = str(uuid.uuid4())
    failed_steps = [steps[i] for i in failed_indices if i < len(steps)]

    new_run = {
        "run_id": new_run_id,
        "chain_id": chain_id,
        "device_id": device_id,
        "status": "running",
        "steps": failed_steps,
        "current_step": 0,
        "params_override": chain_run.get("params_override"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parent_run_id": run_id,
        "results": [],
    }
    _save_chain_run(new_run)
    _dispatch_step(new_run, 0)

    logger.info("[chain] rerun %d 个失败步骤 from %s → new run %s",
                len(failed_steps), run_id[:8], new_run_id[:8])
    return {
        "rerun": True,
        "new_run_id": new_run_id,
        "failed_steps": len(failed_steps),
        "device_id": device_id,
    }


def get_chain_runs(device_id: str = "", limit: int = 20) -> List[dict]:
    """查询最近的链执行记录。"""
    global _table_ensured
    if not _table_ensured:
        _ensure_table()
        _table_ensured = True

    try:
        from .database import get_conn
        with get_conn() as conn:
            if device_id:
                rows = conn.execute(
                    "SELECT data FROM chain_runs WHERE device_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (device_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data FROM chain_runs "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [json.loads(r["data"]) for r in rows if r["data"]]
    except Exception as e:
        logger.debug("[chain] 查询 chain_runs 失败: %s", e)
        return []


def setup_chain_listener():
    """注册 EventBus 监听，在任务完成时自动推进链。

    复用 risk_auto_heal.py 的 inproc_listener + push_event monkey-patch 机制，
    在 task.completed / task.failed 事件触发时检查是否属于某条链。
    应在 server 启动时调用一次。
    """
    try:
        from src.host.risk_auto_heal import register_inproc_listener, patch_event_stream
        patch_event_stream()  # 幂等，确保 push_event 已打补丁

        def _on_task_event(payload: dict):
            data = payload.get("data") or {}
            task_id = data.get("task_id", "")
            if not task_id:
                return
            try:
                from .task_store import get_task
                task = get_task(task_id)
                if not task:
                    return
                params = task.get("params")
                if isinstance(params, str):
                    params = json.loads(params)
                if not params or "_chain_run_id" not in params:
                    return
                result = task.get("result") or {}
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except Exception:
                        result = {}
                on_task_done(
                    task_id=task_id,
                    task_type=task.get("type", ""),
                    success=(task.get("status") == "completed"),
                    error=result.get("error", ""),
                    device_id=task.get("device_id", ""),
                    params=params,
                )
            except Exception as e:
                logger.debug("[chain] on_task_event 处理失败: %s", e)

        register_inproc_listener("task.completed", _on_task_event)
        register_inproc_listener("task.failed", _on_task_event)
        logger.info("[chain] EventBus inproc 监听已注册 (task.completed/failed → chain advance)")
    except Exception as e:
        logger.warning("[chain] setup_chain_listener 失败: %s", e)
