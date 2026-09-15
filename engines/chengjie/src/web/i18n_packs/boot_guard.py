# -*- coding: utf-8 -*-
"""全局前端错误守卫（_boot_error_guard.html）词条域。"""

ZH = {
    # 2026-09-15 措辞改人话：旧文案「页面脚本出错，部分功能可能失效（_psnArRender）」把内部
    # 标识符直接给用户看、又不说该怎么办。函数名只进 beacon 与报障 note（客服要），红条本身
    # 只说「发生了什么 + 先做什么」。
    "fe.guard.page_error": "这个页面刚出了点问题，部分操作可能没反应——先刷新试试；还不行就点「报障」",
    "fe.guard.reload": "刷新",
    # 网络型 fetch 失败（后端重启/断网瞬断）≠脚本 bug：黄条措辞只说「重试中会自愈」，
    # 不吓人不甩锅（2026-09-01 WEXX7E：重启瞬断被红条误报成脚本出错）
    "fe.guard.net_retry": "连接中，正在重试…恢复后本提示自动消失",
}

EN = {
    "fe.guard.page_error": "Something went wrong on this page and some actions may not respond. Try refreshing first; if it persists, tap “Report”.",
    "fe.guard.reload": "Refresh",
    "fe.guard.net_retry": "Reconnecting, retrying automatically… this notice clears itself once the connection is back",
}
