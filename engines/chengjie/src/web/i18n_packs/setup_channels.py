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
}
