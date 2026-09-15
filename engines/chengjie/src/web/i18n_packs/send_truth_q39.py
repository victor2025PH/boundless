# -*- coding: utf-8 -*-
"""Q-39 发送真相四闸词条（#327 #326 #323 #324，2026-09-15）。

- ``inbox.cs.xlate_hold*``：状态带新状态「出站翻译 HOLD」（conv_state.state=xlate_hold，红）+ 动作
  ``inbox.cs.act.retranslate``（POST /api/unified-inbox/drafts/{id}/retranslate）。
- ``inbox.systag.*``：全部账号视图列表顶 / 筛选面板系统族筹码的人话（#324：``dormant:ignored`` /
  ``risk:*`` / 「客户要求停联」原码不再直出；``_sysTagLabel`` 消费）。
- ``err.inbox.retranslate_*``：重试翻译端点的 4xx/5xx 文案。
繁体手写 ZH_HANT（不碰生成物 zh_hant_auto）。占位符：``{hhmm}`` 时刻；``{n}`` 次数；``{reason}`` 原因码；
``{target}`` 目标语。
"""

ZH = {
    "inbox.cs.xlate_hold": "翻译引擎没回话 · 这条没发 · {hhmm}",
    "inbox.cs.xlate_hold_t": "AI 稿已生成（中文），译成客户语言时引擎空响应 / 超时——同引擎重试、换引擎、按目标语重起草都没救回来，按「不发原文」纪律扣住没发。点「重试翻译」再走一遍翻译链补投",
    "inbox.cs.xlate_hold.other": "翻译结果不可用（{reason}）· 这条没发 · {hhmm}",
    "inbox.cs.xlate_hold.other_t": "译文没过校验（语种不对 / 引擎拒绝 / 残留中文），按「不发原文」纪律扣住。点「重试翻译」再试一次，仍不行就手动改稿发",
    "inbox.cs.act.retranslate": "重试翻译",
    "inbox.cs.act.retranslate_t": "把这条被扣住的稿再过一遍翻译链（重试 / 换引擎 / 重起草），成功即发出",
    "inbox.cs.retranslate_ok": "已重译并发出",
    "inbox.cs.retranslate_fail": "翻译仍失败，这条还是没发",
    # ── #324 系统族筹码人话 ────────────────────────────────────────
    "inbox.systag.dormant": "长期未回",
    "inbox.systag.dormant_t": "系统标：这位客户很久没回了（沉睡）。真来消息自动摘",
    "inbox.systag.risk": "需留意",
    "inbox.systag.risk_t": "系统标：来话命中过风险词，按人设政策处理中。处理完摘标即消",
    "inbox.systag.stop_contact": "别再联系",
    "inbox.systag.stop_contact_t": "系统标：客户要求过停联，AI 对 TA 保持沉默。处理完点「归档并移除」",
    # ── 重试翻译端点 ───────────────────────────────────────────────
    "err.inbox.retranslate_not_on_hold": "这条草稿不是被翻译扣住的（或已补投成功），不能用「重试翻译」",
    "err.inbox.retranslate_worker_missing": "自动发送服务未装载，暂时无法补投（重启后再试）",
}

EN = {
    "inbox.cs.xlate_hold": "Translation engine didn't answer · not sent · {hhmm}",
    "inbox.cs.xlate_hold_t": "The AI draft exists (Chinese) but translating it into the customer's language returned empty / timed out — same-engine retry, engine switch and redrafting in the target language all failed, so it was held under the “never send the untranslated original” rule. Click “Retry translation” to run the chain again and deliver",
    "inbox.cs.xlate_hold.other": "Translation unusable ({reason}) · not sent · {hhmm}",
    "inbox.cs.xlate_hold.other_t": "The translation failed validation (wrong language / engine refusal / leftover Chinese) and was held under the “never send the original” rule. Click “Retry translation”; if it still fails, edit and send manually",
    "inbox.cs.act.retranslate": "Retry translation",
    "inbox.cs.act.retranslate_t": "Run the held draft through the translation chain again (retry / switch engine / redraft); delivered on success",
    "inbox.cs.retranslate_ok": "Re-translated and sent",
    "inbox.cs.retranslate_fail": "Translation still failed; this message is still unsent",
    "inbox.systag.dormant": "Long silent",
    "inbox.systag.dormant_t": "System tag: this customer hasn't replied for a long time (dormant). Clears automatically when they write",
    "inbox.systag.risk": "Watch",
    "inbox.systag.risk_t": "System tag: an inbound hit a risk word; handled by persona policy. Clear the tag once handled",
    "inbox.systag.stop_contact": "Stop contacting",
    "inbox.systag.stop_contact_t": "System tag: the customer asked to stop contact; the AI stays silent. Click “Archive & remove” once handled",
    "err.inbox.retranslate_not_on_hold": "This draft isn't held by translation (or was already delivered); “Retry translation” doesn't apply",
    "err.inbox.retranslate_worker_missing": "The auto-send service isn't loaded; can't redeliver right now (try after a restart)",
}

ZH_HANT = {
    "inbox.cs.xlate_hold": "翻譯引擎沒回話 · 這條沒發 · {hhmm}",
    "inbox.cs.xlate_hold_t": "AI 稿已生成（中文），譯成客戶語言時引擎空回應 / 逾時——同引擎重試、換引擎、按目標語重起草都沒救回來，按「不發原文」紀律扣住沒發。點「重試翻譯」再走一遍翻譯鏈補投",
    "inbox.cs.xlate_hold.other": "翻譯結果不可用（{reason}）· 這條沒發 · {hhmm}",
    "inbox.cs.xlate_hold.other_t": "譯文沒過校驗（語種不對 / 引擎拒絕 / 殘留中文），按「不發原文」紀律扣住。點「重試翻譯」再試一次，仍不行就手動改稿發",
    "inbox.cs.act.retranslate": "重試翻譯",
    "inbox.cs.act.retranslate_t": "把這條被扣住的稿再過一遍翻譯鏈（重試 / 換引擎 / 重起草），成功即發出",
    "inbox.cs.retranslate_ok": "已重譯並發出",
    "inbox.cs.retranslate_fail": "翻譯仍失敗，這條還是沒發",
    "inbox.systag.dormant": "長期未回",
    "inbox.systag.dormant_t": "系統標：這位客戶很久沒回了（沉睡）。真來訊息自動摘",
    "inbox.systag.risk": "需留意",
    "inbox.systag.risk_t": "系統標：來話命中過風險詞，按人設政策處理中。處理完摘標即消",
    "inbox.systag.stop_contact": "別再聯絡",
    "inbox.systag.stop_contact_t": "系統標：客戶要求過停聯，AI 對 TA 保持沉默。處理完點「歸檔並移除」",
    "err.inbox.retranslate_not_on_hold": "這條草稿不是被翻譯扣住的（或已補投成功），不能用「重試翻譯」",
    "err.inbox.retranslate_worker_missing": "自動發送服務未裝載，暫時無法補投（重啟後再試）",
}
