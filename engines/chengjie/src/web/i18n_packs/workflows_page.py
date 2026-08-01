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
}
