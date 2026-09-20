# -*- coding: utf-8 -*-
"""收件箱「账号已停用/离线」会话横幅词条（P1-198，2026-08-05）。

198 事故第二课：测试者 16:17 在 UI 手动停用了 Telegram 账号（注册表
status=offline，时间戳与日志精确吻合），此后会话界面毫无痕迹——继续打字、
继续等回复，所有「不回消息」都被归因成「产品坏了」。账号 rail 有 offline
徽章但不够响；本 pack 承载**会话头红条**的词条：明确告知「这个会话此刻
收发都不会送达」，并给一键出路（重新上线 / 去账号栏重扫）。

独立成 pack 的原因与 inbox_budget.py 相同：共享树零撞车。
"""

ZH = {
    "inbox.acct.offline": "该账号已停用/离线——此会话现在收不到消息、发出也不会送达。",
    "inbox.acct.offline_pending": "该账号登录未完成——完成扫码前此会话不会收发消息。",
    "inbox.acct.restart_btn": "重新上线",
    "inbox.acct.restarting": "正在上线…",
    "inbox.acct.restart_ok": "账号已重新上线",
    "inbox.acct.restart_fail": "上线失败——会话可能已失效，请到顶部账号栏重新扫码",
    "inbox.acct.t": "账号在线状态来自账号注册表：停用/掉线的账号不会自动收发，"
                    "重启程序也不会自动拉起（防止误拉被风控的号），需人工恢复上线。",
}

EN = {
    "inbox.acct.offline": "This account is stopped/offline — the conversation "
                          "cannot receive messages now, and anything you send "
                          "will not be delivered.",
    "inbox.acct.offline_pending": "Login for this account is incomplete — the "
                                  "conversation will not send or receive until "
                                  "the QR login finishes.",
    "inbox.acct.restart_btn": "Bring back online",
    "inbox.acct.restarting": "Starting…",
    "inbox.acct.restart_ok": "Account is back online",
    "inbox.acct.restart_fail": "Failed to start — the session may be invalid; "
                               "rescan the QR from the account rail",
    "inbox.acct.t": "Account presence comes from the account registry: stopped/"
                    "offline accounts do not send or receive, and restarts do "
                    "not auto-revive them (protects risk-controlled accounts); "
                    "bring them back manually.",
}
