# -*- coding: utf-8 -*-
"""侧栏导航域词条(分组标题 + 渠道状态点浮层 + 命令面板专属项标签)。结构见包 docstring。"""

ZH = {
    # 命令面板专属项(CMD_EXTRA_ITEMS.work_goal):括注同义词,搜「工作计划」的人一眼认出
    "nav_work_goal": "工作目标（工作计划）",
    # 工作台主管看板(2026-08-16 升格正式侧栏入口;2026-08-18 P1 收成页群:
    # 侧栏一个「主管看板」入口,四页经页内 Tab 互切,单页仍可 Ctrl+K 直达)
    "nav_ws_boards": "主管看板",
    "nav_ws_queue": "运营队列看板",
    "nav_ws_perf": "坐席绩效看板",
    "nav_ws_aiq": "AI 质量看板",
    "nav_ws_roi": "ROI 看板",
    "nav_ws_usage": "用量看板",
    # 用量与计费组(2026-08-16 管理面改造)
    "nav_usage_center": "用量与额度",
    "section_usage_billing": "用量与计费",
    "section_workbench": "工作台",
    "section_channels": "真机矩阵",
    "section_ai_kb": "AI 与知识",
    "section_insights": "数据洞察",
    "section_compliance": "安全合规",
    "section_support": "支持",
    # 分组一句话定位(base.html 分组标题 title 悬浮;防分类语义再漂移的 UI 锚点)
    "section_note_workbench": "今天要处理的事",
    "section_note_channels": "渠道与设备运维",
    "section_note_ai_kb": "教 AI 怎么说话",
    "section_note_insights": "只看数，不改配置",
    "section_note_usage_billing": "资源花到哪、还剩多少",
    "section_note_compliance": "出了事怎么查",
    "section_note_system": "配置这套系统",
    "section_note_support": "帮助与个性化",
    "nav.dot.paused": "已暂停",
    "nav.dot.none": "暂无账号数据",
    "nav.dot.goto": "打开渠道页",
    # 分组折叠(2026-08-16):完整模式分组头按钮的悬浮提示
    "nav_grp_toggle_hint": "点击折叠/展开分组",
    # 页群 Tab 条(_cluster_tabs.html)的无障碍标签
    "nav_cluster_aria": "相关页面",
}

EN = {
    "nav_work_goal": "Work Goal (work plan)",
    "nav_ws_boards": "Supervisor boards",
    "nav_ws_queue": "Ops queue board",
    "nav_ws_perf": "Agent performance board",
    "nav_ws_aiq": "AI quality board",
    "nav_ws_roi": "ROI board",
    "nav_ws_usage": "Usage board",
    "nav_usage_center": "Usage & Quota",
    "section_usage_billing": "Usage & Billing",
    "section_workbench": "Workbench",
    "section_channels": "Device Matrix",
    "section_ai_kb": "AI & Knowledge",
    "section_insights": "Data Insights",
    "section_compliance": "Security & Compliance",
    "section_support": "Support",
    "section_note_workbench": "What needs handling today",
    "section_note_channels": "Channel & device ops",
    "section_note_ai_kb": "Teach the AI how to talk",
    "section_note_insights": "Read-only analytics",
    "section_note_usage_billing": "Where resources go & what's left",
    "section_note_compliance": "Investigate incidents",
    "section_note_system": "Configure the system",
    "section_note_support": "Help & personalization",
    "nav.dot.paused": "Paused",
    "nav.dot.none": "No account data",
    "nav.dot.goto": "Open channel page",
    "nav_grp_toggle_hint": "Click to collapse/expand this section",
    "nav_cluster_aria": "Related pages",
}
