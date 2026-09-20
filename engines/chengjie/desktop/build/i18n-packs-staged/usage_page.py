# -*- coding: utf-8 -*-
"""用量与额度面板词条（2026-08-16 用量页一页两态 + 坐席字符归因）。

覆盖 workspace_usage.html 新增的三块 UI：
- 主管视图「字符额度」区（授权总池 + 本月团队消耗）与「分坐席用量」表；
- 坐席/观察员「我的用量」自视图（本月/今日/分类目/重置日/状态）。

按仓库词条单源规则进 pack（勿往 web_i18n.py 单体加键）；键前缀 ``uq_``，
新建前已 rg 确认全库唯一。ZH/EN 键一一对应，由
``test_i18n_packs_bilingual_and_no_collision`` 门禁钉住。
"""

ZH = {
    "uq_page_sub": "用量与额度",
    # ── 字符额度区（主管视图）──
    "uq_char_title": "字符额度",
    "uq_pool": "授权总池",
    "uq_pool_unlimited": "不限 / 未启用",
    "uq_pool_remaining": "剩余",
    "uq_team_month": "本月团队消耗",
    "uq_by_cat_translation": "翻译",
    "uq_by_cat_tts": "语音合成",
    "uq_by_cat_other": "其它",
    "uq_buy_more": "增购",
    # ── 分坐席用量表（主管视图）──
    "uq_agents_title": "分坐席用量",
    "uq_col_member": "成员",
    "uq_col_role": "角色",
    "uq_col_month": "本月字符",
    "uq_col_today": "今日",
    "uq_col_quota": "额度",
    "uq_col_pct": "使用率",
    "uq_col_level": "状态",
    "uq_unlimited": "不限",
    "uq_level_ok": "正常",
    "uq_level_warn": "预警",
    "uq_level_over": "超额",
    "uq_not_enabled": "坐席级计量未开启（usage.agent_chars.enabled）",
    # ── 我的用量（坐席自视图）──
    "uq_my_title": "我的用量",
    "uq_my_today": "今日",
    "uq_my_reset": "重置日",
    "uq_empty_backend": "计量未启用或后端未更新",
    # ── 用量页 v2（2026-08-16：硬限徽章 / 推导 AI 自动行 / 预计耗尽）──
    "uq_enforce_on": "硬限已开启",
    "uq_enforce_soft": "软提醒模式",
    "uq_derived_auto": "AI 自动+未归因（推导）",
    "uq_derived_auto_tip": "授权池当月消耗 − 坐席归因合计，推导值非直接计量",
    "uq_exhaust_text": "按近 7 天日均约 {days} 天耗尽（≈{date}）",
    # ── P1（2026-08-18）：环比低基数治理——|环比|≥500% 属小样本假信号，
    #    显示中性说明替代吓人的 ▲9000%（低基数期误导决策）。──
    "uq_low_base": "基数小，环比暂不具参考",
    # ── P2（2026-08-18）：空态行动化 + 趋势峰值标注 ──
    "uq_empty_cta": "去接入渠道 →",
    "uq_spark_peak": "峰值",
    # ── 工作台额度预警条（workspace_base uqw-bar，warn/over 才显示）──
    "uq_warnbar_text": "字符额度已用 {pct}%（{used}/{quota}）",
    "uq_warnbar_cta": "点击查看",
}

EN = {
    "uq_page_sub": "Usage & quota",
    # ── Character quota section (supervisor view) ──
    "uq_char_title": "Character quota",
    "uq_pool": "License pool",
    "uq_pool_unlimited": "Unlimited / not enabled",
    "uq_pool_remaining": "Remaining",
    "uq_team_month": "Team usage this month",
    "uq_by_cat_translation": "Translation",
    "uq_by_cat_tts": "Voice (TTS)",
    "uq_by_cat_other": "Other",
    "uq_buy_more": "Buy more",
    # ── Per-agent usage table (supervisor view) ──
    "uq_agents_title": "Per-agent usage",
    "uq_col_member": "Member",
    "uq_col_role": "Role",
    "uq_col_month": "Chars (month)",
    "uq_col_today": "Today",
    "uq_col_quota": "Quota",
    "uq_col_pct": "Usage",
    "uq_col_level": "Status",
    "uq_unlimited": "Unlimited",
    "uq_level_ok": "OK",
    "uq_level_warn": "Warning",
    "uq_level_over": "Over",
    "uq_not_enabled": "Per-agent metering is off (usage.agent_chars.enabled)",
    # ── My usage (agent self view) ──
    "uq_my_title": "My usage",
    "uq_my_today": "Today",
    "uq_my_reset": "Resets on",
    "uq_empty_backend": "Metering not enabled or backend not updated yet",
    # ── Usage page v2 (2026-08-16: enforce badge / derived AI-auto row / runway) ──
    "uq_enforce_on": "Hard limit on",
    "uq_enforce_soft": "Soft-reminder mode",
    "uq_derived_auto": "AI auto + unattributed (derived)",
    "uq_derived_auto_tip": ("License pool usage this month minus attributed agent "
                            "total; derived estimate, not directly metered"),
    "uq_exhaust_text": "~{days} days left at the 7-day team average (≈{date})",
    "uq_low_base": "Small base — trend not meaningful yet",
    "uq_empty_cta": "Connect a channel →",
    "uq_spark_peak": "Peak",
    # ── Workspace quota warn bar (workspace_base uqw-bar, warn/over only) ──
    "uq_warnbar_text": "Character quota {pct}% used ({used}/{quota})",
    "uq_warnbar_cta": "View details",
}
