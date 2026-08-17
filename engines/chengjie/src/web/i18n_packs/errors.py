# -*- coding: utf-8 -*-
"""路由错误文案域词条(tr(request, 'err.*') 消费)。结构见包 docstring。"""

ZH = {
    "err.enable.text_required": "text 必填",
    "err.enable.to_lang_required": "to_lang 必填",
    "err.login.password_unsupported": "该登录会话不支持两步验证密码提交",
    "err.login.password_empty": "云密码不能为空",
    "err.login.password_submit_failed": "两步验证提交异常",
    "err.login.code_unsupported": "该登录会话不支持验证码提交",
    "err.login.code_empty": "验证码不能为空",
    "err.login.code_submit_failed": "验证码提交异常",
    "err.login.code_resend_failed": "重发验证码失败",
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
    # Telegram 聊天记录同步快速失败（sync-history POST 的 reason → 人话）
    "err.acct.tg_client_cooling": "该账号连接刚出过错，正在冷却，请一两分钟后再试",
    "err.acct.tg_client_unavailable": "该账号客户端未就绪（离线或未登录），无法同步聊天记录",
    "err.acct.tg_busy_full_sync": "该账号正在深度同步，等它完成后再同步聊天记录",
    # 授权档位功能闸门（licensing.feature_gate）
    "err.lic.feature_locked": "当前授权档位未包含此功能，请升级套餐后使用",
    # 营销目标「今日工作清单」查询参数（其余 err.goals.* 词条在 goals pack）
    "err.goals.bad_scope": "暂不支持该清单范围（当前只有 today）",
    "err.goals.bad_state": "无效的清单筛选（push/pending/hold/adopted/rejected）",
    # 被骂回怼治理（companion.temper，temper_routes）
    "err.temper.readonly": "只读账号无权修改回怼治理配置",
    "err.temper.config_na": "配置系统未就绪，稍后再试",
    "err.temper.overlay_na": "配置写入能力不可用（请升级 ConfigManager）",
    "err.temper.bad_level": "无效的回怼档位：{level}（可选 off/gentle/sharp/feisty）",
    "err.temper.bad_rounds": "无效的熔断轮数：{value}（0-20 的整数，0=不熔断）",
    "err.temper.no_keys": "请求里没有任何可写的治理键",
    "err.temper.write_failed": "写入 {name} 失败：{detail}",
    "err.temper.no_text": "text 必填（要诊断的那句话）",
    "err.temper.bad_conv": "conversation_id 需为 platform:account:chat_key 三段格式",
    # 发送护栏可见化（P0 2026-08-12：send_blocked 显式回执，人工发送路由三条）
    "err.inbox.send_blocked_quota":
        "该账号今日发送额度已用完（{used}/{cap}），为保护账号安全已暂停发送。"
        "额度按最近 24 小时滚动计算，会随较早的发送记录过期自动恢复；"
        "如需立即发送，请联系管理员调整额度或将该客户加入白名单。",
    "err.inbox.send_blocked_health":
        "该账号健康评分为红灯，系统已暂停其发送以保护账号（近期风控/失败信号偏多）。",
    "err.inbox.send_blocked_banned": "该账号已被标记为封禁/受限，发送已停止，请人工核查。",
    "err.inbox.send_blocked_gate": "发送被账号安全闸门拦截（{reason}），请联系管理员检查发送配额配置。",
    "err.inbox.send_blocked_killswitch": "发送被紧急停发开关拦截（运营已手动冻结外发，解除后自动恢复）。",
    "err.inbox.send_blocked_canary": "该账号不在灰度放量名单内，发送已暂停（放量控制中）。",
    "err.inbox.send_blocked_license": "授权已到期或受限，出站发送被禁用，请联系管理员续期。",
    "err.inbox.send_blocked_session": "该平台会话已掉线，消息无法送达，请在账号管理里重新登录。",
    "err.inbox.send_blocked_generic": "发送被安全护栏拦截（{reason}），消息未送出。",
    "err.inbox.send_not_delivered": "消息未送达：{msg}",
    # P1 白名单直达（send-gate/exempt）
    "err.inbox.gate_exempt_cfg_na": "配置系统未就绪，暂时无法写入白名单，请稍后再试",
    "err.inbox.gate_exempt_failed": "白名单写入失败：{msg}",
    # 出站媒体体积上限（P3 2026-08-17：按平台配置，替代 25MB 硬编码文案）
    "err.inbox.file_too_large_mb": "文件超过该平台体积上限（{mb} MB）",
    # 表情包（贴纸）主线（2026-08-17，sticker_routes）
    "err.stk.disabled": "表情包功能未启用（inbox.stickers.enabled）",
    "err.stk.readonly": "只读账号无权管理表情包",
    "err.stk.store_unavailable": "表情包存储未就绪，请稍后再试",
    "err.stk.not_found": "贴纸不存在或已被删除",
    "err.stk.pack_not_found": "表情包不存在",
    "err.stk.pack_limit": "表情包数量已达上限（{n} 个）",
    "err.stk.pack_full": "该表情包已满，请新建一个包",
    "err.stk.not_image": "不是可识别的图片文件（支持 png/jpg/webp/gif）",
    "err.stk.too_large": "图片过大，无法作为贴纸（请换小一点的图）",
    "err.stk.encode_failed": "贴纸转码失败，请换一张图试试",
    "err.stk.save_failed": "贴纸保存失败：{err}",
    "err.stk.send_failed": "贴纸发送失败：{err}",
    "err.stk.line_only": "这是 LINE 官方贴纸，只能在 LINE 会话中发送",
    "err.stk.line_worker_na": "该 LINE 账号当前不在线或不支持发送官方贴纸",
    "err.stk.manifest_missing": "未找到官方表情包清单（assets/sticker_packs/official）",
    "err.stk.manifest_bad": "官方表情包清单解析失败：{err}",
}

EN = {
    "err.enable.text_required": "text is required",
    "err.enable.to_lang_required": "to_lang is required",
    "err.login.password_unsupported": "This login session does not support 2FA password submission",
    "err.login.password_empty": "Cloud password must not be empty",
    "err.login.password_submit_failed": "2FA password submission failed",
    "err.login.code_unsupported": "This login session does not support code submission",
    "err.login.code_empty": "Verification code must not be empty",
    "err.login.code_submit_failed": "Verification code submission failed",
    "err.login.code_resend_failed": "Failed to resend verification code",
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
    # Telegram history sync fast-fail (sync-history POST reason → human detail)
    "err.acct.tg_client_cooling": "This account's connection errored recently and is cooling down; try again in a minute or two",
    "err.acct.tg_client_unavailable": "This account's Telegram client is not ready (offline or not logged in); cannot sync history",
    "err.acct.tg_busy_full_sync": "A deep sync is running for this account; retry the history sync after it finishes",
    # License feature gate (licensing.feature_gate)
    "err.lic.feature_locked": "This feature is not included in your current plan. Please upgrade to unlock it.",
    # Marketing-goal "today agenda" query params (other err.goals.* live in the goals pack)
    "err.goals.bad_scope": "Unsupported agenda scope (only 'today' for now)",
    "err.goals.bad_state": "Invalid agenda filter (push/pending/hold/adopted/rejected)",
    # Comeback governance (companion.temper, temper_routes)
    "err.temper.readonly": "Read-only accounts cannot change comeback governance settings",
    "err.temper.config_na": "Config system not ready; try again later",
    "err.temper.overlay_na": "Config overlay writing unavailable (upgrade ConfigManager)",
    "err.temper.bad_level": "Invalid comeback level: {level} (choose off/gentle/sharp/feisty)",
    "err.temper.bad_rounds": "Invalid feud-break rounds: {value} (integer 0-20, 0 = never break)",
    "err.temper.no_keys": "No writable governance keys in the request",
    "err.temper.write_failed": "Failed to write {name}: {detail}",
    "err.temper.no_text": "text is required (the message to diagnose)",
    "err.temper.bad_conv": "conversation_id must be platform:account:chat_key (3 segments)",
    # Send-guard visibility (P0 2026-08-12: explicit send_blocked receipts on manual send routes)
    "err.inbox.send_blocked_quota":
        "This account has used up today's send quota ({used}/{cap}); sending is "
        "paused to protect the account. The quota is a rolling 24-hour window and "
        "frees up as older sends expire; contact an admin to raise the cap or "
        "whitelist this customer if you need to send now.",
    "err.inbox.send_blocked_health":
        "This account's health score is red; sending is paused to protect it "
        "(recent risk-control / failure signals).",
    "err.inbox.send_blocked_banned": "This account is flagged banned/restricted; sending stopped. Please review manually.",
    "err.inbox.send_blocked_gate": "Blocked by the account-safety send gate ({reason}); ask an admin to review the send quota settings.",
    "err.inbox.send_blocked_killswitch": "Blocked by the emergency kill switch (outbound frozen by operations; resumes once lifted).",
    "err.inbox.send_blocked_canary": "This account is outside the canary rollout cohort; sending is on hold (rollout control).",
    "err.inbox.send_blocked_license": "License expired or restricted; outbound sending is disabled. Contact an admin to renew.",
    "err.inbox.send_blocked_session": "The platform session is offline; the message cannot be delivered. Re-login from account management.",
    "err.inbox.send_blocked_generic": "Blocked by a safety guard ({reason}); the message was not sent.",
    "err.inbox.send_not_delivered": "Message not delivered: {msg}",
    # P1 whitelist shortcut (send-gate/exempt)
    "err.inbox.gate_exempt_cfg_na": "Config system not ready; cannot write the whitelist right now. Try again later.",
    "err.inbox.gate_exempt_failed": "Failed to write whitelist: {msg}",
    # Outbound media size cap (P3 2026-08-17: per-platform config replaces the 25MB literal)
    "err.inbox.file_too_large_mb": "File exceeds this platform's size limit ({mb} MB)",
    # Sticker packs (2026-08-17, sticker_routes)
    "err.stk.disabled": "Sticker packs are not enabled (inbox.stickers.enabled)",
    "err.stk.readonly": "Read-only accounts cannot manage sticker packs",
    "err.stk.store_unavailable": "Sticker storage is not ready; try again later",
    "err.stk.not_found": "Sticker not found or already deleted",
    "err.stk.pack_not_found": "Sticker pack not found",
    "err.stk.pack_limit": "Sticker pack count limit reached ({n})",
    "err.stk.pack_full": "This pack is full; create a new one",
    "err.stk.not_image": "Not a recognizable image file (png/jpg/webp/gif supported)",
    "err.stk.too_large": "Image too large for a sticker (please use a smaller one)",
    "err.stk.encode_failed": "Sticker transcoding failed; try another image",
    "err.stk.save_failed": "Failed to save sticker: {err}",
    "err.stk.send_failed": "Failed to send sticker: {err}",
    "err.stk.line_only": "This is a LINE shop sticker and can only be sent in LINE chats",
    "err.stk.line_worker_na": "This LINE account is offline or cannot send shop stickers",
    "err.stk.manifest_missing": "Official sticker manifest not found (assets/sticker_packs/official)",
    "err.stk.manifest_bad": "Failed to parse the official sticker manifest: {err}",
}
