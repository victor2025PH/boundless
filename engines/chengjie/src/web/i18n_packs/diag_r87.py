# -*- coding: utf-8 -*-
"""R87 P1-2 词条：AI 体检面板「为什么没回」两条新 finding（2026-09-17）。

- ``inbox.diag.route_offline``：会话选了「无限制」而 ChatX聊天模型 端点离线（reply_diagnosis finding，与
  conv_state.route_offline 同源）+ 就地「切回标准」。
- ``inbox.diag.abort_recent``：拦截台账里本会话近 24h 有拦截行 → 面板画时间线（时刻 · 原因 · 细节）。
占位符：``{hhmm}`` 时刻；``{n}`` 条数；``{reason}`` 人话原因；``{detail}`` 细节。
"""

ZH = {
    "inbox.diag.route_offline": "本会话选了「无限制」（ChatX聊天模型），端点 {hhmm} 起连不上（{reason}）· {fallback_txt}",
    "inbox.diag.route_offline_fb_on": "这几轮已按标准档代答，端点恢复后自动回到无限制",
    "inbox.diag.route_offline_fb_off": "离线回退已关（ai.unrestricted.offline_fallback=false），这几轮 AI 没有回复",
    "inbox.diag.fix_route_standard": "切回标准",
    "inbox.diag.abort_recent": "近 24 小时有 {n} 次自动回复被拦下，最近一次 {hhmm}：{reason}{detail_txt}",
    "inbox.diag.abort_timeline": "拦截时间线（新 → 旧）",
    "inbox.diag.abort_row_hit": "对照：{hit}",
    "inbox.cs.dup_detail": "与 {at} 那条相近（相似度 {sim}）· 已自动改写 {n} 次仍相近",
}

EN = {
    "inbox.diag.route_offline": "This thread is set to Unrestricted (ChatX chat model); the endpoint has been unreachable since {hhmm} ({reason}) · {fallback_txt}",
    "inbox.diag.route_offline_fb_on": "recent turns were answered on the Standard profile; it returns to Unrestricted once the endpoint is back",
    "inbox.diag.route_offline_fb_off": "offline fallback is disabled (ai.unrestricted.offline_fallback=false), so the AI did not reply on these turns",
    "inbox.diag.fix_route_standard": "Back to Standard",
    "inbox.diag.abort_recent": "{n} auto-reply attempt(s) were withheld in the last 24h; latest at {hhmm}: {reason}{detail_txt}",
    "inbox.diag.abort_timeline": "Withheld timeline (newest first)",
    "inbox.diag.abort_row_hit": "compared with: {hit}",
    "inbox.cs.dup_detail": "Similar to the message at {at} (similarity {sim}) · auto-rewritten {n}× and still similar",
}

ZH_HANT = {
    "inbox.diag.route_offline": "本會話選了「無限制」（ChatX聊天模型），端點 {hhmm} 起連不上（{reason}）· {fallback_txt}",
    "inbox.diag.route_offline_fb_on": "這幾輪已按標準檔代答，端點恢復後自動回到無限制",
    "inbox.diag.route_offline_fb_off": "離線回退已關（ai.unrestricted.offline_fallback=false），這幾輪 AI 沒有回覆",
    "inbox.diag.fix_route_standard": "切回標準",
    "inbox.diag.abort_recent": "近 24 小時有 {n} 次自動回覆被攔下，最近一次 {hhmm}：{reason}{detail_txt}",
    "inbox.diag.abort_timeline": "攔截時間線（新 → 舊）",
    "inbox.diag.abort_row_hit": "對照：{hit}",
    "inbox.cs.dup_detail": "與 {at} 那條相近（相似度 {sim}）· 已自動改寫 {n} 次仍相近",
}
