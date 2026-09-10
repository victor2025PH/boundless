# -*- coding: utf-8 -*-
"""告警「一眼看」只读页词条（Q-14 #262 E，2026-09-10）。键族 ``og.*``。

页面 ``/ops/glance?t=<token>&to=<path>``（``ops_glance.html``）：Telegram 告警卡里的链接
落点，十分钟一次性令牌，只读——会话号 / 账号 / 最近 5 条 / 当前档，一个「在工作台打开」
按钮走正常登录。令牌过期或被改动只给一句话和「去登录」。
"""

ZH = {
    "og.title": "告警速览",
    "og.subtitle": "十分钟一次性只读页，看完即失效；要处理请在工作台打开",
    "og.conv": "会话",
    "og.account": "账号",
    "og.platform": "平台",
    "og.contact": "对方",
    "og.tier": "当前档",
    "og.tier_manual": "全人工",
    "og.tier_review": "AI 拟稿 · 人审",
    "og.tier_multi_choice": "AI 多选 · 人挑",
    "og.tier_auto_ai": "AI 自动回",
    "og.recent": "最近 5 条",
    "og.no_messages": "这条会话还没有消息记录",
    "og.dir_in": "客户",
    "og.dir_out": "我方",
    "og.no_conv": "这条告警没有指向某个会话，下面是它要打开的地址",
    "og.conv_missing": "会话 {cid} 在本机没有记录（可能已归档或属于另一实例）",
    "og.target": "目标地址",
    "og.open": "在工作台打开",
    "og.open_hint": "需要登录；登录后直接进入这条会话",
    "og.expired": "这个速览链接已过期或已经看过一次",
    "og.bad_sig": "这个速览链接不完整或被改动过",
    "og.unconfigured": "本机没有配置速览签名密钥，无法校验链接",
    "og.go_login": "去登录",
    "og.store_unavailable": "收件箱存储还没就绪，稍后再点一次",
}

EN = {
    "og.title": "Alert glance",
    "og.subtitle": "Read-only, single-use link valid for ten minutes; open the workspace to act",
    "og.conv": "Conversation",
    "og.account": "Account",
    "og.platform": "Platform",
    "og.contact": "Contact",
    "og.tier": "Current tier",
    "og.tier_manual": "Manual",
    "og.tier_review": "AI draft, human review",
    "og.tier_multi_choice": "AI options, human picks",
    "og.tier_auto_ai": "AI auto-reply",
    "og.recent": "Last 5 messages",
    "og.no_messages": "No messages recorded for this conversation yet",
    "og.dir_in": "Customer",
    "og.dir_out": "Us",
    "og.no_conv": "This alert does not point at a conversation; below is the address it opens",
    "og.conv_missing": "Conversation {cid} has no record on this instance (archived or belongs to another instance)",
    "og.target": "Target",
    "og.open": "Open in workspace",
    "og.open_hint": "Login required; lands directly on this conversation",
    "og.expired": "This glance link has expired or was already used once",
    "og.bad_sig": "This glance link is incomplete or has been altered",
    "og.unconfigured": "No glance signing key is configured on this instance, so the link cannot be checked",
    "og.go_login": "Go to login",
    "og.store_unavailable": "Inbox storage is not ready yet; try the link again shortly",
}
