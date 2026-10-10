# -*- coding: utf-8 -*-
"""智安发送限速闸（send_rate_gate）拦截文案（2026-10-08）。

两条拦因：预热期号滚动 24h 超建议上限（拦 AI 与坐席）/ 脚本测试流量超每号日上限。
文案原则与 inbox_send_gate 同族：讲成「保护」不是「故障」，说清为什么、几点恢复、现在能做什么；
额度是滚动 24 小时窗口，绝不承诺「明天零点恢复」。独立成 pack：多线并发下新文件零撞车。
繁体人工词条直接写在本 pack 的 ZH_HANT（regen 不覆盖已有人工键）。
"""

ZH = {
    # 409 回执（服务端 tr）
    "err.inbox.send_blocked_rate_warmup":
        "该账号还在预热期，最近 24 小时已发 {used} 条，达到建议上限 {cap} 条；"
        "为防封号，AI 和坐席发送都已暂停。{resume}如确需发送，请用手机发或联系管理员。",
    "err.inbox.send_blocked_rate_script":
        "脚本测试流量已达该账号每日上限（{used}/{cap}），测试发送已暂停，避免把号打封。{resume}",
    "err.inbox.send_rate_resume_at": "预计 {time} 恢复（按最近 24 小时滚动释放）。",
    "err.inbox.send_rate_resume_unknown": "最早一条发送满 24 小时后会自动恢复。",
    # 收件箱横幅（前端 window.Tf）
    "inbox.gate.rate_warmup":
        "预热期保护：该账号最近 24 小时已发 {used}/{cap} 条（建议上限），AI 和坐席发送已暂停，{frees}。"
        "急需联系客户可用手机发送。",
    "inbox.gate.rate_script": "脚本测试已达该账号每日上限（{used}/{cap}），测试发送已暂停，{frees}。",
    "inbox.gate.rate_frees": "预计 {time} 恢复",
    "inbox.gate.rate_frees_unknown": "最早一条满 24 小时后自动恢复",
    # 失败原因短语（消息气泡悬停）
    "inbox.failr.rate_warmup": "预热期限速：已达建议上限，暂停 AI 与坐席发送",
    "inbox.failr.rate_script": "脚本测试已达每号日上限",
    "inbox.gate.rate_degraded": "发送限速闸刚才异常了 {n} 次，这些发送已放行，没有改日上限。请看日志。",
    "inbox.gate.rate_degraded_t": "闸门坏掉时不会把全部发送卡死。次数记在本机限速库里，重启还在；库打不开时只记在本进程。",
}

EN = {
    "err.inbox.send_blocked_rate_warmup":
        "This account is still warming up and has sent {used} messages in the last 24 hours, "
        "reaching the recommended cap of {cap}. To protect it from bans, AI and agent sending is paused. "
        "{resume}If you must reach the customer, send from the phone or contact an admin.",
    "err.inbox.send_blocked_rate_script":
        "Script test traffic has hit this account's daily cap ({used}/{cap}); test sending is paused "
        "so the account does not get banned. {resume}",
    "err.inbox.send_rate_resume_at": "Expected to resume around {time} (rolling 24-hour window).",
    "err.inbox.send_rate_resume_unknown": "Sending resumes automatically once the oldest send is 24 hours old.",
    "inbox.gate.rate_warmup":
        "Warm-up protection: this account sent {used}/{cap} messages in the last 24 hours (recommended cap); "
        "AI and agent sending is paused — {frees}. Use the phone if you need to reach the customer now.",
    "inbox.gate.rate_script": "Script testing hit this account's daily cap ({used}/{cap}); test sending is paused — {frees}.",
    "inbox.gate.rate_frees": "expected to resume around {time}",
    "inbox.gate.rate_frees_unknown": "resumes once the oldest send is 24 hours old",
    "inbox.failr.rate_warmup": "Warm-up rate limit: recommended cap reached, AI and agent sending paused",
    "inbox.failr.rate_script": "Script testing hit the per-account daily cap",
    "inbox.gate.rate_degraded": "The send rate gate failed open {n} time(s). Those sends went through. The daily cap was not raised. Check the log.",
    "inbox.gate.rate_degraded_t": "A broken gate does not freeze every send. The count is stored in the local rate-gate database and survives a restart. If that database cannot be opened, only this process keeps the count.",
}

ZH_HANT = {
    "err.inbox.send_blocked_rate_warmup":
        "該帳號還在預熱期，最近 24 小時已發 {used} 則，達到建議上限 {cap} 則；"
        "為防封號，AI 和坐席傳送都已暫停。{resume}如確需傳送，請用手機傳送或聯絡管理員。",
    "err.inbox.send_blocked_rate_script":
        "腳本測試流量已達該帳號每日上限（{used}/{cap}），測試傳送已暫停，避免帳號被封。{resume}",
    "err.inbox.send_rate_resume_at": "預計 {time} 恢復（按最近 24 小時滾動釋放）。",
    "err.inbox.send_rate_resume_unknown": "最早一則傳送滿 24 小時後會自動恢復。",
    "inbox.gate.rate_warmup":
        "預熱期保護：該帳號最近 24 小時已發 {used}/{cap} 則（建議上限），AI 和坐席傳送已暫停，{frees}。"
        "急需聯絡客戶可用手機傳送。",
    "inbox.gate.rate_script": "腳本測試已達該帳號每日上限（{used}/{cap}），測試傳送已暫停，{frees}。",
    "inbox.gate.rate_frees": "預計 {time} 恢復",
    "inbox.gate.rate_frees_unknown": "最早一則滿 24 小時後自動恢復",
    "inbox.failr.rate_warmup": "預熱期限速：已達建議上限，暫停 AI 與坐席傳送",
    "inbox.failr.rate_script": "腳本測試已達每帳號日上限",
    "inbox.gate.rate_degraded": "傳送限速閘剛才異常了 {n} 次，這些傳送已放行，沒有改日上限。請看日誌。",
    "inbox.gate.rate_degraded_t": "閘門壞掉時不會把全部傳送卡住。次數記在本機限速庫裡，重啟還在；庫打不開時只記在本行程。",
}
