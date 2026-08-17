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
}
