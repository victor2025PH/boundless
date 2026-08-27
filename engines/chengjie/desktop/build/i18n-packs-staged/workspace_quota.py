# -*- coding: utf-8 -*-
"""坐席顶栏「字符额度 / 首启体验档」徽章词条（P2）。

为什么这些词条不进 `web_i18n.py` 单体：仓库约定新增词条一律进按域拆分的 pack
（16k 行单体曾多次发生并行编辑互相覆盖的丢失更新）。同族的存量 ``base.pill.*``
仍在单体里，本 pack 只承载新增键——两边不得同 key，由门禁
``test_i18n_packs_bilingual_and_no_collision`` 拦截。

文案原则：徽章只讲「还剩多少」，**用尽后会发生什么**放 tooltip 里讲清楚
（翻译与 AI 草稿暂停、其余功能不受影响）。坐席最怕的不是额度用尽，是
「AI 忽然不干活了而没人告诉我为什么」。
"""

ZH = {
    "base.pill.quota": "额度",
    "base.pill.quota_trial": "体验档",
    "base.pill.quota_t": "剩余字符额度 · 点击进入会员中心",
    "base.pill.quota_out": "已用尽",
    "base.pill.quota_tip": "已用 {used} / 含 {included}",
    "base.pill.quota_tip_hours": "体验档还剩 {h} 小时",
    "base.pill.quota_tip_out": "额度已用尽：翻译与 AI 草稿会暂停，收件箱与手动发送不受影响",
    "base.pill.quota_tip_expired": "体验档已到期：翻译与 AI 草稿会暂停；注册可领免费 100 万字符",
    # ── 用尽拦截弹层（2026-08-11 免费额度升级：拦得住也要接得住——三条出路并排）──
    "ws.quotawall.title": "字符额度已用尽",
    "ws.quotawall.body": "翻译与 AI 拟稿已暂停；收件箱与手动发送不受影响。马上恢复：",
    "ws.quotawall.invite": "🎁 邀请好友领字符（免费）",
    "ws.quotawall.cs": "💬 联系客服申请",
    "ws.quotawall.buy": "🚀 购买 / 升级",
    "ws.quotawall.later": "稍后再说",
    "ws.quotawall.agent_hint": "请联系你的管理员处理额度；恢复前翻译与 AI 拟稿会暂停。",
    "ws.quotawall.view": "查看额度详情",
    # ── quotawall v2（2026-08-21：四变体 + 弹层内充值闭环）──
    # 变体文案原则：先讲「什么停了、什么照常」（防恐慌），再给按转化价值排序的
    # 出路；tok 变体必须诚实说「降级」而不是「停摆」——服务还在只是省耗模式。
    "ws.quotawall.title_trial": "体验额度已用尽",
    "ws.quotawall.body_trial": "免费体验额度已用完：翻译与 AI 拟稿暂停，收件箱与手动"
                               "发送不受影响。注册即可免费领 100 万字符继续用：",
    "ws.quotawall.claim": "🎁 注册领 100 万字符（免费）",
    "ws.quotawall.title_token": "Token 余额已用完",
    "ws.quotawall.body_token": "为保证不断线，AI 回复已切换免费本地模型、专业翻译已"
                               "降级为标准翻译（免费不限量）；收件箱与手动发送一切"
                               "照常。充值后立即恢复满血：",
    "ws.quotawall.title_agent": "本月坐席额度已用尽",
    "ws.quotawall.body_agent": "已用 {used} / 上限 {quota}，月初自动重置。请联系管理员"
                               "调整你的月度额度；期间翻译与 AI 拟稿暂停，收件箱与"
                               "手动发送不受影响。",
    "ws.quotawall.recharge": "立即充值（到账自动恢复）",
    "ws.quotawall.watching": "已打开充值页——支付完成后额度自动到账，这里会自动恢复",
    "ws.quotawall.credited": "+{n} 已到账，AI 功能已恢复",
    "ws.quotawall.recovered": "额度已恢复，AI 功能已就绪",
    "ws.quotawall.voucher_err": "凭证兑换失败，请核对后重试",
    # 顶栏徽章补充态（Token 钱包 / 坐席月度额度也值得一颗 pill）
    "base.pill.quota_tokens": "Token",
    "base.pill.quota_tip_token": "Token 余额已用完：AI 回复走省耗模式、专业翻译降级"
                                 "标准翻译；充值后立即恢复",
    "base.pill.quota_tip_agent": "你本月的坐席字符额度已用尽（已用 {used} / 上限 "
                                 "{quota}）；月初自动重置，可联系管理员调整",
    # ── 预计耗尽预警条（quotawall v2 P2，2026-08-21）：80%/临期不是拦截时刻，
    #    是最好的销售时刻——用户还没被打断，给日期给出路，别等撞墙 ──
    "ws.quotalow.text": "字符额度仅剩 {remaining}，按近 7 天用量预计 {date} 前后"
                        "用尽（约 {days} 天）——现在充值，到账自动恢复",
    "ws.quotalow.text_agent": "字符额度仅剩 {remaining}，预计 {date} 前后用尽"
                              "（约 {days} 天）；请提醒管理员及时续费，用尽后"
                              "翻译与 AI 拟稿会暂停",
    "ws.quotalow.later": "今天不再提醒",
    "ws.quotalow.tip": "按近 7 天用量预计约 {days} 天后用尽",
}

EN = {
    "base.pill.quota": "Quota",
    "base.pill.quota_trial": "Starter",
    "base.pill.quota_t": "Remaining character quota · open membership center",
    "base.pill.quota_out": "Used up",
    "base.pill.quota_tip": "{used} used / {included} included",
    "base.pill.quota_tip_hours": "Starter allowance: {h}h left",
    "base.pill.quota_tip_out": "Quota exhausted: translation and AI drafts pause; "
                               "inbox and manual sending keep working",
    "base.pill.quota_tip_expired": "Starter allowance expired: translation and AI drafts "
                                   "pause; register to claim 1,000,000 free characters",
    # ── Quota-exhausted wall (2026-08-11 free-quota upgrade: three ways out) ──
    "ws.quotawall.title": "Character quota used up",
    "ws.quotawall.body": "Translation and AI drafting are paused; the inbox and manual "
                         "sending keep working. Get going again:",
    "ws.quotawall.invite": "🎁 Invite friends (free characters)",
    "ws.quotawall.cs": "💬 Ask support",
    "ws.quotawall.buy": "🚀 Buy / upgrade",
    "ws.quotawall.later": "Later",
    "ws.quotawall.agent_hint": "Ask your admin to top up the quota; translation and AI "
                               "drafting stay paused until then.",
    "ws.quotawall.view": "View quota details",
    # ── quotawall v2 (2026-08-21: four variants + in-wall recharge loop) ──
    "ws.quotawall.title_trial": "Starter allowance used up",
    "ws.quotawall.body_trial": "Your free starter allowance is used up: translation and "
                               "AI drafting pause while the inbox and manual sending "
                               "keep working. Register to claim 1,000,000 free "
                               "characters and keep going:",
    "ws.quotawall.claim": "🎁 Claim 1,000,000 free characters",
    "ws.quotawall.title_token": "Token balance used up",
    "ws.quotawall.body_token": "To keep you online, AI replies switched to the free "
                               "local model and pro translation degraded to standard "
                               "(free, unlimited); the inbox and manual sending are "
                               "untouched. Top up to restore full power instantly:",
    "ws.quotawall.title_agent": "Monthly seat quota used up",
    "ws.quotawall.body_agent": "{used} used / {quota} limit; resets at the start of "
                               "each month. Ask your admin to raise your monthly "
                               "quota — translation and AI drafting pause until then, "
                               "while the inbox and manual sending keep working.",
    "ws.quotawall.recharge": "Top up now (auto-credited)",
    "ws.quotawall.watching": "Order page opened — once paid, the credit lands "
                             "automatically and everything resumes here",
    "ws.quotawall.credited": "+{n} credited — AI features restored",
    "ws.quotawall.recovered": "Quota restored — AI features are back",
    "ws.quotawall.voucher_err": "Voucher redemption failed; check it and try again",
    # Top-bar pill extras (token wallet / per-seat monthly quota deserve a pill too)
    "base.pill.quota_tokens": "Tokens",
    "base.pill.quota_tip_token": "Token balance used up: AI replies run in eco mode and "
                                 "pro translation degrades to standard; top up to "
                                 "restore instantly",
    "base.pill.quota_tip_agent": "Your monthly seat character quota is used up "
                                 "({used} used / {quota} limit); it resets monthly — "
                                 "ask your admin to adjust",
    # ── Running-low warning bar (quotawall v2 P2, 2026-08-21) ──
    "ws.quotalow.text": "Only {remaining} characters left — at the last 7 days' pace "
                        "they run out around {date} (~{days} days). Top up now and "
                        "the credit lands automatically.",
    "ws.quotalow.text_agent": "Only {remaining} characters left — expected to run out "
                              "around {date} (~{days} days). Remind your admin to "
                              "renew; translation and AI drafting pause once it's gone.",
    "ws.quotalow.later": "Not today",
    "ws.quotalow.tip": "At the last 7 days' pace, ~{days} days left",
}
