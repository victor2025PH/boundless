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
    "inbox.effcap.generic": "⚠️ System cap",
    "inbox.effcap.generic_t": "This conversation actually runs as \"{mode}\" "
                              "(source: {layer}).",
}
