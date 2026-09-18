# -*- coding: utf-8 -*-
"""拦截台账词条（Q-18 #293，2026-09-11）。键族 ``rps_al_*``（回复设置页「今日拦截」卡：标题 / 说明 /
表头 / 空态 / 合计 / 阶段 + 原因码人话 ``rps_al_r_<code>``，code 与 ``src/inbox/abort_ledger.REASONS``
一一对应）。繁体手写 ZH_HANT（不碰生成物 zh_hant_auto）。
"""

ZH = {
    "rps_al_title": "🚧 今日拦截 · AI 为什么没回",
    "rps_al_hint": "最近 24 小时自动回复被拦下的次数，按原因码计：成人内容 / 风险保持 / 需人工 / 坐席刚发过（让位） / 坐席在打字（让位） / 档位切换 / 班表休息 / 同一句只发一次。让位两项从 v1.0.82 起只是延后不是丢稿，窗过自动发。",
    "rps_al_col_conv": "会话",
    "rps_al_col_time": "时间",
    "rps_al_col_reason": "原因",
    "rps_al_col_hit": "命中词 / 阶段",
    "rps_al_empty": "最近 24 小时没有拦截记录。",
    "rps_al_total": "24h 共 {n} 次",
    "rps_al_stage": "阶段 {stage}",
    "rps_al_r_adult": "成人内容",
    "rps_al_r_risk_hold": "风险保持",
    "rps_al_r_needs_human": "需人工",
    "rps_al_r_agent_sent": "坐席刚发过（让位）",
    "rps_al_r_agent_typing": "坐席在打字（让位）",
    "rps_al_r_mode_changed": "档位切换",
    "rps_al_r_work_schedule": "班表休息",
    "rps_al_r_dup_suppressed": "同一句只发一次 · 已拦第二条",
    "rps_al_r_risk_recorded": "敏感话题 · 已记录（AI 照常回，没拦）",
}

EN = {
    "rps_al_title": "🚧 Blocked today · why the AI didn't reply",
    "rps_al_hint": "Auto-replies held back in the last 24h, by reason code: adult content / risk hold / needs human / agent just sent (yield) / agent typing (yield) / mode switched / off-hours schedule / same sentence sent once. Since v1.0.82 the two yield reasons only defer the draft — it auto-sends once the window passes.",
    "rps_al_col_conv": "Conversation",
    "rps_al_col_time": "Time",
    "rps_al_col_reason": "Reason",
    "rps_al_col_hit": "Hit / stage",
    "rps_al_empty": "No blocks in the last 24 hours.",
    "rps_al_total": "{n} in 24h",
    "rps_al_stage": "stage {stage}",
    "rps_al_r_adult": "Adult content",
    "rps_al_r_risk_hold": "Risk hold",
    "rps_al_r_needs_human": "Needs human",
    "rps_al_r_agent_sent": "Agent just sent (yield)",
    "rps_al_r_agent_typing": "Agent typing (yield)",
    "rps_al_r_mode_changed": "Mode switched",
    "rps_al_r_work_schedule": "Off-hours schedule",
    "rps_al_r_dup_suppressed": "Same sentence sent once · second copy blocked",
    "rps_al_r_risk_recorded": "Sensitive topic · recorded only (AI replied as usual)",
}

ZH_HANT = {
    "rps_al_title": "🚧 今日攔截 · AI 為什麼沒回",
    "rps_al_hint": "最近 24 小時自動回覆被攔下的次數，按原因碼計：成人內容 / 風險保持 / 需人工 / 坐席剛發過（讓位） / 坐席在打字（讓位） / 檔位切換 / 班表休息 / 同一句只發一次。讓位兩項從 v1.0.82 起只是延後不是丟稿，窗過自動發。",
    "rps_al_col_conv": "會話",
    "rps_al_col_time": "時間",
    "rps_al_col_reason": "原因",
    "rps_al_col_hit": "命中詞 / 階段",
    "rps_al_empty": "最近 24 小時沒有攔截記錄。",
    "rps_al_total": "24h 共 {n} 次",
    "rps_al_stage": "階段 {stage}",
    "rps_al_r_adult": "成人內容",
    "rps_al_r_risk_hold": "風險保持",
    "rps_al_r_needs_human": "需人工",
    "rps_al_r_agent_sent": "坐席剛發過（讓位）",
    "rps_al_r_agent_typing": "坐席在打字（讓位）",
    "rps_al_r_mode_changed": "檔位切換",
    "rps_al_r_work_schedule": "班表休息",
    "rps_al_r_dup_suppressed": "同一句只發一次 · 已攔第二條",
    "rps_al_r_risk_recorded": "敏感話題 · 已記錄（AI 照常回，沒攔）",
}
