# -*- coding: utf-8 -*-
"""Messenger 网页代发链止血域词条（Q-24 #298，2026-09-12）。

工作台发送失败红字三段式「原因 · 已自动重试 N 次 · 出路」+ 重试按钮、
待重试队列铃铛、e2ee PIN 会话头黄条、手机端发出（origin=external）角标、
出站媒体「我方发出的图」标签。七码与 services/messenger-web/send_chain.js 同源。
独立成包：inbox_workspace.py 为 Q-20 热区，本线不碰。
"""

ZH = {
    # 三段式第一段：按边车七码出「原因」
    "inbox.ms.fail.composer_detached": "Messenger 输入框丢失",
    "inbox.ms.fail.thread_not_found": "Messenger 会话页没打开",
    "inbox.ms.fail.e2ee_pin_pending": "Messenger 加密会话未解锁（需在手机确认 PIN）",
    "inbox.ms.fail.call_overlay": "Messenger 通话浮层挡住了输入框",
    "inbox.ms.fail.send_backoff": "Messenger 连续失败，暂停发送中",
    "inbox.ms.fail.login_expired": "Messenger 登录已失效",
    "inbox.ms.fail.upload_failed": "Messenger 附件没挂上",
    # 第二段：自动重试次数
    "inbox.ms.fail.retried": "已自动重试 {n} 次",
    "inbox.ms.fail.retried_none": "未自动重试",
    # 第三段：出路
    "inbox.ms.fail.next_phone": "你可以手机发，或 {sec} 秒后再试",
    "inbox.ms.fail.next_phone_now": "你可以手机发，或稍后再试",
    "inbox.ms.fail.next_pin": "请在手机 Messenger 确认 PIN 后重试",
    "inbox.ms.fail.next_login": "请在账号页重新登录后重试",
    "inbox.ms.fail.next_backoff": "{sec} 秒后自动恢复，也可以手机发",
    "inbox.ms.fail.retry_btn": "重试",
    "inbox.ms.fail.retrying": "重试中…",
    # 待重试队列铃铛（通知中心 sys_status）
    "inbox.ms.bell.send_stuck": "Messenger 发送卡住，已排队重试（{name}）",
    "inbox.ms.bell.send_recovered": "Messenger 排队重试已送达（{name}）",
    "inbox.ms.bell.send_stuck_final": "Messenger 排队重试 3 次仍失败，请手机发（{name}）",
    # e2ee PIN 会话头黄条
    "inbox.ms.pin_bar": "Messenger 需在手机确认 PIN 后才能同步",
    "inbox.ms.pin_bar_sub": "加密会话未解锁：新消息与历史暂时收不到，PIN 通过后自动补抄",
    # 手机端发出的消息回抄（direction=out origin=external）
    "inbox.ms.external_out": "手机发出",
    "inbox.ms.external_out_t": "这条是在手机 Messenger 上发的，已回抄到工作台（不触发 AI 让位）",
    # 出站媒体上下文标签（F 段）
    "inbox.ms.media_out_label": "[我方发出的图片]",
    # 反应入站角标
    "inbox.ms.reaction_in": "对方回应了 {emoji}",
    # 体检面板 finding（reply_diagnosis sidecar_send_fail）
    "inbox.diag.sidecar_send_fail": "Messenger 边车发送失败：{code_label}（{hhmm}，近 24h 共 {n} 条未发出，文案「{preview}」）——稿生成了但客户没收到；可在气泡上点「重试」或手机发",
}

EN = {
    "inbox.ms.fail.composer_detached": "Messenger composer lost",
    "inbox.ms.fail.thread_not_found": "Messenger thread did not open",
    "inbox.ms.fail.e2ee_pin_pending": "Messenger encrypted chat locked (confirm PIN on phone)",
    "inbox.ms.fail.call_overlay": "A Messenger call overlay is covering the composer",
    "inbox.ms.fail.send_backoff": "Messenger sending paused after repeated failures",
    "inbox.ms.fail.login_expired": "Messenger login expired",
    "inbox.ms.fail.upload_failed": "Messenger attachment failed to upload",
    "inbox.ms.fail.retried": "auto-retried {n}×",
    "inbox.ms.fail.retried_none": "not auto-retried",
    "inbox.ms.fail.next_phone": "send from your phone, or retry in {sec}s",
    "inbox.ms.fail.next_phone_now": "send from your phone, or retry shortly",
    "inbox.ms.fail.next_pin": "confirm the PIN in Messenger on your phone, then retry",
    "inbox.ms.fail.next_login": "re-login on the accounts page, then retry",
    "inbox.ms.fail.next_backoff": "auto-resumes in {sec}s; you can also send from your phone",
    "inbox.ms.fail.retry_btn": "Retry",
    "inbox.ms.fail.retrying": "Retrying…",
    "inbox.ms.bell.send_stuck": "Messenger send stuck — queued for retry ({name})",
    "inbox.ms.bell.send_recovered": "Messenger queued retry delivered ({name})",
    "inbox.ms.bell.send_stuck_final": "Messenger retry failed 3× — please send from phone ({name})",
    "inbox.ms.pin_bar": "Messenger needs the PIN confirmed on your phone before it can sync",
    "inbox.ms.pin_bar_sub": "Encrypted chat locked: new and past messages are not synced yet; backfills automatically once the PIN passes",
    "inbox.ms.external_out": "Sent from phone",
    "inbox.ms.external_out_t": "Sent from Messenger on the phone and mirrored here (does not trigger AI hand-off)",
    "inbox.ms.media_out_label": "[image sent by us]",
    "inbox.ms.reaction_in": "They reacted {emoji}",
    "inbox.diag.sidecar_send_fail": "Messenger sidecar send failed: {code_label} ({hhmm}; {n} undelivered in the last 24h, text \"{preview}\") — the draft was generated but the customer never got it; click Retry on the bubble or send from your phone",
}

ZH_HANT = {
    "inbox.ms.fail.composer_detached": "Messenger 輸入框遺失",
    "inbox.ms.fail.thread_not_found": "Messenger 會話頁沒打開",
    "inbox.ms.fail.e2ee_pin_pending": "Messenger 加密會話未解鎖（需在手機確認 PIN）",
    "inbox.ms.fail.call_overlay": "Messenger 通話浮層擋住了輸入框",
    "inbox.ms.fail.send_backoff": "Messenger 連續失敗，暫停傳送中",
    "inbox.ms.fail.login_expired": "Messenger 登入已失效",
    "inbox.ms.fail.upload_failed": "Messenger 附件沒掛上",
    "inbox.ms.fail.retried": "已自動重試 {n} 次",
    "inbox.ms.fail.retried_none": "未自動重試",
    "inbox.ms.fail.next_phone": "你可以手機發，或 {sec} 秒後再試",
    "inbox.ms.fail.next_phone_now": "你可以手機發，或稍後再試",
    "inbox.ms.fail.next_pin": "請在手機 Messenger 確認 PIN 後重試",
    "inbox.ms.fail.next_login": "請在帳號頁重新登入後重試",
    "inbox.ms.fail.next_backoff": "{sec} 秒後自動恢復，也可以手機發",
    "inbox.ms.fail.retry_btn": "重試",
    "inbox.ms.fail.retrying": "重試中…",
    "inbox.ms.bell.send_stuck": "Messenger 傳送卡住，已排隊重試（{name}）",
    "inbox.ms.bell.send_recovered": "Messenger 排隊重試已送達（{name}）",
    "inbox.ms.bell.send_stuck_final": "Messenger 排隊重試 3 次仍失敗，請手機發（{name}）",
    "inbox.ms.pin_bar": "Messenger 需在手機確認 PIN 後才能同步",
    "inbox.ms.pin_bar_sub": "加密會話未解鎖：新訊息與歷史暫時收不到，PIN 通過後自動補抄",
    "inbox.ms.external_out": "手機發出",
    "inbox.ms.external_out_t": "這條是在手機 Messenger 上發的，已回抄到工作台（不觸發 AI 讓位）",
    "inbox.ms.media_out_label": "[我方發出的圖片]",
    "inbox.ms.reaction_in": "對方回應了 {emoji}",
    "inbox.diag.sidecar_send_fail": "Messenger 邊車傳送失敗：{code_label}（{hhmm}，近 24h 共 {n} 條未發出，文案「{preview}」）——稿生成了但客戶沒收到；可在氣泡上點「重試」或手機發",
}
