# -*- coding: utf-8 -*-
"""版本对比（/diff）页词条包。

存量 ``diff_s*`` / ``diff_js_*`` 键仍在 web_i18n 单体（迁移待无并行编辑窗口）；
按 P4 词条单源规则，本页**新增**键一律进本包。
"""

ZH = {
    # 快照列表空态下的自解释提示（2026-08-18：老板实录「这个没有什么作用啊」——
    # 空态只说「暂无快照」没人知道快照从哪来）。
    "diff_js_008": "快照会在后台保存「话术模板 / 回复策略」等配置时自动生成；保存过一次后，这里即可对比历史版本并一键回滚。",
    # 空态 CTA：解释之外给一个能点的出口（话术模板页就是产生快照的地方）。
    "diff_js_009": "去话术模板页 →",
}

EN = {
    "diff_js_008": "Snapshots are created automatically when reply templates / strategies are saved in the admin UI. Once a save has happened, you can compare versions and roll back here.",
    "diff_js_009": "Open reply templates →",
}
