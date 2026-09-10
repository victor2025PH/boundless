# -*- coding: utf-8 -*-
"""工作时间班表·休息期扣留可视（Q-4 #267 D-Q1，2026-09-10）。

会话列表「作息外 · 到点重新拟稿」标签由 unified_inbox_read_routes._enrich_chat_list
读侧计算、按请求语言取词（work_hours_gate.OFF_HOURS_HOLD_TAG_KEY），不落库。
"""

ZH = {
    "inbox.conv.off_hours_hold": "作息外 · 到点重新拟稿",
    "inbox.conv.off_hours_hold_t": "该账号此刻在班表休息期（{tz}），AI 草稿已扣住不发；{until} 复班后作废重拟再发。坐席手动发送不受限。",
}

EN = {
    "inbox.conv.off_hours_hold": "Off hours · redrafts at shift start",
    "inbox.conv.off_hours_hold_t": "This account is outside its roster hours ({tz}); the AI draft is held. It will be discarded and redrafted when the shift starts at {until}. Manual sends are not affected.",
}
