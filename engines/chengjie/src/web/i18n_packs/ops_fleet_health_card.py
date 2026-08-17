# -*- coding: utf-8 -*-
"""ops-overview「🛡️ 账号健康分」老板卡词条（P3 2026-08-09）。

**薄消费卡**：数据源＝既有 ``/api/accounts/fleet-health``（M7 account_health ×
account_signals.fleet_overview，rpa_overview 同源）——绝不另算一套评分，只是把
「哪个号在冒烟」搬到老板看的 ops-overview（与自动化覆盖率卡同一屏回答
「跑得多不多 × 跑得安不安全」）。**刻意独立成 pack**（新文件＝零撞车）。
"""

ZH = {
    "ov2_s_fleethealth": "账号健康分（M7 反封号评分 · 哪个号在冒烟）",
    "ov2_fh_sub": "与机群总览/发送闸门同源（注册表天龄/代理 × 限频/失败 24h × 今日发量 × 资料变更）；"
                  "分数驱动 recommended_cap 自动降速。零账号整卡隐藏",
    "ov2_fh_total": "受管账号",
    "ov2_fh_light": "机群灯",
    "ov2_fh_avg": "平均分",
    "ov2_fh_red": "红灯号",
    "ov2_fh_warming": "预热中",
    "ov2_fh_risk": "受限/封禁",
    "ov2_fh_col_account": "账号",
    "ov2_fh_col_score": "分数",
    "ov2_fh_col_cap": "建议日上限",
    "ov2_fh_col_reasons": "扣分原因",
    "ov2_fh_worst_title": "最需要关注的账号",
    "ov2_fh_lifecycle": "生命周期",
    "ov2_fh_churn": "改资料热点（7 天 ≥3 次）",
    "ov2_fh_all_green": "全部账号健康，无风控信号",
    # P2 2026-08-13 额度双道 + 拦截计数（人工预留额度可视化）
    "ov2_fh_col_quota": "今日发量（滚动24h）",
    "ov2_fh_blocks": "护栏拦截·重启后",
    "ov2_fh_blocks_manual": "其中人工被拦",
    "ov2_fh_quota_line": "额度水位",
}

EN = {
    "ov2_s_fleethealth": "Account health (M7 anti-ban score · which account is smoking)",
    "ov2_fh_sub": "Same source as fleet overview / send gate (registry age/proxy × flood/error 24h × sends today × profile churn); "
                  "the score drives recommended_cap auto-slowdown. Card hides with zero accounts",
    "ov2_fh_total": "Managed accounts",
    "ov2_fh_light": "Fleet light",
    "ov2_fh_avg": "Avg score",
    "ov2_fh_red": "Red accounts",
    "ov2_fh_warming": "Warming up",
    "ov2_fh_risk": "Restricted/banned",
    "ov2_fh_col_account": "Account",
    "ov2_fh_col_score": "Score",
    "ov2_fh_col_cap": "Daily cap hint",
    "ov2_fh_col_reasons": "Deductions",
    "ov2_fh_worst_title": "Accounts needing attention",
    "ov2_fh_lifecycle": "Lifecycle",
    "ov2_fh_churn": "Profile-churn hotspots (≥3 in 7d)",
    "ov2_fh_all_green": "All accounts healthy, no risk signals",
    # P2 2026-08-13 dual-lane quota + block counter (manual-reserve visibility)
    "ov2_fh_col_quota": "Sends today (rolling 24h)",
    "ov2_fh_blocks": "Guard blocks (since boot)",
    "ov2_fh_blocks_manual": "…manual sends blocked",
    "ov2_fh_quota_line": "Quota levels",
}
