# -*- coding: utf-8 -*-
"""ops-overview「📡 官方渠道回调」卡词条（webhook 可达性观测，2026-08-07）。

独立成 pack（不并入 ops_overview_page.py）：大包常被并行工作流编辑，按 i18n_packs
治理机制（自动发现、跨 pack 同 key 即抛错）拆小文件零冲突。
卡片消费方：src/web/templates/ops_overview.html::loadOfficialWebhooks；
数据源：GET /api/admin/official-webhook-status（unified_inbox_account_routes）。
"""

ZH = {
    "ov2_s_offwh": "官方渠道回调（webhook 可达性）",
    "ov2_ow_sub": "Instagram / Zalo / Messenger 官方 / WhatsApp Cloud 是被动回调入站——回调打不进来时系统零报错，客户消息静默丢失。此卡回答「对面到底打没打进来过」。",
    "ov2_ow_verdict_live": "正常（事件到达过）",
    "ov2_ow_verdict_handshake_only": "已握手 · 零事件",
    "ov2_ow_verdict_auth_failing": "有请求但从未通过验证",
    "ov2_ow_verdict_never_reached": "回调从未到达",
    "ov2_ow_verdict_not_mounted": "已启用但路由未装载",
    "ov2_ow_verdict_disabled": "未启用",
    "ov2_ow_events": "累计事件",
    "ov2_ow_last_event": "最近事件",
    "ov2_ow_last_verify": "最近握手",
    "ov2_ow_errors": "验签/格式错误",
    "ov2_ow_verify_fails": "握手失败",
    "ov2_ow_path": "回调路径",
    "ov2_ow_never": "从未",
    "ov2_ow_hint_never_reached": "检查：公网回调 URL 是否指到本机、隧道/反代是否在跑、开发者后台是否配置并订阅了该回调",
    "ov2_ow_hint_handshake_only": "握手已通（公网可达）。零事件多为正常冷启动；持续为零请检查开发者后台的订阅字段（messages 等）是否勾选",
    "ov2_ow_hint_auth_failing": "有请求到达（公网可达）但没有一次通过验证：多为 verify_token / app_secret 配错；也可能只是外部扫描噪声——若正在接入请核对凭证",
    "ov2_ow_hint_not_mounted": "凭证缺失，或启动后才填的凭证：webhook 路由在下次实例重启时装载",
}

EN = {
    "ov2_s_offwh": "Official channel webhooks (callback reachability)",
    "ov2_ow_sub": "Instagram / Zalo / official Messenger / WhatsApp Cloud receive inbound via passive callbacks — when callbacks can't reach us the system reports nothing and customer messages are silently lost. This card answers: has the other side ever reached us?",
    "ov2_ow_verdict_live": "Live (events received)",
    "ov2_ow_verdict_handshake_only": "Handshake OK · no events",
    "ov2_ow_verdict_auth_failing": "Requests arrive but never pass auth",
    "ov2_ow_verdict_never_reached": "Callback never reached",
    "ov2_ow_verdict_not_mounted": "Enabled but routes not mounted",
    "ov2_ow_verdict_disabled": "Disabled",
    "ov2_ow_events": "Events total",
    "ov2_ow_last_event": "Last event",
    "ov2_ow_last_verify": "Last handshake",
    "ov2_ow_errors": "Auth/format errors",
    "ov2_ow_verify_fails": "Handshake failures",
    "ov2_ow_path": "Callback path",
    "ov2_ow_never": "never",
    "ov2_ow_hint_never_reached": "Check: does the public callback URL point at this machine, is the tunnel/reverse-proxy running, and is the callback configured & subscribed in the developer console",
    "ov2_ow_hint_handshake_only": "Handshake passed (publicly reachable). Zero events is usually a cold start; if it stays zero, check the subscription fields (messages etc.) in the developer console",
    "ov2_ow_hint_auth_failing": "Requests arrive (publicly reachable) but none ever passed verification: usually a wrong verify_token / app_secret; could also be external scanner noise — verify credentials if you are onboarding",
    "ov2_ow_hint_not_mounted": "Credentials missing, or added after boot: webhook routes mount on the next instance restart",
}
