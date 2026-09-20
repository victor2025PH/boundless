# -*- coding: utf-8 -*-
"""收件箱「新账号接管确认」横幅词条（inbox.am.* 前缀，P0 2026-08-30）。

配套后端 ``/api/reply-settings/account-modes``：新登录账号在确认 AI 接管方式
（全自动/拟稿人审/关闭）前按拟稿人审运行；收件箱顶部横幅是坐席侧提醒面。
"""

ZH = {
    "inbox.am.banner_one": "🆕 新账号 {name} 待选 AI 接管方式（确认前按「拟稿人审」运行，AI 只写稿不发送）",
    "inbox.am.banner_more": "还有 {m} 个待确认",
    "inbox.am.mode_auto": "🚀 全自动",
    "inbox.am.mode_review": "📝 拟稿人审 · 推荐",
    "inbox.am.mode_manual": "✋ 关闭",
    "inbox.am.m_auto": "全自动",
    "inbox.am.m_review": "拟稿人审",
    "inbox.am.m_manual": "关闭",
    "inbox.am.all": "全部账号 →",
    "inbox.am.done": "✅ {name} 已设为「{mode}」",
    "inbox.am.fail": "设置失败：",
}

EN = {
    "inbox.am.banner_one": "🆕 New account {name} needs an AI takeover choice (runs as Draft & Review until confirmed — AI drafts only, never sends)",
    "inbox.am.banner_more": "{m} more awaiting",
    "inbox.am.mode_auto": "🚀 Full auto",
    "inbox.am.mode_review": "📝 Draft & review · Recommended",
    "inbox.am.mode_manual": "✋ Off",
    "inbox.am.m_auto": "Full auto",
    "inbox.am.m_review": "Draft & review",
    "inbox.am.m_manual": "Off",
    "inbox.am.all": "All accounts →",
    "inbox.am.done": "✅ {name} set to {mode}",
    "inbox.am.fail": "Save failed: ",
}
