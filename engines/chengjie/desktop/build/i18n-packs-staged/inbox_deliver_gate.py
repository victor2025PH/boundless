# -*- coding: utf-8 -*-
"""收件箱「真发总闸」应急态可见化词条（#142，2026-09-02，归 #63 族）。

#140/#142（钧 0902 实锤）：总闸关着时「会话档=全自动」的会话静默不发，界面
只有顶栏一个小标签——用户体感「自动回复坏了」。本 pack 承载三件整改的前端
词条：① 会话视图内显式拦截提示 + 一键恢复；② 全局醒目横幅（谁关的/何时/
几点自动恢复）；③ 翻动来源的人话标签。

文案原则（与 inbox_effective_mode 同族）：把拦截讲成「现状 + 为什么 + 怎么
立即恢复」，绝不只说「不发」。
"""

ZH = {
    # ── 全局应急态横幅（顶栏醒目横幅，非小标签）──
    "inbox.dpause.global": "⏸️ 全局已暂停真发：全自动会话只拟稿、不外发"
                           "（Telegram 私聊直答不受影响）",
    "inbox.dpause.global_by": "{time} 由 {actor} 经「{src}」关闭",
    "inbox.dpause.auto_resume": "将于 {time} 自动恢复",
    # ── 会话视图内拦截提示（#142 件一）──
    "inbox.dpause.conv": "本会话未真发：全局已暂停真发——AI 草稿照常在写，"
                         "但不会自动发出",
    "inbox.dpause.conv_t": "这个会话的档位是全自动/多选，但全局「真发总闸」"
                           "当前关闭（{reason}），自动投递被拦下：AI 只写"
                           "草稿、不外发。恢复总闸后即恢复自动发送。",
    # ── 一键恢复入口 ──
    "inbox.dpause.resume_btn": "▶ 恢复真发",
    "inbox.dpause.resume_confirm": "恢复真发总闸：全自动会话将立即恢复 AI "
                                   "自动发送（发送前仍过安全闸与额度护栏）。确定？",
    "inbox.dpause.resume_ok": "✅ 真发总闸已恢复，全自动会话恢复自动发送",
    "inbox.dpause.resume_fail": "恢复失败：{msg}",
    "inbox.dpause.need_supervisor": "恢复真发需主管权限，请联系管理员",
    # ── 翻动来源人话标签（与设置页同口径）──
    "inbox.dpause.src.standby": "设置页一键三档",
    "inbox.dpause.src.split": "设置页拆开控制",
    "inbox.dpause.src.preset": "能力看板预设",
    "inbox.dpause.src.rollback": "一键回滚",
    "inbox.dpause.src.banner": "收件箱横幅一键恢复",
    "inbox.dpause.src.auto": "自动过期恢复",
    "inbox.dpause.src.unknown": "未知入口",
}

EN = {
    "inbox.dpause.global": "⏸️ Auto-send paused globally: full-auto "
                           "conversations draft only, nothing goes out "
                           "(Telegram DMs unaffected)",
    "inbox.dpause.global_by": "closed by {actor} at {time} via \"{src}\"",
    "inbox.dpause.auto_resume": "auto-resumes at {time}",
    "inbox.dpause.conv": "This conversation is not sending: auto-send is "
                         "paused globally — the AI keeps drafting but "
                         "nothing goes out automatically",
    "inbox.dpause.conv_t": "This conversation is set to full-auto/multi-choice, "
                           "but the global deliver master switch is currently "
                           "off ({reason}), so automatic delivery is "
                           "intercepted: the AI only drafts. Resume the "
                           "switch to restore auto-sending.",
    "inbox.dpause.resume_btn": "▶ Resume auto-send",
    "inbox.dpause.resume_confirm": "Resume the deliver master switch: "
                                   "full-auto conversations immediately go "
                                   "back to AI auto-sending (safety gates and "
                                   "quota guards still apply). Continue?",
    "inbox.dpause.resume_ok": "✅ Deliver master switch resumed; full-auto "
                              "conversations are auto-sending again",
    "inbox.dpause.resume_fail": "Resume failed: {msg}",
    "inbox.dpause.need_supervisor": "Resuming requires supervisor permission; "
                                    "please contact an admin",
    "inbox.dpause.src.standby": "settings page one-click presets",
    "inbox.dpause.src.split": "settings page split controls",
    "inbox.dpause.src.preset": "capability board preset",
    "inbox.dpause.src.rollback": "one-click rollback",
    "inbox.dpause.src.banner": "inbox banner quick resume",
    "inbox.dpause.src.auto": "auto-expiry resume",
    "inbox.dpause.src.unknown": "unknown entry",
}
