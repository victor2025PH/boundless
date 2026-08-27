# -*- coding: utf-8 -*-
"""帮助域词条（原「?」帮助球域；球已于 2026-08-21 退役）。

现存消费面：
- ``hb.toast.tips_*``：base.html ``window.toggleTermTips`` 开关提示；
- ``hb.cmd.*``：命令面板「术语提示/重看引导」动作项（球退役后的功能收编位）；
- ``hb.stale.*``：管理后台壳「后台已更新」陈旧页横幅（与工作台 ws.uibuild.* 同机制）。

球本体词条（title/aria/menu.*/toast.hidden/undo）已随球退役删除（2026-08-21）。
⚠ 单体遗留键 ``tip_toggle_title`` / ``tip_hidden_toast``（web_i18n.py）当时因单体
活跃编辑窗刻意未删，现同为死键——仍等单体静默窗一并回收（勿再引用）。
"""

ZH = {
    "hb.toast.tips_on": "术语提示已开启：悬停虚线下划线词条可查看解释",
    "hb.toast.tips_off": "术语提示已关闭",
    # ?球退役后（2026-08-21）功能移入命令面板的入口词条
    "hb.cmd.tips": "术语提示 开/关",
    "hb.cmd.tour": "重看新手引导",
    # 管理后台壳「后台已更新」陈旧页横幅（与工作台 ws.uibuild.* 同机制）
    "hb.stale.text": "后台界面已更新到新版本，刷新页面即可加载。",
    "hb.stale.btn": "立即刷新",
    "hb.stale.dismiss": "暂不（2 小时内不再提醒）",
}

EN = {
    "hb.toast.tips_on": "Term tips on — hover dashed-underlined terms for explanations",
    "hb.toast.tips_off": "Term tips off",
    "hb.cmd.tips": "Term tips on/off",
    "hb.cmd.tour": "Replay onboarding tour",
    "hb.stale.text": "The admin UI has been updated — refresh to load the new version.",
    "hb.stale.btn": "Refresh now",
    "hb.stale.dismiss": "Not now (snooze 2h)",
}
