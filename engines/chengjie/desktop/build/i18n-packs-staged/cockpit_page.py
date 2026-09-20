# -*- coding: utf-8 -*-
"""驾驶舱页词条（cockpit P1 2026-08-13；P2 可读性改版 2026-08-14）。

消费方：templates/cockpit.html（/workspace/cockpit）+ workspace_base 顶栏入口。
刻意独立成 pack（共享树多线并发下新文件＝零撞车）。接管动作的确认/回执文案
复用 takeover pack 的 tko.*（合并视图全局可取，不造同义新键）。

P2 改版随笔（改前先读）：KPI 单口径＝只用 /api/cockpit/overview 推导，
「待回复/SLA 超时」两键随 dashboard 双口径一起废弃回收；ck.why.* 是每张卡的
「为什么在这」一句话（带 {age} 变量）；ck.st.* 是右栏账号状态的人话映射
（按红绿灯三档，不透传英文 slug）。
"""

ZH = {
    "ck.nav": "驾驶舱",
    "ck.title": "驾驶舱",
    "ck.back": "工作台",
    "ck.sub": "谁需要人、按优先级排好——处理完一个划掉一个",
    "ck.refresh": "刷新",
    "ck.auto_note": "每 30 秒自动刷新",
    # ── 首次引导（可关，localStorage 记忆） ──
    "ck.hint": "AI 正在自动接待所有会话，这页只列需要真人的：「接管」＝AI 停手你来聊，「交还 AI」＝你聊完 AI 继续，「去处理」＝打开完整对话。",
    "ck.hint.dismiss": "知道了",
    # ── KPI 带（单口径：全部由介入队列快照推导） ──
    "ck.kpi.queue": "需介入",
    "ck.kpi.takeover": "接管中",
    "ck.kpi.oldest": "等最久的客户",
    "ck.kpi.stale": "历史积压",
    # ── 等待时长人话化（P3：「2d23h」是工程师缩写，卡片/KPI 全量中文分级） ──
    "ck.age.m": "{m} 分钟",
    "ck.age.h": "{h} 小时 {m} 分",
    "ck.age.d": "{d} 天 {h} 小时",
    # ── 介入队列 ──
    "ck.q.title": "介入队列",
    "ck.q.empty": "一切正常，AI 在岗 ✓",
    "ck.q.empty_sub": "没有需要人工介入的会话；有红点会第一时间排到这里",
    # 空态 ROI 行（P2 运营化）：「没事干」→「AI 替你干了 N 件事」的正反馈。
    # 数据源=坐席可读轻端点 /api/workspace/ai-weekly-brief（1h TTL），
    # 端点缺席/零流量整行不渲染——绝不摆 0。
    "ck.empty.roi": "本周 AI 已替你自动发出 {n} 条回复",
    "ck.q.fail": "队列加载失败，稍后自动重试",
    "ck.q.filter_empty": "这一类当前没有卡",
    "ck.filter.all": "全部",
    "ck.kind.takeover_overdue": "接管超时",
    "ck.kind.needs_human": "需人工",
    "ck.kind.waiting": "客户在等",
    "ck.kind.draft_pending": "草稿待审",
    "ck.why.takeover_overdue": "人工接管已 {age} 未交还——完成后记得交还 AI",
    "ck.why.needs_human": "AI 判断需要真人处理（高风险 / 生成失败 / 配额触顶等）",
    "ck.why.waiting": "客户说了最后一句，已等 {age} 没人回",
    "ck.why.draft_pending": "AI 草稿已拟好，等人审核 {age}",
    "ck.card.anon": "客户（尾号 {tail}）",
    # ── 身份卡（P3：头像+昵称+原话——卡片自答「这是谁、他说了什么」） ──
    # 冒号收进词条（中文全角/英文半角，模板不再拼标点）
    "ck.card.said": "客户说：",
    "ck.card.draft_prefix": "AI 已拟好草稿：",
    "ck.card.tk_by": "接管人：",
    # 纯媒体末条占位（P5 引用补齐：库里 last_msg 空、消息表末条入站是媒体）
    "ck.media.image": "[图片]",
    "ck.media.voice": "[语音]",
    "ck.media.video": "[视频]",
    "ck.media.file": "[文件]",
    "ck.media.other": "[附件]",
    "ck.wait.label": "已持续",
    "ck.flag.more": "同时命中",
    "ck.unread": "未读",
    "ck.act.open": "去处理",
    "ck.act.take": "接管",
    "ck.act.back": "交还 AI",
    "ck.act.resolve": "已处理",
    "ck.resolve.confirm": "把「需人工」标记从该会话摘掉？表示你已处理完或不需要再跟进。",
    "ck.resolved": "已清除「需人工」标记",
    "ck.stale.title": "历史积压（{n}）",
    "ck.stale.sub": "超过 3 天的旧信号——处理完点「已处理」清掉，别让它一直占屏",
    "ck.src.error": "部分信号源异常（{list}）——队列可能不全",
    # ── 右栏 ──
    "ck.rail.takeover": "接管在场",
    "ck.rail.tk_none": "当前没有人工接管中的会话",
    "ck.rail.tk_stats": "累计接管 {n} 次 · 平均 {min} 分钟",
    "ck.rail.acct": "账号健康",
    "ck.rail.acct_sum": "{ok} 在线 · {bad} 异常",
    "ck.rail.acct_fail": "账号数据暂不可用",
    "ck.rail.acct_empty": "暂无在册账号",
    "ck.st.ok": "在线",
    "ck.st.bad": "异常",
    "ck.st.warn": "状态未知",
    # 状态未上报的账号折叠成一行（满屏「状态未知」没有信息量还制造焦虑）
    "ck.rail.acct_unknown": "另有 {n} 个账号未上报状态",
    # ── 学习队列打通（P3：打通不合并——急诊台 vs 教研室） ──
    # 空态引流：驾驶舱没活时，若学习队列有待审草稿顺手指过去
    "ck.empty.learner": "学习队列有 {n} 条 AI 知识草稿等你审核",
    "ck.empty.learner_go": "去审核 →",
    # 「已处理」成功后的顺手喂料（可跳过；query 契约=POST /api/learner/feed 2..200 字）
    "ck.feed.confirm": "顺手教 AI？把客户这句话送进学习队列，AI 学会后下次能自动回答：「{q}」",
    "ck.feed.ok_btn": "送进学习队列",
    "ck.feed.done": "已送进学习队列，AI 生成草稿后可去审核入库",
}

EN = {
    "ck.nav": "Cockpit",
    "ck.title": "Cockpit",
    "ck.back": "Workspace",
    "ck.sub": "Who needs a human, sorted by priority — clear them one by one",
    "ck.refresh": "Refresh",
    "ck.auto_note": "Auto-refreshes every 30s",
    # ── first-visit hint (dismissible) ──
    "ck.hint": "AI is handling every conversation automatically; this page only lists the ones that need a human. Take over = AI stops and you chat; Hand back = AI resumes; Open = view the full conversation.",
    "ck.hint.dismiss": "Got it",
    # ── KPI band (single source: derived from the intervention queue snapshot) ──
    "ck.kpi.queue": "Needs attention",
    "ck.kpi.takeover": "Taken over",
    "ck.kpi.oldest": "Longest waiting",
    "ck.kpi.stale": "Backlog",
    # ── humanized wait durations (P3) ──
    "ck.age.m": "{m} min",
    "ck.age.h": "{h}h {m}m",
    "ck.age.d": "{d}d {h}h",
    # ── intervention queue ──
    "ck.q.title": "Intervention queue",
    "ck.q.empty": "All clear — AI on duty ✓",
    "ck.q.empty_sub": "No conversations need a human right now; new red flags land here first",
    "ck.empty.roi": "AI sent {n} replies for you this week",
    "ck.q.fail": "Failed to load the queue, retrying shortly",
    "ck.q.filter_empty": "No cards in this category right now",
    "ck.filter.all": "All",
    "ck.kind.takeover_overdue": "Takeover overdue",
    "ck.kind.needs_human": "Needs human",
    "ck.kind.waiting": "Customer waiting",
    "ck.kind.draft_pending": "Draft pending review",
    "ck.why.takeover_overdue": "Manually taken over for {age} without handing back — remember to hand back when done",
    "ck.why.needs_human": "AI flagged this for a human (high risk / generation failed / quota reached, etc.)",
    "ck.why.waiting": "Customer sent the last message and has waited {age} with no reply",
    "ck.why.draft_pending": "AI draft is ready, awaiting review for {age}",
    "ck.card.anon": "Customer (…{tail})",
    # ── identity card (P3: avatar + nickname + verbatim quote) ──
    "ck.card.said": "Customer said: ",
    "ck.card.draft_prefix": "AI draft ready: ",
    "ck.card.tk_by": "Taken by: ",
    "ck.media.image": "[Photo]",
    "ck.media.voice": "[Voice message]",
    "ck.media.video": "[Video]",
    "ck.media.file": "[File]",
    "ck.media.other": "[Attachment]",
    "ck.wait.label": "for",
    "ck.flag.more": "Also flagged",
    "ck.unread": "Unread",
    "ck.act.open": "Open",
    "ck.act.take": "Take over",
    "ck.act.back": "Hand back to AI",
    "ck.act.resolve": "Done",
    "ck.resolve.confirm": "Remove the \"needs human\" flag from this conversation? This means you've handled it or no follow-up is needed.",
    "ck.resolved": "\"Needs human\" flag cleared",
    "ck.stale.title": "Backlog ({n})",
    "ck.stale.sub": "Signals older than 3 days — click Done after handling so they stop cluttering the queue",
    "ck.src.error": "Some signal sources errored ({list}) — queue may be partial",
    # ── right rail ──
    "ck.rail.takeover": "Active takeovers",
    "ck.rail.tk_none": "No conversations under manual takeover",
    "ck.rail.tk_stats": "{n} takeovers total · avg {min} min",
    "ck.rail.acct": "Account health",
    "ck.rail.acct_sum": "{ok} online · {bad} unhealthy",
    "ck.rail.acct_fail": "Account data unavailable",
    "ck.rail.acct_empty": "No accounts registered",
    "ck.st.ok": "Online",
    "ck.st.bad": "Unhealthy",
    "ck.st.warn": "Unknown",
    "ck.rail.acct_unknown": "{n} more accounts haven't reported status",
    # ── learning-queue linkage (P3) ──
    "ck.empty.learner": "The learning queue has {n} AI knowledge drafts awaiting your review",
    "ck.empty.learner_go": "Review →",
    "ck.feed.confirm": "Teach the AI? Send this customer question to the learning queue so the AI can answer it next time: \"{q}\"",
    "ck.feed.ok_btn": "Send to learning queue",
    "ck.feed.done": "Sent to the learning queue — review the AI draft there to add it to the KB",
}
