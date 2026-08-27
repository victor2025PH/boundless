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
}

EN = {
    "lg2_note_account": "Accounts are created by your administrator. "
                        "Forgot your password? Ask an admin to reset it.",
    "lg2_note_setup": "First deployment? Run the setup wizard to create the admin account →",
    "lg2_token_mode": "Admin token sign-in",
    "lg2_token_hint": "Token sign-in is for system administrators; "
                      "agents sign in with username & password.",
}
