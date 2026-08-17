# -*- coding: utf-8 -*-
"""收件箱「每日自动回复预算」横幅词条（peer_bot_guard P2，2026-08-04）。

198 事故沉淀：预算熔断此前只写日志——会话仍亮着绿色「全自动」，客户被
已读不回、坐席零感知，体感＝「产品坏了」。本 pack 承载熔断可见化的全部
前端词条。**刻意独立成 pack**（不进 inbox_workspace.py）：落地当晚另一条
agent 线正在编辑该 pack，共享树协议下新文件＝零撞车。

文案原则（与 workspace_quota 同族）：把风控讲成「保护」而不是「故障」，
并且永远给一个当场可点的出路（今日继续 / 明天自动恢复）。
"""

ZH = {
    "inbox.budget.soft": "本会话今日 AI 自动回复已达上限（{used}/{limit} 轮）。"
                         "为保护账号安全已转「AI草稿·我审」：草稿会继续生成，"
                         "发送需人工确认，明天自动恢复全自动。",
    "inbox.budget.hard": "本会话今日 AI 互动已达硬顶（{used} 轮，上限 {limit} 的 2 倍）。"
                         "自动回复与 AI 拟稿均已暂停，明天自动恢复；"
                         "可手动回复，或点「今日继续」解除限制。",
    "inbox.budget.relieved": "预算限制已解除（仅今日）：AI 按原档位继续工作，明天回到正常预算。",
    "inbox.budget.relief_btn": "今日继续自动回复",
    "inbox.budget.relief_confirm": "解除本会话今日的 AI 回复上限？\n\n"
                                   "上限是防「对面是机器人/异常刷屏」烧穿账号的保护，"
                                   "确认对方是真人客户再解除。仅今日有效，明天自动恢复。",
    "inbox.budget.relief_ok": "已解除：本会话今日不再受预算限制",
    "inbox.budget.relief_fail": "解除失败，请重试",
    "inbox.budget.t": "AI 每日预算保护：单会话每天的 AI 自动回复轮次有上限，"
                      "防止与机器人互聊空转、保护账号不被风控。",
    # P1 预警（2026-08-12）：触顶**之前**（≥80%）给提前量——事前可豁免/调额，
    # 不用等静默熔断后事后救火
    "inbox.budget.near": "本会话今日 AI 自动回复已用 {used}/{limit} 轮，接近上限。"
                         "触顶后将转人审/停发（明天自动恢复）；"
                         "如确认对方是真人可提前点「今日继续」解除今日限制。",
    # 触顶弹窗（P3 2026-08-17）：SSE bot_peer_alert(reason=daily_budget) →
    # 工作台任意页中央弹窗（workspace_base __wsBudgetPop）——修「不开着那个
    # 会话就零感知」的盲区。relief_* 结构化键供 __wsModal 美化确认弹窗；
    # 旧整段 relief_confirm 保留作降级 fallback（__wsModal 缺席时走原生 confirm）。
    "inbox.budget.pop_title": "AI 自动回复已达今日上限",
    "inbox.budget.pop_lead": "「{name}」今日 AI 自动回复已用 {used}/{limit} 轮。",
    "inbox.budget.pop_soft_note": "已转「AI 草稿·我审」：草稿继续生成，发送需人工确认，"
                                  "明天自动恢复全自动。确认对方是真人客户，可点"
                                  "「今日继续自动回复」解除今日限制。",
    "inbox.budget.pop_hard_note": "已达硬顶（上限的 2 倍）：自动回复与 AI 拟稿均已暂停，"
                                  "明天自动恢复；可手动回复，或点「今日继续自动回复」解除。",
    "inbox.budget.pop_view_btn": "查看会话",
    "inbox.budget.pop_later_btn": "知道了",
    "inbox.budget.relief_title": "解除本会话今日的 AI 回复上限？",
    "inbox.budget.relief_lead": "上限是保护账号的保险丝，解除前请确认：",
    "inbox.budget.relief_p1": "它防的是「对面是机器人 / 异常刷屏」把账号烧穿",
    "inbox.budget.relief_p2": "请确认对方是真人客户，再解除限制",
    "inbox.budget.relief_p3": "仅今日有效，明天自动恢复正常上限",
    "inbox.budget.relief_go": "确认解除",
    "inbox.budget.relief_cancel": "取消",
}

EN = {
    "inbox.budget.soft": "This conversation hit today's AI auto-reply cap "
                         "({used}/{limit} turns). To protect the account it "
                         "switched to \"AI drafts · you review\": drafts keep "
                         "coming, sending needs your approval. Full-auto "
                         "resumes tomorrow.",
    "inbox.budget.hard": "This conversation hit today's hard ceiling "
                         "({used} turns, 2× the {limit} cap). Auto-reply and "
                         "AI drafting are paused until tomorrow; you can still "
                         "reply manually, or click \"Resume today\" to lift it.",
    "inbox.budget.relieved": "Budget lifted for today only: AI keeps working at "
                             "the current mode; the normal cap returns tomorrow.",
    "inbox.budget.relief_btn": "Resume auto-reply today",
    "inbox.budget.relief_confirm": "Lift today's AI reply cap for this conversation?\n\n"
                                   "The cap protects your account from bot loops "
                                   "and spam bursts. Lift it only if you are sure "
                                   "the peer is a real customer. Today only; the "
                                   "normal cap returns tomorrow.",
    "inbox.budget.relief_ok": "Lifted: this conversation ignores the budget for today",
    "inbox.budget.relief_fail": "Failed to lift, please retry",
    "inbox.budget.t": "Daily AI budget guard: each conversation has a daily cap on "
                      "AI auto-reply turns, preventing bot-to-bot loops and "
                      "protecting the account from platform risk control.",
    "inbox.budget.near": "This conversation used {used}/{limit} AI auto-reply "
                         "turns today — close to the cap. At the cap it switches "
                         "to review/pauses (resets tomorrow); if the peer is a "
                         "real customer you can lift today's cap now via "
                         "\"Resume today\".",
    "inbox.budget.pop_title": "AI auto-reply hit today's cap",
    "inbox.budget.pop_lead": "\"{name}\" used {used}/{limit} AI auto-reply turns today.",
    "inbox.budget.pop_soft_note": "Switched to \"AI drafts · you review\": drafts keep "
                                  "coming, sending needs your approval; full-auto "
                                  "resumes tomorrow. If the peer is a real customer, "
                                  "click \"Resume auto-reply today\" to lift today's cap.",
    "inbox.budget.pop_hard_note": "Hard ceiling reached (2× the cap): auto-reply and AI "
                                  "drafting are paused until tomorrow; you can still "
                                  "reply manually, or click \"Resume auto-reply today\".",
    "inbox.budget.pop_view_btn": "Open conversation",
    "inbox.budget.pop_later_btn": "Got it",
    "inbox.budget.relief_title": "Lift today's AI reply cap for this conversation?",
    "inbox.budget.relief_lead": "The cap is a fuse that protects your account. Before lifting:",
    "inbox.budget.relief_p1": "It guards against bot loops / spam bursts burning the account",
    "inbox.budget.relief_p2": "Make sure the peer is a real customer before lifting",
    "inbox.budget.relief_p3": "Today only — the normal cap returns tomorrow",
    "inbox.budget.relief_go": "Lift the cap",
    "inbox.budget.relief_cancel": "Cancel",
}
