# -*- coding: utf-8 -*-
"""收件箱「发送护栏（额度/急停/放量/授权/会话）」横幅词条（P0 2026-08-12）。

当日实锤沉淀：反封号日发额度拦下坐席的「填入并发送」，三层反馈全失效
（路由把拦截包成 ok:true / 广播 toast 被 30min 防抖吃掉 / 无事前预判），
坐席体感＝「点了没反应，产品坏了」。本 pack 承载护栏可见化的全部前端词条。
**刻意独立成 pack**（与 inbox_budget 同决策）：共享树多线并发下新文件＝零撞车。

文案原则（与 inbox_budget / workspace_quota 同族）：把限制讲成「保护」而非
「故障」，说清 ①为什么 ②什么时候恢复 ③现在能做什么；额度是滚动 24h 窗，
绝不许「明天零点恢复」这种与实现不符的承诺。
"""

ZH = {
    # 横幅主文案（按拦因族谱分键；quota 带数字，其余定性说明）
    "inbox.gate.quota": "该账号今日发送额度已用完（{used}/{cap}），为保护账号安全暂停发送。"
                        "额度按最近 24 小时滚动恢复{frees}；急需发送请联系管理员调整。",
    "inbox.gate.quota_frees": "，最早 {time} 释放一个名额",
    "inbox.gate.health": "该账号健康评分为红灯（近期风控/失败信号偏多），系统已暂停其发送以保护账号。",
    "inbox.gate.banned": "该账号已被标记封禁/受限，发送已停止，请人工核查。",
    "inbox.gate.gate": "发送被账号安全闸门拦截（{reason}），请联系管理员检查发送配额配置。",
    # 急停族三分化（P0 2026-08-23 归因可见化）：旧后端快照无 kill 详情时回落本
    # 通用键；有详情时按 source 分「系统自动风控」/「管理员手动」两键 + TTL 倒计时。
    # 文案刻意不再断言「运营手动冻结」——自动置位（ban_signal）也走同一横幅，
    # 错误归因是本次实录事故（被读成「紧急开发」+ 以为有人手动干预）的根源。
    "inbox.gate.killswitch": "出于账号保护，对外发送已被急停开关暂停（系统自动风控或管理员手动冻结），解除后自动恢复。",
    "inbox.gate.killswitch_auto": "检测到平台风控信号（{cause}），系统已自动暂停该账号的对外发送以保护账号{until}。",
    "inbox.gate.killswitch_manual": "管理员已手动开启紧急停发，对外发送已暂停{until}，解除后自动恢复。",
    "inbox.gate.killswitch_until": "，约 {time} 自动恢复",
    # 管理员就地解除（与 DELETE /api/ops/kill-switch 的 manage_ops 闸同口径）
    "inbox.gate.lift_btn": "解除停发",
    "inbox.gate.lift_confirm": "解除紧急停发「{scope}」？\n\n"
                               "解除后该范围的对外发送（含 AI 自动发送）立即恢复。"
                               "若是系统自动风控触发，请先确认风险已排除再解除。",
    "inbox.gate.lift_ok": "已解除，发送已恢复",
    "inbox.gate.lift_fail": "解除失败：{msg}",
    # 工单直达增补：冻结范围/来源/后端标识/解除路径（双后端拓扑下管理员才知道去哪台解）
    "inbox.gate.brief_scope": "冻结范围：{scope}（来源：{src}）",
    "inbox.gate.brief_src_auto": "系统自动风控",
    "inbox.gate.brief_src_manual": "人工置位 {actor}",
    "inbox.gate.brief_backend": "后端：{origin}",
    "inbox.gate.brief_lift_how": "解除：管理员在此横幅点「解除停发」，或直达 {origin}/rpa-overview "
                                 "「风控防护」卡（侧栏菜单隐藏时直接输入网址即可）",
    "inbox.gate.canary": "该账号不在灰度放量名单内，发送暂停（放量控制中）。",
    "inbox.gate.license": "授权已到期或受限，出站发送被禁用，请联系管理员续期。",
    "inbox.gate.session": "平台会话已掉线，消息无法送达——请到「账号与平台管理」重新登录。",
    "inbox.gate.generic": "发送被安全护栏拦截（{reason}），消息未送出。",
    # 横幅悬停解释（为什么有这个东西）
    "inbox.gate.t": "账号安全保护：新号/受控号有每日发送上限与健康闸门，"
                    "防止发送过量触发平台风控被封号。限制是暂态的，会自动恢复。",
    # 工单直达：一键复制情况说明给管理员（复用连接向导 ops-brief 模式）
    "inbox.gate.copy_btn": "复制说明给管理员",
    "inbox.gate.copied": "已复制，可直接粘贴给管理员",
    "inbox.gate.brief_title": "[发送护栏] {who} 发送被拦截，请协助处理",
    "inbox.gate.brief_reason": "拦截原因：{reason}",
    "inbox.gate.brief_quota": "今日额度：{used}/{cap}（滚动 24h 窗）",
    "inbox.gate.brief_how": "处理选项：调大 companion_send_gate.target_cap / 将该客户加入 "
                            "exempt_peers 白名单 / 等待额度随时间自动释放",
    # 点击发送被拦时的即时反馈（横幅之外的动作级反馈）
    "inbox.gate.hint_blocked": "发送被安全护栏拦截，内容已保留在输入框",
    "inbox.gate.send_title": "发送受限：{msg}",
    # P1 人工预留额度：自动链让路的软信息条（人工仍可发时显示，非警示）
    # B72（实施67 P2-l，`_350` 撞名实录）：标来源=账号级防封安全额度 + 构成
    # （今日上限/让路线/人工预留），并显式与会话级「回复额度守卫」区分——
    # 两个「额度」同屏出现时坐席分不清哪个在管什么。
    # 2026-08-27 实施72 P6 勘正：旧文案把 {auto_cap}（AI 自动让路线）标成「AI 已发」，
    # 新号 auto_cap=0 时渲染出「AI 已发 0/上限 15 却已触顶」的自相矛盾（当日 02:32
    # 老板实录困惑）。数字一直是对的，标签在撒谎——按真实语义重标并把预留亮出来。
    "inbox.gate.auto_yield": "【账号防封安全额度】AI 自动发送已让路——"
                             "AI 今日可自动发 {auto_cap} 条（账号总上限 {cap}，"
                             "其中 {reserve} 条预留人工），今日已用 {used}："
                             "剩余名额专供人工回复。这是账号级防封额度，"
                             "与单个会话的「回复额度守卫」是两回事。",
    "inbox.gate.auto_yield_frees": "AI 约 {time} 恢复自动发送。",
    "inbox.gate.auto_yield_t": "账号级防封安全额度：为避免平台风控，每账号每日外发有上限；"
                               "自动外发（主动问候/AI 全自动回复）在触顶前提前让路，"
                               "把最后的名额留给坐席人工回复。构成=今日上限−人工预留=让路线；"
                               "管理员可调 reserve_for_manual。与会话级「回复额度守卫」"
                               "（防对面是机器人刷额度）互相独立。",
    # P1 管理员直达：白名单此客户（仅豁免限额；急停/授权不受影响）
    "inbox.gate.exempt_btn": "白名单此客户",
    "inbox.gate.exempt_confirm": "把该客户加入发送白名单？\n\n"
                                 "白名单客户不受本账号日发额度限制（急停/授权闸门仍有效）。"
                                 "适用于重要客户在等回复、又不便调整全局额度的场景。"
                                 "写入实例配置 overlay，热生效、重启后仍保留。",
    "inbox.gate.exempt_ok": "已加入白名单，本会话发送已放行",
    "inbox.gate.exempt_already": "该客户已在白名单内",
    "inbox.gate.exempt_fail": "白名单写入失败：{msg}",
}

EN = {
    "inbox.gate.quota": "This account has used up today's send quota ({used}/{cap}); "
                        "sending is paused to protect the account. The quota is a "
                        "rolling 24-hour window{frees}; contact an admin if you need "
                        "to send right now.",
    "inbox.gate.quota_frees": " — the next slot frees around {time}",
    "inbox.gate.health": "This account's health score is red (recent risk-control / failure "
                         "signals); sending is paused to protect it.",
    "inbox.gate.banned": "This account is flagged banned/restricted; sending stopped. Please review manually.",
    "inbox.gate.gate": "Blocked by the account-safety send gate ({reason}); ask an admin to review the quota settings.",
    "inbox.gate.killswitch": "Outbound sending is paused by the emergency freeze switch to protect the account "
                             "(set automatically by risk-control or manually by an admin); it resumes once lifted.",
    "inbox.gate.killswitch_auto": "Platform risk-control signal detected ({cause}); sending from this account was "
                                  "paused automatically to protect it{until}.",
    "inbox.gate.killswitch_manual": "An administrator manually enabled the emergency send freeze; outbound is "
                                    "paused{until} and resumes once lifted.",
    "inbox.gate.killswitch_until": ", expected to auto-resume around {time}",
    "inbox.gate.lift_btn": "Lift freeze",
    "inbox.gate.lift_confirm": "Lift the emergency freeze on \"{scope}\"?\n\n"
                               "Outbound sending in this scope (including AI auto-send) resumes immediately. "
                               "If it was set by automatic risk-control, confirm the risk is cleared first.",
    "inbox.gate.lift_ok": "Freeze lifted — sending restored",
    "inbox.gate.lift_fail": "Failed to lift: {msg}",
    "inbox.gate.brief_scope": "Freeze scope: {scope} (source: {src})",
    "inbox.gate.brief_src_auto": "automatic risk-control",
    "inbox.gate.brief_src_manual": "manually set by {actor}",
    "inbox.gate.brief_backend": "Backend: {origin}",
    "inbox.gate.brief_lift_how": "To lift: an admin clicks \"Lift freeze\" on this banner, or opens "
                                 "{origin}/rpa-overview → the risk-guard card (type the URL directly "
                                 "if the sidebar menu is hidden)",
    "inbox.gate.canary": "This account is outside the canary rollout cohort; sending is on hold (rollout control).",
    "inbox.gate.license": "License expired or restricted; outbound sending is disabled. Contact an admin to renew.",
    "inbox.gate.session": "The platform session is offline; messages cannot be delivered — re-login from account management.",
    "inbox.gate.generic": "Blocked by a safety guard ({reason}); the message was not sent.",
    "inbox.gate.t": "Account safety protection: new/managed accounts have daily send caps "
                    "and health gates to avoid platform risk-control bans. Limits are "
                    "temporary and recover automatically.",
    "inbox.gate.copy_btn": "Copy details for admin",
    "inbox.gate.copied": "Copied — paste it to your admin",
    "inbox.gate.brief_title": "[Send guard] {who} sending blocked, please assist",
    "inbox.gate.brief_reason": "Reason: {reason}",
    "inbox.gate.brief_quota": "Today's quota: {used}/{cap} (rolling 24h window)",
    "inbox.gate.brief_how": "Options: raise companion_send_gate.target_cap / add this customer "
                            "to exempt_peers / wait for the quota to free up over time",
    "inbox.gate.hint_blocked": "Blocked by a safety guard — your text is kept in the composer",
    "inbox.gate.send_title": "Sending restricted: {msg}",
    "inbox.gate.auto_yield": "[Account anti-ban quota] AI auto-sending has yielded — "
                             "AI may auto-send {auto_cap} msg(s) today (account cap {cap}, "
                             "{reserve} reserved for manual), {used} used today: the "
                             "remaining slots are kept for manual replies. This is the "
                             "account-level anti-ban quota — separate from the "
                             "per-conversation Reply Budget guard.",
    "inbox.gate.auto_yield_frees": " AI auto-sending resumes around {time}.",
    "inbox.gate.auto_yield_t": "Account-level anti-ban quota: each account has a daily outbound "
                               "cap to avoid platform risk-control; automated outbound "
                               "(proactive greetings / full-auto replies) yields early so the "
                               "last slots stay available for human agents. Makeup: daily cap − "
                               "manual reserve = yield line; admins can tune reserve_for_manual. "
                               "Independent from the per-conversation Reply Budget guard "
                               "(which protects against bot-to-bot quota burn).",
    "inbox.gate.exempt_btn": "Whitelist this customer",
    "inbox.gate.exempt_confirm": "Add this customer to the send whitelist?\n\n"
                                 "Whitelisted customers bypass this account's daily send cap "
                                 "(kill-switch / license gates still apply). For important "
                                 "customers waiting on a reply when you don't want to raise "
                                 "the global cap. Written to the instance overlay; takes effect "
                                 "immediately and survives restarts.",
    "inbox.gate.exempt_ok": "Whitelisted — sending for this conversation is unblocked",
    "inbox.gate.exempt_already": "This customer is already whitelisted",
    "inbox.gate.exempt_fail": "Failed to write whitelist: {msg}",
}
