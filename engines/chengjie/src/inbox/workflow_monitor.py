"""P47 — 工作链执行可视化：记录富化与监控辅助。"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

_TYPE_LABELS = {
    "template": "话术模板",
    "task": "创建任务",
    "tag": "添加标签",
    "note": "内部备注",
    "escalate": "升级人工",
    "chain": "触发工作链",
}

_STATUS_LABELS = {
    "running": "运行中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "pending": "待执行",
}


def enrich_execution(row: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    """将 workflow_executions 行富化为前端可展示结构。"""
    ts = float(now if now is not None else time.time())
    try:
        steps = json.loads(row.get("steps_json") or "[]")
    except Exception:
        steps = []
    try:
        last_result = json.loads(row.get("last_result_json") or "{}")
    except Exception:
        last_result = {}
    try:
        context = json.loads(row.get("context_json") or "{}")
    except Exception:
        context = {}

    total = len(steps)
    cur = int(row.get("current_step") or 0)
    status = str(row.get("status") or "pending")
    next_at = float(row.get("next_step_at") or 0)

    cur_step = steps[cur] if 0 <= cur < total else None
    preview: List[Dict[str, Any]] = []
    for i, s in enumerate(steps[:8]):
        preview.append({
            "index": i,
            "done": i < cur or status in ("completed", "cancelled"),
            "active": i == cur and status == "running",
            "action_type": s.get("action_type", "template"),
            "action_label": _TYPE_LABELS.get(s.get("action_type", ""), s.get("action_type", "")),
            "note": str(s.get("note") or s.get("text") or "")[:80],
            "delay_hours": float(s.get("delay_hours") or 0),
        })

    countdown = max(0, int(next_at - ts)) if next_at > ts else 0
    return {
        "exec_id": row.get("exec_id"),
        "chain_id": row.get("chain_id"),
        "chain_name": row.get("chain_name") or row.get("chain_id") or "",
        "conversation_id": row.get("conversation_id"),
        "display_name": row.get("display_name") or "",
        "platform": row.get("platform") or "",
        "status": status,
        "status_label": _STATUS_LABELS.get(status, status),
        "current_step": cur,
        "current_step_display": min(cur + 1, total) if total else 0,
        "total_steps": total,
        "current_step_type": (cur_step or {}).get("action_type", ""),
        "current_step_label": _TYPE_LABELS.get(
            (cur_step or {}).get("action_type", ""), (cur_step or {}).get("action_type", ""),
        ),
        "current_step_note": str((cur_step or {}).get("note") or (cur_step or {}).get("text") or "")[:120],
        "steps_preview": preview,
        "last_result": last_result,
        "context": context,
        "started_at": float(row.get("started_at") or 0),
        "updated_at": float(row.get("updated_at") or 0),
        "next_step_at": next_at,
        "countdown_sec": countdown,
        "is_due": status == "running" and next_at > 0 and next_at <= ts,
        "progress_pct": round(cur / total * 100) if total else 0,
    }


def enrich_executions(rows: List[Dict[str, Any]], *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    return [enrich_execution(r, now=now) for r in rows]


# ── B2：链效果漏斗 ───────────────────────────────────────────────────────────

_FUNNEL_EXEC_CAP = 500  # 窗口内最多统计条数（监控页刷新预算，防大库拖垮）


def chain_funnel(
    store: Any,
    *,
    days: int = 14,
    reply_window_hours: int = 72,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """窗口内按链聚合执行漏斗 + 「启动后 N 小时内客户有入站」回复率。

    口径（诚实标注，勿夸大）：
    - 一切以 ``executions.started_at`` 落在窗口内为准，最多取 ``_FUNNEL_EXEC_CAP`` 条；
    - 回复＝启动后 ``reply_window_hours`` 内该会话有任意 ``direction='in'`` 消息，
      属**近似归因**（无法证明客户是因为链才回的）；
    - 回复率分母只算**已满窗口期**的执行（mature）——刚启动的链还没来得及被回复，
      混进分母会人为压低指标。
    异常整体软失败：返回空结构而不是把监控页打红。
    """
    ts_now = float(now if now is not None else time.time())
    window_sec = max(1, int(reply_window_hours)) * 3600
    since = ts_now - max(1, int(days)) * 86400

    try:
        rows = store._conn.execute(
            """SELECT e.exec_id, e.chain_id, e.conversation_id, e.status, e.started_at,
                      e.context_json, COALESCE(c.name, '') AS chain_name
               FROM workflow_executions e
               LEFT JOIN workflow_chains c ON c.chain_id = e.chain_id
               WHERE e.started_at >= ?
               ORDER BY e.started_at DESC LIMIT ?""",
            (since, _FUNNEL_EXEC_CAP),
        ).fetchall()
    except Exception:
        return {"ok": True, "days": days, "reply_window_hours": reply_window_hours,
                "total": {}, "chains": [], "goal_chain_starts": {}}

    def _bucket() -> Dict[str, Any]:
        return {"started": 0, "completed": 0, "failed": 0, "cancelled": 0,
                "running": 0, "mature_n": 0, "replied_n": 0, "attributed": 0}

    total = _bucket()
    # 归因组回复率拆分（总量级；per-chain 只出 attributed 计数防表过宽）：
    # 「挂在目标下启动的链 vs 散开的链，谁更能带来回话」——goal_id 随启动落
    # context_json（C1/C2 归因地基），此处消费。
    attr_mature = 0
    attr_replied = 0
    by_chain: Dict[str, Dict[str, Any]] = {}
    # J 推荐跟随原料：goal_id → {chain_id: 启动数}（纯 store 数据，不解析目标——
    # 模板归属/推荐比对属 goals 域，由路由层带护栏做，本函数保持零跨域）。
    goal_chain_starts: Dict[str, Dict[str, int]] = {}

    for r in rows:
        row = dict(r)
        cid = str(row.get("chain_id") or "")
        b = by_chain.setdefault(cid, {**_bucket(), "chain_id": cid,
                                      "chain_name": row.get("chain_name") or cid})
        status = str(row.get("status") or "pending")
        attributed = False
        gid = ""
        try:
            gid = str(
                (json.loads(row.get("context_json") or "{}") or {}).get("goal_id") or ""
            ).strip()
            attributed = bool(gid)
        except Exception:
            attributed, gid = False, ""
        if gid:
            gc = goal_chain_starts.setdefault(gid, {})
            gc[cid] = gc.get(cid, 0) + 1
        for bk in (total, b):
            bk["started"] += 1
            if attributed:
                bk["attributed"] += 1
            if status in ("completed", "failed", "cancelled", "running"):
                bk[status] += 1

        started_at = float(row.get("started_at") or 0)
        mature = started_at > 0 and (ts_now - started_at) >= window_sec
        replied = False
        if started_at > 0:
            try:
                hit = store._conn.execute(
                    """SELECT 1 FROM messages
                       WHERE conversation_id = ? AND direction = 'in'
                         AND ts > ? AND ts <= ? LIMIT 1""",
                    (row.get("conversation_id"), started_at, started_at + window_sec),
                ).fetchone()
                replied = hit is not None
            except Exception:
                replied = False
        if mature:
            for bk in (total, b):
                bk["mature_n"] += 1
                if replied:
                    bk["replied_n"] += 1
            if attributed:
                attr_mature += 1
                if replied:
                    attr_replied += 1

    def _rate(b: Dict[str, Any]) -> Optional[float]:
        return round(b["replied_n"] / b["mature_n"], 3) if b["mature_n"] else None

    total["reply_rate"] = _rate(total)
    total["attr_mature_n"] = attr_mature
    total["attr_replied_n"] = attr_replied
    total["attr_reply_rate"] = round(attr_replied / attr_mature, 3) if attr_mature else None
    chains = sorted(by_chain.values(), key=lambda x: -x["started"])
    for b in chains:
        b["reply_rate"] = _rate(b)

    # P1 2026-08-09：每环节执行聚合（workflow_step_log 窗口内 attempts/ok/failed
    # + 链定义里的动作类型与文案摘要）——「链走到哪一环节在损耗」从翻日志变成读数。
    # 步骤标签取链**当前**定义（定义改过则按 idx 尽力对齐，超界只出统计不出标签）；
    # store 无该表/方法（旧库/假 store）→ 空 map 软降级，绝不打红监控页。
    try:
        step_stats = store.workflow_step_stats(since)
    except Exception:
        step_stats = {}
    if step_stats:
        for b in chains:
            cid = str(b.get("chain_id") or "")
            per = step_stats.get(cid)
            if not per:
                continue
            try:
                ch = store.get_workflow_chain(cid) or {}
                steps_def = json.loads(ch.get("steps_json") or "[]")
            except Exception:
                steps_def = []
            by_step = []
            for idx in sorted(per, key=lambda x: int(x)):
                s = per[idx]
                i = int(idx)
                sd = steps_def[i] if 0 <= i < len(steps_def) else {}
                by_step.append({
                    "step_idx": i,
                    "action_type": str(sd.get("action_type") or ""),
                    "note": str(sd.get("note") or sd.get("text") or "")[:40],
                    "attempts": int(s.get("attempts") or 0),
                    "ok": int(s.get("ok") or 0),
                    "failed": int(s.get("failed") or 0),
                })
            if by_step:
                b["by_step"] = by_step

    return {
        "ok": True,
        "days": days,
        "reply_window_hours": reply_window_hours,
        "total": total,
        "chains": chains,
        # J：推荐跟随原料（路由层消费后从 API 响应中剔除，不对外暴露 goal_id 明细）
        "goal_chain_starts": goal_chain_starts,
    }
