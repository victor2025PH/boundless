# -*- coding: utf-8 -*-
"""待审草稿「重新生成”按钮词条（P2 2026-08-09）。

stale 护栏（409 too_stale）把老稿拦下后的出路闭环：卡上一键作废旧稿、按
**当前**对话上下文重走人设产线铸新稿（POST /api/drafts/{id}/regenerate）。
**刻意独立成 pack**（共享树协议：新文件＝零撞车）。
"""

ZH = {
    "inbox.draft_mini.regen": "🔁 重新生成",
    "inbox.draft_mini.regen_t": "作废这条过期草稿，按当前对话重新生成一条（不会自动发送，仍需人审）",
    "inbox.draft_mini.regen_busy": "生成中…",
    "inbox.draft_mini.regen_ok": "已按当前对话重新生成草稿",
    "inbox.draft_mini.regen_fail": "重新生成失败，稍后重试或用「编辑」改写",
    "inbox.draft_mini.regen_conflict": "这条草稿刚被处理过，已刷新队列",
}

EN = {
    "inbox.draft_mini.regen": "🔁 Regenerate",
    "inbox.draft_mini.regen_t": "Cancel this stale draft and regenerate from the current conversation (won't auto-send; still needs review)",
    "inbox.draft_mini.regen_busy": "Generating…",
    "inbox.draft_mini.regen_ok": "Draft regenerated from the current conversation",
    "inbox.draft_mini.regen_fail": "Regeneration failed; retry later or use Edit",
    "inbox.draft_mini.regen_conflict": "This draft was just handled elsewhere; queue refreshed",
}
