# -*- coding: utf-8 -*-
"""路由错误文案域词条(tr(request, 'err.*') 消费)。结构见包 docstring。"""

ZH = {
    # KB 来源隔离（2026-09-05 J-9 #184）
    "err.kb.purge_source_invalid": "只能整批清空 vendor / system / import 来源的条目",
    # 工具箱「AI 生成图片」/「智能养号」（2026-08-21）
    "err.image.viewer_denied": "只读角色不能生成图片",
    "err.image.disabled": "AI 生成图片未启用",
    "err.image.prompt_empty": "请先填写提示词",
    "err.image.engine_unknown": "未知出图引擎：{name}",
    "err.image.engine_unconfigured": "引擎未配置：{name}（缺 command_args）",
    "err.image.gen_failed": "生成失败：{err}",
    "err.image.preview_failed": "预览生成失败：{err}",
    "err.image.save_missing": "缺少人设或图片路径",
    "err.image.path_denied": "非法图片路径",
    "err.image.src_missing": "源图片不存在",
    "err.image.persona_bad": "人设标识非法",
    "err.image.save_failed": "存相册失败：{err}",
    "err.image.job_not_found": "生成任务不存在或已过期",
    "err.nurture.viewer_denied": "只读角色不能修改养护配置",
    "err.nurture.no_config_mgr": "配置管理器不可用",
    "err.nurture.save_failed": "保存失败：{err}",
    "err.nurture.profile_bad": "未知养护档：{name}",
    "err.nurture.platform_empty": "平台不能为空",
    "err.nurture.engine_action_bad": "未知引擎动作：{name}",
    # 回连认领（账号资产保全 P1）
    "err.asset.conv_not_found": "会话不存在：{cid}",
    "err.asset.claim_failed": "认领失败：{reason}",
    "err.asset.same_account": "新旧账号不能相同",
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
    "err.inbox.send_blocked_gate": "发送被账号安全限额拦截（{reason}），请联系管理员检查发送配额配置。",
    "err.inbox.send_blocked_killswitch": "发送被急停开关拦截（外发已暂停保护账号；可能为系统自动风控或管理员手动冻结，解除后自动恢复）。",
    "err.inbox.send_blocked_canary": "该账号不在分批放量名单内，发送已暂停（放量控制中）。",
    "err.inbox.send_blocked_license": "授权已到期或受限，出站发送被禁用，请联系管理员续期。",
    "err.inbox.send_blocked_session": "该平台会话已掉线，消息无法送达，请在账号管理里重新登录。",
    "err.inbox.send_blocked_generic": "发送被安全护栏拦截（{reason}），消息未送出。",
    "err.inbox.send_not_delivered": "消息未送达：{msg}",
    # 实施86 域B-1（工单 #21/#23/#49）：发送失败三类人话（与失败留痕气泡
    # inbox.failr.* 同一分类口径，映射在 send_failure_class.FAILURE_CLASS_I18N）
    "err.inbox.sendfail.rate_limited": "触发平台发送限频，系统已自动退避降速，稍后自动恢复——这条消息已留痕，可稍后一键重发",
    "err.inbox.sendfail.platform_block": "账号被平台风控暂时限制发送（已自动冻结保护，到期自动解除）——消息已留痕，解除后可一键重发",
    "err.inbox.sendfail.e2ee_pin": "加密会话未解锁：请先在该账号输入恢复 PIN，再一键重发这条消息",
    "err.inbox.sendfail.session": "平台会话已掉线：请到账号管理重新登录，再一键重发这条消息",
    "err.inbox.sendfail.channel": "发送通道异常（我方组件），失败详情已记录——可稍后一键重发",
    # 投递被平台拒绝的人话映射（2026-08-20 内测工单 #3：语音发送失败裸报英文）
    "err.inbox.voice_peer_privacy": "对方在 Telegram 隐私设置里限制了接收语音消息，语音发不进去——请改发文字或图片（这不是系统故障）。",
    "err.inbox.send_peer_blocked": "对方与这个账号处于拉黑状态，消息无法送达。",
    "err.inbox.send_flood": "平台判定发送过于频繁，账号被临时限流——请稍后再试，避免连续快速发送。",
    "err.inbox.send_write_forbidden": "没有向该会话发送内容的权限（可能被禁言、被移出，或对方作了限制）。",
    "err.inbox.send_peer_deactivated": "对方账号已注销，消息无法送达。",
    # P1 白名单直达（send-gate/exempt）
    "err.inbox.gate_exempt_cfg_na": "配置系统未就绪，暂时无法写入白名单，请稍后再试",
    "err.inbox.gate_exempt_failed": "白名单写入失败：{msg}",
    # 出站媒体体积上限（P3 2026-08-17：按平台配置，替代 25MB 硬编码文案）
    "err.inbox.file_too_large_mb": "文件超过该平台体积上限（{mb} MB）",
    # #169 2026-09-05：带实际大小与差值（size/over 为 MB，一位小数；size 未知时路由回落上一条）
    "err.inbox.file_too_large_detail": "文件 {size} MB 超过该平台体积上限 {cap} MB（超出 {over} MB，请压缩后重发）",
    # 图片问答「问这张图」（P2 2026-08-19，unified_inbox_vision_routes）
    "err.imgask.question_required": "请先输入要问的问题",
    "err.imgask.not_image": "该消息不是图片，暂不支持问图",
    "err.imgask.media_unavailable": "找不到这条消息的图片文件（可能未归档或已清理）",
    "err.imgask.vision_off": "识图能力未开启（vision.enabled）",
    "err.imgask.failed": "识图没有返回结果，请稍后重试",
    # 报障工单截图取图（2026-08-27，bug_intake_routes）
    "err.bug.shot_not_found": "该截图不存在（工单可能没有附件，或文件已被清理）",
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
    # 会话内文档一键译（P0-D 2026-08-19，translate-message-media document 分支）
    "err.docmsg.unsupported_ext": "文档翻译支持 .docx / .xlsx / .pptx / .pdf / .srt / .vtt",
    "err.docmsg.too_large": "文件过大（上限 10MB）",
    "err.docmsg.read_failed": "文件读取失败",
    # 工作台图片「识别翻译」失败原因拆分（2026-09-06 L-6 #213，原一律「识别翻译不可用」）
    "err.vision.unconfigured": "识图服务未配置",
    "err.vision.busy": "识图服务忙，稍后重试",
    "err.vision.no_text": "图中未识别到文字",
}

EN = {
    # KB source isolation (2026-09-05 J-9 #184)
    "err.kb.purge_source_invalid": "Only vendor / system / import entries can be purged in bulk",
    # Toolbox "AI Image" / "Smart Nurturing" (2026-08-21)
    "err.image.viewer_denied": "Viewer role cannot generate images",
    "err.image.disabled": "AI image generation is disabled",
    "err.image.prompt_empty": "Enter a prompt first",
    "err.image.engine_unknown": "Unknown image engine: {name}",
    "err.image.engine_unconfigured": "Engine not configured: {name} (missing command_args)",
    "err.image.gen_failed": "Generation failed: {err}",
    "err.image.preview_failed": "Preview failed: {err}",
    "err.image.save_missing": "Missing persona or image path",
    "err.image.path_denied": "Illegal image path",
    "err.image.src_missing": "Source image not found",
    "err.image.persona_bad": "Invalid persona id",
    "err.image.save_failed": "Save to album failed: {err}",
    "err.image.job_not_found": "Generation job not found or expired",
    "err.nurture.viewer_denied": "Viewer role cannot change nurturing config",
    "err.nurture.no_config_mgr": "Config manager unavailable",
    "err.nurture.save_failed": "Save failed: {err}",
    "err.nurture.profile_bad": "Unknown nurturing profile: {name}",
    "err.nurture.platform_empty": "Platform must not be empty",
    "err.nurture.engine_action_bad": "Unknown engine action: {name}",
    # Reconnect claim (account asset vault P1)
    "err.asset.conv_not_found": "Conversation not found: {cid}",
    "err.asset.claim_failed": "Claim failed: {reason}",
    "err.asset.same_account": "New and old accounts must differ",
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
    "err.inbox.send_blocked_killswitch": "Blocked by the emergency freeze switch (outbound paused to protect the account; set automatically by risk-control or manually by an admin; resumes once lifted).",
    "err.inbox.send_blocked_canary": "This account is outside the canary rollout cohort; sending is on hold (rollout control).",
    "err.inbox.send_blocked_license": "License expired or restricted; outbound sending is disabled. Contact an admin to renew.",
    "err.inbox.send_blocked_session": "The platform session is offline; the message cannot be delivered. Re-login from account management.",
    "err.inbox.send_blocked_generic": "Blocked by a safety guard ({reason}); the message was not sent.",
    "err.inbox.send_not_delivered": "Message not delivered: {msg}",
    # impl86 domain B-1 (tickets #21/#23/#49): three-way human-readable send failures
    "err.inbox.sendfail.rate_limited": "Platform rate limit hit — sending is auto-throttled and recovers shortly; the message is kept and can be resent with one click",
    "err.inbox.sendfail.platform_block": "This account is temporarily blocked from sending by the platform (auto-frozen for protection; lifts automatically) — the message is kept for resend",
    "err.inbox.sendfail.e2ee_pin": "Encrypted chat locked: enter this account's recovery PIN first, then resend the kept message",
    "err.inbox.sendfail.session": "Platform session offline: re-login from account management, then resend the kept message",
    "err.inbox.sendfail.channel": "Send channel error (our side) — details recorded; resend later with one click",
    "err.inbox.voice_peer_privacy": "The recipient's Telegram privacy settings block incoming voice messages — send text or an image instead (this is not a system fault).",
    "err.inbox.send_peer_blocked": "This account and the recipient have blocked each other; the message cannot be delivered.",
    "err.inbox.send_flood": "The platform rate-limited this account for sending too fast — wait a bit and avoid rapid consecutive sends.",
    "err.inbox.send_write_forbidden": "No permission to send to this conversation (muted, removed, or restricted by the peer).",
    "err.inbox.send_peer_deactivated": "The recipient's account is deactivated; the message cannot be delivered.",
    # P1 whitelist shortcut (send-gate/exempt)
    "err.inbox.gate_exempt_cfg_na": "Config system not ready; cannot write the whitelist right now. Try again later.",
    "err.inbox.gate_exempt_failed": "Failed to write whitelist: {msg}",
    # Outbound media size cap (P3 2026-08-17: per-platform config replaces the 25MB literal)
    "err.inbox.file_too_large_mb": "File exceeds this platform's size limit ({mb} MB)",
    "err.inbox.file_too_large_detail": "File is {size} MB, over this platform's {cap} MB limit (by {over} MB) — please compress and resend",
    "err.imgask.question_required": "Enter a question first",
    "err.imgask.not_image": "This message is not an image",
    "err.imgask.media_unavailable": "Image file for this message is unavailable (not archived or already cleaned up)",
    "err.imgask.vision_off": "Vision is not enabled (vision.enabled)",
    "err.imgask.failed": "Vision returned no answer, please try again later",
    # Bug-report ticket screenshot fetch (2026-08-27, bug_intake_routes)
    "err.bug.shot_not_found":
        "Screenshot not found (the ticket may have no attachment, "
        "or the file was cleaned up)",
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
    # In-conversation document translate (P0-D 2026-08-19, translate-message-media document branch)
    "err.docmsg.unsupported_ext": "Document translation supports .docx / .xlsx / .pptx / .pdf / .srt / .vtt",
    "err.docmsg.too_large": "File too large (max 10MB)",
    "err.docmsg.read_failed": "Failed to read the file",
    # Workbench image "recognize + translate" failure reasons (2026-09-06 L-6 #213)
    "err.vision.unconfigured": "Image recognition is not configured",
    "err.vision.busy": "Image recognition is busy, please retry shortly",
    "err.vision.no_text": "No text recognized in the image",
}
