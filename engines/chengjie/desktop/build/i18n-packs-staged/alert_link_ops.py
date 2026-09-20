# -*- coding: utf-8 -*-
"""ops-overview「🔔 告警链路」卡词条（告警最后一公里自检可视化，2026-08-02）。

独立成 pack（而非并入 ops_overview_page.py）：该大包正被并行工作流编辑，
按 i18n_packs 治理机制（自动发现、跨 pack 同 key 即抛错）拆小文件零冲突。
卡片消费方：src/web/templates/ops_overview.html::loadAlertLink；
数据源：GET /api/admin/alert-link-status（unified_inbox_account_routes）。
"""

ZH = {
    "ov2_s_alertlink": "告警链路（最后一公里自检）",
    "ov2_al_verdict_healthy": "已接通",
    "ov2_al_verdict_no_channel": "未接通（0 个启用通道）",
    "ov2_al_verdict_uncovered": "部分覆盖（有别名没人订阅）",
    "ov2_al_verdict_divergent": "文件与进程不一致",
    "ov2_al_verdict_not_running": "通知器未在消费事件",
    "ov2_al_channels": "启用通道",
    "ov2_al_cover": "关注别名覆盖",
    "ov2_al_sent": "本进程已外发",
    "ov2_al_errors": "投递失败",
    "ov2_al_uncovered_label": "未覆盖别名",
    "ov2_al_store": "配置落点",
    "ov2_al_src_overlay": "覆盖层（面板可管）",
    "ov2_al_src_config": "config.yaml（建议迁到面板）",
    "ov2_al_src_none": "无任何来源",
    "ov2_al_orphan_warn": "引擎根遗留同名配置文件（服务读不到那份）——请清理，防运营误编辑",
    "ov2_al_divergent_hint": "磁盘配置与运行中通知器装载的不是同一份：经面板重新保存可热更；手改文件需重启才生效",
    "ov2_al_cta": "接通步骤：工作台副驾「告警渠道」面板填 Telegram bot token + chat_id，保存即热更；验收可跑 python tools/alert_link_selfcheck.py",
    "ov2_al_deliver_stats": "投递统计",
    "ov2_al_connect_btn": "立即接通",
    # ── 最后一公里接通部件（_alertlink_connect.html：工作台横幅 + 一键弹窗）──
    "ws.alertlink.text": "告警通道未接通：账号掉线、收不到消息、草稿积压等故障不会通知到任何人，只能靠人盯页面。",
    "ws.alertlink.connect": "立即接通",
    "ws.alertlink.later": "24 小时内不再提醒",
    "al.ct.title": "接通告警通道",
    "al.ct.desc": "接一个 Telegram 机器人：账号掉线、收不到消息、草稿积压等故障会主动推送给你。保存即生效，无需重启。",
    "al.ct.how": "① Telegram 搜 @BotFather → /newbot 创建机器人，得到 Token；② 用要收告警的账号先给这个机器人发一句话（否则它无法主动私聊你）；③ Chat ID 可发消息给 @userinfobot 查到。",
    "al.ct.token_ph": "Bot Token（形如 123456:ABC-…）",
    "al.ct.chat_ph": "Chat ID（个人为正数，群为负数）",
    "al.ct.test": "发送测试消息",
    "al.ct.testing": "测试中…",
    "al.ct.test_ok": "测试消息已发出，请到 Telegram 查收——收到即可保存。",
    "al.ct.test_fail": "测试失败：{err}",
    # B24（2026-08-21）测试失败原因人话映射（终结「测试失败：HTTP 200」）
    "al.ct.err.chat_not_found": (
        "没找到会话：Chat ID 不对，或你还没跟这个机器人说过话"
        "（私聊先给机器人发一条消息；群聊要先把机器人拉进群。"
        "自己的 Chat ID 可向 @userinfobot 查询）"
    ),
    "al.ct.err.bad_token": "Bot Token 无效或未填：请到 @BotFather 核对后重填",
    "al.ct.err.blocked": "机器人被对方屏蔽或已被移出群，无法投递",
    "al.ct.err.network": "连不上 Telegram 服务器：请检查本机出网/代理",
    "al.ct.err.unknown": "渠道拒收（未返回原因）：请核对 Token 与 Chat ID",
    "al.ct.save": "保存并接通",
    "al.ct.saving": "保存中…",
    "al.ct.save_ok": "已接通：关键故障将推送到该 Telegram。",
    "al.ct.save_fail": "保存失败：{err}",
    "al.ct.need_both": "请先填写 Token 和 Chat ID",
    "al.ct.need_test": "先发送测试消息、确认真能收到，再保存。",
    "al.ct.no_perm": "需要管理员权限（坐席/观察员不可配置告警渠道）。",
    "al.ct.close": "关闭",
    # ── 目标达成推送状态区（2026-08-18：弹窗内三灯——扫描器/渠道订阅/本人绑定；
    #    数据 GET /api/goals/notify-status，扫描器开关写 POST /api/goals/notify-settings。
    #    goals 总闸关（403）/旧后端（404）→ 区块整体隐藏＝特性探测不裸奔）──
    "al.ct.goal_title": "🎯 目标达成推送",
    "al.ct.goal_hint": "客户达成目标（成交/摸底完成等）时推送管理员与坐席——下面几项都绿才真有人收到。",
    "al.ct.goal_scan_on": "完成扫描器已开启（约每分钟结算一次）",
    "al.ct.goal_scan_off": "完成扫描器未开启：目标达成不会提醒任何人，只能事后翻报表",
    "al.ct.goal_scan_btn": "一键开启",
    "al.ct.goal_scan_saving": "开启中…",
    "al.ct.goal_scan_ok": "已写入配置，约 30 秒热生效",
    "al.ct.goal_scan_fail": "开启失败：{err}",
    "al.ct.goal_cover_on": "已有渠道订阅「目标达成」：{names}",
    "al.ct.goal_cover_off": "尚无渠道订阅「目标达成」——在上方接通一个 Telegram 即自动覆盖",
    "al.ct.goal_self_on": "你已绑定个人通知号：达成推送「管理员 + 你」",
    "al.ct.goal_self_off": "想自己也收一份？到目标卡通知行点「我也要收」自助绑定",
    # 推送选项三开关（2026-08-18 二批）：与 /api/goals/notify-settings 白名单
    # 一一对应——此前 UI 只露扫描器开关，这三个还得改 YAML；尤其 include_profile
    # 涉及客户画像出境到外部 IM，可见的开关才谈得上治理。
    "al.ct.goal_opt_title": "推送选项",
    "al.ct.goal_opt_agent": "坐席副本",
    "al.ct.goal_opt_agent_t": "开：目标达成时，除管理员渠道外，再给「建目标/认领会话的坐席」绑定的 Telegram 抄送一份（坐席未绑定则自然只推管理员）",
    "al.ct.goal_opt_miss": "失守日报",
    "al.ct.goal_opt_miss_t": "开：失败/到期的目标聚合成一条日报推送（攒够条数或最老超一天才出账，绝不逐条轰炸）",
    "al.ct.goal_opt_profile": "推送带摸底要点",
    "al.ct.goal_opt_profile_t": "开：达成推送附一行已采画像事实（称呼/职业/预算等）——客户数据会出境到你的 IM，确认合规再开；关：只推模板/金额/用时等目标层事实",
    "al.ct.goal_opt_fail": "保存失败：{err}",
}

EN = {
    "ov2_s_alertlink": "Alert delivery link (last-mile self-check)",
    "ov2_al_verdict_healthy": "Connected",
    "ov2_al_verdict_no_channel": "Not connected (0 enabled channels)",
    "ov2_al_verdict_uncovered": "Partial coverage (some aliases unsubscribed)",
    "ov2_al_verdict_divergent": "File and process out of sync",
    "ov2_al_verdict_not_running": "Notifier is not consuming events",
    "ov2_al_channels": "Enabled channels",
    "ov2_al_cover": "Focus alias coverage",
    "ov2_al_sent": "Sent by this process",
    "ov2_al_errors": "Delivery failures",
    "ov2_al_uncovered_label": "Uncovered aliases",
    "ov2_al_store": "Config location",
    "ov2_al_src_overlay": "Overlay (panel-managed)",
    "ov2_al_src_config": "config.yaml (consider migrating to the panel)",
    "ov2_al_src_none": "No source at all",
    "ov2_al_orphan_warn": "A same-named config file is stranded at the engine root (the service never reads it) — clean it up to avoid misleading edits",
    "ov2_al_divergent_hint": "The config on disk differs from what the running notifier loaded: re-save via the panel to hot-reload; manual file edits need a restart",
    "ov2_al_cta": "To connect: fill in the Telegram bot token + chat_id in the copilot \u201cAlert channels\u201d panel; saving hot-reloads. Verify with: python tools/alert_link_selfcheck.py",
    "ov2_al_deliver_stats": "Delivery stats",
    "ov2_al_connect_btn": "Connect now",
    # ── Last-mile connect widget (_alertlink_connect.html: shell banner + modal) ──
    "ws.alertlink.text": "Alert channel not connected: account drops, unreadable inboxes and draft backlogs will notify no one — someone has to watch the screen.",
    "ws.alertlink.connect": "Connect now",
    "ws.alertlink.later": "Snooze for 24 hours",
    "al.ct.title": "Connect an alert channel",
    "al.ct.desc": "Hook up a Telegram bot: account drops, unreadable inboxes and draft backlogs get pushed to you proactively. Takes effect on save, no restart.",
    "al.ct.how": "① In Telegram, find @BotFather → /newbot to create a bot and get the Token; ② send the new bot one message from the account that should receive alerts (otherwise it cannot DM you); ③ get your Chat ID from @userinfobot.",
    "al.ct.token_ph": "Bot token (looks like 123456:ABC-…)",
    "al.ct.chat_ph": "Chat ID (positive for a user, negative for a group)",
    "al.ct.test": "Send test message",
    "al.ct.testing": "Testing…",
    "al.ct.test_ok": "Test message sent — check Telegram; once received you can save.",
    "al.ct.test_fail": "Test failed: {err}",
    "al.ct.err.chat_not_found": (
        "Chat not found: wrong Chat ID, or you haven't messaged this bot yet "
        "(DM the bot once first; for groups, add the bot to the group. "
        "Ask @userinfobot for your own Chat ID)"
    ),
    "al.ct.err.bad_token": "Invalid or missing bot token — check with @BotFather",
    "al.ct.err.blocked": "The bot is blocked or was removed from the group",
    "al.ct.err.network": "Cannot reach Telegram servers — check egress/proxy",
    "al.ct.err.unknown": "Channel rejected the message (no reason returned) — check token and Chat ID",
    "al.ct.save": "Save & connect",
    "al.ct.saving": "Saving…",
    "al.ct.save_ok": "Connected: critical incidents will be pushed to this Telegram.",
    "al.ct.save_fail": "Save failed: {err}",
    "al.ct.need_both": "Fill in both the token and the chat ID first",
    "al.ct.need_test": "Send a test message and confirm it arrives before saving.",
    "al.ct.no_perm": "Admin permission required (agents/viewers cannot configure alert channels).",
    "al.ct.close": "Close",
    # ── Goal-win push status section (2026-08-18: three lights in the modal) ──
    "al.ct.goal_title": "🎯 Goal-win push",
    "al.ct.goal_hint": "When a customer completes a goal (won / discovery finished), push admin & agents — all rows below must be green for anyone to actually receive it.",
    "al.ct.goal_scan_on": "Completion scanner is on (settles about once a minute)",
    "al.ct.goal_scan_off": "Completion scanner is off: achieved goals notify nobody — only visible in reports afterwards",
    "al.ct.goal_scan_btn": "Enable now",
    "al.ct.goal_scan_saving": "Enabling…",
    "al.ct.goal_scan_ok": "Written to config — hot-applies in ~30s",
    "al.ct.goal_scan_fail": "Enable failed: {err}",
    "al.ct.goal_cover_on": "Channels subscribed to “Goal achieved”: {names}",
    "al.ct.goal_cover_off": "No channel subscribes to “Goal achieved” yet — connect a Telegram above and it is covered automatically",
    "al.ct.goal_self_on": "You have a personal notify chat bound: wins push to “admin + you”",
    "al.ct.goal_self_off": "Want a personal copy? Use “Notify me too” on the goal card notify row",
    # Push option toggles (batch 2, 2026-08-18): map 1:1 to the
    # /api/goals/notify-settings whitelist keys
    "al.ct.goal_opt_title": "Push options",
    "al.ct.goal_opt_agent": "Agent copy",
    "al.ct.goal_opt_agent_t": "On: wins also CC the Telegram bound by the agent who created the goal / claimed the conversation (unbound agents fall back to admin-only)",
    "al.ct.goal_opt_miss": "Miss digest",
    "al.ct.goal_opt_miss_t": "On: failed/expired goals aggregate into one digest push (fires only past a count or age threshold — never one-by-one spam)",
    "al.ct.goal_opt_profile": "Include profile brief",
    "al.ct.goal_opt_profile_t": "On: win pushes carry one line of captured profile facts (name/occupation/budget…) — customer data leaves for your IM, confirm compliance first; Off: goal-level facts only (template/amount/days)",
    "al.ct.goal_opt_fail": "Save failed: {err}",
}
