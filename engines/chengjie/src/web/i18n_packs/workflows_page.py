# -*- coding: utf-8 -*-
"""workflows.html 增量词条（B1 种子包 + B2 效果漏斗）。

存量 wf_s*/wf_js* 键仍在 web_i18n.py 单体；按「新增词条一律进 pack」约定，
本文件只承载工作流页新功能的键。
"""

ZH = {
    "wf_seed_btn": "导入示例链",
    "wf_seed_done": "已导入 {n} 条示例链",
    "wf_seed_none": "示例链已全部存在，未重复导入",
    "wf_seed_fail": "导入失败",
    "wf_funnel_title": "近 {d} 天效果漏斗",
    "wf_funnel_started": "启动",
    "wf_funnel_reply": "72h 回复率",
    "wf_funnel_reply_hint": "回复率 = 启动后 72 小时内客户有入站消息的执行占比（只统计已满 72h 的执行；近似归因，不代表因果）",
    "wf_funnel_empty": "窗口内暂无执行记录——在会话右栏「跟进 SOP（工作链）」卡或下一步建议里启动一条链后，这里开始累计",
    "wf_funnel_col_chain": "工作链",
    "wf_funnel_col_reply": "回复率（成熟数）",
    "wf_funnel_attr": "带目标归因 {n}",
    "wf_funnel_recf": "推荐跟随",
    "wf_funnel_recf_hint": "目标归因的启动里，启动的恰是该目标推荐链的占比（分母只算模板有推荐链的启动；无推荐可跟不算「没跟」）",
    "wf_onboard_title": "一分钟上手",
    "wf_onboard_s1": "① 点「导入示例链」拿到 4 条现成跟进 SOP（破冰/唤回/关怀/跟单），或自己新建",
    "wf_onboard_s2": "② 到收件箱会话右栏「跟进 SOP（工作链）」卡（客户&关系 tab），给具体客户一键挂上",
    "wf_onboard_s3": "③ 回本页「执行监控」看漏斗：谁在跑、完成率、启动后 72h 回复率",
    "wf_onboard_dismiss": "知道了",
    # ── 引导式教程（复用 workspace_base 的 blTour 聚光灯组件）──
    "wf_tour_replay": "▶ 播放引导",
    "wf_tour_t1": "这里是工作链（跟进 SOP）",
    "wf_tour_b1": "把「什么时候该对客户说什么」编成按时间自动推进的步骤链；挂到客户后系统按节奏提醒或代发，跟进不再漏人。",
    "wf_tour_t2": "先导入 4 条现成示例链",
    "wf_tour_b2": "新客破冰、沉默唤回、成交后关怀、报价跟单——点这里一键导入（已存在的不会重复导入），导入后可随意改步骤。",
    "wf_tour_t3": "每条链＝若干按序步骤",
    "wf_tour_b3": "每步有动作类型（话术/任务/标签…）和「距上一步的延迟」，到点自动推进；卡片上可直接停用、编辑或删除整条链。",
    "wf_tour_t4": "去收件箱挂到客户身上",
    "wf_tour_b4": "回聊天工作台，会话右栏「跟进 SOP（工作链）」卡一键启动；给会话建了工作目标的话，配套链会自动置顶推荐。",
    "wf_tour_t5": "回来看效果",
    "wf_tour_b5": "「执行监控」有漏斗：启动/完成/失败、启动后 72 小时客户回复率、目标归因与推荐跟随率——用数据判断哪条链有效。",
    # ── 工作链卡片增强 ──
    "wf_chain_running_n": "运行中 {n}",
    "wf_chain_disabled": "已停用",
    "wf_chain_enable_t": "停用后不再自动触发、也不出现在收件箱启动列表；运行中的执行不受影响",
    "wf_step_imm": "立即",
    "wf_step_cum": "累计第 {h} 小时",
    "wf_auto_trigger_hint": "后台定时扫描，满足条件时自动启动本链（同会话同链仅一条运行中，不会重复）",
    # ── P2：页头价值条 / 空态包名预览 ──
    "wf_vb_head": "近 {d} 天",
    "wf_vb_goal": "带目标归因",
    "wf_vb_go_monitor": "看明细 →",
    "wf_empty_packs": "示例包含：新客破冰 · 沉默唤回 · 成交后关怀 · 报价跟单",
    # ── P3：每链回复率 pill / 价值条空账面引导 ──
    "wf_chain_reply_pill": "72h 回复 {p}%（{n}/{m}）",
    "wf_vb_empty_note": "暂无运行记录——把跟进 SOP 挂到客户后，这里开始记账",
    "wf_vb_empty_cta": "📦 去导入/挂链 →",
    # ── P5：漏斗表格环节明细下钻（消费 by_step 步骤日志聚合）──
    "wf_bs_show": "环节明细",
    "wf_bs_hide": "收起明细",
}

EN = {
    "wf_seed_btn": "Import starter chains",
    "wf_seed_done": "Imported {n} starter chain(s)",
    "wf_seed_none": "All starter chains already exist — nothing imported",
    "wf_seed_fail": "Import failed",
    "wf_funnel_title": "Funnel (last {d} days)",
    "wf_funnel_started": "Started",
    "wf_funnel_reply": "72h reply rate",
    "wf_funnel_reply_hint": "Share of executions with an inbound customer message within 72h of start (only executions past the 72h window are counted; approximate attribution, not causal)",
    "wf_funnel_empty": "No executions in this window — start a chain from the conversation sidebar (Follow-up SOP card) or Next-step suggestions, then numbers accrue here",
    "wf_funnel_col_chain": "Chain",
    "wf_funnel_col_reply": "Reply rate (mature n)",
    "wf_funnel_attr": "Goal-attributed {n}",
    "wf_funnel_recf": "Followed suggestion",
    "wf_funnel_recf_hint": "Among goal-attributed starts, share that launched the goal's suggested chain (denominator counts only starts whose goal template has a suggestion)",
    "wf_onboard_title": "One-minute start",
    "wf_onboard_s1": "① Click \u201cImport starter chains\u201d for 4 ready follow-up SOPs (icebreak/reactivate/care/quote), or build your own",
    "wf_onboard_s2": "② In the inbox right rail, open the \u201cFollow-up SOP (Workflows)\u201d card (Customer tab) and attach one to a customer",
    "wf_onboard_s3": "③ Come back to \u201cMonitor\u201d for the funnel: what runs, completion, 72h reply rate after start",
    "wf_onboard_dismiss": "Got it",
    # ── Guided tour (reuses workspace_base blTour spotlight) ──
    "wf_tour_replay": "▶ Play tour",
    "wf_tour_t1": "This is Workflow Chains (follow-up SOPs)",
    "wf_tour_b1": "Turn \u201cwhat to say to a customer, and when\u201d into time-driven step chains; once attached, the system nudges or sends on schedule so no one slips through.",
    "wf_tour_t2": "Import 4 ready-made starter chains first",
    "wf_tour_b2": "Icebreak, reactivate, post-sale care, quote follow-up — one click imports them (existing ones are never duplicated), then edit steps freely.",
    "wf_tour_t3": "Each chain = ordered steps",
    "wf_tour_b3": "Every step has an action type (template/task/tag…) and a delay from the previous step; it advances automatically. Disable, edit or delete a chain right on its card.",
    "wf_tour_t4": "Attach it to a customer in the inbox",
    "wf_tour_b4": "Back in the chat workspace, use the \u201cFollow-up SOP (Workflows)\u201d card in the right rail to start one; if the conversation has an active goal, its suggested chain is pinned on top.",
    "wf_tour_t5": "Come back to check results",
    "wf_tour_b5": "\u201cMonitor\u201d shows the funnel: started/completed/failed, 72h customer reply rate after start, goal attribution and suggestion-follow rate — judge chains by data.",
    # ── Chain card upgrades ──
    "wf_chain_running_n": "Running {n}",
    "wf_chain_disabled": "Disabled",
    "wf_chain_enable_t": "Disabled chains stop auto-triggering and disappear from the inbox start list; executions already running are not affected",
    "wf_step_imm": "Now",
    "wf_step_cum": "cumulative hour {h}",
    "wf_auto_trigger_hint": "Background scan auto-starts this chain when conditions match (one running execution per conversation per chain — no duplicates)",
    # ── P2: header value bar / empty-state pack preview ──
    "wf_vb_head": "Last {d} days",
    "wf_vb_goal": "Goal-attributed",
    "wf_vb_go_monitor": "Details →",
    "wf_empty_packs": "Includes: icebreak · reactivate · post-sale care · quote follow-up",
    # ── P3: per-chain reply pill / value-bar empty CTA ──
    "wf_chain_reply_pill": "72h reply {p}% ({n}/{m})",
    "wf_vb_empty_note": "No runs yet — attach a follow-up SOP to a customer and numbers accrue here",
    "wf_vb_empty_cta": "📦 Import / attach →",
    # ── P5: funnel table per-step drill-down (consumes by_step log aggregation) ──
    "wf_bs_show": "Step detail",
    "wf_bs_hide": "Hide detail",
}
