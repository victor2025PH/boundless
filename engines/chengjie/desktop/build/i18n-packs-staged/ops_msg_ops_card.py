# -*- coding: utf-8 -*-
"""ops-overview「🗑️ 消息管理审计」卡词条（消息管理 P2 2026-08-17）。

数据源＝``GET /api/admin/msg-ops-stats``（ops_events 台账 7 天窗聚合）——
「前台删除、后台留痕」故事的读数面：谁删了什么/撤回成功率/失败归因。
**刻意独立成 pack**（新文件＝零撞车，与 ops_fleet_health_card 同惯例）。
"""

ZH = {
    "ov2_s_msgops": "消息管理审计（谁删了什么 · 撤回成功率）",
    "ov2_mo_sub": "ops_events 台账 7 天窗（双端撤回/仅工作台删除/清空记录/删除会话/回收站恢复全量留痕，"
                  "软删数据后台保留可追溯）；失败归因分布是「要不要做撤回时限预判」的判据。零操作整卡隐藏",
    "ov2_mo_revoke": "双端撤回",
    "ov2_mo_revoke_ok": "撤回成功率",
    "ov2_mo_del": "删除（仅工作台）",
    "ov2_mo_clear": "清空记录",
    "ov2_mo_delconv": "删除会话",
    "ov2_mo_restore": "回收站恢复",
    "ov2_mo_fail_reasons": "撤回失败归因",
    "ov2_mo_recent": "最近操作",
    "ov2_mo_col_time": "时间",
    "ov2_mo_col_kind": "操作",
    "ov2_mo_col_who": "操作人",
    "ov2_mo_col_where": "会话",
    "ov2_mo_col_n": "条数",
    "ov2_mo_col_result": "结果",
    "ov2_mo_ok": "成功",
    "ov2_mo_k_msg_revoke": "撤回",
    "ov2_mo_k_msg_delete_local": "删除(工作台)",
    "ov2_mo_k_conv_clear": "清空",
    "ov2_mo_k_conv_clear_remote": "清空(双方)",
    "ov2_mo_k_conv_delete": "删会话",
    "ov2_mo_k_msg_restore": "恢复",
}

EN = {
    "ov2_s_msgops": "Message-ops audit (who deleted what · revoke success)",
    "ov2_mo_sub": "ops_events ledger, 7-day window (delete-for-everyone / workspace deletes / history clears / chat deletes / "
                  "recycle-bin restores all logged; soft-deleted data kept for audit). Failure-reason mix drives the "
                  "\"revoke time-limit pre-check\" decision. Card hides with zero activity",
    "ov2_mo_revoke": "Delete for everyone",
    "ov2_mo_revoke_ok": "Revoke success",
    "ov2_mo_del": "Workspace deletes",
    "ov2_mo_clear": "History clears",
    "ov2_mo_delconv": "Chats deleted",
    "ov2_mo_restore": "Recycle restores",
    "ov2_mo_fail_reasons": "Revoke failure reasons",
    "ov2_mo_recent": "Recent operations",
    "ov2_mo_col_time": "Time",
    "ov2_mo_col_kind": "Action",
    "ov2_mo_col_who": "Actor",
    "ov2_mo_col_where": "Conversation",
    "ov2_mo_col_n": "Count",
    "ov2_mo_col_result": "Result",
    "ov2_mo_ok": "ok",
    "ov2_mo_k_msg_revoke": "Revoke",
    "ov2_mo_k_msg_delete_local": "Delete (ws)",
    "ov2_mo_k_conv_clear": "Clear",
    "ov2_mo_k_conv_clear_remote": "Clear (both)",
    "ov2_mo_k_conv_delete": "Delete chat",
    "ov2_mo_k_msg_restore": "Restore",
}
