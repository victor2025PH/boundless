# -*- coding: utf-8 -*-
"""R87 #331（X9B22T）词条：「今日拦截」卡子原因人话 + 细节行（2026-09-17）。

节奏页「最近 24 小时」此前把 needs_human 的子原因原码（dup_guard_blocked / empty_reply …）直出在
「命中词 / 阶段」列。这里给每个子原因一句人话（``rps_al_sub_*``），dup 拦截行再补第二行细节
（与哪条相近 · 相似度 · 已改写几次），并给「去会话处理」入口。
"""

ZH = {
    "rps_al_sub_dup_guard_blocked": "检测到回复与近期内容重复，已暂停发送",
    "rps_al_sub_empty_reply": "AI 生成了空回复",
    "rps_al_sub_generate_error": "AI 生成出错",
    "rps_al_sub_send_error": "发送失败",
    "rps_al_sub_quota_hour": "本小时自动回复额度已用完",
    "rps_al_sub_quota_day": "今日自动回复额度已用完",
    "rps_al_sub_circuit_open": "AI 服务熔断保护中",
    "rps_al_sub_off_hours": "非营业时段",
    "rps_al_sub_service_tone": "AI 稿带客服腔，被守卫扣下",
    "rps_al_sub_media_lie_caught_repeat": "客户连说两次没收到图，转人工",
    "rps_al_sub_exposure": "客户疑似识破 AI，转人工",
    "rps_al_sub_crisis": "客户消息触发危机升级",
    "rps_al_dup_detail": "与 {at} 那条相近（相似度 {sim}）· 已自动改写 {n} 次仍相近",
    "rps_al_dup_detail_norw": "与 {at} 那条相近（相似度 {sim}）· 未配置改写链",
    "rps_al_go_conv": "去会话处理",
    "rps_al_go_conv_t": "打开该会话：会话头有「需人工」原因与出路（摘标 / 重拟 / 手发）",
}

EN = {
    "rps_al_sub_dup_guard_blocked": "Reply was too close to what was just sent — paused",
    "rps_al_sub_empty_reply": "AI produced an empty reply",
    "rps_al_sub_generate_error": "AI generation error",
    "rps_al_sub_send_error": "Send failed",
    "rps_al_sub_quota_hour": "Hourly auto-reply quota reached",
    "rps_al_sub_quota_day": "Daily auto-reply quota reached",
    "rps_al_sub_circuit_open": "AI service circuit breaker is open",
    "rps_al_sub_off_hours": "Outside business hours",
    "rps_al_sub_service_tone": "AI draft sounded like support desk — withheld",
    "rps_al_sub_media_lie_caught_repeat": "Customer said twice the photo never arrived — handed to a human",
    "rps_al_sub_exposure": "Customer likely spotted the AI — handed to a human",
    "rps_al_sub_crisis": "Customer message triggered a crisis escalation",
    "rps_al_dup_detail": "Similar to the message at {at} (similarity {sim}) · auto-rewritten {n}× and still similar",
    "rps_al_dup_detail_norw": "Similar to the message at {at} (similarity {sim}) · no rewrite chain configured",
    "rps_al_go_conv": "Open thread",
    "rps_al_go_conv_t": "Open this thread: the header shows the needs-human reason and the exits (clear / redraft / send manually)",
}

ZH_HANT = {
    "rps_al_sub_dup_guard_blocked": "偵測到回覆與近期內容重複，已暫停發送",
    "rps_al_sub_empty_reply": "AI 生成了空回覆",
    "rps_al_sub_generate_error": "AI 生成出錯",
    "rps_al_sub_send_error": "發送失敗",
    "rps_al_sub_quota_hour": "本小時自動回覆額度已用完",
    "rps_al_sub_quota_day": "今日自動回覆額度已用完",
    "rps_al_sub_circuit_open": "AI 服務熔斷保護中",
    "rps_al_sub_off_hours": "非營業時段",
    "rps_al_sub_service_tone": "AI 稿帶客服腔，被守衛扣下",
    "rps_al_sub_media_lie_caught_repeat": "客戶連說兩次沒收到圖，轉人工",
    "rps_al_sub_exposure": "客戶疑似識破 AI，轉人工",
    "rps_al_sub_crisis": "客戶訊息觸發危機升級",
    "rps_al_dup_detail": "與 {at} 那條相近（相似度 {sim}）· 已自動改寫 {n} 次仍相近",
    "rps_al_dup_detail_norw": "與 {at} 那條相近（相似度 {sim}）· 未配置改寫鏈",
    "rps_al_go_conv": "去會話處理",
    "rps_al_go_conv_t": "打開該會話：會話頭有「需人工」原因與出路（摘標 / 重擬 / 手發）",
}
