# -*- coding: utf-8 -*-
"""会话级接管（takeover）词条（驾驶舱 P0 2026-08-13）。

覆盖：① /api/takeover/* 路由错误文案（err.tko.*）；② 工作台会话头
「接管 / 交还」按钮与状态（tko.*）。刻意独立成 pack（共享树多线并发下
新文件＝零撞车，与 surface_fusion pack 同决策）。
"""

ZH = {
    # ── 路由错误 ──
    "err.tko.conv_required": "缺少 conversation_id 参数",
    "err.tko.readonly": "只读账号无权接管/交还会话",
    "err.tko.store_unready": "收件箱存储未就绪",
    "err.tko.not_active": "该会话当前不在人工接管中（可能已被交还）",
    "err.tko.start_failed": "接管失败：{reason}",
    "err.tko.end_failed": "交还失败：{reason}",
    # ── 会话头按钮/状态 ──
    "tko.btn.take": "接管",
    "tko.btn.take_t": "我来聊这个客户：AI 对本会话立即让位（在途草稿取消），去原生页或直接手动回复；完成后记得交还",
    "tko.btn.back": "交还 AI",
    "tko.btn.back_t": "人工处理完毕，本会话交还 AI 自动接管（恢复接管前档位）",
    "tko.confirm_take": "接管本会话？AI 将立即停止自动回复（在途草稿会被取消），直到你交还。",
    "tko.confirm_back": "交还本会话给 AI？将恢复接管前的自动化档位。",
    "tko.took": "已接管，AI 已让位——完成后记得交还",
    "tko.took_native": "已接管并通知桌面壳打开原生页",
    "tko.returned": "已交还 AI（人工接管 {min} 分钟）",
    "tko.failed": "操作失败，请重试",
    "tko.elapsed": "已接管 {min} 分钟",
    # ── 系统标签显示映射（数据值恒中文，仅显示层按界面语言取词）──
    "tko.tag.needs_human": "需人工",
    "tko.tag.takeover": "人工接管中",
}

EN = {
    # ── route errors ──
    "err.tko.conv_required": "Missing required parameter: conversation_id",
    "err.tko.readonly": "Read-only account cannot take over / hand back conversations",
    "err.tko.store_unready": "Inbox store not ready",
    "err.tko.not_active": "This conversation is not under manual takeover (may already be handed back)",
    "err.tko.start_failed": "Takeover failed: {reason}",
    "err.tko.end_failed": "Handback failed: {reason}",
    # ── chat header button / states ──
    "tko.btn.take": "Take over",
    "tko.btn.take_t": "I'll chat with this customer: AI yields on this conversation immediately (in-flight drafts cancelled). Chat on the native tab or reply manually; remember to hand back when done",
    "tko.btn.back": "Hand back to AI",
    "tko.btn.back_t": "Manual handling done — return this conversation to AI (restores the pre-takeover automation mode)",
    "tko.confirm_take": "Take over this conversation? AI stops auto-replying immediately (in-flight drafts get cancelled) until you hand back.",
    "tko.confirm_back": "Hand this conversation back to AI? The pre-takeover automation mode will be restored.",
    "tko.took": "Taken over — AI yielded. Remember to hand back when done",
    "tko.took_native": "Taken over; asked the desktop shell to open the native tab",
    "tko.returned": "Handed back to AI (manual takeover lasted {min} min)",
    "tko.failed": "Operation failed, please retry",
    "tko.elapsed": "Taken over {min} min ago",
    # ── system tag display mapping (data values stay Chinese; display-only) ──
    "tko.tag.needs_human": "Needs human",
    "tko.tag.takeover": "Manual takeover",
}
