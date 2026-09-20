# -*- coding: utf-8 -*-
"""运营总览「🛑 紧急停发」卡词条（P1 2026-08-23 急停可见化）。

背景：唯一的置位/解除面板在 /rpa-overview，而矩阵导航默认对坐席隐藏
（ui_visibility.matrix_nav 缺省 False）——急停生效时管理员没有可见入口，
只能靠收件箱横幅的直达链接。本卡把「当前生效急停 + 一键解除」放进常驻
可见的运营总览（无生效急停整卡隐藏，安全网待命不占版面）。

**刻意独立成 pack**（与 inbox_send_gate 同决策）：共享树多线并发下新文件＝零撞车。
置位入口刻意不进本卡（防误触；灭火操作留在 /rpa-overview 专业面板）。
"""

ZH = {
    "ov2_s_ksguard": "紧急停发（生效急停 / 一键解除）",
    "ov2_ks_active": "生效作用域",
    "ov2_ks_global": "全局停发",
    "ov2_ks_global_on": "⛔ 生效中",
    "ov2_ks_global_off": "未触发（局部）",
    "ov2_ks_auto": "自动风控置位",
    "ov2_ks_src_auto": "系统自动风控",
    "ov2_ks_src_manual": "人工置位",
    "ov2_ks_ttl_until": "至 {time} 自动恢复",
    "ov2_ks_ttl_perm": "永久（需人工解除）",
    "ov2_ks_lift_btn": "🔓 解除",
    "ov2_ks_lift_confirm": "解除「{scope}」的紧急停发？\n\n该范围的对外发送（含 AI 自动发送）立即恢复。"
                           "若为系统自动风控置位，请先确认风险已排除。",
    "ov2_ks_lift_fail": "解除失败：{msg}",
    "ov2_ks_lift_fail_net": "解除失败：网络错误，请重试",
    "ov2_ks_hint": "置位/金丝雀放量在 /rpa-overview「风控防护」卡（侧栏菜单隐藏时直接输入网址）；"
                   "本卡只做「看见 + 解除」。",
}

EN = {
    "ov2_s_ksguard": "Emergency send freeze (active scopes / one-click lift)",
    "ov2_ks_active": "Active scopes",
    "ov2_ks_global": "Global freeze",
    "ov2_ks_global_on": "⛔ ACTIVE",
    "ov2_ks_global_off": "not triggered (partial)",
    "ov2_ks_auto": "Set by auto risk-control",
    "ov2_ks_src_auto": "auto risk-control",
    "ov2_ks_src_manual": "manually set",
    "ov2_ks_ttl_until": "auto-recovers at {time}",
    "ov2_ks_ttl_perm": "permanent (manual lift only)",
    "ov2_ks_lift_btn": "🔓 Lift",
    "ov2_ks_lift_confirm": "Lift the emergency freeze on \"{scope}\"?\n\nOutbound sending in this scope "
                           "(including AI auto-send) resumes immediately. If it was set by automatic "
                           "risk-control, confirm the risk is cleared first.",
    "ov2_ks_lift_fail": "Failed to lift: {msg}",
    "ov2_ks_lift_fail_net": "Failed to lift: network error, please retry",
    "ov2_ks_hint": "Set/canary controls live on /rpa-overview (risk-guard card; type the URL directly "
                   "if the sidebar menu is hidden); this card is view + lift only.",
}
