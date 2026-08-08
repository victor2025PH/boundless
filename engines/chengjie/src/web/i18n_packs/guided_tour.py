# -*- coding: utf-8 -*-
"""「新手交互引导」词条（P1，2026-08-07）。

独立成 pack（照 loading_overlay / tenant_ops_card 先例），避开被并行工作流
频繁编辑的大 pack。消费方：templates/_guided_tour.html + workspace_dashboard.html
的首登引导卡（引导入口只在清单非绿时出现，配置完整的实例零打扰）。
"""

ZH = {
    "gtour_start": "开始引导",
    "gtour_next": "下一步",
    "gtour_prev": "上一步",
    "gtour_skip": "跳过引导",
    "gtour_done": "完成",
    "gtour_welcome_t": "欢迎！三步就能让 AI 替你接待客户",
    "gtour_welcome_b": "我会逐步指给你「下一步点哪」。想先看看 AI 说话的效果？点下面按钮，"
                       "在人设里点任意人设 →「💬 试聊」，不接真实渠道也能先聊两句。",
    "gtour_try_btn": "先看 AI 效果（新标签打开）",
    "gtour_cfg_hint": "点这一行右侧的按钮去完成它；做完这里会自动变绿。",
    "gtour_final_t": "就这些，去开工吧",
    "gtour_final_b": "随时可以点这里查看完整的上线自检清单。全部变绿＝你已上线，"
                     "客户消息会进「聊天」工作台。",
    "gtour_none_t": "没有待办了 🎉",
    "gtour_none_b": "你的上线清单已经没有待处理项。",
}

EN = {
    "gtour_start": "Start guide",
    "gtour_next": "Next",
    "gtour_prev": "Back",
    "gtour_skip": "Skip guide",
    "gtour_done": "Done",
    "gtour_welcome_t": "Welcome! Three steps to let AI serve your customers",
    "gtour_welcome_b": "I'll point out exactly what to click next. Want to see the AI talk "
                       "first? Open Personas, pick any persona → “💬 Test chat” — no channel needed.",
    "gtour_try_btn": "See the AI first (opens new tab)",
    "gtour_cfg_hint": "Click the button on the right of this row to complete it — it turns green when done.",
    "gtour_final_t": "That's it — you're set",
    "gtour_final_b": "Open the full go-live checklist here anytime. All green means you're live, "
                     "and customer messages land in the Chat workspace.",
    "gtour_none_t": "Nothing pending 🎉",
    "gtour_none_b": "Your go-live checklist has no outstanding items.",
}
