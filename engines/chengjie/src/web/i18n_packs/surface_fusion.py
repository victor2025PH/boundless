# -*- coding: utf-8 -*-
"""双面板融合（surface fusion）词条（P0 2026-08-13）。

覆盖三块：① /api/surface/* 路由错误文案（err.sf.*）；② 能力注册表标签
（sf.cap.*，与 src/integrations/surface_fusion.py 的 capability id 一一对应，
门禁 test_surface_fusion 钉住双语齐平）；③ 工作台会话头「驾驶权徽章 +
去原生页」前端词条（sf.pilot.* / sf.native.*）。

**刻意独立成 pack**（与 inbox_send_gate 同决策）：共享树多线并发下新文件＝零撞车。
"""

ZH = {
    # ── 路由错误 ──
    "err.sf.platform_required": "缺少 platform 参数",
    "err.sf.owner_invalid": "驾驶权持有者非法：{owner}（只能是 workspace 或 native）",
    "err.sf.readonly": "只读账号无权切换驾驶权",
    "err.sf.write_failed": "驾驶权写入失败，请稍后重试",
    # ── 能力注册表标签（sf.cap.<capability_id>）──
    "sf.cap.send_text": "发送文字",
    "sf.cap.send_media": "发送图片/语音/视频",
    "sf.cap.translate_text": "文字翻译",
    "sf.cap.translate_media": "图片/语音翻译",
    "sf.cap.ai_draft": "AI 草稿/智能回复",
    "sf.cap.message_requests": "消息请求处置",
    "sf.cap.e2ee_pin": "端到端加密 PIN",
    "sf.cap.autosend": "全自动回复",
    "sf.cap.mark_read": "已读回执",
    "sf.cap.typing": "正在输入指示",
    "sf.cap.reaction_out": "表情回应",
    "sf.cap.quote_reply": "引用回复",
    "sf.cap.forward": "转发",
    "sf.cap.pin_manage": "置顶/会话管理",
    "sf.cap.report": "举报",
    "sf.cap.calls": "语音/视频通话",
    # ── 工作台会话头 ──
    "sf.native.open_btn": "原生页打开",
    "sf.native.open_btn_t": "在桌面壳的官方网页标签中打开此会话（通话/转发/置顶等官方功能在那边）",
    "sf.native.open_btn_t_caps": "在桌面壳的官方网页标签中打开此会话——这些官方功能在那边：{caps}",
    "sf.native.open_sent": "已通知桌面壳打开原生页",
    # ── 能力总览浮层（P1）：把「工作台 vs 原生页」的能力区别讲给坐席 ──
    "sf.caps.btn_t": "双面板能力对照：工作台能做什么、原生页能做什么、缺的去哪",
    "sf.caps.title": "双面板能力对照",
    "sf.caps.capability": "能力",
    "sf.caps.workspace": "工作台",
    "sf.caps.native": "原生页",
    "sf.caps.legend": "● 可用　◐ 辅助（翻译/草稿）　↗ 去原生页　· 暂无。全自动回复是工作台专属；引用回复/表情回应/已读两边都支持；「正在输入」指示目前仅原生页（网页版无此接口）。",
    "sf.pilot.workspace": "托管：工作台",
    "sf.pilot.native": "托管：原生页",
    "sf.pilot.t": "该账号的自动化持有者。同一账号同一时刻只有一处在自动发送——工作台托管＝AI 全自动在统一收件箱；原生页托管＝工作台自动链让位。点击切换。",
    "sf.pilot.confirm_to_native": "切换为「原生页托管」？工作台的 AI 全自动将对该账号让位（待发草稿会被取消）。",
    "sf.pilot.confirm_to_workspace": "切换为「工作台托管」？该账号恢复统一收件箱 AI 全自动发送。",
    "sf.pilot.switched": "驾驶权已切换",
    "sf.pilot.switch_failed": "驾驶权切换失败",
}

EN = {
    # ── route errors ──
    "err.sf.platform_required": "Missing required parameter: platform",
    "err.sf.owner_invalid": "Invalid pilot owner: {owner} (must be workspace or native)",
    "err.sf.readonly": "Read-only account cannot switch pilot ownership",
    "err.sf.write_failed": "Failed to persist pilot ownership, please retry",
    # ── capability labels ──
    "sf.cap.send_text": "Send text",
    "sf.cap.send_media": "Send image/voice/video",
    "sf.cap.translate_text": "Text translation",
    "sf.cap.translate_media": "Image/voice translation",
    "sf.cap.ai_draft": "AI drafts / smart reply",
    "sf.cap.message_requests": "Message requests",
    "sf.cap.e2ee_pin": "E2EE PIN",
    "sf.cap.autosend": "Full-auto reply",
    "sf.cap.mark_read": "Read receipts",
    "sf.cap.typing": "Typing indicator",
    "sf.cap.reaction_out": "Reactions",
    "sf.cap.quote_reply": "Quote reply",
    "sf.cap.forward": "Forward",
    "sf.cap.pin_manage": "Pin / conversation management",
    "sf.cap.report": "Report",
    "sf.cap.calls": "Voice / video calls",
    # ── workspace conversation header ──
    "sf.native.open_btn": "Open in native tab",
    "sf.native.open_btn_t": "Open this conversation in the embedded official web tab (calls / forward / pin live there)",
    "sf.native.open_btn_t_caps": "Open this conversation in the embedded official web tab — these official features live there: {caps}",
    "sf.native.open_sent": "Asked the desktop shell to open the native tab",
    # ── capability overview popover (P1) ──
    "sf.caps.btn_t": "Dual-surface capability map: what Workspace can do, what Native can do, where the rest lives",
    "sf.caps.title": "Dual-surface capabilities",
    "sf.caps.capability": "Capability",
    "sf.caps.workspace": "Workspace",
    "sf.caps.native": "Native",
    "sf.caps.legend": "● available   ◐ assist (translate/draft)   ↗ go to native tab   · not yet. Full-auto reply is Workspace-only; quote reply / reactions / read receipts work on both; typing indicator is Native-only (web has no such API).",
    "sf.pilot.workspace": "Pilot: Workspace",
    "sf.pilot.native": "Pilot: Native",
    "sf.pilot.t": "Automation owner for this account. Only one surface auto-sends at any time — Workspace = AI full-auto in the unified inbox; Native = workspace auto-send yields. Click to switch.",
    "sf.pilot.confirm_to_native": "Switch pilot to Native? Workspace AI full-auto will yield for this account (pending drafts get cancelled).",
    "sf.pilot.confirm_to_workspace": "Switch pilot to Workspace? AI full-auto resumes in the unified inbox for this account.",
    "sf.pilot.switched": "Pilot ownership switched",
    "sf.pilot.switch_failed": "Failed to switch pilot ownership",
}
