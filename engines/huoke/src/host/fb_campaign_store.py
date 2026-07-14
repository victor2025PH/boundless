# -*- coding: utf-8 -*-
"""Facebook campaign_run 运行状态存储（2026-04-21 P1-3a）。

**场景**: `_run_facebook_campaign` 跑到 `add_friends` 挂了，当前实现
会整单任务失败、重跑时所有 warmup / group_engage / extract_members 白白重做。

**方案**:
  * 每个 facebook_campaign_run 任务启动时 upsert 一条 `fb_campaign_runs` 记录
  * 每步完成后写入 `state_json.steps_completed[]`
  * 再次提交时带 `params.resume_from_run_id`，executor 从 store 读 state，
    已完成的步骤 skip，未完成的继续
  * 自动用 task_id 作为 run_id（无需前端显式传，失败任务重试时走 /tasks/retry 即可）
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from .database import _connect

logger = logging.getLogger(__name__)


def start_run(run_id: str, device_id: str, *,
              task_id: str = "", preset_key: str = "",
              total_steps: int = 0,
              existing_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """开始一次 campaign。若 run_id 已存在则复用（resume 语义）。

    返回当前 state_json（dict）。若是全新 run 则 state_json={}。
    """
    if not run_id or not device_id:
        return {}
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM fb_campaign_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row:
                # Resume
                conn.execute(
                    "UPDATE fb_campaign_runs SET state='running', "
                    "updated_at=datetime('now'), task_id=COALESCE(NULLIF(?,''), task_id)"
                    " WHERE run_id=?",
                    (task_id, run_id),
                )
                try:
                    sj = json.loads(row["state_json"] or "{}")
                except Exception:
                    sj = {}
                return sj
            sj = existing_state or {}
            conn.execute(
                "INSERT INTO fb_campaign_runs "
                "(run_id, task_id, device_id, preset_key, total_steps, state, state_json)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, task_id, device_id, preset_key, int(total_steps),
                 "running", json.dumps(sj, ensure_ascii=False)),
            )
            return dict(sj)
    except Exception as e:
        logger.warning("[fb_campaign] start_run 失败 run=%s: %s", run_id, e)
        return {}


def update_step(run_id: str, step_idx: int, step_name: str,
                state_json: Dict[str, Any]):
    """每步结束后调用：更新当前步指针 + state_json 快照。"""
    if not run_id:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE fb_campaign_runs SET "
                "current_step_idx=?, current_step_name=?, "
                "state_json=?, updated_at=datetime('now') "
                "WHERE run_id=?",
                (int(step_idx), step_name,
                 json.dumps(state_json, ensure_ascii=False, default=str),
                 run_id),
            )
    except Exception as e:
        logger.warning("[fb_campaign] update_step 失败: %s", e)


def finish_run(run_id: str, state: str, state_json: Dict[str, Any]):
    """state ∈ completed/failed/cancelled/partial。"""
    if not run_id:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE fb_campaign_runs SET state=?, state_json=?, "
                "finished_at=datetime('now'), updated_at=datetime('now') "
                "WHERE run_id=?",
                (state, json.dumps(state_json, ensure_ascii=False, default=str), run_id),
            )
    except Exception as e:
        logger.warning("[fb_campaign] finish_run 失败: %s", e)


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    if not run_id:
        return None
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM fb_campaign_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["state_json"] = json.loads(d.get("state_json") or "{}")
            except Exception:
                d["state_json"] = {}
            return d
    except Exception:
        return None


def list_runs(device_id: Optional[str] = None, state: Optional[str] = None,
              limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM fb_campaign_runs WHERE 1=1"
    params: list = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if state:
        sql += " AND state=?"
        params.append(state)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["state_json"] = json.loads(d.get("state_json") or "{}")
            except Exception:
                d["state_json"] = {}
            out.append(d)
        return out
    except Exception:
        return []
