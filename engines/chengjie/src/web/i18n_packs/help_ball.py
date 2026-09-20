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
    # （hb.cmd.tour 已随新手引导退役删除，2026-08-31）
    "hb.cmd.tips": "术语提示 开/关",
    # 管理后台壳「后台已更新」陈旧页横幅（与工作台 ws.uibuild.* 同机制）
    # 2026-08-28：同批去掉「刷新页面」浏览器术语（桌面壳无此入口）；short=胶囊短句
    "hb.stale.short": "后台有新版本",
    "hb.stale.text": "后台界面有新版本，点「立即更新」重新载入本页。",
    "hb.stale.btn": "立即更新",
    "hb.stale.dismiss": "暂不（2 小时内不再提醒）",
}

EN = {
    "hb.toast.tips_on": "Term tips on — hover dashed-underlined terms for explanations",
    "hb.toast.tips_off": "Term tips off",
    "hb.cmd.tips": "Term tips on/off",
    "hb.stale.short": "New admin version",
    "hb.stale.text": "A new version of the admin UI is ready. Click \u201cUpdate now\u201d to reload this page.",
    "hb.stale.btn": "Update now",
    "hb.stale.dismiss": "Not now (snooze 2h)",
}
