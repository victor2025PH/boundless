# -*- coding: utf-8 -*-
"""登录页说明与引导域词条（P0 首屏/账号区说明引导，2026-08-21）。结构见包 docstring。

背景：登录页此前零说明——坐席第一天拿到账号不知道账号从哪来、忘记密码找谁；
全新部署（无用户）时只有一个突兀的令牌输入框，没有指路 /setup 初始化向导。
键前缀 lg2_（登录页存量 lg_s* 在单体，按词条单源规则新键进 pack 不动单体）。
"""

ZH = {
    "lg2_note_account": "账号由管理员创建分配；忘记密码请联系管理员重置。",
    "lg2_note_setup": "首次部署？先完成初始化，创建管理员账号 →",
    "lg2_token_mode": "管理员令牌登录",
    "lg2_token_hint": "令牌登录仅供系统管理员使用；坐席请用帐号密码登录。",
    # 实施97 P1：企业微信成员扫码登录
    "lg2_wecom_btn": "企业微信扫码登录",
    "lg2_wecom_hint": "用企业微信扫码即可登录，首次登录自动创建坐席账号。",
    # 用户管理 P0-1（2026-09-11）：主动退出后落地提示（/login?manual=1）
    "lg2_manual_note": "已退出登录。请用帐号密码登录；系统管理员可切到「管理员令牌登录」。",
    "lg2_manual_shell": "桌面端的自动登录已暂停；重新打开应用会恢复主帐号自动登录。",
}

EN = {
    "lg2_note_account": "Accounts are created by your administrator. "
                        "Forgot your password? Ask an admin to reset it.",
    "lg2_note_setup": "First deployment? Run the setup wizard to create the admin account →",
    "lg2_token_mode": "Admin token sign-in",
    "lg2_token_hint": "Token sign-in is for system administrators; "
                      "agents sign in with username & password.",
    "lg2_wecom_btn": "Sign in with WeCom",
    "lg2_wecom_hint": "Scan with WeCom to sign in; an agent account is created on first sign-in.",
    "lg2_manual_note": "You have signed out. Sign in with username & password; "
                       "system administrators can switch to admin token sign-in.",
    "lg2_manual_shell": "Automatic sign-in in the desktop app is paused; "
                        "reopening the app restores master auto sign-in.",
}

# 繁體人工詞條（本批新鍵，regen 前的兜底；regen 不會復活已轉正鍵）
ZH_HANT = {
    "lg2_manual_note": "已退出登入。請用帳號密碼登入；系統管理員可切到「管理員令牌登入」。",
    "lg2_manual_shell": "桌面端的自動登入已暫停；重新開啟應用會恢復主帳號自動登入。",
}
