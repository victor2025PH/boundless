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
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "pending": "待执行",
}

_ERROR_ZH = {
    "no_contact_id": "会话未关联联系人",
    "no_contacts_store": "任务步未接线",
    "task_store_error": "任务写入失败",
    "note_store_error": "备注写入失败",
    "tag_apply_error": "打标失败",
    "step_failed": "步骤失败",
    "chain_not_found": "链定义已删",
    "no_inbound": "会话无入站",
    "empty_draft": "拟稿为空",
    "deferred_quiet": "静默窗顺延",
    "deferred_mutex": "通道互斥顺延",
}


def human_wait(sec: int) -> str:
    """倒计时口语（#168 卡文案：9 小时 56 分后）。"""
    sec = max(0, int(sec))
    if sec < 60:
        return "不到 1 分钟"
    mins = sec // 60
    if mins < 60:
        return f"{mins} 分钟"
    hours, rem = divmod(mins, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {rem} 分" if rem else "")
    days, h = divmod(hours, 24)
    return f"{days} 天" + (f" {h} 小时" if h else "")


def _hhmm(ts: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "--:--"


def _fail_reason_zh(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return "未知原因"
    head = s.split(":", 1)[0].strip()
    zh = _ERROR_ZH.get(head)
    if zh and ":" in s and s != head:
        tail = s.split(":", 1)[1].strip()
        return f"{zh}（{tail}）" if tail and tail != head else zh
    return zh or s[:80]


def explain_execution_card(
    *,
    status: str,
    countdown_sec: int = 0,
    step_name: str = "",
    context: Optional[Dict[str, Any]] = None,
    last_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """链卡三态文案（#168）：运行中 / 已让路暂停 / 失败。纯函数。"""
    ctx = context if isinstance(context, dict) else {}
    last = last_result if isinstance(last_result, dict) else {}
    step = str(step_name or "").strip() or "下一步"
    st = str(status or "")
    yield_ts = 0.0
    try:
        yield_ts = float(ctx.get("reply_yield_ack_ts") or ctx.get("reply_yield_at") or 0)
    except (TypeError, ValueError):
        yield_ts = 0.0
    if st == "failed":
        raw = str(ctx.get("last_error") or last.get("last_error")
                  or last.get("error") or last.get("fail_reason") or "")
        return {"card_state": "failed",
                "card_label": f"失败（{_fail_reason_zh(raw)}）"}
    if st == "paused" and yield_ts > 0:
        hhmm = _hhmm(yield_ts)
        if countdown_sec > 0:
            mins = max(1, int(countdown_sec) // 60) if countdown_sec >= 60 else 1
            return {
                "card_state": "yielded",
                "card_label": f"已让路暂停（客户 {hhmm} 回话，静默 {mins} 分钟后续跑）",
            }
        return {
            "card_state": "yielded",
            "card_label": f"已让路暂停（客户 {hhmm} 回话，待坐席恢复后续跑）",
        }
    if st == "running":
        wait = human_wait(countdown_sec) if countdown_sec > 0 else "即将执行"
        suffix = f"{wait}后" if countdown_sec > 0 else wait
        return {"card_state": "running",
                "card_label": f"运行中 · 下一步 {suffix}（{step}）"}
    return {"card_state": st or "pending",
            "card_label": _STATUS_LABELS.get(st, st or "待执行")}


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
            "active": i == cur and status in ("running", "paused"),
            "action_type": s.get("action_type", "template"),
            "action_label": _TYPE_LABELS.get(s.get("action_type", ""), s.get("action_type", "")),
            "note": str(s.get("note") or s.get("text") or "")[:80],
            "delay_hours": float(s.get("delay_hours") or 0),
        })

    # P1 2026-08-12「采用并拟稿」数据源：最近一次**已发出**的话术建议。
    # current_step 指向「下一步」，坐席此刻该跟的是它前面最近的对外话术步
    # （template / 未知类型都会 publish 建议文案；note/tag/task 是内部动作不算）。
    actionable_note = ""
    actionable_idx = -1
    if status in ("running", "paused"):
        for i in range(min(cur, total) - 1, -1, -1):
            s = steps[i]
            a_type = str(s.get("action_type") or "template")
            note_txt = str(s.get("note") or s.get("text") or "").strip()
            if a_type not in ("note", "tag", "task") and note_txt:
                actionable_note = note_txt[:160]
                actionable_idx = i
                break

    countdown = max(0, int(next_at - ts)) if next_at > ts else 0
    step_name = (
        str((cur_step or {}).get("note") or (cur_step or {}).get("text") or "").strip()
        or _TYPE_LABELS.get(
            (cur_step or {}).get("action_type", ""),
            (cur_step or {}).get("action_type", ""))
        or "下一步"
    )
    card = explain_execution_card(
        status=status, countdown_sec=countdown, step_name=step_name[:40],
        context=context, last_result=last_result)
    return {
        "exec_id": row.get("exec_id"),
        "chain_id": row.get("chain_id"),
        "chain_name": row.get("chain_name") or row.get("chain_id") or "",
        "conversation_id": row.get("conversation_id"),
        "display_name": row.get("display_name") or "",
        "platform": row.get("platform") or "",
        "status": status,
        "status_label": _STATUS_LABELS.get(status, status),
        "card_state": card["card_state"],
        "card_label": card["card_label"],
        "current_step": cur,
        "current_step_display": min(cur + 1, total) if total else 0,
        "total_steps": total,
        "current_step_type": (cur_step or {}).get("action_type", ""),
        "current_step_label": _TYPE_LABELS.get(
            (cur_step or {}).get("action_type", ""), (cur_step or {}).get("action_type", ""),
        ),
        "current_step_note": str((cur_step or {}).get("note") or (cur_step or {}).get("text") or "")[:120],
        "actionable_note": actionable_note,
        "actionable_step_idx": actionable_idx,
        # P2：链执行档位（remind=提醒坐席 / auto=话术步自动拟稿投料）——前端徽标用
        "exec_mode": str(row.get("chain_exec_mode") or "remind"),
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
    deal_attr_days: int = 14,
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
                      e.context_json, COALESCE(c.name, '') AS chain_name,
                      COALESCE(c.exec_mode, 'remind') AS exec_mode
               FROM workflow_executions e
               LEFT JOIN workflow_chains c ON c.chain_id = e.chain_id
               WHERE e.started_at >= ?
               ORDER BY e.started_at DESC LIMIT ?""",
            (since, _FUNNEL_EXEC_CAP),
        ).fetchall()
    except Exception:
        return {"ok": True, "days": days, "reply_window_hours": reply_window_hours,
                "total": {}, "chains": [], "goal_chain_starts": {},
                "by_mode": {}}

    def _bucket() -> Dict[str, Any]:
        return {"started": 0, "completed": 0, "failed": 0, "cancelled": 0,
                "running": 0, "mature_n": 0, "replied_n": 0, "attributed": 0,
                "deals_n": 0, "deal_amount": 0.0}

    total = _bucket()
    # 归因组回复率拆分（总量级；per-chain 只出 attributed 计数防表过宽）：
    # 「挂在目标下启动的链 vs 散开的链，谁更能带来回话」——goal_id 随启动落
    # context_json（C1/C2 归因地基），此处消费。
    attr_mature = 0
    attr_replied = 0
    by_chain: Dict[str, Dict[str, Any]] = {}
    # P3 2026-08-13：执行档位分组（remind=提醒坐席 / auto=自动拟稿投料）——
    # 灰度开闸后「自动推进到底有没有人推得好」的直接对比读数。口径诚实标注：
    # 档位取链**当前**定义（中途切档的历史执行按现值归组）；不同链客群不同，
    # 对比是方向性参考不是 A/B 实验。
    by_mode: Dict[str, Dict[str, Any]] = {}
    # J 推荐跟随原料：goal_id → {chain_id: 启动数}（纯 store 数据，不解析目标——
    # 模板归属/推荐比对属 goals 域，由路由层带护栏做，本函数保持零跨域）。
    goal_chain_starts: Dict[str, Dict[str, int]] = {}

    for r in rows:
        row = dict(r)
        cid = str(row.get("chain_id") or "")
        mode = str(row.get("exec_mode") or "remind")
        b = by_chain.setdefault(cid, {**_bucket(), "chain_id": cid,
                                      "chain_name": row.get("chain_name") or cid,
                                      "exec_mode": mode})
        m = by_mode.setdefault(mode, _bucket())
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
        for bk in (total, b, m):
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
            for bk in (total, b, m):
                bk["mature_n"] += 1
                if replied:
                    bk["replied_n"] += 1
            if attributed:
                attr_mature += 1
                if replied:
                    attr_replied += 1

    # ── 实施92 P0-3：链→成交归因（近似口径，诚实标注）────────────────────
    # 每笔未撤销成交归到「它之前最近启动、且启动在 deal_attr_days 窗内」的那
    # 条执行（同会话多链并行时只归最近一条，营收不双计）；与回复率同属近似
    # 归因——无法证明客户是因为链才成交的。store 无 deal_events 表（旧库/
    # 假 store）→ 全零软降级。
    try:
        deal_rows = store.deal_events_between(since)
    except Exception:
        deal_rows = []
    if deal_rows:
        attr_win = max(1, int(deal_attr_days)) * 86400
        execs_by_conv: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            row = dict(r)
            conv = str(row.get("conversation_id") or "")
            if conv:
                execs_by_conv.setdefault(conv, []).append(row)
        for ev in deal_rows:
            conv = str(ev.get("conversation_id") or "")
            ev_ts = float(ev.get("ts") or 0)
            best = None
            for ex_row in execs_by_conv.get(conv, []):
                st = float(ex_row.get("started_at") or 0)
                if st < ev_ts <= st + attr_win:
                    if best is None or st > float(best.get("started_at") or 0):
                        best = ex_row
            if best is None:
                continue
            amount = float(ev.get("amount") or 0)
            bcid = str(best.get("chain_id") or "")
            for bk in (total, by_chain.get(bcid)):
                if bk is None:
                    continue
                bk["deals_n"] += 1
                bk["deal_amount"] = round(
                    float(bk.get("deal_amount") or 0) + amount, 2)

    def _rate(b: Dict[str, Any]) -> Optional[float]:
        return round(b["replied_n"] / b["mature_n"], 3) if b["mature_n"] else None

    total["reply_rate"] = _rate(total)
    total["attr_mature_n"] = attr_mature
    total["attr_replied_n"] = attr_replied
    total["attr_reply_rate"] = round(attr_replied / attr_mature, 3) if attr_mature else None
    chains = sorted(by_chain.values(), key=lambda x: -x["started"])
    for b in chains:
        b["reply_rate"] = _rate(b)
    for mb in by_mode.values():
        mb["reply_rate"] = _rate(mb)

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
        # P3：档位分组（remind/auto 各一桶，含 reply_rate）——零 auto 流量时只有 remind 桶
        "by_mode": by_mode,
        # J：推荐跟随原料（路由层消费后从 API 响应中剔除，不对外暴露 goal_id 明细）
        "goal_chain_starts": goal_chain_starts,
    }
