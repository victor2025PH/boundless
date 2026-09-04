# -*- coding: utf-8 -*-
"""收件箱「有效档位」封顶胶囊词条（effective_automation P0，2026-08-07）。

.104/.198 双向全自动排障沉淀：档位下拉显示的是基础值，实际执行档位可能被
系统封顶（新号冷启动预热 / 平台降级 / 账号业务线），此前对坐席完全不可见
——「界面亮全自动、实际全进人审」的体感就是「产品坏了」。本 pack 承载
封顶可见化胶囊的全部前端词条。**刻意独立成 pack**（与 inbox_budget 同款
决策）：共享树协议下新文件＝零撞车。

文案原则（与 inbox_budget / workspace_quota 同族）：把封顶讲成「保护 +
何时自动恢复 + 怎么立即解除」，绝不只说「不行」。
"""

ZH = {
    "inbox.effcap.warmup": "🕐 新号预热 · 转人审",
    "inbox.effcap.warmup_t": "该账号接入系统未满预热窗，为保护新账号，全自动暂按"
                             "「{mode}」执行：AI 照常拟稿，人工确认后发送。"
                             "约 {h} 小时后自动恢复全自动。如需立即恢复："
                             "配置 companion.proactive_topic.cold_start."
                             "warmup_review: false",
    "inbox.effcap.platform": "🚧 平台封顶 · 转人审",
    "inbox.effcap.platform_t": "本平台被运营降级（inbox.auto_draft."
                               "platform_modes），会话实际按「{mode}」执行："
                               "AI 拟稿、人审后发。恢复全自动需运营移除该平台"
                               "的封顶配置。",
    "inbox.effcap.business_line": "🏷️ 业务线封顶 · 转人审",
    "inbox.effcap.business_line_t": "该账号属「{line}」业务线，档位上限为"
                                    "「{mode}」：AI 拟稿、人审后发。恢复全自动"
                                    "需调整 inbox.auto_draft.business_line_modes"
                                    " 或摘掉账号业务线标签。",
    # ── 实施72（2026-08-27）新增三层：此前掉进 generic 分支，把 identity_pending
    # 这种原始层名直接晒给坐席（还不告诉他能做什么）。
    "inbox.effcap.identity_pending": "🪪 身份待确认 · 转人审",
    "inbox.effcap.identity_pending_t": "这个登录位被检测到换了账号（{acct}），身份尚未"
                                       "人工确认：会话按「{mode}」执行——AI 照常拟稿，"
                                       "人确认后再发，且不会自动挂人设。这是防「AI 用"
                                       "错身份对客户开口」的闸。解除：主管在运维总览"
                                       "「🪪 登录身份决议」卡里点「确认转正」（≤60 秒生效）。",
    # ── #156（2026-09-04 I-2 补齐）：④b 未选人设封顶——对话 B 上了后端层（933da0f8），
    # 坐席文案 + 前端白名单这半没跟上，掉进 generic 分支把 persona_unselected 原始层名晒给坐席。
    "inbox.effcap.persona_unselected": "🎭 未选人设 · 转人审",
    "inbox.effcap.persona_unselected_t": "该账号（{acct}）还没有选定人设：新登录的账号不再"
                                         "自动挂默认人设，选定前会话按「{mode}」执行——AI "
                                         "照常拟稿，人确认后再发，不会以任何身份代答。解除："
                                         "在账号栏点该账号 →「选择人设」（或人设页绑定），"
                                         "选定后本层自动消失（≤60 秒生效）。",
    "inbox.effcap.reconnect_backlog": "🔌 断线补收窗 · 转人审",
    "inbox.effcap.reconnect_backlog_t": "该账号刚从长时间断线恢复，正在补收断线期的旧"
                                        "消息：这些消息的时间不可靠（可能是几天前的），"
                                        "全自动容易「大半夜回一条上周的消息」。恢复窗内"
                                        "按「{mode}」执行，十几分钟后自动恢复全自动。",
    "inbox.effcap.own_fleet_peer": "🏠 对端是自有号 · 转人审",
    "inbox.effcap.own_fleet_peer_t": "对面（{peer}）是登记在册的自有账号（在另一台机器上"
                                     "运营）。两台机器的 AI 互相自动回复会无限烧算力，"
                                     "在平台风控眼里还是两个号互刷，所以这条会话按"
                                     "「{mode}」执行。要放开：从 companion.own_fleet.extra "
                                     "里摘掉该登记。",
    # ── #12（2026-08-30）：全局真发总闸关着，「全自动」只拟稿不发送 ──
    "inbox.effcap.deliver_paused": "⏸️ 全局已暂停真发",
    "inbox.effcap.deliver_paused_t": "全局「AI 接管」当前是拟稿人审/关闭档（真发总闸"
                                     "关闭），本会话虽设为全自动，实际按「{mode}」执行"
                                     "：AI 只写草稿、不会自动发出。要恢复这类会话的"
                                     "自动发送：设置 → 自动回复设置 → AI 接管 选"
                                     "「全自动」。Telegram 私聊不受此闸影响。",
    "inbox.effcap.generic": "⚠️ 系统封顶",
    "inbox.effcap.generic_t": "会话实际执行档位为「{mode}」（来源：{layer}）。",
}

EN = {
    "inbox.effcap.warmup": "🕐 Warm-up · human review",
    "inbox.effcap.warmup_t": "This account is still inside its onboarding "
                             "warm-up window. To protect the new account, "
                             "full-auto temporarily runs as \"{mode}\": AI "
                             "keeps drafting, a human confirms before sending. "
                             "Full-auto resumes automatically in about {h} "
                             "hours. To lift it now, set companion."
                             "proactive_topic.cold_start.warmup_review: false",
    "inbox.effcap.platform": "🚧 Platform cap · human review",
    "inbox.effcap.platform_t": "This platform is downgraded by ops "
                               "(inbox.auto_draft.platform_modes), so the "
                               "conversation actually runs as \"{mode}\": AI "
                               "drafts, humans approve. Remove the platform "
                               "cap to restore full-auto.",
    "inbox.effcap.business_line": "🏷️ Business-line cap · human review",
    "inbox.effcap.business_line_t": "This account belongs to the \"{line}\" "
                                    "business line, capped at \"{mode}\": AI "
                                    "drafts, humans approve. Adjust inbox."
                                    "auto_draft.business_line_modes or remove "
                                    "the account label to restore full-auto.",
    # ── impl72 (2026-08-27) three new layers ──
    "inbox.effcap.identity_pending": "🪪 Identity unconfirmed · human review",
    "inbox.effcap.identity_pending_t": "This login slot was detected to have "
                                       "changed accounts ({acct}) and the identity "
                                       "is not confirmed yet, so the conversation "
                                       "runs as \"{mode}\": the AI still drafts but "
                                       "a human sends, and no persona is attached "
                                       "automatically. This gate stops the AI from "
                                       "speaking to customers as the wrong identity. "
                                       "To lift it, a supervisor clicks \"Confirm "
                                       "identity\" on the ops overview card "
                                       "\"Login identity\" (takes effect within 60s).",
    "inbox.effcap.persona_unselected": "🎭 No persona selected · human review",
    "inbox.effcap.persona_unselected_t": "This account ({acct}) has no persona selected "
                                         "yet: newly logged-in accounts no longer get a "
                                         "default persona attached automatically, so until "
                                         "one is chosen the conversation runs as \"{mode}\" "
                                         "— the AI still drafts, a human sends, and the AI "
                                         "never answers under an unchosen identity. To lift "
                                         "it, pick a persona for this account from the "
                                         "account bar (or the Personas page); the cap clears "
                                         "automatically (within 60s).",
    "inbox.effcap.reconnect_backlog": "🔌 Reconnect backlog · human review",
    "inbox.effcap.reconnect_backlog_t": "This account just recovered from a long "
                                        "outage and is importing messages from the "
                                        "downtime. Their timestamps are unreliable "
                                        "(they may be days old), so full-auto would "
                                        "happily reply to last week's message at 3am. "
                                        "The recovery window runs as \"{mode}\" and "
                                        "full-auto resumes automatically in minutes.",
    "inbox.effcap.own_fleet_peer": "🏠 Peer is our own account · human review",
    "inbox.effcap.own_fleet_peer_t": "The peer ({peer}) is a registered account of "
                                     "our own fleet, operated on another machine. "
                                     "Letting two machines' AIs auto-reply to each "
                                     "other burns compute forever and looks like "
                                     "self-farming to platform anti-abuse, so this "
                                     "conversation runs as \"{mode}\". To open it up, "
                                     "remove the entry from companion.own_fleet.extra.",
    # ── #12 (2026-08-30): global deliver gate is off, "full-auto" only drafts ──
    "inbox.effcap.deliver_paused": "⏸️ Auto-send paused globally",
    "inbox.effcap.deliver_paused_t": "The global \"AI takeover\" is currently on "
                                     "suggest/off (the deliver master switch is "
                                     "closed), so although this conversation is set "
                                     "to full-auto it actually runs as \"{mode}\": "
                                     "the AI only drafts and never sends by itself. "
                                     "To resume auto-sending for such conversations: "
                                     "Settings → Auto-reply → AI takeover → "
                                     "Full-auto. Telegram DMs are not affected by "
                                     "this gate.",
    "inbox.effcap.generic": "⚠️ System cap",
    "inbox.effcap.generic_t": "This conversation actually runs as \"{mode}\" "
                              "(source: {layer}).",
}
