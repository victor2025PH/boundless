# -*- coding: utf-8 -*-
"""ops-overview「🤖 自动化覆盖率」卡词条（P1 2026-08-09）。

.198/.104 事故的老板级问题可视化：「多少会话真在全自动跑、没跑的被什么压住」。
**刻意独立成 pack**（共享树协议：新文件＝零撞车）。
"""

ZH = {
    "ov2_s_autocov": "自动化覆盖率（多少会话真在全自动跑 · 被什么压住）",
    "ov2_ac_sub": "口径与护栏同源：基础档位 × 账号级封顶（平台/业务线/新号预热）× 接管态；"
                  "「有效全自动」＝档位是全自动且账号未被任何一层封顶。零会话整卡隐藏",
    "ov2_ac_total": "会话总数",
    "ov2_ac_eff_auto": "有效全自动",
    "ov2_ac_capped": "全自动被封顶",
    "ov2_ac_takeover": "接管转人工中",
    "ov2_ac_pending": "待审草稿",
    "ov2_ac_stale": "超24h陈稿",
    "ov2_ac_col_account": "账号",
    "ov2_ac_col_status": "状态",
    "ov2_ac_col_convs": "会话",
    "ov2_ac_col_auto": "全自动",
    "ov2_ac_col_eff": "有效全自动",
    "ov2_ac_col_caps": "封顶",
    "ov2_ac_col_takeover": "接管中",
    "ov2_ac_cap_none": "无",
    "ov2_ac_drafts_line": "待审 {n} 条：2 小时内 {fresh} / 当天 {day} / 超 24h {stale}（最老 {oldest}h）",
    "ov2_ac_counters_line": "本进程窗口：接管 {takeover} 次 · 自动接回 {rearm} 次",
    "ov2_ac_defaulted_hint": "「按全局默认」＝会话从未显式设过档位，按全局 {mode} 执行",
    "ov2_ac_trend_eff": "有效全自动占比 %（按日快照）",
    "ov2_ac_trend_pending": "待审草稿数（按日快照）",
}

EN = {
    "ov2_s_autocov": "Automation coverage (how many conversations truly run full-auto · what's capping the rest)",
    "ov2_ac_sub": "Same source of truth as the guards: base mode × account-level caps (platform/business-line/warm-up) × takeover state; "
                  "\"effective full-auto\" = mode is auto AND the account is not capped by any layer. Card hides with zero conversations",
    "ov2_ac_total": "Conversations",
    "ov2_ac_eff_auto": "Effective full-auto",
    "ov2_ac_capped": "Auto but capped",
    "ov2_ac_takeover": "Taken over (human)",
    "ov2_ac_pending": "Drafts pending",
    "ov2_ac_stale": "Stale >24h",
    "ov2_ac_col_account": "Account",
    "ov2_ac_col_status": "Status",
    "ov2_ac_col_convs": "Convs",
    "ov2_ac_col_auto": "Auto",
    "ov2_ac_col_eff": "Effective auto",
    "ov2_ac_col_caps": "Caps",
    "ov2_ac_col_takeover": "Taken over",
    "ov2_ac_cap_none": "none",
    "ov2_ac_drafts_line": "{n} pending: {fresh} within 2h / {day} today / {stale} over 24h (oldest {oldest}h)",
    "ov2_ac_counters_line": "This process window: {takeover} takeovers · {rearm} auto re-arms",
    "ov2_ac_defaulted_hint": "\"defaulted\" = conversation never explicitly set; runs at the global {mode}",
    "ov2_ac_trend_eff": "Effective full-auto % (daily snapshots)",
    "ov2_ac_trend_pending": "Pending drafts (daily snapshots)",
}
