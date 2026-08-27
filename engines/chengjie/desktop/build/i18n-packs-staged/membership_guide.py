# -*- coding: utf-8 -*-
"""会员中心「说明与引导」子域词条（P0 首屏/账号区说明引导，2026-08-21）。

刻意独立于 membership.py：该文件正被充值履约/钱包等多条并行线频繁编辑，
子 pack 隔离文件级「后写覆盖先写」风险（pack 机制按目录合并，键不冲突即可）。
键前缀 mb2_。消费方：templates/membership.html 顶部接回横幅 + 锚点导航。
"""

ZH = {
    # 尝鲜（未注册）状态接回横幅：把首启向导里跳过领取的人在会员中心接回来。
    # {n}=本机尝鲜总额度（动态值，勿写死 10 万——额度由 licensing.trial 配置决定）。
    "mb2_trial_banner": "当前是尝鲜额度（{n} 字符 · 未注册）。留个联系方式即可免费领 "
                        "100 万字符——无期限、绑定本机。",
    "mb2_trial_banner_cta": "立即领取 ↓",
    "mb2_nav_quota": "额度总览",
    # low/out 状态行尾的一步直达（P1-L 缩水版：不复制按钮组，只给锚点）
    "mb2_more_jump": "获取更多 ↓",
}

EN = {
    "mb2_trial_banner": "You're on the starter allowance ({n} characters, unregistered). "
                        "Leave a contact to claim 1,000,000 free characters — "
                        "no expiry, bound to this machine.",
    "mb2_trial_banner_cta": "Claim now ↓",
    "mb2_nav_quota": "Quota",
    "mb2_more_jump": "Get more ↓",
}
