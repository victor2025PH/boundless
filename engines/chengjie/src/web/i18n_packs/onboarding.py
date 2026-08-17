# -*- coding: utf-8 -*-
"""首启引导词条：简洁模式欢迎弹窗（onb_*）+ /welcome 五步向导（onb_wz_*，WP-2 2026-08）。

弹窗段（2026-08-03）：功能清单与简洁模式新主区对齐（工作台/案例/关怀/自动回复设置），
行标签复用既有导航键（workspace_inbox/care/rps_nav 等），本包只补一句话描述。

向导段（WP-2）：/welcome 页（welcome.html + welcome_routes.py）全部词条。
键后缀与 onboarding_state.STEP_IDS（license/channel/persona/automation/test_message）
对应，改步骤 id 记得同步两边。
"""

ZH = {
    "onb_ws_desc": "全渠道客户消息集中在这里回复",
    "onb_care_desc": "到点该问候谁，系统替你记着",
    "onb_rps_desc": "调 AI 替你回话的快慢与风格",

    # ── /welcome 五步向导（WP-2）──────────────────────────────────────────
    "onb_wz_title": "首启向导",
    "onb_wz_subtitle": "五步跑通：激活授权 → 绑渠道 → 挑人设 → 选自动化档位 → 发一条测试消息。每步都可跳过、可回退，中断后再进来会接着走。",
    "onb_wz_step_license": "授权",
    "onb_wz_step_channel": "渠道",
    "onb_wz_step_persona": "人设",
    "onb_wz_step_automation": "自动化",
    "onb_wz_step_test": "测试消息",
    "onb_wz_next": "下一步",
    "onb_wz_prev": "上一步",
    "onb_wz_skip": "先跳过",
    "onb_wz_finish": "完成向导",
    "onb_wz_refresh": "刷新",
    "onb_wz_loading": "加载中…",
    "onb_wz_err": "组件加载失败，请刷新页面重试",
    "onb_wz_completed_note": "向导已完成过；登录后不会再自动进入本页。",
    # ① 授权 / 试用
    "onb_wz_lic_title": "① 授权 / 试用状态",
    "onb_wz_lic_hint": "已有授权码可直接粘贴激活；没有也可以先用首装体验额度往下走。",
    "onb_wz_lic_licensed": "已授权",
    "onb_wz_lic_trial": "体验中",
    "onb_wz_lic_missing": "未授权",
    "onb_wz_lic_plan": "档位：",
    "onb_wz_lic_hours_left": "体验额度剩余约 {n} 小时",
    "onb_wz_lic_paste_ph": "粘贴授权码（可选）",
    "onb_wz_lic_activate": "激活",
    "onb_wz_lic_activated": "激活成功！",
    "onb_wz_lic_act_fail": "激活失败：",
    "onb_wz_lic_trial_cta": "还没有授权？去官网领取试用 →",
    # ② 渠道
    "onb_wz_ch_title": "② 绑一个聊天渠道",
    "onb_wz_ch_hint": "点「去接入」会打开渠道接入向导（新标签页）；扫码/填凭证完成后回到这里点「刷新」。",
    "onb_wz_ch_ready": "已就绪",
    "onb_wz_ch_not_ready": "待接入",
    "onb_wz_ch_open": "去接入",
    "onb_wz_ch_none": "暂无可接入渠道（请检查授权渠道位）",
    # ③ 人设
    "onb_wz_ps_title": "③ 挑一个 AI 人设",
    "onb_wz_ps_hint": "从内置模板（陪聊/商务/客服/导师/导购）快速建一个人设；之后随时可在「人设工作室」细调。",
    "onb_wz_ps_count": "当前已有 {n} 个人设",
    "onb_wz_ps_create": "从模板新建人设",
    "onb_wz_ps_studio": "打开人设工作室",
    "onb_wz_ps_created": "人设已创建！本步已完成。",
    # ④ 自动化档位
    "onb_wz_au_title": "④ AI 自动化档位",
    "onb_wz_au_hint": "决定客户消息进来后 AI 做到哪一步。选保守档随时可以再升；全自动发送有服务端护栏兜底。",
    "onb_wz_au_manual": "🙋 全人工",
    "onb_wz_au_manual_d": "AI 不出手，所有消息由坐席亲自回复。",
    "onb_wz_au_review": "📝 AI 拟稿，人审后发",
    "onb_wz_au_review_d": "AI 先写好草稿，坐席点「通过」才发出。最常用的稳妥档。",
    "onb_wz_au_auto": "🚀 全自动",
    "onb_wz_au_auto_d": "AI 直接回复客户；出站安全闸与额度护栏仍然在场。",
    "onb_wz_au_apply": "应用此档位",
    "onb_wz_au_applied": "档位已生效。",
    "onb_wz_au_blocked": "部分开关被护栏拦下：",
    "onb_wz_au_current": "当前档位：",
    # ⑤ 测试消息
    "onb_wz_ts_title": "⑤ 发一条测试消息",
    "onb_wz_ts_hint": "发到你自己账号的「收藏消息」（Saved Messages），不打扰任何客户——发出即证明整条链路通了。",
    "onb_wz_ts_send": "发送测试消息",
    "onb_wz_ts_sent": "已发出！去 Telegram 的「收藏消息」里看一眼吧。",
    "onb_wz_ts_fail": "发送失败：",
    "onb_wz_ts_no_account": "暂无可用的 Telegram 协议账号（先在上一步接入渠道）",
    "onb_wz_ts_default_text": "你好，这是一条来自智聊的测试消息 🎉",
    # 完成页
    "onb_wz_fin_title": "配置完成！",
    "onb_wz_fin_body": "去工作台开始接待客户吧。这里的每一项之后都能在对应设置页里再调整。",
    "onb_wz_fin_workspace": "进入工作台",
}

EN = {
    "onb_ws_desc": "Reply to customer messages from every channel in one place",
    "onb_care_desc": "The system keeps track of who to check in with, and when",
    "onb_rps_desc": "Tune how fast and in what style the AI replies for you",

    # ── /welcome first-run wizard (WP-2) ─────────────────────────────────
    "onb_wz_title": "Getting started",
    "onb_wz_subtitle": "Five steps: activate a license → connect a channel → pick a persona → choose an automation tier → send a test message. Every step can be skipped or revisited; progress is saved if you leave.",
    "onb_wz_step_license": "License",
    "onb_wz_step_channel": "Channel",
    "onb_wz_step_persona": "Persona",
    "onb_wz_step_automation": "Automation",
    "onb_wz_step_test": "Test message",
    "onb_wz_next": "Next",
    "onb_wz_prev": "Back",
    "onb_wz_skip": "Skip for now",
    "onb_wz_finish": "Finish",
    "onb_wz_refresh": "Refresh",
    "onb_wz_loading": "Loading…",
    "onb_wz_err": "Component failed to load; please refresh the page",
    "onb_wz_completed_note": "This wizard was already completed; sign-in will no longer land here automatically.",
    # 1) license / trial
    "onb_wz_lic_title": "1) License / trial status",
    "onb_wz_lic_hint": "Paste a license key if you have one; otherwise you can continue on the built-in starter allowance.",
    "onb_wz_lic_licensed": "Licensed",
    "onb_wz_lic_trial": "Trial",
    "onb_wz_lic_missing": "No license",
    "onb_wz_lic_plan": "Plan:",
    "onb_wz_lic_hours_left": "About {n} hours of trial allowance left",
    "onb_wz_lic_paste_ph": "Paste license key (optional)",
    "onb_wz_lic_activate": "Activate",
    "onb_wz_lic_activated": "Activated!",
    "onb_wz_lic_act_fail": "Activation failed:",
    "onb_wz_lic_trial_cta": "No license yet? Claim a trial on the website →",
    # 2) channel
    "onb_wz_ch_title": "2) Connect a chat channel",
    "onb_wz_ch_hint": "\u201cConnect\u201d opens the channel setup wizard in a new tab; after scanning / entering credentials, come back and hit Refresh.",
    "onb_wz_ch_ready": "Ready",
    "onb_wz_ch_not_ready": "Not connected",
    "onb_wz_ch_open": "Connect",
    "onb_wz_ch_none": "No channels available (check licensed channel slots)",
    # 3) persona
    "onb_wz_ps_title": "3) Pick an AI persona",
    "onb_wz_ps_hint": "Create one quickly from the built-in templates (companion / business / support / mentor / sales); fine-tune it any time in Persona Studio.",
    "onb_wz_ps_count": "{n} persona(s) configured",
    "onb_wz_ps_create": "New persona from template",
    "onb_wz_ps_studio": "Open Persona Studio",
    "onb_wz_ps_created": "Persona created! This step is done.",
    # 4) automation tier
    "onb_wz_au_title": "4) AI automation tier",
    "onb_wz_au_hint": "Decides how far the AI goes when a customer message arrives. Start conservative and upgrade any time; full-auto sending is still protected by server-side guards.",
    "onb_wz_au_manual": "🙋 Fully manual",
    "onb_wz_au_manual_d": "The AI stays out; agents reply to every message themselves.",
    "onb_wz_au_review": "📝 AI drafts, human approves",
    "onb_wz_au_review_d": "The AI writes a draft; nothing is sent until an agent approves. The most popular safe tier.",
    "onb_wz_au_auto": "🚀 Fully automatic",
    "onb_wz_au_auto_d": "The AI replies to customers directly; outbound safety gates and quota guards stay active.",
    "onb_wz_au_apply": "Apply this tier",
    "onb_wz_au_applied": "Tier applied.",
    "onb_wz_au_blocked": "Some switches were blocked by guards:",
    "onb_wz_au_current": "Current tier:",
    # 5) test message
    "onb_wz_ts_title": "5) Send a test message",
    "onb_wz_ts_hint": "Sends to your own account's Saved Messages — no customer is disturbed, and a delivered message proves the whole chain works.",
    "onb_wz_ts_send": "Send test message",
    "onb_wz_ts_sent": "Sent! Check Saved Messages in your Telegram.",
    "onb_wz_ts_fail": "Send failed:",
    "onb_wz_ts_no_account": "No Telegram protocol account available (connect a channel in the previous step)",
    "onb_wz_ts_default_text": "Hello! This is a test message from ChatX 🎉",
    # finish
    "onb_wz_fin_title": "All set!",
    "onb_wz_fin_body": "Head to the workspace and start talking to customers. Everything here can be adjusted later in its own settings page.",
    "onb_wz_fin_workspace": "Open workspace",
}
