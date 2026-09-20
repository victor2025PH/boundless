# -*- coding: utf-8 -*-
"""ops-overview「☁️ 托管租户」卡词条（托管 SaaS 观测面，2026-08-07）。

独立成 pack（照 alert_link_ops.py 先例）：ops_overview_page.py 大包常被并行
工作流编辑，拆小文件零冲突。卡片消费方：ops_overview.html::loadTenantOverview；
数据源：GET /api/admin/tenant-overview（收集器 src/ops/tenant_overview.py）。
"""

ZH = {
    "ov2_s_tenantov": "托管租户（SaaS）",
    "ov2_to_total": "租户实例",
    "ov2_to_running": "在跑",
    "ov2_to_susp": "暂停",
    "ov2_to_down": "掉线",
    "ov2_to_held": "持单中",
    "ov2_to_done": "累计交付",
    "ov2_to_guard": "履约心跳",
    "ov2_to_guard_never": "从未",
    "ov2_to_guard_stale": "守护心跳超时（计划任务停了？查 logs\\tenant_guard\\）",
    "ov2_to_held_label": "持单订单（实例已开通、公网不可达——放行安全组或加泛解析即自动送达）",
    "ov2_to_held_hours": "已持",
    "ov2_to_tbl_iid": "实例",
    "ov2_to_tbl_state": "状态",
    "ov2_to_tbl_pub": "公网",
    "ov2_to_tbl_expiry": "到期",
    "ov2_to_exp_expired": "已到期",
    "ov2_to_exp_days": "剩 {n} 天",
    "ov2_to_exp_hint": "有租户已到期——确认未续费后 suspend（watch 巡检已告警；到期口径=停机）",
    "ov2_to_tbl_backup": "最近备份",
    "ov2_to_backup_none": "无",
    "ov2_to_state_running": "在跑",
    "ov2_to_state_suspended": "暂停",
    "ov2_to_state_down": "掉线",
    "ov2_to_down_hint": "有租户掉线（应在跑而没跑）——TenantSelfHeal 每 10 分钟自愈；连败会转人工",
}

EN = {
    "ov2_s_tenantov": "Hosted tenants (SaaS)",
    "ov2_to_total": "Tenant instances",
    "ov2_to_running": "Running",
    "ov2_to_susp": "Suspended",
    "ov2_to_down": "Down",
    "ov2_to_held": "Deliveries held",
    "ov2_to_done": "Delivered total",
    "ov2_to_guard": "Fulfill heartbeat",
    "ov2_to_guard_never": "never",
    "ov2_to_guard_stale": "Guard heartbeat stale (scheduled task stopped? see logs\\tenant_guard\\)",
    "ov2_to_held_label": "Held orders (instance up, public URL unreachable — opens automatically once SG/DNS is fixed)",
    "ov2_to_held_hours": "held",
    "ov2_to_tbl_iid": "Instance",
    "ov2_to_tbl_state": "State",
    "ov2_to_tbl_pub": "Public",
    "ov2_to_tbl_expiry": "Expiry",
    "ov2_to_exp_expired": "expired",
    "ov2_to_exp_days": "{n}d left",
    "ov2_to_exp_hint": "Tenant(s) expired — suspend after confirming no renewal (watch already alerted; hosted expiry = stop)",
    "ov2_to_tbl_backup": "Last backup",
    "ov2_to_backup_none": "none",
    "ov2_to_state_running": "running",
    "ov2_to_state_suspended": "suspended",
    "ov2_to_state_down": "down",
    "ov2_to_down_hint": "Tenant down (should be running) — TenantSelfHeal retries every 10 min; repeated failures escalate to manual",
}
