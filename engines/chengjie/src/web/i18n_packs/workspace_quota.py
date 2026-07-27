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
    "base.pill.quota_tip_expired": "体验档已到期：翻译与 AI 草稿会暂停；注册可换 7 天完整试用",
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
                                   "pause; register to get the full 7-day trial",
}
