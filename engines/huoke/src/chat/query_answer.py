# -*- coding: utf-8 -*-
"""
只读查询 — 将分流到 QUERY 的请求转为自然语言回复，数据来自 OpenClaw HTTP API（与 IntentExecutor 同源）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from src.chat.intent_executor import IntentExecutor


def _fmt_device_list(data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        return f"读取设备列表失败：{data.get('error')}"
    rows = data if isinstance(data, list) else []
    if not rows:
        return "当前没有登记设备或列表为空。"
    lines = []
    online = 0
    for d in rows:
        if not isinstance(d, dict):
            continue
        did = d.get("device_id", d.get("id", "?"))
        st = (d.get("status") or "").lower()
        if st in ("connected", "online"):
            online += 1
        name = d.get("display_name") or d.get("name") or ""
        busy = "忙" if d.get("busy") else "闲"
        lines.append(f"  • {did[:16]}{'…' if len(str(did)) > 16 else ''}  {name}  状态:{st or 'unknown'}  {busy}")
    head = f"共 {len(lines)} 台设备，其中约 {online} 台显示为在线/已连接。\n"
    return head + "\n".join(lines[:30]) + ("\n…（仅显示前30条）" if len(lines) > 30 else "")


def _fmt_funnel(data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        return f"读取漏斗数据失败：{data.get('error')}"
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)[:4000]
    except Exception:
        return str(data)[:2000]


def _fmt_daily(data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        return f"读取日报失败：{data.get('error')}"
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)[:4000]
    except Exception:
        return str(data)[:2000]


def _fmt_error_analysis(data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        return f"读取任务错误分析失败：{data.get('error')}"
    if not isinstance(data, dict):
        return str(data)[:2000]
    parts = [
        f"统计区间：最近约 {data.get('period_hours', '?')} 小时",
        f"任务总数：{data.get('total_tasks', 0)}，失败：{data.get('total_failed', 0)}，失败率：{data.get('failure_rate', 0)}%",
        f"主要错误类型：{data.get('top_category', 'unknown')}",
    ]
    cats = data.get("categories") or {}
    if cats:
        top_c = sorted(cats.items(), key=lambda x: -x[1])[:6]
        parts.append("错误分类：" + "，".join(f"{k}:{v}" for k, v in top_c if v))
    alerts = data.get("alerts") or []
    if alerts:
        parts.append("告警：" + "；".join(a.get("message", "") for a in alerts[:3]))
    sug = data.get("suggestions") or []
    if sug:
        parts.append("建议：" + "；".join(s.get("action", "") for s in sug[:3]))
    samples = data.get("samples") or {}
    if isinstance(samples, dict) and samples:
        parts.append("错误样本（节选）：")
        for cat, errs in list(samples.items())[:3]:
            for e in (errs or [])[:1]:
                parts.append(f"  [{cat}] {e[:160]}")
    return "\n".join(parts)


def run_query(
    subtype: str,
    executor: IntentExecutor,
    user_message: str = "",
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    返回 (reply_text, logical_intent, synthetic_actions_for_ui)
    """
    sub = (subtype or "general").strip() or "general"
    ex = executor
    actions: List[Dict[str, Any]] = []

    if sub == "device_list":
        ar = ex.execute("device_list", [], {})
        actions = ar
        data = ar[0].get("data") if ar else None
        reply = _fmt_device_list(data)
        return reply, "device_list", actions

    if sub == "stats":
        ar = ex.execute("stats", [], {})
        actions = ar
        data = ar[0].get("data") if ar else None
        reply = "【漏斗 / 统计】\n" + _fmt_funnel(data)
        return reply, "stats", actions

    if sub == "daily_report":
        ar = ex.execute("daily_report", [], {})
        actions = ar
        data = ar[0].get("data") if ar else None
        reply = "【今日日报】\n" + _fmt_daily(data)
        return reply, "daily_report", actions

    if sub == "health":
        ar = ex.execute("health", [], {})
        actions = ar
        data = ar[0].get("data") if ar else None
        reply = "【系统健康】\n" + _fmt_funnel(data)
        return reply, "health", actions

    if sub == "leads":
        ar = ex.execute("leads", [], {})
        actions = ar
        data = ar[0].get("data") if ar else None
        reply = "【线索 / CRM】\n" + _fmt_funnel(data)
        return reply, "leads", actions

    if sub == "schedule_list":
        ar = ex.execute("schedule_list", [], {})
        actions = ar
        data = ar[0].get("data") if ar else None
        reply = "【定时任务】\n" + _fmt_funnel(data)
        return reply, "schedule_list", actions

    if sub == "task_errors":
        raw = ex.http_get("/tasks/error-analysis?hours=24&include_samples=true")
        actions = [{"action": "task_insights", "data": raw}]
        reply = "【任务错误分析】\n" + _fmt_error_analysis(raw)
        return reply, "task_insights", actions

    # general：尝试设备列表（最常见问法兜底）
    ar = ex.execute("device_list", [], {})
    actions = ar
    data = ar[0].get("data") if ar else None
    reply = (
        "【查询结果】\n"
        + _fmt_device_list(data)
        + "\n\n提示：需要漏斗/日报/任务失败统计时，可问「漏斗数据」「今日日报」「任务为什么失败」。"
    )
    return reply, "device_list", actions
