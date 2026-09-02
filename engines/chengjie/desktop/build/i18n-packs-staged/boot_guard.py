# -*- coding: utf-8 -*-
"""全局前端错误守卫（_boot_error_guard.html）词条域。"""

ZH = {
    "fe.guard.page_error": "页面脚本出错，部分功能可能失效",
    # 网络型 fetch 失败（后端重启/断网瞬断）≠脚本 bug：黄条措辞只说「重试中会自愈」，
    # 不吓人不甩锅（2026-09-01 WEXX7E：重启瞬断被红条误报成脚本出错）
    "fe.guard.net_retry": "连接中，正在重试…恢复后本提示自动消失",
}

EN = {
    "fe.guard.page_error": "A page script error occurred; some features may not work",
    "fe.guard.net_retry": "Reconnecting, retrying automatically… this notice clears itself once the connection is back",
}
