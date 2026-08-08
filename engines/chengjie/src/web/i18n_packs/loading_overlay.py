# -*- coding: utf-8 -*-
"""「早绘制载入层」词条（P0 感知性能，2026-08-07）。

独立成 pack（照 alert_link_ops / tenant_ops_card 先例）：base.html 与
workspace_base.html 两个基础模板共用，避开被并行工作流频繁编辑的大 pack。
消费方：templates/base.html + templates/workspace_base.html 顶部载入层。
"""

ZH = {
    "ldov_title": "智聊 ChatX",
    "ldov_s1": "正在连接…",
    "ldov_s2": "载入界面…",
    "ldov_s3": "准备数据…",
    "ldov_slow": "首次载入较慢，再次打开会快很多",
}

EN = {
    "ldov_title": "ChatX",
    "ldov_s1": "Connecting…",
    "ldov_s2": "Loading interface…",
    "ldov_s3": "Preparing data…",
    "ldov_slow": "First load is slower — it will be much faster next time",
}
