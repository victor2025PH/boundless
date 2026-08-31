# -*- coding: utf-8 -*-
"""全局悬浮提示词典单一数据源(原 base.html 内联 TERM_DICT)。

base.html 的 tooltip 引擎经 `const TERM_DICT={{ help_terms|tojson }};` 消费本表;
admin.py `_enrich_context` 注入模板上下文。词条结构:
    key → {zh, en, desc, desc_en, usage?, usage_en?}
zh/en=标题,desc*=功能描述,usage*=典型操作路径(可选)。
导航项的 key(nav_* 等)由 src/web/nav_schema.py 的 item.help 关联。
tooltip 引擎 matchTerm 是「整元素文本 trim/小写后与 key / zh / en 精确相等」才挂——
不做子串匹配,所以同一功能的多种屏幕说法(如工作目标 / 工作计划)须各占一条词条。
"bot_admin" 品牌词条此处存默认品牌名;线上 zh/en 随 site_name 动态,
由 base.html 注入 TERM_DICT 后覆写(与原内联 Jinja 表达式行为一致)。
"""

HELP_TERMS: dict = {
    # ── 导航页面 ───
    "nav_dashboard": {
        "zh": "仪表盘",
        "en": "Dashboard",
        "desc": "系统总览页面，展示 Bot 运行状态、通道健康度、最近操作等关键指标",
        "desc_en": "System overview page showing bot status, channel health, recent activity and other key metrics",
        "usage": "打开即可查看全局状态，点击各卡片的快捷链接可跳转到对应功能模块",
        "usage_en": "Open it to see global status; click a card's quick link to jump to the matching module"
    },
    "nav_ops_overview": {
        "zh": "运营总览",
        "en": "Ops Overview",
        "desc": "老板单页：业务 ROI、计费用量、运行时健康、运维可靠性与运维事件的聚合看板，全部运维卡片（语音/翻译/命理/主动触达等）都在这里",
        "desc_en": "The single-page boss view: business ROI, billing, runtime health, ops reliability and incidents, plus every ops card (voice, translation, bazi, proactive outreach, ...)",
        "usage": "日常巡检从这里开始；仪表盘上的可靠性摘要与工程 chip 也都深链到本页",
        "usage_en": "Start daily checks here; the dashboard's reliability summary and engineering chips deep-link into this page"
    },
    # ── 仪表盘区块（2026-08 改版：待办条 / 统计卡 / 折叠区） ───
    "dash_todo": {
        "zh": "待办",
        "en": "To-dos",
        "desc": "需要人处理的事项汇总：待审回复草稿、学习队列、未处理危机事件、进行中案例。数据与侧栏徽标同源",
        "desc_en": "Everything waiting on a human: reply drafts, learning queue, unhandled crisis events and open cases. Same source as the sidebar badges",
        "usage": "点击任一徽标直达对应处理页；全部清零时显示「暂无待办」",
        "usage_en": "Click any pill to jump to its handling page; shows \"All clear\" when empty"
    },
    "dash_health": {
        "zh": "系统运行状态",
        "en": "Runtime health",
        "desc": "运行时红绿灯：数据库、AI 大模型、授权、消息渠道、后台 Worker、草稿队列等组件的存活/积压/熔断状况",
        "desc_en": "Runtime traffic light: liveness, backlog and circuit state of DB, AI model, license, channels, workers and draft queues",
        "usage": "任一组件 chip 都可点击，直达该问题的处理页；异常组件的原因直接显示在行内",
        "usage_en": "Every component chip is clickable and jumps to where you fix it; failing chips show their reason inline"
    },
    "dash_reliability": {
        "zh": "运维可靠性",
        "en": "Ops reliability",
        "desc": "近 24 小时出站处置量、拦截率、升级率与各 Worker 错误率的汇总评分，用于判断自动化链路是否健康",
        "desc_en": "24h dispositions, block/reject rates and per-worker error rates rolled into one score for judging automation health",
        "usage": "摘要行足够判断好坏；点开看各 Worker 明细，完整运维卡片在「运营总览」页",
        "usage_en": "The summary row tells you good/bad at a glance; expand for per-worker detail, full cards live in Ops Overview"
    },
    "dash_eng": {
        "zh": "工程明细",
        "en": "Engineering detail",
        "desc": "Bot 实时性能（响应耗时/并发/队列/熔断）与触发器决策日志，供排障和调参使用，默认折叠",
        "desc_en": "Bot performance (latency, concurrency, queues, circuit breaker) and trigger decision log for debugging and tuning; collapsed by default",
        "usage": "展开后才开始加载与高频刷新；日常运营无需展开",
        "usage_en": "Loads and polls only when expanded; day-to-day operators can leave it closed"
    },
    "dash_msgs": {
        "zh": "消息 收/发",
        "en": "Messages in/out",
        "desc": "自本次启动以来收到与回复的消息总量及回复率（⏱ 为统计窗口时长），点击进入运营分析看完整趋势",
        "desc_en": "Messages received/replied since this process started, with reply rate (⏱ shows the window); click through to Analytics for full trends",
        "usage": "点击卡片跳转「运营分析」页查看按日趋势与分渠道数据",
        "usage_en": "Click the card to open Analytics for daily trends and per-channel breakdowns"
    },
    "dash_diag": {
        "zh": "系统诊断",
        "en": "Diagnostics",
        "desc": "汇总运行时健康组件的正常/注意/异常计数；点击运行一次全面体检（配置、连通性、策略完整性）",
        "desc_en": "Rolls up ok/warn/fail counts from runtime health; click to run a full check across config, connectivity and strategies",
        "usage": "点击卡片弹出诊断面板，逐项列出问题与修复入口",
        "usage_en": "Click to open the diagnostic modal listing each issue with a fix link"
    },
    "dash_proactive": {
        "zh": "主动触达（14天）",
        "en": "Proactive outreach (14d)",
        "desc": "近 14 天主动打招呼/回访的发送总量与回复率（按开场 mode 聚合，与运营总览主动触达卡同口径）",
        "desc_en": "Proactive greeting/check-in sends and reply rate over the last 14 days (aggregated by opener mode, same source as the Ops Overview card)",
        "usage": "点击卡片进入运营总览查看 mode 级回复率、退避与媒体形态明细",
        "usage_en": "Click through to Ops Overview for per-mode reply rates, backoff and media-form detail"
    },
    "dash_proactive": {
        "zh": "主动触达",
        "en": "Proactive outreach",
        "desc": "近 14 天主动开场（问候/生活分享/记忆追问等）的发送量与回复率——判断「主动找客户聊」有没有换来回应的核心读数",
        "desc_en": "Sends and reply rate of proactive openers (check-ins, life shares, memory follow-ups) over the last 14 days — the key read on whether outreach earns replies",
        "usage": "点击卡片进入运营总览，看分 mode 回复率、未回退避与媒体形态反哺明细",
        "usage_en": "Click through to Ops Overview for per-mode reply rates, no-reply backoff and media feedback detail"
    },
    "nav_templates": {
        "zh": "话术模板（已迁移）",
        "en": "Reply Templates (moved)",
        "desc": "话术模板已统一迁移到知识库的「系统话术」分类，可在知识库页面进行编辑",
        "desc_en": "Reply templates have moved to the Knowledge Base's \"System Scripts\" category and are edited there",
        "usage": "打开知识库 → 筛选「系统话术」分类 → 编辑对应的话术条目",
        "usage_en": "Open Knowledge Base → filter the \"System Scripts\" category → edit the matching entry"
    },
    "nav_channels": {
        "zh": "通道管理",
        "en": "Channel Management",
        "desc": "配置支付通道的费率、金额限制、状态开关，实时监控成功率",
        "desc_en": "Configure payment-channel rates, amount limits and status toggles, and monitor success rate in real time",
        "usage": "点击通道卡片展开详情 → 调整参数 → 保存。可切换状态为 启用/维护中/波动",
        "usage_en": "Click a channel card to expand → adjust parameters → save. Status can be set to Enabled / Maintenance / Fluctuating"
    },
    "nav_strategies": {
        "zh": "回复策略",
        "en": "Reply Strategies",
        "desc": "为不同用户意图配置 AI 回复参数（温度、长度、上下文轮数），支持 A/B 灰度测试。注意：这里是 AI 回复参数，不是营销策略",
        "desc_en": "Configure AI reply parameters (temperature, length, context rounds) per user intent, with A/B testing. Note: these are AI reply parameters, not marketing strategy",
        "usage": "选择策略 → 调整 Temperature/Max Tokens 等参数 → 保存 → 在意图映射中绑定",
        "usage_en": "Pick a strategy → adjust Temperature/Max Tokens → save → bind it in the intent mapping"
    },
    "nav_strategy_analytics": {
        "zh": "策略效果",
        "en": "Strategy Performance",
        "desc": "追踪各策略的质量评分、追问率、响应时间等指标，自动识别最优策略",
        "desc_en": "Track each strategy's quality score, follow-up rate, response time and more, auto-spotting the best one",
        "usage": "选择时间范围查看趋势图，点击策略卡片查看详细指标",
        "usage_en": "Pick a time range to view trends; click a strategy card for detailed metrics"
    },
    "nav_knowledge": {
        "zh": "知识库",
        "en": "Knowledge Base",
        "desc": "管理 AI 的知识条目、触发词、错误码字典，支持 BM25 + 语义搜索",
        "desc_en": "Manage the AI's knowledge entries, trigger words and error-code dictionary, with BM25 + semantic search",
        "usage": "搜索/浏览条目 → 编辑内容和触发词 → 用沙盒测试验证效果",
        "usage_en": "Search/browse entries → edit content and triggers → verify with the sandbox test"
    },
    "nav_users": {
        "zh": "用户管理",
        "en": "User Management",
        "desc": "添加/管理后台管理员账户，设置角色权限（主帐号/管理员/观察员）",
        "desc_en": "Add and manage admin accounts and assign role permissions (Master / Admin / Viewer)",
        "usage": "点击「创建子帐号」→ 填写信息 → 分配角色。可随时禁用/删除账户",
        "usage_en": "Click \"Create sub-account\" → fill in details → assign a role. Accounts can be disabled/deleted anytime"
    },
    "nav_settings": {
        "zh": "系统设置",
        "en": "System Settings",
        "desc": "配置 AI 提示词和人工转接设置",
        "desc_en": "Configure the AI system prompt and human-handoff settings",
        "usage": "编辑提示词 → 保存。底层 API/安全配置请进入开发者工具",
        "usage_en": "Edit the prompt → save. For low-level API/security config, use Developer Tools"
    },
    "nav_developer": {
        "zh": "开发者工具",
        "en": "Developer Tools",
        "desc": "API 密钥、底层参数、安全令牌、Bot 行为（含是否处理私聊）、回复逻辑、意图路由等高级配置（需密码）",
        "desc_en": "Advanced config — API keys, low-level params, security tokens, bot behavior (incl. private chats), reply logic, intent routing (password required)",
        "usage": "输入开发者密码进入 → 「Bot 行为配置」中勾选「私聊消息」可允许私聊 → 保存",
        "usage_en": "Enter the developer password → tick \"Private messages\" under Bot Behavior to allow DMs → save"
    },
    "nav_audit": {
        "zh": "操作记录",
        "en": "Activity Log",
        "desc": "查看所有后台操作的审计日志，支持按操作人/类型/时间筛选",
        "desc_en": "View the audit log of all admin actions, filterable by operator / type / time",
        "usage": "使用顶部筛选条件缩小范围 → 点击导出按钮下载 CSV",
        "usage_en": "Use the top filters to narrow down → click Export to download CSV"
    },
    "nav_diff": {
        "zh": "版本对比",
        "en": "Version Diff",
        "desc": "对比配置的不同版本快照，查看变更详情，支持一键回滚",
        "desc_en": "Compare configuration version snapshots, inspect change details, and roll back in one click",
        "usage": "选择两个版本 → 查看差异 → 确认后可回滚到旧版本",
        "usage_en": "Pick two versions → review the diff → confirm to roll back to the older one"
    },
    "nav_logs": {
        "zh": "实时日志",
        "en": "Live Logs",
        "desc": "实时查看系统运行日志流，支持按级别过滤（DEBUG/INFO/WARN/ERROR）和关键词搜索",
        "desc_en": "Stream system logs in real time, filterable by level (DEBUG/INFO/WARN/ERROR) and keyword",
        "usage": "选择日志级别按钮过滤 → 输入关键词进一步筛选 → 点击暂停冻结画面",
        "usage_en": "Pick a level button to filter → type a keyword to narrow further → click Pause to freeze the view"
    },
    "nav_analytics": {
        "zh": "运营分析",
        "en": "Analytics",
        "desc": "查看运营数据图表、KB 命中率、响应时间趋势，内置 AI Copilot 自然语言查询",
        "desc_en": "View operations charts, KB hit rate and response-time trends, with a built-in AI Copilot for natural-language queries",
        "usage": "切换时间范围查看趋势 → 在 Copilot 输入框用中文提问查询数据",
        "usage_en": "Switch the time range to view trends → ask the Copilot a plain-language question to query data"
    },
    "nav_cases": {
        "zh": "案例跟进",
        "en": "Case Follow-ups",
        "desc": "AI 自动识别需要人工跟进的会话并立案：客户要求人工、怀疑机器人、质疑照片造假、危机信号、投诉升级等；支持跳转会话、备注、结案",
        "desc_en": "AI opens cases for conversations that need a human: requests for a real agent, bot suspicion, photo-fake doubts, crisis signals, complaint escalation; jump to the conversation, add notes, close cases",
        "usage": "按严重度处理案例 → 点「打开会话」直达工作台 → 处理完结案销账",
        "usage_en": "Work cases by severity → click Open conversation to jump to the workspace → close the case when resolved"
    },
    "nav_escalation": {
        "zh": "人工转接",
        "en": "Agent Handoff",
        "desc": "配置客服用户名、转接触发规则、排班与话术，让 AI 在需要时自动 @ 人工客服",
        "desc_en": "Configure agent usernames, handoff trigger rules, schedules and scripts so the AI auto-@s a human agent when needed",
        "usage": "启用 → 填写客服用户名 → 设置触发次数和冷却时间 → 保存",
        "usage_en": "Enable → fill in agent usernames → set trigger count and cooldown → save"
    },
    "nav_help": {
        "zh": "帮助",
        "en": "Help",
        "desc": "查看所有可用的 Bot 指令和操作说明，支持关键词搜索",
        "desc_en": "Browse all available bot commands and how-to guides, with keyword search",
        "usage": "输入关键词搜索 → 点击展开查看指令详情和用法",
        "usage_en": "Type a keyword to search → expand an item to see command details and usage"
    },
    "nav_import": {
        "zh": "导入/导出",
        "en": "Import / Export",
        "desc": "导入/导出配置文件（YAML/JSON/ZIP），管理 Webhook 变更通知",
        "desc_en": "Import/export configuration files (YAML/JSON/ZIP) and manage Webhook change notifications",
        "usage": "拖拽文件到上传区 → 选择覆盖或合并模式 → 确认导入",
        "usage_en": "Drag a file onto the upload area → choose Overwrite or Merge mode → confirm the import"
    },
    "nav_unified_inbox": {
        "zh": "坐席工作台",
        "en": "Agent Workspace",
        "desc": "多平台统一收件箱：坐席在此接待所有渠道的客户对话，支持人工/AI 协作回复（独立窗口打开；已开着的工作台会被复用聚焦，不会重复新开）",
        "desc_en": "Unified multi-platform inbox where agents handle customer chats from every channel, with human/AI co-reply (opens in its own window; an already-open workspace is reused and focused instead of duplicated)",
        "usage": "点击在独立窗口打开（已打开则切过去）→ 扫码接入账号 → 认领会话开始接待",
        "usage_en": "Click to open in its own window (or switch to the one already open) → link accounts via QR → claim a conversation to start"
    },
    "nav_learner": {
        "zh": "学习队列",
        "en": "Learning Queue",
        "desc": "AI 从未命中对话中挖掘的新知识草稿，人工审核通过后自动写入知识库",
        "desc_en": "Knowledge drafts the AI mines from unanswered conversations; approved items are written into the Knowledge Base",
        "usage": "查看待审草稿 → 修改并通过 / 拒绝 → 通过项自动入库",
        "usage_en": "Review pending drafts → edit & approve / reject → approved entries auto-enter the KB"
    },
    "nav_personas": {
        "zh": "人设工作室",
        "en": "Persona Studio",
        "desc": "管理 AI 人设池、会话绑定与全局人设规则，决定 AI 用什么身份和口吻说话",
        "desc_en": "Manage the persona pool, session bindings and global persona rules — deciding the AI's identity and tone",
        "usage": "创建/编辑人设 → 设为默认或绑定到指定会话 → 配置全局规则",
        "usage_en": "Create/edit a persona → set as default or bind to sessions → configure global rules"
    },
    "nav_rpa_overview": {
        "zh": "矩阵总览",
        "en": "Matrix Overview",
        "desc": "真机矩阵的聚合看板：Telegram / LINE / Messenger / WhatsApp 全渠道运行状态、账号健康、待审、告警与漏斗",
        "desc_en": "Aggregate board of the device matrix: runtime status, account health, pending reviews, alerts and funnels across Telegram / LINE / Messenger / WhatsApp",
        "usage": "先看告警和待审数 → 点击渠道卡片跳到对应渠道页处理",
        "usage_en": "Check alerts and pending counts first → click a channel card to jump to that channel's page"
    },
    "nav_telegram": {
        "zh": "Telegram（真机矩阵）",
        "en": "Telegram (Device Matrix)",
        "desc": "配置 Telegram 主号的 AI 自动回复：接收范围、回复逻辑、屏蔽名单、语音与运营漏斗。坐席扫码登录的账号请在坐席工作台管理，不在此页",
        "desc_en": "Configure the Telegram main account's AI auto-reply: scope, reply logic, block list, voice and funnel. QR-linked agent accounts are managed in the Agent Workspace, not here",
        "usage": "选择消息处理范围 → 调整回复逻辑 → 保存后对主号自动回复生效",
        "usage_en": "Choose the message scope → tune reply logic → save to apply to the main account's auto-replies"
    },
    "nav_line_rpa": {
        "zh": "LINE（真机矩阵）",
        "en": "LINE (Device Matrix)",
        "desc": "LINE 真机自动化运营台：设备监控、聊天设置、运维工具与运营漏斗",
        "desc_en": "LINE device-automation console: device monitoring, chat settings, ops tools and funnel",
        "usage": "监控页看设备状态 → 设置页调聊天行为 → 运维页处理异常",
        "usage_en": "Watch device status on Monitor → tune chat behavior in Settings → handle issues in Ops"
    },
    "nav_messenger_rpa": {
        "zh": "Messenger（真机矩阵）",
        "en": "Messenger (Device Matrix)",
        "desc": "Messenger 网页自动化运营台：客户线索、人设策略、账号设备、审批质检与数据中心",
        "desc_en": "Messenger web-automation console: leads, persona strategy, accounts & devices, review QA and data center",
        "usage": "总览看运行态 → 客户线索跟进 → 审批质检处理待审消息",
        "usage_en": "Check the overview → follow up leads → clear pending items in Review QA"
    },
    "nav_whatsapp_rpa": {
        "zh": "WhatsApp（真机矩阵）",
        "en": "WhatsApp (Device Matrix)",
        "desc": "WhatsApp 协议自动化：对话管理、待审队列、模板分析、配置与运维",
        "desc_en": "WhatsApp protocol automation: conversations, review queue, template analytics, config and ops",
        "usage": "对话页看会话 → 待审页处理草稿 → 模板分析优化话术",
        "usage_en": "Browse chats → clear the review queue → optimize scripts via template analytics"
    },
    "nav_episodic": {
        "zh": "AI 记忆",
        "en": "AI Memory",
        "desc": "查看与校正 AI 对每个用户的情景记忆，保证长期对话不「失忆」、不记错",
        "desc_en": "Inspect and correct the AI's episodic memory about each user, so long-running chats stay consistent",
        "usage": "搜索用户 → 查看记忆条目 → 修正错误记忆或删除过期信息",
        "usage_en": "Search a user → review memory entries → fix wrong memories or delete stale ones"
    },
    "nav_crisis_audit": {
        "zh": "危机审计",
        "en": "Crisis Audit",
        "desc": "危机安全链事件留痕：高危消息的识别、处理过程与复盘记录",
        "desc_en": "Audit trail of the crisis-safety chain: how high-risk messages were detected, handled and reviewed",
        "usage": "关注未处理事件 → 查看上下文 → 标记处理结果",
        "usage_en": "Watch unhandled events → inspect context → mark the outcome"
    },
    "nav_voice_eval": {
        "zh": "声音评测",
        "en": "Voice Eval",
        "desc": "试听每个人设的克隆声（直念/拟人化/慢速多版本对比），打分并决定保留、修正或替换",
        "desc_en": "Audition each persona's cloned voice (verbatim / humanized / slow variants), rate and decide keep, fix or replace",
        "usage": "逐人设试听 → 打星+判定 → 需替换的在候选区选新音色",
        "usage_en": "Audition per persona → star + verdict → pick a replacement candidate where needed"
    },
    "nav_singing": {
        "zh": "歌房",
        "en": "Song Studio",
        "desc": "人设清唱能力管理：开关与频控护栏、每人设备货试听、曲库启停与声库锚定音",
        "desc_en": "Manage persona singing: switches and rate guardrails, per-persona stock audition, songbook toggles and voice anchors",
        "usage": "开总闸 → 备货矩阵逐条试听 → 曲库启停曲目（补货走 song_factory 命令行）",
        "usage_en": "Enable the master switch → audition the stock matrix → toggle songs (restock via the song_factory CLI)"
    },
    "nav_care": {
        "zh": "主动关怀",
        "en": "Proactive Care",
        "desc": "到点主动问候客户的关怀任务：排期、话术与发送状态一目了然",
        "desc_en": "Scheduled proactive check-ins: timing, scripts and delivery status at a glance",
        "usage": "查看今日待发关怀 → 调整排期或话术 → 跟踪发送结果",
        "usage_en": "See today's due check-ins → adjust schedule or script → track delivery"
    },
    "nav_relations_health": {
        "zh": "流失预警",
        "en": "Churn Alerts",
        "desc": "按关系健康度给用户分级，提前发现可能流失的客户并及时干预",
        "desc_en": "Grades users by relationship health, surfacing customers at churn risk early for intervention",
        "usage": "从高风险分组看起 → 打开用户详情 → 安排关怀或人工跟进",
        "usage_en": "Start with the high-risk group → open a user's detail → schedule care or manual follow-up"
    },
    "nav_monetization": {
        "zh": "变现营收",
        "en": "Monetization",
        "desc": "订阅、解锁、打赏等 C 端营收与转化漏斗报表",
        "desc_en": "Reports on consumer revenue (subscriptions, unlocks, tips) and conversion funnels",
        "usage": "切换时间范围看营收趋势 → 用漏斗定位转化瓶颈",
        "usage_en": "Switch the time range to view revenue trends → use the funnel to find conversion bottlenecks"
    },
    "nav_funnel": {
        "zh": "运营漏斗",
        "en": "Ops Funnel",
        "desc": "跨平台客户旅程漏斗:新客→活跃→转化各阶段人数、转化率与漏斗告警,支持按渠道筛选",
        "desc_en": "Cross-platform customer-journey funnel: stage counts, conversion rates and funnel alerts, filterable by channel",
        "usage": "看整体漏斗 → 用渠道筛选片定位掉量渠道 → 处理漏斗告警",
        "usage_en": "Review the overall funnel → use channel chips to find the leaking channel → handle funnel alerts"
    },
    # ── 工作目标（= 工作计划 = 营销目标，同一子系统的三种叫法）───
    # 三个同义词各占一条:tooltip 引擎 matchTerm 是「整元素文本精确相等」,
    # 一条词条只能认领一种屏幕说法,所以同义词只能靠多开 key 覆盖。
    "work_goal": {
        "zh": "工作目标",
        "en": "Work Goal",
        "desc": "为一段客户关系设定的推进目标：选模板（付费解锁/会员订阅/关系推进/沉默唤回/获客转化…）+ 期限 + 自治档，AI 按里程碑弧线（多数模板 4 段）分天推进，每天出一次「今日拍」。转化类与自定义可选「今天收口 / 这轮聊完」限时节奏。也叫「工作计划」，管理端与配置里叫「营销目标」——三个说法是同一个东西。需管理员开启 companion.goals.enabled，未开启时整卡不出现",
        "desc_en": "A progress goal set for one customer relationship: pick a template (paid unlock / subscription / relationship / reactivation / acquire-and-convert…) plus a deadline and an autonomy level, and the AI advances it day by day along a milestone arc (4 segments in most templates), producing one daily beat. Conversion and custom templates can pick a same-day or this-chat sprint cadence. Also called the \"work plan\"; the admin side and config call it \"marketing goal\" — all three are the same thing. Requires companion.goals.enabled; the whole card is hidden while it is off",
        "usage": "坐席工作台 → 选中会话 → 右栏「客户关系」→「工作目标」卡",
        "usage_en": "Agent Workspace → select a conversation → right rail \"Customer\" → the \"Work Goal\" card"
    },
    "work_plan": {
        "zh": "工作计划",
        "en": "Work Plan",
        "desc": "「工作目标」的口语叫法，指的是同一张卡：给这段关系定推进目标与期限，AI 按里程碑分天推进并每天出「今日拍」。管理端与配置里同一子系统叫「营销目标」",
        "desc_en": "The colloquial name for the \"work goal\" — the very same card: set a progress goal and deadline for this relationship, and the AI advances it along milestones with a daily beat. The admin side and config call the same subsystem the \"marketing goal\"",
        "usage": "坐席工作台 → 选中会话 → 右栏「客户关系」→「工作目标」卡",
        "usage_en": "Agent Workspace → select a conversation → right rail \"Customer\" → the \"Work Goal\" card"
    },
    "marketing_goal": {
        "zh": "营销目标",
        "en": "Marketing Goal",
        "desc": "同一子系统在管理端与配置里的名字（配置键 companion.goals，运营总览的「营销目标」卡是只读读数：进行中/今日拍/让路/注入/终态）。坐席端那张可操作的卡叫「工作目标」，运营口语里也叫「工作计划」",
        "desc_en": "The admin-side and config name for the same subsystem (config key companion.goals; the Ops Overview \"marketing goals\" card is read-only telemetry: active / beats / holds / injections / outcomes). The card agents actually operate is called the \"work goal\", also spoken of as the \"work plan\"",
        "usage": "只读读数看运营总览的「营销目标」卡；要建目标/改目标去坐席工作台 → 右栏「客户关系」→「工作目标」卡",
        "usage_en": "For read-only numbers see the \"marketing goals\" card on Ops Overview; to create or change a goal use Agent Workspace → right rail \"Customer\" → the \"Work Goal\" card"
    },
    "goal_beat_today": {
        "zh": "今日拍",
        "en": "Today's Beat",
        "desc": "目标在「今天」的一次推进安排＝今日意图 + 推进力度（陪伴日：只字不提推进 / 顺势：自然带到 / 可直说：可以直接说）。坐席可采纳或驳回：驳回后今天只陪伴、不带目标内容，且会回流规划器降档——驳回 1 次把「可直说」压到「顺势」，连续 2 次整天退回纯陪伴。对方情绪低落或连发未回时系统自己让路，当天不出拍",
        "desc_en": "One day's push arrangement for a goal: today's intent plus a push level (companion day = never mention the goal / gentle = weave it in naturally / direct OK = say it outright). Agents can adopt or reject it: after a reject the day stays companion-only with no goal content, and the verdict flows back to the planner as a step-down — one reject caps \"direct OK\" down to \"gentle\", two in a row drop the whole day to companion-only. When the contact is feeling low or hasn't replied to several sends, the system yields on its own and plans no beat",
        "usage": "「工作目标」卡的今日意图行 → 👍 采纳 / 👎 驳回；运营总览「营销目标」卡看今日拍与让路的总数",
        "usage_en": "On the \"Work Goal\" card, use 👍 adopt / 👎 reject on the today-intent row; the Ops Overview \"marketing goals\" card shows total beats and holds"
    },
    "goal_autonomy": {
        "zh": "自治档",
        "en": "Autonomy",
        "desc": "建目标时选的 AI 介入程度，三档：只观察＝只跟踪进度，不影响 AI 回复；顺势建议＝在 AI 回复里注入顺势引导，不硬推（默认档）；自动推进＝允许 AI 按日程主动推进目标。任何档位都受情绪让路与沉默熔断护栏约束",
        "desc_en": "How far the AI may go, chosen when creating a goal. Three levels: Observe only = track progress, AI replies unaffected; Suggest = inject gentle nudges into AI replies, never pushy (the default); Auto advance = let the AI proactively advance the goal on schedule. Every level is still bound by the emotion-yield and silence-cutoff guardrails",
        "usage": "「工作目标」卡 →「设定目标」→ 表单里的「自治档」；建好后可暂停/恢复目标",
        "usage_en": "\"Work Goal\" card → \"Set a goal\" → the \"Autonomy\" field in the form; an existing goal can be paused/resumed"
    },
    "goal_milestone": {
        "zh": "里程碑",
        "en": "Milestone",
        "desc": "目标推进弧线切成的几段（多数模板 4 段，如破冰回暖→价值铺垫→顺势开价→跟进收口；获客转化模板 5 段），卡上的分段条显示当前走到哪一段。每段自带默认推进力度，越靠后越可以直说；模板只声明弧线与今日意图，具体话术由回复生成层现场生成",
        "desc_en": "The segments a goal's arc is cut into (4 in most templates, e.g. reconnect → seed value → soft offer → follow up; the acquire-and-convert template has 5). The segmented bar on the card shows which segment you are in. Each segment carries a default push level, growing more direct toward the end; templates declare only the arc and the daily intent — the actual wording is generated live by the reply layer",
        "usage": "「工作目标」卡标题下的分段条：绿=已过，高亮=当前段，灰=待推进",
        "usage_en": "The segmented bar under the \"Work Goal\" card title: green = passed, highlighted = current, grey = pending"
    },
    # ── 通用术语 ───
    "buy_rate": {
        "zh": "买入费率",
        "en": "Buy-in Rate",
        "desc": "用户向该通道充值时平台收取的费率百分比",
        "desc_en": "The percentage fee the platform charges when a user tops up via this channel"
    },
    "sell_rate": {
        "zh": "卖出费率",
        "en": "Sell-out Rate",
        "desc": "用户从该通道提现时的费率",
        "desc_en": "The fee charged when a user withdraws from this channel"
    },
    "fee_rate": {
        "zh": "手续费率",
        "en": "Fee Rate",
        "desc": "该通道的交易手续费百分比。例如 0.5% 表示每笔扣除 0.5%",
        "desc_en": "The channel's transaction fee percentage. E.g. 0.5% deducts 0.5% per transaction"
    },
    "circuit_breaker": {
        "zh": "熔断器",
        "en": "Circuit Breaker",
        "desc": "当 AI 接口连续失败超过阈值时自动停止调用一段时间，防止雪崩",
        "desc_en": "Automatically halts calls for a while when the AI API fails consecutively beyond a threshold, preventing cascading failures"
    },
    "cooldown": {
        "zh": "冷却时间",
        "en": "Cooldown",
        "desc": "两次回复之间的最小间隔（秒），防止刷屏",
        "desc_en": "Minimum interval (seconds) between two replies, to prevent flooding"
    },
    "temperature": {
        "zh": "回复温度（Temperature）",
        "en": "Reply Temperature",
        "desc": "控制 AI 回复的随机性。0=最确定稳定，1=最多样创意。业务场景建议 0.3-0.5",
        "desc_en": "Controls how random the AI's replies are. 0 = most deterministic, 1 = most creative. 0.3-0.5 recommended for business",
        "usage": "拖动滑块调整，保存后对该策略下所有新回复生效",
        "usage_en": "Drag the slider; saving applies it to all new replies under this strategy"
    },
    "max_tokens": {
        "zh": "最大字数（Max Tokens）",
        "en": "Max Tokens",
        "desc": "AI 单次回复的最大 token 数。1 token ≈ 1.5 个中文字。256=简短，512=正常，1024=详细",
        "desc_en": "Max tokens per AI reply. 1 token ≈ 1.5 Chinese characters. 256 = short, 512 = normal, 1024 = detailed",
        "usage": "输入数值后保存。值越大回复越长，但速度变慢、费用增加",
        "usage_en": "Enter a value and save. Larger = longer replies, but slower and pricier"
    },
    "context_rounds": {
        "zh": "上下文轮数",
        "en": "Context Rounds",
        "desc": "传入 AI 的历史对话轮数。轮数越多上下文越丰富，但速度更慢费用更高",
        "desc_en": "Number of past dialogue rounds fed to the AI. More rounds = richer context but slower and pricier",
        "usage": "通常 3-5 轮即可。复杂业务场景可增至 8-10 轮",
        "usage_en": "3-5 rounds is usually enough; raise to 8-10 for complex scenarios"
    },
    "split_send": {
        "zh": "分条发送",
        "en": "Split Send",
        "desc": "长回复自动拆成多条消息发送，模拟真人打字节奏",
        "desc_en": "Long replies are auto-split into several messages, mimicking a human typing rhythm"
    },
    "reply_probability": {
        "zh": "回复概率",
        "en": "Reply Probability",
        "desc": "收到此类消息时实际回复的概率。0.1=只有10%的消息会回复",
        "desc_en": "Probability of actually replying to such a message. 0.1 = only 10% of messages get a reply",
        "usage": "设为 1.0 表示 100% 回复。水群场景可调低避免刷屏",
        "usage_en": "Set 1.0 for a 100% reply rate. Lower it in busy group chats to avoid flooding"
    },
    "skip_ai": {
        "zh": "跳过 AI",
        "en": "Skip AI",
        "desc": "启用后直接使用模板回复，不调用 AI 接口，响应极快（<1秒）",
        "desc_en": "When enabled, replies straight from templates without calling the AI API — extremely fast (<1s)",
        "usage": "适用于固定话术场景（如问候语），开启后节省 API 费用",
        "usage_en": "Good for fixed scripts (e.g. greetings); saves API cost"
    },
    "success_rate": {
        "zh": "成功率",
        "en": "Success Rate",
        "desc": "该通道交易成功的比例。低于 80% 建议排查或暂停",
        "desc_en": "Share of successful transactions on this channel. Below 80% warrants investigation or pausing"
    },
    "alert_threshold": {
        "zh": "告警阈值",
        "en": "Alert Threshold",
        "desc": "成功率低于此值时通道卡片显示红色告警",
        "desc_en": "When the success rate drops below this, the channel card shows a red alert"
    },
    "P50": {
        "zh": "响应中位数（P50）",
        "en": "Response Median (P50)",
        "desc": "50% 的请求在此时间内完成。反映典型用户体验",
        "desc_en": "50% of requests finish within this time. Reflects typical user experience"
    },
    "P90": {
        "zh": "响应 P90",
        "en": "Response P90",
        "desc": "90% 的请求在此时间内完成。反映大多数用户的最慢体验",
        "desc_en": "90% of requests finish within this time. Reflects most users' slowest experience"
    },
    "P99": {
        "zh": "响应 P99",
        "en": "Response P99",
        "desc": "99% 的请求在此时间内完成。用于发现极端慢请求",
        "desc_en": "99% of requests finish within this time. Used to surface extreme slow requests"
    },
    "SSE": {
        "zh": "服务端推送（SSE）",
        "en": "Server-Sent Events (SSE)",
        "desc": "Server-Sent Events，服务器实时推送日志到浏览器，无需手动刷新",
        "desc_en": "Server-Sent Events: the server pushes logs to the browser in real time, no manual refresh needed"
    },
    "minimum_amount": {
        "zh": "最小金额",
        "en": "Minimum Amount",
        "desc": "该通道允许的单笔最小交易金额",
        "desc_en": "The smallest single-transaction amount this channel allows"
    },
    "maximum_amount": {
        "zh": "最大金额",
        "en": "Maximum Amount",
        "desc": "该通道允许的单笔最大交易金额",
        "desc_en": "The largest single-transaction amount this channel allows"
    },
    "processing_time": {
        "zh": "处理时间",
        "en": "Processing Time",
        "desc": "从提交到完成的平均处理耗时",
        "desc_en": "Average time from submission to completion"
    },
    "per_user": {
        "zh": "用户冷却",
        "en": "Per-User Cooldown",
        "desc": "同一用户两次触发回复的最小间隔",
        "desc_en": "Minimum interval between two reply triggers from the same user"
    },
    "per_content": {
        "zh": "内容冷却",
        "en": "Per-Content Cooldown",
        "desc": "相同内容重复发送时的冷却时间",
        "desc_en": "Cooldown applied when identical content is sent repeatedly"
    },
    "global": {
        "zh": "全局冷却",
        "en": "Global Cooldown",
        "desc": "所有回复之间的最小间隔",
        "desc_en": "Minimum interval between all replies"
    },
    "by_intent": {
        "zh": "意图冷却",
        "en": "Per-Intent Cooldown",
        "desc": "按意图类型分别设置的冷却时间",
        "desc_en": "Cooldown configured separately by intent type"
    },
    "enabled": {
        "zh": "已启用",
        "en": "Enabled",
        "desc": "此功能/通道当前处于开启状态",
        "desc_en": "This feature/channel is currently switched on"
    },
    "rate_limit": {
        "zh": "限流",
        "en": "Rate Limiting",
        "desc": "防止短时间内过多请求的保护机制，基于令牌桶算法",
        "desc_en": "A protection mechanism preventing too many requests in a short time, based on the token-bucket algorithm"
    },
    "webhook": {
        "zh": "Webhook 推送通知",
        "en": "Webhook Notifications",
        "desc": "配置变更时自动推送通知到外部系统（Slack/微信/Telegram）",
        "desc_en": "Auto-push notifications to external systems (Slack/WeChat/Telegram) when config changes",
        "usage": "填写推送地址 → 选择监听事件 → 保存 → 发送测试验证",
        "usage_en": "Enter the push URL → choose events to watch → save → send a test to verify"
    },
    "intent": {
        "zh": "意图",
        "en": "Intent",
        "desc": "用户消息被 AI 识别为的类型，如 greeting（问候）、order_query（查单）、complaint（投诉）等",
        "desc_en": "The type the AI classifies a user message as, e.g. greeting, order_query, complaint, etc."
    },
    "strategy": {
        "zh": "回复策略",
        "en": "Reply Strategy",
        "desc": "根据消息意图选择的 AI 参数组合（温度、输出长度、上下文轮数等）",
        "desc_en": "The AI parameter set (temperature, output length, context rounds, etc.) chosen by message intent"
    },
    "follow_up_rate": {
        "zh": "追问率",
        "en": "Follow-up Rate",
        "desc": "用户在收到 AI 回复后 5 分钟内再次发送消息的比例；高追问率可能表示回复质量不足",
        "desc_en": "Share of users who message again within 5 minutes of an AI reply; a high rate may signal insufficient reply quality"
    },
    "same_intent_rate": {
        "zh": "同意图追问率",
        "en": "Same-Intent Follow-up Rate",
        "desc": "追问消息与原始消息具有相同意图的比例；越高说明 AI 可能没解决用户问题",
        "desc_en": "Share of follow-ups carrying the same intent as the original; higher means the AI likely didn't solve the problem"
    },
    "silence_rate": {
        "zh": "静默率",
        "en": "Silence Rate",
        "desc": "回复后用户无追问的比例；高静默率通常表示一次解决",
        "desc_en": "Share of replies with no follow-up; a high rate usually means one-shot resolution"
    },
    "template_hit_rate": {
        "zh": "模板命中率",
        "en": "Template Hit Rate",
        "desc": "使用预设模板直接回复（未调用 AI API）的比例；可节省 API 调用",
        "desc_en": "Share of replies served directly from preset templates (no AI API call); saves API calls"
    },
    "backfill": {
        "zh": "回填",
        "en": "Backfill",
        "desc": "当用户新消息到达时，系统回溯标记前一条 AI 回复的追问状态",
        "desc_en": "When a new user message arrives, the system retroactively marks the previous AI reply's follow-up status"
    },
    "strategy_event": {
        "zh": "策略事件",
        "en": "Strategy Event",
        "desc": "每次 AI 通过某个策略回复用户时记录的一条追踪数据",
        "desc_en": "A tracking record logged each time the AI replies via a given strategy"
    },
    "quality_score": {
        "zh": "质量评分",
        "en": "Quality Score",
        "desc": "综合评分 0-100，融合响应速度(20%)、一次解决率(35%)、同意图追问惩罚(30%)、API 效率(15%)",
        "desc_en": "Composite score 0-100, blending response speed (20%), one-shot resolution (35%), same-intent follow-up penalty (30%) and API efficiency (15%)"
    },
    "ab_test": {
        "zh": "A/B 灰度测试",
        "en": "A/B Test",
        "desc": "对同一意图分流多个策略，用一致性哈希保证同一用户始终进入同一分桶，实现对照实验",
        "desc_en": "Splits one intent across multiple strategies, using consistent hashing so a user always lands in the same bucket — a controlled experiment",
        "usage": "创建两个策略 → 在意图映射中同时绑定 → 系统自动分流 → 查看策略效果对比",
        "usage_en": "Create two strategies → bind both in the intent mapping → the system auto-splits → compare in Strategy Performance"
    },
    "data_retention": {
        "zh": "数据保留",
        "en": "Data Retention",
        "desc": "策略追踪事件和通用事件的自动清理周期（天数），防止数据库无限膨胀",
        "desc_en": "Auto-cleanup period (days) for strategy-tracking and generic events, preventing unbounded database growth"
    },
    "advisor": {
        "zh": "智能诊断",
        "en": "Smart Diagnostics",
        "desc": "系统自动分析策略指标，检测异常（高追问率、慢响应等）并给出参数调整建议",
        "desc_en": "Auto-analyzes strategy metrics, detects anomalies (high follow-up rate, slow response, etc.) and suggests parameter tweaks"
    },
    "purge": {
        "zh": "数据清理",
        "en": "Data Purge",
        "desc": "删除超过保留期限的旧追踪数据并回收磁盘空间（VACUUM）",
        "desc_en": "Deletes tracking data past its retention period and reclaims disk space (VACUUM)"
    },
    "autopilot": {
        "zh": "自动驾驶（Auto-Pilot）",
        "en": "Auto-Pilot",
        "desc": "系统持续监测策略质量评分，当某策略评分低于阈值时自动将其映射的意图切换到更优策略",
        "desc_en": "Continuously monitors strategy quality scores and auto-switches an intent's mapping to a better strategy when a score drops below threshold"
    },
    "session": {
        "zh": "会话",
        "en": "Session",
        "desc": "同一用户在 30 分钟内的连续交互归为一个会话，用于追踪整体对话解决率",
        "desc_en": "A user's continuous interactions within 30 minutes count as one session, used to track overall resolution"
    },
    "resolve_rate": {
        "zh": "解决率",
        "en": "Resolution Rate",
        "desc": "会话中最后一条回复后用户未再追问的比例；高解决率意味着问题一次性解决",
        "desc_en": "Share of sessions with no follow-up after the last reply; high means problems were solved in one go"
    },
    "param_suggestion": {
        "zh": "参数微调建议",
        "en": "Parameter Tuning Suggestion",
        "desc": "系统根据策略指标异常自动生成具体参数调整建议（如增加 context_rounds），可一键应用",
        "desc_en": "Concrete parameter-adjustment suggestions auto-generated from metric anomalies (e.g. raise context_rounds), applicable in one click"
    },
    "model_id": {
        "zh": "模型 ID",
        "en": "Model ID",
        "desc": "当前策略使用的 AI 模型名称，可在策略中配置以实现模型级 A/B 测试",
        "desc_en": "The AI model name used by the current strategy; configurable per strategy for model-level A/B testing"
    },
    "model_ab": {
        "zh": "模型 A/B 对比",
        "en": "Model A/B Comparison",
        "desc": "将相同策略配置不同模型，通过 A/B 灰度分流对比模型效果差异",
        "desc_en": "Configure different models for the same strategy and compare their effect via A/B traffic splitting"
    },
    "user_segment": {
        "zh": "用户分群",
        "en": "User Segments",
        "desc": "按活跃度分群：高频(≥10条)、中频(3-9条)、低频(1-2条)，分析各群体对策略的响应差异",
        "desc_en": "Segments by activity: heavy (≥10 msgs), moderate (3-9), light (1-2), to analyze how each group responds to strategies"
    },
    "heavy": {
        "zh": "高频用户",
        "en": "Heavy Users",
        "desc": "分析窗口内产生 ≥10 条交互的用户群体，通常是核心用户",
        "desc_en": "Users with ≥10 interactions in the analysis window — usually core users"
    },
    "moderate": {
        "zh": "中频用户",
        "en": "Moderate Users",
        "desc": "分析窗口内产生 3-9 条交互的用户群体",
        "desc_en": "Users with 3-9 interactions in the analysis window"
    },
    "light": {
        "zh": "低频用户",
        "en": "Light Users",
        "desc": "分析窗口内仅 1-2 条交互的用户群体，可能是新用户或偶尔使用者",
        "desc_en": "Users with only 1-2 interactions in the analysis window — possibly new or occasional users"
    },
    # ── AI / 配置术语 ───
    "api_key": {
        "zh": "API 密钥（API Key）",
        "en": "API Key",
        "desc": "调用 AI 服务的身份验证密钥，类似密码，请妥善保管不要泄露",
        "desc_en": "Authentication key for calling the AI service, like a password — keep it safe and never leak it",
        "usage": "从 AI 服务商后台复制密钥 → 粘贴到此处 → 点击测试连接验证",
        "usage_en": "Copy the key from your AI provider's console → paste it here → click Test Connection to verify"
    },
    "base_url": {
        "zh": "接口地址（Base URL）",
        "en": "Base URL",
        "desc": "AI 服务的 API 访问地址。不同服务商地址不同",
        "desc_en": "The AI service's API endpoint. It differs by provider",
        "usage": "填写完整 URL（含 https://），如 https://generativelanguage.googleapis.com",
        "usage_en": "Enter the full URL (incl. https://), e.g. https://generativelanguage.googleapis.com"
    },
    "embedding_model": {
        "zh": "向量模型（Embedding）",
        "en": "Embedding Model",
        "desc": "将文本转换为数学向量的模型，用于知识库语义搜索（理解意思而非精确匹配）",
        "desc_en": "A model that turns text into mathematical vectors, used for Knowledge Base semantic search (understanding meaning, not exact matching)",
        "usage": "通常使用默认模型即可。如需更换，确保模型支持中文",
        "usage_en": "The default model is usually fine. If you change it, make sure it supports Chinese"
    },
    "system_prompt": {
        "zh": "系统提示词（System Prompt）",
        "en": "System Prompt",
        "desc": "AI 的核心指令，定义 AI 的角色、风格、回复规则。所有回复都受此约束",
        "desc_en": "The AI's core instruction, defining its role, style and reply rules. Every reply is bound by it",
        "usage": "编辑提示词 → 保存即生效。可用右侧快捷按钮跳转到不同段落",
        "usage_en": "Edit the prompt → save to apply. Use the side buttons to jump between sections"
    },
    "thinking_budget": {
        "zh": "思考预算（Thinking Budget）",
        "en": "Thinking Budget",
        "desc": "AI 深度推理的 token 预算，越高推理越深入但速度越慢、费用更高",
        "desc_en": "Token budget for the AI's deep reasoning; higher = deeper reasoning but slower and pricier",
        "usage": "简单问答设 0（关闭推理），复杂业务场景设 1024-4096",
        "usage_en": "Set 0 for simple Q&A (reasoning off); 1024-4096 for complex scenarios"
    },
    "session_key": {
        "zh": "会话密钥（Session Key）",
        "en": "Session Key",
        "desc": "Web 管理面板的登录会话加密密钥，用于保护 Cookie 安全",
        "desc_en": "Encryption key for the web admin login session, protecting cookie security",
        "usage": "初始化时自动生成，无需手动修改。如需重置登录状态可更换",
        "usage_en": "Auto-generated at init; no manual change needed. Rotate it to reset all login states"
    },
    # ── 知识库术语 ───
    "kb_entry": {
        "zh": "知识条目",
        "en": "Knowledge Entry",
        "desc": "知识库中的一条记录，包含标题、触发词、处理步骤、示例回复等字段",
        "desc_en": "A Knowledge Base record with fields like title, triggers, handling steps and a sample reply",
        "usage": "创建条目 → 设置触发词 → 用沙盒测试命中效果",
        "usage_en": "Create an entry → set triggers → verify hits with the sandbox test"
    },
    "triggers": {
        "zh": "触发词",
        "en": "Trigger Words",
        "desc": "用户消息匹配这些关键词时会命中对应的知识条目。支持多个词，用逗号分隔",
        "desc_en": "When a user message matches these keywords, the matching entry is hit. Multiple words allowed, comma-separated",
        "usage": "添加常见的用户提问关键词，如「查单」「订单号」「到账」",
        "usage_en": "Add common user-question keywords, e.g. \"track order\", \"order number\", \"received\""
    },
    "sandbox": {
        "zh": "沙盒测试",
        "en": "Sandbox Test",
        "desc": "在安全环境中测试 KB 搜索和 AI 回复效果，不影响真实用户",
        "desc_en": "Test KB search and AI replies in a safe environment without affecting real users",
        "usage": "输入模拟用户消息 → 查看命中的条目和 AI 生成的回复",
        "usage_en": "Enter a simulated user message → see the matched entry and the AI-generated reply"
    },
    "vectorize": {
        "zh": "向量化",
        "en": "Vectorize",
        "desc": "将所有知识条目转为数学向量，启用语义搜索（理解含义而非仅关键词匹配）",
        "desc_en": "Convert all knowledge entries into vectors to enable semantic search (meaning, not just keyword matching)",
        "usage": "点击后等待处理完成。新增条目后建议重新向量化",
        "usage_en": "Click and wait for processing. Re-vectorize after adding new entries"
    },
    "batch_translate": {
        "zh": "批量翻译",
        "en": "Batch Translate",
        "desc": "将所有未翻译的 KB 条目自动翻译为英语/乌尔都语/葡萄牙语/阿拉伯语",
        "desc_en": "Auto-translate all untranslated KB entries into English/Urdu/Portuguese/Arabic",
        "usage": "点击后自动翻译，翻译结果可在「翻译审核」标签页中审核",
        "usage_en": "Click to auto-translate; review the results in the \"Translation Review\" tab"
    },
    "hit_rate": {
        "zh": "命中率",
        "en": "Hit Rate",
        "desc": "用户消息能匹配到 KB 知识条目的比例。命中率越高，AI 回复质量越好",
        "desc_en": "Share of user messages that match a KB entry. Higher hit rate = better AI reply quality",
        "usage": "命中率低于 60% 时建议扩充知识库或优化触发词",
        "usage_en": "Below 60%, consider expanding the KB or refining triggers"
    },
    "miss_log": {
        "zh": "未命中记录",
        "en": "Miss Log",
        "desc": "用户消息没有匹配到任何 KB 条目时的记录，用于发现知识盲区",
        "desc_en": "Records of user messages that matched no KB entry, used to find knowledge gaps",
        "usage": "定期查看未命中记录 → 为高频未命中创建新的知识条目",
        "usage_en": "Review misses regularly → create new entries for frequent misses"
    },
    "kb_feedback": {
        "zh": "效果反馈",
        "en": "Feedback",
        "desc": "对 AI 回复的评价记录（好评/差评），用于持续优化知识库内容",
        "desc_en": "Records of ratings (thumbs up/down) on AI replies, used to continuously improve KB content"
    },
    "bm25": {
        "zh": "BM25 搜索",
        "en": "BM25 Search",
        "desc": "基于关键词频率的传统搜索算法，速度快但无法理解语义",
        "desc_en": "A traditional keyword-frequency search algorithm — fast but unable to understand semantics"
    },
    "semantic_search": {
        "zh": "语义搜索",
        "en": "Semantic Search",
        "desc": "基于向量的搜索，能理解同义词和相似表达（如「退钱」=「退款」）",
        "desc_en": "Vector-based search that understands synonyms and similar phrasing (e.g. \"give my money back\" = \"refund\")"
    },
    # ── 日志级别 ───
    "DEBUG": {
        "zh": "调试日志（DEBUG）",
        "en": "Debug Log (DEBUG)",
        "desc": "最详细的日志级别，包含开发调试信息。通常仅开发人员使用",
        "desc_en": "The most detailed log level, including dev-debug info. Usually for developers only",
        "usage": "选中此按钮可查看所有级别的日志，信息量最大",
        "usage_en": "Select this to see logs of all levels — the most verbose"
    },
    "INFO": {
        "zh": "信息日志（INFO）",
        "en": "Info Log (INFO)",
        "desc": "常规运行信息，如消息收发、回复生成、KB 查询等正常操作",
        "desc_en": "Routine runtime info such as message send/receive, reply generation and KB queries — normal operations",
        "usage": "日常监控推荐使用此级别",
        "usage_en": "Recommended for day-to-day monitoring"
    },
    "WARNING": {
        "zh": "警告日志（WARNING）",
        "en": "Warning Log (WARNING)",
        "desc": "潜在问题警告，如成功率下降、KB 未命中、接口超时等",
        "desc_en": "Warnings of potential issues, e.g. dropping success rate, KB misses, API timeouts",
        "usage": "重点关注此级别，可提前发现问题",
        "usage_en": "Watch this level closely to catch problems early"
    },
    "ERROR": {
        "zh": "错误日志（ERROR）",
        "en": "Error Log (ERROR)",
        "desc": "运行错误，需要关注但系统仍可运行。如 API 调用失败、数据异常等",
        "desc_en": "Runtime errors needing attention while the system still runs, e.g. API call failures, data anomalies",
        "usage": "出现 ERROR 时应检查原因并尽快修复",
        "usage_en": "On ERROR, investigate the cause and fix it promptly"
    },
    "CRITICAL": {
        "zh": "严重错误（CRITICAL）",
        "en": "Critical Error (CRITICAL)",
        "desc": "系统级故障，如数据库连接断开、服务崩溃等，需要立即处理",
        "desc_en": "System-level failures such as a dropped database connection or a crashed service — handle immediately",
        "usage": "CRITICAL 日志出现时应立即排查，可能影响所有用户",
        "usage_en": "When CRITICAL appears, investigate at once; it may affect all users"
    },
    # ── Case / 意图链 ───
    "case_id": {
        "zh": "案例 ID",
        "en": "Case ID",
        "desc": "系统自动为识别到的意图链模式分配的唯一追踪编号",
        "desc_en": "A unique tracking number the system assigns to each detected intent-chain pattern"
    },
    "intent_chain": {
        "zh": "意图链",
        "en": "Intent Chain",
        "desc": "用户在对话中的意图变化轨迹，如「查单 → 投诉 → 退款」",
        "desc_en": "The trajectory of a user's intent changes in a conversation, e.g. \"track order → complaint → refund\"",
        "usage": "通过意图链可判断用户问题是否在升级，及时干预",
        "usage_en": "An intent chain shows whether a user's issue is escalating, so you can step in early"
    },
    "satisfaction": {
        "zh": "满意度",
        "en": "Satisfaction",
        "desc": "基于用户行为（追问、投诉、催促等）实时计算的满意度评分，范围 0-100",
        "desc_en": "A real-time satisfaction score (0-100) computed from user behavior (follow-ups, complaints, prompting, etc.)",
        "usage": "低于 40 分标记为高风险，建议人工介入",
        "usage_en": "Below 40 is flagged high-risk; human intervention is advised"
    },
    "at_risk": {
        "zh": "高风险",
        "en": "At Risk",
        "desc": "满意度评分低于阈值的用户，可能即将流失或投诉",
        "desc_en": "Users whose satisfaction score is below threshold, who may churn or complain soon",
        "usage": "在 Case 列表中带红色标记的即为高风险用户",
        "usage_en": "Users flagged red in the case list are high-risk"
    },
    "escalation": {
        "zh": "人工升级",
        "en": "Escalation",
        "desc": "AI 判断无法解决用户问题时，自动标记为需要人工客服介入",
        "desc_en": "When the AI judges it can't solve the issue, it auto-flags the case for a human agent"
    },
    "close_case": {
        "zh": "结案",
        "en": "Close Case",
        "desc": "标记案例为已解决，并记录解决方案",
        "desc_en": "Mark a case as resolved and record the solution",
        "usage": "点击结案按钮 → 输入解决说明 → 确认",
        "usage_en": "Click Close → enter the resolution note → confirm"
    },
    # ── 数据分析 ───
    "copilot": {
        "zh": "运营 Copilot",
        "en": "Operations Copilot",
        "desc": "AI 助手，用自然语言查询内部运营数据。支持问「今天知识库命中率多少？」等问题",
        "desc_en": "An AI assistant for querying internal operations data in natural language. Ask things like \"What's today's KB hit rate?\"",
        "usage": "在输入框输入中文问题 → 按回车 → 等待 AI 分析并返回结果",
        "usage_en": "Type a question in the box → press Enter → wait for the AI to analyze and return results"
    },
    # ── 按钮操作 ───
    "btn_refresh": {
        "zh": "刷新",
        "en": "Refresh",
        "desc": "重新从服务器加载最新数据",
        "desc_en": "Reload the latest data from the server",
        "usage": "点击后等待数据更新。页面数据通常每 30 秒自动刷新",
        "usage_en": "Click and wait for the update. Page data usually auto-refreshes every 30s"
    },
    "btn_save": {
        "zh": "保存",
        "en": "Save",
        "desc": "将当前修改保存到服务器，立即生效",
        "desc_en": "Save the current changes to the server, effective immediately",
        "usage": "修改完成后点击保存。也可使用快捷键 Ctrl+S",
        "usage_en": "Click Save when done. You can also use the Ctrl+S shortcut"
    },
    "btn_export": {
        "zh": "导出",
        "en": "Export",
        "desc": "将当前数据导出为文件（CSV/JSON/YAML）下载到本地",
        "desc_en": "Export the current data as a file (CSV/JSON/YAML) to download locally",
        "usage": "点击后浏览器自动下载文件",
        "usage_en": "Click and the browser downloads the file automatically"
    },
    "btn_import": {
        "zh": "导入",
        "en": "Import",
        "desc": "从本地文件导入数据到系统",
        "desc_en": "Import data into the system from a local file",
        "usage": "选择文件 → 确认导入模式（覆盖/合并） → 确认",
        "usage_en": "Choose a file → confirm the import mode (overwrite/merge) → confirm"
    },
    "btn_test": {
        "zh": "测试",
        "en": "Test",
        "desc": "验证当前配置是否能正常工作",
        "desc_en": "Verify whether the current configuration works",
        "usage": "点击后等待测试结果，成功会显示绿色提示",
        "usage_en": "Click and wait for the result; success shows a green prompt"
    },
    "btn_delete": {
        "zh": "删除",
        "en": "Delete",
        "desc": "永久删除此项，操作不可恢复",
        "desc_en": "Permanently delete this item; the action cannot be undone",
        "usage": "点击后需要二次确认才会执行",
        "usage_en": "A second confirmation is required before it executes"
    },
    "btn_pause": {
        "zh": "暂停/继续",
        "en": "Pause / Resume",
        "desc": "暂停实时数据流，方便查看当前内容",
        "desc_en": "Pause the live data stream to inspect current content",
        "usage": "暂停后数据不再滚动，再次点击恢复",
        "usage_en": "When paused, data stops scrolling; click again to resume"
    },
    # ── 导入/导出格式 ───
    "JSON": {
        "zh": "JSON 格式",
        "en": "JSON Format",
        "desc": "通用数据格式，结构化存储，适合程序处理和 API 传输",
        "desc_en": "A universal, structured data format suited to programmatic processing and API transport"
    },
    "CSV": {
        "zh": "CSV 格式",
        "en": "CSV Format",
        "desc": "逗号分隔表格格式，可直接用 Excel/WPS 打开编辑",
        "desc_en": "Comma-separated tabular format, openable and editable directly in Excel/WPS"
    },
    "YAML": {
        "zh": "YAML 格式",
        "en": "YAML Format",
        "desc": "人类友好的配置文件格式，层级清晰易读",
        "desc_en": "A human-friendly config file format with a clear, readable hierarchy"
    },
    "ZIP": {
        "zh": "ZIP 压缩包",
        "en": "ZIP Archive",
        "desc": "将多个文件打包压缩为一个文件，方便传输和备份",
        "desc_en": "Bundles multiple files into one compressed file for easy transfer and backup"
    },
    "HMAC": {
        "zh": "HMAC-SHA256 签名",
        "en": "HMAC-SHA256 Signature",
        "desc": "消息认证码算法，用于验证 Webhook 推送消息的真实性，防止伪造",
        "desc_en": "A message-authentication-code algorithm used to verify the authenticity of Webhook push messages and prevent forgery"
    },
    # ── 通道状态 ───
    "ch_enabled": {
        "zh": "启用",
        "en": "Enabled",
        "desc": "通道正常运行中，可以处理交易",
        "desc_en": "The channel is running normally and can process transactions"
    },
    "ch_maintenance": {
        "zh": "维护中",
        "en": "Under Maintenance",
        "desc": "通道暂停服务，正在维护。AI 会告知用户通道维护状态",
        "desc_en": "The channel is paused for maintenance. The AI will tell users about the maintenance status"
    },
    "ch_fluctuation": {
        "zh": "波动",
        "en": "Fluctuating",
        "desc": "通道成功率不稳定，可能影响交易。AI 会提醒用户注意风险",
        "desc_en": "The channel's success rate is unstable and may affect transactions. The AI will warn users of the risk"
    },
    "zalo_7d_window": {
        "zh": "7 天互动窗",
        "en": "7-day messaging window",
        "desc": "Zalo OA 客服消息政策：用户最后一次互动后 7 天内可主动给 TA 发消息，超过窗口需等对方再次发起会话。不是本系统限制，是平台规则",
        "desc_en": "Zalo OA customer-service policy: you may message a user within 7 days of their last interaction; after that you must wait for them to message first. A platform rule, not a limit of this system",
        "usage": "接入 Zalo 官方渠道后留意回复时效；窗口内尽快回复可避免会话失联",
        "usage_en": "After connecting the Zalo official channel, reply within the window to avoid losing the conversation"
    },
    # ── 用户角色 ───
    "role_master": {
        "zh": "主帐号",
        "en": "Master",
        "desc": "拥有全部权限，可管理其他用户、修改系统设置、查看所有数据",
        "desc_en": "Has full permissions: manage other users, change system settings, and view all data"
    },
    "role_admin": {
        "zh": "管理员",
        "en": "Admin",
        "desc": "拥有编辑权限，可管理模板、通道、知识库，但不能管理用户和系统设置",
        "desc_en": "Has edit permissions: manage templates, channels and the knowledge base, but not users or system settings"
    },
    "role_viewer": {
        "zh": "观察员",
        "en": "Viewer",
        "desc": "只读权限，可查看所有数据但不能修改任何配置",
        "desc_en": "Read-only: can view all data but cannot modify any configuration"
    },
    # ── brand / site_name ───
    "bot_admin": {
        "zh": "无界科技 · 智聊",
        "en": "Boundless · ChatX",
        "desc": "面向 Telegram 等渠道的 AI 客服与人工协同后台，用于知识库、案例、转接与监控",
        "desc_en": "An AI customer-service and human-collaboration backend for channels like Telegram, covering knowledge base, cases, handoff and monitoring"
    },
    # ── 其他 ───
    "token": {
        "zh": "Token（令牌）",
        "en": "Token",
        "desc": "AI 处理文本的基本单位。1 个 token 约等于 1.5 个中文字或 0.75 个英文单词",
        "desc_en": "The basic unit the AI processes text in. 1 token ≈ 1.5 Chinese characters or 0.75 English words"
    },
    "Bot": {
        "zh": "Bot（机器人）",
        "en": "Bot",
        "desc": "Telegram 上的 AI 客服机器人，自动接收并回复用户消息",
        "desc_en": "The AI customer-service bot on Telegram that automatically receives and replies to user messages"
    },
    "Case": {
        "zh": "Case（案例/工单）",
        "en": "Case",
        "desc": "由系统自动识别的用户对话升级事件，需要运营关注和跟进",
        "desc_en": "A user-conversation escalation event auto-detected by the system, needing ops attention and follow-up"
    },
    # ── 群脉导播台（group_show）：只经 data-help 显式挂载，不劫持通用词的全局精确匹配 ──
    "gs_softad": {
        "zh": "软广强度",
        "en": "Soft-ad level",
        "desc": "把产品带进对话的力度，0=只闲聊完全不提，数字越大越明示。剧本里前低后高再回落最像真人；* 表示这一拍单独调过、覆盖了剧本默认值",
        "desc_en": "How hard the product is woven into the chat: 0 = pure chit-chat, higher = more explicit. Low-then-high-then-ease reads most human; * means this beat overrides the playbook default"
    },
    "gs_pace": {
        "zh": "语速档",
        "en": "Pace",
        "desc": "这一拍的发言节奏：chatty=你一言我一语抢着说，normal=常规群聊，slow=有人在思考或刚看到。节奏本身也会出卖机器人，故按拍设定",
        "desc_en": "This beat's rhythm: chatty = fast back-and-forth, normal = regular chat, slow = someone thinking or just noticing. Rhythm itself can betray a bot, so it's set per beat"
    },
    "gs_beatcount": {
        "zh": "拍数",
        "en": "Beats",
        "desc": "这出戏由几拍组成。每一拍是一句节拍（谁说、什么意图），台词不写死、由人设 AI 现场生成",
        "desc_en": "How many beats the show has. Each beat is one step (who speaks, what intent); lines aren't fixed - the persona AI generates them live"
    },
    "gs_prodline": {
        "zh": "产品线",
        "en": "Product line",
        "desc": "这出戏软推的产品系：growth 获客系 / studio 内容陪伴系 / lingo 翻译系。选戏其实就是选产品",
        "desc_en": "Which product family the show seeds: growth (acquisition), studio (content companion), lingo (translation). Picking a show is picking a product"
    },
    "gs_naturalness": {
        "zh": "自然度",
        "en": "Naturalness",
        "desc": "这场戏像不像真人：绿=像真人，黄=各说各话，红=模板复读一眼假。看三个指标——熵（用词多样度）、间隔 CV（节奏像真人还是定时器）、均衡度（是不是一个号包场）",
        "desc_en": "How human the show looks: green = human, amber = talking past each other, red = robotic. Three metrics: entropy (word variety), interval CV (human rhythm vs timer), balance (does one account hog the floor)"
    },
    "gs_linkage": {
        "zh": "关联风险体检",
        "en": "Linkage risk check",
        "desc": "同一个群里同时能上几个号，取决于独立网络出口数——不是在线号数。同一出口下多个号一唱一和，是关联封号最典型的姿势。这里说的是你手上真号真发时的上限",
        "desc_en": "How many accounts can be in one group at once depends on independent network exits, not how many are online. Several accounts behind one exit is the classic linked-ban pattern. This is the ceiling for your real accounts"
    },
    "gs_attendance": {
        "zh": "出席矩阵",
        "en": "Attendance matrix",
        "desc": "管「谁进了哪些群」。同一批号同时混在同一批群里，是多号多群下最容易被抓的特征。给出每个群该派哪几个号，把任意两号的共同出席压到真人区间。加群一旦发生就冻结，退群更可疑",
        "desc_en": "Governs who is in which groups. The same accounts sharing the same groups is the top giveaway. It tells you which accounts to put in each group, keeping any pair's shared attendance human-like. Once joined it's frozen; leaving looks even more suspicious"
    },
    "gs_exposure": {
        "zh": "演出矩阵",
        "en": "Performance matrix",
        "desc": "管「谁开过口」。成员面加完就冻结了，这是之后唯一还能动的暴露面，而且平台是靠消息实时统计的。最强杠杆：每场少让一个号开口，能安全覆盖的群数是二次增长",
        "desc_en": "Governs who has spoken. Membership freezes once joined; this is the only exposure surface you can still adjust, and platforms count messages in real time. Biggest lever: one fewer speaker per show grows safe group coverage quadratically"
    },
    "gs_schedule": {
        "zh": "开演排期",
        "en": "Show schedule",
        "desc": "静态特征洗干净后，节奏是最后一条出卖人的轴：一批群挤在同一小时、精确每 30 分钟一场、每天同一时刻。这张管开演时间，和管加群时间的出席矩阵相互独立",
        "desc_en": "After static traits are cleaned, rhythm is the last axis that betrays: groups bunched in one hour, exactly every 30 min, the same time daily. This governs show times, independent of the attendance matrix that governs join times"
    },
    "gs_outcome": {
        "zh": "演出效果",
        "en": "Show outcomes",
        "desc": "唯一回答「值不值得继续投」的读数：真发场次在归因窗口内的群内反响（多少人接话）与私聊转化（群里发过言的人之后首次私聊我们）。排练不计——它一条消息都没发过",
        "desc_en": "The only reading that answers \"is this worth continuing\": in-group response (how many people replied) and DM conversions (people who spoke in the group and then DM'd us for the first time) within the attribution window. Rehearsals never count - they send nothing",
        "usage": "真发之后隔一个窗口回来看；转化数是下限，潜水观众无法归因",
        "usage_en": "Check back one window after going live; conversions are a floor - lurkers cannot be attributed"
    },
    "gs_livecheck": {
        "zh": "开演前体检",
        "en": "Pre-live check",
        "desc": "真发前最后一道体检，只体检不发一条消息。看选角/闸门预检是否通过、双锁是否武装、是否处于禁演时段。真发需另开配置锁 + 代码锁两把锁",
        "desc_en": "The final check before going live - it checks only, sends nothing. Shows whether casting/gate preflight passes, whether the double lock is armed, and quiet-hours status. Going live needs both the config lock and the code lock"
    },
    "rps_peer_budget": {
        "zh": "单会话额度",
        "en": "Per-conversation budget",
        "desc": "每个会话每天最多自动回复几轮。对面也是机器人时，没有这道保险丝会空转烧额度（曾实录 80 秒 78 轮）。0＝不限额。坐席手动发送不受限，次日自动恢复。",
        "desc_en": "Caps auto-reply rounds per conversation per day. Without this fuse, bot-vs-bot loops burn budget (a real incident: 78 rounds in 80 seconds). 0 = unlimited. Manual agent sends are never limited; the cap resets next day."
    },
    "rps_account_send_gate": {
        "zh": "单账号日发额度",
        "en": "Per-account daily send cap",
        "desc": "整个账号每天最多发多少条（含新号爬坡）。单号发太多会触发平台风控。与「单会话额度」正交：那道管对轰，这道管整号总量。运营在 overlay 里显式关掉后，一键全自动也不会重开。",
        "desc_en": "How many messages one account may send per day (new accounts ramp up). Too many on one account trips platform rate flags. Orthogonal to the per-conversation budget: that stops bot loops; this caps the whole account. If operators explicitly turn it off in the overlay, switching to full auto will not reopen it."
    },
    # ── v1.0.66 新功能词条（2026-09-01，实施93）：小智对话测试实锤这批功能
    #    零语料只能拒答——补齐后 seed_corpus 自动进帮助库。只写已验证行为。 ───
    "workflow_sop": {
        "zh": "工作链",
        "en": "Workflow SOP",
        "desc": "预设的多步跟进剧本（破冰/报价跟单/复购唤醒等），按天数间隔逐步推进；执行档分「拟稿人审」与「自动发送」，链推进期间 AI 普通回复自动让路",
        "desc_en": "Preset multi-step follow-up scripts (icebreak, quote follow-up, win-back) advanced on day intervals; runs in draft-review or auto-send mode, and normal AI replies yield while a chain is running",
        "usage": "坐席工作台右栏「工作链」组件给当前会话挂链/换链/停链；收件箱筛选面板可按筛选结果批量挂链；建链与预设包在「工作链」页（/workflows）",
        "usage_en": "Attach/switch/stop chains from the Workflows panel in the workspace right rail; bulk-attach from the inbox filter panel; create chains and seed preset packs on /workflows"
    },
    "deal_engine": {
        "zh": "成交引擎",
        "en": "Deal engine",
        "desc": "按会话内容识别客户旅程阶段（破冰→试探→报价→成交），给出下一步最优动作与建议跟进链，并在工作链页出成交看板；属服务端开关",
        "desc_en": "Classifies each conversation's journey stage (icebreak, probing, quoting, closing), suggests the next best action and chain, and adds a deal dashboard to the Workflows page; enabled server-side",
        "usage": "开启后建议自动出现在工作台右栏；「工作链」页看不到成交引擎卡＝当前部署未开启，请管理员启用",
        "usage_en": "Once enabled, suggestions appear in the right rail automatically; no deal-engine card on /workflows means this deployment has it off - ask an admin"
    },
    "voice_clone_bind": {
        "zh": "克隆音色并绑定人设",
        "en": "Clone voice & bind persona",
        "desc": "从客户语音消息一键克隆音色并绑到人设，之后该人设的语音回复即用克隆声；登记需勾选授权确认，完成页可直接试听/撤销",
        "desc_en": "Clone a voice from a customer's voice message and bind it to a persona; later voice replies use the clone. Registration requires a consent tick; the success screen offers audition and undo",
        "usage": "消息流语音行「⋮」→「克隆音色并绑定人设」；弹层只 ×/完成/Esc 可关，点外部不会误关（试听生成中安全）",
        "usage_en": "Voice row \"...\" menu → Clone voice & bind persona; the dialog closes only via X / Done / Esc - clicking outside never dismisses it mid-audition"
    },
    "ai_image_gen": {
        "zh": "AI 生成图片",
        "en": "AI image generation",
        "desc": "工具箱卡片：按人设/模式（自拍/物件）/场景现场生成图片，预览后发送到会话或存入相册；相册有匹配存货时先出缩略图零等待直发",
        "desc_en": "Toolbox card: generate images by persona, mode (selfie/object) and scene, preview, then send to the conversation or save to the album; matching album stock shows thumbnails for zero-wait sending",
        "usage": "「功能未启用」＝该部署没接出图算力（外网部署属正常）；引擎「未部署」置灰；「服务器不可达」黄条预警；失败出人话错误卡可复制诊断",
        "usage_en": "\"Not enabled\" means no image backend in this deployment (normal off-LAN); undeployed engines are greyed; \"server unreachable\" shows an amber banner; failures show an error card with diagnostic copy"
    },
    "media_ai_desc": {
        "zh": "AI 识图",
        "en": "AI vision summary",
        "desc": "客户发来的图片/视频自动识别成摘要，显示在媒体卡下方的折叠行（超长折 3 行）；是坐席内部参考，绝不以消息气泡出现、也不会发给客户",
        "desc_en": "Customer images and videos are auto-summarised in a folded row under the media card (long text folds to three lines); internal reference for agents - never rendered as a chat bubble, never sent to the customer",
        "usage": "点「展开全文」看完整识别；对图片点「问这张图」可就画面追问",
        "usage_en": "Click expand for the full text; use \"Ask about this image\" to probe the picture"
    },
    "voice_fallback_note": {
        "zh": "标准音色回落",
        "en": "Standard-voice fallback",
        "desc": "生成/发送语音未用克隆声时，语音面板会说明原因（该语言暂不支持克隆声/额度用尽/克隆通道暂不可用）并给出路，如一键关翻译重新生成",
        "desc_en": "When a voice is synthesized without the cloned timbre, the voice panel explains why (language unsupported by the clone, quota exhausted, clone channel down) and offers a way out, e.g. one-tap regenerate without translation",
        "usage": "看到黄条按提示操作；「标准音色」徽标＝本条不是人设克隆声",
        "usage_en": "Follow the amber note's suggestion; a \"standard voice\" badge means this clip is not the persona's cloned voice"
    },
    "birthday_capture": {
        "zh": "生日自动记忆",
        "en": "Birthday auto-capture",
        "desc": "客户明说生日会自动写入 AI 记忆——包括「今天是我的生日」这类没有具体日期的说法（按当天记）；AI 自己的反问不会被误记",
        "desc_en": "When a customer states their birthday it is captured into AI memory - including date-less phrasings like \"today is my birthday\" (recorded as today); the AI's own questions are never miscaptured",
        "usage": "「AI 记忆」页搜索该客户可查看/修正生日条目",
        "usage_en": "Search the customer on the AI Memory page to view or correct the birthday entry"
    }
}


def get_help_terms() -> dict:
    """供 admin.py 与渲染类测试注入模板上下文(静态数据,进程内单例)。"""
    return HELP_TERMS
