# -*- coding: utf-8 -*-
"""AI 让位词条（Q-18 #292 #291，2026-09-11）。键族 ``inbox.ay.*``（收件箱会话头「AI 让位中 · N 秒后接回」
chip、点击接回 toast、体检面板一键接回按钮）与 ``inbox.diag.agent_yield``（reply_diagnosis finding）。

繁体手写 ZH_HANT（不碰生成物 zh_hant_auto）。占位符：``{by}`` = 「坐席 60s 内发过 / 打过字」，
``{n}`` = 剩余秒数，``{until_hhmm}`` = 接回时刻，``{by_label}`` 同 ``{by}``（诊断文案用）。
"""

ZH = {
    "inbox.ay.by_sent": "坐席 60s 内发过",
    "inbox.ay.by_typing": "坐席 60s 内打过字",
    "inbox.ay.chip": "AI 让位中 · {by} · {n}s 后接回",
    "inbox.ay.chip_t": "{by}，AI 稿先等一等、窗过自动发（不丢稿）。点这里 = 立即让 AI 接回",
    "inbox.ay.resumed": "AI 已接回，待发稿立即放行",
    "inbox.ay.resume_fail": "接回失败，稍后重试",
    "inbox.ay.fix_resume": "立即让 AI 接回",
    "inbox.diag.agent_yield": "AI 让位中：{by_label}，稿留队等到 {until_hhmm}（约 {n}s）自动复检再发，不是丢弃；切全自动或点会话头 chip 可立即接回",
}

EN = {
    "inbox.ay.by_sent": "agent sent within 60s",
    "inbox.ay.by_typing": "agent typed within 60s",
    "inbox.ay.chip": "AI yielding · {by} · resumes in {n}s",
    "inbox.ay.chip_t": "{by}; the AI draft waits and auto-sends after the window (not dropped). Click = let AI resume now",
    "inbox.ay.resumed": "AI resumed; queued draft released",
    "inbox.ay.resume_fail": "Resume failed, try again",
    "inbox.ay.fix_resume": "Let AI resume now",
    "inbox.diag.agent_yield": "AI yielding: {by_label}. The draft stays queued until {until_hhmm} (~{n}s), then is re-checked and sent — not dropped. Switch to Full Auto or click the header chip to resume now",
}

ZH_HANT = {
    "inbox.ay.by_sent": "坐席 60s 內發過",
    "inbox.ay.by_typing": "坐席 60s 內打過字",
    "inbox.ay.chip": "AI 讓位中 · {by} · {n}s 後接回",
    "inbox.ay.chip_t": "{by}，AI 稿先等一等、窗過自動發（不丟稿）。點這裡 = 立即讓 AI 接回",
    "inbox.ay.resumed": "AI 已接回，待發稿立即放行",
    "inbox.ay.resume_fail": "接回失敗，稍後重試",
    "inbox.ay.fix_resume": "立即讓 AI 接回",
    "inbox.diag.agent_yield": "AI 讓位中：{by_label}，稿留隊等到 {until_hhmm}（約 {n}s）自動複檢再發，不是丟棄；切全自動或點會話頭 chip 可立即接回",
}
