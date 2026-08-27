# -*- coding: utf-8 -*-
"""客服支持通道词条域（实施49 P1-9，2026-08-20）。

内测反馈：出错时用户只会说「打不开」，客服要机器码/版本/日志得口述半小时。
本域覆盖三个入口的文案——「关于与支持」面板（机器码常显可复制）、报错 toast 内嵌
的「报障」入口、帮助球菜单项——三者共用同一套键，文案口径不会分叉。

⚠ 文案纪律：**只说用户能执行的下一步**。上传成功后的主语是「把这个编码告诉客服」，
不是「上传成功」——后者对用户是无动作信息。
"""

ZH = {
    # ── 面板骨架 ──────────────────────────────────────────────────────────
    # 实施51（2026-08-21）：面板从「关于页附带上传」翻转为「报障优先」——标题、
    # 引导语、成功态全部按「已自动送达客服」的真实链路措辞（官网收包即推送客服
    # TG，用户**不需要**再转告 6 位码；回执号只是核对用）。
    "sup.title": "求助与支持",
    "sup.close": "关闭",
    "sup.product": "产品",
    "sup.version": "版本",
    "sup.machine": "机器码",
    "sup.site": "官网",
    "sup.copy": "复制",
    "sup.copied": "已复制",
    "sup.copy_all": "复制全部信息",
    "sup.loading": "加载中…",
    "sup.about.toggle": "详细信息（版本 / 机器码）",

    # ── 诊断直传 ──────────────────────────────────────────────────────────
    "sup.diag.hint": "遇到问题？点「发给客服」——日志与配置自动打包送达客服，之后不需要你再做任何事。",
    "sup.diag.privacy": "只上传运行日志与配置，不含聊天内容与账号密码。",
    "sup.desc.ph": "（可选）一句话描述发生了什么，例：点「发送」没反应",
    "sup.diag.btn": "🩺 一键发给客服",
    "sup.diag.busy": "正在打包发送…",
    "sup.diag.sent": "已送达客服",
    "sup.diag.code_lbl": "回执号",
    "sup.last.prefix": "上次报障",
    "sup.diag.expect": "客服核对时报回执号即可；工作时间内通常几分钟内响应。",
    "sup.diag.ok": "已送达客服，回执号：",
    "sup.diag.fail": "发送失败，请检查网络后重试；也可点「复制全部信息」发给客服。",
    "sup.diag.off": "当前版本不支持诊断直传，请点「复制全部信息」发给客服。",

    # ── 入口（顶栏 / toast / 菜单 / 命令面板）─────────────────────────────
    "sup.cta.header": "求助",
    "sup.cta.header_t": "遇到问题？一键把现场发给客服",
    "sup.cta.banner": "一键报给客服",
    "sup.cta.toast": "报障",
    "sup.cta.toast_title": "把这次报错的诊断信息一键发给客服",
    # 菜单项**不带 emoji**：坐席顶栏菜单的图标位走 ui_icons.js（data-ui-icon="help"），
    # 词条里再塞一个 🩺 就是双图标，且会顶穿 workspace 模板的 emoji 棘轮（该棘轮的存在
    # 理由＝控件图标要 SVG 单一事实源）。面板内主按钮 sup.diag.btn 不在棘轮范围、且那里
    # 没有图标位，故保留 🩺。
    "sup.cta.menu": "求助 / 发诊断给客服",
}

EN = {
    "sup.title": "Help & Support",
    "sup.close": "Close",
    "sup.product": "Product",
    "sup.version": "Version",
    "sup.machine": "Machine code",
    "sup.site": "Website",
    "sup.copy": "Copy",
    "sup.copied": "Copied",
    "sup.copy_all": "Copy all details",
    "sup.loading": "Loading…",
    "sup.about.toggle": "Details (version / machine code)",

    "sup.diag.hint": "Hit a problem? Press “Send to support” — logs and config are packed and delivered automatically. Nothing else to do.",
    "sup.diag.privacy": "Only runtime logs and config are uploaded — no chat content, no credentials.",
    "sup.desc.ph": "(Optional) One line about what happened, e.g. “Send button does nothing”",
    "sup.diag.btn": "🩺 Send to support",
    "sup.diag.busy": "Packing and sending…",
    "sup.diag.sent": "Delivered to support",
    "sup.diag.code_lbl": "Reference code",
    "sup.last.prefix": "Last report",
    "sup.diag.expect": "Quote the reference code if support asks; replies usually arrive within minutes during working hours.",
    "sup.diag.ok": "Delivered to support. Reference code:",
    "sup.diag.fail": "Sending failed. Check the network and retry, or use “Copy all details” and send it to support.",
    "sup.diag.off": "This build cannot upload diagnostics. Use “Copy all details” and send it to support.",

    "sup.cta.header": "Help",
    "sup.cta.header_t": "Hit a problem? Send the scene to support in one click",
    "sup.cta.banner": "Report to support",
    "sup.cta.toast": "Report",
    "sup.cta.toast_title": "Send diagnostics for this error to support in one click",
    "sup.cta.menu": "Help / send diagnostics",
}
