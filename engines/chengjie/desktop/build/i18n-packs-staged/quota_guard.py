# -*- coding: utf-8 -*-
"""坐席能力权限 / 字符额度守卫词条（2026-08-16 坐席额度强制闸批次）。

消费方：unified_inbox_translate_routes / voice_routes / unified_inbox_send_routes /
drafts_routes 的路由层守卫（``tr(request, ...)`` 请求级取词，前端 verbatim 直显）。
占位符命名遵守 i18n 硬护栏：禁用 request/key/default。
"""

ZH = {
    # 能力权限守卫（resolve_user_perm 拒绝时的统一文案；具体差在哪项能力由
    # 管理员在用户管理页看 perms 配置即知，错误文案不还原 perm 键防黑话泄漏）
    "err.perm.capability_denied": "当前账号未被授予此能力，请联系管理员开通",
    # 坐席月度字符额度耗尽（enforce 开启才会触发；额度按自然月自动重置）
    "err.quota.agent_chars_exhausted": (
        "本月字符额度已用尽（已用 {used} / 上限 {quota}）。"
        "额度月初自动重置；如需继续请联系管理员调整月度额度"
    ),
}

EN = {
    "err.perm.capability_denied": (
        "This account has not been granted this capability. "
        "Please contact your administrator."
    ),
    "err.quota.agent_chars_exhausted": (
        "Monthly character quota exhausted ({used} / {quota} used). "
        "The quota resets at the start of each month; contact your "
        "administrator to raise it."
    ),
}
