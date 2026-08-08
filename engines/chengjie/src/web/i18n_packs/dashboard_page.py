# -*- coding: utf-8 -*-
"""仪表盘 2026-08 改版域词条（待办条 / 统计卡重映射 / 工程明细折叠区）。

结构见包 docstring。历史 db_* 键仍在 web_i18n 单体；本次新增一律 db2_ 前缀
落本 pack（词条单源规则：新增禁止进单体）。
"""

ZH = {
    # ── 待办条 ───
    "db2_todo_title": "待办",
    "db2_todo_workspace": "打开工作台",
    "db2_todo_drafts": "待审草稿",
    "db2_todo_learner": "学习队列",
    "db2_todo_crisis": "危机待审",
    "db2_todo_cases": "案例待处理",
    "db2_todo_sla": "SLA 超时",
    "db2_todo_empty": "暂无待办 ✓",
    # ── 统计卡 ───
    "db2_stat_msgs": "消息 收/发",
    "db2_stat_msgs_today": "今日消息 收/发",
    "db2_stat_ai_sent": "AI 发出",
    "db2_stat_reply_rate": "回复率",
    "db2_stat_diag": "系统诊断",
    "db2_stat_proactive": "主动触达（14天）",
    # ── 折叠区 ───
    "db2_eng_title": "工程明细（Bot 性能 · 触发器决策）",
    "db2_eng_hint": "排障调参用，点击展开",
    "db2_rel_more": "完整运维卡片看运营总览",
    "db2_kb_entries_unit": "条",
    "db2_kb_hit": "今日命中",
    # ── 导航 ───
    "db2_nav_ops": "运营总览",
}

EN = {
    # ── todo bar ───
    "db2_todo_title": "To-dos",
    "db2_todo_workspace": "Open workspace",
    "db2_todo_drafts": "Drafts to review",
    "db2_todo_learner": "Learning queue",
    "db2_todo_crisis": "Crisis pending",
    "db2_todo_cases": "Open cases",
    "db2_todo_sla": "SLA overdue",
    "db2_todo_empty": "All clear ✓",
    # ── stat cards ───
    "db2_stat_msgs": "Messages in/out",
    "db2_stat_msgs_today": "Messages today in/out",
    "db2_stat_ai_sent": "AI sent",
    "db2_stat_reply_rate": "Reply rate",
    "db2_stat_diag": "Diagnostics",
    "db2_stat_proactive": "Proactive outreach (14d)",
    # ── folds ───
    "db2_eng_title": "Engineering detail (bot metrics · trigger log)",
    "db2_eng_hint": "For debugging & tuning — click to expand",
    "db2_rel_more": "Full ops cards in Ops Overview",
    "db2_kb_entries_unit": "entries",
    "db2_kb_hit": "hit rate today",
    # ── nav ───
    "db2_nav_ops": "Ops Overview",
}
