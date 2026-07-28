# -*- coding: utf-8 -*-
"""渠道中心（/workspace/channels/*）页面词条。

四个旧后台渠道页（telegram / line-rpa / messenger-rpa / whatsapp-rpa）融合进
工作台壳后的页头/导航词条；正文词条仍在各自渠道域 pack
（telegram_page / line_page / messenger_page / whatsapp_page）。
"""

ZH = {
    "chc_title": "渠道中心",
    "chc_sub": "账号 · 自动化 · 语音 · 媒体 · 观测 —— 四个渠道一站式配置",
    "chc_overview_link": "跨平台总览",
    "chc_admin_link": "管理后台",
    "base.nav.channels": "渠道中心",
    "base.nav.channels_t": "渠道中心：四渠道账号与自动化配置（主管）",
    # 职责边界引导条（P1-5）：本页管什么 / 其他设置去哪改
    "chc_note_lead": "本页管平台级配置（账号进程 · 接收规则 · 回复节奏 · 语音链路 · 运维）。常找的其他设置：",
    "chc_note_conv": "单个客户的自动化档位 → 聊天坐席",
    "chc_note_persona": "每个人设的音色 → 人设工作室",
    "chc_note_gate": "AI 真发总闸 → 能力看板",
    # 收件箱封顶/跳过徽章（P0：inbox.auto_draft.platform_modes / skip_platforms 可见化）
    "chc_cap_capped": "本渠道已被收件箱封顶为 {mode} · 全自动不生效",
    "chc_cap_skipped": "本渠道已被收件箱跳过自动拟稿",
    # 网页会话健康条（P0：messenger/whatsapp 外部 worker 会话状态 + 一键重登）
    "chc_sess_ok_line": "网页会话正常（{n} 个账号）",
    "chc_sess_bad_title": "网页会话异常（{bad}/{total} 个账号掉线）",
    "chc_sess_down_for": "已掉线 {t}",
    "chc_sess_relogin_btn": "重新登录",
    "chc_sess_relogin_ok": "已触发重登，30 分钟内在 worker 主机完成登录",
    "chc_sess_relogin_fail": "触发重登失败",
    "chc_sess_wa_hint": "请到聊天坐席的账号面板重新扫码",
    "chc_sess_wa_link": "去聊天坐席",
    "chc_sess_age_sec": "{n} 秒",
    "chc_sess_age_min": "{n} 分钟",
    "chc_sess_age_hour": "{n} 小时",
    "chc_sess_age_day": "{n} 天",
}

EN = {
    "chc_title": "Channel Center",
    "chc_sub": "Accounts, automation, voice, media & observability — all four channels in one place",
    "chc_overview_link": "Cross-platform overview",
    "chc_admin_link": "Admin console",
    "base.nav.channels": "Channel Center",
    "base.nav.channels_t": "Channel Center: accounts & automation for all four channels (supervisor)",
    "chc_note_lead": "This page manages platform-level config (account process, inbound rules, reply pacing, voice chain, ops). Looking for something else?",
    "chc_note_conv": "Per-customer automation level → Agent Workspace",
    "chc_note_persona": "Per-persona voice → Persona Studio",
    "chc_note_gate": "Global AI auto-send gate → Capability Board",
    "chc_cap_capped": "Inbox has capped this channel at {mode} — full-auto is disabled here",
    "chc_cap_skipped": "Inbox skips auto-drafting for this channel",
    "chc_sess_ok_line": "Web sessions healthy ({n} accounts)",
    "chc_sess_bad_title": "Web session issues ({bad}/{total} accounts down)",
    "chc_sess_down_for": "down for {t}",
    "chc_sess_relogin_btn": "Re-login",
    "chc_sess_relogin_ok": "Re-login triggered — complete the login on the worker host within 30 minutes",
    "chc_sess_relogin_fail": "Failed to trigger re-login",
    "chc_sess_wa_hint": "Re-scan the QR code from the account panel in the Agent Workspace",
    "chc_sess_wa_link": "Open Agent Workspace",
    "chc_sess_age_sec": "{n}s",
    "chc_sess_age_min": "{n} min",
    "chc_sess_age_hour": "{n} h",
    "chc_sess_age_day": "{n} d",
}
