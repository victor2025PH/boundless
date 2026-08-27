# -*- coding: utf-8 -*-
"""ops-overview「🪦 被埋会话」卡词条（P0-198，2026-08-04）。

被埋 = 会话归档着、却有未读入站：客户在等，而工作台所有默认视图都过滤 archived=1
→ 没人看得见、也没有任何信号会响。事故原型是一条 33 条消息的活跃会话在坐席聊天
途中从工作台彻底消失。

独立成小 pack（而非并入 ops_overview_page.py）：那个大包常被并行工作流编辑，
按 i18n_packs 治理机制（自动发现 + 跨 pack 同 key 即抛错）拆小文件零冲突。
卡片消费方：src/web/templates/ops_overview.html::loadBuriedConvs；
数据源：GET /api/admin/buried-conversations（unified_inbox_account_routes）。
"""

ZH = {
    "ov2_s_buried": "被埋会话（已归档 · 客户还在等）",
    "ov2_bc_count": "被埋会话",
    "ov2_bc_unread": "未读消息",
    "ov2_bc_manual": "人工归档",
    "ov2_bc_auto": "自动归档",
    "ov2_bc_hours": " 小时前",
    "ov2_bc_days": " 天前",
    "ov2_bc_src_auto": "自动",
    "ov2_bc_src_manual": "人工",
    "ov2_bc_th_who": "客户",
    "ov2_bc_th_platform": "平台",
    "ov2_bc_th_unread": "未读",
    "ov2_bc_th_age": "最近活动",
    "ov2_bc_th_src": "归档来源",
    "ov2_bc_cta": "这些会话不出现在收件箱任何默认视图里，客户还在发消息但坐席看不到。"
                  "处置：收件箱「更多 → 归档」逐个确认，还在服务的点「取消归档」；"
                  "若多为自动归档，说明策略把活跃会话判死了。",
}

EN = {
    "ov2_s_buried": "Buried conversations (archived · customer still waiting)",
    "ov2_bc_count": "Buried",
    "ov2_bc_unread": "Unread messages",
    "ov2_bc_manual": "Archived manually",
    "ov2_bc_auto": "Auto-archived",
    "ov2_bc_hours": "h ago",
    "ov2_bc_days": "d ago",
    "ov2_bc_src_auto": "Auto",
    "ov2_bc_src_manual": "Manual",
    "ov2_bc_th_who": "Customer",
    "ov2_bc_th_platform": "Platform",
    "ov2_bc_th_unread": "Unread",
    "ov2_bc_th_age": "Last activity",
    "ov2_bc_th_src": "Archived by",
    "ov2_bc_cta": "These conversations are hidden from every default inbox view — the "
                  "customer keeps writing but no agent can see it. Fix: Inbox → More → "
                  "Archived, review each one and Unarchive those still in service. If "
                  "most are auto-archived, the policy is killing active conversations.",
}
