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
    # 官方后台直达链接（数据源＝channel_setup.Channel.console_url，单一事实源；
    # 旧后端无该字段时前端不渲染此行）
    "setup.ch.console_link": "打开官方后台获取凭证 ↗",
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
    # auth_failing 的定向变体：入站被 no_app_secret 拒收（Messenger 热门控新错误类）
    # ——处置是「补一个字段」而非泛泛排查，指哪补哪
    "sw_reach_auth_nosecret": "⚠ 渠道已启用但缺 App Secret——入站事件被拒收。在上方表单补填 App Secret 并保存即可，无需重启",
    # Messenger「保存即探针」结果横幅（数据源＝保存响应的 probe 字段，2026-08-10）
    "sw_probe_ok": "✓ 已连接主页：{name}（ID {pid}）",
    "sw_probe_auth_fail": "⚠ Page Token 校验未通过：{err}——请到 Meta 后台核对/重新生成 token 后再保存",
    "sw_probe_net_fail": "Token 校验没有完成（{err}）——凭证已保存；网络恢复后重新保存一次即可再验",
    # ── 媒体理解卡（合并回放，原在单体） ──
    "setup.media.applied": "已应用生效",
    "setup.media.apply_fail": "应用失败",
    "setup.media.applying": "正在应用…",
    "setup.media.card_sub": "让 AI 看懂对方发来的图片/语音/视频、并能按需发自拍。默认全关，点下面一键开启。",
    "setup.media.card_title": "多媒体能力",
    "setup.media.h_album": "相册 {t} 条 · 投放 {s} 次（{u} 图 / {c} 会话）",
    "setup.media.h_asr": "语音转写成功 {r}%",
    "setup.media.h_dedup": "防封号去重 {n} 次 · 回落 {k}",
    "setup.media.h_video": "视频理解成功 {r}%",
    "setup.media.h_vision": "近期识图成功 {r}% · 缓存命中 {c}%",
    "setup.media.preset_off": "全部关闭",
    "setup.media.preset_understand": "一键开齐入站识别",
    "setup.media.preset_understand_selfie": "识别 + 自动发自拍",
    "setup.media.st_active": "已开启",
    "setup.media.st_off": "未开启",
    "setup.media.st_partial": "部分待配",
    "setup.media.stage_active": "已开且后端就绪",
    "setup.media.stage_needs_backend": "已开但缺后端",
    "setup.media.stage_off": "未开启",
    "setup.media.warn_title": "已开开关，但以下后端还没配好（可先开、后补）：",
}

EN = {
    "setup.ch.path_login_title": "Use your own account · recommended",
    "setup.ch.path_login_cta": "Connect an account",
    "setup.ch.path_login_accounts": "{n} account(s) connected",
    "setup.ch.path_login_none": "No account connected yet",
    "setup.ch.path_login_blocked": "Unavailable right now — open it to see exactly what's missing",
    "setup.ch.path_api_title": "Use the official business API instead (developer account required)",
    "setup.ch.path_api_prereq": "Requires an app and callback URL in the platform's developer console. Most customers don't need this.",
    "setup.ch.console_link": "Open the official console to get credentials ↗",
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
    # Targeted auth_failing variant: inbound rejected due to missing app_secret
    "sw_reach_auth_nosecret": "⚠ Channel enabled but App Secret is missing — inbound events are rejected. Fill in App Secret in the form above and save; no restart needed",
    # Messenger save-time probe result banner (from the save response `probe` field)
    "sw_probe_ok": "✓ Page connected: {name} (ID {pid})",
    "sw_probe_auth_fail": "⚠ Page token failed validation: {err} — double-check / regenerate the token in the Meta console, then save again",
    "sw_probe_net_fail": "Token validation didn't complete ({err}) — credentials are saved; save again once the network recovers to re-validate",
    # ── media understanding card (merge replay) ──
    "setup.media.applied": "Applied",
    "setup.media.apply_fail": "Apply failed",
    "setup.media.applying": "Applying…",
    "setup.media.card_sub": "Let the AI understand incoming images/voice/video and send selfies on request. Off by default — enable with one click below.",
    "setup.media.card_title": "Multimedia capabilities",
    "setup.media.h_album": "Album {t} items · {s} sends ({u} media / {c} convs)",
    "setup.media.h_asr": "Voice transcription OK {r}%",
    "setup.media.h_dedup": "Anti-ban dedup {n} · fallback {k}",
    "setup.media.h_video": "Video understanding OK {r}%",
    "setup.media.h_vision": "Recent image OK {r}% · cache hit {c}%",
    "setup.media.preset_off": "Turn all off",
    "setup.media.preset_understand": "Enable all inbound recognition",
    "setup.media.preset_understand_selfie": "Recognition + auto selfie",
    "setup.media.st_active": "On",
    "setup.media.st_off": "Off",
    "setup.media.st_partial": "Backend pending",
    "setup.media.stage_active": "On, backend ready",
    "setup.media.stage_needs_backend": "On, backend missing",
    "setup.media.stage_off": "Off",
    "setup.media.warn_title": "Switches enabled, but these backends aren't configured yet (you can enable now, configure later):",
}
