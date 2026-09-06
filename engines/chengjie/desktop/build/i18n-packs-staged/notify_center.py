# -*- coding: utf-8 -*-
"""统一通知总线词条（实施75 P0，2026-08-27）。

独立成 pack（照 loading_overlay / alert_link_ops 先例）：避开被并行工作流
高频编辑的 inbox_workspace 大 pack。消费方：workspace_base.html（重启冷却 /
AI 降级迁到右下 toast + 状态胶囊 + 消息中心）与 notify-bus.js 宿主调用。

文案纪律（实施75 §3.1）：客户可见面禁运维黑话（「连环重启」「冷却保护」
「熔断」等不得出现），句式＝「发生了什么 + 要不要做什么」。
旧键 ws.restartcool.text / .hint 仍留在 inbox_workspace.py（已无消费方，
待该文件无并行编辑窗口时回收）。
"""

ZH = {
    "ntf.type_sys": "系统状态",
    "ntf.capsule_title": "系统状态 · 点击查看通知中心",
    "ntf.rc_toast": "系统刚完成维护，正在预热，约 {n} 分钟后恢复最佳状态；期间偶发加载慢属正常，无需刷新。",
    "ntf.rc_capsule": "维护预热中 · 约 {n} 分钟",
    "ntf.rc_notif": "系统完成了一次维护，预热约 {n} 分钟，期间偶发加载慢属正常。",
    "ntf.ai_capsule": "AI 备用通道顶班中",
    "ntf.ai_recovered": "云端 AI 已恢复，回复速度回到正常。",
    # P1-batch2（2026-08-27）：消息中心「进行中」置顶分组（胶囊明细面）
    "ntf.ongoing_hdr": "进行中",
    # batch3：通道离线重登等待卡（原横幅按钮上的逐秒计时改为一次性等待卡）
    "ntf.chan_relogin_wait": "已请求重新登录，正在联系服务器…（最长约 1 分钟，结果会在右下角提示）",
    # #196（L-3 C）：右下角同屏最多一张告警卡，第二项到来即折成汇总卡，点开进通知中心
    "ntf.summary_n": "有 {n} 项需要注意 ›",
}

EN = {
    "ntf.type_sys": "System status",
    "ntf.capsule_title": "System status · click to open notifications",
    "ntf.rc_toast": "Maintenance just finished — warming up, back to full speed in about {n} min. Occasional slow loads are normal; no need to refresh.",
    "ntf.rc_capsule": "Warming up · ~{n} min",
    "ntf.rc_notif": "A maintenance restart just completed; warming up for about {n} min. Occasional slow loads are normal.",
    "ntf.ai_capsule": "AI running on backup channel",
    "ntf.ai_recovered": "Cloud AI has recovered — reply speed is back to normal.",
    "ntf.ongoing_hdr": "Ongoing",
    "ntf.chan_relogin_wait": "Re-login requested — contacting the server… (up to ~1 min; the result will appear at the bottom right)",
    "ntf.summary_n": "{n} items need attention ›",
}
