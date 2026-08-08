# -*- coding: utf-8 -*-
"""渠道接入向导「选接入方式」词条（2026-07-31）。

背景：向导原本每个渠道只呈现一件事——填企业官方 API 的 token（LINE Messaging API /
Meta Page Token）。那是给有开发团队的企业用的集成路径，要开发者账号、要回调域名；
而产品真正的主推形态是「用你自己的账号扫码登录」，藏在收件箱侧边抽屉里。等于把
劝退项摆在首屏、把卖点埋进二级页面。这批键把两条路径显式化并调换主次。

键族：
- setup.ch.path_*   两条接入路径的标题/说明/按钮
- setup.ch.badge_*  渠道卡右上角状态（按「能不能收发消息」而非「yaml 填没填」）
- setup.ch.prog     顶部进度条口径
"""

ZH = {
    "setup.ch.path_login_title": "用你自己的账号　·　推荐",
    "setup.ch.path_login_cta": "去接入账号",
    "setup.ch.path_login_accounts": "已接入 {n} 个账号",
    "setup.ch.path_login_none": "还没有账号接进来",
    "setup.ch.path_login_blocked": "这条路当前不可用——点进去可以看到具体缺什么",
    "setup.ch.path_api_title": "改用企业官方 API（需要开发者账号）",
    "setup.ch.path_api_prereq": "需要在平台开发者后台建应用并配置回调地址，多数客户不需要走这条。",
    "setup.ch.badge_linked": "✓ 已接 {n} 个账号",
    "setup.ch.badge_api": "✓ 官方接入已就绪",
    "setup.ch.badge_on": "✓ 已开启",
    "setup.ch.badge_none": "未接入",
    "setup.ch.enable_cta": "立即启用",
    "setup.ch.prog": "能收发消息的渠道 {r} / {t}",
    "setup.ch.prog_hint": "按「现在能不能收发消息」统计：登录了账号，或官方接入凭据已就绪。",
    # official 媒体 URL 状态条（IG/LINE 官方通道发图/语音的前置；P1，2026-08-05）
    "sw_media_probe": "检测可达性",
    "sw_media_probing": "检测中…",
    "sw_media_unset": "提示：{chs} 官方通道要发图/语音，需在渠道表单里填「公网媒体 URL」（当前仅能发文字）。",
    "sw_media_set": "公网媒体 URL：{url}",
    "sw_media_ok": "本机可达 ✓（注意：平台侧最终以公网可达为准）",
    "sw_media_fail": "不可达（{why}）——检查域名 / 隧道 / TLS；平台将拉取失败",
    # 官方 webhook 回调状态条（接入向导内嵌握手反馈；数据源 /api/admin/official-webhook-status）
    "sw_reach_title": "回调状态",
    "sw_reach_live": "✓ 回调已通 · 最近事件 {age} 前",
    "sw_reach_handshake": "✓ 平台握手成功（公网可达）· 还没有消息事件——持续为零请检查开发者后台的订阅字段（messages 等）",
    "sw_reach_never": "等待平台回调握手… 检查：公网回调 URL 是否指到本机、隧道/反代是否在跑、开发者后台是否已配置回调",
    "sw_reach_auth": "⚠ 有请求到达但从未通过验证——核对 verify_token / app_secret（也可能只是外部扫描噪声）",
    "sw_reach_not_mounted": "凭证已保存 · 回调路由将在下次实例重启时装载，装载后此处自动更新",
    "sw_reach_path": "回调路径：{path}",
}

EN = {
    "setup.ch.path_login_title": "Use your own account · recommended",
    "setup.ch.path_login_cta": "Connect an account",
    "setup.ch.path_login_accounts": "{n} account(s) connected",
    "setup.ch.path_login_none": "No account connected yet",
    "setup.ch.path_login_blocked": "Unavailable right now — open it to see exactly what's missing",
    "setup.ch.path_api_title": "Use the official business API instead (developer account required)",
    "setup.ch.path_api_prereq": "Requires an app and callback URL in the platform's developer console. Most customers don't need this.",
    "setup.ch.badge_linked": "✓ {n} account(s)",
    "setup.ch.badge_api": "✓ Official API ready",
    "setup.ch.badge_on": "✓ On",
    "setup.ch.badge_none": "Not connected",
    "setup.ch.enable_cta": "Enable now",
    "setup.ch.prog": "Channels that can send & receive: {r} / {t}",
    "setup.ch.prog_hint": "Counted by what actually works: an account is logged in, or official API credentials are ready.",
    # Official media URL status bar (prerequisite for IG/LINE official media sends)
    "sw_media_probe": "Check reachability",
    "sw_media_probing": "Checking…",
    "sw_media_unset": "Note: the official channels for {chs} need a public media URL (form field on the channel card) to send images/voice — text only until then.",
    "sw_media_set": "Public media URL: {url}",
    "sw_media_ok": "Reachable from this server ✓ (final say is public reachability from the platform side)",
    "sw_media_fail": "Unreachable ({why}) — check domain / tunnel / TLS; the platform will fail to fetch",
    # Official webhook callback status strip (inline handshake feedback in the wizard)
    "sw_reach_title": "Callback status",
    "sw_reach_live": "✓ Callback live · last event {age} ago",
    "sw_reach_handshake": "✓ Platform handshake OK (publicly reachable) · no message events yet — if it stays zero, check the subscription fields (messages etc.) in the developer console",
    "sw_reach_never": "Waiting for the platform handshake… Check: the public callback URL points at this machine, the tunnel/reverse-proxy is running, and the callback is configured in the developer console",
    "sw_reach_auth": "⚠ Requests arrive but never pass verification — double-check verify_token / app_secret (could also be external scanner noise)",
    "sw_reach_not_mounted": "Credentials saved · callback routes mount on the next instance restart; this strip updates automatically once live",
    "sw_reach_path": "Callback path: {path}",
}
