# -*- coding: utf-8 -*-
"""实施96（2026-09-08）：平台回复窗 / 每轮配额与渠道出站策略的 composer 可见面文案。

数据源 ``/api/unified-inbox/send-caps?chat_key=`` 的 ``reply_window``（``src/inbox/window_guard``）与
``outbound_policy``（``src/inbox/channel_policy``）；渲染在 ``unified_inbox.html`` ``_renderReplyWindow`` /
``_renderPolicyHint``。占位符：{plat} 平台名、{left} 剩余时长、{remaining}/{cap} 条数、{h} 窗口小时、{n} 数量。
"""

ZH = {
    "inbox.rw.open": "{plat} 回复窗剩余 {left} · 本轮还可发 {remaining}/{cap} 条",
    "inbox.rw.reserve": "自动回复会给你留 {n} 条",
    "inbox.rw.expired": "{plat} 回复窗已关闭（距对方最后一条已超 {h} 小时），等客户再发言后重新打开",
    "inbox.rw.no_inbound": "{plat} 只允许回复先发消息的客户，对方还没开口，暂时发不了",
    "inbox.rw.dur_d": "{d} 天 {h} 小时",
    "inbox.rw.dur_h": "{h} 小时 {m} 分",
    "inbox.rw.dur_m": "{m} 分钟",
    "inbox.rw.dur_lt1m": "不到 1 分钟",
    "inbox.policy.link_hint": "{plat} 私信不允许带外部链接，发送会被拦——请删掉链接（抖音可改发留资卡 / 问题引导卡）",
    "inbox.policy.len_hint": "已 {n} 字，超过 {plat} 单条上限 {max} 字，发送会被拦，请拆短",
}

EN = {
    "inbox.rw.open": "{plat} reply window: {left} left · {remaining}/{cap} messages left this turn",
    "inbox.rw.reserve": "auto-reply keeps {n} for you",
    "inbox.rw.expired": "{plat} reply window closed ({h}h since the customer's last message); it reopens when they write again",
    "inbox.rw.no_inbound": "{plat} only allows replying to customers who message first; they haven't yet",
    "inbox.rw.dur_d": "{d}d {h}h",
    "inbox.rw.dur_h": "{h}h {m}m",
    "inbox.rw.dur_m": "{m}m",
    "inbox.rw.dur_lt1m": "under a minute",
    "inbox.policy.link_hint": "{plat} DMs do not allow external links; sending will be blocked — remove the link (on Douyin, send a lead card / question card instead)",
    "inbox.policy.len_hint": "{n} chars exceeds {plat}'s per-message limit of {max}; sending will be blocked — split it",
}
