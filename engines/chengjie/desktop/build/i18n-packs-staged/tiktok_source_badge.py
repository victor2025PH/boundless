# -*- coding: utf-8 -*-
"""TikTok 会话来源徽标文案（TK-3 E3，2026-09-11）：官方 / 真机 / 网页 + 个人号「消息请求 · 对方未回」。

服务端 ``src/integrations/tiktok_source_badge.py`` 按账号 mode 打 ``tiktok_source``；前端 ``_convTikTokTags``。
不宣称官方个人号私信：真机 / 网页两枚徽标的悬停文案都写明「非官方」。
"""

ZH = {
    "inbox.tt.src_official": "官方",
    "inbox.tt.src_official_t": "TikTok Business Messaging 官方接口进来的私信（Business Account）",
    "inbox.tt.src_rpa": "真机",
    "inbox.tt.src_rpa_t": "获客真机（huoke）读到的个人号私信 / 评论线索；非官方通道，智聊起草、真机发出",
    "inbox.tt.src_web": "网页",
    "inbox.tt.src_web_t": "网页托管边车读到的个人号私信；非官方通道，只读不代发",
    "inbox.tt.never_replied": "对方未回",
    "inbox.tt.never_replied_t": "消息请求：对方还没回过这条私信。智聊不会代发——等客户先开口，或让获客真机做首触",
}

EN = {
    "inbox.tt.src_official": "Official",
    "inbox.tt.src_official_t": "DM received via the TikTok Business Messaging API (Business Account)",
    "inbox.tt.src_rpa": "Phone",
    "inbox.tt.src_rpa_t": "Personal-account DM / comment lead read by the capture phone (huoke); unofficial — ChatX drafts, the phone sends",
    "inbox.tt.src_web": "Web",
    "inbox.tt.src_web_t": "Personal-account DM read by the web-hosted sidecar; unofficial, read-only",
    "inbox.tt.never_replied": "No reply yet",
    "inbox.tt.never_replied_t": "Message request: they have not replied to this DM. ChatX will not send first — wait for them, or let the phone bot do the first touch",
}

ZH_HANT = {
    "inbox.tt.src_official": "官方",
    "inbox.tt.src_official_t": "TikTok Business Messaging 官方介面進來的私信（Business Account）",
    "inbox.tt.src_rpa": "真機",
    "inbox.tt.src_rpa_t": "獲客真機（huoke）讀到的個人號私信 / 評論線索；非官方通道，智聊起草、真機發出",
    "inbox.tt.src_web": "網頁",
    "inbox.tt.src_web_t": "網頁託管邊車讀到的個人號私信；非官方通道，只讀不代發",
    "inbox.tt.never_replied": "對方未回",
    "inbox.tt.never_replied_t": "訊息請求：對方還沒回過這條私信。智聊不會代發——等客戶先開口，或讓獲客真機做首觸",
}
