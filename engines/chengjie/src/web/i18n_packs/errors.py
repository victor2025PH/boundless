# -*- coding: utf-8 -*-
"""路由错误文案域词条(tr(request, 'err.*') 消费)。结构见包 docstring。"""

ZH = {
    "err.enable.text_required": "text 必填",
    "err.enable.to_lang_required": "to_lang 必填",
    "err.login.password_unsupported": "该登录会话不支持两步验证密码提交",
    "err.login.password_empty": "云密码不能为空",
    "err.login.password_submit_failed": "两步验证提交异常",
    # 账号官方资料修改（accounts.profile_push）
    "err.acct.profile_disabled": "资料修改功能未启用（accounts.profile_push.enabled）",
    "err.acct.profile_platform_manual": "该平台不支持在后台直接修改资料，请在手机官方 App 修改",
    "err.acct.profile_no_fields": "至少提供一个要修改的字段（昵称 / 签名 / 头像）",
    "err.acct.profile_cooldown": "距上次修改不足冷却时间，请 {remain} 后再试",
    "err.acct.profile_avatar_invalid": "头像不是有效的图片文件",
    "err.acct.profile_avatar_too_large": "头像文件超过体积上限（{mb} MB）",
    "err.acct.profile_name_too_long": "昵称过长（该平台上限 {max} 字符）",
    "err.acct.profile_status_too_long": "签名过长（该平台上限 {max} 字符）",
    "err.acct.profile_offline": "账号当前不在线，无法推送资料修改",
    "err.acct.profile_push_failed": "资料推送失败，请稍后重试",
    "err.acct.profile_no_persona": "该账号未绑定人设或人设缺少可用的名称/头像素材",
    # 授权档位功能闸门（licensing.feature_gate）
    "err.lic.feature_locked": "当前授权档位未包含此功能，请升级套餐后使用",
}

EN = {
    "err.enable.text_required": "text is required",
    "err.enable.to_lang_required": "to_lang is required",
    "err.login.password_unsupported": "This login session does not support 2FA password submission",
    "err.login.password_empty": "Cloud password must not be empty",
    "err.login.password_submit_failed": "2FA password submission failed",
    # Account official profile push (accounts.profile_push)
    "err.acct.profile_disabled": "Profile editing is disabled (accounts.profile_push.enabled)",
    "err.acct.profile_platform_manual": "This platform does not support editing the profile from the console; please change it in the official mobile app",
    "err.acct.profile_no_fields": "Provide at least one field to update (name / status / avatar)",
    "err.acct.profile_cooldown": "Profile was changed recently; please try again in {remain}",
    "err.acct.profile_avatar_invalid": "Avatar is not a valid image file",
    "err.acct.profile_avatar_too_large": "Avatar file exceeds the size limit ({mb} MB)",
    "err.acct.profile_name_too_long": "Name is too long (platform limit {max} characters)",
    "err.acct.profile_status_too_long": "Status is too long (platform limit {max} characters)",
    "err.acct.profile_offline": "Account is currently offline; cannot push profile changes",
    "err.acct.profile_push_failed": "Profile push failed; please retry later",
    "err.acct.profile_no_persona": "This account has no bound persona, or the persona has no usable name/avatar material",
    # License feature gate (licensing.feature_gate)
    "err.lic.feature_locked": "This feature is not included in your current plan. Please upgrade to unlock it.",
}
