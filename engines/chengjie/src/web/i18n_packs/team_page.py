# -*- coding: utf-8 -*-
"""用户管理页「团队角色分层 + 坐席字符额度 + L3 按人权限覆写」词条（2026-08-16）。

域覆盖：users.html 统计条/每卡用量行/额度弹窗/删除逐字确认/权限覆写编辑器 +
auth_user_routes 的分层守卫错误文案（err.team.*）+ supervisor 角色名。
新增词条一律进本 pack（勿回填 web_i18n.py 单体——并行编辑丢失更新的温床）；
键唯一性由 test_i18n_packs_bilingual_and_no_collision 门禁守住。
"""

ZH = {
    # ── 统计条（四枚小卡）──
    "tq_stat_members": "成员总数",
    "tq_stat_enabled": "已启用",
    "tq_stat_month_chars": "本月团队字符",
    "tq_stat_pool_left": "授权池剩余",
    "tq_unlimited": "不限",
    # ── 每卡「本月用量」行 + 额度弹窗 ──
    "tq_month_usage": "本月用量",
    "tq_quota_btn": "额度",
    "tq_quota_title": "设置月度字符额度",
    "tq_quota_hint": "0 = 不限；额度按 UTC 自然月计，每月 1 日自动重置。",
    "tq_lv_over": "超额",
    "tq_lv_warn": "接近上限",
    # ── JS 文案（经模板 const TQ 注入，不走 window.T）──
    "tq_js_del_confirm": "危险操作：输入用户名 {name} 以确认删除",
    "tq_js_del_mismatch": "用户名不匹配，已取消删除",
    "tq_js_quota_saved": "额度已保存",
    # ── L3 按人权限覆写编辑器（perm-modal；能力名与 PERM_REGISTRY 一一对应）──
    "tq_perm_btn": "权限",
    "tq_perm_title": "按人权限覆写",
    "tq_perm_inherit": "继承默认",
    "tq_perm_allow": "覆写允许",
    "tq_perm_deny": "覆写禁止",
    "tq_perm_inherit_allow": "继承默认允许",
    "tq_perm_inherit_deny": "继承默认禁止",
    "tq_perm_domain_chat": "聊天",
    "tq_perm_domain_ai": "AI",
    "tq_perm_send_text": "发送文字",
    "tq_perm_send_media": "发送图片/媒体",
    "tq_perm_send_voice": "语音合成与发送",
    "tq_perm_translate": "手动翻译",
    "tq_perm_saved": "权限已保存",
    "tq_perm_overridden": "已覆写",
    "tq_perm_load_fail": "权限接口未就绪（服务重启后可用）",
    "tq_perm_hint": "覆写只对该账号生效；「继承默认」跟随角色权限。",
    # ── 角色名（模板按 role_<key> 取；其余角色键在单体存量里）──
    "role_supervisor": "主管（坐席+团队看板）",
    # ── 分层守卫错误文案（路由 tr() 消费，前端 verbatim 直显）──
    "err.team.cannot_manage": "无权管理该账号（角色层级不足）",
    "err.team.role_not_allowed": "无权分配该角色",
    "err.team.master_protected": "主帐号受保护，不可在此修改",
    "err.team.user_not_found": "用户不存在",
    "err.team.bad_perms": "权限提交无效（未知能力键，或同一能力同时出现在允许与禁止）",
}

EN = {
    "tq_stat_members": "Members",
    "tq_stat_enabled": "Enabled",
    "tq_stat_month_chars": "Team chars this month",
    "tq_stat_pool_left": "License pool left",
    "tq_unlimited": "Unlimited",
    "tq_month_usage": "Monthly usage",
    "tq_quota_btn": "Quota",
    "tq_quota_title": "Set monthly character quota",
    "tq_quota_hint": "0 = unlimited; quota follows the UTC calendar month and resets on the 1st.",
    "tq_lv_over": "Over",
    "tq_lv_warn": "Near limit",
    "tq_js_del_confirm": "Danger: type the username {name} to confirm deletion",
    "tq_js_del_mismatch": "Username mismatch — deletion cancelled",
    "tq_js_quota_saved": "Quota saved",
    "tq_perm_btn": "Permissions",
    "tq_perm_title": "Per-user permission overrides",
    "tq_perm_inherit": "Inherit default",
    "tq_perm_allow": "Override: allow",
    "tq_perm_deny": "Override: deny",
    "tq_perm_inherit_allow": "Inherited: allowed",
    "tq_perm_inherit_deny": "Inherited: denied",
    "tq_perm_domain_chat": "Chat",
    "tq_perm_domain_ai": "AI",
    "tq_perm_send_text": "Send text",
    "tq_perm_send_media": "Send images/media",
    "tq_perm_send_voice": "Voice synthesis & send",
    "tq_perm_translate": "Manual translation",
    "tq_perm_saved": "Permissions saved",
    "tq_perm_overridden": "Overridden",
    "tq_perm_load_fail": "Permissions API not ready (pending service restart)",
    "tq_perm_hint": "Overrides apply to this account only; \"Inherit default\" follows the role permission.",
    "role_supervisor": "Supervisor (workspace + team boards)",
    "err.team.cannot_manage": "You cannot manage this account (insufficient role tier)",
    "err.team.role_not_allowed": "You cannot assign this role",
    "err.team.master_protected": "The master account is protected and cannot be changed here",
    "err.team.user_not_found": "User not found",
    "err.team.bad_perms": "Invalid permissions payload (unknown capability key, or the same key in both allow and deny)",
}
