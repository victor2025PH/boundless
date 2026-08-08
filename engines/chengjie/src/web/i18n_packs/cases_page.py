# -*- coding: utf-8 -*-
"""案例跟进页（/cases）2026-08-03 改造词条：立案原因 + 新版 UI。

命名约定：
- ``case.reason.*``   服务端 reason 渲染键（cases_routes 经 tr() 按请求语言出文案；
  上下文里只存键+参数，绝不落单语文案）
- ``cases.*``         前端新版 UI 键（筛选/卡片/结案弹窗/空态教育/相对时间）
- ``cases.int.*``     意图链步骤的人话标签（JS 动态拼键，配套兜底为原始枚举名）

旧版页面的 ``cases_*``（下划线）键仍留在 web_i18n 单体里，语义未变的直接复用，
本包不重复定义（pack×单体同 key 会被门禁拦截）。
"""

ZH = {
    # ── 服务端立案原因（cases_routes._localized_reason） ─────────────────────
    "case.reason.pat.escalation_complaint": "咨询升级为投诉",
    "case.reason.pat.repeated_failure": "反复查询同一问题未解决",
    "case.reason.pat.refund_flow": "投诉后要求退款/补偿",
    "case.reason.pat.channel_troubleshoot": "通道问题排查中",
    "case.reason.pat.generic": "意图链风险模式：{desc}",
    "case.reason.escalation": "满意度偏低且连续追问 {n} 次未解决，已触发人工升级",
    "case.reason.crisis": "检测到危机信号，请尽快人工关注",
    "case.reason.media_repeat": "客户觉得发的照片重复/发过了",
    "case.reason.media_not_you": "客户觉得照片不像本人",
    "case.reason.media_fake": "客户怀疑照片是假的/生成的",
    "case.reason.media_lie_caught": "客户指出承诺未兑现（说话不算话）",
    "case.reason.media_distrust": "客户表达失望/不信任",
    "case.reason.media_complaint": "客户质疑发送的媒体内容",
    "case.reason.human_request": "客户明确要求人工/真人服务",
    "case.reason.ai_doubt": "客户怀疑在和机器人聊天",
    "case.reason.legacy": "意图链风险模式",

    # ── 筛选与统计 ───────────────────────────────────────────────────────────
    "cases.filter.all": "全部",
    "cases.filter.open": "待处理",
    "cases.filter.risk": "高风险",
    "cases.filter.closed": "已结案",
    "cases.src.all": "全部来源",
    "cases.src.intent_chain": "意图链模式",
    "cases.src.escalation": "人工升级",
    "cases.src.crisis": "危机信号",
    "cases.src.media_complaint": "媒体质疑",
    "cases.src.human_request": "要求人工",
    "cases.src.ai_doubt": "怀疑机器人",
    "cases.search.ph": "搜索用户 / 案号 / 内容…",
    "cases.stat.new_today": "今日新增",

    # ── 认领（多坐席分工，2026-08-03 P4） ───────────────────────────────────
    "cases.claim": "认领",
    "cases.claim.release": "释放",
    "cases.claim.by": "跟进人",
    "cases.filter.mine": "我认领的",
    "err.case.claimed_by_other": "案例已由 {name} 认领",

    # ── 卡片 ─────────────────────────────────────────────────────────────────
    "cases.reason.label": "立案原因",
    "cases.chain.label": "意图轨迹",
    "cases.open_conv": "打开会话",
    "cases.copy_id": "点击复制案号",
    "cases.copied": "已复制",
    "cases.signal_times": "信号 ×{n}",
    "cases.sev.3": "紧急",
    "cases.sev.2": "重要",
    "cases.sev.1": "关注",
    "cases.rel.now": "刚刚",
    "cases.rel.min": "{n} 分钟前",
    "cases.rel.hour": "{n} 小时前",
    "cases.rel.day": "{n} 天前",
    "cases.note.saved": "已保存",
    "cases.note.editing": "编辑中，本轮自动刷新已暂停",
    "cases.closed.at": "结案于",
    "cases.closed.res": "处理结果",
    "cases.closed.none": "（未填写处理结果）",

    # ── 结案弹窗 ─────────────────────────────────────────────────────────────
    "cases.close.title": "结案",
    "cases.close.desc": "简单记录处理结果（可选），团队之后能看懂这单是怎么解决的。",
    "cases.close.ph": "例如：已安抚，客户接受补偿方案",
    "cases.close.quick.soothed": "已安抚",
    "cases.close.quick.handoff": "已转人工",
    "cases.close.quick.false_alarm": "误报",
    "cases.close.confirm": "确认结案",
    "cases.close.cancel": "取消",

    # ── 空态教育 + 示例卡 ────────────────────────────────────────────────────
    "cases.empty.sub": "AI 正在持续盯着所有会话，出现下面这些情况时会自动立案：",
    "cases.empty.b1": "客户明确要求人工 / 真人服务",
    "cases.empty.b2": "检测到危机信号（自伤等），需要真人关注",
    "cases.empty.b3": "客户质疑照片 / 身份造假（穿帮风险）",
    "cases.empty.b4": "满意度走低且连续追问未解决（升级前兆）",
    "cases.empty.b5": "会话意图走势命中风险模式（咨询→投诉 等）",
    "cases.empty.example_label": "案例卡片长这样（示例，非真实数据）：",
    "cases.empty.filtered": "当前筛选下没有案例",
    "cases.example.badge": "示例",
    "cases.example.user_msg": "我要找真人客服！你是机器人吧？？",
    "cases.example.ai_msg": "别生气嘛，我在呢～你说说看遇到什么问题了？",
    "cases.example.note": "客户对发货时效不满，已联系仓库加急",

    # ── 引导横幅（简洁模式） ─────────────────────────────────────────────────
    "cases.guide.body": "这页是 AI 自动整理的「需要人跟进」清单：客户要人工、怀疑机器人、质疑照片、情绪危机等信号会自动立案。点<strong>打开会话</strong>直达工作台处理，处理完<strong>结案</strong>销账，可随时加<strong>备注</strong>给同事留话。",

    # ── 意图链步骤人话标签（JS 动态键，兜底=原始枚举名） ─────────────────────
    "cases.int.order_query": "订单查询",
    "cases.int.complaint": "投诉",
    "cases.int.channel_info": "通道咨询",
    "cases.int.status_check": "状态查询",
    "cases.int.direct_chat": "对话",
    "cases.int.small_talk": "闲聊",
    "cases.int.greeting": "问候",
}

EN = {
    # ── Server-side case reasons ────────────────────────────────────────────
    "case.reason.pat.escalation_complaint": "Inquiry escalated to complaint",
    "case.reason.pat.repeated_failure": "Repeated queries left unresolved",
    "case.reason.pat.refund_flow": "Refund/compensation requested after complaint",
    "case.reason.pat.channel_troubleshoot": "Troubleshooting a channel issue",
    "case.reason.pat.generic": "Intent-chain risk pattern: {desc}",
    "case.reason.escalation": "Low satisfaction with {n} consecutive unresolved follow-ups; escalation triggered",
    "case.reason.crisis": "Crisis signal detected; needs human attention now",
    "case.reason.media_repeat": "Customer says the photo was already sent before",
    "case.reason.media_not_you": "Customer says the photo doesn't look like you",
    "case.reason.media_fake": "Customer suspects the photo is fake or AI-generated",
    "case.reason.media_lie_caught": "Customer called out a broken promise",
    "case.reason.media_distrust": "Customer expresses distrust or disappointment",
    "case.reason.media_complaint": "Customer doubts the media that was sent",
    "case.reason.human_request": "Customer explicitly asked for a human agent",
    "case.reason.ai_doubt": "Customer suspects they are chatting with a bot",
    "case.reason.legacy": "Intent-chain risk pattern",

    # ── Filters & stats ─────────────────────────────────────────────────────
    "cases.filter.all": "All",
    "cases.filter.open": "Open",
    "cases.filter.risk": "High risk",
    "cases.filter.closed": "Closed",
    "cases.src.all": "All sources",
    "cases.src.intent_chain": "Intent chain",
    "cases.src.escalation": "Escalation",
    "cases.src.crisis": "Crisis",
    "cases.src.media_complaint": "Media doubt",
    "cases.src.human_request": "Human requested",
    "cases.src.ai_doubt": "Bot suspicion",
    "cases.search.ph": "Search user / case ID / text...",
    "cases.stat.new_today": "New today",

    # ── Claiming (multi-agent ownership, 2026-08-03 P4) ─────────────────────
    "cases.claim": "Claim",
    "cases.claim.release": "Release",
    "cases.claim.by": "Owner",
    "cases.filter.mine": "Mine",
    "err.case.claimed_by_other": "Case already claimed by {name}",

    # ── Card ────────────────────────────────────────────────────────────────
    "cases.reason.label": "Reason",
    "cases.chain.label": "Intent trail",
    "cases.open_conv": "Open conversation",
    "cases.copy_id": "Click to copy case ID",
    "cases.copied": "Copied",
    "cases.signal_times": "signals x{n}",
    "cases.sev.3": "Urgent",
    "cases.sev.2": "Important",
    "cases.sev.1": "Watch",
    "cases.rel.now": "just now",
    "cases.rel.min": "{n} min ago",
    "cases.rel.hour": "{n} h ago",
    "cases.rel.day": "{n} d ago",
    "cases.note.saved": "Saved",
    "cases.note.editing": "Editing - auto-refresh paused for this round",
    "cases.closed.at": "Closed",
    "cases.closed.res": "Resolution",
    "cases.closed.none": "(no resolution recorded)",

    # ── Close modal ─────────────────────────────────────────────────────────
    "cases.close.title": "Close case",
    "cases.close.desc": "Briefly record the outcome (optional) so teammates can see how this was resolved.",
    "cases.close.ph": "e.g. Soothed the customer; compensation accepted",
    "cases.close.quick.soothed": "Soothed",
    "cases.close.quick.handoff": "Handed to human",
    "cases.close.quick.false_alarm": "False alarm",
    "cases.close.confirm": "Close case",
    "cases.close.cancel": "Cancel",

    # ── Empty-state education + sample card ─────────────────────────────────
    "cases.empty.sub": "AI keeps watching every conversation and opens a case when:",
    "cases.empty.b1": "The customer explicitly asks for a human agent",
    "cases.empty.b2": "A crisis signal is detected (self-harm etc.) and needs a human",
    "cases.empty.b3": "The customer doubts photos or identity (cover-blown risk)",
    "cases.empty.b4": "Satisfaction drops with repeated unresolved follow-ups",
    "cases.empty.b5": "The intent trail hits a risk pattern (inquiry to complaint etc.)",
    "cases.empty.example_label": "A case card looks like this (sample, not real data):",
    "cases.empty.filtered": "No cases match the current filter",
    "cases.example.badge": "SAMPLE",
    "cases.example.user_msg": "I want a real human agent! Are you a bot??",
    "cases.example.ai_msg": "Don't be upset - I'm right here. Tell me what happened?",
    "cases.example.note": "Customer unhappy about shipping delay; warehouse expediting",

    # ── Guide banner (simple mode) ──────────────────────────────────────────
    "cases.guide.body": "This page is the AI-curated list of conversations that need a human: requests for a real agent, bot suspicion, photo doubts, emotional crises and more open cases automatically. Click <strong>Open conversation</strong> to handle it in the workspace, <strong>close</strong> the case when done, and leave <strong>notes</strong> for teammates anytime.",

    # ── Intent-chain step labels (dynamic JS keys; fallback = raw enum) ─────
    "cases.int.order_query": "Order query",
    "cases.int.complaint": "Complaint",
    "cases.int.channel_info": "Channel info",
    "cases.int.status_check": "Status check",
    "cases.int.direct_chat": "Chat",
    "cases.int.small_talk": "Small talk",
    "cases.int.greeting": "Greeting",
}
