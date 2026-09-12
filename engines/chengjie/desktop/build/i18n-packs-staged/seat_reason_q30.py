# -*- coding: utf-8 -*-
"""Q-30（#307 #308 #309 #310 #311，2026-09-12）词条：坐席规模开关 + 三个原因码人话。

键族：
- ``rps_ms_*``            回复设置页 → 自动化与风控 →「多坐席协作」卡（开关 + 生效读数行）；
- ``inbox.cs.deferred.first_reply``  状态带：沉寂后首条回复的拟人首回延迟（#311 W2CSGA 真闸，
                          此前状态带只说「AI 会自动回」，坐席看着草稿两分钟不动就报「全自动不发」）；
- ``inbox.cs.held.colleague / ops_group / peer_guard``  状态带：peer_bot_guard 硬拦拟稿
                          （同事账号 / 报障群 / 机器人对端），此前只打日志、界面零提示（jun #312 #314）；
- ``inbox.handoff.r_media_lie_caught_repeat``  「需人工」红条原因码人话（#307 此前晒原码）；
- ``inbox.mode.cancelled_l1_stale``  切全自动时作废切档前 L1 旧稿的提示（#308）；
- ``inbox.l1r.peer_budget``  L1 原因码：额度软停封顶（此前误归「全局默认半自动」，#308 复核发现）。

繁体手写 ZH_HANT（不碰生成物 zh_hant_auto；regen 会自动排除本包已覆盖键）。
"""

ZH = {
    # ── 回复设置 · 多坐席协作卡（rps-ms-）────────────────────────────────
    "rps_ms_title": "👥 多坐席协作 · 认领 / 处理中 / 「我的」筛选",
    "rps_ms_hint": "多人同守一批客户时才需要「认领」：点开会话＝我来跟，列表标「处理中」，别人不抢。单人使用这些全是噪音——默认关；近 30 分钟内有 2 个及以上坐席登录过工作台会自动按多坐席显示，不必手动开。",
    "rps_ms_row": "多坐席协作（认领 / 处理中）",
    "rps_ms_row_hint": "开＝一直按多坐席显示（团队固定多人）。关＝自动判：单人时收起认领 / 处理中 / 「我的」，多人同时在线时自动出现。保存后收件箱最迟 1 分钟刷新即生效。",
    "rps_ms_mode_multi": "多坐席",
    "rps_ms_mode_single": "单坐席",
    "rps_ms_src_explicit": "本页开关 / 配置显式指定",
    "rps_ms_src_presence": "近 {m} 分钟有 {n} 个坐席登录过工作台，自动判为多坐席",
    "rps_ms_src_default": "近 {m} 分钟只有 {n} 个坐席在线，按单坐席显示",
    "rps_ms_eff_line": "收件箱当前按「{mode}」显示 · {src}",
    "rps_ms_pending_save": "开关已改，保存后生效",
    # ── 状态带：拟人首回延迟（#311）──────────────────────────────────────
    "inbox.cs.deferred.first_reply": "AI {n} 秒后发出 · 沉寂 {silence_h} 小时后的首条回复刻意慢一点",
    "inbox.cs.deferred.first_reply_t": "对方沉寂几小时后的第一条回复，AI 会按真人节奏晾 1–5 分钟再发（草稿已生成、到点自动发，不丢稿）。这不是「全自动不发」。节奏参数在 自动回复设置 → 自动化与风控",
    "inbox.cs.deferred.first_contact": "AI {n} 秒后发出 · 首次来信的第一条回复刻意慢一点",
    "inbox.cs.deferred.first_contact_t": "对方第一次来信，AI 不会 10 秒内秒回，按真人节奏晾 1–5 分钟再发（草稿已生成、到点自动发，不丢稿）。这不是「全自动不发」",
    # ── 状态带：peer_bot_guard 硬拦（#312 #314 可见化）────────────────────
    "inbox.cs.held.colleague": "AI 不拟稿不发 · 对方在『永不自动回复』名单（同事 / 本租户账号 {id}）",
    "inbox.cs.held.colleague_t": "自家账号 / 同事之间互聊永远不自动回（防两台机器互刷）。自测请换一个不在名单里的号；名单在 设置 › 自动化与风控 › 单会话额度 · 防机器人互刷（never_auto_reply）",
    "inbox.cs.held.ops_group": "AI 不拟稿不发 · 这是报障群 / 运维群（{id}），在『永不自动回复』名单",
    "inbox.cs.held.ops_group_t": "报障群 / 运维群 / 通知目标群永远不自动回（防在工作群里说话）。要改名单 → 设置 › 自动化与风控 › 单会话额度 · 防机器人互刷",
    "inbox.cs.held.peer_guard": "AI 不拟稿不发 · 对方判定为机器人 / 对轰（{code}）",
    "inbox.cs.held.peer_guard_t": "机器人守卫拦下了本会话的自动拟稿（Telegram bot 账号 / 复读 / 秒回熔断）。确认对方是真人 → 设置 › 自动化与风控 › 单会话额度 · 防机器人互刷 里放行",
    # ── 「需人工」原因码人话（#307）──────────────────────────────────────
    "inbox.handoff.r_media_lie_caught_repeat": "客户说没收到图 · 已连续 2 次 · 交给你",
    # ── 切档提示（#308）+ L1 原因码 ───────────────────────────────────────
    "inbox.mode.cancelled_l1_stale": "已作废 {n} 条切档前的待审旧稿（AI 接下来按全自动重新拟）",
    "inbox.l1r.peer_budget": "单会话额度触顶 · 本轮转人审",
}

EN = {
    "rps_ms_title": "👥 Multi-seat collaboration · claim / in progress / “Mine” filter",
    "rps_ms_hint": "“Claiming” only matters when several agents share the same customers: opening a chat = I take it, the list shows “in progress”, nobody else grabs it. For a single agent all of that is noise — off by default; when 2+ agents have signed in to the workspace in the last 30 minutes the inbox switches to multi-seat automatically, no need to flip this.",
    "rps_ms_row": "Multi-seat collaboration (claim / in progress)",
    "rps_ms_row_hint": "On = always show multi-seat controls (a fixed team). Off = auto: hide claim / in progress / “Mine” for a single agent, show them when several agents are online. Takes effect in the inbox within a minute after saving.",
    "rps_ms_mode_multi": "multi-seat",
    "rps_ms_mode_single": "single seat",
    "rps_ms_src_explicit": "set explicitly by this switch / config",
    "rps_ms_src_presence": "{n} agents signed in during the last {m} min → auto multi-seat",
    "rps_ms_src_default": "only {n} agent online in the last {m} min → single seat",
    "rps_ms_eff_line": "The inbox currently shows “{mode}” · {src}",
    "rps_ms_pending_save": "switch changed, save to apply",
    "inbox.cs.deferred.first_reply": "AI sends in {n}s · first reply after {silence_h}h of silence is deliberately slower",
    "inbox.cs.deferred.first_reply_t": "The first reply after the customer was silent for hours waits 1–5 minutes like a real person would (the draft exists and sends automatically — not dropped). This is not “Full Auto not sending”. Pacing lives under Auto-reply Settings → Automation & Risk",
    "inbox.cs.deferred.first_contact": "AI sends in {n}s · the very first reply is deliberately slower",
    "inbox.cs.deferred.first_contact_t": "It's the customer's first message: the AI won't answer within 10 seconds; it waits 1–5 minutes like a real person (the draft exists and sends automatically — not dropped). This is not “Full Auto not sending”",
    "inbox.cs.held.colleague": "AI won't draft or send · peer is on the “never auto-reply” list (colleague / own account {id})",
    "inbox.cs.held.colleague_t": "Own accounts and colleagues never get automatic replies (prevents two machines chatting with each other). Test with an account that is not on the list; the list lives under Settings › Automation & Risk › per-conversation budget / bot guard (never_auto_reply)",
    "inbox.cs.held.ops_group": "AI won't draft or send · this is an ops / bug-report group ({id}) on the “never auto-reply” list",
    "inbox.cs.held.ops_group_t": "Bug-report / ops / notification groups never get automatic replies. To change the list → Settings › Automation & Risk › per-conversation budget / bot guard",
    "inbox.cs.held.peer_guard": "AI won't draft or send · peer looks like a bot / echo loop ({code})",
    "inbox.cs.held.peer_guard_t": "The bot guard blocked auto-drafting for this conversation (Telegram bot account / repeats / instant-reply breaker). If the peer is human → release under Settings › Automation & Risk › per-conversation budget / bot guard",
    "inbox.handoff.r_media_lie_caught_repeat": "Customer says the photo never arrived · 2nd time in a row · over to you",
    "inbox.mode.cancelled_l1_stale": "Discarded {n} stale review draft(s) created before the switch (AI redrafts on Full Auto from here)",
    "inbox.l1r.peer_budget": "Per-conversation budget reached · this turn goes to review",
}

ZH_HANT = {
    "rps_ms_title": "👥 多坐席協作 · 認領 / 處理中 / 「我的」篩選",
    "rps_ms_hint": "多人同守一批客戶時才需要「認領」：點開會話＝我來跟，列表標「處理中」，別人不搶。單人使用這些全是噪音——預設關；近 30 分鐘內有 2 個及以上坐席登入過工作台會自動按多坐席顯示，不必手動開。",
    "rps_ms_row": "多坐席協作（認領 / 處理中）",
    "rps_ms_row_hint": "開＝一直按多坐席顯示（團隊固定多人）。關＝自動判：單人時收起認領 / 處理中 / 「我的」，多人同時線上時自動出現。儲存後收件箱最遲 1 分鐘刷新即生效。",
    "rps_ms_mode_multi": "多坐席",
    "rps_ms_mode_single": "單坐席",
    "rps_ms_src_explicit": "本頁開關 / 設定顯式指定",
    "rps_ms_src_presence": "近 {m} 分鐘有 {n} 個坐席登入過工作台，自動判為多坐席",
    "rps_ms_src_default": "近 {m} 分鐘只有 {n} 個坐席線上，按單坐席顯示",
    "rps_ms_eff_line": "收件箱目前按「{mode}」顯示 · {src}",
    "rps_ms_pending_save": "開關已改，儲存後生效",
    "inbox.cs.deferred.first_reply": "AI {n} 秒後發出 · 沉寂 {silence_h} 小時後的首條回覆刻意慢一點",
    "inbox.cs.deferred.first_reply_t": "對方沉寂幾小時後的第一條回覆，AI 會按真人節奏晾 1–5 分鐘再發（草稿已生成、到點自動發，不丟稿）。這不是「全自動不發」。節奏參數在 自動回覆設定 → 自動化與風控",
    "inbox.cs.deferred.first_contact": "AI {n} 秒後發出 · 首次來信的第一條回覆刻意慢一點",
    "inbox.cs.deferred.first_contact_t": "對方第一次來信，AI 不會 10 秒內秒回，按真人節奏晾 1–5 分鐘再發（草稿已生成、到點自動發，不丟稿）。這不是「全自動不發」",
    "inbox.cs.held.colleague": "AI 不擬稿不發 · 對方在『永不自動回覆』名單（同事 / 本租戶帳號 {id}）",
    "inbox.cs.held.colleague_t": "自家帳號 / 同事之間互聊永遠不自動回（防兩台機器互刷）。自測請換一個不在名單裡的號；名單在 設定 › 自動化與風控 › 單會話額度 · 防機器人互刷（never_auto_reply）",
    "inbox.cs.held.ops_group": "AI 不擬稿不發 · 這是報障群 / 運維群（{id}），在『永不自動回覆』名單",
    "inbox.cs.held.ops_group_t": "報障群 / 運維群 / 通知目標群永遠不自動回（防在工作群裡說話）。要改名單 → 設定 › 自動化與風控 › 單會話額度 · 防機器人互刷",
    "inbox.cs.held.peer_guard": "AI 不擬稿不發 · 對方判定為機器人 / 對轟（{code}）",
    "inbox.cs.held.peer_guard_t": "機器人守衛攔下了本會話的自動擬稿（Telegram bot 帳號 / 複讀 / 秒回熔斷）。確認對方是真人 → 設定 › 自動化與風控 › 單會話額度 · 防機器人互刷 裡放行",
    "inbox.handoff.r_media_lie_caught_repeat": "客戶說沒收到圖 · 已連續 2 次 · 交給你",
    "inbox.mode.cancelled_l1_stale": "已作廢 {n} 條切檔前的待審舊稿（AI 接下來按全自動重新擬）",
    "inbox.l1r.peer_budget": "單會話額度觸頂 · 本輪轉人審",
}
