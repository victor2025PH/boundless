# -*- coding: utf-8 -*-
"""老板日报页词条（/workspace/boss，WP-3 2026-08-17）。

铁律（规格验收硬条款）：本页词汇只用钱和时间的人话——「草稿 / 拟稿 / autosend /
L2 / draft」这类工程黑话**禁止**出现在任何 bp_* 词条里（有门禁扫描）。
"""

ZH = {
    "bp_title": "经营日报",
    "bp_sub": "AI 今天替你做了什么——只看钱和时间，细节都在工作台里。",
    "bp_refresh": "刷新",
    "bp_export": "导出本周报告",
    "bp_loading": "加载中…",
    "bp_loading_txt": "加载中…",
    "bp_err": "数据加载失败，请稍后重试",
    # 英雄数字
    "bp_k_replies": "今天 AI 替你发出的回复",
    "bp_k_saved": "折算省下的人工时间",
    "bp_k_outreach": "今天主动问候的客户",
    "bp_k_inbound": "今天收到的客户消息",
    "bp_unit_hours": "小时",
    "bp_unit_minutes": "分钟",
    "bp_cmp_new": "昨天还没有数据",
    "bp_cmp_yday": "较昨天 {p}%",
    "bp_outreach_resp": "其中 {n} 位已回应",
    # 一句话解读
    "bp_say_replies": "今天 AI 替你发出了 {n} 条回复，折算省下约 {h} 人工。",
    "bp_say_outreach": "主动问候了 {z} 位客户，其中 {w} 位有了回应。",
    "bp_say_quiet": "今天还很安静——AI 正在待命，有客户消息进来会自动接待。",
    # 本周合计
    "bp_week_title": "本周合计",
    "bp_week_note": "按最近 7 天滚动统计；数字来自实例数据库，重启不清零。",
    "bp_week_replies": "AI 发出回复 {n} 条",
    "bp_week_saved": "省下约 {h} 小时",
    "bp_week_outreach": "主动问候 {z} 次（{w} 次回应）",
    "bp_week_traffic": "发出 {o} 条 / 收到 {i} 条",
    # 趋势
    "bp_trend_title": "近 7 天趋势",
    "bp_legend_out": "发出的消息",
    "bp_legend_ai": "AI 发出的回复",
    "bp_coef_note": "「省下的人工时间」按每条回复的人工均时估算，系数可由管理员按团队实情调整。",
    # 空态（诚实：不摆零假数据）
    "bp_empty_title": "还没有可统计的数据",
    "bp_empty_body": "接入渠道并让 AI 开始接待后，这里会出现你的价值账单。",
}

EN = {
    "bp_title": "Business Daily",
    "bp_sub": "What the AI did for you today — money and time only; details live in the workspace.",
    "bp_refresh": "Refresh",
    "bp_export": "Export weekly report",
    "bp_loading": "Loading…",
    "bp_loading_txt": "Loading…",
    "bp_err": "Failed to load data, please retry later",
    # hero numbers
    "bp_k_replies": "Replies the AI sent for you today",
    "bp_k_saved": "Estimated staff time saved",
    "bp_k_outreach": "Customers greeted proactively today",
    "bp_k_inbound": "Customer messages received today",
    "bp_unit_hours": "h",
    "bp_unit_minutes": "min",
    "bp_cmp_new": "no data yesterday",
    "bp_cmp_yday": "{p}% vs yesterday",
    "bp_outreach_resp": "{n} responded",
    # one-line interpretation
    "bp_say_replies": "The AI sent {n} replies for you today, saving roughly {h} of staff time.",
    "bp_say_outreach": "It proactively greeted {z} customers; {w} responded.",
    "bp_say_quiet": "Quiet so far today — the AI is on standby and will handle incoming customers automatically.",
    # weekly totals
    "bp_week_title": "This week",
    "bp_week_note": "Rolling 7-day window; numbers come from the instance database and survive restarts.",
    "bp_week_replies": "{n} replies sent by AI",
    "bp_week_saved": "~{h} hours saved",
    "bp_week_outreach": "{z} proactive greetings ({w} responses)",
    "bp_week_traffic": "{o} sent / {i} received",
    # trend
    "bp_trend_title": "Last 7 days",
    "bp_legend_out": "Messages sent",
    "bp_legend_ai": "Replies sent by AI",
    "bp_coef_note": "\u201cStaff time saved\u201d is estimated from average handling minutes per reply; the admin can tune the coefficient to match your team.",
    # empty state (honest: no fake zeros)
    "bp_empty_title": "Nothing to report yet",
    "bp_empty_body": "Once channels are connected and the AI starts handling customers, your value ledger appears here.",
}
