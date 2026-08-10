# -*- coding: utf-8 -*-
"""收件箱「接管让位横幅 / AI 状态体检 / 群聊全自动确认」词条（P0 2026-08-09）。

.198/.104 事故沉淀三件套的前端文案：

- **接管横幅**：坐席手动发送触发「接管即静音」后，会话顶部说清「AI 已让位、
  谁触发的、几分钟后自动接回」，并给一键「让 AI 接回」——此前该状态完全不可见，
  两台坐席机的会话在 manual 卡了 27 小时没人知道。
- **AI 状态体检**：why_no_reply CLI 的面板化（GET /api/unified-inbox/why-no-reply
  findings 码 → 本 pack 文案），坐席自己就能回答「为什么这条没自动回」。
- **群聊确认**：群/频道上开全自动需显式确认（AI 会在群里自动发言）。

**刻意独立成 pack**（与 inbox_effective_mode 同款决策）：共享树协议下新文件＝零撞车。
文案原则：讲清「发生了什么 + 系统接下来会怎样 + 我现在能按什么」，绝不只说状态。
"""

ZH = {
    # ── 接管让位横幅 ────────────────────────────────────────────────
    "inbox.takeover.banner": "🙋 AI 已让位：{time} 坐席手动发送后，本会话转人工（后续消息 AI 不再自动回）",
    "inbox.takeover.banner_rearm": "，{min} 分钟无人工操作将自动接回",
    "inbox.takeover.resume_btn": "让 AI 接回",
    "inbox.takeover.resumed": "AI 已接回本会话（{mode}）",
    # ── 群聊全自动确认 ──────────────────────────────────────────────
    "inbox.mode.group_confirm": "这是群聊/频道：开全自动后 AI 会在群里自动发言。确定要开吗？",
    # ── AI 状态体检面板 ─────────────────────────────────────────────
    "inbox.diag.btn": "AI 状态",
    "inbox.diag.btn_t": "AI 回复链路体检：这个会话为什么没自动回 / 下一条入站会不会自动回",
    "inbox.diag.title": "🩺 AI 回复状态体检",
    "inbox.diag.loading": "体检中…",
    "inbox.diag.fail": "体检失败（网络或权限问题），稍后重试",
    "inbox.diag.stale_backend": "后端版本较旧（无体检接口）：升级后可用；先用档位下拉与封顶胶囊判断",
    "inbox.diag.close": "关闭",
    "inbox.diag.recheck": "重新体检",
    # findings 文案（码 → 人话；参数由后端 findings.params 注入）
    "inbox.diag.account_status": "账号状态 {status}（非在线）：worker 不在跑，收发全停。先在账号面板重新上线",
    "inbox.diag.account_missing": "注册表无此账号行（主协议号 / 纯 RPA 会话属正常现象）",
    "inbox.diag.conv_missing": "会话记录不存在——系统从未收到过这位对方的消息，先确认账号在线、消息真的进来了",
    "inbox.diag.takeover_manual": "坐席手动发送后 AI 已让位（接管转人工），后续入站不会自动回",
    "inbox.diag.explicit_non_auto": "会话被显式设为「{mode}」（来源：{source}）——切回全自动才会自动发",
    "inbox.diag.cap_warmup": "新号预热封顶：还剩 {left_h} 小时，期间按「{ceiling}」执行（AI 拟稿、人审后发）",
    "inbox.diag.cap_platform": "平台封顶（{detail}）：实际按「{ceiling}」执行，恢复需运营调整 platform_modes",
    "inbox.diag.cap_business_line": "业务线封顶（{detail}）：实际按「{ceiling}」执行",
    "inbox.diag.guard": "对方机器人守卫将拦下一条入站（{reason}）：{evidence}",
    "inbox.diag.deliver_off": "投递开关关闭（inbox.l2_autosend.deliver=false）：AI 草稿只标记不真发",
    "inbox.diag.l2_off": "自动投递 worker 未启用（inbox.l2_autosend.enabled=false）",
    "inbox.diag.work_schedule": "工作班表扣留自动回复（{hold}）：复班后自动投递/补拟",
    "inbox.diag.pending_drafts": "有 {n} 条待审草稿（最老 {oldest_h} 小时）——AI 一直在拟稿，只是没人点「发送」",
    "inbox.diag.managed_peer": "对端 {peer_account_id} 是本系统受管账号：两个 AI 会互相聊天，谨防无限对话环",
    "inbox.diag.peer_bot_flag": "会话被标记为「对方是机器人」，自动回复受限",
    "inbox.diag.looks_alive": "链路通畅：有效档位「{effective}」，下一条入站会被自动处理",
    "inbox.diag.unknown": "未识别的体检项 {code}（后端比前端新，升级页面后可读）",
    # 修复动作按钮
    "inbox.diag.fix_resume": "让 AI 接回",
    "inbox.diag.fix_auto": "切回全自动",
    "inbox.diag.fix_warmup": "关闭预热人审（本机·主管）",
    "inbox.diag.fix_drafts": "打开待审队列",
    "inbox.diag.warmup_off_ok": "已关闭预热人审：热重载约 30 秒内生效",
    "inbox.diag.warmup_off_fail": "关闭失败（需主管权限或后端过旧）",
    # ── 档位变更时间线（P2 2026-08-09：谁在什么时候把档位改成什么）────────
    "inbox.diag.history_title": "档位变更历史",
    "inbox.diag.src_human": "坐席手选",
    "inbox.diag.src_bootstrap": "新会话按全局默认",
    "inbox.diag.src_guard": "风控守卫降档",
    "inbox.diag.src_sweep": "守卫巡检降档",
    "inbox.diag.src_bulk": "批量操作",
    "inbox.diag.src_takeover": "手动发送接管",
    "inbox.diag.src_rearm": "AI 自动接回",
    "inbox.diag.src_unknown": "未知来源",
}

EN = {
    "inbox.takeover.banner": "🙋 AI stepped aside: after your manual send at {time}, this conversation switched to human (AI no longer auto-replies here)",
    "inbox.takeover.banner_rearm": "; AI re-arms automatically after {min} min without agent activity",
    "inbox.takeover.resume_btn": "Let AI take over again",
    "inbox.takeover.resumed": "AI re-armed for this conversation ({mode})",
    "inbox.mode.group_confirm": "This is a group/channel: with Full-Auto the AI will speak in the group on its own. Enable anyway?",
    "inbox.diag.btn": "AI status",
    "inbox.diag.btn_t": "Reply-chain checkup: why didn't this conversation auto-reply / will the next inbound be auto-replied",
    "inbox.diag.title": "🩺 AI reply checkup",
    "inbox.diag.loading": "Checking…",
    "inbox.diag.fail": "Checkup failed (network or permission); try again later",
    "inbox.diag.stale_backend": "Backend too old (no checkup endpoint): upgrade to use this; meanwhile rely on the mode select and cap chips",
    "inbox.diag.close": "Close",
    "inbox.diag.recheck": "Re-check",
    "inbox.diag.account_status": "Account status {status} (not online): the worker is stopped, nothing is sent or received. Bring the account back online first",
    "inbox.diag.account_missing": "No registry row for this account (normal for the primary protocol account / pure RPA sessions)",
    "inbox.diag.conv_missing": "Conversation not found — the system never received a message from this peer. Check the account is online and messages actually arrive",
    "inbox.diag.takeover_manual": "AI stepped aside after an agent sent manually (takeover mute); new inbound won't be auto-replied",
    "inbox.diag.explicit_non_auto": "Conversation explicitly set to \"{mode}\" (source: {source}) — switch back to Full-Auto to resume auto-replies",
    "inbox.diag.cap_warmup": "New-account warm-up cap: {left_h}h left, running as \"{ceiling}\" (AI drafts, human approves)",
    "inbox.diag.cap_platform": "Platform cap ({detail}): actually running as \"{ceiling}\"; ops must adjust platform_modes to restore",
    "inbox.diag.cap_business_line": "Business-line cap ({detail}): actually running as \"{ceiling}\"",
    "inbox.diag.guard": "Peer-bot guard will block the next inbound ({reason}): {evidence}",
    "inbox.diag.deliver_off": "Delivery switch off (inbox.l2_autosend.deliver=false): AI drafts are marked only, never actually sent",
    "inbox.diag.l2_off": "Autosend worker disabled (inbox.l2_autosend.enabled=false)",
    "inbox.diag.work_schedule": "Work schedule is holding auto-replies ({hold}): they resume when the shift starts",
    "inbox.diag.pending_drafts": "{n} draft(s) awaiting review (oldest {oldest_h}h) — AI kept drafting, nobody pressed Send",
    "inbox.diag.managed_peer": "Peer {peer_account_id} is a managed account of this system: two AIs will chat with each other, beware of loops",
    "inbox.diag.peer_bot_flag": "Conversation flagged as \"peer is a bot\"; auto-replies restricted",
    "inbox.diag.looks_alive": "Chain looks healthy: effective mode \"{effective}\"; the next inbound will be handled automatically",
    "inbox.diag.unknown": "Unrecognized finding {code} (backend newer than this page; refresh after upgrade)",
    "inbox.diag.fix_resume": "Let AI take over again",
    "inbox.diag.fix_auto": "Back to Full-Auto",
    "inbox.diag.fix_warmup": "Disable warm-up review (this machine · supervisor)",
    "inbox.diag.fix_drafts": "Open review queue",
    "inbox.diag.warmup_off_ok": "Warm-up review disabled: hot reload applies within ~30s",
    "inbox.diag.warmup_off_fail": "Failed (needs supervisor permission or backend too old)",
    "inbox.diag.history_title": "Mode change history",
    "inbox.diag.src_human": "agent selected",
    "inbox.diag.src_bootstrap": "new conversation (global default)",
    "inbox.diag.src_guard": "risk guard downgrade",
    "inbox.diag.src_sweep": "guard sweep downgrade",
    "inbox.diag.src_bulk": "bulk operation",
    "inbox.diag.src_takeover": "manual-send takeover",
    "inbox.diag.src_rearm": "AI auto re-arm",
    "inbox.diag.src_unknown": "unknown source",
}
